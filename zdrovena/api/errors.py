"""zdrovena.api.errors — jednolita koperta błędu API + handlery FastAPI.

Cel: przestać wyciekać surowe ``str(exc)`` (po angielsku, z detalami
technicznymi) do operatora. Zamiast tego każdy błąd domenowy przesyłek
mapowany jest na kopertę::

    {
        "error_code":     "INPOST_LOCKER_UNAVAILABLE",   # stabilny, maszynowy
        "message_pl":     "Paczkomat InPost jest pełny…", # dla operatora (PL)
        "details":        {"courier": "inpost", ...},     # metadane, bez PII
        "correlation_id": "a1b2c3d4e5f6"                  # z observability (PR-8)
    }

Front (``frontend/src/api.js`` → ``fetchJson``) czyta ``message_pl`` w
pierwszej kolejności, więc operator widzi czytelny polski komunikat.

Handlery HTTPException celowo NIE są nadpisywane — istniejące jawne
``raise HTTPException(..., detail=...)`` zwracają dalej ``{"detail": ...}``
(fetchJson czyta też ``detail``), więc nie zmieniamy kontraktu innych
routerów (close/files).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from zdrovena.api.observability import get_correlation_id
from zdrovena.common.shipping_exceptions import (
    CancellationError,
    CourierAuthError,
    CourierBusinessError,
    CourierTransientError,
    LabelNotReadyError,
    ShopifyPayloadError,
    ZdrovenaShippingError,
)

logger = logging.getLogger("zdrovena.api.errors")

# Polski komunikat dla konkretnej klasy wyjątku (nazwa klasy → tekst).
# Klasy bez wpisu spadają do komunikatu bazowego swojej kategorii.
_MESSAGES_PL: dict[str, str] = {
    # ── Shopify payload (dane zamówienia) ──
    "MissingShippingAddressError": "Brak adresu wysyłki w zamówieniu.",
    "UnparseableShippingLineError": "Nie można odczytać metody wysyłki z zamówienia.",
    "UnknownCarrierError": "Nieznany przewoźnik w metodzie wysyłki zamówienia.",
    "InvalidLockerIdError": "Nieprawidłowy identyfikator paczkomatu.",
    "InvalidPhoneNumberError": "Nieprawidłowy numer telefonu (wymagany format +48XXXXXXXXX).",
    "InvalidPostCodeError": "Nieprawidłowy kod pocztowy (wymagany format XX-XXX).",
    "UnparseableAddressError": "Nie można rozdzielić adresu na ulicę i numer budynku.",
    "WeightOutOfRangeError": "Waga przesyłki poza zakresem (dozwolone: 0 < waga ≤ 25 kg).",
    "PackageTypeUnknownError": "Brak mapowania typu paczki dla tego SKU.",
    # ── Auth przewoźnika ──
    "InPostAuthError": "Błąd uwierzytelnienia InPost (401/403) — sprawdź token API.",
    "ApaczkaSignatureError": "Apaczka odrzuciła podpis HMAC — sprawdź klucze API.",
    "AllegroAuthError": "Błąd autoryzacji Allegro (OAuth 401/403).",
    "ApaczkaInsufficientBalanceError": "Niewystarczające środki na koncie Apaczka.",
    # ── Błędy biznesowe przewoźnika ──
    "InPostLockerUnavailableError": "Paczkomat InPost jest pełny lub niedostępny.",
    "InPostInvalidServiceError": "InPost odrzucił usługę lub wymiary paczki.",
    "InPostShipmentNotCancellable": "Przesyłki InPost nie można już anulować (została nadana).",
    "InPostOrganizationError": (
        "Błąd konfiguracji organizacji InPost — skontaktuj się z administratorem."
    ),
    "ApaczkaServiceUnavailableError": "Wybrana usługa Apaczka jest obecnie niedostępna.",
    "AllegroBusinessError": "Błąd biznesowy Allegro — sprawdź szczegóły zamówienia.",
    "AllegroCommandPending": "Allegro nadal przetwarza przesyłkę — spróbuj ponownie za chwilę.",
    "PickupSlotUnavailableError": (
        "Brak dostępnych slotów podjazdu (po godzinie granicznej lub pełna rezerwacja)."
    ),
    "AddressGeocodingError": "Przewoźnik nie może zlokalizować podanego adresu.",
    "LabelNotReadyError": (
        "Etykieta nie jest jeszcze gotowa — przesyłka nie została jeszcze "
        "potwierdzona przez przewoźnika. Spróbuj ponownie za chwilę."
    ),
    # ── Anulowanie ──
    "ShipmentAlreadyDispatchedError": (
        "Przesyłka została już nadana — nie można anulować przez API."
    ),
    "DispatchAlreadyAcceptedError": (
        "Zlecenie podjazdu zostało już przyjęte — zadzwoń do wsparcia przewoźnika."
    ),
    "MissingDispatchIdError": "Draft nie ma identyfikatora podjazdu — nie można anulować.",
}

# Komunikat bazowy dla kategorii, gdy klasa nie ma wpisu w _MESSAGES_PL.
_CATEGORY_FALLBACK: list[tuple[type, int, str]] = [
    (ShopifyPayloadError, status.HTTP_400_BAD_REQUEST, "Nieprawidłowe dane zamówienia z Shopify."),
    (
        CourierAuthError,
        status.HTTP_502_BAD_GATEWAY,
        "Błąd uwierzytelnienia u przewoźnika — skontaktuj się z administratorem.",
    ),
    (
        CourierTransientError,
        status.HTTP_502_BAD_GATEWAY,
        "Chwilowy problem z przewoźnikiem — spróbuj ponownie za chwilę.",
    ),
    # LabelNotReadyError must precede the generic CourierBusinessError entry so
    # it maps to 409 (transient/informational), not 422 (operator must fix).
    (
        LabelNotReadyError,
        status.HTTP_409_CONFLICT,
        "Etykieta nie jest jeszcze gotowa — spróbuj ponownie za chwilę.",
    ),
    (
        CourierBusinessError,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "Przewoźnik odrzucił przesyłkę — wymagana reakcja operatora.",
    ),
    (CancellationError, status.HTTP_409_CONFLICT, "Nie można anulować przesyłki."),
]


def _classify(exc: ZdrovenaShippingError) -> tuple[int, str]:
    """Zwróć (http_status, message_pl) dla wyjątku przesyłkowego."""
    for base, http_status, fallback_msg in _CATEGORY_FALLBACK:
        if isinstance(exc, base):
            return http_status, _MESSAGES_PL.get(type(exc).__name__, fallback_msg)
    # Bazowy ZdrovenaShippingError bez kategorii
    return (
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        _MESSAGES_PL.get(type(exc).__name__, "Błąd przetwarzania przesyłki."),
    )


def _details(exc: ZdrovenaShippingError) -> dict[str, Any]:
    """Metadane pomocne przy diagnozie, bez wrażliwych danych klienta."""
    out: dict[str, Any] = {}
    for key in ("courier", "action", "order_id"):
        value = getattr(exc, key, "")
        if value:
            out[key] = value
    return out


def _envelope(
    *, error_code: str, message_pl: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "error_code": error_code,
        "message_pl": message_pl,
        "details": details or {},
        "correlation_id": get_correlation_id(),
    }


def install_exception_handlers(app: FastAPI) -> None:
    """Zarejestruj handlery koperty błędu na instancji FastAPI."""

    @app.exception_handler(ZdrovenaShippingError)
    async def _shipping_error_handler(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: ZdrovenaShippingError
    ) -> JSONResponse:
        http_status, message_pl = _classify(exc)
        # Surowy (angielski) komunikat trafia tylko do logów, nie do operatora.
        logger.warning(
            "Shipping error %s on %s: %s",
            type(exc).__name__,
            request.url.path,
            exc,
        )
        return JSONResponse(
            status_code=http_status,
            content=_envelope(
                error_code=type(exc).__name__,
                message_pl=message_pl,
                details=_details(exc),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope(
                error_code="INTERNAL_ERROR",
                message_pl=(
                    "Wystąpił nieoczekiwany błąd serwera. Spróbuj ponownie lub "
                    "skontaktuj się z administratorem."
                ),
            ),
        )
