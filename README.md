# zdrovena-reconciliation

Wewnętrzny system back-office dla **Zdrovena / HUMIO** — fakturowanie, audyt butelek, zamknięcie miesiąca.

Składa się z dwóch warstw:
- **REST API** (FastAPI) — serwowane przez Azure Container Apps, chronione JWT (Entra ID)
- **Frontend** (vanilla HTML + MSAL.js) — Azure Static Web Apps, proxy `/api/*` → Container App

---

## REST API

### Endpoints

| Method | Path | Rola | Opis |
|--------|------|------|------|
| `GET` | `/health` | — | Liveness check + wersja API |
| `GET` | `/docs` | — | Swagger UI (interaktywna dokumentacja) |
| `GET` | `/files` | viewer+ | Lista plików w storage (opcjonalny `?prefix=`) |
| `GET` | `/files/{key}` | viewer+ | Pobranie pliku (streaming) |
| `PUT` | `/files/{key}` | accountant+ | Wgranie pliku |
| `POST` | `/close` | accountant+ | Uruchomienie pipeline zamknięcia miesiąca |
| `GET` | `/shipping/drafts` | shipment-mgr+ | Lista projektów wysyłki |
| `POST` | `/shipping/drafts/{id}/execute` | shipment-mgr+ | Realizuj projekt (retry po błędzie) |
| `POST` | `/shipping/drafts/{id}/pickup` | shipment-mgr+ | Zamów podjazd kuriera InPost |
| `PATCH` | `/shipping/drafts/{id}` | shipment-mgr+ | Aktualizuj liczbę paczek |

### Autentykacja

Wszystkie endpointy (poza `/health` i `/docs`) wymagają `Authorization: Bearer <token>`.

Token pochodzi z Azure Entra ID — aplikacja `zdrovena-api` (App Registration).

```bash
# pobranie tokenu przez Azure CLI
TOKEN=$(az account get-access-token \
  --resource "api://<AZURE_API_CLIENT_ID>" \
  --query accessToken -o tsv)

curl -H "Authorization: Bearer $TOKEN" https://<API_URL>/files
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
BASE=https://<API_URL>

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

# health check
curl "$BASE/health"
# → {"status": "ok", "version": "2.0.0"}
```

### Uruchomienie lokalne

```bash
pip install -e '.[all,dev]'

# bez Azure (dev/testy)
AZURE_AUTH_DISABLED=true uvicorn zdrovena.api.main:app --reload

# z lokalnym storage (Azurite)
AZURE_STORAGE_CONNECTION_STRING="UseDevelopmentStorage=true" \
AZURE_AUTH_DISABLED=true \
uvicorn zdrovena.api.main:app --reload
```

Swagger UI dostępne pod `http://localhost:8000/docs`.

---

## Frontend

Vanilla HTML + MSAL.js — logowanie przez Microsoft (Entra ID), brak frameworka, brak bundlera.

### Uruchomienie lokalne

```bash
# opcja A — sam frontend (bez API proxy)
cd frontend && python3 -m http.server 3000

# opcja B — SWA CLI z pełnym proxy do API
npm install -g @azure/static-web-apps-cli
AZURE_AUTH_DISABLED=true uvicorn zdrovena.api.main:app --port 8000 &
swa start frontend --api-location http://localhost:8000
# → http://localhost:4280
```

### Wersjonowanie

Przy każdym deploy frontend pobiera `/version.json` i `/api/health`, porównuje major version. Niezgodność = żółty banner informacyjny.

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
│  └──────────────┘       │ min=0, max=2     │                   │
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
- **Scale-to-zero** — Container Apps z `min_replicas=0` → brak kosztów compute w idle
- **Managed Identity wszędzie** — zero secrets w env vars/kodzie, pełen RBAC
- **Storage isolation** — osobne kontenery dla prod (`files`) i staging (`files_staging`)
- **Network security** — RBAC jako główna bariera (shared_access_key_enabled=false, bypass=AzureServices)

### Monitoring — Log Analytics queries (KQL)

App Insights (`zdrovena-ai`) jest w trybie **workspace-based** — dane trafiają do LAW `zdrovena-law`.

**Dwie ścieżki do zapytań KQL:**
- **App Insights → Logs**: tabele bez prefixu: `requests`, `traces`, `exceptions`, `dependencies`
- **LAW → Logs**: te same dane, ale tabele z prefixem `App`: `AppRequests`, `AppTraces`, `AppExceptions`, `AppDependencies`

Poniższe zapytania używają składni **LAW** (`App*`).
Otwórz: **Azure Portal → Log Analytics Workspace → `zdrovena-law` → Logs**
_(lub App Insights → Logs i zamień `AppRequests` → `requests` etc.)_

#### Ostatnie błędy i wyjątki (ostatnia godzina)
```kql
AppExceptions
| where TimeGenerated > ago(1h)
| project TimeGenerated, ExceptionType, OuterMessage, SeverityLevel, OperationName, AppRoleInstance
| order by TimeGenerated desc
```

#### Requesty zakończone błędem (5xx)
```kql
AppRequests
| where TimeGenerated > ago(24h) and ResultCode >= 500
| project TimeGenerated, Name, ResultCode, DurationMs, OperationId, Url
| order by TimeGenerated desc
```

#### Najwolniejsze requesty (p95 ostatnie 6h)
```kql
AppRequests
| where TimeGenerated > ago(6h)
| summarize p95 = percentile(DurationMs, 95), count_ = count() by Name
| where count_ > 5
| order by p95 desc
| take 20
```

#### Logi z pipeline zamknięcia miesiąca
```kql
AppTraces
| where TimeGenerated > ago(7d)
| where Properties["logger"] startswith "zdrovena"
| where Message contains "Close" or Message contains "close"
| project TimeGenerated, Message, SeverityLevel, Properties["logger"]
| order by TimeGenerated desc
```

#### Error rate per endpoint (ostatnie 24h)
```kql
AppRequests
| where TimeGenerated > ago(24h)
| summarize
    total = count(),
    failed = countif(Success == false)
  by Name
| extend error_pct = round(100.0 * failed / total, 1)
| where total > 3
| order by error_pct desc
```

#### Dependency calls — Storage i Key Vault
```kql
AppDependencies
| where TimeGenerated > ago(1h)
| where Type in ("Azure blob", "HTTP")
| project TimeGenerated, Name, Type, DurationMs, Success, ResultCode
| order by DurationMs desc
| take 50
```

#### Performance counters (CPU, pamięć)
```kql
AppPerformanceCounters
| where TimeGenerated > ago(1h)
| project TimeGenerated, Name, Value, AppRoleInstance
| order by TimeGenerated desc
| take 50
```

#### Alert: czy alerty były wyzwolone?
```kql
AzureActivity
| where TimeGenerated > ago(7d)
| where OperationNameValue == "microsoft.insights/alertrules/activated/action"
| project TimeGenerated, ResourceGroup, Description = tostring(Properties)
| order by TimeGenerated desc
```

### CI/CD pipeline

```
  push → develop
       │
       ▼
  develop-gate.yml          ← quality gate + full test suite (staging deploy)
  ├── _quality-gate.yml     ← ruff · pyright · pytest ≥80% · bandit · trivy · gitleaks
  └── _full-test-suite.yml  ← build Docker → push to ACR → deploy staging → smoke tests
                                └── post-deploy: auto-rollback on smoke failure

  PR develop → main
       │
       ▼
  pr-validate.yml           ← quality gate only (~1 min); full suite ran at develop-gate stage
  └── _quality-gate.yml

  merge → main
       │
       ▼
  main-gate.yml
  └── _deploy.yml           ← promote staging image → deploy prod → post-deploy verify
       ├── promote-image.sh  ← re-tag staging-{sha} as latest
       ├── deploy-prod       ← az containerapp update --image
       ├── post-deploy-verify← smoke 3× retry; auto-rollback on failure + webhook notify
       ├── deploy-frontend   ← Vite build → SWA upload (parallel to backend)
       └── release           ← gh release create vYYYY.MM.DD-{sha::7}
```

