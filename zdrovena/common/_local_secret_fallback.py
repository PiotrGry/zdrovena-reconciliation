"""zdrovena.common._local_secret_fallback — local SOPS+age encrypted secret store.

Used by zdrovena.common.secrets as a fallback tier when Azure Key Vault is
unreachable (e.g. a dev sandbox with no Azure connectivity at all). Replaces
the old OS-keyring tier: keyring behaves inconsistently across the multiple
operating systems this project is developed from (macOS Keychain vs. Linux
Secret Service vs. Windows Credential Manager — the latter two often
unavailable on headless/sandboxed Linux boxes). SOPS+age behaves identically
on every OS, which is the property actually needed here.

Backed by one encrypted dotenv file at the repo root: .env.local.sops.
Requires the `sops` and `age` binaries plus a local age private key
(~/.config/sops/age/keys.txt) — if either is missing, every function here
returns None/False (a silent no-op), never raises. This tier is opt-in: an
environment with neither `sops` installed nor an age key configured behaves
exactly as if this module didn't exist.

Encryption always goes through a temporary file whose name ends in
".env.local.sops" (matching the path_regex rule in .sops.yaml), then is
moved atomically into place — the real target file is never left holding
plaintext, even briefly, and a crash mid-write can't corrupt it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("zdrovena.common._local_secret_fallback")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOPS_FILE = _REPO_ROOT / ".env.local.sops"
_AGE_KEY_FILE = Path.home() / ".config" / "sops" / "age" / "keys.txt"
_SUBPROCESS_TIMEOUT = 15


def _available() -> bool:
    return bool(shutil.which("sops")) and _AGE_KEY_FILE.exists()


def _to_env_key(service: str) -> str:
    return service.upper().replace("-", "_")


def _decrypt(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["sops", "-d", "--input-type", "dotenv", "--output-type", "dotenv", str(path)],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=True,
        )
    except Exception as exc:
        logger.debug("Local SOPS fallback decrypt failed: %s", exc)
        return None
    return result.stdout


def read_local_fallback(service: str) -> str | None:
    """Look up one secret from the local SOPS+age encrypted file.

    Returns None (never raises) if the tooling isn't available, the file
    doesn't exist, or the key isn't present in it.
    """
    if not _available() or not _SOPS_FILE.exists():
        return None

    plaintext = _decrypt(_SOPS_FILE)
    if plaintext is None:
        return None

    key = _to_env_key(service)
    prefix = f"{key}="
    for line in plaintext.splitlines():
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value or None
    return None


def write_local_fallback(service: str, value: str) -> bool:
    """Persist one secret into the local SOPS+age encrypted file.

    Decrypts the existing file (if any), updates/adds the one key, and
    re-encrypts atomically. Returns False (never raises) if the tooling
    isn't available or the write fails for any reason.
    """
    if not _available():
        return False

    key = _to_env_key(service)
    prefix = f"{key}="

    lines: list[str] = []
    if _SOPS_FILE.exists():
        plaintext = _decrypt(_SOPS_FILE)
        if plaintext is None:
            # Existing file couldn't be decrypted (wrong key, corrupted,
            # transient sops failure) — refuse to overwrite it blindly.
            return False
        lines = [line for line in plaintext.splitlines() if line]

    found = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix}{value}"
            found = True
            break
    if not found:
        lines.append(f"{prefix}{value}")

    plaintext_out = "\n".join(lines) + "\n"

    # Encrypt via a temp file whose name ends in ".env.local.sops" so it
    # matches .sops.yaml's path_regex rule (sops selects recipients by
    # matching the file PATH, not by content).
    tmp_plain_fd, tmp_plain_path_str = tempfile.mkstemp(
        suffix=".env.local.sops", dir=str(_REPO_ROOT)
    )
    tmp_plain_path = Path(tmp_plain_path_str)
    try:
        with os.fdopen(tmp_plain_fd, "w", encoding="utf-8") as f:
            f.write(plaintext_out)

        try:
            result = subprocess.run(
                [
                    "sops",
                    "-e",
                    "--input-type",
                    "dotenv",
                    "--output-type",
                    "dotenv",
                    str(tmp_plain_path),
                ],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
                check=True,
            )
        except Exception as exc:
            logger.debug("Local SOPS fallback encrypt failed for %s: %s", service, exc)
            return False

        # Write the encrypted output to a second temp file (same directory,
        # matching suffix) then atomically replace the real target — the
        # real .env.local.sops is never left holding plaintext.
        tmp_enc_fd, tmp_enc_path_str = tempfile.mkstemp(
            suffix=".env.local.sops", dir=str(_REPO_ROOT)
        )
        tmp_enc_path = Path(tmp_enc_path_str)
        try:
            with os.fdopen(tmp_enc_fd, "w", encoding="utf-8") as f:
                f.write(result.stdout)
            os.replace(tmp_enc_path, _SOPS_FILE)
        except Exception as exc:
            logger.debug("Local SOPS fallback atomic write failed for %s: %s", service, exc)
            tmp_enc_path.unlink(missing_ok=True)
            return False
    finally:
        tmp_plain_path.unlink(missing_ok=True)

    return True
