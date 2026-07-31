"""Table clients must be built once per store instance, not per operation.

Each store method called `_table_client()`, which constructed a fresh
TableServiceClient, a fresh DefaultAzureCredential and issued
`create_table_if_not_exists` — a network round-trip returning 409 every time.
In production the Allegro poller alone burned ~373 of those calls and ~11.7k
managed-identity token fetches a day.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zdrovena.common.damage_store import DamageStore
from zdrovena.common.shipping_store import ShippingStore
from zdrovena.common.shopify_dedup_store import ShopifyDedupStore

_ACCOUNT_URL = "https://zdrovenafiles.blob.core.windows.net"


def _patched_sdk():
    """Patch the azure SDK symbols the stores import lazily inside the method."""
    svc = MagicMock(name="TableServiceClient")
    svc.create_table_if_not_exists.return_value = MagicMock(name="TableClient")
    tsc_cls = MagicMock(name="TableServiceClientClass", return_value=svc)
    cred_cls = MagicMock(name="DefaultAzureCredential")
    tables_mod = MagicMock(TableServiceClient=tsc_cls)
    identity_mod = MagicMock(DefaultAzureCredential=cred_cls)
    return (
        svc,
        tsc_cls,
        cred_cls,
        patch.dict(
            "sys.modules",
            {"azure.data.tables": tables_mod, "azure.identity": identity_mod},
        ),
    )


@pytest.mark.parametrize(
    ("store_cls", "method"),
    [
        (ShippingStore, "_table_client"),
        (DamageStore, "_table_client"),
        (ShopifyDedupStore, "_table_client"),
    ],
)
def test_table_client_built_once_per_instance(store_cls, method):
    svc, tsc_cls, cred_cls, sdk_patch = _patched_sdk()
    with sdk_patch:
        store = store_cls(account_url=_ACCOUNT_URL)
        first = getattr(store, method)()
        second = getattr(store, method)()
        third = getattr(store, method)()

    assert first is second is third, "the same TableClient must be reused"
    assert tsc_cls.call_count == 1, (
        f"TableServiceClient rebuilt {tsc_cls.call_count}x — expected 1 per store instance"
    )
    assert cred_cls.call_count == 1, (
        f"DefaultAzureCredential rebuilt {cred_cls.call_count}x — expected 1"
    )
    assert svc.create_table_if_not_exists.call_count == 1, (
        "create_table_if_not_exists must not be re-issued on every operation"
    )


def test_shipping_dlq_client_cached_separately_from_drafts():
    svc, _tsc_cls, _cred_cls, sdk_patch = _patched_sdk()
    drafts_client = MagicMock(name="draftsTable")
    dlq_client = MagicMock(name="dlqTable")
    svc.create_table_if_not_exists.side_effect = [drafts_client, dlq_client]

    with sdk_patch:
        store = ShippingStore(account_url=_ACCOUNT_URL)
        d1 = store._table_client()
        q1 = store._dlq_table_client()
        d2 = store._table_client()
        q2 = store._dlq_table_client()

    assert d1 is d2 and q1 is q2, "each table must keep its own cached client"
    assert d1 is not q1, "drafts and DLQ must not share a client"
    assert svc.create_table_if_not_exists.call_count == 2, "one call per distinct table"
