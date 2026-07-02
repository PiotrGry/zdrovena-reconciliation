# Audyt cross-cutting — antypaterny wspólne dla wszystkich integracji

**Zakres**: wzorce projektowe i antypaterny, które pojawiają się w więcej niż jednej integracji. Ten raport konsoliduje findings z Allegro, Fakturownia, Shopify, InPost i Apaczka.

**Metodologia**: agregacja `ruff strict` + `pyright` + manual review z §3 raportów 01-05.

---

## 1. Hierarchia wyjątków — potrzeba unifikacji

### 1.1 Obecny stan

`zdrovena/common/shipping_exceptions.py` definiuje bogatą hierarchię 30+ klas:

```
ZdrovenaError
└── ZdrovenaShippingError
    ├── ShopifyPayloadError → 400 (walidacja Shopify webhook payload)
    ├── CourierAuthError → 502 (auth do dostawcy)
    │   ├── AllegroAuthError
    │   ├── InPostAuthError
    │   ├── ApaczkaSignatureError
    │   └── ApaczkaInsufficientBalanceError
    ├── CourierBusinessError → 502 (4xx z API dostawcy)
    │   ├── AllegroBusinessError
    │   │   └── AllegroCommandPending
    │   ├── InPostLockerUnavailableError
    │   ├── InPostInvalidServiceError
    │   ├── ApaczkaServiceUnavailableError
    │   ├── PickupSlotUnavailableError
    │   └── AddressGeocodingError
    ├── CourierTransientError → 502 (retry-able)
    │   ├── CourierTimeoutError
    │   ├── CourierConnectionError
    │   └── CourierServerError
    ├── CancellationError → 409
    │   ├── ShipmentAlreadyDispatchedError
    │   ├── DispatchAlreadyAcceptedError
    │   └── MissingDispatchIdError
    ├── FakturowniaAuthError → CourierAuthError
    ├── FakturowniaBusinessError → CourierBusinessError
    └── FakturowniaServerError → CourierServerError
```

**Ocena**: hierarchia jest **kompletna i dobrze zaprojektowana**. Semantyka: Auth = alert, Business = 4xx, Transient = retry-able.

### 1.2 🟥 CRITICAL — klienci NIE UŻYWAJĄ tej hierarchii konsekwentnie

| Klient                       | Używa hierarchii? | Rzuca                                    |
| ---------------------------- | ----------------- | ---------------------------------------- |
| `allegro.py`                 | ✅ (fully)         | AllegroAuthError, AllegroBusinessError, CourierServerError |
| `fakturownia.py`             | ✅ (fully)         | FakturowniaAuthError, FakturowniaBusinessError, FakturowniaServerError, CourierTimeoutError, CourierConnectionError |
| **`inpost.py`**              | 🟥 **NIE**         | `InPostError(Exception)` **poza hierarchią** |
| **`apaczka.py`**             | 🟥 **NIE**         | `ApaczkaError(Exception)` **poza hierarchią** |

**Konsekwencja**: router `webhooks.py` łapie `except Exception` (BLE001) bo nie może polegać na `except ZdrovenaShippingError` — bo InPost/Apaczka rzucają **poza** tą hierarchią.

**Fix wspólny (F-X1 CRITICAL)** — Blok A w Raportach InPost (F-I4) + Apaczka (F-A4):
1. Usuń `InPostError(Exception)` i `ApaczkaError(Exception)`.
2. Refactor klientów na typowaną hierarchię (istniejące klasy w `shipping_exceptions.py`).
3. Alias `InPostError = ApaczkaError = ZdrovenaShippingError` (deprecated, 1 release).
4. W routerze zamień `except Exception` → `except ZdrovenaShippingError` + granular handling.

---

## 2. `except Exception` (BLE001) — 12 miejsc w kodzie

### 2.1 Lokalizacje

| Plik                        | Linie                       | Powód                                                             |
| --------------------------- | --------------------------- | ----------------------------------------------------------------- |
| `allegro_poller.py`         | 56, 67, 87, 100             | Poller: łapie wszystko żeby nie crashować cyklu                    |
| `fakturownia_patcher.py`    | 100, 113, 132               | Patcher: j.w.                                                     |
| `webhooks.py`               | 100, 187, 321, 528, 732, 869, 934, 1039 | Routery: łapią all → 500                              |
| `apaczka.py`                | 75, 92                      | Cache read/write fallback                                          |

### 2.2 Fix wspólny (F-X2)

```python
# BAD
try:
    do_work()
except Exception as exc:
    logger.error("failed: %s", exc)

# GOOD
try:
    do_work()
except (CourierTransientError, CourierAuthError, CourierBusinessError):
    logger.exception("Known error class")
    raise  # lub obsłuż kontekstowo
except Exception:
    logger.exception("Unexpected error — please add to hierarchy")
    raise
```

Pattern **guard rails + typed catches**:
- `except (Typed1, Typed2)` — obsłuż każdy explicit
- `except Exception` — tylko jako ostatni resort, ZAWSZE z `.exception()` i re-raise / metric

**Whitelist**: routery mogą łapać `Exception` przed konwersją na `HTTPException(500)`, ale ZAWSZE z `.exception()`.

---

## 3. `logging.error(..., exc)` (TRY400) — 12 miejsc

Zamiana na `.exception(...)` — traceback bierze się automatycznie z `exc_info=True`.

### 3.1 Lokalizacje

| Plik                        | Linie                       |
| --------------------------- | --------------------------- |
| `allegro_poller.py`         | 57, 68, 88                  |
| `fakturownia_patcher.py`    | 101, 114, 133               |
| `webhooks.py`               | 188, 870, 935, 1040         |
| `apaczka.py`                | 93 (jest `.warning`, docelowo `.exception`) |

### 3.2 Fix wspólny (F-X3)

```python
# BAD
logger.error("Allegro list_orders failed: %s", exc)

# GOOD
logger.exception("Allegro list_orders failed")
# → automatycznie dołącza stack trace
```

**Reguła**: **każdy** `except` **musi** używać `.exception()` (chyba że explicit `.debug()`/`.warning()` bez potrzeby traceback).

---

## 4. Magic HTTP status codes (PLR2004) — 15 miejsc

### 4.1 Lokalizacje

| Plik                        | Wartości                                 |
| --------------------------- | ---------------------------------------- |
| `allegro.py`                | 400, 500, 204 (linie 168, 173, 182, 191) |
| `fakturownia.py`            | 200, 300, 204, 500, 600 (linia 118, 133) |
| `inpost.py`                 | 422 (linia 264, 278)                     |
| `webhooks.py`               | wiele (400, 401, 404, 409, 502, 503, ...) |

### 4.2 Fix wspólny (F-X4)

Stworzyć `zdrovena/common/http_status.py`:
```python
# Successful
HTTP_OK = 200
HTTP_NO_CONTENT = 204

# Client errors
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE_ENTITY = 422

# Server errors
HTTP_INTERNAL_SERVER_ERROR = 500
HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503

# Ranges
HTTP_STATUS_SUCCESS = range(200, 300)
HTTP_STATUS_CLIENT_ERROR = range(400, 500)
HTTP_STATUS_SERVER_ERROR = range(500, 600)
```

**Alternatywa**: użyć `http.HTTPStatus` z stdlib (enum). Preferowane, bo:
- `HTTPStatus.NO_CONTENT.value == 204`
- `HTTPStatus.OK` czytelne w kodzie
- Zero maintenance ze stronny naszej strony

**Fix (F-X4)**: zamień wszystkie liczby-magic na `HTTPStatus.<NAME>.value` LUB własne stałe.

---

## 5. Bezpieczeństwo — spotkane antypaterny

### 5.1 Idempotency — wszystkie POST-y

**Wszystkie 5 integracji** ma non-idempotent POST endpointy które **każdy call kosztuje** (opłata za przesyłkę, opłata za command Allegro, itd.). Brak `Idempotency-Key` header na naszych publicznych endpointach.

**Fix (F-X5)**:
- Wszystkie publiczne POST `/allegro/...`, `/shipping/drafts/{id}/execute`, `/inpost/...`, `/apaczka/...` powinny wymagać nagłówka `Idempotency-Key: <uuid>`.
- Dedup w Redis TTL 30 min. Cache klucza + odpowiedzi. Duplikat → zwróć cached response.
- Alternatywnie: guardy w `shipping_store` (jest już `shopify_order_id` dedup w draftach).

### 5.2 Silent exceptions w idempotency check

`webhooks.py:719-734` (`_is_duplicate_webhook`) — `except Exception: pass` → jeśli storage padnie, tworzymy duplikaty draftów.

**Fix (F-X6)**: Fail-fast w idempotency checks. Silent fallback tylko dla non-critical work (np. cache write).

### 5.3 HMAC bypass (Shopify)

`ALLOW_UNSIGNED_SHOPIFY_WEBHOOKS=1` przepuszcza niesignowane webhooki. **Do usunięcia w prod** — patrz Raport Shopify F-S2.

---

