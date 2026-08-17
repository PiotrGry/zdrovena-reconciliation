"""Tests for durable month-close workflow state and claims."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from zdrovena.month_closing.run_store import (
    CloseRunStore,
    RunBusyError,
    RunConflictError,
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


def test_run_persisted_before_waivers_existed_is_backfilled(tmp_path):
    """Runs written by an older release have no 'waivers' key — reads must not KeyError."""
    store = CloseRunStore(local_root=tmp_path)
    legacy = new_close_run(2026, 6, "owner@example.com")
    del legacy["waivers"]
    store.save(legacy)

    reloaded = store.get(2026, 6)
    claimed = store.try_claim(2026, 6, "check", "owner@example.com")

    assert reloaded is not None
    assert reloaded["waivers"] == []
    assert claimed["waivers"] == []


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


def test_stale_writer_is_rejected_instead_of_clobbering(tmp_path):
    store = CloseRunStore(local_root=tmp_path)
    store.get_or_create(2026, 6, "owner@example.com")
    first = store.get(2026, 6)
    second = store.get(2026, 6)
    assert first is not None and second is not None

    first["status"] = "collecting"
    store.save(first, expect_rev=int(first["rev"]))

    second["status"] = "failed"
    with pytest.raises(RunConflictError):
        store.save(second, expect_rev=int(second["rev"]))

    assert store.get(2026, 6)["status"] == "collecting"


def test_update_replays_the_mutation_after_losing_a_race(tmp_path):
    store = CloseRunStore(local_root=tmp_path)
    store.get_or_create(2026, 6, "owner@example.com")
    seen_revs = []

    def mutate(run):
        seen_revs.append(int(run["rev"]))
        if len(seen_revs) == 1:
            # Another writer lands between our read and our save.
            other = store.get(2026, 6)
            other["status"] = "collecting"
            store.save(other, expect_rev=int(other["rev"]))
        run["logs"] = [*run.get("logs", []), "waiver"]

    result = store.update(2026, 6, mutate, "owner@example.com")

    assert len(seen_revs) == 2, "mutation should be replayed against the fresh run"
    assert result["status"] == "collecting", "the competing write must survive"
    assert result["logs"] == ["waiver"], "and our change lands on top of it"


def test_update_gives_up_when_every_attempt_loses(tmp_path):
    store = CloseRunStore(local_root=tmp_path)
    store.get_or_create(2026, 6, "owner@example.com")

    def mutate(run):
        other = store.get(2026, 6)
        store.save(other, expect_rev=int(other["rev"]))
        run["logs"] = [*run.get("logs", []), "waiver"]

    with pytest.raises(RunConflictError):
        store.update(2026, 6, mutate, "owner@example.com", retries=2)


def test_finish_action_loses_to_a_revoked_claim(tmp_path):
    store = CloseRunStore(local_root=tmp_path)
    claimed = store.try_claim(2026, 6, "costs", "owner@example.com")
    store.reset(2026, 6, "owner@example.com")  # operator started a fresh run

    with pytest.raises(RunConflictError):
        store.finish_action(claimed, "costs", success=True, message="late", status="collecting")

    assert store.get(2026, 6)["steps"]["costs"]["status"] == "pending"


def test_table_backend_rejects_a_stale_revision(tmp_path):
    store = CloseRunStore(connection_string="UseDevelopmentStorage=true")
    run = new_close_run(2026, 6, "owner@example.com")
    run["rev"] = 4
    client = _TableClient(store, run)

    with patch.object(store, "_table_client", return_value=client):
        fresh = run | {"status": "collecting"}
        store.save(fresh, expect_rev=4)
        assert client.update_calls, "matching revision must write through"

        with pytest.raises(RunConflictError):
            store.save(run | {"status": "failed"}, expect_rev=2)


def test_prod_and_staging_use_separate_partitions(tmp_path):
    prod = CloseRunStore(local_root=tmp_path, namespace="zdrovena-files")
    staging = CloseRunStore(local_root=tmp_path, namespace="zdrovena-files-staging")

    prod_run = prod.get_or_create(2026, 6, "prod@example.com")
    staging_run = staging.get_or_create(2026, 6, "staging@example.com")

    assert prod_run["run_id"] != staging_run["run_id"]
    assert prod.get(2026, 6)["requested_by"] == "prod@example.com"
    assert staging.get(2026, 6)["requested_by"] == "staging@example.com"
