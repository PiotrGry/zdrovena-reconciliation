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
RELEASE_VALIDATION = (REPO_ROOT / ".github" / "workflows" / "release-validation.yml").read_text(
    encoding="utf-8"
)
CONTINUOUS_DELIVERY = (REPO_ROOT / ".github" / "workflows" / "continuous-delivery.yml").read_text(
    encoding="utf-8"
)
AUTO_MERGE_DEVELOP = (REPO_ROOT / ".github" / "workflows" / "auto-merge-develop.yml").read_text(
    encoding="utf-8"
)
PROD_HEALTH = (REPO_ROOT / ".github" / "workflows" / "prod-health.yml").read_text(encoding="utf-8")
SMOKE_TEST = (REPO_ROOT / "scripts" / "ci" / "smoke-test.sh").read_text(encoding="utf-8")
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


def test_production_deploy_requires_exact_main_sha() -> None:
    assert 'RELEASE_REF" != "refs/heads/main' in PROD_DEPLOY
    assert 'EXPECTED_SHA" != "$RELEASE_SHA' in PROD_DEPLOY
    assert "expected_sha:" in PROD_DEPLOY
    assert "inputs.backend_changed" in PROD_DEPLOY
    assert "inputs.frontend_changed" in PROD_DEPLOY


def test_post_deploy_verifies_immutable_backend_and_frontend_revisions() -> None:
    assert "Backend image mismatch" in REUSABLE_DEPLOY
    assert "Poller image mismatch" in REUSABLE_DEPLOY
    assert "Frontend version mismatch" in REUSABLE_DEPLOY
    assert "/version.json" in REUSABLE_DEPLOY


def test_health_and_shell_smoke_reject_false_green_401() -> None:
    assert 'HTTP" == "200"' in SMOKE_TEST
    assert 'HTTP" == "401"' not in SMOKE_TEST
    assert 'HTTP_DOCS" == "401"' not in SMOKE_TEST
    assert 'HTTP" == "200"' in PROD_HEALTH
    assert 'HTTP" != "401"' not in PROD_HEALTH
    assert "status/version payload" in PROD_HEALTH
    assert "40-character Git SHA" in PROD_HEALTH


def test_missing_smoke_token_fails_instead_of_skipping() -> None:
    assert "SKIP: nie udało się pobrać tokenu" not in SMOKE_TEST
    assert 'fail "nie udało się pobrać tokenu' in SMOKE_TEST


def test_staging_shutdown_uses_valid_bounded_teardown() -> None:
    assert "--max-replicas 0" not in STAGING_SCHEDULE
    assert STAGING_SCHEDULE.count("scripts/ci/teardown-staging.sh") == 2


def test_continuous_delivery_revalidates_exact_shas_at_each_mutation() -> None:
    assert "expected_sha" in CONTINUOUS_DELIVERY
    assert "--match-head-commit" in CONTINUOUS_DELIVERY
    assert "mergeStateStatus" in CONTINUOUS_DELIVERY
    assert "back_sync_ready" in CONTINUOUS_DELIVERY


def test_continuous_delivery_polls_children_instead_of_chaining_their_workflow_runs() -> None:
    assert CONTINUOUS_DELIVERY.count("await_dispatched_run()") == 2
    assert "No $workflow run appeared for exact SHA $expected_sha" in CONTINUOUS_DELIVERY
    assert '[[ "$conclusion" == "success" ]]' in CONTINUOUS_DELIVERY
    assert "github.event.workflow_run.name == 'PR Validate" not in CONTINUOUS_DELIVERY
    assert "github.event.workflow_run.name == 'Production Deploy" not in CONTINUOUS_DELIVERY


def test_actions_token_merge_starts_release_only_after_exact_pr_is_merged() -> None:
    assert "workflows: [Develop — Fast Gate]" in CONTINUOUS_DELIVERY
    assert "github.event.workflow_run.event == 'pull_request'" in CONTINUOUS_DELIVERY
    assert '[[ "$SOURCE_STATE" == "MERGED"' in CONTINUOUS_DELIVERY
    assert '[[ "$(jq -r .headRefOid' in CONTINUOUS_DELIVERY
    assert "PUSH_SHA=$(jq -r '.mergeCommit.oid'" in CONTINUOUS_DELIVERY


def test_bot_created_prs_receive_status_only_after_exact_validation() -> None:
    release_status = "-f context='CI Gate'"
    back_sync_status = "-f context='Fast gate / Quality Gate'"
    assert release_status in CONTINUOUS_DELIVERY
    assert back_sync_status in CONTINUOUS_DELIVERY
    assert CONTINUOUS_DELIVERY.index(
        "await_dispatched_run pr-validate.yml"
    ) < CONTINUOUS_DELIVERY.index(release_status)
    assert CONTINUOUS_DELIVERY.index(
        "await_dispatched_run develop-gate.yml"
    ) < CONTINUOUS_DELIVERY.index(back_sync_status)


def test_develop_auto_merge_never_executes_pull_request_code() -> None:
    assert "pull_request_target:" in AUTO_MERGE_DEVELOP
    assert "actions/checkout" not in AUTO_MERGE_DEVELOP
    assert "head.repo.full_name == github.repository" in AUTO_MERGE_DEVELOP
    assert "--match-head-commit" in AUTO_MERGE_DEVELOP


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


def test_staging_and_release_validation_are_on_demand_only() -> None:
    staging_trigger = STAGING_SCHEDULE.split("permissions:", maxsplit=1)[0]
    validation_trigger = RELEASE_VALIDATION.split("permissions:", maxsplit=1)[0]
    assert "\n  schedule:" not in staging_trigger
    assert "\n  workflow_dispatch:" in staging_trigger
    assert "\n  schedule:" not in validation_trigger
    assert "\n  workflow_dispatch:" in validation_trigger
    assert "uses: ./.github/workflows/_full-test-suite.yml" in RELEASE_VALIDATION
    assert "preview_environment: on-demand-${{ github.run_id }}" in RELEASE_VALIDATION


def test_full_suite_uses_and_deletes_the_same_isolated_preview() -> None:
    assert "preview_environment:" in FULL_SUITE
    assert "steps.swa-deploy.outputs.static_web_app_url" in FULL_SUITE
    assert '--environment-name "$PREVIEW_ENV"' in FULL_SUITE
