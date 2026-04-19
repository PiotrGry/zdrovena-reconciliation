"""Integration tests for preflight — NO mocks at the boundary.

These tests verify that preflight_cmd and PreflightChecker actually work
together with real types. Unit tests with mocks can't catch type mismatches
at module boundaries (e.g., passing date objects where strings are expected).
"""

from __future__ import annotations

from unittest.mock import patch


class TestPreflightIntegration:
    """Run preflight_cmd → PreflightChecker without mocking the checker."""

    @patch("zdrovena.month_closing.commands.preflight_cmd._get_secret", return_value=None)
    def test_preflight_does_not_crash_on_real_checker(self, mock_secret, tmp_path):
        """The actual PreflightChecker is instantiated — no mocks.

        This is the test that would have caught the date vs string bug.
        If _run() passes wrong types to PreflightChecker, it blows up here.
        """
        import argparse

        from zdrovena.month_closing.commands.preflight_cmd import _run

        # Point inbox to an empty tmp dir so no real files are needed
        with patch("zdrovena.month_closing.config.DOWNLOAD_WATCH_DIR", tmp_path):
            args = argparse.Namespace(
                period="2025-06",
                period_flag=None,
                verbose=False,
                no_browser=True,
            )
            # Behavior depends on local files (may pass or fail), but must not crash.
            try:
                _run(args)
            except SystemExit as exc:
                assert exc.code in (0, 1)

    @patch("zdrovena.month_closing.commands.preflight_cmd._get_secret", return_value=None)
    def test_december_boundary_real_checker(self, mock_secret, tmp_path):
        """December → January year rollover with real PreflightChecker."""
        import argparse

        from zdrovena.month_closing.commands.preflight_cmd import _run

        with patch("zdrovena.month_closing.config.DOWNLOAD_WATCH_DIR", tmp_path):
            args = argparse.Namespace(
                period="2025-12",
                period_flag=None,
                verbose=False,
                no_browser=True,
            )
            try:
                _run(args)
            except SystemExit as exc:
                assert exc.code in (0, 1)
