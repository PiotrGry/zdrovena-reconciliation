"""SMS notifications via SMSAPI.pl REST API."""

import httpx

SMSAPI_URL = "https://api.smsapi.pl/sms.do"

_INPOST_TRACKING = "https://inpost.pl/sledzenie-przesylek?number={}"


def _normalize_phone(phone: str) -> str:
    """Return phone in SMSAPI format: 48XXXXXXXXX (no +, no spaces)."""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("48") and len(digits) == 11:
        return digits
    if len(digits) == 9:
        return "48" + digits
    return digits


def send_shipment_sms(
    phone: str,
    order_number: str,
    tracking: str,
    courier: str,
    token: str,
) -> None:
    """Send dispatch notification. Raises httpx.HTTPStatusError on API error."""
    normalized = _normalize_phone(phone)
    if not normalized:
        return
    if courier == "inpost":
        msg = f"Zamowienie #{order_number} wyslane! Sledz: {_INPOST_TRACKING.format(tracking)}"
    else:
        msg = f"Zamowienie #{order_number} wyslane! Nr przesylki: {tracking}"
    httpx.post(
        SMSAPI_URL,
        headers={"Authorization": f"Bearer {token}"},
        data={"format": "json", "to": normalized, "message": msg},
        timeout=10,
    ).raise_for_status()
