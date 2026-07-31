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
# DLACZEGO poprzednia wersja tej reguły (`no_shipments_despite_drafts`, ec28a2c)
# była zła: porównywała `draft.created` z `shipment.created`, a `shipment.created`
# emitujemy WYŁĄCZNIE wtedy, gdy przesyłkę tworzy nasza automatyzacja. Wysyłka
# jest dziś nadawana RĘCZNIE przez panele przewoźników, więc `shipment.created`
# jest trwale zerowe i reguła alarmowałaby bez końca — klasyczny alert, który
# uczy operatora ignorować powiadomienia.
#
# Sygnał, którego naprawdę pilnujemy, to brak numeru nadania, a nie brak naszego
# własnego sukcesu. `draft.tracking_assigned` pojawia się niezależnie od tego,
# kto nadał paczkę (automat czy człowiek w panelu), więc reguła mierzy realny
# stan wysyłki, a nie ścieżkę, którą przebiegła.
#
# Próg czasowy 48h: dość długo, by zamówienie z piątku spokojnie poczekało do
# poniedziałku bez alarmu, i dość krótko, by systemowy zator wyszedł na jaw w
# dwa dni, a nie po tygodniach — tyle przetrwała awaria sendera.
#
# Podział odpowiedzialności z `dlq_backlog`: tamta reguła łapie każdą pojedynczą
# porażkę (także nieudane *wykonanie*, kind=draft_execution), więc pokrywa awarie
# częściowe, np. kurier leży a paczkomat jeździ. Ta reguła łapie przypadek, w
# którym nikt nawet nie próbuje wykonać draftu — wtedy nie ma żadnego wpisu w
# DLQ i widać to tylko po braku trackingu.
#
# Drafty tworzy zarówno API, jak i poller Allegro — dlatego filtr ról obejmuje
# obie. Zdarzenia `draft.tracking_assigned` nie filtrujemy po roli, bo tracking
# może dopisać także backfill/import spoza tych dwóch ról.
#
# UWAGA przy analizie historii: telemetria sprzed ~23.07.2026 ma
# cloud_RoleName = "unknown_service" (service.name w OTel zostało naprawione
# deployem z 24.07). Zapytania z filtrem po roli nie zobaczą tamtych rekordów,
# co przy kalibracji progu potrafi mylnie pokazać, że przesyłek było zero,
# podczas gdy w rzeczywistości były.
#
# ZALEŻNOŚĆ WDROŻENIOWA: ta reguła nie może trafić na produkcję przed deployem
# kodu emitującego `draft.tracking_assigned` — bez tych zdarzeń każdy draft
# starszy niż 48h wygląda na nienadany.
#
# ODSTĘPSTWO OD PROJEKTU (okno P7D): Azure Monitor log search alerts twardo
# ograniczają zakres czasowy zapytania do 2 dni — zarówno `window_duration`,
# jak i `query_time_range_override` przyjmują maksymalnie "P2D" (provider
# odrzuca "P7D" już na etapie `terraform validate`). Siedmiodniowy przegląd
# wstecz jest więc niewykonalny w tym typie reguły.
#
# Zamiast przeglądu retrospektywnego reguła działa krawędziowo: przy oknie 2 dni
# najstarszy widoczny draft ma dokładnie 48h, więc łapiemy drafty w paśmie
# 46–48h życia. Przy `evaluation_frequency = PT1H` każdy draft przechodzi przez
# to pasmo w 2 kolejnych ewaluacjach (zapas na spóźniony przebieg), a próg 48h —
# jedyny parametr, który był świadomą decyzją biznesową — zostaje zachowany.
#
# Konsekwencja, o której trzeba wiedzieć: alert sam się zamyka, gdy draft
# wypadnie z pasma, nawet jeśli tracking nadal nie powstał. Przy awarii
# systemowej to nie szkodzi — kolejne drafty wchodzą w pasmo i alert odpala
# ponownie. Przy pojedynczym zapomnianym zamówieniu dostajemy alert trwający
# ~2h. Jeśli okaże się to za słabe, alternatywą jest obniżenie progu do
# `ago(24h)` (ciągły alert przez dobę kosztem fałszywek weekendowych) — to
# decyzja właściciela, nie zmiana techniczna.

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "orders_without_tracking" {
  name                = "${var.prefix}-alert-no-tracking"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  description         = "Zamówienia bez numeru nadania po 48h — wysyłka stoi (niezależnie od tego, kto nadaje)"
  severity            = 2

  evaluation_frequency = "PT1H"
  window_duration      = "P2D"
  scopes               = [azurerm_application_insights.ai.id]

  criteria {
    # Okno zapytania to P2D (patrz komentarz wyżej), więc `ago(46h)` wycina
    # drafty w wieku 46–48h — te, które właśnie przekraczają próg 48h.
    query                   = <<-KQL
      let created = traces
      | where timestamp < ago(46h)
      | where cloud_RoleName in ("${var.prefix}-api-prod", "${var.prefix}-allegro-poller")
      | extend payload = parse_json(message)
      | where tostring(payload.event) == "draft.created"
      | extend draft_id = tostring(payload.draft_id)
      | distinct draft_id;
      let tracked = traces
      | extend payload = parse_json(message)
      | where tostring(payload.event) == "draft.tracking_assigned"
      | extend draft_id = tostring(payload.draft_id)
      | distinct draft_id;
      created
      | join kind=leftanti tracked on draft_id
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
