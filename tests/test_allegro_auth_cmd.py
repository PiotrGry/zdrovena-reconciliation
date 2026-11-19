"""Tests for zdrovena.api.commands.allegro_auth_cmd (Device Authorization flow)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock

import pytest
import requests

from zdrovena.api.commands import allegro_auth_cmd
from zdrovena.common import secrets as secrets_mod


class _FakeClock:
    """Deterministic replacement for the ``time`` module used by the command."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _response(status_code: int = 200, payload=None, json_exc: Exception | None = None):
    r = MagicMock()
    r.status_code = status_code
    if json_exc is not None:
        r.json.side_effect = json_exc
    else:
        r.json.return_value = payload if payload is not None else {}
    r.raise_for_status.return_value = None
    return r


_DEVICE_PAYLOAD = {
    "device_code": "dev-123",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://allegro.pl/skojarz-aplikacje",
    "interval": 2,
    "expires_in": 60,
}

_TOKEN_PAYLOAD = {
    "access_token": "at-xyz",
    "refresh_token": "rt-xyz",
    "expires_in": 7200,
}


@pytest.fixture
def env(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(allegro_auth_cmd, "time", clock)
    monkeypatch.delenv("ALLEGRO_ENV", raising=False)

    secrets_store = {
        "allegro-client-id": "cid",
        "allegro-client-secret": "csecret",
    }
    saved: dict[str, str] = {}

    def fake_get_secret(name, *a, **kw):
        return secrets_store[name]

    def fake_set_secret(name, value, *a, **kw):
        saved[name] = value
        return True

    monkeypatch.setattr(secrets_mod, "get_secret", fake_get_secret)
    monkeypatch.setattr(secrets_mod, "set_secret", fake_set_secret)

    post = MagicMock()
    monkeypatch.setattr(requests, "post", post)

    return {"clock": clock, "saved": saved, "post": post}


def _args(sandbox: bool = False) -> argparse.Namespace:
    return argparse.Namespace(sandbox=sandbox)


def test_success_saves_tokens_to_key_vault(env, capsys):
    env["post"].side_effect = [
        _response(200, _DEVICE_PAYLOAD),
        _response(400, {"error": "authorization_pending"}),
        _response(200, _TOKEN_PAYLOAD),
    ]

    allegro_auth_cmd.run(_args())

    saved = env["saved"]
    assert saved["allegro-access-token"] == "at-xyz"
    assert saved["allegro-refresh-token"] == "rt-xyz"
    # expiry = time at success + expires_in (two sleeps of 2s each before success)
    assert float(saved["allegro-access-token-expiry"]) == pytest.approx(1_000_000.0 + 4 + 7200)

    # Production endpoints are used.
    calls = env["post"].call_args_list
    assert calls[0].args[0] == allegro_auth_cmd._DEVICE_URL
    assert calls[0].kwargs["params"] == {"client_id": "cid"}
    assert calls[1].args[0] == allegro_auth_cmd._TOKEN_URL
    assert calls[1].kwargs["data"] == {
        "grant_type": allegro_auth_cmd._GRANT_DEVICE,
        "device_code": "dev-123",
    }
    auth = calls[0].kwargs["auth"]
    assert (auth.username, auth.password) == ("cid", "csecret")
    assert env["clock"].sleeps == [2, 2]

    out = capsys.readouterr().out
    assert "(prod)" in out
    assert "https://allegro.pl/skojarz-aplikacje" in out
    assert "ABCD-EFGH" in out
    assert "Authorization successful" in out
    assert "valid for 2h" in out


@pytest.mark.parametrize("via_env", [False, True])
def test_sandbox_uses_sandbox_endpoints(env, monkeypatch, capsys, via_env):
    if via_env:
        monkeypatch.setenv("ALLEGRO_ENV", "sandbox")
    env["post"].side_effect = [
        _response(200, _DEVICE_PAYLOAD),
        _response(200, {"access_token": "a", "refresh_token": "r"}),
    ]

    allegro_auth_cmd.run(_args(sandbox=not via_env))

    calls = env["post"].call_args_list
    assert calls[0].args[0] == allegro_auth_cmd._SANDBOX_DEVICE_URL
    assert calls[1].args[0] == allegro_auth_cmd._SANDBOX_TOKEN_URL
    # Default token lifetime of 43200s (12h) is applied when expires_in is absent.
    assert float(env["saved"]["allegro-access-token-expiry"]) == pytest.approx(
        1_000_000.0 + 2 + 43200
    )
    assert "(sandbox)" in capsys.readouterr().out


def test_device_code_request_failure_exits_1(env):
    env["post"].side_effect = requests.ConnectionError("boom")

    with pytest.raises(SystemExit) as exc:
        allegro_auth_cmd.run(_args())

    assert exc.value.code == 1
    assert env["saved"] == {}


def test_device_code_http_error_exits_1(env):
    bad = _response(500)
    bad.raise_for_status.side_effect = requests.HTTPError("500")
    env["post"].side_effect = [bad]

    with pytest.raises(SystemExit) as exc:
        allegro_auth_cmd.run(_args())

    assert exc.value.code == 1


def test_network_error_while_polling_is_retried(env):
    env["post"].side_effect = [
        _response(200, _DEVICE_PAYLOAD),
        requests.Timeout("slow"),
        _response(200, _TOKEN_PAYLOAD),
    ]

    allegro_auth_cmd.run(_args())

    assert env["saved"]["allegro-refresh-token"] == "rt-xyz"
    assert env["post"].call_count == 3


def test_slow_down_increases_poll_interval(env):
    env["post"].side_effect = [
        _response(200, _DEVICE_PAYLOAD),
        _response(400, {"error": "slow_down"}),
        _response(200, _TOKEN_PAYLOAD),
    ]

    allegro_auth_cmd.run(_args())

    assert env["clock"].sleeps == [2, 7]


@pytest.mark.parametrize("error_code", ["expired_token", "access_denied"])
def test_terminal_errors_exit_1(env, capsys, error_code):
    env["post"].side_effect = [
        _response(200, _DEVICE_PAYLOAD),
        _response(400, {"error": error_code}),
    ]

    with pytest.raises(SystemExit) as exc:
        allegro_auth_cmd.run(_args())

    assert exc.value.code == 1
    assert f"Authorization failed: {error_code}" in capsys.readouterr().out
    assert env["saved"] == {}


def test_unexpected_response_with_unparseable_body_exits_1(env, capsys):
    env["post"].side_effect = [
        _response(200, _DEVICE_PAYLOAD),
        _response(502, json_exc=ValueError("not json")),
    ]

    with pytest.raises(SystemExit) as exc:
        allegro_auth_cmd.run(_args())

    assert exc.value.code == 1
    assert "Unexpected response 502: {}" in capsys.readouterr().out


def test_device_code_expires_before_approval_exits_1(env, capsys):
    payload = {k: v for k, v in _DEVICE_PAYLOAD.items() if k not in ("interval", "expires_in")}
    pending = _response(400, {"error": "authorization_pending"})
    env["post"].side_effect = [_response(200, payload)] + [pending] * 200

    with pytest.raises(SystemExit) as exc:
        allegro_auth_cmd.run(_args())

    assert exc.value.code == 1
    # Defaults: interval=5s, expires_in=600s -> 120 polls.
    assert env["clock"].sleeps == [5] * 120
    out = capsys.readouterr().out
    assert "expires in 600s" in out
    assert "Device code expired before authorization was granted." in out
    assert env["saved"] == {}


def test_add_subparser_registers_command():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    allegro_auth_cmd.add_subparser(sub)

    ns = parser.parse_args(["allegro-auth"])
    assert ns.sandbox is False
    assert ns.func is allegro_auth_cmd.run

    ns = parser.parse_args(["allegro-auth", "--sandbox"])
    assert ns.sandbox is True
