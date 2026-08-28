from __future__ import annotations

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)

from zdrovena.common.damage_store import DamageStore
from zdrovena.common.exceptions import StorageUnavailableError


def test_damage_store_persists_cases_and_cursor(tmp_path):
    store = DamageStore(local_root=tmp_path)
    store.upsert_case(
        {
            "id": "case-1",
            "status": "needs_review",
            "classification": "damage",
            "detected_at": "2026-07-15T13:40:42Z",
            "evidence": [{"code": "ISSUE"}],
        }
    )

    assert store.get_case("case-1")["evidence"] == [{"code": "ISSUE"}]
    assert store.count_needs_review() == 1
    assert store.update_case("case-1", {"status": "approved"}) is True
    assert store.count_needs_review() == 0

    store.set_state("cursor", 123)
    reloaded = DamageStore(local_root=tmp_path)
    assert reloaded.get_state("cursor") == 123
    assert reloaded.get_case("case-1")["status"] == "approved"


def test_damage_store_count_ignores_non_damage_carrier_issues(tmp_path):
    store = DamageStore(local_root=tmp_path)
    store.upsert_case({"id": "damage", "status": "needs_review", "classification": "damage"})
    store.upsert_case({"id": "delay", "status": "needs_review", "classification": "carrier_issue"})

    assert store.count_needs_review() == 1


def test_damage_store_update_missing_case_returns_false(tmp_path):
    store = DamageStore(local_root=tmp_path)
    assert store.update_case("missing", {"status": "ignored"}) is False


def test_damage_store_email_claim_is_atomic_and_releasable(tmp_path):
    store = DamageStore(local_root=tmp_path)
    store.upsert_case({"id": "case-1", "status": "replacement_created"})

    assert store.try_claim_email("case-1") is True
    assert store.try_claim_email("case-1") is False
    store.update_case("case-1", {"email_sending": False})
    assert store.try_claim_email("case-1") is True
    store.update_case("case-1", {"email_sending": False, "email_sent_at": "now"})
    assert store.try_claim_email("case-1") is False


# ── Storage outage is not emptiness (issue #310) ──────────────────────────────

_OUTAGES = [
    ServiceRequestError("timeout"),
    HttpResponseError("429 ServerBusy"),
    ClientAuthenticationError("token expired"),
]


class _BrokenTableClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_entity(self, *args, **kwargs):
        raise self._exc

    def query_entities(self, *args, **kwargs):
        raise self._exc


def _broken_damage_store(monkeypatch, exc: Exception) -> DamageStore:
    store = DamageStore(account_url="https://fake.blob.core.windows.net")
    monkeypatch.setattr(store, "_table_client", lambda: _BrokenTableClient(exc))
    return store


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
