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
  zdrovena-admin       — full access
  zdrovena-viewer      — read-only (list, download)
  zdrovena-accountant  — close + download
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
        from jose import JWTError, jwt
        from jose.backends import RSAKey  # noqa: F401 — ensures rsa support present
    except ImportError as exc:
        raise RuntimeError(
            "python-jose not installed. Install with: pip install zdrovena-reconciliation[api]"
        ) from exc

    try:
        import json as _json
        import urllib.request

        with urllib.request.urlopen(_jwks_uri(), timeout=5) as resp:  # nosec B310 — URL from OIDC config, not user input
            jwks = _json.loads(resp.read())
    except Exception as exc:
        logger.error("Failed to fetch JWKS: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service unavailable",
        ) from exc

    # python-jose's `audience` argument requires a single string at runtime
    # (despite type stubs claiming Optional[str|list]). To accept both v1-style
    # aud=<guid> and v2-style aud=api://<guid> tokens we skip the built-in
    # check and validate the audience claim manually below.
    # Read AZURE_API_AUDIENCE; fall back to AZURE_CLIENT_ID for backwards
    # compatibility during the rename rollout, but prefer the new name —
    # AZURE_CLIENT_ID is reserved by azure-identity for managed identity client_id.
    client_id = os.environ.get("AZURE_API_AUDIENCE") or os.environ.get("AZURE_CLIENT_ID", "")
    try:
        claims = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            options={"verify_exp": True, "verify_aud": False},
        )
    except JWTError as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

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
