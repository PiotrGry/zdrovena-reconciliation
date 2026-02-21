"""
zdrovena.month_closing.state – Pipeline State Tracker
=======================================================
Persists step-completion state to ``<month_dir>/.state.json`` so that
re-running the pipeline skips steps that have already succeeded.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("zdrovena.month_closing.state")

_STATE_FILE = ".state.json"


class PipelineState:
    def __init__(self, month_dir: Path) -> None:
        self.path = month_dir / _STATE_FILE
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
        logger.info("State: reset (all steps will re-run)")

    @property
    def completed_steps(self) -> list[str]:
        return list(self._data.get("completed_steps", []))

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Corrupt state file %s: %s — starting fresh", self.path, exc)
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
