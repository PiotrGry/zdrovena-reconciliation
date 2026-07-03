"""zdrovena.common.shipping_exceptions — Shipping error hierarchy.

Hierarchy::

    ZdrovenaShippingError
    ├── ShopifyPayloadError        — bad data from Shopify; return 200 (retry won't help)
    │   ├── MissingShippingAddressError
    │   ├── UnparseableShippingLineError
    │   ├── UnknownCarrierError
    │   ├── InvalidLockerIdError
    │   ├── InvalidPhoneNumberError
    │   ├── InvalidPostCodeError
    │   ├── UnparseableAddressError
    │   ├── WeightOutOfRangeError
    │   └── PackageTypeUnknownError
    ├── CourierAuthError           — 401/403 from courier; alert admin, return 500 for Shopify retry
    │   ├── InPostAuthError
    │   ├── ApaczkaSignatureError
    │   └── ApaczkaInsufficientBalanceError
    ├── CourierBusinessError       — 4xx business logic; return 200, alert operator
    │   ├── InPostLockerUnavailableError
    │   ├── InPostInvalidServiceError
    │   ├── ApaczkaServiceUnavailableError
    │   ├── PickupSlotUnavailableError
    │   └── AddressGeocodingError
    ├── CourierTransientError      — 5xx/network; retry with backoff, then return 500
    │   ├── CourierTimeoutError
    │   ├── CourierConnectionError
    │   └── CourierServerError
    └── CancellationError
        ├── ShipmentAlreadyDispatchedError
        ├── DispatchAlreadyAcceptedError
        └── MissingDispatchIdError

Each exception carries metadata for log reconstruction without visiting Shopify panel.

Orthogonal to the tree above, per-courier marker bases allow catching every error
from one courier regardless of handling semantics::

    InPostError   ← InPostAuthError, InPostBusinessError (+ locker/service),
                    InPostTransientError
    ApaczkaError  ← ApaczkaAuthError (+ signature/balance), ApaczkaBusinessError
                    (+ service), ApaczkaTransientError

Concrete classes multiply-inherit (InPostError, Courier*Error) so both axes work:
`except InPostError` and `except CourierAuthError` each match InPostAuthError.
"""

from __future__ import annotations

from zdrovena.common.exceptions import ZdrovenaError


class ZdrovenaShippingError(ZdrovenaError):
    """Base for all shipping pipeline errors."""

    def __init__(
        self,
        message: str,
        *,
        order_id: str = "",
        shopify_webhook_id: str = "",
        courier: str = "",
        action: str = "",
        payload_snippet: str = "",
    ) -> None:
        self.order_id = order_id
        self.shopify_webhook_id = shopify_webhook_id
        self.courier = courier
        self.action = action
        self.payload_snippet = payload_snippet
        super().__init__(message)


# ── Per-courier marker bases (unified catch: `except InPostError` / `ApaczkaError`) ──
# These are orthogonal to the handling-semantics axis (Auth/Business/Transient).
# The concrete InPost*/Apaczka* errors below multiply-inherit from BOTH a marker
# and a Courier*Error so callers can catch either "all InPost errors" or "all auth
# errors" without losing the other classification. Fixes audit F-I4 / F-A4, where
# InPostError/ApaczkaError lived outside ZdrovenaShippingError and escaped the
# shared `except ZdrovenaShippingError` handler as bare 500s.


class InPostError(ZdrovenaShippingError):
    """Base marker for all InPost (ShipX) errors."""


class ApaczkaError(ZdrovenaShippingError):
    """Base marker for all Apaczka errors."""


# ── Shopify payload errors (return 200, retry won't help) ─────────────────────


class ShopifyPayloadError(ZdrovenaShippingError):
    """Bad or unrecognised data from Shopify."""


class MissingShippingAddressError(ShopifyPayloadError):
    def __init__(self, order_id: str = "") -> None:
        super().__init__("shipping_address is None", order_id=order_id, action="validate_payload")


class UnparseableShippingLineError(ShopifyPayloadError):
    def __init__(self, title: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Cannot parse shipping_lines title: {title!r}",
            order_id=order_id,
            action="parse_shipping_line",
            payload_snippet=title[:100],
        )


class UnknownCarrierError(ShopifyPayloadError):
    def __init__(self, title: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Unknown carrier in shipping title: {title!r}",
            order_id=order_id,
            action="pick_courier",
            payload_snippet=title[:100],
        )


class InvalidLockerIdError(ShopifyPayloadError):
    def __init__(self, locker_id: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Locker ID does not match expected format: {locker_id!r}",
            order_id=order_id,
            action="parse_locker_id",
            payload_snippet=locker_id,
        )


class InvalidPhoneNumberError(ShopifyPayloadError):
    def __init__(self, phone: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Phone cannot be normalised to +48XXXXXXXXX: {phone!r}",
            order_id=order_id,
            action="normalize_phone",
            payload_snippet=phone,
        )


