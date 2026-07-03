"""zdrovena.common.allegro — Allegro REST API v1 client.

Allegro has no webhooks — this client is used by pollers to sync orders and
invoices. Auth is OAuth 2.0 refresh-token flow (access 12h, refresh 3 months).
All external calls are made via a single `requests.Session` so tests can stub
`requests.Session.request` in one place.

Secrets (Azure Key Vault):
    allegro-client-id, allegro-client-secret, allegro-refresh-token

Environment vars:
    ALLEGRO_ENV=prod|sandbox (default prod)
    ALLEGRO_HTTP_TIMEOUT   (default 30 seconds)
"""

from __future__ import annotations

import logging
import os
import time
from http import HTTPStatus
from typing import Any, Protocol

import requests
from requests.auth import HTTPBasicAuth

from zdrovena.common.shipping_exceptions import (
    AllegroAuthError,
    AllegroBusinessError,
    AllegroCommandPending,
    CourierConnectionError,
    CourierServerError,
    CourierTimeoutError,
)

logger = logging.getLogger("zdrovena.common.allegro")

_BASE_URL_PROD = "https://api.allegro.pl"
_BASE_URL_SANDBOX = "https://api.allegro.pl.allegrosandbox.pl"
_AUTH_URL_PROD = "https://allegro.pl/auth/oauth/token"
_AUTH_URL_SANDBOX = "https://allegro.pl.allegrosandbox.pl/auth/oauth/token"

_ACCEPT_HEADER = "application/vnd.allegro.public.v1+json"
_DEFAULT_TIMEOUT = int(os.environ.get("ALLEGRO_HTTP_TIMEOUT", "30"))

# Refresh window before actual expiry so we never hand out an about-to-expire token.
_TOKEN_REFRESH_SKEW_S = 30


class AllegroTokenStore(Protocol):
    """Persistence contract for the OAuth refresh token.

    Allegro rotates the refresh token on every use — if we do not persist
    the new value, the first restart of the process loses it, and the
    integration dies until manual re-auth. Every production deployment MUST
    inject a real store (Key Vault / Table Storage). Tests may use the
    in-memory default.
    """

    def load_refresh_token(self) -> str | None:  # pragma: no cover - protocol
        ...

    def save_refresh_token(self, token: str) -> bool:  # pragma: no cover - protocol
        ...


class InMemoryAllegroTokenStore:
    """Trivial store — keeps the token in a single instance attribute.

    Useful for tests and for the CLI where the process is short-lived. NOT
    safe for long-running services (see AllegroTokenStore docstring).
    """

    def __init__(self, initial_token: str | None = None) -> None:
        self._token = initial_token

    def load_refresh_token(self) -> str | None:
        return self._token

    def save_refresh_token(self, token: str) -> bool:
        self._token = token
        return True


class SecretsAllegroTokenStore:
    """Persist rotated refresh tokens via ``zdrovena.common.secrets``.

    Reads/writes go through ``get_secret`` / ``set_secret``, so the token
    ends up in Key Vault (prod), keyring (dev), or env-var (last resort,
    with a loud warning). Errors during save are logged and surfaced to
    the caller as ``False`` — the caller is responsible for alerting when
    persistence fails, because a lost rotated token = broken integration.
    """

    _SECRET_NAME = "allegro-refresh-token"

    def load_refresh_token(self) -> str | None:
        from zdrovena.common.secrets import get_secret

        return get_secret(self._SECRET_NAME, required=False)

    def save_refresh_token(self, token: str) -> bool:
        from zdrovena.common.secrets import set_secret

        return set_secret(self._SECRET_NAME, token)


def _normalize_pickup_proposals(data: Any) -> list[dict[str, Any]]:
    """Flatten Allegro pickup-proposals response into a single list of slots.

    Handles three response shapes seen across Allegro API versions:

    1. **New nested (post-2026-07-01)** — top-level list, each entry has
       ``proposals[].pickupTimes[]``. Each pickupTime carries ``date`` /
       ``minTime`` / ``maxTime`` and no ``id``.
    2. **Legacy nested** — top-level list, each entry has
       ``proposals[].proposalItems[]``, each with a legacy ``id``.
    3. **Legacy flat** — top-level dict ``{"proposalItems": [...]}`` (older
       sandbox/mocked responses).

    Returns a flat list of dicts. New-format items include ``date``, ``minTime``,
    ``maxTime`` (usable directly with ``create_ship_with_allegro_pickup``'s
    ``pickup_time`` kwarg). Legacy items include ``id`` (usable with the
    deprecated ``proposal_item_id`` kwarg). If both shapes coexist in one
    response, both new AND legacy items are returned; callers should prefer
    entries that carry ``date``.
    """
    if isinstance(data, dict):
        legacy_flat = data.get("proposalItems")
        if isinstance(legacy_flat, list):
            return [item for item in legacy_flat if isinstance(item, dict)]
        # Fall through: dict may also be a single entry from the nested shape.
        entries: list[Any] = [data]
    elif isinstance(data, list):
        entries = data
    else:
        return []

    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for proposal in entry.get("proposals") or []:
            if not isinstance(proposal, dict):
                continue
            # New format — pickupTimes[]
            for pt in proposal.get("pickupTimes") or []:
                if isinstance(pt, dict) and pt.get("date"):
                    out.append({**pt, "shipmentId": proposal.get("shipmentId")})
            # Legacy format — proposalItems[] (deprecated, kept for compat)
            for pi in proposal.get("proposalItems") or []:
                if isinstance(pi, dict) and pi.get("id"):
                    out.append({**pi, "shipmentId": proposal.get("shipmentId")})
    return out


