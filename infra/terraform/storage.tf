# ── Storage Account (private blob files) ──────────────────────────────────────

resource "azurerm_storage_account" "storage" {
  # Storage account name: alphanumeric only, max 24 chars
  # checkov:skip=CKV_AZURE_43: Name is dynamically computed via replace(var.prefix,"-","") — valid alphanumeric, length enforced by variable validation
  # checkov:skip=CKV_AZURE_33: No Azure Queue service used — this is blob-only storage
  # checkov:skip=CKV_AZURE_206: LRS replication intentional — single-region deployment, non-critical files, cost optimised
  # checkov:skip=CKV2_AZURE_41: No SAS tokens issued — all access via managed identity (RBAC)
  # checkov:skip=CKV2_AZURE_1: Customer Managed Key not required — files are non-sensitive reports; Microsoft-managed encryption at rest is sufficient for this tier
  # checkov:skip=CKV2_AZURE_33: Private endpoint requires VNet not present in this architecture; access is gated by RBAC + Shared Key auth disabled (shared_access_key_enabled=false), no SAS issued
  # checkov:skip=CKV_AZURE_59: public_network_access_enabled=true is required because Container Apps managed identity is not in Azure's "trusted services" bypass for Storage; RBAC + disabled shared key auth is the security boundary
  # checkov:skip=CKV_AZURE_35: default_action=Allow accepted intentionally — Container Apps' AzureServices bypass is unreliable, and the actual security control is identity-based (RBAC + shared_access_key_enabled=false). Network position is not the security boundary here. See commit a87fdf3 for the incident that drove this decision.
  # checkov:skip=CKV2_AZURE_21: Blob diagnostic logging (read requests) not configured — operational overhead not justified for this single-region non-critical storage
  name                            = "${replace(var.prefix, "-", "")}files"
  resource_group_name             = azurerm_resource_group.rg.name
  location                        = azurerm_resource_group.rg.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false # CKV2_AZURE_40 — disable Shared Key auth; all access via managed identity
  min_tls_version                 = "TLS1_2"
  tags                            = local.tags

  # Block all public internet access.
  # AzureServices bypass allows the Container App to access blobs via
  # Network ACLs intentionally permissive: storage is fully RBAC-locked
  # (shared_access_key_enabled=false, no SAS issued), so identity-based
  # access — not network position — is the actual security boundary.
  # Container Apps' "AzureServices" bypass is unreliable; rather than
  # special-case its outbound IPs, we let the access layer enforce policy.
  network_rules {
    default_action             = "Allow"
    bypass                     = ["AzureServices"]
    ip_rules                   = []
    virtual_network_subnet_ids = []
  }

  blob_properties {
    # CKV2_AZURE_38 — soft-delete protects blobs from accidental deletion for 7 days
    delete_retention_policy {
      days = 7
    }
  }
}

resource "azurerm_storage_container" "files" {
  # checkov:skip=CKV2_AZURE_21: Blob diagnostic logging not configured — non-critical storage, operational overhead not justified
  name                  = "zdrovena-files"
  storage_account_id    = azurerm_storage_account.storage.id
  container_access_type = "private"
}

resource "azurerm_storage_container" "files_staging" {
  # checkov:skip=CKV2_AZURE_21: Blob diagnostic logging not configured — non-critical storage, operational overhead not justified
  name                  = "zdrovena-files-staging"
  storage_account_id    = azurerm_storage_account.storage.id
  container_access_type = "private"
}
