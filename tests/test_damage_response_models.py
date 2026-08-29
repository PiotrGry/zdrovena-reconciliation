"""Publishing a schema must not truncate the payload (issue #356).

FastAPI filters out fields a response model does not declare. A damage case is
assembled from detection context and provider payloads, so its key set is not
fixed — declaring a schema without `extra="allow"` would silently drop fields
the operator screen reads, and nothing would fail while it happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zdrovena.api.models import (
    DamageCaseModel,
    DamageCasesResponse,
    DamageCaseWithDraftResponse,
    DamageRefreshResponse,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestNothingIsFilteredOut:
    def test_an_undeclared_field_survives(self):
        case = DamageCaseModel.model_validate(
            {"id": "c-1", "status": "needs_review", "wymyslone_pole": "zostaje"}
        )

        assert case.model_dump()["wymyslone_pole"] == "zostaje"

    def test_provider_context_survives_on_a_nested_case(self):
        payload = DamageCaseWithDraftResponse.model_validate(
            {
                "case": {"id": "c-1", "allegro_dispute_id": "D-9"},
                "draft": {"id": "d-1", "tracking_number": "T-1"},
            }
        )

        assert payload.model_dump()["case"]["allegro_dispute_id"] == "D-9"

    def test_a_list_response_keeps_extras_on_every_case(self):
        payload = DamageCasesResponse.model_validate(
            {"cases": [{"id": "a", "zoho_message_id": "m-1"}], "needs_review": 1}
        )

        assert payload.model_dump()["cases"][0]["zoho_message_id"] == "m-1"

    def test_provider_errors_survive_on_refresh(self):
        """A failure on one provider must not hide the other's result."""
        payload = DamageRefreshResponse.model_validate(
            {"allegro": {"error": "boom"}, "zoho": {"scanned": 3}, "needs_review": 2}
        )

        dumped = payload.model_dump()
        assert dumped["allegro"] == {"error": "boom"}
        assert dumped["zoho"] == {"scanned": 3}


class TestAMissingFieldIsNotAServerError:
    """Response validation runs on the way out: a required field the record
    happens to lack would become a 500 instead of a response."""

    def test_only_the_identifier_is_required(self):
        case = DamageCaseModel.model_validate({"id": "c-1"})

        assert case.status is None

    @pytest.mark.parametrize(
        "payload", [{"case": {"id": "c-1"}}, {"case": {"id": "c-1"}, "draft": None}]
    )
    def test_a_step_that_produced_no_draft_still_serialises(self, payload):
        assert DamageCaseWithDraftResponse.model_validate(payload).case.id == "c-1"


class TestTheContractStoppedBeingEmpty:
    def test_damage_endpoints_publish_a_schema(self):
        """The whole point: the generated type stops being `{[k: string]: unknown}`."""
        schema = json.loads((REPO_ROOT / "contracts" / "openapi.json").read_text())
        unconstrained = []
        for path, ops in schema["paths"].items():
            if "damage-cases" not in path:
                continue
            for method, op in ops.items():
                body = (
                    op.get("responses", {})
                    .get("200", {})
                    .get("content", {})
                    .get("application/json", {})
                    .get("schema", {})
                )
                if body and "$ref" not in body and body.get("type") == "object":
                    unconstrained.append(f"{method.upper()} {path}")

        assert unconstrained == []
