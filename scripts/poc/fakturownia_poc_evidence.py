"""One evidence folder per PoC run (zdrovena#583). Never committed: it shows account data."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

EVIDENCE_ROOT = Path.home() / ".zdrovena" / "poc-runs"
SECRET_ENV = ("FAKTUROWNIA_LOGIN", "FAKTUROWNIA_PASSWORD")


def _redact(text: str) -> str:
    for name in SECRET_ENV:
        value = os.environ.get(name, "")
        if len(value) >= 4:
            text = text.replace(value, "[redacted]")
    return text


def _descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(entry.name))
    found, stack = [], [root]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def peak_rss_mb(root: int | None = None) -> float:
    """Sum of VmHWM (peak resident memory) of this process's descendants: the browser."""
    total_kb = 0
    for pid in _descendants(root or os.getpid()):
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    total_kb += int(line.split()[1])
        except OSError:
            continue
    return round(total_kb / 1024, 1)


@dataclass
class Evidence:
    dir: Path
    experiment: str
    report: str
    started: float = field(default_factory=time.monotonic)
    steps: int = 0

    @classmethod
    def start(cls, root: Path = EVIDENCE_ROOT, *, experiment: str, report: str) -> Evidence:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = root / f"{stamp}-{experiment}-{report}"
        path.mkdir(parents=True, exist_ok=False)
        return cls(dir=path, experiment=experiment, report=report)

    def _append(self, name: str, entry: dict) -> None:
        with (self.dir / name).open("a", encoding="utf-8") as handle:
            handle.write(_redact(json.dumps(entry, ensure_ascii=False)) + "\n")

    def request(self, entry: dict) -> None:
        self._append("requests.jsonl", entry)

    def click(self, entry: dict) -> None:
        self._append("clicks.jsonl", entry)

    def screenshot(self, page, label: str) -> Path:
        self.steps += 1
        path = self.dir / f"{self.steps:02d}-{label}.png"
        page.screenshot(path=str(path), full_page=True)
        return path

    def finish(
        self,
        *,
        outcome: str,
        files: list[str] | None = None,
        error: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        run = {
            "experiment": self.experiment,
            "report": self.report,
            "outcome": outcome,
            "files": files or [],
            "error": _redact(error) if error else None,
            "duration_s": round(time.monotonic() - self.started, 1),
            "peak_rss_mb": peak_rss_mb(),
            **(extra or {}),
        }
        (self.dir / "run.json").write_text(_redact(json.dumps(run, ensure_ascii=False, indent=1)))
        return run
