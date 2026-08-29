"""Deprecated shim. The shipping API now lives in ``routers.shipping`` (#313).

This module used to hold every shipping endpoint in one 2000-line file. It is
kept only so ``main.py`` and any external import of ``webhooks.router`` keep
working; there is no logic here and nothing new should be added.

Import the specific router instead:

    from zdrovena.api.routers.shipping import execution, labels
"""

from zdrovena.api.routers.shipping import router

__all__ = ["router"]
