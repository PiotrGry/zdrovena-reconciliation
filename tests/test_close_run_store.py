"""Tests for durable month-close workflow state and claims."""

from __future__ import annotations

import pytest

from zdrovena.month_closing.run_store import (
    CloseRunStore,
    RunBusyError,
    new_close_run,
)


class _TableEntity(dict):
    @property
    def metadata(self):
        return {"etag": "1"}


class _TableClient:
    def __init__(self, store, run):
        self.entity = _TableEntity(store._entity(run))
        self.update_calls = []

    def get_entity(self, partition_key, row_key):
        return self.entity

    def update_entity(self, entity, **kwargs):
        assert kwargs["etag"] == "1"
        self.update_calls.append((entity, kwargs))
        self.entity = _TableEntity(entity)


def test_create_claim_finish_and_reload(tmp_path):
    store = CloseRunStore(local_root=tmp_path)
    run = store.get_or_create(2026, 6, "owner@example.com")

    claimed = store.try_claim(2026, 6, "check", "owner@example.com")
    assert claimed["run_id"] == run["run_id"]
    assert claimed["active_action"] == "check"
    assert claimed["steps"]["check"]["status"] == "running"

    finished = store.finish_action(
        claimed,
        "check",
        success=True,
        message="gotowe",
        status="ready",
    )
    reloaded = store.get(2026, 6)

    assert finished["active_action"] is None
    assert reloaded is not None
    assert reloaded["steps"]["check"]["status"] == "done"
    assert reloaded["status"] == "ready"


def test_second_action_cannot_claim_busy_period(tmp_path):
    store = CloseRunStore(local_root=tmp_path)
    store.try_claim(2026, 6, "check", "owner@example.com")

    with pytest.raises(RunBusyError):
        store.try_claim(2026, 6, "sales", "owner@example.com")


def test_reset_creates_new_run_without_deleting_period_files(tmp_path):
    store = CloseRunStore(local_root=tmp_path)
    first = store.get_or_create(2026, 6, "owner@example.com")
    second = store.reset(2026, 6, "owner@example.com")

    assert second["run_id"] != first["run_id"]
    assert all(step["status"] == "pending" for step in second["steps"].values())


def test_table_claim_uses_etag_and_blocks_second_action():
    run = new_close_run(2026, 6, "owner@example.com")
    store = CloseRunStore(
        account_url="https://example.blob.core.windows.net",
        namespace="prod-files",
    )
    client = _TableClient(store, run)
    store._table_client = lambda: client

    claimed = store.try_claim(2026, 6, "costs", "owner@example.com")

    assert claimed["active_action"] == "costs"
    assert len(client.update_calls) == 1
    with pytest.raises(RunBusyError):
        store.try_claim(2026, 6, "package", "owner@example.com")


def test_prod_and_staging_use_separate_partitions(tmp_path):
    prod = CloseRunStore(local_root=tmp_path, namespace="zdrovena-files")
    staging = CloseRunStore(local_root=tmp_path, namespace="zdrovena-files-staging")

    prod_run = prod.get_or_create(2026, 6, "prod@example.com")
    staging_run = staging.get_or_create(2026, 6, "staging@example.com")

    assert prod_run["run_id"] != staging_run["run_id"]
    assert prod.get(2026, 6)["requested_by"] == "prod@example.com"
    assert staging.get(2026, 6)["requested_by"] == "staging@example.com"
