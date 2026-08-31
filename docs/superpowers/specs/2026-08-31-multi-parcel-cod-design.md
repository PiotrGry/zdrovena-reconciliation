# Multi-Parcel COD Split — Design Spec

**Date:** 2026-08-31
**Status:** Draft — awaiting owner review
**Reported by:** operator
**Blocked order:** Shopify #1731 (draft `d262a79e-7dd5-4c6a-952f-fecfb728ac27`), `pending` since
2026-08-28
**Scope:** split the collected amount across the physical parcels of a COD order, recompute it
when the operator repacks, and move the recipient-phone edit out of the draft row.

---

## Problem

A COD order that needs more than one parcel cannot be shipped from the portal at all. The
operator sees:

> COD requires exactly one physical InPost shipment; multi-parcel COD is blocked

The block lives in three places:

| Location | What it does |
|---|---|
| `zdrovena/shipping/providers/inpost.py:84` | Refuses to build call specs |
| `zdrovena/shipping/providers/apaczka.py:173` | Same, for Apaczka |
| `zdrovena/api/routers/shipping/drafts.py:174` | Refuses a repack that yields >1 parcel |

The block is not arbitrary. Today **one physical parcel is one carrier shipment** — the
`courier_shipments` checkpoint list is keyed by `(package_type, package_number)`, each entry gets
its own label and its own tracking number, and resume-after-partial-failure walks that same key.
The `cod` object is attached to *every* call spec, so without the block a customer with two
parcels would be charged the full amount twice.

### The blocked order

```
source:   shopify                cod:     351.00 PLN  (gateway "cash on delivery (cod)")
courier:  inpost                 service: inpost_courier_standard
packages: 3-pak ×2               → 2 physical parcels ⇒ blocked
order_items: "Imprezowy zapas wody HUMIO – 72 butelki" ×1
```

72 bottles → 12 half-packs → 6 packs → `3-pak ×2`.

### Why not "one shipment, many parcels"

Both ShipX (`parcels[]`) and Apaczka (`shipment[]`) accept several parcels inside one shipment
with a single COD, which is the carrier-native answer. It is rejected here because it breaks the
invariant the rest of the system is built on — **label = parcel = tracking number**. Adopting it
would rewrite the checkpoint model, label generation, the Apaczka pickup poller and the damage
flow. Splitting the amount keeps all of that untouched.

---

## Scope

**In scope**

- Per-parcel COD for `inpost_courier_standard` and `apaczka`.
- Recompute on repack, with no stored state to go stale.
- Persist the money the split needs (line values, shipping price) on the draft record.
- Recipient-phone edit moves from the draft row into a Settings form.

**Out of scope (deliberate)**

- **InPost Paczkomat stays blocked.** Owner's decision. At a locker each parcel is collected
  separately, so a split would let the customer pay for some parcels and abandon the rest. The
  condition becomes service-specific instead of disappearing.
- **Allegro COD.** `allegro_to_shopify_order` (`common/allegro_mapper.py:47`) carries only
  `name`, `quantity`, `sku` — it drops `payment.type`, `delivery.cost.amount`,
  `lineItems[].price.amount` and `summary.totalToPay`. Consequence: an Allegro order whose
  delivery method is literally "Allegro Kurier24 InPost pobranie" reaches us with `cod: null`
  (`shipping_draft_composition.py:420` only reads COD when `source == "shopify"`), so the portal
  never shows that it is a COD order. Draft `1f2f5dfd-f430-4038-a9ff-d373fb6508ce` is that case,
  shipped over 3 parcels. The money itself is believed safe — with Allegro Delivery, Allegro
  orders the transport and settles the collection, and we never send an amount
  (`allegro_delivery.py:78`, "Allegro derives it from the order"). Recorded here as a known gap,
  not fixed in this change.

---

## Design

### 1. The formula

All arithmetic in **integer grosze**. No floats anywhere near money.

```
S     = shipping price (shipping_lines, after discounts)
G     = total_outstanding − S          # goods + deposit + discounts + corrections
w_i   = value of the goods assigned to parcel i
cod_i = split_equal(S, N)[i] + split_weighted(G, w)[i]
```

Both splits use the **largest-remainder method** on integers, ties broken by ascending parcel
index. Therefore `Σ cod_i == total_outstanding` exactly, by construction — this is the invariant
the tests defend hardest.

