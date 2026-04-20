"""Tests for zdrovena.month_closing.commands.close_cmd."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.month_closing.commands.close_cmd import (
    _configure_logging,
    _parse_month,
    add_subparser,
)

# ── _parse_month ──────────────────────────────────────────────────────────────


class TestParseMonth:
    def test_valid(self):
        assert _parse_month("2025-06") == (2025, 6)

    def test_valid_december(self):
        assert _parse_month("2025-12") == (2025, 12)

    def test_valid_january(self):
        assert _parse_month("2025-01") == (2025, 1)

    def test_invalid_format(self):
        with pytest.raises(argparse.ArgumentTypeError, match="Invalid format"):
            _parse_month("06-2025")

    def test_invalid_month_0(self):
        with pytest.raises(argparse.ArgumentTypeError, match="Month must be 01–12"):
            _parse_month("2025-00")

    def test_invalid_month_13(self):
        with pytest.raises(argparse.ArgumentTypeError, match="Month must be 01–12"):
            _parse_month("2025-13")

    def test_year_too_early(self):
        with pytest.raises(argparse.ArgumentTypeError, match="Year out of range"):
            _parse_month("2019-06")

    def test_year_too_late(self):
        with pytest.raises(argparse.ArgumentTypeError, match="Year out of range"):
            _parse_month("2100-06")

    def test_missing_leading_zero_fails(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_month("2025-6")


# ── add_subparser ─────────────────────────────────────────────────────────────


class TestAddSubparser:
    def test_subparser_registered(self):
        main_parser = argparse.ArgumentParser()
        subparsers = main_parser.add_subparsers()
        add_subparser(subparsers)
        # Should be able to parse basic 'close' command
        args = main_parser.parse_args(["close", "2025-06"])
        assert args.period == "2025-06"

    def test_dry_run_default_false(self):
        main_parser = argparse.ArgumentParser()
        subparsers = main_parser.add_subparsers()
        add_subparser(subparsers)
        args = main_parser.parse_args(["close", "2025-06"])
        assert args.dry_run is False

    def test_dry_run_flag(self):
        main_parser = argparse.ArgumentParser()
        subparsers = main_parser.add_subparsers()
        add_subparser(subparsers)
        args = main_parser.parse_args(["close", "2025-06", "--dry-run"])
        assert args.dry_run is True

    def test_ignore_vendor_multiple(self):
        main_parser = argparse.ArgumentParser()
        subparsers = main_parser.add_subparsers()
        add_subparser(subparsers)
        args = main_parser.parse_args(
            ["close", "2025-06", "--ignore-vendor", "PayU", "--ignore-vendor", "Canva"]
        )
        assert "PayU" in args.ignore_vendors
        assert "Canva" in args.ignore_vendors

    def test_zip_and_send_flags(self):
        main_parser = argparse.ArgumentParser()
        subparsers = main_parser.add_subparsers()
        add_subparser(subparsers)
        args = main_parser.parse_args(["close", "2025-06", "--zip", "--send"])
        assert args.zip is True
        assert args.send is True


# ── _configure_logging ────────────────────────────────────────────────────────


class TestConfigureLogging:
    def test_runs_without_error(self, tmp_path, monkeypatch):
        """_configure_logging should not raise."""
        import logging

        monkeypatch.chdir(tmp_path)
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        _configure_logging(verbose=False)
        # Clean up added handlers to avoid log file pollution between tests
        for h in root.handlers[:]:
            if h not in original_handlers:
                h.close()
                root.removeHandler(h)

    def test_verbose_sets_debug(self, tmp_path, monkeypatch):
        import logging

        monkeypatch.chdir(tmp_path)
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        _configure_logging(verbose=True)
        assert root.level == logging.DEBUG
        for h in root.handlers[:]:
            if h not in original_handlers:
                h.close()
                root.removeHandler(h)


# ── _run_local integration ────────────────────────────────────────────────────


class TestRunLocal:
    @patch("zdrovena.month_closing.orchestrator.MonthCloseOrchestrator")
    def test_full_pipeline_called(self, mock_orch_cls, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_orch = MagicMock()
        mock_orch.execute.return_value = MagicMock(errors=[])
        mock_orch_cls.return_value = mock_orch

        args = SimpleNamespace(
            period="2025-06",
            period_flag=None,
            dry_run=True,
            zip=False,
            send=False,
            reset=False,
            verbose=False,
            non_interactive=False,
            ignore_warnings=False,
            ignore_vendors=[],
        )
        from zdrovena.month_closing.commands.close_cmd import _run_local

        with pytest.raises(SystemExit) as exc:
            _run_local(args)
        assert exc.value.code == 0
        mock_orch.execute.assert_called_once()

    @patch("zdrovena.month_closing.orchestrator.MonthCloseOrchestrator")
    def test_zip_only_mode(self, mock_orch_cls, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_orch = MagicMock()
        mock_orch.execute_zip_only.return_value = MagicMock(errors=[])
        mock_orch_cls.return_value = mock_orch

        args = SimpleNamespace(
            period="2025-06",
            period_flag=None,
            dry_run=False,
            zip=True,
            send=False,
            reset=False,
            verbose=False,
            non_interactive=False,
            ignore_warnings=False,
            ignore_vendors=[],
        )
        from zdrovena.month_closing.commands.close_cmd import _run_local

        with pytest.raises(SystemExit) as exc:
            _run_local(args)
        assert exc.value.code == 0
        mock_orch.execute_zip_only.assert_called_once()

    @patch("zdrovena.month_closing.orchestrator.MonthCloseOrchestrator")
    def test_errors_exit_1(self, mock_orch_cls, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_orch = MagicMock()
        mock_orch.execute.return_value = MagicMock(errors=["Something failed"])
        mock_orch_cls.return_value = mock_orch

        args = SimpleNamespace(
            period="2025-06",
            period_flag=None,
            dry_run=True,
            zip=False,
            send=False,
            reset=False,
            verbose=False,
            non_interactive=False,
            ignore_warnings=False,
            ignore_vendors=[],
        )
        from zdrovena.month_closing.commands.close_cmd import _run_local

        with pytest.raises(SystemExit) as exc:
            _run_local(args)
        assert exc.value.code == 1

    @patch("zdrovena.month_closing.orchestrator.MonthCloseOrchestrator")
    def test_reset_calls_state_reset(self, mock_orch_cls, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_orch = MagicMock()
        mock_orch.execute.return_value = MagicMock(errors=[])
        mock_orch_cls.return_value = mock_orch

        args = SimpleNamespace(
            period="2025-06",
            period_flag=None,
            dry_run=True,
            zip=False,
            send=False,
            reset=True,
            verbose=False,
            non_interactive=False,
            ignore_warnings=False,
            ignore_vendors=[],
        )
        from zdrovena.month_closing.commands.close_cmd import _run_local

        with pytest.raises(SystemExit):
            _run_local(args)
        mock_orch.state.reset.assert_called_once()


# ── _run (period conflict check) ─────────────────────────────────────────────


class TestRun:
    def test_conflicting_periods_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        args = SimpleNamespace(
            period="2025-06",
            period_flag="2025-07",  # different!
        )
        from zdrovena.month_closing.commands.close_cmd import _run

        with pytest.raises(SystemExit) as exc:
            _run(args)
        assert exc.value.code == 1

    def test_missing_period_exits_1(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = SimpleNamespace(period=None, period_flag=None)
        from zdrovena.month_closing.commands.close_cmd import _run

        with pytest.raises(SystemExit) as exc:
            _run(args)
        assert exc.value.code == 1

    @patch("zdrovena.month_closing.commands.close_cmd._run_local")
    def test_delegates_to_local(self, mock_local, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ZDROVENA_API_URL", raising=False)
        args = SimpleNamespace(
            period="2025-06",
            period_flag=None,
        )
        from zdrovena.month_closing.commands.close_cmd import _run

        _run(args)
        mock_local.assert_called_once_with(args)
