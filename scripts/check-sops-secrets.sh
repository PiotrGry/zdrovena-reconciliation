#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

fail() {
  printf 'sops check failed: %s\n' "$1" >&2
  exit 1
}

tracked_files="$(git ls-files)"

if printf '%s\n' "$tracked_files" | grep -Eq '(^|/)([^/]*\.agekey|[^/]*\.agekey\.txt|age[^/]*\.key|keys\.txt)$'; then
  printf '%s\n' "$tracked_files" | grep -E '(^|/)([^/]*\.agekey|[^/]*\.agekey\.txt|age[^/]*\.key|keys\.txt)$' >&2
  fail "private age key file is tracked by Git"
fi

if git grep -n -I -E 'AGE-SECRET-KEY-1[A-Z0-9]{58}' -- . >/tmp/sops-age-private-key-matches.txt; then
  cat /tmp/sops-age-private-key-matches.txt >&2
  fail "private age key material was found in tracked files"
fi

status=0
while IFS= read -r file; do
  if ! grep -q '^sops:' "$file"; then
    printf '%s: missing top-level sops metadata\n' "$file" >&2
    status=1
  fi
  if ! grep -q 'ENC\[' "$file"; then
    printf '%s: missing encrypted ENC[...] values\n' "$file" >&2
    status=1
  fi
done < <(printf '%s\n' "$tracked_files" | grep -E '\.sops\.(ya?ml|json)$' || true)

# .env.local.sops is dotenv-format sops output (see
# zdrovena/common/_local_secret_fallback.py), not YAML — it has no
# top-level `sops:` block. Instead, genuine sops dotenv output always ends
# with sops_version=... and sops_mac=ENC[...] lines, which is what we check
# for here to distinguish real encrypted output from a plaintext file or a
# hand-typed "ENC[" placeholder.
while IFS= read -r file; do
  if ! grep -q 'ENC\[' "$file"; then
    printf '%s: missing encrypted ENC[...] values\n' "$file" >&2
    status=1
  fi
  if ! grep -q 'sops_version=' "$file"; then
    printf '%s: missing sops_version= marker (not genuine sops dotenv output)\n' "$file" >&2
    status=1
  fi
  if ! grep -q 'sops_mac=ENC\[' "$file"; then
    printf '%s: missing sops_mac=ENC[...] marker (not genuine sops dotenv output)\n' "$file" >&2
    status=1
  fi
done < <(printf '%s\n' "$tracked_files" | grep -E '(^|/)\.env\.local\.sops$' || true)

if [[ "$status" -ne 0 ]]; then
  fail "one or more sops-managed files are not properly encrypted"
fi

printf 'sops check passed\n'
