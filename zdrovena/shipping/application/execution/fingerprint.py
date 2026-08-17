"""Pure execution-preview fingerprint calculation and verification."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def preview_fingerprint(draft: dict[str, Any], preview: dict[str, Any]) -> str:
    """Return the stable SHA-256 digest for a draft and its rendered preview."""
    fingerprint_input = json.dumps(
        {"draft": draft, "preview": preview},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(fingerprint_input).hexdigest()


def fingerprints_match(current: str, reviewed: str) -> bool:
    """Compare preview fingerprints using the existing constant-time primitive."""
    return hmac.compare_digest(current, reviewed)
