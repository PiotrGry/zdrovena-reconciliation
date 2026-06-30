"""Tests for zdrovena.common.shipping_store.ShippingStore (local backend).

Sections marked **TDD-red** describe target behaviour that requires fixes in
the production module. They are intentionally left failing so the next agent
implementing production code knows what to satisfy.

Audit reference: zdrovena_test_audit.md §7.4 — concurrency, idempotency and
silent-failure tests are missing.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from zdrovena.common.shipping_store import ShippingStore


def _draft(draft_id: str, **extra) -> dict:
    base = {
        "id": draft_id,
        "created_at": "2026-06-30T10:00:00+00:00",
        "source": "shopify",
        "shopify_order_id": f"order-{draft_id}",
        "shopify_order_number": "1000",
        "customer_name": "Test",
        "courier": "inpost",
        "service": "inpost_courier_standard",
        "tracking_number": None,
        "courier_draft_id": None,
        "status": "pending",
        "packages_count": 1,
        "pickup_ordered": False,
        "receiver": {
            "first_name": "T",
            "last_name": "T",
            "email": "",
            "phone": "",
            "locker_id": "",
        },
        "shipping_address": {"street": "X", "city": "Y", "post_code": "Z"},
        "parcel": {"template": "small", "weight_kg": None},
        "error": None,
    }
    base.update(extra)
    return base


@pytest.fixture()
def store(tmp_path) -> ShippingStore:
    return ShippingStore(local_root=tmp_path / "store")


# ── Basic CRUD on local backend ───────────────────────────────────────────────


class TestUpsertAndGet:
    def test_upsert_then_get_roundtrip(self, store):
        d = _draft("abc-1")
        store.upsert_draft(d)
        loaded = store.get_draft("abc-1")
        assert loaded == d

    def test_upsert_is_overwrite_for_same_id(self, store):
        store.upsert_draft(_draft("abc-2", status="pending"))
        store.upsert_draft(_draft("abc-2", status="created", tracking_number="TRK"))
        loaded = store.get_draft("abc-2")
        assert loaded["status"] == "created"
        assert loaded["tracking_number"] == "TRK"

    def test_get_unknown_returns_none(self, store):
        assert store.get_draft("does-not-exist") is None

    def test_list_drafts_sorted_by_created_at_desc(self, store):
        store.upsert_draft(_draft("old", created_at="2026-01-01T00:00:00+00:00"))
        store.upsert_draft(_draft("mid", created_at="2026-02-01T00:00:00+00:00"))
        store.upsert_draft(_draft("new", created_at="2026-03-01T00:00:00+00:00"))
        ids = [r["id"] for r in store.list_drafts()]
        assert ids == ["new", "mid", "old"]

    def test_list_drafts_respects_limit(self, store):
        for i in range(5):
            store.upsert_draft(_draft(f"d-{i}", created_at=f"2026-06-{i + 1:02d}T00:00:00+00:00"))
        result = store.list_drafts(limit=3)
        assert len(result) == 3
        # Newest first
        assert result[0]["id"] == "d-4"


class TestUpdateDraft:
    def test_update_merges_fields(self, store):
        store.upsert_draft(_draft("u-1"))
        ok = store.update_draft("u-1", {"status": "created", "tracking_number": "T"})
        assert ok is True
        loaded = store.get_draft("u-1")
        assert loaded["status"] == "created"
        assert loaded["tracking_number"] == "T"
        # Unrelated fields preserved
        assert loaded["customer_name"] == "Test"

    def test_update_returns_false_for_missing(self, store):
        assert store.update_draft("missing", {"status": "x"}) is False

    def test_update_does_not_corrupt_nested(self, store):
        store.upsert_draft(_draft("u-2"))
        store.update_draft("u-2", {"receiver": {"locker_id": "WAW01A"}})
        loaded = store.get_draft("u-2")
        # Whole receiver dict is replaced (current behaviour) — pin it
        assert loaded["receiver"] == {"locker_id": "WAW01A"}


class TestDeleteDraft:
    def test_delete_existing(self, store):
        store.upsert_draft(_draft("del-1"))
        store.delete_draft("del-1")
        assert store.get_draft("del-1") is None

    def test_delete_missing_is_noop(self, store):
        store.delete_draft("never-existed")  # must not raise


# ── On-disk format ────────────────────────────────────────────────────────────


class TestOnDiskFormat:
    def test_file_is_valid_utf8_json(self, tmp_path):
        store = ShippingStore(local_root=tmp_path / "store")
        store.upsert_draft(_draft("file-1", customer_name="Łukasz Żółć"))
        path = tmp_path / "store" / "shipping-drafts.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "file-1" in data
        # Non-ASCII is preserved
        assert data["file-1"]["customer_name"] == "Łukasz Żółć"

    def test_corrupted_file_treated_as_empty(self, tmp_path):
        store_root = tmp_path / "store"
        store_root.mkdir()
        (store_root / "shipping-drafts.json").write_text("not-json", encoding="utf-8")
        store = ShippingStore(local_root=store_root)
        # Should not raise — returns empty rather than crashing the API
        assert store.list_drafts() == []
        # And next upsert recovers the file
        store.upsert_draft(_draft("recover-1"))
        assert store.get_draft("recover-1") is not None


# ── TDD-red: idempotency by shopify_order_id ──────────────────────────────────


class TestShopifyOrderIdempotency:
    """**TDD-red** — Shopify retries webhooks. Two upserts with the same
    shopify_order_id but different generated UUIDs must NOT create two drafts.

    Target: shipping_store grows an `upsert_by_shopify_order_id` (or
    upsert_draft de-duplicates on shopify_order_id + courier) — see audit §7.4.
    """

    def test_duplicate_shopify_order_id_produces_single_draft(self, store):
        d1 = _draft("uuid-aaa", shopify_order_id="shop-9001")
        d2 = _draft("uuid-bbb", shopify_order_id="shop-9001")
        store.upsert_draft(d1)
        store.upsert_draft(d2)
        # Should be exactly one logical draft for the Shopify order
        drafts = [r for r in store.list_drafts() if r["shopify_order_id"] == "shop-9001"]
        assert len(drafts) == 1


# ── TDD-red: concurrent writes ────────────────────────────────────────────────


class TestConcurrentWrites:
    """Concurrency safety — the local backend does naive read-modify-write
    without explicit locking (audit §7.4). The lost-update demonstration below
    forces a deterministic race by stalling between read and write; on a fixed
    implementation (file lock or atomic write+rename) it will pass instead.
    """

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "TDD/flaky: 20 parallel upserts on the local backend lose writes "
            "intermittently because _local_save does naive read-modify-write "
            "with Path.write_text(). Strict=False because the GIL sometimes "
            "serialises the critical section. Once shipping_store gains a "
            "file lock + atomic write this test should be flipped to PASS."
        ),
    )
    def test_parallel_upserts_all_persist(self, store):
        """Smoke test: 20 parallel upserts all land in the store.

        On the current Path.write_text() implementation this happens to pass
        most of the time because the critical section is short and the GIL
        serialises Python-level calls — but no guarantee. The next test forces
        the race deterministically.
        """
        N = 20
        errors: list[Exception] = []

        def worker(i: int):
            try:
                store.upsert_draft(_draft(f"par-{i}"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        ids = {r["id"] for r in store.list_drafts(limit=1000)}
        assert ids == {f"par-{i}" for i in range(N)}

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "TDD: lost-update race — two writers reading the same baseline both "
            "save back without seeing each other's update. Needs file lock or "
            "atomic compare-and-swap in _local_save."
        ),
    )
    def test_lost_update_race_is_prevented(self, store, monkeypatch):
        """Force the read-modify-write race by inserting a stall between
        _local_load() and _local_save() in the worker thread.

        Both threads read the same empty baseline, both add a different
        draft and save. On the current implementation the second save
        overwrites the first — we observe only 1 draft in storage instead
        of 2. A safe implementation (file lock or atomic CAS) preserves
        both.
        """
        original_load = store._local_load
        gate = threading.Event()
        stalled = threading.Event()

        def stalling_load():
            data = original_load()
            # Only the FIRST loader stalls; the second proceeds normally
            if not stalled.is_set():
                stalled.set()
                gate.wait(timeout=2.0)
            return data

        monkeypatch.setattr(store, "_local_load", stalling_load)

        results: dict[str, Exception | None] = {}

        def writer(name: str):
            try:
                store.upsert_draft(_draft(name))
                results[name] = None
            except Exception as exc:
                results[name] = exc

        t1 = threading.Thread(target=writer, args=("race-a",))
        t2 = threading.Thread(target=writer, args=("race-b",))
        t1.start()
        # Wait until t1 is stalled inside _local_load, then start t2
        assert stalled.wait(timeout=1.0)
        t2.start()
        t2.join(timeout=2.0)
        # Release t1 so it finishes its (now stale) save
        gate.set()
        t1.join(timeout=2.0)

        assert results == {"race-a": None, "race-b": None}
        ids = {r["id"] for r in store.list_drafts(limit=1000)}
        # Both writes must be present — a lost update means one is missing
        assert ids == {"race-a", "race-b"}, (
            f"Lost update: expected both drafts persisted, got {ids}"
        )


# ── TDD-red: atomic write semantics ───────────────────────────────────────────


class TestAtomicWrite:
    """**TDD-red** — _local_save uses Path.write_text(), which writes in place.
    A crash mid-write leaves a truncated file. Target: temp file + os.replace.
    """

    def test_write_is_atomic_via_temp_file(self, tmp_path, monkeypatch):
        store_root = tmp_path / "store"
        store = ShippingStore(local_root=store_root)
        store.upsert_draft(_draft("atom-1"))

        # The implementation should write to a temp file and then os.replace.
        # We detect this by patching write_text to detect direct in-place writes.
        from zdrovena.common import shipping_store as ss_mod  # noqa: F401

        write_calls: list[Path] = []
        original_write_text = Path.write_text

        def tracking_write_text(self, *args, **kwargs):
            write_calls.append(self)
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", tracking_write_text)
        store.upsert_draft(_draft("atom-2"))

        # An atomic implementation writes to a *.tmp / *.partial path, not to
        # the live shipping-drafts.json file directly.
        target_writes = [p for p in write_calls if p.name == "shipping-drafts.json"]
        assert target_writes == [], (
            "shipping-drafts.json was written in-place; expected atomic write-to-temp + os.replace"
        )
