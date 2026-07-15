# ZDROVENA - Unified Implementation & Release Plan

**Repo:** `PiotrGry/zdrovena-reconciliation`  
**Owner:** Piotr Gryzło  
**Version:** 1.0  
**Date:** 2026-07-12

## 1. Executive summary

Zdrovena-reconciliation is a mature internal back-office system built on FastAPI, Azure and React SPA. The technical foundations are solid: JWT/HMAC validation, Decimal-based financial logic, optimistic concurrency in Table Storage, broad backend tests and layered CI.

The current priority is not a rewrite. The key problem is reliability of quality signals: a green workflow does not always prove that the complete authenticated flow works. The implementation order is therefore:

1. consolidate staging validation and observability,
2. fix invoicing and deposit calculations,
3. harden shipment state transitions,
4. complete label printing,
5. build frontend contracts, fake providers and E2E gates.

> **Rule:** Production is a promotion target, not the first validation environment.

## 2. Implementation principles

- Each unit is atomic and leaves `develop` green.
- One conventional commit per unit.
- No unrelated changes to KSeF / monthly-close / invoicing logic.
- Business-critical changes deploy during low-volume windows.
- Staging uses real authentication, roles and production-like configuration.
- Mock external boundaries, not internal domain modules.
- Each operational flow needs success logging, error logging, correlation ID, tests and rollback criteria.

## 3. Release status

| Release | Status | Scope |
|---|---|---|
| R0 | Done | Removed obsolete xfail; fixed Azure OIDC permissions |
| R1 | Done | `fetchJson`, toasts, ErrorBoundary, API error envelope, real close progress |
| R2 | Done with follow-ups | Correlation ID, structured events, auth guard, CORS, release validation |
| R3 | Done with infra verification | Polling, DLQ UI, action group, DLQ alert |
| R4 | Next | Invoicing, deposit, 502 loop, invoice preview parity |
| R5 | Planned | Shipment state machine, atomic claim, single and bulk labels |
| R6 | Planned | Frontend tests, contracts, fake providers, E2E |

## 4. R2/R3 consolidation package

### Required changes

- `smoke --strict`: missing token or critical SKIP must fail release validation.
- Any authenticated `401/403` is a failure, never a skip.
- Warm-up without HTTP 200 after retries fails the workflow.
- Validate correlation ID format and length.
- Reset ContextVar in `finally`.
- Require one fail-closed `APP_ENV`.
- Mask e-mails, tracking numbers and identifiers in structured logs.
- Apply Terraform, verify the actual KQL table and confirm test alert delivery.

### Acceptance criteria

- Zero critical skips.
- Authenticated reads return expected statuses.
- Dead API cannot produce a green release.
- Test alert is visible in Azure Monitor and delivered by e-mail.

## 5. R4 - invoicing and deposit

Split R4 into four small units:

### R4.1 - break the 502 loop

Recover the already-created invoice and resume the remaining steps instead of resetting the invoice ID and creating it again.

### R4.2 - single deposit source

Use one shared calculation. Prefer native Allegro deposit data; use a documented fallback only when necessary.

### R4.3 - idempotency and validation

- Protect orders with multiple invoices.
- Re-read before write.
- Validate `quantity`.
- Keep financial values as `Decimal` until serialization.
- Prevent duplicate side effects during retry.

### R4.4 - invoice preview parity

- VAT from the correct field.
- Display settlement positions correctly.
- Compare payable amount against `summary.totalToPay` minus delivery.

### Mandatory tests

- Real anonymized Allegro payload -> mapper -> endpoint -> response.
- Do not mock the internal invoice mapper in endpoint tests.
- Multiple invoices do not receive duplicated full deposit.
- Retry after partial success does not create a second invoice.
- Difference between expected and actual payable amount equals `0.00 PLN`.

## 6. R5 - shipment state machine and labels

### R5A - shipment safety

- Atomic claim before `execute_draft`.
- Block double execution.
- Explicit allowed state transitions.
- Guard `mark_fulfilled` for cancelled/error drafts.
- Retry without creating duplicate courier shipments.

### R5B - label printing

- Return `LABEL_NOT_READY` with HTTP 409 before InPost confirmation.
- Show an informational toast instead of generic 502/alert.
- Add `POST /shipping/labels/batch`.
- Use InPost/Allegro bulk endpoints; merge Apaczka PDFs if required.
- Optionally support A6/A4 and PDF/ZPL.

## 7. R6 - development without production testing

### Frontend

- Vitest + React Testing Library.
- Tests for ShippingView, DLQ, error envelope, polling and addresses.
- Generate TypeScript types from FastAPI OpenAPI.
- CI fails when generated contracts are stale.

### Fake providers

