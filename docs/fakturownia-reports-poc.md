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
