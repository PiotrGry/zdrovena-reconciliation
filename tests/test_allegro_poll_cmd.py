from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.api.commands import allegro_poll_cmd


def test_build_fakturownia_client_uses_configured_base_url(monkeypatch):
    monkeypatch.setenv("FAKTUROWNIA_BASE_URL", "https://invoices.example.test/")

    with patch("zdrovena.common.secrets.get_secret", return_value="secret-token"):
        client = allegro_poll_cmd._build_fakturownia_client()

    assert client.base_url == "https://invoices.example.test"
    assert client.api_token == "secret-token"


def test_build_fakturownia_client_fails_loudly_without_credentials():
    with patch("zdrovena.common.secrets.get_secret", return_value=None):
        with pytest.raises(SystemExit) as exc_info:
            allegro_poll_cmd._build_fakturownia_client()

    assert exc_info.value.code == 1


def test_run_wires_fakturownia_into_scheduled_poller():
    allegro_client = MagicMock(name="allegro_client")
    fakturownia_client = MagicMock(name="fakturownia_client")
    shipping_store = MagicMock(name="shipping_store")
    storage = MagicMock(name="storage")

    with (
        patch.object(allegro_poll_cmd, "_setup_logging"),
        patch.object(allegro_poll_cmd, "_build_allegro_client", return_value=allegro_client),
        patch.object(
            allegro_poll_cmd,
            "_build_fakturownia_client",
            return_value=fakturownia_client,
        ),
        patch("zdrovena.common.shipping_store.get_shipping_store", return_value=shipping_store),
        patch("zdrovena.common.storage.get_storage_service", return_value=storage),
        patch(
            "zdrovena.api.routers.allegro_poller.poll_orders_once",
            return_value={"fetched": 0},
        ) as poll,
    ):
        allegro_poll_cmd.run(argparse.Namespace())

    poll.assert_called_once_with(
        client=allegro_client,
        shipping_store=shipping_store,
        storage=storage,
        fakturownia_client=fakturownia_client,
    )
