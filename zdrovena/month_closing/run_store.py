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
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

TABLE_NAME = "monthcloseruns"
LOCAL_FILE_NAME = "month-close-runs.json"
ACTIVE_ACTION_TTL = timedelta(minutes=30)

STEP_IDS = ("check", "sales", "costs", "reports", "bank", "package", "send")

#: How many times :meth:`CloseRunStore.update` replays a mutation that lost a race.
UPDATE_RETRIES = 3


class RunBusyError(RuntimeError):
    """Raised when another request already owns the period action."""


class RunConflictError(RuntimeError):
    """Raised when the stored run moved on while an update was in flight."""


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
        "waivers": [],
        # Compare-and-swap revision. Every persisted write bumps it, so a writer
        # that read an older copy is rejected instead of silently clobbering.
        "rev": 0,
    }


def ensure_schema(run: dict[str, Any]) -> dict[str, Any]:
    """Backfill keys added after a run was first persisted."""
    run.setdefault("waivers", [])
    run.setdefault("rev", 0)
    return run


def _has_moved_on(stored: dict[str, Any], run: dict[str, Any], expect_rev: int) -> bool:
    """True when the stored run is no longer the one ``run`` was derived from.

    A bumped revision means someone else wrote; a different ``run_id`` means the
    period was reset out from under us, which the counter alone cannot catch.
    """
    if int(stored.get("rev", 0)) != expect_rev:
        return True
    return stored.get("run_id") != run.get("run_id")


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
        return ensure_schema(json.loads(payload))

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
            return ensure_schema(json.loads(json.dumps(run))) if run else None
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
        previous = self.get(year, month)
        run = new_close_run(year, month, requested_by)
        if previous:
            # Keep the revision sequence monotonic per period, otherwise a writer
            # holding a pre-reset copy could match the restarted counter.
            run["rev"] = int(previous.get("rev", 0))
        self.save(run)
        return run

    def save(self, run: dict[str, Any], *, expect_rev: int | None = None) -> None:
        """Persist ``run``.

        ``expect_rev`` makes the write a compare-and-swap: the stored run must
        still carry that revision, otherwise :class:`RunConflictError` is raised
        and the caller must re-read. Pass ``None`` only to deliberately replace
        whatever is stored (``reset``), never as a way around a conflict.
        """
        key = _period_key(int(run["year"]), int(run["month"]))
        if self._use_table:
            self._save_table(run, key, expect_rev)
            return
        self._save_local(run, f"{self._partition_key}:{key}", expect_rev)

    def _save_local(self, run: dict[str, Any], local_key: str, expect_rev: int | None) -> None:
        lock_fd = self._acquire_lock()
        try:
            data = self._local_load()
            if expect_rev is not None:
                stored = data.get(local_key)
                if stored is not None and _has_moved_on(stored, run, expect_rev):
                    raise RunConflictError(
                        f"Run {local_key} zmienił się w trakcie zapisu "
                        f"(rev {stored.get('rev', 0)} != {expect_rev})."
                    )
            run["rev"] = int(run.get("rev", 0)) + 1
            run["updated_at"] = _now()
            data[local_key] = run
            self._local_save(data)
        finally:
            self._release_lock(lock_fd)

    def _save_table(self, run: dict[str, Any], key: str, expect_rev: int | None) -> None:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

        client = self._table_client()
        if expect_rev is None:
            run["rev"] = int(run.get("rev", 0)) + 1
            run["updated_at"] = _now()
            client.upsert_entity(self._entity(run))
            return

        try:
            entity = client.get_entity(partition_key=self._partition_key, row_key=key)
        except ResourceNotFoundError:
            entity = None
        if entity is not None:
            stored = self._run_from_entity(entity)
            if _has_moved_on(stored, run, expect_rev):
                raise RunConflictError(
                    f"Run {key} zmienił się w trakcie zapisu "
                    f"(rev {stored.get('rev', 0)} != {expect_rev})."
                )
        run["rev"] = expect_rev + 1
        run["updated_at"] = _now()
        if entity is None:
            client.create_entity(self._entity(run))
            return
        try:
            # If-Match closes the window between the read above and this write.
            client.update_entity(
                self._entity(run),
                mode="replace",
                etag=entity.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except ResourceModifiedError as exc:
            raise RunConflictError(f"Run {key} zmienił się w trakcie zapisu.") from exc

    def update(
        self,
        year: int,
        month: int,
        mutate: Callable[[dict[str, Any]], None],
        requested_by: str,
        *,
        retries: int = UPDATE_RETRIES,
    ) -> dict[str, Any]:
        """Read-modify-write one run under compare-and-swap, replaying on conflict.

        ``mutate`` must be replayable: it is re-applied to a freshly read run
        whenever another writer wins the race. Exceptions it raises (a busy run,
        an invalid target) propagate untouched.
        """
        for _attempt in range(retries):
            run = self.get_or_create(year, month, requested_by)
            expect_rev = int(run.get("rev", 0))
            mutate(run)
            try:
                self.save(run, expect_rev=expect_rev)
            except RunConflictError:
                continue
            return run
        raise RunConflictError(
            f"Nie udało się zapisać zmiany dla okresu {_period_key(year, month)} — "
            "run zmieniał się w trakcie."
        )

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
            run = ensure_schema(data.get(local_key) or new_close_run(year, month, requested_by))
            if run.get("active_action") and not _is_active_stale(run):
                raise RunBusyError(
                    f"Etap {run['active_action']} jest już wykonywany dla okresu {key}."
                )
            self._mark_claimed(run, action, requested_by)
            run["rev"] = int(run.get("rev", 0)) + 1
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
            run["rev"] = int(run.get("rev", 0)) + 1
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
        # Re-running a stage invalidates every waiver granted for its previous
        # result — the operator must judge the fresh outcome again.
        run["waivers"] = [
            waiver
            for waiver in run.get("waivers", [])
            if waiver.get("target") != f"step:{action}" and waiver.get("stage") != action
        ]
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
        # CAS: if the run moved on since we claimed it, our claim was revoked
        # (stale-action takeover) and this late result must not win.
        self.save(run, expect_rev=int(run.get("rev", 0)))
        return run
