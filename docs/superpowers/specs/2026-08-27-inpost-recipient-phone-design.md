# InPost Mandatory Recipient Phone — Design Spec

**Date:** 2026-08-27
**Status:** Approved
**Issue:** #294
**Deadline:** must be deployed before **2026-09-08**
**Scope:** validate and normalise the recipient phone on the InPost shipment path, give the
operator a way to fix a bad one, and close the hole that lets a cleared draft stay executable.

---

## Problem

InPost makes the recipient phone number mandatory and validated on **2026-09-08**. After that
date a missing or malformed number stops shipment creation for the affected orders.

Formats InPost accepts: `+48 000 000 000`, `48 000 000 000`, `000 000 000`, and the same without
spaces.

### What already works

`normalize_pl_phone()` (`zdrovena/common/shipping_format.py:12`) already maps every one of those
four shapes to `+48000000000`, and returns `None` for anything else. It runs once, in
`build_draft_record` (`zdrovena/api/shipping_draft_composition.py:453`), on a value read at line
377 from `shipping_address.phone`, then `order.phone`, then `customer.phone`. That path is shared
by Shopify and Allegro — the Allegro mapper feeds it `address.phoneNumber` or `buyer.phoneNumber`
(`zdrovena/common/allegro_mapper.py:42`). A draft whose phone will not parse is stored with
`receiver.phone = None` and flagged `needs_review`, and `execute_draft` refuses to run a draft in
that status (`zdrovena/shipping/application/execution/workflow.py:178`).

There is exactly **one** InPost shipment creation site:
`zdrovena/api/shipping_execution_composition.py:241-243`, fed by `inpost_call_specs`.

### The three gaps

1. **No guard at the carrier boundary.** `inpost_call_specs`
   (`zdrovena/shipping/providers/inpost.py:88,98`) reads `receiver.get("phone", "")`. The key
   exists with value `None`, so the default never applies and `"phone": null` goes into the ShipX
   payload (`zdrovena/common/inpost.py:442,506`). Today ShipX accepts it. After 2026-09-08 it
   will not.

2. **`reviewed: true` clears the flag without fixing anything.** `update_draft`
   (`zdrovena/api/routers/webhooks.py:1468`) moves `needs_review` → `pending` unconditionally.
   Worse, the endpoint accepts `packages_count`, `packages_breakdown`, `service`, `locker_id`,
   `apaczka_service_id` and `reviewed` — **there is no phone field**. The operator cannot fix a
   missing number in the portal at all; they can only wave it through.

3. **The hole is durable.** `merge_synced_draft`
   (`zdrovena/shipping/application/drafts.py:163-168`) deliberately keeps a draft the operator
   already cleared at `pending`, even when the recomputed record says `needs_review`. So one click
   makes a phone-less draft executable forever, across every later sync.

Gap 2 is why "just reject before ShipX" is not enough on its own: it would convert a silent
failure into a draft the operator can neither ship nor repair.

---

## Design

### 1. Validate at the single funnel

Add `InPostRecipientPhoneError(InPostBusinessError)` to
`zdrovena/common/shipping_exceptions.py`, following the existing
`InPostLockerUnavailableError` / `InPostInvalidServiceError` pattern, plus its `_MESSAGES_PL`
entry in `zdrovena/api/errors.py`.

`inpost_call_specs` validates before building any spec: it reads `receiver.phone`, passes it
through `normalize_pl_phone()`, raises `InPostRecipientPhoneError` when the result is `None`, and
puts the **normalised** value into the payload rather than the raw one. Normalising here also
repairs drafts written before `build_draft_record` normalised anything.

That location is deliberate:

- it is the one funnel both the paczkomat and the kurier path go through;
- it is pure — no network, no storage — so it is cheap to test exhaustively;
- it runs before the paid ShipX POST, so an invalid draft costs nothing;
- the operator's execution preview already routes through `pending_inpost_call_specs`, and
  `build_preview` already catches `InPostBusinessError` and renders
  `preview_available: false` with the exception text as `note`
  (`zdrovena/api/shipping_execution_composition.py:675`). The operator therefore sees the reason
  in the preview panel *before* clicking send, with no extra work.

