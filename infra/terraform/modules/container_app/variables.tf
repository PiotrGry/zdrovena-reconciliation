variable "name" {
  description = "Container App resource name"
  type        = string
}

variable "environment" {
  description = "Logical environment: prod | staging"
  type        = string
}

variable "resource_group_name" {
  type = string
}

variable "container_app_environment_id" {
  type = string
}

variable "acr_login_server" {
  type = string
}

variable "acr_id" {
  type = string
}

variable "storage_account_url" {
  type = string
}

variable "storage_container_name" {
  type = string
}

variable "storage_container_id" {
  type        = string
  description = "Storage container Resource Manager ID for RBAC scope"
}

variable "storage_account_id" {
  type        = string
  description = "Storage account Resource Manager ID — used for Table Storage RBAC scope (tables live at account level, not container level)"
}

variable "key_vault_id" {
  type = string
}

variable "key_vault_url" {
  type = string
}

variable "azure_tenant_id" {
  type = string
}

variable "azure_client_id_entra" {
  type = string
}

variable "allowed_origins" {
  type = string
}

variable "min_replicas" {
  type    = number
  default = 0
}

variable "max_replicas" {
  type    = number
  default = 2
}

variable "cpu" {
  type = number
}

variable "memory" {
  type = string
}

variable "tags" {
  type = map(string)
}

variable "target_port" {
  type        = number
  default     = 8000
  description = "Container ingress target port."
}

variable "container_name" {
  type        = string
  default     = "api"
  description = "Logical container name inside the Container App revision."
}

variable "command" {
  type        = list(string)
  default     = null
  description = "Optional container command override."
}

variable "extra_env" {
  type        = map(string)
  default     = {}
  description = "Additional plain environment variables for the container."
}

variable "applicationinsights_connection_string" {
  type        = string
  default     = null
  description = "Azure Application Insights connection string for distributed tracing and logging"
}

variable "shopify_allowed_domains" {
  type        = string
  default     = ""
  description = "Comma-separated Shopify shop domain whitelist, set as SHOPIFY_ALLOWED_DOMAINS. Leave empty for permissive dev/staging behaviour — required for prod (webhooks.py fails closed when APP_ENV=prod and this is unset)."
}
