# R4-C: verify Azure monitoring delivery and make DLQ alert operational

## Goal
Turn declared monitoring into verified operational monitoring.

## Scope
- `infra/terraform/monitoring.tf`
- `infra/terraform/variables.tf`
- Terraform documentation/template
- KQL query used by DLQ alert
- runbook/checklist only; no application business logic

## Required changes
- Confirm actual Log Analytics table names used by Container Apps.
- Adjust KQL to match real structured event fields.
- Ensure action group is attached to error-rate, latency and DLQ alerts.
- Document `terraform plan/apply` procedure.
- Add a controlled test-alert procedure and evidence checklist.

## Acceptance criteria
- [ ] Terraform validate passes.
- [ ] Plan contains expected alert/action group changes.
- [ ] Test DLQ event triggers the alert.
- [ ] E-mail reaches the configured recipient.
- [ ] Runbook records query, time window and correlation ID.

## Copilot completion report
- Changed files:
- Commands executed:
- Test results:
- Remaining risks:
- Proposed commit message:
