"""Unattended Fakturownia report download and the no-send guard (issue #579).

No network: a local HTTP server imitates the Fakturownia pages (login,
verification, report form, job modal, result page, newsletter popup, a page with
"Wyślij do urzędu") and the real Playwright flow runs against it. When Chromium
cannot start here, the browser tests skip; the pure unit tests always run.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zdrovena.month_closing import config as mc_config
from zdrovena.month_closing import fakturownia_reports as fr

# ─── Pure unit tests ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_report_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKTUROWNIA_PROFILE_DIR", str(tmp_path / "profiles"))
    for name in (
        "FAKTUROWNIA_REPORT_HEADLESS",
        "FAKTUROWNIA_WEB_BASE_URL",
        "FAKTUROWNIA_VERIFICATION_WAIT_MS",
    ):
        monkeypatch.delenv(name, raising=False)


class TestBlockedHosts:
    @pytest.mark.parametrize(
        "host",
        [
            "mf.gov.pl",
            "e-deklaracje.mf.gov.pl",
            "api.ksef.mf.gov.pl",
            "ksef.gov.pl",
            "www.podatki.gov.pl",
            "e-deklaracje.gov.pl",
            "E-Deklaracje.MF.gov.pl.",
        ],
    )
    def test_tax_office_hosts_are_blocked(self, host):
        assert fr.is_blocked_host(host)

    @pytest.mark.parametrize(
        "host",
        ["zdrovena.fakturownia.pl", "127.0.0.1", "gov.pl", "mf.gov.pl.example.com", "", None],
    )
    def test_other_hosts_pass(self, host):
        assert not fr.is_blocked_host(host)

    def test_url_matching_uses_the_host_only(self):
        assert fr.is_blocked_url("https://e-deklaracje.mf.gov.pl/")
        assert fr.is_blocked_url("wss://ksef.gov.pl:443/socket")
        assert not fr.is_blocked_url("https://zdrovena.fakturownia.pl/?next=mf.gov.pl")
        assert not fr.is_blocked_url("data:text/html,mf.gov.pl")


class TestSendControlPattern:
    @pytest.mark.parametrize(
        "label",
        [
            "Wyślij do urzędu",
            "WYSLIJ",
            "Wysyłka JPK",
            "wysylka",
            "Podpisz i wyślij",
            "e-Deklaracje",
            "Edeklaracje",
            "Złóż do urzędu",
            "Przekaż do urzedu",
            "Wyślij do MF",
            "przekaz do mf",
        ],
    )
    def test_send_labels_match(self, label):
        assert fr.is_send_control_text(label)

    @pytest.mark.parametrize(
        "label",
        [
            "Generuj raport",
            "Gotowe",
            "Eksportuj do XML",
            "Pobierz PDF",
            "Nie, dziękuję",
            "Tak, chętnie!",
            "do mfa",
            "",
            None,
        ],
    )
    def test_download_labels_do_not_match(self, label):
        assert not fr.is_send_control_text(label)

    def test_the_init_script_carries_the_same_pattern(self):
        script = fr._no_send_init_script()
        assert "__ZDV_SEND_PATTERN__" not in script
        assert '"wy[s\\u015b]lij' in script or '"wy[sś]lij' in script


class TestUrlMapping:
    def test_configured_host_is_replaced(self):
        mapped = fr.map_to_web_base(
            "https://zdrovena.fakturownia.pl/reports/jpk_fa?kind=jpk_fa", "http://127.0.0.1:9000"
        )
        assert mapped == "http://127.0.0.1:9000/reports/jpk_fa?kind=jpk_fa"

    def test_any_fakturownia_subdomain_is_replaced(self):
        assert (
            fr.map_to_web_base("https://other.fakturownia.pl/a", "https://test.fakturownia.pl")
            == "https://test.fakturownia.pl/a"
        )

    def test_relative_urls_resolve_against_the_base(self):
        assert (
            fr.map_to_web_base("/jobs/1/result", "https://x.fakturownia.pl")
            == "https://x.fakturownia.pl/jobs/1/result"
        )

    def test_foreign_hosts_are_left_alone(self):
        url = "https://bucket.s3.amazonaws.com/f.xml?sig=1"
        assert fr.map_to_web_base(url, "http://127.0.0.1:1") == url

    def test_base_url_from_env(self, monkeypatch):
        assert mc_config.fakturownia_web_base_url() == "https://zdrovena.fakturownia.pl"
        monkeypatch.setenv("FAKTUROWNIA_WEB_BASE_URL", "https://test.fakturownia.pl/")
        assert mc_config.fakturownia_web_base_url() == "https://test.fakturownia.pl"

    def test_profile_dir_is_per_host(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FAKTUROWNIA_PROFILE_DIR", str(tmp_path))
        assert mc_config.fakturownia_profile_dir("https://zdrovena.fakturownia.pl") == (
            tmp_path / "zdrovena.fakturownia.pl"
        )
        assert mc_config.fakturownia_profile_dir("http://127.0.0.1:8080") == (
            tmp_path / "127.0.0.1_8080"
        )

    def test_default_profile_root(self, monkeypatch):
        monkeypatch.delenv("FAKTUROWNIA_PROFILE_DIR", raising=False)
        assert mc_config.fakturownia_profile_dir("https://a.fakturownia.pl") == (
            Path.home() / ".zdrovena" / "fakturownia_profile" / "a.fakturownia.pl"
        )


class TestModeFlags:
    @pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), ("0", False)])
    def test_headless_flag(self, monkeypatch, raw, expected):
        monkeypatch.setenv("FAKTUROWNIA_REPORT_HEADLESS", raw)
        assert mc_config.fakturownia_report_headless() is expected

    @pytest.mark.parametrize(("raw", "expected"), [("", None), ("x", None), ("0", None), ("5", 5)])
    def test_verification_wait(self, monkeypatch, raw, expected):
        monkeypatch.setenv("FAKTUROWNIA_VERIFICATION_WAIT_MS", raw)
        assert mc_config.fakturownia_verification_wait_ms() == expected

    def test_unattended_is_headless_even_for_wizard_reports(self):
        wizard = [{"use_wizard_navigation": True}]
        assert fr._resolve_headless(wizard, False, unattended=True, verification_wait_ms=None)

    def test_verification_wait_needs_a_window(self):
        assert not fr._resolve_headless([], True, unattended=True, verification_wait_ms=1000)

    def test_manual_mode_keeps_the_visible_sidebar_session(self):
        wizard = [{"use_wizard_navigation": True}]
        assert not fr._resolve_headless(wizard, True, unattended=False, verification_wait_ms=None)
        assert fr._resolve_headless([{}], True, unattended=False, verification_wait_ms=None)

    def test_only_jpk_fa_and_vat_register_have_an_unattended_flow(self):
        flows = {r["name"]: r.get("headless_flow") for r in mc_config.FAKTUROWNIA_REPORTS}
        assert flows == {"JPK_FA": "jpk_fa", "JPK_V7M": None, "VAT Sales Register": "vat_register"}
        assert set(fr.HEADLESS_FLOWS) == {"jpk_fa", "vat_register"}


class TestClickAllowlist:
    def _locator(self, label: str | list[str], href: str | None = None) -> MagicMock:
        loc = MagicMock()
        loc.evaluate.return_value = label
        loc.get_attribute.side_effect = lambda name: href if name == "href" else None
        return loc

    def test_allowlisted_control_is_clicked(self):
        loc = self._locator("Generuj raport")
        fr._click_allowlisted(loc, fr.GENERATE_REPORT)
        loc.click.assert_called_once()

    @pytest.mark.parametrize("label", ["Wyślij do urzędu", "Tak, chętnie!", "Generuj i wyślij"])
    def test_send_or_subscribe_labels_are_refused(self, label):
        loc = self._locator(label)
        with pytest.raises(fr.NoSendViolation):
            fr._click_allowlisted(loc, fr.GENERATE_REPORT)
        loc.click.assert_not_called()

    def test_label_parts_are_checked_separately(self):
        dup = self._locator(["", "Generuj raport", "", "Generuj raport"])
        fr._click_allowlisted(dup, fr.GENERATE_REPORT)
        dup.click.assert_called_once()
        sneaky = self._locator(["Generuj raport", "", "", "Wyślij do urzędu"])
        with pytest.raises(fr.NoSendViolation):
            fr._click_allowlisted(sneaky, fr.GENERATE_REPORT)
        sneaky.click.assert_not_called()

    def test_a_control_outside_the_allowlist_is_refused(self):
        rogue = fr.AllowedControl(name="rogue", roles=("button",), label_re=re.compile("x"))
        loc = self._locator("x")
        with pytest.raises(fr.NoSendViolation):
            fr._click_allowlisted(loc, rogue)
        loc.click.assert_not_called()

    def test_job_link_must_point_at_a_job_result(self):
        good = self._locator("Gotowe", "/jobs/42/result")
        fr._click_allowlisted(good, fr.JOB_RESULT_LINK)
        good.click.assert_called_once()
        bad = self._locator("Gotowe", "https://e-deklaracje.mf.gov.pl/jobs/1/send")
        with pytest.raises(fr.NoSendViolation):
            fr._click_allowlisted(bad, fr.JOB_RESULT_LINK)

    def test_dismiss_control_never_matches_the_accept_button(self):
        assert fr.DISMISS_NEWSLETTER.label_re.search("Nie, dziękuję")
        assert not fr.DISMISS_NEWSLETTER.label_re.search("Tak, chętnie!")


class TestReportBytes:
    def test_xml(self):
        assert fr._check_report_bytes(b"<?xml version='1.0'?><JPK>" + b"x" * 200, "xml") is None
        assert fr._check_report_bytes(b"<!DOCTYPE html><html>" + b"x" * 200, "xml")
        assert fr._check_report_bytes(b"<x/>", "xml")

    def test_pdf(self):
        assert fr._check_report_bytes(b"%PDF-1.4" + b"x" * 200, "pdf") is None
        assert fr._check_report_bytes(b"<html>" + b"x" * 200, "pdf")


# ─── Local imitation of the Fakturownia web UI ───────────────────────────────

JPK_FA_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n<JPK xmlns="http://jpk.mf.gov.pl/wzor/2022/02/17/02171/">'
    b"<Naglowek><DataOd>2026-10-01</DataOd><DataDo>2026-10-31</DataDo></Naglowek>"
    + b"<Faktura/>" * 30
    + b"</JPK>\n"
)
VAT_PDF = b"%PDF-1.4\n" + b"% rejestr sprzedazy VAT\n" * 20 + b"%%EOF\n"

NEWSLETTER_POPUP = """
<div id="nl" style="position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000">
  <div style="background:#fff;margin:80px auto;width:300px;padding:20px">
    <h3>Bądź na bieżąco!</h3>
    <button id="nl-yes" onclick="location.href='/newsletter/subscribe'">Tak, chętnie!</button>
    <button id="nl-no"
      onclick="fetch('/newsletter/declined');document.getElementById('nl').remove()"
    >Nie, dziękuję</button>
  </div>
