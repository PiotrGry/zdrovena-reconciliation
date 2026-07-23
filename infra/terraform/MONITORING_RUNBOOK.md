# Monitoring runbook (R4-C)

Turns the *declared* Azure Monitor config in `monitoring.tf` into *verified,
operational* monitoring: alerts that actually fire and reach the owner.

Resources in scope (all in `monitoring.tf`):

| Resource | Fires when | Signal |
|---|---|---|
| `azurerm_monitor_metric_alert.high_error_rate` | > 5 failed requests / 5 min | App Insights metric `requests/failed` |
| `azurerm_monitor_metric_alert.high_latency` | avg response > 3000 ms / 5 min | App Insights metric `requests/duration` |
| `azurerm_monitor_scheduled_query_rules_alert_v2.dlq_backlog` | any persisted `dlq.enqueued` / 15 min | KQL over App Insights `traces` |

All three send to **one** action group — `azurerm_monitor_action_group.ops`
(`email_receiver` → `var.ops_alert_email`). If that variable is empty or
malformed, `terraform plan` fails (validation in `variables.tf`).

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

- update KQL in `azurerm_monitor_scheduled_query_rules_alert_v2.dlq_backlog`,
- add `OTEL_SERVICE_NAME` to API prod, API staging and Allegro poller,
- create new Container App revisions only where Azure requires them,
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

### Request telemetry i alert failed requests

Najpierw potwierdź pojedynczy request:

```bash
curl -fsS \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Correlation-ID: $CID-ok" \
  "$STAGING_API/__test__/monitoring/request?response_status=200"
```

Następnie wygeneruj sześć kontrolowanych odpowiedzi 500 — próg alertu wynosi
`Count > 5` w oknie 5 minut:

```bash
for i in 1 2 3 4 5 6; do
  curl -sS -o /dev/null -w "%{http_code}\n" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Correlation-ID: $CID-5xx-$i" \
    "$STAGING_API/__test__/monitoring/request?response_status=500"
done
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

### Alert latency

Wykonaj kilka requestów z kontrolowanym opóźnieniem. Użyj osobnego, spokojnego
okna testowego, ponieważ reguła mierzy średnią wszystkich requestów:

```bash
for i in 1 2 3 4 5 6; do
  curl -fsS -o /dev/null \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Correlation-ID: $CID-latency-$i" \
    "$STAGING_API/__test__/monitoring/request?delay_ms=3500"
done
```

### Alert DLQ

Goal: prove the DLQ alert fires and the e-mail arrives, without corrupting real
data.

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

3. Within ~5–15 min (evaluation freq 5 min, window 15 min) the
   `zdrovena-alert-dlq-backlog` rule should transition to *Fired*.
4. Confirm the e-mail lands at `var.ops_alert_email`.
5. Clean up the controlled record:

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

## 4. Evidence checklist

Record this in the deploy log / PR comment after the controlled test:

- [ ] KQL query text used (paste exact query from the fired alert).
- [ ] Time window of the firing (UTC start–end).
- [ ] `X-Correlation-ID` każdego kontrolowanego probe.
- [ ] Requesty są widoczne w `AppRequests` z poprawnym `AppRoleName`.
- [ ] Alert failed requests przeszedł do *Fired*.
- [ ] Alert latency przeszedł do *Fired*.
- [ ] Alert DLQ przeszedł do *Fired*.
- [ ] E-mail received at the configured recipient (timestamp + subject).
- [ ] Test DLQ entry cleaned up (retried or discarded).
- [ ] `terraform plan` after apply shows no drift.

Only when every box is ticked is the DLQ alert considered *operational*, not just
*declared*.
