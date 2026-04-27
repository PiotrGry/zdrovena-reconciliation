# ── Container Apps ─────────────────────────────────────────────────────────────
# Prod and staging share the same module — differ only in name, environment,
# replicas and storage container. One Key Vault serves both.

module "api_prod" {
  source = "./modules/container_app"

  name                         = "${var.prefix}-api-prod"
  environment                  = "prod"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  acr_login_server             = azurerm_container_registry.acr.login_server
  acr_id                       = azurerm_container_registry.acr.id
  storage_account_url          = "https://${azurerm_storage_account.storage.name}.blob.core.windows.net"
  storage_container_name       = azurerm_storage_container.files.name
  storage_container_id         = azurerm_storage_container.files.id
  key_vault_id                 = azurerm_key_vault.kv.id
  key_vault_url                = azurerm_key_vault.kv.vault_uri
  azure_tenant_id              = var.azure_tenant_id
  azure_client_id_entra        = var.azure_client_id_entra
  allowed_origins              = "https://${azurerm_static_web_app.ui.default_host_name}"
  min_replicas                 = 0
  max_replicas                 = 2
  cpu                          = var.container_app_cpu
  memory                       = var.container_app_memory
  tags                         = local.tags
}

module "api_staging" {
  source = "./modules/container_app"

  name                         = "${var.prefix}-api-staging"
  environment                  = "staging"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  acr_login_server             = azurerm_container_registry.acr.login_server
  acr_id                       = azurerm_container_registry.acr.id
  storage_account_url          = "https://${azurerm_storage_account.storage.name}.blob.core.windows.net"
  storage_container_name       = azurerm_storage_container.files_staging.name
  storage_container_id         = azurerm_storage_container.files_staging.id
  key_vault_id                 = azurerm_key_vault.kv.id
  key_vault_url                = azurerm_key_vault.kv.vault_uri
  azure_tenant_id              = var.azure_tenant_id
  azure_client_id_entra        = var.azure_client_id_entra
  allowed_origins              = "https://${azurerm_static_web_app.ui.default_host_name}"
  min_replicas                 = 0
  max_replicas                 = 1
  cpu                          = var.container_app_cpu
  memory                       = var.container_app_memory
  tags                         = merge(local.tags, { environment = "staging" })
}
