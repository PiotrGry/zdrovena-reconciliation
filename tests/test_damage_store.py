from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)

from zdrovena.common.damage_store import DamageStore, get_damage_store
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


# ── Local JSON backend: damaged file and failed writes ────────────────────────


def test_corrupt_local_file_reads_as_empty_store(tmp_path):
    (tmp_path / "damage-cases.json").write_text("{not json", encoding="utf-8")
    store = DamageStore(local_root=tmp_path)

    assert store.get_case("case-1") is None
    assert store.list_cases() == []
    assert store.get_state("cursor", "fallback") == "fallback"


def test_local_file_without_sections_still_accepts_writes(tmp_path):
    (tmp_path / "damage-cases.json").write_text("{}", encoding="utf-8")
    store = DamageStore(local_root=tmp_path)

    store.upsert_case({"id": "case-1", "status": "needs_review"})

    assert store.get_case("case-1") == {"id": "case-1", "status": "needs_review"}


def test_failed_local_write_raises_and_keeps_previous_data(tmp_path):
    store = DamageStore(local_root=tmp_path)
    store.upsert_case({"id": "case-1", "status": "needs_review"})

    with pytest.raises(TypeError):
        store.upsert_case({"id": "case-2", "not_json": {1, 2}})

    assert store.get_case("case-1") == {"id": "case-1", "status": "needs_review"}
    assert store.get_case("case-2") is None
    assert list(tmp_path.glob(".tmp-damage-*")) == []


def test_failed_local_write_surfaces_original_error_when_cleanup_fails(tmp_path, monkeypatch):
    store = DamageStore(local_root=tmp_path)

    def refuse_replace(src, dst):
        raise PermissionError("replace refused")

    def refuse_unlink(path):
        raise OSError("unlink refused")

    monkeypatch.setattr("zdrovena.common.damage_store.os.replace", refuse_replace)
    monkeypatch.setattr("zdrovena.common.damage_store.os.unlink", refuse_unlink)

    with pytest.raises(PermissionError, match="replace refused"):
        store.upsert_case({"id": "case-1"})


def test_local_list_cases_sorts_newest_first_and_honours_limit(tmp_path):
    store = DamageStore(local_root=tmp_path)
    store.upsert_case({"id": "old", "detected_at": "2026-07-01T00:00:00Z"})
    store.upsert_case({"id": "new", "created_at": "2026-07-03T00:00:00Z"})
    store.upsert_case({"id": "mid", "detected_at": "2026-07-02T00:00:00Z"})
    store.upsert_case({"id": "undated"})

    assert [case["id"] for case in store.list_cases()] == ["new", "mid", "old", "undated"]
    assert [case["id"] for case in store.list_cases(limit=2)] == ["new", "mid"]


def test_find_case_by_fingerprint_local(tmp_path):
    store = DamageStore(local_root=tmp_path)
    store.upsert_case({"id": "case-1", "event_fingerprint": "fp-1"})
    store.upsert_case({"id": "case-2", "event_fingerprint": "fp-2"})

    assert store.find_case_by_fingerprint("fp-2")["id"] == "case-2"
    assert store.find_case_by_fingerprint("fp-missing") is None


# ── Email claim vs. recorded send attempt (issue #312) ────────────────────────


