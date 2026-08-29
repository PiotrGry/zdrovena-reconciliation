"""Alert rules must reference events the code actually emits (issue #214).

A scheduled query alert that filters on an event name is silently dead the
moment that name changes: the query still runs, still returns zero rows, and
still reports healthy. That is the same failure shape as #279, #278 and #310 --
a guarantee that looks real and is not.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
MONITORING_TF = ROOT / "infra" / "terraform" / "monitoring.tf"


def _event_names_emitted_by_the_code() -> set[str]:
    names: set[str] = set()
    pattern = re.compile(r"log_event\(\s*\n?\s*[\"']([a-zA-Z0-9_.]+)[\"']")
    for path in (ROOT / "zdrovena").rglob("*.py"):
        names.update(pattern.findall(path.read_text(encoding="utf-8")))
    return names


def _event_names_referenced_by_alerts() -> set[str]:
    text = MONITORING_TF.read_text(encoding="utf-8")
    return set(re.findall(r'tostring\(payload\.event\)\s*==\s*"([^"]+)"', text))


def test_every_alert_filters_on_an_event_the_code_emits():
    referenced = _event_names_referenced_by_alerts()
    assert referenced, "no event-based alert rules found — did the query shape change?"

    unknown = sorted(referenced - _event_names_emitted_by_the_code())

    assert unknown == [], (
        f"monitoring.tf filters on events nothing emits: {unknown}. "
        "The alert will never fire and will report healthy forever."
    )


def test_the_signals_named_in_the_issue_are_alerted_on():
    """Pins the coverage #214 asked for, so a rule cannot quietly disappear."""
    referenced = _event_names_referenced_by_alerts()

    for event in (
        "storage_unavailable",
        "sync.completed",
        "shipping.stuck_execution_snapshot",
        "kaucja_source_divergence",
    ):
        assert event in referenced, f"no alert rule covers {event}"


def test_every_alert_rule_is_wired_to_the_action_group():
    """An alert with no receiver fires into the void -- the [LOG] H1 finding."""
    text = MONITORING_TF.read_text(encoding="utf-8")
    rules = re.findall(
        r'resource "azurerm_monitor_(?:scheduled_query_rules_alert_v2|metric_alert)" "([^"]+)"',
        text,
    )
    action_blocks = text.count("azurerm_monitor_action_group.ops.id")

    assert len(rules) == action_blocks, (
        f"{len(rules)} alert rules but {action_blocks} references to the action group"
    )


def test_activity_log_is_exported_to_the_workspace():
    """An empty `AzureActivity` cannot tell "nothing happened" from "not collected".

    Alert history is the audit trail for every rule in this file; without the
    diagnostic setting the rules exist but their activations cannot be traced
    (issue #217).
    """
    text = MONITORING_TF.read_text(encoding="utf-8")

    assert 'resource "azurerm_monitor_diagnostic_setting" "activity_log"' in text
    assert "log_analytics_workspace_id" in text

    exported = set(re.findall(r'enabled_log\s*\{\s*category\s*=\s*"([^"]+)"', text))
    assert "Alert" in exported, "alert history is the whole point of the export"
    assert "Administrative" in exported, (
        "without it, a rule silenced by hand looks like a rule that stopped firing"
    )

    # Paid per ingested record: an unjustified category is a standing cost.
    assert exported <= {"Alert", "Administrative", "ServiceHealth", "ResourceHealth"}, (
        f"undocumented Activity Log categories exported: "
        f"{sorted(exported - {'Alert', 'Administrative', 'ServiceHealth', 'ResourceHealth'})}"
    )


def test_retention_is_decided_in_one_place():
    """Overriding retention on the diagnostic setting would create a second,
    silently diverging source of truth next to the workspace's own setting."""
    text = MONITORING_TF.read_text(encoding="utf-8")
    activity = text.split('resource "azurerm_monitor_diagnostic_setting" "activity_log"')[-1]

    assert "retention_policy" not in activity
    assert "retention_in_days" not in activity
