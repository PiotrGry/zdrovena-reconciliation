"""
zdrovena.api.deps – FastAPI dependency injection
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from zdrovena.common.shipping_store import ShippingStore, get_shipping_store
from zdrovena.common.shopify_dedup_store import ShopifyDedupStore, get_shopify_dedup_store
from zdrovena.common.storage import StorageService, get_storage_service


@lru_cache(maxsize=1)
def _storage_singleton() -> StorageService:
    return get_storage_service()


def get_storage() -> StorageService:
    """Return the application-wide StorageService instance."""
    return _storage_singleton()


StorageDep = Annotated[StorageService, Depends(get_storage)]


@lru_cache(maxsize=1)
def _shipping_store_singleton() -> ShippingStore:
    return get_shipping_store()


def get_shipping() -> ShippingStore:
    """Return the application-wide ShippingStore instance."""
    return _shipping_store_singleton()


ShippingStoreDep = Annotated[ShippingStore, Depends(get_shipping)]


@lru_cache(maxsize=1)
def _shopify_dedup_singleton() -> ShopifyDedupStore:
    return get_shopify_dedup_store()


def get_shopify_dedup() -> ShopifyDedupStore:
    """Return the application-wide ShopifyDedupStore instance."""
    return _shopify_dedup_singleton()


ShopifyDedupStoreDep = Annotated[ShopifyDedupStore, Depends(get_shopify_dedup)]
