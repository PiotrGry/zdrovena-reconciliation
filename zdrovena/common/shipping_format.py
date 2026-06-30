"""zdrovena.common.shipping_format — Shipping address + phone formatting helpers."""

from __future__ import annotations

import re

_ADDR_RE = re.compile(r"^(.+?)\s+(\d+[A-Za-z]?(?:[/-]\d+)?)(?:\s+.*)?$", re.IGNORECASE)
_PHONE_DIGITS_RE = re.compile(r"\D")
_LOCKER_ID_RE = re.compile(r"^[A-Z0-9]{3,12}$")


def normalize_pl_phone(raw: str | None) -> str | None:
    """Normalize Polish phone to +48XXXXXXXXX format. Returns original if can't parse."""
    if not raw:
        return None
    digits = _PHONE_DIGITS_RE.sub("", raw)
    if digits.startswith("48") and len(digits) == 11:
        return f"+{digits}"
    if len(digits) == 9:
        return f"+48{digits}"
    return raw


def parse_pl_address(raw: str) -> tuple[str, str]:
    """Split 'ul. Testowa 12' → ('ul. Testowa', '12'). Falls back gracefully."""
    if not raw:
        return ("", "1")
    m = _ADDR_RE.match(raw.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return raw.strip(), "1"


def extract_locker_id_from_title(title: str) -> str:
    """Extract locker ID = last segment after • in shipping_lines title."""
    if not title:
        return ""
    parts = [p.strip() for p in title.split("•")]
    if len(parts) >= 2:
        candidate = parts[-1].strip()
        if _LOCKER_ID_RE.match(candidate):
            return candidate
    return ""
