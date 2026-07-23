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

# ── Action group: powiadamia właściciela e-mailem ────────────────────────────
# Bez odbiorcy alerty istniały, ale nikt nie był powiadamiany ([LOG] H1 —
# największe "pozorne bezpieczeństwo" audytu monitoringu). Ten action_group jest
# podpięty do wszystkich reguł alertów poniżej.

resource "azurerm_monitor_action_group" "ops" {
  name                = "${var.prefix}-ag-ops"
  resource_group_name = azurerm_resource_group.rg.name
  short_name          = "zdrovena" # max 12 znaków

  email_receiver {
    name          = "owner"
    email_address = var.ops_alert_email
  }

  tags = local.tags
}

# ── Alert: failed request count (> 5 over 5 minutes) ──────────────────────────
#
# Terraform address zachowuje historyczną nazwę ``high_error_rate``, aby nie
# wymuszać destroy/create istniejącej reguły. Sygnał jest licznikiem błędnych
# requestów, a nie procentowym error rate.

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

  action {
    action_group_id = azurerm_monitor_action_group.ops.id
  }

  tags = local.tags
}

# ── Alert: average request latency (> 3s over 5 minutes) ──────────────────────

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

  action {
    action_group_id = azurerm_monitor_action_group.ops.id
  }

  tags = local.tags
}

# ── Alert: DLQ backlog (dowolna nowa porażka trafiająca do DLQ) ──────────────
# DLQ to Azure Table Storage (shippingdraftsdlq), nie kolejka — brak natywnej
# metryki "liczba wiadomości". Reguła oparta na ustrukturyzowanym zdarzeniu
# ``dlq.enqueued``, emitowanym dopiero po udanym zapisie wpisu do DLQ. Dzięki
# temu nie alarmuje o samym zamiarze zapisu ani o awarii DLQ storage. Próg > 0
# w oknie 15 min ⇒ każde nowe niepowodzenie powiadamia właściciela
# ([LOG] H3, [EVT] R2/H3, [API] M3).
#
# WAŻNE — schemat: scope tej reguły to zasób Application Insights
# (azurerm_application_insights.ai), więc KQL działa na schemacie App Insights
# (tabele ``traces`` / ``exceptions`` mapowane do workspace'u Log Analytics przez
# ``workspace_id``), a NIE na surowej tabeli ContainerAppConsoleLogs_CL. Log
# ``log_event`` trafia do ``traces`` jako JSON z ``severityLevel = 3`` (Error).
#
# Procedurę weryfikacji nazw tabel, `terraform plan/apply`, kontrolowany
# test-alert i checklistę dowodową opisuje infra/terraform/MONITORING_RUNBOOK.md.

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "dlq_backlog" {
  name                = "${var.prefix}-alert-dlq-backlog"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  description         = "Nowy wpis w DLQ (nieudane utworzenie draftu) — wymaga retry/discard przez operatora"
  severity            = 1

  evaluation_frequency = "PT5M"
  window_duration      = "PT15M"
  scopes               = [azurerm_application_insights.ai.id]

  criteria {
    query                   = <<-KQL
      traces
      | extend payload = parse_json(message)
      | where severityLevel >= 3
      | where tostring(payload.event) == "dlq.enqueued"
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  auto_mitigation_enabled = false

  action {
    action_groups = [azurerm_monitor_action_group.ops.id]
  }

  tags = local.tags
}
