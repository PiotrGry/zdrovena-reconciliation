# Monitoring runbook (R4-C)

Turns the *declared* Azure Monitor config in `monitoring.tf` into *verified,
operational* monitoring: alerts that actually fire and reach the owner.

Resources in scope (all in `monitoring.tf`):

| Resource | Fires when | Signal |
|---|---|---|
| `azurerm_monitor_metric_alert.high_error_rate` | > 5 failed production requests / 5 min | App Insights metric `requests/failed`, role `zdrovena-api-prod` |
| `azurerm_monitor_metric_alert.high_latency` | production avg response > 3000 ms / 5 min | App Insights metric `requests/duration`, role `zdrovena-api-prod` |
| `azurerm_monitor_scheduled_query_rules_alert_v2.dlq_backlog` | any production `dlq.enqueued` / 15 min | KQL over App Insights `traces`, role `zdrovena-api-prod` |

All three send to **one** action group — `azurerm_monitor_action_group.ops`
(`email_receiver` → `var.ops_alert_email`). If that variable is empty or
malformed, `terraform plan` fails (validation in `variables.tf`).

Staging deliberately shares the Application Insights component so test
telemetry remains queryable, but every alert is filtered by
`cloud/roleName = zdrovena-api-prod`. Expected staging 401/403, controlled 5xx
and latency probes must never send production alert e-mails.

---

## 1. Confirm the Log Analytics / App Insights table names

The DLQ rule's `scopes` is the **Application Insights** resource, so its KQL runs
against the App Insights schema (`traces`, `exceptions`, `requests`, …), surfaced
in the linked Log Analytics workspace via `workspace_id`. It does **not** query
the raw `ContainerAppConsoleLogs_CL` custom table.

Verify the `traces` table is populated and the DLQ message shape matches before
trusting the alert:

```bash
# Resolve the workspace GUID
az monitor app-insights component show \
  --app zdrovena-ai --resource-group zdrovena-rg \
  --query "customerId" -o tsv

# Does the structured DLQ event actually land in `traces`?
az monitor app-insights query \
  --app zdrovena-ai --resource-group zdrovena-rg \
  --analytics-query '
    traces
    | extend payload = parse_json(message)
    | where tostring(payload.event) == "dlq.enqueued"
    | project timestamp, severityLevel, payload, operation_Id, cloud_RoleName
    | take 5
  ' \
  -o table
```

If your workspace surfaces the log under a different table/column, adjust the
`query` block in `monitoring.tf` accordingly and re-run section 3.

---

## 2. `terraform plan` / `apply` procedure

> Never run `apply` from a laptop against prod without the owner present. `plan`
> is safe and read-only.

```bash
cd infra/terraform

# Offline sanity (no cloud creds needed) — always run before pushing:
terraform fmt -check
terraform init -backend=false
terraform validate

# Real plan (needs `az login` + backend). Review the diff — expect ONLY
# monitoring changes (alert rule query, action group, variable validation):
terraform init
terraform plan -out=monitoring.tfplan

# Apply only after reviewing the plan and confirming the blast radius:
terraform apply monitoring.tfplan
```

Expected P0 plan changes:

- add production `cloud/roleName` dimensions to failed-request and latency alerts,
- add the production role filter to the DLQ KQL rule,
- no destructive replacement of monitoring, storage or identity resources.

`ops_alert_email` must already be set in `terraform.tfvars`. Stop if the plan
contains unrelated or destructive actions.

---

## 3. Controlled staging probes

Ukryte endpointy testowe są dostępne tylko, gdy `PROVIDER_MODE=fake` i
środowisko nie jest produkcyjne. Nadal wymagają JWT z rolą administratora lub
shipment managera odpowiednio do endpointu. Na produkcji zwracają `404`.

```bash
STAGING_API="https://<staging-fqdn>/api"
TOKEN="<entra-token>"
PROBE_ID="e2e-monitoring-$(date -u +%Y%m%d%H%M%S)"
CID="monitoring-${PROBE_ID}"
```

### Request telemetry and controlled 5xx

Najpierw potwierdź pojedynczy request:

```bash
curl -fsS \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Correlation-ID: $CID-ok" \
  "$STAGING_API/__test__/monitoring/request?response_status=200"
```

Następnie wygeneruj jedną kontrolowaną odpowiedź 500:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Correlation-ID: $CID-5xx" \
  "$STAGING_API/__test__/monitoring/request?response_status=500"
