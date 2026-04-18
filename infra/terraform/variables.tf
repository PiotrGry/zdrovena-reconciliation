variable "subscription_id" {
  description = "Azure subscription ID"
  type        = string
  default     = "f8942601-3bfe-437d-b849-86f3b5519fea"
}

variable "location" {
  description = "Azure region for all resources"
  type        = string
  default     = "polandcentral"
}

variable "prefix" {
  description = "Short prefix used for all resource names"
  type        = string
  default     = "zdrovena"
}

variable "github_owner" {
  description = "GitHub organisation or user owning the repository (for OIDC federated credential)"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "zdrovena-reconciliation"
}

variable "azure_tenant_id" {
  description = "Azure AD tenant ID — injected into the Container App as AZURE_TENANT_ID"
  type        = string
}

variable "azure_client_id_entra" {
  description = "Entra ID app registration client ID used for JWT audience validation (AZURE_CLIENT_ID)"
  type        = string
  default     = ""
}

variable "container_app_cpu" {
  description = "vCPU allocated to the API container"
  type        = number
  default     = 0.5
}

variable "container_app_memory" {
  description = "Memory allocated to the API container (must match CPU tier)"
  type        = string
  default     = "1Gi"
}

variable "tailscale_auth_key" {
  description = <<-EOT
    Tailscale ephemeral+reusable auth key.
    Generate at: https://login.tailscale.com/admin/settings/keys
    Select: Reusable=true, Ephemeral=true, Tags=[tag:server]
    Pass via TF_VAR_tailscale_auth_key env var or -var flag — never commit.
  EOT
  type      = string
  sensitive = true
}
