# Shipping Draft Automation — Design Spec

**Date:** 2026-05-15  
**Status:** Approved  
**Scope:** Shopify webhook → FastAPI → InPost ShipX / Apaczka draft creation

---

## Problem

Orders placed in Shopify (including COD) require a shipping draft to be created in
InPost or Apaczka before the package can be sent. Currently this is done manually.
The goal is to automate draft creation immediately when an order is placed, with
Shopify only knowing that the request was received — not whether the draft succeeded.

---

## Architecture

```
Shopify
  │
  │  POST /webhooks/shopify/order-created
  │  X-Shopify-Hmac-Sha256: <sig>
  ▼
FastAPI (zdrovena-api)
  ├── HMAC-SHA256 validation (reject 401 on mismatch)
  ├── Return 200 immediately
  └── BackgroundTask
        ├── Parse order: shipping_lines[0].title
        ├── Route: "kurier"|"paczkomat" → InPost ShipX
        │         everything else       → Apaczka
        ├── Call courier API
        └── Log result (existing logging infrastructure)
```

No new Azure resources. No queue. Events may be lost if the Container App crashes
mid-background-task, which is acceptable at current order volume (~50/month).

---

## Routing Logic

```python
title = order["shipping_lines"][0]["title"].lower()
if "kurier" in title or "paczkomat" in title:
    courier = "inpost"
else:
    courier = "apaczka"
```

---

## Module Structure

```
zdrovena/
  common/
    inpost.py       # InPost ShipX client
    apaczka.py      # Apaczka client
  api/
    routers/
      webhooks.py   # POST /webhooks/shopify/order-created (new file)
```

---

## Shopify Webhook Validation

Shopify signs every webhook with HMAC-SHA256 of the **raw request body** using the
webhook secret.

```python
import base64
import hashlib
import hmac

def verify_shopify_hmac(raw_body: bytes, signature_header: str, secret: str) -> bool:
    computed = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(computed, signature_header)
```

Secret stored in Key Vault as `shopify-webhook-secret`, fetched via `get_secret()`.
Validation failure returns HTTP 401 — Shopify does **not** retry on 4xx, which
prevents duplicate drafts from retries.

The endpoint must receive the **raw bytes** before JSON parsing so the HMAC covers
the original payload. Use `Request.body()` in FastAPI, not the parsed `body` param.

---

## InPost ShipX Client (`zdrovena/common/inpost.py`)

**API:** `https://api-shipx-pl.easypack24.net`  
**Auth:** `Authorization: Bearer <token>` (static API key from Key Vault)  
**Secrets needed:** `inpost-api-token`, `inpost-organization-id`

### Shipment creation

```
POST /v1/organizations/{org_id}/shipments
```

**For paczkomat** (`service: "inpost_locker_standard"`):

Required fields:
- `receiver.first_name`, `receiver.last_name`, `receiver.email`, `receiver.phone`
- `parcels[].template` — one of `"small"` / `"medium"` / `"large"`
- `custom_attributes.target_point` — locker ID (source depends on which Shopify app presents the locker picker; investigate during implementation — likely `note_attributes["inpost_locker_id"]` or encoded in the shipping line title)
- `custom_attributes.sending_method` — `"dispatch_order"` (we book a courier pickup)
- `reference` — Shopify order number

**For kurier** (`service: "inpost_courier_standard"`):

Required fields:
- All receiver fields + `receiver.address` (street, building_number, city, post_code, country_code)
- `sender` block — full address (stored in Key Vault or config)
- `parcels[].dimensions` + `parcels[].weight` (explicit, no template for kurier)
- `custom_attributes.sending_method` — `"dispatch_order"`
- `reference` — Shopify order number

### Courier pickup dispatch (kurier only)

After creating the shipment, a **separate** call is required:

```
POST /v1/organizations/{org_id}/dispatch_orders
```

Body: `{"shipments": [shipment_id], "address": {...sender address...}, ...}`

Paczkomat shipments do not need this — the parcel is dropped at a locker.

### Response note

The `tracking_number` field may be `null` in the create response (async confirmation).
Log the `id` field — it is always present and sufficient for the audit trail.

---

## Apaczka Client (`zdrovena/common/apaczka.py`)

**API:** `https://www.apaczka.pl/api/v2/`  
**Auth:** HMAC-SHA256 per-request signature (see below)  
**Secrets needed:** `apaczka-app-id`, `apaczka-app-secret`  
**No sandbox** — production only; test orders must be cancelled via `cancel_order`.

### Auth signature (every request)

