# zdrovena-reconciliation

Wewnętrzny system back-office dla **Zdrovena / HUMIO** — fakturowanie, audyt butelek, zamknięcie miesiąca.

Składa się z dwóch warstw:
- **REST API** (FastAPI) — serwowane przez Azure Container Apps, chronione JWT (Entra ID). Endpointy biznesowe pod prefixem `/api`; `/health` i `/docs` na roocie.
- **Frontend** (React + Vite + MSAL.js) — Azure Static Web Apps, proxy `/api/*` → Container App

Integracje zewnętrzne: Shopify (webhooki), Allegro (Ship-with-Allegro), InPost (paczkomaty + kurier), Apaczka, DPD, Fakturownia, KSeF 2.0.

---

## REST API

### Endpoints

Większość endpointów biznesowych zamontowana pod prefixem `/api` (np. `GET /api/files`) — dla czytelności pomijam go w tabelach poniżej. `/health` i `/docs` są na roocie.

**System:**

| Method | Path | Rola | Opis |
|--------|------|------|------|
| `GET` | `/health` | — | Liveness check + wersja API (na roocie, bez `/api`) |
| `GET` | `/docs` | — | Swagger UI (na roocie, bez `/api`) |
| `GET` | `/redoc` | — | ReDoc (na roocie, bez `/api`) |

**Pliki:**

| Method | Path | Rola | Opis |
|--------|------|------|------|
| `GET` | `/files` | viewer+ | Lista plików w storage (opcjonalny `?prefix=`) |
| `GET` | `/files/{key}` | viewer+ | Pobranie pliku (streaming) |
| `PUT` | `/files/{key}` | accountant+ | Wgranie pliku |
| `DELETE` | `/files/{key}` | accountant+ | Usunięcie pliku |

**Miesięczne zamknięcie:**

| Method | Path | Rola | Opis |
|--------|------|------|------|
| `POST` | `/close` | accountant+ | Dry-run całego pipeline; tryb live kieruje do etapowego workflow |
| `GET` | `/close/workflow` | viewer+ | Trwały stan etapowego zamknięcia dla miesiąca |
| `POST` | `/close/workflow/actions/{action}` | accountant+ | Wykonanie etapu: `check`, `sales`, `costs`, `reports`, `bank`, `package`, `send` |
| `POST` | `/close/workflow/reset` | accountant+ | Rozpoczęcie nowego przebiegu dla okresu |
| `GET` | `/close/state` | viewer+ | Stan checkpointów pipeline (`PipelineState`) |
| `GET` | `/close/history` | viewer+ | Historia zamknięć |
| `DELETE` | `/close/history/{ts}` | accountant+ | Kasowanie wpisu z historii |

**Fakturownia:**

| Method | Path | Rola | Opis |
|--------|------|------|------|
| `GET` | `/invoices/sales` | viewer+ | Lista faktur sprzedażowych |
| `GET` | `/invoices/products` | viewer+ | Lista produktów |

**Shopify webhooki (bez auth JWT — HMAC + domain whitelist):**

| Method | Path | Opis |
|--------|------|------|
| `POST` | `/webhooks/shopify/order-create` | Canonical webhook — tworzy shipping draft |
| `POST` | `/webhooks/shopify/order-created` | Legacy alias (używane przez starsze konfiguracje Shopify) |

`SHOPIFY_ALLOWED_DOMAINS` jest **fail-closed w produkcji** (brak zmiennej
odrzuca webhook kodem 403); w dev/testach brak listy dopuszcza domenę z
ostrzeżeniem w logu.

**Shipping — projekty wysyłki (P0-P2 audit):**

| Method | Path | Rola | Opis |
|--------|------|------|------|
| `GET` | `/shipping/drafts` | viewer+ | Lista projektów wysyłki |
| `GET` | `/shipping/apaczka-services` | viewer+ | Lista dozwolonych usług Apaczki |
| `PATCH` | `/shipping/drafts/{id}` | shipment-mgr+ | Aktualizuj liczbę paczek |
| `POST` | `/shipping/drafts/{id}/execute` | shipment-mgr+ | Realizuj projekt (create draft w InPost/Allegro/Apaczka) |
| `POST` | `/shipping/drafts/{id}/confirm` | shipment-mgr+ | Potwierdź pending Allegro create-command (P1-3) |
| `POST` | `/shipping/drafts/{id}/pickup` | shipment-mgr+ | Zamów podjazd kuriera InPost |
| `DELETE` | `/shipping/drafts/{id}/shipment` | shipment-mgr+ | Anuluj shipment (z InPost cancel guard, P1-4) |
| `DELETE` | `/shipping/drafts/{id}/dispatch` | shipment-mgr+ | Anuluj dispatch order (odbiór) |
| `POST` | `/shipping/drafts/{id}/mark-fulfilled` | shipment-mgr+ | Oznacz jako zrealizowaną w Shopify |
| `GET` | `/shipping/drafts/{id}/label` | viewer+ | Pobranie etykiety (PDF) |
| `POST` | `/shipping/labels/batch` | viewer+ | Pobranie zbiorczego PDF etykiet |
| `GET` | `/shipping/drafts/{id}/invoice-preview` | viewer+ | Podgląd danych faktury |
| `POST` | `/shipping/drafts/{id}/create-invoice` | shipment-mgr+ | Utworzenie faktury |
| `POST` | `/shipping/sync` | shipment-mgr+ | Synchronizacja zamówień z Allegro i Shopify |

**Shipping — Dead Letter Queue (P1-9):**

