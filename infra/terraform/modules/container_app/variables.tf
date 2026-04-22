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

variable "storage_container_resource_manager_id" {
  type = string
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
