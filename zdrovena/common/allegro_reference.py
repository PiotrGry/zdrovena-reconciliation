"""What Zdrovena may send to Allegro as ``referenceNumber`` and ``textOnLabel``.

Why this exists (issue #462)
----------------------------
The portal-wide packing annotation (``shipment_reference`` →
``"<order> | szkło | 1-pak 2/3"``) was sent unchanged as the Allegro
``input.referenceNumber``. Allegro rejected it with ``VALIDATION_ERROR``
twice in production (2026-09-15, OperationId cc8fd8131385faf2aead5ec9437977f2;
2026-10-26, OperationId 597656160853e0e03e4951ffebc4a4aa, rejected value
``"<checkout UUID> | plastik | 3-pak"``). The validation message allows only
letters without diacritics, digits and ``_/-``. Spaces, ``|`` and ``ł`` do
not pass.

Evidence on lengths
-------------------
Allegro does not publish one limit. The "Wysyłam z Allegro" tutorial says each
carrier sets its own character limits, and Allegro answers with a validation
error when a carrier's limit is exceeded. Community reports mention 35
characters (Allegro One ``label.referenceNumber``, which rejected a 36-character
UUID) and 28 characters (DPD). Neither is an official, universal limit, and
**20 is not a universal Allegro limit either**.

Application policy (conservative, ours, not Allegro's)
------------------------------------------------------
``referenceNumber``
  * Only ``[A-Za-z0-9_-]``. ``/`` is allowed by the message we saw but left
    out: forum reports show slashes rejected by some carriers.
  * Diacritics fold to ASCII (``ł`` → ``l``). Every other disallowed
    character is dropped.
  * A UUID-shaped order id (Allegro checkout id, 36 characters) loses its
    hyphens. That keeps all 32 hex digits, so the id stays recoverable
    without hashing or abbreviating, and it fits under the 35-character
    report.
  * At most :data:`ALLEGRO_REFERENCE_MAX_LENGTH` (32) characters. A longer
    value fails closed **before** any HTTP call. It is never truncated,
    because a truncated id could match another order. A carrier with a
    stricter limit (e.g. the reported 28 for DPD) still answers
    ``VALIDATION_ERROR``. That is an operator-visible failure, not a silent
    loss, and changing it needs an owner decision on an abbreviation scheme.
  * It is a technical identifier only. The packing information no longer
    lives here.

``packages[].textOnLabel``
  * Carries the packing information: material, pack size and, when the order
    has more than one box of the same type, ``n/count``. Example:
    ``"szklo 1-pak 2/3"``.
  * Folded to printable ASCII so no carrier label font can mangle it.
  * At most :data:`ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH` (30) characters. Today's
    longest catalogue value is ``"plastik pol-pak 99/99"`` (21). A longer
    value fails closed rather than dropping packing information.

The full business annotation with Polish characters stays in the portal. The
preview returns it as ``annotation``, and InPost and Apaczka keep sending it
unchanged.
"""

from __future__ import annotations

import re
import unicodedata

from zdrovena.common.shipping_exceptions import AllegroBusinessError

ALLEGRO_REFERENCE_MAX_LENGTH = 32
ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH = 30

ALLEGRO_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
# Printable ASCII. Rules out the control and non-ASCII characters some
# carrier label printers render as "?".
ALLEGRO_TEXT_ON_LABEL_PATTERN = re.compile(r"^[\x20-\x7E]+$")

_UUID_SHAPE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_REFERENCE_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")
# NFKD does not decompose these Polish letters, so map them explicitly.
_NON_DECOMPOSING = str.maketrans({"ł": "l", "Ł": "L"})


def fold_to_ascii(text: str) -> str:
    """Return ``text`` with diacritics removed (``"szkło pół"`` → ``"szklo pol"``)."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_NON_DECOMPOSING))
    return decomposed.encode("ascii", "ignore").decode("ascii")


def allegro_reference_number(order_number: str) -> str:
    """Return the deterministic Allegro ``referenceNumber`` for an order.

    Raises :class:`AllegroBusinessError` when nothing usable remains or the
    result exceeds the application cap. Never truncates.
    """
    raw = str(order_number or "").strip()
    if _UUID_SHAPE.fullmatch(raw):
        raw = raw.replace("-", "")
    reference = _REFERENCE_DISALLOWED.sub("", fold_to_ascii(raw))
    validate_allegro_reference_number(reference, source=str(order_number or ""))
    return reference


def validate_allegro_reference_number(reference: str, *, source: str | None = None) -> None:
    """Fail closed when ``reference`` breaks the application policy."""
    origin = f" (from {source!r})" if source is not None and source != reference else ""
    if not reference or not ALLEGRO_REFERENCE_PATTERN.fullmatch(reference):
        raise AllegroBusinessError(
            detail=(
                f"Allegro referenceNumber {reference!r}{origin} must be non-empty and "
                "contain only A-Z, a-z, 0-9, '_' or '-'"
            ),
            action="create_ship_with_allegro_shipment",
        )
    if len(reference) > ALLEGRO_REFERENCE_MAX_LENGTH:
        raise AllegroBusinessError(
            detail=(
                f"Allegro referenceNumber {reference!r}{origin} has {len(reference)} "
                f"characters; the application limit is {ALLEGRO_REFERENCE_MAX_LENGTH} "
                "and it is never truncated"
            ),
            action="create_ship_with_allegro_shipment",
        )


def allegro_text_on_label(material: str, size: str, position: int, count_for_type: int) -> str:
    """Return the label text for one physical parcel (``"szklo 1-pak 2/3"``)."""
    text = f"{material} {size}"
    if count_for_type > 1:
        text = f"{text} {position}/{count_for_type}"
    folded = " ".join(fold_to_ascii(text).split())
    validate_allegro_text_on_label(folded, source=text)
    return folded


def validate_allegro_text_on_label(text: str, *, source: str | None = None) -> None:
    """Fail closed when ``text`` breaks the application policy."""
    origin = f" (from {source!r})" if source is not None and source != text else ""
    if not text or not ALLEGRO_TEXT_ON_LABEL_PATTERN.fullmatch(text):
        raise AllegroBusinessError(
            detail=f"Allegro textOnLabel {text!r}{origin} must be non-empty printable ASCII",
            action="create_ship_with_allegro_shipment",
        )
    if len(text) > ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH:
        raise AllegroBusinessError(
            detail=(
                f"Allegro textOnLabel {text!r}{origin} has {len(text)} characters; the "
                f"application limit is {ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH} and packing "
                "information is never truncated"
            ),
            action="create_ship_with_allegro_shipment",
        )


__all__ = [
    "ALLEGRO_REFERENCE_MAX_LENGTH",
    "ALLEGRO_REFERENCE_PATTERN",
    "ALLEGRO_TEXT_ON_LABEL_MAX_LENGTH",
    "ALLEGRO_TEXT_ON_LABEL_PATTERN",
    "allegro_reference_number",
    "allegro_text_on_label",
    "fold_to_ascii",
    "validate_allegro_reference_number",
    "validate_allegro_text_on_label",
]
