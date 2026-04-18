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

output "container_app_url" {
  description = "HTTPS URL of the deployed API"
  value       = "https://${azurerm_container_app.api.latest_revision_fqdn}"
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
