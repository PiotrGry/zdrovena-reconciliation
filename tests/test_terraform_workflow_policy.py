"""Properties of the Terraform workflow that must not regress (issue #138).

Every one of these fails silently. A workflow with no `environment:` still runs;
an approval granted before the plan exists still looks like an approval; an
`apply` that recomputes its own plan still succeeds. The workflow going green is
not evidence that any of it is right.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "terraform.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _step_names(job: dict) -> list[str]:
    return [step.get("name") or step.get("uses", "") for step in job["steps"]]


class TestEnvironmentBinding:
    def test_plan_runs_in_staging(self, workflow):
        assert workflow["jobs"]["plan"]["environment"] == "staging"

    def test_apply_runs_in_production(self, workflow):
        """Without this the job cannot see production variables, and the
        OIDC subject claim does not match what Azure was told to expect."""
        assert workflow["jobs"]["apply"]["environment"] == "production"

    def test_every_environment_used_has_a_documented_oidc_subject(self, workflow):
        """Changing `environment:` changes the OIDC subject claim, and a subject
        with no federated credential simply stops authenticating — that is how
        `terraform apply` on main broke on 2026-08-29.

        Checked against the runbook, deliberately NOT against security.tf. The
        credentials that decide whether `azure/login` succeeds sit on the Entra
        app registration and are managed by hand; the identity security.tf
        manages holds no role assignments and authenticates nothing. Asserting
        against security.tf passes while proving nothing about the workflow —
        it reads as infrastructure-as-code coverage that does not exist.
        """
        runbook = (REPO_ROOT / "docs" / "devops" / "oidc-poswiadczenia.md").read_text(
            encoding="utf-8"
        )
        # Only the registered-subjects table counts. Searching the whole document
        # also matches the incident narrative, which names the very subject that
        # had no credential — the guard would then pass on exactly the change
        # that broke production. Found by deliberately re-breaking it.
        table = runbook.split("## Zarejestrowane subjecty")[1].split("\n## ")[0]
        registered = set(re.findall(r":environment:([\w-]+)`", table))

        used = {
            job["environment"]
            for job in workflow["jobs"].values()
            if isinstance(job.get("environment"), str)
        }
        undocumented = sorted(used - registered)

        assert undocumented == [], (
            f"environments with no registered OIDC subject in the runbook: {undocumented}. "
            "Register the credential BEFORE merging the workflow change."
        )


class TestConfigurationSource:
    def test_the_alert_email_is_not_committed(self, raw):
        assert "piotr@wodahumio.pl" not in raw

    @pytest.mark.parametrize("job", ["plan", "apply"])
    def test_the_alert_email_comes_from_environment_variables(self, workflow, job):
        assert (
            workflow["jobs"][job]["env"]["TF_VAR_ops_alert_email"] == "${{ vars.OPS_ALERT_EMAIL }}"
        )

    def test_there_is_no_secrets_fallback_for_it(self, raw):
        """Two sources means nobody can say which one is live."""
        assert "secrets.OPS_ALERT_EMAIL" not in raw


class TestApprovalCoversARealPlan:
    def test_the_production_plan_is_generated_before_approval(self, workflow):
        names = _step_names(workflow["jobs"]["apply"])
        plan = next(i for i, n in enumerate(names) if n == "Terraform plan")
        approval = next(
            i
            for i, n in enumerate(names)
            if "manual-approval" in n or n == "Wait for manual approval"
        )

        assert plan < approval, "the approver would be deciding on a plan that does not exist yet"

    def test_apply_runs_after_approval(self, workflow):
        names = _step_names(workflow["jobs"]["apply"])
        approval = next(i for i, n in enumerate(names) if n == "Wait for manual approval")
        apply_step = next(i for i, n in enumerate(names) if n == "Terraform apply")

        assert approval < apply_step

    def test_apply_uses_the_saved_plan(self, workflow):
        """Otherwise apply computes a fresh plan and the approval covered
        something else."""
        step = next(
            s for s in workflow["jobs"]["apply"]["steps"] if s.get("name") == "Terraform apply"
        )

        assert step["run"].strip().endswith("tfplan")

    def test_the_plan_is_saved_to_a_file(self, workflow):
        step = next(
            s for s in workflow["jobs"]["apply"]["steps"] if s.get("name") == "Terraform plan"
        )

        assert "-out=tfplan" in step["run"]


class TestPlanArtifactIsNotPublished:
    def test_no_job_uploads_the_binary_plan(self, workflow):
        """A saved plan can contain variable values, and the PR job never
        applies it — the artifact was exposure with no consumer."""
        for job_name, job in workflow["jobs"].items():
            for step in job["steps"]:
                uses = step.get("uses", "")
                if "upload-artifact" in uses:
                    pytest.fail(f"{job_name} uploads an artifact: {step}")


class TestFailFast:
    @pytest.mark.parametrize(
        "job,step_name", [("plan", "Require plan inputs"), ("apply", "Require apply inputs")]
    )
    def test_missing_variables_fail_with_a_clear_message(self, workflow, job, step_name):
        step = next(s for s in workflow["jobs"][job]["steps"] if s.get("name") == step_name)

        assert "TF_VAR_ops_alert_email" in step["run"]
        assert "::error::" in step["run"]

    def test_apply_still_refuses_an_empty_api_client_id(self, workflow):
        """That default silently disables JWT audience validation in prod."""
        step = next(
            s for s in workflow["jobs"]["apply"]["steps"] if s.get("name") == "Require apply inputs"
        )

        assert "TF_VAR_azure_client_id_entra" in step["run"]


class TestNoCredentialRegression:
    def test_oidc_is_still_used(self, workflow):
        assert workflow["env"]["ARM_USE_OIDC"] == "true"

    def test_no_client_secret_is_introduced(self, raw):
        assert "ARM_CLIENT_SECRET" not in raw
        assert "client-secret" not in raw

    def test_the_credential_step_is_defined_once(self, raw):
        """It used to be ~25 duplicated lines in both jobs; the copy nobody
        remembers is the one that ends up wrong in production."""
        assert raw.count("ARM_TENANT_ID=$TENANT") == 0
        assert raw.count("./.github/actions/azure-terraform-credentials") == 2


class TestPullRequestsStayReadOnly:
    def test_the_plan_job_never_applies(self, workflow):
        for step in workflow["jobs"]["plan"]["steps"]:
            assert "terraform apply" not in step.get("run", "")

    def test_apply_only_runs_on_a_push_to_main(self, workflow):
        condition = workflow["jobs"]["apply"]["if"]

        assert "github.event_name == 'push'" in condition
        assert "refs/heads/main" in condition
