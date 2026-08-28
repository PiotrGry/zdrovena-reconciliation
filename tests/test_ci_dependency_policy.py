"""Policy tests for how CI and the images install dependencies.

uv.lock only means something if the things that matter read it. CI and both
Dockerfiles used to install with pip, which ignores the lockfile, so a green
pipeline did not establish what shipped. Measured before the fix: 46 of 78
shared packages in the production image were at a version other than the one
uv.lock pins, fastapi among them (issue #278).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CONVERTED = [
    WORKFLOWS / "_quality-gate.yml",
    WORKFLOWS / "mutation.yml",
    WORKFLOWS / "_full-test-suite.yml",
]
DOCKERFILES = [REPO_ROOT / "Dockerfile", REPO_ROOT / "Dockerfile.dev"]


def _install_lines(path: Path) -> list[str]:
    """Lines that actually run an install, ignoring comments about one."""
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if "pip install" in line and not line.strip().lstrip("#").strip().startswith("#")
        if not line.strip().startswith("#")
    ]


class TestInstallsComeFromTheLockfile:
    def test_no_converted_workflow_installs_with_pip(self) -> None:
        offenders = {path.name: _install_lines(path) for path in CONVERTED}
        offenders = {name: lines for name, lines in offenders.items() if lines}

        assert offenders == {}, (
            f"pip ignores uv.lock, so these installs would not match what ships: {offenders}"
        )

    def test_no_dockerfile_installs_with_pip(self) -> None:
        offenders = {path.name: _install_lines(path) for path in DOCKERFILES}
        offenders = {name: lines for name, lines in offenders.items() if lines}

        assert offenders == {}

    def test_every_sync_is_locked_not_frozen(self) -> None:
        """--frozen installs from the lock without checking it still matches
        pyproject.toml; --locked fails when it does not. Verified by hand: with
        an edited pyproject, --frozen builds and --locked exits 1."""
        for path in CONVERTED + DOCKERFILES:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "uv sync" not in line or line.strip().startswith("#"):
                    continue
                assert "--locked" in line, f"{path.name}: uv sync without --locked: {line.strip()}"
                assert "--frozen" not in line, (
                    f"{path.name}: --frozen is not a check: {line.strip()}"
                )

    def test_nothing_uses_all_extras(self) -> None:
        """--all-extras pulls the iac extra -> checkov -> ecdsa, whose unfixable
        Minerva CVE pyproject deliberately keeps out of pip-audit's scope."""
        for path in CONVERTED + DOCKERFILES:
            assert "--all-extras" not in path.read_text(encoding="utf-8"), path.name

    def test_the_lockfile_reaches_the_image_build_context(self) -> None:
        # The old Dockerfile copied pyproject.toml but never uv.lock, so the
        # lock could not have governed the build even in principle.
        for path in DOCKERFILES:
            assert "uv.lock" in path.read_text(encoding="utf-8"), path.name

    def test_ci_tooling_outside_the_lockfile_is_pinned(self) -> None:
        """pyright and mutmut are not project dependencies, so they run through
        uvx. Unpinned they could disagree with local runs - which is how an
        eslint rule violation reached CI on 2026-08-27."""
        for path in CONVERTED:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "uvx " not in line or line.strip().startswith("#"):
                    continue
                tool = line.split("uvx ", 1)[1].split()[0]
                assert "@" in tool, f"{path.name}: unpinned uvx tool: {tool}"
