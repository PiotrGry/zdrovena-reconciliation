# Audyt techniczny — Allegro

**Zakres**: `zdrovena/common/allegro.py`, `zdrovena/api/routers/allegro_poller.py`, `zdrovena/api/routers/fakturownia_patcher.py`, sekcje Allegro w `zdrovena/api/routers/webhooks.py`.

**Metodologia**: kontraktowy audyt względem [dokumentacji Allegro Developer](https://developer.allegro.pl/documentation) + `ruff check --select ALL` (strict) + `pyright` + manual review.

**Wynik pyright**: ✅ 0 errors, 0 warnings (wszystkie 3 pliki Allegro).
**Wynik ruff strict**: 🟥 15 znalezisk w poller + patcher, 11 znalezisk w `allegro.py`, 12 znalezisk w sekcji Allegro `webhooks.py`.

---

## 1. Kontraktowe niezgodności z API Allegro (CRITICAL)

Poniżej pełna tabela znalezionych rozbieżności między naszym kodem a rzeczywistym API. **Wszystkie 5 powodują 400 Bad Request / brakujące dane w produkcji.**

| #   | Severity     | Plik : Linia                            | Endpoint                                     | Nasz kod                              | Wg dokumentacji                          | Fixture                                                        |
| --- | ------------ | --------------------------------------- | -------------------------------------------- | ------------------------------------- | ---------------------------------------- | -------------------------------------------------------------- |
| 1   | 🟥 CRITICAL | `fakturownia_patcher.py:119`            | `GET /order/checkout-forms/{id}/invoices`    | `allegro_inv.get("number")`           | `allegro_inv.get("invoiceNumber")`       | [`fixtures/allegro_get_invoices.json`](fixtures/allegro_get_invoices.json)         |
| 2   | 🟥 CRITICAL | `allegro.py:323-341` + `webhooks.py:455-480` | `POST /shipping/drafts/{id}/create-commands` | `weight.amount`, `dimensions.length.amount`, `unit: "KILOGRAM"` | `weight.value`, flat `length.value` (bez `dimensions` wrapper), `unit: "KILOGRAMS"` (plural), `type: "PACKAGE"` wymagane | [`fixtures/allegro_create_commands_request.json`](fixtures/allegro_create_commands_request.json) |
| 3   | 🟥 CRITICAL | `allegro.py:323-341` + `webhooks.py:455-480` | `POST /shipping/drafts/{id}/create-commands` | Wysyłamy `orderId` (pole nie istnieje w API) | Wymagane bloki `sender` + `receiver` z pełnym adresem/telefonem/email | j.w.                                                           |
| 4   | 🟥 CRITICAL | `allegro.py:328` + `webhooks.py:468`    | `POST /shipping/drafts/{id}/create-commands` | `additionalServices: {"sendingAtPoint": "parcel_locker"}` (dict) | `additionalServices: ["sendingAtPoint"]` (Array of strings). Wartość `"parcel_locker"` nie występuje w API. | j.w.                                                           |
| 5   | 🟥 CRITICAL | `allegro.py:335` + `webhooks.py:474`    | `POST /shipping/drafts/{id}/create-commands` | `input.pickupPointId`                 | `input.receiver.point`                   | j.w.                                                           |

**Efekt sumaryczny**: cały flow "Wysyłam z Allegro" (feature dostarczony w PR #72) jest **nieoperacyjny** — 4 z 5 bugów są w jednym endpoincie i każdy pojedynczo powoduje 400 Bad Request. Bug #1 (invoiceNumber) sprawia, że `fakturownia_patcher` nie potrafi zidentyfikować faktury do dopięcia kaucji — mockowane testy przechodzą, bo używają fikcyjnego pola `number`.

### 1.1 Bug #6 — dokumentowe endpointy DELETE nie istnieją

`webhooks.py:1-11` docstring reklamuje endpointy DELETE `/shipping/drafts/{id}/shipment` i `/shipping/drafts/{id}/dispatch`. **Router ich nie definiuje.** Decyzja: **dodać brakujące endpointy w finalnym PR** (potwierdzone przez usera).

### 1.2 Bug #7 — pending martwe endpointy

7 z 16 endpointów Allegro w `allegro.py` nie ma call site w projekcie:

- `get_order`, `get_shipments`, `get_delivery_services`, `get_delivery_proposal`, `create_invoice_declaration`, `upload_invoice_file`, `get_ship_with_allegro_label`.

Rekomendacja: usunąć lub oznaczyć `# noqa: unused-public-api` z uzasadnieniem.

---

## 2. Endpointy — inwentaryzacja i status

### 2.1 Klient `AllegroClient` (16 metod, 10 użytkowane)

| #   | Method | Path                                                        | Idempotent | Retry safe | Użytkowane? |
| --- | ------ | ----------------------------------------------------------- | ---------- | ---------- | ----------- |
| 1   | POST   | `/auth/oauth/token`                                         | Tak (token refresh) | ✅ | ✅ (`_fetch_token`) |
| 2   | GET    | `/order/checkout-forms?status=…`                            | Tak         | ✅         | ✅ (`allegro_poller`) |
| 3   | GET    | `/order/checkout-forms/{id}`                                | Tak         | ✅         | 🔴 martwy    |
| 4   | PUT    | `/order/checkout-forms/{id}/fulfillment`                    | Tak         | ✅         | ✅ (`mark_order_processed`, poller) |
| 5   | GET    | `/order/checkout-forms/{id}/invoices`                       | Tak         | ✅         | ✅ (`fakturownia_patcher`) |
| 6   | POST   | `/order/checkout-forms/{id}/invoices`                       | ❌ NIE     | ❌ (idempotent tylko przez check-then-post) | ✅ (`create_invoice_declaration` — martwe) |
| 7   | PUT    | `/order/checkout-forms/{id}/invoices/{invoiceId}/file`      | Tak (upload)| ✅         | 🔴 martwy    |
| 8   | GET    | `/order/checkout-forms/{id}/shipments`                      | Tak         | ✅         | 🔴 martwy    |
| 9   | POST   | `/order/checkout-forms/{id}/shipments`                      | ❌         | ❌         | ✅ (tracking push, `webhooks.py:325`) |
| 10  | GET    | `/sale/delivery-services`                                   | Tak         | ✅         | 🔴 martwy    |
| 11  | GET    | `/shipping/carriers`                                        | Tak         | ✅         | ✅ (`webhooks.py:735`) |
| 12  | POST   | `/shipping/drafts/{id}/delivery-proposals`                  | ❌         | ❌         | 🔴 martwy    |
| 13  | POST   | `/shipping/drafts/{id}/create-commands` (SHIPMENT)          | ❌         | ❌         | ✅ (`create_ship_with_allegro_shipment`) 🟥 **5 bugów kontraktowych** |
| 14  | POST   | `/shipping/drafts/{id}/create-commands` (DISPATCH)          | ❌         | ❌         | ✅ (`create_ship_with_allegro_dispatch`) |
| 15  | GET    | `/shipping/drafts/{id}/commands/{cmdId}`                    | Tak         | ✅         | ✅ (polling `AllegroCommandPending`) |
| 16  | GET    | `/shipment-management/shipments/{id}/label`                 | Tak         | ✅         | 🔴 martwy    |

### 2.2 Router własny — 6 endpointów publicznych (`webhooks.py`)

| #   | Method | Path                                       | Idempotent? | Idempotency-Key? | Uwagi |
| --- | ------ | ------------------------------------------ | ----------- | ---------------- | ----- |
| 1   | POST   | `/allegro/poll-orders`                     | Semi (drafts) | ❌               | manualny trigger; poller wewnętrznie dedupuje po `order.id` |
| 2   | POST   | `/allegro/patch-invoices`                  | Semi         | ❌               | Fakturownia PATCH sam sprawdza istniejący settlement |
| 3   | POST   | `/allegro/orders/{id}/push-tracking`       | ❌          | ❌               | wielokrotny call → wielokrotne shipmenty w Allegro |
| 4   | POST   | `/allegro/drafts/{id}/create-shipment`     | ❌          | ❌               | wielokrotny call → wielokrotne commands (koszt!) |
| 5   | POST   | `/allegro/drafts/{id}/create-dispatch`     | ❌          | ❌               | wielokrotny call → wielokrotne dispatch |
| 6   | GET    | `/allegro/commands/{id}/{cmdId}`           | Tak          | ✅               | polling status |

**Brakujące DELETE**:
- `DELETE /shipping/drafts/{id}/shipment` (cancel przed dispatch)
- `DELETE /shipping/drafts/{id}/dispatch` (cancel przed acceptance)

---

## 3. Antypaterny — ruff strict (26 znalezisk w 4 plikach)

### 3.1 `allegro.py` (11 znalezisk)

| Reguła    | Ile | Linie                       | Severity  | Opis                                                                                        |
| --------- | --- | --------------------------- | --------- | ------------------------------------------------------------------------------------------- |
| PLR2004   | 5   | 168, 173, 182, 191          | 🟡 medium | Magic values (400, 500, 204). Fix: wprowadzić `HTTP_BAD_REQUEST = 400`, `HTTP_NO_CONTENT = 204` itd. |
| S101      | 1   | 124                         | 🟡 medium | `assert self._access_token is not None` — assert w kodzie prod. Fix: raise własny lub cast. |
| ERA001    | 1   | 293                         | 🟢 low    | Commented-out code (link do dokumentacji). Fix: przenieść do docstringa metody.             |
| E501      | 1   | 293                         | 🟢 low    | Line too long (104 > 100). Fix po ERA001.                                                    |
| PLC0415   | 1   | 441                         | 🟢 low    | `import base64` w środku funkcji `get_ship_with_allegro_label` (i tak martwe). Fix: usunąć martwe. |

### 3.2 `allegro_poller.py` (5 znalezisk — wszystkie o exception handling)

| Reguła    | Ile | Linie              | Severity  | Opis                                                                                        |
| --------- | --- | ------------------ | --------- | ------------------------------------------------------------------------------------------- |
| BLE001    | 4   | 56, 67, 87, 100    | 🟥 high    | `except Exception` — łapie WSZYSTKO włącznie z `KeyboardInterrupt` semantycznie, gubi CourierServerError → CourierTransientError. Fix: `except (AllegroBusinessError, AllegroAuthError, CourierTransientError) as exc:` |
| TRY400    | 3   | 57, 68, 88         | 🟥 high    | `logger.error` w bloku `except` bez traceback. Fix: `logger.exception(...)` (auto `exc_info=True`). |

### 3.3 `fakturownia_patcher.py` (7 znalezisk)

| Reguła    | Ile | Linie                       | Severity  | Opis                                                                                        |
| --------- | --- | --------------------------- | --------- | ------------------------------------------------------------------------------------------- |
| BLE001    | 3   | 100, 113, 132               | 🟥 high    | `except Exception` — jak w pollerze. Fix: typowane wyjątki + fallback `Exception` z re-raise. |
| TRY400    | 3   | 101, 114, 133               | 🟥 high    | j.w. `logger.exception(...)` obowiązkowo w bloku except.                                    |
| RUF002    | 2   | 10, 77                      | 🟢 low    | Ambiguous `×` (MULTIPLICATION SIGN) w docstringu. Fix: `x` lub `*`.                          |

### 3.4 Sekcja Allegro `webhooks.py` (12 znalezisk)

| Reguła    | Ile | Linie                                | Severity  | Opis                                                                                        |
| --------- | --- | ------------------------------------ | --------- | ------------------------------------------------------------------------------------------- |
| BLE001    | 5   | 100, 187, 321, 528, 732              | 🟥 high    | Endpointy publiczne — łapią wszystko i zwracają 500. Powinny łapać typowane wyjątki + zwracać 4xx/5xx per HTTP semantyka. |
| TRY400    | 4   | 188, 870, 935, 1040                  | 🟥 high    | `logger.exception` w except.                                                                 |
| TRY003    | 2   | 446, 451                             | 🟢 low    | Długie wiadomości w `HTTPException(detail=...)`. OK dla routera FastAPI (użytkownik potrzebuje info) — **można whitelistować**. |
| TRY301    | 1   | 1036                                 | 🟢 low    | `raise` z outer `try`. OK jeśli intencjonalne.                                              |

---

## 4. Idempotency

### 4.1 Idempotent (safe do retry)

- `list_orders`, `list_order_invoices`, `get_command_status`, `get_shipping_carriers` — GET-y, safe.
- `mark_order_processed` — PUT z tym samym payloadem. Allegro akceptuje, ale **każdy call = koszt API**. Fix: `if form["fulfillment"]["status"] == "PROCESSING": skip`.

### 4.2 NIE-idempotent (wymaga guard)

| Endpoint                                    | Ryzyko                                             | Fix                                                                    |
| ------------------------------------------- | -------------------------------------------------- | ---------------------------------------------------------------------- |
| `push_shipment_tracking` (POST shipments)   | Duplikat = wielokrotne shipmenty w Allegro          | Dodać kolumnę `allegro_tracking_pushed_at` w `shipping_store`         |
| `create_ship_with_allegro_shipment`         | Duplikat = wielokrotne commands (opłata za każdy!) | Sprawdzić `draft.status IN ('SHIPMENT_CREATED','DISPATCH_ACCEPTED')`   |
| `create_ship_with_allegro_dispatch`         | j.w.                                                | j.w.                                                                    |
| POST `/allegro/drafts/{id}/create-shipment` | j.w., publiczny endpoint                            | Wymagać `Idempotency-Key` header w opts, dedup w Redis (5min TTL)      |

### 4.3 Rekomendacja: `Idempotency-Key` na wszystkich POST-ach

Wszystkie publiczne endpointy `/allegro/...` typu POST powinny obowiązkowo przyjmować header `Idempotency-Key: <uuid>` i dedupować w Redis (30 min TTL). Cache klucza + odpowiedzi (nie samego "widziałem"). Duplikat → zwróć cached response.

---

## 5. Exception mapping — audyt

### 5.1 Hierarchia w `shipping_exceptions.py` (istniejąca, dobra baza)

```
ZdrovenaError
└── ZdrovenaShippingError
    ├── ShopifyPayloadError → 400 (walidacja)
    ├── CourierAuthError → 502 (auth do dostawcy)
    │   ├── AllegroAuthError
    │   ├── InPostAuthError
    │   ├── ApaczkaSignatureError
    │   └── ApaczkaInsufficientBalanceError
    ├── CourierBusinessError → 502 (4xx z API dostawcy)
    │   └── AllegroBusinessError → AllegroCommandPending
    ├── CourierTransientError → 502 (retry-able)
    │   ├── CourierTimeoutError
    │   ├── CourierConnectionError
    │   └── CourierServerError
    └── CancellationError → 409
```

**Ocena**: hierarchia Allegro (`AllegroAuthError`, `AllegroBusinessError`, `AllegroCommandPending`, `CourierServerError` dla 5xx) jest **poprawna i kompletna**. `_request()` w `allegro.py:158-175` prawidłowo mapuje status code → wyjątek.

### 5.2 Problem — łapanie `except Exception`

Przykład (`allegro_poller.py:54-59`):
```python
try:
    forms = client.list_orders(status=status)
except Exception as exc:              # BLE001 🟥
    logger.error("Allegro list_orders failed: %s", exc)  # TRY400 🟥
    stats["errors"] += 1
    return stats
```

**Co się gubi:**
1. `CourierTransientError` powinno być retry-owane (backoff), nie połknięte.
2. `CourierAuthError` powinno być **alertem** (Slack/PagerDuty) — token się zepsuł.
3. Traceback jest tracony (brak `exc_info=True`).

**Fix (przykład dla poller):**
```python
try:
    forms = client.list_orders(status=status)
except CourierTransientError:
    logger.exception("Allegro transient error — will retry next cycle")
    stats["errors"] += 1
    stats["retryable_errors"] += 1
    return stats
except (AllegroAuthError, CourierAuthError):
    logger.exception("Allegro AUTH FAILURE — check token/creds")
    stats["errors"] += 1
    stats["auth_errors"] += 1
    raise  # niech alertuje wyżej
except AllegroBusinessError:
    logger.exception("Allegro business error — likely bad request")
    stats["errors"] += 1
    return stats
```

### 5.3 `InPostError` / `ApaczkaError` — poza hierarchią (heads-up dla InPost/Apaczka audytów)

`inpost.py:107` i `apaczka.py:28` definiują `class InPostError(Exception)` i `class ApaczkaError(Exception)` NIE dziedzicząc z `ZdrovenaShippingError`. Do refaktoru w audytach InPost/Apaczka.

---

## 6. Logging

### 6.1 Standard obowiązujący (do włączenia)

- Każdy `except` **musi** używać `logger.exception(...)` (nie `.error()` z `%s` na exc).
- Log message w formacie `<Klasa/Serwis> <akcja>: <konkret>` — np. `"Allegro list_orders failed"` (nie `"list_orders failed"`).
- Log level:
  - `INFO` — happy path (POST created, PUT succeeded).
  - `WARNING` — retry-able (transient, auth expired, empty result).
  - `ERROR` (przez `.exception()`) — nieoczekiwane, breaking.
- **NIE** logować payloadu z access tokenem, credentials, PII (email/tel klienta) w plain-text.

### 6.2 Obecny stan w plikach Allegro

| Plik                        | `.error(` z exc | `.exception(` | Standard? |
| --------------------------- | --------------- | ------------- | --------- |
| `allegro.py`                | 0               | 0             | brak (używa raise) |
| `allegro_poller.py`         | 3 (linie 57,68,88) | 0          | 🟥 do naprawy |
| `fakturownia_patcher.py`    | 3 (linie 101,114,133) | 0       | 🟥 do naprawy |
| `webhooks.py` (Allegro sekcja) | 4 (linie 188,870,935,1040) | 0 | 🟥 do naprawy |

---

## 7. Lista fixów do finalnego PR (Allegro)

**Blok A — Kontrakty API (CRITICAL, 5 bugów blokujących feature Wysyłam z Allegro):**

- [ ] **F1**: `fakturownia_patcher.py:119` — zmień `"number"` → `"invoiceNumber"`. Zaktualizuj 15 testów w `tests/test_fakturownia_patcher.py`.
- [ ] **F2**: `allegro.py:323-341` — przebuduj `packages` structure (`weight.value`, flat `length.value`, `unit: "KILOGRAMS"`, `type: "PACKAGE"`).
- [ ] **F3**: `allegro.py:323-341` + `webhooks.py:455-480` — dodać `sender`/`receiver` blocks (dane z `shipping_store` draftu, decyzja usera).
- [ ] **F4**: `allegro.py:328` + `webhooks.py:468` — `additionalServices` z dict → Array of strings.
- [ ] **F5**: `allegro.py:335` + `webhooks.py:474` — `pickupPointId` top-level → `input.receiver.point`.

**Blok B — Endpointy DELETE (feature completion):**

- [ ] **F6**: dodać `DELETE /allegro/drafts/{id}/shipment` → `allegro.py.cancel_ship_with_allegro_shipment(draft_id)` → `DELETE /shipping/drafts/{id}/shipment`.
- [ ] **F7**: dodać `DELETE /allegro/drafts/{id}/dispatch` → analogicznie.

**Blok C — Exception handling (Allegro-only, 12 miejsc):**

- [ ] **F8**: `allegro_poller.py:56,67,87,100` — zastąp `except Exception` typowanym łapaniem.
- [ ] **F9**: `fakturownia_patcher.py:100,113,132` — j.w.
- [ ] **F10**: `webhooks.py:100,187,321,528,732` (sekcja Allegro) — j.w.
- [ ] **F11**: zamień 10× `logger.error(..., exc)` na `logger.exception(...)`.

**Blok D — Idempotency:**

- [ ] **F12**: dodać `allegro_tracking_pushed_at` do `shipping_store`, w `push_shipment_tracking` skipnij jeśli set.
- [ ] **F13**: `mark_order_processed` — skip jeśli `fulfillment.status == "PROCESSING"`.
- [ ] **F14**: publiczne POST-y `/allegro/...` → wymóg `Idempotency-Key` header + Redis dedup.

**Blok E — Cleanup:**

- [ ] **F15**: usunąć 6 martwych metod (`get_order`, `get_shipments`, `get_delivery_services`, `get_delivery_proposal`, `create_invoice_declaration`, `upload_invoice_file`, `get_ship_with_allegro_label`).
- [ ] **F16**: `allegro.py:293` — commented-out link do docs → docstring.
- [ ] **F17**: `allegro.py:168,173,182,191` — magic values → stałe HTTP_*.
- [ ] **F18**: `fakturownia_patcher.py:10,77` — `×` → `x`.
- [ ] **F19**: docstring `webhooks.py:1-11` — usuń fałszywą wzmiankę o DELETE albo poczekać po F6/F7.

**Estymacja rozmiaru fixu**: ~450 linii kodu produkcyjnego + ~300 linii testów. **1 PR** (decyzja usera).

---

## Załączniki

- [fixtures/allegro_get_invoices.json](fixtures/allegro_get_invoices.json) — realny kontrakt `GET /order/checkout-forms/{id}/invoices` (pole `invoiceNumber`)
- [fixtures/allegro_post_shipment_request.json](fixtures/allegro_post_shipment_request.json) — realny kontrakt `POST shipments`
- [fixtures/allegro_create_commands_request.json](fixtures/allegro_create_commands_request.json) — poprawny schemat `POST create-commands`

**Źródła**:
- [Allegro Developer — Dokumentacja](https://developer.allegro.pl/documentation)
- [Wysyłam z Allegro — Tutorial](https://developer.allegro.pl/tutorials/jak-zarzadzac-przesylkami-przez-wysylam-z-allegro-LRVjK7K21sY)
- Content-Type: `application/vnd.allegro.public.v1+json`
