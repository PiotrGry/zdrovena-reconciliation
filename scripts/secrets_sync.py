#!/usr/bin/env python3
"""Sync zdrovena secrets between Azure Key Vault, .env.local, and .env.local.sops.

Companion CLI to the local SOPS+age fallback tier in
zdrovena.common._local_secret_fallback: that tier persists secrets
one key at a time as they rotate through get_secret()/set_secret(). This
script covers the bulk / bootstrapping operations on top of it:

  pull     Fetch every secret in scripts.secrets_manifest.ENV_LOCAL_SECRETS
           from Key Vault and write/update it in .env.local.
  push     Read .env.local and upload every secret it has a value for to
           Key Vault (also backfills secrets never uploaded before).
  encrypt  Whole-file encrypt .env.local -> .env.local.sops (bootstrapping
           a new machine from a git-committed encrypted snapshot).
  decrypt  Whole-file decrypt .env.local.sops -> .env.local (OVERWRITES
           .env.local if it exists).

Usage:
    uv run python scripts/secrets_sync.py pull
    uv run python scripts/secrets_sync.py push
    uv run python scripts/secrets_sync.py encrypt
    uv run python scripts/secrets_sync.py decrypt

Env:
    AZURE_KEYVAULT_URL   -> required for pull/push (not needed for
                            encrypt/decrypt, which never touch Key Vault)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Allow importing zdrovena/scripts without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.secrets_manifest import ENV_LOCAL_SECRETS

ROOT = Path(__file__).resolve().parents[1]
ENV_LOCAL_PATH = ROOT / ".env.local"
SOPS_PATH = ROOT / ".env.local.sops"
_SUBPROCESS_TIMEOUT = 30


def _to_env_key(name: str) -> str:
    """Key Vault secret name -> .env.local var name.

    Exact same transform as zdrovena.common.secrets.get_secret/set_secret
    (service.upper().replace("-", "_")) — must round-trip identically so
    a value pulled here and later read via get_secret() matches.
    """
    return name.upper().replace("-", "_")


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically via temp file + os.replace.

    Matches the pattern in zdrovena.common._local_secret_fallback.
    write_local_fallback — the temp file is created in the same directory
    as the target (required for os.replace to be atomic across
    filesystems) so a crash mid-write never leaves the target holding a
    partial/corrupted file.
    """
    fd, tmp_path_str = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _write_lines(path: Path, lines: list[str]) -> None:
    content = "\n".join(lines)
    if content:
        content += "\n"
    _atomic_write_text(path, content)


def _parse_env_map(lines: list[str]) -> dict[str, str]:
    """Parse simple KEY=value lines, ignoring blanks/comments."""
    env: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.strip()
    return env


def _apply_updates(lines: list[str], updates: dict[str, str]) -> list[str]:
    """Replace matching KEY=... lines in place; append the rest.

    Only active (uncommented) `KEY=value` lines are treated as existing —
    commented-out template lines (e.g. "# SHOPIFY_ACCESS_TOKEN=") are left
    untouched, and a new active line is appended instead. Every other
    existing line (comments, unrelated keys) is preserved verbatim.
    """
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    return out


def _require_vault_url() -> str | None:
    vault_url = os.environ.get("AZURE_KEYVAULT_URL")
    if not vault_url:
        print(
            "error: AZURE_KEYVAULT_URL is not set — required for pull/push",
            file=sys.stderr,
        )
        return None
    return vault_url


def cmd_pull(_args: argparse.Namespace) -> int:
    vault_url = _require_vault_url()
    if vault_url is None:
        return 1

    from zdrovena.common._keyvault import get_keyvault_secret

    found: dict[str, str] = {}
    missing: list[str] = []
    for name in ENV_LOCAL_SECRETS:
        value = get_keyvault_secret(vault_url, name)
        if value:
            found[_to_env_key(name)] = value
        else:
            missing.append(name)

    lines = _read_lines(ENV_LOCAL_PATH)
    _write_lines(ENV_LOCAL_PATH, _apply_updates(lines, found))

    print(f"pull: {len(found)} found in Key Vault, {len(missing)} missing")
    if missing:
        print("  missing (expected until backfilled via TODOS.md / `push`):")
        for name in missing:
            print(f"    - {name}")
    print(f"wrote {ENV_LOCAL_PATH}")
    return 0


