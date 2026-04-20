"""
zdrovena.api.deps – FastAPI dependency injection
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from zdrovena.common.storage import StorageService, get_storage_service


@lru_cache(maxsize=1)
def _storage_singleton() -> StorageService:
    return get_storage_service()


def get_storage() -> StorageService:
    """Return the application-wide StorageService instance."""
    return _storage_singleton()


StorageDep = Annotated[StorageService, Depends(get_storage)]
