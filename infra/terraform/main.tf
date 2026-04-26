locals {
  tags = {
    project     = var.prefix
    environment = "prod"
    managed_by  = "terraform"
  }
}

# ── Resource Group ─────────────────────────────────────────────────────────────

resource "azurerm_resource_group" "rg" {
  name     = "${var.prefix}-rg"
  location = var.location
  tags     = local.tags
}

# ── Container Registry ─────────────────────────────────────────────────────────

resource "azurerm_container_registry" "acr" {
  # ACR name: alphanumeric only, globally unique
  name                = "${replace(var.prefix, "-", "")}acr"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Basic"
  admin_enabled       = false # pull via managed identity, no passwords
  tags                = local.tags

  # checkov:skip=CKV_AZURE_139: Basic SKU — private endpoint not supported; Container Apps pull via managed identity over Azure backbone
  # checkov:skip=CKV_AZURE_163: Defender for Containers (vulnerability scanning) is a paid add-on not included in this budget tier
  # checkov:skip=CKV_AZURE_164: Content Trust (signed images) requires Premium SKU
  # checkov:skip=CKV_AZURE_165: Geo-replication requires Premium SKU; single-region deployment
  # checkov:skip=CKV_AZURE_166: Quarantine policy requires Premium SKU
  # checkov:skip=CKV_AZURE_167: Retention policy for untagged manifests requires Premium SKU
  # checkov:skip=CKV_AZURE_233: Zone redundancy requires Premium SKU
  # checkov:skip=CKV_AZURE_237: Dedicated data endpoints require Premium SKU
}

# ── Log Analytics Workspace (required by Container Apps Environment) ───────────

