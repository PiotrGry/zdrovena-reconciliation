# Zdrovena — integracja Shopify → InPost/Apaczka

**Projekt:** zdrovena-reconciliation v2.6.0
**Repo:** https://github.com/PiotrGry/zdrovena-reconciliation
**Branch:** `develop`
**Data:** 2026-06-30
**Status:** notatka robocza — ustalenia przed refaktorem kodu

---

## 1. Kontekst i cel

Integracja sklepu Shopify z dwoma kurierami: **InPost** (paczkomaty) i **Apaczka** (DPD, Orlen, Poczta Polska — kurier do drzwi). Cel końcowy: zamówienie w Shopify → webhook → utworzenie wysyłki u kuriera → zamówienie podjazdu, z możliwością indywidualnego anulowania każdej operacji.

Stack: FastAPI + React/Vite + Azure Container Apps + Terraform + Entra ID.

---

## 2. Środowisko developerskie (gotowe)

- **Sklep dev:** `humio-b2b-2.myshopify.com` (organizacja GrizzlyByte, zarządzany przez Cloudflare).
- **Custom App:** `zdrovena-reconciliation-dev`, status: Zainstalowane.
- **Scope'y:** `read_customers`, `read_orders`.
- **API version:** `2026-04`.
- **Webhooki:** konfigurowane w `Ustawienia → Powiadomienia → Webhooki` (Shopify zmienił UI w 2026, nie ma już tego w aplikacji).
- **Tunel:** `cloudflared tunnel --url http://localhost:8000` — daje publiczny URL dla webhooka. Cloudflared zainstalowany z `.deb` z GitHub releases (apt z repo Ubuntu nie działał).
- **Decyzja:** realny flow z dev store, bez symulacji lokalnej.

---

## 3. Decyzja architektoniczna — osobne wysyłki i podjazdy

**Wybrana strategia:** każda wysyłka i każdy podjazd osobno, bez batchowania w warstwie shipmentu.

**Powód:** możliwość indywidualnego anulowania, gdy coś się popsuje.

**Implikacje:**
- Osobny `dispatch_order_id` per wysyłka (InPost).
- Osobny `order_id` per zamówienie w Apaczce.
- Konieczność trzymania tych ID w naszym draft schema — bez nich nie ma po czym anulować.

**Wyjątek do rozważenia później:** batchowanie podjazdów InPost (sama wysyłka zostaje 1:1) — szczegóły w sekcji 12.

---

## 4. Wymiary paczek vs skrytki

Wszystkie typy paczek wchodzą do gabarytów wszystkich kurierów — działa od ponad roku w produkcji, nie podlega dyskusji.

| Paczka | wys × szer × dł | Waga | Mieści się w |
|---|---|---|---|
| Duża 3×12 szt. | 40×40×20 | 18kg | InPost C, Orlen L, DPD L |
| Średnie 2×12 szt. | 40×30×20 | 12kg | InPost C, Orlen L, DPD L |
| Szkło | 30×30×20 | 9kg | InPost B/C, Orlen M/L, DPD L |
| Zgrzewka 12 szt. | 30×20×20 | 6kg | InPost B/C, Orlen M/L, DPD L |
| Połówka 6 szt. | 20×15×20 | 3kg | wszystkie |

**Skrytki referencyjne (2026):**
- **InPost A/B/C:** 8/19/41 × 38 × 64 cm, 25kg.
- **Orlen S/M/L:** 8/19/41 × 38 × 60 cm; S=5kg indyw./20kg biz, M/L=20kg.
- **DPD Pickup automat L:** 50×44×59, 20kg.
- **DPD Pickup punkt Żabka:** 64×41×38, 20kg.

---

## 5. Schemat danych — Shopify (input)

W webhooku `orders/create` interesujące pola:

- `shipping_address` — imię, nazwisko, `address1`, `city`, `zip`, `phone`, `email`.
- `shipping_lines[0].title` — **kluczowe pole**, zawiera wybór kuriera i punkt odbioru.
- `total_weight` — w gramach (dzielić przez 1000).
- `line_items` — do mapowania na typ paczki.

**Wzorce `shipping_lines[0].title` z produkcji:**

