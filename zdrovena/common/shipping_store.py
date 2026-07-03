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
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

logger = logging.getLogger("zdrovena.common.shipping_store")

TABLE_NAME = "shippingdrafts"
PARTITION_KEY = "drafts"
_LOCAL_FILE_NAME = "shipping-drafts.json"
_DEFAULT_ROOT = Path.home() / ".zdrovena" / "storage"

# Dead-letter queue for failed draft creation attempts (P1-9).
# Entries hold the original payload + last error and can be retried via a
# dedicated endpoint. Storage layout mirrors the drafts table.
DLQ_TABLE_NAME = "shippingdraftsdlq"
DLQ_PARTITION_KEY = "dlq"
_DLQ_LOCAL_FILE_NAME = "shipping-drafts-dlq.json"


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

    def _table_client(self) -> Any:
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        if self._account_url:
            svc = TableServiceClient(
                endpoint=_table_endpoint(self._account_url),
                credential=DefaultAzureCredential(),
            )
        else:
            if not self._connection_string:
                raise RuntimeError(
                    "ShippingStore: neither account_url nor connection_string is set"
                )
            svc = TableServiceClient.from_connection_string(self._connection_string)
        return svc.create_table_if_not_exists(TABLE_NAME)

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
            shopify_order_id = record.get("shopify_order_id")
            if shopify_order_id:
                escaped = str(shopify_order_id).replace("'", "''")
                query_filter = (
                    f"PartitionKey eq '{PARTITION_KEY}' and "
                    f"shopify_order_id eq '{escaped}' and "
                    f"RowKey ne '{record['id']}'"
                )
                try:
                    for existing in client.query_entities(query_filter=query_filter):
                        client.delete_entity(existing["PartitionKey"], existing["RowKey"])
                except Exception as exc:
                    logger.warning(
                        "Table dedup lookup failed for shopify_order_id %s: %s",
                        shopify_order_id,
                        exc,
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
                shopify_order_id = record.get("shopify_order_id")
                if shopify_order_id:
                    for existing_id, existing_record in list(data.items()):
                        if existing_record.get("shopify_order_id") == shopify_order_id:
                            del data[existing_id]
                data[record["id"]] = record
                self._local_save(data)
            finally:
                self._release_lock(lock_fd)

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
                entity = self._table_client().get_entity(
                    partition_key=PARTITION_KEY, row_key=draft_id
                )
                return _deserialize(dict(entity))
            except Exception:
                return None
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
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        if self._account_url:
            svc = TableServiceClient(
                endpoint=_table_endpoint(self._account_url),
                credential=DefaultAzureCredential(),
            )
        else:
            if not self._connection_string:
                raise RuntimeError(
                    "ShippingStore: neither account_url nor connection_string is set"
                )
            svc = TableServiceClient.from_connection_string(self._connection_string)
        return svc.create_table_if_not_exists(DLQ_TABLE_NAME)

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
    ) -> dict[str, Any]:
        """Record a failed draft-creation attempt for later retry.

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
                logger.warning("DLQ list failed: %s", exc)
                return []
        else:
            records = list(self._dlq_load_unlocked().values())
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return records[:limit]

    def get_dlq_entry(self, entry_id: str) -> dict[str, Any] | None:
        if self._use_table:
            try:
                entity = self._dlq_table_client().get_entity(
                    partition_key=DLQ_PARTITION_KEY, row_key=entry_id
                )
                return self._deserialize_dlq_entry(dict(entity))
            except Exception:
                return None
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
                logger.warning("Table list_drafts failed: %s", exc)
                return []
        else:
            records = list(self._local_load().values())
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
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
