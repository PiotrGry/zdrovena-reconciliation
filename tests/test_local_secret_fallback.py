"""Tests for zdrovena.common._local_secret_fallback.

Mocks `sops`/`age` presence and subprocess calls throughout — CI has
neither binary installed, so these tests must not depend on the real
tooling being available (that's exactly the "opt-in, no-op unless
configured" property being tested). Uses real temp files (via tmp_path +
monkeypatch on the module's path constants) instead of patching Path
methods directly, since Path instances don't support per-instance
attribute patching.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zdrovena.common import _local_secret_fallback as fallback


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Point the module's file-location constants at a fresh temp dir so
    tests never touch the real .env.local.sops or ~/.config/sops/age/keys.txt.
    """
    monkeypatch.setattr(fallback, "_SOPS_FILE", tmp_path / ".env.local.sops")
    monkeypatch.setattr(fallback, "_AGE_KEY_FILE", tmp_path / "age-keys.txt")
    monkeypatch.setattr(fallback, "_REPO_ROOT", tmp_path)
    return tmp_path


class TestAvailability:
    def test_unavailable_when_sops_missing(self):
        with patch("shutil.which", return_value=None):
            assert fallback._available() is False

    def test_unavailable_when_age_key_missing(self, tmp_path):
        # _AGE_KEY_FILE points at a temp path that doesn't exist yet.
        with patch("shutil.which", return_value="/usr/bin/sops"):
            assert fallback._available() is False

    def test_available_when_both_present(self, tmp_path):
        fallback._AGE_KEY_FILE.write_text("AGE-SECRET-KEY-fake\n")
        with patch("shutil.which", return_value="/usr/bin/sops"):
            assert fallback._available() is True


class TestReadLocalFallback:
    def test_returns_none_when_unavailable(self):
        with patch.object(fallback, "_available", return_value=False):
            assert fallback.read_local_fallback("allegro-refresh-token") is None

    def test_returns_none_when_file_missing(self):
        # _SOPS_FILE points at a temp path that doesn't exist.
        with patch.object(fallback, "_available", return_value=True):
            assert fallback.read_local_fallback("allegro-refresh-token") is None

    def test_returns_value_when_key_present(self, tmp_path):
        fallback._SOPS_FILE.write_text("encrypted-placeholder")
        proc = MagicMock(stdout="ALLEGRO_REFRESH_TOKEN=secret-value\nOTHER_KEY=x\n")
        with patch.object(fallback, "_available", return_value=True):
            with patch("subprocess.run", return_value=proc):
                result = fallback.read_local_fallback("allegro-refresh-token")
        assert result == "secret-value"

    def test_returns_none_when_key_absent(self, tmp_path):
        fallback._SOPS_FILE.write_text("encrypted-placeholder")
        proc = MagicMock(stdout="OTHER_KEY=x\n")
        with patch.object(fallback, "_available", return_value=True):
            with patch("subprocess.run", return_value=proc):
                assert fallback.read_local_fallback("allegro-refresh-token") is None

    def test_returns_none_on_decrypt_failure(self, tmp_path):
        fallback._SOPS_FILE.write_text("encrypted-placeholder")
        with patch.object(fallback, "_available", return_value=True):
            with patch("subprocess.run", side_effect=RuntimeError("sops exploded")):
                assert fallback.read_local_fallback("allegro-refresh-token") is None


