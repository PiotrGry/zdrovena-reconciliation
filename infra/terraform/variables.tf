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
  description = "Entra ID app registration client ID (zdrovena-api) used for JWT audience validation — set as Container App env var AZURE_CLIENT_ID and GitHub Secret AZURE_API_CLIENT_ID"
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

variable "swa_location" {
  description = "Azure region for Static Web Apps (limited availability — westeurope recommended)"
  type        = string
  default     = "westeurope"
}

variable "terraform_ip_allowlist" {
  description = "List of IPs allowed to access storage account (needed for Terraform to manage storage containers). Remove after initial apply if desired."
  type        = list(string)
  default     = []
}

