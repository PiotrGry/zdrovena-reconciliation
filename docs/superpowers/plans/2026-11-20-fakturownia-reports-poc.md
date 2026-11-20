# Fakturownia report download PoC: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer the PoC questions of zdrovena#583 with evidence from real headless runs against the Fakturownia test account. Nothing may ever reach the tax office.

**Architecture:** A PoC driver in `scripts/poc/` reuses `zdrovena/month_closing/fakturownia_reports.py` (login, no-send guard, headless flows). It adds three things around that module:
- a test-account lock (a host allowlist routed before anything else);
- an evidence folder per run;
- a supervised step-by-step walker for the reports nobody has checked yet.

Production code changes only when a run shows the module is wrong.

**Tech stack:**
- Python 3.12, `uv`;
- Playwright (`report` extra), Chromium;
- pytest;
- Azure CLI (Key Vault `zdrovenakv`).

Design: `docs/superpowers/specs/2026-11-20-fakturownia-reports-poc-design.md`.

**Run context:** Claude runs everything in an interactive session. The operator is on SSH with no visible browser. Screenshots reach the operator through a private Artifact page, and verification codes come from the operator in the terminal.

---

## File structure

| File | Responsibility |
|---|---|
| `scripts/poc/__init__.py` | empty package marker |
| `scripts/poc/fakturownia_poc_safety.py` | Test-account lock: base URL check, host allowlist, `*.gov.pl` always blocked, route handler that records aborted requests |
| `scripts/poc/fakturownia_poc_evidence.py` | One run folder: `requests.jsonl`, `clicks.jsonl`, `run.json`, step screenshots, peak memory of the browser processes |
| `scripts/poc/fakturownia_poc.py` | CLI. `run` executes N automatic runs of one report; `walk` opens a supervised session driven through a command file |
| `tests/test_fakturownia_poc_safety.py` | Unit tests of the lock |
| `tests/test_fakturownia_poc_evidence.py` | Unit tests of the evidence writer |
| `docs/fakturownia-reports-poc.md` | Findings: one answer per design question, with run ids |

The evidence root is `~/.zdrovena/poc-runs/`. It is never committed.

---

### Task 1: Environment

**Files:** none.

- [ ] **Step 1: Install the report extra in the worktree**

Run: `cd /home/pepus/dev/zdrovena-wt-poc && uv sync --extra report`

Then: `uv run python -c "import playwright, sys; print('ok', sys.version)"`

Expected: `ok 3.12…`

- [ ] **Step 2: Check Chromium for this Playwright version**

Run: `uv run playwright install chromium`

Expected: either a download, or nothing because the browser is already present in `~/.cache/ms-playwright`.

- [ ] **Step 3: Check the test credentials without printing them**

Run: `for s in fakturownia-test-login fakturownia-test-password; do az keyvault secret show --vault-name zdrovenakv -n $s --query value -o tsv | wc -c; done`

Expected: two non-zero numbers.

---

### Task 2: Test-account lock

