"""The pre-push hook gates what a branch adds to develop, not what a merge of develop brings in.

Pirx (and anyone else) brings a PR up to date by merging develop into it. The
pushed range `remote..local` then holds every develop commit since the branch
point: code develop's own gate already passed. Checking that range ran the full
local gate on a docs-only PR and failed it on this machine's environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}
ZERO = "0" * 40


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **IDENTITY}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(remote))
    _git(tmp_path, "init", "-q", "-b", "develop", str(repo))
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "docs-only.sh", repo / "scripts" / "docs-only.sh")
    shutil.copy(REPO_ROOT / "scripts" / "install-hooks.sh", repo / "scripts" / "install-hooks.sh")
    # Stand-in gate: records the range the hook handed it and whether it is docs-only.
    (repo / "scripts" / "check.sh").write_text(
        '#!/usr/bin/env bash\nif scripts/docs-only.sh "$CHECK_RANGE" >/dev/null; then echo DOCS; else echo FULL; fi\n',
        encoding="utf-8",
    )
    (repo / "scripts" / "check.sh").chmod(0o755)
    _commit(repo, "app.py", "x = 1\n")
    _git(repo, "push", "-q", "origin", "develop")
    subprocess.run(["bash", "scripts/install-hooks.sh"], cwd=repo, check=True, capture_output=True)
    return repo


def _hook(repo: Path, local: str, remote_ref: str, remote_sha: str) -> str:
    result = subprocess.run(
        [str(repo / ".git" / "hooks" / "pre-push"), "origin", "url"],
        cwd=repo,
        input=f"refs/heads/x {local} {remote_ref} {remote_sha}\n",
        capture_output=True,
        text=True,
        env={**os.environ, **IDENTITY, "PATH": "/usr/bin:/bin"},
        check=False,
    )
    return result.stdout + result.stderr


def test_a_docs_pr_with_develop_merged_in_is_docs_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "switch", "-q", "-c", "feature")
    pushed = _commit(repo, "CHANGELOG.md", "notes\n")
    _git(repo, "push", "-q", "--no-verify", "origin", "feature")
    _git(repo, "switch", "-q", "develop")
    _commit(repo, "app.py", "x = 2\n")
    _git(repo, "push", "-q", "--no-verify", "origin", "develop")
    _git(repo, "switch", "-q", "feature")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "origin/develop")

    assert "DOCS" in _hook(repo, _git(repo, "rev-parse", "HEAD"), "refs/heads/feature", pushed)


def test_code_on_a_branch_still_runs_the_full_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "switch", "-q", "-c", "feature")
    pushed = _commit(repo, "lib.py", "y = 1\n")
    _git(repo, "push", "-q", "--no-verify", "origin", "feature")
    local = _commit(repo, "README.md", "docs\n")

    # The PR adds code, so a docs-only follow-up push is still gated as code.
    assert "FULL" in _hook(repo, local, "refs/heads/feature", pushed)


def test_a_direct_push_to_develop_is_checked_for_what_it_pushes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    local = _commit(repo, "lib.py", "y = 1\n")

    output = _hook(repo, local, "refs/heads/develop", before)
    assert "tylko dla dokumentacji" in output
