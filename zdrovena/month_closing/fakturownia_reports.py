"""
zdrovena.month_closing.fakturownia_reports – Auto-download Fakturownia reports
===============================================================================
Downloads JPK_FA, JPK_V7M, and VAT Sales Register from Fakturownia's web UI
using Playwright. These reports are only available via the browser interface.

Two modes:

* Unattended (``FAKTUROWNIA_REPORT_HEADLESS=1``): JPK_FA and the VAT sales register
  are generated and downloaded in a headless browser along the path verified by
  hand (open form → "Generuj raport" → job modal link "Gotowe" → "Eksportuj do
  XML"). Reports without such a flow (JPK_V7M) are left for manual download.
* Manual fallback (flag unset): a visible browser with the report sidebar.

Both modes use a persistent profile per host (``~/.zdrovena/fakturownia_profile/<host>``)
and the no-send guard: requests to the tax office hosts are aborted and every
control that could file or sign anything is disabled before any page loads.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from zdrovena.month_closing.config import (
    FAKTUROWNIA_BLOCKED_HOST_SUFFIXES,
    FAKTUROWNIA_SEND_CONTROL_PATTERN,
    FAKTUROWNIA_WEB_BASE_URL_DEFAULT,
)

logger = logging.getLogger("zdrovena.month_closing.fakturownia_reports")

BASE_URL = FAKTUROWNIA_WEB_BASE_URL_DEFAULT
LOGIN_URL = f"{BASE_URL}/login"
DL_LINK_SEL = "#job_download_link a[href*='/jobs/']"
JOB_RESULT_LINK_SEL = "a[href*='/jobs/'][href$='/result']"

SEND_CONTROL_RE = re.compile(FAKTUROWNIA_SEND_CONTROL_PATTERN, re.IGNORECASE)
# The newsletter popup's accept button. Never clicked; listed so the allowlist
# check refuses it even if a selector were ever to match it by accident.
_NEWSLETTER_ACCEPT_RE = re.compile(r"tak,?\s*ch[eę]tnie", re.IGNORECASE)


def _web_base_url() -> str:
    from zdrovena.month_closing.config import fakturownia_web_base_url

    return fakturownia_web_base_url()


def map_to_web_base(url: str, base_url: str) -> str:
    """Point a configured Fakturownia URL at the configured web host.

    Report URLs in ``FAKTUROWNIA_REPORTS`` name ``zdrovena.fakturownia.pl``; any
    ``*.fakturownia.pl`` host is replaced by ``base_url``'s scheme and host, a
    relative URL is resolved against it, and other hosts are left alone.
    """
    base = urlparse(base_url)
    parsed = urlparse(url)
    if not parsed.netloc:
        return urljoin(base_url.rstrip("/") + "/", url)
    host = (parsed.hostname or "").lower()
    if host == "fakturownia.pl" or host.endswith(".fakturownia.pl"):
        return urlunparse(parsed._replace(scheme=base.scheme, netloc=base.netloc))
    return url


# ─── No-send guard ───────────────────────────────────────────────────────────


def is_blocked_host(host: str | None) -> bool:
    """True when ``host`` is, or ends with, one of the tax office host suffixes."""
    if not host:
        return False
    normalized = host.strip().lower().rstrip(".")
    return any(normalized.endswith(suffix) for suffix in FAKTUROWNIA_BLOCKED_HOST_SUFFIXES)


def is_blocked_url(url: str) -> bool:
    try:
        return is_blocked_host(urlparse(url).hostname)
    except ValueError:
        return False


def is_send_control_text(text: str | None) -> bool:
    """True when a control's label looks like sending, signing or filing."""
    return bool(text) and SEND_CONTROL_RE.search(text or "") is not None


def _no_send_init_script() -> str:
    """JS run in every document before page scripts: makes send controls inert."""
    import json

    pattern = json.dumps(FAKTUROWNIA_SEND_CONTROL_PATTERN)
    return """(() => {
  if (window.__zdvNoSendGuard) return;
  window.__zdvNoSendGuard = true;
  const RE = new RegExp(__ZDV_SEND_PATTERN__, "iu");
  const SEL = 'button, a, input[type="submit"], input[type="button"], input[type="image"], '
    + '[role="button"], [role="link"], [role="menuitem"]';
  const label = (el) => [
    el.textContent || "", el.value || "", el.getAttribute("aria-label") || "",
    el.getAttribute("title") || "", el.getAttribute("alt") || "",
  ].join(" ");
  const isSend = (el) => !!el && el.nodeType === 1 && el.matches(SEL) && RE.test(label(el));
  const block = (el) => {
    if (el.getAttribute("data-zdv-blocked") === "1") return;
    el.setAttribute("data-zdv-blocked", "1");
    try { el.disabled = true; } catch (e) {}
    el.setAttribute("disabled", "disabled");
    el.setAttribute("aria-disabled", "true");
    el.style.setProperty("pointer-events", "none", "important");
    if (el.hasAttribute("href")) {
      el.setAttribute("data-zdv-href", el.getAttribute("href"));
      el.removeAttribute("href");
    }
    el.removeAttribute("formaction");
    el.removeAttribute("onclick");
  };
  const scan = (root) => {
    if (!root || !root.querySelectorAll) return;
    if (root.nodeType === 1 && isSend(root)) block(root);
    root.querySelectorAll(SEL).forEach((el) => { if (isSend(el)) block(el); });
  };
  const cancel = (ev) => {
    const path = ev.composedPath ? ev.composedPath() : [ev.target];
    for (const node of path) {
      if (isSend(node) || (node && node.nodeType === 1
          && node.getAttribute("data-zdv-blocked") === "1")) {
        block(node);
        ev.preventDefault();
        ev.stopImmediatePropagation();
        return;
      }
    }
  };
  for (const type of ["click", "auxclick", "dblclick", "mousedown", "mouseup",
                      "pointerdown", "pointerup", "touchstart"]) {
    window.addEventListener(type, cancel, true);
  }
  window.addEventListener("submit", (ev) => {
    if (ev.submitter && isSend(ev.submitter)) {
      block(ev.submitter);
      ev.preventDefault();
      ev.stopImmediatePropagation();
    }
  }, true);
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      const el = m.target.nodeType === 1 ? m.target : m.target.parentElement;
      if (el) {
        const owner = el.closest(SEL);
        if (owner && isSend(owner)) block(owner);
        if (m.type === "childList") {
          m.addedNodes.forEach((n) => { if (n.nodeType === 1) scan(n); });
        }
      }
    }
  });
  observer.observe(document, {
    childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: ["value", "aria-label", "title", "alt"],
  });
  scan(document);
  document.addEventListener("DOMContentLoaded", () => scan(document));
})();""".replace("__ZDV_SEND_PATTERN__", pattern)


class NoSendGuard:
    """Context-wide request filter: aborts every request to a tax office host."""

    def __init__(self) -> None:
        self.blocked: list[str] = []

    def handle_route(self, route) -> None:
        url = route.request.url
        if is_blocked_url(url):
            self.blocked.append(url)
            logger.warning("No-send guard: aborted request to %s", url)
            route.abort("blockedbyclient")
            return
        if "consentmanager" in url:
            route.abort()
            return
        route.fallback()

    def handle_websocket(self, ws) -> None:
        self.blocked.append(ws.url)
        logger.warning("No-send guard: refused websocket to %s", ws.url)
        ws.close()


def install_no_send_guard(context) -> NoSendGuard:
    """Install the guard on a browser context. Call before any page loads."""
    guard = NoSendGuard()
    context.route("**/*", guard.handle_route)
    route_ws = getattr(context, "route_web_socket", None)
    if callable(route_ws):
        route_ws(is_blocked_url, guard.handle_websocket)
    context.add_init_script(script=_no_send_init_script())
    return guard


def _get_credentials() -> tuple[str, str]:
    """Resolve Fakturownia login/password from env vars or keyring."""
    import os

    from zdrovena.common.config import KEYCHAIN_ACCOUNT
    from zdrovena.month_closing.config import (
        KEYCHAIN_SERVICE_FAKTUROWNIA_LOGIN,
        KEYCHAIN_SERVICE_FAKTUROWNIA_PASSWORD,
    )

    login = os.environ.get("FAKTUROWNIA_LOGIN")
    password = os.environ.get("FAKTUROWNIA_PASSWORD")

    if not login:
        try:
            import keyring

            login = keyring.get_password(KEYCHAIN_SERVICE_FAKTUROWNIA_LOGIN, KEYCHAIN_ACCOUNT)
        except Exception:
            pass
    if not password:
        try:
            import keyring

            password = keyring.get_password(KEYCHAIN_SERVICE_FAKTUROWNIA_PASSWORD, KEYCHAIN_ACCOUNT)
        except Exception:
            pass

    if not login or not password:
        raise RuntimeError(
            "Fakturownia login credentials not found.\n"
            "Set FAKTUROWNIA_LOGIN and FAKTUROWNIA_PASSWORD env vars, or store in keyring."
        )
    return login, password


