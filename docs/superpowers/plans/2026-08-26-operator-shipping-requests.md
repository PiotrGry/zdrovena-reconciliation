# Operator Shipping Requests (Aug 2026) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator a correctly named label PDF, one tracking number per parcel, an editable parcel plan, and the carrier's pickup-order id.

**Architecture:** Four independent slices over the existing shipping stack. Titles are computed by a new pure module in `zdrovena/shipping/domain/` and applied in the one place label bytes are assembled. Tracking numbers and pickup ids are already persisted — they only need rendering, except Apaczka's, which needs a new `order/:id/` call plus a poller pass. The parcel plan gains a validated write path on the existing `PATCH /shipping/drafts/{id}` and an editable table extracted into `frontend/src/views/shipping/`.

**Tech Stack:** Python 3.12, FastAPI, pypdf, pytest; React 18 + Vite + Vitest + Testing Library; Azure Table Storage (schemaless — no migration needed for the new fields).

**Spec:** `docs/superpowers/specs/2026-08-26-operator-shipping-requests-design.md`

---

## File Structure

**Create:**
- `zdrovena/shipping/domain/labels.py` — pure title strings for printable label documents. No I/O, no FastAPI.
- `zdrovena/api/routers/apaczka_pickup_poller.py` — one resolution cycle filling in missing Apaczka pickup numbers. Mirrors `inpost_poller.py` exactly.
- `frontend/src/views/shipping/TrackingList.jsx` — renders every parcel's tracking number.
- `frontend/src/views/shipping/PackagesEditor.jsx` — the editable TYP/SZT. table.
- `tests/test_shipping_labels.py`, `tests/test_apaczka_pickup_poller.py`
- `frontend/src/views/shipping/TrackingList.test.jsx`, `frontend/src/views/shipping/PackagesEditor.test.jsx`

**Modify:**
- `zdrovena/api/routers/webhooks.py` — label titling, `packages_breakdown` in `PATCH`
- `zdrovena/common/apaczka.py` — `get_order()`
- `zdrovena/api/shipping_execution_composition.py` — capture `pickup_number`
- `zdrovena/api/shipping_draft_composition.py` — seed `packages_source`
- `zdrovena/shipping/application/drafts.py` — preserve an operator plan across resync
- `zdrovena/api/commands/allegro_poll_cmd.py` — run the new poller
- `zdrovena/fake_providers/apaczka.py`, `zdrovena/fake_providers/common.py` — real PDF bytes, `order/:id/`
- `frontend/src/views/ShippingView.jsx` — wire the two new components, pickup-id row
- `docs/audit/shipment-provider-contracts.md`, `CHANGELOG.md`, `contracts/openapi.json`, `frontend/src/api/generated/schema.d.ts`

**Language convention (project rule):** docstrings, comments, and log messages in English. Operator-visible strings — PDF titles, HTTP `detail` bodies that reach a toast, UI copy — in Polish.

---

## Task 1: Pure label titles

**Files:**
- Create: `zdrovena/shipping/domain/labels.py`
- Test: `tests/test_shipping_labels.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for zdrovena.shipping.domain.labels.

Chrome takes the "Save as PDF" filename from the printed document's /Title.
These titles are what the operator ends up seeing in the save dialog, so the
date has to be the Polish one, not the server's UTC one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from zdrovena.shipping.domain.labels import batch_label_title, single_label_title


class TestBatchLabelTitle:
    def test_uses_the_operator_requested_wording_and_day(self):
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))
        assert batch_label_title(moment) == "Etykiety portal 26.08"

    def test_reads_the_warsaw_day_not_the_utc_one(self):
        # 23:30 UTC on the 25th is 01:30 on the 26th in Warsaw. The container
        # runs on UTC, so without the conversion a late batch is misdated.
        moment = datetime(2026, 8, 25, 23, 30, tzinfo=timezone.utc)
        assert batch_label_title(moment) == "Etykiety portal 26.08"


class TestSingleLabelTitle:
    def test_keeps_the_order_number(self):
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))
        assert single_label_title("1723", moment) == "Etykieta 1723 26.08"

    def test_strips_the_shopify_hash(self):
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))
        assert single_label_title("#1723", moment) == "Etykieta 1723 26.08"

    def test_falls_back_when_there_is_no_order_number(self):
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))
        assert single_label_title("", moment) == "Etykieta bez numeru 26.08"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_shipping_labels.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zdrovena.shipping.domain.labels'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Pure title strings for the printable label documents the operator saves."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


def _day_stamp(now: datetime | None) -> str:
    """Return the Polish calendar day as dd.mm.

    The container clock is UTC. A sheet printed at 23:30 CEST would carry
    tomorrow's date without this conversion, which is exactly the kind of
    quiet wrongness a filename is supposed to prevent.
    """
    moment = now or datetime.now(WARSAW)
    return moment.astimezone(WARSAW).strftime("%d.%m")


def batch_label_title(now: datetime | None = None) -> str:
    """Title for the merged sheet printed for a whole batch of drafts."""
    return f"Etykiety portal {_day_stamp(now)}"


def single_label_title(order_number: str, now: datetime | None = None) -> str:
    """Title for one draft's label.

    The order number stays: with a single label on the page it is the
    identifying information, and the operator's naming request was about the
    batch sheet.
    """
    order = str(order_number or "").lstrip("#").strip() or "bez numeru"
    return f"Etykieta {order} {_day_stamp(now)}"


__all__ = ["WARSAW", "batch_label_title", "single_label_title"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_shipping_labels.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add zdrovena/shipping/domain/labels.py tests/test_shipping_labels.py
git commit -m "feat(shipping): tytuły dokumentów etykiet liczone w czasie warszawskim"
```

---

## Task 2: Apply the title to every label response

Today `_fetch_label_pdf` merges a draft's parcels, and `batch_labels` merges the results again — a double pass. Collapse it: `_fetch_label_pdfs` returns the list, and the endpoints assemble once with a title.

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py` (`_safe_label_filename` ~1396, `_fetch_label_pdf` ~1406, `_merge_pdfs` ~1464, `batch_labels` ~1487, `get_label` ~1559)
- Test: `tests/test_shipping_webhook.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_shipping_webhook.py`, in the class that already covers labels (the one holding `test_inpost_label_returns_pdf`):

```python
    @staticmethod
    def _real_pdf() -> bytes:
        """A parsable one-page PDF. `b"%PDF-1.4 fake"` is not one: pypdf raises
        PdfReadError on it, so it cannot exercise the titling path."""
        import io as _io

        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=300)
        buffer = _io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    @staticmethod
    def _title_of(pdf_bytes: bytes) -> str | None:
        import io as _io

        from pypdf import PdfReader

        return PdfReader(_io.BytesIO(pdf_bytes)).metadata.title

    def test_single_label_carries_the_dated_title(self, client, store):
        draft = self._seed_created_draft(store, courier="inpost")
        store.update_draft(draft["id"], {"shopify_order_number": "1723"})
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))

        with (
            patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"),
            patch(
                "zdrovena.common.inpost.InPostClient.get_label",
                return_value=self._real_pdf(),
            ),
            patch("zdrovena.api.routers.webhooks._now_warsaw", return_value=moment),
        ):
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/label?courier=inpost")

        assert resp.status_code == 200
        assert self._title_of(resp.content) == "Etykieta 1723 26.08"
        assert resp.headers["content-disposition"] == (
            'inline; filename="Etykieta 1723 26.08.pdf"'
        )

    def test_batch_label_carries_the_portal_title(self, client, store):
        draft = self._seed_created_draft(store, courier="inpost")
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))

        with (
            patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"),
            patch(
                "zdrovena.common.inpost.InPostClient.get_label",
                return_value=self._real_pdf(),
            ),
            patch("zdrovena.api.routers.webhooks._now_warsaw", return_value=moment),
        ):
            resp = client.post(
                "/api/shipping/labels/batch", json={"draft_ids": [draft["id"]]}
            )

        assert resp.status_code == 200
        assert self._title_of(resp.content) == "Etykiety portal 26.08"
        assert resp.headers["content-disposition"] == (
            'inline; filename="Etykiety portal 26.08.pdf"'
        )

    def test_an_unparsable_carrier_pdf_is_still_returned(self, client, store):
        # A label the operator cannot print is a worse failure than an ugly
        # filename, so titling degrades instead of raising.
        draft = self._seed_created_draft(store, courier="inpost")

        with (
            patch("zdrovena.api.routers.webhooks.get_secret", return_value="tok"),
            patch(
                "zdrovena.common.inpost.InPostClient.get_label",
                return_value=b"%PDF-1.4 not really a pdf",
            ),
        ):
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/label?courier=inpost")

        assert resp.status_code == 200
        assert resp.content == b"%PDF-1.4 not really a pdf"
```

Add to that file's imports if missing:

```python
from datetime import datetime
from zoneinfo import ZoneInfo
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_shipping_webhook.py -k "dated_title or portal_title or unparsable_carrier" -v`
Expected: FAIL — `AttributeError: <module 'zdrovena.api.routers.webhooks'> does not have the attribute '_now_warsaw'`

- [ ] **Step 3: Write the implementation**

In `zdrovena/api/routers/webhooks.py`, replace `_safe_label_filename` with a title-driven version and add the clock seam:

```python
def _now_warsaw() -> datetime:
    """Single seam so tests can freeze the day used in label titles."""
    return datetime.now(WARSAW)


def _label_filename(title: str) -> str:
    """Return an ASCII-only filename safe for a quoted response header."""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9 ._-]+", "_", ascii_title).strip(" ._-")
    return f"{safe[:80] or 'label'}.pdf"
```

Imports at the top of the module:

```python
from datetime import datetime

from zdrovena.shipping.domain.labels import WARSAW, batch_label_title, single_label_title
```

Replace `_merge_pdfs` with:

```python
def _titled_pdf(pdfs: list[bytes], title: str) -> bytes:
    """Assemble label PDFs into one document carrying ``title`` as its /Title.

    Chrome takes the "Save as PDF" filename from the printed document's title.
    The label is printed from a ``blob:`` URL, which has no filename and does
    not carry Content-Disposition, so this metadata is the only lever we have.

    A single carrier PDF pypdf cannot parse is returned unchanged: an
    unprintable label is a worse failure than an untitled one. A multi-PDF
    merge still raises, because there is no meaningful fallback for it and
    that was already the behaviour.
    """
    from pypdf import PdfWriter

    try:
        writer = PdfWriter()
        for pdf in pdfs:
            writer.append(io.BytesIO(pdf))
        writer.add_metadata({"/Title": title})
        out = io.BytesIO()
        writer.write(out)
        writer.close()
        return out.getvalue()
    except Exception:
        if len(pdfs) != 1:
            raise
        logger.exception("Could not title a label PDF — streaming it untitled")
        return pdfs[0]
```

Rename `_fetch_label_pdf` to `_fetch_label_pdfs`, change its return annotation to `list[bytes]`, and replace each of its three `return _merge_pdfs(pdfs) if len(pdfs) > 1 else pdfs[0]` lines with `return pdfs`.

In `batch_labels`, replace the fetch loop body `pdfs.append(_fetch_label_pdf(d, d["courier"], storage))` with `pdfs.extend(_fetch_label_pdfs(d, d["courier"], storage))`, and the response with:

```python
    title = batch_label_title(_now_warsaw())
    return StreamingResponse(
        io.BytesIO(_titled_pdf(pdfs, title)),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{_label_filename(title)}"'},
    )
```

In `get_label`, replace the tail with:

```python
    pdfs = _fetch_label_pdfs(draft, courier, storage)
    title = single_label_title(str(draft.get("shopify_order_number") or ""), _now_warsaw())
    return StreamingResponse(
        io.BytesIO(_titled_pdf(pdfs, title)),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{_label_filename(title)}"'},
    )
```

- [ ] **Step 4: Update the existing filename-safety test**

`test_label_content_disposition_uses_safe_ascii_filename` asserts the old name. The injection assertions stay — only the expected string changes:

```python
        if order_number == "5000":
            assert disposition == 'inline; filename="Etykieta 5000 26.08.pdf"'
```

and the test must freeze the clock the same way the new tests do, plus return `self._real_pdf()` from `get_label` so the titled path runs. Keep every `\r`, `\n`, `/`, `\\`, and quote-count assertion exactly as it is: those guard header injection through the order number, and that risk is unchanged.

- [ ] **Step 5: Run the label tests**

Run: `.venv/bin/python -m pytest tests/test_shipping_webhook.py -k "label" -v`
Expected: PASS, no failures

- [ ] **Step 6: Update the frontend print titles to match**

`frontend/src/views/ShippingView.jsx` passes an iframe title that no longer matches what the document says. Keep the two in step — `handlePrintLabel`:

```js
            printPdf(blob, `Etykieta ${draft.shopify_order_number || draft.id}`)
```

becomes

```js
            printPdf(blob, labelSheetTitle(draft.shopify_order_number || draft.id))