**Files:**
- Create: `scripts/poc/__init__.py` (empty)
- Create: `scripts/poc/fakturownia_poc_safety.py`
- Test: `tests/test_fakturownia_poc_safety.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from scripts.poc.fakturownia_poc_safety import (
    TEST_BASE_URL,
    HostLock,
    check_base_url,
    host_allowed,
)


def test_only_the_test_account_is_accepted():
    assert check_base_url("https://piotrgryzlo.fakturownia.pl") == TEST_BASE_URL
    assert check_base_url("https://piotrgryzlo.fakturownia.pl/") == TEST_BASE_URL
    for url in (
        "https://zdrovena.fakturownia.pl",
        "http://piotrgryzlo.fakturownia.pl",
        "https://piotrgryzlo.fakturownia.pl.evil.com",
        "https://x.piotrgryzlo.fakturownia.pl",
        "",
    ):
        with pytest.raises(SystemExit):
            check_base_url(url)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("piotrgryzlo.fakturownia.pl", True),
        ("PIOTRGRYZLO.fakturownia.pl", True),
        ("zdrovena.fakturownia.pl", False),
        ("fakturownia.pl", False),
        ("e-deklaracje.mf.gov.pl", False),
        ("ksef.mf.gov.pl", False),
        ("podatki.gov.pl", False),
        ("static.example-cdn.com", False),
        ("", False),
    ],
)
def test_host_allowed(host, allowed):
    assert host_allowed(host, extra=()) is allowed


def test_gov_hosts_stay_blocked_even_when_listed():
    assert host_allowed("e-deklaracje.mf.gov.pl", extra=("e-deklaracje.mf.gov.pl", "gov.pl")) is False


def test_extra_static_hosts_match_exactly_or_as_subdomain():
    assert host_allowed("fonts.gstatic.com", extra=("gstatic.com",)) is True
    assert host_allowed("evilgstatic.com", extra=("gstatic.com",)) is False


class _Route:
    def __init__(self, url):
        self.request = type("R", (), {"url": url, "method": "GET", "resource_type": "document"})()
        self.result = None

    def abort(self, reason="blockedbyclient"):
        self.result = ("abort", reason)

    def fallback(self):
        self.result = ("fallback",)


def test_lock_aborts_and_records_everything_outside_the_allowlist():
    lock = HostLock(extra=("gstatic.com",))
    allowed, cdn, other, gov = (
        _Route("https://piotrgryzlo.fakturownia.pl/reports"),
        _Route("https://fonts.gstatic.com/a.woff2"),
        _Route("https://zdrovena.fakturownia.pl/login"),
        _Route("https://e-deklaracje.mf.gov.pl/"),
    )
    for route in (allowed, cdn, other, gov):
        lock.handle_route(route)
    assert allowed.result == ("fallback",)
    assert cdn.result == ("fallback",)
    assert other.result[0] == "abort"
    assert gov.result[0] == "abort"
    assert [entry["url"] for entry in lock.aborted] == [other.request.url, gov.request.url]
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `uv run pytest tests/test_fakturownia_poc_safety.py -q`

Expected: `ModuleNotFoundError: No module named 'scripts.poc'`

- [ ] **Step 3: Implement the lock**

```python
"""Test-account lock for the Fakturownia PoC (zdrovena#583).

The PoC must never reach the production account or any tax office. This lock
runs before the module's own no-send guard: only the test host and the static
hosts listed in EXTRA_HOSTS may load, and *.gov.pl never may.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TEST_HOST = "piotrgryzlo.fakturownia.pl"
TEST_BASE_URL = f"https://{TEST_HOST}"
# Static hosts the test account's pages need, filled from the first run's aborted list (Task 5).
EXTRA_HOSTS: tuple[str, ...] = ()


def check_base_url(url: str) -> str:
    """Return the test base URL, or exit: the PoC never runs against another account."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != TEST_HOST or parsed.path not in ("", "/"):
        raise SystemExit(f"Refusing to run: the PoC runs only against {TEST_BASE_URL}, not {url!r}.")
    return TEST_BASE_URL


def _matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def host_allowed(host: str, *, extra: tuple[str, ...] = EXTRA_HOSTS) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host or _matches(host, "gov.pl"):
        return False
    return host == TEST_HOST or any(_matches(host, item.lower()) for item in extra)


class HostLock:
    """Playwright route handler: fall through for allowed hosts, abort and record the rest."""

    def __init__(self, extra: tuple[str, ...] = EXTRA_HOSTS) -> None:
        self.extra = extra
        self.aborted: list[dict] = []

    def handle_route(self, route) -> None:
        request = route.request
        if host_allowed(urlparse(request.url).hostname or "", extra=self.extra):
            route.fallback()
            return
        self.aborted.append({"url": request.url, "method": request.method, "type": request.resource_type})
        logger.info("PoC lock: aborted %s", request.url)
        route.abort("blockedbyclient")
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest tests/test_fakturownia_poc_safety.py -q`

Expected: `13 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/poc/__init__.py scripts/poc/fakturownia_poc_safety.py tests/test_fakturownia_poc_safety.py
git commit -m "feat(poc): test-account lock for the Fakturownia PoC (#583)"
```

---

### Task 3: Evidence writer

**Files:**
- Create: `scripts/poc/fakturownia_poc_evidence.py`
- Test: `tests/test_fakturownia_poc_evidence.py`

- [ ] **Step 1: Write the failing tests**

```python
import json