| Method | Path | Rola | Opis |
|--------|------|------|------|
| `GET` | `/shipping/drafts/dlq` | viewer+ | Lista nieudanych draftów (`retries` + `last_error`) |
| `POST` | `/shipping/drafts/dlq/{entry_id}/retry` | shipment-mgr+ | Ponów tworzenie draftu |
| `DELETE` | `/shipping/drafts/dlq/{entry_id}` | shipment-mgr+ | Usuń wpis z DLQ (po ręcznej analizie) |

**Shipping — bezpośrednie operacje na przewoźnikach (obejście draftów — troubleshooting):**

| Method | Path | Rola | Opis |
|--------|------|------|------|
| `DELETE` | `/inpost/shipments/{shipment_id}` | shipment-mgr+ | Twardy delete shipment w InPost |
| `DELETE` | `/inpost/dispatch_orders/{dispatch_order_id}` | shipment-mgr+ | Delete dispatch order |
| `DELETE` | `/apaczka/orders/{order_id}` | shipment-mgr+ | Delete order w Apaczce |

**Uszkodzone przesyłki:**

| Method | Path | Rola | Opis |
|--------|------|------|------|
| `GET` | `/damage-cases`, `/damage-cases/summary`, `/damage-cases/{id}` | viewer+ | Lista, podsumowanie i szczegóły spraw |
| `POST` | `/damage-cases/refresh` | shipment-mgr+ | Pobranie sygnałów Allegro i Zoho |
| `POST` | `/damage-cases/{id}/confirm` | shipment-mgr+ | Potwierdzenie uszkodzenia |
| `POST` | `/damage-cases/{id}/ignore` | shipment-mgr+ | Odrzucenie fałszywego sygnału |
| `POST` | `/damage-cases/{id}/close` | shipment-mgr+ | Zamknięcie sprawy |
| `POST` | `/damage-cases/{id}/prepare-replacement` | shipment-mgr+ | Przygotowanie przesyłki zastępczej |
| `POST` | `/damage-cases/{id}/create-replacement` | shipment-mgr+ | Utworzenie przesyłki zastępczej |
| `POST` | `/damage-cases/{id}/confirm-replacement` | shipment-mgr+ | Potwierdzenie przesyłki zastępczej |
| `POST`, `PATCH` | `/damage-cases/{id}/email-draft` | shipment-mgr+ | Utworzenie lub edycja szkicu wiadomości |
| `POST` | `/damage-cases/{id}/send-email` | shipment-mgr+ | Wysłanie zatwierdzonej wiadomości |

### Autentykacja

Wszystkie endpointy poza `/health`, `/docs`, `/redoc` i webhookami Shopify wymagają
`Authorization: Bearer <token>`. Webhooki Shopify zamiast JWT weryfikują HMAC,
temat i domenę sklepu.

Token pochodzi z Azure Entra ID — aplikacja `zdrovena-api` (App Registration).

```bash
# pobranie tokenu przez Azure CLI
TOKEN=$(az account get-access-token \
  --resource "api://<AZURE_API_CLIENT_ID>" \
  --query accessToken -o tsv)

curl -H "Authorization: Bearer $TOKEN" https://<API_URL>/api/files
```

### Role (Entra ID App Roles)

| Rola | Wartość | Uprawnienia |
|------|---------|-------------|
| Admin | `zdrovena-admin` | Pełny dostęp |
| Accountant | `zdrovena-accountant` | Pliki (odczyt + zapis) + zamknięcie miesiąca |
| Viewer | `zdrovena-viewer` | Tylko odczyt plików |
| Shipment Manager | `zdrovena-shipment-mgr` | Zarządzanie wysyłkami (execute, pickup, aktualizacja paczek) |

