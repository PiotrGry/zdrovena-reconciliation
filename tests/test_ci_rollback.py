"""scripts/ci/rollback-revision.sh against a fake ``az``.

Regression for the Copilot review on #495: after A (good) -> B (bad) ->
rollback copy of A -> C (bad), the rollback must restore the copy of A, never
B. History cannot tell which revision was healthy, so the deploy records the
revision active before it changes anything and passes it as the target.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "rollback-revision.sh"
DEPLOY = (REPO_ROOT / ".github" / "workflows" / "_deploy.yml").read_text(encoding="utf-8")
MANUAL = (REPO_ROOT / ".github" / "workflows" / "rollback.yml").read_text(encoding="utf-8")

IMAGE = "acr.example/zdrovena-api:"
# Revision -> image for the A, B, rollback copy of A, C history.
REVISIONS = {
    "app--a": IMAGE + "a",
    "app--b": IMAGE + "b",
    "app--rb-100": IMAGE + "a",
    "app--c": IMAGE + "c",
}

FAKE_AZ = """#!/usr/bin/env bash
echo "$*" >> "$AZ_LOG"
if [[ "$*" == *"revision show"* ]]; then
  rev=$(sed -E 's/.*--revision ([^ ]+).*/\\1/' <<< "$*")
  line=$(grep -E "^$rev " "$AZ_REVISIONS") || exit 3
  echo "${line#* }"
fi
"""


def _run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    az = bin_dir / "az"
    az.write_text(FAKE_AZ, encoding="utf-8")
    az.chmod(az.stat().st_mode | stat.S_IEXEC)
    revisions = tmp_path / "revisions"
    revisions.write_text("".join(f"{k} {v}\n" for k, v in REVISIONS.items()), encoding="utf-8")
    log = tmp_path / "az.log"
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AZ_LOG": str(log),
        "AZ_REVISIONS": str(revisions),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), "app", "rg", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc, log.read_text(encoding="utf-8")


def test_successive_failed_releases_restore_the_pre_deploy_revision(tmp_path):
    # C failed; the deploy recorded the rollback copy of A as active before it.
    proc, log = _run(tmp_path, "app--rb-100", IMAGE + "c")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    copy = [line for line in log.splitlines() if "revision copy" in line]
    assert len(copy) == 1
    assert "--from-revision app--rb-100" in copy[0]
    assert "--revision-suffix rb-" in copy[0]
    assert "app--b" not in log


def test_retry_with_the_same_recorded_target_copies_it_again(tmp_path):
    for _ in range(2):
        proc, log = _run(tmp_path, "app--a", IMAGE + "b")
        assert proc.returncode == 0
        assert "--from-revision app--a" in log


def test_refuses_a_target_running_the_bad_image(tmp_path):
    proc, log = _run(tmp_path, "app--b", IMAGE + "b")
    assert proc.returncode != 0
    assert "revision copy" not in log


def test_refuses_without_a_target(tmp_path):
    proc, log = _run(tmp_path)
    assert proc.returncode != 0
    assert "revision copy" not in log


def test_refuses_an_unknown_target(tmp_path):
    proc, log = _run(tmp_path, "app--missing")
    assert proc.returncode != 0
    assert "revision copy" not in log


def test_deploy_rolls_back_to_the_revision_recorded_before_it_changed_anything():
    record = DEPLOY.index("Record the revision running before this deploy")
    update = DEPLOY.index("Update Container App image")
    assert record < update
    assert "rollback_revision: ${{ steps.before.outputs.revision }}" in DEPLOY
    assert "TARGET: ${{ needs.deploy-prod.outputs.rollback_revision }}" in DEPLOY
    assert 'rollback-revision.sh "$PROD_APP" "$RG" "$TARGET" "$BAD_IMAGE"' in DEPLOY


def test_manual_rollback_never_guesses_from_history():
    assert "inputs.image_sha == '' && inputs.revision == ''" in MANUAL
    assert 'rollback-revision.sh "$APP" "$RG" "$TARGET"' in MANUAL
