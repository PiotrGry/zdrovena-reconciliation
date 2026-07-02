# Audyt techniczny integracji — Executive Summary

**Data:** 2026-07-02
**Zakres:** Allegro, Fakturownia, Shopify, InPost, Apaczka
**Baseline:** `develop` @ `39ad39b` (post-PR#75)
**Autor:** Perplexity Computer

---

## 1. Metoda audytu

Dla każdej integracji sprawdzamy **8 wymiarów**:

1. **URL + method** — czy zgadza się z oficjalną dokumentacją API
2. **Request fields** — nazwy, typy, wartości enumów, wymagalność
3. **Response fields** — nazwy pól z których czytamy dane (**tu żyje bug Allegro `invoiceNumber`**)
4. **Headers** — Content-Type, Accept, Authorization
5. **Antypaterny** — `except Exception`, magic strings, brak type hints, duplikacja logiki
6. **Idempotency** — czy retry/duplicate call nie stworzy duplikatu przesyłki/faktury
7. **Exception handling** — czy używamy typowanej hierarchii `ZdrovenaShippingError` czy `raise RuntimeError`
8. **Logging** — czy każdy call ma log, czy błędy mają `exc_info=True`, czy komunikaty są czytelne

**Tryb pracy:** B (ruff `--select ALL` + pyright strict + manual review, per file).
**Format wynikowy:** C (Markdown + PDF na koniec).

---

## 2. Inwentaryzacja endpointów

### 2.1 Allegro (`zdrovena/common/allegro.py`)

Base URL: `https://api.allegro.pl` (prod) | `https://api.allegro.pl.allegrosandbox.pl` (sandbox)
Auth: OAuth 2.0 refresh-token → `Bearer <access>` (12h żywotności)
Content-Type: `application/vnd.allegro.public.v1+json`

| Metoda klienta | HTTP | Path | Grupa | Wołane w |
|---|---|---|---|---|
| `_fetch_token` | POST | `/auth/oauth/token` (Basic) | OAuth | `_get_token` |
| `list_orders` | GET | `/order/checkout-forms` | Orders | `allegro_poller.poll_orders_once` |
| `get_order` | GET | `/order/checkout-forms/{id}` | Orders | *(nieużywane)* |
| `mark_order_processed` | PUT | `/order/checkout-forms/{id}/fulfillment` | Orders | `allegro_poller` (za flagą `ALLEGRO_MARK_ON_DRAFT`) |
| `create_shipment` | POST | `/order/checkout-forms/{id}/shipments` | Tracking push | `webhooks._maybe_push_tracking_to_allegro` |
| `get_shipments` | GET | `/order/checkout-forms/{id}/shipments` | Tracking push | *(nieużywane)* |
| `list_order_invoices` | GET | `/order/checkout-forms/{id}/invoices` | Invoices | `fakturownia_patcher.patch_allegro_invoices_once` |
| `create_invoice_declaration` | POST | `/order/checkout-forms/{id}/invoices` | Invoices | *(nieużywane — placeholder do przyszłego upload)* |
| `upload_invoice_file` | PUT | `/order/checkout-forms/{id}/invoices/{invId}/file` | Invoices | *(nieużywane)* |
| `get_delivery_services` | GET | `/shipment-management/delivery-services` | Wysyłam z Allegro | *(bootstrap — nieużywane w runtime)* |
| `get_delivery_proposal` | GET | `/shipment-management/delivery-proposals/{id}` | Wysyłam z Allegro | *(nieużywane)* |
| `create_ship_with_allegro_shipment` | POST | `/shipment-management/shipments/create-commands` | Wysyłam z Allegro | `webhooks._run_allegro_delivery` |
| `get_ship_with_allegro_command_status` | GET | `/shipment-management/shipments/create-commands/{cmd}` | Wysyłam z Allegro | `wait_for_ship_with_allegro_shipment` (loop) |
| `get_ship_with_allegro_shipment` | GET | `/shipment-management/shipments/{shipId}` | Wysyłam z Allegro | `webhooks._run_allegro_delivery` |
| `get_ship_with_allegro_pickup_proposals` | POST | `/shipment-management/pickup-proposals` | Wysyłam z Allegro | `webhooks._run_allegro_delivery` (jeśli pickup_date) |
| `create_ship_with_allegro_pickup` | POST | `/shipment-management/pickups/create-commands` | Wysyłam z Allegro | `webhooks._run_allegro_delivery` |
| `get_ship_with_allegro_label` | GET | `/shipment-management/shipments/{shipId}/label` | Wysyłam z Allegro | *(nieużywane w routerze — do dodania label endpoint dla `allegro_delivery`)* |

**Endpointów: 16 (10 użytkowanych w produkcji, 6 nieużywanych).**

### 2.2 Fakturownia (`zdrovena/common/fakturownia.py`)

Base URL: `https://zdrovena.fakturownia.pl` (z env `FAKTUROWNIA_BASE_URL`)
Auth: `api_token` w query string + w body dla PUT/POST
Content-Type: `application/json` (implicit z `json=` w requests)

| Metoda klienta | HTTP | Path | Wołane w |
|---|---|---|---|
| `get_invoice` | GET | `/invoices/{id}.json` | `add_settlement_position`, `fakturownia_patcher._process_one_invoice` |
| `list_invoices` | GET | `/invoices.json?period=&page=&per_page=&number=&oid=&include_positions=` | `fakturownia_patcher._process_one_invoice` |
| `update_invoice` | PUT | `/invoices/{id}.json` | `add_settlement_position` (wewnętrznie) |
| `add_settlement_position` | (2 calls: GET + PUT) | — | `fakturownia_patcher._process_one_invoice` |

**Endpointów: 3 (GET/PUT invoice, GET list) — plus composite `add_settlement_position`.**

### 2.3 InPost (`zdrovena/common/inpost.py`)

Base URL: `https://api-shipx-pl.easypack24.net` (z env `INPOST_BASE_URL`)
Auth: `Authorization: Bearer <token>` (session-level)
Content-Type: `application/json`

| Metoda klienta | HTTP | Path | Wołane w |
|---|---|---|---|
| `create_paczkomat_shipment` | POST | `/v1/organizations/{orgId}/shipments` | `webhooks._run_inpost` (paczkomat) |
| `create_kurier_shipment` | POST | `/v1/organizations/{orgId}/shipments` | `webhooks._run_inpost` (kurier) |
| `create_dispatch_order` | POST | `/v1/organizations/{orgId}/dispatch_orders` | `_run_inpost` + `webhooks.order_pickup` |
| `cancel_shipment` | DELETE | `/v1/shipments/{shipId}` | *(nieużywane w routerze — brak endpointu DELETE)* |
| `cancel_dispatch_order` | DELETE | `/v1/organizations/{orgId}/dispatch_orders/{disId}` | *(nieużywane w routerze)* |
| `get_label` | GET | `/v1/shipments/{shipId}/label` | `webhooks.get_label` |

**Endpointów: 4 (użytkowane) + 2 (cancel — martwy kod).**

### 2.4 Apaczka (`zdrovena/common/apaczka.py`)

Base URL: `https://www.apaczka.pl/api/v2`
Auth: HMAC-SHA256 signature per-request (`app_id`, `signature`, `expires`)
Content-Type: `application/x-www-form-urlencoded` (implicit z `data=` w requests)

| Metoda klienta | HTTP | Endpoint | Wołane w |
|---|---|---|---|
| `_get_service_structure` | POST | `service_structure` | `create_shipment` (przez cache 23h) |
| `create_shipment` | POST | `order_send` | `webhooks._run_apaczka` |
| `cancel_shipment` | POST | `order_cancel` | *(nieużywane w routerze)* |
| `get_label` | POST | `waybill` | `webhooks.get_label` |

**Endpointów: 3 (użytkowane) + 1 (cancel — martwy kod).**

### 2.5 Shopify (webhook przychodzący, nie klient)

| HTTP | Path | Handler | Idempotency |
|---|---|---|---|
| POST | `/webhooks/shopify/order-created` | `shopify_order_created` | Dedup po `shopify_order_id` w `shipping_store` + HMAC-SHA256 verify |

Uwaga: Shopify jest **konsumentem** (odbieramy webhook, nie wołamy API), więc audyt endpointów sprowadza się do jednego callback URL.

### 2.6 Router `webhooks.py` — publiczne endpointy naszego API

| HTTP | Path | Handler | Auth role |
|---|---|---|---|
| POST | `/webhooks/shopify/order-created` | `shopify_order_created` | HMAC only |
| GET  | `/shipping/drafts` | `list_drafts` | viewer+ |
| POST | `/shipping/drafts/{id}/execute` | `execute_draft` | shipment_mgr+ |
| POST | `/shipping/drafts/{id}/pickup` | `order_pickup` | shipment_mgr+ |
| PATCH | `/shipping/drafts/{id}` | `update_draft` | shipment_mgr+ |
| GET | `/shipping/drafts/{id}/label` | `get_label` | viewer+ |

⚠️ **Rozjazd docstring vs kod:** docstring modułu `webhooks.py` (linie 1-11) reklamuje 2 endpointy DELETE (`/shipment`, `/dispatch`), które **nie istnieją** w kodzie. Do usunięcia z docstringu albo do zaimplementowania — decyzja użytkownika.

---

## 3. Top-10 najpilniejszych obserwacji (wstępne, przed pełnym audytem)

| # | Priorytet | Miejsce | Problem | Skutek |
|---|---|---|---|---|
| 1 | 🔴 CRITICAL | `fakturownia_patcher.py:119` | `allegro_inv.get("number")` — Allegro API zwraca pole `invoiceNumber` (docs) | Patcher **NIGDY** nie dopisze kaucji na produkcji |
| 2 | 🔴 CRITICAL | `webhooks.py` docstring l.1-11 | Reklamuje endpointy DELETE które nie istnieją | Mylące — konsumenci API myślą że można anulować |
| 3 | 🟠 HIGH | `allegro_poller.py:56, 68, 87` | Trzy `except Exception` bez `exc_info=True` | Debug niemożliwy — nie widać stacktrace ani typu wyjątku |
| 4 | 🟠 HIGH | `fakturownia_patcher.py:100, 113, 132` | `except Exception` łyka wszystko, w tym `KeyboardInterrupt`, `SystemExit` (przez `try/except Exception`, ale wciąż połyka `FakturowniaAuthError` bez odróżnienia) | Auth error traktowany jak business error → alert nie leci |
| 5 | 🟠 HIGH | `webhooks._maybe_push_tracking_to_allegro:187` | `except Exception as exc` łyka wszystko | Auth Allegro fail → tylko warning, nikt się nie dowie |
| 6 | 🟡 MEDIUM | `inpost.py:107` | `class InPostError(Exception)` — własna hierarchia zamiast `CourierBusinessError`/`CourierServerError` | Router nie odróżni 401 od 5xx od 422 — wszystko idzie do jednego `except` |
| 7 | 🟡 MEDIUM | `apaczka.py:28` | Analogicznie: `class ApaczkaError(Exception)` — nie dziedziczy z `ZdrovenaShippingError` | Utrudnia unified handling |
| 8 | 🟡 MEDIUM | `webhooks.py:869` | `except Exception as exc:` w `execute_draft` łapie **wszystko**, w tym `HTTPException` | Skutek niegroźny (i tak lecimy w 502) ale ukrywa root cause |
| 9 | 🟡 MEDIUM | `allegro.py:229-236` (`mark_order_processed`) | Zawsze status=`PROCESSING` — brak weryfikacji czy zamówienie już nie jest w tym stanie (idempotent, ale marnujemy call) | Rate-limit ryzyko przy retry burst |
| 10 | 🟡 MEDIUM | `webhooks.py:41` | `_MOCK_COURIER` czytane raz na import — nie da się zmienić bez restartu | Utrudnia testy manualne |

**UWAGA:** to lista wstępna z 2h przeglądu. Pełny audyt (fazy 2-6 per integracja) może wygenerować więcej znalezisk.

---

## 4. Plan raportów per integracja

```
docs/audit/
├── 00-summary.md          ← ten plik
├── 01-allegro.md          ← pełny audyt Allegro (8 wymiarów)
├── 02-fakturownia.md
├── 03-inpost.md
├── 04-shopify.md
├── 05-apaczka.md
├── 06-cross-cutting.md    ← wspólne antypaterny, hierarchia wyjątków, standard logów
└── fixtures/              ← realne JSON z docs dla contract tests
```

Każdy plik `0X-<integracja>.md` będzie miał sekcje:
1. **Endpoint contract table** (URL/method/req/resp/headers)
2. **Antypaterny** (z linkami do linii kodu)
3. **Idempotency** (per operacja: safe/unsafe + rekomendacja)
4. **Exception handling** (mapa: który wyjątek → jaki HTTP status → jaka reakcja operatora)
5. **Logging** (co logujemy, na jakim poziomie, czy z `exc_info`)
6. **Fix list** (bugi do naprawy jako osobne PR-y — jeden PR per bug)

---

## 5. Konwencje raportu

- **🔴 CRITICAL** — bug produkcyjny, fix w tym audycie
- **🟠 HIGH** — poważny antypattern, fix zaraz po CRITICAL
- **🟡 MEDIUM** — do refaktoru w osobnym cyklu
- **🟢 LOW** — cosmetic / nice-to-have

Każde znalezisko musi mieć:
- **Lokalizacja:** `plik.py:linia`
- **Opis problemu**
- **Skutek** (co się popsuje na prod)
- **Rekomendacja** (konkretna zmiana)
- **Estymata** (S / M / L)
