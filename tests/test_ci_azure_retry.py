"""Tests for bounded Azure CLI retries used by staging workflows."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RETRY_SCRIPT = REPO_ROOT / "scripts" / "ci" / "azure-cli-retry.sh"
TEARDOWN_SCRIPT = REPO_ROOT / "scripts" / "ci" / "teardown-staging.sh"


def _write_fake_az(tmp_path: Path, body: str) -> tuple[Path, Path]:
    attempts_file = tmp_path / "attempts"
    fake_az = tmp_path / "az"
    fake_az.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f'ATTEMPTS_FILE="{attempts_file}"',
                'attempts=$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo 0)',
                "attempts=$((attempts + 1))",
                'echo "$attempts" > "$ATTEMPTS_FILE"',
                body,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_az.chmod(fake_az.stat().st_mode | stat.S_IXUSR)
    return fake_az, attempts_file


def _retry_env(tmp_path: Path, *, max_attempts: int = 4) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "AZURE_CLI_MAX_ATTEMPTS": str(max_attempts),
        "AZURE_CLI_RETRY_DELAY_SECONDS": "0",
    }


def test_retry_succeeds_after_transient_failures(tmp_path: Path) -> None:
    _, attempts_file = _write_fake_az(
        tmp_path,
        'if [[ "$attempts" -lt 3 ]]; then exit 1; fi\necho "resolved.example.net"',
    )

    result = subprocess.run(
        ["bash", str(RETRY_SCRIPT), "az", "containerapp", "show"],
        env=_retry_env(tmp_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "resolved.example.net"
    assert attempts_file.read_text(encoding="utf-8").strip() == "3"
    assert "attempt 2/4" in result.stderr


def test_retry_preserves_failure_after_limit(tmp_path: Path) -> None:
    _, attempts_file = _write_fake_az(tmp_path, "exit 42")

    result = subprocess.run(
        ["bash", str(RETRY_SCRIPT), "az", "containerapp", "update"],
        env=_retry_env(tmp_path, max_attempts=3),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 42
    assert attempts_file.read_text(encoding="utf-8").strip() == "3"
    assert "failed after 3 attempts" in result.stderr


def test_teardown_uses_retry_wrapper(tmp_path: Path) -> None:
    _, attempts_file = _write_fake_az(
        tmp_path,
        'if [[ "$attempts" -lt 2 ]]; then exit 1; fi\nprintf "%s\\n" "$*"',
    )

    result = subprocess.run(
        ["bash", str(TEARDOWN_SCRIPT), "staging-app", "staging-rg"],
        env=_retry_env(tmp_path),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert attempts_file.read_text(encoding="utf-8").strip() == "2"
    assert "containerapp update --name staging-app" in result.stdout
    assert "--resource-group staging-rg" in result.stdout
    assert "--min-replicas 0 --max-replicas 1" in result.stdout
