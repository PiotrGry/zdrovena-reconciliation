# Apaczka Per-Draft Service Selection — Design Spec

**Date:** 2026-07-09
**Status:** Approved
**Scope:** Move Apaczka's courier-service selection from a single global secret to a per-draft field, driven by the Shopify shipping-line title

---

## Problem

`zdrovena/common/apaczka.py`'s `ApaczkaClient` takes one `service_id` at construction
time, and every call site (`_run_apaczka`, the pickup-scheduling path, and the label
endpoint in `zdrovena/api/routers/webhooks.py`) reads it from a single Key Vault
secret (`apaczka-service-id`). This assumes there is exactly one Apaczka courier
product for the whole business — wrong. Apaczka is a courier *broker*: its
`service_structure` endpoint lists 70 distinct products across 20 suppliers (DPD,
UPS, DHL, GLS, Poczta, Orlen Paczka, Packeta, etc.), including both door-to-door
courier delivery and locker/pickup-point ("skrytki") delivery.

Which product a given order should ship via is decided by the customer at Shopify
checkout (the shipping method they pick), the same way it already is for InPost
(`shipping_lines[0].title` → `paczkomat` vs `kurier` via `INPOST_SERVICE_TITLE_MAP`).
Apaczka needs the same mechanism — there is no single correct default.

This surfaced while backfilling secrets into Key Vault (see `TODOS.md`'s Key Vault
section): `apaczka-service-id` was listed as a secret to add, but a single global
value would silently mis-route every Apaczka order to whatever courier happened to
be configured, regardless of what the customer actually chose. We're not adding it.

---

## Existing pattern this reuses

`zdrovena/api/routers/webhooks.py` already solves this exact problem for InPost:

- `COURIER_TITLE_MAP` env var (`"inpost=inpost;dpd=apaczka"`) — explicit
  title-keyword → courier mapping, `_courier_title_map()` (cached), falls back to a
  substring heuristic.
- `INPOST_SERVICE_TITLE_MAP` env var (`"paczkomat=paczkomat;kurier=kurier"`) —
  explicit title-keyword → InPost sub-service mapping, `_inpost_service_title_map()`
  (cached), falls back to a substring heuristic.
- Both parsed by the shared `_parse_title_map()` helper.
- `_create_draft` calls these at Shopify-webhook time and stores the result on the
  draft (`courier`, `service` fields).
- If a draft's Apaczka-relevant fields can't be determined (today: multi-package
  orders), it's created with `status="needs_review"` instead of `"pending"`, and an
  operator fixes it via `PATCH /shipping/drafts/{id}` in the admin UI before it can
  be executed.

This spec adds a third mapping, `APACZKA_SERVICE_TITLE_MAP`, following the identical
convention — no new mechanism, no new env-var format.

---

## Live data (verified against the real Apaczka account, 2026-07-09)

`ApaczkaClient._call("service_structure", {})` against the real `apaczka-app-id`/
`apaczka-app-secret` (now in Key Vault) returned 70 services across 20 suppliers.
Filtering to non-InPost (`supplier != "INPOST"`) door-to-door and point/locker
("skrytki") options gives the curated candidate list below. **This list is a
starting point, not a permanent constant** — it lives in Python (see Architecture),
is covered by tests, and can be edited in a follow-up PR without another design
cycle if the actual set of Shopify shipping methods needs more/fewer entries.

Door-to-door (`door_to_door == "1"`):

| service_id | Label |
|---|---|
| 1 | UPS Standard |
| 2 | UPS Express Saver |
| 3 | UPS Express Plus do 12:00 |
| 4 | UPS Express Plus do 9:00 |
| 21 | DPD Kurier |
| 24 | DPD Kurier do 9:30 |
| 25 | DPD Kurier do 12:00 |
| 60 | Pocztex Kurier Drzwi-Drzwi |
| 82 | DHL Parcel Kurier |
| 83 | DHL Parcel Kurier do 12:00 |
| 84 | DHL Parcel Kurier do 9:00 |
| 151 | FEDEX Kurier |
| 202 | GLS Kurier Drzwi-Drzwi |

Point/locker ("skrytki", `door_to_point == "1"` or `point_to_point == "1"`):

