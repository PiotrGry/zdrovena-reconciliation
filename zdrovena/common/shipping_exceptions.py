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


class InPostAuthError(CourierAuthError):
    def __init__(self, detail: str = "", order_id: str = "") -> None:
        super().__init__(
            f"InPost 401/403: {detail}",
            order_id=order_id,
            courier="inpost",
            action="authenticate",
        )


class ApaczkaSignatureError(CourierAuthError):
    def __init__(self, detail: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Apaczka HMAC signature rejected: {detail}",
            order_id=order_id,
            courier="apaczka",
            action="authenticate",
        )


class ApaczkaInsufficientBalanceError(CourierAuthError):
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


class InPostLockerUnavailableError(CourierBusinessError):
    def __init__(self, locker_id: str = "", order_id: str = "") -> None:
        super().__init__(
            f"InPost locker {locker_id!r} is full or offline",
            order_id=order_id,
            courier="inpost",
            action="create_shipment",
            payload_snippet=locker_id,
        )


class InPostInvalidServiceError(CourierBusinessError):
    def __init__(self, detail: str = "", order_id: str = "") -> None:
        super().__init__(
            f"InPost rejected service/parcel dimensions: {detail}",
            order_id=order_id,
            courier="inpost",
            action="create_shipment",
        )


class ApaczkaServiceUnavailableError(CourierBusinessError):
    def __init__(self, service_id: str = "", order_id: str = "") -> None:
        super().__init__(
            f"Apaczka service {service_id!r} is currently unavailable",
            order_id=order_id,
            courier="apaczka",
            action="create_shipment",
        )


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
