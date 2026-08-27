"""Policy tests for scripts/check.sh — the local pre-push quality gate.

A step that cannot run must fail the gate. Printing a skip and continuing to
"All checks passed - safe to push" is how a broken environment silently
downgrades every guarantee the gate is supposed to give (issue #279).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"
SOURCE = CHECK_SH.read_text(encoding="utf-8")


def _run_helper(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a snippet against the real helper definitions from check.sh.

    Sources only the header (colour vars + helper functions) so the assertions
    are about actual behaviour, not about the text of the file.
    """
    header = SOURCE.split("# Aktywuj .venv", 1)[0]
    return subprocess.run(
        ["bash", "-c", header + "\n" + script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        check=False,
    )


class TestMissingTool:
    def test_a_missing_tool_fails_the_gate(self) -> None:
        result = _run_helper('missing_tool "trivy" CHECK_TRIVY "zainstaluj trivy"')

        assert result.returncode != 0
        assert "trivy" in result.stdout

    def test_the_hint_names_the_opt_out_so_nobody_reaches_for_no_verify(self) -> None:
        result = _run_helper('missing_tool "trivy" CHECK_TRIVY "zainstaluj trivy"')

        assert "CHECK_TRIVY" in result.stdout
        assert "zainstaluj trivy" in result.stdout

    def test_an_explicit_opt_out_is_honoured(self) -> None:
        result = _run_helper(
            'missing_tool "trivy" CHECK_TRIVY "zainstaluj trivy"; echo "CONTINUED"',
            env={"CHECK_TRIVY": "0"},
        )

        assert result.returncode == 0
        assert "CONTINUED" in result.stdout

    def test_an_opt_out_set_to_anything_else_still_fails(self) -> None:
        # Only "0" opts out. A stray "false" or "no" must not disable a check.
        for value in ("1", "false", "no", ""):
            result = _run_helper(
                'missing_tool "trivy" CHECK_TRIVY "zainstaluj trivy"',
                env={"CHECK_TRIVY": value},
            )
            assert result.returncode != 0, f"CHECK_TRIVY={value!r} should not opt out"


class TestNoBareSkips:
    def test_every_skip_is_either_an_opt_out_or_goes_through_missing_tool(self) -> None:
        """A bare `echo ${SKIP} ... not found` is the bug this issue is about.

        Only two skips may be printed directly: the deliberate CHECK_TESTS and
        CHECK_TYPECHECK opt-outs. Everything else must route through
        missing_tool, which fails unless the developer opted out on purpose.
        """
        allowed = ("CHECK_TYPECHECK=0", "CHECK_TESTS=0", "($var=0)")

        offenders = []
        for number, line in enumerate(SOURCE.splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("echo -e") or "${SKIP}" not in stripped:
                continue
            if any(token in stripped for token in allowed):
                continue
            offenders.append(f"{number}: {stripped}")

        assert offenders == [], (
            "check.sh must not print a skip outside missing_tool — a step that "
            "cannot run has to fail the gate (issue #279). Offending lines:\n"
            + "\n".join(offenders)
        )

    def test_the_frontend_hints_do_not_advise_npm_install(self) -> None:
        # npm install resolves fresh versions and lets node_modules drift from
        # package-lock.json, which is how the local eslint became weaker than
        # CI's. npm ci installs exactly what the lockfile pins.
        assert "npm install" not in SOURCE

    def test_missing_tool_is_defined_before_it_is_used(self) -> None:
        definition = SOURCE.index("missing_tool()")
        first_call = SOURCE.index('missing_tool "')
        assert definition < first_call

    def test_every_documented_opt_out_is_actually_read_by_the_script(self) -> None:
        """A table that lies is worse than no table."""
        contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        # Not opt-outs: CHECK_DOCS_FASTPATH forces a full run past the
        # docs-only shortcut, CHECK_RANGE carries the git range from the
        # pre-push hook. Neither disables a step.
        not_opt_outs = {"CHECK_DOCS_FASTPATH", "CHECK_RANGE"}
        used = set(re.findall(r"CHECK_[A-Z_]+", SOURCE)) - not_opt_outs
        documented = set(re.findall(r"CHECK_[A-Z_]+", contributing)) - not_opt_outs

        assert used - documented == set(), f"undocumented opt-outs: {sorted(used - documented)}"
        assert documented - used == set(), f"documented but unused: {sorted(documented - used)}"