- Fake Allegro, InPost, Apaczka and Fakturownia over HTTP.
- Validate URL, method, headers, body and enums.
- Stateful scenarios: timeout, retry, 500, duplicate request, partial success.
- Fixtures are provider data, not direct mock return values.

### E2E scenarios

1. Webhook -> draft -> visible in UI.
2. Draft -> InPost execution -> tracking number.
3. Provider 500 -> Polish toast + correlation ID.
4. Invoice preview -> create invoice -> amount parity.
5. DLQ -> retry -> draft created.

## 8. Target CI/CD flow

1. PR quality gate.
2. Docker build and local health/readiness.
3. Staging deployment.
4. Strict staging validation.
5. Manual approval for finance/shipping side effects.
6. Production promotion.
7. Post-deploy checks and first real operations verification.

Every required step must pass. Critical SKIP is not accepted.

## 9. Risk register

| Risk | Priority | Mitigation |
|---|---|---|
| False-green smoke | P0 | Strict mode; required tests; authenticated 401 = FAIL |
| Incorrect deposit/invoice | P0 | One calculation source; Decimal; manual 3-5 invoice verification |
| Double shipment execution | P1 | Atomic claim and state machine |
| Terraform alert not working | P1 | Apply + test alert + e-mail confirmation |
| FE-BE contract drift | P1 | OpenAPI-generated types + contract tests |
| Provider sandbox instability | P1 | Local HTTP fake providers |
| Large `webhooks.py` | P3 | Refactor after R0-R6 stabilization |

## 10. Implementation order

1. R2/R3 consolidation.
2. R4.1 + R4.2.
3. R4.3 + R4.4.
4. R5A.
5. R5B.
6. R6.

## 11. Release checklist

### Before merge

- [ ] Atomic scope.
- [ ] Tests and CI green.
- [ ] Zero critical SKIP.
- [ ] Acceptance and rollback criteria documented.
- [ ] Environment templates updated.

### Before production

- [ ] Staging deploy complete.
- [ ] Strict smoke passed.
- [ ] Authenticated API paths verified.
- [ ] Correlation ID visible in logs.
- [ ] Test alert delivered.
- [ ] Low-volume window confirmed for R4/R5.

### After production

- [ ] Health/readiness 200.
- [ ] No 5xx or latency spike.
- [ ] First real operations verified.
- [ ] DLQ checked.
- [ ] Rollback remains available during observation.

## 12. Source documents

- `zdrovena_master_plan.pdf`
- `zdrovena_r0_r1_summary.pdf`
- `zdrovena_gate_r2_summary.pdf`
- `zdrovena_r3_summary.pdf`
- `zdrovena_label_printing_plan.pdf`
- PR #116, test suite and GitHub Actions review from 2026-07-12

## 13. Copilot execution instructions

This section is binding for implementation work.

- Work on exactly one GitHub issue at a time.
- Treat the issue scope and acceptance criteria as binding.
- Do not modify files/functions outside the explicit scope unless compilation requires it; report every such change.
- Do not modify KSeF, monthly-close or invoicing logic outside the specific issue scope.
- Do not weaken, delete, skip or xfail tests only to make CI pass.
- Use `Decimal` for financial calculations.
- Mock external boundaries, not internal domain mappers or business rules.
- Every external side effect must be idempotent or protected by an atomic claim.
- Never use production credentials in tests.
- Never call real provider write endpoints from tests.
- Never test first on production.
- Authenticated 401/403 in smoke tests is always a failure.
- Every fix requires a regression test, acceptance criteria and rollback.
- Stop and report a blocker when the business rule is ambiguous, a production secret/write call is required or migration has no rollback.

### Required completion report

Copilot must report:

1. root cause,
2. changed files,
3. behavior before/after,
4. tests added/changed,
5. exact commands executed,
6. test results and coverage,
7. remaining risk,
8. proposed commit message.

## 14. GitHub Issue backlog

The repository package contains ready issue bodies under `issues/`.

| # | Issue | Release |
|---|---|---|
| 1 | R4-A: strict staging smoke validation | R4 consolidation |
| 2 | R4-B: correlation/auth hardening | R4 consolidation |
| 3 | R4-C: Azure monitoring verification | R4 consolidation |
| 4 | R4.1: recover partially created invoices | R4 |
| 5 | R4.2: unify deposit calculation | R4 |
| 6 | R4.3: invoice preview parity | R4 |
| 7 | R5-A: atomic shipment state machine | R5 |
| 8 | R5-B: label readiness and bulk printing | R5 |
| 9 | R6: frontend/contracts/fake providers/E2E | R6 |

Create all issues with:

```bash
gh auth login
./create-github-issues.sh PiotrGry/zdrovena-reconciliation
```