class InvalidPostCodeError(ShopifyPayloadError):
    def __init__(self, post_code: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Post code not in XX-XXX format: {post_code!r}",
            order_id=order_id,
            action="validate_address",
            payload_snippet=post_code,
        )


class UnparseableAddressError(ShopifyPayloadError):
    def __init__(self, address1: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Cannot split address into street + building number: {address1!r}",
            order_id=order_id,
            action="parse_address",
            payload_snippet=address1[:100],
        )


class WeightOutOfRangeError(ShopifyPayloadError):
    def __init__(self, weight_kg: float = 0.0, order_id: str = "") -> None:
        super().__init__(
            f"Order weight out of range: {weight_kg}kg (must be 0 < w <= 25)",
            order_id=order_id,
            action="validate_weight",
        )


class PackageTypeUnknownError(ShopifyPayloadError):
    def __init__(self, sku: str = "", order_id: str = "") -> None:
        super().__init__(
            f"SKU not in package type mapping: {sku!r}",
            order_id=order_id,
            action="map_package_type",
            payload_snippet=sku,
        )


# ── Courier auth errors (alert admin, return 500 for Shopify retry) ───────────


class CourierAuthError(ZdrovenaShippingError):
    """Auth failure with courier API — token expired or wrong credentials."""


class InPostAuthError(InPostError, CourierAuthError):
    def __init__(self, detail: str = "", order_id: str = "") -> None:
        super().__init__(
            f"InPost 401/403: {detail}",
            order_id=order_id,
            courier="inpost",
            action="authenticate",
        )


class ApaczkaAuthError(ApaczkaError, CourierAuthError):
    """401/403 (or HMAC rejection) from Apaczka."""


class ApaczkaSignatureError(ApaczkaAuthError):
    def __init__(self, detail: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Apaczka HMAC signature rejected: {detail}",
            order_id=order_id,
            courier="apaczka",
            action="authenticate",
        )


class AllegroAuthError(CourierAuthError):
    def __init__(self, detail: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Allegro OAuth 401/403: {detail}",
            order_id=order_id,
            courier="allegro",
            action="authenticate",
        )


class ApaczkaInsufficientBalanceError(ApaczkaAuthError):
    def __init__(self, order_id: str = "") -> None:
        super().__init__(
            "Apaczka account has insufficient balance",
            order_id=order_id,
            courier="apaczka",
            action="create_shipment",
        )


# ── Courier business errors (return 200, alert operator) ─────────────────────


class CourierBusinessError(ZdrovenaShippingError):
    """Business-logic rejection from courier — operator action required."""


class InPostBusinessError(InPostError, CourierBusinessError):
    """4xx business/validation rejection from InPost (bad address, dimensions, 422)."""


class ApaczkaBusinessError(ApaczkaError, CourierBusinessError):
    """4xx or in-body business error from Apaczka (invalid service, address, waybill)."""


class InPostLockerUnavailableError(InPostBusinessError):
    def __init__(self, locker_id: str = "", order_id: str = "") -> None:
        super().__init__(
            f"InPost locker {locker_id!r} is full or offline",
            order_id=order_id,
            courier="inpost",
            action="create_shipment",
            payload_snippet=locker_id,
        )


class InPostInvalidServiceError(InPostBusinessError):
    def __init__(self, detail: str = "", order_id: str = "") -> None:
        super().__init__(
            f"InPost rejected service/parcel dimensions: {detail}",
            order_id=order_id,
            courier="inpost",
            action="create_shipment",
        )


class InPostShipmentNotCancellable(InPostBusinessError):
    """Shipment is in a status that no longer allows cancellation.

    Raised by ``InPostClient.cancel_shipment`` when the pre-flight status
    check reveals the shipment has already been handed off to the courier
    (statuses like ``dispatched_by_sender``, ``sent_from_source_branch``,
    ``delivered``). Also raised when the server responds with a 422 to the
    DELETE call — kept as a dedicated subclass so callers can distinguish
    "you missed the window" from generic 4xx failures.
    """

    def __init__(
        self,
        shipment_id: str = "",
        current_status: str = "",
        order_id: str = "",
    ) -> None:
        message = f"InPost shipment {shipment_id!r} cannot be cancelled" + (
            f" (status={current_status!r})" if current_status else ""
        )
        super().__init__(
            message,
            order_id=order_id,
            courier="inpost",
            action="cancel_shipment",
            payload_snippet=current_status or shipment_id,
        )
        self.shipment_id = shipment_id
        self.current_status = current_status


class InPostOrganizationError(InPostBusinessError):
    """Non-recoverable organisation-level error surfaced by the InPost API.

    Covers cases like ``debt_collection`` (billing hold) and
    ``trucker_id_not_set`` (missing carrier assignment on the organisation).
    These are configuration/business issues that block *any* shipment for the
    organisation — no amount of retrying will fix them, so they are surfaced
    as their own subclass rather than looking like a transient 4xx.
    """

    def __init__(
        self,
        code: str = "",
        detail: str = "",
        order_id: str = "",
        action: str = "create_shipment",
    ) -> None:
        message = f"InPost organisation error {code!r}: {detail or '(no detail)'}"
        super().__init__(
            message,
            order_id=order_id,
            courier="inpost",
            action=action,
            payload_snippet=code,
        )
        self.code = code
        self.detail = detail


