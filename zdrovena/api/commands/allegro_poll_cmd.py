"""zdrovena.api.commands.allegro_poll_cmd — run one Allegro polling cycle.

Intended for use as a scheduled Azure Container App Job:
    zdrovena allegro-poll

Bootstraps AllegroClient, ShippingStore, StorageService, and FakturowniaClient
from env vars / Key Vault secrets, then delegates to poll_orders_once() which
handles idempotency and invoice creation.

Exit codes:
    0  — cycle completed (even if fetched=0 or some individual orders errored)
    1  — fatal: missing required credentials or unexpected top-level exception
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logger = logging.getLogger("zdrovena.api.commands.allegro_poll")


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
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor

            configure_azure_monitor()
        except Exception as exc:
            logger.warning("Azure Monitor configuration failed (non-fatal): %s", exc)


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
        logger.critical(
            "Missing Fakturownia credentials (%s). Automatic Allegro invoicing cannot run.",
            KEYCHAIN_SERVICE_FAKTUROWNIA,
        )
        sys.exit(1)
    base_url = os.environ.get("FAKTUROWNIA_BASE_URL", "").strip()
    if not base_url:
        base_url = f"https://{DEFAULT_DOMAIN}"
    return FakturowniaClient(base_url=base_url, api_token=api_token)


def run(args: argparse.Namespace) -> None:
    _setup_logging()

    from zdrovena.api.routers.allegro_poller import poll_orders_once
    from zdrovena.common.shipping_store import get_shipping_store
    from zdrovena.common.storage import get_storage_service

    allegro_client = _build_allegro_client()
    fakturownia_client = _build_fakturownia_client()

    try:
        shipping_store = get_shipping_store()
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


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "allegro-poll",
        help="Run one Allegro order polling cycle (fetch new orders, create drafts and Fakturownia invoices).",
    )
    p.set_defaults(func=run)
