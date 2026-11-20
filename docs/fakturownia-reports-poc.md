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

## E3: JPK_V7M (2026-11-20, supervised walk on the test account)

The walk runs are 20261120T184122Z through 20261120T193638Z (`*-E3-walk`). The operator approved each step with an unknown effect. The taxpayer data was made up, with the operator's approval, on the test account only.

**What the wizard does:**

1. The page is `/accounting/app/reports/jpk_vat`, titled "JPK V7 (JPK VAT z deklaracją)". It belongs to the accounting module, which is served by Księgosoft: assets from `assets.ksiegosoft.pl`, and the API at `<account>.ksiegosoft.pl/api/v1`. The form takes Firma, "Za miesiąc", "Cel złożenia" (złożenie) and "Wariant formularza" (JPK V7 (3)). It has one button, **Nowy raport**. There was no list of earlier reports.
2. **"Nowy raport" creates a saved report on Księgosoft's server**: `POST /api/v1/reports/generate`, a job, then report `179092` at `/accounting/app/reports/jpk_vat/179092`. This is what the wizard leaves behind: every "Nowy raport" makes a new report record.
3. The report page has these sections:
   - "Dane identyfikacyjne Podatnika": tax office, person or entity, NIP, first name, last name, date of birth, e-mail, phone;
   - "Nagłówek";
   - "Deklaracja": tax due 153 PLN;
   - "Ewidencja sprzedaży VAT": 13 rows;
   - "Ewidencja zakupów VAT": 0 rows;
   - "Pouczenia", with the required checkbox "potwierdzam zapoznanie się z treścią i akceptację poniższych pouczeń" (Tax Penal Code).
4. **"zapisz i generuj xml"** stays effectively disabled until all of these are set: tax office, first name, last name, date of birth (`yyyy-mm-dd`), **e-mail**, and the ticked statement. The app checks the form when the statement is ticked. The button is "zapisz i generuj xml"; next to it is "zapisz zmiany". "Zapisz zmiany" did not keep the taxpayer data across a reload.
5. After "zapisz i generuj xml" (`PUT` the report, then `POST …/add_output` and a job), the page shows **"pobierz xml"** and **"Wyślij do Ministerstwa Finansów"**. **The guard disabled the send link** on the real page (`disabled=true` in controls.json). No request to any `gov.pl` host was made in any E3 run.
6. **"pobierz xml"** calls `GET /api/v1/reports/<id>/output_url?output_kind=xml&disposition=attachment`, which redirects to a presigned S3 link in Księgosoft's bucket (`https://s3.eu-west-1.amazonaws.com/files.ksiegosoft.pl/…`). The file is `piotrgryzlo_jpk_v7_<timestamp>.xml`, 7,911 B. It is `JPK_V7M (3)` with `VAT-7 (23)`, `CelZlozenia` 1, `KodUrzedu` 0202, 2026-10.
7. A report that already has its XML shows "pobierz xml" straight after a reload, with no regeneration.

**Safety-layer changes from E3:**
- The lock now allows `assets.ksiegosoft.pl` and the test account's `piotrgryzlo.ksiegosoft.pl`. The production host `zdrovena.ksiegosoft.pl` stays blocked, and a test proves it.
- The lock allows Księgosoft's S3 bucket only by exact URL prefix (`https://s3.eu-west-1.amazonaws.com/files.ksiegosoft.pl/`). Other buckets on the same host stay blocked, and a test proves it.

**What this means for automation:**
- A full V7 run needs the taxpayer data and the ticked legal statement. The statement is a declaration by a person, so an unattended production job must not tick it.
- Downloading the XML of a report that a person has already generated needs only "pobierz xml", which is safe to automate.
