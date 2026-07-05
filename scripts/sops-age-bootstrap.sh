#!/usr/bin/env bash
set -euo pipefail

env_name="${1:-dev}"
repo_root="$(git rev-parse --show-toplevel)"
key_file="$repo_root/age-$env_name.agekey"

if ! command -v age-keygen >/dev/null 2>&1; then
  echo "age-keygen is required. Install age first." >&2
  exit 1
fi

if [[ -e "$key_file" ]]; then
  echo "Refusing to overwrite existing key: $key_file" >&2
  exit 1
fi

age-keygen -o "$key_file"
chmod 600 "$key_file"

public_key="$(age-keygen -y "$key_file")"

mkdir -p "$HOME/.config/sops/age"
if [[ ! -e "$HOME/.config/sops/age/keys.txt" ]]; then
  cp "$key_file" "$HOME/.config/sops/age/keys.txt"
  chmod 600 "$HOME/.config/sops/age/keys.txt"
else
  echo "Local SOPS key file already exists: $HOME/.config/sops/age/keys.txt"
  echo "Append $key_file manually if this key should be usable on this workstation."
fi

cat <<EOF

Generated private key: $key_file
Public key for .sops.yaml:
$public_key

Store the private key in the team secret manager, then remove the local repo copy
after copying it to ~/.config/sops/age/keys.txt or another secure location.
EOF
