# Storage Outage Must Not Look Like Missing Data — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An Azure Table Storage failure must raise, not answer "there is no such record".

**Architecture:** One `StorageUnavailableError` in the general exception hierarchy (not the shipping one — the same stores back Damage and the DLQ). Six read methods stop catching bare `Exception`: single-entity reads let `ResourceNotFoundError` through as `None`, list reads treat every exception as an outage. A new FastAPI handler turns it into 503 with the correlation id the envelope already carries.

**Tech Stack:** Python 3.12, FastAPI, `azure-data-tables`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-storage-outage-not-empty-design.md`
**Issue:** #310

---

## File Structure

**Modify:**
- `zdrovena/common/exceptions.py` — `StorageUnavailableError` + a factory that also emits the telemetry event
- `zdrovena/common/shipping_store.py` — four read paths, plus the local-JSON case
- `zdrovena/common/damage_store.py` — two read paths
- `zdrovena/api/errors.py` — the 503 handler
- `docs/audit/production-readiness.md`, `CHANGELOG.md`

**Test:**
- `tests/test_shipping_store.py`, `tests/test_damage_store.py`, `tests/test_api_errors.py`, and one endpoint test file per surface

**Language convention:** docstrings, comments and log messages in English; the operator-facing `message_pl` in Polish.

---

## Task 1: The exception and its 503

**Files:**
- Modify: `zdrovena/common/exceptions.py`, `zdrovena/api/errors.py`
- Test: `tests/test_api_errors.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_errors.py`. That file already builds a `FastAPI` app with
`install_exception_handlers` and a `TestClient` — reuse that idiom; read the existing
unhandled-error test for the exact shape.

```python
def test_storage_outage_is_503_with_a_correlation_id(self):
    """An outage is not the caller's fault and is not a 500 either — 503 is
    what tells the operator (and any alerting) the system is unavailable."""
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom")
    def _boom():
        raise storage_unavailable("shipping", "list_drafts", RuntimeError("timeout"))

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 503
    body = response.json()
    assert body["error_code"] == "StorageUnavailableError"
    assert body["correlation_id"]


def test_the_raw_azure_text_never_reaches_the_operator(self):
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/boom")
    def _boom():
        raise storage_unavailable(
            "shipping", "list_drafts", RuntimeError("ServerBusy: subscription xyz throttled")
        )

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert "ServerBusy" not in response.text
    assert "xyz" not in response.text
```

Add `storage_unavailable` and `StorageUnavailableError` to the file's import from
`zdrovena.common.exceptions`. Match the surrounding tests' structure — if they are module-level
functions rather than methods on a class, drop `self`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api_errors.py -k storage -v`
Expected: FAIL — `ImportError: cannot import name 'storage_unavailable'`

- [ ] **Step 3: Write the exception**

In `zdrovena/common/exceptions.py`, add to the hierarchy docstring at the top:

```
    ├── StorageUnavailableError — Azure Table Storage unreachable (NOT "no data")
```

and the class plus factory:

```python
class StorageUnavailableError(ZdrovenaError):
    """Azure Table Storage could not answer — which is not the same as "no data".

    Read paths used to catch bare ``Exception`` and return ``None`` / ``[]``, so a
    timeout looked exactly like a record that is genuinely absent. That made an
    outage read as "the draft does not exist" and let a fingerprint lookup answer
    "no existing case", inviting a duplicate write (issue #310).
    """

    def __init__(self, store: str, operation: str, cause: BaseException) -> None:
        super().__init__(f"{store} storage unavailable during {operation}: {cause!r}")
        self.store = store
        self.operation = operation
        self.cause = cause


def storage_unavailable(
    store: str, operation: str, cause: BaseException
) -> StorageUnavailableError:
    """Build the error to raise, emitting the event alerting can key on.

    Telemetry lives here rather than at each of the six call sites so an outage
    always produces the same signal. Issue #214 wants operational alerts; an
    empty-list metric would be full of false positives, this is not.
    """
    log_event(
        "storage_unavailable",
        level=logging.ERROR,
        store=store,
        operation=operation,
        error_type=type(cause).__name__,
    )
    return StorageUnavailableError(store=store, operation=operation, cause=cause)
```

with the imports:

```python
import logging

from zdrovena.common.events import log_event
```

`events.py` imports only `correlation`, so there is no import cycle — verify with
`.venv/bin/python -c "import zdrovena.common.exceptions"` before moving on.

