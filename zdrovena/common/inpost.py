"""zdrovena.common.inpost — InPost ShipX API client.

Creates shipment drafts for paczkomat and kurier services.
Secrets: inpost-api-token, inpost-organization-id (Key Vault).
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger("zdrovena.common.inpost")

_BASE = "https://api-shipx-pl.easypack24.net"
_TIMEOUT = 15


class InPostError(Exception):
    pass


class InPostClient:
    def __init__(self, api_token: str, organization_id: str) -> None:
        self._org_id = organization_id
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
        )

    # ── Shipment creation ─────────────────────────────────────────────────────

    def create_paczkomat_shipment(
        self,
        *,
        receiver_first_name: str,
        receiver_last_name: str,
        receiver_email: str,
        receiver_phone: str,
        target_point: str,
        reference: str,
        template: str = "small",
    ) -> dict[str, Any]:
        payload = {
            "service": "inpost_locker_standard",
            "reference": reference,
            "receiver": {
                "first_name": receiver_first_name,
                "last_name": receiver_last_name,
                "email": receiver_email,
                "phone": receiver_phone,
            },
            "parcels": [{"template": template}],
            "custom_attributes": {
                "target_point": target_point,
                "sending_method": "dispatch_order",
            },
        }
        return self._post_shipment(payload)

    def create_kurier_shipment(
        self,
        *,
        receiver_first_name: str,
        receiver_last_name: str,
        receiver_email: str,
        receiver_phone: str,
        receiver_street: str,
        receiver_building_number: str,
        receiver_city: str,
        receiver_post_code: str,
        sender: dict[str, str],
        reference: str,
        weight_kg: float = 1.0,
        dimensions: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        dims = dimensions or {"length": 30, "width": 20, "height": 15}
        payload = {
            "service": "inpost_courier_standard",
            "reference": reference,
            "receiver": {
                "first_name": receiver_first_name,
                "last_name": receiver_last_name,
                "email": receiver_email,
                "phone": receiver_phone,
                "address": {
                    "street": receiver_street,
                    "building_number": receiver_building_number,
                    "city": receiver_city,
                    "post_code": receiver_post_code,
                    "country_code": "PL",
                },
            },
            "sender": sender,
            "parcels": [
                {
                    "dimensions": {
                        "unit": "mm",
                        "length": int(dims["length"] * 10),
                        "width": int(dims["width"] * 10),
                        "height": int(dims["height"] * 10),
                    },
                    "weight": {"unit": "kg", "amount": weight_kg},
                }
            ],
            "custom_attributes": {"sending_method": "dispatch_order"},
        }
        return self._post_shipment(payload)

    def _post_shipment(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{_BASE}/v1/organizations/{self._org_id}/shipments"
        resp = self._session.post(url, json=payload, timeout=_TIMEOUT)
        if not resp.ok:
            raise InPostError(
                f"InPost shipment creation failed {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        logger.info(
            "InPost shipment created: id=%s tracking=%s service=%s",
            data.get("id"),
            data.get("tracking_number"),
            data.get("service"),
        )
        return data

    # ── Dispatch order (kurier only) ──────────────────────────────────────────

    def create_dispatch_order(
        self,
        shipment_id: str,
        sender: dict[str, str],
        *,
        pickup_date: str | None = None,
        pickup_from: str | None = None,
        pickup_to: str | None = None,
    ) -> dict[str, Any]:
        """Create a dispatch order (courier pickup).

        pickup_date: YYYY-MM-DD, pickup_from/pickup_to: HH:MM (min 2h window).
        If omitted, InPost picks the next available slot.
        """
        url = f"{_BASE}/v1/organizations/{self._org_id}/dispatch_orders"
        payload: dict[str, Any] = {
            "shipments": [shipment_id],
            "address": {
                "name": sender.get("name", ""),
                "phone": sender.get("phone", ""),
                "email": sender.get("email", ""),
                "street": sender.get("street", ""),
                "building_number": sender.get("building_number", "1"),
                "city": sender.get("city", ""),
                "post_code": sender.get("post_code", ""),
                "country_code": "PL",
            },
        }
        if pickup_date:
            payload["pickup_date"] = pickup_date
        if pickup_from:
            payload["pickup_from"] = pickup_from
        if pickup_to:
            payload["pickup_to"] = pickup_to
        resp = self._session.post(url, json=payload, timeout=_TIMEOUT)
        if not resp.ok:
            raise InPostError(f"InPost dispatch order failed {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        logger.info("InPost dispatch order created: id=%s", data.get("id"))
        return data

    # ── Label ─────────────────────────────────────────────────────────────────

    def get_label(self, shipment_id: str) -> bytes:
        url = f"{_BASE}/v1/shipments/{shipment_id}/label"
        resp = self._session.get(url, timeout=_TIMEOUT)
        if not resp.ok:
            raise InPostError(f"InPost label fetch failed {resp.status_code}: {resp.text[:200]}")
        return resp.content
