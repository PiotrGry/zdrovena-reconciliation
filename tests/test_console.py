"""Tests for zdrovena.month_closing.console.ConsoleReporter."""

from __future__ import annotations

from io import StringIO

import pytest

from zdrovena.month_closing.console import ConsoleReporter


@pytest.fixture()
def reporter() -> tuple[ConsoleReporter, StringIO]:
    buf = StringIO()
    return ConsoleReporter(stream=buf), buf


class TestConsoleReporter:
    def test_banner(self, reporter):
        out, buf = reporter
        out.banner("Month Close 2025-06")
        output = buf.getvalue()
        assert "Month Close 2025-06" in output
        assert "═" in output

    def test_step(self, reporter):
        out, buf = reporter
        out.step(1, "Create folders")
        output = buf.getvalue()
        assert "[1/7]" in output
        assert "Create folders" in output

    def test_ok(self, reporter):
        out, buf = reporter
        out.ok("All good")
        assert "✅" in buf.getvalue()
        assert "All good" in buf.getvalue()

    def test_warn(self, reporter):
        out, buf = reporter
        out.warn("Minor issue")
        assert "⚠️" in buf.getvalue()

    def test_error(self, reporter):
        out, buf = reporter
        out.error("Fatal")
        assert "❌" in buf.getvalue()

    def test_skip(self, reporter):
        out, buf = reporter
        out.skip("Already done")
        assert "⏭" in buf.getvalue()

    def test_plain(self, reporter):
        out, buf = reporter
        out.plain("raw line")
        assert buf.getvalue() == "raw line\n"

    def test_plain_empty(self, reporter):
        out, buf = reporter
        out.plain()
        assert buf.getvalue() == "\n"

    def test_blocker_box(self, reporter):
        out, buf = reporter
        out.blocker_box(["  Missing: JPK_FA", "  Missing: bank stmt"])
        output = buf.getvalue()
        assert "MISSING DOCUMENTS" in output
        assert "JPK_FA" in output
        assert "bank stmt" in output

    def test_summary(self, reporter):
        out, buf = reporter
        out.summary_header("Summary Report")
        out.summary_line("Invoices:", "42")
        out.summary_line("Total:", "12,345.67 PLN")
        out.summary_footer(success=True)

        output = buf.getvalue()
        assert "Summary Report" in output
        assert "42" in output
        assert "completed successfully" in output

    def test_summary_failure(self, reporter):
        out, buf = reporter
        out.summary_footer(success=False)
        assert "ERRORS" in buf.getvalue()

    def test_item(self, reporter):
        out, buf = reporter
        out.item("item message")
        assert "item message" in buf.getvalue()

    def test_info(self, reporter):
        out, buf = reporter
        out.info("informational message")
        assert "informational message" in buf.getvalue()

    def test_detail(self, reporter):
        out, buf = reporter
        out.detail("detail line")
        assert "detail line" in buf.getvalue()

    def test_section_methods(self, reporter):
        out, buf = reporter
        out.section_start("Start section")
        out.section_mid("Mid section")
        out.section_end("End section")
        output = buf.getvalue()
        assert "Start section" in output
        assert "Mid section" in output
        assert "End section" in output
