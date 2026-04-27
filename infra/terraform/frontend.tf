# ── Static Web App (frontend) ──────────────────────────────────────────────────
# SWA serves the JS/React/Vue bundle from Azure CDN.
# /api/* routes are proxied by the SWA edge to the Container App above;
# the browser never learns the Container App URL.
# Standard SKU required for linked backend feature.

resource "azurerm_static_web_app" "ui" {
  name                = "${var.prefix}-ui"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.swa_location
  sku_tier            = "Standard"
  sku_size            = "Standard"
  tags                = local.tags
}

# Custom domain — opt-in via swa_custom_domain variable.
# Prereq: the hostname must already CNAME to azurerm_static_web_app.ui.default_host_name
# (apex domains use ALIAS / ANAME or a TXT-validated record). SWA provisions the
# managed certificate automatically once validation succeeds.
resource "azurerm_static_web_app_custom_domain" "ui" {
  count             = var.swa_custom_domain == "" ? 0 : 1
  static_web_app_id = azurerm_static_web_app.ui.id
  domain_name       = var.swa_custom_domain
  validation_type   = "cname-delegation"
}
