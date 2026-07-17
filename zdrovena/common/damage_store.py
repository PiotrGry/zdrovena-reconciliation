"""Durable storage for damaged-shipment cases and scanner cursors.

Production uses Azure Table Storage (``damagecases``). Local development and
tests use an atomically-written JSON file under ``~/.zdrovena/storage``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

logger = logging.getLogger("zdrovena.common.damage_store")

TABLE_NAME = "damagecases"
CASES_PARTITION = "cases"
STATE_PARTITION = "state"
_LOCAL_FILE_NAME = "damage-cases.json"
_DEFAULT_ROOT = Path.home() / ".zdrovena" / "storage"


def _table_endpoint(url: str) -> str:
    return url.replace(".blob.core.windows.net", ".table.core.windows.net")


def _serialize(record: dict[str, Any], *, partition: str) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "PartitionKey": partition,
        "RowKey": str(record["id"]),
    }
    for key, value in record.items():
        if key == "id":
            continue
        if isinstance(value, (dict, list)):
            entity[key] = json.dumps(value, ensure_ascii=False)
        elif value is None:
            entity[key] = ""
        else:
            entity[key] = value
    return entity


def _deserialize(entity: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {"id": entity["RowKey"]}
    for key, value in entity.items():
        if key in {"PartitionKey", "RowKey", "etag", "Timestamp"}:
            continue
        if isinstance(value, str):
            if value == "":
                value = None
            else:
                try:
                    decoded = json.loads(value)
                    if isinstance(decoded, (dict, list)):
                        value = decoded
                except (json.JSONDecodeError, ValueError):
                    pass
        record[key] = value
    return record


class DamageStore:
    """Storage backend for damage cases and scanner state."""

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
            service = TableServiceClient(
                endpoint=_table_endpoint(self._account_url),
                credential=DefaultAzureCredential(),
            )
        else:
            if not self._connection_string:
                raise RuntimeError("DamageStore: storage configuration is missing")
            service = TableServiceClient.from_connection_string(self._connection_string)
        return service.create_table_if_not_exists(TABLE_NAME)

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

    def _load_local(self) -> dict[str, Any]:
        if not self._local_file.exists():
            return {"cases": {}, "state": {}}
        try:
            data = json.loads(self._local_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"cases": {}, "state": {}}
        data.setdefault("cases", {})
        data.setdefault("state", {})
        return data

    def _save_local(self, data: dict[str, Any]) -> None:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._local_root), prefix=".tmp-damage-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._local_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _locked_local_update(self, callback: Any) -> Any:
        lock_fd = os.open(str(self._lock_file), os.O_CREAT | os.O_RDWR)
        flock(lock_fd, LOCK_EX)
        try:
            data = self._load_local()
            result = callback(data)
            self._save_local(data)
            return result
        finally:
            flock(lock_fd, LOCK_UN)
            os.close(lock_fd)

    def upsert_case(self, record: dict[str, Any]) -> None:
        if self._use_table:
            self._table_client().upsert_entity(_serialize(record, partition=CASES_PARTITION))
            return

        def update(data: dict[str, Any]) -> None:
            data["cases"][str(record["id"])] = record

        self._locked_local_update(update)

    def update_case(self, case_id: str, fields: dict[str, Any]) -> bool:
        if self._use_table:
            if self.get_case(case_id) is None:
                return False
            patch = _serialize({"id": case_id, **fields}, partition=CASES_PARTITION)
            self._table_client().update_entity(patch, mode="merge")
            return True

        def update(data: dict[str, Any]) -> bool:
            case = data["cases"].get(case_id)
            if case is None:
                return False
            case.update(fields)
            return True

        return bool(self._locked_local_update(update))

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        if self._use_table:
            try:
                entity = self._table_client().get_entity(CASES_PARTITION, case_id)
            except Exception:
                return None
            return _deserialize(dict(entity))
        return self._load_local()["cases"].get(case_id)

    def find_case_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        for case in self.list_cases(limit=500):
            if case.get("event_fingerprint") == fingerprint:
                return case
        return None

    def list_cases(self, limit: int = 200) -> list[dict[str, Any]]:
        if self._use_table:
            try:
                entities = self._table_client().query_entities(
                    f"PartitionKey eq '{CASES_PARTITION}'"
                )
                records = [_deserialize(dict(entity)) for entity in entities]
            except Exception as exc:
                logger.warning("Damage case list failed: %s", exc)
                return []
        else:
            records = list(self._load_local()["cases"].values())
        records.sort(
            key=lambda case: case.get("detected_at") or case.get("created_at") or "",
            reverse=True,
        )
        return records[:limit]

    def count_needs_review(self) -> int:
        return sum(
            case.get("status") == "needs_review" and case.get("classification") == "damage"
            for case in self.list_cases(limit=500)
        )

    def try_claim_email(self, case_id: str) -> bool:
        """Atomically claim a customer email send and prevent double clicks.

        A claim older than ten minutes is considered abandoned so a process
        crash cannot block the case forever.
        """

        def can_claim(case: dict[str, Any]) -> bool:
            if case.get("email_sent_at"):
                return False
            if not case.get("email_sending"):
                return True
            raw_claimed_at = case.get("email_sending_at")
            try:
                claimed_at = datetime.fromisoformat(str(raw_claimed_at))
                return datetime.now(timezone.utc) - claimed_at > timedelta(minutes=10)
            except (TypeError, ValueError):
                return False

        claimed_at = datetime.now(timezone.utc).isoformat()
        if self._use_table:
            client = self._table_client()
            try:
                entity = client.get_entity(CASES_PARTITION, case_id)
            except Exception:
                return False
            case = _deserialize(dict(entity))
            if not can_claim(case):
                return False
            patch = {
                "PartitionKey": CASES_PARTITION,
                "RowKey": case_id,
                "email_sending": True,
                "email_sending_at": claimed_at,
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
                return False

        def update(data: dict[str, Any]) -> bool:
            case = data["cases"].get(case_id)
            if case is None or not can_claim(case):
                return False
            case["email_sending"] = True
            case["email_sending_at"] = claimed_at
            return True

        return bool(self._locked_local_update(update))

    def get_state(self, key: str, default: Any = None) -> Any:
        if self._use_table:
            try:
                entity = self._table_client().get_entity(STATE_PARTITION, key)
            except Exception:
                return default
            return _deserialize(dict(entity)).get("value", default)
        return self._load_local()["state"].get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        record = {"id": key, "value": value}
        if self._use_table:
            self._table_client().upsert_entity(_serialize(record, partition=STATE_PARTITION))
            return

        def update(data: dict[str, Any]) -> None:
            data["state"][key] = value

        self._locked_local_update(update)


def get_damage_store(local_root: Path | None = None) -> DamageStore:
    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if account_url:
        return DamageStore(account_url=account_url)
    connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if connection_string:
        return DamageStore(connection_string=connection_string)
    return DamageStore(local_root=local_root)
