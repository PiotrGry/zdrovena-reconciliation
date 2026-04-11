"""CLI smoke tests — run actual commands, verify they don't crash.

These catch import errors, type mismatches, and configuration problems
that unit tests with mocks will never find.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

PYTHON = sys.executable


class TestCLISmoke:
    """Run CLI commands as subprocesses — the closest thing to real usage."""

    def test_help(self):
        r = subprocess.run([PYTHON, "-m", "zdrovena.cli", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "close" in r.stdout

    def test_preflight_help(self):
        r = subprocess.run([PYTHON, "-m", "zdrovena.cli", "preflight", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "inbox" in r.stdout

    def test_close_help(self):
        r = subprocess.run([PYTHON, "-m", "zdrovena.cli", "close", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "--period" in r.stdout

    def test_preflight_missing_period_exits_1(self):
        r = subprocess.run([PYTHON, "-m", "zdrovena.cli", "preflight"], capture_output=True, text=True)
        assert r.returncode == 1

    def test_preflight_bad_format_exits_1(self):
        r = subprocess.run([PYTHON, "-m", "zdrovena.cli", "preflight", "not-a-date"], capture_output=True, text=True)
        assert r.returncode == 1

    def test_close_no_period_exits_1(self):
        r = subprocess.run([PYTHON, "-m", "zdrovena.cli", "close"], capture_output=True, text=True)
        assert r.returncode == 1
