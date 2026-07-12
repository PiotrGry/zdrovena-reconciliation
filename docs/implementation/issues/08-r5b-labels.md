# R5-B: add LABEL_NOT_READY handling and batch label printing

## Goal
Give the operator a clear label-readiness message and support printing selected labels as one document.

## Scope
- courier label clients
- shipping label endpoints
- `ShippingView.jsx`
- PDF merge dependency/helper if required
- backend/frontend tests

## Required changes
- Map InPost pre-confirmation/invalid-action to `LABEL_NOT_READY` HTTP 409.
- Replace generic 502/alert with Polish informational toast.
- Add `POST /shipping/labels/batch` with `draft_ids`.
- Group by courier.
- Use provider bulk APIs where available; merge PDFs when required.
- Enforce provider limits and return useful validation errors.
- Optional: A6/A4 and PDF/ZPL selection.

## Acceptance criteria
- [ ] Single label before confirmed gives a clear non-500 response.
- [ ] Selected drafts produce one printable output.
- [ ] Unsupported/mixed error cases are deterministic.
- [ ] Existing single-label flow remains compatible.

## Copilot completion report
- Changed files:
- Commands executed:
- Test results:
- Remaining risks:
- Proposed commit message:
