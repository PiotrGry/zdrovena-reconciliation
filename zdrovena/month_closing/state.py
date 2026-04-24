"""
zdrovena.month_closing.state – Pipeline State Tracker
=======================================================
Persists step-completion state to ``<month_dir>/.state.json`` locally and,
when a StorageService is provided, mirrors it to Blob Storage so that the API
and other nodes can read checkpoint state without access to the local disk.

Blob key: ``faktury/{year}/{month_pl}/.state.json``
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zdrovena.common.storage import StorageService

logger = logging.getLogger("zdrovena.month_closing.state")

_STATE_FILE = ".state.json"


class PipelineState:
    def __init__(
        self,
        month_dir: Path,
        *,
        storage: StorageService | None = None,
        blob_key: str | None = None,
    ) -> None:
        self.path = month_dir / _STATE_FILE
        self._storage = storage
        self._blob_key = blob_key  # e.g. "faktury/2026/Kwiecień/.state.json"
        self._data: dict[str, Any] = self._load()

    def is_done(self, step: str) -> bool:
        return step in self._data.get("completed_steps", [])

    def mark_done(self, step: str) -> None:
        steps: list[str] = self._data.setdefault("completed_steps", [])
        if step not in steps:
            steps.append(step)
        self._save()
        logger.debug("State: marked '%s' done", step)

    def reset(self) -> None:
        self._data = {}
        if self.path.exists():
            self.path.unlink()
        if self._storage and self._blob_key:
            try:
                self._storage.delete(self._blob_key)
                logger.info("State: deleted blob checkpoint %s", self._blob_key)
            except Exception as exc:
                logger.warning("State: could not delete blob checkpoint: %s", exc)
        logger.info("State: reset (all steps will re-run)")

    @property
    def completed_steps(self) -> list[str]:
        return list(self._data.get("completed_steps", []))

    def _load(self) -> dict[str, Any]:
        # Try blob first (source of truth)
        if self._storage and self._blob_key:
            try:
                if self._storage.exists(self._blob_key):
                    tmp = self.path.parent / (_STATE_FILE + ".tmp")
                    self._storage.download(self._blob_key, tmp)
                    data = json.loads(tmp.read_text(encoding="utf-8"))
                    tmp.unlink(missing_ok=True)
                    logger.debug("State: loaded from blob %s", self._blob_key)
                    return data
            except Exception as exc:
                logger.warning("State: could not load from blob, falling back to local: %s", exc)
        # Fallback: local file
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Corrupt state file %s: %s — starting fresh", self.path, exc)
        return {}

    def _save(self) -> None:
        payload = json.dumps(self._data, indent=2, ensure_ascii=False) + "\n"
        # Write local copy
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(payload, encoding="utf-8")
        # Mirror to blob
        if self._storage and self._blob_key:
            try:
                self._storage.upload_stream(
                    io.BytesIO(payload.encode()),
                    self._blob_key,
                    content_type="application/json",
                )
                logger.debug("State: synced to blob %s", self._blob_key)
            except Exception as exc:
                logger.warning("State: could not sync to blob: %s", exc)
