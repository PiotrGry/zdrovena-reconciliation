resource "azurerm_container_app" "this" {
  name                         = var.name
  container_app_environment_id = var.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = var.tags

  # image is managed by GitHub Actions (az containerapp update --image)
  # Terraform only provisions the resource — it must not reset the image on plan.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].command,
    ]
  }

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
    target_port      = var.target_port
    transport        = "auto" # Enables HTTP/2 and gRPC support

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name = var.container_name
      # Bootstrap image — GitHub Actions owns later deploy images.
      image   = var.initial_image
      cpu     = var.cpu
      memory  = var.memory
      command = var.command

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
        # JWT audience — NOT the managed identity client_id. Named differently
        # because AZURE_CLIENT_ID is reserved by azure-identity (DefaultAzureCredential
        # uses it as the user-assigned MI client_id and hangs trying to fetch a token
        # for an MI that doesn't exist).
        name  = "AZURE_API_AUDIENCE"
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

      env {
        # Pipeline working directory — avoids writing to non-existent /home/app
        # (useradd -r system user has no home dir in the container image).
        name  = "FAKTUROWNIA_BASE_DIR"
        value = "/tmp/zdrovena"
      }

      dynamic "env" {
        for_each = var.applicationinsights_connection_string != null ? [1] : []
        content {
          name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
          value = var.applicationinsights_connection_string
        }
      }

      dynamic "env" {
        for_each = var.applicationinsights_connection_string != null ? [1] : []
        content {
          # OpenTelemetry service.name → Application Insights AppRoleName.
          name  = "OTEL_SERVICE_NAME"
          value = var.name
        }
      }

      dynamic "env" {
        for_each = var.shopify_allowed_domains != "" ? [1] : []
        content {
          name  = "SHOPIFY_ALLOWED_DOMAINS"
          value = var.shopify_allowed_domains
        }
      }

      dynamic "env" {
        for_each = var.extra_env
        content {
          name  = env.key
          value = env.value
        }
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
  scope                = var.storage_container_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_container_app.this.identity[0].principal_id
}

resource "azurerm_role_assignment" "storage_table_contributor" {
  scope                = var.storage_account_id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_container_app.this.identity[0].principal_id
}

# ── RBAC: Container App → Key Vault Secrets User ──────────────────────────────

resource "azurerm_role_assignment" "kv_secrets_user" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_container_app.this.identity[0].principal_id
}
