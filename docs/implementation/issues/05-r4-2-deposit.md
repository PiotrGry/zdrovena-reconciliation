# R4.2: unify Allegro deposit calculation and keep monetary values Decimal-safe

## Goal
Use one authoritative deposit calculation so mapper, patcher, preview and final invoice cannot disagree.

## Scope
- Allegro invoice mapper
- Fakturownia patcher
- bottle/deposit helper
- invoice preview
- focused tests and anonymized contract fixtures

## Required behavior
- Prefer native Allegro `deposit.price.amount * quantity`.
- Use one explicitly documented fallback only when native data is absent.
- Validate quantity; do not silently turn zero into one.
- Keep values as `Decimal` until serialization.
- Prevent full deposit from being added to every invoice when an order has multiple invoices.

## Out of scope
- No tax-rate policy changes.
- No KSeF changes.

## Acceptance criteria
- [ ] Mapper and patcher return the same deposit.
- [ ] Multiple invoices cannot receive duplicated full deposit.
- [ ] Zero/invalid quantity follows an explicit tested rule.
- [ ] No float-based monetary arithmetic is introduced.

## Copilot completion report
- Changed files:
- Commands executed:
- Test results:
- Remaining risks:
- Proposed commit message:
