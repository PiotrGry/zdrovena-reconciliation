# Audyt techniczny — InPost (ShipX)

**Zakres**: `zdrovena/common/inpost.py` (295 linii) — klient ShipX API + kod webhooks.py w sekcji InPost.

**Metodologia**: kontraktowy audyt względem [InPost ShipX 1.9.0 Shipment docs](https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/18153485/1.9.0+Shipment) + `ruff strict` + `pyright` + manual review.

**Wynik pyright**: ✅ 0 errors.
**Wynik ruff strict**: 🟡 14 znalezisk (głównie RUF003 dla `❓/❌/✅` w komentarzach, TRY003/EM102 dla InPostError raise'ów).

**Ogólna ocena**: kontrakt endpointów **zgodny** z ShipX API. Kluczowe problemy: `InPostError(Exception)` **poza hierarchią** `ZdrovenaShippingError` → utrudnia unified handling wyjątków w warstwie routera.

---

## 1. Kontraktowe niezgodności z InPost ShipX API

### 1.1 ✅ Zgodne z dokumentacją

| Aspekt                                    | Nasz kod                                       | Dokumentacja                                       | Status  |
| ----------------------------------------- | ---------------------------------------------- | -------------------------------------------------- | ------- |
| Base URL                                  | `https://api-shipx-pl.easypack24.net`         | api-shipx-pl.easypack24.net (potwierdzone URL-em)  | ✅      |
| Auth                                      | `Authorization: Bearer <token>`                | Bearer token                                       | ✅      |
| POST `/v1/organizations/{id}/shipments`   | `receiver.first_name/last_name/email/phone` + `receiver.address.{street,building_number,city,post_code,country_code}` + `sender` + `parcels[]` + `service` | Peer object: name, company_name, first_name, last_name, email, phone, address. Parcel: dimensions {length,width,height,unit=mm} + weight {amount,unit=kg} | ✅      |
| `parcels` w courier                       | `dimensions.unit="mm"`, `weight.unit="kg"`     | `unit: "mm"` (only), `unit: "kg"` (only)          | ✅      |
| `parcels` w paczkomat                     | `[{"template": "large"}]`                      | `template: String` predefined                      | ✅      |
| `service` values                          | `inpost_locker_standard`, `inpost_courier_standard` | Zgodne z dokumentacją                        | ✅      |
| DELETE `/v1/shipments/{id}`               | Kod wywołuje                                   | Zgodne (klient obsługuje `422 → already dispatched`) | ✅      |
| DELETE `/v1/organizations/{id}/dispatch_orders/{id}` | Kod wywołuje                            | Zgodne (klient obsługuje `422 → already accepted`) | ✅      |

**Zero bugów kontraktowych** w klienic InPost. 👍

### 1.2 🟡 MEDIUM — `custom_attributes.sending_method="dispatch_order"`

**Bug #I1 (MEDIUM)** — `inpost.py:143-145,194`. Wysyłamy `custom_attributes: {sending_method: "dispatch_order"}`. Dokumentacja ShipX 1.9.0 na przeglądanej stronie NIE dokumentuje `custom_attributes`. Może być feature specyficzny dla `organization_id` (custom pola per klient).

**Weryfikacja**: sprawdzić w `curl` przeciwko sandboxowi ShipX czy field jest akceptowany (integration test opt-in). Alternatywnie: pobrać CzeK z **InPost support** czy `custom_attributes.sending_method` to publiczny kontrakt.

**Fix (F-I1)**: dodać integration test `test_inpost_shipx_contract.py` (opt-in, `@pytest.mark.integration`) który waliduje POST przeciwko sandboxowi.

### 1.3 🟢 LOW — `target_point` dla paczkomatu

Kod używa `custom_attributes.target_point: <locker_id>`. Standardowy pattern ShipX dla paczkomatu to `receiver.address.line1 = "PACZKOMAT_ID"` LUB `custom_attributes.target_point`. Oba są używane w różnych integracjach — nasze użycie **prawdopodobnie poprawne** (potwierdza działanie w prod), ale warto zweryfikować.

**Fix (F-I2)**: włącz w F-I1 integration test też paczkomatowy flow.

---

## 2. Endpointy — inwentaryzacja

| #   | Method | Path                                            | Idempotent | Retry safe | Użytkowane? |
| --- | ------ | ----------------------------------------------- | ---------- | ---------- | ----------- |
| 1   | POST   | `/v1/organizations/{id}/shipments`              | ❌         | ❌         | ✅ (paczkomat + kurier) |
| 2   | POST   | `/v1/organizations/{id}/dispatch_orders`        | ❌         | ❌         | ✅          |
| 3   | DELETE | `/v1/shipments/{id}`                            | Tak         | ✅         | 🟡 metoda istnieje, ale **brak routera** (`webhooks.py`) — Fix F-I3 |
| 4   | DELETE | `/v1/organizations/{id}/dispatch_orders/{id}`   | Tak         | ✅         | 🟡 j.w.     |
| 5   | GET    | `/v1/shipments/{id}/label`                      | Tak         | ✅         | ✅ (get_label) |

**Brakuje 2 routerów DELETE** (zgodne z deklaracją w naszym `webhooks.py:1-11` docstringu, ale nie zaimplementowane):
- `DELETE /inpost/shipments/{id}` → `client.cancel_shipment(id)`
- `DELETE /inpost/dispatch_orders/{id}` → `client.cancel_dispatch_order(id)`

---

## 3. Antypaterny — ruff strict (`inpost.py`)

| Reguła    | Ile | Linie                       | Severity  | Opis                                                                                        |
| --------- | --- | --------------------------- | --------- | ------------------------------------------------------------------------------------------- |
| TRY003    | 7   | 202, 252, 265, 268, 279, 282, 293 | 🟢 low  | Długie messages w `raise InPostError(...)`. Fix: struct wyjątków (Fix I5) lub whitelist.    |
| EM102     | 7   | j.w.                        | 🟢 low    | f-string literal w raise. Powiązane z TRY003.                                                |
| RUF003    | 4   | 72, 80, 87, 94              | 🟢 low    | Ambiguous chars `❓/✅` w KOMENTARZACH. **False positive** — to dokumentacja. Whitelist. |
| PLR2004   | 3   | 264, 278                    | 🟡 medium | Magic value `422`. Fix: `HTTP_UNPROCESSABLE_ENTITY = 422` (do cross-cutting).               |

---

## 4. Idempotency

### 4.1 Klient

- **POST shipments/dispatch_orders NIE są idempotent.** Każdy call = nowa przesyłka (opłata!).
- **DELETE endpointy są idempotent**: 200 przy sukcesie, 422 przy "already dispatched" (semantyczne "no-op czy conflict").

### 4.2 Braki

- ShipX nie oferuje `Idempotency-Key` header — dedup musi być po naszej stronie (`shipping_store` guard: nie POST-uj jeśli `courier_draft_id` już set na draft).
- Obecnie: `_run_inpost` (`webhooks.py:243+`) nie sprawdza czy `draft.courier_draft_id` już istnieje — retry z UI może utworzyć duplikat.

**Fix (F-I3)**: w `_run_inpost` sprawdzać przy wejściu:
```python
if draft.get("courier_draft_id"):
    raise HTTPException(409, "Shipment already created — DELETE first if you want to recreate")
```

---

## 5. Exception mapping — 🟥 CRITICAL problem

### 5.1 `InPostError(Exception)` — poza hierarchią

**Bug #I2 (CRITICAL — antypater)** — `inpost.py:107-108`:
```python
class InPostError(Exception):
    pass
```

**Problemy**:
1. **Nie dziedziczy z `ZdrovenaShippingError`** → w routerze `except ZdrovenaShippingError` **NIE złapie** InPostError → wpadnie do bardziej ogólnego `except Exception` (BLE001).
2. **Brak rozróżnienia** auth vs business vs transient error — wszystkie mapuje na 1 klasę.
3. **Brak strukturalnych atrybutów** (courier, status, action) jak w `CourierBusinessError`.

**Fix (F-I4 CRITICAL)**: refactor `inpost.py` na wzór `fakturownia.py`:

```python
from zdrovena.common.shipping_exceptions import (
    CourierAuthError,
    CourierBusinessError,
    CourierServerError,
    CourierTimeoutError,
    CourierConnectionError,
    InPostAuthError,            # istnieje już
    InPostLockerUnavailableError, # istnieje już
    InPostInvalidServiceError,  # istnieje już
)

class InPostClient:
    def _parse_response(self, resp, *, method, path):
        status = resp.status_code
        if 200 <= status < 300:
            return resp.json() if status != 204 else None
        body = resp.text[:300]
        if status in (401, 403):
            raise InPostAuthError(detail=body)
        if status == 422:
            # ShipX validation error — mapować na business
            raise CourierBusinessError(courier="inpost", detail=body, action=f"{method} {path}")
        if 400 <= status < 500:
            raise CourierBusinessError(courier="inpost", detail=body, action=f"{method} {path}")
        if status >= 500:
            raise CourierServerError(courier="inpost", status=status)
```

Zamiast `raise InPostError(...)` — używać typowanej hierarchii.

**Backward compat**: pozostawić `InPostError = ZdrovenaShippingError` (deprecated alias) na 1 release, dodać deprecation warning.

### 5.2 Timeout/connection nieobsługiwane

**Bug #I3 (MEDIUM)** — `inpost.py:198-212, 250-255, 261-271, 275-285, 289-294` używa `self._session.post/get/delete` **bez try/except**. Jeśli `requests.Timeout` lub `requests.ConnectionError` — propaguje raw wyjątek. Powinno być zamapowane na `CourierTimeoutError` / `CourierConnectionError`.

**Fix (F-I5)**: dodać `_request()` helper (jak w `fakturownia.py:89-113`) z try/except na Timeout/ConnectionError.

---

## 6. Logging

### 6.1 Stan obecny

- `_post_shipment` (201): `logger.info("InPost shipment created: id=%s tracking=%s service=%s", ...)` ✅
- `create_dispatch_order` (232): `logger.info("InPost dispatch order created: id=%s", ...)` ✅
- `cancel_shipment` (250): `logger.info("InPost shipment cancelled: id=%s", ...)` ✅
- `cancel_dispatch_order` (264): `logger.info("InPost dispatch order cancelled: id=%s", ...)` ✅
- Brak logów WARNING/ERROR — wszystkie błędy propagują jako wyjątek. Router loguje sam.

**Ocena**: **dobrze**. Klient jest cichy w error path (raise), głośny w success path (info). 👍

### 6.2 Rekomendacja

Dodać `logger.debug` na req/resp payload (z redakcją PII — email, telefon receivera). Pomocne przy troubleshootingu.

---

## 7. Lista fixów do finalnego PR (InPost)

**Blok A — Exception refactor (CRITICAL):**

- [ ] **F-I4**: `inpost.py:107` — usuń `InPostError(Exception)`. Refactor `_post_shipment`, `cancel_shipment`, `cancel_dispatch_order`, `get_label` na typowaną hierarchię (`InPostAuthError`, `CourierBusinessError`, `CourierServerError`). Alias `InPostError = ZdrovenaShippingError` z `DeprecationWarning`.
- [ ] **F-I5**: Dodać `_request()` helper z mapowaniem `requests.Timeout` → `CourierTimeoutError`, `requests.ConnectionError` → `CourierConnectionError`.

**Blok B — Cancel endpoints (feature completion):**

- [ ] **F-I3**: Dodać w `webhooks.py`:
  - `DELETE /inpost/shipments/{id}` → `client.cancel_shipment(id)` → 204 lub 409 (już dispatched)
  - `DELETE /inpost/dispatch_orders/{id}` → `client.cancel_dispatch_order(id)` → 204 lub 409
  - `POST /shipping/drafts/{id}/execute` — guard `if draft.get("courier_draft_id"): raise 409`
- [ ] **F-I6**: usuń z docstringa `webhooks.py:1-11` obietnice DELETE albo poczekaj po F-I3.

**Blok C — Contract validation (MEDIUM):**

- [ ] **F-I1**: Integration test `tests/integration/test_inpost_shipx_contract.py` (opt-in, `@pytest.mark.integration`) — sandbox POST + assert response schema.
- [ ] **F-I7**: Rozwiązać `custom_attributes.sending_method`/`target_point` — potwierdzenie od InPost support LUB test na sandboxie.

**Blok D — Cosmetics (low, do cross-cutting):**

- [ ] **F-I8**: `inpost.py:264,278` — magic 422 → stała.
- [ ] **F-I9**: `inpost.py:72,80,87,94` — RUF003 (❓/✅) — whitelist w `pyproject.toml` (to dokumentacja, nie kod).

**Estymacja**: ~250 linii kodu produkcyjnego + ~150 linii testów + 1 nowy integration test file.

---

## Załączniki

**Źródła**:
- [InPost ShipX 1.9.0 Shipment](https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/18153485/1.9.0+Shipment)
- Wewnętrzna dokumentacja: `zdrovena/common/shipping_exceptions.py` (hierarchia bazowa)
