"""Fakturownia report download PoC driver (zdrovena#583). Test account only.

  uv run python -m scripts.poc.fakturownia_poc run --experiment E1 --report jpk_fa --runs 5 --month 2026-10
  uv run python -m scripts.poc.fakturownia_poc walk --experiment E2 --url /reports/income_tax_records --commands /tmp/walk.cmd

The walk reads one command per line from the command file (appended by Claude), and after each command
writes a screenshot and controls.json to the run folder:
  goto <path>        open a path on the test host
  click <exact text> click the first visible control with exactly this text (send controls stay disabled)
  clicksub <text>    click the first visible element whose text contains this text
  fill <label>|<v>   fill a field found by its label
  select <label>|<o> open a dropdown found by its label and pick an option
  check <label>      tick a checkbox found by its label
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

from scripts.poc.fakturownia_poc_evidence import EVIDENCE_ROOT, Evidence, peak_rss_mb
from scripts.poc.fakturownia_poc_safety import TEST_BASE_URL, HostLock, check_base_url
from zdrovena.month_closing import fakturownia_reports as fr
from zdrovena.month_closing import fakturownia_v7m as v7
from zdrovena.month_closing.config import (
    FAKTUROWNIA_JOB_WAIT_MS,
    FAKTUROWNIA_REPORTS,
    fakturownia_profile_dir,
)

CONTROLS_JS = """() => [...document.querySelectorAll('button, a, input[type=submit], input[type=button], [role=button], [role=menuitem], [role=option], [role=combobox]')]
  .filter(el => el.offsetParent !== null)
  .map(el => ({tag: el.tagName, text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80),
               href: el.getAttribute('href'), disabled: el.disabled === true || el.style.pointerEvents === 'none'}))"""


def load_test_credentials() -> None:
    """Test login and password from Key Vault into this process's environment; never printed."""
    for env, secret in (
        ("FAKTUROWNIA_LOGIN", "fakturownia-test-login"),
        ("FAKTUROWNIA_PASSWORD", "fakturownia-test-password"),
    ):
        if not os.environ.get(env):
            os.environ[env] = subprocess.run(
                [
                    "az",
                    "keyvault",
                    "secret",
                    "show",
                    "--vault-name",
                    "zdrovenakv",
                    "-n",
                    secret,
                    "--query",
                    "value",
                    "-o",
                    "tsv",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()


def month_range(month: str) -> tuple[str, str]:
    year, mon = (int(part) for part in month.split("-"))
    return (
        f"{year:04d}-{mon:02d}-01",
        f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}",
    )


_ORIGINAL_CLICK = fr._click_allowlisted


def open_context(playwright, ev: Evidence, profile: Path | None):
    os.environ["FAKTUROWNIA_WEB_BASE_URL"] = TEST_BASE_URL
    context = playwright.chromium.launch_persistent_context(
        str(profile or fakturownia_profile_dir(TEST_BASE_URL)),
        headless=True,
        accept_downloads=True,
        locale="pl-PL",
    )
    lock = HostLock()
    context.route("**/*", lock.handle_route)  # registered first, so it runs after the module guard
    guard = fr.install_no_send_guard(
        context
    )  # runs first: aborts tax-office hosts, then falls back to the lock
    context.on(
        "request", lambda request: ev.request({"url": request.url, "method": request.method})
    )
    context.tracing.start(screenshots=True, snapshots=True)
    original = _ORIGINAL_CLICK  # never a wrapper from an earlier run: clicks go to this run's evidence only

    def logged_click(locator, control, **kwargs):
        page = locator.page
        ev.click({"control": control.name, "url": page.url, "at": time.time()})
        ev.screenshot(
            page, "before-" + "".join(c if c.isalnum() else "_" for c in control.name)[:40]
        )
        return original(locator, control, **kwargs)

    fr._click_allowlisted = logged_click
    return context, lock, guard


def close_context(context, ev: Evidence, lock: HostLock, guard) -> dict:
    peak = peak_rss_mb()  # before close: the browser processes are gone afterwards
    fr._click_allowlisted = _ORIGINAL_CLICK
    context.tracing.stop(path=str(ev.dir / "trace.zip"))
    context.close()
    return {"aborted_by_lock": lock.aborted, "aborted_by_guard": guard.blocked, "peak_rss_mb": peak}


def cmd_run(args) -> int:
    check_base_url(TEST_BASE_URL)
    load_test_credentials()
    rpt = next((r for r in FAKTUROWNIA_REPORTS if r.get("headless_flow") == args.report), None)
    if args.report == "jpk_v7m":
        # Made-up taxpayer data: the PoC runs on the test account only (operator's decision, E3).
        os.environ.setdefault(
            v7.TAXPAYER_ENV,
            json.dumps(
                {
                    "tax_office": "URZĄD SKARBOWY W BOLESŁAWCU",
                    "first_name": "Jan",
                    "last_name": "Testowy",
                    "birth_date": "1980-01-01",
                    "email": "jan.testowy@example.com",
                }
            ),
        )
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
                path = (
                    v7.run_v7m_report(
                        page,
                        args.month,
                        ev.dir,
                        base_url=TEST_BASE_URL,
                        taxpayer=v7.load_taxpayer(),
                        job_wait_ms=FAKTUROWNIA_JOB_WAIT_MS,
                    )
                    if args.report == "jpk_v7m"
                    else fr.run_headless_report(
                        page,
                        rpt,
                        date_from,
                        date_to,
                        ev.dir,
                        base_url=TEST_BASE_URL,
                        job_wait_ms=FAKTUROWNIA_JOB_WAIT_MS,
                        step_timeout_ms=15_000,
                    )
                )
                files = [f"{path.name} ({path.stat().st_size} B)"]
            except Exception as exc:  # recorded, the next run still happens
                outcome, error, failures = "error", f"{type(exc).__name__}: {exc}", failures + 1
                ev.screenshot(page, "error")
            extra = close_context(context, ev, lock, guard)
        run = ev.finish(outcome=outcome, files=files, error=error, extra=extra)
        print(
            json.dumps(
                {
                    "run": index + 1,
                    "dir": str(ev.dir),
                    **{
                        k: run[k]
                        for k in ("outcome", "duration_s", "peak_rss_mb", "files", "error")
                    },
                },
                ensure_ascii=False,
            )
        )
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
            (ev.dir / f"{ev.steps:02d}-controls.json").write_text(
                json.dumps(
                    {"url": page.url, "controls": page.evaluate(CONTROLS_JS)},
                    ensure_ascii=False,
                    indent=1,
                )
            )
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
                    with (
                        page.expect_download(timeout=15_000)
                        if arg.startswith("download:")
                        else _nullcontext() as download
                    ):
                        page.get_by_text(arg.removeprefix("download:"), exact=True).first.click(
                            timeout=10_000
                        )
                    if download is not None:
                        saved = ev.dir / download.value.suggested_filename
                        download.value.save_as(saved)
                        print(f"downloaded {saved.name} ({saved.stat().st_size} B)")
                elif (
                    verb == "clicksub"
                ):  # clicksub <text part>: same send-control check, text need not be exact
                    if fr.is_send_control_text(arg):
                        raise RuntimeError(f"refused: {arg!r} looks like a send control")
                    page.get_by_text(arg, exact=False).first.click(timeout=10_000)
                elif verb == "fill":  # fill <label>|<value>
                    label, _, value = arg.partition("|")
                    page.get_by_label(label, exact=False).first.fill(value, timeout=10_000)
                elif (
                    verb == "select"
                ):  # select <label>|<option text>: material dropdown, then the option
                    label, _, option = arg.partition("|")
                    page.get_by_label(label, exact=False).first.click(timeout=10_000)
                    page.get_by_role("option", name=option).first.click(timeout=10_000)
                elif verb == "check":  # check <label text>
                    page.get_by_label(arg, exact=False).first.check(timeout=10_000)
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
    run.add_argument("--report", required=True, choices=[*sorted(fr.HEADLESS_FLOWS), "jpk_v7m"])
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
