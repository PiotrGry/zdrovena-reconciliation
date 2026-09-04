# Funkcje aplikacji — co portal robi i jak

**Stan na:** 2026-09-02, wersja 2.11.0
**Dla kogo:** właściciel, operator, każdy kto wraca do kodu po przerwie

Dokument opisuje **cztery obszary funkcjonalne** i to, co dzieje się pod spodem. Nie zastępuje
ADR-ów (decyzje architektoniczne) ani specyfikacji w `docs/superpowers/specs/` (projekty
pojedynczych zmian) — jest mapą, od której się zaczyna.

Rozmiar systemu: **31 339 linii Pythona w 127 modułach**, 373 zależności między nimi, **zero
cykli importów**. Największy obszar to zamykanie miesiąca (8 217 linii, 26% kodu).

---

## Spis

1. [Zamykanie miesiąca](#1-zamykanie-miesiąca) — największy i najbardziej „sztywny" obszar
2. [Wysyłki](#2-wysyłki) — od zamówienia do etykiety
3. [DLQ](#3-dlq--kolejka-nieudanych-draftów)
4. [Uszkodzone przesyłki](#4-uszkodzone-przesyłki)
5. [Co się dzieje pod spodem](#5-co-się-dzieje-pod-spodem) — fundamenty wspólne dla wszystkiego

---

## 1. Zamykanie miesiąca

**Po co:** zebrać komplet dokumentów za miesiąc, sprawdzić je, spakować w ZIP i wysłać księgowej.

**Gdzie w kodzie:** `zdrovena/month_closing/` (22 moduły), API w `zdrovena/api/routers/close.py`,
UI w `frontend/src/views/CloseView.jsx` + `frontend/src/views/close/`.

### Siedem kroków, w tej kolejności

```
check → sales → costs → reports → bank → package → send
  │       └────────────┬────────────┘       │        │
  │            COLLECTION_ACTIONS           │        │
  │         (kolejność między nimi          │        │
  │          dowolna, ale wszystkie         │        │
  │          po check)                      │        │
  │                                          │        │
kontrola                                  budowa   wysyłka
wstępna                                    ZIP     do księgowej
```

| krok | co robi |
|---|---|
| `check` | Kontrola wstępna — czy są faktury kosztowe od dostawców i wyciąg bankowy |
| `sales` | Zbiera faktury sprzedażowe z Fakturowni |
| `costs` | Zbiera faktury kosztowe |
| `reports` | Generuje raporty (JPK_FA, zestawienia) |
| `bank` | Wyciąg PKO BP |
| `package` | Buduje ZIP z zebranego materiału |
| `send` | Wysyła maila do księgowej |

Definicja: `run_store.py:24` (`STEP_IDS`), reguły przejść: `workflow.py:908` (`_validate_action`).

### Bramki — i tu jest źródło sztywności

Reguły są **twarde i sekwencyjne** (`workflow.py:908`):

- Żaden krok zbierający nie ruszy, dopóki `check` nie jest zaliczony — *„Najpierw uruchom
  kontrolę wstępną."*
- `package` wymaga, żeby **wszystkie cztery** kroki zbierające były `done` albo pominięte.
- `package` i `send` są blokowane przez **każdy** problem o wadze `blocker` lub `error`
  (`BLOCKING_SEVERITIES`, `workflow.py:52`).
- `send` wymaga: zbudowanej paczki, jawnego potwierdzenia operatora, a przy ostrzeżeniach —
  **podania powodu na piśmie**.
- `send` dodatkowo weryfikuje, że wysyłana paczka to **ta sama**, którą operator przejrzał
  (`_package_blocking_reason`, hash artefaktu — #311).

### Zawory bezpieczeństwa, które już istnieją

Warto wiedzieć, że system **nie jest** całkiem sztywny — ma trzy furtki:

**Waivery** (`WAIVABLE_STEPS`, `workflow.py:50`) — operator może odpuścić krok `check`, `sales`,
`costs`, `reports` lub `bank` z uzasadnieniem. Waiver jest **unieważniany automatycznie**, gdy
ten krok zostanie uruchomiony ponownie (`run_store.py:455`) — żeby nie został jako martwe
zwolnienie z kontroli, której już nie ma. Limit: 200 na przebieg.

**Override przy ostrzeżeniach** — wysyłka mimo `warning` jest możliwa, ale wymaga wpisania powodu.

**Rozstrzygnięcie niejednoznacznej wysyłki** (`resolve_email_attempt`, #312) — jeśli nie wiadomo,
czy mail doszedł (przyjęty-i-zgubiony wygląda z naszej strony identycznie jak niewysłany),
**tylko człowiek** może to rozstrzygnąć. `delivered=True` zamyka okres bez drugiego maila,
`delivered=False` odblokowuje ponowienie.

### Odwracalność

Zamknięcie jest odwracalne (ADR 0002). Ponowne uruchomienie dla tego samego okresu przelicza
kontrolę, buduje nowy ZIP i zapisuje nową wersję. Jest też `reset` całego przebiegu.

Historia przebiegów: `close_history.py`, `table_history.py`.

### Konkurencja

Przebieg ma licznik rewizji (`rev`) i działa na zasadzie compare-and-swap: pisarz, który czytał
starszą kopię, dostaje odmowę zamiast po cichu nadpisać cudzą zmianę (`run_store.py:76`).
`RunBusyError` chroni przed dwoma operatorami na tym samym okresie.

---

## 2. Wysyłki

**Po co:** zamienić zamówienie ze Shopify lub Allegro w etykiety kurierskie, bez ręcznego
przepisywania danych.

**Gdzie w kodzie:** `zdrovena/shipping/` (domena + providerzy), `zdrovena/api/routers/shipping/`
(8 routerów), `zdrovena/api/shipping_draft_composition.py` (budowa draftu),
`shipping_execution_composition.py` (wykonanie), UI w `frontend/src/views/ShippingView.jsx`.

### Ścieżka zamówienia

```
Shopify webhook ─┐
                 ├─→ draft (plan paczek) ─→ przegląd operatora ─→ wykonanie ─→ etykiety
Allegro poller ──┘         │                                          │
                           │                                          ├→ InPost ShipX
                    calc_packages()                                   ├→ Apaczka
                  butelki → zgrzewki → pudła                          └→ Allegro Delivery
```

### Plan paczek

`calc_packages` (`shipping/domain/planning.py`) czyta liczbę butelek z **nazwy produktu**
(`bottles_per_unit`), zwija je do **półpaków** (1 półpak = 6 butelek) i zachłannie wypełnia pudła:

| typ | 3-pak | 2-pak | 1-pak | pół-pak | szkło |
|---|---|---|---|---|---|
| półpaki | 6 | 4 | 2 | 1 | 2 |

Szkło i plastik pakowane są **osobno**. Pojemności trzyma `PARCEL_HALF_PACKS`, wymiary i wagi
`PARCEL_SPECS` (oba w `common/shipping_parcels.py`).

Zachłanne wypełnianie dotyczy tylko plastiku — **szkło jedzie po jednej zgrzewce na pudełko**,
bo większego kartonu na szkło nie ma. Każde pudełko to osobna paczka, etykieta i numer śledzenia.

> **Zawieszone: `szkło-2pak`** (wrzesień 2026). Typ oznaczał *dwa* pudełka szkła i był planowany
> zachłannie dla 2+ zgrzewek, ale nic go nie rozwijało: kurier dostawał **jedną** etykietę i wagę
> **jednego** pudełka (9 kg) na dwa (zamówienie #1735). Planer już go nie tworzy, a drafty zapisane
> wcześniej `physical_parcels()` rozwija na dwie paczki `szkło`. W edytorze planu typ nie jest do
> wyboru; na starym drafcie pokazuje się jako `szkło-2pak (wycofany)` i liczy się jako 2 paczki.
>
> Typ jest **zawieszony, nie usunięty** — przełącznik `GLASS_2PAK_SUSPENDED`
> (`shipping/domain/planning.py`, lustrzany w `frontend/.../parcelTypes.js`) przywraca go
> w jednym miejscu. Przed przywróceniem trzeba zmierzyć realny karton i poprawić
> `PARCEL_SPECS["szkło-2pak"]` — dziś trzyma wymiary i wagę *jednego* pudełka. Pełna lista
> kroków jest w komentarzu przy przełączniku.

Gdy nazwa produktu jest nieczytelna, planer zgaduje („jedna sztuka = jedna zgrzewka") i **oznacza
draft do przeglądu** — po tym, jak zmiana nazwy SKU szkła cicho zaklasyfikowała je jako plastik
(zamówienia #1710–#1712). Operator może przepakować plan ręcznie w UI.

### Statusy draftu

`pending` → `needs_review` → `executing` → `pending_confirmation` → `created` / `cancelled`

Po wejściu w `executing` **plan paczek jest zamrożony** (`_BREAKDOWN_LOCKED_STATUSES`) — inaczej
zapisany plan przestałby się zgadzać z etykietami już u kuriera.

Status `error` jest **celowo edytowalny**: większość awarii zdarza się zanim cokolwiek zostanie
zabookowane (zły telefon, podział COD, którego paczkomat nie przyjmie), a przepakowanie planu to
wtedy jedyna droga wyjścia dla operatora. Ale jeśli draft padł *w połowie* i ma już
`courier_shipments`, plan jest zamrożony mimo statusu (`_breakdown_locked_reason`) — utworzone
etykiety są wydrukowane i opłacone, a to plan je numeruje: przepakowanie przenumerowałoby te
jeszcze niezabookowane („1/2" już u kuriera, „2/3" dobookowane), a zmiana typu zostawiłaby
utworzoną etykietę na pudełku, którego w planie już nie ma.

### Niezmiennik, na którym stoi całość

> **etykieta = paczka = numer śledzenia**

Jedna fizyczna paczka to jedna przesyłka u kuriera. `courier_shipments` to lista checkpointów
kluczowana `(package_type, package_number)`; wznowienie po częściowej awarii chodzi po tym samym
kluczu i nie tworzy duplikatów.

### Pobranie (COD)

Kwota to `total_outstanding` ze Shopify — **nie** `total_price`, bo po częściowej wpłacie
przepłacilibyśmy klienta. COD rozpoznajemy po `payment_gateway_names`, nie po statusie płatności
(karta też bywa „pending").

Przy wielu paczkach kwota **dzieli się**: koszt dostawy równo na paczki, reszta proporcjonalnie
do wartości towaru, który w danej paczce faktycznie leży. Arytmetyka w całych groszach metodą
największych reszt, więc części sumują się do `total_outstanding` co do grosza z konstrukcji.

Kaucja i rabaty jadą wewnątrz dzielonej puli — dlatego pula towaru jest **rozdzielana** wagami,
a nie sumowana z cen pozycji: `kaucja` wypada z planu paczek przez `SKIP_RE`, a klient i tak
ją płaci.

Podział nigdzie nie jest zapisywany. Jest funkcją kwoty, kosztu dostawy, pozycji zamówienia
i planu paczek (`shipping/domain/cod.py`), więc przepakowanie przelicza go samo i nie ma czego
zestarzeć. Jego wejścia zamrażają się razem z `cod` przy starcie wysyłki — inaczej edycja
zamówienia w Shopify dałaby wznawianej paczce inną kwotę niż ta na etykiecie leżącej już
u kuriera.

**Paczkomat pozostaje zablokowany dla wielu paczek** — tam każdą paczkę odbiera się osobno,
więc podział pozwoliłby klientowi zapłacić za jedno pudło i zostawić resztę.

Pojemności pudeł w półpakach: `PARCEL_HALF_PACKS` (`common/shipping_parcels.py`) — trzymane
obok `PARCEL_SPECS`, a nie w środku, bo tamten rekord jedzie do kuriera wprost jako `dimensions`
przesyłki i pojemność podróżowałaby jako fałszywy wymiar.

Ubezpieczenie jedzie za pobraniem: InPost wymaga `insurance ≥ cod`, Apaczka `shipment_value ≥ cod`,
liczone per paczka.

### Kurierzy

| kurier | usługi | uwagi |
|---|---|---|
| InPost | paczkomat, kurier | ShipX; od 2026-09-08 wymaga poprawnego telefonu odbiorcy (#294) |
| Apaczka | kurier + punkty (DPD, Orlen, Pocztex, DHL, UPS) | `order.content` max 50 znaków; COD wymaga NRB |
| Allegro Delivery | „Wyślij z Allegro" | Allegro zleca transport i rozlicza pobranie samo |

**Znana dziura:** mapper Allegro (`common/allegro_mapper.py`) nie przenosi żadnych kwot, więc
zamówienie Allegro z metodą „…InPost pobranie" trafia do nas z `cod: null` i portal nie pokazuje,
że to pobranie. Pieniądze są bezpieczne (rozlicza je Allegro), ale informacji brak.

### Po nadaniu

Etykiety (`labels.py`), zlecenie podjazdu kuriera, fulfillment do Shopify/Allegro, faktura w
Fakturowni (`shipping/invoices.py`), pollery statusów (`inpost_poller`, `apaczka_pickup_poller`,
`allegro_poller`).

---

## 3. DLQ — kolejka nieudanych draftów

**Po co:** żeby zamówienie, przy którym utworzenie draftu padło, **nie zniknęło**.

**Gdzie:** `zdrovena/api/routers/shipping/dlq.py`, UI w `frontend/src/views/DlqView.jsx`.

Gdy webhook Shopify albo poller Allegro nie potrafi zbudować draftu, surowy payload ląduje w
dead-letter queue zamiast zostać zgubiony. Operator ma trzy operacje:

| operacja | co robi |
|---|---|
| **lista** | Pokazuje nieudane próby wraz z payloadem i powodem |
| **retry** | Ponawia budowę draftu z zapisanego payloadu |
| **discard** | Usuwa wpis bez ponawiania — gdy zamówienie okazało się nieistotne |

Przed powstaniem tego widoku endpointy istniały, ale nie było ich w UI — operator musiał używać
`curl`.

---

## 4. Uszkodzone przesyłki

**Po co:** wyłapać zgłoszenie o uszkodzonej paczce i doprowadzić do wysyłki zastępczej.

**Gdzie:** `zdrovena/api/damage_detection.py` (wykrywanie), `zdrovena/damage/application/workflow.py`
(przebieg), `zdrovena/api/routers/damage.py` (API), UI w `frontend/src/views/DamageView.jsx`.

### Wykrywanie — świadomie tylko do odczytu

Detekcja **nigdy** nie tworzy przesyłki zastępczej ani nie kontaktuje się z klientem. Może
najwyżej założyć lokalną sprawę w stanie `needs_review`. Wszystko dalej to jawne decyzje operatora.

Dwa źródła:

- **`allegro_tracking`** — statusy śledzenia z Allegro (`scan_allegro_damage_cases`)
- **`zoho_inpost`** — skrzynka mailowa: zgłoszenia z oddziałów InPost (`scan_zoho_damage_cases`)

### Przebieg sprawy

```
needs_review → (potwierdzenie operatora) → replacement_created → customer_notified → closed
```

Przesyłka zastępcza powstaje przez **sklonowanie oryginalnego draftu** z wyczyszczeniem pól, które
nie mogą przejść dalej (identyfikatory kurierskie, tracking, status), i oznaczeniem
`is_replacement: True` (`clone_replacement_draft`).

---

## 5. Co się dzieje pod spodem

### Warstwy

Graf zależności ma **zero cykli**. Kierunek importów jest jednostronny:

```
cli ─┐
     ├→ api ──→ shipping / month_closing / damage / audit ──→ common
     └→ audit                                                   ↑
                     wszystko może zależeć od common ───────────┘
```

Najczęściej importowane moduły: `common.secrets` (16 zależnych), `common.config` (14),
`common.shipping_exceptions` (14), `api.auth` (14). Strażnikiem granic jest test
`tests/fitness/test_module_boundaries.py`, a decyzja — ADR 0001.

### Sekrety

Azure Key Vault przez Managed Identity — **żadnych haseł w kodzie**. Lokalnie fallback na
keychain (`common/_local_secret_fallback.py`). Manifest wymaganych sekretów:
`scripts/secrets_manifest.py`.

### Przechowywanie

- **Azure Table Storage** — drafty przesyłek, sprawy uszkodzeń, przebiegi zamknięcia, dedup
  webhooków Shopify
- **Azure Blob Storage** — pliki: faktury, ZIP-y, etykiety; z namespace'ami
  (`common/storage_namespaces.py`) pilnującymi granic

**Ważna zasada:** awaria Table Storage **nie może wyglądać jak brak danych** (#310). Sześć ścieżek
odczytu rozróżnia „pusto" od „nie udało się przeczytać" — inaczej chwilowa awaria wyglądałaby jak
zero zamówień i operator zacząłby działać na fałszywym obrazie.

### Idempotencja i odporność

| mechanizm | gdzie | po co |
|---|---|---|
| Dedup webhooków | `shopify_dedup_store.py` | Shopify potrafi wysłać ten sam webhook kilka razy |
| Checkpointy paczek | `courier_shipments` | Wznowienie nie tworzy duplikatów etykiet |
| Compare-and-swap | `run_store.py` (`rev`) | Dwóch operatorów nie nadpisze się nawzajem |
| Ponowienia | `common/retry.py` | Chwilowe awarie API kurierów |
| `SendAttempt` | `common/send_attempt.py` | Rozróżnia „wysłane" od „nie wiadomo" |
| Weryfikacja paczki | `package_integrity.py` | Wysyłamy dokładnie to, co przejrzano |

### Obserwowalność

Ustrukturyzowane logi (`logging_setup.py`), identyfikatory korelacji przez cały request
(`correlation.py`), telemetria OpenTelemetry → Azure Monitor, zdarzenia biznesowe
(`common/events.py`). Alerty operacyjne w Log Analytics: awarie zależności, nieudana
synchronizacja, zakleszczone drafty, rozbieżność kaucji.

### Uwierzytelnianie

Microsoft Entra ID (MSAL) z rolami: `zdrovena-admin`, `zdrovena-accountant`, `zdrovena-viewer`.
Egzekwowane w `api/auth.py`.

### Fake providers

`zdrovena/fake_providers/` — działające atrapy InPost, Apaczki, Allegro i Fakturowni. Pozwalają
przetestować pełną ścieżkę bez ruszania prawdziwych API i bez płacenia za etykiety.

### Kontrakty API

`contracts/openapi.json` generowany deterministycznie ze schematu FastAPI, a z niego typy
TypeScriptu dla frontendu (`frontend/src/api/generated/schema.d.ts`). CI pilnuje dryfu — endpoint
bez modelu odpowiedzi nie przejdzie.

---

## Gdzie szukać dalej

| temat | plik |
|---|---|
| Granice modułów | `docs/ADR/0001-module-boundaries.md` |
| Zamykanie miesiąca — decyzje | `docs/ADR/0002-month-closing-audit-flow.md` |
| Sieć prywatna | `docs/ADR/0003-private-network-model.md` |
| Kontrakty kurierów | `docs/audit/shipment-provider-contracts.md` |
| Gotowość produkcyjna | `docs/audit/production-readiness.md` |
| Alerty | `docs/devops/alerty-operacyjne.md` |
| Projekty pojedynczych zmian | `docs/superpowers/specs/` |
| Historia zmian | `CHANGELOG.md` |
