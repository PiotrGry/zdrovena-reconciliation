"""Zdrovena Reconciliation – invoice audit, bottle tracking & month-close."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    # pyproject.toml is the one source of truth; this reads what was installed
    # from it. A second literal is how /health came to report 2.0.0 for a 2.9.0
    # deployment while version.json said 2.9.0 (issue #216).
    __version__ = _dist_version("zdrovena-reconciliation")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    # Deliberately not a plausible number: an obviously fake version is safer
    # than a stale real one, which is exactly what this issue was about.
    __version__ = "0.0.0+unknown"