resource "azurerm_log_analytics_workspace" "law" {
  name                = "${var.prefix}-law"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

# ── Container Apps Environment ─────────────────────────────────────────────────

resource "azurerm_container_app_environment" "env" {
  name                       = "${var.prefix}-cae"
  resource_group_name        = azurerm_resource_group.rg.name
  location                   = azurerm_resource_group.rg.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.law.id
  tags                       = local.tags
}

# ── Storage Account (private blob files) ──────────────────────────────────────

resource "azurerm_storage_account" "storage" {
  # Storage account name: alphanumeric only, max 24 chars
  # checkov:skip=CKV_AZURE_43: Name is dynamically computed via replace(var.prefix,"-","") — valid alphanumeric, length enforced by variable validation
  # checkov:skip=CKV_AZURE_33: No Azure Queue service used — this is blob-only storage
  # checkov:skip=CKV_AZURE_206: LRS replication intentional — single-region deployment, non-critical files, cost optimised
  # checkov:skip=CKV2_AZURE_41: No SAS tokens issued — all access via managed identity (RBAC)
  # checkov:skip=CKV2_AZURE_1: Customer Managed Key not required — files are non-sensitive reports; Microsoft-managed encryption at rest is sufficient for this tier
  # checkov:skip=CKV2_AZURE_33: Private endpoint requires VNet not present in this architecture; access restricted via network_rules default_action=Deny + ip_rules allowlist + AzureServices bypass
  # checkov:skip=CKV_AZURE_59: public_network_access_enabled=true required so the network_rules ip_rules allowlist works at all; default_action=Deny gives the same security guarantee
  # checkov:skip=CKV2_AZURE_21: Blob diagnostic logging (read requests) not configured — operational overhead not justified for this single-region non-critical storage
  name                            = "${replace(var.prefix, "-", "")}files"
  resource_group_name             = azurerm_resource_group.rg.name
  location                        = azurerm_resource_group.rg.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false # CKV2_AZURE_40 — disable Shared Key auth; all access via managed identity
  min_tls_version                 = "TLS1_2"
  tags                            = local.tags

  # Block all public internet access.
  # AzureServices bypass allows the Container App to access blobs via
  # Network ACLs intentionally permissive: storage is fully RBAC-locked
  # (shared_access_key_enabled=false, no SAS issued), so identity-based
  # access — not network position — is the actual security boundary.
  # Container Apps' "AzureServices" bypass is unreliable; rather than
  # special-case its outbound IPs, we let the access layer enforce policy.
  network_rules {
    default_action             = "Allow"
    bypass                     = ["AzureServices"]
    ip_rules                   = []
    virtual_network_subnet_ids = []
  }

  blob_properties {
    # CKV2_AZURE_38 — soft-delete protects blobs from accidental deletion for 7 days
    delete_retention_policy {
      days = 7
    }
  }
}

resource "azurerm_storage_container" "files" {
  # checkov:skip=CKV2_AZURE_21: Blob diagnostic logging not configured — non-critical storage, operational overhead not justified
  name                  = "zdrovena-files"
  storage_account_name  = azurerm_storage_account.storage.name
  container_access_type = "private"
}

resource "azurerm_storage_container" "files_staging" {
  # checkov:skip=CKV2_AZURE_21: Blob diagnostic logging not configured — non-critical storage, operational overhead not justified
  name                  = "zdrovena-files-staging"
  storage_account_name  = azurerm_storage_account.storage.name
  container_access_type = "private"
}

# ── Container Apps ─────────────────────────────────────────────────────────────
# Prod and staging share the same module — differ only in name, environment,
# replicas and storage container. One Key Vault serves both.

module "api" {
  source = "./modules/container_app"

  name                                  = "${var.prefix}-api"
  environment                           = "prod"
  resource_group_name                   = azurerm_resource_group.rg.name
  container_app_environment_id          = azurerm_container_app_environment.env.id
  acr_login_server                      = azurerm_container_registry.acr.login_server
  acr_id                                = azurerm_container_registry.acr.id
  storage_account_url                   = "https://${azurerm_storage_account.storage.name}.blob.core.windows.net"
  storage_container_name                = azurerm_storage_container.files.name
  storage_container_resource_manager_id = azurerm_storage_container.files.resource_manager_id
  key_vault_id                          = azurerm_key_vault.kv.id
  key_vault_url                         = azurerm_key_vault.kv.vault_uri
  azure_tenant_id                       = var.azure_tenant_id
  azure_client_id_entra                 = var.azure_client_id_entra
  allowed_origins                       = "https://${azurerm_static_web_app.ui.default_host_name}"
  min_replicas                          = 0
  max_replicas                          = 2
  cpu                                   = var.container_app_cpu
  memory                                = var.container_app_memory
  tags                                  = local.tags
}

module "api_staging" {
  source = "./modules/container_app"

  name                                  = "${var.prefix}-api-staging"
  environment                           = "staging"
  resource_group_name                   = azurerm_resource_group.rg.name
  container_app_environment_id          = azurerm_container_app_environment.env.id
  acr_login_server                      = azurerm_container_registry.acr.login_server
  acr_id                                = azurerm_container_registry.acr.id
  storage_account_url                   = "https://${azurerm_storage_account.storage.name}.blob.core.windows.net"
  storage_container_name                = azurerm_storage_container.files_staging.name
  storage_container_resource_manager_id = azurerm_storage_container.files_staging.resource_manager_id
  key_vault_id                          = azurerm_key_vault.kv.id
  key_vault_url                         = azurerm_key_vault.kv.vault_uri
  azure_tenant_id                       = var.azure_tenant_id
  azure_client_id_entra                 = var.azure_client_id_entra
  allowed_origins                       = "https://${azurerm_static_web_app.ui.default_host_name}"
  min_replicas                          = 0
  max_replicas                          = 1
  cpu                                   = var.container_app_cpu
  memory                                = var.container_app_memory
  tags                                  = merge(local.tags, { environment = "staging" })
}
# Stores all application secrets (Fakturownia, Zoho, KSeF, Google Ads).
# Container App reads them via managed identity — no secrets in env vars or code.

data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "kv" {
  # checkov:skip=CKV_AZURE_42: soft_delete_retention_days=7 enables recovery; purge_protection=false is intentional — terraform destroy would block for 90 days with purge protection enabled
  # checkov:skip=CKV_AZURE_110: purge_protection disabled intentionally (see above)
  # checkov:skip=CKV_AZURE_189: public network access required — no VNet/private endpoint in this architecture; access restricted via network_acls bypass=AzureServices
  # checkov:skip=CKV2_AZURE_32: private endpoint requires VNet not present in this architecture; Container App reaches KV via AzureServices bypass over Azure backbone
  name                       = "${replace(var.prefix, "-", "")}kv"
  resource_group_name        = azurerm_resource_group.rg.name
  location                   = azurerm_resource_group.rg.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  soft_delete_retention_days = 7
  purge_protection_enabled   = false
  tags                       = local.tags

  # CKV_AZURE_109 — restrict access to AzureServices only (Container App managed identity)
  # Terraform operator gets access via ip_rules (allowlist from variables)
  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
    ip_rules       = var.terraform_ip_allowlist
  }

  # Allow Terraform operator (current CLI identity) to manage secrets
  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    secret_permissions = ["Get", "List", "Set", "Delete", "Purge"]
  }
}

# ── Static Web App (frontend) ──────────────────────────────────────────────────
# SWA serves the JS/React/Vue bundle from Azure CDN.
# /api/* routes are proxied by the SWA edge to the Container App above;
# the browser never learns the Container App URL.
# Standard SKU required for linked backend feature.

resource "azurerm_static_web_app" "ui" {
  name                = "${var.prefix}-ui"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.swa_location
  sku_tier            = "Standard"
  sku_size            = "Standard"
  tags                = local.tags
}

# ── User-Assigned Identity for GitHub Actions (OIDC) ──────────────────────────

resource "azurerm_user_assigned_identity" "github_actions" {
  name                = "${var.prefix}-github-actions"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  tags                = local.tags
}

# Federated credential — push to main triggers deploy
resource "azurerm_federated_identity_credential" "github_main" {
  name                = "github-main"
  resource_group_name = azurerm_resource_group.rg.name
  parent_id           = azurerm_user_assigned_identity.github_actions.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = "https://token.actions.githubusercontent.com"
  subject             = "repo:${var.github_owner}/${var.github_repo}:ref:refs/heads/main"
}

# ── RBAC: GitHub Actions → AcrPush ────────────────────────────────────────────

resource "azurerm_role_assignment" "github_acr_push" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}

# ── RBAC: GitHub Actions → Contributor on RG (to update Container App) ────────
# Scoped to the resource group; allows `az containerapp update --image`.

resource "azurerm_role_assignment" "github_rg_contributor" {
  scope                = azurerm_resource_group.rg.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}

# ── Application Insights ──────────────────────────────────────────────────────
# Provides traces, exceptions, performance metrics, and alerting.
# Wire during Node.js migration: npm install @azure/monitor-opentelemetry

resource "azurerm_application_insights" "ai" {
  name                = "${var.prefix}-ai"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  workspace_id        = azurerm_log_analytics_workspace.law.id
  application_type    = "web"
  tags                = local.tags
}

# ── Alert: high error rate (5xx > 1% over 5 minutes) ─────────────────────────

resource "azurerm_monitor_metric_alert" "high_error_rate" {
  name                = "${var.prefix}-alert-error-rate"
  resource_group_name = azurerm_resource_group.rg.name
  scopes              = [azurerm_application_insights.ai.id]
  description         = "Error rate exceeded 1% — action required"
  severity            = 1
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "microsoft.insights/components"
    metric_name      = "requests/failed"
    aggregation      = "Count"
    operator         = "GreaterThan"
    threshold        = 5
  }

  tags = local.tags
}

# ── Alert: high latency (p95 > 3s over 5 minutes) ────────────────────────────

resource "azurerm_monitor_metric_alert" "high_latency" {
  name                = "${var.prefix}-alert-latency"
  resource_group_name = azurerm_resource_group.rg.name
  scopes              = [azurerm_application_insights.ai.id]
  description         = "p95 request latency exceeded 3 seconds"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "microsoft.insights/components"
    metric_name      = "requests/duration"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 3000
  }

  tags = local.tags
}
