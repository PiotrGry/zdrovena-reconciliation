"""zdrovena.api.commands.allegro_poll_cmd — run one Allegro polling cycle.

Intended for use as a scheduled Azure Container App Job:
    zdrovena allegro-poll

Bootstraps the required AllegroClient, ShippingStore and StorageService plus an
optional FakturowniaClient from env vars / Key Vault secrets, then delegates to
poll_orders_once() which handles idempotency and invoice creation when possible.

Exit codes:
    0  — cycle completed (even if fetched=0 or some individual orders errored)
    1  — fatal: missing required Allegro credentials or unexpected top-level exception
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("zdrovena.api.commands.allegro_poll")

_TRACKING_OVERDUE_HOURS = 48


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _emit_orders_without_tracking_snapshot(
    shipping_store: Any, *, now: datetime | None = None
) -> int:
    """Emit the current count of actionable drafts untracked for at least 48h.

    Azure log alerts can query at most two days of history, so deriving this
    state by joining historical ``draft.created`` events loses the record at
    exactly the threshold and cannot implement true 48-hour semantics. The
    scheduled poller already reads the authoritative draft store; publishing a
    small current-state snapshot makes the alert exact and rollout-safe.
    """
    try:
        drafts = shipping_store.list_drafts()
    except Exception:
        logger.exception("Failed to read drafts for the no-tracking snapshot")
        return 0

    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    overdue: list[tuple[str, float]] = []
    for draft in drafts:
        if draft.get("tracking_number"):
            continue
        if draft.get("status") == "cancelled" or draft.get("fulfillment_status") == "fulfilled":
            continue
        created_at = _parse_utc_timestamp(draft.get("created_at"))
        if created_at is None:
            logger.warning("Draft %s has no valid created_at", draft.get("id"))
            continue
        age_hours = (observed_at - created_at).total_seconds() / 3600
        if age_hours >= _TRACKING_OVERDUE_HOURS:
            overdue.append((str(draft.get("id") or ""), age_hours))

    from zdrovena.common.events import log_event

    overdue.sort(key=lambda item: item[1], reverse=True)
    log_event(
        "shipping.orders_without_tracking_snapshot",
        overdue_count=len(overdue),
        draft_ids=[draft_id for draft_id, _age in overdue[:50]],
        oldest_age_hours=round(overdue[0][1], 1) if overdue else 0,
        threshold_hours=_TRACKING_OVERDUE_HOURS,
        snapshot_truncated=len(overdue) > 50,
    )
    return len(overdue)


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    _azure_log_level = os.environ.get("LOG_LEVEL_AZURE", "WARNING").upper()
    for _name in (
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
        "azure.storage",
        "azure.data.tables",
        "azure.monitor.opentelemetry",
    ):
        logging.getLogger(_name).setLevel(_azure_log_level)

    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        from zdrovena.common.telemetry import configure_azure_telemetry

        configure_azure_telemetry(default_service_name="zdrovena-allegro-poller")


def _build_allegro_client():
    from zdrovena.common.allegro import AllegroClient, SecretsAllegroTokenStore
    from zdrovena.common.secrets import get_secret

    client_id = get_secret("allegro-client-id", required=False)
    client_secret = get_secret("allegro-client-secret", required=False)
    refresh_token = get_secret("allegro-refresh-token", required=False)
    if not (client_id and client_secret and refresh_token):
        logger.critical(
            "Missing Allegro credentials (allegro-client-id / allegro-client-secret / allegro-refresh-token). "
            "Check Key Vault or env vars."
        )
        sys.exit(1)
    return AllegroClient(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        env=os.environ.get("ALLEGRO_ENV", "prod"),
        token_store=SecretsAllegroTokenStore(),
    )


def _build_fakturownia_client():
    from zdrovena.common.config import DEFAULT_DOMAIN, KEYCHAIN_SERVICE_FAKTUROWNIA
    from zdrovena.common.fakturownia import FakturowniaClient
    from zdrovena.common.secrets import get_secret

    api_token = get_secret(KEYCHAIN_SERVICE_FAKTUROWNIA, required=False)
    if not api_token:
        logger.error(
            "Missing Fakturownia credentials (%s). Order ingestion will continue without "
            "automatic invoicing.",
            KEYCHAIN_SERVICE_FAKTUROWNIA,
        )
        return None
    base_url = os.environ.get("FAKTUROWNIA_BASE_URL", "").strip()
    if not base_url:
        base_url = f"https://{DEFAULT_DOMAIN}"
    return FakturowniaClient(base_url=base_url, api_token=api_token)


def _run_cycle(args: argparse.Namespace) -> None:
    from zdrovena.api.damage_detection import (
        build_apaczka_lookup_client,
        build_inpost_lookup_client,
        build_zoho_client,
        scan_allegro_damage_cases,
        scan_zoho_damage_cases,
    )
    from zdrovena.api.routers.allegro_poller import poll_orders_once
    from zdrovena.common.damage_store import get_damage_store
    from zdrovena.common.shipping_store import get_shipping_store
    from zdrovena.common.storage import get_storage_service

    allegro_client = _build_allegro_client()
    fakturownia_client = _build_fakturownia_client()

    try:
        shipping_store = get_shipping_store()
        damage_store = get_damage_store()
        storage = get_storage_service()
    except Exception as exc:
        logger.critical("Failed to initialise storage dependencies: %s", exc)
        sys.exit(1)

    logger.info("Starting Allegro polling cycle.")
    try:
        stats = poll_orders_once(
            client=allegro_client,
            shipping_store=shipping_store,
            storage=storage,
            fakturownia_client=fakturownia_client,
        )
    except Exception as exc:
        logger.exception("Unexpected error during polling cycle: %s", exc)
        sys.exit(1)

    logger.info("Polling cycle complete: %s", stats)

    # ShipX issues a waybill after the POST returns, so InPost drafts park at
    # pending_confirmation. The browser poll only runs while the shipping page
    # is open; this is what resolves them when nobody is watching. A failure
    # here must not invalidate the completed order/invoice cycle above.
    try:
        from zdrovena.api.routers.inpost_poller import resolve_pending_inpost_once

        inpost_stats = resolve_pending_inpost_once(shipping_store=shipping_store)
        logger.info("InPost pending resolution complete: %s", inpost_stats)
    except Exception:
        logger.exception("InPost pending resolution failed")

    overdue_count = _emit_orders_without_tracking_snapshot(shipping_store)
    logger.info("Orders without tracking after 48h: %d", overdue_count)

    # Detection only: create manual-review cases, never replacement shipments
    # and never customer emails. A detection failure must not invalidate the
    # already completed order/invoice polling cycle.
    try:
        allegro_damage = scan_allegro_damage_cases(
            client=allegro_client,
            shipping_store=shipping_store,
            damage_store=damage_store,
        )
        logger.info("Allegro damage scan complete: %s", allegro_damage)
    except Exception:
        logger.exception("Allegro damage scan failed")

    try:
        zoho_client = build_zoho_client()
        if zoho_client is None:
            logger.warning("Zoho damage scan skipped: OAuth credentials are missing")
        else:
            zoho_damage = scan_zoho_damage_cases(
                client=zoho_client,
                shipping_store=shipping_store,
                damage_store=damage_store,
                inpost_client=build_inpost_lookup_client(),
                apaczka_client=build_apaczka_lookup_client(storage),
            )
            logger.info("Zoho damage scan complete: %s", zoho_damage)
    except Exception:
        logger.exception("Zoho damage scan failed")


def run(args: argparse.Namespace) -> None:
    """Uruchom cykl i zawsze opróżnij telemetrykę krótkotrwałego joba."""

    _setup_logging()
    try:
        _run_cycle(args)
    finally:
        from zdrovena.common.telemetry import force_flush_azure_telemetry

        force_flush_azure_telemetry()


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "allegro-poll",
        help=(
            "Run one Allegro order polling cycle (fetch new orders, create drafts and, "
            "when configured, Fakturownia invoices)."
        ),
    )
    p.set_defaults(func=run)