</div>"""


def _page(body: str) -> bytes:
    return f"<!doctype html><html><head><meta charset='utf-8'></head><body>{body}</body></html>".encode()


def _report_form(
    kind: str, job_id: int, *, start: str = "Generuj raport", link: str = "Gotowe"
) -> bytes:
    # The VAT register (PoC E2): "Generuj raport" only shows the table; "Drukuj do PDF" starts
    # the job, and its modal offers "Pobierz".
    extra = (
        ""
        if start == "Generuj raport"
        else '<input type="button" value="Generuj raport" onclick="window.__table = true">'
    )
    return _page(
        f"""
<h1>Raport {kind}</h1>
<form id="f" onsubmit="event.preventDefault(); startJob()">
  <input name="date_from"><input name="date_to">
  {extra}
  <input type="submit" name="commit" value="{start}">
</form>
<button id="send-on-form" onclick="fetch('/sent')">Wyślij do urzędu</button>
<div id="modal" style="display:none">
  <p>Raport w przygotowaniu…</p>
  <a id="done" href="/jobs/{job_id}/result">{link}</a>
</div>
<script>
  function startJob() {{
    const p = new URLSearchParams(new FormData(document.getElementById('f')));
    fetch('/generate?kind={kind}&' + p.toString());
    setTimeout(() => {{ document.getElementById('modal').style.display = 'block'; }}, 400);
  }}
