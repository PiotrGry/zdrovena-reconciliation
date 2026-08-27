"""Pure title strings for the printable label documents the operator saves."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


def _day_stamp(now: datetime | None) -> str:
    """Return the Polish calendar day as dd.mm.

    The container clock is UTC. A sheet printed at 23:30 CEST would carry
    tomorrow's date without this conversion, which is exactly the kind of
    quiet wrongness a filename is supposed to prevent.
    """
    moment = now or datetime.now(WARSAW)
    return moment.astimezone(WARSAW).strftime("%d.%m")


def batch_label_title(now: datetime | None = None) -> str:
    """Title for the merged sheet printed for a whole batch of drafts."""
    return f"Etykiety portal {_day_stamp(now)}"


def single_label_title(order_number: str, now: datetime | None = None) -> str:
    """Title for one draft's label.

    The order number stays: with a single label on the page it is the
    identifying information, and the operator's naming request was about the
    batch sheet.
    """
    order = str(order_number or "").lstrip("#").strip() or "bez numeru"
    return f"Etykieta {order} {_day_stamp(now)}"


__all__ = ["WARSAW", "batch_label_title", "single_label_title"]
