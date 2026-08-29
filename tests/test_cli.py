"""Tests for zdrovena.cli argument parsing."""

from __future__ import annotations

import subprocess
import sys

from zdrovena import __version__


class TestCLIVersion:
    def test_version_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "--version"],
            capture_output=True,
            text=True,
        )
        # Asserting the literal is what let the CLI drift to 2.0.0 while the
        # package was on 2.9.0 -- compare against the single source (#238).
        assert __version__ in result.stdout

    def test_short_version_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "-V"],
            capture_output=True,
            text=True,
        )
        # Asserting the literal is what let the CLI drift to 2.0.0 while the
        # package was on 2.9.0 -- compare against the single source (#238).
        assert __version__ in result.stdout


class TestCLIHelp:
    def test_help_shows_commands(self):
        result = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "audit" in result.stdout
        assert "close" in result.stdout
        assert "setup" in result.stdout
        assert "report" in result.stdout

    def test_audit_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "audit", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_report_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "report", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "vat-sales" in result.stdout

    def test_setup_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "setup", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "canva-login" in result.stdout


class TestCLINoCommand:
    def test_no_command_exits_with_error(self):
        result = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


class TestCLIDayRequiresMonth:
    def test_day_without_month_fails(self):
        result = subprocess.run(
            [sys.executable, "-m", "zdrovena.cli", "audit", "-y", "2025", "-d", "15"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "month" in result.stderr.lower() or "wymaga" in result.stderr.lower()