## 6. Testowalność — braki wspólne

### 6.1 Brak testów kontraktowych

**Wszystkie 4 zewnętrzne integracje** (Allegro, Fakturownia, InPost, Apaczka) mają **jednostkowe testy z mocks**, ale **zero testów kontraktowych** przeciwko realnym sandboxom.

**Konsekwencja**: nasze mocki mogą dryftować od kontraktu. Bug #1 (invoiceNumber vs number) był ukryty właśnie dlatego — 15 testów z fikcyjnym `"number"` przechodziło.

**Fix wspólny (F-X7)**:
- Nowa struktura: `tests/integration/test_<integration>_contract.py`
- Markery: `@pytest.mark.integration` (opt-in, nie w default fast gate)
- Runbook: cotygodniowy nightly run w CI przeciwko sandboxom
- Fixtures: przechowywać w `tests/integration/fixtures/` (JSON responses z realnych API)

### 6.2 Testy fixture drift

Fixture `docs/audit/fixtures/allegro_get_invoices.json` (z Fazy 2) jako "golden master" — testy powinny go używać, nie fikcyjnych struktur.

**Fix (F-X8)**: przy każdym integration test run, jeśli response format się zmieni → alert, wygeneruj diff, wymagaj human review.

---

## 7. Logging standard (docelowy)

### 7.1 Convention

```python
# Success (audit trail)
logger.info("%s: created %s (id=%s)", carrier, action, ext_id)

# Retry-able (transient)
logger.warning("%s: transient error, will retry: %s", carrier, exc)

# Fatal — with traceback
logger.exception("%s: unexpected error", carrier)  # exc_info auto

# Security event
logger.warning("Shopify HMAC mismatch — rejected. correlation_id=%s", webhook_id)

# Debug (protocol tracing) — with PII redaction
logger.debug("Apaczka payload: %s", redact_pii(payload))
```

### 7.2 Structured fields obowiązkowe

Każdy log w integrationie MUSI zawierać:
- `carrier` (allegro/inpost/apaczka/fakturownia/shopify)
- `action` (list_orders/create_shipment/order_send/...)
- `external_id` (jeśli mamy — order_id, invoice_id, tracking_number)
- `correlation_id` (X-Shopify-Webhook-Id lub UUID z Idempotency-Key)

### 7.3 PII redaction

Payloady w `logger.debug` mogą zawierać email/telefon receivera. **Zawsze redaktować** przed logowaniem (regex email/phone → `***`).

---

## 8. Docstring vs kod — obietnice bez pokrycia

### 8.1 `webhooks.py:1-11` mówi:

```
POST /webhooks/shopify/order-created          — ✅ istnieje
GET  /shipping/drafts                         — ✅ istnieje
GET  /shipping/drafts/{id}/label              — ✅ istnieje
POST /shipping/drafts/{id}/execute            — ✅ istnieje
POST /shipping/drafts/{id}/pickup             — ✅ istnieje (Apaczka)
DELETE /shipping/drafts/{id}/shipment         — 🟥 NIE ISTNIEJE
DELETE /shipping/drafts/{id}/dispatch         — 🟥 NIE ISTNIEJE
```

**Fix (F-X9)** — **decyzja usera: dodaj DELETE endpointy** (nie usuwaj z docstringa):
- `DELETE /allegro/drafts/{id}/shipment` (Fix F6 z Raportu Allegro)
- `DELETE /allegro/drafts/{id}/dispatch` (Fix F7 z Raportu Allegro)
- `DELETE /inpost/shipments/{id}` (Fix F-I3 z Raportu InPost)
- `DELETE /inpost/dispatch_orders/{id}` (Fix F-I3 z Raportu InPost)
- `DELETE /apaczka/orders/{id}` (Fix F-A3 z Raportu Apaczka)

---

## 9. Podsumowanie fixów do finalnego PR

### 9.1 CRITICAL (blokujące feature)

| ID    | Integracja | Opis                                                                 | Szacunek LoC |
| ----- | ---------- | -------------------------------------------------------------------- | ------------ |
| F1    | Allegro    | `invoiceNumber` fix + 15 testów                                     | 20 + 60      |
| F2-F5 | Allegro    | 4 bugi Ship with Allegro `create-commands` payload                   | 80 + 100     |
| F6-F7 | Allegro    | DELETE endpointy + cancel methods                                    | 80 + 60      |
| F-S1  | Shopify    | `X-Shopify-Webhook-Id` dedup table                                   | 70 + 100     |
| F-S2  | Shopify    | Usuń `ALLOW_UNSIGNED_SHOPIFY_WEBHOOKS` z prod                        | 10 + 20      |
| F-S6  | Shopify    | Whitelist `X-Shopify-Topic` i `X-Shopify-Shop-Domain`                | 30 + 40      |
| F-I4  | InPost     | Refactor `InPostError(Exception)` → typowana hierarchia              | 100 + 80     |
| F-A4  | Apaczka    | Refactor `ApaczkaError(Exception)` → typowana hierarchia             | 80 + 60      |

