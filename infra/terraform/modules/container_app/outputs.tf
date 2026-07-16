output "name" {
  description = "Container App resource name"
  value       = azurerm_container_app.this.name
}

output "fqdn" {
  description = "Stable Container App ingress FQDN"
  value       = azurerm_container_app.this.ingress[0].fqdn
}

output "latest_revision_name" {
  description = "Latest active revision name (useful for debugging deployments)"
  value       = azurerm_container_app.this.latest_revision_name
}

output "principal_id" {
  description = "System-assigned managed identity principal ID"
  value       = azurerm_container_app.this.identity[0].principal_id
}