class AllegroClient:
    """Thin wrapper over Allegro REST API for orders + invoices flow.

    All methods raise a subclass of ZdrovenaShippingError on failure so callers
    can distinguish auth/business/transient errors without inspecting HTTP codes.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        env: str = "prod",
        timeout: int | None = None,
        token_store: AllegroTokenStore | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        # If a store is supplied AND it already holds a (possibly newer, rotated)
        # token, prefer that — the value in `refresh_token` may be stale from env.
        self._token_store: AllegroTokenStore = token_store or InMemoryAllegroTokenStore(
            refresh_token
        )
        stored = self._token_store.load_refresh_token()
        self._refresh_token = stored or refresh_token
        self._env = env
        if env == "sandbox":
            self._base_url = _BASE_URL_SANDBOX
            self._auth_url = _AUTH_URL_SANDBOX
        else:
            self._base_url = _BASE_URL_PROD
            self._auth_url = _AUTH_URL_PROD
        self._timeout = timeout or _DEFAULT_TIMEOUT
        self._session = requests.Session()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    # ── OAuth ──────────────────────────────────────────────────────────────

    def _fetch_token(self) -> None:
        """Refresh access token using the stored refresh_token grant.

        Allegro returns `{"access_token", "expires_in", "refresh_token", "token_type"}`.
        We cache both the access token and the (possibly rotated) refresh token.
        """
        try:
            resp = self._session.request(
                "POST",
                self._auth_url,
                auth=HTTPBasicAuth(self._client_id, self._client_secret),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise CourierTimeoutError(courier="allegro", action="oauth_token") from exc
        except requests.ConnectionError as exc:
            raise CourierConnectionError(courier="allegro", detail=str(exc)) from exc

        if resp.status_code in (401, 403):
            raise AllegroAuthError(detail=(resp.text or "")[:200])
        if not resp.ok:
            raise CourierServerError(courier="allegro", status=resp.status_code)

        payload = resp.json() or {}
        token = payload.get("access_token")
        if not token:
            raise AllegroAuthError(detail="missing access_token in response")
        self._access_token = token
        expires_in = int(payload.get("expires_in", 43200))
        self._expires_at = time.time() + max(expires_in - _TOKEN_REFRESH_SKEW_S, 0)
        # Refresh tokens rotate on every use. Persist the new one to the
        # injected store so a process restart does not lose it. A failed
        # persist is logged at ERROR by the store; we still keep the token
        # in-memory so the current process can continue — but alerts should
        # already fire and operators need to fix persistence before restart.
        new_rt = payload.get("refresh_token")
        if new_rt and new_rt != self._refresh_token:
            self._refresh_token = new_rt
            try:
                ok = self._token_store.save_refresh_token(new_rt)
            except Exception:  # pragma: no cover - defensive
                logger.exception("AllegroTokenStore.save_refresh_token raised")
                ok = False
            if not ok:
                logger.error(
                    "Rotated Allegro refresh token could NOT be persisted. "
                    "Current process is still authenticated, but a restart "
                    "will break the integration — fix the token store now."
                )

    def _get_token(self) -> str:
        if self._access_token is None or time.time() >= self._expires_at:
            self._fetch_token()
        assert self._access_token is not None
        return self._access_token

    # ── Low-level HTTP ─────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> requests.Response:
        token = self._get_token()
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": _ACCEPT_HEADER,
        }
        if extra_headers:
            headers.update(extra_headers)

        url = f"{self._base_url}{path}"
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": self._timeout,
        }
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data

        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.Timeout as exc:
            raise CourierTimeoutError(courier="allegro", action=path) from exc
        except requests.ConnectionError as exc:
            raise CourierConnectionError(courier="allegro", detail=str(exc)) from exc

        if resp.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise AllegroAuthError(detail=(resp.text or "")[:200])
        if HTTPStatus.BAD_REQUEST <= resp.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
            raise AllegroBusinessError(
                detail=f"{resp.status_code} {(resp.text or '')[:200]}",
                action=path,
            )
        if resp.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise CourierServerError(courier="allegro", status=resp.status_code)
        return resp

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params).json() or {}

    def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        resp = self._request("POST", path, json_body=json_body)
        if resp.status_code == HTTPStatus.NO_CONTENT:
            return {}
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    def _put_json(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        resp = self._request("PUT", path, json_body=json_body)
        if resp.status_code == HTTPStatus.NO_CONTENT:
            return {}
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    # ── Orders API ─────────────────────────────────────────────────────────

    def list_orders(
        self,
        *,
        status: str | None = None,
        bought_at_gte: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """List checkout forms (buyer orders).

        Filters (all optional):
            status: e.g. "READY_FOR_PROCESSING" | "BOUGHT" | "PROCESSING"
            bought_at_gte: ISO 8601 timestamp (line-items purchased at ≥ this)
            limit / offset: pagination
        """
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if bought_at_gte:
            params["lineItems.boughtAt.gte"] = bought_at_gte
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        data = self._get("/order/checkout-forms", params=params or None)
        return list(data.get("checkoutForms") or [])

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._get(f"/order/checkout-forms/{order_id}")

    def mark_order_processed(self, order_id: str, status: str = "PROCESSING") -> None:
        """PUT /order/checkout-forms/{id}/fulfillment with fulfillment status."""
        self._request(
            "PUT",
            f"/order/checkout-forms/{order_id}/fulfillment",
            json_body={"status": status},
        )

    # ── Shipments ──────────────────────────────────────────────────────────

    def create_shipment(
        self,
        *,
        order_id: str,
        carrier_id: str,
        waybill: str,
    ) -> dict[str, Any]:
        return self._post(
            f"/order/checkout-forms/{order_id}/shipments",
            {"carrierId": carrier_id, "waybill": waybill},
        )

    def get_shipments(self, order_id: str) -> list[dict[str, Any]]:
        data = self._get(f"/order/checkout-forms/{order_id}/shipments")
        return list(data.get("shipments") or [])

    # ── Invoices ───────────────────────────────────────────────────────────

    def list_order_invoices(self, order_id: str) -> list[dict[str, Any]]:
        data = self._get(f"/order/checkout-forms/{order_id}/invoices")
        return list(data.get("invoices") or [])

    def create_invoice_declaration(
        self,
        *,
        order_id: str,
        invoice_number: str,
        file_type: str = "VAT",
    ) -> dict[str, Any]:
        """Declare a new invoice for an order; returned id is used to upload PDF."""
        return self._post(
            f"/order/checkout-forms/{order_id}/invoices",
            {
                "invoiceNumber": invoice_number,
                "file": {"type": file_type},
            },
        )

    def upload_invoice_file(
        self,
        *,
        order_id: str,
        invoice_id: str,
        pdf_bytes: bytes,
    ) -> None:
        self._request(
            "PUT",
            f"/order/checkout-forms/{order_id}/invoices/{invoice_id}/file",
            data=pdf_bytes,
            extra_headers={"Content-Type": "application/pdf"},
        )

    # ── Wysyłam z Allegro / Ship with Allegro ────────────────────────────────
    # Docs: developer.allegro.pl/tutorials/jak-zarzadzac-przesylkami-przez-wysylam-z-allegro-LRVjK7K21sY

    def get_delivery_services(self) -> list[dict[str, Any]]:
        """List available delivery services (Allegro Standard + own agreements).

        .. deprecated:: 2026-Q1 2027
            This endpoint (``GET /shipment-management/delivery-services``) is
            marked for removal in Q1 2027. ``deliveryMethodId`` is now optional
            in ``create-commands`` — Allegro auto-derives it from the order.
            Do not call this in the request path; move to an offline job if
            you still need the list for UI/config.
        """
        import warnings

        warnings.warn(
            "AllegroClient.get_delivery_services is deprecated: Allegro is "
            "removing GET /shipment-management/delivery-services in Q1 2027. "
            "Rely on deliveryMethodId being auto-derived by the order.",
            DeprecationWarning,
            stacklevel=2,
        )
        data = self._get("/shipment-management/delivery-services")
        return list(data.get("deliveryServices") or [])

    def get_delivery_proposal(self, order_id: str) -> dict[str, Any]:
        """Proposed shipping data prefilled from the order."""
        return self._get(f"/shipment-management/delivery-proposals/{order_id}")

    def create_ship_with_allegro_shipment(
        self,
        *,
        command_id: str,
        order_id: str,
        credentials_id: str | None,
        packages: list[dict[str, Any]],
        sender: dict[str, Any],
        receiver: dict[str, Any],
        additional_services: list[str] | None = None,
        additional_properties: dict[str, Any] | None = None,
        delivery_method_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /shipment-management/shipments/create-commands.

        Contract (see docs/audit/fixtures/allegro_create_commands_request.json):
          - order_id is sent as ``referenceNumber`` (there is no ``orderId`` field).
          - ``sender`` / ``receiver`` are required address blocks (name, company,
            street, postalCode, city, state, countryCode, email, phone, point?).
            For pickup-point / locker deliveries put the point code in
            ``receiver["point"]``.
          - Each package must carry ``type: "PACKAGE"`` and FLAT dimensions
            (``length``/``width``/``height``/``weight`` each a ``{"value", "unit"}``
            object — weight unit is the plural ``KILOGRAMS``).
          - ``additional_services`` is an Array of Allegro service strings.
          - ``additional_properties`` is a dict of carrier-specific extras
            (e.g. ``{"inpost#sendingMethod": "parcel_locker"}`` — see Allegro
            issue #9915). Keys are namespaced by carrier; only sent when set.

        For Allegro Standard: pass credentials_id=None.
        For own agreements: pass the credentialsId returned by get_delivery_services.

        .. note::
            Since 2026-07-01 ``deliveryMethodId`` is optional — Allegro auto-
            derives it from the order. Omit it (or pass None) to future-proof
            against the Q1 2027 removal of GET /shipment-management/delivery-
            services. Kept accepting an explicit value for callers that still
            manage their own agreements manually.
        """
        input_body: dict[str, Any] = {
            "sender": sender,
            "receiver": receiver,
            "referenceNumber": order_id,
            "packages": packages,
        }
        if delivery_method_id:
            # Kept for callers using own agreements; Allegro Standard should
            # simply omit this and let the server pick.
            input_body["deliveryMethodId"] = delivery_method_id
        if credentials_id is not None:
            input_body["credentialsId"] = credentials_id
        if additional_services:
            input_body["additionalServices"] = list(additional_services)
        if additional_properties:
            input_body["additionalProperties"] = dict(additional_properties)

        return self._post(
            "/shipment-management/shipments/create-commands",
            {"commandId": command_id, "input": input_body},
        )

    def get_ship_with_allegro_command_status(self, command_id: str) -> dict[str, Any]:
        """Poll status of a create-command. Returns dict with status, shipmentId, errors."""
        return self._get(f"/shipment-management/shipments/create-commands/{command_id}")

    def wait_for_ship_with_allegro_shipment(
        self,
        command_id: str,
        *,
        max_attempts: int = 20,
        interval_s: float = 1.5,
    ) -> str:
        """Poll create-command until SUCCESS → returns shipmentId.

        Raises AllegroBusinessError on ERROR status or timeout.
        """
        for _ in range(max_attempts):
            payload = self.get_ship_with_allegro_command_status(command_id)
            status = payload.get("status")
            if status == "SUCCESS":
                ship_id = payload.get("shipmentId")
                if not ship_id:
                    raise AllegroBusinessError(
                        detail="SUCCESS status but no shipmentId returned",
                        action="wait_for_ship_with_allegro_shipment",
                    )
                return str(ship_id)
            if status == "ERROR":
                errors = payload.get("errors") or []
                detail = errors[0].get("message") if errors else "unknown error"
                raise AllegroBusinessError(
                    detail=f"create-command ERROR: {detail}",
                    action="wait_for_ship_with_allegro_shipment",
                )
            time.sleep(interval_s)
        # Timeout krótkiego polling — komenda może jeszcze ukończyć się asynchronicznie.
        # Osobny podtyp wyjątku pozwala wołającemu odróżnić pending od twardego ERROR bez
        # sprawdzania stringów.
        raise AllegroCommandPending(command_id=command_id)

    def get_ship_with_allegro_shipment(self, shipment_id: str) -> dict[str, Any]:
        """GET /shipment-management/shipments/{shipmentId} — full shipment with waybill."""
        return self._get(f"/shipment-management/shipments/{shipment_id}")

    @staticmethod
    def extract_shipment_waybill(
        shipment: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """Return (carrierId, carrierWaybill) from the first package's transportingInfo.

        Uses the NEW field packages.transportingInfo (packages.waybill was deprecated
        and removed on 2026-07-01).
        """
        packages = shipment.get("packages") or []
        if not packages:
            return (None, None)
        info_list = packages[0].get("transportingInfo") or []
        if not info_list:
            return (None, None)
        info = info_list[0]
        carrier = info.get("carrierId")
        waybill = info.get("carrierWaybill") or None
        return (carrier, waybill)

    def get_ship_with_allegro_pickup_proposals(
        self, shipment_ids: list[str]
    ) -> list[dict[str, Any]]:
        """POST /shipment-management/pickup-proposals — available pickup slots.

        Since 2026-07-01 Allegro replaced ``proposalItems`` with ``pickupTimes``
        (see https://developer.allegro.pl/news/wysylam-z-allegro-wprowadzilismy-
        zmiany-na-zasobach-do-zarzadzania-wysylka-przesylek-i-ich-odbiorem-przez-
        kuriera-oADdP41WVHA). The new response shape is::

            [
                {
                    "proposals": [
                        {
                            "shipmentId": "...",
                            "pickupTimes": [
                                {"date": "2026-01-17",
                                 "minTime": "08:00",
                                 "maxTime": "12:00"}
                            ]
                        }
                    ],
                    "address": {...}
                }
            ]

        Returned items are normalized to a flat list of dicts, each carrying at
        minimum a ``date`` key (new format) and, if present, the legacy ``id``
        (deprecated) — callers should prefer ``date``/``minTime``/``maxTime``.
        """
        data = self._post(
            "/shipment-management/pickup-proposals",
            {"input": {"shipmentIds": list(shipment_ids)}},
        )
        return _normalize_pickup_proposals(data)

    def create_ship_with_allegro_pickup(
        self,
        *,
        command_id: str,
        shipment_ids: list[str],
        pickup_time: dict[str, str] | None = None,
        proposal_item_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /shipment-management/pickups/create-commands — order courier pickup.

        Since 2026-07-01 Allegro accepts one of two mutually-exclusive fields:

        - ``pickupTime`` (preferred, new): ``{"date": "YYYY-MM-DD", "minTime":
          "HH:MM", "maxTime": "HH:MM"}``
        - ``pickupDateProposalId`` (deprecated): the ``id`` from the legacy
          ``proposalItems``

        At least one must be provided. If both are supplied, ``pickupTime`` wins.
        """
        if not pickup_time and not proposal_item_id:
            raise ValueError(
                "create_ship_with_allegro_pickup requires either pickup_time "
                "(new format) or proposal_item_id (legacy)."
            )
        input_body: dict[str, Any] = {"shipmentIds": list(shipment_ids)}
        if pickup_time:
            input_body["pickupTime"] = dict(pickup_time)
        else:
            # legacy path — pre-2026-07-01 servers
            input_body["pickupDateProposalId"] = proposal_item_id
        return self._post(
            "/shipment-management/pickups/create-commands",
            {"commandId": command_id, "input": input_body},
        )

    def cancel_ship_with_allegro_shipment(
        self, *, command_id: str, shipment_id: str
    ) -> dict[str, Any]:
        """POST /shipment-management/shipments/cancel-commands.

        Cancels a created shipment before it is dispatched.
        """
        return self._post(
            "/shipment-management/shipments/cancel-commands",
            {"commandId": command_id, "input": {"shipmentId": shipment_id}},
        )

    def cancel_ship_with_allegro_dispatch(
        self, *, command_id: str, dispatch_id: str
    ) -> dict[str, Any]:
        """POST /shipment-management/dispatches/cancel-commands.

        Cancels a dispatch (pickup) order before it is accepted by the courier.
        """
        return self._post(
            "/shipment-management/dispatches/cancel-commands",
            {"commandId": command_id, "input": {"dispatchId": dispatch_id}},
        )

    def get_ship_with_allegro_label(self, shipment_id: str) -> bytes:
        """GET /shipment-management/shipments/{shipmentId}/label — returns PDF bytes.

        Accepts either raw PDF response or a JSON envelope with base64-encoded label.
        """
        import base64 as _b64

        resp = self._request("GET", f"/shipment-management/shipments/{shipment_id}/label")
        headers = getattr(resp, "headers", {}) or {}
        content_type = (headers.get("Content-Type", "") if hasattr(headers, "get") else "").lower()
        if "pdf" in content_type or (resp.content and resp.content.startswith(b"%PDF")):
            return resp.content
        try:
            payload = resp.json() or {}
        except (ValueError, AttributeError):
            return resp.content
        encoded = payload.get("label") if isinstance(payload, dict) else None
        if encoded:
            return _b64.b64decode(encoded)
        return resp.content
