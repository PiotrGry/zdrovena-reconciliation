"""zdrovena.common.shipping_store – Shipping draft record storage.

Production: Azure Table Storage (table 'shippingdrafts')
  - PartitionKey = "drafts"
  - RowKey       = draft UUID
  - dict fields (shipping_address, parcel, receiver) JSON-serialized as strings

Local dev / tests: JSON file at ~/.zdrovena/storage/shipping-drafts.json
  - keyed by draft UUID, all fields native Python types
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

from zdrovena.common.exceptions import storage_unavailable

logger = logging.getLogger("zdrovena.common.shipping_store")

TABLE_NAME = "shippingdrafts"
#: Reading at least this many rows to answer one list is worth reporting.
_PARTITION_SCAN_ALARM_ROWS = 2_000

PARTITION_KEY = "drafts"
_LOCAL_FILE_NAME = "shipping-drafts.json"
_DEFAULT_ROOT = Path.home() / ".zdrovena" / "storage"

# Dead-letter queue for failed draft creation attempts (P1-9).
# Entries hold the original payload + last error and can be retried via a
# dedicated endpoint. Storage layout mirrors the drafts table.
DLQ_TABLE_NAME = "shippingdraftsdlq"
DLQ_PARTITION_KEY = "dlq"
_DLQ_LOCAL_FILE_NAME = "shipping-drafts-dlq.json"

# What failed, so the retry endpoint knows which operation to re-run. Entries
# written before this field existed deserialize with kind=None and are treated
# as creations, which is what they were.
DLQ_KIND_CREATION = "draft_creation"
DLQ_KIND_EXECUTION = "draft_execution"


def _table_endpoint(url: str) -> str:
    return url.replace(".blob.core.windows.net", ".table.core.windows.net")


def _serialize(record: dict[str, Any]) -> dict[str, Any]:
    entity: dict[str, Any] = {"PartitionKey": PARTITION_KEY, "RowKey": record["id"]}
    for k, v in record.items():
        if k == "id":
            continue
        if isinstance(v, (dict, list)):
            entity[k] = json.dumps(v, ensure_ascii=False)
        elif v is None:
            entity[k] = ""
        else:
            entity[k] = v
    return entity


def _deserialize(entity: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {"id": entity["RowKey"]}
    for k, v in entity.items():
        if k in ("PartitionKey", "RowKey", "etag", "Timestamp"):
            continue
        if isinstance(v, str):
            if v == "":
                v = None
            else:
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, (dict, list)):
                        v = parsed
                except (json.JSONDecodeError, ValueError):
                    pass
        record[k] = v
    return record


class ShippingStore:
    """Storage backend for shipping draft records."""

    def __init__(
        self,
        *,
        account_url: str | None = None,
        connection_string: str | None = None,
        local_root: Path | None = None,
    ) -> None:
        self._account_url = account_url
        self._connection_string = connection_string
        self._local_root = local_root or _DEFAULT_ROOT
        self._use_table = bool(account_url or connection_string)
        self._service: Any = None
        self._table_clients: dict[str, Any] = {}

    def _table_service(self) -> Any:
        """Build the service client once — it holds the credential and HTTP pool."""
        if self._service is not None:
            return self._service
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        if self._account_url:
            self._service = TableServiceClient(
                endpoint=_table_endpoint(self._account_url),
                credential=DefaultAzureCredential(),
            )
        else:
            if not self._connection_string:
                raise RuntimeError(
                    "ShippingStore: neither account_url nor connection_string is set"
                )
            self._service = TableServiceClient.from_connection_string(self._connection_string)
        return self._service

    def _cached_table(self, table_name: str) -> Any:
        """Return the table client for ``table_name``, creating the table once.

        create_table_if_not_exists is a network round-trip that answers 409 for
        an existing table, so calling it per operation is pure overhead.
        """
        cached = self._table_clients.get(table_name)
        if cached is None:
            cached = self._table_service().create_table_if_not_exists(table_name)
            self._table_clients[table_name] = cached
        return cached

    def _table_client(self) -> Any:
        return self._cached_table(TABLE_NAME)

    # ── Local fallback ─────────────────────────────────────────────────────────

    @property
    def _local_file(self) -> Path:
        path = self._local_root / _LOCAL_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def _lock_file(self) -> Path:
        path = self._local_root / f".{_LOCAL_FILE_NAME}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _local_load_unlocked(self) -> dict[str, Any]:
        if not self._local_file.exists():
            return {}
        try:
            return json.loads(self._local_file.read_text(encoding="utf-8"))
        except Exception:
            # Deliberate, and NOT the outage-looks-empty bug of #310: the local
            # JSON backend self-heals. TestOnDiskFormat pins it — a mangled file
            # reads as empty and the next upsert rewrites it, so a developer is
            # never stuck with a store they cannot use. Azure has no equivalent
            # recovery, which is why the table paths raise instead.
            return {}

    def _local_save_unlocked(self, data: dict[str, Any]) -> None:
        temp_fd, temp_path = tempfile.mkstemp(
            dir=str(self._local_root), prefix=".tmp-", suffix=".json"
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, str(self._local_file))
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _acquire_lock(self):
        lock_fd = os.open(str(self._lock_file), os.O_CREAT | os.O_RDWR)
        flock(lock_fd, LOCK_EX)
        return lock_fd

    def _release_lock(self, lock_fd):
        flock(lock_fd, LOCK_UN)
        os.close(lock_fd)

    def _local_load(self) -> dict[str, Any]:
        return self._local_load_unlocked()

    def _local_save(self, data: dict[str, Any]) -> None:
        return self._local_save_unlocked(data)

    # ── Public API ─────────────────────────────────────────────────────────────

    def upsert_draft(self, record: dict[str, Any]) -> None:
        if self._use_table:
            client = self._table_client()
            for existing in self._duplicate_rows_for(record):
                try:
                    client.delete_entity(existing["PartitionKey"], existing["RowKey"])
                except Exception as exc:
                    logger.warning(
                        "Table dedup delete failed for draft %s: %s", existing["RowKey"], exc
                    )
            try:
                client.upsert_entity(_serialize(record))
            except Exception as exc:
                logger.error("Table upsert failed for draft %s: %s", record.get("id"), exc)
                raise
        else:
            lock_fd = self._acquire_lock()
            try:
                data = self._local_load()
                for existing_id in self._local_duplicate_ids(data, record):
                    del data[existing_id]
                data[record["id"]] = record
                self._local_save(data)
            finally:
                self._release_lock(lock_fd)

    # ── Targeted order lookup (issue #316) ─────────────────────────────────────
    #
    # Table Storage has no secondary index: a filter on a non-key property is
    # still evaluated across the partition. What it does buy is completeness and
    # a small transfer -- the server returns only matching rows, and nothing is
    # truncated. That is the half that matters here. Building a dedup index out
    # of list_drafts() misses every row past its limit, so an order that exists
    # reads as new and is written again; the duplicate carries created_at=now,
    # enters the newest-first window, evicts another old row, and duplicates on
    # the next cycle. That loop produced roughly 70 Allegro drafts.

    @staticmethod
    def _quote(value: Any) -> str:
        return str(value).replace("'", "''")

    def _external_id_filter(self, source: str, external_order_id: str) -> str:
        return (
            f"PartitionKey eq '{PARTITION_KEY}' and "
            f"source eq '{self._quote(source)}' and "
            f"external_order_id eq '{self._quote(external_order_id)}'"
        )

    def _order_filters(self, record: dict[str, Any]) -> list[str]:
        """Every filter identifying "the same external order" as this record.

        Two keys, not one. ``(source, external_order_id)`` is the current shape
        and the only one that covers Allegro. ``shopify_order_id`` is kept for
        rows written before ``external_order_id`` existed: dropping it would
        quietly stop deduplicating exactly the oldest records, which is the
        failure this issue is about.
        """
        filters: list[str] = []
        source = record.get("source")
        external_order_id = record.get("external_order_id")
        if source and external_order_id:
            filters.append(self._external_id_filter(str(source), str(external_order_id)))
        shopify_order_id = record.get("shopify_order_id")
        if shopify_order_id:
            filters.append(
                f"PartitionKey eq '{PARTITION_KEY}' and "
                f"shopify_order_id eq '{self._quote(shopify_order_id)}'"
            )
        return filters

    def find_drafts_by_external_id(
        self, *, source: str, external_order_id: str
    ) -> list[dict[str, Any]]:
        """Every non-replacement draft for one external order. Never truncated.

        Replacements are excluded in Python rather than in the filter: most rows
        have no ``is_replacement`` property at all, and a Table Storage filter on
        an absent property matches nothing, so ``is_replacement eq false`` would
        silently return an empty set.
        """
        if not source or not external_order_id:
            return []

        if self._use_table:
            query_filter = self._external_id_filter(source, external_order_id)
            try:
                entities = list(self._table_client().query_entities(query_filter=query_filter))
            except Exception as exc:
                # Same rule as every other list read (#310): there is no
                # not-found case, so any exception is an outage. Answering
                # "no match" here would create a duplicate draft.
                raise storage_unavailable("shipping", "find_drafts_by_external_id", exc) from exc
            candidates = [_deserialize(dict(e)) for e in entities]
        else:
            candidates = [
                record
                for record in self._local_load().values()
                if record.get("source") == source
                and str(record.get("external_order_id", "")) == str(external_order_id)
            ]

        return [record for record in candidates if not record.get("is_replacement")]

    def find_draft_by_external_id(
        self, *, source: str, external_order_id: str
    ) -> dict[str, Any] | None:
        """The draft for one external order, preferring a non-errored one.

        A draft left in ``error`` is a failed attempt at the same order; callers
        want to retry it rather than create a second one, but a healthy draft
        always wins.
        """
        matches = self.find_drafts_by_external_id(
            source=source, external_order_id=external_order_id
        )
        if not matches:
            return None
        for record in matches:
            if record.get("status") != "error":
                return record
        return matches[0]

    def _duplicate_rows_for(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Rows for the same external order that this write supersedes.

        Keyed on (source, external_order_id), not on shopify_order_id as before:
        that field is None on every Allegro draft, so Allegro had no write-time
        protection at all. Replacement drafts share the external order id on
        purpose and must survive.
        """
        if record.get("is_replacement"):
            return []

        client = self._table_client()
        duplicates: dict[str, dict[str, Any]] = {}
        for query_filter in self._order_filters(record):
            try:
                entities = list(client.query_entities(query_filter=query_filter))
            except Exception as exc:
                # Best effort: failing the lookup must not fail the write. The
                # targeted read in find_draft_by_external_id is what prevents the
                # duplicate; this is the last-line cleanup.
                logger.warning("Table dedup lookup failed for draft %s: %s", record["id"], exc)
                continue
            for entity in entities:
                if entity["RowKey"] == record["id"] or entity.get("is_replacement"):
                    continue
                duplicates[str(entity["RowKey"])] = dict(entity)
        return list(duplicates.values())

    def _local_duplicate_ids(self, data: dict[str, Any], record: dict[str, Any]) -> list[str]:
        if record.get("is_replacement"):
            return []
        source = record.get("source")
        external_order_id = record.get("external_order_id")
        shopify_order_id = record.get("shopify_order_id")

        def is_same_order(existing: dict[str, Any]) -> bool:
            matches_external = bool(
                source
                and external_order_id
                and existing.get("source") == source
                and str(existing.get("external_order_id", "")) == str(external_order_id)
            )
            matches_shopify = bool(
                shopify_order_id and existing.get("shopify_order_id") == shopify_order_id
            )
            return matches_external or matches_shopify

        return [
            existing_id
            for existing_id, existing in data.items()
            if existing_id != record["id"]
            and not existing.get("is_replacement")
            and is_same_order(existing)
        ]

    def update_draft(self, draft_id: str, fields: dict[str, Any]) -> bool:
        """Merge-update specific fields of a draft. Returns False if not found (local only)."""
        if self._use_table:
            try:
                patch: dict[str, Any] = {"PartitionKey": PARTITION_KEY, "RowKey": draft_id}
                for k, v in fields.items():
                    if isinstance(v, (dict, list)):
                        patch[k] = json.dumps(v, ensure_ascii=False)
                    elif v is None:
                        patch[k] = ""
                    else:
                        patch[k] = v
                self._table_client().update_entity(patch, mode="merge")
                return True
            except Exception as exc:
                logger.error("Table update failed for draft %s: %s", draft_id, exc)
                raise
        else:
            lock_fd = self._acquire_lock()
            try:
                data = self._local_load()
                if draft_id not in data:
                    return False
                data[draft_id].update(fields)
                self._local_save(data)
                return True
            finally:
                self._release_lock(lock_fd)

    def try_claim_execution(self, draft_id: str) -> bool:
        """Atomically claim a draft for execution (R5-A).

        Moves the draft ``status`` to ``executing`` only if its current status is
        an executable one (``pending`` / ``pending_confirmation`` / ``error``) and
        it has not changed underneath us (ETag ``IfNotModified``). Returns True if
        this call won the claim, False if the draft is missing, in a
        non-executable state (already ``executing`` / ``created`` / ``cancelled`` /
        ``needs_review``), or a concurrent request won the race.

        Claiming *before* the courier call closes the check-then-act race where
        two concurrent execute requests both pass a ``status != "created"`` check
        and both create a shipment.
        """
        from zdrovena.common.shipping_state import EXECUTING, can_execute

        execution_started_at = datetime.now(timezone.utc).isoformat()

        if self._use_table:
            client = self._table_client()
            try:
                entity = client.get_entity(partition_key=PARTITION_KEY, row_key=draft_id)
            except Exception:
                return False
            if not can_execute(entity.get("status")):
                return False
            patch = {
                "PartitionKey": PARTITION_KEY,
                "RowKey": draft_id,
                "status": EXECUTING,
                "execution_started_at": entity.get("execution_started_at") or execution_started_at,
            }
            try:
                from azure.core import MatchConditions

                client.update_entity(
                    patch,
                    mode="merge",
                    etag=entity.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return True
            except Exception:
                # Lost the race to a concurrent claim, or a transient error.
                return False
        else:
            lock_fd = self._acquire_lock()
            try:
                data = self._local_load()
                record = data.get(draft_id)
                if record is None or not can_execute(record.get("status")):
                    return False
                record["status"] = EXECUTING
                record["execution_started_at"] = (
                    record.get("execution_started_at") or execution_started_at
                )
                self._local_save(data)
                return True
            finally:
                self._release_lock(lock_fd)

    def try_claim_pickup(self, draft_id: str) -> bool:
        """Atomically claim pickup for a draft.

        Returns True if this call won the claim (caller should proceed to call the
        courier), False if pickup was already claimed — by this or a concurrent
        request — or the draft does not exist. Claiming *before* the courier call
        (rather than marking pickup_ordered after) closes the check-then-act race
        where two concurrent requests could both pass the pickup_ordered check and
        both call the courier.
        """
        if self._use_table:
            client = self._table_client()
            try:
                entity = client.get_entity(partition_key=PARTITION_KEY, row_key=draft_id)
            except Exception:
                return False
            if entity.get("pickup_ordered"):
                return False
            patch = {"PartitionKey": PARTITION_KEY, "RowKey": draft_id, "pickup_ordered": True}
            try:
                from azure.core import MatchConditions

                client.update_entity(
                    patch,
                    mode="merge",
                    etag=entity.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return True
            except Exception:
                # Lost the race to a concurrent claim, or a transient error —
                # either way this call did not win the claim.
                return False
        else:
            lock_fd = self._acquire_lock()
            try:
                data = self._local_load()
                record = data.get(draft_id)
                if record is None or record.get("pickup_ordered"):
                    return False
                record["pickup_ordered"] = True
                self._local_save(data)
                return True
            finally:
                self._release_lock(lock_fd)

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        if self._use_table:
            try:
                from azure.core.exceptions import ResourceNotFoundError

                entity = self._table_client().get_entity(
                    partition_key=PARTITION_KEY, row_key=draft_id
                )
                return _deserialize(dict(entity))
            except ResourceNotFoundError:
                return None
            except Exception as exc:
                # An outage is not an absence. Returning None here made a timeout
                # look like "this draft does not exist" (issue #310).
                raise storage_unavailable("shipping", "get_draft", exc) from exc
        else:
            return self._local_load().get(draft_id)

    def delete_draft(self, draft_id: str) -> None:
        if self._use_table:
            try:
                self._table_client().delete_entity(PARTITION_KEY, draft_id)
            except Exception:
                pass
        else:
            lock_fd = self._acquire_lock()
            try:
                data = self._local_load()
                data.pop(draft_id, None)
                self._local_save(data)
            finally:
                self._release_lock(lock_fd)

    # ── Dead-letter queue for failed drafts (P1-9) ────────────────────────────

    @property
    def _dlq_local_file(self) -> Path:
        path = self._local_root / _DLQ_LOCAL_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _dlq_load_unlocked(self) -> dict[str, Any]:
        if not self._dlq_local_file.exists():
            return {}
        try:
            return json.loads(self._dlq_local_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _dlq_save_unlocked(self, data: dict[str, Any]) -> None:
        temp_fd, temp_path = tempfile.mkstemp(
            dir=str(self._local_root), prefix=".tmp-dlq-", suffix=".json"
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, str(self._dlq_local_file))
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _dlq_table_client(self) -> Any:
        return self._cached_table(DLQ_TABLE_NAME)

    @staticmethod
    def _serialize_dlq_entry(entry: dict[str, Any]) -> dict[str, Any]:
        entity: dict[str, Any] = {
            "PartitionKey": DLQ_PARTITION_KEY,
            "RowKey": entry["id"],
        }
        for k, v in entry.items():
            if k == "id":
                continue
            if isinstance(v, (dict, list)):
                entity[k] = json.dumps(v, ensure_ascii=False)
            elif v is None:
                entity[k] = ""
            else:
                entity[k] = v
        return entity

    @staticmethod
    def _deserialize_dlq_entry(entity: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {"id": entity["RowKey"]}
        for k, v in entity.items():
            if k in ("PartitionKey", "RowKey", "etag", "Timestamp"):
                continue
            if isinstance(v, str):
                if v == "":
                    v = None
                else:
                    try:
                        parsed = json.loads(v)
                        if isinstance(parsed, (dict, list)):
                            v = parsed
                    except (json.JSONDecodeError, ValueError):
                        pass
            record[k] = v
        return record

    def enqueue_dlq(
        self,
        *,
        payload: dict[str, Any],
        error: str,
        source: str = "shopify",
        entry_id: str | None = None,
        kind: str = DLQ_KIND_CREATION,
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a failed shipping operation for later retry.

        ``kind`` says what failed, because the two cases need different retries:
        ``draft_creation`` re-runs ingestion from ``payload``, while
        ``draft_execution`` re-runs the courier call for an existing
        ``draft_id`` — retrying the latter as a creation would duplicate it.

        Idempotent: if ``entry_id`` is provided and already exists, the entry is
        updated in-place with the new error and an incremented ``retries`` counter.
        Returns the stored entry.
        """
        import uuid as _uuid
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        now = _dt.now(_tz.utc).isoformat()
        eid = entry_id or str(_uuid.uuid4())

        if self._use_table:
            try:
                client = self._dlq_table_client()
                try:
                    existing_entity = client.get_entity(
                        partition_key=DLQ_PARTITION_KEY, row_key=eid
                    )
                    existing = self._deserialize_dlq_entry(dict(existing_entity))
                    entry = {
                        **existing,
                        "last_error": error,
                        "retries": int(existing.get("retries") or 0) + 1,
                        "updated_at": now,
                    }
                except Exception:
                    entry = {
                        "id": eid,
                        "created_at": now,
                        "updated_at": now,
                        "source": source,
                        "payload": payload,
                        "last_error": error,
                        "retries": 0,
                        "kind": kind,
                        "draft_id": draft_id,
                    }
                client.upsert_entity(self._serialize_dlq_entry(entry))
                return entry
            except Exception as exc:
                logger.error("DLQ enqueue failed for entry %s: %s", eid, exc)
                raise
        lock_fd = self._acquire_lock()
        try:
            data = self._dlq_load_unlocked()
            if eid in data:
                existing = data[eid]
                entry = {
                    **existing,
                    "last_error": error,
                    "retries": int(existing.get("retries") or 0) + 1,
                    "updated_at": now,
                }
            else:
                entry = {
                    "id": eid,
                    "created_at": now,
                    "updated_at": now,
                    "source": source,
                    "payload": payload,
                    "last_error": error,
                    "retries": 0,
                    "kind": kind,
                    "draft_id": draft_id,
                }
            data[eid] = entry
            self._dlq_save_unlocked(data)
            return entry
        finally:
            self._release_lock(lock_fd)

    def list_dlq(self, limit: int = 200) -> list[dict[str, Any]]:
        if self._use_table:
            try:
                entities = list(
                    self._dlq_table_client().query_entities(
                        f"PartitionKey eq '{DLQ_PARTITION_KEY}'"
                    )
                )
                records = [self._deserialize_dlq_entry(dict(e)) for e in entities]
            except Exception as exc:
                raise storage_unavailable("shipping", "list_dlq", exc) from exc
        else:
            records = list(self._dlq_load_unlocked().values())
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return records[:limit]

    def get_dlq_entry(self, entry_id: str) -> dict[str, Any] | None:
        if self._use_table:
            try:
                from azure.core.exceptions import ResourceNotFoundError

                entity = self._dlq_table_client().get_entity(
                    partition_key=DLQ_PARTITION_KEY, row_key=entry_id
                )
                return self._deserialize_dlq_entry(dict(entity))
            except ResourceNotFoundError:
                return None
            except Exception as exc:
                raise storage_unavailable("shipping", "get_dlq_entry", exc) from exc
        return self._dlq_load_unlocked().get(entry_id)

    def delete_dlq_entry(self, entry_id: str) -> None:
        if self._use_table:
            try:
                self._dlq_table_client().delete_entity(DLQ_PARTITION_KEY, entry_id)
            except Exception:
                pass
        else:
            lock_fd = self._acquire_lock()
            try:
                data = self._dlq_load_unlocked()
                data.pop(entry_id, None)
                self._dlq_save_unlocked(data)
            finally:
                self._release_lock(lock_fd)

    def list_drafts(self, limit: int = 200) -> list[dict[str, Any]]:
        if self._use_table:
            try:
                entities = list(
                    self._table_client().query_entities(f"PartitionKey eq '{PARTITION_KEY}'")
                )
                records: list[dict[str, Any]] = [_deserialize(dict(e)) for e in entities]
            except Exception as exc:
                # A list read has no not-found case: an empty partition yields an
                # empty result without raising. So every exception is an outage.
                raise storage_unavailable("shipping", "list_drafts", exc) from exc
        else:
            records = list(self._local_load().values())
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        if len(records) >= _PARTITION_SCAN_ALARM_ROWS:
            # Table Storage cannot sort, so "newest N" still reads the whole
            # partition. Make that cost visible before it hurts, rather than
            # discovering it from a latency graph (#316).
            from zdrovena.common.events import log_event

            log_event(
                "storage.partition_scan",
                level=logging.WARNING,
                store="shipping",
                operation="list_drafts",
                rows_read=len(records),
                rows_returned=min(len(records), limit),
                threshold=_PARTITION_SCAN_ALARM_ROWS,
            )
        if len(records) > limit:
            # Truncation is newest-first, so the rows dropped here are the
            # oldest. Any caller building a lookup index from this list will
            # miss them and treat those orders as new. That silently duplicated
            # 70 Allegro drafts before anyone noticed, so say it out loud.
            logger.warning(
                "list_drafts truncated: %d rows in store, returning newest %d — "
                "callers deduplicating by order id must pass a higher limit",
                len(records),
                limit,
            )
        return records[:limit]


def get_shipping_store(local_root: Path | None = None) -> ShippingStore:
    """Factory: resolves Table Storage or local JSON based on environment."""
    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if account_url:
        return ShippingStore(account_url=account_url)
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        return ShippingStore(connection_string=conn)
    return ShippingStore(local_root=local_root)
