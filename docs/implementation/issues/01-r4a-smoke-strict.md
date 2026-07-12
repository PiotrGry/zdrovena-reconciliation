# R4-A: make staging smoke validation strict and eliminate false-green 401/SKIP results

## Goal
Make release validation prove that authenticated staging flows work.

## Current behavior
Critical auth/business tests may return `SKIP`, while anonymous 401 tests pass. The runner exits non-zero only when `failed > 0`, so the workflow can stay green without a working authenticated flow.

## Scope
- `scripts/smoke/runner.ts`
- `scripts/smoke/tests/auth-real.ts`
- `scripts/smoke/tests/business.ts`
- `.github/workflows/release-validation.yml`

## Required changes
- Add `--strict` / `SMOKE_STRICT=true`.
- Missing required credentials/token in strict mode is `FAIL`.
- Authenticated 401/403 is always `FAIL`.
- Keep anonymous 401/403 tests as valid negative tests.
- Unexpected authenticated non-2xx must not become `SKIP`.
- Warm-up without HTTP 200 exits non-zero.
- Print required pass/fail/skip summary.

## Out of scope
- No Entra tenant/app-registration changes.
- No production calls.
- No close/invoicing business logic changes.

## Acceptance criteria
- [ ] Missing smoke credentials makes release validation red.
- [ ] Invalid token or authenticated 401/403 makes release validation red.
- [ ] Anonymous rejection tests stay green on 401/403.
- [ ] Failed warm-up exits non-zero.
- [ ] Local non-strict mode may skip optional tests.

## Validation
```bash
cd scripts/smoke
npm ci
npx tsx runner.ts --output report.json
SMOKE_STRICT=true npx tsx runner.ts --output strict-report.json
```

## Copilot completion report
- Changed files:
- Commands executed:
- Test results:
- Remaining risks:
- Proposed commit message:
