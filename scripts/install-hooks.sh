#!/usr/bin/env bash
# scripts/install-hooks.sh — instaluje lokalne git hooks dla projektu
# Uruchom raz po sklonowaniu repo: bash scripts/install-hooks.sh
set -euo pipefail

HOOKS_DIR="$(git rev-parse --git-dir)/hooks"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

install_hook() {
  local name="$1"
  local target="$HOOKS_DIR/$name"
  cat > "$target" << EOF
#!/usr/bin/env bash
# git hook: $name — auto-installed by scripts/install-hooks.sh
exec "$SCRIPT_DIR/check.sh"
EOF
  chmod +x "$target"
  echo "✓ $name → scripts/check.sh"
}

install_hook "pre-push"

echo ""
echo "Hook zainstalowany. Przy każdym 'git push' uruchomi scripts/check.sh."
echo "Aby pominąć jednorazowo: git push --no-verify"
