# Shipping: trust in the automation, visibility when it stops

**Date:** 2026-07-31
**Branch:** feat/inpost-sandbox
**Status:** Draft

## Problem

The shipping automation exists, is deployed, and is essentially unused.

Measured against production storage and Application Insights on 2026-07-31:

| signal | value |
|---|---|
| drafts in `shippingdrafts` | 197 |
| shipments created **by this system** (non-empty `courier_draft_id`) | **2** (both `inpost_locker_standard`, 22–23 July) |
| drafts whose tracking number came from the Shopify sync | 126 |
| `POST .../execute` requests in 30 days | **3, none successful** |
| `GET /shipping/drafts` in 30 days | 856 |

The operator ships through the carrier portals instead, and the Shopify sync
writes the resulting tracking numbers back onto the drafts. That is why 189
drafts show status `created` while the system created almost none — `created` is
set by the sync, not by us.

The stated reason is trust, not preference: earlier attempts on production
failed, the flow was never properly tested, and the operator is unwilling to
click a button whose effect they cannot predict. A courier-sender bug
(fixed in `6eca1b3`) sat in production for weeks and produced no alert at all,
which is consistent with a flow nobody exercises.

So there are two problems, and they are different:

1. **Trust.** The operator cannot see what a click will do before making it, and
   `inpost_courier_standard` has never once succeeded on this account.
2. **Visibility.** Nothing tells anyone when shipping stops working — and the
   signal must survive the fact that shipping is currently done by hand.

## Non-goals

Do not change how the sender is chosen. See below — the current behaviour is
correct and was verified against real labels.

Weights in `PARCEL_SPECS` stay as they are. Real waybills show 19.2 kg for
"Humio PET 36 szt." against our 18.0 kg for `3-pak`, but the owner has confirmed
the current values are intended.

InPost cancellation-path defects (HTTP 400 vs 422 mapping, missing statuses in
`_INPOST_UNCANCELLABLE_STATUSES`) are out of scope.

## Sender and pickup are two different things

This was misdiagnosed once during design and is recorded here so it is not
"fixed" again by mistake.

- **Sender** is the address printed on the label. It is **Kraków**
  (Cieszyńska 6/12, 30-015), sourced from the `sender_*` secrets.
- **Pickup** is where the courier physically collects. It is **Naściszowa**
  (Naściszowa 41, 33-300), sourced from the `pickup_*` secrets. It does **not**
  appear on the label.

They are distinct objects and must not be merged.

Verified against real labels supplied by the owner:

| path | what reaches the label | correct? |
|---|---|---|
| InPost shipment (`webhooks.py:978` ← `_get_sender()`) | Kraków | yes |
| InPost dispatch order (`webhooks.py:2508` ← `_get_pickup_address()`) | Naściszowa, off-label | yes |
| Apaczka (`webhooks.py:1081` ← `_get_pickup_address()`) | Naściszowa | **intentional** |

