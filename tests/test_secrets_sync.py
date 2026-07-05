"""Tests for scripts/secrets_sync.py.

Mocks zdrovena.common._keyvault.get_keyvault_secret/set_keyvault_secret for
pull/push (never hits a real Key Vault), and subprocess.run for
encrypt/decrypt (never requires the real `sops` binary — same approach as
tests/test_local_secret_fallback.py). Uses tmp_path for .env.local /
.env.local.sops so the real repo files are never touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts import secrets_sync as sync
from scripts.secrets_manifest import ENV_LOCAL_SECRETS


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Point the module's file-location constants at a fresh temp dir."""
    monkeypatch.setattr(sync, "ENV_LOCAL_PATH", tmp_path / ".env.local")
    monkeypatch.setattr(sync, "SOPS_PATH", tmp_path / ".env.local.sops")
    monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://fake-vault.vault.azure.net/")
    return tmp_path


class TestPull:
    def test_require_vault_url_missing(self, monkeypatch):
        monkeypatch.delenv("AZURE_KEYVAULT_URL", raising=False)
        rc = sync.cmd_pull(None)
        assert rc == 1

    def test_writes_found_secrets_and_reports_missing(self, tmp_path):
        def fake_get(vault_url, name):
            if name == "allegro-refresh-token":
                return "secret-refresh-value"
            if name == "notify-phone":
                return "48123123123"
            return None

        with patch("zdrovena.common._keyvault.get_keyvault_secret", side_effect=fake_get):
            rc = sync.cmd_pull(None)

        assert rc == 0
        content = sync.ENV_LOCAL_PATH.read_text()
        assert "ALLEGRO_REFRESH_TOKEN=secret-refresh-value" in content
        assert "NOTIFY_PHONE=48123123123" in content
        # Everything else in the manifest was not found -> not written.
        assert "ALLEGRO_CLIENT_ID=" not in content

    def test_missing_secrets_do_not_crash(self, tmp_path):
        with patch("zdrovena.common._keyvault.get_keyvault_secret", return_value=None):
            rc = sync.cmd_pull(None)
        assert rc == 0
        # No secrets found -> file may be empty/untouched, but must exist cleanly.
        assert sync.ENV_LOCAL_PATH.exists()
        assert sync.ENV_LOCAL_PATH.read_text() == ""

    def test_preserves_existing_unrelated_lines(self, tmp_path):
        sync.ENV_LOCAL_PATH.write_text(
            "AZURE_STORAGE_CONNECTION_STRING=devstore\nAZURE_AUTH_DISABLED=true\n# a comment\n"
        )

        def fake_get(vault_url, name):
            return "new-value" if name == "shopify-access-token" else None

        with patch("zdrovena.common._keyvault.get_keyvault_secret", side_effect=fake_get):
            sync.cmd_pull(None)

        content = sync.ENV_LOCAL_PATH.read_text()
        assert "AZURE_STORAGE_CONNECTION_STRING=devstore" in content
        assert "AZURE_AUTH_DISABLED=true" in content
        assert "# a comment" in content
        assert "SHOPIFY_ACCESS_TOKEN=new-value" in content

    def test_updates_existing_matching_line_in_place(self, tmp_path):
        sync.ENV_LOCAL_PATH.write_text("SHOPIFY_ACCESS_TOKEN=old-value\nOTHER=x\n")

        def fake_get(vault_url, name):
            return "rotated-value" if name == "shopify-access-token" else None

        with patch("zdrovena.common._keyvault.get_keyvault_secret", side_effect=fake_get):
            sync.cmd_pull(None)

        lines = sync.ENV_LOCAL_PATH.read_text().splitlines()
        assert lines.count("SHOPIFY_ACCESS_TOKEN=rotated-value") == 1
        assert "OTHER=x" in lines

    def test_looks_up_every_manifest_secret(self, tmp_path):
        seen: list[str] = []

        def fake_get(vault_url, name):
            seen.append(name)
            return None

        with patch("zdrovena.common._keyvault.get_keyvault_secret", side_effect=fake_get):
            sync.cmd_pull(None)

        assert seen == ENV_LOCAL_SECRETS


