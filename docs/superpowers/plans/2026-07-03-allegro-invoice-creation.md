# Allegro Invoice Creation (own pipeline) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For every Allegro order that requests a VAT invoice, create one correct Fakturownia invoice (kaucja/deposit included from the start via `settlement_positions`) and push it to Allegro as the order's attached invoice — replacing reliance on Fakturownia's Allegro app-store integration, which does not compute kaucja and which the business does not control.

**Architecture:** Mirrors the existing Shopify invoice flow (`humio-checkout-b2b/invoice_creation_Flow.js`): build the invoice payload once, at order-detection time, with the deposit already included — no after-the-fact patching. A new pure mapper (`allegro_invoice_mapper.py`) turns an Allegro checkout-form order into a Fakturownia invoice payload, using Allegro's native per-line `deposit.price.amount` field (no title-string heuristics needed, unlike the Shopify version). A new orchestrator (`allegro_invoicer.py`) calls Fakturownia to create the invoice, downloads the PDF, and pushes it to Allegro via the already-written (but currently unused) `AllegroClient.create_invoice_declaration()` / `upload_invoice_file()`. It is wired into the existing `allegro_poller.py` loop, right after a shipping draft is successfully created for that order.

**Explicitly out of scope for this plan** (tracked separately, discussed earlier in this session):
- Actually scheduling `poll_orders_once()` to run periodically (Container App Job / cron). This plan only makes invoice creation *possible* when the poller runs; wiring the poller itself onto a schedule is separate infra work.
- Disabling Fakturownia's Allegro app-store integration in the Fakturownia account settings — that is a manual step in the Fakturownia dashboard (Ustawienia > Integracje) that the business owner must do; this code assumes it has already been done. If it hasn't, Allegro orders will end up with **two** invoices (the app-store one and this pipeline's one) until it is.
- Historical/legacy Allegro orders that already went through the old app-store flow before the switch. This plan only fixes orders going forward.

**Tech Stack:** Python 3.10, FastAPI, `requests` (via existing `FakturowniaClient`/`AllegroClient` patterns), pytest, `unittest.mock`.

**Logging & alerting requirement (per business owner — this is a money-facing feature, treat failures as first-class):**
- Every failure path logs at `ERROR` level with full context (Allegro order id, the step that failed, the exception) via the module logger. Because `APPLICATIONINSIGHTS_CONNECTION_STRING` is already wired in `zdrovena/api/main.py` via `configure_azure_monitor()`, every `logger.error(...)` call is automatically shipped to Azure Application Insights in production — no extra plumbing needed for that part.
- On top of that, a failure sends an immediate SMS via the existing `zdrovena/common/sms_service.py` mechanism (same one used for "new order arrived" notifications) — logs alone are easy to miss; a missing/wrong invoice is a compliance problem, not just an operational one.
- Alerts must not spam: once an order's invoice creation has failed, it is not silently retried forever — see idempotency design in Task 5.

---

## File Structure

| File | Responsibility |
|---|---|
| `zdrovena/common/fakturownia.py` (modify) | Add `create_invoice()` and `get_invoice_pdf()` to `FakturowniaClient`. Add raw (non-JSON) response support to the low-level request helper. |
| `zdrovena/common/allegro_invoice_mapper.py` (new) | Pure function: Allegro checkout-form order dict → Fakturownia invoice payload dict. No I/O, no side effects — easy to unit test exhaustively. |
| `zdrovena/common/sms_service.py` (modify) | Add `send_invoice_failure_sms()` alongside the existing `send_new_order_sms()`. |
| `zdrovena/api/routers/allegro_invoicer.py` (new) | Orchestrator: calls the mapper, calls Fakturownia to create + fetch PDF, calls Allegro to declare + upload, handles idempotency, logs, alerts. |
| `zdrovena/api/routers/allegro_poller.py` (modify) | Call the invoicer right after a draft is successfully created for a new order. |
| `tests/test_fakturownia_client.py` (modify) | Tests for the two new client methods. |
| `tests/test_allegro_invoice_mapper.py` (new) | Tests for the pure mapper — company vs. private buyer, deposit present/absent, invoice not requested, multi-line orders. |
| `tests/test_allegro_invoicer.py` (new) | Tests for the orchestrator — success path, idempotency, each failure mode, alerting behavior. |
| `tests/test_allegro_poller.py` (modify) | Tests that the poller calls the invoicer after a successful draft, and does not call it after a failed one. |

---

### Task 1: `FakturowniaClient.create_invoice()`

**Files:**
- Modify: `zdrovena/common/fakturownia.py`
- Test: `tests/test_fakturownia_client.py`

Fakturownia's create-invoice endpoint is `POST /invoices.json` with body `{"api_token": ..., "invoice": {...}}` — the exact same wrapper shape `update_invoice()` already uses for `PUT`, just without an `{id}` in the path. Confirmed against the official API reference (`github.com/fakturownia/api`):

```
curl https://YOUR_DOMAIN.fakturownia.pl/invoices.json \
-H 'Accept: application/json' -H 'Content-Type: application/json' \
-d '{"api_token": "API_TOKEN", "invoice": {"kind":"vat", "positions":[...]}}'
```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fakturownia_client.py`, after the `TestUpdateInvoice` class:

```python
# ── create_invoice ───────────────────────────────────────────────────────────


class TestCreateInvoice:
    def test_create_invoice_posts_wrapped_payload(self, client):
        payload = {
            "kind": "vat",
            "buyer_name": "Anna Nowak",
            "positions": [{"name": "HUMIO 6 PET", "tax": 8, "total_price_gross": 73.00, "quantity": 1}],
        }
        with patch(
            "requests.Session.request", return_value=_resp({"id": 777, **payload})
        ) as mock:
            out = client.create_invoice(payload)
            _, kwargs = mock.call_args
            assert kwargs["method"] == "POST"
            assert kwargs["url"].endswith("/invoices.json")
            body = kwargs["json"]
            assert body["api_token"] == "test-token-abc"
            assert body["invoice"] == payload
            assert out["id"] == 777

    def test_create_invoice_422_raises_business_error(self, client):
        err = {"code": "error", "message": {"buyer_name": ["can't be blank"]}}
        with patch("requests.Session.request", return_value=_resp(err, status=422)):
            with pytest.raises(FakturowniaBusinessError):
                client.create_invoice({"kind": "vat", "positions": []})

    def test_create_invoice_401_raises_auth_error(self, client):
        with patch(
            "requests.Session.request", return_value=_resp({"code": "unauthorized"}, status=401)
        ):
            with pytest.raises(FakturowniaAuthError):
                client.create_invoice({"kind": "vat", "positions": []})

    def test_create_invoice_500_raises_server_error(self, client):
        with patch("requests.Session.request", return_value=_resp({}, status=500)):
            with pytest.raises(FakturowniaServerError):
                client.create_invoice({"kind": "vat", "positions": []})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fakturownia_client.py::TestCreateInvoice -v`
Expected: FAIL with `AttributeError: 'FakturowniaClient' object has no attribute 'create_invoice'`

- [ ] **Step 3: Implement `create_invoice`**

In `zdrovena/common/fakturownia.py`, add after `update_invoice`:

```python
    def create_invoice(self, invoice: dict[str, Any]) -> dict[str, Any]:
        """Create a new Fakturownia document (VAT invoice, nota księgowa, etc.).

        `invoice["kind"]` selects the document type (e.g. "vat", "accounting_note").
        Returns the created document, including its `id`.
        """
        body = {"api_token": self.api_token, "invoice": invoice}
        return self._request("POST", "/invoices.json", json=body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fakturownia_client.py::TestCreateInvoice -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/fakturownia.py tests/test_fakturownia_client.py
git commit -m "feat(fakturownia): add create_invoice() to FakturowniaClient"
```

---

### Task 2: `FakturowniaClient.get_invoice_pdf()` (raw/binary response support)

**Files:**
- Modify: `zdrovena/common/fakturownia.py`
- Test: `tests/test_fakturownia_client.py`

The PDF endpoint is `GET /invoices/{id}.pdf?api_token=...` and returns raw PDF bytes, not JSON. The existing `_request`/`_parse_response` always calls `resp.json()` on success — for a PDF body this raises `ValueError` internally and the `except ValueError: return None` branch would silently swallow it, returning `None` instead of the PDF bytes. This needs an explicit `raw=True` path, not a workaround.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fakturownia_client.py`, after `TestCreateInvoice`:

```python
# ── get_invoice_pdf ──────────────────────────────────────────────────────────


class TestGetInvoicePdf:
    def test_get_invoice_pdf_returns_raw_bytes(self, client):
        r = _resp(status=200)
        r.content = b"%PDF-1.4 fake pdf bytes"
        with patch("requests.Session.request", return_value=r) as mock:
            out = client.get_invoice_pdf(777)
            assert out == b"%PDF-1.4 fake pdf bytes"
            _, kwargs = mock.call_args
            assert kwargs["method"] == "GET"
            assert "/invoices/777.pdf" in kwargs["url"]
            assert kwargs["params"]["api_token"] == "test-token-abc"

    def test_get_invoice_pdf_404_raises_business_error(self, client):
        with patch("requests.Session.request", return_value=_resp({}, status=404)):
            with pytest.raises(FakturowniaBusinessError):
                client.get_invoice_pdf(999999)

    def test_get_invoice_pdf_500_raises_server_error(self, client):
        with patch("requests.Session.request", return_value=_resp({}, status=500)):
            with pytest.raises(FakturowniaServerError):
                client.get_invoice_pdf(777)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fakturownia_client.py::TestGetInvoicePdf -v`
Expected: FAIL with `AttributeError: 'FakturowniaClient' object has no attribute 'get_invoice_pdf'`

- [ ] **Step 3: Add raw-response support to `_request`/`_parse_response`, then `get_invoice_pdf`**

In `zdrovena/common/fakturownia.py`, replace the `_request` and `_parse_response` methods:

```python
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        # api_token in query for GET; both places acceptable per Fakturownia docs.
        merged_params = {"api_token": self.api_token, **(params or {})}
        try:
            resp = self._session.request(
                method=method,
                url=url,
                params=merged_params,
                json=json,
                timeout=self.timeout,
            )
        except requests.Timeout as e:
            raise CourierTimeoutError(courier="fakturownia", action=method.lower()) from e
        except requests.ConnectionError as e:
            raise CourierConnectionError(courier="fakturownia", detail=str(e)) from e

        return self._parse_response(resp, method=method, path=path, raw=raw)

    @staticmethod
    def _parse_response(
        resp: requests.Response, *, method: str, path: str, raw: bool = False
    ) -> Any:
        status = resp.status_code
        if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
            if raw:
                return resp.content
            if status == HTTPStatus.NO_CONTENT:
                return None
            try:
                return resp.json()
            except ValueError:
                return None
        # error mapping
        try:
            body = resp.json()
        except ValueError:
            body = {"text": (resp.text or "")[:200]}
        detail = f"{method} {path} → {status}: {body}"
        if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise FakturowniaAuthError(detail=detail)
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise FakturowniaServerError(status=status)
        # everything else (400, 404, 422, ...) is business error
        raise FakturowniaBusinessError(detail=detail, action=f"{method.lower()} {path}")
```

Then add `get_invoice_pdf`, after `get_invoice`:

```python
    def get_invoice_pdf(self, invoice_id: int) -> bytes:
        """Download the invoice PDF. Returns raw PDF bytes."""
        return self._request("GET", f"/invoices/{invoice_id}.pdf", raw=True)
```

- [ ] **Step 4: Run the full client test file to verify nothing broke and new tests pass**

Run: `uv run pytest tests/test_fakturownia_client.py -v`
Expected: all PASS, including the 3 new `TestGetInvoicePdf` tests

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/fakturownia.py tests/test_fakturownia_client.py
git commit -m "feat(fakturownia): add get_invoice_pdf() with raw-response support"
```

---

### Task 3: `allegro_invoice_mapper.py` — pure mapping function

**Files:**
- Create: `zdrovena/common/allegro_invoice_mapper.py`
- Test: `tests/test_allegro_invoice_mapper.py`

This maps one Allegro checkout-form order (the same raw shape `allegro_poller.py` already receives from `AllegroClient.list_orders()`) into a Fakturownia `create_invoice()` payload. Confirmed field paths against `tests/fixtures/allegro/checkout-form-detail.json` (real, sanitized production data) and the Allegro API docs:

- `order["invoice"]["required"]` (bool) — if `False`, the buyer did not request a VAT invoice (receipt/paragon only). No invoice should be created — this is a normal case, not an error.
- `order["invoice"]["address"]["company"]["name"]` / `["taxId"]` — present only for company buyers.
- `order["buyer"]["firstName"]` / `["lastName"]` / `["email"]` — private buyer fallback.
- `order["lineItems"][i]["offer"]["name"]`, `["quantity"]`, `["price"]["amount"]` (post-discount actual price, NOT `originalPrice`), `["tax"]["rate"]` (e.g. `"8.00"` meaning 8%).
- `order["lineItems"][i]["deposit"]["price"]["amount"]` — kaucja for that line, when present. **Assumption to verify against a real multi-quantity deposit order during manual testing (Task 6): this plan treats it as the total deposit for the whole line (matching how `price.amount` is also a line total, not a per-unit price), not a per-unit amount.** If a real order shows otherwise, adjust the multiplication in this mapper.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_allegro_invoice_mapper.py`:

```python
"""Tests for zdrovena.common.allegro_invoice_mapper.

Pure function: Allegro checkout-form order → Fakturownia create_invoice() payload.
No I/O — every case is a plain dict-in, dict-out (or None) assertion.
"""

from __future__ import annotations

from zdrovena.common.allegro_invoice_mapper import allegro_order_to_fakturownia_invoice


def _order(**overrides) -> dict:
    base = {
        "id": "af1",
        "buyer": {
            "email": "buyer@allegromail.pl",
            "firstName": "Anna",
            "lastName": "Nowak",
        },
        "invoice": {"required": True, "address": None},
        "lineItems": [
            {
                "offer": {"name": "HUMIO - Alkaliczna Woda Humusowa 500ml x 12"},
                "quantity": 2,
                "price": {"amount": "73.00", "currency": "PLN"},
                "tax": {"rate": "8.00"},
                "deposit": {"price": {"amount": "6.00"}},
            }
        ],
    }
    base.update(overrides)
    return base


class TestInvoiceNotRequired:
    def test_returns_none_when_invoice_not_required(self):
        order = _order(invoice={"required": False, "address": None})
        assert allegro_order_to_fakturownia_invoice(order) is None

    def test_returns_none_when_invoice_key_missing(self):
        order = _order()
        del order["invoice"]
        assert allegro_order_to_fakturownia_invoice(order) is None


class TestPrivateBuyer:
    def test_maps_buyer_name_and_email(self):
        order = _order()
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["buyer_first_name"] == "Anna"
        assert invoice["buyer_last_name"] == "Nowak"
        assert invoice["buyer_email"] == "buyer@allegromail.pl"
        assert invoice["buyer_company"] == "0"

    def test_oid_is_allegro_order_id(self):
        order = _order()
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["oid"] == "af1"
        assert invoice["oid_unique"] == "yes"

    def test_kind_is_vat(self):
        assert allegro_order_to_fakturownia_invoice(_order())["kind"] == "vat"


class TestCompanyBuyer:
    def test_maps_company_name_and_tax_no(self):
        order = _order(
            invoice={
                "required": True,
                "address": {
                    "company": {"name": "Nazwa Firmy Sp. z o.o.", "taxId": "5252674798"}
                },
            }
        )
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["buyer_name"] == "Nazwa Firmy Sp. z o.o."
        assert invoice["buyer_tax_no"] == "5252674798"
        assert invoice["buyer_company"] == "1"
        assert "buyer_first_name" not in invoice


class TestPositionsAndDeposit:
    def test_position_uses_actual_price_not_original(self):
        order = _order()
        invoice = allegro_order_to_fakturownia_invoice(order)
        pos = invoice["positions"][0]
        assert pos["name"] == "HUMIO - Alkaliczna Woda Humusowa 500ml x 12"
        assert pos["quantity"] == 2
        assert pos["total_price_gross"] == 73.00
        assert pos["tax"] == 8

    def test_deposit_becomes_settlement_position_charge(self):
        order = _order()
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["settlement_positions"] == [
            {"kind": "charge", "amount": "6.00", "description": "Kaucja za opakowania zwrotne"}
        ]

    def test_no_settlement_positions_key_when_no_deposit(self):
        order = _order()
        order["lineItems"][0].pop("deposit")
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert "settlement_positions" not in invoice

    def test_multiple_lines_deposits_summed(self):
        order = _order()
        order["lineItems"].append(
            {
                "offer": {"name": "HUMIO 500ml x 6"},
                "quantity": 1,
                "price": {"amount": "40.00", "currency": "PLN"},
                "tax": {"rate": "8.00"},
                "deposit": {"price": {"amount": "3.00"}},
            }
        )
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert len(invoice["positions"]) == 2
        assert invoice["settlement_positions"] == [
            {"kind": "charge", "amount": "9.00", "description": "Kaucja za opakowania zwrotne"}
        ]

    def test_tax_rate_converted_to_integer_percent(self):
        order = _order()
        order["lineItems"][0]["tax"]["rate"] = "23.00"
        invoice = allegro_order_to_fakturownia_invoice(order)
        assert invoice["positions"][0]["tax"] == 23
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_allegro_invoice_mapper.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'zdrovena.common.allegro_invoice_mapper'`

- [ ] **Step 3: Implement the mapper**

Create `zdrovena/common/allegro_invoice_mapper.py`:

```python
"""zdrovena.common.allegro_invoice_mapper — Allegro order → Fakturownia invoice.

Maps one Allegro checkout-form order (the shape returned by
AllegroClient.list_orders() / used by allegro_poller.py) into the payload
expected by FakturowniaClient.create_invoice().

Kaucja (deposit) is read directly from Allegro's native per-line
`deposit.price.amount` field and folded into `settlement_positions` on the
SAME invoice at creation time — unlike the Shopify flow (which detects
kaucja via a "kaucja" substring in the line item title, because Shopify has
no native deposit concept), Allegro models deposits structurally, so no
heuristic matching is needed here.

Returns None when the buyer did not request a VAT invoice at all
(`invoice.required` is False or missing) — Allegro lets buyers opt for a
receipt/paragon instead, which is a normal case, not an error.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

_KAUCJA_DESCRIPTION = "Kaucja za opakowania zwrotne"


def allegro_order_to_fakturownia_invoice(order: dict[str, Any]) -> dict[str, Any] | None:
    invoice_request = order.get("invoice") or {}
    if not invoice_request.get("required"):
        return None

    buyer = order.get("buyer") or {}
    address = invoice_request.get("address") or {}
    company = address.get("company") or {}
    is_company = bool(company.get("name"))

    positions: list[dict[str, Any]] = []
    deposit_total = Decimal("0")

    for item in order.get("lineItems") or []:
        offer = item.get("offer") or {}
        quantity = int(item.get("quantity", 1) or 1)
        price = Decimal(str((item.get("price") or {}).get("amount", "0")))
        tax_rate = Decimal(str((item.get("tax") or {}).get("rate", "23")))
        positions.append(
            {
                "name": offer.get("name", ""),
                "quantity": quantity,
                "total_price_gross": float(price),
                "tax": int(tax_rate),
            }
        )
        deposit = item.get("deposit")
        if deposit:
            deposit_total += Decimal(str((deposit.get("price") or {}).get("amount", "0")))

    invoice: dict[str, Any] = {
        "kind": "vat",
        "oid": str(order.get("id") or ""),
        "oid_unique": "yes",
        "positions": positions,
    }

    if is_company:
        invoice["buyer_name"] = company.get("name", "")
        invoice["buyer_company"] = "1"
        tax_no = company.get("taxId")
        if tax_no:
            invoice["buyer_tax_no"] = tax_no
    else:
        invoice["buyer_first_name"] = buyer.get("firstName", "")
        invoice["buyer_last_name"] = buyer.get("lastName", "")
        invoice["buyer_company"] = "0"

    invoice["buyer_email"] = buyer.get("email", "")

    if deposit_total > 0:
        invoice["settlement_positions"] = [
            {
                "kind": "charge",
                "amount": f"{deposit_total:.2f}",
                "description": _KAUCJA_DESCRIPTION,
            }
        ]

    return invoice
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_allegro_invoice_mapper.py -v`
Expected: all PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/allegro_invoice_mapper.py tests/test_allegro_invoice_mapper.py
git commit -m "feat(allegro): add order-to-Fakturownia-invoice mapper"
```

---

### Task 4: SMS alerting for invoice failures

**Files:**
- Modify: `zdrovena/common/sms_service.py`
- Test: `tests/test_sms_service.py` (check if it exists first — if not, create it)

- [ ] **Step 0: Check for an existing test file**

Run: `ls tests/test_sms_service.py 2>&1`

If it exists, read it first and add the new test class to it in Step 1 using the same conventions. If not, Step 1 creates it fresh as shown below.

- [ ] **Step 1: Write the failing test**

Create (or append to) `tests/test_sms_service.py`:

```python
"""Tests for zdrovena.common.sms_service."""

from __future__ import annotations

from unittest.mock import patch

from zdrovena.common.sms_service import send_invoice_failure_sms


class TestSendInvoiceFailureSms:
    def test_sends_sms_with_order_and_reason(self):
        with patch("httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            send_invoice_failure_sms(
                notify_phone="+48601000000",
                allegro_order_id="af1",
                reason="Fakturownia 500",
                token="tok",
            )
        _, kwargs = mock_post.call_args
        assert "af1" in kwargs["data"]["message"]
        assert "Fakturownia 500" in kwargs["data"]["message"]

    def test_empty_phone_is_noop(self):
        with patch("httpx.post") as mock_post:
            send_invoice_failure_sms(
                notify_phone="", allegro_order_id="af1", reason="x", token="tok"
            )
        mock_post.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sms_service.py::TestSendInvoiceFailureSms -v`
Expected: FAIL with `ImportError: cannot import name 'send_invoice_failure_sms'`

- [ ] **Step 3: Implement `send_invoice_failure_sms`**

In `zdrovena/common/sms_service.py`, add after `send_new_order_sms`:

```python
def send_invoice_failure_sms(
    notify_phone: str,
    allegro_order_id: str,
    reason: str,
    token: str,
) -> None:
    """Alert the operator that Allegro invoice creation/push failed and needs
    manual attention — a missing/wrong invoice is a compliance issue, not
    just an operational one, so this fires immediately rather than waiting
    for someone to notice it in logs.
    """
    msg = f"BLAD faktury Allegro #{allegro_order_id}: {reason[:100]}. Sprawdz recznie w Fakturowni."
    _send(notify_phone, msg, token)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_sms_service.py::TestSendInvoiceFailureSms -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/sms_service.py tests/test_sms_service.py
git commit -m "feat(sms): add invoice-failure alert SMS"
```

---

### Task 5: `allegro_invoicer.py` — orchestrator with idempotency, logging, alerting

**Files:**
- Create: `zdrovena/api/routers/allegro_invoicer.py`
- Test: `tests/test_allegro_invoicer.py`

This is the piece that ties everything together. Idempotency: before creating anything, check `fakturownia_client.list_invoices(oid=order_id)` — if a non-empty result already exists, skip (Fakturownia is the source of truth, no separate local state to keep in sync, matching the existing pattern in `fakturownia_patcher.py`'s idempotency check). On any failure, log at `ERROR` and send exactly one SMS alert — no retry loop here; if `poll_orders_once()` runs again for an order whose invoice already errored, `list_invoices(oid=...)` will still find nothing (since it never got created), so **it would retry and re-alert every cycle**. To prevent alert spam, the caller (`allegro_poller.py`, Task 6) must not call this a second time for an order that already has an active (non-error) shipping draft — which is exactly the existing `_existing_active_allegro_draft` check already used for draft creation. Document this coupling clearly in the docstring so nobody "fixes" it into an infinite-retry loop later.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_allegro_invoicer.py`:

```python
"""Tests for zdrovena.api.routers.allegro_invoicer.create_invoice_for_order.

Flow: map order -> Fakturownia invoice, skip if invoice not required or
already exists (oid lookup), else create + fetch PDF + push to Allegro.
On any failure: log ERROR and send exactly one SMS alert.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zdrovena.api.routers.allegro_invoicer import create_invoice_for_order


def _order(**overrides) -> dict:
    base = {
        "id": "af1",
        "buyer": {"email": "b@example.com", "firstName": "Anna", "lastName": "Nowak"},
        "invoice": {"required": True, "address": None},
        "lineItems": [
            {
                "offer": {"name": "HUMIO 6 PET"},
                "quantity": 1,
                "price": {"amount": "73.00", "currency": "PLN"},
                "tax": {"rate": "8.00"},
                "deposit": {"price": {"amount": "6.00"}},
            }
        ],
    }
    base.update(overrides)
    return base


class TestNotRequired:
    def test_skips_when_invoice_not_required(self):
        fakturownia = MagicMock()
        allegro = MagicMock()
        order = _order(invoice={"required": False, "address": None})
        result = create_invoice_for_order(
            order, fakturownia_client=fakturownia, allegro_client=allegro
        )
        assert result["status"] == "not_required"
        fakturownia.create_invoice.assert_not_called()
        allegro.create_invoice_declaration.assert_not_called()


class TestIdempotency:
    def test_skips_when_invoice_already_exists_for_order(self):
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = [{"id": 1, "oid": "af1"}]
        allegro = MagicMock()
        result = create_invoice_for_order(
            _order(), fakturownia_client=fakturownia, allegro_client=allegro
        )
        assert result["status"] == "already_exists"
        fakturownia.create_invoice.assert_not_called()
        fakturownia.list_invoices.assert_called_once_with(oid="af1")


class TestSuccessPath:
    def test_creates_invoice_fetches_pdf_and_pushes_to_allegro(self):
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.return_value = {"id": "alg-inv-1"}

        result = create_invoice_for_order(
            _order(), fakturownia_client=fakturownia, allegro_client=allegro
        )

        assert result["status"] == "created"
        assert result["fakturownia_invoice_id"] == 999
        fakturownia.get_invoice_pdf.assert_called_once_with(999)
        allegro.create_invoice_declaration.assert_called_once_with(
            order_id="af1", invoice_number="FV/2026/999"
        )
        allegro.upload_invoice_file.assert_called_once_with(
            order_id="af1", invoice_id="alg-inv-1", pdf_bytes=b"%PDF-1.4 fake"
        )


class TestFailureAlerts:
    def test_fakturownia_create_failure_logs_and_alerts(self, monkeypatch):
        monkeypatch.setenv("SMSAPI_TOKEN_FOR_TEST", "unused")
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.side_effect = RuntimeError("Fakturownia 500")
        allegro = MagicMock()

        with patch(
            "zdrovena.api.routers.allegro_invoicer._alert_invoice_failure"
        ) as mock_alert:
            result = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "error"
        assert "Fakturownia 500" in result["error"]
        mock_alert.assert_called_once()
        assert mock_alert.call_args.kwargs["allegro_order_id"] == "af1"
        allegro.create_invoice_declaration.assert_not_called()

    def test_allegro_push_failure_logs_and_alerts_but_invoice_already_created(self):
        """If Fakturownia succeeded but the Allegro push fails, the invoice
        still exists in Fakturownia (oid-based idempotency will find it next
        time) — we must not lose that fact, just alert that the push failed.
        """
        fakturownia = MagicMock()
        fakturownia.list_invoices.return_value = []
        fakturownia.create_invoice.return_value = {"id": 999, "number": "FV/2026/999"}
        fakturownia.get_invoice_pdf.return_value = b"%PDF-1.4 fake"
        allegro = MagicMock()
        allegro.create_invoice_declaration.side_effect = RuntimeError("Allegro 502")

        with patch(
            "zdrovena.api.routers.allegro_invoicer._alert_invoice_failure"
        ) as mock_alert:
            result = create_invoice_for_order(
                _order(), fakturownia_client=fakturownia, allegro_client=allegro
            )

        assert result["status"] == "error"
        assert result["fakturownia_invoice_id"] == 999
        mock_alert.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_allegro_invoicer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'zdrovena.api.routers.allegro_invoicer'`

- [ ] **Step 3: Implement the orchestrator**

Create `zdrovena/api/routers/allegro_invoicer.py`:

```python
"""zdrovena.api.routers.allegro_invoicer — create + push a Fakturownia
invoice for an Allegro order.

Replaces reliance on Fakturownia's Allegro app-store integration, which
does not compute kaucja (deposit) and which this business does not control.
Instead of patching an already-wrong invoice after the fact, this creates
the invoice correctly the first time (kaucja baked in via
settlement_positions from allegro_invoice_mapper) and pushes it to Allegro
via the order-invoices API.

Idempotency: before creating anything, checks Fakturownia for an existing
invoice with oid=<allegro_order_id> (Fakturownia is the source of truth —
no separate local state to keep in sync).

IMPORTANT — do not turn this into a retry loop: if invoice creation fails,
this does NOT mark anything as "already attempted" locally. The caller
(allegro_poller.py) only invokes this once per order, at the same point it
creates the shipping draft, and relies on the existing
_existing_active_allegro_draft() check to avoid re-processing an order that
already has an active draft. If you call this function from a new call site
without an equivalent guard, a persistently-failing order will alert on
every call.

Logging & alerting: every failure logs at ERROR (auto-forwarded to Azure
Application Insights via the OpenTelemetry wiring in api/main.py) AND sends
exactly one SMS via sms_service — a missing/wrong invoice is a compliance
problem, not just an operational one, so it must not silently disappear
into logs.
"""

from __future__ import annotations

import logging
from typing import Any

from zdrovena.common.allegro_invoice_mapper import allegro_order_to_fakturownia_invoice
from zdrovena.common.secrets import get_secret

logger = logging.getLogger("zdrovena.api.routers.allegro_invoicer")


def _alert_invoice_failure(*, allegro_order_id: str, reason: str) -> None:
    token = get_secret("smsapi_token", required=False)
    notify_phone = get_secret("notify_phone", required=False)
    if not token or not notify_phone:
        return
    try:
        from zdrovena.common.sms_service import send_invoice_failure_sms

        send_invoice_failure_sms(
            notify_phone=notify_phone,
            allegro_order_id=allegro_order_id,
            reason=reason,
            token=token,
        )
    except Exception:
        # Resilience boundary: alerting must never raise into the caller —
        # the ERROR log above already captured the real failure.
        logger.exception("Invoice-failure SMS alert itself failed for order %s", allegro_order_id)


def create_invoice_for_order(
    order: dict[str, Any],
    *,
    fakturownia_client: Any,
    allegro_client: Any,
) -> dict[str, Any]:
    """Create a Fakturownia invoice for one Allegro order and push it back.

    Returns a dict with at least a "status" key:
      "not_required"   — buyer did not request a VAT invoice, nothing to do
      "already_exists" — Fakturownia already has an invoice for this order
      "created"        — success; also has fakturownia_invoice_id/number
      "error"          — also has "error" (str); fakturownia_invoice_id is
                          present if Fakturownia succeeded but the Allegro
                          push failed
    """
    allegro_order_id = str(order.get("id") or "")

    payload = allegro_order_to_fakturownia_invoice(order)
    if payload is None:
        return {"status": "not_required"}

    try:
        existing = fakturownia_client.list_invoices(oid=allegro_order_id)
    except Exception as exc:
        logger.exception(
            "Fakturownia list_invoices lookup failed for Allegro order %s", allegro_order_id
        )
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc)}

    if existing:
        logger.info(
            "Fakturownia already has an invoice for Allegro order %s — skipping", allegro_order_id
        )
        return {"status": "already_exists"}

    try:
        created = fakturownia_client.create_invoice(payload)
        fakturownia_invoice_id = created["id"]
        fakturownia_invoice_number = created["number"]
    except Exception as exc:
        logger.exception("Fakturownia create_invoice failed for Allegro order %s", allegro_order_id)
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {"status": "error", "error": str(exc)}

    try:
        pdf_bytes = fakturownia_client.get_invoice_pdf(fakturownia_invoice_id)
        declaration = allegro_client.create_invoice_declaration(
            order_id=allegro_order_id, invoice_number=fakturownia_invoice_number
        )
        allegro_client.upload_invoice_file(
            order_id=allegro_order_id,
            invoice_id=declaration["id"],
            pdf_bytes=pdf_bytes,
        )
    except Exception as exc:
        logger.exception(
            "Pushing Fakturownia invoice %s to Allegro failed for order %s",
            fakturownia_invoice_id,
            allegro_order_id,
        )
        _alert_invoice_failure(allegro_order_id=allegro_order_id, reason=str(exc))
        return {
            "status": "error",
            "error": str(exc),
            "fakturownia_invoice_id": fakturownia_invoice_id,
        }

    logger.info(
        "Created and pushed Fakturownia invoice %s (%s) for Allegro order %s",
        fakturownia_invoice_id,
        fakturownia_invoice_number,
        allegro_order_id,
    )
    return {
        "status": "created",
        "fakturownia_invoice_id": fakturownia_invoice_id,
        "fakturownia_invoice_number": fakturownia_invoice_number,
    }
```

- [ ] **Step 4: Run tests and lint to verify everything passes**

Run: `uv run pytest tests/test_allegro_invoicer.py -v`
Expected: all PASS (6 tests)

Run: `uv run ruff check zdrovena/api/routers/allegro_invoicer.py`
Expected: no errors (remove the unused `import os` if flagged, per the note above)

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/allegro_invoicer.py tests/test_allegro_invoicer.py
git commit -m "feat(allegro): add invoice creation orchestrator with alerting"
```

---

### Task 6: Wire into `allegro_poller.py`

**Files:**
- Modify: `zdrovena/api/routers/allegro_poller.py`
- Test: `tests/test_allegro_poller.py`

Call `create_invoice_for_order` right after a draft is successfully created for a new order — not before (an order that fails draft creation shouldn't get invoiced), and not for orders skipped as duplicates (they already went through this on a prior cycle).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_allegro_poller.py`, after the existing `TestPollOrdersOnce` tests:

```python
class TestInvoiceCreationWiring:
    def test_calls_invoicer_after_successful_draft_creation(self, monkeypatch):
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []
        fakturownia = MagicMock()

        with patch(
            "zdrovena.api.routers.allegro_poller.create_invoice_for_order",
            return_value={"status": "created", "fakturownia_invoice_id": 1},
        ) as mock_invoicer:
            poll_orders_once(
                client=client,
                shipping_store=store,
                storage=MagicMock(),
                fakturownia_client=fakturownia,
            )

        mock_invoicer.assert_called_once()
        called_order = mock_invoicer.call_args.args[0]
        assert called_order["id"] == "af1"
        assert mock_invoicer.call_args.kwargs["fakturownia_client"] is fakturownia
        assert mock_invoicer.call_args.kwargs["allegro_client"] is client

    def test_does_not_call_invoicer_when_draft_creation_fails(self, monkeypatch):
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []
        store.upsert_draft.side_effect = RuntimeError("store down")
        fakturownia = MagicMock()

        with patch(
            "zdrovena.api.routers.allegro_poller.create_invoice_for_order"
        ) as mock_invoicer:
            poll_orders_once(
                client=client,
                shipping_store=store,
                storage=MagicMock(),
                fakturownia_client=fakturownia,
            )

        mock_invoicer.assert_not_called()

    def test_does_not_call_invoicer_for_skipped_duplicate(self, monkeypatch):
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = [
            {"source": "allegro", "external_order_id": "af1", "status": "created"}
        ]
        fakturownia = MagicMock()

        with patch(
            "zdrovena.api.routers.allegro_poller.create_invoice_for_order"
        ) as mock_invoicer:
            poll_orders_once(
                client=client,
                shipping_store=store,
                storage=MagicMock(),
                fakturownia_client=fakturownia,
            )

        mock_invoicer.assert_not_called()

    def test_invoicer_failure_does_not_abort_cycle(self, monkeypatch):
        """One order's invoice failing must not block the next order's draft."""
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1"), _form("af2")]
        store = MagicMock()
        store.list_drafts.return_value = []
        fakturownia = MagicMock()

        with patch(
            "zdrovena.api.routers.allegro_poller.create_invoice_for_order",
            side_effect=RuntimeError("boom"),
        ):
            stats = poll_orders_once(
                client=client,
                shipping_store=store,
                storage=MagicMock(),
                fakturownia_client=fakturownia,
            )

        assert stats["created"] == 2
        assert stats["invoice_errors"] == 2

    def test_missing_fakturownia_client_skips_invoicing_gracefully(self, monkeypatch):
        """fakturownia_client is optional — callers not ready to wire it up
        yet (or environments without Fakturownia credentials) must not crash.
        """
        monkeypatch.delenv("ALLEGRO_MARK_ON_DRAFT", raising=False)
        client = MagicMock()
        client.list_orders.return_value = [_form("af1")]
        store = MagicMock()
        store.list_drafts.return_value = []

        with patch(
            "zdrovena.api.routers.allegro_poller.create_invoice_for_order"
        ) as mock_invoicer:
            stats = poll_orders_once(client=client, shipping_store=store, storage=MagicMock())

        mock_invoicer.assert_not_called()
        assert stats["created"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_allegro_poller.py::TestInvoiceCreationWiring -v`
Expected: FAIL — `poll_orders_once() got an unexpected keyword argument 'fakturownia_client'`

- [ ] **Step 3: Wire the invoicer into `poll_orders_once`**

In `zdrovena/api/routers/allegro_poller.py`, add the import at the top:

```python
from zdrovena.api.routers.allegro_invoicer import create_invoice_for_order
```

Update the `_new_stats`-equivalent stats dict and the function signature + loop body. The current function looks like this (from the existing file):

```python
def poll_orders_once(
    *,
    client: Any,
    shipping_store: Any,
    storage: Any,
    status: str = "READY_FOR_PROCESSING",
) -> dict[str, int]:
    """One polling cycle. Returns per-cycle stats."""
    stats = {
        "fetched": 0,
        "created": 0,
        "skipped_duplicate": 0,
        "errors": 0,
    }
```

Change the signature to add `fakturownia_client: Any = None` and the stats dict to add invoice counters:

```python
def poll_orders_once(
    *,
    client: Any,
    shipping_store: Any,
    storage: Any,
    fakturownia_client: Any = None,
    status: str = "READY_FOR_PROCESSING",
) -> dict[str, int]:
    """One polling cycle. Returns per-cycle stats.

    fakturownia_client is optional: pass it to also create + push a Fakturownia
    invoice for each newly-created draft (see allegro_invoicer.py). Omit it
    (or pass None) to skip invoicing entirely — e.g. in environments without
    Fakturownia credentials configured.
    """
    stats = {
        "fetched": 0,
        "created": 0,
        "skipped_duplicate": 0,
        "errors": 0,
        "invoices_created": 0,
        "invoice_errors": 0,
    }
```

Then in the loop body, right after the existing:

```python
        try:
            shopify_like = allegro_to_shopify_order(form)
            _create_draft(shopify_like, shipping_store, storage, source="allegro")
        except Exception:
            # Resilience boundary: one malformed/failing order must not abort the
            # rest of the cycle. logger.exception captures the traceback (TRY400).
            logger.exception("Failed to create draft for Allegro order %s", allegro_id)
            stats["errors"] += 1
            continue

        stats["created"] += 1
```

add the invoicer call right after `stats["created"] += 1`, still inside the loop, before the existing `ALLEGRO_MARK_ON_DRAFT` block:

```python
        if fakturownia_client is not None:
            try:
                invoice_result = create_invoice_for_order(
                    form, fakturownia_client=fakturownia_client, allegro_client=client
                )
                if invoice_result["status"] == "created":
                    stats["invoices_created"] += 1
            except Exception:
                # Resilience boundary: an invoicing failure must not block the
                # next order's draft — create_invoice_for_order already logs
                # and alerts internally, this only guards against a bug in
                # the orchestrator itself raising instead of returning "error".
                logger.exception(
                    "create_invoice_for_order raised for Allegro order %s", allegro_id
                )
                stats["invoice_errors"] += 1
```

- [ ] **Step 4: Run the full poller test file to verify everything passes**

Run: `uv run pytest tests/test_allegro_poller.py -v`
Expected: all PASS, including the 5 new `TestInvoiceCreationWiring` tests and all pre-existing tests (they don't pass `fakturownia_client`, which now defaults to `None` — must still pass unchanged)

- [ ] **Step 5: Commit**

```bash
git add zdrovena/api/routers/allegro_poller.py tests/test_allegro_poller.py
git commit -m "feat(allegro): wire invoice creation into the order poller"
```

---

### Task 7: Full suite + lint + format check

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: all pass, no regressions (compare pass count against the baseline before this plan — should be baseline + ~29 new tests: 4 + 3 + 12 + 2 + 6 + 5 = 32, but 3 of the "already fail" verification runs don't count as new passing tests, so expect roughly baseline + 29)

- [ ] **Step 2: Run ruff check and format check on all changed files**

```bash
uv run ruff check zdrovena/common/fakturownia.py zdrovena/common/allegro_invoice_mapper.py zdrovena/common/sms_service.py zdrovena/api/routers/allegro_invoicer.py zdrovena/api/routers/allegro_poller.py tests/test_fakturownia_client.py tests/test_allegro_invoice_mapper.py tests/test_sms_service.py tests/test_allegro_invoicer.py tests/test_allegro_poller.py
uv run ruff format --check zdrovena/common/fakturownia.py zdrovena/common/allegro_invoice_mapper.py zdrovena/common/sms_service.py zdrovena/api/routers/allegro_invoicer.py zdrovena/api/routers/allegro_poller.py tests/test_fakturownia_client.py tests/test_allegro_invoice_mapper.py tests/test_sms_service.py tests/test_allegro_invoicer.py tests/test_allegro_poller.py
```

Expected: no errors from either command. If `ruff format --check` reports files needing formatting, run `uv run ruff format <files>` and re-check.

- [ ] **Step 3: Commit any formatting fixes**

```bash
git add -u
git commit -m "style: ruff format allegro invoice creation feature"
```

(Skip this step if Step 2 reported no formatting changes needed.)

---

### Task 8: Manual verification runbook (before relying on this in production)

**Files:** none — this is a manual, human-run task, not code. Do this before wiring `poll_orders_once` onto a schedule (separate follow-up work, out of scope here).

This feature touches real customer invoices. Do not skip this step even though the automated tests pass — the tests mock Fakturownia and Allegro entirely; they prove the code does what you told it to do, not that Fakturownia and Allegro actually behave the way this plan assumed.

- [ ] **Step 1: Confirm the Fakturownia↔Allegro app-store integration is actually disabled**

In the Fakturownia dashboard: Ustawienia > Integracje. If it's still on, stop here — running this pipeline alongside it will create duplicate invoices.

- [ ] **Step 2: Verify the `deposit.price.amount` assumption from Task 3 against one real order**

Pick one real (or sandbox) Allegro order that has a multi-quantity line item with a deposit — e.g. 2× a product with a per-unit kaucja. Run:

```bash
uv run python3 -c "
from zdrovena.common.allegro import AllegroClient, SecretsAllegroTokenStore
from zdrovena.common.secrets import get_secret
client = AllegroClient(
    client_id=get_secret('allegro-client-id'),
    client_secret=get_secret('allegro-client-secret'),
    refresh_token=get_secret('allegro-refresh-token'),
    env='prod',
    token_store=SecretsAllegroTokenStore(),
)
import json
order = client.get_order('<REAL_ORDER_ID>')
print(json.dumps(order.get('lineItems'), indent=2, ensure_ascii=False))
"
```

Check whether `deposit.price.amount` is the TOTAL for that line (quantity × per-unit deposit) or a PER-UNIT amount. If it's per-unit, fix the mapper in `allegro_invoice_mapper.py`: change `deposit_total += Decimal(...)` to `deposit_total += Decimal(...) * quantity`, and add a test case in `tests/test_allegro_invoice_mapper.py` pinning the corrected behavior before re-running the suite.

- [ ] **Step 2: Run one real end-to-end invoice creation manually**

```bash
uv run python3 -c "
from zdrovena.common.fakturownia import FakturowniaClient
from zdrovena.common.allegro import AllegroClient, SecretsAllegroTokenStore
from zdrovena.common.secrets import get_secret
from zdrovena.api.routers.allegro_invoicer import create_invoice_for_order

fakturownia = FakturowniaClient.from_env()
allegro = AllegroClient(
    client_id=get_secret('allegro-client-id'),
    client_secret=get_secret('allegro-client-secret'),
    refresh_token=get_secret('allegro-refresh-token'),
    env='prod',
    token_store=SecretsAllegroTokenStore(),
)
order = allegro.get_order('<REAL_ORDER_ID>')
result = create_invoice_for_order(order, fakturownia_client=fakturownia, allegro_client=allegro)
print(result)
"
```

- [ ] **Step 3: Verify in both systems**

- Fakturownia dashboard: the new invoice exists, `settlement_positions` shows the kaucja row with the correct amount, `oid` matches the Allegro order id.
- Allegro seller panel, on that order's page: the invoice PDF is attached and downloadable, and matches the Fakturownia one.

- [ ] **Step 4: Verify the alert path fires**

Temporarily break something reversible (e.g. pass a fake `allegro_order_id` that the real `AllegroClient.create_invoice_declaration` will reject with a 404, forcing the Allegro-push failure branch) and confirm the SMS alert actually arrives on the configured `notify_phone`. Do this once, deliberately, rather than discovering during a real failure that the alert path itself is broken.