**Why `G` is distributed proportionally rather than summed from line prices:** deposit and
discounts. `kaucja` is matched by `SKIP_RE` (`common/bottles.py:27`) alongside shipping-cost
lines, so it is absent from `product_items` — yet the customer pays it. Distributing `G` (which
contains it) by goods weights spreads deposit and order-level discounts proportionally on their
own, and the sum still reconciles under partial payments or an edited order. Summing line prices
would drift from `total_outstanding` in every one of those cases.

### 2. Assigning goods to parcels

Nothing in the codebase maps an order line to a parcel. `calc_packages`
(`shipping/domain/planning.py:33`) collapses everything into a pool of half-packs (1 half-pack =
6 bottles) and greedily fills boxes; a `3-pak` is pure capacity and does not know what went into
it. A new deterministic assignment is needed.

Parcel capacity, in half-packs, added as `half_packs` to `PARCEL_SPECS`
(`common/shipping_parcels.py`) so there is one source of truth:

| type | 3-pak | 2-pak | 1-pak | pół-pak | szkło | szkło-2pak |
|---|---|---|---|---|---|---|
| half-packs | 6 | 4 | 2 | 1 | 2 | 4 |

Verified against production: draft `1f2f5dfd-…` has 3 × plastic ×12 and 3 × glass ×12, and
`calc_packages` produced exactly `3-pak ×1` + `szkło-2pak ×1` + `szkło ×1`.

Algorithm — `parcel_line_shares(draft) -> list[dict[int, int]]`, aligned to `physical_parcels`:

1. Build two FIFO queues of half-packs, `(line_index, count)`, in `order_items` order: one
   plastic, one glass. Per-line count reuses `calc_packages`' own formula
   (`ceil(qty * bottles_per_unit(name) / 6)`) — imported, never re-derived.
2. Walk parcels in `packages_breakdown` order. Glass types (`szkło`, `szkło-2pak`) draw from the
   glass queue, everything else from the plastic queue.
3. A parcel takes up to its `half_packs` capacity. **A queue may run dry before capacity is
   filled** — glass rounds up (`(glass_half_packs + 1) // 2`), so a partly empty glass box is
   normal, not an error.
4. If goods outlast capacity (the operator repacked into fewer or smaller boxes), the **last
   parcel of that material absorbs the remainder**.

Weight per parcel: `w_i = Σ line_total_gr × taken_half_packs / line_half_packs`, held as
`fractions.Fraction` so the largest-remainder comparison stays exact.

### 3. Where it is computed

A pure function in `zdrovena/shipping/domain/cod.py`:

```python
@dataclass(frozen=True)
class CodAllocation:
    amounts: tuple[Decimal, ...]   # aligned to physical_parcels(draft)
    basis: str                     # "value" | "equal"

def cod_allocation(draft: dict) -> CodAllocation
```

**Nothing is stored on the draft.** The split is a function of `(cod.amount, shipping_price,
order_items, packages_breakdown)`, so a repack changes it automatically — which is the
"dynamiczne" requirement — and there is no cached copy to go stale. Both
`providers/inpost.py` and `providers/apaczka.py` consume it, so the execution preview and the
real request cannot drift.

The amount actually sent is written into the `courier_shipments` checkpoint when a parcel is
created, as an audit trail only — never read back as a source of truth.

Idempotency holds without extra work: `_BREAKDOWN_LOCKED_STATUSES`
(`drafts.py:76`) already freezes the breakdown once execution starts, so a resume after a partial
failure recomputes the identical allocation. Both providers build specs for **all** parcels and
filter checkpointed ones afterwards, so indices stay aligned.

### 4. Data the draft must start carrying

`build_draft_record` (`shipping_draft_composition.py:507`) stores `order_items` as
`{name, quantity}` — no prices — and no shipping cost at all. Two additions, formatted as decimal
strings to match the existing `cod.amount` convention:

- `order_items[].line_total` — `price × quantity − total_discount` from the Shopify line item.
- `shipping_price` (draft root) — `discounted_price` of the shipping lines, falling back to
  `price`.

**Legacy drafts** created before this change have neither. They fall back to an **equal split of
`G`**. `cod_allocation` reports which basis it used — `"value"` or `"equal"` — and the UI renders
`"equal"` as a badge, so the operator can see the split is even rather than value-based. The total
is still exact and the customer still pays the same. (Owner's decision; the alternatives were
fail-closed or a Shopify backfill.)

### 5. Failure modes — all fail closed

| Condition | Behaviour |
|---|---|
| `S > total_outstanding` (partial payment) | `cod_error`, no shipment |
| A parcel computes to `0.00` | Refuse — both carriers reject `amount <= 0` (`apaczka.py:425`) |
| `Σ w_i == 0` (no price data at all) | Equal split + warning, as for legacy drafts |
| Paczkomat + COD + >1 parcel | Existing block, kept, message reworded to name the locker |

