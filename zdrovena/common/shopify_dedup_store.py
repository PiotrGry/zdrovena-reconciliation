"""zdrovena.common.shopify_dedup_store — Shopify webhook delivery deduplication.

Shopify retries webhook deliveries (up to 8× over 4h) on any non-2xx response or
timeout, and can also deliver the same event more than once. Each delivery carries
a stable ``X-Shopify-Webhook-Id`` header; per Shopify's docs we persist that id and
skip any delivery we have already seen.

Production: Azure Table Storage (table 'shopifywebhookdedup')
  - PartitionKey = "webhook"
  - RowKey       = X-Shopify-Webhook-Id
  - seen_at      = ISO-8601 UTC timestamp (used for TTL expiry)

Local dev / tests: JSON file at ~/.zdrovena/storage/shopify-webhook-dedup.json
  - keyed by webhook_id -> seen_at ISO string

Entries older than ``_TTL_SECONDS`` (24h) are treated as expired and pruned lazily,
so the store never grows unbounded and a webhook_id can never block a genuinely new
delivery once Shopify's own retry window has elapsed.
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

logger = logging.getLogger("zdrovena.common.shopify_dedup_store")

TABLE_NAME = "shopifywebhookdedup"
PARTITION_KEY = "webhook"
_LOCAL_FILE_NAME = "shopify-webhook-dedup.json"
_DEFAULT_ROOT = Path.home() / ".zdrovena" / "storage"

# Shopify retries a delivery for up to 4h; 24h gives ample margin before an id
# may be reused for a genuinely new event.
_TTL_SECONDS = 24 * 60 * 60


def _table_endpoint(url: str) -> str:  # pragma: no cover — Azure-only, needs live account
    return url.replace(".blob.core.windows.net", ".table.core.windows.net")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DedupStoreError(RuntimeError):
    """Raised when the dedup backend is unavailable or returns corrupt data.

    Callers must fail-closed on this (respond 503 so Shopify retries) rather than
    assume "not a duplicate" — the latter risks creating a duplicate draft.
    """


def _is_expired(seen_at: str, now: datetime) -> bool:
    try:
        seen = datetime.fromisoformat(seen_at)
    except ValueError:
        # Unparseable timestamp — treat as expired so it gets pruned/overwritten.
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (now - seen).total_seconds() >= _TTL_SECONDS


class ShopifyDedupStore:
    """Persistent set of processed Shopify webhook delivery ids with 24h TTL.

    Both methods raise on storage failure (they never silently succeed): the caller
    must fail-closed — returning 503 so Shopify retries — rather than risk creating a
    duplicate draft when the dedup backend is unavailable.
    """

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

    def _table_client(self) -> Any:  # pragma: no cover — Azure SDK adapter, live-only
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
                    "ShopifyDedupStore: neither account_url nor connection_string is set"
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

    def _local_load(self) -> dict[str, str]:
        if not self._local_file.exists():
            return {}
        # A corrupt file must NOT be swallowed into an empty set — that would silently
        # disable dedup. Let json errors propagate so the caller fails closed (503).
        return json.loads(self._local_file.read_text(encoding="utf-8"))

    def _local_save(self, data: dict[str, str]) -> None:
        temp_fd, temp_path = tempfile.mkstemp(
            dir=str(self._local_root), prefix=".tmp-", suffix=".json"
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, str(self._local_file))
        except OSError:  # pragma: no cover — disk-failure cleanup, not unit-reproducible
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _acquire_lock(self) -> int:
        lock_fd = os.open(str(self._lock_file), os.O_CREAT | os.O_RDWR)
        flock(lock_fd, LOCK_EX)
        return lock_fd

    def _release_lock(self, lock_fd: int) -> None:
        flock(lock_fd, LOCK_UN)
        os.close(lock_fd)

    # ── Public API ─────────────────────────────────────────────────────────────

    def is_duplicate(self, webhook_id: str) -> bool:
        """Return True if this delivery id was already recorded within the TTL window.

        Raises DedupStoreError if the backend is unavailable or the local store is
        corrupt, so the caller can fail-closed instead of assuming "not a duplicate".
        """
        if not webhook_id:
            return False
        now = _now()
        if self._use_table:  # pragma: no cover — Azure branch, live-only
            return self._table_is_duplicate(webhook_id, now)
        return self._local_is_duplicate(webhook_id, now)

    def mark_seen(self, webhook_id: str) -> None:
        """Record a delivery id as processed. No-op for an empty id.

        NOTE: this is a plain write — it can race with a concurrent is_duplicate()
        check. Prefer :meth:`mark_seen_if_new` for the webhook flow, which does an
        atomic check-and-set. Kept for callers that only want to record, unconditionally.

        Raises DedupStoreError on backend failure.
        """
        if not webhook_id:
            return
        now = _now()
        stamp = now.isoformat()
        if self._use_table:  # pragma: no cover — Azure branch, live-only
            try:
                self._table_client().upsert_entity(
                    {"PartitionKey": PARTITION_KEY, "RowKey": webhook_id, "seen_at": stamp}
                )
            except Exception as exc:
                raise DedupStoreError(f"table upsert failed: {exc}") from exc
            return

        lock_fd = self._acquire_lock()
        try:
            data = self._local_load()
            # Prune expired entries so the local file cannot grow without bound.
            data = {k: v for k, v in data.items() if not _is_expired(v, now)}
            data[webhook_id] = stamp
            self._local_save(data)
        except (OSError, ValueError) as exc:
            raise DedupStoreError(f"local store write failed: {exc}") from exc
        finally:
            self._release_lock(lock_fd)

    def mark_seen_if_new(self, webhook_id: str) -> bool:
        """Atomic check-and-set: returns True iff *this* call was the one to insert.

        Returns False when the webhook_id was already recorded (still within TTL) or
        when webhook_id is empty. Two concurrent calls with the same webhook_id can
        never both return True — exactly one wins.

        Azure branch: uses ``create_entity`` (INSERT, not upsert) and treats
        ``ResourceExistsError`` as "already recorded". If the existing entity is
        expired we DELETE+INSERT and count it as a fresh insert.

        Local branch: holds the flock across load→check→save so no interleaving is
        possible between processes on the same host.

        Raises DedupStoreError on backend failure.
        """
        if not webhook_id:
            return False
        now = _now()
        stamp = now.isoformat()

        if self._use_table:  # pragma: no cover — Azure branch, live-only
            from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

            entity = {
                "PartitionKey": PARTITION_KEY,
                "RowKey": webhook_id,
                "seen_at": stamp,
            }
            client = self._table_client()
            try:
                client.create_entity(entity=entity)
                return True
            except ResourceExistsError:
                # Row exists — check whether it's expired; if so, delete + retry insert.
                try:
                    existing = client.get_entity(partition_key=PARTITION_KEY, row_key=webhook_id)
                except ResourceNotFoundError:
                    # Deleted between exists error and lookup — retry once as fresh.
                    try:
                        client.create_entity(entity=entity)
                        return True
                    except ResourceExistsError:
                        return False
                    except Exception as exc:
                        raise DedupStoreError(f"table insert retry failed: {exc}") from exc
                if not _is_expired(str(existing.get("seen_at", "")), now):
                    return False
                # Expired — delete + insert atomically enough (Azure Table has no CAS
                # on create; two concurrent expirations may race, but at most one
                # create_entity will succeed which is still exactly-once).
                try:
                    client.delete_entity(PARTITION_KEY, webhook_id)
                except ResourceNotFoundError:
                    pass
                except Exception as exc:
                    raise DedupStoreError(f"table expiry delete failed: {exc}") from exc
                try:
                    client.create_entity(entity=entity)
                    return True
                except ResourceExistsError:
                    return False
                except Exception as exc:
                    raise DedupStoreError(f"table post-expiry insert failed: {exc}") from exc
            except Exception as exc:
                raise DedupStoreError(f"table create failed: {exc}") from exc

        # Local branch — flock guarantees load→check→save is atomic per host.
        lock_fd = self._acquire_lock()
        try:
            data = self._local_load()
            existing = data.get(webhook_id)
            if existing is not None and not _is_expired(existing, now):
                return False
            # Prune expired entries opportunistically.
            data = {k: v for k, v in data.items() if not _is_expired(v, now)}
            data[webhook_id] = stamp
            self._local_save(data)
            return True
        except (OSError, ValueError) as exc:
            raise DedupStoreError(f"local store atomic write failed: {exc}") from exc
        finally:
            self._release_lock(lock_fd)

    def _table_is_duplicate(  # pragma: no cover — Azure SDK adapter, live-only
        self, webhook_id: str, now: datetime
    ) -> bool:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._table_client().get_entity(
                partition_key=PARTITION_KEY, row_key=webhook_id
            )
        except ResourceNotFoundError:
            return False
        except Exception as exc:
            raise DedupStoreError(f"table lookup failed: {exc}") from exc
        seen_at = str(entity.get("seen_at", ""))
        if _is_expired(seen_at, now):
            try:
                self._table_client().delete_entity(PARTITION_KEY, webhook_id)
            except ResourceNotFoundError:
                pass
            return False
        return True

    def _local_is_duplicate(self, webhook_id: str, now: datetime) -> bool:
        lock_fd = self._acquire_lock()
        try:
            data = self._local_load()
            seen_at = data.get(webhook_id)
            if seen_at is None:
                return False
            if _is_expired(seen_at, now):
                del data[webhook_id]
                self._local_save(data)
                return False
            return True
        except (OSError, ValueError) as exc:
            raise DedupStoreError(f"local store read failed: {exc}") from exc
        finally:
            self._release_lock(lock_fd)


def get_shopify_dedup_store(local_root: Path | None = None) -> ShopifyDedupStore:
    """Factory: resolves Table Storage or local JSON based on environment."""
    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if account_url:
        return ShopifyDedupStore(account_url=account_url)
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        return ShopifyDedupStore(connection_string=conn)
    return ShopifyDedupStore(local_root=local_root)
