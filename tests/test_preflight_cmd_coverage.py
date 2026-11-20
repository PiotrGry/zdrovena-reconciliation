"""Coverage tests for zdrovena.month_closing.commands.preflight_cmd (issue #549).

Covers: ``_parse_month`` validation, ``add_subparser`` wiring, ``_run`` error
paths (invalid period, KeyboardInterrupt, generic failure, missing bank
statement, verbose logging) and every resolution branch of ``_get_secret``.
"""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.month_closing.commands import preflight_cmd
from zdrovena.month_closing.commands.preflight_cmd import (
    _get_secret,
    _parse_month,
    _run,
    add_subparser,
)


def _mock_result(missing=None, missing_reports=None, bank_found=True, warnings=None):
    result = MagicMock()
    result.missing_vendors = missing or []
    result.missing_reports = missing_reports or []
    result.bank_statement_found = bank_found
    result.warnings = warnings or []
    return result


def _make_args(period=None, period_flag=None, verbose=False, no_browser=False):
    return argparse.Namespace(
        period=period, period_flag=period_flag, verbose=verbose, no_browser=no_browser
    )


# ---------------------------------------------------------------------------
# _parse_month
# ---------------------------------------------------------------------------


class TestParseMonth:
    def test_valid(self):
        assert _parse_month("2025-06") == (2025, 6)

    @pytest.mark.parametrize("value", ["2025-6", "202506", "abcd-ef", "2025-06-01", ""])
    def test_bad_format(self, value):
        with pytest.raises(argparse.ArgumentTypeError, match="Invalid format"):
            _parse_month(value)

    @pytest.mark.parametrize("value", ["2025-00", "2025-13"])
    def test_month_out_of_range(self, value):
        with pytest.raises(argparse.ArgumentTypeError, match="Month must be 01"):
            _parse_month(value)

    @pytest.mark.parametrize("value", ["2019-12", "2100-01"])
    def test_year_out_of_range(self, value):
        with pytest.raises(argparse.ArgumentTypeError, match="Year out of range"):
            _parse_month(value)


# ---------------------------------------------------------------------------
# add_subparser
# ---------------------------------------------------------------------------


class TestAddSubparser:
    def _parser(self):
        parser = argparse.ArgumentParser(prog="zdrovena")
        add_subparser(parser.add_subparsers(dest="command"))
        return parser

    def test_positional_period_and_defaults(self):
        args = self._parser().parse_args(["preflight", "2025-06"])
        assert args.command == "preflight"
        assert args.period == "2025-06"
        assert args.period_flag is None
        assert args.verbose is False
        assert args.no_browser is False
        assert args.func is _run

    def test_flags(self):
        args = self._parser().parse_args(["preflight", "--period", "2025-07", "-v", "--no-browser"])
        assert args.period is None
        assert args.period_flag == "2025-07"
        assert args.verbose is True
        assert args.no_browser is True


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------