class TestWriteLocalFallback:
    def test_returns_false_when_unavailable(self):
        with patch.object(fallback, "_available", return_value=False):
            assert fallback.write_local_fallback("allegro-refresh-token", "v") is False

    def test_returns_false_when_existing_file_undecryptable(self, tmp_path):
        fallback._SOPS_FILE.write_text("encrypted-placeholder")
        with patch.object(fallback, "_available", return_value=True):
            with patch.object(fallback, "_decrypt", return_value=None):
                assert fallback.write_local_fallback("allegro-refresh-token", "v") is False

    def test_creates_new_file_when_none_exists(self, tmp_path):
        encrypt_proc = MagicMock(stdout="ALLEGRO_REFRESH_TOKEN=ENC[...]\n")
        with patch.object(fallback, "_available", return_value=True):
            with patch("subprocess.run", return_value=encrypt_proc) as run:
                result = fallback.write_local_fallback("allegro-refresh-token", "new-value")
        assert result is True
        assert fallback._SOPS_FILE.read_text() == "ALLEGRO_REFRESH_TOKEN=ENC[...]\n"
        # Only one subprocess call (encrypt) since no existing file to decrypt.
        assert run.call_count == 1
        encrypt_call_args = run.call_args.args[0]
        assert encrypt_call_args[:2] == ["sops", "-e"]

    def test_updates_existing_key_preserving_others(self, tmp_path):
        fallback._SOPS_FILE.write_text("placeholder-encrypted-content")
        decrypt_proc = MagicMock(stdout="ALLEGRO_REFRESH_TOKEN=old-value\nOTHER_KEY=untouched\n")
        seen_plaintext: dict[str, str] = {}

        def _fake_run(args, **kwargs):
            if args[:2] == ["sops", "-d"]:
                return decrypt_proc
            # encrypt call: args[-1] is the plaintext temp-file path — read it
            # NOW, before write_local_fallback's `finally` deletes it.
            seen_plaintext["content"] = Path(args[-1]).read_text()
            return MagicMock(stdout="re-encrypted-content\n")

        with patch.object(fallback, "_available", return_value=True):
            with patch("subprocess.run", side_effect=_fake_run):
                result = fallback.write_local_fallback("allegro-refresh-token", "new-value")

        assert result is True
        assert fallback._SOPS_FILE.read_text() == "re-encrypted-content\n"
        assert "ALLEGRO_REFRESH_TOKEN=new-value" in seen_plaintext["content"]
        assert "OTHER_KEY=untouched" in seen_plaintext["content"]

    def test_returns_false_on_encrypt_failure(self, tmp_path):
        with patch.object(fallback, "_available", return_value=True):
            with patch("subprocess.run", side_effect=RuntimeError("boom")):
                result = fallback.write_local_fallback("allegro-refresh-token", "v")
        assert result is False
        assert not fallback._SOPS_FILE.exists()


class TestTempFileLocation:
    """Temp files must land next to the target, not at the repo root.

    write_local_fallback finishes with os.replace(), which is only atomic
    within a single filesystem. Under ZDROVENA_SOPS_FILE the target lives in
    a bind-mounted directory that is a different mount than the repo root, so
    staging the temp file at the repo root would make the rename cross a
    mount boundary and fail.
    """

    def test_temp_files_are_created_beside_the_target_file(self, tmp_path):
        secrets_dir = tmp_path / "mounted-secrets"
        secrets_dir.mkdir()
        elsewhere = tmp_path / "repo-root"
        elsewhere.mkdir()

        with patch.object(fallback, "_SOPS_FILE", secrets_dir / ".env.local.sops"):
            with patch.object(fallback, "_REPO_ROOT", elsewhere):
                tmp_dirs: list[Path] = []

                def _fake_run(args, **kwargs):
                    tmp_dirs.append(Path(args[-1]).parent)
                    return MagicMock(stdout="encrypted\n")

                with patch.object(fallback, "_available", return_value=True):
                    with patch("subprocess.run", side_effect=_fake_run):
                        assert fallback.write_local_fallback("allegro-refresh-token", "v") is True

                assert tmp_dirs == [secrets_dir]
                assert list(elsewhere.iterdir()) == []
                # And nothing was left behind next to the target either.
                assert [p.name for p in secrets_dir.iterdir()] == [".env.local.sops"]