At execute time the error surfaces as HTTP 422 through the existing `CourierBusinessError`
mapping, carrying the specific Polish message rather than the generic "carrier rejected" one.

### 2. Give the operator a way to fix it

`PATCH /shipping/drafts/{draft_id}` accepts `receiver_phone`:

- validated with `normalize_pl_phone`; a value that will not parse is a **400** with a Polish
  `detail` (this endpoint's `detail` reaches an operator toast verbatim through
  `apiErrorMessage`);
- the **normalised** value is written to `receiver.phone`, never the raw input;
- saving a phone does **not** clear `needs_review`. Clearing review stays a separate, explicit
  click, so the operator confirms the whole draft rather than having it silently unblocked by one
  field.

The UI gains an editable phone field in the receiver details block of `ShippingView`, following
the pattern the parcel editor established: read-only when the draft is past editing or the user
lacks the shipment-manager role.

### 3. Close the "cleared forever" hole

Two narrow changes:

- `reviewed: true` returns **400** when the draft's courier is InPost and its phone will not
  normalise. The block lifts by itself if the operator switches the draft to another carrier, so
  it constrains exactly the case that would break and nothing else.
- `merge_synced_draft` gains a second exception to its "a draft the operator already cleared is
  not re-flagged" rule: an InPost draft whose phone will not normalise re-flags, exactly as
  `unreadable_products` already does. That exception exists for the same reason — an executable
  guess is how orders #1710-#1712 shipped in the wrong boxes.

### 4. Find the affected drafts before the deadline

`scripts/audit-inpost-phones.py`, read-only, following the `scripts/backfill-shipment-origin.py`
precedent. It walks the draft store and reports every draft that is InPost, in a non-terminal
status, and whose `receiver.phone` will not normalise: draft id, order number, status, and the raw
stored value. It writes nothing.

Discovery beats waiting: without it the first sign of trouble is a failed shipment on the morning
of 2026-09-08, when the operator has parcels to send.

---

## Out of scope

- **Apaczka and Allegro Delivery.** The issue is explicit that other carriers keep their current
  behaviour. Both accept a phone field, but neither has announced enforcement.
- **Backfilling or auto-repairing stored phone values.** The audit reports; a human decides. We do
  not invent a recipient's phone number.
- **Sender phone.** Unrelated to this enforcement, and it comes from a secret, not from an order.

---

## Testing

Every case the issue names, plus the two regressions that guard the durability fix:

**`normalize_pl_phone` at the InPost boundary** — `+48 000 000 000`, `48 000 000 000`,
`000 000 000`, `000000000` all reach the payload as `+48000000000`; missing, empty, and malformed
values raise `InPostRecipientPhoneError`.

**No POST on bad data** — a draft with an unusable phone produces zero calls to
`create_paczkomat_shipment` and `create_kurier_shipment`. This is the assertion the issue asks for
by name, and the one that actually proves money was not spent.

**Both sources** — a Shopify-derived draft and an Allegro-derived draft behave identically, since
they share `build_draft_record`.

**Legacy normalisation** — a draft holding a raw `500 600 700` (written before normalisation
existed) ships as `+48500600700` rather than being rejected.

**Preview** — a draft with an unusable phone renders `preview_available: false` and a `note`
naming the phone, instead of raising out of the preview endpoint.

**PATCH** — a valid phone is stored normalised; an invalid one is a 400 and leaves the stored
value untouched; setting a phone does not clear `needs_review`.

**Review block** — `reviewed: true` on an InPost draft with an unusable phone is a 400; the same
call on an Apaczka draft succeeds.

**Resync** — an InPost draft the operator cleared, still without a usable phone, is re-flagged
`needs_review` by `merge_synced_draft`; the same draft with a usable phone stays `pending`.

**Audit script** — reports the bad drafts, skips terminal statuses and non-InPost couriers, and
performs no writes.