```python
import hmac, hashlib, json, time

def _sign(app_id: str, secret: str, endpoint: str, data: dict) -> dict:
    request_json = json.dumps(data, separators=(",", ":"))
    expires = str(int(time.time()) + 1800)
    msg = f"{app_id}:{endpoint}:{request_json}:{expires}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "app_id": app_id,
        "request": request_json,
        "expires": expires,
        "signature": sig,
    }
```

Signatures are valid for 30 minutes max. Never pre-generate and cache them.

### Service ID discovery and caching

`service_id` is required to create an order. The list is fetched from:

```
POST https://www.apaczka.pl/api/v2/service_structure/
```

**Rate limit: once per 24 hours.** The result must be cached in blob storage
(`zdrovena-files` container, key: `apaczka/service_structure.json`) with a
`fetched_at` timestamp. On each request, check if cache is older than 23 hours
before re-fetching.

The service to use (e.g. DPD Classic) should be configured via Key Vault secret
`apaczka-service-id` so it can be changed without a code deployment.

### Shipment creation

```
POST https://www.apaczka.pl/api/v2/order_send/
```

Required fields in `data`:
- `service_id` — from cache/config
- `address.sender` — name, firstname, lastname, email, phone, address, city, zip, country_code
- `address.receiver` — same fields (from Shopify order)
- `shipment[].type` — `"package"`
- `shipment[].weight` — kg
- `shipment[].width`, `height`, `depth` — cm
- `options.pickup_type` — `"courier"` (Apaczka books the pickup — no separate call needed)

All monetary values in API responses are in **groszy** (divide by 100 for PLN).

Response `status: 200` = success. `response.id` is the order ID; log it.

---

## Sender Address

Both couriers need a sender address. Store as Key Vault secrets:
- `sender-name`
- `sender-street`
- `sender-city`
- `sender-post-code`
- `sender-phone`
- `sender-email`

Fetched once at startup via the existing KV caching layer.

---

## Packaging Logic

### Product units

| Product type | Unit | Bottles/unit |
|---|---|---|
| Plastik (`12 butelek`) | zgrzewka | 12 |
| Szkło (`12 butelek w szkle`) | zgrzewka | 12 |

Shopify `line_item.quantity` = liczba zgrzewek.

### Box types

| Typ kartonu | Zgrzewki | Zastosowanie |
|---|---|---|
| 3-pak | 3 | plastik, pełny karton |
| 2-pak | 2 | plastik, pełny karton |
| 1-pak | 1 | plastik, pełny karton |
| pół-pak | 0.5 | plastik, niepełna zgrzewka |
| szkło | 1 | szkło, 1 pudełko/zgrzewkę |
| szkło-2pak | 2 | szkło, karton na 2 zgrzewki |

### Packing algorithm

**Plastik** — greedy, largest box first:
```
remaining = total_plastic_zgrzewki
while remaining >= 3: use 3-pak, remaining -= 3
if remaining == 2: use 2-pak
elif remaining == 1: use 1-pak
elif remaining > 0: use pół-pak
```

**Szkło** — greedy 2-pak first, then single:
```
while glass >= 2: use szkło-2pak, glass -= 2
if glass == 1: use szkło
```

**Łącznie:**
```
packages_count = plastic_boxes + glass_boxes
packages_breakdown = [{"type": "3-pak", "qty": N}, ..., {"type": "szkło", "qty": M}]
```

### Rozpoznawanie szkła

Funkcja `is_glass(name)` z `zdrovena/audit/bottles.py` — zwraca `True` gdy nazwa zawiera `szkle` lub `szkło`.

### Przykłady

| Zamówienie | Wynik |
|---|---|
| 3× plastik | 1 karton (3-pak) |
| 5× plastik | 2 kartony (3-pak + 2-pak) |
| 6× plastik | 2 kartony (3-pak + 3-pak) |
| 4× plastik | 2 kartony (3-pak + 1-pak) |
| 1× szkło | 1 pudełko (szkło) |
| 3× plastik + 1× szkło | 2 paczki (3-pak + szkło) |

### Draft schema

Pole `packages_breakdown` dodane do rekordu draftu:
```json
"packages_count": 2,
"packages_breakdown": [
  {"type": "3-pak", "qty": 1},
  {"type": "szkło", "qty": 1}
]
```

---

## Parcel Dimensions

Default parcel dimensions (used when Shopify order does not carry explicit weight/size):
- Default template for InPost paczkomat: `"small"`
- Default weight for Apaczka: `1.0 kg`, dimensions `30×20×15 cm`

Store defaults in config (`zdrovena/common/config.py` or env vars). Per-order
overrides can be added later via Shopify metafields — not in scope now.

---

## Error Handling

Errors are logged with the Shopify order number as context. The webhook endpoint
always returns 200 — courier API failures are not surfaced to Shopify.

Known failure modes and handling:
| Failure | Handling |
|---------|----------|
| InPost invalid locker ID (paczkomat) | Log error with order number |
| InPost/Apaczka bad address (422) | Log error with order number |
| Courier API timeout / 5xx | Log error; no retry (acceptable at this volume) |
| Missing `shipping_lines` in order | Log warning, skip |
| Apaczka service_structure fetch fails | Log error, abort draft creation |

---

## Secrets Required (Key Vault)

| Secret name | Used by |
|---|---|
| `shopify-webhook-secret` | HMAC validation |
| `inpost-api-token` | InPost Bearer auth |
| `inpost-organization-id` | InPost org scoping |
| `apaczka-app-id` | Apaczka auth |
| `apaczka-app-secret` | Apaczka auth |
| `apaczka-service-id` | Apaczka service routing |
| `sender-name` | Both couriers |
| `sender-street` | Both couriers |
| `sender-city` | Both couriers |
| `sender-post-code` | Both couriers |
| `sender-phone` | Both couriers |
| `sender-email` | Both couriers |

---

## Shopify Configuration (manual, outside codebase)

1. In Shopify admin → Settings → Notifications → Webhooks: add webhook for
   **Order creation** pointing to `https://<zdrovena-api-url>/webhooks/shopify/order-created`
2. Copy the webhook signing secret into Key Vault as `shopify-webhook-secret`

---

---

## Draft Persistence

The background task writes one record to blob storage after the courier API responds.

**Location:** `zdrovena-files` container, path `shipping/drafts.jsonl` (append-only, one JSON object per line)

**Record schema:**

```json
{
  "id": "uuid-v4",
  "created_at": "2026-05-15T15:30:00Z",
  "shopify_order_id": "12345678",
  "shopify_order_number": "#1042",
  "customer_name": "Jan Kowalski",
  "courier": "inpost",
  "service": "inpost_locker_standard",
  "tracking_number": "630000000000000000000000",
  "courier_draft_id": "98765432",
  "status": "created",
  "shipping_address": {
    "street": "ul. Odbiorcza 10",
    "city": "Kraków",
    "post_code": "30-001"
  },
  "parcel": {
    "template": "small",
    "weight_kg": null
  },
  "error": null
}
```

Failed drafts set `status: "error"` and `error: "<message>"`, `courier_draft_id: null`.

---

## Draft List API

```
GET /shipping/drafts
```

Reads `shipping/drafts.jsonl` from blob, parses all records, returns sorted by
`created_at` descending. No pagination needed at current volume.

Response:

```json
{
  "drafts": [ ...records... ]
}
```

---

## Label Print API

```
GET /shipping/drafts/{courier_draft_id}/label?courier=inpost
GET /shipping/drafts/{courier_draft_id}/label?courier=apaczka
```

Fetches the waybill PDF from the courier and streams it as `Content-Type: application/pdf`
with `Content-Disposition: inline` so the browser opens it directly (print dialog appears).

- **InPost:** `GET /v1/shipments/{id}/label` → returns PDF bytes directly
- **Apaczka:** `POST /waybill/` with `{"order_id": id}` → returns base64-encoded PDF, decoded server-side before streaming

---

## Frontend — ShippingView

New view `frontend/src/views/ShippingView.jsx` added to the sidebar alongside existing views.

**Accordion list — collapsed row (always visible):**

| Field | Source |
|-------|--------|
| Order # | `shopify_order_number` |
| Customer | `customer_name` |
| Courier | `courier` + `service` as pill (InPost Paczkomat / InPost Kurier / Apaczka) |
| Date | `created_at` formatted |
| Status | pill: green `created` / red `error` |

Click anywhere on the row to expand/collapse.

**Expanded detail panel:**

- Full shipping address
- Parcel info (template or dimensions/weight)
- Tracking number (copyable, or "pending" if null)
- Courier draft ID
- Error message (if status is error)
- **Print label** button — calls `GET /shipping/drafts/{id}/label?courier=...`, opens PDF in new tab

Expansion state is local (no URL change, no persistence). Only one row expanded at a time.

---

## Out of Scope

- Tracking status polling or webhooks back from couriers
- Daily exception report (deferred — logs + Application Insights cover this)
- Per-order parcel size from Shopify metafields
- Multi-parcel orders
- Label re-printing after courier confirms (label from `created` status is sufficient)
