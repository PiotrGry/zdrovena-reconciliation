# R4.1: recover partially created Allegro invoices instead of looping on HTTP 502

## Goal
A retry after partial invoice creation must resume the process without creating a duplicate invoice or resetting the existing invoice relationship.

## Scope
- invoice creation endpoint/flow in `zdrovena/api/routers/webhooks.py`
- `zdrovena/common/allegro_invoicer.py`
- relevant store/state fields
- invoice endpoint regression tests

## Current failure mode
1. Fakturownia invoice is created.
2. A later Allegro/PDF/settlement step fails.
3. Stored invoice ID is reset or lost.
4. Retry gets `already_exists`.
5. Endpoint returns 502 repeatedly.

## Expected behavior
- Locate/recover the existing invoice.
- Persist its ID.
- Resume at the first incomplete step.
- Never create a second invoice for the same order.

## Out of scope
- No KSeF changes.
- No monthly-close changes.
- No broad `webhooks.py` split.

## Tests
- partial success then retry,
- existing invoice recovery,
- two repeated retries,
- no duplicate invoice,
- state remains resumable after transient failure.

## Acceptance criteria
- [ ] The known 502-loop order completes.
- [ ] No duplicate invoice is created.
- [ ] Retry is idempotent.
- [ ] Full backend tests remain green.

## Copilot completion report
- Changed files:
- Commands executed:
- Test results:
- Remaining risks:
- Proposed commit message:
