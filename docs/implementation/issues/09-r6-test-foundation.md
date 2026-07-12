# R6: add frontend tests, generated API contracts, fake providers and critical E2E flows

## Goal
Enable safe development without production testing and make CI validate the real frontend-backend-provider contract.

## Scope
- frontend Vitest + React Testing Library
- FastAPI OpenAPI -> TypeScript generation
- local HTTP fake providers
- Playwright critical flows
- CI wiring and test documentation

## Required changes
- Add component tests for ShippingView, DLQ, polling, errors and addresses.
- Generate TS API types/client from OpenAPI and fail CI on stale output.
- Implement strict stateful fake Allegro/InPost/Apaczka/Fakturownia services.
- Route real application clients to fake HTTP endpoints in test mode.
- Add E2E flows:
  1. webhook -> draft -> UI,
  2. execute InPost -> tracking,
  3. provider 500 -> Polish toast + correlation ID,
  4. preview -> create invoice -> amount parity,
  5. DLQ -> retry -> draft.

## Guardrails
- Do not mock internal domain mappers in integration tests.
- Fake services must validate request contracts.
- No real provider write calls.

## Acceptance criteria
- [ ] Frontend tests run in CI.
- [ ] Contract drift fails CI.
- [ ] Five critical E2E flows pass against fake providers.
- [ ] Test documentation supports local execution.

## Copilot completion report
- Changed files:
- Commands executed:
- Test results:
- Remaining risks:
- Proposed commit message:
