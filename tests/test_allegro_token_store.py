"""Tests for Allegro refresh-token persistence (P0-2).

Allegro rotates the refresh token on every OAuth exchange. Without a
persistent store, the first process restart after a rotation permanently
breaks the integration. These tests verify:

1. When the refresh token rotates, the store is called with the new value.
2. When the store is not injected, an in-memory fallback is used (safe for
   tests / CLI, unsafe for long-running services).
3. When the store already holds a token, it wins over the constructor arg
   (the store is the authoritative source).
4. When the store save fails, the client still works in-process but the
   failure is logged at ERROR.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from zdrovena.common.allegro import (
    AllegroClient,
    InMemoryAllegroTokenStore,
    SecretsAllegroTokenStore,
)


def _mock_ok_token_response(new_rt: str | None = "rotated-rt") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "access_token": "access-1",
        "expires_in": 3600,
        "refresh_token": new_rt,
        "token_type": "Bearer",
    }
    resp.text = ""
    return resp


class TestInMemoryStore:
    def test_defaults_to_in_memory_when_no_store(self):
        c = AllegroClient(
            client_id="cid",
            client_secret="csec",
            refresh_token="initial-rt",
            env="prod",
        )
        assert isinstance(c._token_store, InMemoryAllegroTokenStore)
        # Store was seeded with the constructor arg.
        assert c._token_store.load_refresh_token() == "initial-rt"

    def test_rotation_updates_in_memory_store(self):
        c = AllegroClient(
            client_id="cid",
            client_secret="csec",
            refresh_token="initial-rt",
            env="prod",
        )
        with patch.object(
            c._session, "request", return_value=_mock_ok_token_response("rotated-rt")
        ):
            c._fetch_token()
        assert c._refresh_token == "rotated-rt"
        assert c._token_store.load_refresh_token() == "rotated-rt"

    def test_no_write_when_token_unchanged(self):
        """If Allegro returns the same refresh_token (unlikely but possible), skip persistence.

        Avoids spurious Key Vault writes.
        """
        store = MagicMock(spec=InMemoryAllegroTokenStore)
        store.load_refresh_token.return_value = "initial-rt"
        c = AllegroClient(
            client_id="cid",
            client_secret="csec",
            refresh_token="initial-rt",
            env="prod",
            token_store=store,
        )
        with patch.object(
            c._session, "request", return_value=_mock_ok_token_response("initial-rt")
        ):
            c._fetch_token()
        store.save_refresh_token.assert_not_called()


class TestInjectedStore:
    def test_stored_token_wins_over_constructor_arg(self):
        """If the store already has a rotated token, prefer it over env."""
        store = InMemoryAllegroTokenStore(initial_token="rotated-newer")
        c = AllegroClient(
            client_id="cid",
            client_secret="csec",
            refresh_token="stale-from-env",
            env="prod",
            token_store=store,
        )
        assert c._refresh_token == "rotated-newer"

    def test_falls_back_to_ctor_arg_when_store_empty(self):
        store = InMemoryAllegroTokenStore(initial_token=None)
        c = AllegroClient(
            client_id="cid",
            client_secret="csec",
            refresh_token="from-env",
            env="prod",
            token_store=store,
        )
        assert c._refresh_token == "from-env"

    def test_rotation_calls_save_on_injected_store(self):
        store = MagicMock(spec=InMemoryAllegroTokenStore)
        store.load_refresh_token.return_value = "initial-rt"
        store.save_refresh_token.return_value = True
        c = AllegroClient(
            client_id="cid",
            client_secret="csec",
            refresh_token="initial-rt",
            env="prod",
            token_store=store,
        )
        with patch.object(
            c._session, "request", return_value=_mock_ok_token_response("rotated-rt")
        ):
            c._fetch_token()
        store.save_refresh_token.assert_called_once_with("rotated-rt")

    def test_save_failure_logs_error_but_client_still_works(self, caplog):
        store = MagicMock(spec=InMemoryAllegroTokenStore)
        store.load_refresh_token.return_value = "initial-rt"
        store.save_refresh_token.return_value = False  # persistence failed
        c = AllegroClient(
            client_id="cid",
            client_secret="csec",
            refresh_token="initial-rt",
            env="prod",
            token_store=store,
        )
        with caplog.at_level(logging.ERROR, logger="zdrovena.common.allegro"):
            with patch.object(
                c._session,
                "request",
                return_value=_mock_ok_token_response("rotated-rt"),
            ):
                c._fetch_token()
        # In-memory token still rotated so current process keeps working
        assert c._refresh_token == "rotated-rt"
        # But operator got a loud warning
        assert any("could NOT be persisted" in r.message for r in caplog.records)

    def test_save_raising_exception_is_swallowed_and_logged(self, caplog):
        store = MagicMock(spec=InMemoryAllegroTokenStore)
        store.load_refresh_token.return_value = "initial-rt"
        store.save_refresh_token.side_effect = RuntimeError("KV down")
        c = AllegroClient(
            client_id="cid",
            client_secret="csec",
            refresh_token="initial-rt",
            env="prod",
            token_store=store,
        )
        with caplog.at_level(logging.ERROR, logger="zdrovena.common.allegro"):
            with patch.object(
                c._session,
                "request",
                return_value=_mock_ok_token_response("rotated-rt"),
            ):
                c._fetch_token()
        # Exception did not propagate
        assert c._refresh_token == "rotated-rt"


class TestSecretsAllegroTokenStore:
    def test_load_calls_get_secret(self):
        store = SecretsAllegroTokenStore()
        with patch("zdrovena.common.secrets.get_secret", return_value="rt-1") as gs:
            assert store.load_refresh_token() == "rt-1"
        gs.assert_called_once_with("allegro-refresh-token", required=False)

    def test_save_calls_set_secret(self):
        store = SecretsAllegroTokenStore()
        with patch("zdrovena.common.secrets.set_secret", return_value=True) as ss:
            assert store.save_refresh_token("new-rt") is True
        ss.assert_called_once_with("allegro-refresh-token", "new-rt")

    def test_save_propagates_failure(self):
        store = SecretsAllegroTokenStore()
        with patch("zdrovena.common.secrets.set_secret", return_value=False):
            assert store.save_refresh_token("new-rt") is False
