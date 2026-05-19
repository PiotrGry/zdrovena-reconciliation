# ── Key Vault ──────────────────────────────────────────────────────────────────
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
  # When private network is enabled, access is further restricted to VNet subnet
  network_acls {
    default_action             = "Deny"
    bypass                     = "AzureServices"
    ip_rules                   = var.terraform_ip_allowlist
    virtual_network_subnet_ids = var.enable_private_network ? [azurerm_subnet.container_apps[0].id] : []
  }

  # Allow Terraform operator (current CLI identity) to manage secrets
  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    secret_permissions = ["Get", "List", "Set", "Delete", "Purge"]
  }
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
  name                      = "github-main"
  user_assigned_identity_id = azurerm_user_assigned_identity.github_actions.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "repo:${var.github_owner}/${var.github_repo}:ref:refs/heads/main"
}

resource "azurerm_federated_identity_credential" "github_staging_env" {
  name                      = "github-staging-env"
  user_assigned_identity_id = azurerm_user_assigned_identity.github_actions.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "repo:${var.github_owner}/${var.github_repo}:environment:staging"
}

resource "azurerm_federated_identity_credential" "github_develop" {
  name                      = "github-develop"
  user_assigned_identity_id = azurerm_user_assigned_identity.github_actions.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = "repo:${var.github_owner}/${var.github_repo}:ref:refs/heads/develop"
}

# ── RBAC: GitHub Actions → AcrPush ────────────────────────────────────────────

resource "azurerm_role_assignment" "github_acr_push" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}

# ── RBAC: GitHub Actions → Storage Blob Data Contributor on staging container ──
# Required for seed-staging CI step to upload test invoice files to blob storage.
# Contributor on RG does not grant data-plane blob access (Azure RBAC split).
resource "azurerm_role_assignment" "github_staging_blob" {
  scope                = azurerm_storage_account.storage.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}

# ── RBAC: GitHub Actions → Contributor on RG (to update Container App) ────────
# Scoped to the resource group; allows `az containerapp update --image`.

resource "azurerm_role_assignment" "github_rg_contributor" {
  scope                = azurerm_resource_group.rg.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}

# ── RBAC: GitHub Actions → Key Vault Secrets User ─────────────────────────────
# Required for CI e2e job to fetch smoke-client-id and smoke-client-secret
# from Key Vault (instead of storing raw credentials as GitHub secrets).

resource "azurerm_role_assignment" "github_kv_secrets_user" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.github_actions.principal_id
}
