"""Tests for zdrovena.common.shipping_store.ShippingStore (local backend).

Regression coverage for concurrency, idempotency and silent-failure behaviour.

Audit reference: zdrovena_test_audit.md §7.4.
"""

from __future__ import annotations

import json
import threading
import time
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


# ── idempotency by shopify_order_id ───────────────────────────────────────────


class TestShopifyOrderIdempotency:
    """Shopify retries webhooks. Two upserts with the same shopify_order_id but
    different generated UUIDs must NOT create two drafts — upsert_draft
    de-duplicates on shopify_order_id + courier (audit §7.4).
    """

    def test_duplicate_shopify_order_id_produces_single_draft(self, store):
        d1 = _draft("uuid-aaa", shopify_order_id="shop-9001")
        d2 = _draft("uuid-bbb", shopify_order_id="shop-9001")
        store.upsert_draft(d1)
        store.upsert_draft(d2)
        # Should be exactly one logical draft for the Shopify order
        drafts = [r for r in store.list_drafts() if r["shopify_order_id"] == "shop-9001"]
        assert len(drafts) == 1


# ── concurrent writes ─────────────────────────────────────────────────────────


class TestConcurrentWrites:
    """Concurrency safety for the local backend (audit §7.4). upsert_draft
    guards the read-modify-write critical section with a file lock
    (_acquire_lock → flock) and an atomic write+rename in _local_save, so
    parallel writers neither error nor lose each other's updates.
    """

    def test_parallel_upserts_all_persist(self, store):
        """20 parallel upserts all land in the store — the file lock serialises
        the read-modify-write critical section so none are lost.
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

    def test_lost_update_race_is_prevented(self, store, monkeypatch):
        """Force the read-modify-write race by stalling the first writer
        inside _local_load(), then verify both writes survive.

        The first writer stalls while holding the store lock; the second
        writer must therefore block until the first releases, re-read the
        now-updated baseline and add its own draft. A lock-free (or
        lost-update) implementation would let the second writer proceed on
        the stale empty baseline and the first writer's save would clobber
        it — leaving only 1 draft instead of 2. upsert_draft's flock
        (_acquire_lock before _local_load) prevents that.
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
        # Wait until t1 is stalled inside _local_load (holding the lock),
        # then start t2 so it contends for the same lock.
        assert stalled.wait(timeout=1.0)
        t2.start()
        # Give t2 a moment to reach the lock and block on it.
        time.sleep(0.1)
        # Release t1 so it finishes its save and frees the lock; t2 then
        # acquires the lock, re-reads t1's write, and adds its own.
        gate.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        assert results == {"race-a": None, "race-b": None}
        ids = {r["id"] for r in store.list_drafts(limit=1000)}
        # Both writes must be present — a lost update means one is missing
        assert ids == {"race-a", "race-b"}, (
            f"Lost update: expected both drafts persisted, got {ids}"
        )


# ── atomic write semantics ────────────────────────────────────────────────────