| service_id | Label |
|---|---|
| 14 | UPS AP Punkt-Punkt |
| 15 | UPS AP Drzwi-Punkt |
| 23 | DPD Pickup Drzwi-Punkt |
| 26 | DPD Pickup Punkt-Punkt |
| 50 | Orlen Paczka Punkt-Punkt |
| 53 | Orlen Paczka Drzwi-Punkt |
| 64 | Pocztex Kurier Drzwi-Punkt |
| 66 | Pocztex Punkt Punkt-Punkt |
| 86 | DHL POP do punktu |
| 203 | GLS Kurier Drzwi-Punkt |
| 314 | Packeta Punkt-Punkt |
| 317 | Packeta Magazyn-Punkt |

Explicitly excluded: all `supplier == "INPOST"` entries (service_ids 40, 41, 42, 45,
46) — those ship through the dedicated InPost integration, never through Apaczka.
Also excluded from this starting list: rarer international/freight suppliers
(AMBRO, HELLMANN, RHENUS, PEKAES, KEX, CBL, PWR's non-Orlen entries, DHL_INT,
PP_PACKETA, PP_CBL) — not used by this business today; add them to the constant
later if a Shopify shipping method needs one.

---

## Architecture

```
Shopify order webhook
  │
  ▼
_create_draft()
  ├── courier = _pick_courier(order)          # existing, unchanged
  ├── if courier == "apaczka":
  │     apaczka_service_id = _pick_apaczka_service(title)   # NEW
  │     if apaczka_service_id is None:
  │         status = "needs_review"            # same convention as multi-package
  │     else:
  │         status = "pending" (unless other needs_review reasons apply)
  └── draft["apaczka_service_id"] = apaczka_service_id       # NEW field, may be None

PATCH /shipping/drafts/{id}
  └── apaczka_service_id: str | None            # NEW body param
        validated against APACZKA_SERVICE_CATALOG (curated whitelist, see below)
        only updates the field — matches today's exact behavior for `service`/
        `locker_id`, neither of which auto-clears "needs_review" either. The
        operator still confirms separately via the existing `reviewed=True` flag
        to flip status back to "pending" (frontend can send both in one PATCH
        call, but the backend logic stays two independent, composable checks —
        no new special-casing).

_run_apaczka() / pickup-scheduling path / label endpoint
  └── service_id = draft.get("apaczka_service_id")   # NEW — was get_secret(...)
      if not service_id: raise (shouldn't happen — draft would still be needs_review)
```

### `APACZKA_SERVICE_TITLE_MAP` env var

Same format and lookup convention as `INPOST_SERVICE_TITLE_MAP`:

```
APACZKA_SERVICE_TITLE_MAP="dpd kurier=21;orlen paczka=53;ups standard=1"
```

`_apaczka_service_title_map()` — `@lru_cache(maxsize=1)`, parsed via the existing
shared `_parse_title_map()`, cleared by the existing `_reset_courier_maps_cache()`
test helper (add this cache to that function alongside the other two).

`_pick_apaczka_service(title: str) -> str | None`:
1. Check `APACZKA_SERVICE_TITLE_MAP` for a keyword match in the lowercased title.
2. No substring-heuristic fallback (unlike `_pick_courier`/`_pick_inpost_service`) —
   Apaczka's title strings are business-configured Shopify shipping-method names,
   not consistently predictable substrings like "inpost"/"paczkomat". Returning
   `None` on no match is correct and expected; it routes to `needs_review`.

### `APACZKA_SERVICE_CATALOG` (new Python constant)

A `dict[str, str]` (service_id → human label) in `zdrovena/common/apaczka.py`,
seeded from the tables above. Used for:
- Validating `PATCH /shipping/drafts/{id}`'s new `apaczka_service_id` body param
  (400 if not in the catalog — same pattern as the existing `service` field's
  `valid = {...}` set check).
- A new read-only endpoint, `GET /shipping/apaczka-services`, returning the catalog
  as `[{"service_id": "21", "label": "DPD Kurier"}, ...]` for the frontend dropdown.
  (Static list from the constant — does not call the live Apaczka API on every
  request; the constant is the source of truth, matching how `INPOST` services are
  hardcoded as an enum today rather than fetched live.) Auth: `require_viewer_or_above`
  — same minimum role as `GET /shipping/drafts` and `get_label`, since this is
  read-only reference data, not a mutation.

### Draft field

New field on the shipping-draft record: `apaczka_service_id: str | None`. Present
(possibly `None`) on every draft, matching how `packages_breakdown`/`tracking_number`
etc. are always-present-but-nullable fields today. `None` only makes sense when
`courier == "apaczka"`; for other couriers it's simply unused/`None`, no validation
needed against `courier` (mirrors how `service="apaczka"` today has no special
meaning when `courier != "apaczka"` either).

