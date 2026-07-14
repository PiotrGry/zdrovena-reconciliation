# Monitoring runbook (R4-C)

Turns the *declared* Azure Monitor config in `monitoring.tf` into *verified,
operational* monitoring: alerts that actually fire and reach the owner.

Resources in scope (all in `monitoring.tf`):

| Resource | Fires when | Signal |
|---|---|---|
| `azurerm_monitor_metric_alert.high_error_rate` | > 5 failed requests / 5 min | App Insights metric `requests/failed` |
| `azurerm_monitor_metric_alert.high_latency` | avg response > 3000 ms / 5 min | App Insights metric `requests/duration` |
| `azurerm_monitor_scheduled_query_rules_alert_v2.dlq_backlog` | any new DLQ enqueue / 15 min | KQL over App Insights `traces` |

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
  --query "id" -o tsv

# Does the DLQ log actually land in `traces`? (run after at least one deploy)
az monitor app-insights query \
  --app zdrovena-ai --resource-group zdrovena-rg \
  --analytics-query 'traces | where message has "enqueueing to DLQ" | take 5' \
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

Expected `plan` changes for this PR: an update to
`azurerm_monitor_scheduled_query_rules_alert_v2.dlq_backlog` (KQL query text) and
no destructive actions. `ops_alert_email` must already be set in
`terraform.tfvars`.

---

## 3. Controlled test-alert procedure (DLQ)

Goal: prove the DLQ alert fires and the e-mail arrives, without corrupting real
data.

1. Pick a **staging** environment (never prod).
2. Trigger exactly one DLQ enqueue by sending a deliberately malformed Shopify
   order webhook to staging (a payload that passes HMAC but fails
   `_create_draft`, e.g. missing required address fields). This exercises the
   real `_create_draft_safely` → `logger.exception("... enqueueing to DLQ")`
   path — do **not** hand-write rows into the DLQ table.
3. Note the `X-Correlation-ID` returned by the webhook response.
4. Within ~5–15 min (evaluation freq 5 min, window 15 min) the
   `zdrovena-alert-dlq-backlog` rule should transition to *Fired*.
5. Confirm the e-mail lands at `var.ops_alert_email`.
6. Clean up: retry or discard the test DLQ entry via
   `POST /shipping/drafts/dlq/{entry_id}/retry` (or discard).

---

## 4. Evidence checklist

Record this in the deploy log / PR comment after the controlled test:

- [ ] KQL query text used (paste exact query from the fired alert).
- [ ] Time window of the firing (UTC start–end).
- [ ] `X-Correlation-ID` of the test order that produced the DLQ entry.
- [ ] Alert transitioned to *Fired* in Azure Portal (screenshot / rule ID).
- [ ] E-mail received at the configured recipient (timestamp + subject).
- [ ] Test DLQ entry cleaned up (retried or discarded).
- [ ] `terraform plan` after apply shows no drift.

Only when every box is ticked is the DLQ alert considered *operational*, not just
*declared*.
