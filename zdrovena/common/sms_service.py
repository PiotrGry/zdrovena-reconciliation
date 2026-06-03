"""SMS notifications via SMSAPI.pl REST API."""

import httpx

SMSAPI_URL = "https://api.smsapi.pl/sms.do"


def _normalize_phone(phone: str) -> str:
    """Return phone in SMSAPI format: 48XXXXXXXXX (no +, no spaces)."""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("48") and len(digits) == 11:
        return digits
    if len(digits) == 9:
        return "48" + digits
    return digits


def _send(phone: str, message: str, token: str) -> None:
    normalized = _normalize_phone(phone)
    if not normalized:
        return
    httpx.post(
        SMSAPI_URL,
        headers={"Authorization": f"Bearer {token}"},
        data={"format": "json", "to": normalized, "message": message},
        timeout=10,
    ).raise_for_status()


def send_new_order_sms(
    notify_phone: str,
    order_number: str,
    customer_name: str,
    packages_count: int,
    courier: str,
    token: str,
) -> None:
    """Notify operator that a new order arrived and needs fulfillment."""
    courier_label = "InPost" if courier == "inpost" else "Apaczka"
    msg = f"Nowe zam. #{order_number} ({customer_name}) - {packages_count} paczek, {courier_label}. Do realizacji!"
    _send(notify_phone, msg, token)
