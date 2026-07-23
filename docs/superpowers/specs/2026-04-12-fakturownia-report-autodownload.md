# Fakturownia Report Auto-Download via Playwright

**Date:** 2026-04-12
**Branch:** chore/spec-driven-bootstrap
**Status:** Draft

## Problem

The month close pipeline requires 3 Fakturownia reports (JPK_FA.xml, JPK_V7M.xml, Wykaz_sprzedazy_VAT.pdf) that are only available through the browser UI. Currently the user must manually download each one, place them in `inbox/`, then re-run the pipeline. This is the last manual bottleneck for reports that can be automated.

## Solution

Create `zdrovena/month_closing/fakturownia_reports.py` that uses Playwright to log into Fakturownia, trigger report generation, and download native files (XML/PDF) via the download link in the DOM. Integrate into preflight so missing reports are auto-fetched.

## Architecture

### New file: `zdrovena/month_closing/fakturownia_reports.py`

**Function signature:**

```python
def download_fakturownia_reports(
    reports: list[dict],       # subset of FAKTUROWNIA_REPORTS that are missing
    date_from: str,            # "YYYY-MM-DD" (first day of month)
    date_to: str,              # "YYYY-MM-DD" (last day of month)
    output_dir: Path,          # inbox/ directory
    *,
    headless: bool = True,
    timeout: int = 120_000,
) -> list[tuple[dict, Path]]:  # list of (report_config, downloaded_path) pairs
```

**Returns** list of successfully downloaded (report, path) tuples. Reports that fail are silently skipped (logged as warnings).

### Login flow (reuse from report_downloader.py)

Extract a shared helper or duplicate the pattern. The login steps are:

1. Block ConsentManager script: `page.route("**/*consentmanager*", lambda route: route.abort())`
2. Navigate to `https://zdrovena.fakturownia.pl/login`
3. Wait for `[name='user_session[login]']`
4. Fill login + password from keyring
5. Click `[name='commit']`
6. Wait for URL to change away from `/login`
7. Same error handling as existing (permission denied, wrong credentials)

### Per-report download flow

For each report in `reports`:

1. Navigate to `report["url"]` with params: `?date_from={date_from}&date_to={date_to}&submitted=true&currency_convert_to_main=false`
2. Wait for `#job_download_link a[href*='/jobs/']` (report generation complete)
3. Use `page.expect_download()` context manager + click the download link
4. Save downloaded file to `output_dir / report["dest_name"]`
5. Validate file size >= 100 bytes
6. Log success or failure

**Note on JPK_V7M:** The URL is `/accounting/app/reports/jpk_vat` (different path pattern than the other two which are under `/reports/`). The DOM selector for the download link should be the same, but needs verification. If the accounting app has a different DOM structure, add a report-specific selector override in the config dict.

### Config changes: `zdrovena/month_closing/config.py`

Add optional `download_selector` field to `FAKTUROWNIA_REPORTS` dicts for report-specific DOM overrides. Default remains `#job_download_link a[href*='/jobs/']`.

### Integration: `zdrovena/month_closing/preflight.py`

In `_check_reports()`, after identifying missing reports:

```python
# Before falling back to manual URLs, attempt auto-download
if missing_reports:
    try:
        from zdrovena.month_closing.fakturownia_reports import download_fakturownia_reports

        downloaded = download_fakturownia_reports(
            missing_reports,
            date_from,
            date_to,
            watch_dir,
            headless=not self.visible,  # controlled by --visible flag
        )
        # Re-check: remove successfully downloaded reports from missing list
        for rpt, path in downloaded:
            # Copy to month folder (same logic as existing match handling)
            ...
    except Exception as exc:
        logger.warning("Auto-download failed: %s — falling back to manual URLs", exc)
```

### CLI: `zdrovena/month_closing/commands/preflight_cmd.py`

Add `--no-browser` flag to skip Playwright auto-download (useful in CI or when browser is unavailable). Default: attempt auto-download.

### Credential resolution

Reuse existing keyring lookup:
- `KEYCHAIN_SERVICE_FAKTUROWNIA_LOGIN` / env var `FAKTUROWNIA_LOGIN`
- `KEYCHAIN_SERVICE_FAKTUROWNIA_PASSWORD` / env var `FAKTUROWNIA_PASSWORD`

Follow the existing `_get_secret()` pattern (env var first, keyring fallback).

## Error handling

| Failure | Behavior |
|---------|----------|
| Playwright not installed | Log warning, skip auto-download, show manual URLs |
| Login fails | Log error, skip auto-download, show manual URLs |
| Report job timeout | Skip that report, continue to next |
| Download fails | Skip that report, log warning |
| File too small (<100 bytes) | Treat as failed, delete partial file |
| JPK_V7M different DOM | Fall back to manual URL for that report only |

All failures are non-fatal. The pipeline degrades gracefully to manual download instructions.

## Testing

- Unit test: mock Playwright, verify download flow logic and error handling
- Integration test: verify preflight falls back gracefully when Playwright unavailable
- Manual test: run `zdrovena preflight 2026-03` with credentials, verify all 3 reports download

## Files changed

| File | Change |
|------|--------|
| `zdrovena/month_closing/fakturownia_reports.py` | **New** — Playwright download logic |
| `zdrovena/month_closing/preflight.py` | Add auto-download call before falling back to manual URLs |
| `zdrovena/month_closing/commands/preflight_cmd.py` | Add `--no-browser` flag |
| `zdrovena/month_closing/config.py` | Optional: add `download_selector` to report dicts |
| `tests/test_fakturownia_reports.py` | **New** — unit tests |

## Out of scope

- Canva / Google Ads / bank statement automation (not automatable)
- Modifying the existing `report_downloader.py` (separate concern, different output format)
- Headless server deployment (requires display or xvfb, deferred to cloud deploy spec)
