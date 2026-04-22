resource "azurerm_container_app" "this" {
  name                         = var.name
  container_app_environment_id = var.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = var.tags

  # System-assigned managed identity — used for ACR pull + blob read + KV access
  identity {
    type = "SystemAssigned"
  }

  # Pull images from ACR using managed identity (no password needed)
  registry {
    server   = var.acr_login_server
    identity = "System"
  }

  # External ingress required so Static Web Apps linked backend can proxy
  # /api/* requests to this Container App.
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
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name = "api"
      # Placeholder — GitHub Actions replaces on first deploy
      image  = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
      cpu    = var.cpu
      memory = var.memory

      env {
        name  = "APP_ENV"
        value = var.environment
      }

      env {
        name  = "AZURE_STORAGE_ACCOUNT_URL"
        value = var.storage_account_url
      }

      env {
        name  = "AZURE_STORAGE_CONTAINER"
        value = var.storage_container_name
      }

      env {
        name  = "AZURE_TENANT_ID"
        value = var.azure_tenant_id
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = var.azure_client_id_entra
      }

      env {
        name  = "ALLOWED_ORIGINS"
        value = var.allowed_origins
      }

      env {
        name  = "AZURE_KEYVAULT_URL"
        value = var.key_vault_url
      }
    }
  }
}

# ── RBAC: Container App → AcrPull ─────────────────────────────────────────────

resource "azurerm_role_assignment" "acr_pull" {
  scope                = var.acr_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_container_app.this.identity[0].principal_id
}

# ── RBAC: Container App → Storage Blob Data Contributor ───────────────────────

resource "azurerm_role_assignment" "storage_contributor" {
  scope                = var.storage_container_resource_manager_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_container_app.this.identity[0].principal_id
}

# ── RBAC: Container App → Key Vault Secrets User ──────────────────────────────

resource "azurerm_role_assignment" "kv_secrets_user" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_container_app.this.identity[0].principal_id
}
