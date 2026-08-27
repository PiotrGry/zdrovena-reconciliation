"""Policy tests for scripts/check.sh — the local pre-push quality gate.

A step that cannot run must fail the gate. Printing a skip and continuing to
"All checks passed - safe to push" is how a broken environment silently
downgrades every guarantee the gate is supposed to give (issue #279).
"""

from __future__ import annotations

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
