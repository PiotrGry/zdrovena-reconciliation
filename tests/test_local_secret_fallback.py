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
