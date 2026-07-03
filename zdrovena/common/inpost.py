"""zdrovena.common.inpost — InPost ShipX API client.

Creates shipment drafts for paczkomat and kurier services.
Secrets: inpost-api-token, inpost-organization-id (Key Vault).
"""

from __future__ import annotations

import logging
import os
from http import HTTPStatus
from typing import Any

import requests

from zdrovena.common.shipping_exceptions import (
    InPostAuthError,
    InPostBusinessError,
    InPostError,
    InPostOrganizationError,
    InPostShipmentNotCancellable,
    InPostTransientError,
)

logger = logging.getLogger("zdrovena.common.inpost")

_BASE = os.environ.get("INPOST_BASE_URL", "https://api-shipx-pl.easypack24.net")
_TIMEOUT = int(os.environ.get("INPOST_TIMEOUT", "15"))
# Courier service slug. Production accounts with a signed contract use
# inpost_courier_standard (requires trucker_id). Sandbox / prepaid accounts
# without a contract must use inpost_courier_c2c. Set the env var only on
# sandbox; in production leave it unset to keep the standard service.
_COURIER_SERVICE = os.environ.get("INPOST_COURIER_SERVICE", "inpost_courier_standard")

# Organisation-level error codes surfaced as InPostOrganizationError.
# These are business/config problems that block ALL shipments for the account
# — no amount of retrying at the shipment level will unblock them.
_INPOST_ORG_ERROR_CODES = frozenset(
    {
        "debt_collection",  # organisation account is on billing hold
        "trucker_id_not_set",  # missing carrier assignment (kurier account)
    }
)

# Shipment statuses beyond which a cancel is no longer possible.
# Source: ShipX API status glossary. Anything at or past `dispatched_by_sender`
# means the parcel has been handed to the courier network.
_INPOST_UNCANCELLABLE_STATUSES = frozenset(
    {
        "dispatched_by_sender",
        "collected_from_sender",
        "taken_by_courier",
        "sent_from_source_branch",
        "adopted_at_source_branch",
        "out_for_delivery",
        "ready_to_pickup",
        "delivered",
        "returned_to_sender",
        "canceled",
    }
)


def _extract_error_code(resp: requests.Response) -> str:
    """Best-effort pull of the InPost error code from a 4xx response body.

    ShipX 4xx envelopes look like ``{"error": "...", "message": "...", ...}``
    or occasionally ``{"code": "...", "details": {...}}``. Returns "" if we
    can't parse JSON or find a known key.
    """
    try:
        payload = resp.json()
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "code"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""

# Physical dimensions and weights per package type produced by _calc_packages.
# Dimensions in cm; weight_kg is gross weight of a single box.
# szkło-2pak = two szkło boxes → same per-box spec, sent as qty=2 in parcels list.
# paczkomat_template: InPost locker template (A=small/B=medium/C=large); None = too big for any locker.
# dpd_template / orlen_template: to be filled when those carriers are integrated.
PARCEL_SPECS: dict[str, dict] = {
    "3-pak": {
        "length": 40,
        "width": 40,
        "height": 20,
        "weight_kg": 18.0,
        "paczkomat_template": "large",
    },
    "2-pak": {
        "length": 40,
        "width": 30,
        "height": 20,
        "weight_kg": 12.0,
        "paczkomat_template": "large",
    },
    "1-pak": {
        "length": 30,
        "width": 20,
        "height": 20,
        "weight_kg": 6.0,
        "paczkomat_template": "large",
    },
    "pół-pak": {
        "length": 20,
        "width": 15,
        "height": 20,
        "weight_kg": 3.0,
        "paczkomat_template": "large",
    },
    "szkło": {
        "length": 30,
        "width": 30,
        "height": 20,
        "weight_kg": 9.0,
        "paczkomat_template": "large",
    },
    "szkło-2pak": {
        "length": 30,
        "width": 30,
        "height": 20,
        "weight_kg": 9.0,
        "paczkomat_template": "large",
    },
}

# Max package dimensions that fit in the "large" slot of each carrier's locker/automat.
# Dimensions: height × width × depth (cm), max_weight_kg.
# ✅ = verified against carrier/aggregator website; ❓ = unverified, use with caution.
LOCKER_LARGE_SLOT: dict[str, dict] = {
    "inpost": {
        "height": 41,
        "width": 38,
        "depth": 64,
        "max_weight_kg": 25,
        "verified": True,
    },  # ✅ apaczka.pl / inpost.pl
    "orlen": {
        "height": 41,
        "width": 38,
        "depth": 60,
        "max_weight_kg": 20,
        "verified": True,
    },  # ✅ apaczka.pl (60×41×38)
    "dpd_automat": {
        "height": 50,
        "width": 44,
        "depth": 59,
        "max_weight_kg": 20,
        "verified": False,
    },  # ❓ DPD nie publikuje wymiarów skrytki
    "dpd_punkt": {
        "height": 64,
        "width": 41,
        "depth": 38,
        "max_weight_kg": 20,
        "verified": False,
    },  # ❓ DPD nie publikuje wymiarów skrytki
}

