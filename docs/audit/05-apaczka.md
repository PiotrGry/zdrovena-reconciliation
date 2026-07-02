# Audyt techniczny — Apaczka

**Zakres**: `zdrovena/common/apaczka.py` (190 linii) — klient Apaczka Web API v2 z HMAC-SHA256 + kod webhooks.py w sekcji Apaczka.

**Metodologia**: kontraktowy audyt względem [Dokumentacja Web API ver. 2 (Apaczka)](https://panel.apaczka.pl/dokumentacja_api_v2.php) + `ruff strict` + `pyright` + manual review.

**Wynik pyright**: ✅ 0 errors.
**Wynik ruff strict**: 🟢 10+ znalezisk (głównie stylowe: TRY003, EM102, BLE001 w cache fallback).

**Ogólna ocena**: HMAC signature format i endpointy zgodne z dokumentacją. Kluczowe problemy identyczne jak w InPost: `ApaczkaError(Exception)` poza hierarchią + brak mapowania na retry-able.

---

## 1. Kontraktowe niezgodności z Apaczka API v2

### 1.1 ✅ Zgodne z dokumentacją

| Aspekt                            | Nasz kod                                                       | Dokumentacja                                                          | Status  |
| --------------------------------- | -------------------------------------------------------------- | --------------------------------------------------------------------- | ------- |
| Base URL                          | `https://www.apaczka.pl/api/v2` (`apaczka.py:22`)              | Zgodne                                                                | ✅      |
| Endpoint call: `POST {base}/{endpoint}/` | `f"{_BASE}/{endpoint}/"` (`apaczka.py:55`)              | "Wszystkie dane należy kierować na odpowiedni endpoint..."           | ✅      |
| Body params                       | `app_id`, `request`, `expires`, `signature`                    | Zgodne (4 wymagane pola)                                              | ✅      |
| Content-Type                      | `data=body` (form-encoded, requests default)                   | Zgodne (POST)                                                          | ✅      |
| HMAC signature                    | `HMAC-SHA256({app_id}:{endpoint}:{request_json}:{expires}, secret)` (`apaczka.py:32-42`) | "signature musi być wygenerowana na podstawie App ID, nazwy endpoint'u, danych w request oraz daty wygaśnięcia... HMAC + SHA256, klucz App Secret" | ✅      |
| `expires` max                     | `time.time() + 1800` (30 min) (`apaczka.py:34`)                | "Maksymalna ważność request'u to 30 minut"                            | ✅      |
| Response envelope                 | `result.get("status")` sprawdza (200 = OK) (`apaczka.py:59`) | Response wrapper z `status`                                            | ✅      |
| Endpoints użytkowane              | `service_structure`, `order_send`, `order_cancel`, `waybill`   | Wszystkie 4 udokumentowane                                             | ✅      |

**Zero bugów kontraktowych** w kliencie Apaczka. 👍

### 1.2 🟡 MEDIUM — signature format vs `json.dumps` separators

**Bug #A1 (MEDIUM)** — `apaczka.py:33`:
```python
request_json = json.dumps(data, separators=(",", ":"))
```

Dokumentacja Apaczka mówi że `request` to "zestaw wymaganych danych zapisanych w strukturze JSON" — **nie precyzuje** czy signature ma być liczony na compact JSON, na normalized JSON, ani jak encodować polskie znaki.

**Ryzyko**: jeśli Apaczka zmieni parsing (obecnie akceptuje compact JSON i signature obliczony na tym samym compact JSON — dowód: kod działa), to zmiana strategii separatorów po naszej stronie **zepsuje signature**. Trzeba mieć test regresyjny.

**Fix (F-A1)**: dodać unit test `test_apaczka_sign_signature` z hard-codowanymi:
- app_id, secret, endpoint, data, expires
- oczekiwany signature (obliczony ręcznie/z Postman)
Chroni przed regresją przy zmianie `separators`, `ensure_ascii`, `sort_keys`.

### 1.3 🟢 LOW — Brak `is_zebra` w request

Dokumentacja wspomina: *"Parametr is_zebra jest opcjonalny. W przypadku jego nie podania etykieta będzie wygenerowana zgodnie z ustawieniami konta."*

Nasz kod NIE ustawia `is_zebra`. **OK** — używamy default (Zebra vs A4 = konfiguracja konta).

---

## 2. Endpointy — inwentaryzacja

| #   | Method | Path                          | Idempotent | Retry safe | Użytkowane? |
| --- | ------ | ----------------------------- | ---------- | ---------- | ----------- |
| 1   | POST   | `/api/v2/service_structure/`  | Tak         | ✅         | ✅ (cache 23h) |
| 2   | POST   | `/api/v2/order_send/`         | ❌         | ❌         | ✅          |
| 3   | POST   | `/api/v2/order_cancel/`       | Tak (idempotent) | ✅   | ✅ metoda w kliencie, **brak routera** — Fix F-A3 |
| 4   | POST   | `/api/v2/waybill/`            | Tak         | ✅         | ✅ (get_label) |

**Brakuje 1 routera DELETE** (deklaracja w `webhooks.py:1-11` docstringu):
- `DELETE /apaczka/orders/{id}` → `client.cancel_shipment(id)`

---

## 3. Antypaterny — ruff strict (`apaczka.py`)

| Reguła    | Ile | Linie                       | Severity  | Opis                                                                                        |
| --------- | --- | --------------------------- | --------- | ------------------------------------------------------------------------------------------- |
| BLE001    | 2   | 75, 92                      | 🟡 medium | `except Exception: pass` w cache read (linia 75) i `logger.warning` bez `.exception` w cache write (linia 92). Fix: `except (KeyError, ValueError, json.JSONDecodeError, StorageError)` explicit. |
| TRY003    | 3   | 57, 60, 188                 | 🟢 low    | `raise ApaczkaError(f"...")`. Fix: wspólny z InPost (F-A4).                                 |
| EM102     | 3   | j.w.                        | 🟢 low    | j.w.                                                                                         |
| PLC0415   | 3   | 67, 86, 183                 | 🟢 low    | `import io`, `import base64` w środku funkcji. Fix: top-level.                              |

---

## 4. Idempotency

### 4.1 Klient

- `service_structure` — cachowany 23h (dobrze!). ✅
- `order_send` — NIE idempotent. Każdy call = nowe zamówienie = opłata!
- `order_cancel` — Apaczka akceptuje wielokrotny cancel na tym samym `order_id` (idempotent).
- `waybill` — GET semantics przez POST (nietypowe API, ale idempotent).

### 4.2 Braki

**Bug #A2 (MEDIUM)** — brak guard w `_run_apaczka` przed POST `order_send`. Jeśli draft ma już `courier_draft_id` (zamówienie utworzone) i user kliknie "execute" ponownie → duplikat zamówienia + duplikat opłaty.

**Fix (F-A2)**: analogicznie do InPost F-I3 — sprawdzać `draft.courier_draft_id` przed `_run_apaczka`.

### 4.3 Cache invalidation

Cache `service_structure` ma TTL 23h. **OK**, ale brak invalidation przy błędzie (np. gdy Apaczka doda nową usługę i my używamy 22h-stały cache). Rekomendacja: cache bust przy `ApaczkaError` z komunikatem "invalid service_id".

---

## 5. Exception mapping — 🟥 CRITICAL problem

### 5.1 `ApaczkaError(Exception)` — poza hierarchią

**Bug #A3 (CRITICAL — antypater)** — `apaczka.py:28-29`:
```python
class ApaczkaError(Exception):
    pass
```

**Problem identyczny jak w InPost** — nie dziedziczy z `ZdrovenaShippingError`, brak rozróżnienia typów błędów.

**Dodatkowo hierarchia W `shipping_exceptions.py` MA już Apaczka-specific errors**:
- `ApaczkaSignatureError(CourierAuthError)` (linia 172)
- `ApaczkaInsufficientBalanceError(CourierAuthError)` (linia 192)
- `ApaczkaServiceUnavailableError(CourierBusinessError)` (linia 230)

Ale klient ich NIE UŻYWA! `_call` (linia 53-61) rzuca tylko `ApaczkaError`.

**Fix (F-A4 CRITICAL)**: refactor `_call` na typowaną hierarchię:

```python
def _call(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
    body = _sign(self._app_id, self._secret, endpoint, data)
    try:
        resp = self._session.post(f"{_BASE}/{endpoint}/", data=body, timeout=_TIMEOUT)
    except requests.Timeout as e:
        raise CourierTimeoutError(courier="apaczka", action=endpoint) from e
    except requests.ConnectionError as e:
        raise CourierConnectionError(courier="apaczka", detail=str(e)) from e

    if not resp.ok:
        if resp.status_code >= 500:
            raise CourierServerError(courier="apaczka", status=resp.status_code)
        raise CourierBusinessError(courier="apaczka",
                                   detail=f"{resp.status_code}: {resp.text[:200]}",
                                   action=endpoint)

    result = resp.json()
    if result.get("status") != 200:
        message = result.get("message", "")
        if "signature" in message.lower():
            raise ApaczkaSignatureError(detail=message)
        if "insufficient" in message.lower() or "saldo" in message.lower():
            raise ApaczkaInsufficientBalanceError(detail=message)
        if "service" in message.lower():
            raise ApaczkaServiceUnavailableError(detail=message)
        raise CourierBusinessError(courier="apaczka", detail=message, action=endpoint)
    return result
```

**Backward compat**: `ApaczkaError = ZdrovenaShippingError` (deprecated alias).

### 5.2 Cache read `except Exception: pass`

**Bug #A4 (MEDIUM)** — `apaczka.py:75-76`:
```python
except Exception:
    pass
```

Silent fail — jeśli storage service jest niedostępny, kontynuujemy i robimy fresh API call. **W praktyce to jest OK** (fallback do świeżego call), ale:
- Nie logujemy że cache miss z powodu błędu.
- Nie mierzymy jak często storage service pada.

**Fix (F-A5)**: `except (KeyError, ValueError, json.JSONDecodeError) as exc: logger.debug("Apaczka cache miss: %s", exc)`. Dla `StorageError` osobno: `logger.warning`.

---

## 6. Logging

### 6.1 Stan obecny

- `_call` sukces: brak logu na sukces (INFO). Powinien być w `_call` (audit trail).
- `create_shipment` sukces: `logger.info("Apaczka shipment created: order_id=%s reference=%s", ...)` ✅
- `cancel_shipment` sukces: `logger.info("Apaczka shipment cancelled: order_id=%s", ...)` ✅
- Cache miss: `logger.info("Fetching Apaczka service_structure (cache miss)")` ✅
- Cache write fail: `logger.warning("Failed to cache Apaczka service_structure: %s", exc)` — ⚠️ powinno być `.exception` (TRY400 by wykryło, ale reguła jest wyłączona ANN).
- Cache read fail: **cichy** (bug #A4).

### 6.2 Rekomendacja

- Dodać `logger.info` na happy path `_call` (audit trail: endpoint + response status).
- Zamienić `logger.warning(..., exc)` w `apaczka.py:93` na `.exception()`.
- Dodać `logger.exception` w cache read fallback.

---

## 7. Lista fixów do finalnego PR (Apaczka)

**Blok A — Exception refactor (CRITICAL):**

- [ ] **F-A4**: `apaczka.py:28` — usuń `ApaczkaError(Exception)`. Refactor `_call` na typowaną hierarchię (`ApaczkaSignatureError`, `ApaczkaInsufficientBalanceError`, `ApaczkaServiceUnavailableError`, `CourierBusinessError`, `CourierServerError`, `CourierTimeoutError`, `CourierConnectionError`). Alias `ApaczkaError = ZdrovenaShippingError` z `DeprecationWarning`.

**Blok B — Cancel endpoint (feature completion):**

- [ ] **F-A3**: Dodać w `webhooks.py`:
  - `DELETE /apaczka/orders/{id}` → `client.cancel_shipment(id)` → 204 (Apaczka jest idempotent, więc bez 422)
  - `POST /shipping/drafts/{id}/execute` — guard `if draft.get("courier_draft_id"): raise 409`

**Blok C — Contract & idempotency (MEDIUM):**

- [ ] **F-A1**: Unit test `test_apaczka_sign_signature` — hardcoded fixture, chroni signature format.
- [ ] **F-A2**: `_run_apaczka` — guard przed re-POST `order_send`.
- [ ] **F-A5**: `apaczka.py:75` — explicit except types + logger.debug/warning (nie pass).

**Blok D — Cosmetics (low):**

- [ ] **F-A6**: `apaczka.py:67,86,183` — `import io`, `import base64` na top-level.
- [ ] **F-A7**: `apaczka.py:93` — `logger.warning(...)` → `.exception()` (wraz z TRY400 sweep).

**Estymacja**: ~150 linii kodu produkcyjnego + ~100 linii testów.

---

## Załączniki

**Źródła**:
- [Dokumentacja Web API ver. 2 - Apaczka](https://panel.apaczka.pl/dokumentacja_api_v2.php) (2022-08-22)
- [Apaczka Push Tracking API PDF](https://www.apaczka.pl/app/uploads/2023/06/Push-tracking-API-v2-2.pdf) (dla webhooków tracking-back — obecnie nie używamy)
- Wewnętrzna dokumentacja: `zdrovena/common/shipping_exceptions.py` (Apaczka-specific error classes 172-197)
