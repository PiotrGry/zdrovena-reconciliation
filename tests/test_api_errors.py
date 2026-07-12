"""Tests for zdrovena.api.errors — unified error envelope + FastAPI handlers.

Verifies that shipping-domain exceptions map to a Polish ``message_pl``
envelope (not raw English ``str(exc)``) and that unhandled exceptions
return a generic 500 envelope instead of leaking a stack trace.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zdrovena.api.errors import (
    _classify,
    _details,
    _envelope,
    install_exception_handlers,
)
from zdrovena.common.shipping_exceptions import (
    ApaczkaServiceUnavailableError,
    CourierServerError,
    InPostAuthError,
    InPostLockerUnavailableError,
    MissingShippingAddressError,
    ShipmentAlreadyDispatchedError,
    ZdrovenaShippingError,
)


class TestClassify:
    def test_payload_error_is_400(self):
        http_status, msg = _classify(MissingShippingAddressError(order_id="o-1"))
        assert http_status == 400
        assert msg == "Brak adresu wysyłki w zamówieniu."

    def test_auth_error_is_502(self):
        http_status, _ = _classify(InPostAuthError(detail="token", order_id="o-1"))
        assert http_status == 502

    def test_transient_error_is_502_with_category_fallback(self):
        http_status, msg = _classify(CourierServerError(courier="inpost", status=503))
        assert http_status == 502
        # No per-class message → category fallback
        assert "Chwilowy problem" in msg

    def test_business_error_is_422_with_specific_message(self):
        http_status, msg = _classify(InPostLockerUnavailableError(locker_id="WAW01A"))
        assert http_status == 422
        assert msg == "Paczkomat InPost jest pełny lub niedostępny."

    def test_cancellation_error_is_409(self):
        http_status, msg = _classify(ShipmentAlreadyDispatchedError(shipment_id="s-1"))
        assert http_status == 409
        assert "już nadana" in msg

    def test_base_shipping_error_is_500(self):
        http_status, msg = _classify(ZdrovenaShippingError("boom"))
        assert http_status == 500
        assert msg == "Błąd przetwarzania przesyłki."


class TestDetails:
    def test_includes_only_non_empty_metadata(self):
        exc = InPostLockerUnavailableError(locker_id="WAW01A", order_id="o-9")
        details = _details(exc)
        assert details["courier"] == "inpost"
        assert details["action"] == "create_shipment"
        assert details["order_id"] == "o-9"

    def test_omits_empty_fields(self):
        exc = ZdrovenaShippingError("boom")
        assert _details(exc) == {}


class TestEnvelope:
    def test_shape_and_correlation_placeholder(self):
        env = _envelope(error_code="X", message_pl="msg", details={"a": 1})
        assert env == {
            "error_code": "X",
            "message_pl": "msg",
            "details": {"a": 1},
            "correlation_id": "-",
        }

    def test_details_defaults_to_empty_dict(self):
        env = _envelope(error_code="X", message_pl="msg")
        assert env["details"] == {}


def _app_with_handlers() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom-shipping")
    def _boom_shipping():
        raise ApaczkaServiceUnavailableError(service_id="svc-1", order_id="o-1")

    @app.get("/boom-generic")
    def _boom_generic():
        raise KeyError("secret-internal-detail")

    return app


class TestHandlersViaTestClient:
    def test_shipping_error_returns_polish_envelope(self):
        client = TestClient(_app_with_handlers())
        res = client.get("/boom-shipping")
        assert res.status_code == 422
        body = res.json()
        assert body["error_code"] == "ApaczkaServiceUnavailableError"
        assert body["message_pl"] == "Wybrana usługa Apaczka jest obecnie niedostępna."
        assert body["details"]["courier"] == "apaczka"
        assert body["correlation_id"] == "-"

    def test_unhandled_error_returns_generic_500_without_leaking_detail(self):
        client = TestClient(_app_with_handlers(), raise_server_exceptions=False)
        res = client.get("/boom-generic")
        assert res.status_code == 500
        body = res.json()
        assert body["error_code"] == "INTERNAL_ERROR"
        # Raw exception text must NOT leak to the operator-facing message.
        assert "secret-internal-detail" not in body["message_pl"]
        assert "nieoczekiwany błąd serwera" in body["message_pl"]
