# Production-readiness audit — Shipping flow

**Data**: 2026-07-03
**Zakres**: `develop @ 385a6a7` (po merge PR #78)
**Autor**: Piotr Gryzlo
**Cel**: Ocena gotowości produkcyjnej ścieżki Shopify → InPost / Apaczka / Wysyłam z Allegro w kontekście: (a) zgodności z aktualnym Allegro Delivery API, (b) edge case'ów Shopify → InPost + Apaczka, (c) filtrów i routingu zamówień.

---

## Executive summary

Kod po merge PR #78 jest **funkcjonalnie sprawny w środowisku testowym**, ale przed pełnym uruchomieniem produkcyjnym wymaga adresowania **2 krytycznych (P0)**, **9 ważnych (P1)** i **3 kosmetycznych (P2)** problemów.

**Top 3 rekomendacje (posortowane po pilności):**

1. **P0-1** — Migracja `proposalItems` → `pickupTimes` w Wysyłam z Allegro. **Deadline: 1 lipca 2026, minął 2 dni temu.** Endpoint stary jeszcze może działać, ale odpowiedź może się zmienić w dowolnym momencie. Fix jest kilkulinijkowy.
2. **P0-2** — Refresh token Allegro jest w pamięci procesu. Allegro rotuje refresh token przy każdym użyciu. Po każdym restarcie kontenera integracja umiera i wymaga ręcznego re-authu. Konieczna trwała persystencja (Key Vault / Table Storage).
3. **P1-1** — Field `deliveryMethodId` w `create-commands` jest deprecated (usuwany Q1 2027). Aktualnie opcjonalny — Allegro sam wyprowadza go z orderu. Warto już teraz przestać go wysyłać, żeby uniknąć niespodzianek.

**Pozytywy warte podkreślenia:**

- HMAC Shopify wymuszany bezwarunkowo (brak trybu „unsigned bypass").
- Dedup zamówień atomowy (Azure `create_entity` + `ResourceExistsError`, lokalnie `flock` wokół load→check→save).
- `mark_fulfilled` idempotentny, respektuje 5-sekundowy timeout Shopify przez `BackgroundTasks`.
- Typowana hierarchia wyjątków (`AllegroBusinessError`, `InPostBusinessError`, `AllegroCommandPending`).

---

## P0 — Krytyczne

### P0-1 · Wysyłam z Allegro: `proposalItems` → `pickupTimes` — deadline minął 2026-07-01

**Plik**: `zdrovena/common/allegro.py:413-421`, `zdrovena/api/routers/webhooks.py:594`
**Testy**: `tests/test_allegro_delivery_execute.py:173-228`, `tests/test_allegro_ship_with_allegro.py:438-448`

**Problem**:
Kod używa starego formatu odpowiedzi z `POST /shipment-management/pickup-proposals`:

```python
# allegro.py:421
return list(data.get("proposalItems") or [])
```

Analogicznie w create-pickup jest używany `pickupDateProposalId`. Allegro ogłosił zmianę: `proposalItems` staje się `pickupTimes`, `pickupDateProposalId` staje się `pickupTime.date`. Deadline: **1 lipca 2026** ([komunikat Allegro Developer](https://developer.allegro.pl/news/wysylam-z-allegro-wprowadzilismy-zmiany-na-zasobach-do-zarzadzania-wysylka-przesylek-i-ich-odbiorem-przez-kuriera-oADdP41WVHA)).

**Ryzyko**: Endpoint po deadline może:
1. Nadal zwracać `proposalItems` w trybie zgodności wstecznej (obserwowany scenariusz przy podobnych migracjach Allegro), lub
2. Zwrócić `pickupTimes` bez `proposalItems` → nasz kod dostaje pustą listę → `_run_allegro_delivery` nie utworzy pickupu → paczki nie zostaną odebrane przez kuriera.

**Rekomendacja**:

```python
# allegro.py
items = data.get("pickupTimes") or data.get("proposalItems") or []
return list(items)
```

W create-pickup — analogicznie akceptować oba formaty w warstwie wywołania. Testy: dodać `test_pickup_proposals_new_format` z `pickupTimes` w fixture i upewnić się, że stary format nadal działa (regresja).

**Effort**: ~30 min (kod + 2 testy).

---

### P0-2 · Refresh token Allegro tylko w pamięci procesu

**Plik**: `zdrovena/common/allegro.py:62-120`

**Problem**:
Konstruktor przyjmuje `refresh_token: str`, zapisuje jako `self._refresh_token`. Po `_refresh_access_token()` (linia 84-120) Allegro odsyła **nowy** refresh token (rotacja przy każdym użyciu — potwierdzone w dokumentacji Allegro OAuth 2.0). Zapisujemy go do `self._refresh_token`, ale nigdy nie utrwalamy poza procesem.

**Skutek**: po restarcie kontenera (deploy, autoscaling, OOM, awaria) — stary refresh token z env/Key Vault jest już nieważny (bo Allegro go zrotował w międzyczasie), a nowy zniknął z RAM. Integracja umiera do momentu ręcznego re-authu przez `/oauth/authorize?client_id=...`.

**Ryzyko**: w produkcji to jest **jedno-restartowa bomba zegarowa**. Nie zauważysz problemu na dev/test bo kontener chodzi tygodniami — na prod padnie przy pierwszym rolloucie.

**Rekomendacja**: Persistować rotowany refresh token do Azure Key Vault (preferowane) lub Azure Table Storage z RBAC-lockiem. Wzorzec:

```python
async def _refresh_access_token(self) -> None:
    # ... istniejący request ...
    new_rt = payload.get("refresh_token")
    if new_rt and new_rt != self._refresh_token:
        self._refresh_token = new_rt
        await self._token_store.save_refresh_token(new_rt)  # NEW
```

Store abstrahowany za interfejsem `AllegroTokenStore` z implementacją `KeyVaultTokenStore` (prod) i `InMemoryTokenStore` (testy). Init klienta ładuje token ze store'a, fallback do env tylko przy pierwszym uruchomieniu.

**Effort**: ~2h (interfejs + KV impl + testy + wpięcie w bootstrap).

**Uwaga bezpieczeństwa**: Warto też dodać monitorowanie „ile razy w ostatnich 24h refreshowaliśmy token" — nagła seria pod rząd sugeruje problem z zapisem.

---

## P1 — Ważne

### P1-1 · `deliveryMethodId` w `create-commands` — deprecated, usuwany Q1 2027

**Plik**: `zdrovena/common/allegro.py` (create-commands), `zdrovena/api/routers/webhooks.py:549` (mapowanie)

**Problem**: Zgodnie z [tutorialem Allegro](https://developer.allegro.pl/tutorials/jak-zarzadzac-przesylkami-przez-wysylam-z-allegro-LRVjK7K21sY): `deliveryMethodId` jest opcjonalny — Allegro sam wyprowadza go z orderu na podstawie wybranej przez kupującego metody wysyłki. Endpoint `GET /shipment-management/delivery-services` (zwracający listę metod) jest **oznaczony jako deprecated i usuwany w Q1 2027**.

**Ryzyko**: Nasz kod zależy od tego endpointa do listowania metod. Jeśli klient Allegro zmieni metody wysyłki, a my mamy zcache'owaną starą listę, wysyłamy błędny `deliveryMethodId` i order odpada.

**Rekomendacja**:
1. Przestać wysyłać `deliveryMethodId` w `create-commands` (Allegro wyprowadzi z orderu).
2. Wycofać wywołania `GET /shipment-management/delivery-services` z runtime — jeśli potrzebne do UI/config, przenieść do jobu offline.

**Effort**: ~1h.

---

### P1-2 · InPost `additionalServices[].sendingAtPoint` — TODO, nie jest mapowany

**Plik**: `zdrovena/api/routers/webhooks.py:549-551, 700-717, 794`

**Problem**: W kodzie wyznaczamy `allegro_sending_method` (`parcel_locker` / `dispatch_order` / `pop` / `any_point`) i zapisujemy w rekordzie draftu (linia 794), ale w `create-commands` (linia 549) mamy jawne `TODO`:

```python
# TODO: map draft["allegro_sending_method"] to a valid Allegro additionalServices
# "sendingAtPoint"/"parcel_locker" values were not valid API values, so we omit
```

Aktualnie pole jest opcjonalne — Allegro używa domyślnego (`parcel_locker` dla nadania w paczkomacie). Ale Allegro sygnalizuje ([issue #9915 allegro-api](https://github.com/allegro/allegro-api/issues/9915)), że pole **stanie się obowiązkowe w przyszłości**.

**Rekomendacja**: Zmapować i wysyłać w `additionalServices`. Format (potwierdzony w [tutorialu](https://developer.allegro.pl/tutorials/jak-zarzadzac-przesylkami-przez-wysylam-z-allegro-LRVjK7K21sY)):

```python
additional_services = []
if draft.get("allegro_sending_method"):
    additional_services.append({"sendingAtPoint": draft["allegro_sending_method"]})
```

Uwaga: wartości enum są case-sensitive. Zweryfikować przy fixture-driven teście integracyjnym.

**Effort**: ~1h + 2 testy.

---

### P1-3 · `AllegroCommandPending` — brak workera dokańczającego drafty

**Plik**: `zdrovena/api/routers/webhooks.py` (`_run_allegro_delivery`, `execute_draft`)

**Problem**: Klient robi 3 próby polling'u (`_poll_command_status`), potem rzuca `AllegroCommandPending` — kod łapie to i zwraca „draft pozostaje w pending". **Nie ma workera**, który by później sprawdził status i sfinalizował. Draft utknie na wieki w stanie `pending` bez ręcznej interwencji.

**Ryzyko**: Order zapłacony, klient czeka, paczka nie idzie. Operator musi ręcznie retriggerować draft.

**Rekomendacja**: Cron/timer job w Azure Functions, który co N minut skanuje drafty w statusie `pending` z `commandId` i wznawia poll:

```python
# nowy endpoint / timer trigger
async def resume_pending_drafts():
    for draft in shipping_store.list_by_status("pending"):
        if not draft.get("allegro_command_id"):
            continue
        try:
            status = await client.get_command_status(draft["allegro_command_id"])
            if status == "SUCCESS":
                await _finalize_draft(draft)
            elif status == "FAILURE":
                await _mark_draft_failed(draft, status_details)
        except AllegroCommandPending:
            continue  # nadal czeka, spróbujemy za kolejnych N minut
```

Alternatywa lżejsza: przy każdym GET `/drafts/{id}` przez UI/admina — spróbuj wznowić poll (opportunistic).

**Effort**: ~3h (timer + endpoint + monitoring alertu na drafty pending > 1h).

---

### P1-4 · InPost cancel — brak status-guard

**Plik**: `zdrovena/common/inpost.py` (`cancel_shipment`), użycie w `webhooks.py`

**Problem**: [Dokumentacja InPost](https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/451903492/InPost+Integration+FAQ): cancel `/shipments/{id}` jest ważny **tylko w statusach `created` / `offers_prepared`**. Po `confirmed` — endpoint zwraca 422 `invalid_action`. Kod łapie generyczne 422 jako `InPostBusinessError` z całym payloadem, ale operator w logu widzi tylko „422 error" — musi kopać w JSON żeby zrozumieć.

**Ryzyko**: Frontend/operator myśli „cancel się nie udał, spróbuj jeszcze raz" — retry, retry, retry. Szum w Sentry.

**Rekomendacja**:
1. Przed wywołaniem cancel — sprawdź status shipmentu (`GET /shipments/{id}`) i odmów lokalnie z sensownym komunikatem, jeśli już `confirmed`.
2. W handlerze błędu — sparsuj `error.key` i wyekspozuj konkretną klasę wyjątku (`InPostShipmentNotCancellable`).

Analogicznie dla `DispatchOrder` — cancel tylko w `new` / `sent`.

**Effort**: ~2h.

---

### P1-5 · InPost `debt_collection` / `trucker_id_not_set_for_organization` — silent-fail

**Plik**: `zdrovena/common/inpost.py`

**Problem**: Te dwa error keys ([opis na ProstaPaczka](https://prostapaczka.pl/instrukcja/komunikat-debt-collection-z-api-inpost-shipx/)) oznaczają problemy organizacyjne (dług na koncie / brak przypisanego kuriera) — nie code bugi. Aktualnie idą jako generyczne 422. Operator dowiaduje się, że „coś nie działa" dopiero jak paczki nie idą.

**Rekomendacja**:
- Osobna klasa `InPostOrganizationError`.
- Alert (log level `ERROR` + Sentry tag `inpost_org_issue`) — natychmiast do operatora.
- Nie retry'ować (bo to nie transient).

**Effort**: ~1h.

---

### P1-6 · `SHOPIFY_ALLOWED_DOMAINS` — fallback „accept all" w produkcji

**Plik**: `zdrovena/api/routers/webhooks.py:105-123`

**Problem**: Jeśli `SHOPIFY_ALLOWED_DOMAINS` nie jest ustawione — kod loguje `WARNING` i **akceptuje webhooki z dowolnego domenu Shopify**. HMAC to weryfikuje (poprawnie), ale domena jest orthogonalną warstwą obrony.

**Ryzyko**: Ktoś zdobył `SHOPIFY_WEBHOOK_SECRET` (leak, insider) → może wysyłać webhooki spoofowane jako z dowolnego sklepu. Zamówienia z lewych sklepów trafiają do naszej kolejki, generujemy etykiety, tracimy pieniądze na InPost. Whitelist domenowy to defense-in-depth przeciw temu.

**Rekomendacja**:
- Jeśli `ENV=production` — fail-closed: log `ERROR` + zwróć 503 zamiast akceptować.
- Runtime check przy starcie aplikacji (fail on boot): odmówić uruchomienia serwera, jeśli `ENV=production` i `SHOPIFY_ALLOWED_DOMAINS` puste.

```python
if os.getenv("ENV") == "production" and not _allowed_domains():
    raise RuntimeError("SHOPIFY_ALLOWED_DOMAINS required in production")
```

**Effort**: ~30 min.

---

### P1-7 · `_pick_courier` — fragile substring routing

**Plik**: `zdrovena/api/routers/webhooks.py:261-273`

**Problem**: Routing wybiera kuriera po substringu z `shipping_lines[0].title.lower()`:

```python
def _pick_courier(order):
    title = shipping_lines[0]["title"].lower()
    if "inpost" in title or "paczkomat" in title:
        return "inpost"
    return "apaczka"
```

Rozgałęzienia:
- **Empty shipping_lines** → kod skipuje z warningiem (OK, ale operator nie ma info czemu order utknął).
- **Renaming w Shopify** → operator zmienia „InPost Paczkomat 24/7" na „Paczkomat 24/7 (InPost)" — nadal działa. Ale jeśli ktoś zrobi „DPD via InPost Sieć Sklepów" — routing pójdzie do InPost, a to prawdopodobnie zła intencja.
- **Wiele shipping_lines** — ignorujemy [1..N], bierzemy tylko [0]. Rare, ale możliwe w multi-parcel orderach.
- **Brak fallbacku** — jeśli InPost padnie na live, cały ruch idzie w błąd; nie ma automatycznego fallbacku do Apaczki dla tego samego zamówienia.

**Rekomendacja**:
1. Explicit mapping w konfigu (env / Table Storage) zamiast substring:
   ```python
   SHIPPING_TITLE_ROUTING = {
       "InPost Paczkomat": ("inpost", "paczkomat"),
       "InPost Kurier": ("inpost", "kurier"),
       "DPD": ("apaczka", "dpd"),
       ...
   }
   ```
2. Loggować **surowy title** dla każdego draftu → operator w Grafanie widzi „hej, pojawił się nowy title X, wpadł w default".
3. Empty shipping_lines → zapisać draft ze statusem `needs_manual_routing` (nie skipować cicho).

**Effort**: ~2h.

---

### P1-8 · `_pick_inpost_service` — również substring

**Plik**: `zdrovena/api/routers/webhooks.py:270-273`

Ta sama fragility co P1-7 — wybór locker vs kurier InPost po substringu `paczkomat`. Analogiczna rekomendacja (włączyć do routing tablicy z P1-7).

**Effort**: uwzględniony w P1-7.

---

### P1-9 · Brak dead-letter queue dla nieudanych draftów

**Plik**: `zdrovena/api/routers/webhooks.py` (`_run_allegro_delivery`, `_run_inpost_delivery`, `_run_apaczka_delivery`)

**Problem**: `BackgroundTasks` FastAPI nie ma persystencji ani retry. Jeśli task rzuci wyjątek — task ginie, zamówienie zostaje w Shopify jako „paid, unfulfilled". Operator dowiaduje się od klienta.

**Rekomendacja**:
- Przenieść executor na Azure Queue Storage / Service Bus (persistent, retry with backoff, DLQ).
- Alternatywa lżejsza: zapisywać failed drafts do tabeli `shipping-failed` i mieć timer, który retry'uje z exponential backoff (max N razy, potem alert).

**Effort**: ~4h (Service Bus poller) lub ~2h (retry table).

---

## P2 — Nice-to-have

### P2-1 · InPost `PARCEL_SPECS` — wszystkie ustawione na `paczkomat_template: "large"`

**Plik**: `zdrovena/common/inpost.py:PARCEL_SPECS`

Nawet `pół-pak` używa najlargeszej gabaryty paczkomatu — koszt wysyłki wyższy, mniej dostępnych paczkomatów (large slots są rzadziej wolne). Powinien być mapping po objętości/wadze paczki na `small` / `medium` / `large`.

**Effort**: ~2h (tabela mapowania + testy).

---

### P2-2 · `LOCKER_LARGE_SLOT` dla DPD marked `verified: false`

**Plik**: `zdrovena/common/inpost.py` (`LOCKER_LARGE_SLOT`)

Wymiary DPD `dpd_automat` / `dpd_punkt` są w kodzie oznaczone `verified: false` — czyli ktoś wrzucił szacunkowe wymiary. Ryzyko: paczka nie mieści się w automacie, DPD odrzuca w sortowni, klient dostaje maila „paczka niedostarczona".

**Rekomendacja**: zweryfikować w [oficjalnej dokumentacji DPD](https://www.dpd.com/pl/pl/wsparcie/dpd-pickup/) i usunąć flagę `verified: false`.

**Effort**: ~15 min research + fix.

---

### P2-3 · `_calc_packages` — greedy allocation bez sprawdzania locker constraint

**Plik**: `zdrovena/api/routers/webhooks.py` (`_calc_packages`)

Greedy pack po liczbie sztuk × waga jednostkowa. Nie sprawdza, czy sumaryczna paczka mieści się w slocie paczkomatu (`large`: 41×38×64cm). Aktualne SKU (`3-pak`: 40×40×20cm) mieszczą się, ale przy nowym SKU może się wykrzaczyć na runtime.

**Rekomendacja**: Warunek asercyjny `assert package.fits_in(LOCKER_LARGE_SLOT)` przy alokacji + degradacja do „kurier" jeśli za duża.

**Effort**: ~1h.

---

## Pozytywy — co warto zachować

- **HMAC always required** (`_verify_hmac` bezwarunkowe, brak trybu bypass).
- **Dedup atomowy** — `shopify_dedup_store.mark_seen_if_new` używa `create_entity` + `ResourceExistsError` (Azure) / `flock` (local). Fail-closed przy błędzie store → Shopify retry (503).
- **TTL dedupu 24h** — Shopify retryuje przez 4h, więc 24h daje sensowną marginę bez zaśmiecania storage.
- **`ALLOWED_SHOPIFY_TOPICS = {"orders/create"}`** — defense-in-depth, `orders/updated` intencjonalnie wykluczony.
- **`mark_fulfilled` idempotentny** — bezpieczny do retry.
- **BackgroundTasks** — respektuje 5s timeout Shopify.
- **Typed exception hierarchy** (`AllegroBusinessError`, `AllegroCommandPending`, `InPostBusinessError`, etc.) — pozwala precyzyjnie łapać i logować.
- **PII TEST_MODE fixtures** (PR #78) — właściwe podejście do testów integracyjnych.

---

## Rekomendowana sekwencja wdrożenia

1. **Dziś / jutro** — P0-1 (Allegro `pickupTimes`, deadline minął), P0-2 (refresh token persistence). Bez tych dwóch nie ma sensu iść na prod.
2. **Ten tydzień** — P1-1 (deliveryMethodId), P1-2 (sendingAtPoint), P1-6 (SHOPIFY_ALLOWED_DOMAINS fail-closed).
3. **Ten sprint** — P1-3 (worker dla pending draftów), P1-4 + P1-5 (InPost error surfacing), P1-7 + P1-8 (routing config).
4. **Przed pierwszym pełnym prodowym ruchem** — P1-9 (DLQ).
5. **Backlog** — P2-1, P2-2, P2-3.

---

## Źródła

- [Allegro Developer — proposalItems → pickupTimes migration (deadline 2026-07-01)](https://developer.allegro.pl/news/wysylam-z-allegro-wprowadzilismy-zmiany-na-zasobach-do-zarzadzania-wysylka-przesylek-i-ich-odbiorem-przez-kuriera-oADdP41WVHA)
- [Allegro Developer — tutorial „Jak zarządzać przesyłkami przez Wysyłam z Allegro"](https://developer.allegro.pl/tutorials/jak-zarzadzac-przesylkami-przez-wysylam-z-allegro-LRVjK7K21sY)
- [GitHub allegro/allegro-api #9915 — sendingAtPoint enum values](https://github.com/allegro/allegro-api/issues/9915)
- [GitHub allegro/allegro-api #11716 — SHIPMENT_CANCELLATION_NOT_SUPPORTED](https://github.com/allegro/allegro-api/issues/11716)
- [InPost Integration FAQ — cancel statuses](https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/451903492/InPost+Integration+FAQ)
- [ProstaPaczka — komunikat debt_collection z API InPost ShipX](https://prostapaczka.pl/instrukcja/komunikat-debt-collection-z-api-inpost-shipx/)
