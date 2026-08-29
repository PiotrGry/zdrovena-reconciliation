"""Which blob keys belong to the operator and which belong to the workflows.

The same container holds documents an operator manages through the Files UI and
artefacts the internal workflows own. A generic PUT/DELETE over the whole
container means one mistyped key silently destroys internal state, and no role
check helps: the operator is *supposed* to hold the accountant role (issue #311).

The rules are deliberately narrow. Blocking all of ``faktury/`` would be simpler
and wrong -- uploading missing invoices under that prefix is exactly what the
Files UI is for. Only two things are actually workflow-owned:

* ``apaczka/`` -- provider caches written by the Apaczka client,
* any dot-prefixed path segment -- ``.state.json``, ``.file_hashes.json``, the
  month-close internal state that ``zip_service`` already refuses to archive.

The package ZIP stays operator-visible and operator-writable on purpose; it is
protected by hash verification before send (see ``package_integrity``), which is
stronger than a namespace rule because it also covers replacement by any other
route.
"""

from __future__ import annotations

import urllib.parse

#: Top-level path segments owned entirely by internal workflows.
SYSTEM_PREFIXES: tuple[str, ...] = ("apaczka",)

#: How many times to percent-decode before giving up. A key needing more than
#: this is not a real key; it is someone probing for a decoding gap.
_MAX_DECODE_PASSES = 5


class SystemKeyError(ValueError):
    """The key is malformed, or names storage the Files API must not write."""


def normalise_storage_key(key: str) -> str:
    """Reduce a key to the single form every check is made against.

    Percent-decoding repeats until stable: a single pass leaves ``%252F`` as
    ``%2F``, which compares unequal to ``/`` and would walk straight past a
    prefix check. Backslashes become slashes and repeated slashes collapse.

    Case is PRESERVED. Blob names are case-sensitive, so this is the key the
    caller then reads and writes -- lower-casing it here would silently miss
    ``faktury/2026/Kwiecień/…``. Case-insensitive matching happens in
    ``_comparison_key``, which is only ever used to decide ownership.

    Raises SystemKeyError for anything empty, absolute after normalisation, or
    containing a relative segment.
    """
    decoded = (key or "").strip()
    for _ in range(_MAX_DECODE_PASSES):
        once = urllib.parse.unquote(decoded)
        if once == decoded:
            break
        decoded = once
    else:
        raise SystemKeyError("Key requires implausibly many decoding passes")

    decoded = decoded.replace("\\", "/").strip()
    segments = [segment for segment in decoded.split("/") if segment != ""]
    if not segments:
        raise SystemKeyError("Empty key")
    if any(segment in (".", "..") for segment in segments):
        raise SystemKeyError("Relative path segments are not allowed")
    if "\x00" in decoded:
        raise SystemKeyError("Key contains a null byte")

    return "/".join(segments)


def _comparison_key(normalised: str) -> str:
    """Ownership is decided case-insensitively; storage access is not."""
    return normalised.lower()


def is_system_key(key: str) -> bool:
    """True when the key names storage owned by an internal workflow."""
    try:
        normalised = _comparison_key(normalise_storage_key(key))
    except SystemKeyError:
        # A malformed key is not "system", but it is not writable either;
        # require_user_key is what rejects it.
        return False

    segments = normalised.split("/")
    if segments[0] in SYSTEM_PREFIXES:
        return True
    return any(segment.startswith(".") for segment in segments)


def require_user_key(key: str) -> str:
    """Return the normalised key, or raise if it is malformed or system-owned."""
    normalised = normalise_storage_key(key)
    if is_system_key(normalised):
        raise SystemKeyError(f"Key belongs to internal storage: {normalised}")
    return normalised
