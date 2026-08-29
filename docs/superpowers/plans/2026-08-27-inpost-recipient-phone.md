# InPost Mandatory Recipient Phone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop an InPost shipment from being created without a usable recipient phone, give the operator a way to supply one, and make sure a cleared draft cannot stay executable without it.

**Architecture:** All validation lands in `inpost_call_specs` — the one pure funnel both InPost services pass through, ahead of the paid ShipX POST and already wired into the operator's preview. A new `receiver_phone` field on the existing draft PATCH gives the operator the repair path, and two narrow guards (review-clear, resync re-flag) stop the fix from being waved away. A read-only script surfaces affected drafts before the carrier's 2026-09-08 enforcement date.

**Tech Stack:** Python 3.12, FastAPI, pytest; React 18 + Vitest; Azure Table Storage (schemaless).

**Spec:** `docs/superpowers/specs/2026-08-27-inpost-recipient-phone-design.md`
**Issue:** #294 — **must be deployed before 2026-09-08**

---

## File Structure

**Create:**
- `scripts/audit-inpost-phones.py` — read-only report of drafts that would fail after enforcement
- `frontend/src/views/shipping/RecipientPhone.jsx` + `.test.jsx` — the editable phone field

**Modify:**
- `zdrovena/common/shipping_exceptions.py` — `InPostRecipientPhoneError`
- `zdrovena/api/errors.py` — its Polish operator message
- `zdrovena/shipping/providers/inpost.py` — validate + normalise at the funnel
- `zdrovena/api/routers/webhooks.py` — `receiver_phone` on PATCH, review-clear guard
- `zdrovena/shipping/application/drafts.py` — resync re-flag exception
- `frontend/src/views/ShippingView.jsx` — wire the phone field
- `contracts/openapi.json`, `frontend/src/api/generated/schema.d.ts` — regenerated
- `CHANGELOG.md`, `docs/audit/shipment-provider-contracts.md`

**Language convention:** docstrings, comments and log messages in English. Operator-visible strings — HTTP `detail` bodies that reach a toast, `_MESSAGES_PL` entries, UI copy — in Polish.

---

## Task 1: The exception and its operator message

**Files:**
- Modify: `zdrovena/common/shipping_exceptions.py`, `zdrovena/api/errors.py`
- Test: `tests/test_shipping_exceptions.py` (create if absent — check first)

- [ ] **Step 1: Write the failing test**

```python
def test_recipient_phone_error_is_a_business_error_mapped_to_422():
    """InPost enforces a recipient phone from 2026-09-08. The operator must see
    what to fix, not a generic "carrier rejected" message."""
    from zdrovena.api.errors import _classify
    from zdrovena.common.shipping_exceptions import (
        InPostBusinessError,
        InPostRecipientPhoneError,
    )

    exc = InPostRecipientPhoneError(raw_phone="", order_id="1700")

    assert isinstance(exc, InPostBusinessError)
    status_code, message_pl = _classify(exc)
    assert status_code == 422
    assert "telefon" in message_pl.lower()
```

If `tests/test_shipping_exceptions.py` does not exist, put this in the test file that already
covers `zdrovena/api/errors.py` — find it with
`grep -rln "_classify\|_MESSAGES_PL" tests/`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/ -k recipient_phone_error -v`
Expected: FAIL — `ImportError: cannot import name 'InPostRecipientPhoneError'`

- [ ] **Step 3: Write the implementation**

In `zdrovena/common/shipping_exceptions.py`, after `InPostInvalidServiceError` (~line 270),
following that class's exact shape:

```python
class InPostRecipientPhoneError(InPostBusinessError):
    """InPost requires a valid recipient phone from 2026-09-08.

    Raised before the ShipX POST rather than after: an invalid draft must cost
    nothing, and the operator needs to know which field to fix.
    """

    def __init__(self, raw_phone: str = "", order_id: str = "") -> None:
        super().__init__(
            f"InPost requires a valid recipient phone; got {raw_phone!r}",
            order_id=order_id,
            courier="inpost",
            action="create_shipment",
            payload_snippet=raw_phone,
        )
```

In `zdrovena/api/errors.py`, add to `_MESSAGES_PL` beside the other InPost business entries:

```python
    "InPostRecipientPhoneError": (
        "InPost wymaga numeru telefonu odbiorcy — uzupełnij go w danych przesyłki."
    ),
```

No change to `_CATEGORY_FALLBACK` is needed: the class inherits `CourierBusinessError`, which
already maps to 422.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/ -k recipient_phone_error -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/shipping_exceptions.py zdrovena/api/errors.py tests/
git commit -m "feat(shipping): wyjątek dla brakującego telefonu odbiorcy InPost"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 2: Validate and normalise at the InPost funnel

This is the task that actually prevents the outage. Everything else supports it.

**Files:**
- Modify: `zdrovena/shipping/providers/inpost.py` (`inpost_call_specs`, ~line 80)
- Test: `tests/test_inpost_provider.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_inpost_provider.py` already has a `_draft(**overrides)` helper whose receiver carries
`"phone": "600100200"`, and a `_RecordingPayloadBuilder`. Reuse both.

```python
import pytest