class TestTargetIdentityIsPreserved:
    """A rewrite must not change who owns the file or who can read it.

    os.replace() swaps in a new inode. The dev container runs as root and
    writes the same .env.local.sops the host developer does, so without
    carrying the previous mode/ownership across, one rotation inside the
    container leaves the host locked out of its own secrets.
    """

    def test_existing_mode_survives_a_rewrite(self, tmp_path):
        fallback._SOPS_FILE.write_text("placeholder-encrypted-content")
        fallback._SOPS_FILE.chmod(0o640)
        decrypt_proc = MagicMock(stdout="OTHER_KEY=untouched\n")

        def _fake_run(args, **kwargs):
            if args[:2] == ["sops", "-d"]:
                return decrypt_proc
            return MagicMock(stdout="re-encrypted\n")

        with patch.object(fallback, "_available", return_value=True):
            with patch("subprocess.run", side_effect=_fake_run):
                assert fallback.write_local_fallback("allegro-refresh-token", "v") is True

        assert fallback._SOPS_FILE.stat().st_mode & 0o777 == 0o640

    def test_new_file_stays_private(self, tmp_path):
        encrypt_proc = MagicMock(stdout="ENC[...]\n")
        with patch.object(fallback, "_available", return_value=True):
            with patch("subprocess.run", return_value=encrypt_proc):
                assert fallback.write_local_fallback("allegro-refresh-token", "v") is True

        # No prior file to inherit from — mkstemp's owner-only default holds.
        assert fallback._SOPS_FILE.stat().st_mode & 0o777 == 0o600

    def test_write_still_succeeds_when_ownership_cannot_be_carried(self, tmp_path):
        """Non-root host rewriting a file it does not own must not fail."""
        fallback._SOPS_FILE.write_text("placeholder")
        decrypt_proc = MagicMock(stdout="OTHER_KEY=x\n")

        def _fake_run(args, **kwargs):
            if args[:2] == ["sops", "-d"]:
                return decrypt_proc
            return MagicMock(stdout="re-encrypted\n")

        with patch.object(fallback, "_available", return_value=True):
            with patch("subprocess.run", side_effect=_fake_run):
                with patch("os.chown", side_effect=PermissionError("not root")):
                    result = fallback.write_local_fallback("allegro-refresh-token", "v")

        assert result is True
        assert fallback._SOPS_FILE.read_text() == "re-encrypted\n"


class TestPathOverrides:
    """ZDROVENA_SOPS_FILE / SOPS_AGE_KEY_FILE are read at import time."""

    @staticmethod
    def _reload_with(monkeypatch, **env):
        import importlib

        for key in ("ZDROVENA_SOPS_FILE", "SOPS_AGE_KEY_FILE"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(fallback)

    @pytest.fixture(autouse=True)
    def _restore_module(self, monkeypatch):
        yield
        import importlib

        for key in ("ZDROVENA_SOPS_FILE", "SOPS_AGE_KEY_FILE"):
            monkeypatch.delenv(key, raising=False)
        importlib.reload(fallback)

    def test_sops_file_defaults_to_repo_root(self, monkeypatch):
        mod = self._reload_with(monkeypatch)
        resolved = mod._SOPS_FILE
        assert resolved == mod._REPO_ROOT / ".env.local.sops"

    def test_sops_file_honours_override(self, monkeypatch, tmp_path):
        target = tmp_path / "secrets" / ".env.local.sops"
        mod = self._reload_with(monkeypatch, ZDROVENA_SOPS_FILE=str(target))
        resolved = mod._SOPS_FILE
        assert resolved == target

    def test_age_key_file_defaults_to_home(self, monkeypatch):
        mod = self._reload_with(monkeypatch)
        resolved = mod._AGE_KEY_FILE
        assert resolved == Path.home() / ".config" / "sops" / "age" / "keys.txt"

    def test_age_key_file_honours_sops_own_variable(self, monkeypatch, tmp_path):
        key = tmp_path / "mounted" / "keys.txt"
        mod = self._reload_with(monkeypatch, SOPS_AGE_KEY_FILE=str(key))
        resolved = mod._AGE_KEY_FILE
        assert resolved == key

    def test_empty_override_falls_back_to_default(self, monkeypatch):
        mod = self._reload_with(monkeypatch, ZDROVENA_SOPS_FILE="")
        resolved = mod._SOPS_FILE
        assert resolved == mod._REPO_ROOT / ".env.local.sops"
