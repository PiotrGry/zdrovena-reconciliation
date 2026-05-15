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

# Also run staging CI validation if az is logged in
cat > "$HOOKS_DIR/pre-push" << EOF
#!/usr/bin/env bash
# git hook: pre-push — auto-installed by scripts/install-hooks.sh
"$SCRIPT_DIR/check.sh" || exit 1
# Validate staging CI permissions if az is logged in (prevents wasted CI runs)
if az account show &>/dev/null 2>&1; then
  "$SCRIPT_DIR/validate-staging-ci.sh" || exit 1
fi
EOF
chmod +x "$HOOKS_DIR/pre-push"

echo ""
echo "Hooks zainstalowane:"
echo "  pre-push → scripts/check.sh + scripts/validate-staging-ci.sh (jeśli az zalogowany)"
echo "Aby pominąć jednorazowo: git push --no-verify"
