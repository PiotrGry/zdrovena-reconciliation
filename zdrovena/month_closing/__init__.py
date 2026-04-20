"""
zdrovena.month_closing – Monthly accounting close pipeline.

Usage::

    from zdrovena.month_closing.orchestrator import MonthCloseOrchestrator

    orch = MonthCloseOrchestrator(year=2025, month=6, dry_run=True)
    report = orch.execute()
"""

from zdrovena.month_closing.orchestrator import CloseReport, MonthCloseOrchestrator

__all__ = ["CloseReport", "MonthCloseOrchestrator"]