### Removed

- `get_secret("apaczka_service_id")` call sites (3, in `webhooks.py`) — deleted,
  replaced by `draft.get("apaczka_service_id")`.
- `apaczka-service-id` — removed from `scripts/secrets_manifest.py`'s
  `ENV_LOCAL_SECRETS` (it's not a secret at all now — it's per-order data, never a
  Key Vault/`.env.local` value). Not present in Key Vault today, so no live secret
  needs deleting; `TODOS.md`'s Key Vault checklist gets this row removed instead of
  marked done.
- `apaczka.py`'s module docstring line `Secrets: apaczka-app-id, apaczka-app-secret,
  apaczka-service-id (Key Vault).` — update to drop `apaczka-service-id`.

---

## Frontend (`ShippingView.jsx`)

**Correction after re-checking the frontend:** there is currently no UI anywhere in
`ShippingView.jsx` that calls `PATCH /shipping/drafts/{id}` at all — `service`,
`locker_id`, and `reviewed` are only reachable via direct API calls (curl/Postman)
today; this was already a known gap for InPost's `locker_id`. This spec adds the
*first* such UI, scoped narrowly to Apaczka — it does not fix the pre-existing
`locker_id` gap (separate, already-tracked follow-up).

Minimal new UI, added to the existing accordion-row detail view:
- When a draft has `courier === "apaczka"` and `apaczka_service_id` is falsy, show
  a `<select>` populated from `GET /shipping/apaczka-services` (fetched once per
  page load, held in component state — no need for a cache library at this size)
  plus a "Zapisz" button. On submit: `PATCH /shipping/drafts/{id}` with
  `{"apaczka_service_id": <value>, "reviewed": true}` in one call (both fields the
  existing endpoint already accepts independently — sending both together is just
  two independent patches applied in the same request, not new backend behavior),
  then refresh the draft list.
- When `apaczka_service_id` is already set, show it as plain text (catalog label,
  e.g. "DPD Kurier") instead of the dropdown — no edit affordance for already-set
  values in this pass (YAGNI; add an "edit" toggle later if operators need to
  correct a wrong selection post-hoc).
- `courierLabel(draft)` gains a case: when `courier === "apaczka"` and
  `apaczka_service_id` is set, show `Apaczka — <catalog label>` instead of the
  current bare `"Apaczka"`.

---

## Backward compatibility

No Apaczka orders have shipped through this integration in production yet (per
`TODOS.md`, Key Vault secrets for it were never even added). No existing-draft
migration is needed. If any `needs_review` Apaczka drafts exist in a lower
environment from manual testing, they'll simply need `apaczka_service_id` set via
the new PATCH param like any other needs_review draft — no special-cased backfill.

---

## Testing

- `_pick_apaczka_service()`: matches via `APACZKA_SERVICE_TITLE_MAP`, no match →
  `None`, cache-clearing test helper covers the new map too.
- `_create_draft`: Apaczka order with a matching title → `apaczka_service_id` set,
  `status="pending"`; non-matching title → `apaczka_service_id=None`,
  `status="needs_review"`.
- `PATCH /shipping/drafts/{id}`: valid `apaczka_service_id` from the catalog →
  field updated, status unchanged unless `reviewed=True` also sent; invalid/unknown
  id → 400.
- `GET /shipping/apaczka-services`: returns the full catalog.
- `_run_apaczka` / pickup / label call sites: read `draft["apaczka_service_id"]`,
  no `get_secret("apaczka_service_id")` call remains anywhere (grep-verifiable).
- Update `tests/test_secrets_sync.py`/`scripts/secrets_manifest.py` tests if any
  hardcode the 20-item count (manifest becomes 19 items).

---

## Out of scope

- Automatically calling the live `service_structure` API to keep the catalog in
  sync — the 23h-cached fetch already exists (`_get_service_structure`) for
  `create_shipment`'s own use; this spec's catalog is a separate, smaller,
  manually-curated Python constant for the admin dropdown + validation, not wired
  to the live cache. Reconciling the two (e.g. warning if a curated id disappears
  from the live list) is a future improvement, not required now.
- Customer-facing shipping-method selection UI in Shopify itself — out of scope,
  unrelated to this repo.
- Pricing/rate-shopping across Apaczka services — out of scope.
- Expanding the curated catalog beyond the ~25 seeded entries — trivial follow-up
  edit to the Python constant when a new Shopify shipping method needs a new
  service_id, not part of this implementation.
