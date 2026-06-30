"""zdrovena.common.apaczka — Apaczka API v2 client.

Creates shipment drafts. Auth uses per-request HMAC-SHA256 signatures.
Service structure is cached in blob storage (max once per 23 hours).
Secrets: apaczka-app-id, apaczka-app-secret, apaczka-service-id (Key Vault).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger("zdrovena.common.apaczka")

_BASE = "https://www.apaczka.pl/api/v2"
_TIMEOUT = 15
_SERVICE_CACHE_KEY = "apaczka/service_structure.json"
_SERVICE_CACHE_TTL_H = 23


class ApaczkaError(Exception):
    pass


def _sign(app_id: str, secret: str, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
    request_json = json.dumps(data, separators=(",", ":"))
    expires = str(int(time.time()) + 1800)
    msg = f"{app_id}:{endpoint}:{request_json}:{expires}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
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
        body = _sign(self._app_id, self._secret, endpoint, data)
        resp = self._session.post(f"{_BASE}/{endpoint}/", data=body, timeout=_TIMEOUT)
        if not resp.ok:
            raise ApaczkaError(f"Apaczka {endpoint} failed {resp.status_code}: {resp.text[:300]}")
        result = resp.json()
        if result.get("status") != 200:
            raise ApaczkaError(f"Apaczka {endpoint} error: {result}")
        return result

    # ── Service structure cache ───────────────────────────────────────────────

    def _get_service_structure(self) -> list[dict[str, Any]]:
        try:
            import io

            cached_bytes = b"".join(self._storage.stream(_SERVICE_CACHE_KEY))  # type: ignore[arg-type]
            cached = json.loads(cached_bytes)
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            age_h = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
            if age_h < _SERVICE_CACHE_TTL_H:
                return cached["services"]  # type: ignore[return-value]
        except Exception:
            pass

        logger.info("Fetching Apaczka service_structure (cache miss)")
        result = self._call("service_structure", {})
        services = result.get("response", {}).get("services", [])
        cache_doc = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "services": services,
        }
        try:
            import io

            json_bytes = json.dumps(cache_doc).encode()
            self._storage.upload_stream(io.BytesIO(json_bytes), _SERVICE_CACHE_KEY, "application/json")  # type: ignore[attr-defined]
        except Exception as exc:
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
                    "address": sender.get("street", ""),
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
        result = self._call("order_cancel", {"order_id": order_id})
        logger.info("Apaczka shipment cancelled: order_id=%s", order_id)
        return result.get("response", result)

    # ── Label ─────────────────────────────────────────────────────────────────

    def get_label(self, order_id: str) -> bytes:
        import base64

        result = self._call("waybill", {"order_id": order_id})
        encoded = result.get("response", {}).get("waybill") or result.get("response", "")
        if not encoded:
            raise ApaczkaError(f"No waybill in Apaczka response for order {order_id}")
        return base64.b64decode(encoded)
