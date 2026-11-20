# Fakturownia report download PoC: findings

PoC for zdrovena#583. Design: `docs/superpowers/specs/2026-11-20-fakturownia-reports-poc-design.md`. Plan: `docs/superpowers/plans/2026-11-20-fakturownia-reports-poc.md`.

All runs used the test account `https://piotrgryzlo.fakturownia.pl`. Evidence is in `~/.zdrovena/poc-runs/<run>/` on pepus-pc and is not committed.

## E1: JPK_FA unattended (2026-11-20)

**Result: 5 of 5 runs passed**, headless, with the persistent profile from the morning's manual check.

| Run | Duration | Peak browser memory* | File |
|---|---|---|---|
| 20261120T183400Z | 8.0 s | 990 MB | JPK_FA.xml, 18,711 B |
| 20261120T183408Z | 5.2 s | 992 MB | 18,711 B |
| 20261120T183413Z | 5.3 s | 998 MB | 18,711 B |
| 20261120T183419Z | 5.7 s | 1,017 MB | 18,711 B |
| 20261120T183424Z | 5.9 s | 992 MB | 18,711 B |

\* The sum of VmHWM over the browser processes. It counts shared pages more than once, so the real need is lower. The container measurement (E6) is the one to size the job by.

- **Clicks:** exactly three, all from the allowlist, in every run: "Generuj raport", the job's "Gotowe" result link, and "Eksportuj do XML".
- **No-send guard:** it aborted nothing, because the flow made no request to a tax-office host.
- **Login:** the profile session was reused. There was no `/login/verification` in any run.
- **Files:** they are identical except for `DataWytworzeniaJPK`, the generation time.

**Defects found and fixed on the way:**

1. **PoC lock.** It blocked `assets2.firmlet.com`, which serves Fakturownia's JavaScript, and the job modal then never polled. That host is now allowed. Still blocked: `widgets.intum.net`, `browser.sentry-cdn.com`, `www.googletagmanager.com`.
2. **Production code from #580.** `run_headless_report` registered `list.append` as the download listener, and Playwright rejects a builtin method (`'builtin_function_or_method' object has no attribute '_pw_impl_instance_'`). So every unattended download failed. The offline Playwright tests catch this, but they were skipped where #580 was written, because that sandbox had no Chromium. Fixed in 65d94c9. `tests/test_fakturownia_headless.py` now runs 76 of 76 here.
   - **Lesson for the production job:** CI must run the Chromium tests, not skip them.

## E2: VAT sales register (2026-11-20)

**The page, from a supervised walk (run 20261120T183504Z-E2-walk):**

- The page is `/reports/income_tax_records`, titled "Wykaz sprzedaży VAT". The form takes the report type, a date range and "Szukaj po dacie". Its buttons are **Generuj raport**, **Eksportuj do XLS**, **Drukuj do PDF** and **Wydruk**. There is no send or sign control.
- **Generuj raport** starts a job (`job_id` in the URL) and reloads the page with the table of invoices. It shows no result link and gives no file.
- **Drukuj do PDF** starts its own job. When the job is done, a modal shows a **Pobierz** link to `/jobs/<id>/result` and a **Zamknij** button. Clicking **Pobierz** downloads the PDF (`%PDF-1.4`, about 25 KB for 2026-10).

**Fix in production code:**

- The #580 flow pressed "Generuj raport" and waited for a job link that never comes on this page. It failed on the test account: run 20261120T183819Z, "link wyniku joba 'Gotowe' not shown within 120 s".
- The `vat_register` flow now starts with the new allowlisted control `PRINT_PDF` ("Drukuj do PDF"), and the job link's click delivers the file.
- The offline fake site now imitates the real page: "Generuj raport" only shows the table, and "Drukuj do PDF" leads to a "Pobierz" link.

**Result: 5 of 5 runs passed**, plus 2 more after a driver fix.

| Run | Duration | Peak browser memory | File |
|---|---|---|---|
| 20261120T183919Z | 4.2 s | 910 MB | Wykaz_sprzedazy_VAT.pdf, 25,121 B |
| 20261120T183924Z | 6.9 s | 910 MB | 25,121 B |
| 20261120T183930Z | 5.3 s | 911 MB | 25,121 B |
| 20261120T183936Z | 5.1 s | 911 MB | 25,121 B |
| 20261120T183941Z | 5.3 s | 906 MB | 25,121 B |

- **Clicks:** two per run, both allowlisted: "Drukuj do PDF" and the job link.
- **No-send guard:** it aborted nothing.
- **Driver defect:** the PoC driver re-wrapped the click logger on every run without restoring it. So the click logs of the E1 runs and of the first four E2 runs above also hold the clicks of the later runs. Fixed. Runs 20261120T184001Z and 20261120T184005Z show exactly two clicks each. All logged clicks, before and after the fix, were allowlisted controls.
