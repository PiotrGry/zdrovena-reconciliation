"""Code and ADR 0003 must keep describing the same network model (issue #215).

Everything here drifted silently once already: the flag defaults to false and
nothing was ever deployed, so three mutually contradictory descriptions of the
model sat in the repo without a single failure to reveal them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TF_DIR = REPO_ROOT / "infra" / "terraform"
ADR = REPO_ROOT / "docs" / "ADR" / "0003-private-network-model.md"


def _tf(name: str) -> str:
    return (TF_DIR / name).read_text(encoding="utf-8")


def _all_tf() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(TF_DIR.rglob("*.tf")))


class TestTheDecisionIsRecorded:
    def test_the_adr_exists(self):
        assert ADR.exists()

    def test_the_adr_states_cost_risk_and_limits(self):
        text = ADR.read_text(encoding="utf-8")

        for heading in ("## Decyzja", "## Uzasadnienie", "## Ograniczenia i zagrożenia"):
            assert heading in text
        assert "€" in text, "cost is part of the decision, not a footnote"


class TestServiceEndpointsNotPrivateEndpoints:
    def test_no_private_endpoint_resources_exist(self):
        """Both models at once is not defence in depth — it is two paths with
        separate failure modes, and the subnet rules are dead under PE."""
        assert 'resource "azurerm_private_endpoint"' not in _all_tf()

    def test_no_private_dns_zones_exist(self):
        assert 'resource "azurerm_private_dns_zone"' not in _all_tf()

    def test_the_container_apps_subnet_declares_service_endpoints(self):
        network = _tf("network.tf")

        assert "Microsoft.Storage" in network
        assert "Microsoft.KeyVault" in network


class TestBothServicesHaveBothModes:
    @pytest.mark.parametrize("filename", ["storage.tf", "security.tf"])
    def test_the_acl_is_conditional_on_the_flag(self, filename):
        """Key Vault had no private branch at all: enabling the flag built a
        Private Endpoint for it while leaving its firewall wide open."""
        text = _tf(filename)

        assert "enable_private_network" in text
        assert "container_apps[0].id" in text

    def test_key_vault_denies_by_default_in_private_mode(self):
        security = _tf("security.tf")

        assert 'var.enable_private_network ? "Deny" : "Allow"' in security


class TestIdentityGuaranteesAreUntouched:
    """The ADR must not have quietly bought network isolation with RBAC."""

    def test_shared_key_auth_stays_disabled(self):
        assert "shared_access_key_enabled       = false" in _tf("storage.tf")

    def test_tls_floor_stays(self):
        assert 'min_tls_version                 = "TLS1_2"' in _tf("storage.tf")

    def test_the_flag_still_defaults_to_off(self):
        """Enabling private networking stays a separate, explicitly approved change."""
        variables = _tf("variables.tf")
        block = variables.split('variable "enable_private_network"')[1].split("}")[0]

        assert "default     = false" in block


class TestCommentsMatchResources:
    def test_no_comment_claims_access_is_only_via_private_endpoint(self):
        """That sentence described a model the ACL below it does not implement."""
        assert "access only via Private Endpoint" not in _all_tf()

    def test_the_variable_description_names_the_actual_model(self):
        variables = _tf("variables.tf")
        block = variables.split('variable "enable_private_network"')[1].split("\n}")[0]

        assert "NO Private Endpoints" in block
        assert "ADR 0003" in block
