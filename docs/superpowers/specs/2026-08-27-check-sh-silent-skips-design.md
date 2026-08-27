# Local Quality Gate: No Silent Skips — Design Spec

**Date:** 2026-08-27
**Status:** Approved
**Issue:** #279
**Scope:** make `scripts/check.sh` fail when a step cannot run, instead of printing a skip and
exiting 0; give every skip an explicit, documented opt-out.

---

## Problem

`scripts/check.sh` runs from `.git/hooks/pre-push` and is the last gate before code leaves a
developer's machine. It contains **14** branches that print `~ <something> skipped` and let the
script continue to `All checks passed — safe to push.`

Two of them are deliberate and stay: `CHECK_TYPECHECK=0` and `CHECK_TESTS=0` are explicit
opt-outs a developer chooses.

The other **twelve** are accidents waiting to happen. Each has the shape:

```bash
if command -v ruff >/dev/null 2>&1; then
  ruff check . && ok "ruff check" || fail "ruff check failed"
else
  echo -e "${SKIP} ruff not found — skipping lint"
fi
```

A missing binary, a half-built `.venv`, or an absent `frontend/node_modules` silently downgrades
the gate and the push still reports success. The affected steps are ruff, bandit, uv-lock,
pip-audit, the SOPS guard, gitleaks, trivy, terraform fmt, checkov, and the three frontend steps
(lint, tests, build).

This is not hypothetical. Two instances landed in this repository within the last two days:

- A local `frontend/node_modules` holding `eslint-plugin-react-hooks@5.2.0` while
  `package-lock.json` pinned `7.1.1` made the local lint weaker than CI's. `npm run lint` passed
  locally and CI failed on the first run — costing a round-trip. The gate's own hint says
  `npm install`, which is precisely the command that lets the tree drift from the lockfile.
- The `uv lock --check` step added on 2026-08-27 was itself written in this anti-pattern.

The repository already states the governing rule, in `scripts/docs-only.sh`:

> Exit 0 = documentation only. Exit 1 = contains code, OR the range could not be resolved —
> unknown always means "run the full gate", never "skip it".

A step that cannot run is the same class of unknown. It must not read as a pass.

---

## Design

### 1. One helper, one rule

A `missing_tool` helper joins `step` / `ok` / `fail` / `SKIP` at the top of the script:

```
missing_tool <label> <CHECK_VAR> <hint>
```

It honours an explicit `CHECK_VAR=0` by printing the skip and returning, and otherwise calls
`fail` with an actionable install hint that also names the opt-out. There is no path through it
that silently returns success.

### 2. Twelve call sites

Each accidental-skip branch calls the helper instead of `echo`. The opt-out variables follow the
naming the script already uses:

| Step | Variable |
| --- | --- |
| ruff lint + format | `CHECK_RUFF` |
| bandit | `CHECK_BANDIT` |
| uv lock consistency | `CHECK_UV_LOCK` |
| pip-audit | `CHECK_PIPAUDIT` |
| SOPS age guard | `CHECK_SOPS` |
| gitleaks | `CHECK_GITLEAKS` |
| trivy | `CHECK_TRIVY` |
| terraform fmt | `CHECK_TERRAFORM` |
| checkov | `CHECK_CHECKOV` |
| frontend lint, tests, build | `CHECK_FRONTEND` |

One variable covers all three frontend steps: they fail for the same reason and are fixed by the
same command, so three separate switches would be noise.

`CHECK_TYPECHECK` and `CHECK_TESTS` keep their current behaviour untouched. They are the pattern
being extended, not a target.

### 3. The `npm install` hint becomes `npm ci`

The three frontend branches currently advise `npm install`, which resolves fresh versions and is
how a local tree drifts from `package-lock.json`. `npm ci` installs exactly what the lockfile
pins, which is what CI does and therefore what a local gate must match to mean anything.

### 4. A regression test

`tests/test_check_sh_policy.py`, following the shape of the existing
`tests/test_ci_staging_policy.py` (read the file as text, assert on its contents).

It parses `scripts/check.sh` and asserts that every `${SKIP}` occurrence is either inside a
`CHECK_*` opt-out branch or reached through `missing_tool`. A future step that reintroduces a
bare `echo -e "${SKIP} ... not found"` fails the suite.

This is the part that makes the fix durable rather than a one-off cleanup. The `uv lock` step
proves the anti-pattern is easy to re-add by hand, including by whoever wrote the rule.

### 5. Documentation

`CONTRIBUTING.md` § Quality gate is where a developer looks, and it is currently wrong in a way
that matters: it presents `bash scripts/check.sh` under **"Manual full check (optional)"**, while
the pre-push hook runs it on every push. It also never says which tools the gate needs.

The section gains: a corrected description of when the gate runs, the list of required tools with
their install commands, the opt-out table from section 2, and the rule in one line — a missing
tool fails the gate, and skipping one is a decision you record, not something that happens to you.

The script header gains a two-line pointer to that section rather than a duplicate table, so the
two cannot drift.

---

## Out of scope

- **Scoping requirements to changed areas** (requiring terraform only when `infra/` changed).
  There is precedent for path-awareness in `.github/path-filters.yml` and `scripts/docs-only.sh`,
  but a second local copy of that mapping would need to stay in sync with the CI one, and a drift
  between them would be a new silent-gap class — the exact thing this spec removes.
- **The `CHECK_TYPECHECK` / `CHECK_TESTS` opt-outs.** Deliberate, and the model for the rest.
- **CI workflow changes.** CI installs its own tools and fails when they are absent; this is a
  local-gate problem.

---

## Testing

- **Policy test** — every `${SKIP}` in `check.sh` is guarded, as described in section 4.
- **Opt-out honoured** — `missing_tool` with its variable set to `0` returns success and prints a
  skip; with the variable unset it exits non-zero. Exercised by running the helper in a subshell,
  so the assertion is about real behaviour rather than the text of the script.
- **Hint accuracy** — the frontend branches advise `npm ci`, and no branch in `check.sh` advises
  `npm install`.
- **The gate still passes** — a full `CHECK_DOCS_FASTPATH=0 bash scripts/check.sh` run on a
  machine with every tool present reports `All checks passed — safe to push.`, proving the
  rewrite did not turn a working environment into a failing one.
