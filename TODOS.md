# TODOS

## Shipping Automation

### Daily exception report
- **What:** Automated daily report of failed shipping drafts, bad addresses, orders without drafts
- **Why:** Catches shipping problems before the customer notices
- **Effort:** S (CC: ~15 min)
- **Priority:** P3
- **Depends on:** Shipping draft automation (Azure Function) deployed first
- **Context:** After the shipping Function is live, Application Insights logs are sufficient initially. Add a daily digest (email or Slack) when volume grows beyond ~50 orders/month.
