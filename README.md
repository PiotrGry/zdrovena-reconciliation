# zdrovena-reconciliation

Wewnętrzny system back-office dla **Zdrovena / HUMIO** — fakturowanie, audyt butelek, zamknięcie miesiąca.

Składa się z trzech warstw:
- **REST API** (FastAPI) — serwowane przez Azure Container Apps, chronione JWT (Entra ID)
- **Frontend** (vanilla HTML + MSAL.js) — Azure Static Web Apps, proxy `/api/*` → Container App
- **CLI** — lokalne narzędzia dla właściciela firmy

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

Role przypisuje się w: `Azure Portal → Enterprise applications → zdrovena-api → Users and groups`.

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

## CLI

```bash
pip install -e '.[all]'
playwright install chromium

zdrovena --version                        # 2.0.0
zdrovena -y 2025 audit                    # pełny audyt FV vs WZ
zdrovena -y 2025 -m 6 list               # faktury z czerwca
zdrovena -y 2025 export                   # CSV per miesiąc
zdrovena -y 2025 summary                  # WZ vs FV (plastik/szkło)
zdrovena products --active-only           # aktywne produkty

zdrovena -y 2025 -m 2 report              # Wykaz sprzedaży VAT → PDF
zdrovena -y 2025 -m 2 report -k expenses  # raport kosztów

zdrovena close 2025-06                    # zamknięcie miesiąca
zdrovena close 2025-06 --dry-run          # symulacja
zdrovena close 2025-06 --zip --send       # ZIP + wysyłka

zdrovena setup                            # wizard credentiali
zdrovena setup --check                    # sprawdź co skonfigurowane
```

### Komendy

| Komenda | Opis |
|---------|------|
| `audit` | Pełna rekoncyliacja WZ ↔ FV z kontrolami §2/§7/§8/§10 |
| `list` | Lista faktur sprzedaży z liczbą butelek |
| `export` | Export pozycji butelek do CSV per miesiąc |
| `summary` | Tabela: WZ wysłane vs FV zafakturowane (plastik/szkło) |
| `products` | Lista produktów Fakturownia (`--active-only`) |
| `report` | Pobranie raportów Fakturownia jako PDF (Playwright) |
| `close` | Pipeline zamknięcia miesiąca — preflight → faktury → KSeF → ZIP → e-mail |
| `setup` | Wizard credentiali i OAuth (`--check`, `zoho`, `gads`) |

### Pipeline zamknięcia miesiąca (`zdrovena close`)

| # | Krok | Źródło |
|---|------|--------|
| 0 | Pre-flight — weryfikacja vendorów, wyciągu, raportów | Zoho Mail, lokalny fs |
| 1 | Tworzenie struktury folderów | — |
| 2 | Pobieranie faktur sprzedaży | Fakturownia API |
| 3 | Pobieranie JPK / raportów VAT | Fakturownia API |
| 4 | Pobieranie faktur kosztowych | KSeF → Fakturownia → Zoho Mail |
| 5 | Weryfikacja wyciągu bankowego | lokalny fs |
| 6 | Budowanie archiwum ZIP | — |
| 7 | Wysyłka e-mail do księgowej | Zoho SMTP |

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
| Monitoring | Application Insights + metric alerts |
| Logs | Log Analytics Workspace |

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

### GitHub Secrets (wymagane)

| Secret | Opis |
|--------|------|
| `AZURE_CLIENT_ID` | Client ID SP `zdrovena-github-actions` (OIDC login) |
| `AZURE_TENANT_ID` | ID tenanta Entra ID |
| `AZURE_SUBSCRIPTION_ID` | ID subskrypcji Azure |
| `AZURE_API_CLIENT_ID` | Client ID App Registration `zdrovena-api` (JWT audience) |
| `ACR_LOGIN_SERVER` | URL Container Registry |
| `SWA_DEPLOYMENT_TOKEN` | Token deploymentu Static Web Apps |

### Terraform

```bash
cd infra/terraform
cp terraform.tfvars.template terraform.tfvars
# uzupełnij terraform.tfvars
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

---

## Sekrety CLI (Keychain)

Wszystkie sekrety CLI przechowywane przez `keyring` (macOS Keychain). Konto: `humio`.

| Keychain | Co | Jak uzyskać |
|----------|----|------------|
| `fakturownia_api_token` | Token API Fakturownia | zdrovena.fakturownia.pl → Ustawienia → API |
| `fakturownia_login` | Login webowy Fakturownia | e-mail loginu do Fakturownia UI |
| `fakturownia_password` | Hasło webowe Fakturownia | hasło do Fakturownia UI |
| `zoho_smtp_password` | Hasło SMTP Zoho | hasło konta Zoho |
| `zoho_client_id` | Zoho OAuth Client ID | api-console.zoho.eu → Self Client |
| `zoho_client_secret` | Zoho OAuth Client Secret | api-console.zoho.eu → Self Client |
| `zoho_refresh_token` | Zoho OAuth Refresh Token | `zdrovena setup zoho` |

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
| `ksef` | cryptography, signxml, lxml | KSeF 2.0 e-invoicing |
| `pdf` | pypdf, pdf2image | Ekstrakcja dat z PDF |
| `report` | playwright, playwright-stealth | Pobieranie raportów i Canva |
| `all` | ksef + pdf + report | wszystko |
| `dev` | pytest, pytest-cov, responses | testy |

---

## Licencja

Narzędzie wewnętrzne — Zdrovena / HUMIO sp. z o.o.


```bash
pip install -e '.[all]'
playwright install chromium
```

## Quick start

```bash
zdrovena --version                        # 2.0.0
zdrovena -y 2025 audit                    # pełny audyt FV vs WZ
zdrovena -y 2025 -m 6 list               # faktury z czerwca
zdrovena -y 2025 export                   # CSV per miesiąc
zdrovena -y 2025 summary                  # WZ vs FV (plastik/szkło)
zdrovena products --active-only           # aktywne produkty

zdrovena -y 2025 -m 2 report              # Wykaz sprzedaży VAT → PDF
zdrovena -y 2025 -m 2 report -k expenses  # raport kosztów

zdrovena close 2025-06                    # zamknięcie miesiąca
zdrovena close 2025-06 --dry-run          # symulacja
zdrovena close 2025-06 --zip --send       # ZIP + wysyłka

zdrovena setup                            # wizard credentiali
zdrovena setup --check                    # sprawdź co skonfigurowane
```

## Commands

| Command    | Description |
|------------|-------------|
| `audit`    | Full WZ ↔ FV reconciliation with §2/§7/§8/§10 checks, PASSED / FAILED verdict |
| `list`     | List sales invoices with bottle counts |
| `export`   | Export bottle line-items to monthly CSV files |
| `summary`  | Summary table: WZ dispatched vs FV invoiced (plastic / glass) |
| `products` | List Fakturownia products (with `--active-only`) |
| `report`   | Download Fakturownia reports as PDF (VAT sales, income, expenses, etc.) |
| `close`    | Month-close pipeline — preflight → invoices → KSeF → ZIP → e-mail |
| `setup`    | Keychain & OAuth credential wizard (`--check`, `zoho`, `gads`) |

## Report download (`zdrovena report`)

Downloads reports from Fakturownia's web UI as PDF using a headless Chromium browser
(Playwright). These reports are not available via the REST API.

```bash
zdrovena -y 2025 -m 2 report                          # VAT sales (default)
zdrovena -y 2025 -m 2 report -k expenses              # expenses
zdrovena -y 2025 -m 2 report -o ~/my-report.pdf       # custom output path
zdrovena -y 2025 -m 2 report --show-browser            # visible browser (debug)
```

Available report kinds: `vat-sales` (default), `income`, `expenses`, `unpaid`,
`products-sales`, `products-expense`, `products-margin`.

Output defaults to `~/Downloads/report_<kind>_<year>-<month>.pdf`.

### Report credentials

| Service (Keychain)         | What                      |
|----------------------------|---------------------------|
| `fakturownia_login`        | Fakturownia web login     |
| `fakturownia_password`     | Fakturownia web password  |

## Month-close pipeline (`zdrovena close`)

8-step automated pipeline:

| # | Step | Source |
|---|------|--------|
| 0 | Pre-flight — check vendors, bank stmt, reports | Zoho Mail, local fs |
| 1 | Create folder structure | — |
| 2 | Download sales invoices | Fakturownia API |
| 3 | Download JPK / VAT reports | Fakturownia API |
| 4 | Download cost invoices | KSeF → Fakturownia → Zoho Mail |
| 5 | Verify bank statement | local fs |
| 6 | Build ZIP archive | — |
| 7 | Send e-mail to accountant | Zoho SMTP |

Flags: `--dry-run`, `--zip`, `--send`, `--reset`, `--verbose`, `--non-interactive`, `--ignore-warnings`.

## Canva invoice download

The `close` pipeline can automatically download Canva subscription invoices
using a persistent Playwright browser profile. On first use (or when the session
expires), a visible browser window opens for manual login:

```bash
zdrovena setup canva                      # one-time Canva login
```

The session is saved to `~/.zdrovena/canva_profile/` and reused in subsequent
headless runs.

## Credentials

All secrets are stored via `keyring` (macOS Keychain, Linux SecretService, etc.). Use the built-in setup wizard:

```bash
zdrovena setup                # interactive wizard — prompts for all secrets
zdrovena setup --check        # verify which secrets are configured
zdrovena setup zoho           # Zoho Mail OAuth flow (grant code → refresh token)
zdrovena setup gads           # Google Ads OAuth flow (browser → token exchange)
```

### Required secrets

| Service (Keychain)         | What                    | How to get |
|----------------------------|-------------------------|------------|
| `fakturownia_api_token`    | Fakturownia API token   | zdrovena.fakturownia.pl → Settings → API |
| `fakturownia_login`        | Fakturownia web login   | Email used to log in to Fakturownia UI |
| `fakturownia_password`     | Fakturownia web password| Password for the Fakturownia UI account |
| `zoho_smtp_password`       | Zoho SMTP password      | Your Zoho email password |
| `zoho_client_id`           | Zoho OAuth Client ID    | api-console.zoho.eu → Self Client |
| `zoho_client_secret`       | Zoho OAuth Client Secret| api-console.zoho.eu → Self Client |
| `zoho_refresh_token`       | Zoho OAuth Refresh Token| `zdrovena setup zoho` |

### Optional secrets

| Service (Keychain)         | What                    | How to get |
|----------------------------|-------------------------|------------|
| `ksef_certificate`         | KSeF X.509 cert (.crt)  | Wizard imports file → base64 → Keychain |
| `ksef_private_key`         | KSeF private key (.key) | Wizard imports file → base64 → Keychain |
| `ksef_key_password`        | KSeF key passphrase     | `zdrovena setup` |
| `gads_developer_token`     | Google Ads dev token    | Google Ads → API Center |
| `gads_client_id`           | Google Ads OAuth ID     | Google Cloud Console → Credentials |
| `gads_client_secret`       | Google Ads OAuth Secret | Google Cloud Console → Credentials |
| `gads_refresh_token`       | Google Ads refresh token| `zdrovena setup gads` |

All secrets use Keychain account `humio`.

## Optional dependencies

| Extra  | Packages | Used by |
|--------|----------|---------|
| `ksef`  | cryptography, signxml, lxml | KSeF 2.0 e-invoicing |
| `pdf`   | pypdf, pdf2image | PDF date extraction |
| `report`| playwright, playwright-stealth | Browser-based report & Canva download |
| `all`   | ksef + pdf + report | everything |
| `dev`   | pytest, pytest-cov, responses | testing |

## Project structure

```
zdrovena/
├── cli.py                          # entry-point, argparse
├── __init__.py                     # package version
├── common/
│   ├── __init__.py                 # re-exports
│   ├── client.py                   # FakturowniaClient
│   ├── config.py                   # shared constants
│   ├── exceptions.py               # typed exception hierarchy
│   ├── formatting.py               # ANSI, months, to_decimal
│   └── retry.py                    # retry-with-backoff for HTTP calls
├── audit/
│   ├── api.py                      # AuditAPI (WZ/FV data)
│   ├── bottles.py                  # BottleReconciler
│   ├── sections.py                 # audit analysis sections (§1–§9)
│   ├── report_downloader.py        # Playwright-based report download
│   └── commands/
│       ├── audit_cmd.py
│       ├── export.py
│       ├── list_cmd.py
│       ├── products.py
│       ├── report_cmd.py
│       └── summary.py
└── month_closing/
    ├── __init__.py
    ├── config.py                   # vendors, company, Zoho/KSeF cfg
    ├── state.py                    # PipelineState (.state.json)
    ├── console.py                  # ConsoleReporter
    ├── canva_downloader.py         # Canva invoice PDF download (Playwright)
    ├── download_watcher.py         # interactive download watcher (~/Downloads)
    ├── email_service.py            # Zoho SMTP
    ├── zip_service.py              # ZIP archive
    ├── invoice_date_check.py       # PDF date extraction / OCR
    ├── ksef.py                     # KSeF 2.0 (optional deps)
    ├── google_ads.py               # Google Ads invoices
    ├── zoho_mail.py                # Zoho Mail REST
    ├── preflight.py                # PreflightChecker
    ├── orchestrator.py             # MonthCloseOrchestrator
    └── commands/
        ├── close_cmd.py
        └── setup_cmd.py            # secrets wizard + OAuth flows
tests/                              # pytest test suite
scripts/                            # CI helpers (quality gate, analyzers)
docs/                               # SPEC, PLAN, RUNBOOK, ADRs
.github/workflows/                  # CI pipelines
```

## Development

```bash
pip install -e '.[all,dev]'
pytest
```

## Requirements

- Python ≥ 3.12
- `keyring`-supported secret backend (macOS Keychain, Linux SecretService, etc.)
- Fakturownia API token
- Playwright + Chromium (for `report` and `close` commands): `pip install playwright && playwright install chromium`
- Zoho Mail credentials (for month-close)

## License

Internal tool — Zdrovena / Humio sp. z o.o.