This does put a telemetry call in the exceptions module, which is a mild layering smell. The
alternatives are worse: a new module for twelve lines, or the same `log_event` block duplicated at
six call sites where one of them will eventually be forgotten.

- [ ] **Step 4: Write the 503 handler**

In `zdrovena/api/errors.py`, add a handler inside `install_exception_handlers`, **before** the
bare-`Exception` handler (FastAPI dispatches on the most specific registered type, but keeping the
order readable matters more than relying on that):

```python
    @app.exception_handler(StorageUnavailableError)
    async def _storage_unavailable_handler(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: StorageUnavailableError
    ) -> JSONResponse:
        # The raw Azure text goes to the log, never to the operator.
        logger.exception(
            "Storage unavailable (%s.%s) on %s", exc.store, exc.operation, request.url.path
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_envelope(
                error_code="StorageUnavailableError",
                message_pl=(
                    "Magazyn danych jest chwilowo niedostępny — to nie znaczy, że danych nie ma. "
                    "Spróbuj ponownie za chwilę i nie ponawiaj operacji zapisu."
                ),
            ),
        )
```

Import `StorageUnavailableError` from `zdrovena.common.exceptions`.

The second sentence of the message is load-bearing: the whole point of the issue is that an
operator seeing "no data" may act on an incomplete picture.

- [ ] **Step 5: Run tests to verify they pass**

```
.venv/bin/python -m pytest tests/test_api_errors.py -v
.venv/bin/python -m pytest tests/ -q
```
Both must pass. The suite currently sits at **2078 passed, 2 skipped**.

- [ ] **Step 6: Commit**

```bash
git add zdrovena/common/exceptions.py zdrovena/api/errors.py tests/test_api_errors.py
git commit -m "feat: StorageUnavailableError i odpowiedź 503 zamiast fałszywego braku danych"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 2: ShippingStore stops swallowing

**Files:**
- Modify: `zdrovena/common/shipping_store.py` (`get_draft` ~362, `list_dlq` ~547, `get_dlq_entry` ~564, `list_drafts` ~590)
- Test: `tests/test_shipping_store.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_shipping_store.py` has a `table_store` fixture returning `(store, fake)` where `fake`
is a `_FakeTableClient` patched in via `monkeypatch.setattr(store, "_table_client", lambda: fake)`
(~line 407). Reuse it, and add a client that raises.

```python
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)

from zdrovena.common.exceptions import StorageUnavailableError

_OUTAGES = [
    ServiceRequestError("timeout"),
    HttpResponseError("429 ServerBusy"),
    ClientAuthenticationError("token expired"),
]