class TestPush:
    def test_require_vault_url_missing(self, monkeypatch):
        monkeypatch.delenv("AZURE_KEYVAULT_URL", raising=False)
        rc = sync.cmd_push(None)
        assert rc == 1

    def test_pushes_only_present_values(self, tmp_path):
        sync.ENV_LOCAL_PATH.write_text("ALLEGRO_CLIENT_ID=my-client-id\nSOME_UNRELATED_VAR=x\n")
        calls: list[tuple[str, str, str]] = []

        def fake_set(vault_url, name, value):
            calls.append((vault_url, name, value))
            return True

        with patch("zdrovena.common._keyvault.set_keyvault_secret", side_effect=fake_set):
            rc = sync.cmd_push(None)

        assert rc == 0
        assert calls == [
            ("https://fake-vault.vault.azure.net/", "allegro-client-id", "my-client-id")
        ]

    def test_no_local_values_pushes_nothing(self, tmp_path):
        with patch("zdrovena.common._keyvault.set_keyvault_secret") as mock_set:
            rc = sync.cmd_push(None)
        assert rc == 0
        mock_set.assert_not_called()

    def test_reports_failure_and_returns_nonzero(self, tmp_path):
        sync.ENV_LOCAL_PATH.write_text("ALLEGRO_CLIENT_ID=my-client-id\n")
        with patch("zdrovena.common._keyvault.set_keyvault_secret", return_value=False):
            rc = sync.cmd_push(None)
        assert rc == 1


class TestEncrypt:
    def test_errors_when_sops_missing(self, tmp_path):
        sync.ENV_LOCAL_PATH.write_text("KEY=value\n")
        with patch("shutil.which", return_value=None):
            rc = sync.cmd_encrypt(None)
        assert rc == 1

    def test_errors_when_env_local_missing(self, tmp_path):
        with patch("shutil.which", return_value="/usr/bin/sops"):
            rc = sync.cmd_encrypt(None)
        assert rc == 1

    def test_invokes_sops_and_writes_output(self, tmp_path):
        sync.ENV_LOCAL_PATH.write_text("KEY=value\n")
        proc = MagicMock(stdout="ENC[...]\n")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", return_value=proc) as run:
                rc = sync.cmd_encrypt(None)

        assert rc == 0
        assert sync.SOPS_PATH.read_text() == "ENC[...]\n"
        args = run.call_args.args[0]
        assert args[:2] == ["sops", "-e"]
        assert "--input-type" in args and "dotenv" in args
        assert args[-1] == str(sync.ENV_LOCAL_PATH)


class TestDecrypt:
    def test_errors_when_sops_missing(self, tmp_path):
        sync.SOPS_PATH.write_text("encrypted")
        with patch("shutil.which", return_value=None):
            rc = sync.cmd_decrypt(None)
        assert rc == 1

    def test_errors_when_sops_file_missing(self, tmp_path):
        with patch("shutil.which", return_value="/usr/bin/sops"):
            rc = sync.cmd_decrypt(None)
        assert rc == 1

    def test_invokes_sops_and_writes_output(self, tmp_path):
        sync.SOPS_PATH.write_text("encrypted-content")
        proc = MagicMock(stdout="KEY=value\n")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", return_value=proc) as run:
                rc = sync.cmd_decrypt(None)

        assert rc == 0
        assert sync.ENV_LOCAL_PATH.read_text() == "KEY=value\n"
        args = run.call_args.args[0]
        assert args[:2] == ["sops", "-d"]
        assert args[-1] == str(sync.SOPS_PATH)

    def test_warns_before_overwriting_existing_env_local(self, tmp_path, capsys):
        sync.SOPS_PATH.write_text("encrypted-content")
        sync.ENV_LOCAL_PATH.write_text("OLD_KEY=old\n")
        proc = MagicMock(stdout="NEW_KEY=new\n")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", return_value=proc):
                sync.cmd_decrypt(None)

        captured = capsys.readouterr()
        assert "warning" in captured.out.lower()
        assert sync.ENV_LOCAL_PATH.read_text() == "NEW_KEY=new\n"
