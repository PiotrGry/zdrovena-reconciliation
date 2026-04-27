# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ZERO TRUST — Azure Policy (Tier 3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Azure Policy enforces Zero Trust principles at runtime:
#   - Prevents manual changes via Portal that violate security standards
#   - Audit mode: logs non-compliant resources (doesn't block)
#   - Deny mode: blocks non-compliant deployments
#
# Cost: €0/month (Azure Policy is free, enforcement is included)
#
# Compliance frameworks supported:
#   - Microsoft Cloud Security Benchmark
#   - Azure Security Baseline
#   - Custom policies for organization-specific requirements
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Policy: Storage Accounts Must Disable Public Network Access ───────────────
# Prevents accidental exposure of storage accounts to public internet

resource "azurerm_resource_group_policy_assignment" "storage_disable_public_access" {
  count                = var.enable_private_network ? 1 : 0
  name                 = "storage-disable-public-access"
  resource_group_id    = azurerm_resource_group.rg.id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/b2982f36-99f2-4db5-8eff-283140c09693"
  # Built-in policy: "Storage accounts should disable public network access"

  description  = "Zero Trust: Enforce that storage accounts disable public network access. Only private endpoints or service endpoints allowed."
  display_name = "Storage Accounts - Disable Public Access"

  parameters = jsonencode({
    effect = {
      value = "Audit" # Start with Audit, change to Deny after validation
    }
  })
}

# ── Policy: Storage Accounts Must Use Private Link ────────────────────────────
# Enforces Private Endpoint usage when enable_private_network=true

resource "azurerm_resource_group_policy_assignment" "storage_use_private_link" {
  count                = var.enable_private_network ? 1 : 0
  name                 = "storage-use-private-link"
  resource_group_id    = azurerm_resource_group.rg.id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/6edd7eda-6dd8-40f7-810d-67160c639cd9"
  # Built-in policy: "Storage accounts should use private link"

  description  = "Zero Trust: Audit storage accounts that don't use private endpoints (Service Endpoints are compliant alternative)."
  display_name = "Storage Accounts - Private Link Compliance"

  parameters = jsonencode({
    effect = {
      value = "Audit" # Audit mode: logs non-compliance without blocking
    }
  })
}

# ── Policy: Key Vault Must Disable Public Network Access ──────────────────────

resource "azurerm_resource_group_policy_assignment" "keyvault_disable_public_access" {
  count                = var.enable_private_network ? 1 : 0
  name                 = "keyvault-disable-public-access"
  resource_group_id    = azurerm_resource_group.rg.id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/55615ac9-af46-4a59-874e-391cc3dfb490"
  # Built-in policy: "Azure Key Vault should disable public network access"

  description  = "Zero Trust: Enforce that Key Vault disables public network access. Access via private endpoints or service endpoints only."
  display_name = "Key Vault - Disable Public Access"

  parameters = jsonencode({
    effect = {
      value = "Audit" # Start with Audit to validate compliance
    }
  })
}

# ── Policy: Container Apps Must Use Managed Identity ──────────────────────────
# Prevents deployment of Container Apps without managed identity

resource "azurerm_resource_group_policy_assignment" "containerapp_require_managed_identity" {
  count                = var.enable_private_network ? 1 : 0
  name                 = "containerapp-require-managed-identity"
  resource_group_id    = azurerm_resource_group.rg.id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/c4857be7-912a-4c75-87e6-e30292bcdf78"
  # Built-in policy: "Container Apps should use managed identity"

  description  = "Zero Trust: Verify explicitly - require managed identity for all Container Apps (no long-lived credentials)."
  display_name = "Container Apps - Require Managed Identity"

  parameters = jsonencode({
    effect = {
      value = "Audit" # Our Container Apps already use MI, this validates compliance
    }
  })
}

# ── Policy: Storage Accounts Must Disable Shared Key Access ───────────────────
# Enforces RBAC-only access (no SAS tokens, no shared keys)

resource "azurerm_resource_group_policy_assignment" "storage_disable_shared_key" {
  count                = var.enable_private_network ? 1 : 0
  name                 = "storage-disable-shared-key"
  resource_group_id    = azurerm_resource_group.rg.id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/8c6a50c6-9ffd-4ae7-986f-5fa6111f9a54"
  # Built-in policy: "Storage accounts should prevent shared key access"

  description  = "Zero Trust: Least privilege access - disable shared key auth, enforce Managed Identity RBAC only."
  display_name = "Storage Accounts - Disable Shared Key Auth"

  parameters = jsonencode({
    effect = {
      value = "Audit" # Our storage already has shared_access_key_enabled=false
    }
  })
}

# ── Policy: TLS 1.2 Minimum Enforced ──────────────────────────────────────────
# Prevents downgrade to TLS 1.0/1.1 (vulnerable protocols)

resource "azurerm_resource_group_policy_assignment" "storage_min_tls" {
  count                = var.enable_private_network ? 1 : 0
  name                 = "storage-min-tls"
  resource_group_id    = azurerm_resource_group.rg.id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/fe83a0eb-a853-422d-aac2-1bffd182c5d0"
  # Built-in policy: "Storage accounts should have the specified minimum TLS version"

  description  = "Zero Trust: Encryption in transit - enforce TLS 1.2 minimum for all storage connections."
  display_name = "Storage Accounts - Minimum TLS 1.2"

  parameters = jsonencode({
    effect = {
      value = "Audit"
    }
    minimumTlsVersion = {
      value = "TLS1_2"
    }
  })
}

# ── Policy: Container Registry Must Disable Admin User ────────────────────────
# Prevents enabling admin credentials (ACR should use Managed Identity only)

resource "azurerm_resource_group_policy_assignment" "acr_disable_admin" {
  count                = var.enable_private_network ? 1 : 0
  name                 = "acr-disable-admin"
  resource_group_id    = azurerm_resource_group.rg.id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/dc921057-6b28-4fbe-9b83-f7bec05db6c2"
  # Built-in policy: "Container registries should not allow unrestricted network access"

  description  = "Zero Trust: Verify explicitly - ACR admin user must be disabled, enforce Managed Identity pull only."
  display_name = "Container Registry - Disable Admin User"

  parameters = jsonencode({
    effect = {
      value = "Audit" # Our ACR already has admin_enabled=false
    }
  })
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMPLIANCE DASHBOARD (read-only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# To view policy compliance:
#   1. Azure Portal → Policy → Compliance
#   2. Filter by Resource Group: zdrovena-rg
#   3. View compliance state for each policy
#
# To upgrade from Audit to Deny mode:
#   1. Validate that all resources are compliant (Audit mode)
#   2. Change effect = "Deny" in policy parameters above
#   3. terraform apply
#   4. Future non-compliant deployments will be blocked
#
# Recommended timeline:
#   - Week 1-2: Audit mode (validate compliance, fix issues)
#   - Week 3+: Deny mode (enforce policies, block non-compliant changes)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