class _BrokenTableClient:
    """Every call fails the way Azure fails: not with ResourceNotFoundError."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_entity(self, *args, **kwargs):
        raise self._exc

    def query_entities(self, *args, **kwargs):
        raise self._exc


def _broken_store(monkeypatch, exc: Exception) -> ShippingStore:
    store = ShippingStore(account_url="https://fake.blob.core.windows.net")
    broken = _BrokenTableClient(exc)
    monkeypatch.setattr(store, "_table_client", lambda: broken)
    monkeypatch.setattr(store, "_dlq_table_client", lambda: broken)
    return store


class TestOutageIsNotEmptiness:
    @pytest.mark.parametrize("exc", _OUTAGES, ids=lambda e: type(e).__name__)
    @pytest.mark.parametrize(
        "call",
        [
            lambda s: s.get_draft("d1"),
            lambda s: s.list_drafts(),
            lambda s: s.get_dlq_entry("e1"),
            lambda s: s.list_dlq(),
        ],
        ids=["get_draft", "list_drafts", "get_dlq_entry", "list_dlq"],
    )
    def test_an_outage_raises_instead_of_answering_absent(self, monkeypatch, exc, call):
        store = _broken_store(monkeypatch, exc)

        with pytest.raises(StorageUnavailableError):
            call(store)

    def test_a_genuinely_missing_row_still_returns_none(self, monkeypatch):
        # The contract for real absence is unchanged — this is what stops the
        # fix from turning every read into an error.
        store = _broken_store(monkeypatch, ResourceNotFoundError("no such row"))

        assert store.get_draft("d1") is None
        assert store.get_dlq_entry("e1") is None

    def test_an_empty_partition_still_returns_an_empty_list(self, table_store):
        store, _fake = table_store

        assert store.list_drafts() == []
        assert store.list_dlq() == []
```

Confirm the exception class names against the installed SDK before relying on them:
`.venv/bin/python -c "from azure.core.exceptions import ServiceRequestError, HttpResponseError, ClientAuthenticationError, ResourceNotFoundError; print('ok')"`.
If `_dlq_table_client` is not a separate method, drop that `monkeypatch.setattr` line.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_shipping_store.py -k OutageIsNotEmptiness -v`
Expected: FAIL — the outage cases return `None` / `[]` instead of raising.

- [ ] **Step 3: Write the implementation**

Add the imports to `zdrovena/common/shipping_store.py`:

```python
from azure.core.exceptions import ResourceNotFoundError

from zdrovena.common.exceptions import storage_unavailable
```

**Verified:** this module imports Azure lazily inside the methods that need it
(`from azure.data.tables import TableServiceClient` at line 102, `from azure.core import
MatchConditions` at line 288) so the module stays importable without the SDK. Follow that —
put `from azure.core.exceptions import ResourceNotFoundError` inside each method, not at module
top. Only `storage_unavailable` goes in the top-level imports.

**`get_draft`** (~362) — single-entity read:

```python
try:
    entity = self._table_client().get_entity(partition_key=PARTITION_KEY, row_key=draft_id)
    return _deserialize(dict(entity))
except ResourceNotFoundError:
    return None
except Exception as exc:
    # An outage is not an absence. Returning None here made a timeout
    # look like "this draft does not exist" (issue #310).
    raise storage_unavailable("shipping", "get_draft", exc) from exc
```

**`get_dlq_entry`** (~564) — the same shape, with `"get_dlq_entry"`.

**`list_drafts`** (~590) — a list read has no not-found case, so every exception is an outage:

```python
            except Exception as exc:
                raise storage_unavailable("shipping", "list_drafts", exc) from exc
```

Drop the `logger.warning` that preceded the old `return []`: `storage_unavailable` already emits a
structured event, and the handler logs the traceback.

**`list_dlq`** (~547) — the same, with `"list_dlq"`.

- [ ] **Step 4: Run tests to verify they pass**

```
.venv/bin/python -m pytest tests/test_shipping_store.py -v
.venv/bin/python -m pytest tests/ -q
```

**Expect fallout.** Callers that relied on the silent `[]` will now propagate. Read each failure
before changing it: a poller or scheduled job that should survive an outage may legitimately need
its own `try/except StorageUnavailableError` (the InPost and Apaczka pollers already wrap
`list_drafts` in `try/except Exception` and count an error — those should keep working unchanged).
Do **not** restore a bare `except Exception: return []` in the store to make a caller pass.

- [ ] **Step 5: Lint and types**

```
.venv/bin/python -m ruff check zdrovena/ tests/
.venv/bin/python -m ruff format --check .
/home/pepus/.local/bin/pyright
```
All must pass. pyright is at `/home/pepus/.local/bin/pyright`; its config excludes `tests/`.

- [ ] **Step 6: Commit**

```bash
git add zdrovena/common/shipping_store.py tests/test_shipping_store.py
git commit -m "fix: awaria storage w ShippingStore nie udaje już braku danych"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 3: DamageStore, and the duplicate it was creating

This is the task with the concrete harm behind it.

**Files:**
- Modify: `zdrovena/common/damage_store.py` (`get_case` ~181, `list_cases` ~196)
- Test: `tests/test_damage_store.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestOutageIsNotEmptiness:
    @pytest.mark.parametrize("exc", _OUTAGES, ids=lambda e: type(e).__name__)
    def test_get_case_raises_instead_of_answering_absent(self, monkeypatch, exc):
        store = _broken_damage_store(monkeypatch, exc)

        with pytest.raises(StorageUnavailableError):
            store.get_case("c1")

    @pytest.mark.parametrize("exc", _OUTAGES, ids=lambda e: type(e).__name__)
    def test_list_cases_raises_instead_of_answering_empty(self, monkeypatch, exc):
        store = _broken_damage_store(monkeypatch, exc)

        with pytest.raises(StorageUnavailableError):
            store.list_cases()

    def test_a_genuinely_missing_case_still_returns_none(self, monkeypatch):
        store = _broken_damage_store(monkeypatch, ResourceNotFoundError("no such row"))

        assert store.get_case("c1") is None

    def test_the_fingerprint_lookup_cannot_invite_a_duplicate(self, monkeypatch):
        """find_case_by_fingerprint iterates list_cases. While that returned []
        during an outage, the lookup answered "no existing case" and the caller
        opened a second case for an event already recorded."""
        store = _broken_damage_store(monkeypatch, HttpResponseError("429 ServerBusy"))

        with pytest.raises(StorageUnavailableError):
            store.find_case_by_fingerprint("fp-1")
```

Build `_OUTAGES` and `_broken_damage_store` the same way as in Task 2 — check the existing fixture
idiom in `tests/test_damage_store.py` first and follow it, including the name of the client method
the store patches.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_damage_store.py -k OutageIsNotEmptiness -v`
Expected: FAIL — all four return `None` / `[]`.

- [ ] **Step 3: Write the implementation**

`get_case` (~181):

```python
            try:
                entity = self._table_client().get_entity(CASES_PARTITION, case_id)
            except ResourceNotFoundError:
                return None
            except Exception as exc:
                raise storage_unavailable("damage", "get_case", exc) from exc
            return _deserialize(dict(entity))
```

`list_cases` (~196):

```python
            except Exception as exc:
                raise storage_unavailable("damage", "list_cases", exc) from exc
```

`find_case_by_fingerprint` needs **no change** — it iterates `list_cases()`, so once that raises,
the error propagates. The test above is what pins that down.

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_damage_store.py tests/test_damage_detection.py tests/test_damage_endpoints.py -v
.venv/bin/python -m pytest tests/ -q
```
All must pass. Watch the damage-detection scan: it runs on a schedule and may need to survive an
outage — if it does, wrap the call there, not in the store.

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/damage_store.py tests/test_damage_store.py
git commit -m "fix: awaria storage nie każe już DamageStore tworzyć duplikatu sprawy"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 4: The local JSON backend

Spec section 5. Two lines, same lie, development-only.

**Files:**
- Modify: `zdrovena/common/shipping_store.py` (`_local_load_unlocked` ~147)
- Test: `tests/test_shipping_store.py`

- [ ] **Step 1: Write the failing test**

```python
class TestLocalStoreCorruption:
    def test_a_missing_local_file_is_still_an_empty_store(self, tmp_path):
        store = ShippingStore(local_root=tmp_path)

        assert store.list_drafts() == []

    def test_a_corrupt_local_file_is_not_silently_empty(self, tmp_path):
        # "No drafts" and "this file is mangled" are different facts, and a
        # developer chasing the first when it is really the second loses an
        # afternoon the same way an operator would.
        store = ShippingStore(local_root=tmp_path)
        store.upsert_draft(_draft("d1"))
        store._local_file.write_text("{not json", encoding="utf-8")

        with pytest.raises(StorageUnavailableError):
            store.list_drafts()
```

Check the constructor keyword — the class takes `local_root` per its `__init__`; confirm before
using it, and confirm `_local_file` is the attribute name.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_shipping_store.py -k LocalStoreCorruption -v`
Expected: FAIL — the corrupt case returns `[]`.

- [ ] **Step 3: Write the implementation**

```python
    def _local_load_unlocked(self) -> dict[str, Any]:
        if not self._local_file.exists():
            return {}
        try:
            return json.loads(self._local_file.read_text(encoding="utf-8"))
        except Exception as exc:
            # A missing file is an empty store; a mangled one is not. Returning
            # {} here is the local mirror of the outage-looks-empty bug (#310).
            raise storage_unavailable("shipping-local", "_local_load", exc) from exc
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`

Expected: all pass. If a test deliberately writes a partial file and expects `{}`, read it before
changing it — it may be encoding the old behaviour, or it may be testing something else entirely.

- [ ] **Step 5: Commit**

```bash
git add zdrovena/common/shipping_store.py tests/test_shipping_store.py
git commit -m "fix: uszkodzony lokalny magazyn nie udaje pustego"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 5: The three endpoints answer 503 consistently

The acceptance criterion asks for consistency across Shipping, Damage and the DLQ.

**Files:**
- Test: `tests/test_shipping_webhook.py`, `tests/test_damage_endpoints.py`

- [ ] **Step 1: Write the failing tests**

For the shipping and DLQ surfaces, in `tests/test_shipping_webhook.py` (it already has `client`
and `store` fixtures):

```python
class TestStorageOutageSurfacesAs503:
    @pytest.mark.parametrize(
        "path, method_name",
        [
            ("/api/shipping/drafts", "list_drafts"),
            ("/api/shipping/drafts/dlq", "list_dlq"),
        ],
    )
    def test_an_outage_is_503_not_an_empty_list(self, client, store, path, method_name):
        from zdrovena.common.exceptions import storage_unavailable

        def _boom(*args, **kwargs):
            raise storage_unavailable("shipping", method_name, RuntimeError("timeout"))

        with patch.object(type(store), method_name, _boom):
            resp = client.get(path)

        assert resp.status_code == 503
        body = resp.json()
        assert body["error_code"] == "StorageUnavailableError"
        assert body["correlation_id"]

    def test_an_outage_on_a_single_draft_is_503_not_404(self, client, store):
        from zdrovena.common.exceptions import storage_unavailable

        def _boom(*args, **kwargs):
            raise storage_unavailable("shipping", "get_draft", RuntimeError("timeout"))

        with patch.object(type(store), "get_draft", _boom):
            resp = client.get("/api/shipping/drafts/whatever/label")

        assert resp.status_code == 503
```

Confirm the DLQ route path against `zdrovena/api/routers/webhooks.py` (`/shipping/drafts/dlq`
around line 534) and the single-draft route you pick — any endpoint whose first action is
`get_draft` works; the point is that it answers 503 rather than the 404 it would have produced.

Add the equivalent for the damage surface in `tests/test_damage_endpoints.py`, patching
`list_cases`.

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/test_shipping_webhook.py tests/test_damage_endpoints.py -k 503 -v`

They may pass immediately — the handler from Task 1 does the work. That is fine: these tests exist
to pin the behaviour at the API boundary, which is where the acceptance criterion is written. If
any of them returns 404 or 200-with-empty-list, that endpoint is catching the exception somewhere
between the store and the handler — find that `except` and remove it.

- [ ] **Step 3: Run the full suite**

```
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check zdrovena/ tests/
.venv/bin/python -m ruff format --check .
/home/pepus/.local/bin/pyright
```

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: awaria storage daje 503 z correlation ID na wszystkich trzech powierzchniach"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 6: Documentation, gate and PR

**Files:**
- Modify: `docs/audit/production-readiness.md`, `CHANGELOG.md`

- [ ] **Step 1: Changelog**

Under `## Unreleased` → `### Fixed`:

```markdown
- **storage**: Awaria Azure Table Storage nie wygląda już jak brak danych. Sześć ścieżek odczytu
  w `ShippingStore` i `DamageStore` łapało gołe `Exception` i odpowiadało `None` albo pustą listą,
  więc timeout, throttling czy wygasłe poświadczenia były nieodróżnialne od rekordu, którego
  naprawdę nie ma. Najgroźniejszy skutek: `find_case_by_fingerprint` iteruje po `list_cases`, więc
  przy awarii odpowiadał „nie ma takiej sprawy" i wywołujący tworzył duplikat sprawy uszkodzenia.
  Teraz brak encji nadal daje `None`/404, a każda inna awaria to `StorageUnavailableError` → HTTP
  503 z correlation ID i zdarzenie `storage_unavailable` dla alertów. (#310)
```

- [ ] **Step 2: Note the contract in the audit doc**

Add a short section to `docs/audit/production-readiness.md` stating the rule, so the next store
written in this repo follows it: single-entity reads catch `ResourceNotFoundError` only; list
reads treat every exception as an outage; `shopify_dedup_store.py` was the pattern.

- [ ] **Step 3: Full gate**

```
npm --prefix frontend ci
CHECK_DOCS_FASTPATH=0 bash scripts/check.sh
```
Expected: `All checks passed — safe to push.`

If coverage drops below the threshold, do **not** lower `--cov-fail-under` or add
`# pragma: no cover` — report the shortfall and wait for the owner's decision, per `CLAUDE.md`.

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin fix/storage-outage-not-empty-310
```

Open against `develop`, `Closes #310` in the body. Note in the body that #214 (operational alerts)
can now key on the `storage_unavailable` event, which is what that issue was waiting for.

---

## Verification against the spec

| Spec section | Tasks |
| --- | --- |
| 1. One infrastructure exception | 1 |
| 2. Six read paths stop swallowing | 2, 3 |
| 3. A distinct 503 | 1, 5 |
| 4. A telemetry signal | 1 |
| 5. The local JSON backend | 4 |
| Testing | 1, 2, 3, 4, 5 |