class TestRunErrorPaths:
    def test_invalid_period_exits_with_message(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            _run(_make_args(period="2025-13"))
        assert exc_info.value.code == 1
        assert "Month must be 01" in capsys.readouterr().err

    def test_same_positional_and_flag_is_not_a_conflict(self):
        with (
            patch("zdrovena.month_closing.preflight.PreflightChecker") as cls,
            patch.object(preflight_cmd, "get_storage_service"),
        ):
            cls.return_value.run.return_value = _mock_result()
            _run(_make_args(period="2025-03", period_flag="2025-03"))
        assert cls.call_args.kwargs["month"] == 3

    @patch.object(preflight_cmd, "get_storage_service")
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_keyboard_interrupt_exits_130(self, cls, _storage, capsys):
        cls.return_value.run.side_effect = KeyboardInterrupt
        with pytest.raises(SystemExit) as exc_info:
            _run(_make_args(period="2025-03"))
        assert exc_info.value.code == 130
        assert "Przerwano" in capsys.readouterr().out

    @patch.object(preflight_cmd, "get_storage_service")
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_generic_failure_exits_1(self, cls, _storage, capsys):
        cls.side_effect = RuntimeError("zoho down")
        with pytest.raises(SystemExit) as exc_info:
            _run(_make_args(period="2025-03"))
        assert exc_info.value.code == 1
        assert "Pre-flight failed: zoho down" in capsys.readouterr().err

    @patch.object(preflight_cmd, "get_storage_service")
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_missing_everything_summary(self, cls, _storage, capsys):
        vendor = MagicMock()
        vendor.name = "Canva"
        cls.return_value.run.return_value = _mock_result(
            missing=[vendor], missing_reports=[{"name": "JPK_FA"}], bank_found=False
        )
        with pytest.raises(SystemExit) as exc_info:
            _run(_make_args(period="2025-03"))
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "missing invoices: Canva" in out
        assert "missing reports: JPK_FA" in out
        assert "bank statement not found" in out
        assert "zdrovena close 2025-03" in out

    @patch.object(preflight_cmd, "get_storage_service")
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_only_bank_statement_missing(self, cls, _storage, capsys):
        cls.return_value.run.return_value = _mock_result(bank_found=False)
        with pytest.raises(SystemExit) as exc_info:
            _run(_make_args(period="2025-03"))
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "⚠️  bank statement not found" in out
        assert "missing invoices" not in out
        assert "missing reports" not in out

    @patch.object(preflight_cmd, "get_storage_service")
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_verbose_configures_debug_logging(self, cls, _storage):
        cls.return_value.run.return_value = _mock_result()
        with patch("logging.basicConfig") as basic:
            _run(_make_args(period="2025-03", verbose=True))
        assert basic.call_args.kwargs["level"] == 10  # logging.DEBUG

    @patch.object(preflight_cmd, "get_storage_service")
    @patch("zdrovena.month_closing.preflight.PreflightChecker")
    def test_checker_receives_dry_run_and_secret_resolver(self, cls, storage):
        cls.return_value.run.return_value = _mock_result()
        _run(_make_args(period_flag="2024-02"))
        kwargs = cls.call_args.kwargs
        assert kwargs["year"] == 2024 and kwargs["month"] == 2
        assert kwargs["date_to"] == "2024-02-29"  # leap year
        assert kwargs["dry_run"] is True
        assert kwargs["get_secret"] is _get_secret
        assert kwargs["storage"] is storage.return_value


# ---------------------------------------------------------------------------
# _get_secret
# ---------------------------------------------------------------------------

_ENV_KEY = "PIRX_TEST_SVC_TOKEN"
_SERVICE = "pirx-test.svc-token"


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv(_ENV_KEY, raising=False)


def _fake_env_file(content=None, error=None):
    """Patch Path.is_file/read_text so only the module's ``.env`` path is faked."""
    real_is_file = Path.is_file
    real_read_text = Path.read_text

    def is_file(self):
        if self.name == ".env":
            return content is not None or error is not None
        return real_is_file(self)

    def read_text(self, *a, **kw):
        if self.name == ".env":
            if error is not None:
                raise error
            return content
        return real_read_text(self, *a, **kw)

    return patch.multiple(Path, is_file=is_file, read_text=read_text)


def _keyring(value=None, error=None):
    mod = types.ModuleType("keyring")

    def get_password(service, user):
        assert user == "humio"
        if error is not None:
            raise error
        return value

    mod.get_password = get_password
    return patch.dict(sys.modules, {"keyring": mod})


class TestGetSecret:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv(_ENV_KEY, "from-env")
        assert _get_secret(_SERVICE) == "from-env"

    def test_dotenv_file(self, no_env):
        content = "\n".join(
            [
                "# comment",
                "",
                "NO_EQUALS_LINE",
                "OTHER=x",
                f"  {_ENV_KEY} = from-dotenv  ",
            ]
        )
        with _fake_env_file(content), _keyring(error=AssertionError("not reached")):
            assert _get_secret(_SERVICE) == "from-dotenv"

    def test_dotenv_without_key_falls_to_keyring(self, no_env):
        with _fake_env_file("OTHER=x\n"), _keyring("from-keyring"):
            assert _get_secret(_SERVICE) == "from-keyring"

    def test_dotenv_read_error_is_swallowed(self, no_env):
        with _fake_env_file(error=OSError("perm")), _keyring("from-keyring"):
            assert _get_secret(_SERVICE) == "from-keyring"

    def test_no_dotenv_uses_keyring(self, no_env):
        with _fake_env_file(None), _keyring("from-keyring"):
            assert _get_secret(_SERVICE) == "from-keyring"

    def test_keyring_error_and_required_raises(self, no_env):
        with _fake_env_file(None), _keyring(error=RuntimeError("no backend")):
            with pytest.raises(RuntimeError, match=f"Missing secret: {_SERVICE}.*{_ENV_KEY}"):
                _get_secret(_SERVICE)

    def test_keyring_empty_and_not_required_returns_none(self, no_env):
        with _fake_env_file(None), _keyring(None):
            assert _get_secret(_SERVICE, required=False) is None

    def test_keyring_import_failure_is_swallowed(self, no_env):
        with _fake_env_file(None), patch.dict(sys.modules, {"keyring": None}):
            assert _get_secret(_SERVICE, required=False) is None