def cmd_push(_args: argparse.Namespace) -> int:
    vault_url = _require_vault_url()
    if vault_url is None:
        return 1

    from zdrovena.common._keyvault import set_keyvault_secret

    env_map = _parse_env_map(_read_lines(ENV_LOCAL_PATH))

    pushed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for name in ENV_LOCAL_SECRETS:
        value = env_map.get(_to_env_key(name))
        if not value:
            skipped.append(name)
            continue
        if set_keyvault_secret(vault_url, name, value):
            pushed.append(name)
        else:
            failed.append(name)

    print(f"push: {len(pushed)} pushed to Key Vault (includes any first-time backfills)")
    print(f"      {len(skipped)} skipped (no value in {ENV_LOCAL_PATH.name})")
    if failed:
        print(f"      {len(failed)} FAILED:")
        for name in failed:
            print(f"        - {name}")
        return 1
    return 0


def cmd_encrypt(_args: argparse.Namespace) -> int:
    if shutil.which("sops") is None:
        print("error: `sops` binary not found on PATH", file=sys.stderr)
        return 1
    if not ENV_LOCAL_PATH.exists():
        print(f"error: {ENV_LOCAL_PATH} does not exist — nothing to encrypt", file=sys.stderr)
        return 1

    try:
        result = subprocess.run(
            [
                "sops",
                "-e",
                "--input-type",
                "dotenv",
                "--output-type",
                "dotenv",
                str(ENV_LOCAL_PATH),
            ],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"error: sops encrypt failed: {exc.stderr}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Covers subprocess.TimeoutExpired (sops hung past the timeout),
        # FileNotFoundError (binary vanished after the shutil.which check),
        # and any other OSError — none of these are CalledProcessError
        # subclasses, so without this they'd propagate as raw tracebacks.
        print(f"error: sops encrypt failed: {exc}", file=sys.stderr)
        return 1

    _atomic_write_text(SOPS_PATH, result.stdout)
    print(f"encrypted {ENV_LOCAL_PATH} -> {SOPS_PATH}")
    return 0


def cmd_decrypt(_args: argparse.Namespace) -> int:
    if shutil.which("sops") is None:
        print("error: `sops` binary not found on PATH", file=sys.stderr)
        return 1
    if not SOPS_PATH.exists():
        print(f"error: {SOPS_PATH} does not exist — nothing to decrypt", file=sys.stderr)
        return 1

    if ENV_LOCAL_PATH.exists():
        print(f"warning: overwriting existing {ENV_LOCAL_PATH}")

    try:
        result = subprocess.run(
            ["sops", "-d", "--input-type", "dotenv", "--output-type", "dotenv", str(SOPS_PATH)],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"error: sops decrypt failed: {exc.stderr}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Covers subprocess.TimeoutExpired, FileNotFoundError, and any
        # other OSError — see the matching comment in cmd_encrypt.
        print(f"error: sops decrypt failed: {exc}", file=sys.stderr)
        return 1

    _atomic_write_text(ENV_LOCAL_PATH, result.stdout)
    print(f"decrypted {SOPS_PATH} -> {ENV_LOCAL_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync zdrovena secrets between Key Vault, .env.local, and .env.local.sops"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pull", help="Pull secrets from Key Vault into .env.local")
    sub.add_parser("push", help="Push .env.local secret values up to Key Vault")
    sub.add_parser("encrypt", help="Whole-file encrypt .env.local -> .env.local.sops")
    sub.add_parser("decrypt", help="Whole-file decrypt .env.local.sops -> .env.local")
    args = parser.parse_args()

    dispatch = {
        "pull": cmd_pull,
        "push": cmd_push,
        "encrypt": cmd_encrypt,
        "decrypt": cmd_decrypt,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
