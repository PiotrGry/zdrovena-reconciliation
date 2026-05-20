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
from pathlib import Path
from typing import Any

logger = logging.getLogger("zdrovena.common.shipping_store")

TABLE_NAME = "shippingdrafts"
PARTITION_KEY = "drafts"
_LOCAL_FILE_NAME = "shipping-drafts.json"
_DEFAULT_ROOT = Path.home() / ".zdrovena" / "storage"


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
            assert self._connection_string
            svc = TableServiceClient.from_connection_string(self._connection_string)
        return svc.create_table_if_not_exists(TABLE_NAME)

    # ── Local fallback ─────────────────────────────────────────────────────────

    @property
    def _local_file(self) -> Path:
        path = self._local_root / _LOCAL_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _local_load(self) -> dict[str, Any]:
        if not self._local_file.exists():
            return {}
        try:
            return json.loads(self._local_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _local_save(self, data: dict[str, Any]) -> None:
        self._local_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def upsert_draft(self, record: dict[str, Any]) -> None:
        if self._use_table:
            try:
                self._table_client().upsert_entity(_serialize(record))
            except Exception as exc:
                logger.error("Table upsert failed for draft %s: %s", record.get("id"), exc)
                raise
        else:
            data = self._local_load()
            data[record["id"]] = record
            self._local_save(data)

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
            data = self._local_load()
            if draft_id not in data:
                return False
            data[draft_id].update(fields)
            self._local_save(data)
            return True

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
