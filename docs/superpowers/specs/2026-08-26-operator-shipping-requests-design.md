# Operator Shipping Requests, August 2026 — Design Spec

**Date:** 2026-08-26
**Status:** Approved
**Scope:** Four independent operator-facing changes to the shipping view and its
providers: label PDF naming, per-parcel tracking numbers, an editable parcel plan,
and the carrier pickup-order id.

---

## Problem

The operator relayed four requests after a week of running the shipping portal:

1. *"Nazwa zapisywanych etykiet «Etykiety portal [dd.mm]» z datą dzisiejszą"* —
   saving a printed label sheet produces a meaningless filename.
2. *"Ilość numerów śledzenia na portalu = ilość sztuk (SZT.) w paczkach"* — a
   multi-parcel shipment shows one tracking number, not one per parcel.
3. *"Edytowalne pola «TYP» i «SZT.» w paczkach"* — the calculated parcel plan
   cannot be corrected when the planner gets it wrong.
4. *"Oprócz id śledzenia paczki potrzebujemy jeszcze id zlecenia odbioru […]
   potrzebujemy to w razie issuesów z odbiorem, bo po tym id wyszukują
   w customer support."*

Each has a different cause. Two are display-only gaps over data the backend
already holds; one needs a new write path; one needs a provider call we do not
make today.

---

## 1. Label PDF naming

### Cause

`printPdf()` (`frontend/src/views/ShippingView.jsx:34`) loads the label PDF into a
hidden iframe from a `blob:` URL and calls `window.print()`. Chrome's "Save as PDF"
filename comes from the printed document's title, which for a PDF is its `/Title`
metadata, falling back to the URL's filename — a random blob UUID. Neither the
iframe `title` attribute nor the `Content-Disposition` header on a `blob:` URL
influences it.

The batch endpoint already merges labels through `_merge_pdfs()`
(`zdrovena/api/routers/webhooks.py:1464`), which writes a fresh document with
`pypdf` and therefore controls the metadata. A single-parcel label bypasses it and
streams the carrier's bytes unchanged, which is exactly the case that produces the
worst filename.

### Design

`_merge_pdfs(pdfs, *, title)` sets `/Title` on the output document. Every label
response routes through it, including the single-PDF case, so no path can return
an untitled document. `Content-Disposition` carries the same name, for the browsers
and download paths that honour it.

Titles:

| Path | Title |
| --- | --- |
| `POST /shipping/labels/batch` | `Etykiety portal 26.08` |
| `GET /shipping/drafts/{id}/label` | `Etykieta 1234 26.08` |

The single-label title keeps the order number deliberately: with one label on the
page, the order it belongs to is the information worth carrying, and the operator
asked for the portal name in the context of the batch sheet.

The date is `datetime.now(ZoneInfo("Europe/Warsaw"))`, not UTC. The container runs
on UTC; a sheet printed after 22:00 CEST would otherwise be stamped with
tomorrow's date.

`_safe_label_filename()` keeps its ASCII-sanitising role for the header. Both
titles are ASCII already, so no RFC 5987 encoding is needed.

### Testing

- Batch and single-label responses carry the expected `/Title` (read back with
  `pypdf`) and `Content-Disposition` filename, with the clock frozen.
- A single-parcel label is re-written rather than streamed through, so its title
  is set too.
- A title generated at 23:30 UTC on 25.08 reads `26.08`.

---

## 2. One tracking number per parcel

### Cause

Backend behaviour is already correct. `physical_parcels()`
(`zdrovena/shipping/domain/planning.py:88`) expands each `packages_breakdown` row
into `qty` individual parcels; both providers create one carrier shipment per
parcel and append it to `courier_shipments`; `GET /shipping/drafts` returns whole
draft records, so the list is already in the browser.

`ShippingView` renders only `draft.tracking_number`, which `shipment_patch()`
(`zdrovena/api/shipping_execution_composition.py:153`) sets from the *first*
shipment. The remaining numbers are fetched and discarded at render time.

### Design

The "Numer śledzenia" block becomes a list rendered from `courier_shipments`: one
row per parcel, labelled with its package type and position (`2-pak 1/3`), each
number click-to-copy as today. The heading carries the count.

Drafts created before `courier_shipments` existed still fall back to the single
`draft.tracking_number`, matching how `_fetch_label_pdf()` and
`resume_inpost_shipment()` already handle those records.

`draft.tracking_number` stays as-is. It is the field pushed to Shopify and Allegro
and matched by damage detection; this change is presentation only.

### Testing

Component tests: three shipments render three copyable numbers with their parcel
labels; a legacy draft with no `courier_shipments` renders one; a draft with
neither renders the empty dash.

---

## 3. Editable parcel plan (TYP / SZT.)

### Cause

`packages_breakdown` is computed by `calc_packages()` from Shopify line items and
is read-only. `PATCH /shipping/drafts/{id}` exposes `packages_count`, which no
longer drives anything: every provider reads parcels from `packages_breakdown`.

When the planner is wrong the operator has no correction path. This is not
hypothetical — the August product rename made `calc_packages()` read glass bottles
as plastic on orders #1710–#1712, and the only available remedy was a code fix and
a deploy.

### Design

**API.** `PATCH /shipping/drafts/{draft_id}` accepts `packages_breakdown` as a list
of `{type, qty}` objects.

Validation:

- non-empty; at most 20 rows
- `type` must be a key of `PARCEL_SPECS` (`3-pak`, `2-pak`, `1-pak`, `pół-pak`,
  `szkło`, `szkło-2pak`) — 400 otherwise
- `qty` an integer 1–99; total parcels across rows at most 30
- `packages_count` is recomputed as the sum and stops being independently
  writable; passing both is a 400

Guards:

- **409** when the draft is in `executing`, `pending_confirmation`, `created`, or
  `cancelled`. Once a shipment exists at the carrier, the parcel plan is an audit
  record of what was sent, not a draft.
- **400** when the draft carries COD and the total is not exactly one parcel.
  `apaczka_call_specs()` already refuses multi-parcel COD, because one full
  collection amount per parcel would charge the customer several times. Refusing
  at save time tells the operator while they can still fix it.

Persistence:

- the draft gets `packages_source = "operator"`
- `merge_synced_draft()` keeps an operator plan across a Shopify resync, following
  the `apaczka_service_id` precedent: preserved *conditionally*, only when
  `packages_source == "operator"`, so a genuinely re-planned order is not frozen
  for every other draft.
- the edit is logged in English with draft id, before and after plan

**UI.** The "Paczki" table becomes editable in place: TYP a `<select>` over the six
catalogue types, SZT. a number input, a remove control per row, an "add row"
control, and an explicit save that issues the PATCH and reloads via the existing
`onDraftUpdate`. The table stays read-only when the draft is past editing or the
user lacks the shipment-manager role, which the view already tracks as `canManage`.

**Coupling worth stating to the operator:** editing SZT. changes how many parcels
execution creates, and therefore how many labels and tracking numbers appear. That
is the same mechanism as section 2, seen from the other end.

### Testing

- API: each validation and guard branch; `packages_count` recomputation; an
  operator plan surviving `merge_synced_draft()`; a non-operator plan still being
  replaced by a recalculated one.
- Provider: a draft whose operator plan is two rows produces two call specs of the
  right types, proving the edit reaches the carrier payload.
- Component: editing a row and saving issues the expected PATCH body; an executed
  draft renders the table read-only.

---

## 4. Carrier pickup-order id

### Cause

Three couriers, three states:

- **InPost** — `dispatch_order_id` has been persisted all along, both by
  `_run_inpost()` when a pickup window is supplied and by the standalone
  `POST /shipping/drafts/{id}/pickup` route. It has never been rendered.
- **Allegro** — same, as `allegro_dispatch_id`.
- **Apaczka** — not captured. The pickup travels inside `order_send`, and that
  response carries only `id`, `waybill_number`, `status` and friends. The number is
  available from the order-detail endpoint, `order/:order_id/`, as
  `pickup.pickup_number` (Apaczka Web API v2 documentation).

### Design

**Apaczka client.** `ApaczkaClient.get_order(order_id)` calls `order/{id}/` and
returns the order object. `_run_apaczka()` calls it once per created shipment and
stores `pickup_number` on that shipment's `courier_shipments` entry. Apaczka binds
a pickup to a single order, so a three-parcel draft can carry three distinct
numbers; storing per shipment rather than per draft keeps that faithful.

The call is best-effort: a failure is logged and execution continues. A missing
pickup id must never cost a created shipment.

**Poller.** The carrier assigns `pickup_number` asynchronously, so it can be empty
right after creation. The scheduled cycle that already resolves InPost tracking
numbers gains a pass over Apaczka drafts that are `created`, have a pickup
scheduled, and are still missing a `pickup_number`, with a bounded attempt count so
an order that never gets one is not retried forever.

**Emulator.** `zdrovena/fake_providers/apaczka.py` gains `order/:order_id/`,
returning the stored order with a `pickup` object, so tests and e2e exercise the
real path instead of a mock. The emulator assigns `pickup_number` on the second
read of a given order, so the poller path is covered rather than assumed.

**UI.** A "ID zlecenia odbioru" row next to "ID draftu kuriera", reading whichever
field applies to the draft's courier — `dispatch_order_id`, `allegro_dispatch_id`,
or the Apaczka per-shipment numbers — mono, click-to-copy, dash when absent.

**Docs.** `docs/audit/shipment-provider-contracts.md` records that Apaczka exposes
the pickup number only on order detail and never on `order_send`, so the next
person does not go looking for it in the creation response.

### Open risk

That `pickup.pickup_number` is the identifier the operator sees in the Apaczka
panel and quotes to customer support is documented, not verified — the operator
could not supply screenshots. It can only be confirmed against a real shipment
after release. If it turns out to be a different number, the fix is confined to
which field `get_order()` reads.

### Testing

- Client: `get_order()` signs and calls `order/{id}/`, reads
  `response.order.pickup.pickup_number`, tolerates its absence.
- Execution: a created Apaczka shipment carries the number; a failing detail call
  leaves the shipment intact and the number empty.
- Poller: a draft missing the number gets it on a later pass; the attempt cap
  holds.
- Component: each courier's pickup id renders and copies.

---

## Out of scope

- Surfacing `package_fit_warnings()` in the UI after an operator edit. The warnings
  are logged server-side today; showing them is a separate improvement.
- Any change to how `calc_packages()` plans parcels. Section 3 adds a correction
  path; it does not touch the calculation.
- Editing the parcel plan after execution, including cancel-and-replan.
