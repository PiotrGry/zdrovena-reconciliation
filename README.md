# zdrovena-reconciliation

Unified CLI for **Zdrovena / Humio** — invoice audit, bottle tracking & month-close pipeline.

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

Flags: `--dry-run`, `--zip`, `--send`, `--reset`, `--verbose`.

## Credentials

All secrets are stored in **macOS Keychain** via `keyring`. Use the built-in setup wizard:

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
| `report`| playwright | Browser-based report download |
| `all`   | ksef + pdf + report | everything |

## Project structure

```
zdrovena/
├── cli.py                          # entry-point, argparse
├── common/
│   ├── __init__.py                 # re-exports
│   ├── client.py                   # FakturowniaClient
│   ├── config.py                   # shared constants
│   └── formatting.py               # ANSI, months, to_decimal
├── audit/
│   ├── api.py                      # AuditAPI (WZ/FV data)
│   ├── bottles.py                  # BottleReconciler
│   ├── report_downloader.py            # Playwright-based report download
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
```

## Requirements

- Python ≥ 3.12
- macOS (Keychain for credentials)
- Fakturownia API token
- Playwright + Chromium (for `report` command): `pip install playwright && playwright install chromium`
- Zoho Mail credentials (for month-close)

## License

Internal tool — Zdrovena / Humio sp. z o.o.
