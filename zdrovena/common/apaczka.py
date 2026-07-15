"""zdrovena.common.apaczka — Apaczka API v2 client.

Creates shipment drafts. Auth uses per-request HMAC-SHA256 signatures.
Service structure is cached in blob storage (max once per 23 hours).
Secrets: apaczka-app-id, apaczka-app-secret (Key Vault). ``service_id`` is
per-draft data (from the Shopify shipping-line title, or set manually by an
operator), never a global secret — see APACZKA_SERVICE_CATALOG below and
docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

import requests

from zdrovena.common.shipping_exceptions import (
    ApaczkaAuthError,
    ApaczkaBusinessError,
    ApaczkaError,
    ApaczkaInsufficientBalanceError,
    ApaczkaSignatureError,
    ApaczkaTransientError,
)

# Re-exported for backward compat: callers import ApaczkaError from this module.
__all__ = [
    "ApaczkaAuthError",
    "ApaczkaBusinessError",
    "ApaczkaClient",
    "ApaczkaError",
    "ApaczkaInsufficientBalanceError",
    "ApaczkaSignatureError",
    "ApaczkaTransientError",
]

logger = logging.getLogger("zdrovena.common.apaczka")

_BASE = os.environ.get("APACZKA_BASE_URL", "https://www.apaczka.pl/api/v2").rstrip("/")
_TIMEOUT = 15
_SERVICE_CACHE_KEY = "apaczka/service_structure.json"
_SERVICE_CACHE_TTL_H = 23
# Apaczka signals in-body success with status == 200 (independent of the HTTP status).
_APACZKA_BODY_OK = 200

# Curated subset of Apaczka's ~70 service_ids (fetched live from the
# `service_structure` endpoint, verified 2026-07-09), covering non-InPost
# door-to-door and locker/pickup-point ("skrytki") delivery. InPost-supplier
# entries are deliberately excluded — those ship through the dedicated InPost
# integration, never through Apaczka. See
# docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md for the full
# rationale and how to extend this list.
APACZKA_SERVICE_CATALOG: dict[str, str] = {
    # Door-to-door
    "1": "UPS Standard",
    "2": "UPS Express Saver",
    "3": "UPS Express Plus do 12:00",
    "4": "UPS Express Plus do 9:00",
    "21": "DPD Kurier",
    "24": "DPD Kurier do 9:30",
    "25": "DPD Kurier do 12:00",
    "60": "Pocztex Kurier Drzwi-Drzwi",
    "82": "DHL Parcel Kurier",
    "83": "DHL Parcel Kurier do 12:00",
    "84": "DHL Parcel Kurier do 9:00",
    "151": "FEDEX Kurier",
    "202": "GLS Kurier Drzwi-Drzwi",
    # Point / locker ("skrytki")
    "14": "UPS AP Punkt-Punkt",
    "15": "UPS AP Drzwi-Punkt",
    "23": "DPD Pickup Drzwi-Punkt",
    "26": "DPD Pickup Punkt-Punkt",
    "50": "Orlen Paczka Punkt-Punkt",
    "53": "Orlen Paczka Drzwi-Punkt",
    "64": "Pocztex Kurier Drzwi-Punkt",
    "66": "Pocztex Punkt Punkt-Punkt",
    "86": "DHL POP do punktu",
    "203": "GLS Kurier Drzwi-Punkt",
    "314": "Packeta Punkt-Punkt",
    "317": "Packeta Magazyn-Punkt",
}


def _sign(app_id: str, secret: str, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
    # Apaczka signature format (verified against live API 2026-07):
    #   msg = "{app_id}:{endpoint}/:{request_json}:{expires}"
    # Notes:
    #   * The route MUST include a trailing slash (matches the URL path
    #     the server dispatches to). Signing bare "service_structure"
    #     instead of "service_structure/" returns "Signature doesn't match".
    #   * request_json uses compact separators and ensure_ascii=False so
    #     the byte sequence we sign equals the byte sequence sent in the
    #     form body (Apaczka's PHP server uses JSON_UNESCAPED_UNICODE).
    #   * hmac.new digests the raw UTF-8 bytes of the message.
    request_json = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    expires = str(int(time.time()) + 1800)
    route = endpoint if endpoint.endswith("/") else f"{endpoint}/"
    msg = f"{app_id}:{route}:{request_json}:{expires}"
    sig = hmac.new(secret.encode(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "app_id": app_id,
        "request": request_json,
        "expires": expires,
        "signature": sig,
    }


class ApaczkaClient:
    def __init__(self, app_id: str, app_secret: str, service_id: str, storage: Any) -> None:
        self._app_id = app_id
        self._secret = app_secret
        self._service_id = service_id
        self._storage = storage  # StorageService
        self._session = requests.Session()

    def _call(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        """Sign + POST to Apaczka, mapping failures onto the shared hierarchy.

        Transport: 401/403 -> auth, 5xx -> transient, other 4xx -> business.
        Network failures (timeout/connection) -> ApaczkaTransientError (retryable).
        In-body: status != 200 is a business error, except HMAC signature rejection
        (auth) and insufficient balance (auth) which are routed to their subclasses.
        """
        body = _sign(self._app_id, self._secret, endpoint, data)
        logger.debug(
            "Apaczka %s payload: %s", endpoint, json.dumps(data, ensure_ascii=False)[:1000]
        )
        try:
            resp = self._session.post(f"{_BASE}/{endpoint}/", data=body, timeout=_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise ApaczkaTransientError(
                f"Apaczka network error ({endpoint}): {exc}",
                courier="apaczka",
                action=endpoint,
            ) from exc

        if not resp.ok:
            status = resp.status_code
            detail = (resp.text or "")[:300]
            if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                raise ApaczkaAuthError(
                    f"Apaczka {endpoint} failed {status}: {detail}",
                    courier="apaczka",
                    action=endpoint,
                )
            if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                raise ApaczkaTransientError(
                    f"Apaczka {endpoint} server error {status}: {detail}",
                    courier="apaczka",
                    action=endpoint,
                )
            raise ApaczkaBusinessError(
                f"Apaczka {endpoint} failed {status}: {detail}",
                courier="apaczka",
                action=endpoint,
            )

        result = resp.json()
        if result.get("status") != _APACZKA_BODY_OK:
            message = str(result.get("message", "")).lower()
            if "signature" in message or "podpis" in message:
                raise ApaczkaSignatureError(detail=str(result))
            if "balance" in message or "saldo" in message or "insufficient" in message:
                raise ApaczkaInsufficientBalanceError
            raise ApaczkaBusinessError(
                f"Apaczka {endpoint} error: {result}",
                courier="apaczka",
                action=endpoint,
            )
        return result

    # ── Service structure cache ───────────────────────────────────────────────

    def _get_service_structure(self) -> list[dict[str, Any]]:
        try:
            cached_bytes = b"".join(self._storage.stream(_SERVICE_CACHE_KEY))  # type: ignore[arg-type]
            cached = json.loads(cached_bytes)
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            age_h = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
            if age_h < _SERVICE_CACHE_TTL_H:
                return cached["services"]  # type: ignore[return-value]
        except (OSError, ValueError, KeyError) as exc:
            logger.debug("Apaczka service_structure cache miss/unreadable: %s", exc)

        logger.info("Fetching Apaczka service_structure (cache miss)")
        result = self._call("service_structure", {})
        services = result.get("response", {}).get("services", [])
        cache_doc = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "services": services,
        }
        try:
            json_bytes = json.dumps(cache_doc).encode()
            self._storage.upload_stream(
                io.BytesIO(json_bytes), _SERVICE_CACHE_KEY, "application/json"
            )  # type: ignore[attr-defined]
        except (OSError, ValueError) as exc:
            logger.warning("Failed to cache Apaczka service_structure: %s", exc)
        return services  # type: ignore[return-value]

    # ── Shipment creation ─────────────────────────────────────────────────────

    def create_shipment(
        self,
        *,
        receiver_name: str,
        receiver_firstname: str,
        receiver_lastname: str,
        receiver_email: str,
        receiver_phone: str,
        receiver_address: str,
        receiver_city: str,
        receiver_zip: str,
        sender: dict[str, str],
        reference: str,
        weight_kg: float = 1.0,
        width_cm: float = 20.0,
        height_cm: float = 15.0,
        depth_cm: float = 30.0,
        pickup_date: str | None = None,
        pickup_from: str | None = None,
        pickup_to: str | None = None,
    ) -> dict[str, Any]:
        """pickup_date: YYYY-MM-DD, pickup_from/pickup_to: HH:MM.
        Available slots from Apaczka pickup_hours endpoint (today + 3 biz days).
        """
        options: dict[str, Any] = {"pickup_type": "courier"}
        if pickup_date:
            options["pickup"] = {
                "date": pickup_date,
                **({"hours_from": pickup_from} if pickup_from else {}),
                **({"hours_to": pickup_to} if pickup_to else {}),
            }
        data = {
            "service_id": self._service_id,
            "order_id": reference,
            "address": {
                "sender": {
                    "name": sender.get("name", ""),
                    "firstname": sender.get("firstname", ""),
                    "lastname": sender.get("lastname", ""),
                    "email": sender.get("email", ""),
                    "phone": sender.get("phone", ""),
                    "address": " ".join(
                        filter(None, [sender.get("street", ""), sender.get("building_number", "")])
                    ),
                    "city": sender.get("city", ""),
                    "zip": sender.get("post_code", ""),
                    "country_code": "PL",
                },
                "receiver": {
                    "name": receiver_name,
                    "firstname": receiver_firstname,
                    "lastname": receiver_lastname,
                    "email": receiver_email,
                    "phone": receiver_phone,
                    "address": receiver_address,
                    "city": receiver_city,
                    "zip": receiver_zip,
                    "country_code": "PL",
                },
            },
            "shipment": [
                {
                    "type": "package",
                    "weight": weight_kg,
                    "width": width_cm,
                    "height": height_cm,
                    "depth": depth_cm,
                }
            ],
            "options": options,
        }
        result = self._call("order_send", data)
        order_id = result.get("response", {}).get("id")
        logger.info("Apaczka shipment created: order_id=%s reference=%s", order_id, reference)
        return result.get("response", result)

    # ── Cancel ────────────────────────────────────────────────────────────────

    def cancel_shipment(self, order_id: str) -> dict[str, Any]:
        """Cancel an Apaczka shipment by order_id."""
        # Apaczka endpoint is /api/v2/cancel_order/, not /api/v2/order_cancel/.
        # Verified via panel.apaczka.pl API docs.
        result = self._call("cancel_order", {"order_id": order_id})
        logger.info("Apaczka shipment cancelled: order_id=%s", order_id)
        return result.get("response", result)

    # ── Label ─────────────────────────────────────────────────────────────────

    def get_label(self, order_id: str) -> bytes:
        result = self._call("waybill", {"order_id": order_id})
        encoded = result.get("response", {}).get("waybill") or result.get("response", "")
        if not encoded:
            raise ApaczkaBusinessError(
                f"No waybill in Apaczka response for order {order_id}",
                courier="apaczka",
                action="waybill",
            )
        return base64.b64decode(encoded)