| Wzorzec | Kurier | Punkt odbioru |
|---|---|---|
| `InPost • Paczkomat 24/7 • 2.51 km • RUH02M` | InPost paczkomat | ID po ostatnim `•` (`RUH02M`) |
| `Kurier - dostawa pod drzwi` | Apaczka kurier | brak (dostawa pod drzwi) |
| `DPD • DPD Pickup- "Lidl" • 0.19 km • PL5A362` | Apaczka via DPD | `PL5A362` |
| `Poczta Polska • Sklep Żabka • 0.16 km • 367682` | Apaczka via Poczta | `367682` |

---

## 6. Schemat danych — InPost ShipX (output)

**Endpointy:**

| Operacja | Metoda | Endpoint |
|---|---|---|
| Utwórz wysyłkę | POST | `/v1/organizations/{org_id}/shipments` |
| Zamów podjazd | POST | `/v1/organizations/{org_id}/dispatch_orders` |
| Anuluj wysyłkę | DELETE | `/v1/shipments/{id}` (tylko status `created` / `confirmed`) |
| Anuluj podjazd | DELETE | `/v1/organizations/{org_id}/dispatch_orders/{id}` (tylko przed przyjęciem przez kuriera) |

**Payload zawiera:**
- Receiver — imię, telefon, email. Dla paczkomatu wystarczy `target_point`, dla kuriera pełny adres.
- Parcels — `template` (small/medium/large) lub własne wymiary + waga.
- Sender — z sekretów.
- Service — `inpost_locker_standard` lub `inpost_courier_standard`.

**Dispatch order** to osobny request — referuje shipments po ID, dodaje adres odbioru i okno czasowe.

**Autoryzacja:** `Authorization: Bearer {token}` (JWT z panelu).

---

## 7. Schemat danych — Apaczka (output)

**Endpointy:**

| Operacja | Metoda | Endpoint |
|---|---|---|
| Wysyłka + podjazd (razem) | POST | `/order_send/` |
| Anulowanie | POST | `/order_cancel/` |
| Godziny podjazdu | POST | `/order_pickup_hours/` (nieużywane jeszcze) |

**Różnice względem InPostu:**
- Apaczka łączy nadanie z podjazdem w jednym requeście (`option.pickup_*`).
- Autoryzacja: każdy request podpisany HMAC-SHA256 z `app_secret`, plus `app_id` w body. **Nie ma Bearer tokena.**
- **Nie ma sandboxa** — używa się drugiego konta produkcyjnego (osobna firma w panelu) bez salda do testów.
- `service_id` zależny od kuriera — inny dla DPD, inny dla Orlen, inny dla Poczty.

---

## 8. Sekrety potrzebne w `.env.local`

**InPost** (panel `sandbox-manager.paczkomaty.pl → Settings → API`):
- `inpost_api_token` — JWT.
- `inpost_organization_id` — z URL panelu `/manage/organization/{id}`.
- Base URL sandbox: `https://sandbox-api-shipx-pl.easypack24.net`.
- Base URL prod: `https://api-shipx-pl.easypack24.net`.

**Apaczka** (panel `Ustawienia → Web API`):
- `apaczka_app_id`.
- `apaczka_app_secret` (klucz HMAC).
- `apaczka_service_id` — pobierany z endpointu `service_structure`, osobny per kurier.

**Nadawca** (już w kodzie w `_get_sender()`):
- `sender_name`, `sender_street`, `sender_city`, `sender_post_code`, `sender_phone`, `sender_email`.
- **Do dodania jako osobny sekret:** `sender_building_number` (obecnie zahardcodowane `"1"` — bug).

---

## 9. Bugi w kodzie do naprawienia

Zidentyfikowane w `webhooks.py`, `inpost.py`, `apaczka.py`:

1. **`_pick_courier` (webhooks.py:100-106)** — łapie `"Kurier - dostawa pod drzwi"` jako InPost. Powinien sprawdzać tylko `"inpost"` lub `"paczkomat"` w tytule, reszta = Apaczka.
2. **Brak parsera `target_point`** z `shipping_lines[0].title` — trzeba wyciągnąć ostatni segment po `•`.
3. **`building_number="1"` zahardcodowane** w `_run_inpost` (webhooks.py:163) i `_get_sender` (linia 63) — adres odbiorcy z `address1` trzeba parsować na ulicę + numer.
4. **Paczkomat zawsze `template: "small"`** (gabaryt A) niezależnie od rzeczywistego rozmiaru — trzeba mapować z `line_items`.
5. **Kurier zahardcodowany `weight_kg=1.0` i `dimensions={30,20,15}`** — ignoruje `order.total_weight` i typ paczki.
6. **BLOKER: `dispatch_order_id` nie jest zapisywany w draft** (webhooks.py:185 zwraca tylko boolean) — bez tego ID nie ma jak cofnąć podjazdu.
7. **Brak endpointów cancel** — nie ma `cancel_shipment`, `cancel_dispatch`, `cancel_pickup`.

---

## 10. Mapowanie danych do uzupełnienia w `webhooks.py`

- Parser `shipping_lines[0].title` → kurier + `target_point`.
- Parser `address1` → `street` + `building_number`.
- `order.total_weight / 1000` → waga paczki w kg.
- Normalizacja `phone` do `+48XXXXXXXXX`.
- Mapowanie `line_items` → typ paczki → wymiary + template.
- Zapis `dispatch_order_id` (InPost) i `apaczka_order_id` (Apaczka) do draft schema.
- Nowe endpointy: `/cancel`, `/cancel-pickup`.

---

## 11. Obsługa wyjątków — czego brakuje

Obecny kod jest napisany w trybie "happy path". To największe ryzyko produkcyjne zaraz po brakujących cancel endpointach.

### 11.1 Walidacja danych z Shopify

- `MissingShippingAddressError` — `shipping_address` jest `None`.
- `UnparseableShippingLineError` — pusta lista lub nieznany wzorzec `title`.
- `UnknownCarrierError` — nowy kurier, którego nie mamy w mapowaniu.
- `InvalidLockerIdError` — segment po `•` nie pasuje do regex paczkomatu.
- `InvalidPhoneNumberError` — telefon nie da się znormalizować do `+48XXXXXXXXX`.
- `InvalidPostCodeError` — `zip` nie w formacie `XX-XXX`.
- `UnparseableAddressError` — `address1` bez numeru, albo z mieszkaniem (`123/45`).
- `WeightOutOfRangeError` — `total_weight = 0` lub > 25kg.
- `PackageTypeUnknownError` — SKU spoza mapowania.

### 11.2 Błędy autoryzacji kurierów

- `InPostAuthError` — 401 z ShipX. Token wygasł lub zły `organization_id`.
- `ApaczkaSignatureError` — zły HMAC, zła kolejność pól, zły timestamp.
- `ApaczkaInsufficientBalanceError` — brak środków na koncie testowym.

### 11.3 Błędy biznesowe kuriera (status 4xx)

- `InPostLockerUnavailableError` — paczkomat pełny / wyłączony. 422 z `validation.target_point.unavailable`.
- `InPostInvalidServiceError` — usługa nie pasuje do gabarytu. 422 z `validation.parcels.dimensions`.
- `ApaczkaServiceUnavailableError` — awaria danego kuriera u Apaczki.
- `PickupSlotUnavailableError` — wszystkie sloty na dziś niedostępne (po cut-off).
- `AddressGeocodingError` — Apaczka nie potrafi zgeolokalizować adresu.

### 11.4 Błędy sieciowe

- `CourierTimeoutError` — przekroczony timeout (sugestia: 10s shipment, 15s dispatch). `httpx` domyślnie czeka wiecznie.
- `CourierConnectionError` — DNS, TLS, sieć. Retry z exponential backoff (3 próby, 1s/2s/4s).
- `CourierServerError` — 5xx z kuriera. Też retry, ale ostrożnie — niektóre 5xx oznaczają, że shipment się utworzył, a my dostaliśmy 503 przy odpowiedzi (stąd idempotency).

### 11.5 Idempotencja — ukryty bug

Bez tego duplikujemy shipmenty przy retry webhooka Shopify (5s timeout → retry).