</script>
{NEWSLETTER_POPUP}"""
    )


SEND_PAGE = _page(
    """
<button id="send" onclick="window.__sent = true; fetch('/sent')">Wyślij do urzędu</button>
<a id="edekl" href="https://e-deklaracje.mf.gov.pl/" onclick="window.__sent = true">e-Deklaracje</a>
<form action="/sent" method="post"><input id="sub" type="submit" value="Wyślij JPK"></form>
<button id="gen" onclick="window.__generated = true">Generuj raport</button>
<div id="later"></div>
<script>
  setTimeout(() => {
    const b = document.createElement('button');
    b.id = 'late';
    b.textContent = 'Podpisz i wyślij';
    b.onclick = () => { window.__sent = true; fetch('/sent'); };
    document.getElementById('later').appendChild(b);
  }, 150);
</script>"""
)


@dataclass
class FakeFakturownia:
    verification: bool = False
    verification_auto_pass_ms: int | None = None  # simulates a person typing the code
    vat_result: str = "page"  # "page" | "direct" | "missing"
    requests: list[str] = field(default_factory=list)
    login_posts: int = 0
    base_url: str = ""

    def hits(self, prefix: str) -> list[str]:
        return [r for r in self.requests if r.split(" ", 1)[1].startswith(prefix)]


def _make_handler(state: FakeFakturownia):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _logged_in(self) -> bool:
            return "sid=ok" in (self.headers.get("Cookie") or "")

        def _send(self, status, body=b"", headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            if "Content-Type" not in (headers or {}):
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location, cookie=None):
            headers = {"Location": location}
            if cookie:
                headers["Set-Cookie"] = cookie
            self._send(302, b"", headers)

        def do_POST(self):
            state.requests.append(f"POST {self.path}")
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if self.path == "/login":
                state.login_posts += 1
                if state.verification:
                    self._redirect("/login/verification", "pending=1; Path=/")
                else:
                    self._redirect("/dashboard", "sid=ok; Path=/; Max-Age=86400")
                return
            self._send(404, b"not found")

        def do_GET(self):
            state.requests.append(f"GET {self.path}")
            path = self.path.split("?", 1)[0]
            if path == "/login":
                if self._logged_in():
                    self._redirect("/dashboard")
                    return
                self._send(
                    200,
                    _page(
                        """
