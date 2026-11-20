# PoC: unattended Fakturownia report downloads

Date: 2026-11-20. Status: approved design, before the PoC. Epic: ai-assistant#358. Follows zdrovena#579.

## Why a PoC

Month close needs three reports from Fakturownia every month: JPK_FA, the VAT sales register and JPK_V7M. Today:

- In production, month close runs through the API (`zdrovena/api/routers/close.py`) with `non_interactive=True`. Step 3 then only checks whether the reports are in the blob. It never downloads them.
- From the CLI, step 3 opens a visible browser with a "Go / Skip / Done" sidebar and waits for the operator.
- zdrovena#580 (from #579, merged and released on 2026-11-20) added an unattended flow (`FAKTUROWNIA_REPORT_HEADLESS=1`) for JPK_FA and the VAT register, a no-send guard and offline tests. It was written without access to Fakturownia. Its live behaviour is unverified, except for the JPK_FA steps that were checked by hand on the test account.

The production job is decided: an Azure Container Apps Job, started by a schedule early in the month and by a "Pobierz raporty ponownie" action in month close. Before it can be designed, these questions need answers from real runs:

1. Does the JPK_FA flow work unattended, again and again?
2. What controls does the VAT sales register have? Does it run a background job like JPK_FA, and can its PDF be downloaded?
3. What does "Zapisz i generuj XML" in the JPK_V7M wizard save in Fakturownia? Can the XML be downloaded without any sending step?
4. When does Fakturownia ask for the e-mailed verification code? Does a browser profile carry over to a container, and to Azure's IP addresses? Which address gets the code, what does the e-mail look like, and could it be read automatically?
5. Does the no-send guard block what it must on the real pages, and nothing the download needs?
6. How long does a run take, and how much memory does Chromium need, so that the job can be sized?

## Hard rules

- **Nothing is ever sent to the tax office.** The operator's rule, from 2026-11-20.
- **Test account only.** The PoC runs only against `https://piotrgryzlo.fakturownia.pl`. Production (`zdrovena.fakturownia.pl`) is out of reach by construction, not by configuration.
- Test credentials come from Key Vault (`fakturownia-test-login`, `fakturownia-test-password`) into environment variables. They are never printed or written to evidence.

## Approach

A separate PoC driver, `scripts/poc/fakturownia_reports_poc.py`, reuses the merged module (`zdrovena/month_closing/fakturownia_reports.py`). It reuses the login, the guard and the headless flows, and adds three things around them:

- a stricter safety layer;
- one evidence folder per run;
- the experiments below.

Production code changes only when an experiment shows the module is wrong. Each such change is a small fix with its own test.

Two alternatives were rejected:

- Building the PoC inside month close: experiments would mix with production code.
- Recording only (`playwright codegen`): it cannot answer the login, container and Azure questions. Recording is still used for the first pass over the VAT register and JPK_V7M (E2, E3).

## Safety layer (the PoC driver, on top of the module's guard)

1. **Test-account lock.** The driver refuses to start unless the base URL is exactly the test host. It routes every request through an allowlist: the test host plus the static hosts the pages need, which are listed in the first run and then fixed in the driver. Everything else is aborted and logged. Any `*.gov.pl` host (Ministry of Finance, e-Deklaracje, KSeF) is aborted even if it is on the allowlist.
2. **Clicks from an allowlist only.** Each report has its own list of allowed controls (text and role). The driver clicks nothing else. The module's init script still disables any control matching `wy[sś]lij|wy[sś]y[lł]k|podpisz|e-?deklarac|do urz[eę]du|do mf\b`, including controls added later.
3. **Every click is logged.** The log records the control text, the page URL and a screenshot, so any run can be audited afterwards.
4. **JPK_V7M is gated by the operator.** Before any automatic V7M run, one manual pass is recorded with the operator watching. The operator approves the exact steps, and only those go into the allowlist. The module's existing `_try_v7_generate_then_download` and `_auto_fill_jpk_v7m` are not used until then.

## Experiments

| # | What | Where | Answers |
|---|---|---|---|
| E1 | JPK_FA unattended, 5 runs in a row, a new process each time, same profile | this machine | 1, 6 |
| E2 | VAT sales register: one recorded manual pass, then automatic, 5 runs | this machine | 2, 6 |
| E3 | JPK_V7M: a recorded manual pass with the operator watching, approval of the steps, then automatic, 3 runs. Before and after each run, the list of declarations in Fakturownia is recorded. | this machine | 3 |
| E4 | Login: (a) new profile, (b) reused profile, (c) profile copied into a container. Each verification e-mail is recorded: sender, subject, format, address. | this machine, Docker | 4 |
| E5 | Guard: unit tests, and a check on the real pages. All aborted requests are listed, downloads still work, and a click outside the allowlist raises. | local | 5 |
| E6 | E1–E3 inside the official Playwright Python image | Docker, local | 6, part of 4 |
| E7 | One manual Azure Container Apps Job run against the test account, with the profile from E4 on Azure Files | Azure | 4 (Azure IP) |

E7 creates Azure resources (a temporary job and a file share). It needs the operator's approval and the choice of a resource group before it runs, and the resources are removed after the PoC.

## Evidence

Each run writes `~/.zdrovena/poc-runs/<timestamp>-<experiment>/`:

- `trace.zip` (Playwright trace);
- one screenshot per step;
- `requests.jsonl` (allowed and aborted requests);
- `clicks.jsonl`;
- the downloaded files;
- `run.json` (experiment, report, timings, peak memory, outcome, error).

Evidence is not committed, because it shows account data. The findings are committed in `docs/fakturownia-reports-poc.md`. That file answers each question above with a pointer to the run that shows it, the exact steps of each report, and what JPK_V7M leaves behind in Fakturownia.

## Done when

- JPK_FA and the VAT register pass 5 of 5, and JPK_V7M passes 3 of 3, both locally and in Docker. E7 succeeds once in Azure.
- In every run, every tax-office request was aborted and nothing outside the allowlist was clicked. The evidence shows it.
- The verification-code questions are answered: when it appears, whether the profile carries over, and whether the e-mail can be read automatically. This decides how the production job handles verification.
- The findings document exists, and the production job (zdrovena#579's successor) can be designed from it.

## Out of scope

- The production job, its schedule and the month-close action: designed after the PoC.
- Any run against the production account.
- Sending, signing or submitting anything.