Brakujące mechanizmy:
- **Tabela `processed_webhooks`** z `shopify_webhook_id` (header `X-Shopify-Webhook-Id`) jako unikalny klucz.
- **`X-Shopify-Hmac-Sha256` verification** — bez tego ktoś z publicznym tunelem może podszyć się pod Shopify. **Krytyczne.**
- **Idempotency key w requestach do kuriera** — InPost akceptuje `X-Idempotency-Key`. Apaczka — `external_id` w body + sprawdzanie przed wysłaniem.

### 11.6 Błędy podczas anulowania

- `ShipmentAlreadyDispatchedError` — wysyłka już odebrana przez kuriera. DELETE zwraca 422.
- `DispatchAlreadyAcceptedError` — podjazd potwierdzony przez kuriera, API nie pomoże, trzeba dzwonić.
- `MissingDispatchIdError` — brak `dispatch_order_id` w draft (obecny bloker).

### 11.7 Hierarchia wyjątków — propozycja

```
ZdrovenaShippingError (bazowy)
├── ShopifyPayloadError (4xx walidacji, nie retry)
│   ├── MissingShippingAddressError
│   ├── UnparseableShippingLineError
│   ├── UnknownCarrierError
│   ├── InvalidLockerIdError
│   ├── InvalidPhoneNumberError
│   ├── InvalidPostCodeError
│   ├── UnparseableAddressError
│   ├── WeightOutOfRangeError
│   └── PackageTypeUnknownError
├── CourierAuthError (krytyczny, alert do admina, nie retry)
│   ├── InPostAuthError
│   ├── ApaczkaSignatureError
│   └── ApaczkaInsufficientBalanceError
├── CourierBusinessError (4xx, nie retry, alert do operatora)
│   ├── InPostLockerUnavailableError
│   ├── InPostInvalidServiceError
│   ├── ApaczkaServiceUnavailableError
│   ├── PickupSlotUnavailableError
│   └── AddressGeocodingError
├── CourierTransientError (5xx/sieć, retry z backoff)
│   ├── CourierTimeoutError
│   ├── CourierConnectionError
│   └── CourierServerError
└── CancellationError
    ├── ShipmentAlreadyDispatchedError
    ├── DispatchAlreadyAcceptedError
    └── MissingDispatchIdError
```

Każdy wyjątek niesie metadane `{order_id, shopify_webhook_id, courier, action, payload_snippet}` — żeby z logów dało się zrekonstruować zdarzenie bez wchodzenia do panelu Shopify.

### 11.8 Strategia reakcji w webhook handlerze

| Wyjątek | Odpowiedź do Shopify | Status draftu | Alert |
|---|---|---|---|
| `ShopifyPayloadError` | 200 OK (retry nic nie da) | `validation_failed` | do operatora |
| `CourierAuthError` | 500 (Shopify spróbuje ponownie, do 19× w 48h) | `auth_failed` | krytyczny do admina |
| `CourierBusinessError` | 200 OK | `needs_operator_action` | do operatora z powodem |
| `CourierTransientError` | retry 3× w requeście, potem 500 | `transient_error` | — |
| `CancellationError` | — (tylko z API operatora) | — | — |

---

## 12. Batchowanie — co warto, czego nie

**Decyzja podtrzymana:** shipmenty 1:1 z zamówieniami (cancel per shipment).

### 12.1 Co boli bez batchowania

Realistyczny dzień: 50 zamówień.
- 50× `POST /shipments` do InPost ≈ 40s blokady webhook handlera (bez async).
- 50× `POST /dispatch_orders` = kolejne 40s + 50 osobnych okien czasowych dla kuriera.
- Dispatch w InPost jest darmowy, ale generuje hałas w panelu InPost — kurier może być zdezorientowany.
- Apaczka — każdy `order_send` z `pickup_*` to potencjalnie osobny telefon do dyspozytora kuriera.

### 12.2 Gdzie batch ma sens — podjazd InPost

- ShipX wspiera dispatch z listą shipment IDs: `shipments: [id1, id2, id3, ...]`.
- Jeden dispatch = jedno okno czasowe = kurier przyjedzie raz.
- Cancel pojedynczej wysyłki dalej działa (`DELETE /shipments/{id}`), trzeba zweryfikować w sandboxie czy InPost wymaga `PATCH /dispatch_orders/{id}` z nową listą.
- Cancel całego batcha (`DELETE /dispatch_orders/{id}`) — shipments zostają z statusem `created`, operator musi zamówić nowy podjazd.

