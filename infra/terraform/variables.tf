variable "subscription_id" {
  description = "Azure subscription ID (required: set in terraform.tfvars or TF_VAR_subscription_id)"
  type        = string
  # No default - prevents accidental deployment to wrong subscription
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

  validation {
    condition     = can(regex("^[a-z0-9-]{3,20}$", var.prefix))
    error_message = "Prefix must be 3-20 lowercase alphanumeric characters or hyphens."
  }
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
  description = "Entra ID app registration client ID (zdrovena-api) used for JWT audience validation — set as Container App env var AZURE_CLIENT_ID and GitHub Secret AZURE_API_CLIENT_ID. Required for production to enable authentication."
  type        = string
  default     = ""

  # Note: Empty string disables authentication. For production deployments,
  # set to a valid App Registration client ID (e.g., "api://zdrovena-api-prod").
  # Cross-variable validation (checking var.environment) not supported in Terraform.
}

variable "ops_alert_email" {
  description = "E-mail właściciela — odbiorca alertów Azure Monitor (error-rate, latency, DLQ). Ustaw w terraform.tfvars."
  type        = string

  # Fail-fast at plan time: an empty or malformed recipient would silently make
  # every alert undeliverable — the exact "declared but not operational" gap R4-C
  # closes. Basic RFC-ish shape check (local@domain.tld).
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.ops_alert_email))
    error_message = "ops_alert_email musi być poprawnym adresem e-mail (np. owner@example.com) — alerty Azure Monitor są niedostarczalne bez prawidłowego odbiorcy."
  }
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

variable "swa_custom_domain" {
  description = "Custom domain hostname for the Static Web App (e.g. app.zdrovena.com). Leave empty to skip — domain must be CNAME'd to the SWA default_host_name before apply."
  type        = string
  default     = ""
}

variable "shopify_allowed_domains" {
  description = "Comma-separated Shopify shop domain whitelist for the prod Container App (SHOPIFY_ALLOWED_DOMAINS). Required for prod: webhooks.py fails closed (rejects all Shopify webhooks) when APP_ENV=prod and this is unset."
  type        = string
  default     = "jvepcp-0p.myshopify.com"
}

variable "enable_private_network" {
  description = "Enable VNet + Service Endpoints for Storage and Key Vault (cost-optimized). Cost: ~€3/month (VNet traffic only). Service Endpoints = FREE (vs €14/month for Private Endpoints). Traffic via Microsoft backbone, firewall default_action=Deny, RBAC enforced. Sufficient for business data; upgrade to Private Endpoints if HIPAA/PCI-DSS compliance required."
  type        = bool
  default     = false
}
