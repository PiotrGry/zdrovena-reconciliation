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