class ApaczkaServiceUnavailableError(ApaczkaBusinessError):
    def __init__(self, service_id: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Apaczka service {service_id!r} is currently unavailable",
            order_id=order_id,
            courier="apaczka",
            action="create_shipment",
        )


class AllegroBusinessError(CourierBusinessError):
    def __init__(self, detail: str = "", order_id: str = "", action: str = "allegro_call") -> None:
        super().__init__(
            f"Allegro business error: {detail}",
            order_id=order_id,
            courier="allegro",
            action=action,
        )


class AllegroCommandPending(AllegroBusinessError):
    """Ship with Allegro create-command jeszcze IN_PROGRESS po timeout krótkiego polling.

    To NIE jest twardy błąd — komenda jest kolejkowana po stronie Allegro.
    Wołający powinien zwrócić status='pending_confirmation' i pozostawić dopytanie
    o waybill oddzielnemu workerowi (nie tworzyć kolejnej komendy dla tego samego draftu!).
    """

    def __init__(self, command_id: str = "", order_id: str = "") -> None:
        super().__init__(
            detail=f"create-command {command_id} still pending (async)",
            order_id=order_id,
            action="wait_for_ship_with_allegro_shipment",
        )
        self.command_id = command_id


class PickupSlotUnavailableError(CourierBusinessError):
    def __init__(self, courier: str = "", order_id: str = "") -> None:
        super().__init__(
            "No pickup slots available (past cut-off or fully booked)",
            order_id=order_id,
            courier=courier,
            action="create_dispatch",
        )


class AddressGeocodingError(CourierBusinessError):
    def __init__(self, address: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Courier cannot geocode address: {address!r}",
            order_id=order_id,
            action="geocode_address",
            payload_snippet=address[:100],
        )


# ── Courier transient errors (retry with backoff, then 500) ──────────────────


class CourierTransientError(ZdrovenaShippingError):
    """Network/5xx error — safe to retry with exponential backoff."""


class CourierTimeoutError(CourierTransientError):
    def __init__(self, courier: str = "", action: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Courier request timed out ({courier}/{action})",
            order_id=order_id,
            courier=courier,
            action=action,
        )


class CourierConnectionError(CourierTransientError):
    def __init__(self, courier: str = "", detail: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Courier connection error ({courier}): {detail}",
            order_id=order_id,
            courier=courier,
            action="connect",
        )


class CourierServerError(CourierTransientError):
    def __init__(self, courier: str = "", status: int = 0, order_id: str = "") -> None:
        super().__init__(
            f"Courier server error {status} ({courier})",
            order_id=order_id,
            courier=courier,
            action="request",
        )


class InPostTransientError(InPostError, CourierTransientError):
    """5xx or network error from InPost — safe to retry with backoff."""


class ApaczkaTransientError(ApaczkaError, CourierTransientError):
    """5xx or network error from Apaczka — safe to retry with backoff."""


# ── Cancellation errors ───────────────────────────────────────────────────────


class CancellationError(ZdrovenaShippingError):
    """Errors during cancel operations."""


class ShipmentAlreadyDispatchedError(CancellationError):
    def __init__(self, shipment_id: str = "", courier: str = "") -> None:
        super().__init__(
            f"Shipment {shipment_id!r} already dispatched — cannot cancel via API",
            courier=courier,
            action="cancel_shipment",
        )


class DispatchAlreadyAcceptedError(CancellationError):
    def __init__(self, dispatch_id: str = "", courier: str = "") -> None:
        super().__init__(
            f"Dispatch order {dispatch_id!r} already accepted by courier — call support",
            courier=courier,
            action="cancel_dispatch",
        )


class MissingDispatchIdError(CancellationError):
    def __init__(self, draft_id: str = "") -> None:
        super().__init__(
            f"Draft {draft_id!r} has no dispatch_order_id — cannot cancel pickup",
            action="cancel_dispatch",
        )


# ── Fakturownia (invoicing) errors — reuse shipping hierarchy for uniform handling ──


class FakturowniaAuthError(CourierAuthError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            f"Fakturownia 401/403: {detail}",
            courier="fakturownia",
            action="authenticate",
        )


class FakturowniaBusinessError(CourierBusinessError):
    def __init__(self, detail: str = "", action: str = "fakturownia_call") -> None:
        super().__init__(
            f"Fakturownia business error: {detail}",
            courier="fakturownia",
            action=action,
        )


class FakturowniaServerError(CourierServerError):
    def __init__(self, status: int = 0) -> None:
        super().__init__(courier="fakturownia", status=status)