from scripts.poc.fakturownia_poc_evidence import Evidence


def test_run_folder_holds_jsonl_logs_and_a_summary(tmp_path):
    ev = Evidence.start(tmp_path, experiment="E1", report="jpk_fa")
    ev.request({"url": "https://piotrgryzlo.fakturownia.pl/a", "allowed": True})
    ev.click({"control": "Generuj raport", "url": "https://piotrgryzlo.fakturownia.pl/reports"})
    ev.finish(outcome="ok", files=["JPK_FA.xml"], extra={"aborted": 0})
    assert ev.dir.parent == tmp_path
    assert ev.dir.name.endswith("-E1-jpk_fa")
    assert [json.loads(line)["url"] for line in (ev.dir / "requests.jsonl").read_text().splitlines()] == ["https://piotrgryzlo.fakturownia.pl/a"]
    assert json.loads((ev.dir / "clicks.jsonl").read_text())["control"] == "Generuj raport"
    run = json.loads((ev.dir / "run.json").read_text())
    assert run["experiment"] == "E1" and run["outcome"] == "ok" and run["files"] == ["JPK_FA.xml"]
    assert run["duration_s"] >= 0 and "peak_rss_mb" in run


def test_secrets_never_reach_the_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "s3cret-value")
    ev = Evidence.start(tmp_path, experiment="E1", report="jpk_fa")
    ev.request({"url": "https://piotrgryzlo.fakturownia.pl/login?p=s3cret-value"})
    ev.finish(outcome="error", error="login failed with s3cret-value")
    for path in ev.dir.iterdir():
        assert "s3cret-value" not in path.read_text(errors="ignore")
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `uv run pytest tests/test_fakturownia_poc_evidence.py -q`

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement the writer**