Insurance follows for free: `_insurance_payload` (`common/inpost.py:325`) and Apaczka's
`shipment_value` (`common/apaczka.py:445`) are computed per parcel from that parcel's own COD, so
the "insurance ≥ COD" rule holds per shipment automatically.

### 6. API and UI

- `drafts.py:174` — replace the "must fit in one parcel" rejection with a service-aware check:
  reject only for Paczkomat, otherwise validate that the new breakdown yields a legal allocation
  and return `400` with a specific reason if it does not.
- `ExecutePreview.jsx` already renders `payload.cod` per payload, so per-parcel amounts appear
  with no change.
- `DraftRow.jsx:192` shows `draft.cod.amount` (the total). For a multi-parcel COD draft it also
  lists the per-parcel breakdown, next to the existing per-parcel tracking numbers.

---

## Workstream B — recipient phone

The operator asked that the customer's phone number not be editable on the order.

A blanket block cannot ship: from 2026-09-08 InPost rejects a shipment with an invalid recipient
phone (issue #294), and editing the field on the draft row is today the **only** way to unblock
such an order (`drafts.py:205`, `RecipientPhone.jsx`). Removing it would make an order with a bad
Shopify phone unshippable from the portal.

Resolution (owner's decision): the field stops being editable inline, and the capability moves to
a deliberate form in **Settings** — enter an order number, the form finds the draft and shows the
customer and current number, then accepts a new one. Same permission as today; only the entry
point moves, from something clicked by accident in the flow to something sought out on purpose.

- `DraftRow.jsx` renders the phone read-only.
- `SettingsView.jsx` gains the card. It reuses `GET /api/shipping/drafts` and matches on
  `shopify_order_number`, then `PATCH`es the found draft — no new endpoint.
- `PATCH /api/shipping/drafts/{id}` keeps `receiver_phone` and its `normalize_pl_phone`
  validation unchanged.

---

## Test scenarios

Written first, before the implementation.

| # | Scenario | Expectation |
|---|---|---|
| 1 | Single parcel, COD 150 | `150.00` — today's behaviour must not regress |
| 2 | **#1731 golden case**: 351.00, `3-pak ×2`, one line | `175.50 / 175.50` |
| 3 | 2 parcels, goods 120 + shipping 30 | `60+15 / 60+15` |
| 4 | 3 parcels, shipping 10 | `3.34 / 3.33 / 3.33`, Σ = `10.00` |
| 5 | Indivisible total, 100.01 over 3 | `33.34 / 33.34 / 33.33`, Σ = `100.01` — the 2 spare grosze land on the earliest parcels |
| 6 | Unequal parcels (`3-pak` + `pół-pak`) | Goods by content, shipping equal |
| 7 | **Mixed glass + plastic** (draft `1f2f5dfd-…` layout) | Glass value lands in glass parcels only |
| 8 | Partly empty glass parcel (odd half-pack) | Assignment tolerates capacity > goods |
| 9 | Order with `kaucja` | Deposit spread proportionally, Σ = `total_outstanding` |
| 10 | Free shipping (`S = 0`) | Goods split only, no parcel at `0.00` |
| 11 | Operator repacks 2 → 3 parcels | Recomputes, Σ still exact |
| 12 | Repack after execution started | `409`, unchanged (`_BREAKDOWN_LOCKED_STATUSES`) |
| 13 | Resume after parcel 2 failed | Parcel 2 gets the same amount as on the first attempt |
| 14 | Legacy draft, no prices | Equal split, warning flag set |
| 15 | `S > total_outstanding` | Fail closed, `cod_error` |
| 16 | A parcel computes to `0.00` | Fail closed |
| 17 | Apaczka: `shipment_value` ≥ that parcel's COD | Per parcel, not per order |
| 18 | Paczkomat + COD + 2 parcels | Still blocked, locker-specific message |
| 19 | Property: capacity ≥ content for any `calc_packages` output | Guards the `half_packs` table |
| 20 | Phone read-only on the draft row; Settings form finds and updates | Both paths |

---

## Risks

**Reconciliation.** This repository exists to reconcile payments. N parcels means N collections
and therefore N courier payouts against one Shopify order. Matching a payout to an order gets
harder after this change, not easier. Not addressed here — flagged so it is a known consequence
rather than a surprise.

**Partial collection.** With per-parcel COD the customer can accept and pay for some parcels and
refuse others. For a courier delivering everything in one visit this is largely theoretical, which
is why the locker path stays blocked.
