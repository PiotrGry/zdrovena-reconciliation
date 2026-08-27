"""Tests for zdrovena.shipping.domain.labels.

Chrome takes the "Save as PDF" filename from the printed document's /Title.
These titles are what the operator ends up seeing in the save dialog, so the
date has to be the Polish one, not the server's UTC one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from zdrovena.shipping.domain.labels import batch_label_title, single_label_title


class TestBatchLabelTitle:
    def test_uses_the_operator_requested_wording_and_day(self):
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))
        assert batch_label_title(moment) == "Etykiety portal 26.08"

    def test_reads_the_warsaw_day_not_the_utc_one(self):
        # 23:30 UTC on the 25th is 01:30 on the 26th in Warsaw. The container
        # runs on UTC, so without the conversion a late batch is misdated.
        moment = datetime(2026, 8, 25, 23, 30, tzinfo=timezone.utc)
        assert batch_label_title(moment) == "Etykiety portal 26.08"


class TestSingleLabelTitle:
    def test_keeps_the_order_number(self):
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))
        assert single_label_title("1723", moment) == "Etykieta 1723 26.08"

    def test_strips_the_shopify_hash(self):
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))
        assert single_label_title("#1723", moment) == "Etykieta 1723 26.08"

    def test_falls_back_when_there_is_no_order_number(self):
        moment = datetime(2026, 8, 26, 9, 58, tzinfo=ZoneInfo("Europe/Warsaw"))
        assert single_label_title("", moment) == "Etykieta bez numeru 26.08"