```python
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
    def start(cls, root: Path = EVIDENCE_ROOT, *, experiment: str, report: str) -> "Evidence":
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

    def finish(self, *, outcome: str, files: list[str] | None = None, error: str | None = None, extra: dict | None = None) -> dict:
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
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest tests/test_fakturownia_poc_evidence.py -q`

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/poc/fakturownia_poc_evidence.py tests/test_fakturownia_poc_evidence.py
git commit -m "feat(poc): evidence folder per PoC run, secrets redacted (#583)"
```

---

### Task 4: The driver (`run` and `walk`)

**Files:**
- Create: `scripts/poc/fakturownia_poc.py`

The driver is a thin script around tested pieces. It is checked by running it (Task 5), not with unit tests.

- [ ] **Step 1: Write the driver**

```python
"""Fakturownia report download PoC driver (zdrovena#583). Test account only.

  uv run python -m scripts.poc.fakturownia_poc run --experiment E1 --report jpk_fa --runs 5 --month 2026-10
  uv run python -m scripts.poc.fakturownia_poc walk --experiment E2 --url /reports/income_tax_records --commands /tmp/walk.cmd

The walk reads one command per line from the command file (appended by Claude), and after each command
writes a screenshot and controls.json to the run folder:
  goto <path>        open a path on the test host
  click <exact text> click the first visible control with exactly this text (send controls stay disabled)
  code <digits>      type a verification code and submit
  wait <ms>          wait
  quit
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from scripts.poc.fakturownia_poc_evidence import EVIDENCE_ROOT, Evidence
from scripts.poc.fakturownia_poc_safety import TEST_BASE_URL, HostLock, check_base_url
from zdrovena.month_closing import fakturownia_reports as fr
from zdrovena.month_closing.config import FAKTUROWNIA_JOB_WAIT_MS, FAKTUROWNIA_REPORTS, fakturownia_profile_dir

CONTROLS_JS = """() => [...document.querySelectorAll('button, a, input[type=submit], input[type=button], [role=button], [role=menuitem]')]
  .filter(el => el.offsetParent !== null)
  .map(el => ({tag: el.tagName, text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80),
               href: el.getAttribute('href'), disabled: el.disabled === true || el.style.pointerEvents === 'none'}))"""


def load_test_credentials() -> None:
    """Test login and password from Key Vault into this process's environment; never printed."""
    for env, secret in (("FAKTUROWNIA_LOGIN", "fakturownia-test-login"), ("FAKTUROWNIA_PASSWORD", "fakturownia-test-password")):
        if not os.environ.get(env):
            os.environ[env] = subprocess.run(
                ["az", "keyvault", "secret", "show", "--vault-name", "zdrovenakv", "-n", secret, "--query", "value", "-o", "tsv"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()


def month_range(month: str) -> tuple[str, str]:
    year, mon = (int(part) for part in month.split("-"))
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"


def open_context(playwright, ev: Evidence, profile: Path | None):
    os.environ["FAKTUROWNIA_WEB_BASE_URL"] = TEST_BASE_URL
    context = playwright.chromium.launch_persistent_context(
        str(profile or fakturownia_profile_dir(TEST_BASE_URL)), headless=True, accept_downloads=True, locale="pl-PL",
    )
    lock = HostLock()
    context.route("**/*", lock.handle_route)          # registered first, so it runs after the module guard
    guard = fr.install_no_send_guard(context)          # runs first: aborts tax-office hosts, then falls back to the lock
    context.on("request", lambda request: ev.request({"url": request.url, "method": request.method}))
    context.tracing.start(screenshots=True, snapshots=True)
    original = fr._click_allowlisted

    def logged_click(locator, control, **kwargs):
        page = locator.page
        ev.click({"control": control.name, "url": page.url, "at": time.time()})
        ev.screenshot(page, "before-" + "".join(c if c.isalnum() else "_" for c in control.name)[:40])
        return original(locator, control, **kwargs)

    fr._click_allowlisted = logged_click
    return context, lock, guard


def close_context(context, ev: Evidence, lock: HostLock, guard) -> dict:
    context.tracing.stop(path=str(ev.dir / "trace.zip"))
    context.close()
    return {"aborted_by_lock": lock.aborted, "aborted_by_guard": guard.blocked}


def cmd_run(args) -> int:
    check_base_url(TEST_BASE_URL)
    load_test_credentials()
    rpt = next(r for r in FAKTUROWNIA_REPORTS if r.get("headless_flow") == args.report)
    date_from, date_to = month_range(args.month)
    failures = 0
    for index in range(args.runs):
        ev = Evidence.start(EVIDENCE_ROOT, experiment=args.experiment, report=args.report)
        with sync_playwright() as playwright:
            context, lock, guard = open_context(playwright, ev, args.profile)
            page = context.new_page()
            outcome, files, error = "ok", [], None
            try:
                login, password = fr._get_credentials()
                fr._login(page, login, password, base_url=TEST_BASE_URL, verification_wait_ms=None)
                fr._dismiss_newsletter_popup(page)
                path = fr.run_headless_report(page, rpt, date_from, date_to, ev.dir, base_url=TEST_BASE_URL,
                                              job_wait_ms=FAKTUROWNIA_JOB_WAIT_MS, step_timeout_ms=15_000)
                files = [f"{path.name} ({path.stat().st_size} B)"]
            except Exception as exc:  # recorded, the next run still happens
                outcome, error, failures = "error", f"{type(exc).__name__}: {exc}", failures + 1
                ev.screenshot(page, "error")
            extra = close_context(context, ev, lock, guard)
        run = ev.finish(outcome=outcome, files=files, error=error, extra=extra)
        print(json.dumps({"run": index + 1, "dir": str(ev.dir), **{k: run[k] for k in ("outcome", "duration_s", "peak_rss_mb", "files", "error")}}, ensure_ascii=False))
    return 1 if failures else 0


def cmd_walk(args) -> int:
    check_base_url(TEST_BASE_URL)
    load_test_credentials()
    ev = Evidence.start(EVIDENCE_ROOT, experiment=args.experiment, report="walk")
    commands = Path(args.commands)
    commands.touch()
    done = 0
    with sync_playwright() as playwright:
        context, lock, guard = open_context(playwright, ev, args.profile)
        page = context.new_page()
        login, password = fr._get_credentials()
        try:
            fr._login(page, login, password, base_url=TEST_BASE_URL, verification_wait_ms=None)
        except Exception as exc:
            print(f"login: {exc}")
        page.goto(TEST_BASE_URL + args.url, wait_until="domcontentloaded")

        def snapshot(label: str) -> None:
            shot = ev.screenshot(page, label)
            (ev.dir / f"{ev.steps:02d}-controls.json").write_text(json.dumps({"url": page.url, "controls": page.evaluate(CONTROLS_JS)}, ensure_ascii=False, indent=1))
            print(f"step {ev.steps}: {page.url} -> {shot.name}", flush=True)

        snapshot("start")
        while True:
            lines = commands.read_text().splitlines()
            if done >= len(lines):
                time.sleep(1)
                continue
            line = lines[done].strip()
            done += 1
            verb, _, arg = line.partition(" ")
            ev.click({"command": line, "url": page.url, "at": time.time()})
            try:
                if verb == "quit":
                    break
                if verb == "goto":
                    page.goto(TEST_BASE_URL + arg, wait_until="domcontentloaded")
                elif verb == "click":
                    if fr.is_send_control_text(arg):
                        raise RuntimeError(f"refused: {arg!r} looks like a send control")
                    with page.expect_download(timeout=15_000) if arg.startswith("download:") else _nullcontext() as download:
                        page.get_by_text(arg.removeprefix("download:"), exact=True).first.click(timeout=10_000)
                    if download is not None:
                        saved = ev.dir / download.value.suggested_filename
                        download.value.save_as(saved)
                        print(f"downloaded {saved.name} ({saved.stat().st_size} B)")
                elif verb == "code":
                    page.fill("input[name*='code'], input[type='text']", arg)
                    page.keyboard.press("Enter")
                elif verb == "wait":
                    page.wait_for_timeout(int(arg))
                page.wait_for_load_state("domcontentloaded")
            except Exception as exc:
                print(f"error: {type(exc).__name__}: {exc}", flush=True)
            snapshot(verb)
        extra = close_context(context, ev, lock, guard)
    ev.finish(outcome="walk", extra=extra)
    print(f"walk finished: {ev.dir}")
    return 0


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--experiment", required=True)
    run.add_argument("--report", required=True, choices=sorted(fr.HEADLESS_FLOWS))
    run.add_argument("--runs", type=int, default=1)
    run.add_argument("--month", required=True)
    run.add_argument("--profile", type=Path)
    walk = sub.add_parser("walk")
    walk.add_argument("--experiment", required=True)
    walk.add_argument("--url", required=True)
    walk.add_argument("--commands", required=True)
    walk.add_argument("--profile", type=Path)
    args = parser.parse_args()
    return cmd_run(args) if args.cmd == "run" else cmd_walk(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Check that it refuses production before anything loads**

Run: `uv run python -c "from scripts.poc.fakturownia_poc_safety import check_base_url; check_base_url('https://zdrovena.fakturownia.pl')"`

Expected: `SystemExit: Refusing to run: the PoC runs only against https://piotrgryzlo.fakturownia.pl …`

- [ ] **Step 3: Lint**

Run: `uv run ruff check scripts/poc tests/test_fakturownia_poc_*.py && uv run ruff format scripts/poc tests/test_fakturownia_poc_*.py`

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add scripts/poc/fakturownia_poc.py
git commit -m "feat(poc): PoC driver with run and supervised walk modes (#583)"
```

---

### Task 5: First run and the static-host allowlist

- [ ] **Step 1: One JPK_FA run**

Run: `uv run python -m scripts.poc.fakturownia_poc run --experiment E1 --report jpk_fa --runs 1 --month 2026-10`

Expected: one JSON line. If the outcome is `error`, read `run.json` and the screenshots before changing anything.

- [ ] **Step 2: Read what the lock aborted**

Run: `jq -r '.aborted_by_lock[].url' ~/.zdrovena/poc-runs/*-E1-jpk_fa/run.json | awk -F/ '{print $3}' | sort | uniq -c`

Decide for each host whether the page needs it. Keep CDNs, fonts and Fakturownia's own static hosts. Leave analytics, marketing and `*.gov.pl` aborted.

- [ ] **Step 3: Fix the allowlist**

Set `EXTRA_HOSTS` in `scripts/poc/fakturownia_poc_safety.py` to the hosts kept in Step 2, for example `("fakturownia.pl.cdn…",)`. Write the exact hosts seen, not this example. Add each one to `test_extra_static_hosts_match_exactly_or_as_subdomain` as an allowed case.

Run: `uv run pytest tests/test_fakturownia_poc_safety.py -q` and expect all to pass.

Commit: `git commit -am "feat(poc): static hosts the test account needs (#583)"`

---

### Task 6: E1, JPK_FA, five runs

- [ ] **Step 1:** Run `uv run python -m scripts.poc.fakturownia_poc run --experiment E1 --report jpk_fa --runs 5 --month 2026-10`
- [ ] **Step 2:** For each run, record the outcome, duration, peak memory and file size in `docs/fakturownia-reports-poc.md` under "E1". Check that every `aborted_by_guard` list is empty or contains only tax-office URLs, and that `clicks.jsonl` holds only allowlisted controls.
- [ ] **Step 3:** If a run failed, record the cause before changing anything. A fix to `fakturownia_reports.py` comes with a test in `tests/test_fakturownia_headless.py` that reproduces it.
- [ ] **Step 4:** Commit the findings: `git add docs/fakturownia-reports-poc.md && git commit -m "docs(poc): E1 JPK_FA results (#583)"`

---

### Task 7: E2, VAT sales register (supervised walk, then automatic)

- [ ] **Step 1:** Start the walk in the background:

  `uv run python -m scripts.poc.fakturownia_poc walk --experiment E2 --url "/reports/income_tax_records" --commands ~/.zdrovena/poc-walk-E2.cmd`

- [ ] **Step 2:** After each step, publish the newest screenshot and `controls.json` on a private Artifact page for the operator. Describe the visible controls and propose the next command.
- [ ] **Step 3:** Append a command to the command file (`echo 'click Generuj raport' >> ~/.zdrovena/poc-walk-E2.cmd`) only if one of these holds:
  - its effect is known: a read-only navigation, "Generuj raport", or a download;
  - the operator has approved it in the terminal.

  Repeat until the PDF is downloaded (`click download:<text>`), then `quit`.
- [ ] **Step 4:** Compare the controls seen with `VAT_REGISTER_DOWNLOAD` and the `vat_register` flow in `fakturownia_reports.py`. Correct the label regexes and the flow to match the real page, with tests in `tests/test_fakturownia_headless.py`, and commit.
- [ ] **Step 5:** Run `uv run python -m scripts.poc.fakturownia_poc run --experiment E2 --report vat_register --runs 5 --month 2026-10`. Record the results under "E2" and commit.

---

### Task 8: E3, JPK_V7M (supervised walk, operator approval, then automatic)

- [ ] **Step 1:** Record the declarations list before anything is touched. Walk to the accounting reports list, take a screenshot, and save it as "before" in the findings.
- [ ] **Step 2:** Start the walk at `/accounting/app/reports/jpk_vat`. Step through it as in Task 7. **Stop before "Zapisz i generuj XML"** and before any control not seen before. Publish the screenshots and get the operator's explicit approval for that click.
- [ ] **Step 3:** After the approved generation, record the declarations list again ("after"). Describe exactly what Fakturownia saved: a draft, a status, a number. Download the XML.
- [ ] **Step 4:** Add a `jpk_v7m` entry to `HEADLESS_FLOWS`, with only the approved controls as new `AllowedControl`s, and a `headless_flow` in the V7M config entry. Add tests for the new label regexes, and commit.
- [ ] **Step 5:** Run `run --experiment E3 --report jpk_v7m --runs 3 --month 2026-10`, recording the declarations list before and after each run. Record under "E3" and commit.

---

### Task 9: E4, login and the verification code

- [ ] **Step 1, reused profile:** E1 already shows it. Record whether any run met `/login/verification`.
- [ ] **Step 2, new profile:** run `run --experiment E4a --report jpk_fa --runs 1 --month 2026-10 --profile ~/.zdrovena/poc-profile-fresh`. Expect the verification page. Then:
  - ask the operator for the sender, subject, address and format of the e-mail;
  - type the code through a walk (`walk --experiment E4a … --profile ~/.zdrovena/poc-profile-fresh`, then `code <digits>`);
  - run E4a again and record whether the profile is now trusted.
- [ ] **Step 3, profile in a container:** covered in Task 11, with a copy of the trusted profile.
- [ ] **Step 4:** Record under "E4" whether the code e-mail could be read automatically: which mailbox it goes to, and whether the app's Zoho client can read it. Commit.

---

### Task 10: E5, guard on the real pages

- [ ] **Step 1:** From all runs so far, list `aborted_by_guard` and `aborted_by_lock`. Confirm that no tax-office request went through: no `gov.pl` URL may appear in `requests.jsonl` without also appearing in an aborted list.

  `jq -r 'select(.url|test("gov\\.pl"))|.url' ~/.zdrovena/poc-runs/*/requests.jsonl`

- [ ] **Step 2:** In a walk on the JPK_FA results page, `click` a link to the Ministry of Finance site that the page shows. Expect the request to be aborted and the page to stay. Record it.
- [ ] **Step 3:** Record under "E5" and commit.

---

### Task 11: E6, Docker

**Files:**
- Create: `scripts/poc/Dockerfile.poc`

- [ ] **Step 1: Write the image**

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra report --no-install-project
COPY zdrovena ./zdrovena
COPY scripts ./scripts
ENV PYTHONPATH=/app
ENTRYPOINT ["uv", "run", "--frozen", "python", "-m", "scripts.poc.fakturownia_poc"]
```

Match the image tag to the Playwright version in `uv.lock`: `grep -A1 'name = "playwright"' uv.lock`.

- [ ] **Step 2: Build and run with a copy of the trusted profile**

```bash
docker build -f scripts/poc/Dockerfile.poc -t zdrovena-fakturownia-poc .
cp -r ~/.zdrovena/fakturownia_profile/piotrgryzlo.fakturownia.pl ~/.zdrovena/poc-profile-docker
docker run --rm -e FAKTUROWNIA_LOGIN -e FAKTUROWNIA_PASSWORD \
  -v ~/.zdrovena/poc-profile-docker:/profile -v ~/.zdrovena/poc-runs:/root/.zdrovena/poc-runs \
  zdrovena-fakturownia-poc run --experiment E6 --report jpk_fa --runs 5 --month 2026-10 --profile /profile
```

Export the credentials into the shell first, without echoing them: `export FAKTUROWNIA_LOGIN=$(az keyvault secret show --vault-name zdrovenakv -n fakturownia-test-login --query value -o tsv)`, and the same for the password.

- [ ] **Step 3:** Repeat for `vat_register` (5 runs) and `jpk_v7m` (3 runs). Record the image size (`docker image ls`), durations and peak memory under "E6". Commit the Dockerfile and the findings.

---

### Task 12: E7, one Azure Container Apps Job run

- [ ] **Step 1: Ask the operator for approval and a resource group.** Show what will be created: an Azure Container Registry image push (or an existing registry), a storage account file share for the profile, and a Container Apps Job in an existing environment. Nothing runs before the operator says yes.
- [ ] **Step 2:** Push the E6 image, upload the trusted profile to the file share, and create the job. Mount the share at `/profile`, pass the test credentials as Key Vault secret references, and use the command `run --experiment E7 --report jpk_fa --runs 1 --month 2026-10 --profile /profile`. Start it once, and read the logs and the run folder from the share.
- [ ] **Step 3:** Record under "E7" whether Azure's IP triggered the verification code. Then delete the job, the share and the pushed image, and confirm they are gone.

---

### Task 13: Findings and handover

- [ ] **Step 1:** Complete `docs/fakturownia-reports-poc.md`:
  - one section per design question (1–6), with the answer and the run folders that show it;
  - the exact steps of each report;
  - what JPK_V7M leaves in Fakturownia;
  - the verification-code decision for the production job.
- [ ] **Step 2:** Tick the experiment checkboxes in zdrovena#583. Comment on #584 with the findings link and the open decisions.
- [ ] **Step 3:** Commit, push the branch and open a PR to `develop`.