```

and `handleBulkPrint`'s `printPdf(await res.blob(), \`Etykiety A6 (${selected.length})\`)` becomes `printPdf(await res.blob(), batchSheetTitle())`, with the two helpers added near `fmtDate`:

```js
function dayStamp() {
    // Matches zdrovena/shipping/domain/labels.py — the browser is already in
    // the operator's timezone, so no conversion is needed here.
    const now = new Date()
    return `${String(now.getDate()).padStart(2, '0')}.${String(now.getMonth() + 1).padStart(2, '0')}`
}

function batchSheetTitle() {
    return `Etykiety portal ${dayStamp()}`
}

function labelSheetTitle(orderNumber) {
    return `Etykieta ${String(orderNumber).replace(/^#/, '')} ${dayStamp()}`
}
```

- [ ] **Step 7: Run the frontend tests**

Run: `npm --prefix frontend test -- ShippingView`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py frontend/src/views/ShippingView.jsx
git commit -m "feat(shipping): nazwa zapisywanego PDF-a etykiet z dzisiejszą datą"
```

---

## Task 3: Make the emulator emit a parsable PDF

The emulator's `PDF_BYTES` is not a valid PDF, so e2e never exercises titling and a multi-parcel batch print against the emulator fails today.

**Files:**
- Modify: `zdrovena/fake_providers/common.py:16`
- Test: `tests/test_fake_providers.py`

- [ ] **Step 1: Write the failing test**

```python
def test_emulator_labels_are_parsable_pdfs():
    """The portal merges and titles label PDFs with pypdf. An emulator that
    serves bytes pypdf cannot read makes that path untestable end to end."""
    import io

    from pypdf import PdfReader

    from zdrovena.fake_providers.common import PDF_BYTES

    assert len(PdfReader(io.BytesIO(PDF_BYTES)).pages) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fake_providers.py -k parsable_pdfs -v`
Expected: FAIL — `pypdf.errors.PdfReadError: startxref not found`

- [ ] **Step 3: Write the implementation**

In `zdrovena/fake_providers/common.py`, replace the literal:

```python
def _blank_label_pdf() -> bytes:
    """One valid, empty A6-ish page.

    The portal runs every label through pypdf to merge parcels and set the
    document title. A hand-written byte string does not parse, so the emulator
    would only ever exercise the fallback path.
    """
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=298, height=420)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


PDF_BYTES = _blank_label_pdf()
```

- [ ] **Step 4: Run the fake-provider tests**

Run: `.venv/bin/python -m pytest tests/test_fake_providers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zdrovena/fake_providers/common.py tests/test_fake_providers.py
git commit -m "test(shipping): emulator serwuje parsowalny PDF etykiety"
```

---

## Task 4: Show one tracking number per parcel

**Files:**
- Create: `frontend/src/views/shipping/TrackingList.jsx`, `frontend/src/views/shipping/TrackingList.test.jsx`
- Modify: `frontend/src/views/ShippingView.jsx:946-961`

- [ ] **Step 1: Write the failing test**

`frontend/src/views/shipping/TrackingList.test.jsx`:

```jsx
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TrackingList } from './TrackingList'

describe('TrackingList', () => {
    it('renders one number per parcel with its position', () => {
        render(<TrackingList draft={{
            tracking_number: '620A',
            courier_shipments: [
                { id: 's1', tracking_number: '620A', package_type: '2-pak', package_number: '1' },
                { id: 's2', tracking_number: '620B', package_type: '2-pak', package_number: '2' },
                { id: 's3', tracking_number: '620C', package_type: 'szkło', package_number: '1' },
            ],
        }} />)

        expect(screen.getByText('620A')).toBeInTheDocument()
        expect(screen.getByText('620B')).toBeInTheDocument()
        expect(screen.getByText('620C')).toBeInTheDocument()
        expect(screen.getByText('Numery śledzenia (3)')).toBeInTheDocument()
        expect(screen.getByText('2-pak 1/2')).toBeInTheDocument()
        expect(screen.getByText('szkło 1/1')).toBeInTheDocument()
    })

    it('falls back to the single number on drafts that predate courier_shipments', () => {
        render(<TrackingList draft={{ tracking_number: '620LEGACY', courier_shipments: [] }} />)

        expect(screen.getByText('620LEGACY')).toBeInTheDocument()
        expect(screen.getByText('Numer śledzenia')).toBeInTheDocument()
    })

    it('renders a dash when nothing has a number yet', () => {
        render(<TrackingList draft={{ tracking_number: null, courier_shipments: [] }} />)

        expect(screen.getByText('—')).toBeInTheDocument()
    })

    it('skips parcels the carrier has not numbered yet', () => {
        render(<TrackingList draft={{
            tracking_number: '620A',
            courier_shipments: [
                { id: 's1', tracking_number: '620A', package_type: '1-pak', package_number: '1' },
                { id: 's2', tracking_number: '', package_type: '1-pak', package_number: '2' },
            ],
        }} />)

        expect(screen.getByText('Numery śledzenia (1)')).toBeInTheDocument()
        expect(screen.getByText('1 z 2 paczek czeka na numer')).toBeInTheDocument()
    })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix frontend test -- TrackingList`
Expected: FAIL — cannot resolve `./TrackingList`

- [ ] **Step 3: Write the implementation**

`frontend/src/views/shipping/TrackingList.jsx`:

```jsx
/**
 * Every parcel of a draft gets its own carrier shipment and its own tracking
 * number. The view used to render only draft.tracking_number — the first one —
 * so a three-parcel order looked like a one-parcel order.
 */
export function TrackingList({ draft }) {
    const shipments = draft.courier_shipments || []
    const numbered = shipments.filter(s => String(s.tracking_number || '').trim())

    if (!numbered.length) {
        return (
            <>
                <div className="detail-label">Numer śledzenia</div>
                <div>
                    {draft.tracking_number
                        ? <TrackingNumber value={draft.tracking_number} />
                        : <span className="dim">—</span>}
                </div>
            </>
        )
    }

    const pending = shipments.length - numbered.length
    const countByType = shipments.reduce((acc, s) => {
        acc[s.package_type] = (acc[s.package_type] || 0) + 1
        return acc
    }, {})

    return (
        <>
            <div className="detail-label">Numery śledzenia ({numbered.length})</div>
            <div style={{ display: 'grid', gap: 4 }}>
                {numbered.map(shipment => (
                    <div key={shipment.id || shipment.tracking_number}
                        style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                        <span className="dim" style={{ fontSize: '0.82em', minWidth: 86 }}>
                            {shipment.package_type} {shipment.package_number}/{countByType[shipment.package_type]}
                        </span>
                        <TrackingNumber value={shipment.tracking_number} />
                    </div>
                ))}
            </div>
            {pending > 0 && (
                <div className="dim" style={{ fontSize: '0.82em', marginTop: 4 }}>
                    {pending} z {shipments.length} paczek czeka na numer
                </div>
            )}
        </>
    )
}

function TrackingNumber({ value }) {
    return (
        <span className="mono copyable" title="Kliknij żeby skopiować"
            onClick={() => navigator.clipboard.writeText(value)}
            style={{ cursor: 'pointer' }}>
            {value}
        </span>
    )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix frontend test -- TrackingList`
Expected: PASS, 4 passed

- [ ] **Step 5: Wire it into the draft row**

In `frontend/src/views/ShippingView.jsx`, add `import { TrackingList } from './shipping/TrackingList'` alongside the other view imports, then replace the block at lines 947-958 (the `detail-label` "Numer śledzenia" heading through the closing `</div>` of its value) with:

```jsx
                                <TrackingList draft={draft} />
```

Leave the "ID draftu kuriera" lines that follow untouched.

- [ ] **Step 6: Run the view tests**

Run: `npm --prefix frontend test -- ShippingView`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add frontend/src/views/shipping/TrackingList.jsx frontend/src/views/shipping/TrackingList.test.jsx frontend/src/views/ShippingView.jsx
git commit -m "feat(shipping): numer śledzenia dla każdej paczki, nie tylko pierwszej"
```

---

## Task 5: Accept an operator parcel plan on PATCH

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py:1329-1388` (`update_draft`)
- Test: `tests/test_shipping_webhook.py`

Error `detail` strings here are Polish on purpose: every one of them can reach the operator's toast through `apiErrorMessage`.

- [ ] **Step 1: Write the failing test**

```python
class TestUpdateDraftPackagesBreakdown:
    def test_replaces_the_plan_and_recomputes_the_count(self, client, store):
        draft = self._seed_pending_draft(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}",
            json={"packages_breakdown": [{"type": "szkło", "qty": 2}, {"type": "1-pak", "qty": 1}]},
        )
        assert resp.status_code == 200
        updated = store.get_draft(draft["id"])
        assert updated["packages_breakdown"] == [
            {"type": "szkło", "qty": 2},
            {"type": "1-pak", "qty": 1},
        ]
        assert updated["packages_count"] == 3
        assert updated["packages_source"] == "operator"

    def test_rejects_a_type_outside_the_catalogue(self, client, store):
        draft = self._seed_pending_draft(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}",
            json={"packages_breakdown": [{"type": "karton", "qty": 1}]},
        )
        assert resp.status_code == 400
        assert "karton" in resp.json()["detail"]

    def test_rejects_an_empty_plan(self, client, store):
        draft = self._seed_pending_draft(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}", json={"packages_breakdown": []}
        )
        assert resp.status_code == 400

    def test_rejects_a_quantity_outside_one_to_ninetynine(self, client, store):
        draft = self._seed_pending_draft(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}",
            json={"packages_breakdown": [{"type": "1-pak", "qty": 0}]},
        )
        assert resp.status_code == 400

    def test_rejects_more_than_thirty_parcels(self, client, store):
        draft = self._seed_pending_draft(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}",
            json={"packages_breakdown": [{"type": "1-pak", "qty": 31}]},
        )
        assert resp.status_code == 400

    def test_rejects_both_fields_at_once(self, client, store):
        draft = self._seed_pending_draft(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}",
            json={"packages_count": 2, "packages_breakdown": [{"type": "1-pak", "qty": 1}]},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "status", ["executing", "pending_confirmation", "created", "cancelled"]
    )
    def test_409_once_the_shipment_exists_at_the_carrier(self, client, store, status):
        # Past this point the plan is the audit record of what was sent, not a
        # draft. Editing it would make the stored plan disagree with the labels.
        draft = self._seed_pending_draft(store)
        store.update_draft(draft["id"], {"status": status})
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}",
            json={"packages_breakdown": [{"type": "1-pak", "qty": 2}]},
        )
        assert resp.status_code == 409

    def test_400_when_a_cod_draft_would_get_more_than_one_parcel(self, client, store):
        # apaczka_call_specs refuses multi-parcel COD, because one full
        # collection amount per parcel charges the customer several times.
        # Saying so at save time beats failing at execute time.
        draft = self._seed_pending_draft(store)
        store.update_draft(draft["id"], {"cod": {"amount": 20030, "currency": "PLN"}})
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}",
            json={"packages_breakdown": [{"type": "1-pak", "qty": 2}]},
        )
        assert resp.status_code == 400

    def test_a_cod_draft_may_still_be_repacked_into_one_parcel(self, client, store):
        draft = self._seed_pending_draft(store)
        store.update_draft(draft["id"], {"cod": {"amount": 20030, "currency": "PLN"}})
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}",
            json={"packages_breakdown": [{"type": "szkło", "qty": 1}]},
        )
        assert resp.status_code == 200
```

Add a `_seed_pending_draft` helper to that class if the file has no equivalent:

```python
    @staticmethod
    def _seed_pending_draft(store):
        draft = {
            "id": "draft-packages-1",
            "shopify_order_number": "1801",
            "courier": "apaczka",
            "service": "apaczka",
            "apaczka_service_id": "21",
            "status": "pending",
            "packages_count": 1,
            "packages_breakdown": [{"type": "1-pak", "qty": 1}],
            "packages_source": "planner",
            "courier_shipments": [],
        }
        store.upsert_draft(draft)
        return draft
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_shipping_webhook.py -k UpdateDraftPackagesBreakdown -v`
Expected: FAIL — the PATCH ignores the unknown body field, so `packages_breakdown` is unchanged and `packages_source` is absent

- [ ] **Step 3: Write the implementation**

In `zdrovena/api/routers/webhooks.py`, above `update_draft`:

```python
_MAX_BREAKDOWN_ROWS = 20
_MAX_TOTAL_PARCELS = 30
# Past these statuses the parcel plan describes shipments that already exist at
# the carrier. Editing it would make the record disagree with the printed labels.
_BREAKDOWN_LOCKED_STATUSES = frozenset(
    {"executing", "pending_confirmation", "created", "cancelled"}
)


def _validated_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise an operator parcel plan or raise a 400 the operator can read."""
    from zdrovena.common.shipping_parcels import PARCEL_SPECS

    if not rows:
        raise HTTPException(status_code=400, detail="Plan paczek nie może być pusty")
    if len(rows) > _MAX_BREAKDOWN_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"Za dużo pozycji w planie paczek (maksymalnie {_MAX_BREAKDOWN_ROWS})",
        )

    cleaned: list[dict[str, Any]] = []
    for row in rows:
        package_type = str(row.get("type") or "").strip()
        if package_type not in PARCEL_SPECS:
            raise HTTPException(status_code=400, detail=f"Nieznany typ paczki: {package_type}")
        try:
            qty = int(row.get("qty"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"Liczba sztuk dla {package_type} musi być liczbą całkowitą",
            ) from None
        if not 1 <= qty <= 99:
            raise HTTPException(
                status_code=400,
                detail=f"Liczba sztuk dla {package_type} musi mieścić się w zakresie 1–99",
            )
        cleaned.append({"type": package_type, "qty": qty})

    total = sum(row["qty"] for row in cleaned)
    if total > _MAX_TOTAL_PARCELS:
        raise HTTPException(
            status_code=400,
            detail=f"Za dużo paczek w jednym zamówieniu ({total}, maksymalnie {_MAX_TOTAL_PARCELS})",
        )
    return cleaned
```

Add the parameter to `update_draft`'s signature, after `packages_count`:

```python
    packages_breakdown: list[dict[str, Any]] | None = Body(None),
```

and the branch, immediately after the existing `if packages_count is not None:` block:

```python
    if packages_breakdown is not None:
        if packages_count is not None:
            raise HTTPException(
                status_code=400,
                detail="Podaj plan paczek albo liczbę paczek, nie oba naraz",
            )
        if draft.get("status") in _BREAKDOWN_LOCKED_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="Nie można zmienić paczek po wysłaniu przesyłki do kuriera",
            )
        cleaned = _validated_breakdown(packages_breakdown)
        total = sum(row["qty"] for row in cleaned)
        if draft.get("cod") and total != 1:
            raise HTTPException(
                status_code=400,
                detail="Przesyłka pobraniowa musi mieścić się w jednej paczce",
            )
        logger.info(
            "Operator repacked draft %s: %s -> %s",
            draft_id,
            draft.get("packages_breakdown"),
            cleaned,
        )
        patch["packages_breakdown"] = cleaned
        patch["packages_count"] = total
        patch["packages_source"] = "operator"
```

Update the endpoint `summary` to `"Update draft metadata (packages_breakdown, service, locker_id)"` and the module docstring line 9 to match.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_shipping_webhook.py -k UpdateDraftPackagesBreakdown -v`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git commit -m "feat(shipping): operator może poprawić plan paczek przed wysyłką"
```

---

## Task 6: Keep an operator plan across a Shopify resync

**Files:**
- Modify: `zdrovena/api/shipping_draft_composition.py:502`, `zdrovena/shipping/application/drafts.py:116-198`
- Test: `tests/test_shipping_draft_application.py`

- [ ] **Step 1: Write the failing test**

```python
class TestOperatorParcelPlanSurvivesSync:
    def test_keeps_the_operator_plan_and_its_count(self):
        # The planner recomputes on every sync. Without this, the operator's
        # correction is silently reverted and the wrong boxes ship — which is
        # exactly how #1710-#1712 went out.
        existing = {
            "id": "d1",
            "status": "pending",
            "packages_source": "operator",
            "packages_breakdown": [{"type": "szkło", "qty": 2}],
            "packages_count": 2,
        }
        incoming = {
            "id": "d1",
            "status": "pending",
            "packages_source": "planner",
            "packages_breakdown": [{"type": "1-pak", "qty": 1}],
            "packages_count": 1,
        }

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *a: None)

        assert merged["packages_breakdown"] == [{"type": "szkło", "qty": 2}]
        assert merged["packages_count"] == 2
        assert merged["packages_source"] == "operator"

    def test_a_planner_plan_is_still_replaced_by_a_fresh_one(self):
        existing = {
            "id": "d1",
            "status": "pending",
            "packages_source": "planner",
            "packages_breakdown": [{"type": "1-pak", "qty": 1}],
            "packages_count": 1,
        }
        incoming = {
            "id": "d1",
            "status": "pending",
            "packages_source": "planner",
            "packages_breakdown": [{"type": "3-pak", "qty": 1}],
            "packages_count": 1,
        }

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *a: None)

        assert merged["packages_breakdown"] == [{"type": "3-pak", "qty": 1}]

    def test_a_draft_written_before_the_field_existed_is_treated_as_planner(self):
        existing = {
            "id": "d1",
            "status": "pending",
            "packages_breakdown": [{"type": "1-pak", "qty": 1}],
            "packages_count": 1,
        }
        incoming = {
            "id": "d1",
            "status": "pending",
            "packages_source": "planner",
            "packages_breakdown": [{"type": "3-pak", "qty": 1}],
            "packages_count": 1,
        }

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *a: None)

        assert merged["packages_breakdown"] == [{"type": "3-pak", "qty": 1}]
```

Import `merge_synced_draft` from `zdrovena.shipping.application.drafts` at the top of the test file if it is not already imported.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_shipping_draft_application.py -k OperatorParcelPlanSurvivesSync -v`
Expected: FAIL — the first test gets `[{"type": "1-pak", "qty": 1}]`, because `{**existing, **incoming}` lets the recomputed plan win

- [ ] **Step 3: Write the implementation**

In `zdrovena/api/shipping_draft_composition.py`, next to `"packages_count": packages_count,` add:

```python
        "packages_source": "planner",
```

In `zdrovena/shipping/application/drafts.py`, add the constant near `_MATCH_MANUAL`:

```python
_PACKAGES_SOURCE_OPERATOR = "operator"
```

and in `merge_synced_draft`, after the `apaczka_service_id` preservation block:

```python
    if existing.get("packages_source") == _PACKAGES_SOURCE_OPERATOR:
        # Preserved conditionally, like a manual Apaczka service override: a
        # plan the operator corrected outranks a recomputed one, but a draft
        # nobody touched still re-plans when the order changes.
        merged["packages_breakdown"] = existing["packages_breakdown"]
        merged["packages_count"] = existing["packages_count"]
        merged["packages_source"] = _PACKAGES_SOURCE_OPERATOR
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_shipping_draft_application.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole shipping suite to catch golden-record drift**

Run: `.venv/bin/python -m pytest tests/ -k shipping -v`
Expected: PASS. `packages_source` is a new key in built draft records, so any test asserting a whole record dict needs the key added — update those assertions, do not drop the field.

- [ ] **Step 6: Commit**

```bash
git add zdrovena/api/shipping_draft_composition.py zdrovena/shipping/application/drafts.py tests/
git commit -m "fix(shipping): synchronizacja z Shopify nie kasuje planu paczek od operatora"
```

---

## Task 7: Regenerate the API contracts

The PATCH body changed, and `scripts/check-api-contracts.sh` fails the gate on drift.

**Files:**
- Modify: `contracts/openapi.json`, `frontend/src/api/generated/schema.d.ts`

- [ ] **Step 1: Regenerate**

Run: `scripts/generate-api-contracts.sh`

- [ ] **Step 2: Verify no drift remains**

Run: `scripts/check-api-contracts.sh`
Expected: exits 0, no output about drift

- [ ] **Step 3: Commit**

```bash
git add contracts/openapi.json frontend/src/api/generated/schema.d.ts
git commit -m "chore(api): regeneracja kontraktów po zmianie PATCH /shipping/drafts"
```

---

## Task 8: Editable TYP and SZT. table

**Files:**
- Create: `frontend/src/views/shipping/PackagesEditor.jsx`, `frontend/src/views/shipping/PackagesEditor.test.jsx`
- Modify: `frontend/src/views/ShippingView.jsx:962-994`

- [ ] **Step 1: Write the failing test**

`frontend/src/views/shipping/PackagesEditor.test.jsx`:

```jsx
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { PackagesEditor } from './PackagesEditor'

function setup({ canEdit = true, breakdown = [{ type: '1-pak', qty: 1 }], onSave = vi.fn() } = {}) {
    render(
        <PackagesEditor
            breakdown={breakdown}
            canEdit={canEdit}
            onSave={onSave}
        />,
    )
    return { onSave }
}

describe('PackagesEditor', () => {
    it('sends the edited plan on save', async () => {
        const user = userEvent.setup()
        const { onSave } = setup()

        await user.selectOptions(screen.getByLabelText('Typ paczki 1'), 'szkło')
        await user.clear(screen.getByLabelText('Liczba sztuk 1'))
        await user.type(screen.getByLabelText('Liczba sztuk 1'), '3')
        await user.click(screen.getByRole('button', { name: 'Zapisz paczki' }))

        await waitFor(() => expect(onSave).toHaveBeenCalledWith([{ type: 'szkło', qty: 3 }]))
    })

    it('adds and removes rows', async () => {
        const user = userEvent.setup()
        const { onSave } = setup()

        await user.click(screen.getByRole('button', { name: 'Dodaj typ paczki' }))
        await user.selectOptions(screen.getByLabelText('Typ paczki 2'), 'szkło-2pak')
        await user.click(screen.getByRole('button', { name: 'Usuń typ paczki 1' }))
        await user.click(screen.getByRole('button', { name: 'Zapisz paczki' }))

        await waitFor(() => expect(onSave).toHaveBeenCalledWith([{ type: 'szkło-2pak', qty: 1 }]))
    })

    it('will not let the operator save an empty plan', async () => {
        const user = userEvent.setup()
        const { onSave } = setup()

        await user.click(screen.getByRole('button', { name: 'Usuń typ paczki 1' }))

        expect(screen.getByRole('button', { name: 'Zapisz paczki' })).toBeDisabled()
        expect(onSave).not.toHaveBeenCalled()
    })

    it('keeps an in-progress edit when the poll returns the same plan', async () => {
        // ShippingView refetches every 5s and hands back a new array each time.
        // Resetting on array identity would wipe the operator's edit mid-typing.
        const user = userEvent.setup()
        const { rerender } = render(
            <PackagesEditor breakdown={[{ type: '1-pak', qty: 1 }]} canEdit onSave={vi.fn()} />,
        )

        await user.selectOptions(screen.getByLabelText('Typ paczki 1'), 'szkło')
        rerender(
            <PackagesEditor breakdown={[{ type: '1-pak', qty: 1 }]} canEdit onSave={vi.fn()} />,
        )

        expect(screen.getByLabelText('Typ paczki 1')).toHaveValue('szkło')
    })

    it('renders read-only once the draft can no longer be edited', () => {
        setup({ canEdit: false, breakdown: [{ type: '2-pak', qty: 2 }] })

        expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
        expect(screen.getByText('2-pak')).toBeInTheDocument()
        expect(screen.getByText('2')).toBeInTheDocument()
    })

    it('shows the total parcel count so the operator sees how many labels this makes', () => {
        setup({ breakdown: [{ type: '1-pak', qty: 2 }, { type: 'szkło', qty: 1 }] })

        expect(screen.getByText('Razem 3 paczki — tyle etykiet i numerów śledzenia')).toBeInTheDocument()
    })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix frontend test -- PackagesEditor`
Expected: FAIL — cannot resolve `./PackagesEditor`

- [ ] **Step 3: Write the implementation**

`frontend/src/views/shipping/PackagesEditor.jsx`:

```jsx
import { useEffect, useState } from 'react'

// Mirrors PARCEL_SPECS in zdrovena/common/shipping_parcels.py. The API rejects
// anything outside it, so the dropdown and the validator agree by construction.
export const PACKAGE_TYPES = ['3-pak', '2-pak', '1-pak', 'pół-pak', 'szkło', 'szkło-2pak']

const GLASS_TYPES = new Set(['szkło', 'szkło-2pak'])
const BOX_STYLE = {
    plastic: { color: '#0ea5e9' },
    glass: { color: '#16a34a' },
}

function plural(count) {
    if (count === 1) return 'paczka'
    const rest = count % 10
    const teens = count % 100
    return rest >= 2 && rest <= 4 && !(teens >= 12 && teens <= 14) ? 'paczki' : 'paczek'
}

/**
 * The parcel plan is calculated from Shopify line items and used to be
 * read-only, so a mis-read product name could only be corrected with a deploy.
 * Editing is closed once the shipment exists at the carrier — the API returns
 * 409 there, and `canEdit` mirrors that rule.
 */
export function PackagesEditor({ breakdown, canEdit, onSave, saving = false }) {
    const [rows, setRows] = useState(() => (breakdown || []).map(b => ({ ...b })))

    // Keyed on the serialised plan, not the array identity: ShippingView polls
    // every 5s and hands back a fresh array each time, which would otherwise
    // wipe the operator's half-finished edit on every tick.
    const serverPlan = JSON.stringify(breakdown || [])
    useEffect(() => {
        setRows(JSON.parse(serverPlan))
    }, [serverPlan])

    const total = rows.reduce((sum, row) => sum + (Number(row.qty) || 0), 0)
    const dirty = JSON.stringify(rows) !== serverPlan
    const valid = rows.length > 0 && rows.every(row => Number(row.qty) >= 1 && Number(row.qty) <= 99)

    if (!canEdit) {
        return <ReadOnlyTable rows={rows} total={total} />
    }

    function updateRow(index, patch) {
        setRows(current => current.map((row, i) => (i === index ? { ...row, ...patch } : row)))
    }

    function save() {
        // Errors surface as a toast through the view's withBusy wrapper, the
        // same way every other draft action reports failure.
        return onSave(rows.map(row => ({ type: row.type, qty: Number(row.qty) })))
    }

    return (
        <div>
            <div className="detail-label">Paczki</div>
            <table style={{ borderCollapse: 'collapse', width: '100%', marginTop: 6, fontSize: '0.9em' }}>
                <thead>
                    <tr style={{ borderBottom: '1px solid var(--border)' }}>
                        <th style={HEAD_CELL}>Typ</th>
                        <th style={{ ...HEAD_CELL, textAlign: 'center' }}>Szt.</th>
                        <th style={HEAD_CELL}>Materiał</th>
                        <th style={HEAD_CELL} />
                    </tr>
                </thead>
                <tbody>
                    {rows.map((row, index) => {
                        const style = GLASS_TYPES.has(row.type) ? BOX_STYLE.glass : BOX_STYLE.plastic
                        return (
                            <tr key={index} style={{ borderBottom: '1px solid var(--border)' }}>
                                <td style={{ padding: '6px 12px 6px 0' }}>
                                    <select
                                        aria-label={`Typ paczki ${index + 1}`}
                                        value={row.type}
                                        onChange={e => updateRow(index, { type: e.target.value })}
                                        style={{ width: '100%' }}>
                                        {PACKAGE_TYPES.map(type => (
                                            <option key={type} value={type}>{type}</option>
                                        ))}
                                    </select>
                                </td>
                                <td style={{ padding: '6px 12px', textAlign: 'center' }}>
                                    <input
                                        aria-label={`Liczba sztuk ${index + 1}`}
                                        type="number"
                                        min="1"
                                        max="99"
                                        value={row.qty}
                                        onChange={e => updateRow(index, { qty: e.target.value })}
                                        style={{ width: 64, textAlign: 'center' }} />
                                </td>
                                <td style={{ padding: '6px 0', color: style.color, fontWeight: 500 }}>
                                    {GLASS_TYPES.has(row.type) ? 'szkło' : 'plastik'}
                                </td>
                                <td style={{ padding: '6px 0', textAlign: 'right' }}>
                                    <button
                                        type="button"
                                        aria-label={`Usuń typ paczki ${index + 1}`}
                                        onClick={() => setRows(current => current.filter((_, i) => i !== index))}
                                        style={{ border: 0, background: 'none', cursor: 'pointer', color: 'var(--text-3)' }}>
                                        ×
                                    </button>
                                </td>
                            </tr>
                        )
                    })}
                </tbody>
            </table>

            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8, flexWrap: 'wrap' }}>
                <button
                    type="button"
                    onClick={() => setRows(current => [...current, { type: '1-pak', qty: 1 }])}>
                    Dodaj typ paczki
                </button>
                <button
                    type="button"
                    disabled={!valid || !dirty || saving}
                    onClick={save}>
                    Zapisz paczki
                </button>
                <span className="dim" style={{ fontSize: '0.82em' }}>
                    Razem {total} {plural(total)} — tyle etykiet i numerów śledzenia
                </span>
            </div>

        </div>
    )
}

const HEAD_CELL = {
    textAlign: 'left',
    padding: '3px 12px 3px 0',
    fontSize: '11px',
    fontWeight: 600,
    color: 'var(--text-3)',
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
}

function ReadOnlyTable({ rows, total }) {
    if (!rows.length) return <><div className="detail-label">Paczki</div><span className="dim">—</span></>
    return (
        <div>
            <div className="detail-label">Paczki</div>
            <table style={{ borderCollapse: 'collapse', width: '100%', marginTop: 6, fontSize: '0.9em' }}>
                <thead>
                    <tr style={{ borderBottom: '1px solid var(--border)' }}>
                        <th style={HEAD_CELL}>Typ</th>
                        <th style={{ ...HEAD_CELL, textAlign: 'center' }}>Szt.</th>
                        <th style={HEAD_CELL}>Materiał</th>
                    </tr>
                </thead>
                <tbody>
                    {rows.map((row, index) => {
                        const isGlass = GLASS_TYPES.has(row.type)
                        const style = isGlass ? BOX_STYLE.glass : BOX_STYLE.plastic
                        return (
                            <tr key={index} style={{ borderBottom: '1px solid var(--border)' }}>
                                <td style={{ padding: '6px 12px 6px 0', fontWeight: 500 }}>
                                    <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: style.color, marginRight: 6 }} />
                                    {row.type}
                                </td>
                                <td style={{ padding: '6px 12px', textAlign: 'center' }}>
                                    <span className="mono" style={{ fontWeight: 600 }}>{row.qty}</span>
                                </td>
                                <td style={{ padding: '6px 0', color: style.color, fontWeight: 500 }}>
                                    {isGlass ? 'szkło' : 'plastik'}
                                </td>
                            </tr>
                        )
                    })}
                </tbody>
            </table>
            <div className="dim" style={{ fontSize: '0.82em', marginTop: 6 }}>
                Razem {total} {plural(total)} — tyle etykiet i numerów śledzenia
            </div>
        </div>
    )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix frontend test -- PackagesEditor`
Expected: PASS, 6 passed

- [ ] **Step 5: Wire it into the draft row**

In `frontend/src/views/ShippingView.jsx`:

Add the import next to `TrackingList`:

```js
import { PackagesEditor } from './shipping/PackagesEditor'
```

Replace the whole "Paczki" `<div>` block (lines 962-994, from `<div className="detail-label">Paczki</div>` through its closing `</div>`) with:

```jsx
                                <PackagesEditor
                                    breakdown={draft.packages_breakdown}
                                    canEdit={canManage && !PACKAGES_LOCKED_STATUSES.has(draft.status)}
                                    saving={isBusy}
                                    onSave={rows => onSavePackages(draft, rows)}
                                />
```

Add near the other module constants:

```js
// Mirrors _BREAKDOWN_LOCKED_STATUSES in zdrovena/api/routers/webhooks.py: past
// these the API returns 409, so the table must not offer an edit that cannot land.
const PACKAGES_LOCKED_STATUSES = new Set(['executing', 'pending_confirmation', 'created', 'cancelled'])
```

Add `onSavePackages` to `DraftRow`'s props, pass it through from the list (`onSavePackages={handleSavePackages}` next to `onDraftUpdate={load}`), and add the handler beside `handleSetApaczkaService`:

```js
    function handleSavePackages(draft, rows) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ packages_breakdown: rows }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
        }, 'Nie udało się zapisać paczek')()
    }
```

- [ ] **Step 6: Run the view tests**

Run: `npm --prefix frontend test -- ShippingView`
Expected: PASS. Existing tests that read the packages table by text still pass — the read-only branch renders the same cells.

- [ ] **Step 7: Lint**

Run: `npm --prefix frontend run lint`
Expected: exits 0

- [ ] **Step 8: Commit**

```bash
git add frontend/src/views/shipping/PackagesEditor.jsx frontend/src/views/shipping/PackagesEditor.test.jsx frontend/src/views/ShippingView.jsx
git commit -m "feat(shipping): edytowalne pola TYP i SZT. w planie paczek"
```

---

## Task 9: `ApaczkaClient.get_order()`

**Files:**
- Modify: `zdrovena/common/apaczka.py` (after `list_orders`, ~line 288)
- Test: `tests/test_apaczka_client.py`

- [ ] **Step 1: Write the failing test**

```python
# ── get_order ────────────────────────────────────────────────────────────────


class TestGetOrder:
    def test_calls_the_order_detail_endpoint(self):
        client = _client()
        with patch.object(
            client, "_call", return_value={"response": {"order": {"id": "ord-1"}}}
        ) as call:
            assert client.get_order("ord-1") == {"id": "ord-1"}
        assert call.call_args.args[0] == "order/ord-1"

    def test_reads_the_pickup_number(self):
        # order_send does not return this; it exists only on order detail.
        client = _client()
        response = {
            "response": {
                "order": {
                    "id": "ord-1",
                    "waybill_number": "APZ1",
                    "pickup": {
                        "type": "COURIER",
                        "date": "2026-08-26",
                        "pickup_number": "ZO-77123",
                    },
                }
            }
        }
        with patch.object(client, "_call", return_value=response):
            assert client.get_order_pickup_number("ord-1") == "ZO-77123"

    def test_returns_empty_when_the_carrier_has_not_assigned_one_yet(self):
        client = _client()
        with patch.object(
            client,
            "_call",
            return_value={"response": {"order": {"id": "ord-1", "pickup": {"type": "COURIER"}}}},
        ):
            assert client.get_order_pickup_number("ord-1") == ""

    def test_returns_empty_when_there_is_no_pickup_object(self):
        client = _client()
        with patch.object(client, "_call", return_value={"response": {"order": {"id": "ord-1"}}}):
            assert client.get_order_pickup_number("ord-1") == ""
```

Reuse the file's existing client-construction helper; if it has none, add:

```python
def _client() -> ApaczkaClient:
    return ApaczkaClient("app", "secret", "21", MagicMock())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_apaczka_client.py -k TestGetOrder -v`
Expected: FAIL — `AttributeError: 'ApaczkaClient' object has no attribute 'get_order'`

- [ ] **Step 3: Write the implementation**

```python
    def get_order(self, order_id: str) -> dict[str, Any]:
        """Return one order's detail record.

        This is the only endpoint carrying the pickup block: ``order_send``
        answers with the created order and no pickup information at all.
        """
        result = self._call(f"order/{order_id}", {})
        response = result.get("response") or {}
        if not isinstance(response, dict):
            return {}
        order = response.get("order")
        return order if isinstance(order, dict) else {}

    def get_order_pickup_number(self, order_id: str) -> str:
        """Return the carrier's pickup-order id, or "" if not assigned yet.

        The operator quotes this number to Apaczka support when a collection
        goes wrong, so it is worth one extra read per shipment. The carrier
        assigns it asynchronously, hence the empty-string case.
        """
        pickup = self.get_order(order_id).get("pickup")
        if not isinstance(pickup, dict):
            return ""
        return str(pickup.get("pickup_number") or "").strip()
```

`_call` appends the trailing slash itself (`f"{_BASE}/{endpoint}/"`), so the endpoint string must not carry one — same as `cancel_order/{order_id}` and `waybill/{order_id}` above it.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_apaczka_client.py -k TestGetOrder -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/apaczka.py tests/test_apaczka_client.py
git commit -m "feat(shipping): odczyt numeru zlecenia odbioru z detalu zlecenia Apaczki"
```

---

## Task 10: Emulator serves `order/:id/`

**Files:**
- Modify: `zdrovena/fake_providers/apaczka.py:312-343`
- Test: `tests/test_fake_providers.py`

- [ ] **Step 1: Write the failing test**

```python
def test_apaczka_order_detail_assigns_a_pickup_number_on_the_second_read(fake_provider_url):
    """The carrier assigns pickup_number asynchronously. The emulator withholds
    it on the first read so the poller path is exercised, not assumed."""
    client = _apaczka_client(fake_provider_url)
    created = client.create_shipment(**_minimal_shipment_kwargs())

    assert client.get_order_pickup_number(created["id"]) == ""
    assert client.get_order_pickup_number(created["id"]).startswith("ZO-")


def test_apaczka_order_detail_404s_for_an_unknown_order(fake_provider_url):
    client = _apaczka_client(fake_provider_url)
    with pytest.raises(ApaczkaBusinessError):
        client.get_order("does-not-exist")
```

Reuse the file's existing Apaczka client fixture and shipment-kwargs helper — the file already builds both for `test_apaczka_*` cases around line 340.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fake_providers.py -k apaczka_order_detail -v`
Expected: FAIL — the emulator answers `404 Unsupported endpoint: order/...`

- [ ] **Step 3: Write the implementation**

In `zdrovena/fake_providers/apaczka.py`, inside `order_send`'s `created` dict add the pickup block and a read counter:

```python
        created = {
            "id": order_id,
            "externalId": str(order_data.get("externalId") or ""),
            "status": "SENT",
            "waybill_number": f"APZ{order_id[-4:]}000000",
            "service_id": str(order_data["service_id"]),
            # Mirrors the real contract: the pickup block exists from creation,
            # but the carrier fills pickup_number in later.
            "pickup": {**order_data.get("pickup", {}), "pickup_number": ""},
            "_detail_reads": 0,
        }
```

Add the endpoint branch before the final `return _failure(404, ...)`, and after the `cancel_order/` branch:

```python
    if endpoint.startswith("order/"):
        order_id = endpoint.removeprefix("order/")
        order = STATE.apaczka_orders.get(order_id)
        if not order:
            return _failure(404, "Order not found")
        order["_detail_reads"] = int(order.get("_detail_reads") or 0) + 1
        if order["_detail_reads"] > 1 and not order["pickup"].get("pickup_number"):
            order["pickup"]["pickup_number"] = f"ZO-{order_id[-5:]}"
        detail = deepcopy(order)
        detail.pop("_detail_reads", None)
        return _ok({"order": detail})
```

Place this branch **after** `cancel_order/` and `order_send`, so `order_send` (an exact match) is still matched first — `endpoint.startswith("order/")` would otherwise be tested against `order_send` and miss only because of the slash. Keeping the ordering explicit avoids relying on that.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fake_providers.py -k apaczka -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zdrovena/fake_providers/apaczka.py tests/test_fake_providers.py
git commit -m "test(shipping): emulator Apaczki obsługuje detal zlecenia z numerem odbioru"
```

---

## Task 11: Capture the pickup number at execution

**Files:**
- Modify: `zdrovena/api/shipping_execution_composition.py:329-345` (`_run_apaczka`)
- Test: `tests/test_apaczka_provider.py`

- [ ] **Step 1: Write the failing test**

```python
class TestApaczkaPickupNumberCapture:
    def test_stores_the_pickup_number_on_the_shipment(self):
        client = MagicMock()
        client.create_shipment.return_value = {"id": "ord-1", "waybill_number": "APZ1"}
        client.get_order_pickup_number.return_value = "ZO-77123"

        patch_written = _run_apaczka_with(client, _draft(packages_breakdown=[{"type": "1-pak", "qty": 1}]))

        assert patch_written["courier_shipments"][0]["pickup_number"] == "ZO-77123"

    def test_a_failing_detail_call_does_not_lose_the_shipment(self):
        # The shipment already exists at the carrier. Losing it over a missing
        # support id would be a far worse outcome than an empty field.
        client = MagicMock()
        client.create_shipment.return_value = {"id": "ord-1", "waybill_number": "APZ1"}
        client.get_order_pickup_number.side_effect = RuntimeError("apaczka down")

        patch_written = _run_apaczka_with(client, _draft(packages_breakdown=[{"type": "1-pak", "qty": 1}]))

        assert patch_written["courier_shipments"][0]["id"] == "ord-1"
        assert patch_written["courier_shipments"][0]["tracking_number"] == "APZ1"
        assert patch_written["courier_shipments"][0]["pickup_number"] == ""

    def test_each_parcel_carries_its_own_number(self):
        # Apaczka binds a pickup to one order, so a three-parcel draft can hold
        # three different numbers.
        client = MagicMock()
        client.create_shipment.side_effect = [
            {"id": "ord-1", "waybill_number": "APZ1"},
            {"id": "ord-2", "waybill_number": "APZ2"},
        ]
        client.get_order_pickup_number.side_effect = ["ZO-1", "ZO-2"]

        patch_written = _run_apaczka_with(client, _draft(packages_breakdown=[{"type": "1-pak", "qty": 2}]))

        assert [s["pickup_number"] for s in patch_written["courier_shipments"]] == ["ZO-1", "ZO-2"]
```

`_run_apaczka_with` patches the client factory and the secret reads the module already uses in this file's other `_run_apaczka` tests; follow the existing pattern there rather than inventing a new one.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_apaczka_provider.py -k ApaczkaPickupNumberCapture -v`
Expected: FAIL — `KeyError: 'pickup_number'`

- [ ] **Step 3: Write the implementation**

In `_run_apaczka`, replace the shipment dict construction with:

```python
        result = client.create_shipment(**call_spec["kwargs"])
        order_id = str(result.get("id", ""))
        shipment = {
            "id": order_id,
            "tracking_number": str(result.get("waybill_number") or ""),
            "package_type": call_spec["package_type"],
            "package_number": str(call_spec["package_number"]),
            "pickup_number": _apaczka_pickup_number(client, order_id),
        }
```

and add above `_run_apaczka`:

```python
def _apaczka_pickup_number(client: Any, order_id: str) -> str:
    """Read the pickup-order id the operator quotes to Apaczka support.

    Best-effort by design. The shipment already exists at the carrier by the
    time this runs, so a failed or not-yet-assigned read must leave the record
    intact and let the poller fill it in later.
    """
    if not order_id:
        return ""
    try:
        return client.get_order_pickup_number(order_id)
    except Exception:
        logger.exception("Apaczka pickup number unavailable for order %s", order_id)
        return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_apaczka_provider.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/shipping_execution_composition.py tests/test_apaczka_provider.py
git commit -m "feat(shipping): zapis numeru zlecenia odbioru przy tworzeniu przesyłki Apaczka"
```

---

## Task 12: Poller pass for missing Apaczka pickup numbers

**Files:**
- Create: `zdrovena/api/routers/apaczka_pickup_poller.py`, `tests/test_apaczka_pickup_poller.py`
- Modify: `zdrovena/api/commands/allegro_poll_cmd.py:187-199`

- [ ] **Step 1: Write the failing test**

`tests/test_apaczka_pickup_poller.py`:

```python
"""Tests for zdrovena.api.routers.apaczka_pickup_poller.

Apaczka assigns a pickup number after order_send returns, so a shipment created
today can be missing the id the operator needs for a support ticket tomorrow.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zdrovena.api.routers.apaczka_pickup_poller import (
    MAX_PICKUP_NUMBER_ATTEMPTS,
    resolve_apaczka_pickup_numbers_once,
)


def _draft(draft_id="d1", pickup_number="", attempts=0):
    return {
        "id": draft_id,
        "shopify_order_number": "1801",
        "courier": "apaczka",
        "apaczka_service_id": "21",
        "status": "created",
        "pickup_ordered": True,
        "pickup_number_attempts": attempts,
        "courier_shipments": [
            {
                "id": "ord-1",
                "tracking_number": "APZ1",
                "package_type": "1-pak",
                "package_number": "1",
                "pickup_number": pickup_number,
            }
        ],
    }


class TestResolveApaczkaPickupNumbersOnce:
    def test_fills_in_a_missing_number(self):
        store = MagicMock()
        store.list_drafts.return_value = [_draft()]
        client = MagicMock()
        client.get_order_pickup_number.return_value = "ZO-77123"

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats == {"scanned": 1, "resolved": 1, "still_pending": 0, "errors": 0}
        written = store.update_draft.call_args.args[1]
        assert written["courier_shipments"][0]["pickup_number"] == "ZO-77123"

    def test_skips_drafts_that_already_have_every_number(self):
        store = MagicMock()
        store.list_drafts.return_value = [_draft(pickup_number="ZO-1")]
        client = MagicMock()

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats["scanned"] == 0
        client.get_order_pickup_number.assert_not_called()

    def test_counts_a_still_empty_number_as_pending_and_records_the_attempt(self):
        store = MagicMock()
        store.list_drafts.return_value = [_draft()]
        client = MagicMock()
        client.get_order_pickup_number.return_value = ""

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats["still_pending"] == 1
        assert store.update_draft.call_args.args[1]["pickup_number_attempts"] == 1

    def test_gives_up_after_the_attempt_cap(self):
        # An order the carrier never numbers must not be retried on every cycle
        # for the rest of its life.
        store = MagicMock()
        store.list_drafts.return_value = [_draft(attempts=MAX_PICKUP_NUMBER_ATTEMPTS)]
        client = MagicMock()

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats["scanned"] == 0
        client.get_order_pickup_number.assert_not_called()

    def test_one_bad_draft_does_not_stop_the_rest(self):
        store = MagicMock()
        store.list_drafts.return_value = [_draft("d1"), _draft("d2")]
        client = MagicMock()
        client.get_order_pickup_number.side_effect = [RuntimeError("boom"), "ZO-2"]

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=client)

        assert stats["errors"] == 1
        assert stats["resolved"] == 1

    def test_a_store_read_failure_is_reported_not_raised(self):
        store = MagicMock()
        store.list_drafts.side_effect = RuntimeError("table offline")

        stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=MagicMock())

        assert stats["errors"] == 1

    def test_mock_courier_skips_the_cycle(self):
        store = MagicMock()
        with patch.dict("os.environ", {"MOCK_COURIER": "1"}):
            stats = resolve_apaczka_pickup_numbers_once(shipping_store=store, client=MagicMock())
        assert stats == {"scanned": 0, "resolved": 0, "still_pending": 0, "errors": 0}
        store.list_drafts.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_apaczka_pickup_poller.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zdrovena.api.routers.apaczka_pickup_poller'`

- [ ] **Step 3: Write the implementation**

`zdrovena/api/routers/apaczka_pickup_poller.py`:

```python
"""zdrovena.api.routers.apaczka_pickup_poller — fill in missing Apaczka pickup ids.

Apaczka's ``order_send`` answers without a pickup block; the carrier assigns the
pickup number afterwards and exposes it on ``order/:id/``. Execution reads it
once, best-effort, which leaves the id empty whenever the carrier had not got
around to it yet.

That is the id the operator quotes to Apaczka support when a collection goes
wrong, so "empty because nobody looked again" is not an acceptable resting
state. This module is the second look, run from the same scheduled cycle as the
InPost tracking resolver.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from zdrovena.common.secrets import get_secret

logger = logging.getLogger("zdrovena.api.routers.apaczka_pickup_poller")

# An order the carrier never numbers must not be retried forever. Five cycles
# is roughly a working day at the current schedule.
MAX_PICKUP_NUMBER_ATTEMPTS = 5


def _mock_courier() -> bool:
    """Read at call time, not import time, so tests and dev can toggle it."""
    return os.environ.get("MOCK_COURIER", "").strip() == "1"


def _needs_pickup_number(draft: dict[str, Any]) -> bool:
    if draft.get("courier") != "apaczka" or draft.get("status") != "created":
        return False
    if int(draft.get("pickup_number_attempts") or 0) >= MAX_PICKUP_NUMBER_ATTEMPTS:
        return False
    shipments = draft.get("courier_shipments") or []
    return any(
        str(shipment.get("id") or "").strip()
        and not str(shipment.get("pickup_number") or "").strip()
        for shipment in shipments
    )


def resolve_apaczka_pickup_numbers_once(
    *,
    shipping_store: Any,
    client: Any = None,
) -> dict[str, int]:
    """One resolution cycle over Apaczka drafts missing a pickup number.

    Returns per-cycle stats. Never raises: this runs inside a scheduled job that
    must survive a bad draft or a carrier outage.
    """
    stats = {"scanned": 0, "resolved": 0, "still_pending": 0, "errors": 0}

    if _mock_courier():
        logger.info("MOCK_COURIER: skipping Apaczka pickup number resolution")
        return stats

    try:
        drafts = shipping_store.list_drafts(limit=10_000)
    except Exception:
        # Resilience boundary: a store read failure must not abort the cycle.
        logger.exception("shipping_store.list_drafts failed")
        stats["errors"] += 1
        return stats

    pending = [draft for draft in drafts if _needs_pickup_number(draft)]
    stats["scanned"] = len(pending)
    if not pending:
        return stats

    for draft in pending:
        draft_id = str(draft.get("id") or "")
        shipments = [dict(shipment) for shipment in draft.get("courier_shipments") or []]
        resolved_any = False
        try:
            call_client = client or _build_client(draft, shipping_store)
            for shipment in shipments:
                order_id = str(shipment.get("id") or "").strip()
                if not order_id or str(shipment.get("pickup_number") or "").strip():
                    continue
                number = call_client.get_order_pickup_number(order_id)
                if number:
                    shipment["pickup_number"] = number
                    resolved_any = True
        except Exception:
            # One unresolvable order must not stop the rest.
            logger.exception("Apaczka pickup number resolution failed for draft %s", draft_id)
            stats["errors"] += 1
            continue

        patch = {
            "courier_shipments": shipments,
            "pickup_number_attempts": int(draft.get("pickup_number_attempts") or 0) + 1,
        }
        try:
            shipping_store.update_draft(draft_id, patch)
        except Exception:
            logger.exception("Failed to persist Apaczka pickup numbers for draft %s", draft_id)
            stats["errors"] += 1
            continue

        if resolved_any:
            stats["resolved"] += 1
        else:
            stats["still_pending"] += 1

    return stats


def _build_client(draft: dict[str, Any], storage: Any) -> Any:
    from zdrovena.common.apaczka import ApaczkaClient

    return ApaczkaClient(
        get_secret("apaczka_app_id"),
        get_secret("apaczka_app_secret"),
        str(draft.get("apaczka_service_id") or ""),
        storage,
    )


__all__ = ["MAX_PICKUP_NUMBER_ATTEMPTS", "resolve_apaczka_pickup_numbers_once"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_apaczka_pickup_poller.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Wire it into the scheduled cycle**

In `zdrovena/api/commands/allegro_poll_cmd.py`, directly after the InPost resolution block:

```python
    # Apaczka assigns the pickup number after order_send returns, so execution's
    # one read can come back empty. This is what fills it in — the operator needs
    # that id when a collection goes wrong, not when they remember to click.
    try:
        from zdrovena.api.routers.apaczka_pickup_poller import (
            resolve_apaczka_pickup_numbers_once,
        )

        apaczka_stats = resolve_apaczka_pickup_numbers_once(shipping_store=shipping_store)
        logger.info("Apaczka pickup number resolution complete: %s", apaczka_stats)
    except Exception:
        logger.exception("Apaczka pickup number resolution failed")
```

- [ ] **Step 6: Verify the command still runs its cycle**

Run: `.venv/bin/python -m pytest tests/ -k allegro_poll -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add zdrovena/api/routers/apaczka_pickup_poller.py tests/test_apaczka_pickup_poller.py zdrovena/api/commands/allegro_poll_cmd.py
git commit -m "feat(shipping): poller dociąga brakujące numery zlecenia odbioru z Apaczki"
```

---

## Task 13: Show the pickup-order id

**Files:**
- Modify: `frontend/src/views/ShippingView.jsx` (the "ID draftu kuriera" block, ~line 959)
- Test: `frontend/src/views/ShippingView.test.jsx`

- [ ] **Step 1: Write the failing test**

```jsx
    it('shows the InPost dispatch order id as the pickup order id', async () => {
        installShippingFetch({ drafts: [draft({
            status: 'created', courier: 'inpost', dispatch_order_id: 'DO-4411',
        })] })
        renderWithProviders(<ShippingView />)
        await screen.findByText('1001')
        await userEvent.click(screen.getByText('1001'))

        expect(await screen.findByText('ID zlecenia odbioru')).toBeInTheDocument()
        expect(screen.getByText('DO-4411')).toBeInTheDocument()
    })

    it('shows every Apaczka pickup number, one per parcel', async () => {
        installShippingFetch({ drafts: [draft({
            status: 'created',
            courier: 'apaczka',
            courier_shipments: [
                { id: 'ord-1', tracking_number: 'APZ1', package_type: '1-pak', package_number: '1', pickup_number: 'ZO-1' },
                { id: 'ord-2', tracking_number: 'APZ2', package_type: '1-pak', package_number: '2', pickup_number: 'ZO-2' },
            ],
        })] })
        renderWithProviders(<ShippingView />)
        await screen.findByText('1001')
        await userEvent.click(screen.getByText('1001'))

        expect(await screen.findByText('ZO-1')).toBeInTheDocument()
        expect(screen.getByText('ZO-2')).toBeInTheDocument()
    })

    it('renders a dash when no pickup has been ordered', async () => {
        installShippingFetch({ drafts: [draft({ status: 'created', courier: 'inpost' })] })
        renderWithProviders(<ShippingView />)
        await screen.findByText('1001')
        await userEvent.click(screen.getByText('1001'))

        const row = (await screen.findByText('ID zlecenia odbioru')).nextSibling
        expect(row.textContent).toBe('—')
    })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix frontend test -- ShippingView`
Expected: FAIL — `Unable to find an element with the text: ID zlecenia odbioru`

- [ ] **Step 3: Write the implementation**

In `frontend/src/views/ShippingView.jsx`, add near `courierLabel`:

```js
/**
 * Every carrier calls its pickup order something different and stores it in a
 * different field. The operator quotes this id to that carrier's support when a
 * collection goes wrong, so all three have to surface in the same place.
 */
function pickupOrderIds(draft) {
    if (draft.courier === 'apaczka') {
        return (draft.courier_shipments || [])
            .map(shipment => String(shipment.pickup_number || '').trim())
            .filter(Boolean)
    }
    const single = draft.courier === 'allegro_delivery'
        ? draft.allegro_dispatch_id
        : draft.dispatch_order_id
    return String(single || '').trim() ? [String(single).trim()] : []
}
```

and directly below the "ID draftu kuriera" value line:

```jsx
                                <div className="detail-label" style={{ marginTop: 10 }}>ID zlecenia odbioru</div>
                                <div>
                                    {pickupOrderIds(draft).length
                                        ? pickupOrderIds(draft).map(id => (
                                            <div key={id} className="mono copyable" title="Kliknij żeby skopiować"
                                                onClick={() => navigator.clipboard.writeText(id)}
                                                style={{ cursor: 'pointer' }}>
                                                {id}
                                            </div>
                                        ))
                                        : <span className="dim">—</span>}
                                </div>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix frontend test -- ShippingView`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/views/ShippingView.jsx frontend/src/views/ShippingView.test.jsx
git commit -m "feat(shipping): ID zlecenia odbioru widoczne dla InPostu, Apaczki i Allegro"
```

---

## Task 14: Documentation

**Files:**
- Modify: `docs/audit/shipment-provider-contracts.md`, `CHANGELOG.md`

- [ ] **Step 1: Record the Apaczka pickup contract**

Append to the Apaczka section of `docs/audit/shipment-provider-contracts.md`, before the `Źródła:` line:

```markdown
Numer zlecenia odbioru („ID zlecenia odbioru" w panelu, po którym szuka wsparcie
Apaczki) nie występuje w odpowiedzi `order_send`. Jest wyłącznie w detalu
zlecenia, `order/:order_id/`, jako `pickup.pickup_number` obok `type`, `date`,
`hours_from` i `hours_to`. Przewoźnik nadaje go asynchronicznie, więc odczyt tuż
po utworzeniu przesyłki potrafi zwrócić pustą wartość — dlatego wykonanie czyta
go best-effort, a `zdrovena/api/routers/apaczka_pickup_poller.py` dobija resztę
z limitem prób. Apaczka wiąże odbiór z pojedynczym zleceniem, więc draft o
trzech paczkach może mieć trzy różne numery. InPost trzyma odpowiednik
w `dispatch_order_id` (ShipX `dispatch_orders`), Allegro w `allegro_dispatch_id`.
```

- [ ] **Step 2: Update the changelog**

Under `## Unreleased` → `### Added` in `CHANGELOG.md`:

```markdown
- **shipping**: Arkusz etykiet zapisuje się jako „Etykiety portal DD.MM", a pojedyncza etykieta
  jako „Etykieta <numer> DD.MM". Nazwa idzie w metadanych PDF-a, bo druk leci z `blob:` URL-a i
  Chrome bierze nazwę pliku właśnie stamtąd. Data liczona w strefie Europe/Warsaw — kontener
  chodzi na UTC i wieczorny wydruk dostawał jutrzejszy dzień.
- **shipping**: Panel pokazuje numer śledzenia każdej paczki, nie tylko pierwszej. Backend tworzył
  osobną przesyłkę na sztukę od zawsze; widok wyświetlał jedną z nich.
- **shipping**: Pola TYP i SZT. w planie paczek są edytowalne do momentu wysłania przesyłki do
  kuriera — z dodawaniem i usuwaniem pozycji. Poprawka operatora przeżywa resynchronizację
  z Shopify (`packages_source: operator`), więc przeliczony plan jej nie nadpisze. Po utworzeniu
  przesyłki edycja zwraca 409, a przesyłka pobraniowa nadal musi mieścić się w jednej paczce.
- **shipping**: „ID zlecenia odbioru" widoczne dla wszystkich trzech przewoźników. Dla Apaczki
  czytane z `order/:id/` (`pickup.pickup_number`) przy wykonaniu i dobijane przez poller, gdy
  przewoźnik nada go później; InPost i Allegro miały swoje id zapisane od dawna, tylko nigdy nie
  trafiły do widoku.
```

- [ ] **Step 3: Commit**

```bash
git add docs/audit/shipment-provider-contracts.md CHANGELOG.md
git commit -m "docs(shipping): kontrakt numeru odbioru Apaczki i changelog zgłoszeń operatora"
```

---

## Task 15: Full gate

- [ ] **Step 1: Run the complete quality gate**

Run: `bash scripts/check.sh`
Expected: every step green — ruff, pyright, bandit, pytest with `--cov-fail-under=80`, frontend lint and tests, API contract check.

- [ ] **Step 2: If coverage drops below the threshold**

Do **not** lower `--cov-fail-under` or add `# pragma: no cover`. Per `CLAUDE.md`, report the shortfall and propose either the missing tests or a justified `omit` entry, and wait for the owner's decision. The two new modules (`labels.py`, `apaczka_pickup_poller.py`) are both unit-testable and covered by Tasks 1 and 12, so a drop here means something else regressed.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/operator-shipping-requests-aug2026
```

Open the PR against `develop` — this repo merges feature work into `develop` and promotes `develop → main` as a separate release step that triggers `prod-deploy.yml`.

---

## Verification against the spec

| Spec section | Tasks |
| --- | --- |
| 1. Label PDF naming | 1, 2, 3 |
| 2. One tracking number per parcel | 4 |
| 3. Editable parcel plan | 5, 6, 7, 8 |
| 4. Carrier pickup-order id | 9, 10, 11, 12, 13 |
| Docs / contract record | 14 |

## Post-release check

Section 4 of the spec carries one risk that no test can close: that
`pickup.pickup_number` is the id the operator sees in the Apaczka panel and
quotes to support. After the first real Apaczka shipment, compare the value in
the portal against the panel. If they differ, the fix is confined to which field
`ApaczkaClient.get_order_pickup_number()` reads.
