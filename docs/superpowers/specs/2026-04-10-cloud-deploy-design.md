# Zdrovena Cloud Deploy & Backend Modernization — Design Spec

**Date:** 2026-04-10
**Author:** PiotrGry + Claude
**Status:** Draft

## 1. Context

Zdrovena-reconciliation is a Python CLI tool for invoice audit, bottle tracking, and month-closing workflows. Currently runs locally on developer machines with secrets in macOS Keychain. Goal: deploy to Azure cloud with automation, multi-user access, and security.

### Constraints

- Budget: ~200 PLN/month comfort, 500 PLN hard cap
- 4-10 users, mix of technical (CLI/API) and non-technical (web UI)
- Azure preferred (team has DevOps experience)
- No VMs

## 2. Current Pain Points

| Problem | Current State | Impact |
|---------|--------------|--------|
| Canva invoice | Playwright headless blocked by bot detection | Manual download required |
| Google Ads invoice | No API access, link in email requires login | Manual download required |
| PKO bank statement | No API | Manual download, app crashes if missing |
| Secrets | macOS Keychain only | Can't run on Linux/cloud |
| File storage | Local filesystem (~/.zdrovena/) | Single machine, no sharing |

## 3. Architecture — Two Phases

### Phase 1: Backend Fixes (local)

#### 3.1 browser-use for Fakturownia

Replace Playwright with browser-use for Fakturownia report generation:
- Automatic login via credentials from secrets store
- AI agent navigates DOM, resilient to UI changes
- Clean browser session each run (no persistent profile)

#### 3.2 Batch Invoice Collection (Canva + Google Ads)

**POC completed.** Zoho Mail integration works — finds emails, extracts invoice IDs and URLs.

browser-use batch download flow:
1. Preflight checks what's missing
2. Zoho Mail searches for vendor emails, extracts links/IDs
3. browser-use opens real browser (GUI required), navigates to each URL
4. User logs in where needed, browser-use handles the rest
5. All PDFs collected, app continues

**POC findings:**
- Zoho Mail search + invoice ID extraction: **works**
- browser-use on headless server: **does not work** (needs GUI for login)
- Must run on machine with display (Mac locally, or VNC/noVNC in cloud)

#### 3.3 PKO Bank Statement

No automation possible. Graceful handling:
- Preflight checks `inbox/` folder for statement matching period
- Missing = clear error message + `sys.exit(1)`
- No watcher, no waiting

Inbox abstraction layer:
- `LocalInboxProvider` — reads from `~/.zdrovena/inbox/` (now)
- `BlobInboxProvider` — reads from Azure Blob Storage (cloud phase)

#### 3.4 Secrets Migration

Move from macOS Keychain to `.env` file (local) with path to Azure Key Vault (cloud):

| Secret | Keyring Service | Env Var |
|--------|----------------|---------|
| Fakturownia API Token | fakturownia_api_token | FAKTUROWNIA_API_TOKEN |
| Fakturownia Login | fakturownia_login | FAKTUROWNIA_LOGIN |
| Fakturownia Password | fakturownia_password | FAKTUROWNIA_PASSWORD |
| Zoho SMTP Password | zoho_smtp_password | ZOHO_SMTP_PASSWORD |
| Zoho Client ID | zoho_client_id | ZOHO_CLIENT_ID |
| Zoho Client Secret | zoho_client_secret | ZOHO_CLIENT_SECRET |
| Zoho Refresh Token | zoho_refresh_token | ZOHO_REFRESH_TOKEN |
| KSeF Certificate | ksef_certificate | KSEF_CERTIFICATE |
| KSeF Private Key | ksef_private_key | KSEF_PRIVATE_KEY |
| KSeF Key Password | ksef_key_password | KSEF_KEY_PASSWORD |

Secret resolution order: env var > .env file > keyring (fallback)

### Phase 2: Azure Cloud Deploy

#### 3.5 Target Architecture

```
[Static Web App (Free)]  -->  [Container Apps (Consumption)]  -->  [Blob Storage]
       Web UI                     API + Cron Jobs                   Files/Invoices
                                       |
                                  [Key Vault]
                                    Secrets
```

| Component | Azure Service | Est. Cost |
|-----------|--------------|-----------|
| Backend API + cron jobs | Container Apps (consumption) | ~30-60 PLN |
| Web UI (non-technical users) | Static Web App (Free tier) | 0 PLN |
| Secrets | Key Vault | ~5 PLN |
| State/metadata | Cosmos DB (Free tier 1000 RU/s) | 0 PLN |
| Container registry | ACR (Basic) | ~20 PLN |
| File storage | Blob Storage | ~5 PLN |
| **Total** | | **~60-90 PLN/month** |

#### 3.6 File Storage on Blob

All month-closing artifacts on Azure Blob Storage instead of local filesystem:
- Container: `month-closing`
- Structure: `{year}/{month}/costs/`, `{year}/{month}/reports/`, `{year}/{month}/zip/`
- Inbox uploads: `inbox/{year}/{month}/` (PKO statements, manual uploads)
- Web UI provides drag-and-drop upload for non-technical users

#### 3.7 browser-use in Cloud

Options for running browser-use (needs GUI for manual login):
1. **Container App with noVNC** — user connects via browser to see the automation browser
2. **Separate "browser worker"** — Azure Container Instance spun up on-demand with VNC
3. **Pre-authenticated sessions** — store cookies/tokens, reduce manual login frequency

Decision deferred to implementation phase.

#### 3.8 Infrastructure as Code

Terraform for all Azure resources:
- Resource group, Container Apps environment, ACR, Key Vault, Blob Storage, Static Web App
- Secrets injected from Key Vault into Container Apps as env vars
- CI/CD: GitHub Actions builds container, pushes to ACR, deploys to Container Apps

## 4. What's NOT In Scope

- Google Ads REST API integration (no API access)
- PKO Open Banking / PSD2 (too expensive, requires contracts)
- Email inbox provider (deferred — web UI upload is simpler and more secure)
- Multi-tenant / multi-company support

## 5. Full Business Automation Roadmap

**Business:** Humio (wodahumio.pl) — water e-commerce on Shopify

### Faza 0: Fundamenty (infrastruktura)

**Cel:** Baza pod wszystkie kolejne fazy.

- Azure Container Apps + Key Vault + Blob Storage (Terraform)
- Migracja sekretów z Keychain → Key Vault
- CI/CD: GitHub Actions → ACR → Container Apps
- `.env` / dotenv jako bridge na czas migracji
- PKO preflight: inbox folder + graceful error
- browser-use: replace Playwright (Fakturownia reports)
- browser-use: batch Canva + Google Ads download (wymaga GUI)
- Blob storage abstraction layer
- Web UI dla użytkowników nietechnicznych
- Cron jobs dla automatycznego zamknięcia miesiąca

**Szacowany czas:** 1-2 tygodnie

### Faza 1: Order Fulfillment ← NAJBLIŻSZY PRIORYTET

**Cel:** Nowe zamówienie w Shopify → faktura w Fakturowni + przesyłka automatycznie.

```
Shopify Webhook (order.created)
        │
        ├── Fakturownia API → stwórz fakturę VAT
        │     (dane klienta + produkty z zamówienia)
        │
        └── Shipping API → stwórz zlecenie odbioru
              ├── InPost ShipX API (jeśli paczkomat/kurier InPost)
              └── Apaczka API (jeśli inna forma wysyłki)
```

**Wymagane:**
- Shopify Webhook → Container App endpoint
- Fakturownia REST API (klient już istnieje w `common/`)
- InPost ShipX API (klucz API)
- Apaczka API (klucz API)
- Mapowanie: Shopify shipping method → InPost/Apaczka
- Walidacja danych adresowych przed wysłaniem zlecenia

**Szacowany czas:** 2-3 tygodnie

### Faza 2: Marketing Intelligence

**Cel:** Data-driven marketing — analiza, rekomendacje, automatyczne kampanie.

```
Google Ads API ──┐
                 ├── Data Pipeline → Analiza → Rekomendacje
Shopify API ─────┘                              │
                                    ├── Raporty (słabe punkty strategii)
                                    ├── Generowanie postów/blogów (LLM)
                                    └── Optymalizacja kampanii Google Ads
```

**Wymagane:**
- Google Ads API access (wymaga konta deweloperskiego Google)
- Shopify Analytics API (zamówienia, konwersje, produkty, ruch)
- LLM (Claude/GPT) do generowania treści i analizy
- Dashboard z metrykami (Static Web App)
- Scheduler: cykliczne pobieranie danych i generowanie raportów

**Szacowany czas:** 4-6 tygodni

### Faza 3: Payment Automation

**Cel:** Nowa faktura kosztowa → powiadomienie → dual-approve → automatyczny przelew.

```
Nowa faktura kosztowa (Zoho/Fakturownia)
        │
        ├── Push notification → telefon (2 approverów)
        │
        ├── 2x approve → Revolut Business API → przelew
        │
        └── PKO → Revolut (cykliczny ręczny przelew na pokrycie)
```

**Wymagane:**
- Revolut Business API (OAuth 2.0 + API key)
- Push notifications (Firebase Cloud Messaging lub Slack/Teams bot)
- Approval workflow (2 z N userów musi zatwierdzić — dual authorization)
- Monitoring salda Revolut vs nadchodzące płatności
- Cykliczne powiadomienie o potrzebie przelewu PKO → Revolut

**Security:** Żaden pojedynczy user nie może autoryzować przelewu sam.

**Szacowany czas:** 3-4 tygodnie

### Faza 4: AI Customer Service Agent

**Cel:** AI obsługuje maile klientów — klasyfikuje, briefuje zespół, drafuje odpowiedzi.

```
Zoho Mail (incoming) → AI Agent
        │
        ├── Klasyfikacja intencji
        │     (zamówienie? reklamacja? współpraca? pytanie o cennik?)
        │
        ├── Enrichment — pobranie kontekstu
        │     (Shopify: zamówienia klienta, status przesyłki)
        │     (Fakturownia: faktury klienta)
        │     (Cennik, FAQ, polityka zwrotów)
        │
        ├── Brief → Slack/Teams notification do zespołu
        │     "Klient X pyta o status zamówienia #1234,
        │      wysłane 3 dni temu InPost, tracking: XYZ"
        │
        └── Draft odpowiedzi
              ├── Proste sprawy → auto-send (po konfigurowalnym approve)
              └── Złożone sprawy → czeka na approve zespołu
```

**Wymagane:**
- Zoho Mail API — monitoring incoming mail (już istnieje)
- LLM z RAG — knowledge base: cennik, FAQ, polityka zwrotów, info o produktach
- Shopify API — zamówienia klienta, status przesyłki
- Klasyfikator intencji (prompt-based lub fine-tuned)
- Eskalacja — agent wie kiedy NIE odpowiadać sam (reklamacje, duże B2B, sprawy prawne)
- Human-in-the-loop — drafty do zatwierdzenia przed wysyłką
- Metryki jakości — tracking response quality, customer satisfaction

**Szacowany czas:** 6-8 tygodni

### Dodatkowe propozycje automatyzacji

#### Faza 1.5: Inventory & Restock Alerts

Shopify stock levels → alert gdy produkt spada poniżej progu → auto-zamówienie u dostawcy lub powiadomienie do zespołu.

#### Faza 2.5: Competitor Monitoring

Scraping cen konkurencji (inne sklepy z wodą) → alert gdy cena Humio jest za wysoka/niska → sugestie zmian cenowych.

#### Faza 3.5: Cash Flow Forecasting

Na podstawie historii zamówień + faktur kosztowych → prognoza cash flow na 30/60/90 dni → alert jeśli będzie potrzebny przelew PKO → Revolut.

#### Faza 5: Unified Dashboard

Jeden panel dla zespołu:
- Zamówienia i fulfillment status
- Marketing KPIs (ROAS, CPC, konwersja)
- Cash flow i nadchodzące płatności
- Status agenta mailowego i jakość odpowiedzi
- Alerty i powiadomienia

### Podsumowanie kosztów Azure (wszystkie fazy)

| Faza | Dodatkowe usługi | Szacowany koszt/msc |
|------|-----------------|---------------------|
| 0 (Fundamenty) | Container Apps, Key Vault, Blob, ACR | ~70 PLN |
| 1 (Orders) | + webhook endpoint (w Container Apps) | +0 PLN |
| 2 (Marketing) | + Cosmos DB dla metryk, LLM API calls | +30-50 PLN |
| 3 (Payments) | + push notifications | +10 PLN |
| 4 (AI Agent) | + LLM API calls (heaviest), RAG storage | +50-100 PLN |
| **Total** | | **~160-230 PLN/month** |

Mieści się w budżecie 200-500 PLN. Największy koszt to LLM API calls w fazie 4 — optymalizacja przez cache, mniejsze modele do klasyfikacji, batch processing.

## 6. POC Findings (2026-04-10)

### browser-use POC (`poc_browser_batch.py`)

**Co działa:**
- Zoho Mail integration — szuka maili, wyciąga linki i invoice ID
- Znaleziono fakturę Canvy za marzec 2026
- browser-use instaluje się i konfiguruje poprawnie
- Secret fallback chain (env > .env > keyring)

**Co nie działa:**
- browser-use wymaga GUI do ręcznego logowania
- Na headless serwerze (SSH) nie da się uruchomić przeglądarki z interakcją usera
- Canva/Google blokują headless browser

**Wniosek:** browser-use wymaga maszyny z GUI. W chmurze: Container Instance z noVNC lub pre-authenticated sessions.
