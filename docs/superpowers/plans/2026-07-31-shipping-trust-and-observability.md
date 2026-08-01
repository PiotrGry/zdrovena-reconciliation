# Shipping Trust and Observability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the shipping automation trustworthy enough for the operator to use, and make it visible when shipping stops — without ever sending a test parcel.

**Architecture:** Extract InPost payload construction into `InPostClient` builders so the preview and the real send share one code path by construction. Mark every draft with the origin of its shipment so manual work is distinguishable from ours. Replace the alert rule with one keyed on tracking numbers rather than our own success events. Cut the Allegro poller's per-run cost.

**Tech Stack:** Python 3.10, FastAPI, pytest, Azure Table Storage, Azure Key Vault, Terraform, React (Vite).

**Spec:** `docs/superpowers/specs/2026-07-31-shipping-trust-and-observability-design.md`

---

## Branch and PR strategy

Six PRs, each on its own branch off `develop`, each independently mergeable and independently revertable. All PRs target `develop` (repo convention — `main` is the deploy branch, fed by `develop` → `main` PRs).

| PR | Branch | Depends on | Touches |
|---|---|---|---|
| 1 | `refactor/inpost-payload-builder` | — | `zdrovena/common/inpost.py`, `zdrovena/api/routers/webhooks.py` |
| 2 | `feat/shipping-execute-preview` | PR 1 | `zdrovena/api/routers/webhooks.py` |
| 3 | `feat/shipping-preview-ui` | PR 2 | `frontend/src/views/ShippingView.jsx` |
| 4 | `feat/shipment-origin` | — | `zdrovena/api/routers/webhooks.py`, `scripts/` |
| 5 | `fix/no-tracking-alert` | PR 4 | `infra/terraform/monitoring.tf` |
| 6 | `perf/allegro-poller-cost` | — | `infra/terraform/` |

PRs 1 and 4 and 6 can proceed in parallel. PR 5 needs the `draft.tracking_assigned` event from PR 4.

**Before starting any PR:**

```bash
git fetch origin
git checkout -b <branch> origin/develop
```

---

## PR 1 — Extract the InPost payload builders

**Why:** `create_kurier_shipment` and `create_paczkomat_shipment` build their JSON inline, so nothing else can see a payload without sending it. Extracting the builder is the prerequisite for an honest preview and for contract-level tests.

### Task 1.1: Extract `build_kurier_payload`

**Files:**
- Modify: `zdrovena/common/inpost.py` (`create_kurier_shipment`, currently ~line 380)
- Test: `tests/test_inpost_client.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_inpost_client.py`, after `class TestKurierSenderContract`:

```python
class TestPayloadBuilders:
    """The preview and the real send must be the same payload by construction."""

    def _kwargs(self):
        return {
            "receiver_first_name": "Jan",
            "receiver_last_name": "Kowalski",
            "receiver_email": "jan@example.com",
            "receiver_phone": "600200300",
            "receiver_street": "Kwiatowa",
            "receiver_building_number": "5",
            "receiver_city": "Warszawa",
            "receiver_post_code": "00-001",
            "sender": _SENDER,
            "reference": "order-1060",
        }

    def test_build_kurier_payload_matches_what_create_sends(self):
        client = InPostClient(_TOKEN, _ORG)
        built = client.build_kurier_payload(**self._kwargs())

        resp = _ok_response({"id": "ship-1"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            client.create_kurier_shipment(**self._kwargs())
        sent = mock_post.call_args.kwargs["json"]

        assert built == sent, "preview payload must equal the payload actually sent"

    def test_build_kurier_payload_sends_nothing(self):
        client = InPostClient(_TOKEN, _ORG)
        with patch.object(client._session, "post") as mock_post:
            client.build_kurier_payload(**self._kwargs())
        mock_post.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_inpost_client.py::TestPayloadBuilders -v`
Expected: FAIL with `AttributeError: 'InPostClient' object has no attribute 'build_kurier_payload'`

- [ ] **Step 3: Refactor `create_kurier_shipment`**

In `zdrovena/common/inpost.py`, rename the existing `create_kurier_shipment` body to `build_kurier_payload` by changing its final line, then add a thin `create_kurier_shipment`. The method currently ends with `return self._post_shipment(payload)` — change that to `return payload` and rename the method to `build_kurier_payload`. Then add immediately after it:

```python
class InPostClient:  # existing class — add this method
    def create_kurier_shipment(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build and send. The builder is public so a preview can show the exact
        payload without a network call."""
        return self._post_shipment(self.build_kurier_payload(**kwargs))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_inpost_client.py -v`
Expected: PASS, all tests including the pre-existing `TestKurierShipment` and `TestKurierSenderContract`

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/inpost.py tests/test_inpost_client.py
git commit -m "refactor(inpost): expose the kurier payload builder

create_kurier_shipment built its JSON inline, so nothing could inspect a
payload without sending it. Split the builder out and have create call it,
which makes a preview equal to the real request by construction rather
than by discipline."
```

### Task 1.2: Extract `build_paczkomat_payload`

**Files:**
- Modify: `zdrovena/common/inpost.py` (`create_paczkomat_shipment`, currently ~line 330)
- Test: `tests/test_inpost_client.py`

- [ ] **Step 1: Write the failing test**

Add to `class TestPayloadBuilders`:

```python
class TestPayloadBuilders:  # existing class — add these methods
    def _locker_kwargs(self):
        return {
            "receiver_first_name": "Jan",
            "receiver_last_name": "Kowalski",
            "receiver_email": "jan@example.com",
            "receiver_phone": "600200300",
            "target_point": "KRA01M",
            "reference": "order-1061",
        }

    def test_build_paczkomat_payload_matches_what_create_sends(self):
        client = InPostClient(_TOKEN, _ORG)
        built = client.build_paczkomat_payload(**self._locker_kwargs())

        resp = _ok_response({"id": "ship-2"})
        with patch.object(client._session, "post", return_value=resp) as mock_post:
            client.create_paczkomat_shipment(**self._locker_kwargs())
        sent = mock_post.call_args.kwargs["json"]

        assert built == sent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_inpost_client.py::TestPayloadBuilders -v`
Expected: FAIL with `AttributeError: 'InPostClient' object has no attribute 'build_paczkomat_payload'`

- [ ] **Step 3: Apply the same split to the paczkomat method**

Rename `create_paczkomat_shipment` to `build_paczkomat_payload`, change its final `return self._post_shipment(payload)` to `return payload`, and add:

```python
class InPostClient:  # existing class — add this method
    def create_paczkomat_shipment(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build and send — see build_kurier_payload for why these are split."""
        return self._post_shipment(self.build_paczkomat_payload(**kwargs))
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q -p no:randomly`
Expected: PASS. `tests/test_webhooks_integration.py` and `tests/test_shipping_webhook.py` patch `create_paczkomat_shipment` / `create_kurier_shipment` by name, so they keep working.

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/inpost.py tests/test_inpost_client.py
git commit -m "refactor(inpost): expose the paczkomat payload builder

Same split as the kurier path, for the same reason."
```

### Task 1.3: Record that Apaczka's sender is deliberate

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py:1081`

- [ ] **Step 1: Add the comment**

Find `sender=pickup_address,` in `_run_apaczka` and replace with:

```text
            # Deliberate: Apaczka prints the pickup address (Naściszowa) as the
            # sender, unlike InPost which prints the registered address (Kraków).
            # Confirmed against real Pocztex and DPD waybills. Do not "align"
            # these two — see the shipping trust spec, 2026-07-31.
            sender=pickup_address,
```

- [ ] **Step 2: Run the suite**

Run: `python3 -m pytest tests/test_shipping_webhook.py -q`
Expected: PASS (comment-only change)

- [ ] **Step 3: Commit and open the PR**

```bash
git add zdrovena/api/routers/webhooks.py
git commit -m "docs(shipping): record that Apaczka's sender address is deliberate

Apaczka prints Naściszowa where InPost prints Kraków. That asymmetry is
intentional and verified against real waybills, but it reads like a bug,
so say so in the code before someone helpfully fixes it."
git push -u origin refactor/inpost-payload-builder
gh pr create --base develop --title "refactor(inpost): expose payload builders" --body "Prerequisite for the execute preview. Splits payload construction from sending in InPostClient so a preview equals the real request by construction, and records that Apaczka's sender address differs from InPost deliberately.

No behaviour change. Spec: docs/superpowers/specs/2026-07-31-shipping-trust-and-observability-design.md"
```

---

## PR 2 — Preview endpoint

**Why:** the operator needs to see the payload before committing to it.

### Task 2.1: Extract the per-parcel call plan from `_run_inpost`

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py` (`_run_inpost`, ~line 925-1000)
- Test: `tests/test_shipping_webhook.py`

- [ ] **Step 1: Write the failing test**

```python
class TestInPostPayloadPlan:
    def test_plan_lists_one_payload_per_parcel(self, store):
        from zdrovena.api.routers import webhooks as wh

        draft = {
            "id": "d1",
            "shopify_order_number": "1700",
            "courier": "inpost",
            "service": "inpost_courier_standard",
            "receiver": {
                "first_name": "Jan",
                "last_name": "Kowalski",
                "email": "j@example.com",
                "phone": "600200300",
            },
            "shipping_address": {
                "street": "Kwiatowa",
                "building_number": "5",
                "city": "Warszawa",
                "post_code": "00-001",
            },
            "packages": [{"type": "1-pak", "count": 1}],
        }
        sender = {"name": "Zdrovena", "street": "Cieszynska"}
        plan = wh._inpost_payload_plan(draft, sender)

        assert len(plan) >= 1
        assert plan[0]["service"] == "inpost_courier_standard"
        assert "payload" in plan[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_shipping_webhook.py::TestInPostPayloadPlan -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_inpost_payload_plan'`

- [ ] **Step 3: Add `_inpost_payload_plan` and make `_run_inpost` use it**

Add above `_run_inpost` in `zdrovena/api/routers/webhooks.py`:

```python
def _inpost_payload_plan(draft: dict[str, Any], sender: dict[str, str]) -> list[dict[str, Any]]:
    """Return the exact ShipX payloads this draft would produce, without sending.

    _run_inpost consumes the same list, so a preview cannot drift from reality.
    """
    from zdrovena.common.inpost import PARCEL_SPECS, InPostClient

    receiver = draft.get("receiver") or {}
    addr = draft.get("shipping_address") or {}
    order_number = str(draft.get("shopify_order_number", ""))
    inpost_service = "paczkomat" if draft.get("service") == "inpost_locker_standard" else "kurier"
    client = InPostClient("preview", "preview")

    plan: list[dict[str, Any]] = []
    for package_type, package_number, package_count in _physical_parcels(draft):
        spec = PARCEL_SPECS.get(package_type, PARCEL_SPECS["1-pak"])
        reference = _shipment_reference(order_number, package_type, package_number, package_count)
        if inpost_service == "paczkomat":
            payload = client.build_paczkomat_payload(
                receiver_first_name=receiver.get("first_name", ""),
                receiver_last_name=receiver.get("last_name", ""),
                receiver_email=receiver.get("email", ""),
                receiver_phone=receiver.get("phone", ""),
                target_point=receiver.get("locker_id", ""),
                reference=reference,
                template=spec.get("paczkomat_template") or "large",
            )
        else:
            payload = client.build_kurier_payload(
                receiver_first_name=receiver.get("first_name", ""),
                receiver_last_name=receiver.get("last_name", ""),
                receiver_email=receiver.get("email", ""),
                receiver_phone=receiver.get("phone", ""),
                receiver_street=addr.get("street", ""),
                receiver_building_number="/".join(
                    filter(None, [addr.get("building_number", "1"), addr.get("flat_number", "")])
                ),
                receiver_city=addr.get("city", ""),
                receiver_post_code=addr.get("post_code", ""),
                sender=sender,
                reference=reference,
                weight_kg=spec["weight_kg"],
                dimensions=spec,
            )
        plan.append(
            {
                "service": draft.get("service"),
                "package_type": package_type,
                "package_number": package_number,
                "reference": reference,
                "payload": payload,
            }
        )
    return plan
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_shipping_webhook.py::TestInPostPayloadPlan -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git commit -m "feat(shipping): build the InPost payload plan without sending it"
```

### Task 2.2: Add the preview endpoint

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py`
- Test: `tests/test_shipping_webhook.py`

- [ ] **Step 1: Write the failing test**

```python
class TestExecutePreviewEndpoint:
    def test_preview_returns_payload_and_sends_nothing(self, client, store):
        draft = self._seed_error_draft(store)
        with patch("zdrovena.api.routers.webhooks._run_inpost") as mock_run:
            resp = client.get(f"/api/shipping/drafts/{draft['id']}/execute/preview")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "sender" in body and "parcels" in body
        mock_run.assert_not_called()

    def test_preview_404_for_unknown_draft(self, client):
        resp = client.get("/api/shipping/drafts/does-not-exist/execute/preview")
        assert resp.status_code == 404
```

Copy `_seed_error_draft` usage from the existing `TestExecuteDraft` class; if the helper is a method there, move it to a module-level function `_seed_error_draft(store)` and update both call sites.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_shipping_webhook.py::TestExecutePreviewEndpoint -v`
Expected: FAIL with 404 on the first test (route not registered)

- [ ] **Step 3: Add the endpoint**

Add to `zdrovena/api/routers/webhooks.py`, immediately before the `execute_draft` route:

```python
@router.get(
    "/shipping/drafts/{draft_id}/execute/preview",
    summary="Show exactly what would be sent to the courier, without sending it",
    responses={
        403: {"description": "Insufficient role"},
        404: {"description": "Draft not found"},
    },
)
def preview_execute_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
) -> dict[str, Any]:
    draft = shipping_store.get_draft(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    sender = _get_sender()
    courier = draft.get("courier", "apaczka")
    if courier != "inpost":
        return {
            "courier": courier,
            "sender": _get_pickup_address() if courier == "apaczka" else sender,
            "parcels": [],
            "note": "Preview is currently available for InPost only.",
        }
    return {"courier": courier, "sender": sender, "parcels": _inpost_payload_plan(draft, sender)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_shipping_webhook.py -q`
Expected: PASS

- [ ] **Step 5: Regenerate the OpenAPI contract**

Run: `bash scripts/generate-api-contracts.sh`
Expected: contract files updated with the new route. If the script does not exist or fails, run `python3 scripts/export-openapi.py` instead.

- [ ] **Step 6: Commit and open the PR**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git add -A  # contract artefacts
git commit -m "feat(shipping): add an execute preview endpoint

The operator will not click a button whose effect they cannot predict, and
three execute attempts in thirty days all failed. Return the exact ShipX
payloads for a draft without contacting the courier, reusing the same
builders the real send uses."
git push -u origin feat/shipping-execute-preview
gh pr create --base develop --title "feat(shipping): execute preview endpoint" --body "Returns the exact payloads a draft would send, without contacting the courier. Builds on the payload builders from the previous PR so preview and reality cannot diverge."
```

---

## PR 3 — Preview in the UI

**Why:** an API-only preview does not reach the person who is afraid to click.

### Task 3.1: Confirmation panel before execute

**Files:**
- Modify: `frontend/src/views/ShippingView.jsx`
- Test: `frontend/src/views/ShippingView.test.jsx`

- [ ] **Step 1: Read the existing execute handler**

Run: `grep -n "execute" frontend/src/views/ShippingView.jsx`
Note the current click handler and the API helper it uses. Follow that file's existing state and fetch patterns rather than introducing new ones.

- [ ] **Step 2: Write the failing test**

Add to `frontend/src/views/ShippingView.test.jsx`. The file already imports
`act, screen, waitFor`, `userEvent`, `deferred, jsonResponse, mockFetch` from
`../test/http` and `renderWithProviders` from `../test/render`, and has a
`draft()` factory — reuse all of them.

```javascript
describe('execute preview', () => {
    const previewBody = {
        courier: 'inpost',
        sender: { name: 'Maria Gryzło ZDROVENA', city: 'Krakow' },
        parcels: [{
            service: 'inpost_courier_standard',
            package_type: '1-pak',
            reference: 'order-1001',
            payload: { service: 'inpost_courier_standard' },
        }],
    }

    afterEach(() => { vi.restoreAllMocks() })

    it('shows the preview and does not execute on the first click', async () => {
        const fetchMock = mockFetch({
            '/api/shipping/drafts': jsonResponse({ drafts: [draft()] }),
            '/api/shipping/drafts/draft-1/execute/preview': jsonResponse(previewBody),
        })
        renderWithProviders(<ShippingView />)
        await screen.findByTestId('shipping-execute-draft-1')

        await act(async () => {
            await userEvent.click(screen.getByTestId('shipping-execute-draft-1'))
        })

        await waitFor(() => expect(screen.getByTestId('execute-preview')).toBeTruthy())
        const executeCalls = fetchMock.mock.calls.filter(
            ([url, opts]) => url.endsWith('/execute') && opts?.method === 'POST',
        )
        expect(executeCalls).toHaveLength(0)
    })

    it('executes once the preview is confirmed', async () => {
        const fetchMock = mockFetch({
            '/api/shipping/drafts': jsonResponse({ drafts: [draft()] }),
            '/api/shipping/drafts/draft-1/execute/preview': jsonResponse(previewBody),
            '/api/shipping/drafts/draft-1/execute': jsonResponse({ id: 'draft-1', status: 'created' }),
        })
        renderWithProviders(<ShippingView />)
        await screen.findByTestId('shipping-execute-draft-1')

        await act(async () => {
            await userEvent.click(screen.getByTestId('shipping-execute-draft-1'))
        })
        await screen.findByTestId('execute-preview')
        await act(async () => {
            await userEvent.click(screen.getByTestId('execute-preview-confirm'))
        })

        const executeCalls = fetchMock.mock.calls.filter(
            ([url, opts]) => url.endsWith('/execute') && opts?.method === 'POST',
        )
        expect(executeCalls).toHaveLength(1)
    })
})
```

The component must therefore expose `data-testid="execute-preview"` on the panel
and `data-testid="execute-preview-confirm"` on its confirm button. Drafts that
need a pickup schedule already route through `setPickupModal('execute')` at
`ShippingView.jsx:851` — the preview goes in front of that branch, so both paths
show it.

- [ ] **Step 3: Run test to verify it fails**

Run: `cd frontend && npm test -- ShippingView`
Expected: FAIL — no preview panel rendered

- [ ] **Step 4: Implement the two-step flow**

Clicking "Wykonaj" fetches the preview and opens a panel showing sender, receiver, service, dimensions and weight per parcel. A second button in the panel performs the existing execute call. Cancel closes without sending.

- [ ] **Step 5: Run tests**

Run: `cd frontend && npm test -- ShippingView`
Expected: PASS

- [ ] **Step 6: Commit and open the PR**

```bash
git add frontend/src/views/ShippingView.jsx frontend/src/views/ShippingView.test.jsx
git commit -m "feat(shipping): show the payload preview before executing

Puts the preview where the hesitation actually happens: between the click
and the courier call."
git push -u origin feat/shipping-preview-ui
gh pr create --base develop --title "feat(shipping): preview panel before execute"
```

---

## PR 4 — `shipment_origin` and the tracking event

**Why:** 126 drafts carry tracking numbers this system did not create, and nothing distinguishes them. Without that distinction, no report and no alert can be trusted.

### Task 4.1: Emit `draft.tracking_assigned` with an origin

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py`
- Test: `tests/test_shipping_webhook.py`

- [ ] **Step 1: Write the failing test**

```python
class TestShipmentOrigin:
    def test_system_origin_when_we_create_the_shipment(self, client, store):
        draft = self._seed_error_draft(store)
        with patch(
            "zdrovena.api.routers.webhooks._run_inpost",
            return_value={
                "courier_draft_id": "ship-1",
                "tracking_number": "TRK1",
                "status": "created",
                "error": None,
            },
        ):
            resp = client.post(f"/api/shipping/drafts/{draft['id']}/execute")
        assert resp.status_code == 200
        assert store.get_draft(draft["id"])["shipment_origin"] == "system"

    def test_external_origin_when_sync_brings_the_tracking(self, store):
        from zdrovena.api.routers import webhooks as wh

        record = {"id": "d9", "tracking_number": None, "courier_draft_id": None}
        merged = wh._apply_tracking_from_sync(record, "TRK-EXTERNAL")
        assert merged["shipment_origin"] == "external"
        assert merged["tracking_number"] == "TRK-EXTERNAL"

    def test_sync_does_not_downgrade_a_system_shipment(self, store):
        from zdrovena.api.routers import webhooks as wh

        record = {
            "id": "d9",
            "tracking_number": "TRK1",
            "courier_draft_id": "ship-1",
            "shipment_origin": "system",
        }
        merged = wh._apply_tracking_from_sync(record, "TRK1")
        assert merged["shipment_origin"] == "system"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_shipping_webhook.py::TestShipmentOrigin -v`
Expected: FAIL — `_apply_tracking_from_sync` does not exist and `shipment_origin` is never set

- [ ] **Step 3: Implement**

Add to `zdrovena/api/routers/webhooks.py`:

```python
SHIPMENT_ORIGIN_SYSTEM = "system"
SHIPMENT_ORIGIN_EXTERNAL = "external"


def _apply_tracking_from_sync(record: dict[str, Any], tracking: str | None) -> dict[str, Any]:
    """Attach a tracking number observed by the Shopify sync.

    A tracking number we did not create means the parcel was dispatched in a
    carrier portal by hand. Recording that is what lets reporting and alerting
    tell manual work apart from ours; without it `status == created` is
    ambiguous. Never downgrades a shipment we created.
    """
    merged = dict(record)
    if not tracking:
        return merged
    merged["tracking_number"] = tracking
    if merged.get("courier_draft_id"):
        merged["shipment_origin"] = SHIPMENT_ORIGIN_SYSTEM
    elif not merged.get("shipment_origin"):
        merged["shipment_origin"] = SHIPMENT_ORIGIN_EXTERNAL
    log_event(
        "draft.tracking_assigned",
        draft_id=merged.get("id"),
        order_number=merged.get("shopify_order_number"),
        shipment_origin=merged["shipment_origin"],
    )
    return merged
```

Then, in the execute success path where the draft patch is written (near `tracking_number=patch.get("tracking_number")`, ~line 2313), set `shipment_origin` to `SHIPMENT_ORIGIN_SYSTEM` on the patch when a `courier_draft_id` came back, and emit the same `draft.tracking_assigned` event.

In the sync path, route the tracking assignment at `webhooks.py:1519-1525` through `_apply_tracking_from_sync`.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_shipping_webhook.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git commit -m "feat(shipping): record whether a shipment was ours or manual

126 drafts carry tracking numbers this system never created, because the
operator dispatches through carrier portals and the sync writes the number
back. Status 'created' therefore says nothing about who shipped it. Record
the origin and emit draft.tracking_assigned, which is also the event the
no-tracking alert needs."
```

### Task 4.2: Backfill script

**Files:**
- Create: `scripts/backfill-shipment-origin.py`

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Backfill shipment_origin on existing drafts.

A draft with a courier_draft_id was created by this system; a draft with a
tracking number but no courier_draft_id was dispatched by hand and picked up
by the Shopify sync. Drafts with neither are left untouched.

Usage:
    python3 scripts/backfill-shipment-origin.py --dry-run
    python3 scripts/backfill-shipment-origin.py --apply
"""

from __future__ import annotations

import argparse
import collections
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--dry-run", action="store_true", help="report only (default)")
    args = parser.parse_args()
    if not args.apply:
        print("DRY RUN — pass --apply to write")

    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if not account_url:
        print("AZURE_STORAGE_ACCOUNT_URL is not set", file=sys.stderr)
        return 1

    from zdrovena.common.shipping_store import ShippingStore

    store = ShippingStore(account_url=account_url)
    client = store._table_client()
    counts: collections.Counter[str] = collections.Counter()

    for entity in client.query_entities("PartitionKey eq 'drafts'"):
        if entity.get("shipment_origin"):
            counts["already set"] += 1
            continue
        if str(entity.get("courier_draft_id") or "").strip():
            origin = "system"
        elif str(entity.get("tracking_number") or "").strip():
            origin = "external"
        else:
            counts["no tracking — skipped"] += 1
            continue
        counts[origin] += 1
        if args.apply:
            store.update_draft(entity["RowKey"], {"shipment_origin": origin})

    for key, value in counts.most_common():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the dry run against production**

Run:
```bash
AZURE_STORAGE_ACCOUNT_URL=https://zdrovenafiles.blob.core.windows.net \
  python3 scripts/backfill-shipment-origin.py --dry-run
```
Expected, based on the 2026-07-31 measurement: `system: 2`, `external: 126`, `no tracking — skipped: 61`. Investigate before applying if the numbers differ materially.

- [ ] **Step 3: Commit and open the PR**

```bash
git add scripts/backfill-shipment-origin.py
git commit -m "chore(shipping): backfill shipment_origin on existing drafts"
git push -u origin feat/shipment-origin
gh pr create --base develop --title "feat(shipping): distinguish our shipments from manual ones"
```

Run `--apply` only after the PR is merged and deployed, so the field the code writes and the field the backfill writes agree.

---

## PR 5 — Replace the alert

**Why:** the rule committed in `ec28a2c` fires whenever drafts exist and we created no shipment, which under manual shipping is permanent. It has not reached `main`; it must be replaced before it does.

### Task 5.1: Swap the rule

**Files:**
- Modify: `infra/terraform/monitoring.tf`

- [ ] **Step 1: Validate the new query against real data first**

Run:
```bash
az monitor log-analytics query -w d1af17c0-c042-4cb1-870b-c94b6e950fff --analytics-query "
let created = AppTraces
| where TimeGenerated between (ago(7d) .. ago(48h))
| where AppRoleName in ('zdrovena-api-prod','zdrovena-allegro-poller')
| extend p=parse_json(Message) | where tostring(p.event)=='draft.created'
| extend draft_id=tostring(p.draft_id) | distinct draft_id;
let tracked = AppTraces
| where TimeGenerated > ago(7d)
| extend p=parse_json(Message) | where tostring(p.event)=='draft.tracking_assigned'
| extend draft_id=tostring(p.draft_id) | distinct draft_id;
created | join kind=leftanti tracked on draft_id | summarize bez_trackingu=count()" -o table
```
Expected before PR 4 is deployed: every created draft appears, because no `draft.tracking_assigned` events exist yet. That is the signal this rule must not be deployed ahead of PR 4.

- [ ] **Step 2: Replace the resource**

In `infra/terraform/monitoring.tf`, replace the whole `azurerm_monitor_scheduled_query_rules_alert_v2 "no_shipments_despite_drafts"` resource with:

```hcl
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "orders_without_tracking" {
  name                = "${var.prefix}-alert-no-tracking"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  description         = "Zamówienia bez numeru nadania po 48h — wysyłka stoi (niezależnie od tego, kto nadaje)"
  severity            = 2

  evaluation_frequency = "PT1H"
  window_duration      = "P7D"
  scopes               = [azurerm_application_insights.ai.id]

  criteria {
    query                   = <<-KQL
      let created = traces
      | where timestamp < ago(48h)
      | where cloud_RoleName in ("${var.prefix}-api-prod", "${var.prefix}-allegro-poller")
      | extend payload = parse_json(message)
      | where tostring(payload.event) == "draft.created"
      | extend draft_id = tostring(payload.draft_id)
      | distinct draft_id;
      let tracked = traces
      | extend payload = parse_json(message)
      | where tostring(payload.event) == "draft.tracking_assigned"
      | extend draft_id = tostring(payload.draft_id)
      | distinct draft_id;
      created
      | join kind=leftanti tracked on draft_id
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  auto_mitigation_enabled = true

  action {
    action_groups = [azurerm_monitor_action_group.ops.id]
  }

  tags = local.tags
}
```

Replace the comment block above it to explain the tracking-based signal and why the previous draft-versus-shipment version was wrong: `shipment.created` only fires when *we* create the shipment, and shipping is currently manual, so that rule fires permanently.

- [ ] **Step 3: Validate**

Run: `cd infra/terraform && terraform fmt monitoring.tf && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit and open the PR**

```bash
git add infra/terraform/monitoring.tf
git commit -m "fix(monitoring): alert on orders without tracking, not on our own success

The rule added in ec28a2c fires whenever drafts exist and we created no
shipment. Shipping is currently done by hand, so shipment.created is
permanently zero and the rule would page forever. Key on the tracking
number instead, which is set whichever way the parcel was dispatched.

48h absorbs a weekend order waiting until Monday while still surfacing a
systemic stoppage in two days rather than the weeks the sender outage
survived."
git push -u origin fix/no-tracking-alert
gh pr create --base develop --title "fix(monitoring): alert on orders without tracking" --body "Depends on the draft.tracking_assigned event from the shipment_origin PR — do not merge to main before that is deployed."
```

---

## PR 6 — Allegro poller cost

**Why:** 2,016 runs a week costing 6.5 Key Vault reads, 5.9 MSI tokens and 4.5 table calls each, to produce 8 drafts in 30 days. Frequency is the only lever, because a Job starts a fresh process every cycle and no in-process cache survives.

### Task 6.1: (removed — there is no duplicated secret read)

Investigated and dropped before implementation. `allegro-client-id` is read
exactly once, in `_build_allegro_client` at `zdrovena/api/commands/allegro_poll_cmd.py:51`.

The 2.0 HTTP calls per run against 1.0 for every other secret is the Key Vault
**authentication challenge**, not a duplicate read: the first request from a
fresh process receives a 401 with a `WWW-Authenticate` header, the SDK acquires
a token and retries. `allegro-client-id` happens to be the first secret the
poller asks for, so it alone pays that cost; `allegro-client-secret`,
`allegro-refresh-token` and `allegro-access-token` reuse the token and cost one
call each.

Nothing to fix. Because the poller is a Job, the only lever that reduces this is
fewer process starts — which is Task 6.2. Recorded here so the 2:1 ratio is not
mistaken for a bug again.

### Task 6.2: Lower the poll frequency

**Files:**
- Modify: the `azurerm_container_app_job` resource for the poller in `infra/terraform/`

- [ ] **Step 1: Locate the cron**

Run: `grep -rn "cron_expression\|\*/5" infra/terraform/`

- [ ] **Step 2: Change `*/5 * * * *` to `*/20 * * * *`**

Add a comment recording the arithmetic: 8 drafts per 8,640 runs over 30 days, so a four-fold reduction costs at most 15 extra minutes of latency on an event that occurs roughly once a week.

- [ ] **Step 3: Validate**

Run: `cd infra/terraform && terraform fmt && terraform validate`
Expected: `Success!`

- [ ] **Step 4: Commit**

```bash
git commit -am "perf(allegro): poll every 20 minutes instead of every 5

The job produced 8 drafts across 8,640 runs in 30 days while making 6.5
Key Vault reads, 5.9 managed-identity token fetches and 4.5 table calls
per run. It is a Job, so each cycle is a fresh process and the in-memory
Key Vault cache never survives — frequency is the only lever that moves
all of those numbers at once."
```

### Task 6.3: Make the poller visible to monitoring

**Files:**
- Modify: `infra/terraform/monitoring.tf`

- [ ] **Step 1: Add a failure rule for the job**

```hcl
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "allegro_poller_failing" {
  name                = "${var.prefix}-alert-poller-failing"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  description         = "Poller Allegro zgłasza błędy — niewidoczny dla alert-error-rate, bo Joby nie obsługują HTTP"
  severity            = 2

  evaluation_frequency = "PT15M"
  window_duration      = "PT1H"
  scopes               = [azurerm_application_insights.ai.id]

  criteria {
    query                   = <<-KQL
      exceptions
      | where cloud_RoleName == "${var.prefix}-allegro-poller"
    KQL
    time_aggregation_method = "Count"
    threshold               = 2
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  auto_mitigation_enabled = true

  action {
    action_groups = [azurerm_monitor_action_group.ops.id]
  }

  tags = local.tags
}
```

Threshold 2 per hour tolerates the occasional transient Allegro 503 (two were observed in the period) while catching a poller that fails every cycle.

- [ ] **Step 2: Validate, commit, open the PR**

```bash
cd infra/terraform && terraform fmt && terraform validate && cd ../..
git add infra/terraform/
git commit -m "feat(monitoring): alert when the Allegro poller fails

Jobs serve no HTTP requests and alert-error-rate counts requests/failed,
so the poller could fail every cycle and nobody would be told."
git push -u origin perf/allegro-poller-cost
gh pr create --base develop --title "perf(allegro): cut poller cost and make it visible"
```

---

## Verification before any PR is opened

Every PR must pass, from the repo root:

```bash
python3 -m pytest -q -p no:randomly
python3 -m ruff check zdrovena/ tests/
python3 -m ruff format --check zdrovena/ tests/
```

Expected: all tests pass, `All checks passed!`, `NNN files already formatted`.

Pyright reports pre-existing `reportMissingImports` for `fastapi`, `pypdf`, `azure.*` in this environment. Those are not regressions; do not attempt to fix them in these PRs.

Terraform PRs additionally require `terraform validate` returning `Success!`.

## Deployment order

`develop` accumulates all six PRs. Production deploys from `main` via `prod-deploy.yml` on paths `zdrovena/**`, so a `develop` → `main` PR is what actually ships code. Terraform applies through `terraform.yml`.

PR 5 must not reach `main` before PR 4 is deployed, or the alert will fire on every draft for lack of `draft.tracking_assigned` events.