from zdrovena.common.shipping_exceptions import InPostRecipientPhoneError


@pytest.mark.parametrize(
    "raw",
    ["+48 000 000 000", "48 000 000 000", "000 000 000", "000000000", "+48000000000"],
)
def test_every_inpost_accepted_format_reaches_the_payload_normalised(raw: str) -> None:
    """The four shapes InPost's notice lists, plus the already-normalised one."""
    draft = _draft()
    draft["receiver"] = {**draft["receiver"], "phone": raw}

    specs = inpost_call_specs(draft, _SENDER)

    assert [spec[4]["receiver_phone"] for spec in specs] == ["+48000000000"]


@pytest.mark.parametrize("raw", [None, "", "   ", "12345", "abc", "+1 202 555 0100"])
def test_an_unusable_phone_is_rejected_before_any_payload_is_built(raw) -> None:
    draft = _draft()
    draft["receiver"] = {**draft["receiver"], "phone": raw}

    with pytest.raises(InPostRecipientPhoneError):
        inpost_call_specs(draft, _SENDER)


def test_a_missing_phone_key_is_rejected() -> None:
    draft = _draft()
    receiver = dict(draft["receiver"])
    receiver.pop("phone", None)
    draft["receiver"] = receiver

    with pytest.raises(InPostRecipientPhoneError):
        inpost_call_specs(draft, _SENDER)


def test_a_legacy_raw_phone_is_normalised_rather_than_rejected() -> None:
    # Drafts written before build_draft_record normalised anything still hold
    # raw values. Those are valid numbers — normalise them, do not refuse them.
    draft = _draft()
    draft["receiver"] = {**draft["receiver"], "phone": "500 600 700"}

    specs = inpost_call_specs(draft, _SENDER)

    assert specs[0][4]["receiver_phone"] == "+48500600700"
```

`_SENDER` is the sender dict the file's other `inpost_call_specs` tests already pass — reuse the
same one rather than inventing another.

`spec[4]` is the `kwargs` element of the `InPostCallSpec` tuple
`(service, package_type, package_number, reference, kwargs)`. Confirm that index against the
neighbouring tests before relying on it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_inpost_provider.py -k phone -v`
Expected: FAIL — the accepted-format cases get the raw string back, the rejection cases raise
nothing.

- [ ] **Step 3: Write the implementation**

In `zdrovena/shipping/providers/inpost.py`, add the import beside the existing ones:

```python
from zdrovena.common.shipping_exceptions import InPostRecipientPhoneError
from zdrovena.common.shipping_format import normalize_pl_phone
```

In `inpost_call_specs`, right after `receiver = draft.get("receiver") or {}`:

```python
    # InPost enforces a valid recipient phone from 2026-09-08 (issue #294).
    # Validated here rather than at the call site: this is the one funnel both
    # the paczkomat and the kurier path share, it is pure, and it runs before
    # the paid ShipX POST. The operator's execution preview goes through the
    # same function, so the reason surfaces before they press send.
    raw_phone = receiver.get("phone")
    receiver_phone = normalize_pl_phone(raw_phone)
    if not receiver_phone:
        raise InPostRecipientPhoneError(
            raw_phone=str(raw_phone or ""),
            order_id=str(draft.get("shopify_order_number") or ""),
        )
```

Then replace **both** `"receiver_phone": receiver.get("phone", ""),` lines (currently at 88 and
98) with:

```python
                "receiver_phone": receiver_phone,
```

Normalising here, not just validating, is deliberate: a draft written before
`build_draft_record` normalised anything still holds a raw value, and that value is valid.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_inpost_provider.py -v`
Expected: PASS

- [ ] **Step 5: Prove no ShipX POST happens on bad data**

This is the assertion the issue asks for by name — it is what proves no money was spent. Add to
`tests/test_shipping_webhook.py`, in the class that already covers `_run_inpost`
(find it with `grep -n "_run_inpost" tests/test_shipping_webhook.py`):

```python
    def test_an_unusable_phone_creates_no_inpost_shipment(self):
        from zdrovena.api.shipping_execution_composition import _run_inpost
        from zdrovena.common.shipping_exceptions import InPostRecipientPhoneError

        draft = self._inpost_draft()
        draft["receiver"] = {**draft["receiver"], "phone": None}

        with (
            patch("zdrovena.api.shipping_execution_composition.get_secret", return_value="tok"),
            patch("zdrovena.common.inpost.InPostClient.create_paczkomat_shipment") as paczkomat,
            patch("zdrovena.common.inpost.InPostClient.create_kurier_shipment") as kurier,
        ):
            with pytest.raises(InPostRecipientPhoneError):
                _run_inpost(draft, _SENDER)

        paczkomat.assert_not_called()
        kurier.assert_not_called()
```

Add an `_inpost_draft()` helper to that class if it has none, mirroring the shape the
neighbouring `_run_inpost` tests already build (courier `inpost`, service
`inpost_courier_standard`, a receiver, a shipping address, one `1-pak` in
`packages_breakdown`). Do not rewrite the existing tests to use it.

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/python -m pytest tests/ -q`

Expected: all pass. **Watch for legitimate assertion changes:** any test that drives
`inpost_call_specs` and asserts on `receiver_phone` will now see `+48600100200` where it saw
`600100200`. That is the change working, not a regression — update the expected value. Do NOT
change a test that calls `InPostClient` directly (for example
`tests/test_inpost_client.py:108`); that one bypasses the funnel and its raw value is correct.

- [ ] **Step 7: Lint and types**

```
.venv/bin/python -m ruff check zdrovena/ tests/
.venv/bin/python -m ruff format --check .
/home/pepus/.local/bin/pyright
```
All must pass. pyright lives at `/home/pepus/.local/bin/pyright`, not `.venv/bin/`; its config
excludes `tests/`. `ruff format --check .` also formats python blocks inside Markdown.

- [ ] **Step 8: Commit**

```bash
git add zdrovena/shipping/providers/inpost.py tests/
git commit -m "fix(shipping): InPost nie wyśle przesyłki bez poprawnego telefonu odbiorcy"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 3: Let the operator supply the phone

Without this the previous task turns a silent failure into an unshippable, unrepairable draft:
the PATCH endpoint has no phone field today.

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py` (`update_draft`)
- Test: `tests/test_shipping_webhook.py`

- [ ] **Step 1: Write the failing tests**

Add a new class beside `TestUpdateDraftPackagesBreakdown`:

```python
class TestUpdateDraftRecipientPhone:
    @staticmethod
    def _seed(store, phone=None, courier="inpost", status="needs_review"):
        draft = {
            "id": "draft-phone-1",
            "shopify_order_number": "1802",
            "courier": courier,
            "service": "inpost_courier_standard" if courier == "inpost" else "apaczka",
            "apaczka_service_id": None if courier == "inpost" else "21",
            "status": status,
            "receiver": {"first_name": "Jan", "last_name": "K", "email": "", "phone": phone},
            "courier_shipments": [],
        }
        store.upsert_draft(draft)
        return draft

    def test_stores_the_normalised_number(self, client, store):
        draft = self._seed(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}", json={"receiver_phone": "600 100 200"}
        )
        assert resp.status_code == 200
        assert store.get_draft(draft["id"])["receiver"]["phone"] == "+48600100200"

    @pytest.mark.parametrize("raw", ["", "12345", "abc", "+1 202 555 0100"])
    def test_rejects_a_number_inpost_will_not_accept(self, client, store, raw):
        draft = self._seed(store, phone="+48600100200")
        resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"receiver_phone": raw})
        assert resp.status_code == 400
        # the stored value must survive a rejected edit
        assert store.get_draft(draft["id"])["receiver"]["phone"] == "+48600100200"

    def test_saving_a_phone_does_not_clear_needs_review(self, client, store):
        # Clearing review stays a separate, explicit click: the operator confirms
        # the whole draft, not one field.
        draft = self._seed(store)
        resp = client.patch(
            f"/api/shipping/drafts/{draft['id']}", json={"receiver_phone": "600100200"}
        )
        assert resp.status_code == 200
        assert store.get_draft(draft["id"])["status"] == "needs_review"

    def test_keeps_the_rest_of_the_receiver(self, client, store):
        draft = self._seed(store)
        client.patch(f"/api/shipping/drafts/{draft['id']}", json={"receiver_phone": "600100200"})
        receiver = store.get_draft(draft["id"])["receiver"]
        assert receiver["first_name"] == "Jan"
        assert receiver["email"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_shipping_webhook.py -k UpdateDraftRecipientPhone -v`
Expected: FAIL — the unknown body field is ignored, so the phone never changes.

- [ ] **Step 3: Write the implementation**

Add the parameter to `update_draft`'s signature, after `locker_id`:

```python
receiver_phone: str | None = (Body(None),)
```

and this branch beside the existing `locker_id` one:

```python
    if receiver_phone is not None:
        from zdrovena.common.shipping_format import normalize_pl_phone

        normalized = normalize_pl_phone(receiver_phone)
        if not normalized:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Numer telefonu nie jest poprawnym polskim numerem "
                    "(oczekiwane 9 cyfr lub +48 i 9 cyfr)"
                ),
            )
        receiver = dict(draft.get("receiver") or {})
        receiver["phone"] = normalized
        patch["receiver"] = receiver
```

Note this reuses `patch["receiver"]`, which the `locker_id` branch also writes. If a request
carries both fields, the second assignment would discard the first. Build the receiver dict once,
before both branches, and have each branch mutate it — check the current shape of the `locker_id`
branch and restructure so both can apply in one request.

Update the endpoint `summary=` and the module docstring line to mention `receiver_phone`.

- [ ] **Step 4: Run tests to verify they pass**

```
.venv/bin/python -m pytest tests/test_shipping_webhook.py -k UpdateDraft -v
.venv/bin/python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git commit -m "feat(shipping): operator może uzupełnić telefon odbiorcy w portalu"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 4: Stop the review click from waving the blocker through

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py` (`update_draft`, the `reviewed is True` branch)
- Test: `tests/test_shipping_webhook.py`

- [ ] **Step 1: Write the failing tests**

Add to `TestUpdateDraftRecipientPhone`:

```python
def test_review_cannot_be_cleared_while_an_inpost_draft_has_no_usable_phone(self, client, store):
    # Otherwise one click makes the draft executable and it ships with a
    # null phone — which InPost rejects from 2026-09-08.
    draft = self._seed(store, phone=None)
    resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"reviewed": True})
    assert resp.status_code == 400
    assert store.get_draft(draft["id"])["status"] == "needs_review"


def test_review_clears_once_a_usable_phone_is_present(self, client, store):
    draft = self._seed(store, phone="+48600100200")
    resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"reviewed": True})
    assert resp.status_code == 200
    assert store.get_draft(draft["id"])["status"] == "pending"


def test_the_block_does_not_apply_to_other_carriers(self, client, store):
    # Apaczka has announced no such enforcement; the issue scopes this to InPost.
    draft = self._seed(store, phone=None, courier="apaczka")
    resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"reviewed": True})
    assert resp.status_code == 200
    assert store.get_draft(draft["id"])["status"] == "pending"


def test_a_phone_and_a_review_can_be_sent_together(self, client, store):
    draft = self._seed(store, phone=None)
    resp = client.patch(
        f"/api/shipping/drafts/{draft['id']}",
        json={"receiver_phone": "600100200", "reviewed": True},
    )
    assert resp.status_code == 200
    updated = store.get_draft(draft["id"])
    assert updated["receiver"]["phone"] == "+48600100200"
    assert updated["status"] == "pending"
```

The last test is the one that decides the implementation order: the guard must read the phone
**after** the `receiver_phone` branch has applied, not from the stored draft.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_shipping_webhook.py -k "review_cannot_be_cleared or phone_and_a_review" -v`
Expected: FAIL — review clears unconditionally today.

- [ ] **Step 3: Write the implementation**

Replace the existing `reviewed is True` branch with:

```python
if reviewed is True and draft.get("status") == "needs_review":
    # Read the phone from the patch first: an operator supplying the number
    # and clearing review in one request must succeed.
    effective_receiver = patch.get("receiver") or draft.get("receiver") or {}
    if draft.get("courier") == "inpost" and not normalize_pl_phone(effective_receiver.get("phone")):
        raise HTTPException(
            status_code=400,
            detail=(
                "InPost wymaga numeru telefonu odbiorcy — uzupełnij go przed zatwierdzeniem draftu"
            ),
        )
    patch["status"] = "pending"
    patch["error"] = None
```

Move the `normalize_pl_phone` import to the module's top-level imports rather than repeating the
local import from Task 3.

- [ ] **Step 4: Run tests to verify they pass**

```
.venv/bin/python -m pytest tests/test_shipping_webhook.py -k UpdateDraft -v
.venv/bin/python -m pytest tests/ -q
```
Expected: all pass. If an existing test clears review on a phone-less InPost draft, that test was
encoding the bug — give its draft a valid phone rather than removing the guard.

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git commit -m "fix(shipping): zatwierdzenie draftu InPost wymaga telefonu odbiorcy"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 5: Re-flag on resync so the hole cannot reopen

**Files:**
- Modify: `zdrovena/shipping/application/drafts.py` (`merge_synced_draft`, ~line 163)
- Test: `tests/test_shipping_draft_application.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestInPostPhoneReFlagsOnSync:
    @staticmethod
    def _pair(existing_phone, incoming_phone, courier="inpost"):
        base = {"id": "d1", "courier": courier, "service": "inpost_courier_standard"}
        existing = {
            **base,
            "status": "pending",
            "receiver": {"phone": existing_phone},
        }
        incoming = {
            **base,
            "status": "needs_review",
            "receiver": {"phone": incoming_phone},
        }
        return existing, incoming

    def test_a_cleared_inpost_draft_without_a_phone_is_re_flagged(self):
        # The "already cleared is not re-flagged" rule exists so routine noise
        # does not undo an operator decision. A phone InPost will reject is not
        # noise — after 2026-09-08 the shipment simply cannot be created.
        existing, incoming = self._pair(None, None)

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["status"] == "needs_review"

    def test_a_cleared_inpost_draft_with_a_phone_stays_cleared(self):
        existing, incoming = self._pair("+48600100200", "+48600100200")

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["status"] == "pending"

    def test_other_carriers_keep_the_old_behaviour(self):
        existing, incoming = self._pair(None, None, courier="apaczka")

        merged = merge_synced_draft(existing, incoming, emit_tracking_assigned=lambda *_: None)

        assert merged["status"] == "pending"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_shipping_draft_application.py -k InPostPhoneReFlags -v`
Expected: FAIL — the first test gets `pending`.

- [ ] **Step 3: Write the implementation**

In `zdrovena/shipping/application/drafts.py`, add the import:

```python
from zdrovena.common.shipping_format import normalize_pl_phone
```

and a helper next to the other module-level helpers:

```python
def _inpost_phone_missing(draft: dict[str, Any]) -> bool:
    """Return whether an InPost draft lacks a phone the carrier will accept.

    InPost enforces this from 2026-09-08. A draft in that state cannot ship, so
    it must not sit in `pending` looking ready.
    """
    if draft.get("courier") != "inpost":
        return False
    return not normalize_pl_phone((draft.get("receiver") or {}).get("phone"))
```

Then extend the existing "already cleared is not re-flagged" condition (~line 163) so the missing
phone is a second exception alongside `unreadable_products`:

```python
    elif (
        existing_status == "pending"
        and incoming_status == "needs_review"
        and not merged.get("unreadable_products")
        and not _inpost_phone_missing(merged)
    ):
        # A draft the operator already cleared is not re-flagged. Two exceptions:
        # a product name the planner cannot read, and an InPost draft without a
        # phone the carrier will accept. Both are executable states that cannot
        # actually ship — which is how #1710-#1712 went out in the wrong boxes.
        merged["status"] = "pending"
```

Update the existing comment to describe both exceptions, as shown.

- [ ] **Step 4: Run tests to verify they pass**

```
.venv/bin/python -m pytest tests/test_shipping_draft_application.py -v
.venv/bin/python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add zdrovena/shipping/application/drafts.py tests/test_shipping_draft_application.py
git commit -m "fix(shipping): resync ponownie flaguje draft InPost bez telefonu"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 6: Regenerate the API contracts

The PATCH body gained a field, and `scripts/check-api-contracts.sh` fails the gate on drift.

**Files:**
- Modify: `contracts/openapi.json`, `frontend/src/api/generated/schema.d.ts`

- [ ] **Step 1: Regenerate**

Run: `scripts/generate-api-contracts.sh`

- [ ] **Step 2: Verify no drift remains**

Run: `scripts/check-api-contracts.sh`
Expected: exits 0, no drift message.

- [ ] **Step 3: Confirm the diff is only what you expect**

Run: `git diff --stat contracts/openapi.json frontend/src/api/generated/schema.d.ts`
Expected: the new `receiver_phone` field and the updated endpoint summary, nothing else.

- [ ] **Step 4: Commit**

```bash
git add contracts/openapi.json frontend/src/api/generated/schema.d.ts
git commit -m "chore(api): regeneracja kontraktów po dodaniu receiver_phone"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 7: The phone field in the portal

**Files:**
- Create: `frontend/src/views/shipping/RecipientPhone.jsx`, `frontend/src/views/shipping/RecipientPhone.test.jsx`
- Modify: `frontend/src/views/ShippingView.jsx`

**Before you start:** run `npm --prefix frontend ci`. Local `node_modules` drifts behind
`package-lock.json`, and a stale `eslint-plugin-react-hooks` makes `npm run lint` weaker than CI —
that cost a round-trip on the previous change. Do not sync state in a `useEffect`; the pinned
plugin forbids it. Adjust state during render instead (compare a prop against a `useState`-held
copy and `setState` in the render body).

- [ ] **Step 1: Write the failing test**

Create `frontend/src/views/shipping/RecipientPhone.test.jsx`:

```jsx
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { RecipientPhone } from './RecipientPhone'

describe('RecipientPhone', () => {
    it('saves the number the operator typed', async () => {
        const user = userEvent.setup()
        const onSave = vi.fn()
        render(<RecipientPhone phone={null} canEdit onSave={onSave} />)

        await user.type(screen.getByLabelText('Telefon odbiorcy'), '600100200')
        await user.click(screen.getByRole('button', { name: 'Zapisz telefon' }))

        await waitFor(() => expect(onSave).toHaveBeenCalledWith('600100200'))
    })

    it('will not save an unchanged number', () => {
        render(<RecipientPhone phone="+48600100200" canEdit onSave={vi.fn()} />)

        expect(screen.getByRole('button', { name: 'Zapisz telefon' })).toBeDisabled()
    })

    it('will not save an empty field', async () => {
        const user = userEvent.setup()
        render(<RecipientPhone phone="+48600100200" canEdit onSave={vi.fn()} />)

        await user.clear(screen.getByLabelText('Telefon odbiorcy'))

        expect(screen.getByRole('button', { name: 'Zapisz telefon' })).toBeDisabled()
    })

    it('warns when InPost would reject the stored number', () => {
        render(<RecipientPhone phone={null} canEdit onSave={vi.fn()} courier="inpost" />)

        expect(screen.getByText('InPost wymaga telefonu odbiorcy')).toBeInTheDocument()
    })

    it('does not warn for other carriers', () => {
        render(<RecipientPhone phone={null} canEdit onSave={vi.fn()} courier="apaczka" />)

        expect(screen.queryByText('InPost wymaga telefonu odbiorcy')).not.toBeInTheDocument()
    })

    it('renders read-only without permission', () => {
        render(<RecipientPhone phone="+48600100200" canEdit={false} onSave={vi.fn()} />)

        expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
        expect(screen.getByText('+48600100200')).toBeInTheDocument()
    })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix frontend test -- RecipientPhone`
Expected: FAIL — cannot resolve `./RecipientPhone`

- [ ] **Step 3: Write the implementation**

Create `frontend/src/views/shipping/RecipientPhone.jsx`:

```jsx
import { useState } from 'react'

// Mirrors normalize_pl_phone in zdrovena/common/shipping_format.py. Used only
// to decide whether to show the warning — the API is the authority and
// re-validates every save.
function looksUsable(value) {
    const digits = String(value || '').replace(/\D/g, '')
    return digits.length === 9 || (digits.startsWith('48') && digits.length === 11)
}

/**
 * InPost enforces a valid recipient phone from 2026-09-08. Before this field
 * existed the operator could only wave a phone-less draft through, never fix it.
 */
export function RecipientPhone({ phone, canEdit, onSave, courier, saving = false }) {
    const stored = phone || ''
    const [value, setValue] = useState(stored)
    const [syncedPhone, setSyncedPhone] = useState(stored)
    if (stored !== syncedPhone) {
        setSyncedPhone(stored)
        setValue(stored)
    }

    const warn = courier === 'inpost' && !looksUsable(stored)

    if (!canEdit) {
        return (
            <>
                <div className="detail-label">Telefon odbiorcy</div>
                <div className="mono">{stored || <span className="dim">—</span>}</div>
                {warn && <div style={WARN_STYLE}>InPost wymaga telefonu odbiorcy</div>}
            </>
        )
    }

    return (
        <>
            <div className="detail-label">Telefon odbiorcy</div>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                <input
                    aria-label="Telefon odbiorcy"
                    value={value}
                    onChange={e => setValue(e.target.value)}
                    placeholder="600100200"
                    style={{ width: 150 }} />
                <button
                    type="button"
                    disabled={saving || !value.trim() || value.trim() === stored}
                    onClick={() => onSave(value.trim())}>
                    Zapisz telefon
                </button>
            </div>
            {warn && <div style={WARN_STYLE}>InPost wymaga telefonu odbiorcy</div>}
        </>
    )
}

const WARN_STYLE = { marginTop: 4, fontSize: '0.82em', color: 'var(--warn, #b45309)' }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix frontend test -- RecipientPhone`
Expected: PASS, 6 passed

- [ ] **Step 5: Wire it into the draft row**

In `frontend/src/views/ShippingView.jsx`:

Add the import beside `TrackingList` and `PackagesEditor`:

```js
import { RecipientPhone } from './shipping/RecipientPhone'
```

Render it in the expanded detail panel, in the same column as `TrackingList`, directly above it
(find that column by searching for `<TrackingList draft={draft} />`):

```jsx
                                <RecipientPhone
                                    phone={draft.receiver?.phone}
                                    courier={draft.courier}
                                    canEdit={canManage && !PACKAGES_LOCKED_STATUSES.has(draft.status)}
                                    saving={isBusy}
                                    onSave={value => onSavePhone(draft, value)}
                                />
```

`PACKAGES_LOCKED_STATUSES` and `isBusy` already exist in that component.

Add `onSavePhone` to `DraftRow`'s destructured props, pass `onSavePhone={handleSavePhone}` from
the list render next to `onSavePackages={handleSavePackages}`, and add the handler beside
`handleSavePackages`:

```js
    function handleSavePhone(draft, phone) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ receiver_phone: phone }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(apiErrorMessage(body, res))
            }
        }, 'Nie udało się zapisać telefonu')()
    }
