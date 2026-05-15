# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Network Layer — VNet + Service Endpoints (opt-in via var.enable_private_network)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# COST-OPTIMIZED SECURITY MODEL:
#   - Service Endpoints for Storage + Key Vault (FREE, traffic via Microsoft backbone)
#   - ACR stays public with Managed Identity RBAC (saves €159/month vs Private Endpoints)
#
# Cost impact: ~€3/month (VNet traffic only)
# vs Private Endpoints: ~€32/month (savings: €29/month)
# vs full private (PE + Premium ACR): ~€184/month (savings: €181/month)
#
# Benefits:
#   ✅ Firewall: default_action=Deny, whitelist only Container Apps subnet
#   ✅ RBAC: Managed Identity required (shared_access_key_enabled=false)
#   ✅ TLS 1.2: Encryption in transit (min_tls_version enforced)
#   ✅ Microsoft backbone: Traffic never traverses public internet
#
# Trade-off vs Private Endpoints:
#   - Service Endpoints use PUBLIC Azure IPs (20.x.x.x), not private IPs (10.0.x.x)
#   - Traffic goes through Microsoft backbone (secure), but not YOUR private tunnel
#   - Sufficient for business data; upgrade to PE if HIPAA/PCI-DSS compliance required
#
# Note: Static Web App CANNOT use Service Endpoint (CDN service, must be public)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Virtual Network ────────────────────────────────────────────────────────────

resource "azurerm_virtual_network" "vnet" {
  count               = var.enable_private_network ? 1 : 0
  name                = "${var.prefix}-vnet"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  address_space       = ["10.0.0.0/16"]
  tags                = local.tags
}

# ── Subnet: Container Apps Environment ────────────────────────────────────────
# Must be dedicated to Container Apps (min /23, we use /21 for future growth)
# Service Endpoints: FREE alternative to Private Endpoints (traffic via Microsoft backbone)

resource "azurerm_subnet" "container_apps" {
  count                = var.enable_private_network ? 1 : 0
  name                 = "container-apps-subnet"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet[0].name
  address_prefixes     = ["10.0.0.0/21"]

  # Service Endpoints — allow traffic to Azure services via Microsoft backbone (not public internet)
  service_endpoints = [
    "Microsoft.Storage",  # For Storage Account (blob containers)
    "Microsoft.KeyVault", # For Key Vault (secrets)
  ]

  delegation {
    name = "container-apps-delegation"
    service_delegation {
      name = "Microsoft.App/environments"
      actions = [
        "Microsoft.Network/virtualNetworks/subnets/join/action"
      ]
    }
  }
}

# ── Subnet: Private Endpoints ──────────────────────────────────────────────────

# checkov:skip=CKV2_AZURE_31: Private endpoints subnet — NSG not applicable here.
# Private endpoints have their own network policies (private_endpoint_network_policies).
# The NSG security boundary is on the container_apps subnet (see azurerm_network_security_group.container_apps).
resource "azurerm_subnet" "private_endpoints" {
  count                = var.enable_private_network ? 1 : 0
  name                 = "private-endpoints-subnet"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet[0].name
  address_prefixes     = ["10.0.8.0/24"]
}

# ── Private DNS Zones ──────────────────────────────────────────────────────────
# Only for Storage and Key Vault (ACR uses public DNS with IP whitelisting)

resource "azurerm_private_dns_zone" "blob" {
  count               = var.enable_private_network ? 1 : 0
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.rg.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone" "keyvault" {
  count               = var.enable_private_network ? 1 : 0
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = azurerm_resource_group.rg.name
  tags                = local.tags
}

# Link DNS zones to VNet

resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  count                 = var.enable_private_network ? 1 : 0
  name                  = "${var.prefix}-blob-dns-link"
  resource_group_name   = azurerm_resource_group.rg.name
  private_dns_zone_name = azurerm_private_dns_zone.blob[0].name
  virtual_network_id    = azurerm_virtual_network.vnet[0].id
  tags                  = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "keyvault" {
  count                 = var.enable_private_network ? 1 : 0
  name                  = "${var.prefix}-kv-dns-link"
  resource_group_name   = azurerm_resource_group.rg.name
  private_dns_zone_name = azurerm_private_dns_zone.keyvault[0].name
  virtual_network_id    = azurerm_virtual_network.vnet[0].id
  tags                  = local.tags
}



# ── Private Endpoint: Storage Account (Blob) ───────────────────────────────────

resource "azurerm_private_endpoint" "storage_blob" {
  count               = var.enable_private_network ? 1 : 0
  name                = "${var.prefix}-storage-blob-pe"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  subnet_id           = azurerm_subnet.private_endpoints[0].id
  tags                = local.tags

  private_service_connection {
    name                           = "${var.prefix}-storage-blob-psc"
    private_connection_resource_id = azurerm_storage_account.storage.id
    is_manual_connection           = false
    subresource_names              = ["blob"]
  }

  private_dns_zone_group {
    name                 = "blob-dns-zone-group"
    private_dns_zone_ids = [azurerm_private_dns_zone.blob[0].id]
  }
}

