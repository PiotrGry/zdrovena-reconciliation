# R5-A: add atomic draft execution claim and enforce shipment state transitions

## Goal
Prevent duplicate courier shipments and invalid state transitions during concurrent execution, retry and fulfillment.

## Scope
- ShippingStore claim/update operations
- `execute_draft`
- `mark_fulfilled`
- retry paths
- concurrency/state-machine tests

## Required changes
- Add an atomic execution claim using optimistic concurrency/ETag.
- Define allowed state transitions explicitly.
- Reject execute when already claimed/created/completed.
- Reject fulfillment for cancelled/error states unless an explicit recovery path exists.
- Release or expire claims safely after failures.

## Tests
- two concurrent execute requests create at most one shipment,
- retry after timeout does not duplicate side effects,
- invalid transitions return deterministic domain errors,
- stale claim recovery if designed.

## Acceptance criteria
- [ ] One draft creates at most one courier shipment.
- [ ] State transitions are centrally defined and tested.
- [ ] Retry remains possible after transient failure.

## Copilot completion report
- Changed files:
- Commands executed:
- Test results:
- Remaining risks:
- Proposed commit message:
