# TODOS

## Infrastructure — Production SLA Upgrade (Target 1: 99.75%)

### Overview
- **Goal:** Upgrade from ~98% SLA (7 days downtime/year) to 99.75% SLA (22 hours downtime/year)
- **Cost:** +€35/month (+€420/year)
- **Timeline:** 1-2 weeks
- **Priority:** P1 (blocker for production launch)
- **Status:** Not started

### Prerequisites
- [ ] Terraform refactor complete (split files, api_prod naming) — ✅ DONE
- [ ] Backend state working (backend.hcl configured)
- [ ] GitHub Secrets up to date (AZURE_CLIENT_ID, etc.)
- [ ] Maintenance window scheduled (1-2 hours, low-traffic period)

---

### Task 1: Container App — Always-On Configuration
- **What:** Change `min_replicas = 0` → `min_replicas = 1` for prod Container App
- **Why:** Eliminates cold-start delays, ensures always 1 instance running
- **SLA impact:** 99.0% → 99.95% (Container Apps with 2+ instances guarantee)
- **Cost:** ~€15/month (0.5 vCPU × 1 GiB × 730 hours)
- **Effort:** XS (~5 min)
- **Priority:** P1
- **Status:** ✅ DONE (2026-07-09) — live prod was already running `min_replicas=1`
  (applied outside Terraform at some point), `compute.tf` just hadn't caught up.
  Fixed the drift in code instead of reverting it. Also wired
  `TF_VAR_swa_custom_domain` into `terraform.yml` — CI's plan/apply had never
  set it, so any CI-driven `terraform apply` would have destroyed the live
  `portal.wodahumio.pl` custom domain (created outside CI, same root cause).

**Implementation:**
```terraform
# infra/terraform/compute.tf
module "api_prod" {
  min_replicas = 1  # ← change from 0
  max_replicas = 3  # ← increase from 2 for traffic spikes
}

module "api_staging" {
  min_replicas = 0  # ← keep staging as scale-to-zero (cost optimization)
  max_replicas = 1
}
```

**Validation:**
```bash
terraform plan  # should show: min_replicas 0 → 1
terraform apply
az containerapp show --name zdrovena-api-prod --resource-group zdrovena-rg \
  --query 'properties.template.scale.minReplicas' -o tsv
# Expected: 1
```

**Rollback:**
```bash
# If issues, revert min_replicas to 0
terraform apply -var="container_app_min_replicas_prod=0"
```

---

### Task 2: Storage Account — Zone Redundancy
- **What:** Upgrade Storage Account from LRS (Local Redundant) to ZRS (Zone Redundant)
- **Why:** Data replicated across 3 availability zones in polandcentral (survives datacenter failure)
- **SLA impact:** 99.0% → 99.9% for write operations
- **Cost:** ~€2/month (minimal increase for ZRS)
- **Effort:** S (~15 min, requires data migration)
- **Priority:** P1
- **⚠️ Warning:** LRS → ZRS requires recreate (Azure limitation), causes brief downtime

**Implementation:**
```terraform
# infra/terraform/storage.tf
resource "azurerm_storage_account" "storage" {
  account_replication_type = "ZRS"  # ← change from "LRS"
  # ... rest unchanged
}
```

**Migration steps:**
1. **Backup existing data** (optional, soft-delete already enabled):
   ```bash
   # List current blobs
   az storage blob list --account-name zdrovenafiles --container-name zdrovena-files -o table
   ```

2. **Apply Terraform change:**
   ```bash
   terraform plan  # will show: account_replication_type LRS → ZRS (forces replacement)
   terraform apply
   ```
   **Downtime:** ~2-3 minutes (Storage Account recreate + data sync)

3. **Verify replication:**
   ```bash
   az storage account show --name zdrovenafiles --resource-group zdrovena-rg \
     --query 'sku.name' -o tsv
   # Expected: Standard_ZRS
   ```

**Rollback:**
```terraform
account_replication_type = "LRS"  # revert if issues
```

---

### Task 3: Static Web App — Standard Tier Upgrade
- **What:** Upgrade Static Web App from Free to Standard tier
- **Why:** Guaranteed SLA (99.9%), custom domains with auto-SSL, staging environments
- **SLA impact:** Best-effort → 99.9%
- **Cost:** €8/month
- **Effort:** XS (~5 min)
- **Priority:** P1

**Implementation:**
```terraform
# infra/terraform/frontend.tf
resource "azurerm_static_web_app" "ui" {
  sku_tier = "Standard"  # ← already set ✅
  sku_size = "Standard"  # ← already set ✅
}
```

**Status:** ✅ Already configured in current Terraform (no changes needed)

**Validation:**
```bash
az staticwebapp show --name zdrovena-ui --resource-group zdrovena-rg \
  --query 'sku.{tier:tier,name:name}' -o table
# Expected: Standard
```

---

### Task 4: Application Insights — Extended Retention
- **What:** Increase log retention from 30 days to 90 days
- **Why:** Compliance (audit trail), better debugging (see issues from 2 months ago)
- **SLA impact:** No direct impact, but improves incident response
- **Cost:** ~€10/month (depends on ingestion volume)
- **Effort:** XS (~5 min)
- **Priority:** P2 (nice-to-have, not blocking)

**Implementation:**
```terraform
# infra/terraform/monitoring.tf
resource "azurerm_application_insights" "ai" {
  name                = "${var.prefix}-ai"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  workspace_id        = azurerm_log_analytics_workspace.law.id
  application_type    = "web"
  retention_in_days   = 90  # ← add this (default: 90, but set explicitly)
  tags                = local.tags
}

resource "azurerm_log_analytics_workspace" "law" {
  # ... existing config
  retention_in_days = 90  # ← change from 30
}
```

**Validation:**
```bash
az monitor app-insights component show --app zdrovena-ai --resource-group zdrovena-rg \
  --query 'retentionInDays' -o tsv
# Expected: 90
```

---

### Task 5: Monitoring — Health Checks & Alerts
- **What:** Add automated health checks + email/SMS alerts for downtime
- **Why:** Proactive notification when service is down (not waiting for user complaints)
- **SLA impact:** Reduces MTTR (Mean Time To Recovery)
- **Cost:** ~€2/month (Action Groups + Alert Rules)
- **Effort:** M (~30 min)
- **Priority:** P1

**Implementation:**
```terraform
# infra/terraform/monitoring.tf (add new resources)

resource "azurerm_monitor_action_group" "ops" {
  name                = "${var.prefix}-ops-alerts"
  resource_group_name = azurerm_resource_group.rg.name
  short_name          = "ops"

  email_receiver {
    name          = "owner-email"
    email_address = var.alert_email  # ← add to variables.tf
  }

  # Optional: SMS alerts (extra cost ~€0.01/SMS)
  # sms_receiver {
  #   name         = "owner-sms"
  #   country_code = "48"  # Poland
  #   phone_number = "123456789"
  # }

  tags = local.tags
}

# Alert: API is down (no requests in last 5 minutes)
resource "azurerm_monitor_metric_alert" "api_down" {
  name                = "${var.prefix}-alert-api-down"
  resource_group_name = azurerm_resource_group.rg.name
  scopes              = [azurerm_application_insights.ai.id]
  description         = "API is down — no requests received in 5 minutes"
  severity            = 0  # Critical
  frequency           = "PT1M"   # Check every 1 minute
  window_size         = "PT5M"   # 5-minute window

  criteria {
    metric_namespace = "microsoft.insights/components"
    metric_name      = "requests/count"
    aggregation      = "Count"
    operator         = "LessThan"
    threshold        = 1  # Alert if < 1 request in 5 min
  }

  action {
    action_group_id = azurerm_monitor_action_group.ops.id
  }

  tags = local.tags
}

# Alert: High error rate (already exists, link to action group)
resource "azurerm_monitor_metric_alert" "high_error_rate" {
  # ... existing config
  
  action {
    action_group_id = azurerm_monitor_action_group.ops.id  # ← add this
  }
}
```

**New variable:**
```terraform
# infra/terraform/variables.tf
variable "alert_email" {
  description = "Email address for monitoring alerts"
  type        = string
  default     = ""  # Set in terraform.tfvars
}
```

**Validation:**
```bash
# Trigger test alert
az monitor metrics alert create --name test-alert --resource-group zdrovena-rg \
  --condition "count requests/count < 1" --window-size 5m --evaluation-frequency 1m

# Check if email received
```

---

### Task 6: Documentation Updates
- **What:** Update README with new SLA guarantees, runbook for incidents
- **Why:** Transparency for stakeholders, faster incident response
- **Effort:** S (~15 min)
- **Priority:** P2

**Update README sections:**
- Architecture diagram (add "Always-On" annotation)
- SLA section (document 99.75% uptime guarantee)
- Incident response runbook (who to contact, escalation path)

---

### Deployment Checklist

**Pre-deployment:**
- [ ] Review all Terraform changes (`terraform plan`)
- [ ] Backup current infrastructure state (`terraform show > backup-state.txt`)
- [ ] Schedule maintenance window (low-traffic period)
- [ ] Notify stakeholders (if applicable)

**Deployment order:**
1. ✅ Task 3 (SWA) — already done, verify only
2. 🔧 Task 4 (App Insights retention) — zero downtime
3. 🔧 Task 5 (Monitoring alerts) — zero downtime
4. ⚠️ Task 2 (Storage ZRS) — **2-3 min downtime**, do during maintenance window
5. 🔧 Task 1 (Container App min=1) — zero downtime (scale up)
6. 📝 Task 6 (Documentation) — after deployment

**Post-deployment validation:**
- [ ] All services healthy (`az containerapp show`, `az storage account show`)
- [ ] Test health checks (trigger test alert)
- [ ] Monitor Application Insights for errors (30 minutes)
- [ ] Verify SLA metrics in Azure Monitor

**Estimated total time:** 2 hours (including testing)  
**Estimated downtime:** 2-3 minutes (Storage Account recreate only)

---

### Cost Summary

| Component | Before | After | Increase |
|-----------|--------|-------|----------|
| Container App (min=1) | €0 | €15/mo | +€15 |
| Storage (LRS→ZRS) | €2 | €4/mo | +€2 |
| Static Web App | €0 | €8/mo | +€8 |
| App Insights (90d) | €0 | €10/mo | +€10 |
| Monitoring Alerts | €0 | €2/mo | +€2 |
| **TOTAL** | **€15/mo** | **€51/mo** | **+€36/mo** |

**Annual cost:** €180 → €612 (+€432/year)  
**ROI:** Downtime reduction 7 days → 22 hours = **97% improvement**

---

### Success Metrics

**Before (current):**
- SLA: ~98%
- Downtime: ~7 days/year
- Cold start: 5-15 seconds (scale from zero)
- MTTR: Unknown (no alerts)

**After (Target 1):**
- SLA: 99.75%
- Downtime: ~22 hours/year (0.25% of 8,760 hours)
- Cold start: 0 seconds (always running)
- MTTR: <10 minutes (automated alerts)

**Next milestone:** Target 2 (€125/mo, 99.73% SLA) — add PostgreSQL + Redis + GRS

---

## Shipping Automation

### Daily exception report
- **What:** Automated daily report of failed shipping drafts, bad addresses, orders without drafts
- **Why:** Catches shipping problems before the customer notices
- **Effort:** S (CC: ~15 min)
- **Priority:** P3
- **Depends on:** Shipping draft automation (Azure Function) deployed first
- **Context:** After the shipping Function is live, Application Insights logs are sufficient initially. Add a daily digest (email or Slack) when volume grows beyond ~50 orders/month.

---

## Shipping — Technical Debt (from /review 2026-06-24)

### Webhook idempotency
- **What:** Shopify retries create duplicate drafts (new UUID per call). Double-execute = double shipment.
- **Fix:** Check `shopify_order_id` before insert in `_create_draft`; use it as RowKey for idempotent upsert.
- **Priority:** P1
- **File:** `zdrovena/api/routers/webhooks.py:373`

### execute_draft race condition (TOCTOU)
- **What:** Two concurrent execute calls for same draft can both pass the `status != "created"` guard and create two courier shipments.
- **Fix:** Use ETag/optimistic concurrency in `update_entity` (Azure Table Storage mode="replace" with ETag).
- **Priority:** P2
- **File:** `zdrovena/api/routers/webhooks.py:465`

### ShippingStore _deserialize type coercion
- **What:** Every string from Table Storage is speculatively JSON-parsed. `"null"` → `None`, `"1234567890"` → int. Can corrupt customer names or tracking numbers.
- **Fix:** Whitelist known dict/list fields (`receiver`, `shipping_address`, `packages_breakdown`, `order_items`) instead of parsing all strings.
- **Priority:** P2
- **File:** `zdrovena/common/shipping_store.py:52`

### _table_client() creates new Azure client per operation
- **What:** Each upsert/update/get spawns a new TableServiceClient + create_table_if_not_exists HTTP call. Unnecessary overhead on every request.
- **Fix:** Cache the client at class construction time.
- **Priority:** P3
- **File:** `zdrovena/common/shipping_store.py:81`

### Missing auth enforcement tests (403 for non-privileged callers)
- **What:** execute_draft/order_pickup/update_draft have no tests for `zdrovena-viewer` role (should get 403).
- **Fix:** Add 3-4 tests with non-admin token.
- **Priority:** P3
- **File:** `tests/test_shipping_webhook.py`

### Missing SMS notification tests
- **What:** `_maybe_send_new_order_sms` has zero test coverage — no happy path, no exception-swallowed path.
- **Fix:** Add `TestSmsNotification` class with 3 test cases.
- **Priority:** P3
- **File:** `tests/test_shipping_webhook.py`

---

## Azure Key Vault — sekrety do dodania przed prod

Wszystkie sekrety poniżej muszą być dodane do Key Vault przed uruchomieniem produkcyjnym.
Nazwy używają myślników (AKV nie obsługuje podkreślników). Odpowiadają dokładnie
temu co `get_secret()` szuka po konwersji `_` → `-`.

```bash
# Szablon — podmień <vault> na nazwę swojego Key Vault
AKV="<vault>"

az keyvault secret set --vault-name $AKV --name shopify-webhook-secret  --value "<API secret key z Shopify Admin → Apps → Custom Apps>"
az keyvault secret set --vault-name $AKV --name shopify-access-token    --value "<shpat_...>"
az keyvault secret set --vault-name $AKV --name shopify-shop-domain     --value "humio-b2b-2.myshopify.com"

az keyvault secret set --vault-name $AKV --name inpost-api-token        --value "<JWT z panelu sandbox/prod InPost>"
az keyvault secret set --vault-name $AKV --name inpost-organization-id  --value "5289956"
# Uwaga: inpost-base-url NIE trafia do Key Vault — to konfiguracja, nie sekret.
# Ustaw jako env var w Container App: INPOST_BASE_URL=https://api-shipx-pl.easypack24.net

az keyvault secret set --vault-name $AKV --name apaczka-app-id          --value "<app_id z Apaczka → Ustawienia → Web API>"
az keyvault secret set --vault-name $AKV --name apaczka-app-secret      --value "<klucz HMAC z Apaczka → Ustawienia → Web API>"

az keyvault secret set --vault-name $AKV --name smsapi-token            --value "<token z SMSAPI.pl>"
az keyvault secret set --vault-name $AKV --name notify-phone            --value "48XXXXXXXXX"

az keyvault secret set --vault-name $AKV --name sender-name             --value "Humio Woda Alkaliczna"
az keyvault secret set --vault-name $AKV --name sender-street           --value "<ulica nadawcy>"
az keyvault secret set --vault-name $AKV --name sender-building-number  --value "<numer budynku>"  # TODO: bug #9.3 — currently hardcoded "1"
az keyvault secret set --vault-name $AKV --name sender-city             --value "<miasto>"
az keyvault secret set --vault-name $AKV --name sender-post-code        --value "<XX-XXX>"
az keyvault secret set --vault-name $AKV --name sender-phone            --value "48XXXXXXXXX"
az keyvault secret set --vault-name $AKV --name sender-email            --value "<email nadawcy>"
```

### Status sekretów

| Sekret AKV | Dev (.env.local) | Prod (Key Vault) | Uwagi |
|---|---|---|---|
| `shopify-webhook-secret` | zakomentowany (dev pomija HMAC) | ❌ do dodania | API secret key z Shopify app |
| `shopify-access-token` | ✅ ustawiony | ❌ do dodania | shpat_... |
| `shopify-shop-domain` | ✅ ustawiony | ❌ do dodania | |
| `inpost-api-token` | ✅ sandbox JWT | ❌ do dodania | prod JWT z panelu InPost |
| `inpost-organization-id` | ✅ 5289956 | ❌ do dodania | sprawdzić czy prod ID inne |
| `apaczka-app-id` | ❌ brak | ❌ do dodania | |
| `apaczka-app-secret` | ❌ brak | ❌ do dodania | |
| `smsapi-token` | ❌ brak | ❌ do dodania | |
| `notify-phone` | ❌ brak | ❌ do dodania | |
| `sender-name` | ❌ brak | ❌ do dodania | |
| `sender-street` | ❌ brak | ❌ do dodania | |
| `sender-building-number` | ❌ brak | ❌ do dodania | bug — hardcoded "1" w kodzie |
| `sender-city` | ❌ brak | ❌ do dodania | |
| `sender-post-code` | ❌ brak | ❌ do dodania | |
| `sender-phone` | ❌ brak | ❌ do dodania | |
| `sender-email` | ❌ brak | ❌ do dodania | |

> **Note (2026-07-09):** `apaczka-service-id` was removed from this checklist —
> it's per-draft data now (set from the Shopify shipping-line title via
> `APACZKA_SERVICE_TITLE_MAP`, or manually by an operator), never a global Key
> Vault secret. See `docs/superpowers/specs/2026-07-09-apaczka-per-draft-service.md`.
