#!/usr/bin/env python3
"""Deterministic PII sanitizer for production fixtures.

Same source value → same fake value across all files, so cross-file joins
(same phone appearing in Fakturownia + InPost) remain consistent.

Fake ranges chosen to be structurally valid (right length/format) but obviously
non-real. NIP/IBAN checksums are NOT preserved — tests must not depend on them.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIRS = [
    ROOT / "tests/fixtures/allegro",
    ROOT / "tests/fixtures/apaczka",
    ROOT / "tests/fixtures/fakturownia",
    ROOT / "tests/fixtures/inpost",
]
SHOPIFY_FILES = [
    # Only fixtures that started from real production data need sanitizing.
    # shopify_order_inpost_paczkomat / _inpost_kurier / _apaczka / _real were
    # authored with synthetic data (example.com emails, plausible fake names)
    # and MUST NOT be re-mapped or their names will drift from what tests expect.
    ROOT / "tests/fixtures/shopify_order_dpd_pickup.json",
]

# Deterministic seed table — collected during scan. Each real value is mapped
# to a stable synthetic value that shares its structure.
_MAP: dict[str, str] = {}


def _seed(value: str, fake: str) -> str:
    _MAP[value] = fake
    return fake


def fake_digits(seed: str, length: int) -> str:
    """Deterministic digit string of given length seeded from `seed`."""
    h = hashlib.sha256(seed.encode()).hexdigest()
    # Turn hex into decimal digits by taking int and modding
    n = int(h, 16)
    out = str(n)[:length].ljust(length, "0")
    return out[:length]


def fake_email(seed: str) -> str:
    tag = hashlib.sha256(seed.encode()).hexdigest()[:10]
    # Preserve allegromail suffix pattern so tests keying on it still work
    if "@allegromail.pl" in seed:
        return f"buyer{tag}@allegromail.pl"
    # Preserve wodahumio.pl (Zdrovena's own domain — not PII, keep)
    if "@wodahumio.pl" in seed or "@zdrovena" in seed:
        return seed
    return f"user{tag}@example.com"


def fake_phone(seed: str) -> str:
    d = fake_digits(seed, 9)
    # Force leading 5/6/7 so it looks like a mobile
    return "5" + d[1:]


def fake_nip(seed: str) -> str:
    return fake_digits("nip:" + seed, 10)


def fake_iban_pl(seed: str) -> str:
    return "PL" + fake_digits("iban:" + seed, 26)


# Real values we found during scan — anchor them explicitly so we get memorable
# fake replacements (some tests may hardcode expected strings).
_EXPLICIT: dict[str, str] = {
    # Fakturownia token in view_url — critical, MUST be rotated
    "TESTTOKENFakeXXXXXX0": "TESTTOKENFakeXXXXXX0",
    # Live Apaczka credentials (not in git, but scrub anyway if present):
    "TESTAPP_ID_FAKE_1234567890abc": "TESTAPP_ID_FAKE_1234567890abc",
    "TESTSECRET_FAKE_1234567890abcdef": "TESTSECRET_FAKE_1234567890abcdef",
    # Zdrovena's own bank account (company IBAN) — sanitize even though it's the seller
    "PL00000000000000000000000000": "PL00000000000000000000000000",
    # Full person names encountered during scan — map to test names
    # ("Anna Kowalska" is intentionally NOT mapped: it appears in synthetic
    #  fixtures shopify_order_inpost_paczkomat.json which tests reference by
    #  that exact string.)
    "Jan Kowalski": "Jan Kowalski",
    "Magdalena Nowak": "Magdalena Nowak",
    "Magdalena Nowak": "Magdalena Nowak",
    "Magdalena Nowak": "Magdalena Nowak",
    "Jaros\u0142aw Bielecki": "Adam Nowak",
    "MARIA GRY\u017b\u0141O": "JAN KOWALSKI",
    "Maria Gry\u017cl\u00f3": "Jan Kowalski",
    "Piotr Gry\u017alo": "Jan Kowalski",
    "Piotr Gryzlo": "Jan Kowalski",
    "Piotr Gry\u017cl\u00f3": "Jan Kowalski",
}

# Extra PII field names for less-common structures
_EXTRA_PERSON_FIELDS = {
    "buyer_person", "seller_person", "contact_person",
}

# Domains / values that should be preserved intact
_KEEP = {
    "info@wodahumio.pl",
    "biuro@wodahumio.pl",
    "biuro@zdrovena.pl",
    "info@zdrovena.pl",
    "kontakt@zdrovena.pl",
}


def _sanitize_str(s: str) -> str:
    if s in _KEEP:
        return s
    if s in _EXPLICIT:
        return _EXPLICIT[s]
    if s in _MAP:
        return _MAP[s]

    # Emails
    if re.fullmatch(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", s):
        return _seed(s, fake_email(s))
    # Full PL phone with +48
    if re.fullmatch(r"\+48\d{9}", s):
        return _seed(s, "+48" + fake_phone(s)[1:])
    # 48 + 9 digits (no +)
    if re.fullmatch(r"48\d{9}", s):
        return _seed(s, "48" + fake_phone(s)[1:])
    # Bare 9-digit phone
    if re.fullmatch(r"\d{9}", s):
        return _seed(s, fake_phone(s))
    # NIP (10 digits)
    if re.fullmatch(r"\d{10}", s):
        return _seed(s, fake_nip(s))
    # IBAN PL (26 digits with optional PL prefix)
    if re.fullmatch(r"(?:PL)?\d{26}", s):
        return _seed(s, fake_iban_pl(s))
    # Formatted IBAN with spaces
    if re.fullmatch(r"(?:PL\s*)?(?:\d{2}\s*){13}", s):
        digits = re.sub(r"\D", "", s)
        return _seed(s, fake_iban_pl(digits))

    # Substring replacements — walk explicit map for embedded values (e.g. inside URLs)
    out = s
    for real, fake in _EXPLICIT.items():
        if real in out:
            out = out.replace(real, fake)
    # Embedded emails/phones in longer strings (rare, but safe)
    def _sub_email(m: re.Match[str]) -> str:
        v = m.group(0)
        if v in _KEEP:
            return v
        return _MAP.get(v) or _seed(v, fake_email(v))

    out = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", _sub_email, out)
    return out


# Fields to blank/replace by name (avoid partial address leakage)
_PII_FIELD_NAMES = {
    "street",
    "street1",
    "street2",
    "address1",
    "address2",
    "city",
    "zip_code",
    "postCode",
    "zip",
    "postal_code",
    "zipcode",
    "buyer_street",
    "buyer_city",
    "buyer_post_code",
    "buyer_zip_code",
    "recipient_street",
    "recipient_city",
    "recipient_post_code",
    "first_name",
    "firstName",
    "last_name",
    "lastName",
    "buyer_first_name",
    "buyer_last_name",
    "recipient_first_name",
    "recipient_last_name",
    "buyer_name",
    "recipient_name",
    "recipient_company_name",
    "buyer_company_name",
    "company_name",
    "name",  # careful — used at top level in Allegro checkout form
}

# Parent-key contexts where 'name' means a product/service/etc — NOT a person.
_NAME_SAFE_PARENTS = {
    "product", "products", "service", "services", "shipping", "delivery",
    "package", "packages", "point", "points", "positions", "position",
    "items", "item", "lineItems", "line_items", "offer", "offers",
    "method", "methods", "deliveryMethod", "paymentMethod", "category",
    "categories", "parameters", "parameter", "discount", "tax", "vat",
    "currency", "status", "buyer_bank_account", "seller_bank_account",
    "warehouse", "pickup_point", "pickupPoint",
    # Additional non-person contexts discovered during audit:
    "additionalServices", "additional_services", "carrier", "carriers",
    "attributes", "attribute", "note_attributes", "invoice", "invoices",
    "revenues", "cost", "costs", "positions", "payment", "payments",
    "tax_kind", "kind", "structure", "type", "types",
    "buyer_delivery_point", "buyer_pickup_point", "customer_pickup_point",
    "seller_bank", "buyer_bank",
}


def _pick_replacement(field: str, seed_val: str) -> str | None:
    """Deterministic fake for a named PII field.

    Returns None if the field cannot be safely mapped — caller must keep the
    original value in that case. We never invent generic `<field>_test_N`
    strings anymore because they would leak into codebase and break tests.
    """
    if field in ("first_name", "firstName", "buyer_first_name", "recipient_first_name"):
        pool = ["Jan", "Jan", "Jan", "Jan", "Jan", "Jan", "Magdalena", "Jan"]
    elif field in ("last_name", "lastName", "buyer_last_name", "recipient_last_name"):
        pool = ["Kowalski", "Nowak", "Wiśniewski", "Wójcik", "Kowalczyk", "Kamiński"]
    elif field in ("city", "buyer_city", "recipient_city"):
        pool = ["Wrocław", "Wrocław", "Gdańsk", "Wrocław", "Wrocław", "Wrocław"]
    elif field in ("street", "street1", "address1", "buyer_street", "recipient_street"):
        pool = ["ul. Testowa 1", "ul. Przykładowa 2", "ul. Fikcyjna 3", "ul. Mock 4"]
    elif field in ("street2", "address2"):
        return ""
    elif field in ("zip_code", "postCode", "zip", "postal_code", "zipcode",
                   "buyer_post_code", "buyer_zip_code", "recipient_post_code"):
        pool = ["00-001", "30-001", "80-001", "60-001", "50-001", "90-001"]
    elif field in ("company_name", "buyer_company_name", "recipient_company_name"):
        pool = ["Test Sp. z o.o.", "Fikcyjne Firma SA", "Mock Corp.", "Sample Ltd."]
    elif field in ("buyer_name", "recipient_name", "name"):
        # Full person name field (used in receiver/sender contexts).
        pool = ["Jan Kowalski", "Jan Nowak", "Magdalena Nowak", "Adam Wiśniewski"]
    else:
        # Do NOT fabricate generic values — keep original to avoid cross-file corruption.
        return None
    idx = int(hashlib.sha256(seed_val.encode()).hexdigest(), 16) % len(pool)
    return pool[idx]


# Shopify note_attributes: list of {name, value}. If `name` matches these,
# the sibling `value` must be sanitized as the given PII field.
_NOTE_ATTR_MAP = {
    "customer_first_name": "first_name",
    "customer_last_name": "last_name",
    "customer_email": "__email__",
    "customer_phone": "__phone__",
    "customer_street": "street",
    "customer_city": "city",
    "customer_zip_code": "zip_code",
    "customer_zip": "zip_code",
    "customer_post_code": "postCode",
    "PickupPointName": "__keep__",  # store name, not personal PII - keep
    "PickupPointAddress": "__keep__",
    "PickupPointPostCode": "__keep__",
    "PickupPointCity": "__keep__",
    "PickupPointCourier": "__keep__",
    "PickupPointId": "__keep__",
}


def _process_note_attributes(items: list) -> list:
    """Process Shopify note_attributes list — sanitize `value` based on `name`."""
    out = []
    for item in items:
        if not isinstance(item, dict) or "name" not in item or "value" not in item:
            out.append(_walk(item))
            continue
        n = item["name"]
        v = item["value"]
        if not isinstance(v, str):
            out.append(item)
            continue
        target = _NOTE_ATTR_MAP.get(n)
        if target is None or target == "__keep__":
            out.append(item)
            continue
        if target == "__email__":
            new_v = _sanitize_str(v)  # goes through email regex
        elif target == "__phone__":
            # Force phone sanitization even if it doesn't match strict format
            digits = re.sub(r"\D", "", v)
            if len(digits) >= 9:
                new_v = _sanitize_str(digits[-9:]) if re.fullmatch(r"\d{9}", digits[-9:]) else v
            else:
                new_v = v
        else:
            new_v = _pick_replacement(target, v) or v
        out.append({**item, "value": new_v})
    return out


def _walk(obj, path: tuple[str, ...] = ()):
    if isinstance(obj, dict):
        # Detect if we're inside a name-safe context
        parent = path[-1] if path else ""
        return {k: (_process_note_attributes(v) if k == "note_attributes" and isinstance(v, list) else _walk_value(k, v, path + (k,), parent)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(x, path) for x in obj]
    return obj


def _looks_like_person_name(v: str) -> bool:
    """Heuristic: does this string look like a Polish person name?"""
    if not v or len(v) < 3 or len(v) > 60:
        return False
    # Accept: TitleCase, ALLCAPS, or mixed. Must be 2+ tokens separated by spaces or hyphen.
    letters_pl = r"[A-Za-z\u00c0-\u017f]"
    if not re.fullmatch(rf"{letters_pl}+(?:[ -]{letters_pl}+)+", v):
        return False
    lowered = v.lower()
    # Exclude common non-person strings
    bad = ("sp. z o.o", " sa", " ltd", " corp", "kurier", "paczkomat", "standard",
           "woda humio", "zdrovena", "inpost", "allegro", "fikcyjne", "test sp",
           "mock", "sample", "paczkomaty")
    if any(b in lowered for b in bad):
        return False
    # Reject strings with digits
    if any(ch.isdigit() for ch in v):
        return False
    return True


# Parent-key contexts that mark a person (name here = person's full name)
_PERSON_PARENTS = {
    "receiver", "sender", "buyer", "customer", "contact",
    "shipTo", "billTo", "shipping_address", "billing_address",
    "delivery_address", "person", "passenger",
}


def _walk_value(key: str, value, path: tuple[str, ...], parent: str):
    # buyer_person / contact_person: full name string
    if isinstance(value, str) and key in _EXTRA_PERSON_FIELDS:
        if value.strip() and _looks_like_person_name(value):
            return _EXPLICIT.get(value, "Jan Kowalski")
        return value
    # If key is a known PII field name — replace by generated fake matching field
    if isinstance(value, str) and key in _PII_FIELD_NAMES:
        # For 'name' specifically: person iff DIRECT parent is a person container.
        if key == "name":
            direct_parent = path[-2] if len(path) >= 2 else ""
            # If direct parent is a person container — treat as person name.
            if direct_parent in _PERSON_PARENTS:
                if not _looks_like_person_name(value) and value not in _EXPLICIT:
                    return value
                return _EXPLICIT.get(value) or _pick_replacement("name", value) or value
            # Otherwise: fall through to old safe-parent check (product/service/etc)
            if any(seg in _NAME_SAFE_PARENTS for seg in path):
                return value
            # Only touch values that actually look like person names.
            if not _looks_like_person_name(value) and value not in _EXPLICIT:
                return value
            if value in _EXPLICIT:
                return _EXPLICIT[value]
        replacement = _pick_replacement(key, value)
        if replacement is None:
            # No safe mapping — keep original (avoid cross-file corruption)
            return value
        return replacement
    if isinstance(value, str):
        return _sanitize_str(value)
    return _walk(value, path)


def process_file(p: Path):
    orig = p.read_text(encoding="utf-8")
    try:
        data = json.loads(orig)
    except json.JSONDecodeError:
        print(f"  SKIP (not JSON): {p}")
        return
    cleaned = _walk(data)
    out = json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n"
    p.write_text(out, encoding="utf-8")


def main():
    files = []
    for d in FIXTURE_DIRS:
        if d.exists():
            files.extend(sorted(d.glob("*.json")))
    files.extend([f for f in SHOPIFY_FILES if f.exists()])
    for f in files:
        print(f"sanitizing {f.relative_to(ROOT)}")
        process_file(f)
    print(f"\n{len(_MAP)} unique PII values remapped")
    print(f"{len(_EXPLICIT)} explicit rotations applied")


if __name__ == "__main__":
    main()
