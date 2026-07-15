# R4.3: make invoice preview match the final invoice and Allegro totalToPay

## Goal
The operator must see the same VAT, positions, settlement deposit and payable total that will be used by the real invoice flow.

## Scope
- invoice preview endpoint
- preview response model
- mapper output fields
- frontend preview rendering if required
- contract and endpoint tests

## Required changes
- Read VAT from the actual mapper field.
- Show deposit as settlement position when that is how final invoice is built.
- Compare expected payable amount to Allegro `summary.totalToPay` after documented delivery handling.
- Return a mismatch flag with an explainable difference.

## Acceptance criteria
- [ ] Preview and final payload use identical calculation helpers.
- [ ] VAT is correct.
- [ ] `difference == 0.00` for representative fixtures.
- [ ] Mismatch is visible and blocks unsafe automatic creation when required.

## Copilot completion report
- Changed files:
- Commands executed:
- Test results:
- Remaining risks:
- Proposed commit message:
