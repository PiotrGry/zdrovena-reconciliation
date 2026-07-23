# Apaczka Per-Draft Service Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single global `apaczka-service-id` Key Vault secret with a per-draft `apaczka_service_id` field, derived from the Shopify shipping-line title (mirroring the existing `INPOST_SERVICE_TITLE_MAP` pattern), with operator fallback via a new minimal admin UI.

**Architecture:** A new `APACZKA_SERVICE_TITLE_MAP` env var + `_pick_apaczka_service()` helper (identical convention to `_pick_inpost_service`) sets `apaczka_service_id` on the draft at creation time; no match → `needs_review`. The three call sites that previously read `get_secret("apaczka_service_id")` read `draft.get("apaczka_service_id")` instead (or drop it entirely where unused). A new `APACZKA_SERVICE_CATALOG` constant validates operator input via an extended `PATCH /shipping/drafts/{id}` and powers a new `GET /shipping/apaczka-services` endpoint for a minimal frontend dropdown.

**Tech Stack:** Python 3.10, FastAPI, pytest, React (no build-time test framework — frontend change verified manually via `dev.sh`).

**Design spec:** `docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md`

---

### Task 1: `APACZKA_SERVICE_CATALOG` constant + docstring fix

**Files:**
- Modify: `zdrovena/common/apaczka.py`

- [ ] **Step 1: Add the catalog constant**

Add this after the existing module-level constants (after `_APACZKA_BODY_OK = 200`, before `def _sign(...)`):

```python
# Curated subset of Apaczka's ~70 service_ids (fetched live from the
# `service_structure` endpoint, verified 2026-07-09), covering non-InPost
# door-to-door and locker/pickup-point ("skrytki") delivery. InPost-supplier
# entries are deliberately excluded — those ship through the dedicated InPost
# integration, never through Apaczka. See
# docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md for the full
# rationale and how to extend this list.
APACZKA_SERVICE_CATALOG: dict[str, str] = {
    # Door-to-door
    "1": "UPS Standard",
    "2": "UPS Express Saver",
    "3": "UPS Express Plus do 12:00",
    "4": "UPS Express Plus do 9:00",
    "21": "DPD Kurier",
    "24": "DPD Kurier do 9:30",
    "25": "DPD Kurier do 12:00",
    "60": "Pocztex Kurier Drzwi-Drzwi",
    "82": "DHL Parcel Kurier",
    "83": "DHL Parcel Kurier do 12:00",
    "84": "DHL Parcel Kurier do 9:00",
    "151": "FEDEX Kurier",
    "202": "GLS Kurier Drzwi-Drzwi",
    # Point / locker ("skrytki")
    "14": "UPS AP Punkt-Punkt",
    "15": "UPS AP Drzwi-Punkt",
    "23": "DPD Pickup Drzwi-Punkt",
    "26": "DPD Pickup Punkt-Punkt",
    "50": "Orlen Paczka Punkt-Punkt",
    "53": "Orlen Paczka Drzwi-Punkt",
    "64": "Pocztex Kurier Drzwi-Punkt",
    "66": "Pocztex Punkt Punkt-Punkt",
    "86": "DHL POP do punktu",
    "203": "GLS Kurier Drzwi-Punkt",
    "314": "Packeta Punkt-Punkt",
    "317": "Packeta Magazyn-Punkt",
}
```

- [ ] **Step 2: Fix the module docstring**

Change:
```python
"""zdrovena.common.apaczka — Apaczka API v2 client.

Creates shipment drafts. Auth uses per-request HMAC-SHA256 signatures.
Service structure is cached in blob storage (max once per 23 hours).
Secrets: apaczka-app-id, apaczka-app-secret, apaczka-service-id (Key Vault).
"""
```

To:
```python
"""zdrovena.common.apaczka — Apaczka API v2 client.

Creates shipment drafts. Auth uses per-request HMAC-SHA256 signatures.
Service structure is cached in blob storage (max once per 23 hours).
Secrets: apaczka-app-id, apaczka-app-secret (Key Vault). ``service_id`` is
per-draft data (from the Shopify shipping-line title, or set manually by an
operator), never a global secret — see APACZKA_SERVICE_CATALOG below and
docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md.
"""
```

- [ ] **Step 3: Verify no test breaks**

Run: `uv run pytest tests/ -k apaczka -v`
Expected: all pre-existing Apaczka tests still PASS (this task only adds a constant and edits a docstring, no behavior change yet).

- [ ] **Step 4: Commit**

```bash
git add zdrovena/common/apaczka.py
git commit -m "feat(apaczka): add curated service catalog constant"
```

---

### Task 2: `_pick_apaczka_service()` title-mapping helper

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py`
- Test: `tests/test_courier_picking.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_courier_picking.py` (extend the existing `_clean_env` fixture and imports, add a new test class at the end of the file):

```python
from zdrovena.api.routers.webhooks import (
    _parse_title_map,
    _pick_apaczka_service,
    _pick_courier,
    _pick_inpost_service,
    _reset_courier_maps_cache,
)
```

(Replace the existing `from zdrovena.api.routers.webhooks import (...)` import block with this one — same names plus `_pick_apaczka_service`.)

Update `_clean_env` to also clear the new env var:

```python
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ENV + cache between tests."""
    monkeypatch.delenv("COURIER_TITLE_MAP", raising=False)
    monkeypatch.delenv("INPOST_SERVICE_TITLE_MAP", raising=False)
    monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
    _reset_courier_maps_cache()
    yield
    _reset_courier_maps_cache()
```

Add at the end of the file:

```python
# ── _pick_apaczka_service ────────────────────────────────────────────────────


class TestPickApaczkaService:
    def test_no_env_configured_returns_none(self) -> None:
        assert _pick_apaczka_service("Apaczka DPD") is None

    def test_env_mapping_match_returns_service_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21;orlen paczka=53")
        _reset_courier_maps_cache()
        assert _pick_apaczka_service("Apaczka DPD") == "21"

    def test_env_mapping_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "orlen paczka=53")
        _reset_courier_maps_cache()
        assert _pick_apaczka_service("ORLEN PACZKA - punkt odbioru") == "53"

    def test_no_match_in_configured_map_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21")
        _reset_courier_maps_cache()
        assert _pick_apaczka_service("UPS Express") is None

    def test_json_env_mapping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", '{"dpd": "21", "ups": "1"}')
        _reset_courier_maps_cache()
        assert _pick_apaczka_service("UPS Standard") == "1"

    def test_no_substring_heuristic_fallback(self) -> None:
        """Unlike _pick_courier/_pick_inpost_service, there is no heuristic here —
        Apaczka title strings aren't predictable substrings like inpost/paczkomat."""
        assert _pick_apaczka_service("Kurier ekspresowy XYZ") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_courier_picking.py -v`
Expected: `ImportError: cannot import name '_pick_apaczka_service'` (function doesn't exist yet).

- [ ] **Step 3: Implement the helper**

In `zdrovena/api/routers/webhooks.py`, immediately after `_inpost_service_title_map()` (before `def _reset_courier_maps_cache():`), add:

```python
@lru_cache(maxsize=1)
def _apaczka_service_title_map() -> dict[str, str]:
    """Explicit title → Apaczka service_id mapping from ``APACZKA_SERVICE_TITLE_MAP``.

    Example: ``APACZKA_SERVICE_TITLE_MAP="dpd kurier=21;orlen paczka=53"``.
    """
    return _parse_title_map(os.getenv("APACZKA_SERVICE_TITLE_MAP", ""))


def _pick_apaczka_service(title: str) -> str | None:
    """Map a Shopify shipping-line title to an Apaczka service_id.

    Unlike ``_pick_courier``/``_pick_inpost_service`` there is no
    substring-heuristic fallback: Apaczka's title strings are
    business-configured Shopify shipping-method names, not consistently
    predictable substrings. No configured match -> None, which routes the
    draft to needs_review (see Task 3) instead of guessing.
    """
    lowered = title.lower()
    for keyword, service_id in _apaczka_service_title_map().items():
        if keyword and keyword in lowered:
            return service_id
    return None
```

Update `_reset_courier_maps_cache()`:

```python
def _reset_courier_maps_cache() -> None:
    """Clear cached ENV mapping (test-only helper)."""
    _courier_title_map.cache_clear()
    _inpost_service_title_map.cache_clear()
    _apaczka_service_title_map.cache_clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_courier_picking.py -v`
Expected: all PASS, including the 6 new `TestPickApaczkaService` tests.

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_courier_picking.py
git commit -m "feat(shipping): add _pick_apaczka_service title-mapping helper"
```

---

### Task 3: Wire into `_create_draft`

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py`
- Test: `tests/test_shipping_webhook.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shipping_webhook.py`, inside `class TestCreateDraftApaczka:` (after the existing `test_apaczka_draft_stored` method):

```python
def test_apaczka_service_id_set_from_title_map(self, store, monkeypatch):
    from zdrovena.api.routers.webhooks import _create_draft, _reset_courier_maps_cache

    monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21")
    _reset_courier_maps_cache()
    try:
        storage = object()
        order = _load_fixture("shopify_order_apaczka.json")
        _create_draft(order, store, storage)
        drafts = store.list_drafts()
        assert drafts[0]["apaczka_service_id"] == "21"
    finally:
        monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
        _reset_courier_maps_cache()


def test_apaczka_service_id_none_forces_needs_review(self, store, monkeypatch):
    """Fixture's shipping_lines[0].title is 'Apaczka DPD' — with no env
    mapping configured, apaczka_service_id stays unset and the draft must
    be needs_review even if phone/packages_count would otherwise pass."""
    from zdrovena.api.routers.webhooks import _create_draft, _reset_courier_maps_cache

    monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
    _reset_courier_maps_cache()
    order = _load_fixture("shopify_order_apaczka.json")
    order["shipping_address"]["phone"] = "500600700"
    order["customer"]["phone"] = "500600700"
    storage = object()
    _create_draft(order, store, storage)
    drafts = store.list_drafts()
    assert drafts[0]["apaczka_service_id"] is None
    assert drafts[0]["status"] == "needs_review"


def test_apaczka_service_id_matched_allows_pending(self, store, monkeypatch):
    """Same phone fix as above, but WITH a matching title map — status
    should be 'pending', proving apaczka_service_id was the only blocker."""
    from zdrovena.api.routers.webhooks import _create_draft, _reset_courier_maps_cache

    monkeypatch.setenv("APACZKA_SERVICE_TITLE_MAP", "dpd=21")
    _reset_courier_maps_cache()
    try:
        order = _load_fixture("shopify_order_apaczka.json")
        order["shipping_address"]["phone"] = "500600700"
        order["customer"]["phone"] = "500600700"
        storage = object()
        _create_draft(order, store, storage)
        drafts = store.list_drafts()
        assert drafts[0]["apaczka_service_id"] == "21"
        assert drafts[0]["status"] == "pending"
    finally:
        monkeypatch.delenv("APACZKA_SERVICE_TITLE_MAP", raising=False)
        _reset_courier_maps_cache()


def test_non_apaczka_draft_has_none_apaczka_service_id(self, store):
    """InPost/Allegro drafts get apaczka_service_id=None, never validated."""
    from zdrovena.api.routers.webhooks import _create_draft

    storage = object()
    order = _load_fixture("shopify_order_inpost_kurier.json")
    _create_draft(order, store, storage)
    drafts = store.list_drafts()
    assert drafts[0]["apaczka_service_id"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_shipping_webhook.py::TestCreateDraftApaczka -v`
Expected: `KeyError: 'apaczka_service_id'` (field doesn't exist on the draft record yet).

- [ ] **Step 3: Implement**

In `_create_draft`, in the `if use_allegro_delivery:` branch, add `apaczka_service_id = None` right after `inpost_service = "paczkomat" if allegro_sending_method == "parcel_locker" else None`:

```python
    if use_allegro_delivery:
        courier = "allegro_delivery"
        # sendingAtPoint tylko dla InPost — rozpoznajemy po nazwie w shipping_lines.title.
        # Paczkomat: 'parcel_locker' (jednoznaczne, bezpieczne).
        # InPost Kurier: brak defaultu — operator musi świadomie ustawić sending_method.
        # Za flagą ALLEGRO_INPOST_KURIER_DEFAULT (dispatch_order|pop|any_point) można
        # włączyć domyślne mapowanie.
        title_lower = title.lower()
        if "paczkomat" in title_lower:
            allegro_sending_method: str | None = "parcel_locker"
        elif "inpost" in title_lower:
            kurier_default = (os.getenv("ALLEGRO_INPOST_KURIER_DEFAULT") or "").strip()
            allegro_sending_method = kurier_default or None
        else:
            allegro_sending_method = None
        inpost_service = "paczkomat" if allegro_sending_method == "parcel_locker" else None
        apaczka_service_id: str | None = None
    else:
        courier = _pick_courier(order)
        inpost_service = _pick_inpost_service(title) if courier == "inpost" else None
        allegro_sending_method = None
        apaczka_service_id = _pick_apaczka_service(title) if courier == "apaczka" else None
```

Update the `status` line inside the `record` dict:

```python
        "status": (
            "needs_review"
            if (
                packages_count > 1
                or phone is None
                or (courier == "apaczka" and apaczka_service_id is None)
            )
            else "pending"
        ),
```

Add the new field to the `record` dict, right after `"service": service,`:

```python
        "service": service,
        "apaczka_service_id": apaczka_service_id,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_shipping_webhook.py::TestCreateDraftApaczka -v`
Expected: all PASS, including the 4 new tests.

- [ ] **Step 5: Run the full webhook test file to check for regressions**

Run: `uv run pytest tests/test_shipping_webhook.py -v`
Expected: all PASS. (The multi-line `f-string`/dict change is localized to `_create_draft`; other courier paths set `apaczka_service_id = None` explicitly in both branches, so no other test's assertions on `status`/other fields should change.)

- [ ] **Step 6: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git commit -m "feat(shipping): set apaczka_service_id from title map at draft creation"
```

---

### Task 4: Read `apaczka_service_id` from the draft, not Key Vault

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py`
- Test: `tests/test_shipping_webhook.py`

There are 3 call sites. Only one (`_run_apaczka`, via `create_shipment`) actually uses `service_id` in its Apaczka API payload — verified by reading `zdrovena/common/apaczka.py`: `cancel_shipment`/`get_label` never reference `self._service_id`. Treat them differently:

- [ ] **Step 1: Write the failing regression test for `_run_apaczka`**

Add to `tests/test_shipping_webhook.py`, inside `class TestRunApaczka:` (after the existing `test_creates_shipment_returns_patch`):

```python
    def test_uses_draft_apaczka_service_id_not_secret(self):
        """P0 regression guard: service_id must come from the draft, never
        from a get_secret('apaczka_service_id') call (that secret no longer
        exists — see docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md)."""
        from zdrovena.api.routers.webhooks import _run_apaczka

        storage_mock = object()
        draft = {
            "id": "d-ap-2",
            "shopify_order_number": "1061",
            "courier": "apaczka",
            "service": "apaczka",
            "apaczka_service_id": "53",
            "receiver": {
                "first_name": "Anna",
                "last_name": "N",
                "email": "a@n.pl",
                "phone": "800300401",
                "locker_id": "",
            },
            "shipping_address": {"street": "Polna 1", "city": "Poznań", "post_code": "60-001"},
        }
        with patch("zdrovena.api.routers.webhooks.get_secret") as mock_get_secret:
            mock_get_secret.return_value = "tok"
            with patch("zdrovena.common.apaczka.ApaczkaClient") as MockClient:
                MockClient.return_value.create_shipment.return_value = {
                    "id": "ap-2",
                    "waybill_number": "WAY002",
                }
                _run_apaczka(draft, _SENDER, storage_mock)

        MockClient.assert_called_once_with("tok", "tok", "53", storage_mock)
        requested_secrets = [c.args[0] for c in mock_get_secret.call_args_list]
        assert "apaczka_service_id" not in requested_secrets
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest "tests/test_shipping_webhook.py::TestRunApaczka::test_uses_draft_apaczka_service_id_not_secret" -v`
Expected: FAIL — `assert "apaczka_service_id" not in requested_secrets` fails because the current code still calls `get_secret("apaczka_service_id")`.

- [ ] **Step 3: Implement — `_run_apaczka`**

Change:
```python
    app_id = get_secret("apaczka_app_id")
    app_secret = get_secret("apaczka_app_secret")
    service_id = get_secret("apaczka_service_id")
    client = ApaczkaClient(app_id, app_secret, service_id, storage)
```
To:
```python
    app_id = get_secret("apaczka_app_id")
    app_secret = get_secret("apaczka_app_secret")
    service_id = draft.get("apaczka_service_id") or ""
    client = ApaczkaClient(app_id, app_secret, service_id, storage)
```
(this is inside `_run_apaczka`, which already receives `draft` as its first parameter)

- [ ] **Step 4: Implement — `get_label`'s Apaczka branch**

In the `get_label` endpoint, change:
```python
        elif courier == "apaczka":
            from zdrovena.common.apaczka import ApaczkaClient

            app_id = get_secret("apaczka_app_id")
            app_secret = get_secret("apaczka_app_secret")
            service_id = get_secret("apaczka_service_id")
            pdf_bytes = ApaczkaClient(app_id, app_secret, service_id, storage).get_label(label_id)
```
To:
```python
        elif courier == "apaczka":
            from zdrovena.common.apaczka import ApaczkaClient

            app_id = get_secret("apaczka_app_id")
            app_secret = get_secret("apaczka_app_secret")
            # get_label() never reads service_id (verified in apaczka.py), but
            # pass the real per-draft value anyway for consistency/future-proofing.
            service_id = draft.get("apaczka_service_id") or ""
            pdf_bytes = ApaczkaClient(app_id, app_secret, service_id, storage).get_label(label_id)
```
(`draft` is already in scope here — it's fetched at the top of `get_label` via `draft = shipping_store.get_draft(draft_id)`)

- [ ] **Step 5: Implement — `cancel_apaczka_order`**

This endpoint only receives `order_id` (the Apaczka-side id), never looks up a draft, and `cancel_shipment()` never uses `service_id`. Change:
```python
    from zdrovena.common.apaczka import ApaczkaClient

    app_id = get_secret("apaczka_app_id")
    app_secret = get_secret("apaczka_app_secret")
    service_id = get_secret("apaczka_service_id")
    client = ApaczkaClient(app_id, app_secret, service_id, storage)
```
To:
```python
    from zdrovena.common.apaczka import ApaczkaClient

    app_id = get_secret("apaczka_app_id")
    app_secret = get_secret("apaczka_app_secret")
    # No draft available here (only order_id) and cancel_shipment() never
    # reads service_id — pass an empty placeholder rather than looking one up.
    client = ApaczkaClient(app_id, app_secret, "", storage)
```

- [ ] **Step 6: Run the new test to verify it passes**

Run: `uv run pytest "tests/test_shipping_webhook.py::TestRunApaczka::test_uses_draft_apaczka_service_id_not_secret" -v`
Expected: PASS.

- [ ] **Step 7: Run the full webhook test file to confirm no regressions**

Run: `uv run pytest tests/test_shipping_webhook.py -v`
Expected: all PASS, including unchanged `TestGetLabelApaczka::test_apaczka_label_returns_pdf` and both `TestCancelApaczkaOrderEndpoint` tests (they mock `get_secret` broadly and don't assert exact service_id, so they're unaffected).

- [ ] **Step 8: Grep-verify no call site was missed**

Run: `grep -n 'get_secret("apaczka_service_id")\|get_secret(.apaczka_service_id.)' zdrovena/`
Expected: no output (zero matches).

- [ ] **Step 9: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git commit -m "fix(shipping): read apaczka_service_id from draft, not Key Vault"
```

---

### Task 5: Extend `PATCH /shipping/drafts/{id}` with `apaczka_service_id`

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py`
- Test: `tests/test_shipping_webhook.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shipping_webhook.py`, inside `class TestUpdateDraft:` (after `test_needs_review_draft_still_blocks_execute`):

```python
def test_sets_apaczka_service_id(self, client, store):
    draft = self._seed_draft(store)
    resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"apaczka_service_id": "21"})
    assert resp.status_code == 200
    assert resp.json()["apaczka_service_id"] == "21"
    updated = store.get_draft(draft["id"])
    assert updated["apaczka_service_id"] == "21"


def test_rejects_unknown_apaczka_service_id(self, client, store):
    draft = self._seed_draft(store)
    resp = client.patch(
        f"/api/shipping/drafts/{draft['id']}", json={"apaczka_service_id": "999999"}
    )
    assert resp.status_code == 400
    assert "apaczka_service_id" in resp.json()["detail"].lower()


def test_apaczka_service_id_does_not_auto_clear_needs_review(self, client, store):
    """Matches existing service/locker_id behavior: setting the field
    alone does not flip status — the operator still confirms separately
    via reviewed=True (see design spec's Backward-compatibility-of-behavior
    note)."""
    draft = self._seed_draft(store)
    store.update_draft(draft["id"], {"status": "needs_review"})
    resp = client.patch(f"/api/shipping/drafts/{draft['id']}", json={"apaczka_service_id": "21"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "needs_review"


def test_apaczka_service_id_and_reviewed_together_clears_needs_review(self, client, store):
    draft = self._seed_draft(store)
    store.update_draft(draft["id"], {"status": "needs_review"})
    resp = client.patch(
        f"/api/shipping/drafts/{draft['id']}",
        json={"apaczka_service_id": "21", "reviewed": True},
    )
    assert resp.status_code == 200
    assert resp.json()["apaczka_service_id"] == "21"
    assert resp.json()["status"] == "pending"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_shipping_webhook.py::TestUpdateDraft -v`
Expected: FAIL — `apaczka_service_id` is not a recognized body param (FastAPI silently ignores unknown JSON keys by default, so `resp.json()["apaczka_service_id"]` raises `KeyError` and the "rejects unknown" test gets 200 instead of 400).

- [ ] **Step 3: Implement**

In `update_draft`, add a new parameter to the function signature (after `locker_id`):

```python
def update_draft(
    draft_id: str,
    shipping_store: ShippingStoreDep,
    principal: Annotated[Principal, Depends(require_shipment_mgr_or_above)],
    packages_count: int | None = Body(None, ge=1, le=99),
    service: str | None = Body(None),
    locker_id: str | None = Body(None),
    apaczka_service_id: str | None = Body(None),
    reviewed: bool | None = Body(None),
) -> dict[str, Any]:
```

Add validation logic after the existing `locker_id` block:

```python
    if locker_id is not None:
        receiver = dict(draft.get("receiver") or {})
        receiver["locker_id"] = locker_id
        patch["receiver"] = receiver
    if apaczka_service_id is not None:
        from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

        if apaczka_service_id not in APACZKA_SERVICE_CATALOG:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown apaczka_service_id: {apaczka_service_id}",
            )
        patch["apaczka_service_id"] = apaczka_service_id
    if reviewed is True and draft.get("status") == "needs_review":
```

(the last line shown is the existing line right after — just confirming placement; do not duplicate it)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_shipping_webhook.py::TestUpdateDraft -v`
Expected: all PASS, including the 4 new tests.

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git commit -m "feat(shipping): validate apaczka_service_id in PATCH /shipping/drafts"
```

---

### Task 6: `GET /shipping/apaczka-services` endpoint

**Files:**
- Modify: `zdrovena/api/routers/webhooks.py`
- Test: `tests/test_shipping_webhook.py`

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_shipping_webhook.py` (anywhere after `class TestUpdateDraft:` ends, e.g. right before `class TestCreateDraft:`):

```python
class TestListApaczkaServices:
    def test_returns_full_catalog(self, client):
        from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

        resp = client.get("/api/shipping/apaczka-services")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["services"]) == len(APACZKA_SERVICE_CATALOG)
        assert {"service_id": "21", "label": "DPD Kurier"} in body["services"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_shipping_webhook.py::TestListApaczkaServices -v`
Expected: FAIL with 404 (route doesn't exist yet).

- [ ] **Step 3: Implement**

Add this endpoint in `zdrovena/api/routers/webhooks.py`, right after the `list_drafts` endpoint (before the `# ── Dead-letter queue` comment):

```python
@router.get(
    "/shipping/apaczka-services",
    summary="List the curated Apaczka courier services available for draft selection",
    responses={403: {"description": "Insufficient role"}},
)
def list_apaczka_services(
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
) -> dict[str, Any]:
    from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG

    return {
        "services": [
            {"service_id": service_id, "label": label}
            for service_id, label in APACZKA_SERVICE_CATALOG.items()
        ]
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_shipping_webhook.py::TestListApaczkaServices -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/webhooks.py tests/test_shipping_webhook.py
git commit -m "feat(shipping): add GET /shipping/apaczka-services endpoint"
```

---

### Task 7: Remove `apaczka-service-id` from the secrets manifest

**Files:**
- Modify: `scripts/secrets_manifest.py`

- [ ] **Step 1: Remove the entry**

In `scripts/secrets_manifest.py`, delete the line:
```python
("apaczka-service-id",)
```
from the `ENV_LOCAL_SECRETS` list (it sits between `"apaczka-app-secret",` and `"apaczka-app-id",`'s sibling — remove only the `apaczka-service-id` line, keep `apaczka-app-id`/`apaczka-app-secret`).

- [ ] **Step 2: Run the secrets_sync test suite to confirm no breakage**

Run: `uv run pytest tests/test_secrets_sync.py -v`
Expected: all PASS (tests import `ENV_LOCAL_SECRETS` dynamically and compare against it — `seen == ENV_LOCAL_SECRETS` — no hardcoded count exists, confirmed by reading the test file).

- [ ] **Step 3: Commit**

```bash
git add scripts/secrets_manifest.py
git commit -m "chore(secrets): remove apaczka-service-id — now per-draft, not a secret"
```

---

### Task 8: Update `TODOS.md`

**Files:**
- Modify: `TODOS.md`

- [ ] **Step 1: Remove the `az keyvault secret set` line for `apaczka-service-id`**

Delete this line from the bash template block (around line 404):
```bash
az keyvault secret set --vault-name $AKV --name apaczka-service-id      --value "<service_id dla domyślnego kuriera; reszta w kodzie>"
```

- [ ] **Step 2: Remove the table row and add an explanatory note**

Delete this row from the "Status sekretów" table (around line 429):
```
| `apaczka-service-id` | ❌ brak | ❌ do dodania | |
```

Immediately after the table (before the next `---` or section end), add:

```markdown
> **Note (2026-07-09):** `apaczka-service-id` was removed from this checklist —
> it's per-draft data now (set from the Shopify shipping-line title via
> `APACZKA_SERVICE_TITLE_MAP`, or manually by an operator), never a global Key
> Vault secret. See `docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md`.
```

- [ ] **Step 3: Commit**

```bash
git add TODOS.md
git commit -m "docs(todos): remove apaczka-service-id — superseded by per-draft field"
```

---

### Task 9: Minimal frontend UI

**Files:**
- Modify: `frontend/src/views/ShippingView.jsx`
- Modify: `frontend/src/lang.js`

No automated frontend test framework exists in this repo (confirmed — no `vitest`/`jest` in `frontend/package.json`) — this task is verified manually via `dev.sh` + browser at the end.

- [ ] **Step 1: Add translation keys**

In `frontend/src/lang.js`, find the Polish locale block's line containing `sh_pickup_done: 'podjazd ✓',` and add after it on the same line (matching the existing single-line-per-locale style):

```js
sh_pickup_done: 'podjazd ✓', sh_apaczka_service_label: 'Serwis Apaczka', sh_apaczka_service_placeholder: '— wybierz serwis —', sh_apaczka_service_save: 'Zapisz', sh_apaczka_service_save_busy: 'Zapisywanie…',
```

Find the English locale block's line containing `sh_pickup_done: 'pickup ✓',` and add after it:

```js
sh_pickup_done: 'pickup ✓', sh_apaczka_service_label: 'Apaczka service', sh_apaczka_service_placeholder: '— select service —', sh_apaczka_service_save: 'Save', sh_apaczka_service_save_busy: 'Saving…',
```

- [ ] **Step 2: Update `courierLabel` to show the resolved service label**

In `frontend/src/views/ShippingView.jsx`, change:

```js
function courierLabel(draft) {
    if (draft.courier === 'allegro_delivery') {
        if (draft.allegro_sending_method === 'parcel_locker') return 'Wysyłam z Allegro (Paczkomat)'
        if (draft.allegro_sending_method === 'dispatch_order') return 'Wysyłam z Allegro (Kurier InPost)'
        return 'Wysyłam z Allegro'
    }
    if (draft.courier === 'inpost') {
        if (draft.service === 'inpost_locker_standard') return 'InPost Paczkomat'
        if (draft.service === 'inpost_courier_standard') return 'InPost Kurier'
        return 'InPost'
    }
    return 'Apaczka'
}
```

To:

```js
function courierLabel(draft, apaczkaServices = []) {
    if (draft.courier === 'allegro_delivery') {
        if (draft.allegro_sending_method === 'parcel_locker') return 'Wysyłam z Allegro (Paczkomat)'
        if (draft.allegro_sending_method === 'dispatch_order') return 'Wysyłam z Allegro (Kurier InPost)'
        return 'Wysyłam z Allegro'
    }
    if (draft.courier === 'inpost') {
        if (draft.service === 'inpost_locker_standard') return 'InPost Paczkomat'
        if (draft.service === 'inpost_courier_standard') return 'InPost Kurier'
        return 'InPost'
    }
    if (draft.apaczka_service_id) {
        const match = apaczkaServices.find(s => s.service_id === draft.apaczka_service_id)
        if (match) return `Apaczka — ${match.label}`
    }
    return 'Apaczka'
}
```

- [ ] **Step 3: Update `DraftRow` — accept new props, add the dropdown**

Change the `DraftRow` function signature:

```js
function DraftRow({ draft, onPrintLabel, onExecute, onPickup, onMarkFulfilled, onConfirmPending, busy, canManage, selected, onToggleSelect, forceOpen }) {
```

To:

```js
function DraftRow({ draft, onPrintLabel, onExecute, onPickup, onMarkFulfilled, onConfirmPending, onSetApaczkaService, apaczkaServices, busy, canManage, selected, onToggleSelect, forceOpen }) {
```

Add new local state right after `const [open, setOpen] = useState(false)`:

```js
    const [selectedApaczkaService, setSelectedApaczkaService] = useState('')
```

Update the `<Pill kind={courierPillKind(draft)}>{courierLabel(draft)}</Pill>` line to:

```js
                <span><Pill kind={courierPillKind(draft)}>{courierLabel(draft, apaczkaServices)}</Pill></span>
```

Insert this new block right after the closing `</div>` of the 3-column detail grid (`Adres dostawy` / `Numer śledzenia` / `Paczki`), before the `{draft.error && (...)}` block:

```jsx
                    {draft.courier === 'apaczka' && (
                        <div style={{ marginTop: 12 }}>
                            <div className="detail-label">{T.sh_apaczka_service_label ?? 'Serwis Apaczka'}</div>
                            {draft.apaczka_service_id ? (
                                <div>{apaczkaServices.find(s => s.service_id === draft.apaczka_service_id)?.label || draft.apaczka_service_id}</div>
                            ) : (
                                <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
                                    <select
                                        value={selectedApaczkaService}
                                        onChange={e => setSelectedApaczkaService(e.target.value)}
                                        disabled={isBusy}
                                    >
                                        <option value="">{T.sh_apaczka_service_placeholder ?? '— wybierz serwis —'}</option>
                                        {apaczkaServices.map(s => (
                                            <option key={s.service_id} value={s.service_id}>{s.label}</option>
                                        ))}
                                    </select>
                                    <button
                                        className="btn btn-secondary"
                                        disabled={isBusy || !selectedApaczkaService}
                                        onClick={() => onSetApaczkaService(draft, selectedApaczkaService)}
                                    >
                                        {isBusy
                                            ? (T.sh_apaczka_service_save_busy ?? 'Zapisywanie…')
                                            : (T.sh_apaczka_service_save ?? 'Zapisz')}
                                    </button>
                                </div>
                            )}
                        </div>
                    )}
```

(`T` is already in scope — `DraftRow` calls `const { t, lang } = useT(); const T = t[lang]` at its own top, lines 182-183, independently of `ShippingView`. No new hook call needed.)

- [ ] **Step 4: Add the fetch-once effect and handler in `ShippingView`**

Add new state right after `const [expandAll, setExpandAll] = useState(null)`:

```js
    const [apaczkaServices, setApaczkaServices] = useState([])
```

Add a new `useEffect` right after the existing data-loading `useEffect` blocks (after the one starting `useEffect(() => {` around line 492, i.e. as a sibling effect, not nested inside it):

```js
    useEffect(() => {
        async function loadApaczkaServices() {
            try {
                const token = await getToken()
                const res = await fetch('/api/shipping/apaczka-services', {
                    headers: { Authorization: `Bearer ${token}` },
                })
                if (res.ok) {
                    const body = await res.json()
                    setApaczkaServices(body.services || [])
                }
            } catch {
                // Non-critical: dropdown stays empty; PATCH still works via
                // curl/Postman with a known service_id if this fetch fails.
            }
        }
        loadApaczkaServices()
    }, [getToken])
```

Add the handler right after `handlePickup` (same file/area as the other `withBusy`-wrapped handlers):

```js
    function handleSetApaczkaService(draft, serviceId) {
        return withBusy(draft.id, async () => {
            const token = await getToken()
            const res = await fetch(`/api/shipping/drafts/${draft.id}`, {
                method: 'PATCH',
                headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
                body: JSON.stringify({ apaczka_service_id: serviceId, reviewed: true }),
            })
            if (!res.ok) {
                const body = await res.json().catch(() => ({}))
                throw new Error(body.detail || `${res.status}`)
            }
        })()
    }
```

- [ ] **Step 5: Pass the new props at the `<DraftRow>` call site**

Change:

```jsx
                    <DraftRow
                        key={draft.id}
                        draft={draft}
                        busy={busy}
                        canManage={canManage}
                        onPrintLabel={handlePrintLabel}
                        onExecute={handleExecute}
                        onPickup={handlePickup}
                        onMarkFulfilled={handleMarkFulfilled}
                        onConfirmPending={handleConfirmPending}
                        selected={selectedDraftIds.has(draft.id)}
                        onToggleSelect={handleToggleSelect}
                        forceOpen={expandAll}
                    />
```

To:

```jsx
                    <DraftRow
                        key={draft.id}
                        draft={draft}
                        busy={busy}
                        canManage={canManage}
                        onPrintLabel={handlePrintLabel}
                        onExecute={handleExecute}
                        onPickup={handlePickup}
                        onMarkFulfilled={handleMarkFulfilled}
                        onConfirmPending={handleConfirmPending}
                        onSetApaczkaService={handleSetApaczkaService}
                        apaczkaServices={apaczkaServices}
                        selected={selectedDraftIds.has(draft.id)}
                        onToggleSelect={handleToggleSelect}
                        forceOpen={expandAll}
                    />
```

- [ ] **Step 6: Manual verification**

Run: `MOCK_COURIER=1 ./dev.sh` (from repo root; `MOCK_COURIER=1` is already the default per `dev.sh`, avoids hitting real courier APIs).

In the browser (frontend dev server URL printed by `dev.sh`):
1. Confirm the app loads with no console errors related to `apaczkaServices`/`courierLabel`.
2. Use `scripts/seed-shipping-drafts.py`'s seeded data (already run by `dev.sh`) or manually create an Apaczka-courier draft with no `apaczka_service_id` (e.g. via the API directly if no seeded example exists) and confirm:
   - The row shows a "Serwis Apaczka" dropdown with the 25 curated options.
   - Selecting one and clicking "Zapisz" persists it (row updates after reload, dropdown replaced by the plain-text label "DPD Kurier" etc., no longer shows "— wybierz serwis —").
   - The header pill now reads "Apaczka — DPD Kurier" instead of bare "Apaczka".
3. Confirm a draft that already has `apaczka_service_id` set shows the label directly, no dropdown.

Record the outcome (pass/fail + screenshot description) in the task-completion report — this step has no automated equivalent.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/views/ShippingView.jsx frontend/src/lang.js
git commit -m "feat(shipping): add minimal Apaczka service-selection UI"
```

---

### Task 10: Final review

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -q`
Expected: all PASS, count higher than the pre-task baseline by the number of new tests added across Tasks 2, 3, 4, 5, 6 (6 + 4 + 1 + 4 + 1 = 16 new tests), zero failures, zero regressions.

- [ ] **Step 2: Lint and type-check**

Run: `uv run ruff check zdrovena/ scripts/ tests/`
Expected: all checks pass.

Run: `uv run pyright zdrovena/api/routers/webhooks.py zdrovena/common/apaczka.py scripts/secrets_manifest.py`
Expected: 0 errors, 0 warnings.

- [ ] **Step 3: Grep-verify the secret is fully gone**

Run: `grep -rn "apaczka.service.id\|apaczka-service-id" zdrovena/ scripts/ TODOS.md`
Expected: zero matches (confirms no stray reference survived across all 9 prior tasks — the catalog constant and doc references are `APACZKA_SERVICE_CATALOG`/`apaczka_service_id` with underscores throughout code, distinct from the removed hyphenated secret name).

- [ ] **Step 4: Confirm design-spec fidelity**

Re-read `docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md` section by section and confirm every requirement has a corresponding completed task above. There is no gap: catalog (Task 1), title-map helper (Task 2), draft field + needs_review (Task 3), 3 call sites (Task 4), PATCH validation (Task 5), GET endpoint (Task 6), manifest (Task 7), TODOS.md (Task 8), frontend (Task 9).