class TestAtomicWrite:
    """_local_save must write to a temp file + os.replace, never in place, so a
    crash mid-write cannot leave a truncated shipping-drafts.json.
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


# ── try_claim_pickup — local backend ────────────────────────────────────────


class TestTryClaimPickupLocal:
    def test_first_claim_succeeds(self, store):
        store.upsert_draft(_draft("pk-1"))
        assert store.try_claim_pickup("pk-1") is True
        assert store.get_draft("pk-1")["pickup_ordered"] is True

    def test_second_claim_fails(self, store):
        store.upsert_draft(_draft("pk-2"))
        assert store.try_claim_pickup("pk-2") is True
        assert store.try_claim_pickup("pk-2") is False

    def test_claim_missing_draft_fails(self, store):
        assert store.try_claim_pickup("does-not-exist") is False

    def test_only_one_thread_wins_concurrent_claim(self, store):
        store.upsert_draft(_draft("pk-race"))
        results: list[bool] = []
        lock = threading.Lock()

        def worker():
            won = store.try_claim_pickup("pk-race")
            with lock:
                results.append(won)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1, f"Expected exactly one winner, got {results}"


# ── try_claim_pickup / upsert_draft dedup — table backend (fake client) ──────


class _FakeTableEntity(dict):
    """Minimal stand-in for azure.data.tables.TableEntity."""

    def __init__(self, data, etag):
        super().__init__(data)
        self._metadata = {"etag": etag}

    @property
    def metadata(self):
        return self._metadata


class _FakeTableClient:
    """In-memory fake of azure.data.tables.TableClient covering only the
    operations ShippingStore's table backend calls: get/update/upsert/delete/
    query, including ETag-based optimistic concurrency for update_entity.
    """

    def __init__(self):
        self._rows: dict[tuple[str, str], dict] = {}
        self._etags: dict[tuple[str, str], int] = {}

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self._rows:
            raise KeyError("not found")
        return _FakeTableEntity(dict(self._rows[key]), str(self._etags[key]))

    def upsert_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        self._rows[key] = dict(entity)
        self._etags[key] = self._etags.get(key, 0) + 1

    def update_entity(self, entity, mode="merge", etag=None, match_condition=None):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key not in self._rows:
            raise KeyError("not found")
        if etag is not None and str(self._etags[key]) != etag:
            from azure.core.exceptions import ResourceModifiedError

            raise ResourceModifiedError("etag mismatch")
        self._rows[key].update({k: v for k, v in entity.items()})
        self._etags[key] += 1

    def delete_entity(self, partition_key, row_key):
        self._rows.pop((partition_key, row_key), None)
        self._etags.pop((partition_key, row_key), None)

    def query_entities(self, query_filter):
        # Only supports the "field eq/ne 'value'" AND-chain shapes this
        # codebase generates — not a general OData parser.
        clauses = [c.strip() for c in query_filter.split(" and ")]
        results = []
        for row in self._rows.values():
            ok = True
            for clause in clauses:
                if " ne " in clause:
                    field, _, value = clause.partition(" ne ")
                    value = value.strip().strip("'").replace("''", "'")
                    if str(row.get(field.strip())) == value:
                        ok = False
                        break
                elif " eq " in clause:
                    field, _, value = clause.partition(" eq ")
                    value = value.strip().strip("'").replace("''", "'")
                    if str(row.get(field.strip())) != value:
                        ok = False
                        break
            if ok:
                results.append(dict(row))
        return iter(results)


@pytest.fixture()
def table_store(monkeypatch) -> tuple[ShippingStore, _FakeTableClient]:
    store = ShippingStore(account_url="https://fake.blob.core.windows.net")
    fake = _FakeTableClient()
    monkeypatch.setattr(store, "_table_client", lambda: fake)
    return store, fake


class TestTryClaimPickupTable:
    def test_first_claim_succeeds(self, table_store):
        store, _fake = table_store
        store.upsert_draft(_draft("t-pk-1"))
        assert store.try_claim_pickup("t-pk-1") is True
        assert store.get_draft("t-pk-1")["pickup_ordered"] is True

    def test_second_claim_fails(self, table_store):
        store, _fake = table_store
        store.upsert_draft(_draft("t-pk-2"))
        assert store.try_claim_pickup("t-pk-2") is True
        assert store.try_claim_pickup("t-pk-2") is False

    def test_claim_missing_draft_fails(self, table_store):
        store, _fake = table_store
        assert store.try_claim_pickup("nope") is False

    def test_concurrent_update_between_get_and_claim_loses(self, table_store):
        """Simulates a second writer mutating the row between our get_entity
        and update_entity calls — the etag mismatch must make the claim fail
        rather than silently overwrite.
        """
        store, fake = table_store
        store.upsert_draft(_draft("t-pk-3"))

        original_get_entity = fake.get_entity

        def get_entity_then_mutate(partition_key, row_key):
            entity = original_get_entity(partition_key, row_key)
            # Another process updates the row after our read but before our write.
            fake.update_entity(
                {"PartitionKey": partition_key, "RowKey": row_key, "note": "concurrent-write"}
            )
            return entity

        fake.get_entity = get_entity_then_mutate
        assert store.try_claim_pickup("t-pk-3") is False


class TestUpsertDedupTable:
    def test_duplicate_shopify_order_id_removes_older_draft(self, table_store):
        store, _fake = table_store
        d1 = _draft("t-uuid-aaa", shopify_order_id="shop-9001")
        d2 = _draft("t-uuid-bbb", shopify_order_id="shop-9001")
        store.upsert_draft(d1)
        store.upsert_draft(d2)
        assert store.get_draft("t-uuid-aaa") is None
        assert store.get_draft("t-uuid-bbb") is not None

    def test_distinct_shopify_order_ids_both_kept(self, table_store):
        store, _fake = table_store
        store.upsert_draft(_draft("t-uuid-ccc", shopify_order_id="shop-1"))
        store.upsert_draft(_draft("t-uuid-ddd", shopify_order_id="shop-2"))
        assert store.get_draft("t-uuid-ccc") is not None
        assert store.get_draft("t-uuid-ddd") is not None
