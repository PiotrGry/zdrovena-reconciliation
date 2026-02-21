# zdrovena-reconciliation

Unified CLI for **Zdrovena / Humio** — invoice audit, bottle tracking & month-close pipeline.

```
pip install -e .            # base (audit, list, export, summary, products)
pip install -e '.[all]'     # + KSeF + PDF processing
```

## Quick start

```bash
zdrovena --version                        # 2.0.0
zdrovena -y 2025 audit                    # pełny audyt FV vs WZ
zdrovena -y 2025 -m 6 list               # faktury z czerwca
zdrovena -y 2025 export                   # CSV per miesiąc
zdrovena -y 2025 summary                  # WZ vs FV (plastik/szkło)
zdrovena products --active-only           # aktywne produkty

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
| `close`    | Month-close pipeline — preflight → invoices → KSeF → ZIP → e-mail |
| `setup`    | Keychain & OAuth credential wizard (`--check`, `zoho`, `gads`) |

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
| `all`   | ksef + pdf | everything |

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
│   └── commands/
│       ├── audit_cmd.py
│       ├── export.py
│       ├── list_cmd.py
│       ├── products.py
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
- Zoho Mail credentials (for month-close)

## License

Internal tool — Zdrovena / Humio sp. z o.o.