```

Sprawdź requesty po czasie potrzebnym na eksport:

```kql
AppRequests
| where TimeGenerated > ago(15m)
| where OperationId != ""
| where Name contains "__test__/monitoring/request"
| project TimeGenerated, Name, ResultCode, DurationMs, Success,
          OperationId, AppRoleName
| order by TimeGenerated desc
```

Oba requesty muszą mieć `AppRoleName == "zdrovena-api-staging"`. Żaden z nich
nie może uruchomić produkcyjnego alertu.

### Telemetria latency

Wykonaj pojedynczy request z kontrolowanym opóźnieniem:

```bash
curl -fsS -o /dev/null \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Correlation-ID: $CID-latency" \
  "$STAGING_API/__test__/monitoring/request?delay_ms=3500"
```

Potwierdź `DurationMs >= 3500` w `AppRequests`. Stagingowa latencja jest
wykluczona z produkcyjnego alertu.

### Staging DLQ telemetry

1. Użyj wyłącznie **staging**.
2. Dodaj kontrolowany wpis przez chroniony endpoint testowy. Endpoint zapisuje
   prawdziwy rekord przez `ShippingStore.enqueue_dlq` i emituje to samo
   ustrukturyzowane zdarzenie `dlq.enqueued`, ale nie wywołuje dostawców:

   ```bash
   curl -fsS -X POST \
     -H "Authorization: Bearer $TOKEN" \
     -H "X-Correlation-ID: $CID-dlq" \
     -H "Content-Type: application/json" \
     "$STAGING_API/__test__/shipping/dlq" \
     -d "{
       \"id\": \"$PROBE_ID\",
       \"payload\": {\"id\": 990999, \"order_number\": 990999},
       \"error\": \"Controlled monitoring probe\",
       \"source\": \"shopify\"
     }"
   ```

3. Potwierdź zdarzenie `dlq.enqueued` w `AppTraces` z rolą
   `zdrovena-api-staging`.
4. Potwierdź, że produkcyjny alert DLQ nie przeszedł do *Fired*.
5. Usuń kontrolowany rekord:

   ```bash
   curl -fsS -X DELETE \
     -H "Authorization: Bearer $TOKEN" \
     "$STAGING_API/shipping/drafts/dlq/$PROBE_ID"
   ```

### Action group delivery

Niezależnie od reguł można wysłać kontrolowane powiadomienie testowe z poziomu
Azure Portal: **Monitor → Alerts → Action groups → zdrovena-ag-ops → Test**.
Zapisz czas, odbiorcę i potwierdzenie dostarczenia. Nie umieszczaj adresu e-mail
w repozytorium.

Ten sam test można wykonać z CLI bez zapisywania adresu w historii poleceń.
`budget` jest tu wyłącznie obsługiwanym szablonem testowego powiadomienia —
test weryfikuje transport action group, a nie regułę budżetową:

```bash
RECEIVER_NAME="$(az monitor action-group show \
  --resource-group zdrovena-rg \
  --name zdrovena-ag-ops \
  --query 'emailReceivers[0].name' -o tsv)"
RECEIVER_EMAIL="$(az monitor action-group show \
  --resource-group zdrovena-rg \
  --name zdrovena-ag-ops \
  --query 'emailReceivers[0].emailAddress' -o tsv)"

az monitor action-group test-notifications create \
  --resource-group zdrovena-rg \
  --action-group zdrovena-ag-ops \
  --alert-type budget \
  --add-action email "$RECEIVER_NAME" "$RECEIVER_EMAIL" usecommonalertschema \
  --output none

unset RECEIVER_NAME RECEIVER_EMAIL
```

---

## 4. Sensitive-data audit

Eksporter nie powinien zapisywać nagłówków autoryzacyjnych, tokenów ani danych
osobowych. Uruchom oba zapytania dla okna obejmującego testy:

```kql
union isfuzzy=true AppRequests, AppTraces, AppExceptions
| where TimeGenerated > ago(2h)
| extend AuditText = strcat(
    tostring(column_ifexists("Url", "")), " ",
    tostring(column_ifexists("Message", "")), " ",
    tostring(column_ifexists("OuterMessage", "")), " ",
    tostring(column_ifexists("Properties", dynamic({}))))
| where AuditText matches regex
    @"(?i)(authorization|bearer\s+[a-z0-9._-]{10,}|client[_-]?secret|access[_-]?token|refresh[_-]?token|api[_-]?key)"
