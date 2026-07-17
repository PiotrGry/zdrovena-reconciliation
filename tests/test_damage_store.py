from __future__ import annotations

from zdrovena.common.damage_store import DamageStore


def test_damage_store_persists_cases_and_cursor(tmp_path):
    store = DamageStore(local_root=tmp_path)
    store.upsert_case(
        {
            "id": "case-1",
            "status": "needs_review",
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
