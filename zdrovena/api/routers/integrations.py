"""Environment and integration health endpoints."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Annotated, Literal

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from zdrovena.api.auth import Principal, require_viewer_or_above
from zdrovena.api.deps import ShippingStoreDep, StorageDep
from zdrovena.common.appenv import UNKNOWN_ENV, resolve_app_env
from zdrovena.common.correlation import get_correlation_id

router = APIRouter(prefix="/integrations", tags=["integrations"])

IntegrationStatus = Literal["healthy", "degraded", "unavailable", "not_configured"]
API_VERSION = "2.0.0"


class IntegrationHealthItem(BaseModel):
    key: str
    name: str
    status: IntegrationStatus
    checked_at: str
    latency_ms: int
    environment: str
    mode: str
    safe_operation: str
    message: str
    detail: str
    checks: list[str] = Field(default_factory=list)
    correlation_id: str | None = None


class OperationHealthItem(BaseModel):
    key: str
    name: str
    status: IntegrationStatus
    checked_at: str
    message: str
    metrics: dict[str, int | str | None] = Field(default_factory=dict)


class EnvironmentHealth(BaseModel):
    app_env: str
    auth_disabled: bool
    keyvault_configured: bool
    storage_backend: str
    version: str


class IntegrationsHealthResponse(BaseModel):
    environment: EnvironmentHealth
    integrations: list[IntegrationHealthItem]
    operations: list[OperationHealthItem]


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _truthy(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes"}


def _secret_source(*env_names: str) -> tuple[bool, list[str]]:
    missing = [name for name in env_names if not _env(name)]
    if not missing:
        return True, ["configured from environment"]
    if _env("AZURE_KEYVAULT_URL"):
        return True, ["Azure Key Vault configured", f"not set as env: {', '.join(missing)}"]
    return False, [f"missing: {', '.join(missing)}"]


def _storage_backend() -> str:
    if _env("AZURE_STORAGE_ACCOUNT_URL"):
        return "azure-identity"
    if _env("AZURE_STORAGE_CONNECTION_STRING"):
        conn = _env("AZURE_STORAGE_CONNECTION_STRING").lower()
        return "azurite" if "usedevelopmentstorage=true" in conn else "connection-string"
    return "local-filesystem"


def _item(
    *,
    key: str,
    name: str,
    status: IntegrationStatus,
    checked_at: str,
    started_at: float,
    environment: str,
    mode: str,
    safe_operation: str,
    message: str,
    checks: list[str] | None = None,
    correlation_id: str | None = None,
) -> IntegrationHealthItem:
    return IntegrationHealthItem(
        key=key,
        name=name,
        status=status,
        checked_at=checked_at,
        latency_ms=max(0, round((time.perf_counter() - started_at) * 1000)),
        environment=environment,
        mode=mode,
        safe_operation=safe_operation,
        message=message,
        detail=message,
        checks=checks or [],
        correlation_id=correlation_id,
    )


def _failure_item(
    *,
    key: str,
    name: str,
    checked_at: str,
    started_at: float,
    environment: str,
    mode: str,
    safe_operation: str,
    exc: Exception,
) -> IntegrationHealthItem:
    return _item(
        key=key,
        name=name,
        status="unavailable",
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode=mode,
        safe_operation=safe_operation,
        message=f"{type(exc).__name__}: health check failed",
        checks=["safe failure; provider response not exposed"],
        correlation_id=get_correlation_id(),
    )


def _auth_item(*, checked_at: str, environment: str, run_checks: bool) -> IntegrationHealthItem:
    started_at = time.perf_counter()
    auth_disabled = _truthy("AZURE_AUTH_DISABLED")
    missing = [name for name in ("AZURE_TENANT_ID", "AZURE_API_AUDIENCE") if not _env(name)]
    if auth_disabled:
        return _item(
            key="auth",
            name="Azure Entra ID",
            status="degraded",
            checked_at=checked_at,
            started_at=started_at,
            environment=environment,
            mode="development",
            safe_operation="configuration inspection",
            message="Auth disabled",
            checks=["AZURE_AUTH_DISABLED=true"],
        )
    if missing:
        return _item(
            key="auth",
            name="Azure Entra ID",
            status="unavailable",
            checked_at=checked_at,
            started_at=started_at,
            environment=environment,
            mode="live",
            safe_operation="configuration inspection",
            message="Configuration incomplete",
            checks=[f"missing: {', '.join(missing)}"],
        )
    return _item(
        key="auth",
        name="Azure Entra ID",
        status="healthy",
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode="live",
        safe_operation="configuration inspection"
        if not run_checks
        else "current token accepted by API",
        message="JWT validation configured",
        checks=["tenant and API audience configured"],
    )


def _storage_item(
    *, checked_at: str, environment: str, run_checks: bool, storage: object
) -> IntegrationHealthItem:
    started_at = time.perf_counter()
    backend = _storage_backend()
    checks = [f"backend: {backend}", f"container: {_env('AZURE_STORAGE_CONTAINER') or 'default'}"]
    if run_checks:
        try:
            count = len(storage.list_files(""))  # type: ignore[attr-defined]
            checks.append(f"metadata/list read ok: {count} visible entries")
        except Exception as exc:
            return _failure_item(
                key="storage",
                name="Azure Blob Storage",
                checked_at=checked_at,
                started_at=started_at,
                environment=environment,
                mode=backend,
                safe_operation="list files metadata",
                exc=exc,
            )
    return _item(
        key="storage",
        name="Azure Blob Storage",
        status="healthy" if backend != "local-filesystem" else "degraded",
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode=backend,
        safe_operation="configuration inspection" if not run_checks else "list files metadata",
        message="Storage backend selected" if not run_checks else "Storage metadata read succeeded",
        checks=checks,
    )


def _keyvault_item(*, checked_at: str, environment: str, run_checks: bool) -> IntegrationHealthItem:
    started_at = time.perf_counter()
    configured = bool(_env("AZURE_KEYVAULT_URL"))
    checks = (
        ["startup ping runs when auth is enabled"]
        if configured
        else ["secrets must come from env/local fallback"]
    )
    if run_checks and configured:
        try:
            from zdrovena.common.secrets import get_secret

            required_names = (
                "fakturownia_api_token",
                "allegro-client-id",
                "allegro-client-secret",
                "allegro-refresh-token",
                "inpost-api-token",
                "inpost-organization-id",
                "apaczka-app-id",
                "apaczka-app-secret",
            )
            present = sum(1 for name in required_names if get_secret(name, required=False))
            checks = [f"secret name resolution ok: {present}/{len(required_names)} present"]
        except Exception as exc:
            return _failure_item(
                key="keyvault",
                name="Azure Key Vault",
                checked_at=checked_at,
                started_at=started_at,
                environment=environment,
                mode="live",
                safe_operation="resolve required secret names",
                exc=exc,
            )
    return _item(
        key="keyvault",
        name="Azure Key Vault",
        status="healthy" if configured else "not_configured",
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode="live" if configured else "not_configured",
        safe_operation="configuration inspection"
        if not run_checks
        else "resolve required secret names",
        message="Configured" if configured else "Not configured",
        checks=checks,
    )


def _shopify_item(app_env: str, *, checked_at: str, environment: str) -> IntegrationHealthItem:
    started_at = time.perf_counter()
    allowed = _env("SHOPIFY_ALLOWED_DOMAINS")
    if app_env == "production" and not allowed:
        result: IntegrationStatus = "unavailable"
        message = "Domain allow-list missing"
    elif allowed:
        result = "healthy"
        message = "Domain allow-list configured"
    else:
        result = "degraded"
        message = "Domain allow-list open outside production"
    return _item(
        key="shopify",
        name="Shopify webhooks",
        status=result,
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode="webhook",
        safe_operation="configuration inspection",
        message=message,
        checks=[f"allowed domains: {allowed or '*'}"],
    )


def _fakturownia_item(
    *, checked_at: str, environment: str, run_checks: bool
) -> IntegrationHealthItem:
    started_at = time.perf_counter()
    if _truthy("FAKTUROWNIA_DISABLED"):
        return _item(
            key="fakturownia",
            name="Fakturownia",
            status="not_configured",
            checked_at=checked_at,
            started_at=started_at,
            environment=environment,
            mode="not_configured",
            safe_operation="configuration inspection",
            message="Disabled by configuration",
            checks=["FAKTUROWNIA_DISABLED=true"],
        )
    configured, checks = _secret_source("FAKTUROWNIA_API_TOKEN")
    result: IntegrationStatus = "healthy" if configured else "not_configured"
    operation = "configuration inspection"
    message = "API token available" if result == "healthy" else "API token not confirmed"
    if run_checks and configured:
        operation = "list invoices page 1"
        try:
            from zdrovena.common.config import DEFAULT_DOMAIN
            from zdrovena.common.fakturownia import FakturowniaClient

            base_url = _env("FAKTUROWNIA_BASE_URL") or f"https://{DEFAULT_DOMAIN}"
            client = FakturowniaClient(base_url=base_url, api_token=_env("FAKTUROWNIA_API_TOKEN"))
            invoices = client.list_invoices(page=1, per_page=1, include_positions=False)
            checks.append(f"read ok: {len(invoices)} invoice sample entries")
            message = "Read-only invoice list check succeeded"
        except Exception as exc:
            return _failure_item(
                key="fakturownia",
                name="Fakturownia",
                checked_at=checked_at,
                started_at=started_at,
                environment=environment,
                mode=_env("FAKTUROWNIA_BASE_URL") or "default-domain",
                safe_operation=operation,
                exc=exc,
            )
    return _item(
        key="fakturownia",
        name="Fakturownia",
        status=result,
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode=_env("FAKTUROWNIA_BASE_URL") or "default-domain",
        safe_operation=operation,
        message=message,
        checks=checks,
    )


def _allegro_item(*, checked_at: str, environment: str, run_checks: bool) -> IntegrationHealthItem:
    started_at = time.perf_counter()
    configured, checks = _secret_source(
        "ALLEGRO_CLIENT_ID",
        "ALLEGRO_CLIENT_SECRET",
        "ALLEGRO_REFRESH_TOKEN",
    )
    result: IntegrationStatus = "healthy" if configured else "not_configured"
    operation = "configuration inspection"
    message = "OAuth configured" if result == "healthy" else "OAuth secrets not confirmed"
    mode = _env("ALLEGRO_ENV") or "prod"
    if run_checks and configured:
        operation = "list checkout forms page 1"
        try:
            from zdrovena.common.secrets import get_secret

            access_token = get_secret("allegro-access-token", required=False)
            expiry_raw = get_secret("allegro-access-token-expiry", required=False)
            if not access_token or not expiry_raw or float(expiry_raw) <= time.time():
                return _item(
                    key="allegro",
                    name="Allegro",
                    status="degraded",
                    checked_at=checked_at,
                    started_at=started_at,
                    environment=environment,
                    mode=mode,
                    safe_operation=operation,
                    message="Live check skipped: no valid cached access token; refresh token rotation is not safe in health checks",
                    checks=checks,
                )
            base_url = (
                "https://api.allegro.pl.allegrosandbox.pl"
                if mode == "sandbox"
                else "https://api.allegro.pl"
            )
            response = requests.get(
                f"{base_url}/order/checkout-forms",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.allegro.public.v1+json",
                },
                params={"limit": 1, "offset": 0},
                timeout=10,
            )
            if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                raise RuntimeError("cached access token rejected")
            if response.status_code >= HTTPStatus.BAD_REQUEST:
                raise RuntimeError(f"Allegro read failed with HTTP {response.status_code}")
            orders = (response.json() or {}).get("checkoutForms") or []
            checks.append(f"read ok: {len(orders)} checkout form sample entries")
            message = "Read-only checkout form list check succeeded"
        except Exception as exc:
            return _failure_item(
                key="allegro",
                name="Allegro",
                checked_at=checked_at,
                started_at=started_at,
                environment=environment,
                mode=mode,
                safe_operation=operation,
                exc=exc,
            )
    return _item(
        key="allegro",
        name="Allegro",
        status=result,
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode=mode,
        safe_operation=operation,
        message=message,
        checks=checks,
    )


def _inpost_item(*, checked_at: str, environment: str, run_checks: bool) -> IntegrationHealthItem:
    started_at = time.perf_counter()
    configured, checks = _secret_source("INPOST_API_TOKEN", "INPOST_ORGANIZATION_ID")
    result: IntegrationStatus = "healthy" if configured else "not_configured"
    operation = "configuration inspection"
    message = (
        "API token and organization configured"
        if result == "healthy"
        else "ShipX configuration incomplete"
    )
    mode = _env("INPOST_BASE_URL") or "production"
    if run_checks and configured:
        operation = "read organization metadata"
        try:
            from zdrovena.common.inpost import InPostClient

            client = InPostClient(
                api_token=_env("INPOST_API_TOKEN"),
                organization_id=_env("INPOST_ORGANIZATION_ID"),
            )
            org = client.get_organization()
            checks.append(f"read ok: organization id {org.get('id', 'confirmed')}")
            message = "Read-only organization check succeeded"
        except Exception as exc:
            return _failure_item(
                key="inpost",
                name="InPost ShipX",
                checked_at=checked_at,
                started_at=started_at,
                environment=environment,
                mode=mode,
                safe_operation=operation,
                exc=exc,
            )
    return _item(
        key="inpost",
        name="InPost ShipX",
        status=result,
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode=mode,
        safe_operation=operation,
        message=message,
        checks=checks,
    )


def _apaczka_item(
    *, checked_at: str, environment: str, run_checks: bool, storage: object
) -> IntegrationHealthItem:
    started_at = time.perf_counter()
    configured, checks = _secret_source("APACZKA_APP_ID", "APACZKA_APP_SECRET")
    result: IntegrationStatus = "healthy" if configured else "not_configured"
    operation = "configuration inspection"
    message = (
        "HMAC credentials configured" if result == "healthy" else "HMAC credentials not confirmed"
    )
    if run_checks and configured:
        operation = "read service catalogue"
        try:
            from zdrovena.common.apaczka import APACZKA_SERVICE_CATALOG, ApaczkaClient

            service_id = next(iter(APACZKA_SERVICE_CATALOG))
            client = ApaczkaClient(
                app_id=_env("APACZKA_APP_ID"),
                app_secret=_env("APACZKA_APP_SECRET"),
                service_id=service_id,
                storage=storage,
            )
            result_body = client._call("service_structure", {})
            services = result_body.get("response", {}).get("services", [])
            checks.append(f"read ok: {len(services)} service entries")
            message = "Read-only service catalogue check succeeded"
        except Exception as exc:
            return _failure_item(
                key="apaczka",
                name="Apaczka",
                checked_at=checked_at,
                started_at=started_at,
                environment=environment,
                mode="live",
                safe_operation=operation,
                exc=exc,
            )
    return _item(
        key="apaczka",
        name="Apaczka",
        status=result,
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode="live",
        safe_operation=operation,
        message=message,
        checks=checks,
    )


def _table_storage_item(
    *,
    checked_at: str,
    environment: str,
    run_checks: bool,
    shipping_store: object,
) -> IntegrationHealthItem:
    started_at = time.perf_counter()
    backend = (
        "azure-table"
        if _env("AZURE_STORAGE_ACCOUNT_URL") or _env("AZURE_STORAGE_CONNECTION_STRING")
        else "local-json"
    )
    checks = [f"backend: {backend}", "table: shippingdrafts"]
    if run_checks:
        try:
            drafts = shipping_store.list_drafts(limit=1)  # type: ignore[attr-defined]
            checks.append(f"read ok: {len(drafts)} draft sample entries")
        except Exception as exc:
            return _failure_item(
                key="table_storage",
                name="Azure Table Storage",
                checked_at=checked_at,
                started_at=started_at,
                environment=environment,
                mode=backend,
                safe_operation="list draft metadata",
                exc=exc,
            )
    return _item(
        key="table_storage",
        name="Azure Table Storage",
        status="healthy" if backend == "azure-table" else "degraded",
        checked_at=checked_at,
        started_at=started_at,
        environment=environment,
        mode=backend,
        safe_operation="configuration inspection" if not run_checks else "list draft metadata",
        message="Table storage backend selected"
        if not run_checks
        else "Table metadata read succeeded",
        checks=checks,
    )


def _operation_items(*, checked_at: str, shipping_store: object) -> list[OperationHealthItem]:
    dlq_metrics: dict[str, int | str | None] = {}
    dlq_status: IntegrationStatus = "not_configured"
    dlq_message = "No background retry worker summary is persisted yet"
    try:
        entries = shipping_store.list_dlq(limit=200)  # type: ignore[attr-defined]
        dlq_metrics = {
            "queued": len(entries),
            "retries_total": sum(int(entry.get("retries") or 0) for entry in entries),
        }
        dlq_status = "healthy" if not entries else "degraded"
        dlq_message = (
            "DLQ is empty" if not entries else "DLQ has entries waiting for operator retry"
        )
    except Exception:
        dlq_status = "unavailable"
        dlq_message = "DLQ summary read failed"
    return [
        OperationHealthItem(
            key="allegro_poll",
            name="Allegro polling",
            status="not_configured",
            checked_at=checked_at,
            message="Latest run summary is not persisted yet",
        ),
        OperationHealthItem(
            key="fakturownia_kaucja_patcher",
            name="Fakturownia kaucja patcher",
            status="not_configured",
            checked_at=checked_at,
            message="Latest run summary is not persisted yet",
        ),
        OperationHealthItem(
            key="invoice_push_flow",
            name="Invoice creation and push",
            status="not_configured",
            checked_at=checked_at,
            message="Latest run summary is not persisted yet",
        ),
        OperationHealthItem(
            key="dlq_retry",
            name="DLQ retry worker",
            status=dlq_status,
            checked_at=checked_at,
            message=dlq_message,
            metrics=dlq_metrics,
        ),
    ]


@router.get("/health", response_model=IntegrationsHealthResponse)
def integration_health(
    principal: Annotated[Principal, Depends(require_viewer_or_above)],
    storage: StorageDep,
    shipping_store: ShippingStoreDep,
    run_checks: Annotated[
        bool,
        Query(description="Reserved for admin-triggered live read-only checks."),
    ] = False,
) -> IntegrationsHealthResponse:
    if run_checks and not principal.has_role("zdrovena-admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manual integration checks require zdrovena-admin",
        )
    app_env = resolve_app_env()
    env_label = "unset" if app_env is None else ("unknown" if app_env == UNKNOWN_ENV else app_env)
    checked_at = datetime.now(timezone.utc).isoformat()

    return IntegrationsHealthResponse(
        environment=EnvironmentHealth(
            app_env=env_label,
            auth_disabled=_truthy("AZURE_AUTH_DISABLED"),
            keyvault_configured=bool(_env("AZURE_KEYVAULT_URL")),
            storage_backend=_storage_backend(),
            version=API_VERSION,
        ),
        integrations=[
            _auth_item(checked_at=checked_at, environment=env_label, run_checks=run_checks),
            _storage_item(
                checked_at=checked_at,
                environment=env_label,
                run_checks=run_checks,
                storage=storage,
            ),
            _table_storage_item(
                checked_at=checked_at,
                environment=env_label,
                run_checks=run_checks,
                shipping_store=shipping_store,
            ),
            _keyvault_item(checked_at=checked_at, environment=env_label, run_checks=run_checks),
            _shopify_item(env_label, checked_at=checked_at, environment=env_label),
            _fakturownia_item(checked_at=checked_at, environment=env_label, run_checks=run_checks),
            _allegro_item(checked_at=checked_at, environment=env_label, run_checks=run_checks),
            _inpost_item(checked_at=checked_at, environment=env_label, run_checks=run_checks),
            _apaczka_item(
                checked_at=checked_at,
                environment=env_label,
                run_checks=run_checks,
                storage=storage,
            ),
        ],
        operations=_operation_items(checked_at=checked_at, shipping_store=shipping_store),
    )