def _attempt(state: str, started_at: str | None = None) -> dict:
    return {
        "id": "attempt-1",
        "state": state,
        "fingerprint": "abc",
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.parametrize(
    ("state", "started_at", "expected"),
    [
        ("confirmed", None, False),
        ("pending", None, False),
        ("pending", "2020-01-01T00:00:00+00:00", False),  # aged into unknown
        ("unknown", None, False),
        ("failed", None, True),
    ],
    ids=["confirmed", "pending", "stale-pending", "unknown", "failed"],
)
def test_email_claim_respects_recorded_attempt(tmp_path, state, started_at, expected):
    store = DamageStore(local_root=tmp_path)
    # email_sending=True proves the attempt, not the legacy flag, decides.
    store.upsert_case(
        {"id": "case-1", "email_attempt": _attempt(state, started_at), "email_sending": True}
    )

    assert store.try_claim_email("case-1") is expected


def test_email_claim_records_attempt_and_blocks_second_claim(tmp_path):
    store = DamageStore(local_root=tmp_path)
    store.upsert_case({"id": "case-1"})
    attempt = _attempt("pending")

    assert store.try_claim_email("case-1", attempt) is True
    case = store.get_case("case-1")
    assert case["email_attempt"] == attempt
    assert case["email_sending"] is True
    assert case["email_sending_at"]
    assert store.try_claim_email("case-1", _attempt("pending")) is False


def test_email_claim_on_missing_case_returns_false(tmp_path):
    assert DamageStore(local_root=tmp_path).try_claim_email("missing") is False


# ── Azure Table backend, driven through an in-memory table client ─────────────


class _Entity(dict):
    def __init__(self, data: dict, etag: str) -> None:
        super().__init__(data)
        self.metadata = {"etag": etag}


class _FakeTableClient:
    """Minimal stand-in for azure.data.tables.TableClient."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.versions: dict[tuple[str, str], int] = {}
        self.modify_after_read = False

    def _bump(self, key: tuple[str, str]) -> None:
        self.versions[key] = self.versions.get(key, 0) + 1

    def upsert_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.rows[key] = dict(entity)
        self._bump(key)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError("no such row")
        etag = f"v{self.versions[key]}"
        entity = _Entity(
            {**self.rows[key], "etag": etag, "Timestamp": "2026-07-15T00:00:00Z"}, etag
        )
        if self.modify_after_read:
            self._bump(key)  # another writer got in between read and update
        return entity

    def update_entity(self, entity, mode="merge", etag=None, match_condition=None):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key not in self.rows:
            raise ResourceNotFoundError("no such row")
        if etag is not None and etag != f"v{self.versions[key]}":
            raise HttpResponseError("412 Precondition Failed")
        self.rows[key].update(entity)
        self._bump(key)

    def query_entities(self, query_filter):
        return [
            _Entity(row, f"v{self.versions[key]}")
            for key, row in self.rows.items()
            if f"'{key[0]}'" in query_filter
        ]


@pytest.fixture
def table_store(monkeypatch):
    store = DamageStore(account_url="https://fake.blob.core.windows.net")
    client = _FakeTableClient()
    monkeypatch.setattr(store, "_table_client", lambda: client)
    return store


class TestTableBackend:
    def test_case_round_trips_with_nested_and_empty_values(self, table_store):
        table_store.upsert_case(
            {
                "id": "case-1",
                "status": "needs_review",
                "evidence": [{"code": "ISSUE", "opis": "zgnieciona paczka"}],
                "order": {"name": "#1001"},
                "note": None,
                "attempts": 3,
                "tracking": "123456",
                "flagged": True,
            }
        )

        assert table_store.get_case("case-1") == {
            "id": "case-1",
            "status": "needs_review",
            "evidence": [{"code": "ISSUE", "opis": "zgnieciona paczka"}],
            "order": {"name": "#1001"},
            "note": None,
            "attempts": 3,
            # a numeric-looking string must stay a string
            "tracking": "123456",
            "flagged": True,
        }

    def test_missing_case_returns_none(self, table_store):
        assert table_store.get_case("missing") is None

    def test_update_case_merges_fields(self, table_store):
        table_store.upsert_case({"id": "case-1", "status": "needs_review", "note": "keep"})

        assert table_store.update_case("case-1", {"status": "approved", "tags": ["a"]}) is True
        assert table_store.get_case("case-1") == {
            "id": "case-1",
            "status": "approved",
            "note": "keep",
            "tags": ["a"],
        }

    def test_update_missing_case_returns_false(self, table_store):
        assert table_store.update_case("missing", {"status": "approved"}) is False
        assert table_store.get_case("missing") is None

    def test_list_cases_sorted_limited_and_excludes_state_rows(self, table_store):
        table_store.upsert_case({"id": "old", "detected_at": "2026-07-01T00:00:00Z"})
        table_store.upsert_case({"id": "new", "detected_at": "2026-07-03T00:00:00Z"})
        table_store.upsert_case({"id": "mid", "created_at": "2026-07-02T00:00:00Z"})
        table_store.set_state("cursor", 7)

        assert [case["id"] for case in table_store.list_cases()] == ["new", "mid", "old"]
        assert [case["id"] for case in table_store.list_cases(limit=1)] == ["new"]

    def test_find_case_by_fingerprint(self, table_store):
        table_store.upsert_case({"id": "case-1", "event_fingerprint": "fp-1"})
        table_store.upsert_case({"id": "case-2", "event_fingerprint": "fp-2"})

        assert table_store.find_case_by_fingerprint("fp-2")["id"] == "case-2"
        assert table_store.find_case_by_fingerprint("fp-missing") is None

    def test_count_needs_review(self, table_store):
        table_store.upsert_case({"id": "a", "status": "needs_review", "classification": "damage"})
        table_store.upsert_case(
            {"id": "b", "status": "needs_review", "classification": "carrier_issue"}
        )
        table_store.upsert_case({"id": "c", "status": "approved", "classification": "damage"})

        assert table_store.count_needs_review() == 1

    def test_state_round_trips_and_defaults(self, table_store):
        assert table_store.get_state("cursor", "fallback") == "fallback"

        table_store.set_state("cursor", {"since": "2026-07-15", "page": 2})

        assert table_store.get_state("cursor") == {"since": "2026-07-15", "page": 2}

    def test_email_claim_records_attempt_and_blocks_second_claim(self, table_store):
        table_store.upsert_case({"id": "case-1", "status": "replacement_created"})
        attempt = _attempt("pending")

        assert table_store.try_claim_email("case-1", attempt) is True
        case = table_store.get_case("case-1")
        assert case["email_attempt"] == attempt
        assert case["email_sending"] is True
        assert case["email_sending_at"]
        assert table_store.try_claim_email("case-1", _attempt("pending")) is False

    def test_email_claim_without_attempt_uses_legacy_flag(self, table_store):
        table_store.upsert_case({"id": "case-1"})

        assert table_store.try_claim_email("case-1") is True
        assert "email_attempt" not in table_store.get_case("case-1")
        assert table_store.try_claim_email("case-1") is False

    def test_email_claim_refused_once_email_was_sent(self, table_store):
        table_store.upsert_case({"id": "case-1", "email_sent_at": "2026-07-15T10:00:00Z"})

        assert table_store.try_claim_email("case-1") is False

    def test_email_claim_on_missing_case_returns_false(self, table_store):
        assert table_store.try_claim_email("missing") is False

    def test_email_claim_lost_to_concurrent_writer_returns_false(self, table_store):
        table_store.upsert_case({"id": "case-1"})
        table_store._table_client().modify_after_read = True

        assert table_store.try_claim_email("case-1", _attempt("pending")) is False

        table_store._table_client().modify_after_read = False
        case = table_store.get_case("case-1")
        assert not case.get("email_sending")
        assert "email_attempt" not in case


# ── Client construction and backend selection ─────────────────────────────────


def _patched_tables_sdk():
    service = MagicMock(name="TableServiceClient")
    service.create_table_if_not_exists.return_value = MagicMock(name="TableClient")
    service_cls = MagicMock(name="TableServiceClientClass", return_value=service)
    service_cls.from_connection_string.return_value = service
    modules = {
        "azure.data.tables": MagicMock(TableServiceClient=service_cls),
        "azure.identity": MagicMock(DefaultAzureCredential=MagicMock()),
    }
    return service, service_cls, patch.dict("sys.modules", modules)


def test_table_client_built_from_connection_string():
    service, service_cls, sdk_patch = _patched_tables_sdk()
    with sdk_patch:
        store = DamageStore(connection_string="UseDevelopmentStorage=true")
        client = store._table_client()

    service_cls.from_connection_string.assert_called_once_with("UseDevelopmentStorage=true")
    service_cls.assert_not_called()
    service.create_table_if_not_exists.assert_called_once_with("damagecases")
    assert client is service.create_table_if_not_exists.return_value


def test_table_client_uses_table_endpoint_for_account_url():
    _service, service_cls, sdk_patch = _patched_tables_sdk()
    with sdk_patch:
        DamageStore(account_url="https://acct.blob.core.windows.net")._table_client()

    assert service_cls.call_args.kwargs["endpoint"] == "https://acct.table.core.windows.net"


def test_table_client_without_configuration_raises(tmp_path):
    _service, _service_cls, sdk_patch = _patched_tables_sdk()
    with sdk_patch, pytest.raises(RuntimeError, match="storage configuration is missing"):
        DamageStore(local_root=tmp_path)._table_client()


def test_get_damage_store_selects_backend_from_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_URL", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    local = get_damage_store(local_root=tmp_path)
    local.set_state("cursor", 1)
    assert local._use_table is False
    assert (tmp_path / "damage-cases.json").exists()

    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    by_connection = get_damage_store()
    assert by_connection._use_table is True
    assert by_connection._connection_string == "UseDevelopmentStorage=true"

    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://acct.blob.core.windows.net")
    by_url = get_damage_store()
    assert by_url._use_table is True
    assert by_url._account_url == "https://acct.blob.core.windows.net"