**Zabezpieczenia bramki:**
- Każdy commit do `develop` musi przejść: lint + typy + testy ≥80% + security scan + staging smoke
- PR do `main` = quality gate (~1 min); staging deploy był już wykonany przy commit do `develop`
- Merge do `main` = automatyczny deploy produkcyjny bez manual approval
- Post-deploy smoke (3× retry) z auto-rollbackiem do poprzedniej rewizji Container App
- `prod-health.yml` — cron co 5 min sprawdza `/health`; powiadomienie webhook przy błędzie

**Auto-rollback:**
Przy awarii smoke po deployu — `az containerapp revision activate` poprzednia rewizja,
`az containerapp revision deactivate` nowa rewizja. Czas rollbacku ~15–30 s.

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
- **VNet + Private Endpoints** — izolacja sieciowa Storage/KV/DB (~$15/mies per endpoint)
- **Azure Front Door** — WAF + DDoS protection (~$35/mies + traffic)
- **Azure Policy** — governance (deny public endpoints, enforce tags)

**Szacowane koszty po pełnym rozwoju:** ~$250-300/mies (vs obecne ~$12-15/mies)

### Terraform — struktura plików

```
infra/terraform/
├── main.tf         — Core: Resource Group, ACR, Log Analytics, Container App Env
├── compute.tf      — Container Apps (api_prod, api_staging modules)
├── storage.tf      — Storage Account + blob containers
├── security.tf     — Key Vault, GitHub OIDC Identity, RBAC assignments
├── frontend.tf     — Static Web App + custom domain
├── monitoring.tf   — Application Insights + metric alerts
├── variables.tf    — Input variables
├── outputs.tf      — Output values
├── providers.tf    — Provider config (azurerm 4.2+)
└── modules/
    └── container_app/  — Reusable Container App module
```

**Deployment:**
```bash
cd infra/terraform
cp terraform.tfvars.template terraform.tfvars
# uzupełnij terraform.tfvars
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

---

## Testy

```bash
pip install -e '.[all,dev]'
pytest                          # wszystkie testy
pytest --cov=zdrovena           # z pokryciem
pytest tests/fitness/           # granice modułów
```

Pokrycie mierzalnego kodu biznesowego: ≥80%.

---

## Zależności opcjonalne

| Extra | Pakiety | Używane przez |
|-------|---------|---------------|
| `ksef` | cryptography, signxml, lxml | KSeF 2.0 e-invoicing (pipeline zamknięcia) |
| `pdf` | pypdf, pdf2image | Ekstrakcja dat z PDF faktur kosztowych |
| `api` | fastapi, uvicorn, PyJWT, pypdf | REST API |
| `cloud` | azure-storage-blob, azure-identity, azure-data-tables, … | produkcja Azure |
| `dev` | pytest, pytest-cov, responses | testy |

---

### Struktura projektu

```
zdrovena/
├── __init__.py                     # wersja pakietu
├── common/
│   ├── client.py                   # FakturowniaClient
│   ├── storage.py                  # StorageService (Blob / lokalny fs)
│   ├── shipping_store.py           # ShippingStore (Table Storage / lokalny JSON)
│   ├── exceptions.py               # hierarchia wyjątków
│   └── formatting.py               # ANSI, miesiące, to_decimal
├── api/
│   ├── main.py                     # FastAPI app + Azure Monitor setup
│   ├── auth.py                     # JWT / Entra ID, app roles
│   ├── deps.py                     # FastAPI dependencies (storage, shipping, auth)
│   ├── models.py                   # CloseRequest, CloseResponse
│   └── routers/
│       ├── close.py                # POST /close
│       ├── files.py                # GET/PUT /files
│       ├── invoices.py             # GET /invoices
│       └── webhooks.py             # Shopify webhook + shipping drafts
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
    ├── canva_downloader.py         # Playwright: faktury Canva
    ├── email_service.py            # Zoho SMTP
    └── zip_service.py              # archiwum ZIP
tests/                              # pytest
scripts/                            # CI: quality gate, smoke
infra/terraform/                    # infrastruktura Azure
.github/workflows/                  # CI/CD pipelines
```

---

## Licencja

Narzędzie wewnętrzne — Zdrovena / HUMIO sp. z o.o.