_DEFAULT_DIMS = PARCEL_SPECS["1-pak"]


class InPostClient:
    def __init__(self, api_token: str, organization_id: str) -> None:
        self._org_id = organization_id
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
        )

    # ── Low-level HTTP with typed error mapping ────────────────────────────────

    def _request(self, method: str, url: str, *, action: str, **kwargs: Any) -> requests.Response:
        """Perform a request and map failures onto the shared shipping hierarchy.

        Status mapping: 401/403 -> auth, other 4xx -> business, 5xx -> transient.
        Network failures (timeout/connection) are mapped to InPostTransientError so
        callers can retry them like any other transient courier error.
        """
        kwargs.setdefault("timeout", _TIMEOUT)
        verb = getattr(self._session, method.lower())
        try:
            resp = verb(url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise InPostTransientError(
                f"InPost network error ({action}): {exc}",
                courier="inpost",
                action=action,
            ) from exc

        if resp.ok:
            return resp

        status = resp.status_code
        body = (resp.text or "")[:300]
        if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise InPostAuthError(detail=body)
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise InPostTransientError(
                f"InPost server error {status}: {body}",
                courier="inpost",
                action=action,
            )
        if status >= HTTPStatus.BAD_REQUEST:
            # Surface known organisation-level codes as their own exception so
            # callers don't have to grep response bodies. InPost puts the code
            # in the top-level `error` field on ShipX 4xx envelopes.
            code = _extract_error_code(resp)
            if code in _INPOST_ORG_ERROR_CODES:
                raise InPostOrganizationError(
                    code=code,
                    detail=body,
                    action=action,
                )
            if action == "cancel_shipment" and status == HTTPStatus.UNPROCESSABLE_ENTITY:
                # Server told us the shipment can't be cancelled (usually because
                # it's already been dispatched). Surface the dedicated subclass.
                raise InPostShipmentNotCancellable(current_status=code or "")
            raise InPostBusinessError(
                f"InPost {status}: {body}",
                courier="inpost",
                action=action,
            )
        # Non-2xx that is neither 4xx nor 5xx (e.g. unexpected 3xx) — fall back to base.
        raise InPostError(
            f"InPost unexpected status {status}: {body}",
            courier="inpost",
            action=action,
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
        dims = dimensions or _DEFAULT_DIMS
        payload = {
            "service": _COURIER_SERVICE,
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
        resp = self._request("POST", url, action="create_shipment", json=payload)
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
        resp = self._request("POST", url, action="create_dispatch_order", json=payload)
        data = resp.json()
        logger.info("InPost dispatch order created: id=%s", data.get("id"))
        return data

    # ── Cancel ────────────────────────────────────────────────────────────────

    def get_shipment(self, shipment_id: str) -> dict[str, Any]:
        """Return the ShipX shipment envelope. Includes the current ``status``.

        Used as the pre-flight for cancel_shipment; also useful for reconciliation
        workers polling status transitions.
        """
        url = f"{_BASE}/v1/shipments/{shipment_id}"
        resp = self._request("GET", url, action="get_shipment")
        return resp.json()

    def cancel_shipment(self, shipment_id: str) -> None:
        """Cancel a shipment.

        Guard order:
          1. Pre-flight: GET the shipment and inspect ``status``. If it is one of
             the ``_INPOST_UNCANCELLABLE_STATUSES`` (already dispatched, delivered,
             etc.), raise :class:`InPostShipmentNotCancellable` *without* hitting
             DELETE — avoids the noisy 422 and gives callers a typed error.
          2. Send DELETE. If the server still returns 422 (race condition, or a
             status transition we haven't enumerated), ``_request`` surfaces the
             same :class:`InPostShipmentNotCancellable` exception.
        """
        try:
            existing = self.get_shipment(shipment_id)
        except InPostBusinessError:
            # get_shipment 404 or similar — fall through to DELETE which will
            # surface the definitive error. Never silently swallow.
            existing = {}
        current_status = str(existing.get("status") or "").strip()
        if current_status in _INPOST_UNCANCELLABLE_STATUSES:
            raise InPostShipmentNotCancellable(
                shipment_id=shipment_id,
                current_status=current_status,
            )

        url = f"{_BASE}/v1/shipments/{shipment_id}"
        self._request("DELETE", url, action="cancel_shipment")
        logger.info("InPost shipment cancelled: id=%s", shipment_id)

    def cancel_dispatch_order(self, dispatch_order_id: str) -> None:
        """Cancel a dispatch order. Only possible before courier accepts the pickup.

        A 422 (already accepted) surfaces as InPostBusinessError via _request.
        """
        url = f"{_BASE}/v1/organizations/{self._org_id}/dispatch_orders/{dispatch_order_id}"
        self._request("DELETE", url, action="cancel_dispatch_order")
        logger.info("InPost dispatch order cancelled: id=%s", dispatch_order_id)

    # ── Label ─────────────────────────────────────────────────────────────────

    def get_label(self, shipment_id: str) -> bytes:
        url = f"{_BASE}/v1/shipments/{shipment_id}/label"
        resp = self._request("GET", url, action="get_label")
        return resp.content
