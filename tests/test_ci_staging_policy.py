"""Regression tests for release-PR staging routing."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PR_VALIDATE = (REPO_ROOT / ".github" / "workflows" / "pr-validate.yml").read_text(encoding="utf-8")
PATH_FILTERS = (REPO_ROOT / ".github" / "path-filters.yml").read_text(encoding="utf-8")
PROD_DEPLOY = (REPO_ROOT / ".github" / "workflows" / "prod-deploy.yml").read_text(encoding="utf-8")
REUSABLE_DEPLOY = (REPO_ROOT / ".github" / "workflows" / "_deploy.yml").read_text(encoding="utf-8")
STAGING_SCHEDULE = (REPO_ROOT / ".github" / "workflows" / "staging-schedule.yml").read_text(
    encoding="utf-8"
)
BACK_SYNC_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "back-sync-main.yml"
FULL_SUITE = (REPO_ROOT / ".github" / "workflows" / "_full-test-suite.yml").read_text(
    encoding="utf-8"
)


def test_full_staging_is_conditional_on_runtime_or_staging_changes() -> None:
    assert "needs.quality-gate.outputs.backend_runtime == 'true'" in PR_VALIDATE
    assert "needs.quality-gate.outputs.frontend == 'true'" in PR_VALIDATE
    assert "needs.quality-gate.outputs.staging == 'true'" in PR_VALIDATE
    assert "needs.quality-gate.outputs.backend == 'true'" not in PR_VALIDATE


def test_manual_release_validation_still_runs_full_staging() -> None:
    assert "github.event_name == 'workflow_dispatch'" in PR_VALIDATE


def test_staging_inputs_use_deployable_backend_filter() -> None:
    assert "backend_changed: ${{ needs.quality-gate.outputs.backend_runtime }}" in PR_VALIDATE
    assert "backend_runtime:" in PATH_FILTERS
    assert "staging:" in PATH_FILTERS


def test_docs_are_not_classified_as_runtime_or_staging() -> None:
    assert "README.md" not in PATH_FILTERS
    assert "'docs/**'" not in PATH_FILTERS


def test_production_trigger_contains_only_runtime_paths() -> None:
    trigger = PROD_DEPLOY.split("permissions:", maxsplit=1)[0]

    assert '"zdrovena/**"' in trigger
    assert '"pyproject.toml"' in trigger
    assert '"Dockerfile"' in trigger
    assert '"frontend/**"' in trigger
    assert '"tests/**"' not in trigger
    assert '"scripts/**"' not in trigger
    assert '"README.md"' not in trigger
    assert '".github/workflows/' not in trigger


def test_production_deploy_is_split_by_changed_area() -> None:
    assert "backend_changed:" in PROD_DEPLOY
    assert "frontend_changed:" in PROD_DEPLOY
    assert "if: inputs.backend_changed" in REUSABLE_DEPLOY
    assert "if: inputs.frontend_changed" in REUSABLE_DEPLOY


def test_public_swa_smoke_waits_for_both_deploy_areas() -> None:
    assert "needs: [deploy-prod, deploy-frontend]" in REUSABLE_DEPLOY
    assert "SWA smoke attempt $attempt/6" in REUSABLE_DEPLOY
    assert "SWA/backend link may still be propagating" in REUSABLE_DEPLOY


def test_staging_shutdown_uses_valid_bounded_teardown() -> None:
    assert "--max-replicas 0" not in STAGING_SCHEDULE
    assert STAGING_SCHEDULE.count("scripts/ci/teardown-staging.sh") == 2


def test_release_flow_does_not_use_automatic_back_sync() -> None:
    assert not BACK_SYNC_WORKFLOW.exists()


def _teardown_block() -> str:
    """The `teardown:` job definition, up to the next top-level job key."""
    start = FULL_SUITE.index("\n  teardown:")
    rest = FULL_SUITE[start + 1 :]
    lines = rest.splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        if line and not line.startswith("    ") and line.strip():
            break
        out.append(line)
    return "\n".join(out)


def test_teardown_failure_does_not_block_a_release() -> None:
    """Teardown is cost control, not correctness: it scales staging to zero.

    When it failed, the reusable workflow's result went red and CI Gate blocked
    the merge — so on PR #337 a 4m49s Azure OIDC login held back a production
    fix the operator was waiting for. The nightly staging-schedule cron cleans
    up regardless, so a failure here costs a warm staging environment until
    evening, not correctness.
    """
    assert "continue-on-error: true" in _teardown_block()


def test_teardown_has_room_for_a_slow_azure_login() -> None:
    """The job's real work is ~20s of `az`; the budget is dominated by login,
    which has been observed taking almost five minutes."""
    block = _teardown_block()
    timeout = next(
        int(line.split(":", 1)[1]) for line in block.splitlines() if "timeout-minutes:" in line
    )
    assert timeout >= 10, f"teardown timeout is {timeout}min — one slow login eats it"


def test_the_nightly_cron_still_backs_teardown_up() -> None:
    # This is what makes a non-blocking teardown safe rather than a gamble.
    assert "teardown-staging.sh" in STAGING_SCHEDULE
    assert "schedule:" in STAGING_SCHEDULE