def download_fakturownia_reports(
    reports: list[dict],
    date_from: str,
    date_to: str,
    output_dir: Path,
    *,
    headless: bool = True,
    timeout: int = 120_000,
) -> list[tuple[dict, Path]]:
    """
    Download missing Fakturownia reports via headless browser.

    Parameters
    ----------
    reports : list of report config dicts (from FAKTUROWNIA_REPORTS)
    date_from, date_to : "YYYY-MM-DD"
    output_dir : directory to save downloaded files
    headless : run browser without visible window
    timeout : max wait per report in milliseconds

    Returns
    -------
    List of (report_config, downloaded_path) for successful downloads.
    """
    from zdrovena.month_closing.config import (
        FAKTUROWNIA_REPORT_RUNTIME,
        FAKTUROWNIA_REPORT_TIMEOUT_MS,
    )

    runtime = FAKTUROWNIA_REPORT_RUNTIME.strip().lower()
    resolved_timeout = timeout if timeout != 120_000 else FAKTUROWNIA_REPORT_TIMEOUT_MS
    if runtime == "playwright":
        return _download_reports_with_playwright(
            reports,
            date_from,
            date_to,
            output_dir,
            headless=headless,
            timeout=resolved_timeout,
        )

    logger.warning(
        "Unsupported report runtime '%s' — skipping auto-download (manual fallback remains)",
        runtime,
    )
    return []


