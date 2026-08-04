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

    # Staging smoke tests celowo generują 401/403 i kontrolowane 5xx.
    # Powiadomienia operacyjne dotyczą wyłącznie produkcyjnego API.
    dimension {
      name     = "cloud/roleName"
      operator = "Include"
      values   = ["${var.prefix}-api-prod"]
    }
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

    dimension {
      name     = "cloud/roleName"
      operator = "Include"
      values   = ["${var.prefix}-api-prod"]
    }
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
      | where cloud_RoleName == "${var.prefix}-api-prod"
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

# ── Wynik biznesowy: zamówienia bez numeru nadania po 48h ────────────────────
#
# Awaria mapowania sendera InPost (lipiec 2026) zabiła 100% przesyłek
# kurierskich na kilka tygodni i NIE wywołała żadnego alertu:
#   - `alert-error-rate` liczy nieudane żądania HTTP z progiem >5/5min, a awaria
#     dawała 1–2 dziennie (operator próbuje raz i rezygnuje),
#   - `alert-latency` mierzy czas, a zepsuta przesyłka odpowiada szybko błędem,
#   - `alert-dlq-backlog` obejmował wtedy wyłącznie nieudane *tworzenie* draftu.
#
# Sygnał pochodzi z aktualnego stanu ShippingStore, nie z rekonstrukcji historii
# `draft.created` / `draft.tracking_assigned`. Poller emituje co cykl zdarzenie
# `shipping.orders_without_tracking_snapshot` po odfiltrowaniu draftów z
# trackingiem, anulowanych i już zrealizowanych. Dzięki temu ręczne nadanie jest
# traktowane tak samo jak automatyczne.
#
# Próg czasowy 48h jest świadomą decyzją biznesową: systemowy zator ma wyjść na
# jaw po dwóch dniach, a nie dopiero po tygodniach — tyle przetrwała awaria
# sendera.
#
# Podział odpowiedzialności z `dlq_backlog`: tamta reguła łapie każdą pojedynczą
# porażkę (także nieudane *wykonanie*, kind=draft_execution), więc pokrywa awarie
# częściowe, np. kurier leży a paczkomat jeździ. Ta reguła łapie przypadek, w
# którym nikt nawet nie próbuje wykonać draftu — wtedy nie ma żadnego wpisu w
# DLQ i widać to tylko po braku trackingu.
#
# Snapshot rozwiązuje limit maksymalnego 2-dniowego okna Azure Monitor: wiek
# liczy kod względem `created_at`, więc próg wynosi naprawdę >=48h, a nie 46h.
# Regułę można wdrożyć bez backfillu historycznej telemetrii — pierwsze zdarzenie
# opisuje bieżący stan tabeli. Gdy backlog zniknie, kolejne snapshoty mają count=0
# i stanowy alert automatycznie się zamknie.

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "orders_without_tracking" {
  name                = "${var.prefix}-alert-no-tracking"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  description         = "Zamówienia bez numeru nadania po 48h — wysyłka stoi (niezależnie od tego, kto nadaje)"
  severity            = 2

  evaluation_frequency = "PT30M"
  window_duration      = "PT1H"
  scopes               = [azurerm_application_insights.ai.id]

  criteria {
    query                   = <<-KQL
      traces
      | where cloud_RoleName == "${var.prefix}-allegro-poller"
      | extend payload = parse_json(message)
      | where tostring(payload.event) == "shipping.orders_without_tracking_snapshot"
      | extend overdue_count = toint(payload.overdue_count)
      | where overdue_count > 0
      | project timestamp, overdue_count,
                draft_ids = tostring(payload.draft_ids),
                oldest_age_hours = todouble(payload.oldest_age_hours)
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  auto_mitigation_enabled = true

  action {
    action_groups = [azurerm_monitor_action_group.ops.id]
  }

  tags = local.tags
}

# ── Alert: poller Allegro sypie błędami ──────────────────────────────────────
#
# Poller jest niewidoczny dla wszystkich pozostałych reguł w tym pliku:
# `alert-error-rate` i `alert-latency` opierają się na metrykach żądań HTTP
# (`requests/failed`, `requests/duration`), a Container App Job nie obsługuje
# ŻADNEGO ruchu HTTP — to proces cron, który startuje, robi swoje i umiera.
# W efekcie poller mógłby wywalać się w każdym cyklu, a nikt by się o tym nie
# dowiedział. Ta reguła zamyka tę lukę, patrząc na `exceptions` filtrowane po
# roli pollera.
#
# Próg 2/h (a nie 0): w analizowanym tygodniu Allegro zwróciło dwa przejściowe
# `CourierServerError 503` (29 i 31.07). Pojedyncze 503 od dostawcy to szum, nie
# awaria — poller ponawia w następnym cyklu. Próg > 2 na godzinę przepuszcza taki
# szum, ale poller psujący się w każdym cyklu przekroczy go natychmiast.
#
# UWAGA przy kalibracji na historii: telemetria sprzed ~23.07.2026 ma
# cloud_RoleName = "unknown_service" (service.name w OTel naprawione deployem
# z 24.07), więc filtr po roli nie zobaczy tamtych wyjątków. Zliczanie błędów
# pollera sprzed tej daty pokaże zero, co nie znaczy, że ich nie było.

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "allegro_poller_failing" {
  name                = "${var.prefix}-alert-poller-failing"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  description         = "Poller Allegro zgłasza błędy — niewidoczny dla alert-error-rate, bo Joby nie obsługują HTTP"
  severity            = 2

  evaluation_frequency = "PT15M"
  window_duration      = "PT1H"
  scopes               = [azurerm_application_insights.ai.id]

  criteria {
    query                   = <<-KQL
      exceptions
      | where cloud_RoleName == "${var.prefix}-allegro-poller"
    KQL
    time_aggregation_method = "Count"
    threshold               = 2
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  auto_mitigation_enabled = true

  action {
    action_groups = [azurerm_monitor_action_group.ops.id]
  }

  tags = local.tags
}
