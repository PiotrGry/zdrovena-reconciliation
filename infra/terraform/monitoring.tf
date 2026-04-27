# ── Application Insights ──────────────────────────────────────────────────────
# Provides traces, exceptions, performance metrics, and alerting.
# Wire during Node.js migration: npm install @azure/monitor-opentelemetry

resource "azurerm_application_insights" "ai" {
  name                = "${var.prefix}-ai"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  workspace_id        = azurerm_log_analytics_workspace.law.id
  application_type    = "web"
  tags                = local.tags
}

# ── Alert: high error rate (5xx > 1% over 5 minutes) ─────────────────────────

resource "azurerm_monitor_metric_alert" "high_error_rate" {
  name                = "${var.prefix}-alert-error-rate"
  resource_group_name = azurerm_resource_group.rg.name
  scopes              = [azurerm_application_insights.ai.id]
  description         = "More than 5 failed requests in 5 minutes — action required"
  severity            = 1
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "microsoft.insights/components"
    metric_name      = "requests/failed"
    aggregation      = "Count"
    operator         = "GreaterThan"
    threshold        = 5
  }

  tags = local.tags
}

# ── Alert: high latency (p95 > 3s over 5 minutes) ────────────────────────────

resource "azurerm_monitor_metric_alert" "high_latency" {
  name                = "${var.prefix}-alert-latency"
  resource_group_name = azurerm_resource_group.rg.name
  scopes              = [azurerm_application_insights.ai.id]
  description         = "Average response time exceeded 3 seconds (3000ms)"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "microsoft.insights/components"
    metric_name      = "requests/duration"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 3000
  }

  tags = local.tags
}