### 9.2 MEDIUM (jakość / obserwability)

| ID    | Integracja | Opis                                                                 | Szacunek LoC |
| ----- | ---------- | -------------------------------------------------------------------- | ------------ |
| F-X1  | wspólny    | Zunifikuj `except ZdrovenaShippingError` w routerze                  | 50           |
| F-X2  | wspólny    | Typed catches (BLE001) — 12 miejsc                                   | 100          |
| F-X3  | wspólny    | `.error(..., exc)` → `.exception()` — 12 miejsc                      | 20           |
| F-X4  | wspólny    | Magic HTTP codes → `HTTPStatus.<NAME>.value`                         | 40           |
| F-X5  | wspólny    | `Idempotency-Key` header dla POST + Redis dedup                      | 80 + 100     |
| F-S3, F-A2 | Shopify + Apaczka | Race protection w duplikatach draftów                     | 40           |
| F-S5  | Shopify    | `get_draft_by_shopify_order_id` (indexed)                            | 30 + 30      |
| F-I3, F-I5 | InPost | Cancel endpointy + `_request()` helper                              | 100 + 80     |
| F-I1, F-I7 | InPost | Integration test `test_inpost_shipx_contract`                       | 80           |
| F-A1, F-A3 | Apaczka | Cancel endpoint + sign test                                        | 40 + 40      |
| F-A5  | Apaczka    | Cache read explicit types                                            | 15           |
| FA1-FA3 | Fakturownia | Integration test + deep copy + `id` assertion                     | 60 + 40      |

### 9.3 LOW (cosmetic, cross-cutting)

| ID          | Opis                                                          | Szacunek LoC |
| ----------- | ------------------------------------------------------------- | ------------ |
| F-X4        | Magic values → stałe (cały codebase)                          | 40           |
| F15         | Usuń 6 martwych Allegro methods                               | -100 (delete) |
| RUF002, RUF003 | Unicode chars w docstringach (`×`, `❓`, `✅`)              | 5            |
| PYI041      | `int | float` → `float`                                      | 5            |
| PLC0415     | Imports w środku funkcji (~10 miejsc)                         | 20           |
| ERA001      | Commented-out code                                            | 5            |
| EM101/EM102 | Whitelist w `pyproject.toml` (rekomendowane) LUB refactor    | 15           |

### 9.4 Skala PR

**Suma**: ~1400 linii kodu produkcyjnego + ~800 linii testów.

**Struktura commits w PR** (rekomendowana):
1. `feat(shipping-errors): unify InPost/Apaczka into ZdrovenaShippingError hierarchy` (F-I4 + F-A4 + F-X1)
2. `fix(allegro): invoiceNumber field, ship-with-allegro schema, sender/receiver blocks` (F1-F5)
3. `feat(allegro): add DELETE endpoints for shipment/dispatch cancel` (F6-F7)
4. `feat(inpost,apaczka): add DELETE cancel routers` (F-I3 + F-A3)
5. `fix(shopify): webhook-id dedup, remove unsigned bypass, topic whitelist` (F-S1, F-S2, F-S6)
6. `feat(shipping): add Idempotency-Key on public POST endpoints` (F-X5)
7. `chore(logging): replace .error(..., exc) with .exception()` (F-X3)
8. `chore(types): explicit exception catches instead of blind Exception` (F-X2)
9. `chore(constants): HTTPStatus enum for magic status codes` (F-X4)
10. `test(integration): contract tests for Allegro/Fakturownia/InPost/Apaczka` (F-X7, F-I1, F-A1, FA1)
11. `chore(cleanup): remove dead Allegro methods` (F15)

---

## Załączniki

- Raporty pełne per integracja: [01-allegro.md](01-allegro.md), [02-fakturownia.md](02-fakturownia.md), [03-shopify.md](03-shopify.md), [04-inpost.md](04-inpost.md), [05-apaczka.md](05-apaczka.md)
- Executive summary: [00-summary.md](00-summary.md)
- Fixtures: [fixtures/](fixtures/) — 3 pliki JSON z realnymi kontraktami Allegro
