"""Durable state for the operator-driven month-close workflow.

Production uses one Azure Table entity per accounting period and optimistic
ETag updates to claim an action before it performs provider writes. Local
development uses an atomically replaced JSON file guarded by ``flock``.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

TABLE_NAME = "monthcloseruns"
LOCAL_FILE_NAME = "month-close-runs.json"
ACTIVE_ACTION_TTL = timedelta(minutes=30)

STEP_IDS = ("check", "sales", "costs", "reports", "bank", "package", "send")


class RunBusyError(RuntimeError):
    """Raised when another request already owns the period action."""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _period_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _table_endpoint(url: str) -> str:
    return url.replace(".blob.core.windows.net", ".table.core.windows.net")


def new_close_run(year: int, month: int, requested_by: str) -> dict[str, Any]:
    now = _now()
    return {
        "run_id": str(uuid.uuid4()),
        "year": year,
        "month": month,
        "status": "draft",
        "active_action": None,
        "requested_by": requested_by,
        "created_at": now,
        "updated_at": now,
        "steps": {
            step: {
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "message": None,
            }
            for step in STEP_IDS
        },
        "documents": [],
        "issues": [],
        "metrics": {},
        "artifacts": [],
        "logs": [],
        "overrides": [],
    }


def _is_active_stale(run: dict[str, Any]) -> bool:
    if not run.get("active_action"):
        return False
    try:
        updated_at = datetime.fromisoformat(str(run["updated_at"]))
    except (KeyError, TypeError, ValueError):
        return True
    return datetime.now(tz=timezone.utc) - updated_at > ACTIVE_ACTION_TTL


class CloseRunStore:
    """Read, persist and atomically claim the current run for one period."""

    def __init__(
        self,
        *,
        account_url: str | None = None,
        connection_string: str | None = None,
        local_root: Path | None = None,
        namespace: str | None = None,
    ) -> None:
        self._account_url = account_url
        self._connection_string = connection_string
        self._use_table = bool(account_url or connection_string)
        self._local_root = local_root or Path.home() / ".zdrovena" / "storage"
        raw_namespace = (
            namespace
            or os.environ.get("AZURE_STORAGE_CONTAINER")
            or os.environ.get("APP_ENV")
            or "local"
        )
        safe_namespace = "".join(
            char if char.isalnum() or char in "-_" else "-" for char in raw_namespace.casefold()
        )
        self._partition_key = f"periods-{safe_namespace[:48]}"

    @classmethod
    def from_environment(cls) -> CloseRunStore:
        return cls(
            account_url=os.environ.get("AZURE_STORAGE_ACCOUNT_URL"),
            connection_string=os.environ.get("AZURE_STORAGE_CONNECTION_STRING"),
        )

    def _table_client(self) -> Any:
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        if self._account_url:
            service = TableServiceClient(
                endpoint=_table_endpoint(self._account_url),
                credential=DefaultAzureCredential(),
            )
        elif self._connection_string:
            service = TableServiceClient.from_connection_string(self._connection_string)
        else:
            raise RuntimeError("CloseRunStore has no Azure Storage configuration")
        return service.create_table_if_not_exists(TABLE_NAME)

    @property
    def _local_file(self) -> Path:
        self._local_root.mkdir(parents=True, exist_ok=True)
        return self._local_root / LOCAL_FILE_NAME

    @property
    def _lock_file(self) -> Path:
        self._local_root.mkdir(parents=True, exist_ok=True)
        return self._local_root / f".{LOCAL_FILE_NAME}.lock"

    def _acquire_lock(self) -> int:
        fd = os.open(self._lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        flock(fd, LOCK_EX)
        return fd

    @staticmethod
    def _release_lock(fd: int) -> None:
        flock(fd, LOCK_UN)
        os.close(fd)

    def _local_load(self) -> dict[str, dict[str, Any]]:
        if not self._local_file.exists():
            return {}
        try:
            return json.loads(self._local_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _local_save(self, data: dict[str, dict[str, Any]]) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._local_root),
            prefix=".month-close-",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self._local_file)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _entity(self, run: dict[str, Any]) -> dict[str, Any]:
        return {
            "PartitionKey": self._partition_key,
            "RowKey": _period_key(int(run["year"]), int(run["month"])),
            "run_id": run["run_id"],
            "status": run["status"],
            "updated_at": run["updated_at"],
            "payload": json.dumps(run, ensure_ascii=False),
        }

    @staticmethod
    def _run_from_entity(entity: dict[str, Any]) -> dict[str, Any]:
        payload = entity.get("payload")
        if not isinstance(payload, str):
            raise ValueError("Month-close run entity has no JSON payload")
        return json.loads(payload)

    def get(self, year: int, month: int) -> dict[str, Any] | None:
        key = _period_key(year, month)
        local_key = f"{self._partition_key}:{key}"
        if self._use_table:
            from azure.core.exceptions import ResourceNotFoundError

            try:
                entity = self._table_client().get_entity(
                    partition_key=self._partition_key,
                    row_key=key,
                )
            except ResourceNotFoundError:
                return None
            return self._run_from_entity(entity)

        lock_fd = self._acquire_lock()
        try:
            run = self._local_load().get(local_key)
            return json.loads(json.dumps(run)) if run else None
        finally:
            self._release_lock(lock_fd)

    def get_or_create(self, year: int, month: int, requested_by: str) -> dict[str, Any]:
        existing = self.get(year, month)
        if existing:
            if _is_active_stale(existing):
                action = str(existing["active_action"])
                existing["active_action"] = None
                existing["status"] = "failed"
                existing["steps"][action]["status"] = "failed"
                existing["steps"][action]["message"] = (
                    "Etap nie potwierdził zakończenia w ciągu 30 minut. Można go uruchomić ponownie."
                )
                self.save(existing)
            return existing
        run = new_close_run(year, month, requested_by)
        self.save(run)
        return run

    def reset(self, year: int, month: int, requested_by: str) -> dict[str, Any]:
        run = new_close_run(year, month, requested_by)
        self.save(run)
        return run

    def save(self, run: dict[str, Any]) -> None:
        run["updated_at"] = _now()
        key = _period_key(int(run["year"]), int(run["month"]))
        local_key = f"{self._partition_key}:{key}"
        if self._use_table:
            self._table_client().upsert_entity(self._entity(run))
            return
        lock_fd = self._acquire_lock()
        try:
            data = self._local_load()
            data[local_key] = run
            self._local_save(data)
        finally:
            self._release_lock(lock_fd)

    def try_claim(
        self,
        year: int,
        month: int,
        action: str,
        requested_by: str,
    ) -> dict[str, Any]:
        """Atomically mark ``action`` as running and return the claimed run."""
        if action not in STEP_IDS:
            raise ValueError(f"Unknown month-close action: {action}")
        if self._use_table:
            return self._try_claim_table(year, month, action, requested_by)

        key = _period_key(year, month)
        local_key = f"{self._partition_key}:{key}"
        lock_fd = self._acquire_lock()
        try:
            data = self._local_load()
            run = data.get(local_key) or new_close_run(year, month, requested_by)
            if run.get("active_action") and not _is_active_stale(run):
                raise RunBusyError(
                    f"Etap {run['active_action']} jest już wykonywany dla okresu {key}."
                )
            self._mark_claimed(run, action, requested_by)
            data[local_key] = run
            self._local_save(data)
            return json.loads(json.dumps(run))
        finally:
            self._release_lock(lock_fd)

    def _try_claim_table(
        self,
        year: int,
        month: int,
        action: str,
        requested_by: str,
    ) -> dict[str, Any]:
        from azure.core import MatchConditions
        from azure.core.exceptions import (
            ResourceExistsError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )

        client = self._table_client()
        key = _period_key(year, month)
        for _attempt in range(3):
            try:
                entity = client.get_entity(
                    partition_key=self._partition_key,
                    row_key=key,
                )
            except ResourceNotFoundError:
                run = new_close_run(year, month, requested_by)
                try:
                    client.create_entity(self._entity(run))
                except ResourceExistsError:
                    continue
                entity = client.get_entity(
                    partition_key=self._partition_key,
                    row_key=key,
                )

            run = self._run_from_entity(entity)
            if run.get("active_action") and not _is_active_stale(run):
                raise RunBusyError(
                    f"Etap {run['active_action']} jest już wykonywany dla okresu {key}."
                )
            self._mark_claimed(run, action, requested_by)
            try:
                client.update_entity(
                    self._entity(run),
                    mode="replace",
                    etag=entity.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return run
            except ResourceModifiedError:
                continue
        raise RunBusyError(f"Nie udało się zarezerwować etapu {action} dla okresu {key}.")

    @staticmethod
    def _mark_claimed(run: dict[str, Any], action: str, requested_by: str) -> None:
        now = _now()
        run["active_action"] = action
        run["status"] = "running"
        run["requested_by"] = requested_by
        run["updated_at"] = now
        step = run["steps"][action]
        step["status"] = "running"
        step["started_at"] = now
        step["completed_at"] = None
        step["message"] = None

    def finish_action(
        self,
        run: dict[str, Any],
        action: str,
        *,
        success: bool,
        message: str,
        status: str,
    ) -> dict[str, Any]:
        now = _now()
        run["active_action"] = None
        run["status"] = status
        run["updated_at"] = now
        step = run["steps"][action]
        step["status"] = "done" if success else "failed"
        step["completed_at"] = now
        step["message"] = message
        self.save(run)
        return run
