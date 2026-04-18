output "resource_group_name" {
  description = "Set as AZURE_RESOURCE_GROUP in GitHub Secrets"
  value       = azurerm_resource_group.rg.name
}

output "acr_login_server" {
  description = "Set as ACR_LOGIN_SERVER in GitHub Secrets"
  value       = azurerm_container_registry.acr.login_server
}

output "container_app_name" {
  description = "Set as AZURE_CONTAINER_APP_NAME in GitHub Secrets"
  value       = azurerm_container_app.api.name
}

output "container_app_fqdn" {
  description = "Container App FQDN — only accessible via SWA proxy, not publicly documented"
  value       = azurerm_container_app.api.latest_revision_fqdn
}

output "swa_url" {
  description = "Static Web App public URL (CDN) — the only public-facing endpoint"
  value       = "https://${azurerm_static_web_app.ui.default_host_name}"
}

output "github_secret_SWA_DEPLOYMENT_TOKEN" {
  description = "Set as SWA_DEPLOYMENT_TOKEN in the frontend repo GitHub Secrets"
  value       = azurerm_static_web_app.ui.api_key
  sensitive   = true
}

output "storage_account_name" {
  value = azurerm_storage_account.storage.name
}

# ── GitHub Secrets — copy these values after `terraform apply` ──────────────
# Add them at: https://github.com/<owner>/<repo>/settings/secrets/actions

output "github_secret_AZURE_CLIENT_ID" {
  description = "OIDC identity client ID — set as GitHub Secret AZURE_CLIENT_ID"
  value       = azurerm_user_assigned_identity.github_actions.client_id
}

output "github_secret_AZURE_TENANT_ID" {
  description = "Set as GitHub Secret AZURE_TENANT_ID"
  value       = var.azure_tenant_id
}

output "github_secret_AZURE_SUBSCRIPTION_ID" {
  description = "Set as GitHub Secret AZURE_SUBSCRIPTION_ID"
  value       = var.subscription_id
}