### 12.3 Apaczka — batch jest ryzykowny

- Brak endpointu "dispatch dla listy zamówień".
- Workaround "jeden `order_send` z `pickup=true`, reszta `pickup=false`" — **niebezpieczny**: cancel pierwszego = brak podjazdu dla pozostałych.
- **Wniosek:** zostać przy 1:1 dopóki Apaczka nie wprowadzi dedykowanego endpointu.

### 12.4 Model danych — batch dispatch (propozycja)

Nowa tabela:

```
dispatch_batches
├── id
├── courier ('inpost' | 'apaczka')
├── inpost_dispatch_order_id (nullable)
├── scheduled_date
├── time_window_start
├── time_window_end
├── status ('draft' | 'submitted' | 'accepted' | 'cancelled')
├── created_at
└── submitted_at
```

W tabeli shipments: nullable FK `dispatch_batch_id`.

### 12.5 Dwa tryby pracy

**Tryb webhook (real-time, dla wysyłek):**
- Webhook → walidacja → utwórz shipment w InPost → zapisz `inpost_shipment_id`.
- **NIE** twórz dispatch order natychmiast. `dispatch_batch_id = NULL`.

**Tryb batch (cron / przycisk operatora, dla podjazdów):**
- Co X godzin (np. 10:00, 14:00, 17:00) albo na żądanie.
- Pobierz shipments z `dispatch_batch_id IS NULL AND status = 'created'`.
- Grupuj per courier per dzień odbioru.
- InPost: jeden `POST /dispatch_orders` z listą `shipment_ids`, zapisz `inpost_dispatch_order_id`, ustaw `dispatch_batch_id`.
- Apaczka: zostaw 1:1.

### 12.6 Operatorski panel batchowania

Bez tego batch nie zadziała w praktyce:
- Widok "Wysyłki czekające na podjazd" — `dispatch_batch_id IS NULL`.
- Przycisk "Zaplanuj podjazd na dziś/jutro".
- Widok aktywnych batchy z możliwością cancel.
- Alert "X wysyłek czeka > 4h na podjazd".

### 12.7 Co jeszcze warto batchować / kolejkować

- **Webhook processing** — kolejka (Redis / Postgres queue) + worker zamiast synchronicznego handlera. Inaczej HTTP timeout Shopify (5s) uderzy przy wolniejszej odpowiedzi kuriera.
- **Status sync** — zamiast pollować InPost per shipment, użyć ShipX webhooks (InPost wysyła do nas updates).
- **Reporting/eksport** — raport dzienny dla księgowości raz dziennie cronem.

### 12.8 Granica decyzji — co NIE batchować

- Tworzenie shipmentu — 1:1 (cancel per zamówienie).
- Apaczka — dopóki nie ma "dispatch dla listy".
- Walidacja danych — każde zamówienie osobno, błąd jednego nie blokuje innych.

---

## 13. Priorytety wdrożenia (kolejność)

1. **HMAC verification webhooka Shopify** — krytyczne bezpieczeństwo.
2. **Idempotency `processed_webhooks`** — żeby nie duplikować shipmentów.
3. **Hierarchia wyjątków + walidacja danych z Shopify** — żeby handler się nie wywalał.
4. **Timeout i retry dla httpx** — `CourierTransientError` z backoff.
5. **Zapis `dispatch_order_id` w draft** — odblokowanie cancel.
6. **Cancel endpointy** — `/cancel-shipment`, `/cancel-dispatch`, `/cancel-pickup`.
7. **Batch dispatch dla InPost** — dopiero gdy reszta stabilna.

---

## 14. Otwarty wątek na później

**Refaktor architektury w stronę bardziej reaktywnej** (nie w całości oparty na HTTP request/response). Do rozpatrzenia po ustabilizowaniu obecnego flow i wdrożeniu priorytetów 1–6. Możliwe kierunki: event-driven między modułami (shipment / dispatch / cancel jako eventy), kolejka webhooków Shopify, konsumpcja ShipX webhooków od InPostu jako źródła prawdy o statusie.

---

*Notatka robocza — bazuje na ustaleniach z sesji 29–30 czerwca 2026. Kod nie był jeszcze zmieniany.*
