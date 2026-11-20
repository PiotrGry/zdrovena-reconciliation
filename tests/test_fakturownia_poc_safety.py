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
    assert (
        host_allowed("e-deklaracje.mf.gov.pl", extra=("e-deklaracje.mf.gov.pl", "gov.pl")) is False
    )


def test_extra_static_hosts_match_exactly_or_as_subdomain():
    assert host_allowed("fonts.gstatic.com", extra=("gstatic.com",)) is True
    assert host_allowed("evilgstatic.com", extra=("gstatic.com",)) is False


def test_fakturownia_assets_are_allowed_and_trackers_are_not():
    assert host_allowed("assets2.firmlet.com") is True
    assert host_allowed("assets.ksiegosoft.pl") is True
    assert host_allowed("api.ksiegosoft.pl") is False
    assert host_allowed("piotrgryzlo.ksiegosoft.pl") is True
    assert host_allowed("zdrovena.ksiegosoft.pl") is False
    for host in ("widgets.intum.net", "browser.sentry-cdn.com", "www.googletagmanager.com"):
        assert host_allowed(host) is False


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


def test_files_on_shared_hosts_are_allowed_by_exact_prefix_only():
    lock = HostLock()
    ours = _Route("https://s3.eu-west-1.amazonaws.com/files.ksiegosoft.pl/abc?X-Amz-Signature=1")
    other_bucket = _Route("https://s3.eu-west-1.amazonaws.com/someone-else/abc")
    look_alike = _Route("https://s3.eu-west-1.amazonaws.com/files.ksiegosoft.pl.evil/abc")
    for route in (ours, other_bucket, look_alike):
        lock.handle_route(route)
    assert ours.result == ("fallback",)
    assert other_bucket.result[0] == "abort"
    assert look_alike.result[0] == "abort"