def _download_reports_with_playwright(
    reports: list[dict],
    date_from: str,
    date_to: str,
    output_dir: Path,
    *,
    headless: bool,
    timeout: int,
) -> list[tuple[dict, Path]]:
    """Current runtime adapter: Playwright implementation."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        logger.warning("Playwright not installed — skipping auto-download")
        return []

    from zdrovena.month_closing.config import (
        FAKTUROWNIA_JOB_WAIT_MS,
        fakturownia_profile_dir,
        fakturownia_report_headless,
        fakturownia_verification_wait_ms,
    )

    unattended = fakturownia_report_headless()
    if unattended:
        runnable = [r for r in reports if r.get("headless_flow") in HEADLESS_FLOWS]
        for rpt in reports:
            if rpt not in runnable:
                logger.warning(
                    "%s: no unattended flow — download it manually (manual fallback remains)",
                    rpt["name"],
                )
        if not runnable:
            return []
        reports = runnable

    try:
        login, password = _get_credentials()
    except Exception as exc:
        logger.warning("Fakturownia credentials unavailable — skipping auto-download: %s", exc)
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[tuple[dict, Path]] = []

    base_url = _web_base_url()
    verification_wait_ms = fakturownia_verification_wait_ms()
    headless = _resolve_headless(
        reports,
        headless,
        unattended=unattended,
        verification_wait_ms=verification_wait_ms,
    )
    profile_dir = fakturownia_profile_dir(base_url)

    logger.info(
        "Launching browser (headless=%s, unattended=%s, profile=%s) for Fakturownia reports...",
        headless,
        unattended,
        profile_dir,
    )

    with Stealth().use_sync(sync_playwright()) as pw:
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(
                str(profile_dir),
                headless=headless,
                accept_downloads=True,
                service_workers="block",
            )
        except Exception as exc:
            logger.warning("Unable to launch Playwright browser — skipping auto-download: %s", exc)
            if "Executable doesn't exist" in str(exc):
                logger.warning(
                    "Playwright browser binary missing. Install with: "
                    ".venv/bin/python -m playwright install chromium"
                )
            return []

        try:
            # Before any page loads: nothing may reach the tax office from here on.
            install_no_send_guard(context)
            page = context.pages[0] if context.pages else context.new_page()

            try:
                _login(
                    page,
                    login,
                    password,
                    base_url=base_url,
                    verification_wait_ms=verification_wait_ms,
                )
            except Exception as exc:
                logger.error("Fakturownia login failed: %s", exc)
                return []

            if unattended:
                for rpt in reports:
                    try:
                        path = run_headless_report(
                            page,
                            rpt,
                            date_from,
                            date_to,
                            output_dir,
                            base_url=base_url,
                            job_wait_ms=FAKTUROWNIA_JOB_WAIT_MS,
                            step_timeout_ms=timeout,
                        )
                        downloaded.append((rpt, path))
                    except Exception as exc:
                        logger.warning("%s: unattended download failed: %s", rpt["name"], exc)
                return downloaded

            # Separate wizard reports (combined sidebar session) from legacy reports
            wizard_reports = [r for r in reports if r.get("use_wizard_navigation")]
            legacy_reports = [r for r in reports if not r.get("use_wizard_navigation")]

            # All three standard reports use wizard navigation — run them together
            if wizard_reports:
                try:
                    results = _run_all_wizard_reports(
                        page, wizard_reports, date_from, date_to, output_dir, timeout
                    )
                    downloaded.extend(results)
                except Exception as exc:
                    logger.warning("Combined wizard session failed: %s", exc)

            # Legacy non-wizard reports (if any remain)
            for rpt in legacy_reports:
                try:
                    path = _download_one_report(page, rpt, date_from, date_to, output_dir, timeout)
                    if path:
                        downloaded.append((rpt, path))
                except Exception as exc:
                    logger.warning("Failed to download %s: %s", rpt["name"], exc)
        finally:
            try:
                context.close()
            except Exception:
                pass

    return downloaded


def _resolve_headless(
    reports: list[dict],
    headless: bool,
    *,
    unattended: bool,
    verification_wait_ms: int | None,
) -> bool:
    """Whether the browser runs without a window.

    Unattended mode is headless even for wizard reports; the one exception is a
    configured verification wait, which needs a window to type the code into.
    Without the flag, manual-session (wizard) reports need a visible window.
    """
    if unattended:
        return verification_wait_ms is None
    if any(r.get("use_wizard_navigation") for r in reports):
        return False
    return headless


def _url_path(url: str) -> str:
    try:
        return urlparse(url).path.rstrip("/") or "/"
    except ValueError:
        return ""


def _is_login_url(url: str) -> bool:
    path = _url_path(url)
    return path == "/login" or path.startswith("/login/")


def _is_verification_url(url: str) -> bool:
    return _url_path(url).startswith("/login/verification")


def _login(
    page,
    login: str,
    password: str,
    *,
    base_url: str | None = None,
    verification_wait_ms: int | None = None,
) -> None:
    """Make sure the browser session is logged in to Fakturownia. Raises on failure.

    Three cases: the login form (filled and submitted); an already logged-in
    profile (``/login`` redirects away, the form is skipped); and the
    ``/login/verification`` page, which fails with a readable error unless
    ``verification_wait_ms`` allows a person to type the code in the window.
    """
    base = (base_url or _web_base_url()).rstrip("/")
    logger.info("Logging in to Fakturownia (%s)...", base)
    page.goto(f"{base}/login", wait_until="domcontentloaded")

    if not _is_login_url(page.url):
        logger.info("Already logged in (profile session) → %s", page.url)
        return
    if _is_verification_url(page.url):
        _await_verification(page, base, verification_wait_ms)
        return

    form_field = "[name='user_session[login]']"
    try:
        page.wait_for_selector(form_field, timeout=10_000)
    except Exception as exc:
        if not _is_login_url(page.url):
            logger.info("Already logged in (late redirect) → %s", page.url)
            return
        if _is_verification_url(page.url):
            _await_verification(page, base, verification_wait_ms)
            return
        raise RuntimeError(f"Fakturownia login form not found at {page.url}") from exc

    page.fill(form_field, login)
    page.fill("[name='user_session[password]']", password)
    page.click("[name='commit']", force=True)

    try:
        page.wait_for_url(
            lambda url: not _is_login_url(url) or _is_verification_url(url),
            wait_until="domcontentloaded",
            timeout=10_000,
        )
    except Exception as e:
        content = page.content()
        if "Nie masz uprawnień do tego konta!" in content:
            raise RuntimeError(f"Account '{login}' has no access to {base}") from e
        if "Login/Hasło nie są poprawne" in content:
            raise RuntimeError(f"Invalid credentials for '{login}'") from e
        raise RuntimeError(f"Login timed out. URL: {page.url}") from e

    if _is_verification_url(page.url):
        _await_verification(page, base, verification_wait_ms)
        return

    logger.info("Login OK → %s", page.url)


def _await_verification(page, base: str, wait_ms: int | None) -> None:
    """Handle Fakturownia's ``/login/verification`` (code sent to the account owner)."""
    if not wait_ms:
        raise RuntimeError(
            "Fakturownia asks for a login verification code "
            f"({base}/login/verification) and the unattended download cannot type it. "
            "Run once with FAKTUROWNIA_VERIFICATION_WAIT_MS=180000 (a browser window "
            "opens), type the code there; the persistent profile then remembers the "
            "device for later unattended runs."
        )
    logger.warning(
        "Fakturownia asks for a verification code — type it in the browser window "
        "(waiting up to %d s)",
        wait_ms // 1000,
    )
    print(
        f"\n  🔐  Fakturownia verification: type the code in the browser window "
        f"(waiting up to {wait_ms // 1000} s)...\n"
    )
    try:
        page.wait_for_url(
            lambda url: not _is_login_url(url),
            wait_until="domcontentloaded",
            timeout=wait_ms,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Fakturownia verification code was not entered within {wait_ms // 1000} s "
            f"(still at {page.url})"
        ) from exc
    logger.info("Verification OK → %s", page.url)


# ─── Unattended report flows ─────────────────────────────────────────────────


class ReportFlowError(RuntimeError):
    """The unattended flow could not produce the report file."""


class ReportControlMissing(ReportFlowError):
    """A control the flow needs is not on the page; names the control."""

    def __init__(self, control: str, detail: str = "") -> None:
        self.control = control
        self.detail = detail
        message = f"control not found: {control}"
        super().__init__(f"{message} ({detail})" if detail else message)


class NoSendViolation(ReportFlowError):
    """The flow was about to click a control outside the allowlist."""


@dataclass(frozen=True)
class AllowedControl:
    """A control the unattended flow may click: everything else stays untouched."""

    name: str
    roles: tuple[str, ...] = ()
    label_re: re.Pattern[str] | None = None
    css: str | None = None
    href_re: re.Pattern[str] | None = None


GENERATE_REPORT = AllowedControl(
    name="przycisk 'Generuj raport'",
    roles=("button",),
    label_re=re.compile(r"^\s*generuj raport\s*$", re.IGNORECASE),
)
JOB_RESULT_LINK = AllowedControl(
    name=f"link wyniku joba 'Gotowe' ({JOB_RESULT_LINK_SEL})",
    css=JOB_RESULT_LINK_SEL,
    href_re=re.compile(r"/jobs/[^/?#]+/result$"),
)
EXPORT_XML = AllowedControl(
    name="przycisk 'Eksportuj do XML'",
    roles=("button", "link", "menuitem"),
    label_re=re.compile(r"^\s*eksport(?:uj)? do xml\s*$", re.IGNORECASE),
)
VAT_REGISTER_DOWNLOAD = AllowedControl(
    name="przycisk pobrania rejestru VAT ('Eksportuj do PDF' / 'Pobierz PDF')",
    roles=("button", "link", "menuitem"),
    label_re=re.compile(
        r"^\s*(?:eksport(?:uj)? do pdf|pobierz(?: pdf| raport)?|drukuj do pdf)\s*$",
        re.IGNORECASE,
    ),
)
DISMISS_NEWSLETTER = AllowedControl(
    name="przycisk 'Nie, dziękuję' (popup 'Bądź na bieżąco!')",
    roles=("button", "link"),
    label_re=re.compile(r"^\s*nie,?\s*dzi[eę]kuj[eę]\s*$", re.IGNORECASE),
)
ALLOWED_CONTROLS: tuple[AllowedControl, ...] = (
    GENERATE_REPORT,
    JOB_RESULT_LINK,
    EXPORT_XML,
    VAT_REGISTER_DOWNLOAD,
    DISMISS_NEWSLETTER,
)


@dataclass(frozen=True)
class HeadlessFlow:
    """One report's unattended path: generate → job link → export control."""

    generate: AllowedControl
    export: AllowedControl
    file_kind: str  # "xml" | "pdf" — checked on the downloaded bytes


HEADLESS_FLOWS: dict[str, HeadlessFlow] = {
    # Verified by hand on the test account (2026-11-20).
    "jpk_fa": HeadlessFlow(generate=GENERATE_REPORT, export=EXPORT_XML, file_kind="xml"),
    # Same pattern; the VAT register controls were not checked by hand. Clicking the
    # job link may download the PDF directly, otherwise the export control is used.
    "vat_register": HeadlessFlow(
        generate=GENERATE_REPORT, export=VAT_REGISTER_DOWNLOAD, file_kind="pdf"
    ),
}

_ELEMENT_LABEL_JS = (
    "el => [el.innerText || el.textContent || '', el.value || '', "
    "el.getAttribute('aria-label') || '', el.getAttribute('title') || '']"
)


def _control_locator(page, control: AllowedControl):
    """All elements on ``page`` that ``control`` describes (possibly none)."""
    if control.css:
        return page.locator(control.css)
    locator = None
    for role in control.roles:
        candidate = page.get_by_role(role, name=control.label_re)
        locator = candidate if locator is None else locator.or_(candidate)
    if locator is None:
        raise ValueError(f"control {control.name} has neither css nor roles")
    return locator


def _first_visible(locator):
    try:
        count = locator.count()
    except Exception:
        return None
    for i in range(count):
        item = locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _click_allowlisted(locator, control: AllowedControl, *, timeout_ms: int = 10_000) -> None:
    """Click ``locator`` only if it is ``control`` and nothing that sends or subscribes."""
    if control not in ALLOWED_CONTROLS:
        raise NoSendViolation(f"{control.name} is not on the click allowlist")
    raw = locator.evaluate(_ELEMENT_LABEL_JS) or []
    parts = [" ".join(str(p).split()) for p in ([raw] if isinstance(raw, str) else raw)]
    parts = [p for p in parts if p]
    label = " | ".join(parts)
    # Every part counts for refusing (a title "Wyślij" is enough); one is enough to match.
    if any(is_send_control_text(p) or _NEWSLETTER_ACCEPT_RE.search(p) for p in parts):
        raise NoSendViolation(f"refusing to click {label!r} (expected {control.name})")
    if locator.get_attribute("data-zdv-blocked") == "1":
        raise NoSendViolation(f"refusing to click guarded control {label!r}")
    if control.label_re is not None and not any(control.label_re.search(p) for p in parts):
        raise NoSendViolation(f"label {label!r} does not match {control.name}")
    if control.href_re is not None:
        href = locator.get_attribute("href") or ""
        if not control.href_re.search(href.split("?")[0].split("#")[0]):
            raise NoSendViolation(f"href {href!r} does not match {control.name}")
    locator.click(timeout=timeout_ms)


def _dismiss_newsletter_popup(page) -> bool:
    """Close the "Bądź na bieżąco!" popup with "Nie, dziękuję". Never accepts it."""
    try:
        button = _first_visible(_control_locator(page, DISMISS_NEWSLETTER))
        if button is None:
            return False
        logger.info("Dismissing newsletter popup ('Nie, dziękuję')")
        _click_allowlisted(button, DISMISS_NEWSLETTER, timeout_ms=5_000)
        page.wait_for_timeout(200)
        return True
    except NoSendViolation:
        raise
    except Exception as exc:
        logger.debug("Newsletter popup dismissal failed: %s", exc)
        return False


def _fill_report_dates(page, date_from: str, date_to: str) -> None:
    """Best effort: put the period into the form's date fields when they exist."""
    for suffix, value in (("date_from", date_from), ("date_to", date_to)):
        field = page.locator(
            f"input[name='{suffix}'], input[name$='[{suffix}]'], input#{suffix}"
        ).first
        try:
            if field.count() and field.is_visible() and field.is_editable():
                field.fill(value, timeout=3_000)
        except Exception as exc:
            logger.debug("Could not fill %s: %s", suffix, exc)


def _screenshot_beside(page, output_path: Path) -> Path | None:
    shot = output_path.with_name(output_path.name + ".error.png")
    try:
        page.screenshot(path=str(shot), full_page=True)
        return shot
    except Exception as exc:
        logger.debug("Screenshot failed: %s", exc)
        return None


def _check_report_bytes(data: bytes, kind: str) -> str | None:
    """Why ``data`` is not a ``kind`` report, or ``None`` when it looks right."""
    if len(data) < 100:
        return f"file too small ({len(data)} bytes)"
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if kind == "xml":
        if not head.startswith(b"<") or b"<html" in head[:512].lower():
            return "not an XML document"
    elif kind == "pdf" and not head.startswith(b"%PDF"):
        return "not a PDF document"
    return None


def run_headless_report(
    page,
    rpt: dict,
    date_from: str,
    date_to: str,
    output_dir: Path,
    *,
    base_url: str,
    job_wait_ms: int,
    step_timeout_ms: int,
) -> Path:
    """Generate and download one report without a person. Raises ReportFlowError.

    1. open the report form, 2. click "Generuj raport", 3. wait for the job modal's
    result link, 4. click that link (a real click, never goto/fetch), 5. click the
    export control inside ``expect_download`` and save the file. A missing control
    raises ReportControlMissing naming it, with a screenshot beside the output file.
    """
    flow = HEADLESS_FLOWS[rpt["headless_flow"]]
    name = rpt["name"]
    output_path = output_dir / rpt["dest_name"]
    active = [page]
    try:
        _run_headless_steps(
            page,
            rpt,
            flow,
            date_from,
            date_to,
            output_path,
            active,
            base_url=base_url,
            job_wait_ms=job_wait_ms,
            step_timeout_ms=step_timeout_ms,
        )
    except Exception as exc:
        shot = _screenshot_beside(active[-1], output_path)
        where = f"; screenshot: {shot}" if shot else ""
        if isinstance(exc, ReportControlMissing):
            context = "; ".join(p for p in (exc.detail, f"{name}, page {active[-1].url}") if p)
            raise ReportControlMissing(exc.control, f"{context}{where}") from exc
        if isinstance(exc, NoSendViolation):
            raise NoSendViolation(f"{name}: {exc}{where}") from exc
        raise ReportFlowError(f"{name}: {exc}{where}") from exc
    finally:
        for extra in active[1:]:
            try:
                extra.close()
            except Exception:
                pass
    logger.info("%s: saved %s (%d bytes)", name, output_path, output_path.stat().st_size)
    return output_path


def _run_headless_steps(
    page,
    rpt: dict,
    flow: HeadlessFlow,
    date_from: str,
    date_to: str,
    output_path: Path,
    active: list,
    *,
    base_url: str,
    job_wait_ms: int,
    step_timeout_ms: int,
) -> None:
    name = rpt["name"]
    params = f"?date_from={date_from}&date_to={date_to}&currency_convert_to_main=false"
    form_url = _build_report_url(
        map_to_web_base(rpt["url"], base_url),
        params,
        append_date_params=rpt.get("append_date_params", True),
    )

    # Step 1 — open the report form.
    logger.info("%s: opening report form %s", name, form_url)
    page.goto(form_url, wait_until="domcontentloaded")
    if _is_login_url(page.url):
        raise ReportFlowError(f"redirected to login ({page.url}) — session lost")
    _dismiss_newsletter_popup(page)
    _fill_report_dates(page, date_from, date_to)

    # Step 2 — "Generuj raport".
    generate = _first_visible(_control_locator(page, flow.generate))
    if generate is None:
        _dismiss_newsletter_popup(page)
        generate = _first_visible(_control_locator(page, flow.generate))
    if generate is None:
        raise ReportControlMissing(flow.generate.name)
    _click_allowlisted(generate, flow.generate)

    # Step 3 — wait for the job modal's "Gotowe" result link.
    link = _poll(page, lambda: _first_visible(_control_locator(page, JOB_RESULT_LINK)), job_wait_ms)
    if link is None:
        raise ReportControlMissing(
            JOB_RESULT_LINK.name, f"not shown within {job_wait_ms // 1000} s"
        )

    # Step 4 — click the link. The click may open a tab or start the download itself.
    downloads: list = []

    def on_download(download) -> None:
        # A plain function: Playwright cannot register a bound builtin such as list.append
        # ("'builtin_function_or_method' object has no attribute '_pw_impl_instance_'", PoC E1).
        downloads.append(download)
    context = page.context

    def _watch(p) -> None:
        p.on("download", on_download)
        active.append(p)

    page.on("download", on_download)
    context.on("page", _watch)
    try:
        _click_allowlisted(link, JOB_RESULT_LINK)

        # Step 5 — export control inside expect_download (or the link's own download).
        deadline = time.monotonic() + step_timeout_ms / 1000.0
        download = None
        while download is None and time.monotonic() < deadline:
            if downloads:
                download = downloads[0]
                break
            current = active[-1]
            try:
                current.wait_for_load_state("domcontentloaded", timeout=5_000)
            except Exception:
                pass
            _dismiss_newsletter_popup(current)
            export = _first_visible(_control_locator(current, flow.export))
            if export is not None:
                remaining = max(1_000, int((deadline - time.monotonic()) * 1000))
                with current.expect_download(timeout=remaining) as info:
                    _click_allowlisted(export, flow.export)
                download = info.value
                break
            current.wait_for_timeout(250)
    finally:
        try:
            page.remove_listener("download", on_download)
        except Exception:
            pass
        try:
            context.remove_listener("page", _watch)
        except Exception:
            pass

    if download is None:
        raise ReportControlMissing(
            flow.export.name, f"not shown within {step_timeout_ms // 1000} s of the job link"
        )

    tmp_path = output_path.with_name(output_path.name + ".part")
    download.save_as(str(tmp_path))
    problem = _check_report_bytes(tmp_path.read_bytes(), flow.file_kind)
    if problem:
        tmp_path.unlink(missing_ok=True)
        raise ReportFlowError(f"downloaded {download.suggested_filename!r} — {problem}")
    os.replace(tmp_path, output_path)


def _poll(page, probe: Callable, timeout_ms: int):
    """Call ``probe`` until it returns something, dismissing the popup in between."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        found = probe()
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        _dismiss_newsletter_popup(page)
        page.wait_for_timeout(250)


def _download_one_report(
    page,
    rpt: dict,
    date_from: str,
    date_to: str,
    output_dir: Path,
    timeout: int,
) -> Path | None:
    """Navigate to report page, trigger generation, download the file."""
    name = rpt["name"]
    url = rpt["url"]
    dest_name = rpt["dest_name"]
    from zdrovena.month_closing.config import FAKTUROWNIA_REPORT_DOWNLOAD_SELECTOR

    selector = rpt.get("download_selector", FAKTUROWNIA_REPORT_DOWNLOAD_SELECTOR or DL_LINK_SEL)
    button_texts: list[str] = rpt.get("download_button_texts", [])
    append_date_params = rpt.get("append_date_params", True)
    use_wizard_navigation = bool(rpt.get("use_wizard_navigation", False))

    params = (
        f"?date_from={date_from}&date_to={date_to}&submitted=true&currency_convert_to_main=false"
    )
    target_url = _build_report_url(
        map_to_web_base(url, _web_base_url()), params, append_date_params=append_date_params
    )
    logger.info("Loading report page: %s %s", name, target_url)
    page.goto(target_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    output_path = output_dir / dest_name
    if use_wizard_navigation:
        if not _run_manual_browser_session(page, rpt, output_path, timeout):
            return None
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        return output_path

    # For non-wizard reports the XML also comes via the ksiegosoft SPA fetching
    # a pre-signed S3 URL and triggering a blob: download in Chrome.
    # Playwright's expect_download does NOT fire for blob: downloads.
    # page.route() with route.fetch() captures the body before Chrome discards it.
    import re as _re

    non_wizard_captured: list[bytes] = []

    def _intercept_xml_nw(route, req) -> None:
        try:
            response = route.fetch()
            ct = response.headers.get("content-type", "")
            cd = response.headers.get("content-disposition", "")
            body = response.body()
            if len(body) > 500 and (
                "xml" in ct.lower() or ("octet-stream" in ct.lower() and "xml" in cd.lower())
            ):
                print(f"  📡  [nw route captured XML] {req.url[:70]!r} ({len(body)} bytes)")
                non_wizard_captured.append(body)
            route.fulfill(response=response)
        except Exception as exc:
            print(f"  ⚠️  nw route intercept error: {exc}")
            try:
                route.continue_()
            except Exception:
                pass

    _s3_pattern = _re.compile(r"amazonaws\.com")
    page.route(_s3_pattern, _intercept_xml_nw)
    if _try_download_by_button_text(page, output_path, timeout, button_texts):
        # The route may have captured the real body before expect_download saved HTML.
        if non_wizard_captured:
            output_path.write_bytes(non_wizard_captured[-1])
            logger.info(
                "%s: overwritten with route-captured body (%d bytes)",
                name,
                len(non_wizard_captured[-1]),
            )
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            page.unroute(_s3_pattern, _intercept_xml_nw)
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        page.unroute(_s3_pattern, _intercept_xml_nw)
        return output_path
    if _try_generate_and_download(page, output_path, timeout, button_texts=button_texts):
        if non_wizard_captured:
            output_path.write_bytes(non_wizard_captured[-1])
            logger.info(
                "%s: overwritten with route-captured body (%d bytes)",
                name,
                len(non_wizard_captured[-1]),
            )
        size = output_path.stat().st_size
        if size < 100:
            logger.warning("%s: generated file too small (%d bytes), removing", name, size)
            output_path.unlink()
            page.unroute(_s3_pattern, _intercept_xml_nw)
            return None
        logger.info("%s: saved %s (%d bytes)", name, output_path, size)
        page.unroute(_s3_pattern, _intercept_xml_nw)
        return output_path

    logger.info("Waiting for %s report job (timeout=%ds)...", name, timeout // 1000)
    try:
        page.wait_for_selector(selector, state="attached", timeout=timeout)
    except Exception:
        logger.warning("%s: report selector timed out, trying job-id fallback", name)

    # Click the job link; the S3 route will capture the XML body.
    try:
        page.click(selector, force=True, timeout=10_000)
    except Exception:
        pass
    page.wait_for_timeout(3000)

    if non_wizard_captured:
        output_path.write_bytes(non_wizard_captured[-1])
        logger.info("%s: saved via route intercept (%d bytes)", name, len(non_wizard_captured[-1]))
    # Last resort when nothing usable was saved: authenticated HTTP fetch.
    elif (
        not output_path.exists() or output_path.stat().st_size < 100
    ) and not _download_via_job_url(page, selector, output_path, timeout_ms=30_000):
        logger.warning("%s: all download strategies failed", name)
        page.unroute(_s3_pattern, _intercept_xml_nw)
        return None

    page.unroute(_s3_pattern, _intercept_xml_nw)
    size = output_path.stat().st_size
    if size < 100:
        logger.warning("%s: downloaded file too small (%d bytes), removing", name, size)
        output_path.unlink()
        return None

    logger.info("%s: saved %s (%d bytes)", name, output_path, size)
    return output_path


def _build_report_url(base_url: str, params: str, *, append_date_params: bool) -> str:
    if not append_date_params:
        return base_url
    if "?" in base_url:
        return base_url + "&" + params.lstrip("?")
    return base_url + params


def _try_download_by_button_text(
    page,
    output_path: Path,
    timeout_ms: int,
    button_texts: list[str],
) -> bool:
    if _try_v7_generate_then_download(page, output_path, timeout_ms):
        return True
    _accept_pouczenia_if_present(page, button_texts)
    for label in button_texts:
        try:
            if page.get_by_text(label, exact=False).count() == 0:
                continue
            with page.expect_download(timeout=timeout_ms) as download_info:
                page.get_by_text(label, exact=False).first.click(force=True, timeout=5000)
            download = download_info.value
            download.save_as(str(output_path))
            return True
        except Exception:
            continue
    return False


def _try_v7_generate_then_download(page, output_path: Path, timeout_ms: int) -> bool:
    """VAT V7 flow: accept consent, generate XML, then click download button."""
    # This flow mirrors the verified manual sequence from Playwright Inspector:
    # checkbox -> "zapisz i generuj xml" -> "pobierz xml".
    try:
        if page.get_by_role("button", name=re.compile(r"zapisz i generuj xml", re.I)).count() == 0:
            return False
    except Exception:
        return False

    try:
        _accept_pouczenia_if_present(page, ["pobierz xml", "zapisz i generuj xml"])
        page.get_by_role("button", name=re.compile(r"zapisz i generuj xml", re.I)).first.click(
            force=True,
            timeout=10_000,
        )
        with page.expect_download(timeout=timeout_ms) as download_info:
            page.get_by_role("button", name=re.compile(r"pobierz xml", re.I)).first.click(
                force=True,
                timeout=10_000,
            )
        download = download_info.value
        download.save_as(str(output_path))
        return True
    except Exception:
        return False


def _build_multi_sidebar_js(reports_data: list[tuple[str, str, str]]) -> str:
    """Build JS that injects a multi-report sidebar showing all reports at once.

    reports_data: list of (name, url, dest_name) tuples.
    Each row has Go→ (navigates to that report's URL) and Skip⏭ buttons.
    Python polls sessionStorage keys __zdv_done_N and __zdv_skipped_N.
    """
    rows_js = ""
    for i, (name, url, dest_name) in enumerate(reports_data):
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "")
        safe_url = url.replace("\\", "\\\\").replace("'", "\\'")
        safe_dest = dest_name.replace("\\", "\\\\").replace("\n", "")
        rows_js += f"""
    (function() {{
        var row = document.createElement('div');
        row.id = '__zdv_sb_row_{i}';
        row.style.cssText = 'background:#252540;border-radius:6px;padding:10px;margin-bottom:8px;border:1px solid transparent';

        var hdr = document.createElement('div');
        hdr.style.cssText = 'font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:6px';
        var st = document.createElement('span');
        st.id = '__zdv_sb_status_{i}';
        st.textContent = '\\u23f3';
        var nm = document.createElement('span');
        nm.textContent = '{safe_name}';
        hdr.appendChild(st); hdr.appendChild(nm);
        row.appendChild(hdr);

        var dest = document.createElement('div');
        dest.style.cssText = 'font-size:11px;color:#888;margin-bottom:8px';
        dest.textContent = '\\u2192 {safe_dest}';
        row.appendChild(dest);

        var br = document.createElement('div');
        br.style.cssText = 'display:flex;gap:6px';

        var go = document.createElement('button');
        go.style.cssText = 'flex:1;padding:6px;background:#4f46e5;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600';
        go.textContent = 'Go \\u2192';
        go.onclick = function() {{ __zdv_ck_set('__zdv_active','{i}'); window.location.href='{safe_url}'; }};

        var skip = document.createElement('button');
        skip.id = '__zdv_sb_skip_{i}';
        skip.style.cssText = 'padding:6px 10px;background:#374151;color:#9ca3af;border:none;border-radius:4px;cursor:pointer;font-size:12px';
        skip.textContent = 'Skip \\u23ed';
        skip.onclick = function() {{ __zdv_ck_set('__zdv_skipped_{i}','true'); skip.textContent='Skipped'; skip.style.background='#78350f'; skip.style.color='#fbbf24'; }};

        br.appendChild(go); br.appendChild(skip);
        row.appendChild(br);
        sb.appendChild(row);
    }})();
"""
    return f"""(function() {{
    function __zdv_ck_get(k) {{ var m = document.cookie.match('(?:^|; )'+k+'=([^;]*)'); return m ? decodeURIComponent(m[1]) : null; }}
    function __zdv_ck_set(k,v) {{ document.cookie = k+'='+encodeURIComponent(v)+'; path=/; SameSite=Lax; max-age=3600'; }}

    if (document.getElementById('__zdv_sb')) {{
        // Sidebar already present: restore cookie state on re-injection.
        for (var _i = 0; _i < {len(reports_data)}; _i++) {{
            if (__zdv_ck_get('__zdv_done_'+_i) === 'true') {{
                var _s = document.getElementById('__zdv_sb_status_'+_i);
                var _r = document.getElementById('__zdv_sb_row_'+_i);
                if (_s) _s.textContent = '\\u2705';
                if (_r) {{ _r.style.background='#1a3a1a'; _r.style.borderColor='#4caf50'; }}
            }}
            if (__zdv_ck_get('__zdv_skipped_'+_i) === 'true') {{
                var _b = document.getElementById('__zdv_sb_skip_'+_i);
                if (_b) {{ _b.textContent='Skipped'; _b.style.background='#78350f'; _b.style.color='#fbbf24'; }}
            }}
        }}
        return;
    }}

    window.__zdv_mark_done = function(i) {{
        __zdv_ck_set('__zdv_done_' + i, 'true');
        var s = document.getElementById('__zdv_sb_status_' + i);
        var r = document.getElementById('__zdv_sb_row_' + i);
        if (s) s.textContent = '\\u2705';
        if (r) {{ r.style.background='#1a3a1a'; r.style.border='1px solid #4caf50'; }}
    }};

    var sb = document.createElement('div');
    sb.id = '__zdv_sb';
    sb.style.cssText = 'position:fixed;right:0;top:0;width:240px;height:100vh;background:#1a1a2e;color:#fff;z-index:2147483647;padding:14px 10px;box-sizing:border-box;font-family:system-ui,sans-serif;font-size:13px;overflow-y:auto;border-left:2px solid #3a3a5e;box-shadow:-4px 0 12px rgba(0,0,0,.5)';

    var t = document.createElement('div');
    t.style.cssText = 'font-weight:700;font-size:14px;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid #3a3a5e;color:#a78bfa';
    t.textContent = '\\ud83d\\udccb Zdrovena Reports';
    sb.appendChild(t);

    {rows_js}

    // Green "Done" button — closes session with success
    var doneBtn = document.createElement('button');
    doneBtn.id = '__zdv_sb_all_done';
    doneBtn.style.cssText = 'width:100%;padding:10px;background:#16a34a;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:700;margin-top:8px';
    doneBtn.textContent = '\u2705 Done — close session';
    doneBtn.onclick = function() {{
        doneBtn.disabled = true;
        doneBtn.textContent = '\u2705 Closing...';
        doneBtn.style.background = '#15803d';
        __zdv_ck_set('__zdv_all_done', 'true');
    }};
    sb.appendChild(doneBtn);

    document.body.appendChild(sb);

    // Restore cookie state NOW — elements are in the DOM
    for (var _ri = 0; _ri < {len(reports_data)}; _ri++) {{
        if (__zdv_ck_get('__zdv_done_'+_ri) === 'true') {{
            var _rs = document.getElementById('__zdv_sb_status_'+_ri);
            var _rr = document.getElementById('__zdv_sb_row_'+_ri);
            if (_rs) _rs.textContent = '\u2705';
            if (_rr) {{ _rr.style.background='#1a3a1a'; _rr.style.border='1px solid #4caf50'; }}
        }}
        if (__zdv_ck_get('__zdv_skipped_'+_ri) === 'true') {{
            var _rb = document.getElementById('__zdv_sb_skip_'+_ri);
            if (_rb) {{ _rb.textContent='Skipped'; _rb.style.background='#78350f'; _rb.style.color='#fbbf24'; }}
        }}
    }}
}})();"""


def _build_sidebar_js(name: str, url: str, dest_name: str) -> str:
    """Single-report sidebar — kept for backwards compatibility."""
    return _build_multi_sidebar_js([(name, url, dest_name)])


def _auto_fill_jpk_v7m(page) -> None:
    """Best-effort: click Nowy raport → accept consent → zapisz i generuj xml → pobierz xml.

    Silently returns on any failure so the manual sidebar session takes over.
    """
    try:
        # 1. Click "Nowy raport" if present (we may already be on a report page)
        nowy = page.get_by_role("button", name=re.compile(r"nowy raport", re.I))
        if nowy.count() > 0:
            print("  🤖  Auto: clicking 'Nowy raport'...")
            nowy.first.click(force=True, timeout=8_000)
            page.wait_for_timeout(3000)
        else:
            print("  🤖  Auto: 'Nowy raport' not found — skipping (may already be on report page)")

        # 2. Accept consent checkbox
        cb = page.locator("input[type='checkbox']")
        if cb.count() > 0 and not cb.first.is_checked():
            print("  🤖  Auto: accepting consent checkbox...")
            cb.first.click(force=True, timeout=5_000)
            page.wait_for_timeout(500)

        # 3. Click "zapisz i generuj xml"
        gen = page.get_by_role("button", name=re.compile(r"zapisz i generuj xml", re.I))
        if gen.count() == 0:
            print("  🤖  Auto: 'zapisz i generuj xml' not found — user must complete form manually")
            return
        print("  🤖  Auto: clicking 'zapisz i generuj xml'...")
        gen.first.click(force=True, timeout=10_000)
        page.wait_for_timeout(3000)

        # 4. Wait for and click "pobierz xml" (report generation may take a few seconds)
        print("  🤖  Auto: waiting for 'pobierz xml'...")
        for _ in range(12):  # up to 60s
            pobierz = page.get_by_role("button", name=re.compile(r"pobierz xml", re.I))
            if pobierz.count() > 0:
                print("  🤖  Auto: clicking 'pobierz xml'...")
                pobierz.first.click(force=True, timeout=10_000)
                return
            page.wait_for_timeout(5000)
        print("  🤖  Auto: 'pobierz xml' did not appear — user must click manually")
    except Exception as exc:
        print(f"  🤖  Auto form-fill failed ({exc}) — complete manually in browser")


def _auto_fill_vat_register(page) -> None:
    """VAT Sales Register: click 'Generuj raport' to start the async job.

    Unlike JPK_FA, the income_tax_records page does NOT auto-submit the job on
    load — the user (or automation) must click 'Generuj raport'.
    After the job completes the page's JS navigates to S3 with
    Content-Disposition: attachment. The response listener in
    _run_manual_browser_session captures that URL and re-fetches it.
    """
    try:
        # The submit button is input[name='commit'] labelled 'Generuj raport'.
        btn = page.locator("input[name='commit'], button[type='submit']")
        if btn.count() > 0:
            print("  🤖  VAT Register: clicking 'Generuj raport'...")
            btn.first.click(force=True, timeout=8_000)
            page.wait_for_timeout(1000)
        else:
            print("  🤖  VAT Register: 'Generuj raport' not found — complete manually")
    except Exception as exc:
        print(f"  🤖  VAT Register auto-fill failed ({exc}) — complete manually")


def _auto_fill_jpk_fa(page) -> None:
    """JPK_FA download is triggered automatically by the page JS after job completion.

    The URL is loaded with submitted=true which starts the Fakturownia backend job.
    JavaScript on the result page then auto-navigates to /reports/jpk_fa.xml → S3.
    There is no button to click — the download fires at ~8-10s without user input.
    The response listener in _run_manual_browser_session captures the S3 URL.

    We briefly yield to Playwright's event loop so the response event that fires
    during auto-navigation is processed before we return to the poll loop.
    """
    # The page is loaded with submitted=true — Fakturownia JS will automatically
    # poll /jobs/worker.json, then navigate to /reports/jpk_fa.xml → S3.
    # No action needed here; the response listener in _run_manual_browser_session
    # will capture the S3 URL.  Control is given to the user immediately.
    print("  🤖  JPK_FA: browser ready — waiting for auto-download or your action...")


def _auto_fill_for_report(page, rpt: dict) -> None:
    """Dispatch the right auto-fill function for a given report."""
    name = rpt["name"]
    if name == "JPK_FA":
        _auto_fill_jpk_fa(page)
    elif name == "VAT Sales Register":
        _auto_fill_vat_register(page)
    else:
        _auto_fill_jpk_v7m(page)


def _run_all_wizard_reports(
    page,
    reports: list[dict],
    date_from: str,
    date_to: str,
    output_dir: Path,
    timeout_ms: int,
) -> list[tuple[dict, Path]]:
    """Run all wizard-navigation reports in a single browser session.

    Opens one browser tab with a multi-report sidebar showing all reports.
    Navigates to each report URL in turn, auto-fills the form, and captures
    S3 responses via page.on("response").  The poll loop re-fetches each
    captured S3 URL via page.context.request.get() and saves the result.

    The sidebar lets the user skip any report or trigger a report manually
    via the Go → button.  Session ends when all reports are done/skipped or
    the timeout is reached.

    Returns list of (report_config, path) for successfully downloaded reports.
    """

    # Build per-report metadata
    reports_data = []
    output_paths = []
    for rpt in reports:
        url = rpt["url"]
        dest_name = rpt["dest_name"]
        name = rpt["name"]
        append = rpt.get("append_date_params", True)
        params = (
            f"?date_from={date_from}&date_to={date_to}"
            f"&submitted=true&currency_convert_to_main=false"
        )
        nav_url = _build_report_url(
            map_to_web_base(url, _web_base_url()), params, append_date_params=append
        )
        reports_data.append((name, nav_url, dest_name))
        output_paths.append(output_dir / dest_name)

    n = len(reports)
    sidebar_js = _build_multi_sidebar_js(reports_data)

    # One shared response listener for all reports — stores (idx, s3_url) pairs
    pending_s3: list[tuple[int, str, int]] = []  # (report_idx, url, capture_serial)
    _serial: list[int] = [0]

    # Maps captured S3 URLs to which report index they belong.
    # We track the "active" index set when Go→ is clicked (or sequentially).
    active_idx: list[int] = [0]

    def _on_resp(resp) -> None:
        cd = resp.headers.get("content-disposition", "")
        ct = resp.headers.get("content-type", "")
        url = resp.url
        if "amazonaws.com" in url:
            print(f"  🌐  [aws {resp.status}] ct={ct!r}  cd={cd!r}")
            print(f"       url={url[:90]!r}")
        if "amazonaws.com" in url and "attachment" in cd.lower():
            idx = active_idx[0]
            serial = _serial[0]
            _serial[0] += 1
            print(f"  📡  [captured idx={idx}] {url[:80]!r}")
            pending_s3.append((idx, url, serial))

    page.on("response", _on_resp)

    def _inject_sidebar() -> None:
        """Re-inject sidebar if absent (safe to call from poll loop)."""
        try:
            has_sb = page.evaluate("!!document.getElementById('__zdv_sb')")
            if not has_sb:
                page.evaluate(sidebar_js)
        except Exception:
            pass

    # Navigate to first report and inject sidebar
    first_name, first_url, _ = reports_data[0]
    print(f"\n  ▶  Navigating to {first_name}...")
    page.goto(first_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    _inject_sidebar()

    # Auto-fill first report
    _auto_fill_for_report(page, reports[0])

    print(
        f"\n  🌐  Browser open — {n} report(s) in sidebar\n"
        f"     Files will be saved to: {output_dir}\n"
        f"     Use Go → to navigate, Skip ⏭ to skip a report.\n"
    )

    done: dict[int, Path] = {}  # idx → path
    skipped: set[int] = set()
    poll_n = 0
    current_idx = 0
    _all_notified = False
    # No deadline — user closes via the green Done button or by closing the browser.

    while True:
        try:
            page.wait_for_timeout(500)
        except Exception:
            # Browser was closed by the user
            print("  🔒  Browser closed — ending session")
            break
        poll_n += 1

        # Read active index from cookie (user may have clicked Go→)
        try:
            val = page.evaluate(
                "(function(){var m=document.cookie.match(/(?:^|; )__zdv_active=([^;]*)/);return m?decodeURIComponent(m[1]):null;})()"
            )
            if val is not None:
                active_idx[0] = int(val)
        except Exception:
            pass

        # Auto-navigate to next pending report when current one completes/skips
        if current_idx < n and current_idx not in done and current_idx not in skipped:
            pass  # still working on current_idx
        else:
            # Find next unattempted report
            next_idx = None
            for i in range(n):
                if i not in done and i not in skipped:
                    next_idx = i
                    break
            if next_idx is not None and next_idx != current_idx:
                current_idx = next_idx
                active_idx[0] = next_idx
                rpt = reports[next_idx]
                nav_url = reports_data[next_idx][1]
                print(f"\n  ▶  Auto-navigating to {rpt['name']}...")
                try:
                    page.goto(nav_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                    _inject_sidebar()
                    _auto_fill_for_report(page, rpt)
                except Exception as exc:
                    print(f"  ⚠️  Navigation to {rpt['name']} failed: {exc}")

        # Re-inject sidebar whenever it's missing (e.g. after Go→ navigation)
        _inject_sidebar()

        if poll_n % 10 == 0:
            remaining = [reports[i]["name"] for i in range(n) if i not in done and i not in skipped]
            print(
                f"  ⏳  ({poll_n // 2}s) done={len(done)}/{n}  pending_s3={len(pending_s3)}  remaining={remaining}"
            )

        # Process captured S3 URLs
        for item in list(pending_s3):
            idx, s3_url, _ser = item
            pending_s3.remove(item)
            out_path = output_paths[idx]
            rpt_name = reports[idx]["name"]
            try:
                print(f"  🔄  Re-fetching [{rpt_name}] …{s3_url[-50:]!r}")
                api_resp = page.context.request.get(s3_url, timeout=30_000)
                if api_resp.ok:
                    body = api_resp.body()
                    if len(body) > 500:
                        out_path.write_bytes(body)
                        print(f"  📥  Saved [{rpt_name}] ({len(body)} bytes) → {out_path}")
                        logger.info("%s: saved %s (%d bytes)", rpt_name, out_path, len(body))
                    else:
                        print(f"  ⚠️  [{rpt_name}] S3 body too small ({len(body)}b), skipping")
                else:
                    print(f"  ⚠️  [{rpt_name}] S3 re-fetch HTTP {api_resp.status}")
            except Exception as exc:
                print(f"  ❌  [{rpt_name}] S3 re-fetch failed: {exc}")
                logger.warning("%s: S3 re-fetch error: %s", rpt_name, exc)

        # Mark done when file appears
        for i in range(n):
            if i in done or i in skipped:
                continue
            p = output_paths[i]
            if p.exists() and p.stat().st_size >= 100:
                done[i] = p
                try:
                    page.evaluate(f"window.__zdv_mark_done({i})")
                except Exception:
                    pass
                print(f"  ✅  [{reports[i]['name']}] complete")
                logger.info(
                    "%s: download complete (%d bytes)", reports[i]["name"], p.stat().st_size
                )

        # Check skipped via cookie
        for i in range(n):
            if i in done or i in skipped:
                continue
            try:
                if page.evaluate(
                    f"(function(){{var m=document.cookie.match(/(?:^|; )__zdv_skipped_{i}=([^;]*)/);return m?decodeURIComponent(m[1])==='true':false;}})()"
                ):
                    skipped.add(i)
                    print(f"  ⏭  [{reports[i]['name']}] skipped by user")
                    logger.info("%s: skipped by user", reports[i]["name"])
            except Exception:
                pass

        # All accounted for? — notify user and highlight Done button, but don't auto-close
        if len(done) + len(skipped) >= n and not _all_notified:
            _all_notified = True
            print("  ✅  All reports complete — click ✅ Done button to close session")
            try:
                page.evaluate("""
                    var b = document.getElementById('__zdv_sb_all_done');
                    if (b && !b.disabled) {
                        b.textContent = '\u2705 All done \u2014 click to close';
                        b.style.outline = '3px solid #bbf7d0';
                        b.style.boxShadow = '0 0 14px #16a34a';
                    }
                """)
            except Exception:
                pass

        # User clicked the green Done button (or browser closed)
        try:
            if page.evaluate(
                "(function(){var m=document.cookie.match(/(?:^|; )__zdv_all_done=([^;]*)/);return m?decodeURIComponent(m[1])==='true':false;})()"
            ):
                print("  ✅  Done button pressed — closing session")
                break
        except Exception:
            # Browser closed
            print("  🔒  Browser closed — ending session")
            break

    # Cleanup
    try:
        page.remove_listener("response", _on_resp)
    except Exception:
        pass

    return [(reports[i], done[i]) for i in sorted(done)]


def _run_manual_browser_session(
    page,
    rpt: dict,
    output_path: Path,
    timeout_ms: int,
) -> bool:
    """Single-report wizard session (kept for standalone use).

    Used when _download_one_report is called for a single wizard report outside
    the combined _run_all_wizard_reports flow.
    Returns True if the file was downloaded, False if skipped or timed out.
    """
    name = rpt["name"]
    url = rpt["url"]
    dest_name = rpt["dest_name"]

    # Two download patterns exist:
    #
    # JPK_V7M (ksiegosoft SPA): SPA calls fetch() → S3 pre-signed URL → blob: download.
    #   page.route() works for fetch() requests; route.fetch() captures body.
    #
    # JPK_FA (Fakturownia Rails): page navigates /reports/jpk_fa.xml → 302 → S3 with
    #   Content-Disposition: attachment.  Playwright's CDP download interception fires
    #   before page.route() can see the request, so route.fetch() never runs.
    #
    # Unified approach: page.on("response") fires for BOTH patterns.  In the callback
    # we only store the S3 URL string (pure Python, no re-entrancy risk).  In the poll
    # loop we re-fetch the pre-signed URL via page.context.request.get() — safe because
    # it's called from outside any event callback.  Pre-signed S3 URLs are valid for
    # ~15 minutes, so there is plenty of time.
    captured_xml_urls: list[str] = []

    def _on_resp_capture(resp) -> None:
        # Pure Python only — NO Playwright API calls here.
        cd = resp.headers.get("content-disposition", "")
        ct = resp.headers.get("content-type", "")
        url = resp.url
        # Log every AWS hit so we can see what arrives in the browser
        if "amazonaws.com" in url:
            print(f"  🌐  [aws resp {resp.status}] {url[:90]!r}")
            print(f"       ct={ct!r}  cd={cd!r}")
        if "amazonaws.com" in url and "attachment" in cd.lower():
            print(f"  📡  [captured for re-fetch] {url[:80]!r}")
            captured_xml_urls.append(url)

    page.on("response", _on_resp_capture)

    sidebar_js = _build_sidebar_js(name, url, dest_name)

    def _inject_sb() -> None:
        """Re-inject sidebar if absent — safe to call from poll loop."""
        try:
            if not page.evaluate("!!document.getElementById('__zdv_sb')"):
                page.evaluate(sidebar_js)
        except Exception:
            pass

    _inject_sb()

    # --- Auto form-fill attempt (dispatched by report name) ---
    if name == "JPK_FA":
        _auto_fill_jpk_fa(page)
    elif name == "VAT Sales Register":
        _auto_fill_vat_register(page)
    else:
        _auto_fill_jpk_v7m(page)

    print(
        f"\n  🌐  Browser opened for {name}\n"
        f"     The form was pre-filled — waiting for download.\n"
        f"     If nothing happens, complete the steps manually.\n"
        f"     File will be saved to: {output_path}\n"
        f"     Click 'Skip ⏭' to skip and continue.\n"
    )
    logger.info(
        "%s: manual session started (timeout=%ds), waiting for %s",
        name,
        timeout_ms // 1000,
        output_path,
    )

    deadline = time.monotonic() + timeout_ms / 1000.0
    completed = False
    poll_n = 0
    while time.monotonic() < deadline:
        # page.wait_for_timeout yields to Playwright's event loop so
        # download events are processed and pending_downloads gets populated.
        page.wait_for_timeout(500)
        poll_n += 1
        _inject_sb()  # restore sidebar after any navigation
        if poll_n % 10 == 0:  # print every 5s so user knows we're alive
            print(
                f"  ⏳  Waiting... ({poll_n // 2}s elapsed, captured_urls={len(captured_xml_urls)})"
            )

        # Re-fetch any S3 pre-signed URLs captured by the response listener.
        # Called from the main poll loop (NOT inside an event callback) so
        # page.context.request.get() is safe — no re-entrancy risk.
        for s3_url in list(captured_xml_urls):
            captured_xml_urls.remove(s3_url)
            try:
                print(f"  🔄  Re-fetching S3 URL …{s3_url[-50:]!r}")
                api_resp = page.context.request.get(s3_url, timeout=30_000)
                if api_resp.ok:
                    body = api_resp.body()
                    if len(body) > 500:
                        output_path.write_bytes(body)
                        print(f"  📥  Saved via S3 re-fetch ({len(body)} bytes) → {output_path}")
                        logger.info(
                            "%s: S3 re-fetch saved to %s (%d bytes)", name, output_path, len(body)
                        )
                    else:
                        print(f"  ⚠️  S3 re-fetch body too small ({len(body)} bytes), skipping")
                else:
                    print(f"  ⚠️  S3 re-fetch returned HTTP {api_resp.status}")
            except Exception as exc:
                print(f"  ❌  S3 re-fetch failed: {exc}")
                logger.warning("%s: S3 re-fetch error: %s", name, exc)

        if output_path.exists() and output_path.stat().st_size >= 100:
            try:
                page.evaluate("window.__zdv_mark_done(0)")
            except Exception:
                pass
            completed = True
            logger.info("%s: file detected at %s — session complete", name, output_path)
            page.wait_for_timeout(2000)  # let user see the ✅
            break

        try:
            skipped = page.evaluate(
                "(function(){var m=document.cookie.match(/(?:^|; )__zdv_skipped_0=([^;]*)/);return m?decodeURIComponent(m[1])==='true':false;})()"
            )
        except Exception:
            skipped = False
        if skipped:
            logger.info("%s: skipped by user", name)
            break

    try:
        page.remove_listener("response", _on_resp_capture)
    except Exception:
        pass

    return completed


def _try_v7_wizard_download(page, output_path: Path, timeout_ms: int) -> bool:
    """VAT V7 path verified in UI: navigate wizard and download XML."""
    try:
        # These steps are resilient no-ops when the link/button is absent.
        _safe_click_role(page, "link", r"Raporty")
        _safe_click_role(page, "link", r"Moje JPK")
        _safe_click_role(page, "link", r"Nowy JPK V7")
        _safe_click_role(page, "button", r"Nowy raport")
        _accept_pouczenia_if_present(page, ["pobierz xml", "zapisz i generuj xml"])
        _safe_click_role(page, "button", r"zapisz i generuj xml", required=True)
        with page.expect_download(timeout=timeout_ms) as download_info:
            _safe_click_role(page, "button", r"pobierz xml", required=True)
        download = download_info.value
        download.save_as(str(output_path))
        return True
    except Exception:
        return False


def _safe_click_role(page, role: str, name_pattern: str, *, required: bool = False) -> bool:
    locator = page.get_by_role(role, name=re.compile(name_pattern, re.I))
    if locator.count() == 0:
        if required:
            raise RuntimeError(f"Required {role} '{name_pattern}' not found")
        return False
    locator.first.click(force=True, timeout=10_000)
    page.wait_for_timeout(300)
    return True


def _accept_pouczenia_if_present(page, button_texts: list[str]) -> None:
    """JPK_V7 pages can require consent checkbox before XML button is enabled."""
    lowered = [b.strip().lower() for b in button_texts]
    needs_consent = any("pobierz xml" in b or "zapisz i generuj xml" in b for b in lowered)
    if not needs_consent:
        return
    try:
        checkbox = page.locator("input[type='checkbox']").first
        if page.locator("input[type='checkbox']").count() == 0:
            return
        if not checkbox.is_checked():
            checkbox.click(force=True, timeout=5000)
            page.wait_for_timeout(500)
    except Exception:
        # If consent UI is absent or custom-wired, keep fallback behavior.
        return


def _extract_job_result_href(page, selector: str) -> str | None:
    """Get /jobs/{id}/result href from DOM or URL query."""
    href: str | None = None
    try:
        href = page.locator(selector).first.get_attribute("href")
    except Exception:
        href = None
    if not href:
        # Fallback: broader anchor lookup, used by some report pages.
        try:
            href = page.locator("a[href*='/jobs/'][href$='/result']").first.get_attribute("href")
        except Exception:
            href = None
    if href:
        return href

    # Final fallback: derive from ?job_id=... URL query.
    parsed = urlparse(page.url)
    job_id = parse_qs(parsed.query).get("job_id", [None])[0]
    if not job_id:
        m = re.search(r"/jobs/(\d+)/", parsed.path)
        job_id = m.group(1) if m else None
    if job_id:
        return f"/jobs/{job_id}/result"
    return None


def _has_job_link(page) -> bool:
    try:
        return page.locator("a[href*='/jobs/']").count() > 0
    except Exception:
        return False


def _try_generate_and_download(
    page, output_path: Path, timeout_ms: int, *, button_texts: list[str] | None = None
) -> bool:
    """Click 'Generuj raport', wait for the async job result, then download it.

    Fakturownia report generation is asynchronous: clicking the submit button
    starts a background job.  The download link appears in one of two ways
    depending on the report type:

    * ``#job_download_link a[href*='/jobs/']`` — modal job link (e.g. VAT
      Sales Register), which is present but hidden inside a Bootstrap modal;
      we use the direct HTTP fetch fallback for this case.
    * A named export button (e.g. "Eksport do XML" for JPK_FA) that appears
      in the page body once the job completes.

    We therefore try both paths after clicking the generate button.
    """
    if _has_job_link(page):
        # Job link already present (e.g. submitted=true auto-fired the job).
        # The main wait_for_selector path in _download_one_report will handle it.
        return False

    # Most report pages use input[name='commit'] labelled "Generuj raport".
    for generate_selector in (
        "input[name='commit']",
        "button[type='submit']",
    ):
        if page.locator(generate_selector).count() == 0:
            continue
        try:
            page.click(generate_selector, force=True, timeout=5000)
        except Exception:
            continue

        # After clicking the generate button, poll until the job result appears.
        # Two result patterns exist depending on the report type:
        #   A) A hidden-modal job link: `#job_download_link a[href*='/jobs/']`
        #      (e.g. VAT Sales Register) — download via direct HTTP fetch.
        #   B) A named export button (e.g. "Eksport do XML" for JPK_FA) that
        #      appears in the page body once the backend job completes.
        # We poll for both simultaneously so neither blocks the other.
        combined_job_sel = (
            "#job_download_link a[href*='/jobs/'], a[href*='/jobs/'][href$='/result']"
        )
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            # Path A — job link (hidden modal)
            if page.locator(combined_job_sel).count() > 0:
                if _download_via_job_url(page, combined_job_sel, output_path, timeout_ms=30_000):
                    return True
                try:
                    with page.expect_download(timeout=30_000) as dl_info:
                        page.click(combined_job_sel, force=True, timeout=10_000)
                    dl_info.value.save_as(str(output_path))
                    return True
                except Exception:
                    pass

            # Path B — named export button appears after job completion
            if button_texts:
                for label in button_texts:
                    try:
                        if page.get_by_text(label, exact=False).count() == 0:
                            continue
                        with page.expect_download(timeout=30_000) as dl_info:
                            page.get_by_text(label, exact=False).first.click(
                                force=True, timeout=5_000
                            )
                        dl_info.value.save_as(str(output_path))
                        return True
                    except Exception:
                        continue

            page.wait_for_timeout(2_000)

        # Button was clicked but no result appeared — stop trying.
        return False
    return False


def _download_via_job_url(page, selector: str, output_path: Path, *, timeout_ms: int) -> bool:
    """Fallback: poll for job URL and fetch authenticated result endpoint."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    href = _extract_job_result_href(page, selector)
    while not href and time.monotonic() < deadline:
        href = _extract_job_result_href(page, selector)
        if href:
            break
        page.wait_for_timeout(2000)

    if not href:
        logger.warning("Unable to resolve report job link from DOM or URL")
        return False
    if not href:
        return False
    download_url = href if href.startswith("http") else map_to_web_base(href, _web_base_url())
    try:
        response = page.context.request.get(download_url, timeout=30_000)
    except Exception as exc:
        logger.warning("Direct report download request failed: %s", exc)
        return False
    if not response.ok:
        logger.warning("Direct report download HTTP error for %s", download_url)
        return False
    output_path.write_bytes(response.body())
    return True