Role przypisuje się w: `Azure Portal → Enterprise applications → zdrovena-api → Users and groups`.
Można przypisywać bezpośrednio do użytkowników lub do **grup Entra ID** — patrz sekcja [Zarządzanie dostępem](#zarządzanie-dostępem-entra-id) poniżej.

### Przykłady

```bash
BASE=https://<API_URL>/api

# lista plików
curl -H "Authorization: Bearer $TOKEN" "$BASE/files"

# lista z prefixem
curl -H "Authorization: Bearer $TOKEN" "$BASE/files?prefix=invoices/2026/"

# pobranie pliku
curl -H "Authorization: Bearer $TOKEN" "$BASE/files/invoices/2026/04/faktura-001.pdf" -o faktura.pdf

# wgranie pliku (wymaga roli accountant+)
curl -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/pdf" \
  --data-binary @faktura.pdf \
  "$BASE/files/invoices/2026/04/faktura-001.pdf"

# zamknięcie miesiąca (wymaga roli accountant+)
curl -X POST -H "Authorization: Bearer $TOKEN" "$BASE/close"

# lista projektów wysyłki
curl -H "Authorization: Bearer $TOKEN" "$BASE/shipping/drafts"

# lista nieudanych draftów (DLQ)
curl -H "Authorization: Bearer $TOKEN" "$BASE/shipping/drafts/dlq"

# health check (bez tokenu, na roocie — nie pod /api)
curl "https://<API_URL>/health"
# → {"status": "ok", "version": "2.0.0"}

# status środowiska i integracji (wymaga roli viewer+)
curl -H "Authorization: Bearer $TOKEN" "$BASE/integrations/health"
```

### Uruchomienie lokalne

Najkrótsza ścieżka uruchamia Azurite, API i fake providerów. Frontend działa
natywnie przez Vite:

```bash
cp .env.local.template .env.local
docker compose up --build

# osobny terminal
cd frontend
npm ci
npm run dev
```

API: `http://localhost:8000`, fake providerzy: `http://localhost:9009`,
frontend: adres wypisany przez Vite (domyślnie `http://localhost:5173`).

Uruchomienie bez Compose:

```bash
pip install -e '.[api,cloud,all,dev]'

# bez Azure (dev/testy)
AZURE_AUTH_DISABLED=true uvicorn zdrovena.api.main:app --reload

# z lokalnym storage (Azurite)
AZURE_STORAGE_CONNECTION_STRING="UseDevelopmentStorage=true" \
AZURE_AUTH_DISABLED=true \
uvicorn zdrovena.api.main:app --reload

# z mockowanym kurierem (nie woła InPost/Allegro API)
MOCK_COURIER=true AZURE_AUTH_DISABLED=true uvicorn zdrovena.api.main:app --reload
```

Swagger UI dostępne pod `http://localhost:8000/docs` (na roocie, nie pod `/api`).

### Fake providerzy HTTP

Do bezpiecznych testów integracji uruchom fake provider service:

```bash
uvicorn zdrovena.fake_providers.app:app --port 9009
```

Następnie skieruj realnych klientów aplikacji na fake HTTP endpointy:

```bash
PROVIDER_MODE=fake
ALLEGRO_CLIENT_ID=fake
ALLEGRO_CLIENT_SECRET=fake
ALLEGRO_REFRESH_TOKEN=fake
ALLEGRO_BASE_URL=http://localhost:9009/allegro
ALLEGRO_AUTH_URL=http://localhost:9009/allegro/auth/oauth/token
INPOST_API_TOKEN=fake
INPOST_ORGANIZATION_ID=fake
INPOST_BASE_URL=http://localhost:9009/inpost
APACZKA_APP_ID=fake
APACZKA_APP_SECRET=fake
APACZKA_BASE_URL=http://localhost:9009/apaczka/api/v2
FAKTUROWNIA_BASE_URL=http://localhost:9009/fakturownia
FAKTUROWNIA_API_TOKEN=fake
```

Reset stanu i scenariusze awarii:

```bash
curl -X POST http://localhost:9009/__fake__/reset
curl -X POST http://localhost:9009/__fake__/scenario \
  -H 'Content-Type: application/json' \
  -d '{"provider":"inpost","operation":"get_label","mode":"label_not_ready"}'
```

W `APP_ENV=staging` aplikacja wymaga `PROVIDER_MODE=fake` i odmawia startu, jeśli którykolwiek provider write endpoint wskazuje na znany live host.

### Status integracji

Widok Ustawienia pobiera `/api/integrations/health` i pokazuje bieżące środowisko,
storage, Key Vault oraz konfigurację integracji Shopify, Fakturownia, Allegro,
InPost i Apaczka. Endpoint nie wykonuje zapisów ani live-calli do dostawców i nie
zwraca wartości sekretów; status wynika z trybu środowiska, obecności wymaganych
zmiennych oraz konfiguracji Key Vault. Każdy wynik zawiera `checked_at`,
`latency_ms`, tryb, bezpieczną wykonaną operację i publiczny komunikat.

Ręczne wymuszenie pełniejszych checków jest zarezerwowane dla administratorów:

```bash
curl -H "Authorization: Bearer $TOKEN" "$BASE/integrations/health?run_checks=true"
```

Sekcja procesów operacyjnych pokazuje ostatnie znane podsumowania tam, gdzie
system je persystuje. Brak persystencji jest raportowany jawnie jako
`not_configured`, bez zgadywania danych operacyjnych.

---

## Frontend

React + Vite + MSAL.js — logowanie przez Microsoft (Entra ID).

### Uruchomienie lokalne

```bash
# opcja A — Vite dev server
cd frontend
npm ci
npm run dev

# opcja B — SWA CLI z pełnym proxy do API
npm install -g @azure/static-web-apps-cli
AZURE_AUTH_DISABLED=true uvicorn zdrovena.api.main:app --port 8000 &
(cd frontend && npm ci && npm run build)
swa start frontend/dist --api-location http://localhost:8000
# → http://localhost:4280
```

### Kontrakty API

Kontrakty frontendu są generowane z FastAPI OpenAPI schema. Nie edytuj ręcznie plików `contracts/openapi.json` ani `frontend/src/api/generated/schema.d.ts`.

```bash
scripts/generate-api-contracts.sh
```

CI uruchamia drift check. Jeżeli backend schema zmieni się bez zaktualizowanych kontraktów, napraw to tym samym poleceniem i commituj wygenerowane pliki.

### Testy frontendu

Frontend używa Vitest, React Testing Library i jsdom. Testy komponentów powinny sprawdzać zachowanie widoczne dla użytkownika, a zależności API mockować na granicy HTTP (`fetch`).

```bash
cd frontend
npm ci
npm test
npm run lint
npm run build
```

### Wersjonowanie

Przy każdym deploy frontend pobiera `/version.json` i `/health`, porównuje major version. Niezgodność = żółty banner informacyjny.

---

## Zarządzanie dostępem (Entra ID)

### Jak to działa

Aplikacja używa **App Roles** w Azure Entra ID. Rola trafia do tokenu JWT w claimage `roles` i API sprawdza ją przy każdym żądaniu. Role można przypisywać:

- **bezpośrednio do użytkownika** — OK dla 1–2 osób, ale przy większej liczbie trudno zarządzać
- **do grupy bezpieczeństwa (Security Group)** — zalecane: dodajesz osobę do grupy i automatycznie dostaje odpowiednie uprawnienia

### Aktualna konfiguracja

| Rola | Gdzie przypisana |
|------|-----------------|
| `zdrovena-admin` | Bezpośrednio do właściciela |
| `zdrovena-accountant` | Bezpośrednio do księgowej |
| `zdrovena-shipment-mgr` | — (nowa rola, brak przypisań) |

Grupy bezpieczeństwa: **brak skonfigurowanych** — wszystkie przypisania są bezpośrednie.

> **Uwaga:** Przypisanie grupy do App Role wymaga licencji **Entra ID P1** (lub P2).
> Bez licencji P1 w oknie "Users and groups" widzisz tylko opcję dodania użytkownika, nie grupy.

### Jak stworzyć grupę

1. Otwórz: **Azure Portal → Entra ID → Groups → New group**
2. Wypełnij:
   - **Group type**: `Security`
   - **Group name**: np. `zdrovena-shipping-managers`
   - **Group description**: np. `Dostęp do zarządzania wysyłkami`
   - **Membership type**: `Assigned` (ręczne dodawanie) lub `Dynamic User` (automatyczne wg reguł)
3. W sekcji **Members** dodaj od razu pierwszych użytkowników (opcjonalne — można później)
4. Kliknij **Create**

### Jak dodać użytkownika do grupy

**Opcja A — przez stronę grupy:**

1. **Entra ID → Groups** → kliknij grupę
2. **Members → Add members** → wyszukaj użytkownika → **Select**

**Opcja B — przez stronę użytkownika:**

1. **Entra ID → Users** → kliknij użytkownika
2. **Groups → Add memberships** → wybierz grupę → **Select**

**Azure CLI:**
```bash
# znajdź object ID użytkownika
az ad user show --id email@domena.pl --query id -o tsv

# znajdź object ID grupy
az ad group show --group zdrovena-shipping-managers --query id -o tsv

# dodaj użytkownika do grupy
az ad group member add \
  --group zdrovena-shipping-managers \
  --member-id <object-id-usera>
```

### Jak przypisać grupę do roli aplikacji

1. Otwórz: **Azure Portal → Entra ID → Enterprise applications → zdrovena-api**
2. **Users and groups → Add user/group**
3. Kliknij **None selected** pod Users and groups → wyszukaj **grupę** (np. `zdrovena-shipping-managers`) → **Select**
4. Kliknij **None selected** pod Select a role → wybierz rolę (np. `zdrovena-shipment-mgr`) → **Select**
5. **Assign**

Po przypisaniu: każdy członek grupy przy następnym logowaniu dostanie token z odpowiednią rolą.
Token jest cache'owany przez MSAL — żeby rola zadziałała od razu, użytkownik musi wylogować się i zalogować ponownie.

### Jak dodać nowego użytkownika (bez grup)

1. **Entra ID → Users → New user** (lub **Invite user** jeśli konto spoza organizacji)
2. Wypełnij dane; użytkownik dostaje e-mail z linkiem
3. **Enterprise applications → zdrovena-api → Users and groups → Add user/group**
4. Wybierz użytkownika + rolę → **Assign**

---

## Infrastruktura

| Komponent | Serwis Azure |
|-----------|-------------|
| REST API | Container Apps (`zdrovena-api-prod`, `zdrovena-api-staging`) |
| Frontend | Static Web Apps (`zdrovena-ui`) |
| Pliki | Blob Storage (prywatny kontener, RBAC) |
| Sekrety | Key Vault |
| Obrazy | Container Registry |
| Tożsamość | Entra ID (App Registration `zdrovena-api`, Managed Identity) |
| Monitoring | Application Insights (`zdrovena-ai`, workspace-based) + metric alerts |
| Logs | Log Analytics Workspace `zdrovena-law` — tabele `AppRequests`, `AppTraces`, `AppExceptions` |

### Architektura

```
┌─────────────────────────────────────────────────────────────────┐
│ Production Environment (polandcentral)                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐       ┌──────────────────┐                   │
│  │ Static Web   │──────▶│ Container App    │                   │
│  │ App (UI)     │ /api/*│ (api-prod)       │                   │
│  └──────────────┘       │ min=1, max=2     │                   │
│        │                └────────┬─────────┘                   │
│        │ cdn                     │                             │
│        ▼                         ▼                             │
│  ┌──────────────┐       ┌──────────────────┐                   │
│  │ Custom Domain│       │ Blob Storage     │                   │
│  │ (optional)   │       │ (files)          │                   │
│  └──────────────┘       └──────────────────┘                   │
│                                  │                             │
│                                  ▼                             │
│                         ┌──────────────────┐                   │
│                         │ Key Vault        │                   │
│                         │ (secrets)        │                   │
│                         └──────────────────┘                   │
├─────────────────────────────────────────────────────────────────┤
│ Staging Environment (same region, shared infra)                │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐   ┌──────────────────┐                   │
│  │ Container App    │   │ Blob Storage     │                   │
│  │ (api-staging)    │──▶│ (files_staging)  │                   │
│  │ min=0, max=1     │   └──────────────────┘                   │
│  └──────────────────┘                                          │
├─────────────────────────────────────────────────────────────────┤
│ Shared Infrastructure (cost optimization)                      │
├─────────────────────────────────────────────────────────────────┤
│  • Container Registry (ACR Basic)                              │
│  • Container App Environment                                   │
│  • Log Analytics Workspace                                     │
│  • Key Vault (shared secrets for both envs)                    │
│  • Application Insights (unified monitoring)                   │
│  • GitHub Actions Identity (OIDC)                              │
└─────────────────────────────────────────────────────────────────┘
```

**Kluczowe decyzje architektoniczne:**
- **Shared infrastructure** — pojedyncze ACR, KV, LAW, CAE dla obu środowisk = niższe koszty (~$10-15/mies)
- **Dostępność produkcji** — API produkcyjne ma `min_replicas=1`, `max_replicas=2`; staging i fake providerzy mają `min_replicas=0`, `max_replicas=1`
- **Managed Identity wszędzie** — zero secrets w env vars/kodzie, pełen RBAC
- **Storage isolation** — osobne kontenery dla prod (`files`) i staging (`files_staging`)
- **Network security** — obecne środowisko działa bez VNet; RBAC, wyłączone shared keys i TLS są podstawową barierą
- **Sieć prywatna jest opt-in** — `enable_private_network=false` domyślnie. Włączenie tworzy VNet, subnet CAE, Private Endpoints i prywatne strefy DNS dla Storage/Key Vault; nie jest to stan aktualnej produkcji. ACR pozostaje publiczny na SKU Basic z Managed Identity

### Monitoring — Log Analytics queries (KQL)

App Insights (`zdrovena-ai`) jest w trybie **workspace-based** — dane trafiają do LAW `zdrovena-law`.

**Dwie ścieżki do zapytań KQL:**
- **App Insights → Logs**: tabele bez prefixu: `requests`, `traces`, `exceptions`, `dependencies`
- **LAW → Logs**: te same dane, ale tabele z prefixem `App`: `AppRequests`, `AppTraces`, `AppExceptions`, `AppDependencies`

Poniższe zapytania używają składni **LAW** (`App*`).
Otwórz: **Azure Portal → Log Analytics Workspace → `zdrovena-law` → Logs**
_(lub App Insights → Logs i zamień `AppRequests` → `requests` etc.)_

#### Stan telemetrii po audycie

Pierwszy audyt wykazał puste `AppRequests`, `unknown_service` w `AppRoleName`
oraz dużo logów Azure SDK/exportera. Po poprawce instrumentacji nowe requesty są
widoczne w `AppRequests`, a nowe rekordy mają role `zdrovena-api-prod`,
`zdrovena-api-staging` lub `zdrovena-allegro-poller`. Starsze rekordy
`unknown_service` pozostają w retencji i nie są przepisywane.

Szum z loggerów `azure.*` i `azure.monitor.opentelemetry.*` jest ograniczony,
ale nadal może występować i powinien być odfiltrowany przy analizie logów
aplikacyjnych. Dalsza redukcja szumu jest śledzona w issue
[#213](https://github.com/PiotrGry/zdrovena-reconciliation/issues/213).

```kql
union withsource=SourceTable AppRequests, AppTraces, AppDependencies, AppExceptions
| where TimeGenerated > ago(24h)
| summarize Records = count(), Latest = max(TimeGenerated)
    by SourceTable, AppRoleName
| order by SourceTable asc, Records desc
```

#### Ostatnie błędy i wyjątki

```kql
AppExceptions
| where TimeGenerated > ago(24h)
| project TimeGenerated, AppRoleName, ExceptionType, OuterMessage,
          SeverityLevel, OperationName, OperationId
| order by TimeGenerated desc
```

#### Requesty zakończone błędem (5xx)

```kql
AppRequests
| where TimeGenerated > ago(24h)
| where toint(ResultCode) between (500 .. 599)
| project TimeGenerated, AppRoleName, Name, ResultCode, DurationMs,
          OperationId, Url
| order by TimeGenerated desc
```

#### Najwolniejsze requesty (p95 ostatnie 6h)

```kql
AppRequests
| where TimeGenerated > ago(6h)
| summarize Requests = count(), P95Ms = percentile(DurationMs, 95)
    by AppRoleName, Name
| where Requests > 5
| order by P95Ms desc
| take 20
```

#### Zdarzenia biznesowe

Zdarzenia z loggera `zdrovena.events` są JSON-em w `Message`. Nie używaj
wyszukiwania tekstowego, jeżeli można odpytać pole `event`.

```kql
AppTraces
| where TimeGenerated > ago(7d)
| extend Payload = parse_json(Message)
| extend Event = tostring(Payload.event)
| where isnotempty(Event)
| summarize Events = count() by AppRoleName, Event
| order by Events desc
```

#### Wynik synchronizacji Allegro i Shopify

```kql
AppTraces
| where TimeGenerated > ago(7d)
| extend Payload = parse_json(Message)
| where tostring(Payload.event) == "sync.completed"
| project TimeGenerated, AppRoleName, OperationId,
          Allegro = tostring(Payload.allegro),
          Shopify = tostring(Payload.shopify)
| order by TimeGenerated desc
```

#### Drafty wymagające ręcznej weryfikacji

```kql
AppTraces
| where TimeGenerated > ago(7d)
| extend Payload = parse_json(Message)
| where tostring(Payload.event) in ("draft.created", "draft.updated_from_sync")
| where tostring(Payload.status) == "needs_review"
| project TimeGenerated, AppRoleName, OperationId,
          DraftId = tostring(Payload.draft_id),
          OrderNumber = tostring(Payload.order_number),
          Source = tostring(Payload.source)
| order by TimeGenerated desc
```

#### Nowe wpisy w Dead Letter Queue

```kql
AppTraces
| where TimeGenerated > ago(7d)
| extend Payload = parse_json(Message)
| where tostring(Payload.event) == "dlq.enqueued"
| project TimeGenerated, AppRoleName, OperationId,
          EntryId = tostring(Payload.entry_id),
          Source = tostring(Payload.source),
          ErrorType = tostring(Payload.error_type),
          TestProbe = tobool(Payload.test_probe)
| order by TimeGenerated desc
```

#### Błędy zależności zewnętrznych

W schemacie LAW typ zależności znajduje się w `DependencyType` (nie w `Type`).

```kql
AppDependencies
| where TimeGenerated > ago(24h)
| where Success == false
| project TimeGenerated, AppRoleName, DependencyType, Target, Name,
          ResultCode, DurationMs, OperationId
| order by TimeGenerated desc
```

#### Logi zamknięcia miesiąca bez szumu Azure SDK

Nazwa loggera trafia do `Properties["logger_name"]`.

```kql
AppTraces
| where TimeGenerated > ago(7d)
| extend LoggerName = tostring(Properties["logger_name"])
| where LoggerName startswith "zdrovena.month_closing"
| project TimeGenerated, AppRoleName, LoggerName, SeverityLevel,
          Message, OperationId
| order by TimeGenerated desc
```

#### Error rate per endpoint

```kql
AppRequests
| where TimeGenerated > ago(24h)
| summarize Total = count(), Failed = countif(Success == false)
    by AppRoleName, Name
| extend ErrorPct = round(100.0 * Failed / Total, 1)
| where Total > 3
| order by ErrorPct desc
```

#### Jawne pokazanie szumu SDK/exportera

```kql
AppTraces
| where TimeGenerated > ago(24h)
| extend LoggerName = tostring(Properties["logger_name"])
| where LoggerName startswith "azure."
    or LoggerName startswith "opentelemetry."
| summarize Records = count() by AppRoleName, LoggerName
| order by Records desc
```

#### Alerty i znane luki

Terraform definiuje trzy alerty produkcyjne: liczbę nieudanych requestów,
średni czas odpowiedzi oraz `dlq.enqueued`. Wszystkie są filtrowane do
`zdrovena-api-prod` i wysyłają do jednego action group. Staging współdzieli
Application Insights, ale nie powinien uruchamiać alertów produkcyjnych.

Nie ma jeszcze alertów na błędy synchronizacji, błędy zależności, drafty
`needs_review`, brak przebiegów pollera ani spadek wolumenu zdarzeń biznesowych.
Rozszerzenie alertów jest śledzone w issue
[#214](https://github.com/PiotrGry/zdrovena-reconciliation/issues/214).
Procedura testowania alertów, staging probes i audytu danych wrażliwych znajduje
się w [`infra/terraform/MONITORING_RUNBOOK.md`](infra/terraform/MONITORING_RUNBOOK.md).

### CI/CD pipeline

Obowiązujący workflow zmian:

1. Utwórz lub wybierz issue z zakresem i kryteriami akceptacji.
2. Utwórz krótko żyjący branch z aktualnego `develop`, np.
   `feature/123-opis`, `fix/123-opis` albo `codex/123-opis`.
3. Otwórz PR z brancha do `develop`. Aktywny ruleset wymaga PR, aktualnej bazy
   oraz statusu `Fast gate / Quality Gate`; nie wymaga obecnie akceptacji drugiej
   osoby.
4. Po zebraniu releasu otwórz osobny PR `develop → main`. Ruleset `main`
   wymaga PR i statusu `CI Gate`. `pr-validate.yml` dodatkowo odrzuca inne źródło
   niż `develop`, poza `hotfix/*` z etykietą `hotfix-approved`.
5. Merge do `main` uruchamia wdrożenie tylko wtedy, gdy zmieniono ścieżki
   aplikacyjne wskazane w `prod-deploy.yml`. Zmiana wyłącznie dokumentacji nie
   wdraża produkcji.

```text
issue → branch z develop → PR do develop → PR develop do main → produkcja
             │                    │                  │
             │                    │                  └─ CI Gate + staging smoke/E2E
             │                    └─ Fast gate / Quality Gate
             └─ opcjonalny lokalny pre-push hook
```

| Workflow | Rzeczywisty trigger | Co wykonuje |
|----------|---------------------|-------------|
| `develop-gate.yml` | `push` do `develop`, PR do `develop`, ręcznie | `_quality-gate.yml`; bez deployu i bez staging smoke |
| `pr-validate.yml` | PR do `main` (`opened`, `synchronize`, `reopened`, zmiany etykiet, `ready_for_review`), ręcznie | kontrola źródła releasu, quality gate, `_full-test-suite.yml` ze staging smoke/E2E, zbiorczy `CI Gate` |
| `prod-deploy.yml` | `push` do `main` dla zmian backendu, frontendu, skryptów lub workflowu deploy; ręcznie | promocja/budowa obrazu, deploy prod i pollera, smoke, link SWA, frontend, auto-rollback, GitHub Release |
| `terraform.yml` | PR do `develop`/`main` dotykający Terraform, `push` do `main` dotykający Terraform, ręcznie | fmt/validate/plan; na `main` osobny apply po ręcznej akceptacji |
| `back-sync-main.yml` | każdy `push` do `main`, ręcznie; zakończenie `Develop — Fast Gate` | tworzy PR z merge commitem `main → develop`, uruchamia wymagany fast gate i scala po weryfikacji pochodzenia |

**Zabezpieczenia bramki:**
- PR do `develop` musi przejść lint, typy, testy z pokryciem ≥80% oraz skany bezpieczeństwa odpowiednie dla zmienionych obszarów
- PR `develop → main` ponownie uruchamia quality gate oraz pełny staging smoke/E2E; to celowe dwie bramki na dwóch poziomach ryzyka
- Force-push i usuwanie `develop`/`main` blokują aktywne rulesety; wymagane statusy nie mają bypass actorów
- Po każdym merge do `main` automatyczny back-sync przenosi main-only merge commit do `develop`, dzięki czemu następny release PR nie wymaga ręcznego `Update branch`
- Deploy aplikacji po merge do `main` jest automatyczny i nie ma manual approval
- Post-deploy smoke (3× retry) z auto-rollbackiem do poprzedniej rewizji Container App
- `prod-health.yml` — cron co 5 min sprawdza `/health`; powiadomienie webhook przy błędzie

Lokalny hook nie jest instalowany automatycznie przez GitHub. Włącz go raz:

```bash
bash scripts/install-hooks.sh
```

Hook uruchamia `scripts/check.sh` przed `git push` i może pominąć kosztowne testy,
gdy zmieniła się wyłącznie dokumentacja lub infrastruktura. `--no-verify`
pomija tylko lokalny hook — nigdy wymagane checki GitHub.

**Auto-rollback:**
Przy awarii smoke po deployu — `az containerapp revision activate` poprzednia rewizja,
`az containerapp revision deactivate` nowa rewizja. Czas rollbacku ~15–30 s.

**Terraform apply:** PR-y wykonują tylko plan. Po merge zmian Terraform do
`main` workflow tworzy plan, a job `Apply` czeka na akceptację właściciela
w issue utworzonym przez `manual-approval`. Obecnie apply ponownie oblicza plan
z aktualnego `main`, zamiast używać artefaktu `tfplan`; utwardzenie tego procesu
jest śledzone w issue
[#138](https://github.com/PiotrGry/zdrovena-reconciliation/issues/138).

### Planowane serwisy (rozwój)

#### **Faza 1: Persistence layer (Q2 2026)**
- **PostgreSQL Flexible Server** — transakcyjne dane, historia faktur, audyt trail
  - SKU: Burstable B1ms (1 vCore, 2 GiB) — ~$15/mies
  - Passwordless auth via Managed Identity
  - Backup retention: 7 dni
  - Use cases: faktury cache, user sessions, workflow state

#### **Faza 2: Performance optimization (Q3 2026)**
- **Azure Cache for Redis** — session store, rate limiting, hot data
  - SKU: Basic C0 (250 MB) — ~$17/mies
  - Use cases: JWT blacklist, invoice metadata cache, rate limiting

#### **Faza 3: Async workflows (Q3-Q4 2026)**
- **Service Bus** — kolejki dla długich operacji (KSeF fetch, PDF generation)
  - SKU: Basic — ~$0.05/milion operacji
  - Use cases: month-close pipeline steps, batch invoice processing

#### **Faza 4: Multi-region HA (2027)**
- **ACR Premium** — georeplikacja obrazów do westeurope (~$5/mies → ~$150/mies)
- **Traffic Manager** — DNS-based load balancing między regionami
- **Blob GRS** — geo-redundant storage (LRS → GRS: +50% kosztu)

#### **Faza 5: Enterprise security (2027+)**
- **Pełna prywatność ACR** — Premium SKU + Private Endpoint; Storage i Key Vault mają już opcjonalny tryb VNet/Private Endpoint w Terraform
- **Azure Front Door** — WAF + DDoS protection (~$35/mies + traffic)
- **Azure Policy** — governance (deny public endpoints, enforce tags)

**Szacowane koszty po pełnym rozwoju:** ~$250-300/mies (vs obecne ~$12-15/mies)

### Terraform — struktura plików

```
infra/terraform/
├── main.tf         — Core: Resource Group, ACR, Log Analytics, Container App Env
├── compute.tf      — Container Apps (prod, staging, fake providers) + Allegro poller
├── storage.tf      — Storage Account + blob containers
├── security.tf     — Key Vault, GitHub OIDC Identity, RBAC assignments
├── network.tf      — opcjonalny VNet, Private Endpoints, DNS i NSG
├── policy.tf       — opcjonalne Azure Policy dla trybu prywatnego
├── frontend.tf     — Static Web App + custom domain
├── monitoring.tf   — Application Insights + metric alerts
├── MONITORING_RUNBOOK.md — kontrolowane testy alertów i telemetrii
├── variables.tf    — Input variables
├── outputs.tf      — Output values
├── providers.tf    — Provider config (azurerm 4.2+)
└── modules/
    └── container_app/  — Reusable Container App module
```

**Lokalna walidacja i plan:**

```bash
cd infra/terraform
cp terraform.tfvars.template terraform.tfvars
# uzupełnij terraform.tfvars
terraform fmt -check -recursive
terraform init -backend-config=backend.hcl
terraform validate
terraform plan -out=tfplan
```

Produkcję stosuje wyłącznie `terraform.yml` po merge do `main` i ręcznej
akceptacji. Nie uruchamiaj `terraform apply` z laptopa bez obecności właściciela
i przeglądu zapisanego planu.

---

## Testy

```bash
pip install -e '.[api,cloud,all,dev]'
pytest                          # wszystkie testy
pytest --cov=zdrovena           # z pokryciem (próg ≥80%)
pytest tests/fitness/           # granice modułów
ruff check .                    # lint (blokuje CI)
ruff format --check .           # format check (blokuje CI)
pyright                         # typy (blokuje CI)
```

Pokrycie mierzalnego kodu biznesowego: ≥80% (twardo enforce’owane w `_quality-gate.yml`).

---

## Zależności opcjonalne

| Extra | Pakiety | Używane przez |
|-------|---------|---------------|
| `ksef` | cryptography, signxml, lxml | KSeF 2.0 e-invoicing (pipeline zamknięcia) |
| `pdf` | pypdf, pdf2image | Ekstrakcja dat z PDF faktur kosztowych |
| `report` | playwright, playwright-stealth | Canva downloader (raporty PDF) |
| `api` | fastapi, uvicorn, PyJWT, pypdf | REST API |
| `cloud` | azure-storage-blob, azure-identity, azure-keyvault-secrets, azure-data-tables, azure-monitor-opentelemetry | produkcja Azure |
| `all` | `[ksef,pdf,report]` — **bez `api`/`cloud`** | zbiorczy alias dla lokalnego pipeline zamknięcia |
| `dev` | pytest, pytest-cov, responses, httpx, pip-audit, bandit, hypothesis, ruff | testy + quality gate |
| `iac` | checkov | lokalny skan Terraform; oddzielony od `dev` ze względu na zależności przechodnie |

❌ **Uwaga:** `pip install -e '.[all]'` **nie** zainstaluje REST API ani zależności Azure. Do uruchomienia API użyj `.[api,cloud,all,dev]`.

---

### Struktura projektu

```
zdrovena/
├── __init__.py                     # wersja odpowiedzi API (2.0.0)
├── cli.py                          # CLI: zdrovena {close, audit, files, health, …}
├── common/
│   ├── client.py                   # bazowy HTTP client (retry + logging)
│   ├── fakturownia.py              # FakturowniaClient (REST API)
│   ├── storage.py                  # StorageService (Blob / lokalny fs)
│   ├── secrets.py                  # SecretsProvider (KV / env / lokalny)
│   ├── _keyvault.py                # Azure Key Vault client (fetch + rotate)
│   ├── config.py                   # ładowanie configu (env + KV)
│   ├── exceptions.py               # hierarchia wyjątków domenowych
│   ├── shipping_exceptions.py      # wyjątki wysyłkowe (InPost cancel, DLQ, …)
│   ├── shipping_store.py           # ShippingStore (Table Storage / lokalny JSON) + DLQ
│   ├── shipping_format.py          # formatowanie draftów wysyłki (UI + logi)
│   ├── shopify_dedup_store.py      # deduplikacja webhooków Shopify (idempotency)
│   ├── allegro.py                  # AllegroClient + AllegroTokenStore (P0-2)
│   ├── allegro_mapper.py           # mapping order → Allegro create-command (P0-1, P1-1, P1-2)
│   ├── inpost.py                   # InPostClient + PACZKOMAT_SLOTS + pick_paczkomat_template (P2-1)
│   ├── apaczka.py                  # ApaczkaClient
│   ├── bottles.py                  # audyt butelek (kaucja, saldo)
│   ├── retry.py                    # retry decorator + backoff
│   ├── sms_service.py              # SMS notifications (kurier ETA)
│   └── formatting.py               # ANSI, miesiące, to_decimal
├── api/
│   ├── main.py                     # FastAPI app + Azure Monitor setup
│   ├── observability.py            # OpenTelemetry i role usług
│   ├── damage_detection.py         # wykrywanie uszkodzonych przesyłek
│   ├── auth.py                     # JWT / Entra ID, app roles (admin/accountant/viewer/shipment-mgr)
│   ├── client.py                   # klient CLI→API (na Container App)
│   ├── deps.py                     # FastAPI dependencies (storage, shipping, secrets, auth)
│   ├── models.py                   # pydantic modele request/response
│   ├── commands/                   # CLI: `zdrovena files`, `zdrovena health`
│   └── routers/
│       ├── close.py                # /close + etapowy workflow, state i history
│       ├── files.py                # GET/PUT/DELETE /files
│       ├── integrations.py         # bezpieczny status konfiguracji integracji
│       ├── invoices.py             # GET /invoices/{sales,products}
│       ├── webhooks.py             # Shopify webhook + shipping drafts + DLQ (P0-P2 audit)
│       ├── damage.py               # obsługa uszkodzonych przesyłek
│       ├── monitoring_probe.py     # chronione staging probes (niedostępne na prod)
│       └── fakturownia_patcher.py  # patch klienta Fakturownia (fixup metadanych)
├── audit/                          # audyt butelek + raporty
│   ├── api.py                      # AuditClient (API zdrovena ↔ audyt)
│   ├── bottles.py                  # saldo kaucji, ruchy butelek
│   ├── report_downloader.py        # ściąganie raportów Playwright
│   ├── sections.py                 # sekcje raportu
│   └── commands/                   # CLI: `zdrovena {audit, list, export, summary, report, products}`
└── month_closing/
    ├── config.py                   # vendorzy, firma, cfg Zoho/KSeF
    ├── state.py                    # PipelineState (.state.json w blob)
    ├── orchestrator.py             # MonthCloseOrchestrator
    ├── preflight.py                # PreflightChecker
    ├── close_history.py            # historia zamknięć (Table Storage / lokalny JSON)
    ├── table_history.py            # Azure Table Storage adapter dla historii
    ├── zoho_mail.py                # Zoho Mail REST
    ├── ksef.py                     # KSeF 2.0 (opcjonalne zależności)
    ├── invoice_date_check.py       # ekstrakcja dat z PDF
    ├── fakturownia_reports.py      # generowanie raportów z Fakturowni
    ├── canva_downloader.py         # Playwright: faktury Canva
    ├── email_service.py            # Zoho SMTP
    ├── zip_service.py              # archiwum ZIP
    ├── console.py                  # rich console (progress + logi)
    └── commands/                   # CLI: `zdrovena {close, preflight, setup}`
frontend/                           # React + Vite + MSAL, testy Vitest
contracts/                          # generowany kontrakt OpenAPI
tests/                              # pytest (1550+ testów, coverage ≥80%)
scripts/                            # CI: quality gate, smoke, deploy helpers
infra/terraform/                    # infrastruktura Azure (modules + envs)
.github/workflows/                  # develop-gate, pr-validate, prod-deploy, terraform
```

---

## Licencja

Narzędzie wewnętrzne — Zdrovena / HUMIO sp. z o.o.
