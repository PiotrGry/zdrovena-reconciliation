"""
zdrovena.api.auth – JWT authentication via Azure Entra ID
===========================================================

In production:
  - Azure Entra ID External Identities issues JWT tokens (passkey or email OTP)
  - Tokens are validated against the JWKS endpoint of the configured tenant
  - App roles (zdrovena-admin, zdrovena-viewer, zdrovena-accountant) are in the
    token's ``roles`` claim

Local dev / tests (AZURE_AUTH_DISABLED=true):
  - JWT validation is skipped entirely
  - A static fake principal is injected so all endpoints work without Azure
  - Never enable this in production

Required env vars (production):
  AZURE_TENANT_ID    — Entra ID tenant UUID
  AZURE_API_AUDIENCE — App registration client_id used as JWT audience.
                       Renamed from AZURE_CLIENT_ID to avoid conflict with
                       azure-identity's DefaultAzureCredential, which treats
                       AZURE_CLIENT_ID as the Managed Identity client_id
                       and hangs trying to fetch a token for it.

App roles:
  zdrovena-admin        — full access
  zdrovena-viewer       — read-only (list, download)
  zdrovena-accountant   — close + download
  zdrovena-shipment-mgr — execute shipping actions (create drafts, order pickup)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("zdrovena.api.auth")

_bearer = HTTPBearer(auto_error=False)


# ── Principal ─────────────────────────────────────────────────────────────────


@dataclass
class Principal:
    sub: str
    email: str
    roles: list[str] = field(default_factory=list)
    name: str = ""

    def has_role(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)

    def require_role(self, *roles: str) -> None:
        if not self.has_role(*roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required role(s): {list(roles)}",
            )


_DEV_PRINCIPAL = Principal(
    sub="dev-local",
    email="dev@localhost",
    roles=["zdrovena-admin"],
    name="Local Dev",
)


# ── JWKS / token validation ───────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _jwks_uri() -> str:
    tenant = os.environ["AZURE_TENANT_ID"]
    return f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"


def _validate_token(token: str) -> Principal:
    """Validate a JWT against Entra ID JWKS and return Principal."""
    try:
        import jwt as _jwt
        from jwt import InvalidTokenError, PyJWKClient
    except ImportError as exc:
        raise RuntimeError(
            "PyJWT not installed. Install with: pip install zdrovena-reconciliation[api]"
        ) from exc

    try:
        jwks_client = PyJWKClient(_jwks_uri(), cache_keys=True, timeout=5)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
    except Exception as exc:
        logger.error("Failed to fetch JWKS or find signing key: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service unavailable",
        ) from exc

    # Accept both v1 (aud=<guid>) and v2 (aud=api://<guid>) tokens — validate manually.
    # AZURE_API_AUDIENCE preferred; AZURE_CLIENT_ID kept for backwards compatibility
    # (azure-identity reserves AZURE_CLIENT_ID for managed identity, hence the rename).
    client_id = os.environ.get("AZURE_API_AUDIENCE") or os.environ.get("AZURE_CLIENT_ID", "")
    tenant_id = os.environ.get("AZURE_TENANT_ID", "")

    try:
        claims = _jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},  # audience validated manually below
        )
    except InvalidTokenError as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Issuer validation — reject tokens from other tenants (code-review finding)
    if tenant_id:
        valid_issuers = {
            f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            f"https://sts.windows.net/{tenant_id}/",
        }
        token_iss = claims.get("iss", "")
        if token_iss not in valid_issuers:
            logger.warning("Token issuer mismatch: got %r", token_iss)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token issuer",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Audience validation — reject tokens not intended for this API
    if client_id:
        token_aud = claims.get("aud")
        if token_aud not in (client_id, f"api://{client_id}"):
            logger.warning(
                "Token aud mismatch: got %r, expected %r or %r",
                token_aud,
                client_id,
                f"api://{client_id}",
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token audience",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return Principal(
        sub=claims.get("sub", ""),
        email=claims.get("preferred_username") or claims.get("email", ""),
        roles=claims.get("roles", []),
        name=claims.get("name", ""),
    )


# ── FastAPI dependency ────────────────────────────────────────────────────────


def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Resolve the calling principal from the Bearer token.

    Set AZURE_AUTH_DISABLED=true to bypass validation in local dev / tests.
    """
    if os.environ.get("AZURE_AUTH_DISABLED", "").lower() in ("1", "true", "yes"):
        logger.debug("Auth disabled — returning dev principal")
        return _DEV_PRINCIPAL

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _validate_token(credentials.credentials)


# ── Role shortcuts ─────────────────────────────────────────────────────────────


def require_admin(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    principal.require_role("zdrovena-admin")
    return principal


def require_accountant_or_admin(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    principal.require_role("zdrovena-admin", "zdrovena-accountant")
    return principal


def require_viewer_or_above(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    principal.require_role("zdrovena-admin", "zdrovena-accountant", "zdrovena-viewer")
    return principal


def require_shipment_mgr_or_above(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    principal.require_role("zdrovena-admin", "zdrovena-shipment-mgr")
    return principal