<form method="post" action="/login">
  <input name="user_session[login]"><input type="password" name="user_session[password]">
  <input type="submit" name="commit" value="Zaloguj">
</form>"""
                    ),
                )
                return
            if path == "/login/verification":
                script = ""
                if state.verification_auto_pass_ms is not None:
                    script = (
                        "<script>setTimeout(() => { document.cookie = 'sid=ok; path=/; "
                        f"max-age=86400'; location.href = '/dashboard'; }}, "
                        f"{state.verification_auto_pass_ms});</script>"
                    )
                self._send(
                    200,
                    _page(
                        "<h1>Weryfikacja</h1><p>Wpisz kod wysłany na e-mail</p>"
                        f"<input name='code'><button>Zweryfikuj</button>{script}"
                    ),
                )
                return
            if not self._logged_in():
                self._redirect("/login")
                return
            if path == "/dashboard":
                self._send(200, _page("<h1>Pulpit</h1>" + NEWSLETTER_POPUP))
            elif path == "/reports/jpk_fa":
                self._send(200, _report_form("jpk_fa", 42))
            elif path == "/reports/income_tax_records":
                self._send(
                    200,
                    _report_form("income_tax_records", 43, start="Drukuj do PDF", link="Pobierz"),
                )
            elif path == "/jobs/42/result":
                self._send(
                    200,
                    _page(
                        "<h1>JPK_FA gotowy</h1>"
                        "<a class='btn' href='/reports/jpk_fa.xml'>Eksportuj do XML</a>"
                        "<a href='https://e-deklaracje.mf.gov.pl/'>Wyślij do MF</a>"
                    ),
                )
            elif path == "/reports/jpk_fa.xml":
                self._send(
                    200,
                    JPK_FA_XML,
                    {
                        "Content-Type": "application/xml",
                        "Content-Disposition": 'attachment; filename="jpk_fa.xml"',
                    },
                )
            elif path == "/jobs/43/result":
                if state.vat_result == "direct":
                    self._send_pdf()
                elif state.vat_result == "missing":
                    self._send(200, _page("<h1>Rejestr VAT</h1><p>Brak przycisku</p>"))
                else:
                    self._send(
                        200,
                        _page("<h1>Rejestr VAT</h1><a href='/reports/vat.pdf'>Pobierz PDF</a>"),
                    )
            elif path == "/reports/vat.pdf":
                self._send_pdf()
            elif path == "/send":
                self._send(200, SEND_PAGE)
            else:
                self._send(200, _page(f"<p>{path}</p>"))

        def _send_pdf(self):
            self._send(
                200,
                VAT_PDF,
                {
                    "Content-Type": "application/pdf",
                    "Content-Disposition": 'attachment; filename="rejestr.pdf"',
                },
            )

    return Handler


@pytest.fixture
def fake_site() -> Iterator[FakeFakturownia]:
    state = FakeFakturownia()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


_CHROMIUM: dict[str, str | None] = {}


def _chromium_problem() -> str | None:
    if "problem" not in _CHROMIUM:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                browser.close()
            _CHROMIUM["problem"] = None
        except Exception as exc:  # binary missing, sandbox, no playwright…
            _CHROMIUM["problem"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
    return _CHROMIUM["problem"]


@pytest.fixture
def chromium():
    problem = _chromium_problem()
    if problem:
        pytest.skip(f"Chromium unavailable in this environment: {problem}")


@pytest.fixture
def guarded_context(chromium, tmp_path):
    """A real persistent context with the no-send guard, as production builds it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(tmp_path / "ctx-profile"),
            headless=True,
            accept_downloads=True,
            service_workers="block",
        )
        guard = fr.install_no_send_guard(context)
        try:
            yield context, guard
        finally:
            context.close()


