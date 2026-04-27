# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Zdrovena Reconciliation — Core Infrastructure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Architecture: Container Apps + Static Web App + Blob Storage + Key Vault
# Environments: prod + staging (shared infrastructure for cost optimization)
#
# File organization:
#   main.tf       — Core resources (RG, ACR, Log Analytics, Container App Env)
#   compute.tf    — Container Apps (prod + staging modules)
#   storage.tf    — Storage Account + blob containers
#   security.tf   — Key Vault + GitHub OIDC Identity + RBAC assignments
#   frontend.tf   — Static Web App + custom domain
#   monitoring.tf — Application Insights + metric alerts
#
# Planned services (future growth):
#   - PostgreSQL Flexible Server (transactional data)
#   - Azure Cache for Redis (session/caching layer)
#   - Service Bus (async workflows)
#   - Azure CDN (global asset delivery)
#   - Private Endpoints (network isolation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
  # SKU: Basic (public, Managed Identity-protected) — Premium only if full PE isolation needed
  name                = "${replace(var.prefix, "-", "")}acr"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Basic" # Cost optimization: saves €145/month vs Premium
  admin_enabled       = false   # pull via managed identity, no passwords
  tags                = local.tags

  # Note: Basic SKU does NOT support network_rule_set (requires Premium)
  # Security relies on:
  #   1. Managed Identity RBAC (AcrPull role) — only authorized apps can pull
  #   2. No admin credentials (admin_enabled=false)
  #   3. GitHub Actions uses OIDC federated identity (AcrPush role)
  # Public access is acceptable for container images (code, not data)

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
  infrastructure_subnet_id   = var.enable_private_network ? azurerm_subnet.container_apps[0].id : null
  tags                       = local.tags
}
