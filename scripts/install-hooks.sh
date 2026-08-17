#!/usr/bin/env bash
# scripts/install-hooks.sh — instaluje lokalne git hooks dla projektu
# Uruchom raz po sklonowaniu repo: bash scripts/install-hooks.sh
set -euo pipefail

HOOKS_DIR="$(git rev-parse --git-dir)/hooks"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cat > "$HOOKS_DIR/pre-push" << EOF
#!/usr/bin/env bash
# git hook: pre-push — auto-installed by scripts/install-hooks.sh
#
# Pre-push runs ruff + security + frontend (<1 min). Pyright and pytest run in CI
# (develop-gate.yml / _quality-gate.yml), which is the authoritative gate.
# Run \`bash scripts/check.sh\` for the full suite locally.
set -uo pipefail

SCRIPT_DIR="$SCRIPT_DIR"
ZERO='0000000000000000000000000000000000000000'
RANGE=""
BLOCKED=""
HAS_CODE=""

# git feeds "<local_ref> <local_sha> <remote_ref> <remote_sha>" per ref on stdin.
while read -r _local_ref local_sha remote_ref remote_sha; do
  [[ "\$local_sha" == "\$ZERO" ]] && continue   # branch deletion — nothing to check

  if [[ "\$remote_sha" == "\$ZERO" ]]; then
    # New remote branch: diff against develop rather than the whole history.
    base=\$(git merge-base origin/develop "\$local_sha" 2>/dev/null || true)
    RANGE=\$([[ -n "\$base" ]] && echo "\$base..\$local_sha" || echo "")
  else
    RANGE="\$remote_sha..\$local_sha"
  fi

  if ! "\$SCRIPT_DIR/docs-only.sh" \${RANGE:+"\$RANGE"} >/dev/null 2>&1; then
    HAS_CODE=1
    # develop allows direct pushes (repo-admin bypass on the protect-develop-lite
    # ruleset) but only for documentation. GitHub rulesets cannot be conditioned
    # on changed paths, so that half of the rule is enforced here.
    [[ "\$remote_ref" == "refs/heads/develop" ]] && BLOCKED=1
  fi
done

if [[ -n "\$BLOCKED" ]]; then
  echo ""
  echo "✗ Bezpośredni push na develop jest dozwolony tylko dla dokumentacji (.md / .pdf)."
  echo ""
  echo "  Zmienione pliki spoza dokumentacji:"
  "\$SCRIPT_DIR/docs-only.sh" \${RANGE:+"\$RANGE"} 2>/dev/null | grep -vE '\.(md|pdf)\$' | sed 's/^/    - /'
  echo ""
  echo "  Kod idzie przez PR:"
  echo "    git switch -c <branch> && git push -u origin <branch> && gh pr create --base develop"
  echo ""
  echo "  Świadomie omijasz? git push --no-verify"
  echo ""
  exit 1
fi

# Hand the exact pushed range to check.sh so its docs fast path does not have to
# guess from @{upstream}.
export CHECK_RANGE="\$RANGE"
CHECK_TYPECHECK=0 CHECK_TESTS=0 "\$SCRIPT_DIR/check.sh" || exit 1

# Validate staging CI permissions if az is logged in (prevents wasted CI runs).
# Pointless for a docs-only push — staging never runs for it either way.
if [[ -n "\$HAS_CODE" ]] && az account show &>/dev/null 2>&1; then
  "\$SCRIPT_DIR/validate-staging-ci.sh" || exit 1
fi
EOF
chmod +x "$HOOKS_DIR/pre-push"

echo "✓ pre-push → scripts/check.sh + develop docs-only guard"
echo ""
echo "Hooks zainstalowane:"
echo "  pre-push → strażnik develop + scripts/check.sh + validate-staging-ci.sh (jeśli az zalogowany)"
echo "Aby pominąć jednorazowo: git push --no-verify"
