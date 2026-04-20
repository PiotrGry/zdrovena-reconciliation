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
  name                            = "${replace(var.prefix, "-", "")}files"
  resource_group_name             = azurerm_resource_group.rg.name
  location                        = azurerm_resource_group.rg.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  allow_nested_items_to_be_public = false
  public_network_access_enabled   = false # CKV_AZURE_59 — enforce at resource level, network_rules default_action=Deny also blocks access
  shared_access_key_enabled       = false # CKV2_AZURE_40 — disable Shared Key auth; all access via managed identity
  min_tls_version                 = "TLS1_2"
  tags                            = local.tags

  # Block all public internet access.
  # AzureServices bypass allows the Container App to access blobs via
  # Azure backbone using managed identity — no traffic traverses the internet.
  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    ip_rules                   = var.terraform_ip_allowlist
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
  name                  = "zdrovena-files"
  storage_account_name  = azurerm_storage_account.storage.name
  container_access_type = "private"
}

# ── Container App ──────────────────────────────────────────────────────────────

resource "azurerm_container_app" "api" {
  name                         = "${var.prefix}-api"
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = azurerm_resource_group.rg.name
  revision_mode                = "Single"
  tags                         = local.tags

  # System-assigned managed identity — used for ACR pull + blob read
  identity {
    type = "SystemAssigned"
  }

  # Pull images from ACR using managed identity (no password needed)
  registry {
    server   = azurerm_container_registry.acr.login_server
    identity = "System"
  }

  # External ingress required so Static Web Apps linked backend can proxy
  # /api/* requests to this Container App.
  # The browser NEVER calls this URL directly — only the SWA edge nodes do.
  # CORS is restricted to the SWA origin (ALLOWED_ORIGINS env var below).
  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  template {
    min_replicas = 0 # scale-to-zero when idle
    max_replicas = 2

    container {
      name = "api"
      # Placeholder — GitHub Actions replaces on first deploy
      image  = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
      cpu    = var.container_app_cpu
      memory = var.container_app_memory

      env {
        name  = "APP_ENV"
        value = "prod"
      }

      env {
        name  = "AZURE_STORAGE_ACCOUNT_URL"
        value = "https://${azurerm_storage_account.storage.name}.blob.core.windows.net"
      }

      env {
        name  = "AZURE_STORAGE_CONTAINER"
        value = azurerm_storage_container.files.name
      }

      env {
        name  = "AZURE_TENANT_ID"
        value = var.azure_tenant_id
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = var.azure_client_id_entra
      }

      # Restrict CORS to the SWA origin — blocks any other browser origin.
      # FastAPI reads ALLOWED_ORIGINS and passes it to CORSMiddleware.
      env {
        name  = "ALLOWED_ORIGINS"
        value = "https://${azurerm_static_web_app.ui.default_host_name}"
      }

      env {
        name  = "AZURE_KEYVAULT_URL"
        value = azurerm_key_vault.kv.vault_uri
      }
    }
  }
}

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

# ── RBAC: Container App → Key Vault Secrets User ──────────────────────────────

resource "azurerm_role_assignment" "app_kv_secrets_user" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_container_app.api.identity[0].principal_id
  depends_on           = [azurerm_container_app.api]
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

# ── RBAC: Container App → AcrPull ─────────────────────────────────────────────

resource "azurerm_role_assignment" "app_acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_container_app.api.identity[0].principal_id
  depends_on           = [azurerm_container_app.api]
}

# ── RBAC: Container App → Storage Blob Data Reader (on the container only) ────

resource "azurerm_role_assignment" "app_storage_reader" {
  scope                = azurerm_storage_container.files.resource_manager_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_container_app.api.identity[0].principal_id
  depends_on           = [azurerm_container_app.api]
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
