"""One source of truth for the application version.

`pyproject.toml` is that source. Everything else reads the installed
distribution metadata rather than repeating the number, because a repeated
number drifts: /health reported 2.0.0 while the deployed app was 2.9.0 and
version.json said 2.9.0 (issue #216).
"""

from __future__ import annotations

import re
from importlib.metadata import version as dist_version
from pathlib import Path

import tomllib

import zdrovena
from zdrovena.api.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


class TestSingleVersionSource:
    def test_the_package_version_matches_pyproject(self) -> None:
        assert zdrovena.__version__ == _pyproject_version()

    def test_the_package_version_matches_the_installed_distribution(self) -> None:
        assert zdrovena.__version__ == dist_version("zdrovena-reconciliation")

    def test_the_api_reports_the_same_version(self) -> None:
        # /health returns app.version; it used to be a separate literal and
        # answered 2.0.0 for a 2.9.0 deployment.
        assert app.version == zdrovena.__version__

    def test_no_module_hardcodes_a_version_literal(self) -> None:
        """The regression guard: a second literal is how the drift started."""
        # The "not installed" sentinel is deliberately implausible, so it can
        # never be mistaken for a real version the way a stale 2.0.0 was.
        sentinel = "0.0.0+unknown"

        # Scanning the whole package, not just the files already known to be
        # wrong: the first version of this guard looked only at __init__.py and
        # main.py and therefore could not have found the third literal, which
        # sat in cli.py making `zdrovena --version` print 2.0.0 (#238).
        offenders = []
        # fake_providers is an emulator of somebody else's API; the version
        # it declares is the emulated provider's, not ours.
        for path in sorted((REPO_ROOT / "zdrovena").rglob("*.py")):
            if "fake_providers" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if sentinel in line:
                    continue
                if re.search(r'version[^=\n]*=\s*["\'][^"\']*\d+\.\d+\.\d+', line):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel}:{number}: {line.strip()}")

        assert offenders == [], (
            "version must come from importlib.metadata, not a literal (#216):\n"
            + "\n".join(offenders)
        )