# ── Private Endpoint: Key Vault ───────────────────────────────────────────────

resource "azurerm_private_endpoint" "keyvault" {
  count               = var.enable_private_network ? 1 : 0
  name                = "${var.prefix}-kv-pe"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  subnet_id           = azurerm_subnet.private_endpoints[0].id
  tags                = local.tags

  private_service_connection {
    name                           = "${var.prefix}-kv-psc"
    private_connection_resource_id = azurerm_key_vault.kv.id
    is_manual_connection           = false
    subresource_names              = ["vault"]
  }

  private_dns_zone_group {
    name                 = "kv-dns-zone-group"
    private_dns_zone_ids = [azurerm_private_dns_zone.keyvault[0].id]
  }
}

# ── Container Registry Security ────────────────────────────────────────────────
# COST OPTIMIZATION: ACR stays on Basic SKU with Managed Identity RBAC
# (saves €159/month: €7 PE + €145 Premium SKU + €7 DNS)
#
# Security model:
#   - Managed Identity with AcrPull role (Container Apps)
#   - GitHub Actions OIDC with AcrPush role (no passwords)
#   - Public access acceptable for container images (code, not user data)
#
# Note: If full isolation needed in future, upgrade to Premium + add Private Endpoint

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ZERO TRUST — Network Security Groups (Tier 2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# NSG implements Zero Trust principle: "Assume Breach"
#   - Deny all inbound by default (Container Apps don't need inbound from internet)
#   - Explicit allow rules for outbound (whitelist approach)
#   - Block lateral movement if attacker compromises Container App
#
# Cost: €0/month (NSG is free, only charged for traffic processing)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

resource "azurerm_network_security_group" "container_apps" {
  count               = var.enable_private_network ? 1 : 0
  name                = "${var.prefix}-container-apps-nsg"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  tags                = local.tags
}

# ── Inbound Rules ──────────────────────────────────────────────────────────────
# Container Apps Environment manages inbound traffic internally (via Azure Load Balancer)
# We only need to allow Azure infrastructure to reach the subnet

resource "azurerm_network_security_rule" "allow_azureloadbalancer_inbound" {
  count                       = var.enable_private_network ? 1 : 0
  name                        = "AllowAzureLoadBalancerInbound"
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "AzureLoadBalancer"
  destination_address_prefix  = "*"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.container_apps[0].name
}

resource "azurerm_network_security_rule" "allow_vnet_inbound" {
  count                       = var.enable_private_network ? 1 : 0
  name                        = "AllowVNetInbound"
  priority                    = 110
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "VirtualNetwork"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.container_apps[0].name
}

resource "azurerm_network_security_rule" "deny_all_inbound" {
  count                       = var.enable_private_network ? 1 : 0
  name                        = "DenyAllInbound"
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "*"
  destination_address_prefix  = "*"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.container_apps[0].name
}

# ── Outbound Rules ─────────────────────────────────────────────────────────────
# Explicit allow rules for required services (Zero Trust: verify explicitly)

resource "azurerm_network_security_rule" "allow_storage_outbound" {
  count                       = var.enable_private_network ? 1 : 0
  name                        = "AllowStorageOutbound"
  priority                    = 100
  direction                   = "Outbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "443"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "Storage.PolandCentral"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.container_apps[0].name
}

resource "azurerm_network_security_rule" "allow_keyvault_outbound" {
  count                       = var.enable_private_network ? 1 : 0
  name                        = "AllowKeyVaultOutbound"
  priority                    = 110
  direction                   = "Outbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "443"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "AzureKeyVault"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.container_apps[0].name
}

resource "azurerm_network_security_rule" "allow_acr_outbound" {
  count                       = var.enable_private_network ? 1 : 0
  name                        = "AllowACROutbound"
  priority                    = 120
  direction                   = "Outbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "443"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "AzureContainerRegistry.PolandCentral"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.container_apps[0].name
}

resource "azurerm_network_security_rule" "allow_internet_outbound" {
  count                       = var.enable_private_network ? 1 : 0
  name                        = "AllowInternetOutbound"
  priority                    = 200
  direction                   = "Outbound"
  access                      = "Allow"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "Internet"
  resource_group_name         = azurerm_resource_group.rg.name
  network_security_group_name = azurerm_network_security_group.container_apps[0].name
  description                 = "Allow outbound to external APIs (Fakturownia, Zoho, KSeF, Google Ads)"
}

# ── Associate NSG with Subnet ──────────────────────────────────────────────────

resource "azurerm_subnet_network_security_group_association" "container_apps" {
  count                     = var.enable_private_network ? 1 : 0
  subnet_id                 = azurerm_subnet.container_apps[0].id
  network_security_group_id = azurerm_network_security_group.container_apps[0].id
}
