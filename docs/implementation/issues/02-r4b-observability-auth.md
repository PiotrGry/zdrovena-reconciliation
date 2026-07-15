# R4-B: harden correlation ID propagation and fail-closed production auth configuration

## Goal
Make correlation IDs safe and deterministic, and prevent an ambiguous production environment from starting with disabled auth.

## Scope
- `zdrovena/common/correlation.py`
- `zdrovena/api/observability.py`
- `zdrovena/api/main.py`
- background task entry points in `zdrovena/api/routers/webhooks.py`
- observability/startup tests

## Required changes
- Validate incoming correlation ID length and allowed characters.
- Generate a safe ID when invalid.
- Use ContextVar token/reset in `finally` inside background execution.
- Require one canonical `APP_ENV`.
- Production startup must fail closed when `APP_ENV` is missing/unknown or auth is disabled.
- Mask high-risk PII fields in structured events.

## Out of scope
- No tracing backend migration.
- No OpenTelemetry rollout.
- No business workflow refactor.

## Acceptance criteria
- [ ] Invalid/oversized ID is replaced.
- [ ] Context does not leak between tasks.
- [ ] Production cannot start with ambiguous environment/auth settings.
- [ ] Existing correlation headers and error envelope still work.
- [ ] PII masking tests exist.

## Copilot completion report
- Changed files:
- Commands executed:
- Test results:
- Remaining risks:
- Proposed commit message:
