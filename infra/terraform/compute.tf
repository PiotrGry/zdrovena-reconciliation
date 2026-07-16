# ── Container Apps ─────────────────────────────────────────────────────────────
# Prod and staging share the same module — differ only in name, environment,
# replicas and storage container. One Key Vault serves both.

module "api_prod" {
  source = "./modules/container_app"

  name                                  = "${var.prefix}-api-prod"
  environment                           = "prod"
  resource_group_name                   = azurerm_resource_group.rg.name
  container_app_environment_id          = azurerm_container_app_environment.env.id
  acr_login_server                      = azurerm_container_registry.acr.login_server
  acr_id                                = azurerm_container_registry.acr.id
  storage_account_url                   = "https://${azurerm_storage_account.storage.name}.blob.core.windows.net"
  storage_container_name                = azurerm_storage_container.files.name
  storage_container_id                  = azurerm_storage_container.files.id
  storage_account_id                    = azurerm_storage_account.storage.id
  key_vault_id                          = azurerm_key_vault.kv.id
  key_vault_url                         = azurerm_key_vault.kv.vault_uri
  azure_tenant_id                       = var.azure_tenant_id
  azure_client_id_entra                 = var.azure_client_id_entra
  allowed_origins                       = var.swa_custom_domain != "" ? "https://${azurerm_static_web_app.ui.default_host_name},https://${var.swa_custom_domain}" : "https://${azurerm_static_web_app.ui.default_host_name}"
  applicationinsights_connection_string = azurerm_application_insights.ai.connection_string
  shopify_allowed_domains               = var.shopify_allowed_domains
  min_replicas                          = 1
  max_replicas                          = 2
  cpu                                   = var.container_app_cpu
  memory                                = var.container_app_memory
  tags                                  = local.tags
}

module "fake_providers_staging" {
  source = "./modules/container_app"

  name                         = "${var.prefix}-fake-providers-staging"
  environment                  = "staging"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  acr_login_server             = azurerm_container_registry.acr.login_server
  acr_id                       = azurerm_container_registry.acr.id
  storage_account_url          = "https://${azurerm_storage_account.storage.name}.blob.core.windows.net"
  storage_container_name       = azurerm_storage_container.files_staging.name
  storage_container_id         = azurerm_storage_container.files_staging.id
  storage_account_id           = azurerm_storage_account.storage.id
  key_vault_id                 = azurerm_key_vault.kv.id
  key_vault_url                = azurerm_key_vault.kv.vault_uri
  azure_tenant_id              = var.azure_tenant_id
  azure_client_id_entra        = var.azure_client_id_entra
  allowed_origins              = "https://${azurerm_static_web_app.ui.default_host_name}"
  container_name               = "fake-providers"
  target_port                  = 9009
  min_replicas                 = 0
  max_replicas                 = 1
  cpu                          = 0.25
  memory                       = "0.5Gi"
  tags                         = merge(local.tags, { environment = "staging", role = "fake-providers" })
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
  storage_container_id                  = azurerm_storage_container.files_staging.id
  storage_account_id                    = azurerm_storage_account.storage.id
  key_vault_id                          = azurerm_key_vault.kv.id
  key_vault_url                         = azurerm_key_vault.kv.vault_uri
  azure_tenant_id                       = var.azure_tenant_id
  azure_client_id_entra                 = var.azure_client_id_entra
  allowed_origins                       = "https://${azurerm_static_web_app.ui.default_host_name}"
  applicationinsights_connection_string = azurerm_application_insights.ai.connection_string
  extra_env = {
    ALLEGRO_CLIENT_ID      = "fake"
    ALLEGRO_CLIENT_SECRET  = "fake"
    ALLEGRO_REFRESH_TOKEN  = "fake"
    PROVIDER_MODE          = "fake"
    ALLEGRO_BASE_URL       = "https://${module.fake_providers_staging.fqdn}/allegro"
    ALLEGRO_AUTH_URL       = "https://${module.fake_providers_staging.fqdn}/allegro/auth/oauth/token"
    INPOST_API_TOKEN       = "fake"
    INPOST_ORGANIZATION_ID = "fake"
    INPOST_BASE_URL        = "https://${module.fake_providers_staging.fqdn}/inpost"
    APACZKA_APP_ID         = "fake"
    APACZKA_APP_SECRET     = "fake"
    APACZKA_BASE_URL       = "https://${module.fake_providers_staging.fqdn}/apaczka/api/v2"
    FAKTUROWNIA_BASE_URL   = "https://${module.fake_providers_staging.fqdn}/fakturownia"
    FAKTUROWNIA_API_TOKEN  = "fake"
  }
  min_replicas = 0
  max_replicas = 1
  cpu          = var.container_app_cpu
  memory       = var.container_app_memory
  tags         = merge(local.tags, { environment = "staging" })
}

# ── Allegro Poller — Container App Job (scheduled cron) ───────────────────────
# Allegro has no webhooks, so we poll every 5 minutes.
# Uses the same Docker image as api_prod; CI updates the image via
# `az containerapp job update --image <acr>/<img>:<sha>` after each deploy.

resource "azurerm_container_app_job" "allegro_poller" {
  name                         = "${var.prefix}-allegro-poller"
  location                     = azurerm_resource_group.rg.location
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  tags                         = local.tags

  replica_timeout_in_seconds = 300
  replica_retry_limit        = 1

  schedule_trigger_config {
    cron_expression          = "*/5 * * * *"
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type = "SystemAssigned"
  }

  registry {
    server   = azurerm_container_registry.acr.login_server
    identity = "System"
  }

  template {
    container {
      name   = "poller"
      image  = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
      cpu    = 0.25
      memory = "0.5Gi"

      command = ["zdrovena", "allegro-poll"]

      env {
        name  = "AZURE_KEYVAULT_URL"
        value = azurerm_key_vault.kv.vault_uri
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
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.ai.connection_string
      }
      env {
        name  = "ALLEGRO_ENV"
        value = "prod"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
    ]
  }
}

# ── EasyAuth: Shopify webhook excluded paths ───────────────────────────────────
# azurerm does not expose globalValidation.excludedPaths and azapi requires
# EasyAuth to be initialised before it can PATCH authConfigs/current.
# Applied once via CLI (out-of-band) and persisted by Azure:
#
#   az containerapp auth update \
#     --name <app> --resource-group <rg> \
#     --unauthenticated-client-action Return401 \
#     --excluded-paths "/api/webhooks/shopify/order-created,/api/webhooks/shopify/order-create"
#
# Re-run if the Container App is ever recreated from scratch.

# ── Import: Allegro Poller job pre-exists in Azure ────────────────────────────
# CI deploy creates the job image via az containerapp job update before
# Terraform has a chance to run, so the resource exists in Azure but not in
# state. This block imports it on the next terraform apply, which then lets
# Terraform create the RBAC assignments (AcrPull, Storage, Key Vault) for
# the job's managed identity.
import {
  to = azurerm_container_app_job.allegro_poller
  id = "/subscriptions/${var.subscription_id}/resourceGroups/${var.prefix}-rg/providers/Microsoft.App/jobs/${var.prefix}-allegro-poller"
}
