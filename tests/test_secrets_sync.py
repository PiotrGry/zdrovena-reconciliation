"""Tests for scripts/secrets_sync.py.

Mocks zdrovena.common._keyvault.get_keyvault_secret/set_keyvault_secret for
pull/push (never hits a real Key Vault), and subprocess.run for
encrypt/decrypt (never requires the real `sops` binary — same approach as
tests/test_local_secret_fallback.py). Uses tmp_path for .env.local /
.env.local.sops so the real repo files are never touched.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts import secrets_sync as sync
from scripts.secrets_manifest import ENV_LOCAL_SECRETS, SOPS_ONLY_SECRETS

_REAL_SOPS_AVAILABLE = shutil.which("sops") is not None and shutil.which("age-keygen") is not None


def _setup_real_sops(tmp_path, monkeypatch) -> Path:
    """Generate a throwaway age keypair and a matching .sops.yaml in tmp_path.

    Returns the private key file. sops discovers .sops.yaml relative to the
    process's CWD (verified empirically — NOT relative to the input file's
    own directory), so this chdirs into tmp_path where both the config and
    ENV_LOCAL_PATH/SOPS_PATH (set by the _isolate_paths autouse fixture)
    live. That matches real usage too: the CLI is invoked as
    `uv run python scripts/secrets_sync.py encrypt` from the repo root,
    where .sops.yaml also lives.
    """
    keygen = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    # age-keygen writes the private key (plus a "# public key: ..." comment
    # line) to stdout; stderr only gets a human-readable "Public key: ..."
    # status line. Parse the comment line from stdout so we get the exact
    # key deterministically.
    public_key_line = next(
        line for line in keygen.stdout.splitlines() if line.startswith("# public key:")
    )
    public_key = public_key_line.split(":", 1)[1].strip()

    age_key_file = tmp_path / "age-keys.txt"
    age_key_file.write_text(keygen.stdout)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(age_key_file))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".sops.yaml").write_text(
        f"creation_rules:\n  - path_regex: \\.env\\.local\\.sops$\n    age: {public_key}\n"
    )
    return age_key_file


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
            if name == "allegro-client-secret":
                return "secret-client-value"
            if name == "notify-phone":
                return "48123123123"
            return None

        with patch("zdrovena.common._keyvault.get_keyvault_secret", side_effect=fake_get):
            rc = sync.cmd_pull(None)

        assert rc == 0
        content = sync.ENV_LOCAL_PATH.read_text()
        assert "ALLEGRO_CLIENT_SECRET=secret-client-value" in content
        assert "NOTIFY_PHONE=48123123123" in content
        # Everything else in the manifest was not found -> not written.
        assert "ALLEGRO_CLIENT_ID=" not in content

    def test_never_writes_a_rotating_secret_into_plaintext(self, tmp_path):
        """SOPS_ONLY_SECRETS must not reach .env.local, even from Key Vault.

        An env var there is checked before the SOPS tier by get_secret(), so
        pulling a rotating token into the plaintext file would pin the
        process to a value Allegro invalidates on the next refresh.
        """

        def fake_get(vault_url, name):
            return f"kv-value-for-{name}"

        with patch("zdrovena.common._keyvault.get_keyvault_secret", side_effect=fake_get):
            rc = sync.cmd_pull(None)

        assert rc == 0
        content = sync.ENV_LOCAL_PATH.read_text()
        for name in SOPS_ONLY_SECRETS:
            assert f"{name.upper().replace('-', '_')}=" not in content

    def test_strips_a_rotating_secret_already_present_in_env_local(self, tmp_path):
        sync.ENV_LOCAL_PATH.write_text(
            "ALLEGRO_REFRESH_TOKEN=stale-shadowing-value\nOTHER=keep-me\n"
        )
        with patch("zdrovena.common._keyvault.get_keyvault_secret", return_value=None):
            rc = sync.cmd_pull(None)

        assert rc == 0
        content = sync.ENV_LOCAL_PATH.read_text()
        assert "ALLEGRO_REFRESH_TOKEN" not in content
        assert "OTHER=keep-me" in content

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

    def test_looks_up_every_manifest_secret_except_the_rotating_ones(self, tmp_path):
        seen: list[str] = []

        def fake_get(vault_url, name):
            seen.append(name)
            return None

        with patch("zdrovena.common._keyvault.get_keyvault_secret", side_effect=fake_get):
            sync.cmd_pull(None)

        # Whole manifest, minus the secrets that must never reach plaintext.
        assert seen == [name for name in ENV_LOCAL_SECRETS if name not in SOPS_ONLY_SECRETS]
        assert not SOPS_ONLY_SECRETS & set(seen)
        # Guard against SOPS_ONLY_SECRETS silently drifting out of the
        # manifest and this test degenerating into "pull everything".
        assert SOPS_ONLY_SECRETS & set(ENV_LOCAL_SECRETS)


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
        # sops matches creation_rules against the INPUT path, and
        # .sops.yaml's rule only matches "*.env.local.sops" — so sops must
        # run against a temp file with that suffix, NOT against
        # ENV_LOCAL_PATH (".env.local") directly.
        invoked_path = args[-1]
        assert invoked_path != str(sync.ENV_LOCAL_PATH)
        assert invoked_path.endswith(".env.local.sops")
        assert Path(invoked_path).parent == sync.ENV_LOCAL_PATH.parent
        # The temp plaintext file must be cleaned up after the run.
        assert not Path(invoked_path).exists()

    def test_surfaces_called_process_error_stderr(self, tmp_path, capsys):
        sync.ENV_LOCAL_PATH.write_text("KEY=value\n")
        exc = subprocess.CalledProcessError(1, ["sops", "-e"], stderr="boom: bad age key")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", side_effect=exc):
                rc = sync.cmd_encrypt(None)

        assert rc == 1
        assert "boom: bad age key" in capsys.readouterr().err
        # Failure must not leave a partial/corrupt .env.local.sops behind.
        assert not sync.SOPS_PATH.exists()

    def test_handles_timeout_gracefully(self, tmp_path, capsys):
        sync.ENV_LOCAL_PATH.write_text("KEY=value\n")
        exc = subprocess.TimeoutExpired(cmd=["sops", "-e"], timeout=30)
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", side_effect=exc):
                rc = sync.cmd_encrypt(None)

        assert rc == 1
        assert "error" in capsys.readouterr().err.lower()
        assert not sync.SOPS_PATH.exists()

    def test_handles_binary_vanishing_gracefully(self, tmp_path, capsys):
        sync.ENV_LOCAL_PATH.write_text("KEY=value\n")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", side_effect=FileNotFoundError("sops")):
                rc = sync.cmd_encrypt(None)

        assert rc == 1
        assert "error" in capsys.readouterr().err.lower()
        assert not sync.SOPS_PATH.exists()


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

    def test_surfaces_called_process_error_stderr(self, tmp_path, capsys):
        sync.SOPS_PATH.write_text("encrypted-content")
        exc = subprocess.CalledProcessError(1, ["sops", "-d"], stderr="boom: no age key")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", side_effect=exc):
                rc = sync.cmd_decrypt(None)

        assert rc == 1
        assert "boom: no age key" in capsys.readouterr().err
        # Failure must not leave a partial/corrupt .env.local behind.
        assert not sync.ENV_LOCAL_PATH.exists()

    def test_handles_timeout_gracefully(self, tmp_path, capsys):
        sync.SOPS_PATH.write_text("encrypted-content")
        exc = subprocess.TimeoutExpired(cmd=["sops", "-d"], timeout=30)
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", side_effect=exc):
                rc = sync.cmd_decrypt(None)

        assert rc == 1
        assert "error" in capsys.readouterr().err.lower()
        assert not sync.ENV_LOCAL_PATH.exists()

    def test_handles_binary_vanishing_gracefully(self, tmp_path, capsys):
        sync.SOPS_PATH.write_text("encrypted-content")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", side_effect=FileNotFoundError("sops")):
                rc = sync.cmd_decrypt(None)

        assert rc == 1
        assert "error" in capsys.readouterr().err.lower()
        assert not sync.ENV_LOCAL_PATH.exists()


@pytest.mark.skipif(
    not _REAL_SOPS_AVAILABLE, reason="sops and/or age-keygen binaries not installed"
)
class TestRealSopsRoundTrip:
    """Exercises cmd_encrypt/cmd_decrypt against the REAL sops/age binaries.

    Fully-mocked subprocess.run tests (above) can't catch a wrong sops
    invocation — they'll happily assert on whatever args we passed even if
    those args would fail against a real .sops.yaml. This test generates a
    throwaway age keypair, writes a real .sops.yaml pointing at it (using
    the same path_regex as .sops.yaml.example), and runs encrypt then
    decrypt for real, confirming the round trip recovers the original
    content — this is what actually caught the "no matching creation
    rules found" bug (sops selects rules by matching the INPUT file path,
    not --output/--output-type; encrypting ".env.local" directly doesn't
    match "\\.env\\.local\\.sops$").
    """

    def test_encrypt_then_decrypt_recovers_original_content(self, tmp_path, monkeypatch):
        _setup_real_sops(tmp_path, monkeypatch)

        original_content = "ALLEGRO_CLIENT_ID=real-round-trip-value\nOTHER_KEY=other-value\n"
        sync.ENV_LOCAL_PATH.write_text(original_content)

        rc = sync.cmd_encrypt(None)
        assert rc == 0, "cmd_encrypt failed against a real sops/age setup"
        assert sync.SOPS_PATH.exists()

        encrypted_content = sync.SOPS_PATH.read_text()
        # Actually encrypted — plaintext values must not appear verbatim.
        assert "real-round-trip-value" not in encrypted_content
        assert "ENC[" in encrypted_content

        # Simulate bootstrapping a fresh machine: .env.local is gone, only
        # the encrypted snapshot remains.
        sync.ENV_LOCAL_PATH.unlink()

        rc = sync.cmd_decrypt(None)
        assert rc == 0, "cmd_decrypt failed against a real sops/age setup"
        decrypted_content = sync.ENV_LOCAL_PATH.read_text()
        assert "ALLEGRO_CLIENT_ID=real-round-trip-value" in decrypted_content
        assert "OTHER_KEY=other-value" in decrypted_content

    def test_encrypt_preserves_a_key_that_only_exists_in_the_snapshot(self, tmp_path, monkeypatch):
        """The rotated-token case, end to end against real sops.

        set_secret() writes a rotated Allegro refresh token straight into
        .env.local.sops; that key deliberately has no .env.local counterpart
        (an env var there would shadow the whole SOPS tier). A later
        `encrypt` must not drop it.
        """
        age_key_file = _setup_real_sops(tmp_path, monkeypatch)

        # Snapshot holds the rotated token; .env.local never does.
        sync.ENV_LOCAL_PATH.write_text("ALLEGRO_CLIENT_ID=client-id-v1\n")
        assert sync.cmd_encrypt(argparse.Namespace(replace=True)) == 0
        from zdrovena.common import _local_secret_fallback as fallback

        monkeypatch.setattr(fallback, "_SOPS_FILE", sync.SOPS_PATH)
        monkeypatch.setattr(fallback, "_AGE_KEY_FILE", age_key_file)
        assert fallback.write_local_fallback("allegro-refresh-token", "rotated-token-v2") is True

        # A routine re-encrypt after editing .env.local must keep it.
        sync.ENV_LOCAL_PATH.write_text("ALLEGRO_CLIENT_ID=client-id-v2\n")
        assert sync.cmd_encrypt(argparse.Namespace(replace=False)) == 0

        assert fallback.read_local_fallback("allegro-refresh-token") == "rotated-token-v2"
        assert fallback.read_local_fallback("allegro-client-id") == "client-id-v2"

    def test_replace_flag_drops_snapshot_only_keys(self, tmp_path, monkeypatch):
        """--replace is the documented way to actually remove a key."""
        age_key_file = _setup_real_sops(tmp_path, monkeypatch)

        sync.ENV_LOCAL_PATH.write_text("ALLEGRO_CLIENT_ID=client-id\n")
        assert sync.cmd_encrypt(argparse.Namespace(replace=True)) == 0
        from zdrovena.common import _local_secret_fallback as fallback

        monkeypatch.setattr(fallback, "_SOPS_FILE", sync.SOPS_PATH)
        monkeypatch.setattr(fallback, "_AGE_KEY_FILE", age_key_file)
        assert fallback.write_local_fallback("allegro-refresh-token", "doomed") is True
        assert fallback.read_local_fallback("allegro-refresh-token") == "doomed"

        assert sync.cmd_encrypt(argparse.Namespace(replace=True)) == 0
        assert fallback.read_local_fallback("allegro-refresh-token") is None


class TestEncryptMergeGuards:
    def test_refuses_to_overwrite_an_undecryptable_snapshot(self, tmp_path, capsys):
        sync.ENV_LOCAL_PATH.write_text("KEY=value\n")
        sync.SOPS_PATH.write_text("ENC[corrupted-or-wrong-key]\n")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch.object(sync, "_decrypt_sops_file", return_value=None):
                rc = sync.cmd_encrypt(argparse.Namespace(replace=False))

        assert rc == 1
        assert "could not be decrypted" in capsys.readouterr().err
        # The unreadable snapshot must be left exactly as it was.
        assert sync.SOPS_PATH.read_text() == "ENC[corrupted-or-wrong-key]\n"

    def test_replace_overwrites_even_an_undecryptable_snapshot(self, tmp_path):
        sync.ENV_LOCAL_PATH.write_text("KEY=value\n")
        sync.SOPS_PATH.write_text("ENC[corrupted]\n")
        proc = MagicMock(stdout="ENC[fresh]\n")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch("subprocess.run", return_value=proc):
                rc = sync.cmd_encrypt(argparse.Namespace(replace=True))

        assert rc == 0
        assert sync.SOPS_PATH.read_text() == "ENC[fresh]\n"

    def test_missing_snapshot_needs_no_decrypt(self, tmp_path):
        """First-ever encrypt: nothing to merge, no decrypt attempted."""
        sync.ENV_LOCAL_PATH.write_text("KEY=value\n")
        proc = MagicMock(stdout="ENC[...]\n")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            with patch.object(sync, "_decrypt_sops_file") as decrypt:
                with patch("subprocess.run", return_value=proc):
                    rc = sync.cmd_encrypt(argparse.Namespace(replace=False))

        assert rc == 0
        decrypt.assert_not_called()
