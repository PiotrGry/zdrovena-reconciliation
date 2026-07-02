# Audyt techniczny — Shopify

**Zakres**: `zdrovena/api/routers/webhooks.py` — endpoint `POST /webhooks/shopify/order-created` (linie 737-800) + helpery (`_verify_shopify_hmac`, `_is_duplicate_webhook`).

**Metodologia**: kontraktowy audyt względem [Shopify webhook docs (Verify webhook deliveries)](https://shopify.dev/docs/apps/build/webhooks/subscribe/https) + `ruff strict` + manual review.

**Wynik pyright**: ✅ 0 errors.
**Wynik ruff strict**: 12+ znalezisk w webhooks.py (całość pliku, tylko podzbiór dot. Shopify).

---

## 1. Kontraktowe niezgodności z Shopify Webhook API

### 1.1 🟥 CRITICAL — `X-Shopify-Webhook-Id` czytany ale nie persystowany

**Bug #S1 (CRITICAL)** — `webhooks.py:752` czyta `X-Shopify-Webhook-Id`, ale `_is_duplicate_webhook` (linia 719-734) go **NIE używa** do deduplication. Zamiast tego porównuje `shopify_order_id` z listy draftów.

**Dokumentacja Shopify wymaga** ([source](https://shopify.dev/docs/apps/build/webhooks/subscribe/https)):
> "If your processing isn't idempotent, use the `X-Shopify-Webhook-Id` header to detect and skip duplicates."
> 1. Extract `X-Shopify-Webhook-Id` from headers.
> 2. Check your persistent store for that ID.
> 3. If it exists, skip. If new, process AND save the ID.

**Problem produkcyjny**: order może mieć wiele webhooków (order-created + order-updated + order-fulfilled). Sprawdzanie tylko `order_id` **zablokuje** legalne re-webhooki dla update'ów.

**Fix (F-S1)**: dodać nową tabelę/entity `shopify_webhook_delivery_id` (tylko webhook_id + processed_at). W `_is_duplicate_webhook`:
```python
def _is_duplicate_webhook(webhook_id: str, shipping_store: ShippingStore) -> bool:
    if not webhook_id:
        return False
    return shipping_store.webhook_delivery_exists(webhook_id)
```

Po sukcesie procesowania: `shipping_store.record_webhook_delivery(webhook_id)`.

### 1.2 🟥 CRITICAL — `ALLOW_UNSIGNED_SHOPIFY_WEBHOOKS` bypass

**Bug #S2 (CRITICAL)** — `webhooks.py:766-782`. Jeśli `shopify_webhook_secret` nie jest ustawiony i env var `ALLOW_UNSIGNED_SHOPIFY_WEBHOOKS=1`, endpoint **akceptuje niesignowane webhooki**. Shopify docs jednoznacznie mówią:
> "Always verify HMAC before trusting payload contents."

**Ryzyko**: atakujący z URL-em endpointu może wstrzykiwać dowolne "zamówienia" → utworzą się drafty → zostaną wysłane paczki na obcy adres.

**Fix (F-S2)**:
- Usunąć `ALLOW_UNSIGNED_SHOPIFY_WEBHOOKS` całkowicie (**preferowane**).
- Alternatywa jeśli potrzebne do testów: zostawić flagę ALE tylko w `DEV`/`STAGING` env, w PRODUCTION forced-fail. Można egzekwować: `if os.getenv("APP_ENV") == "prod" and allow_unsigned: raise RuntimeError`.

### 1.3 🟡 MEDIUM — Race condition w idempotency check

**Bug #S3 (MEDIUM)** — Duplicate check odbywa się przed `background_tasks.add_task(_create_draft, ...)`. Dwa równoczesne webhooki (retry) mogą OBIE minąć check zanim pierwszy zapisze draft, tworząc 2 drafty.

**Fix (F-S3)**: przenieść dedup check do `_create_draft` na poziomie storage (INSERT ... IF NOT EXISTS na kluczu `shopify_order_id`), lub użyć locka/leasingu (Azure Table blob lease).

### 1.4 🟡 MEDIUM — Silent exception w `_is_duplicate_webhook`

**Bug #S4 (MEDIUM)** — `webhooks.py:732-733`:
```python
except Exception:
    pass
```

Jeśli `shipping_store.list_drafts()` rzuci, uznajemy że NIE jest duplikatem → tworzymy draft. Może zadziałać nawet gdy storage jest niedostępny → **duplicat draftu przy każdym retry**.

**Fix (F-S4)**: log exception (nie połknij) + fail-safe: `raise HTTPException(503)` (Shopify wtedy retry-uje).

### 1.5 🟡 MEDIUM — O(N) na każdy webhook

**Bug #S5 (MEDIUM)** — `_is_duplicate_webhook` iteruje po **wszystkich draftach** (`shipping_store.list_drafts()`). Przy 10 000 draftów: każdy webhook = 10k rows scan.

**Fix (F-S5)**: dodać indeks/query `get_draft_by_shopify_order_id(order_id)` w `ShippingStore` (Azure Table row key = order_id ⇒ O(1) lookup). Alternatywnie odejść od tego dedupu i użyć webhook_id (Fix F-S1).

### 1.6 🟡 MEDIUM — 5-sekundowy timeout Shopify

**Kontekst**: Shopify wymaga odpowiedzi w 5 sekund ([source](https://shopify.dev/docs/apps/build/webhooks/subscribe/https)):
> "Shopify has a one-second connection timeout and a five-second timeout for the entire request."

**Nasz kod**: używa `background_tasks.add_task(_create_draft, ...)` (linia 798), więc endpoint zwraca 200 przed wykonaniem heavy work. ✅ **Dobra praktyka.**

**ALE**: `_is_duplicate_webhook` na 10k draftów może samo w sobie zająć >5s (Azure Table full scan). Fix F-S5 to rozwiązuje.

### 1.7 🟢 LOW — Brak walidacji `X-Shopify-Topic`, `X-Shopify-Shop-Domain`

**Bug #S6 (LOW)** — endpoint akceptuje HMAC-poprawny payload z DOWOLNEGO tematu / shop. Sensowne jest weryfikować:
- `X-Shopify-Topic == "orders/create"` (lub `orders/paid`)
- `X-Shopify-Shop-Domain == "zdrovena.myshopify.com"` (whitelist)

Bez tego: jeśli Shopify (przez pomyłkę konfiguracji) wyśle webhook z topic "products/create" — wciąż przejdzie HMAC (bo sekret ten sam) i utworzy draft na base ProductJSON → crash.

**Fix (F-S6)**: dodać whitelist headers po HMAC verify.

---

## 2. Endpoint — inwentaryzacja

| #   | Method | Path                              | Idempotent? | HMAC? | Retry-friendly?                     |
| --- | ------ | --------------------------------- | ----------- | ----- | ----------------------------------- |
| 1   | POST   | `/webhooks/shopify/order-created` | Częściowo (order_id-based) | ✅   | ✅ zwraca 200 przez background task |

**Zero endpointów wychodzących do Shopify API** — jesteśmy tylko odbiorcą webhooków (no outbound calls). To znacznie upraszcza audyt.

**Skala**: Shopify wysyła webhook per order. Retry policy: 8× w ciągu 4h przy błędzie/timeoucie. Po 8 consecutive failures subscription się kasuje.

---

## 3. Antypaterny — ruff strict (Shopify sekcja)

**W obrębie sekcji Shopify (webhooks.py:49-57, 719-800)**:

| Reguła    | Ile | Linie                       | Severity  | Opis                                                                                        |
| --------- | --- | --------------------------- | --------- | ------------------------------------------------------------------------------------------- |
| BLE001    | 1   | 732                         | 🟥 high   | `except Exception: pass` w `_is_duplicate_webhook` → Bug #S4 (silent fail).                 |
| TRY400    | 1   | 870                         | 🟥 high   | `logger.error(..., exc)` w `execute_draft` (nie tylko Shopify, ale w tym samym pliku).       |
| PLC0415   | wiele | wiele               | 🟢 low    | Imports w środku funkcji (np. `import os` w `_get_allegro_client`, `import base64`).       |
| FAST002   | wiele | wiele               | 🟢 low    | FastAPI: use `Annotated[X, Depends(...)]` style zamiast `x: X = Depends(...)`. Częściowo już zrobione. |
| ARG001    | 9   | różne         | 🟢 low    | Unused function argument (najczęściej `principal` — jest tylko dla auth guard). Whitelistować. |

**Ocena**: sekcja Shopify sama w sobie NIE ma poważnych antypaternów kodowych — bugi są kontraktowe (§1).

---

## 4. Idempotency

### 4.1 Obecny stan

| Poziom                     | Mechanizm                                                    | Status |
| -------------------------- | ------------------------------------------------------------ | ------ |
| HTTP endpoint              | Zwraca 200 zawsze (przez background_task)                   | ✅     |
| Idempotency key            | Sprawdza `shopify_order_id` w liście draftów                | 🟡 O(N), nie używa `X-Shopify-Webhook-Id` |
| Race protection            | Brak (fire-and-forget background task)                       | 🟥 duplikaty możliwe |
| Success acknowledgment     | Zwraca `{"status": "accepted"}` przed dokończeniem work      | ✅ dobra praktyka dla 5s timeout |

### 4.2 Zalecany model docelowy (po fixach)

```
POST /webhooks/shopify/order-created:
    raw_body = await request.body()
    webhook_id = headers["X-Shopify-Webhook-Id"]
    topic = headers["X-Shopify-Topic"]
    shop = headers["X-Shopify-Shop-Domain"]

    # 1. Verify HMAC (fail 401 if bad)
    # 2. Verify topic + shop in whitelist (fail 400 if bad)
    # 3. Check webhook_id in dedup table (return 200 if seen)
    # 4. INSERT webhook_id in dedup table (with race protection — UPSERT NOT EXISTS)
    # 5. If insert conflict: return 200 (duplicate)
    # 6. Otherwise: background_tasks.add_task(_create_draft, ...)
    # 7. Return 200
```

---

## 5. Exception mapping — audyt

### 5.1 Sekcja Shopify

| Miejsce                             | Wyjątek                                    | Response HTTP                | Ocena |
| ----------------------------------- | ------------------------------------------ | ---------------------------- | ----- |
| Missing HMAC header                 | `HTTPException(401)`                       | 401 Unauthorized             | ✅    |
| Invalid HMAC signature              | `HTTPException(401)`                       | 401 Unauthorized             | ✅    |
| Missing webhook secret (bypass)     | `HTTPException(503)` OR skip validation    | 503 lub skip                 | 🟥 bypass to bug (Fix F-S2) |
| Invalid JSON body                   | `HTTPException(400)`                       | 400 Bad Request              | ✅    |
| Missing shipping_lines              | Return `{"status": "skipped"}`             | 200                          | ✅    |
| Duplicate order                     | Return `{"status": "duplicate"}`           | 200                          | ✅    |
| `list_drafts` fails                 | `except Exception: pass`                   | Kontynuuje jakby nie było duplikatu | 🟥 Fix F-S4 |
| Background `_create_draft` fails    | ??? (poza scope endpointu)                  | Zawsze 200                   | 🟡 potrzebna obserwowalność |

### 5.2 Rekomendacja: obserwowalność background_task

`_create_draft` jest fire-and-forget. Jeśli rzuci, użytkownik dostaje 200 ale draft się nie utworzy. **Trzeba** dodać:
- Metric counter `shopify_webhook_processed{status="success|error"}` (Prometheus/App Insights).
- Alert w monitoring: `rate(shopify_webhook_processed{status="error"}[5m]) > 0`.

---

## 6. Logging

### 6.1 Obecny stan

Endpoint loguje:
- Missing HMAC → `logger.warning`
- HMAC mismatch → `logger.warning`
- Unsigned bypass → `logger.warning`
- Empty shipping_lines → `logger.warning`
- Duplicate → `logger.info` (via `_is_duplicate_webhook`)
- Draft queued → `logger.info`

**Ocena**: dobrze pokryte. Rekomendacja: dodać w każdym logu `X-Shopify-Webhook-Id` + `X-Shopify-Topic` + `X-Shopify-Shop-Domain` (correlation ID).

### 6.2 Alarmy do dodania

- **HIGH**: `HMAC mismatch` >0 w 5 min → **security alert** (ktoś próbuje forge webhooków).
- **MEDIUM**: `webhook secret not configured` w prod → alert do inżyniera on-call.
- **LOW**: `Empty shipping_lines` >5% webhooków → alert do Ops (bad Shopify config).

---

## 7. Lista fixów do finalnego PR (Shopify)

**Blok A — Security (CRITICAL, blok w PR):**

- [ ] **F-S1**: Nowa tabela `webhook_deliveries(webhook_id PK, order_id, topic, received_at)`. Refactor `_is_duplicate_webhook(webhook_id, ...)` — dedup po X-Shopify-Webhook-Id.
- [ ] **F-S2**: Usunąć `ALLOW_UNSIGNED_SHOPIFY_WEBHOOKS` z prod. W DEV/STAGING: OK, w PROD forced 503.
- [ ] **F-S6**: Whitelist `X-Shopify-Topic` (`orders/create`) i `X-Shopify-Shop-Domain` (zdrovena.myshopify.com).

**Blok B — Race & performance (MEDIUM):**

- [ ] **F-S3**: Storage-level INSERT ... IF NOT EXISTS w `_create_draft` (Azure Table `if_none_match="*"`).
- [ ] **F-S4**: `_is_duplicate_webhook` — usuń `except Exception: pass`, log `.exception`, fail-safe raise 503.
- [ ] **F-S5**: `ShippingStore.get_draft_by_shopify_order_id(order_id)` — indeksowany lookup, nie full scan.

**Blok C — Observability (nice-to-have):**

- [ ] **F-S7**: Metric `shopify_webhook_processed{status}` + Prometheus/App Insights.
- [ ] **F-S8**: Correlation ID (`X-Shopify-Webhook-Id`) w każdym logu.

**Estymacja**: ~150 linii kodu produkcyjnego + ~200 linii testów (nowy `test_shopify_webhook_dedup.py`).

---

## Załączniki

**Źródła**:
- [Shopify — Verify webhook deliveries](https://shopify.dev/docs/apps/build/webhooks/subscribe/https)
- [Shopify — Webhook headers reference](https://shopify.dev/docs/apps/build/webhooks/subscribe/https#headers)