Apaczka deliberately ships with the Naściszowa address as sender (confirmed by
the owner and visible on the Pocztex and DPD waybills: "Alsendo Sp. z o.o. na
rzecz: Maria Gryzło ZDROVENA, Naściszowa 41"). The only change here is a comment
at `webhooks.py:1081` recording that this is a decision, not an oversight.

### Secret corrections already applied

Applied directly to the production Key Vault during design, against the labels:

- `sender-phone`, `pickup-phone` → `723624437` (was a dummy `500000000`, then a
  wrong value; every label across InPost, Allegro, Apaczka, Pocztex and DPD
  shows `723624437`)
- `sender-name` → `Maria Gryzło ZDROVENA` (was `Zdrovena`)
- `sender-building-number` → `6/12` (was `6`; the flat number was missing)
- `pickup-phone` created — it had never been provisioned, so every Apaczka
  dispatch raised `MissingSecretError` and returned 502

Reviewed and **accepted as-is** (owner decision, 2026-07-31) — do not "fix"
these; the differences from the labels are known and deliberate:

- `sender-street` stays `Cieszynska` and `sender-city` stays `Krakow`, without
  Polish diacritics, even though labels render `Cieszyńska` and `Kraków`
- `sender-email` / `pickup-email` stay `info@wodahumio.pl`, even though the
  newest InPost label and the Apaczka configuration use `biuro@wodahumio.pl`.
  Consequence to be aware of: shipments this system creates will carry `info@`
  while manually created ones carry `biuro@`
- `pickup-name` stays `Zdrovena`

## 1. Payload preview before execution

**Goal:** the operator sees exactly what will be sent before anything is sent.

Refactor `_run_inpost` so that building the request payload is separate from
sending it. Expose the builder through a read-only endpoint that returns the
payload without calling the courier, and add a confirmation panel in
`frontend/src/views/ShippingView.jsx` between "Wykonaj" and the actual call.
The panel shows sender, receiver, service, parcel dimensions and weight.

**Hard constraint:** the preview and the real execution must call the *same*
builder. A preview assembled by separate code would show fiction and would make
the trust problem worse rather than better. This is the whole point of the
refactor; it is not an optimisation.

The refactor is also what makes the payload unit-testable against the ShipX
contract, which is the only defence that actually works here — see below.

## 2. `shipment_origin` marker

**Goal:** distinguish shipments this system created from shipments made by hand.

Add `shipment_origin` to the draft record:

- `system` — we created it (a `courier_draft_id` was returned)
- `external` — the sync observed a tracking number we did not create

Set by the execution path and by the Shopify sync respectively. Backfill once
over existing data; from the 2026-07-31 measurement that yields 2 × `system`
and 126 × `external`.

Without this field, `created` is ambiguous and neither reporting nor alerting
can be trusted. Everything in section 3 depends on it.

## 3. Alert rework

An alert rule was committed in `ec28a2c` firing when drafts are created but no
`shipment.created` event occurs in the window. **Under manual shipping it would
fire permanently**, because `shipment.created` only ever fires when *we* create
the shipment. It is not deployed (`main` does not have it), so it must be
replaced before it reaches production.

Replace it with: **orders with no tracking number from any source after 48
hours.** That signal is independent of who dispatched the parcel. Against
current data it would flag the 61 drafts that have no tracking at all, and would
ignore the 126 shipped by hand.

48 hours is long enough to absorb a weekend order sitting until Monday without
alerting, and short enough that a systemic stoppage surfaces within two days
rather than the weeks the sender outage went unnoticed.

Keep the existing `dlq-backlog` rule as the acute signal. Since `6322875`,
failed executions write a DLQ entry and emit `dlq.enqueued`, so that rule now
covers shipping failures too, including partial outages where one service works
and another does not.

Calibration note for whoever tunes thresholds: telemetry before ~2026-07-23
carries `cloud_RoleName = "unknown_service"` (OTel `service.name` was fixed by
the 24 July deploy), so role-filtered history under-reports shipments.

## 4. Verifying the courier path without shipping anything

**Decision (owner, 2026-07-31): no test shipment on production.** An InPost
shipment transitions to an uncancellable state almost immediately, so a test
cannot be taken back. That rules the step out entirely — it is not deferred, it
is not happening.

This leaves a known unknown, recorded here so nobody mistakes silence for
safety. The InPost sandbox **cannot** validate this path: it only offers
`inpost_courier_c2c`, which silently substitutes the organisation's own address
for a malformed sender instead of rejecting it (verified — the pre-fix payload
*succeeded* there), and forcing `inpost_courier_standard` fails earlier with
`missing_trucker_id` because the sandbox organisation has no trucker assigned.

Production organisation 107266 ("MARIA GRYZŁO ZDROVENA") *does* list
`inpost_courier_standard` in its services and `inpost_courier` in its carriers,
but across all 27 shipments in its history **zero** used it — the 4 courier
shipments were `inpost_courier_allegro`. So whether `trucker_id` is assigned for
the standard service remains unproven.

What we do instead:

1. **Unit tests against the ShipX contract.** The payload builder from section 1
   is asserted field by field at the `requests.Session` layer. This catches the
   entire class of defect that caused the outage — a payload whose shape the
   carrier rejects — without sending anything.
2. **The preview.** The operator inspects the real payload before committing to
   it, which is what the preview is for.
3. **Ask InPost, do not probe them.** Whether organisation 107266 has a trucker
   assigned for `inpost_courier_standard` is a question for the account manager.
   One email carries no risk; a test parcel cannot be undone.

The first genuine `inpost_courier_standard` shipment will therefore be a real
customer order. The preview is what makes that acceptable: the operator sees the
exact payload first, and unit tests have already proven its shape.

## 5. Allegro poller

The poller is not broken — cron `*/5 * * * *`, every execution `Succeeded`. The
issue is cost against yield.

Per week it makes 13,106 `SecretClient.get_secret` calls, 11,815 managed-identity
token fetches and 9,141 table operations. At 2,016 runs per week that is 6.5
secret reads, 5.9 tokens and 4.5 table calls **per run**. Over 30 days it emitted
16,128 log records and produced **8 drafts** — one draft per 1,080 runs.

The structural reason caching does not solve this: the poller is a **Job**, so
each cycle is a fresh process that dies seconds later. The 30-minute Key Vault
TTL cache in `_keyvault.py` never survives to the next cycle. The store-client
cache added in `360be12` helps *within* a run (table calls should fall from ~4.5
to ~2) but does nothing for Key Vault or MSI, which are paid once per process.

Three items:

1. **Lower the cron frequency.** At one draft per 1,080 runs, moving from 5 to
   15–30 minutes cuts everything proportionally for a delay that is imperceptible
   at this volume. One line in Terraform. This is the largest win by a wide
   margin.
2. **Remove the duplicated secret read.** `allegro-client-id` is fetched exactly
   2.0× per run while `allegro-client-secret` is fetched exactly 1.0×. Two
   independent call sites are reading the same secret; find and consolidate them.
3. **Bring the poller into monitoring.** It is invisible to every existing alert:
   Jobs serve no HTTP requests, and `alert-error-rate` counts `requests/failed`.
   If the poller began failing every cycle, nobody would be told.

Also observed, requiring no action: two transient `CourierServerError 503`
responses from Allegro (29 and 31 July), and `FailedMount` warnings for the
`localproxy-certs` and `identity-secret` volumes on the poller's pods, which is
Azure platform noise — the jobs succeed regardless.

## Testing

- Payload builder: unit tests asserting the outgoing payload against the ShipX
  contract, at the `requests.Session` layer. This is the only defence that works,
  because every existing test mocked `create_kurier_shipment` and one test
  asserted the broken shape verbatim, which is how the outage shipped.
- `shipment_origin`: unit tests for both assignments plus the backfill.
- Preview endpoint: a test asserting the preview payload is byte-identical to
  what the execution path sends. If these can diverge, the feature is worthless.
- Alert query: validated against real historical data before deployment, not
  assumed correct.

## Rollout order

1. Apaczka comment, preview refactor and endpoint, preview UI
2. `shipment_origin` plus backfill
3. Alert replacement (must land before `ec28a2c` reaches `main`)
4. Poller: cron, duplicate secret, monitoring

Step 2 is a prerequisite for 3. Steps 1 and 4 are independent of everything else
and can move at any time.

## Open questions

None blocking. The one unresolved fact — whether organisation 107266 has a
trucker assigned for `inpost_courier_standard` — is answered by asking InPost,
not by writing code.
