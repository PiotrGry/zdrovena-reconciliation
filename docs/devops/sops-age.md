# SOPS + age — local secrets fallback for `.env.local`

This project runs on Azure Container Apps. There is no Kubernetes anywhere in
this repo — no `clusters/` directory, no Flux, no ArgoCD, no GitOps
controller. SOPS + age here has nothing to do with encrypting Kubernetes
Secret manifests; it backs a single encrypted dotenv file,
`.env.local.sops`, that holds a local, git-portable copy of the shipping/API
service's secrets (Allegro, InPost, Apaczka, Shopify, SMS).

> Note: the separate `.env` file (used by the month-closing CLI —
> Fakturownia/Zoho/KSeF/Google Ads) has its own, already-working
> live-Key-Vault-fetch pattern via `.env.template` + `az login`. That is
> untouched by anything on this page.

## 1. Overview

**The problem this solves:** an Allegro OAuth refresh token rotated while a
developer was working in a sandbox with no Azure connectivity. Without a
persistence tier that works offline, `set_secret()` had nowhere to put the
new token except an in-process environment variable — gone the moment the
process exited. The next run would fail with a stale/invalid refresh token,
and there was no record of the rotation anywhere.

The fix is a local, SOPS+age-encrypted fallback tier backed by one file at
the repo root, `.env.local.sops`. It is used in two independent,
complementary ways:

- **(A) Automatic, transparent, per-secret fallback** — built into
  `get_secret()` / `set_secret()`. Zero explicit developer action. Activates
  whenever the `sops`/`age` binaries and a local age private key are present;
  otherwise it's a silent no-op. See [§3](#3-automatic-fallback-tier).
- **(B) Manual, explicit, whole-file operations** via
  `scripts/secrets_sync.py` — bootstrapping a new machine, taking a snapshot
  before going offline, or reconciling with Azure Key Vault. See
  [§4](#4-the-secrets_syncpy-cli).

Both share the same encrypted file and the same age key, but you don't need
to think about (A) at all day to day — it just prevents rotated secrets from
being silently lost. (B) is the tool you reach for deliberately.

**Key design point:** this is a single shared age key across a developer's
own machines, not a multi-environment (dev/staging/prod) setup. One age
keypair; the public key lives in the committed `.sops.yaml`; the private key
is placed manually, out of band, at `~/.config/sops/age/keys.txt` on every
machine you use.

## 2. One-time setup

### Install `sops` and `age`

```bash
brew install sops age
```

or on Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y age
SOPS_VER=3.9.4
curl -Lo sops "https://github.com/getsops/sops/releases/download/v${SOPS_VER}/sops-v${SOPS_VER}.linux.amd64"
sudo install -m 0755 sops /usr/local/bin/sops
```

### Generate the shared keypair

Run once, on your first machine:

```bash
scripts/sops-age-bootstrap.sh <label>
```

`<label>` is just a filename label (there's no dev/staging/prod distinction
in this design) — your name or `local` both work fine, e.g.
`scripts/sops-age-bootstrap.sh piotr`. The script:

- generates an age keypair at `age-<label>.agekey` in the repo root
  (gitignored — never commit it);
- copies the private key to `~/.config/sops/age/keys.txt`, unless that file
  already exists, in which case it prints a message telling you to merge the
  key in manually instead of overwriting it;
- prints the public key (`age1...`).

Example output:

```
Public key: age1nwmzu2asmj6az06prpun0eas7494hujrzlvj9szp65rne9u7qa2q787eqh

Generated private key: /path/to/repo/age-piotr.agekey
Public key for .sops.yaml:
age1nwmzu2asmj6az06prpun0eas7494hujrzlvj9szp65rne9u7qa2q787eqh

Store the private key in the team secret manager, then remove the local repo copy
after copying it to ~/.config/sops/age/keys.txt or another secure location.
```

### Configure the repo

```bash
cp .sops.yaml.example .sops.yaml
```

Replace the placeholder with the real public key from the previous step:

```yaml
creation_rules:
  - path_regex: \.env\.local\.sops$
    age: age1nwmzu2asmj6az06prpun0eas7494hujrzlvj9szp65rne9u7qa2q787eqh
```

`.sops.yaml` (public key only) is committed. There is intentionally no
`encrypted_regex` / `mac_only_encrypted` — those are partial-encryption knobs
for Kubernetes Secret YAML and don't apply to whole-file dotenv encryption.

### Get the private key onto a second/third machine

The private key never travels through Git. Copy
`~/.config/sops/age/keys.txt` (or the raw `AGE-SECRET-KEY-...` line) to the
new machine out of band — a password manager entry or a direct secure copy —
and place it at `~/.config/sops/age/keys.txt` there too:

```bash
mkdir -p ~/.config/sops/age
chmod 600 ~/.config/sops/age/keys.txt   # after copying the file into place
```

Once that's done, decrypt the committed snapshot to get a working
`.env.local` (see [§4](#4-the-secrets_syncpy-cli), `decrypt`).

## 3. Automatic fallback tier

`get_secret()` and `set_secret()` in
[`zdrovena/common/secrets.py`](../../zdrovena/common/secrets.py) resolve
through this priority chain:

**`get_secret(service)`** — first non-empty value wins:
1. Environment variable (`SERVICE_NAME` uppercased, `-` → `_`)
2. Local SOPS+age fallback — `.env.local.sops`
3. Azure Key Vault (when `AZURE_KEYVAULT_URL` is set)

**`set_secret(service, value)`** — most persistent store wins, tries in
order and returns `True` if any tier accepted the write:
1. Azure Key Vault (when `AZURE_KEYVAULT_URL` is set)
2. Local SOPS+age fallback — `.env.local.sops`
3. In-process environment variable only, with a loud warning that the value
   will be lost on restart (this is the "everything failed" last resort)

The local fallback tier lives in
[`zdrovena/common/_local_secret_fallback.py`](../../zdrovena/common/_local_secret_fallback.py).
It activates only when **both** conditions hold:

- the `sops` binary is on `PATH`, and
- `~/.config/sops/age/keys.txt` exists.

If either is missing, every function in that module returns `None`/`False`
immediately — never raises, never blocks. An environment with neither `sops`
installed nor an age key configured behaves exactly as if this tier didn't
exist (this is also why CI and most contributors' machines are unaffected:
Key Vault and/or plain env vars satisfy `get_secret`/`set_secret` before this
tier is ever reached).

This tier replaced an OS-keyring-backed tier: keyring behaves inconsistently
across the multiple operating systems this project is developed from (macOS
Keychain vs. Linux Secret Service vs. Windows Credential Manager — the
latter two often unavailable on headless/sandboxed Linux boxes). SOPS+age
behaves identically on every OS, and the encrypted file is git-portable.

**The Allegro incident, concretely:** a developer is working in a sandbox
with no route to Azure Key Vault. The Allegro OAuth refresh token rotates
mid-session. `set_secret("allegro-refresh-token", new_value)` tries Key
Vault first — unreachable, tier 1 fails — then falls through to the local
SOPS+age tier. If `sops`/`age`/the local key are present, the new token is
written into `.env.local.sops` (encrypted, atomically) and survives process
restarts and even a fresh `git clone` on the same machine. Without this
tier, the value would only have landed in `os.environ` and been lost the
moment the process exited.

Internally, writes always go through a temp file whose name ends in
`.env.local.sops` (matching `.sops.yaml`'s `path_regex` — sops selects
recipients by matching the input file *path*, not by content or
`--output`), then are moved into place atomically, so a crash mid-write
can't corrupt the real file or leave it holding plaintext.

## 4. The `secrets_sync.py` CLI

[`scripts/secrets_sync.py`](../../scripts/secrets_sync.py) covers the bulk /
bootstrapping operations that the per-secret automatic tier in §3 doesn't:

```bash
uv run python scripts/secrets_sync.py pull      # Key Vault -> .env.local
uv run python scripts/secrets_sync.py push      # .env.local -> Key Vault
uv run python scripts/secrets_sync.py encrypt   # .env.local -> .env.local.sops
uv run python scripts/secrets_sync.py decrypt   # .env.local.sops -> .env.local
```

The set of secrets `pull`/`push` operate on is the canonical list in
[`scripts/secrets_manifest.py`](../../scripts/secrets_manifest.py)'s
`ENV_LOCAL_SECRETS` (~20 names: Allegro, Shopify, InPost, Apaczka, SMS, and
sender-address secrets).

| Subcommand | Needs Key Vault? | Use it when... |
|---|---|---|
| `decrypt` | No | Bootstrapping a brand-new machine — get a working `.env.local` from the git-committed `.env.local.sops` snapshot. **Overwrites** `.env.local` if it already exists. |
| `encrypt` | No | You've changed `.env.local` and want to take a fresh snapshot for git (e.g. before going somewhere with no Key Vault access, like a flight or a sandboxed environment). |
| `pull` | Yes (`AZURE_KEYVAULT_URL` + `az login`) | You have live Key Vault access again and want the freshest values written into your local `.env.local`. |
| `push` | Yes (`AZURE_KEYVAULT_URL` + `az login`) | You want to persist local changes/rotations back to Key Vault, or backfill Key Vault for the first time. |

`encrypt`/`decrypt` never touch Key Vault and don't require
`AZURE_KEYVAULT_URL` — they only need `sops` on `PATH` and (for `decrypt`) a
working local age private key.

`push` doubles as the Key Vault backfill mechanism: as of this writing, none
of the ~20 `ENV_LOCAL_SECRETS` exist in Key Vault yet (see the "Sekret AKV"
status table in `TODOS.md`), so the first real `push` run performs that
migration as a side effect — every secret with a value in `.env.local` gets
uploaded, whether or not it existed in Key Vault before.

Sample `pull` output (some secrets not yet backfilled):

```
pull: 12 found in Key Vault, 8 missing
  missing (expected until backfilled via TODOS.md / `push`):
    - apaczka-app-id
    - apaczka-app-secret
    ...
wrote /path/to/repo/.env.local
```

Sample `push` output:

```
push: 15 pushed to Key Vault (includes any first-time backfills)
      5 skipped (no value in .env.local)
```

Both `pull` and `push` require `AZURE_KEYVAULT_URL` to be set; without it
they print `error: AZURE_KEYVAULT_URL is not set — required for pull/push`
and exit non-zero.

## 5. What gets committed vs. never committed

| Committed | Never committed |
|---|---|
| `.sops.yaml` (public key only) | `.env.local` (plaintext — already gitignored) |
| `.env.local.sops` (SOPS+age-encrypted; safe because it's encrypted) | `*.agekey` files (e.g. `age-piotr.agekey`) |
| `scripts/secrets_manifest.py` | `~/.config/sops/age/keys.txt` |
| `scripts/secrets_sync.py`, `scripts/sops-age-bootstrap.sh`, `scripts/check-sops-secrets.sh` | Any literal `AGE-SECRET-KEY-...` text, anywhere |

`.sops.yaml.example` (with a placeholder public key) is also committed as
the template contributors copy from.

## 6. Pre-commit / CI validation

[`scripts/check-sops-secrets.sh`](../../scripts/check-sops-secrets.sh) is
wired in as:

- the `sops-age-secrets` pre-commit hook (`.pre-commit-config.yaml`)
- the `sops-guard` job in `.github/workflows/_quality-gate.yml`

It checks (over `git ls-files`, so it only ever looks at tracked content):

1. **No tracked private key files** — blocks filenames matching
   `*.agekey`, `*.agekey.txt`, `age*.key`, or `keys.txt`.
2. **No tracked private key material** — blocks any tracked file containing
   text matching `AGE-SECRET-KEY-1[A-Z0-9]{58}`.
3. **Genuine SOPS-encrypted `.env.local.sops`** — if `.env.local.sops` is
   tracked, it must contain `ENC[`, a `sops_version=` line, and a
   `sops_mac=ENC[` line. These three markers only appear in real `sops`
   dotenv output, so this distinguishes a genuinely encrypted file from a
   plaintext file or a hand-typed `ENC[` placeholder. If `.env.local.sops`
   isn't tracked yet, this check is a no-op.

(A separate check in the same script also validates any tracked
`*.sops.yaml`/`*.sops.json` files for a `sops:` block and `ENC[` — a
carry-over from before this repo dropped its Kubernetes-Secret-YAML use
case. It doesn't apply to `.env.local.sops`, which is dotenv-formatted and
has no top-level `sops:` block.)

Run it manually at any time:

```bash
scripts/check-sops-secrets.sh
```

## 7. Rotating the shared age key itself

This is different from rotating an individual secret's *value* (§3 handles
that transparently). Rotating the *key* means replacing the age keypair
everyone uses to encrypt/decrypt `.env.local.sops`:

1. Generate a new keypair (pick a fresh label so you don't collide with the
   old `age-<label>.agekey` file):
   ```bash
   scripts/sops-age-bootstrap.sh <new-label>
   ```
2. Update `.sops.yaml`'s `age:` value to the new public key.
3. Re-encrypt `.env.local.sops` for the new recipient. Either:
   ```bash
   sops updatekeys -y --input-type dotenv .env.local.sops
   ```
   (the `--input-type dotenv` flag is required — without it `sops` can't
   guess the format from the `.env.local.sops` filename and fails), or, more
   simply, decrypt with the old key and re-encrypt with the new one:
   ```bash
   uv run python scripts/secrets_sync.py decrypt   # while the OLD key is still at ~/.config/sops/age/keys.txt
   # swap in the new private key, update .sops.yaml as in step 2
   uv run python scripts/secrets_sync.py encrypt   # now encrypts for the NEW key
   ```
4. Redistribute the new private key to every machine that needs it (same
   out-of-band process as initial setup — never via Git).
5. Remove the old private key everywhere, including any lingering
   `age-<old-label>.agekey` file and the old entry in
   `~/.config/sops/age/keys.txt`. After this, `sops -d` with the old key
   fails with `age: no identity matched any of the recipients`, confirming
   the rotation took effect.

## 8. Troubleshooting

**`error loading config: no matching creation rules found`**
`.sops.yaml` is missing, or its `path_regex` doesn't match the path you
passed to `sops`. Important: sops matches `creation_rules` against the
*input* file path you give it as an argument — not `--output`. Running
`sops -e -i .env.local` fails for exactly this reason, because
`.sops.yaml`'s rule only matches paths ending in `.env.local.sops`. This is
why both `_local_secret_fallback.py` and `secrets_sync.py`'s `encrypt`
command route plaintext through a temp file whose name ends in
`.env.local.sops` before invoking `sops -e`.

**Permission errors on `~/.config/sops/age/keys.txt`**
It should be `chmod 600` (readable/writable by you only):
```bash
chmod 600 ~/.config/sops/age/keys.txt
```

**How do I check whether the automatic fallback tier (§3) is even active?**
```bash
python -c "
import shutil
from pathlib import Path
print('sops on PATH:', bool(shutil.which('sops')))
print('age key present:', (Path.home()/'.config/sops/age/keys.txt').exists())
"
```
Both must be `True` for `_local_secret_fallback.py` to do anything; if
either is `False`, `get_secret`/`set_secret` silently skip this tier.
