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
  name                            = "${replace(var.prefix, "-", "")}files"
  resource_group_name             = azurerm_resource_group.rg.name
  location                        = azurerm_resource_group.rg.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"
  tags                            = local.tags

  # Block all public internet access.
  # AzureServices bypass allows the Container App to access blobs via
  # Azure backbone using managed identity — no traffic traverses the internet.
  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    ip_rules                   = []
    virtual_network_subnet_ids = []
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

  # Tailscale auth key — stored as Container App secret (encrypted at rest).
  # Generate at: https://login.tailscale.com/admin/settings/keys
  # Use an ephemeral + reusable key so each new replica auto-registers.
  secret {
    name  = "tailscale-auth-key"
    value = var.tailscale_auth_key
  }

  # No public ingress — all traffic comes through the Tailscale sidecar.
  # The app is reachable only on the tailnet as: zdrovena-api.<tailnet>.ts.net
  ingress {
    external_enabled = false
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

    # ── Main API container ───────────────────────────────────────────────────
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
    }

    # ── Tailscale sidecar ────────────────────────────────────────────────────
    # Registers the app on the tailnet and proxies:
    #   https://zdrovena-api.<tailnet>.ts.net  →  localhost:8000
    # Containers in the same template share the localhost network namespace.
    container {
      name   = "tailscale"
      image  = "tailscale/tailscale:stable"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name        = "TAILSCALE_AUTHKEY"
        secret_name = "tailscale-auth-key"
      }

      # Userspace networking — no kernel TUN device needed in containers
      env {
        name  = "TS_USERSPACE"
        value = "true"
      }

      # Hostname on the tailnet
      env {
        name  = "TS_HOSTNAME"
        value = "${var.prefix}-api"
      }

      # Remove device from tailnet on shutdown (clean scaling)
      env {
        name  = "TS_EXTRA_ARGS"
        value = "--ephemeral"
      }

      # Serve config: proxy tailnet HTTPS → localhost:8000
      # $${TS_CERT_DOMAIN} is a Tailscale runtime variable (escaped for Terraform)
      env {
        name = "TS_SERVE_CONFIG"
        value = jsonencode({
          Version = "v1alpha1"
          TCP     = { "443" = { HTTPS = true } }
          Web = {
            "$${TS_CERT_DOMAIN}:443" = {
              Handlers = {
                "/" = { Proxy = "http+insecure://127.0.0.1:8000" }
              }
            }
          }
        })
      }
    }
  }
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