```

`withBusy` already reloads on success and toasts on failure — do not add another `load()`.

- [ ] **Step 6: Verify**

```
npm --prefix frontend test
npm --prefix frontend run lint
```
Both must pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/views/shipping/RecipientPhone.jsx frontend/src/views/shipping/RecipientPhone.test.jsx frontend/src/views/ShippingView.jsx
git commit -m "feat(shipping): pole telefonu odbiorcy w portalu"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 8: Audit script for the drafts already at risk

**Files:**
- Create: `scripts/audit-inpost-phones.py`
- Test: `tests/test_audit_inpost_phones.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for scripts/audit-inpost-phones.py.

The script exists so the operator finds affected drafts before InPost starts
enforcing on 2026-09-08, rather than one failed shipment at a time afterwards.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "audit_inpost_phones",
    Path(__file__).resolve().parent.parent / "scripts" / "audit-inpost-phones.py",
)
assert _SPEC and _SPEC.loader
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _draft(**overrides):
    draft = {
        "id": "d1",
        "shopify_order_number": "1700",
        "courier": "inpost",
        "status": "pending",
        "receiver": {"phone": "+48600100200"},
    }
    draft.update(overrides)
    return draft


class TestNeedsAttention:
    def test_flags_an_inpost_draft_without_a_usable_phone(self):
        assert audit.needs_attention(_draft(receiver={"phone": None})) is True
        assert audit.needs_attention(_draft(receiver={"phone": "12345"})) is True
        assert audit.needs_attention(_draft(receiver={})) is True

    def test_ignores_a_usable_phone(self):
        assert audit.needs_attention(_draft()) is False
        assert audit.needs_attention(_draft(receiver={"phone": "600 100 200"})) is False

    def test_ignores_other_carriers(self):
        assert audit.needs_attention(_draft(courier="apaczka", receiver={"phone": None})) is False

    def test_ignores_terminal_statuses(self):
        # A shipment already created or cancelled will never be posted again.
        for status in ("created", "cancelled"):
            assert audit.needs_attention(_draft(status=status, receiver={"phone": None})) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_audit_inpost_phones.py -v`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write the implementation**

Create `scripts/audit-inpost-phones.py`, following the structure of
`scripts/backfill-shipment-origin.py` (read it first for the store-construction idiom):

```python
#!/usr/bin/env python3
"""Report InPost drafts that will stop shipping when the carrier enforces phones.

InPost makes the recipient phone mandatory and validated on 2026-09-08. A draft
whose stored phone will not normalise cannot be shipped after that date. This
script finds those drafts so they can be fixed in advance, instead of surfacing
one failed shipment at a time on the morning of the deadline.

Read-only. It never writes to the store, and it never invents a phone number.

Usage:
    python3 scripts/audit-inpost-phones.py
"""

from __future__ import annotations

import os
import sys

# Statuses past which no further ShipX POST happens for this draft.
TERMINAL_STATUSES = frozenset({"created", "cancelled"})


def needs_attention(draft: dict) -> bool:
    """Return whether this draft would fail InPost's phone validation."""
    from zdrovena.common.shipping_format import normalize_pl_phone

    if draft.get("courier") != "inpost":
        return False
    if draft.get("status") in TERMINAL_STATUSES:
        return False
    return not normalize_pl_phone((draft.get("receiver") or {}).get("phone"))


def main() -> int:
    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if not account_url:
        print("AZURE_STORAGE_ACCOUNT_URL is not set", file=sys.stderr)
        return 2

    from zdrovena.common.shipping_store import ShippingStore

    store = ShippingStore(account_url=account_url)
    drafts = store.list_drafts(limit=10_000)
    affected = [draft for draft in drafts if needs_attention(draft)]

    print(f"Scanned {len(drafts)} drafts; {len(affected)} would fail InPost phone validation.")
    if not affected:
        return 0

    print(f"{'draft id':38} {'order':10} {'status':22} stored phone")
    for draft in affected:
        raw = (draft.get("receiver") or {}).get("phone")
        print(
            f"{str(draft.get('id')):38} "
            f"{str(draft.get('shopify_order_number') or ''):10} "
            f"{str(draft.get('status') or ''):22} "
            f"{raw!r}"
        )
    print("\nFix each one in the portal (Telefon odbiorcy) or in the source order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Confirm the `AZURE_STORAGE_ACCOUNT_URL` env-var name and the `ShippingStore` construction against
`scripts/backfill-shipment-origin.py` and use whatever that script actually uses.

`list_drafts(limit=10_000)` is a full-partition scan, which issue #316 wants removed. This script
is a one-off run before a deadline, not a hot path — but note it in the commit message so the
issue's inventory stays accurate.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_audit_inpost_phones.py -v`
Expected: PASS

- [ ] **Step 5: Lint**

```
.venv/bin/python -m ruff check scripts/ tests/
.venv/bin/python -m ruff format --check .
/home/pepus/.local/bin/pyright
```
All must pass — pyright's `include` covers `scripts/`.

- [ ] **Step 6: Commit**

```bash
git add scripts/audit-inpost-phones.py tests/test_audit_inpost_phones.py
git commit -m "feat(shipping): audyt draftów InPost bez telefonu odbiorcy"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 9: Documentation

**Files:**
- Modify: `docs/audit/shipment-provider-contracts.md`, `CHANGELOG.md`

- [ ] **Step 1: Record the carrier contract**

Add to the InPost section of `docs/audit/shipment-provider-contracts.md`, before its `Źródła:`
line:

```markdown
Od 2026-09-08 InPost wymaga numeru telefonu odbiorcy i go waliduje; brak lub zły
format blokuje utworzenie przesyłki. Akceptowane formaty: `+48 000 000 000`,
`48 000 000 000`, `000 000 000` oraz te same bez spacji. Zdrovena waliduje
i normalizuje numer w `inpost_call_specs()`
(`zdrovena/shipping/providers/inpost.py`) — czyli w jedynym lejku wspólnym dla
paczkomatu i kuriera, przed płatnym POST-em do ShipX. Niepoprawny numer to
`InPostRecipientPhoneError` (HTTP 422), widoczny też w podglądzie wykonania jako
`preview_available: false`. Apaczka i Allegro nie mają odpowiednika tego wymogu.
```

- [ ] **Step 2: Update the changelog**

Under `## Unreleased` in `CHANGELOG.md` (create an `### Fixed` subsection if there is none):

```markdown
- **shipping**: InPost wymaga numeru telefonu odbiorcy od 2026-09-08 — portal waliduje go teraz
  przed wysłaniem przesyłki, zamiast czekać na odrzucenie przez ShipX. Numer jest normalizowany
  w jedynym lejku ścieżki InPost, więc stare drafty z surową wartością też przechodzą. Operator
  może uzupełnić brakujący numer w portalu (pole „Telefon odbiorcy"); wcześniej mógł go tylko
  przepchnąć, bo PATCH nie miał takiego pola. Zatwierdzenie draftu InPost bez numeru zwraca teraz
  400, a resynchronizacja z Shopify ponownie flaguje taki draft — dotąd jedno kliknięcie czyniło
  go wykonywalnym na zawsze. `scripts/audit-inpost-phones.py` (tylko odczyt) wypisuje drafty do
  naprawienia przed terminem. Apaczka i Allegro bez zmian.
```

- [ ] **Step 3: Commit**

```bash
git add docs/audit/shipment-provider-contracts.md CHANGELOG.md
git commit -m "docs(shipping): kontrakt telefonu odbiorcy InPost i changelog (#294)"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 10: Full gate and PR

- [ ] **Step 1: Sync the frontend to the lockfile**

Run: `npm --prefix frontend ci`

Local `node_modules` drifts behind `package-lock.json`; without this the lint step is weaker than
CI's.

- [ ] **Step 2: Run the complete quality gate**

Run: `CHECK_DOCS_FASTPATH=0 bash scripts/check.sh`
Expected: `All checks passed — safe to push.`

- [ ] **Step 3: If coverage drops below the threshold**

Do **not** lower `--cov-fail-under` or add `# pragma: no cover`. Per `CLAUDE.md`, report the
shortfall and propose either the missing tests or a justified `omit`, and wait for the owner's
decision.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin fix/inpost-recipient-phone-294
```

Open it against `develop` — features merge there, and `develop → main` is a separate promotion
that triggers `prod-deploy.yml`. Reference `Closes #294` in the body, and state the 2026-09-08
deadline prominently so the promotion to `main` is not left sitting.

---

## Verification against the spec

| Spec section | Tasks |
| --- | --- |
| 1. Validate at the single funnel | 1, 2 |
| 2. Give the operator a way to fix it | 3, 6, 7 |
| 3. Close the "cleared forever" hole | 4, 5 |
| 4. Find the affected drafts before the deadline | 8 |
| Testing (every case the issue names) | 2, 3, 4, 5, 8 |
| Docs / contract record | 9 |

## After the merge

This is only shipped once `develop → main` runs, which is what deploys to production. The
deadline is **2026-09-08** — a PR sitting in `develop` does not protect anything.

Then run `scripts/audit-inpost-phones.py` against production and hand the operator the list.
