from __future__ import annotations

import argparse
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.api.commands import allegro_poll_cmd


def test_no_tracking_snapshot_uses_exact_48h_and_current_draft_state():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    store = MagicMock()
    store.list_drafts.return_value = [
        {
            "id": "exactly-48h",
            "created_at": "2026-07-30T12:00:00Z",
            "status": "pending",
            "tracking_number": None,
        },
        {
            "id": "older",
            "created_at": "2026-07-29T10:00:00+00:00",
            "status": "error",
            "tracking_number": "",
        },
        {
            "id": "too-young",
            "created_at": "2026-07-30T12:01:00Z",
            "status": "pending",
            "tracking_number": None,
        },
        {
            "id": "tracked",
            "created_at": "2026-07-20T12:00:00Z",
            "status": "created",
            "tracking_number": "TRK-1",
        },
        {
            "id": "cancelled",
            "created_at": "2026-07-20T12:00:00Z",
            "status": "cancelled",
            "tracking_number": None,
        },
        {
            "id": "fulfilled",
            "created_at": "2026-07-20T12:00:00Z",
            "status": "created",
            "fulfillment_status": "fulfilled",
            "tracking_number": None,
        },
    ]

    with patch("zdrovena.common.events.log_event") as event:
        count = allegro_poll_cmd._emit_orders_without_tracking_snapshot(store, now=now)

    assert count == 2
    event.assert_called_once_with(
        "shipping.orders_without_tracking_snapshot",
        overdue_count=2,
        draft_ids=["older", "exactly-48h"],
        oldest_age_hours=74.0,
        threshold_hours=48,
        snapshot_truncated=False,
    )


def test_build_fakturownia_client_uses_configured_base_url(monkeypatch):
    monkeypatch.setenv("FAKTUROWNIA_BASE_URL", "https://invoices.example.test/")

    with patch("zdrovena.common.secrets.get_secret", return_value="secret-token"):
        client = allegro_poll_cmd._build_fakturownia_client()

    assert client.base_url == "https://invoices.example.test"
    assert client.api_token == "secret-token"


def test_build_fakturownia_client_returns_none_without_credentials():
    with patch("zdrovena.common.secrets.get_secret", return_value=None):
        client = allegro_poll_cmd._build_fakturownia_client()

    assert client is None


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


def test_run_still_polls_orders_without_fakturownia_credentials():
    allegro_client = MagicMock(name="allegro_client")
    shipping_store = MagicMock(name="shipping_store")
    storage = MagicMock(name="storage")

    with (
        patch.object(allegro_poll_cmd, "_setup_logging"),
        patch.object(allegro_poll_cmd, "_build_allegro_client", return_value=allegro_client),
        patch.object(allegro_poll_cmd, "_build_fakturownia_client", return_value=None),
        patch("zdrovena.common.shipping_store.get_shipping_store", return_value=shipping_store),
        patch("zdrovena.common.storage.get_storage_service", return_value=storage),
        patch(
            "zdrovena.api.routers.allegro_poller.poll_orders_once",
            return_value={"fetched": 1, "created": 1},
        ) as poll,
    ):
        allegro_poll_cmd.run(argparse.Namespace())

    poll.assert_called_once_with(
        client=allegro_client,
        shipping_store=shipping_store,
        storage=storage,
        fakturownia_client=None,
    )


def test_run_flushes_telemetry_when_cycle_exits_with_error():
    with (
        patch.object(allegro_poll_cmd, "_setup_logging"),
        patch.object(allegro_poll_cmd, "_run_cycle", side_effect=SystemExit(1)),
        patch("zdrovena.common.telemetry.force_flush_azure_telemetry") as flush,
        pytest.raises(SystemExit, match="1"),
    ):
        allegro_poll_cmd.run(argparse.Namespace())

    flush.assert_called_once_with()
