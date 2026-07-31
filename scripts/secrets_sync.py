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
  encrypt  Encrypt .env.local -> .env.local.sops (bootstrapping a new
           machine from a git-committed encrypted snapshot). Merges by
           default: keys that exist only in the snapshot — rotated secrets
           written there by set_secret() — survive. --replace overwrites.
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

from scripts.secrets_manifest import ENV_LOCAL_SECRETS, SOPS_ONLY_SECRETS

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


def _sops_only_env_keys() -> set[str]:
    """SOPS_ONLY_SECRETS as .env.local variable names."""
    return {_to_env_key(name) for name in SOPS_ONLY_SECRETS}


def _strip_sops_only(lines: list[str]) -> tuple[list[str], list[str]]:
    """Drop KEY=value lines for secrets that must stay out of .env.local.

    Returns (kept_lines, dropped_keys). Comments and unrelated lines pass
    through untouched.
    """
    blocked = _sops_only_env_keys()
    kept: list[str] = []
    dropped: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in blocked:
                dropped.append(key)
                continue
        kept.append(line)
    return kept, dropped


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
        # Same reason as in cmd_decrypt: a rotating secret must never land in
        # the plaintext file, or the env var would shadow the SOPS tier.
        if name in SOPS_ONLY_SECRETS:
            continue
        value = get_keyvault_secret(vault_url, name)
        if value:
            found[_to_env_key(name)] = value
        else:
            missing.append(name)

    lines, _ = _strip_sops_only(_read_lines(ENV_LOCAL_PATH))
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


def _decrypt_sops_file() -> str | None:
    """Decrypt .env.local.sops, or None if it can't be read."""
    try:
        result = subprocess.run(
            ["sops", "-d", "--input-type", "dotenv", "--output-type", "dotenv", str(SOPS_PATH)],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=True,
        )
    except Exception:
        return None
    return result.stdout


def _keys_only_in_snapshot(plaintext: str) -> dict[str, str]:
    """Keys the encrypted snapshot has that .env.local doesn't.

    These are values written straight into .env.local.sops by
    zdrovena.common.secrets.set_secret() as secrets rotated — most
    importantly the Allegro refresh token, which rotates on every use and
    deliberately no longer lives in .env.local (an env var there would
    shadow the whole SOPS tier; see docs/devops/sops-age.md §3).
    """
    snapshot = _parse_env_map(plaintext.splitlines())
    local = _parse_env_map(_read_lines(ENV_LOCAL_PATH))
    return {k: v for k, v in snapshot.items() if k not in local}


def cmd_encrypt(args: argparse.Namespace) -> int:
    if shutil.which("sops") is None:
        print("error: `sops` binary not found on PATH", file=sys.stderr)
        return 1
    if not ENV_LOCAL_PATH.exists():
        print(f"error: {ENV_LOCAL_PATH} does not exist — nothing to encrypt", file=sys.stderr)
        return 1

    # sops selects its creation_rules by matching the INPUT file path
    # against each rule's path_regex — NOT by --output/--output-type.
    # .sops.yaml's rule only matches paths ending in ".env.local.sops", so
    # running sops directly on ".env.local" fails with "no matching
    # creation rules found". Route the plaintext through a temp file whose
    # name ends in ".env.local.sops" instead — same trick
    # zdrovena.common._local_secret_fallback.write_local_fallback already
    # uses for its encrypt step.
    plaintext = ENV_LOCAL_PATH.read_text(encoding="utf-8")

    # Default is a MERGE, not a plain overwrite: .env.local.sops is not only a
    # snapshot of .env.local, it is also the store set_secret() writes rotated
    # secrets into. A whole-file overwrite would silently delete any key that
    # lives only there. --replace opts back into the destructive behaviour,
    # which is the only way to actually remove a key from the snapshot.
    preserved: dict[str, str] = {}
    if not getattr(args, "replace", False) and SOPS_PATH.exists():
        snapshot_plaintext = _decrypt_sops_file()
        if snapshot_plaintext is None:
            print(
                f"error: {SOPS_PATH.name} exists but could not be decrypted — refusing to\n"
                "       overwrite it and lose secrets that live only there. Fix your age key,\n"
                "       or pass --replace to overwrite it deliberately.",
                file=sys.stderr,
            )
            return 1
        preserved = _keys_only_in_snapshot(snapshot_plaintext)
        if preserved:
            if not plaintext.endswith("\n"):
                plaintext += "\n"
            plaintext += (
                "\n# Preserved from .env.local.sops — written by set_secret() on rotation,\n"
            )
            plaintext += "# deliberately not present in .env.local (see docs/devops/sops-age.md).\n"
            plaintext += "".join(f"{k}={v}\n" for k, v in sorted(preserved.items()))

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        suffix=".env.local.sops", dir=str(ENV_LOCAL_PATH.parent)
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(plaintext)

        try:
            result = subprocess.run(
                ["sops", "-e", "--input-type", "dotenv", "--output-type", "dotenv", str(tmp_path)],
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
            # FileNotFoundError (binary vanished after the shutil.which
            # check), and any other OSError — none of these are
            # CalledProcessError subclasses, so without this they'd
            # propagate as raw tracebacks.
            print(f"error: sops encrypt failed: {exc}", file=sys.stderr)
            return 1
    finally:
        tmp_path.unlink(missing_ok=True)

    _atomic_write_text(SOPS_PATH, result.stdout)
    print(f"encrypted {ENV_LOCAL_PATH} -> {SOPS_PATH}")
    if preserved:
        print(f"  preserved {len(preserved)} key(s) that live only in the snapshot:")
        for key in sorted(preserved):
            print(f"    - {key}")
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

    # Rotating secrets stay out of the plaintext file — writing them back
    # would recreate the env-var shadowing this whole tier exists to avoid.
    kept, dropped = _strip_sops_only(result.stdout.splitlines())
    _write_lines(ENV_LOCAL_PATH, kept)

    print(f"decrypted {SOPS_PATH} -> {ENV_LOCAL_PATH}")
    if dropped:
        print(f"  kept {len(dropped)} rotating secret(s) out of the plaintext file:")
        for key in sorted(set(dropped)):
            print(f"    - {key}  (read directly from {SOPS_PATH.name})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync zdrovena secrets between Key Vault, .env.local, and .env.local.sops"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pull", help="Pull secrets from Key Vault into .env.local")
    sub.add_parser("push", help="Push .env.local secret values up to Key Vault")
    encrypt_parser = sub.add_parser(
        "encrypt", help="Encrypt .env.local -> .env.local.sops (merges, see --replace)"
    )
    encrypt_parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Overwrite .env.local.sops entirely instead of preserving keys that exist "
            "only there (rotated secrets written by set_secret). This is how you REMOVE "
            "a key from the snapshot."
        ),
    )
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