def _headless_env(monkeypatch, site: FakeFakturownia, tmp_path: Path) -> None:
    monkeypatch.setenv("FAKTUROWNIA_REPORT_HEADLESS", "1")
    monkeypatch.setenv("FAKTUROWNIA_WEB_BASE_URL", site.base_url)
    monkeypatch.setenv("FAKTUROWNIA_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("FAKTUROWNIA_LOGIN", "test@example.com")
    monkeypatch.setenv("FAKTUROWNIA_PASSWORD", "secret")


def _report(name: str) -> dict:
    return next(r for r in mc_config.FAKTUROWNIA_REPORTS if r["name"] == name)


# ─── Browser tests ───────────────────────────────────────────────────────────


class TestUnattendedFlow:
    def test_full_jpk_fa_and_vat_register_path_saves_files(
        self, chromium, fake_site, tmp_path, monkeypatch
    ):
        _headless_env(monkeypatch, fake_site, tmp_path)
        out = tmp_path / "deklaracje"

        result = fr.download_fakturownia_reports(
            mc_config.FAKTUROWNIA_REPORTS, "2026-10-01", "2026-10-31", out, timeout=15_000
        )

        assert [(rpt["name"], path.name) for rpt, path in result] == [
            ("JPK_FA", "JPK_FA.xml"),
            ("VAT Sales Register", "Wykaz_sprzedazy_VAT.pdf"),
        ]
        assert (out / "JPK_FA.xml").read_bytes() == JPK_FA_XML
        assert (out / "Wykaz_sprzedazy_VAT.pdf").read_bytes() == VAT_PDF
        assert not list(out.glob("*.part")) and not list(out.glob("*.error.png"))
        # Each report job was started exactly once, by "Generuj raport", for the period.
        jpk_jobs = fake_site.hits("/generate?kind=jpk_fa")
        assert len(jpk_jobs) == 1
        assert "date_from=2026-10-01" in jpk_jobs[0] and "date_to=2026-10-31" in jpk_jobs[0]
        assert len(fake_site.hits("/generate?kind=income_tax_records")) == 1
        # The config's zdrovena.fakturownia.pl URLs were mapped to the local host.
        assert fake_site.hits("/reports/jpk_fa?kind=jpk_fa")
        # The popup was declined, never accepted; nothing was sent.
        assert fake_site.hits("/newsletter/declined")
        assert not fake_site.hits("/newsletter/subscribe")
        assert not fake_site.hits("/sent")
        # The persistent profile lives per host.
        port = fake_site.base_url.rsplit(":", 1)[1]
        assert (tmp_path / "profiles" / f"127.0.0.1_{port}").is_dir()

    def test_vat_register_downloaded_by_the_job_link_itself(
        self, chromium, fake_site, tmp_path, monkeypatch
    ):
        fake_site.vat_result = "direct"
        _headless_env(monkeypatch, fake_site, tmp_path)
        out = tmp_path / "out"

        result = fr.download_fakturownia_reports(
            [_report("VAT Sales Register")], "2026-10-01", "2026-10-31", out, timeout=15_000
        )

        assert [path.name for _rpt, path in result] == ["Wykaz_sprzedazy_VAT.pdf"]
        assert (out / "Wykaz_sprzedazy_VAT.pdf").read_bytes() == VAT_PDF

    def test_missing_vat_control_names_it_and_leaves_a_screenshot(
        self, chromium, fake_site, tmp_path, monkeypatch, caplog
    ):
        fake_site.vat_result = "missing"
        _headless_env(monkeypatch, fake_site, tmp_path)
        out = tmp_path / "out"

        with caplog.at_level("WARNING"):
            result = fr.download_fakturownia_reports(
                [_report("VAT Sales Register")], "2026-10-01", "2026-10-31", out, timeout=2_000
            )

        assert result == []
        assert not (out / "Wykaz_sprzedazy_VAT.pdf").exists()
        assert (out / "Wykaz_sprzedazy_VAT.pdf.error.png").stat().st_size > 0
        assert fr.VAT_REGISTER_DOWNLOAD.name in caplog.text
        assert "control not found" in caplog.text

    def test_jpk_v7m_is_left_for_manual_download(self, fake_site, tmp_path, monkeypatch, caplog):
        _headless_env(monkeypatch, fake_site, tmp_path)
        with caplog.at_level("WARNING"):
            result = fr.download_fakturownia_reports(
                [_report("JPK_V7M")], "2026-10-01", "2026-10-31", tmp_path / "out"
            )
        assert result == []
        assert "JPK_V7M: no unattended flow" in caplog.text
        assert not fake_site.requests


class TestLogin:
    def test_login_form_then_profile_already_logged_in(self, chromium, fake_site, tmp_path):
        from playwright.sync_api import sync_playwright

        profile = tmp_path / "profile"
        for _run in range(2):
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(str(profile), headless=True)
                try:
                    fr.install_no_send_guard(context)
                    page = context.pages[0] if context.pages else context.new_page()
                    fr._login(page, "u", "p", base_url=fake_site.base_url)
                    assert page.url.endswith("/dashboard")
                finally:
                    context.close()

        # The second run reused the profile's session: the form was skipped.
        assert fake_site.login_posts == 1

    def test_verification_page_fails_with_a_readable_error(self, guarded_context, fake_site):
        fake_site.verification = True
        context, _guard = guarded_context
        page = context.new_page()

        with pytest.raises(RuntimeError, match="verification code") as excinfo:
            fr._login(page, "u", "p", base_url=fake_site.base_url)

        assert "FAKTUROWNIA_VERIFICATION_WAIT_MS" in str(excinfo.value)

    def test_verification_wait_lets_a_person_enter_the_code(self, guarded_context, fake_site):
        fake_site.verification = True
        fake_site.verification_auto_pass_ms = 300
        context, _guard = guarded_context
        page = context.new_page()

        fr._login(page, "u", "p", base_url=fake_site.base_url, verification_wait_ms=10_000)

        assert page.url.endswith("/dashboard")

    def test_verification_wait_times_out_readably(self, guarded_context, fake_site):
        fake_site.verification = True
        context, _guard = guarded_context
        page = context.new_page()

        with pytest.raises(RuntimeError, match="not entered within"):
            fr._login(page, "u", "p", base_url=fake_site.base_url, verification_wait_ms=500)


class TestNewsletterPopup:
    def test_popup_is_closed_with_nie_dziekuje(self, guarded_context, fake_site):
        context, _guard = guarded_context
        page = context.new_page()
        page.context.add_cookies([{"name": "sid", "value": "ok", "url": fake_site.base_url}])
        page.goto(f"{fake_site.base_url}/dashboard")

        assert fr._dismiss_newsletter_popup(page) is True

        assert page.locator("#nl").count() == 0
        assert page.url.endswith("/dashboard")
        assert fake_site.hits("/newsletter/declined")
        assert not fake_site.hits("/newsletter/subscribe")
        assert fr._dismiss_newsletter_popup(page) is False


class TestNoSendGuard:
    def _open_send_page(self, context, site):
        page = context.new_page()
        page.context.add_cookies([{"name": "sid", "value": "ok", "url": site.base_url}])
        page.goto(f"{site.base_url}/send")
        page.wait_for_selector("#late", state="attached")
        return page

    def test_send_controls_are_disabled_and_clicks_do_nothing(self, guarded_context, fake_site):
        context, _guard = guarded_context
        page = self._open_send_page(context, fake_site)

        for sel in ("#send", "#edekl", "#sub", "#late"):
            assert page.get_attribute(sel, "data-zdv-blocked") == "1", sel
            page.click(sel, force=True, timeout=2_000)
            page.dispatch_event(sel, "click")
        page.evaluate("document.getElementById('send').click()")
        page.evaluate("document.getElementById('late').click()")
        page.wait_for_timeout(300)

        assert page.evaluate("window.__sent") is None
        assert page.url.endswith("/send")
        assert page.get_attribute("#edekl", "href") is None
        assert page.is_disabled("#send") and page.is_disabled("#sub")
        assert not fake_site.hits("/sent")
        # A download control on the same page is untouched.
        assert page.get_attribute("#gen", "data-zdv-blocked") is None
        page.click("#gen")
        assert page.evaluate("window.__generated") is True

    def test_relabelled_control_is_blocked_later(self, guarded_context, fake_site):
        context, _guard = guarded_context
        page = self._open_send_page(context, fake_site)

        page.evaluate("document.getElementById('gen').textContent = 'Wyślij deklarację'")
        page.wait_for_timeout(100)

        assert page.get_attribute("#gen", "data-zdv-blocked") == "1"

    def test_request_to_e_deklaracje_is_aborted(self, guarded_context, fake_site):
        context, guard = guarded_context
        page = context.new_page()
        page.goto(f"{fake_site.base_url}/login")

        outcome = page.evaluate(
            "fetch('https://e-deklaracje.mf.gov.pl/').then(() => 'ok', e => 'err:' + e)"
        )
        assert outcome.startswith("err:")
        with pytest.raises(Exception, match=r"ERR_BLOCKED_BY_CLIENT|net::"):
            page.goto("https://e-deklaracje.mf.gov.pl/")

        assert "https://e-deklaracje.mf.gov.pl/" in guard.blocked
        assert all(fr.is_blocked_url(u) for u in guard.blocked)

    def test_flow_refuses_to_click_a_send_control(self, guarded_context, fake_site):
        context, _guard = guarded_context
        page = self._open_send_page(context, fake_site)

        with pytest.raises(fr.NoSendViolation):
            fr._click_allowlisted(page.locator("#send"), fr.GENERATE_REPORT)
