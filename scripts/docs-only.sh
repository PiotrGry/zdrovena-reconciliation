#!/usr/bin/env bash
# scripts/docs-only.sh — is this change set documentation only?
#
# Single source of truth for that question. `scripts/check.sh` uses it to skip
# the local gate, and the pre-push hook uses it to decide whether a direct push
# to develop is allowed. GitHub rulesets can only condition on branch names, not
# on changed paths, so the path-awareness has to live here.
#
# Deliberately narrow — markdown and PDF only, matched by extension rather than
# by directory. docs/ currently holds nothing but .md and .pdf, so an extension
# match already covers all of it; whitelisting `docs/**` as a directory would
# only add the risk that a future executable file dropped in there is silently
# waved through, which is exactly how docs/audit/fixtures/*.json used to be
# loaded at runtime by tests/test_allegro_create_shipment.py.
#
# Explicitly NOT:
#   scripts/**   is `backend` in .github/path-filters.yml
#   .github/**   workflow contents are asserted by tests/test_ci_staging_policy.py
#   infra/**     terraform is validated and Checkov-scanned in CI
#   contracts/** and frontend/src/api/generated/** are drift-checked in CI
#
# Usage:  docs-only.sh [<range>]        e.g. docs-only.sh origin/develop..HEAD
# Prints the changed files to stdout.
# Exit 0 = documentation only. Exit 1 = contains code, OR the range could not be
# resolved — unknown always means "run the full gate", never "skip it".
set -euo pipefail

DOCS_RE='\.(md|pdf)$'

range="${1:-}"
if [[ -z "$range" ]]; then
  if git rev-parse --verify --quiet '@{upstream}' >/dev/null 2>&1; then
    # Three dots: what this branch adds since it diverged, not every difference.
    range='@{upstream}...HEAD'
  else
    exit 1
  fi
fi

changed=$(git diff --name-only "$range" 2>/dev/null || true)
if [[ -z "$changed" ]]; then
  exit 1
fi

printf '%s\n' "$changed"

if printf '%s\n' "$changed" | grep -qvE "$DOCS_RE"; then
  exit 1
fi
exit 0
