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
# assets2.firmlet.com serves Fakturownia's own JavaScript; without it the report job modal never polls (E1, 2026-11-20).
# Left blocked: widgets.intum.net (widgets), browser.sentry-cdn.com, www.googletagmanager.com.
# assets.ksiegosoft.pl serves the accounting module (JPK_V7M wizard); without it the page stays empty (E3).
# Only that host: other ksiegosoft.pl hosts stay blocked until a walk shows the page needs them.
# piotrgryzlo.ksiegosoft.pl is the test account's accounting module; never the production one.
EXTRA_HOSTS: tuple[str, ...] = ("firmlet.com", "assets.ksiegosoft.pl", "piotrgryzlo.ksiegosoft.pl")
# Files served from shared hosts are allowed by exact URL prefix, never by host: the JPK_V7M XML is a
# presigned S3 link into Ksiegosoft's bucket (E3). Any other bucket on the same S3 host stays blocked.
EXTRA_URL_PREFIXES: tuple[str, ...] = ("https://s3.eu-west-1.amazonaws.com/files.ksiegosoft.pl/",)


def check_base_url(url: str) -> str:
    """Return the test base URL, or exit: the PoC never runs against another account."""
    parsed = urlparse(url.strip())
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != TEST_HOST
        or parsed.path not in ("", "/")
    ):
        raise SystemExit(
            f"Refusing to run: the PoC runs only against {TEST_BASE_URL}, not {url!r}."
        )
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

    def __init__(
        self, extra: tuple[str, ...] = EXTRA_HOSTS, prefixes: tuple[str, ...] = EXTRA_URL_PREFIXES
    ) -> None:
        self.extra = extra
        self.prefixes = prefixes
        self.aborted: list[dict] = []

    def handle_route(self, route) -> None:
        request = route.request
        if host_allowed(urlparse(request.url).hostname or "", extra=self.extra) or any(
            request.url.startswith(prefix) for prefix in self.prefixes
        ):
            route.fallback()
            return
        self.aborted.append(
            {"url": request.url, "method": request.method, "type": request.resource_type}
        )
        logger.info("PoC lock: aborted %s", request.url)
        route.abort("blockedbyclient")
