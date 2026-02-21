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

All secrets are stored in **macOS Keychain** via `keyring`:

```bash
python -c "import keyring; keyring.set_password('fakturownia_api_token', 'humio', 'TOKEN')"
python -c "import keyring; keyring.set_password('zoho_mail_password', 'humio', 'PASSWORD')"
```

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
        └── close_cmd.py
```

## Requirements

- Python ≥ 3.12
- macOS (Keychain for credentials)
- Fakturownia API token
- Zoho Mail credentials (for month-close)

## License

Internal tool — Zdrovena / Humio sp. z o.o.