| project TimeGenerated, Type, AppRoleName
```

```kql
union isfuzzy=true AppRequests, AppTraces, AppExceptions
| where TimeGenerated > ago(2h)
| extend AuditText = strcat(
    tostring(column_ifexists("Url", "")), " ",
    tostring(column_ifexists("Message", "")), " ",
    tostring(column_ifexists("OuterMessage", "")), " ",
    tostring(column_ifexists("Properties", dynamic({}))))
| where AuditText matches regex @"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"
| project TimeGenerated, Type, AppRoleName
```

Oba zapytania powinny zwrócić zero wierszy. Nie wyświetlaj `AuditText` w
artefaktach CI ani komentarzach PR — w razie trafienia przejrzyj rekord
bezpośrednio w ograniczonym dostępowo workspace.

---

## 5. Evidence checklist

Record this in the deploy log / PR comment after the controlled test:

- [ ] KQL query text and UTC window used for evidence.
- [ ] `X-Correlation-ID` każdego kontrolowanego probe.
- [ ] Requesty są widoczne w `AppRequests` z poprawnym `AppRoleName`.
- [ ] Poller emituje `AppTraces` z `AppRoleName=zdrovena-allegro-poller`.
- [ ] Stagingowe 401/5xx/latency/DLQ nie uruchamiają produkcyjnych alertów.
- [ ] Alert rules w Azure mają filtr `cloud/roleName=zdrovena-api-prod`.
- [ ] Test transportu action group zakończył się sukcesem.
- [ ] Audyt tokenów, sekretów i PII zwrócił zero dopasowań.
- [ ] Test DLQ entry cleaned up (retried or discarded).
- [ ] `terraform plan` after apply shows no drift.

Nie generuj kontrolowanych błędów na produkcji. Produkcyjny sygnał jest
weryfikowany przez konfigurację wymiarów, bieżącą telemetrię i osobny test
transportu action group.

---

## 6. Evidence log

### 2026-07-23 — controlled DLQ delivery test

Test wykonano na stagingu, bez generowania błędu produkcyjnego. Tymczasowa
reguła alertu była ograniczona jednocześnie do roli
`zdrovena-api-staging` i jednego correlation ID. Po osiągnięciu stanu `Fired`
regułę usunięto.

| Pole | Wartość |
|---|---|
| Okno dowodowe UTC | `2026-07-23T23:09:58Z`–`2026-07-23T23:15:00Z` |
| Probe ID | `e2e-monitoring-20260723230958` |
| Correlation ID | `monitoring-e2e-monitoring-20260723230958` |
| App Insights trace | `2026-07-23T23:10:49.855208Z` |
| Operation ID | `788d471677d6b68404c50d38985f7796` |
| Service role | `zdrovena-api-staging` |
| Event | `dlq.enqueued`, severity `3`, `test_probe=true` |
| Tymczasowa reguła | `zdrovena-alert-dlq-e2e-20260723230958` |
| Alert `Fired` | `2026-07-23T23:14:29.1338006Z`, Sev4 |
| Cleanup | wpis DLQ: HTTP 204; reguła: usunięta; staging: `min=0, max=1` |

Zapytanie użyte do powiązania śladu z probe:

```kql
traces
| where timestamp between (
    datetime(2026-07-23T23:09:58Z) ..
    datetime(2026-07-23T23:15:00Z)
  )
| extend payload = parse_json(message)
| where severityLevel >= 3
| where cloud_RoleName == "zdrovena-api-staging"
| where tostring(payload.event) == "dlq.enqueued"
| where tostring(payload.correlation_id)
    == "monitoring-e2e-monitoring-20260723230958"
| project timestamp, cloud_RoleName, operation_Id, severityLevel,
          event=tostring(payload.event),
          correlation_id=tostring(payload.correlation_id),
          test_probe=tobool(payload.test_probe)
```

Produkcyjna reguła `zdrovena-alert-dlq-backlog` nie przeszła do `Fired` od
stagingowego probe, co potwierdza działanie filtra
`cloud_RoleName == "zdrovena-api-prod"`. Kontrolny plan po apply:
[GitHub Actions run 30052062271](https://github.com/PiotrGry/zdrovena-reconciliation/actions/runs/30052062271)
zakończył się wynikiem `No changes`; job `Apply` został pominięty.

Do zamknięcia dowodu pozostaje potwierdzenie przez właściciela, że wiadomość
wysłana przez `zdrovena-ag-ops` faktycznie dotarła do skonfigurowanej skrzynki.
