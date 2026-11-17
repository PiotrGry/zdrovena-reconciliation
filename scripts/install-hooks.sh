#!/usr/bin/env bash
# scripts/install-hooks.sh — instaluje lokalne git hooks dla projektu
# Uruchom raz po sklonowaniu repo: bash scripts/install-hooks.sh
set -euo pipefail

# --git-path: the shared hooks dir, also when run from a linked worktree.
HOOKS_DIR="$(git rev-parse --git-path hooks)"

cat > "$HOOKS_DIR/pre-push" << EOF
#!/usr/bin/env bash
# git hook: pre-push — auto-installed by scripts/install-hooks.sh
#
# Pre-push runs ruff + security + frontend (<1 min). Pyright and pytest run in CI
# (develop-gate.yml / _quality-gate.yml), which is the authoritative gate.
# Run \`bash scripts/check.sh\` for the full suite locally.
set -uo pipefail

# Resolved at push time: a linked worktree (e.g. a Pirx task) checks its own
# scripts and .venv, not the checkout that installed the hook.
SCRIPT_DIR="\$(git rev-parse --show-toplevel)/scripts"
ZERO='0000000000000000000000000000000000000000'
RANGE=""
BLOCKED=""
HAS_CODE=""

# git feeds "<local_ref> <local_sha> <remote_ref> <remote_sha>" per ref on stdin.
while read -r _local_ref local_sha remote_ref remote_sha; do
  [[ "\$local_sha" == "\$ZERO" ]] && continue   # branch deletion — nothing to check

  # A branch is gated for what its PR adds to develop. remote..local would also
  # hold every develop commit a "merge develop in" brought along: code develop's
  # own gate already passed, re-checked here against this machine's environment.
  base=\$(git merge-base origin/develop "\$local_sha" 2>/dev/null || true)
  if [[ "\$remote_ref" != "refs/heads/develop" && -n "\$base" ]]; then
    RANGE="\$base..\$local_sha"
  elif [[ "\$remote_sha" != "\$ZERO" ]]; then
    RANGE="\$remote_sha..\$local_sha"
  else
    RANGE=""
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
