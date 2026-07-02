"""Unit tests for ShopifyDedupStore (local JSON backend + TTL semantics).

The Azure Table branch requires live credentials and is exercised only in
integration; here we cover the local file backend, TTL expiry, empty-id
handling, and the fail-closed DedupStoreError contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from zdrovena.common.shopify_dedup_store import (
    DedupStoreError,
    ShopifyDedupStore,
    _is_expired,
    get_shopify_dedup_store,
)


@pytest.fixture()
def store(tmp_path) -> ShopifyDedupStore:
    return ShopifyDedupStore(local_root=tmp_path / "dedup")


class TestLocalDedup:
    def test_unseen_id_is_not_duplicate(self, store):
        assert store.is_duplicate("wh-1") is False

    def test_mark_then_duplicate(self, store):
        store.mark_seen("wh-1")
        assert store.is_duplicate("wh-1") is True

    def test_distinct_ids_are_independent(self, store):
        store.mark_seen("wh-1")
        assert store.is_duplicate("wh-2") is False

    def test_empty_id_is_never_duplicate_and_mark_is_noop(self, store):
        assert store.is_duplicate("") is False
        store.mark_seen("")  # no-op, must not raise or persist anything
        assert store.is_duplicate("") is False

    def test_mark_seen_persists_across_instances(self, tmp_path):
        root = tmp_path / "dedup"
        a = ShopifyDedupStore(local_root=root)
        a.mark_seen("wh-persist")
        b = ShopifyDedupStore(local_root=root)
        assert b.is_duplicate("wh-persist") is True


class TestTTLExpiry:
    def test_expired_entry_is_not_duplicate_and_is_pruned(self, tmp_path):
        store = ShopifyDedupStore(local_root=tmp_path / "dedup")
        # Write an entry with a timestamp well beyond the 24h TTL.
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        store._local_file.write_text(json.dumps({"wh-old": stale}), encoding="utf-8")

        assert store.is_duplicate("wh-old") is False
        # Reading an expired entry prunes it from the file.
        remaining = json.loads(store._local_file.read_text(encoding="utf-8"))
        assert "wh-old" not in remaining

    def test_mark_seen_prunes_expired_entries(self, tmp_path):
        store = ShopifyDedupStore(local_root=tmp_path / "dedup")
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        store._local_file.write_text(json.dumps({"wh-old": stale}), encoding="utf-8")

        store.mark_seen("wh-new")
        data = json.loads(store._local_file.read_text(encoding="utf-8"))
        assert "wh-old" not in data
        assert "wh-new" in data

    def test_is_expired_helper(self):
        now = datetime.now(timezone.utc)
        fresh = now.isoformat()
        stale = (now - timedelta(hours=25)).isoformat()
        assert _is_expired(fresh, now) is False
        assert _is_expired(stale, now) is True
        # Unparseable timestamps are treated as expired.
        assert _is_expired("not-a-date", now) is True

    def test_naive_timestamp_treated_as_utc(self):
        now = datetime.now(timezone.utc)
        naive_fresh = now.replace(tzinfo=None).isoformat()
        assert _is_expired(naive_fresh, now) is False


class TestFailClosed:
    def test_corrupt_local_file_raises_dedup_error(self, tmp_path):
        store = ShopifyDedupStore(local_root=tmp_path / "dedup")
        store._local_file.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(DedupStoreError):
            store.is_duplicate("wh-1")

    def test_corrupt_local_file_raises_on_mark_seen(self, tmp_path):
        store = ShopifyDedupStore(local_root=tmp_path / "dedup")
        store._local_file.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(DedupStoreError):
            store.mark_seen("wh-1")


class TestFactory:
    def test_factory_returns_local_store_without_azure_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
        monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
        store = get_shopify_dedup_store(local_root=tmp_path / "dedup")
        assert store._use_table is False

    def test_factory_selects_table_when_account_url_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://acct.blob.core.windows.net")
        store = get_shopify_dedup_store(local_root=tmp_path / "dedup")
        assert store._use_table is True

    def test_factory_selects_table_when_connection_string_set(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
        store = get_shopify_dedup_store(local_root=tmp_path / "dedup")
        assert store._use_table is True
