# Monitoring runbook — alerty Azure Monitor (R4-C)

Cel: zamienić „zadeklarowany" monitoring w monitoring **zweryfikowany operacyjnie**.
Runbook opisuje procedurę `terraform plan/apply`, kontrolowany test alertu DLQ
oraz checklist dowodów.

## 1. Architektura dostarczania logów

| Źródło | Tabela Log Analytics | Kolumna z treścią |
| --- | --- | --- |
| stdout/stderr Container App (`zdrovena-api`) | `ContainerAppConsoleLogs_CL` | `Log_s` |
| Azure Monitor OpenTelemetry (`APPLICATIONINSIGHTS_CONNECTION_STRING`) | `AppTraces` | `Message` |

Uwagi:

- `ContainerAppConsoleLogs_CL` powstaje w workspace dopiero po **pierwszym logu**
  Container App — świeży workspace jej nie ma (stąd `union isfuzzy=true` w KQL).
- Application Insights jest workspace-based (`infra/terraform/monitoring.tf`),
  więc `traces` z klasycznego App Insights odpowiada tabeli `AppTraces` w LAW.
- Alert DLQ jest zescope'owany na **Log Analytics Workspace** (`law`), nie na
  komponent App Insights — scope na komponencie nie widzi
  `ContainerAppConsoleLogs_CL`.

## 2. Alerty i action group

Wszystkie trzy reguły (`monitoring.tf`) mają podpięty action group
`${prefix}-ag-ops` (e-mail do `var.ops_alert_email`):

1. `${prefix}-alert-error-rate` — >5 nieudanych requestów w 5 min (severity 1),
2. `${prefix}-alert-latency` — średni czas odpowiedzi >3000 ms w 5 min (severity 2),
3. `${prefix}-alert-dlq-backlog` — dowolny nowy wpis DLQ w oknie 15 min (severity 1).

KQL alertu DLQ (dopasowany do rzeczywistych zdarzeń aplikacji):

```kusto
union isfuzzy=true
  (ContainerAppConsoleLogs_CL | project TimeGenerated, LogText = Log_s),
  (AppTraces | project TimeGenerated, LogText = Message)
| where LogText has "draft.dlq_enqueued" or LogText has "enqueueing to DLQ"
```

- `draft.dlq_enqueued` — ustrukturyzowane zdarzenie JSON emitowane przez
  `log_event` po udanym `enqueue_dlq` (`zdrovena/api/routers/webhooks.py`,
  `_create_draft_safely`); zawiera `correlation_id`, `order_id`, `source`, `error`.
- `enqueueing to DLQ` — towarzyszący log `logger.exception` (fallback, gdyby
  zdarzenie strukturalne nie zostało wyemitowane).

## 3. Procedura `terraform plan/apply`

```bash
cd infra/terraform

# 1. Inicjalizacja (backend Azure Storage — patrz backend.hcl)
terraform init -backend-config=backend.hcl

# 2. Walidacja składni/typów (bez dostępu do Azure)
terraform validate

# 3. Plan — sprawdź, że zawiera wyłącznie oczekiwane zmiany monitoringu:
#    ~ azurerm_monitor_scheduled_query_rules_alert_v2.dlq_backlog
#      (scopes: App Insights → Log Analytics Workspace, nowy KQL)
terraform plan -out=tfplan

# 4. Apply — wyłącznie po ręcznej akceptacji planu przez właściciela
terraform apply tfplan
```

Zasady: `terraform apply` wymaga jawnej zgody właściciela; nie uruchamiać z CI
bez manualnego review (workflow „Terraform apply" ma bramkę approvera).

## 4. Kontrolowany test alertu DLQ (staging)

Nie testować na produkcji. Test wykonuje się na stagingu i polega na wywołaniu
kontrolowanej porażki utworzenia draftu:

1. Zanotuj czas startu testu (UTC) — będzie granicą okna w KQL.
2. Wyślij na staging webhook Shopify z celowo niepoprawnym payloadem, który
   przechodzi weryfikację HMAC, ale wywala `_create_draft` (np. zamówienie bez
   `shipping_address` i bez rozpoznawalnej metody dostawy), z jawnym nagłówkiem
   `X-Correlation-ID: dlq-alert-test-<data>`:

   ```bash
   # przykład — dokładny payload zależny od sekretu HMAC stagingu
   ./scripts/... # użyj istniejącego narzędzia seed/webhook-replay dla stagingu
   ```

3. Potwierdź wpis w DLQ: `GET /api/shipping/drafts/dlq` (token viewer/accountant).
4. W Log Analytics uruchom KQL alertu (sekcja 2) zawężony do okna testu
   i correlation ID:

   ```kusto
   union isfuzzy=true
     (ContainerAppConsoleLogs_CL | project TimeGenerated, LogText = Log_s),
     (AppTraces | project TimeGenerated, LogText = Message)
   | where TimeGenerated > datetime(<start-testu-UTC>)
   | where LogText has "draft.dlq_enqueued"
   | where LogText has "dlq-alert-test-<data>"
   ```

5. Odczekaj do 5 min (evaluation_frequency) — alert powinien przejść w stan
   *Fired* i wysłać e-mail na `ops_alert_email`.
6. Po teście: retry/discard wpisu DLQ
   (`POST /api/shipping/drafts/dlq/{entry_id}/retry` lub discard), aby nie
   zostawiać sztucznego backlogu.

## 5. Checklist dowodów (do wklejenia w Issue)

- [ ] `terraform validate` — wynik `Success! The configuration is valid.`
- [ ] `terraform plan` — diff zawiera tylko oczekiwane zmiany alertu/action group.
- [ ] `terraform apply` — wykonany przez właściciela, bez błędów.
- [ ] Zapytanie KQL + **okno czasowe** (UTC start/koniec) użyte w teście.
- [ ] **Correlation ID** testowego zdarzenia (`dlq-alert-test-<data>`).
- [ ] Zrzut/link: alert w stanie *Fired* w Azure Monitor.
- [ ] Zrzut/nagłówki: e-mail dostarczony na skonfigurowany adres `ops_alert_email`.
- [ ] Wpis DLQ posprzątany (retry/discard) po zakończeniu testu.
