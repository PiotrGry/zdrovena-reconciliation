# Copilot instructions - zdrovena-reconciliation

## Operating model
- Work on exactly one GitHub issue at a time.
- Treat the issue scope and acceptance criteria as binding.
- Keep `develop` green and use one conventional commit per implementation unit.
- Do not push directly to `main`.

## Safety and business boundaries
- Never test write operations on production.
- Never use production credentials in tests.
- Never call real Allegro, InPost, Apaczka or Fakturownia write endpoints from tests.
- Do not modify KSeF, monthly-close or invoicing logic outside the explicit issue scope.
- Use `Decimal` for financial calculations; do not introduce `float`.
- Every external side effect must be idempotent or protected by an atomic claim.

## Tests
- Do not delete, weaken, skip or xfail tests only to make CI green.
- Mock external boundaries, not internal domain mappers or business rules.
- Authenticated 401/403 in smoke tests is always a failure.
- Add a regression test for every fixed failure mode.

## Completion
Report:
1. root cause,
2. changed files,
3. behavior before/after,
4. tests added/changed,
5. exact commands executed,
6. results and coverage,
7. remaining risk,
8. proposed commit message.

Stop and report a blocker when requirements are ambiguous, a production secret/write call is required, or a migration lacks a rollback plan.
