# Local Quality Gate: No Silent Skips — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `scripts/check.sh` step that cannot run must fail the gate, not print a skip and let the push through.

**Architecture:** One `missing_tool` helper next to the existing `step` / `ok` / `fail`, called from the twelve accidental-skip branches. A policy test parses the script and refuses a bare `${SKIP}` outside a guarded branch, so the fix cannot rot. `CONTRIBUTING.md` documents the opt-outs, because an undiscoverable escape hatch pushes people to `--no-verify` instead.

**Tech Stack:** Bash, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-check-sh-silent-skips-design.md`
**Issue:** #279

---

## File Structure

**Create:**
- `tests/test_check_sh_policy.py` — the regression guard, modelled on `tests/test_ci_staging_policy.py`

**Modify:**
- `scripts/check.sh` — the helper plus twelve call sites
- `CONTRIBUTING.md` — § Quality gate

---

## Task 1: The `missing_tool` helper

**Files:**
- Modify: `scripts/check.sh` (helpers block, lines 9-11)
- Test: `tests/test_check_sh_policy.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_check_sh_policy.py`:

```python
"""Policy tests for scripts/check.sh — the local pre-push quality gate.

A step that cannot run must fail the gate. Printing a skip and continuing to
"All checks passed - safe to push" is how a broken environment silently
downgrades every guarantee the gate is supposed to give (issue #279).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"
SOURCE = CHECK_SH.read_text(encoding="utf-8")


def _run_helper(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a snippet against the real helper definitions from check.sh.

    Sources only the header (colour vars + helper functions) so the assertions
    are about actual behaviour, not about the text of the file.
    """
    header = SOURCE.split("# Aktywuj .venv", 1)[0]
    return subprocess.run(
        ["bash", "-c", header + "\n" + script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )


class TestMissingTool:
    def test_a_missing_tool_fails_the_gate(self):
        result = _run_helper('missing_tool "trivy" CHECK_TRIVY "zainstaluj trivy"')

        assert result.returncode != 0
        assert "trivy" in result.stdout

    def test_the_hint_names_the_opt_out_so_nobody_reaches_for_no_verify(self):
        result = _run_helper('missing_tool "trivy" CHECK_TRIVY "zainstaluj trivy"')

        assert "CHECK_TRIVY" in result.stdout
        assert "zainstaluj trivy" in result.stdout

    def test_an_explicit_opt_out_is_honoured(self):
        result = _run_helper(
            'missing_tool "trivy" CHECK_TRIVY "zainstaluj trivy"; echo "CONTINUED"',
            env={"CHECK_TRIVY": "0"},
        )

        assert result.returncode == 0
        assert "CONTINUED" in result.stdout

    def test_an_opt_out_set_to_anything_else_still_fails(self):
        # Only "0" opts out. A stray "false" or "no" must not disable a check.
        for value in ("1", "false", "no", ""):
            result = _run_helper(
                'missing_tool "trivy" CHECK_TRIVY "zainstaluj trivy"',
                env={"CHECK_TRIVY": value},
            )
            assert result.returncode != 0, f"CHECK_TRIVY={value!r} should not opt out"
```

The `SOURCE.split("# Aktywuj .venv", 1)[0]` boundary depends on that comment still being the first thing after the helper block. Confirm it by reading `scripts/check.sh` lines 1-14; if the header ends differently, split on whatever actually follows the `fail()` definition.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_check_sh_policy.py -v`
Expected: FAIL — `missing_tool: command not found`, so `returncode != 0` passes by accident but `test_an_explicit_opt_out_is_honoured` fails.

- [ ] **Step 3: Write the implementation**

In `scripts/check.sh`, directly after the `fail()` definition (line 11):

```bash
# A step that cannot run must never read as a pass. scripts/docs-only.sh states
# the same rule for its own unknown case: unknown means "run the full gate",
# never "skip it" (issue #279).
#
# The escape hatch is deliberate and matches CHECK_TESTS / CHECK_TYPECHECK:
# set CHECK_<STEP>=0 to skip on purpose. That turns a skip into a decision
# somebody recorded, rather than an accident of a broken PATH or a half-built
# .venv. Documented in CONTRIBUTING.md § Quality gate.
missing_tool() {
  local label="$1" var="$2" hint="$3"
  if [[ "${!var:-1}" == "0" ]]; then
    echo -e "${SKIP} $label pominięty ($var=0)"
    return 0
  fi
  fail "$label niedostępny — $hint. Świadome pominięcie: $var=0"
}
```

`${!var}` is bash indirect expansion — it reads the variable *named* by `$var`. The script already runs under `set -euo pipefail` with `#!/usr/bin/env bash`, so this is available.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_check_sh_policy.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Confirm the script still parses and runs**

```
bash -n scripts/check.sh
CHECK_DOCS_FASTPATH=0 bash scripts/check.sh 2>&1 | tail -3
```
Expected: no syntax error; the gate still ends with `All checks passed — safe to push.`

- [ ] **Step 6: Commit**

```bash
git add scripts/check.sh tests/test_check_sh_policy.py
git commit -m "ci: helper missing_tool — brak narzędzia wywala bramkę zamiast ją obniżać"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 2: Convert the twelve accidental skips

**Files:**
- Modify: `scripts/check.sh`

- [ ] **Step 1: Replace each branch**

Twelve `else` branches currently `echo` a skip. Replace the `echo` line in each — leave the
surrounding `if`/`else`/`fi` alone. Line numbers drift as you edit, so match on the text.

| Current `echo` text (match on this) | Replacement |
| --- | --- |
| `${SKIP} ruff not found — skipping lint` | `missing_tool "ruff" CHECK_RUFF "zainstaluj: uv sync --extra dev"` |
| `${SKIP} bandit not found — skipping (pip install bandit[toml])` | `missing_tool "bandit" CHECK_BANDIT "zainstaluj: uv sync --extra dev"` |
| `${SKIP} uv nie znaleziony — pomijam sprawdzenie uv.lock` | `missing_tool "uv" CHECK_UV_LOCK "zainstaluj uv: https://docs.astral.sh/uv/"` |
| `${SKIP} pip-audit nie znaleziony — uruchom: uv add --dev pip-audit` | `missing_tool "pip-audit" CHECK_PIPAUDIT "zainstaluj: uv sync --extra dev"` |
| `${SKIP} scripts/check-sops-secrets.sh nie jest wykonywalny` | `missing_tool "SOPS age guard" CHECK_SOPS "nadaj prawa: chmod +x scripts/check-sops-secrets.sh"` |
| `${SKIP} gitleaks nie znaleziony — zainstaluj: https://github.com/gitleaks/gitleaks` | `missing_tool "gitleaks" CHECK_GITLEAKS "zainstaluj: https://github.com/gitleaks/gitleaks"` |
| `${SKIP} trivy nie znaleziony — zainstaluj: https://aquasecurity.github.io/trivy` | `missing_tool "trivy" CHECK_TRIVY "zainstaluj: https://aquasecurity.github.io/trivy"` |
| `${SKIP} terraform not found — skipping fmt check` | `missing_tool "terraform" CHECK_TERRAFORM "zainstaluj: https://developer.hashicorp.com/terraform/downloads"` |
| `${SKIP} checkov nie znaleziony — zainstaluj: uv sync --extra iac` | `missing_tool "checkov" CHECK_CHECKOV "zainstaluj: uv sync --extra iac"` |
| `${SKIP} frontend/node_modules missing — run 'cd frontend && npm install' first` | `missing_tool "frontend (eslint)" CHECK_FRONTEND "uruchom: npm --prefix frontend ci"` |
| `${SKIP} frontend/node_modules missing — skipping tests` | `missing_tool "frontend (vitest)" CHECK_FRONTEND "uruchom: npm --prefix frontend ci"` |
| `${SKIP} frontend/node_modules missing — skipping build` | `missing_tool "frontend (build)" CHECK_FRONTEND "uruchom: npm --prefix frontend ci"` |

Note the frontend hints say **`npm ci`**, not `npm install`. `npm install` resolves fresh
versions and is how a local tree drifts from `package-lock.json` — which made the local eslint
weaker than CI's and cost a round-trip on 2026-08-27. `npm ci` installs exactly what the lockfile
pins, which is what CI does.

Do **not** touch the two deliberate opt-outs:
- `${SKIP} pyright pominięty (CHECK_TYPECHECK=0)`
- `${SKIP} pytest pominięty (CHECK_TESTS=0)`

They are already the pattern being extended.

- [ ] **Step 2: Verify the script parses**

Run: `bash -n scripts/check.sh`
Expected: no output.

- [ ] **Step 3: Verify no `npm install` advice survives**

Run: `grep -n "npm install" scripts/check.sh`
Expected: no matches.

- [ ] **Step 4: Verify each opt-out actually works**

Run each of these and confirm the step prints a skip and the gate continues rather than dying:

```
CHECK_TRIVY=0 CHECK_DOCS_FASTPATH=0 bash scripts/check.sh 2>&1 | grep -E "trivy|Safe to push|All checks"
```

Then prove the failing direction on a tool you can actually hide:

```
PATH=/usr/bin:/bin bash -c 'cd "$(git rev-parse --show-toplevel)" && CHECK_DOCS_FASTPATH=0 bash scripts/check.sh' 2>&1 | tail -3
```
Expected: a non-zero exit with a `✗ ... niedostępny` line, **not** `All checks passed`. This is
the whole point of the change — confirm it with your own eyes before moving on.

- [ ] **Step 5: Run the full gate normally**

Run: `CHECK_DOCS_FASTPATH=0 bash scripts/check.sh 2>&1 | tail -3`
Expected: `All checks passed — safe to push.` — the rewrite must not break a working environment.

- [ ] **Step 6: Commit**

```bash
git add scripts/check.sh
git commit -m "ci: dwanaście cichych pominięć w bramce zamienione na twardą porażkę"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 3: The regression guard

Without this the cleanup is a one-off. The `uv lock` step added on 2026-08-27 shows how easily
the anti-pattern is re-introduced by hand — by the same person who wrote the rule.

**Files:**
- Modify: `tests/test_check_sh_policy.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_check_sh_policy.py`:

```python
class TestNoBareSkips:
    def test_every_skip_is_either_an_opt_out_or_goes_through_missing_tool(self):
        """A bare `echo ${SKIP} ... not found` is the bug this issue is about.

        Only two skips may be printed directly: the deliberate CHECK_TESTS and
        CHECK_TYPECHECK opt-outs. Everything else must route through
        missing_tool, which fails unless the developer opted out on purpose.
        """
        allowed = {"CHECK_TYPECHECK=0", "CHECK_TESTS=0"}

        offenders = []
        for number, line in enumerate(SOURCE.splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("echo -e") or "${SKIP}" not in stripped:
                continue
            if any(token in stripped for token in allowed):
                continue
            offenders.append(f"{number}: {stripped}")

        assert offenders == [], (
            "check.sh must not print a skip outside missing_tool — a step that "
            "cannot run has to fail the gate (issue #279). Offending lines:\n"
            + "\n".join(offenders)
        )

    def test_the_frontend_hints_do_not_advise_npm_install(self):
        # npm install resolves fresh versions and lets node_modules drift from
        # package-lock.json, which is how the local eslint became weaker than
        # CI's. npm ci installs exactly what the lockfile pins.
        assert "npm install" not in SOURCE

    def test_missing_tool_is_defined_before_it_is_used(self):
        definition = SOURCE.index("missing_tool()")
        first_call = SOURCE.index('missing_tool "')
        assert definition < first_call
```

Note the `missing_tool` inside the helper's own `echo` lines does not start with `echo -e` at the
top level of a step, so it will not trip the scan. Confirm that when you run it — if the helper's
own body registers as an offender, exclude the helper block by line range rather than loosening
the check.

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_check_sh_policy.py -v`
Expected: PASS, 7 passed. (These pass immediately because Task 2 already fixed the script — that
is fine. The test's value is catching the *next* regression, and Step 3 proves it can.)

- [ ] **Step 3: Prove the guard actually catches a regression**

Temporarily add a bare skip to `scripts/check.sh`:

```bash
printf '\necho -e "${SKIP} deliberately-broken not found"\n' >> scripts/check.sh
.venv/bin/python -m pytest tests/test_check_sh_policy.py -k bare_skips -q
```
Expected: FAIL, naming the offending line.

Then revert it:
```bash
git checkout scripts/check.sh
.venv/bin/python -m pytest tests/test_check_sh_policy.py -q
```
Expected: PASS. A guard nobody has seen fail is not known to work.

- [ ] **Step 4: Run the full suite**

```
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check scripts/ tests/ zdrovena/
.venv/bin/python -m ruff format --check .
```
All must pass. `ruff format --check .` also formats python blocks inside Markdown, so run it over
the whole repo exactly as written.

- [ ] **Step 5: Commit**

```bash
git add tests/test_check_sh_policy.py
git commit -m "test(ci): bramka nie może po cichu pominąć kroku"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 4: Documentation

An escape hatch nobody can find is worse than none: a developer without `trivy` will reach for
`git push --no-verify` and bypass the whole gate instead of one step.

**Files:**
- Modify: `CONTRIBUTING.md` (§ Quality gate, lines ~70-118), `scripts/check.sh` (header comment)

- [ ] **Step 1: Fix what § Quality gate currently claims**

That section presents `bash scripts/check.sh` under **"Manual full check (optional)"**, while
`.git/hooks/pre-push` runs it on every push. Correct the framing, then add the tool list and the
opt-out table:

```markdown
### Local (automatic on push)

`.git/hooks/pre-push` runs `scripts/check.sh` on every push. It is not optional — it is the last
gate before code leaves your machine, and it blocks the push when a step fails.

A change touching only `.md` / `.pdf` files takes the fast path in `scripts/docs-only.sh` and
skips the gate. Force a full run with `CHECK_DOCS_FASTPATH=0 bash scripts/check.sh`.

**A step that cannot run fails the gate.** A missing tool used to print a skip and let the push
through, which meant a half-built `.venv` silently downgraded every guarantee below (#279). If
you genuinely cannot install something, skip it on purpose with the matching variable — that way
the skip is a decision you recorded, not something that happened to you.

| Step | Needs | Skip with |
| --- | --- | --- |
| ruff lint + format | `uv sync --extra dev` | `CHECK_RUFF=0` |
| pyright | `uv sync --extra dev` | `CHECK_TYPECHECK=0` |
| bandit | `uv sync --extra dev` | `CHECK_BANDIT=0` |
| pytest + coverage | `uv sync --extra dev` | `CHECK_TESTS=0` |
| uv lock consistency | `uv` | `CHECK_UV_LOCK=0` |
| pip-audit | `uv sync --extra dev` | `CHECK_PIPAUDIT=0` |
| SOPS age guard | `chmod +x scripts/check-sops-secrets.sh` | `CHECK_SOPS=0` |
| gitleaks | [gitleaks](https://github.com/gitleaks/gitleaks) | `CHECK_GITLEAKS=0` |
| trivy | [trivy](https://aquasecurity.github.io/trivy) | `CHECK_TRIVY=0` |
| terraform fmt | [terraform](https://developer.hashicorp.com/terraform/downloads) | `CHECK_TERRAFORM=0` |
| checkov | `uv sync --extra iac` | `CHECK_CHECKOV=0` |
| frontend lint, tests, build | `npm --prefix frontend ci` | `CHECK_FRONTEND=0` |

Use `npm ci`, not `npm install`: `npm install` resolves fresh versions and lets `node_modules`
drift from `package-lock.json`, which makes the local lint weaker than CI's.

`git push --no-verify` bypasses the whole gate. Prefer one targeted variable above.
```

Keep the existing "CI" and "Staging gate" subsections. Remove the now-wrong
"Manual full check (optional)" heading, folding its content into the corrected section above.

**Second inaccuracy in the same section, already verified:** CONTRIBUTING tells the developer to
install the hook with `pre-commit install --hook-type pre-push`. That is not how it works —
`scripts/install-hooks.sh` writes `.git/hooks/pre-push` itself (see its heredoc at line 9 and the
`chmod +x` at line 71). Correct that line to `bash scripts/install-hooks.sh`.

Both errors point the same way: the section describes a gate that is weaker and more optional than
the one that actually runs, which is how somebody ends up trusting a green push that checked
nothing.

- [ ] **Step 2: Point the script at the documentation**

Add to the header comment block of `scripts/check.sh`, under the existing description:

```bash
# Każdy krok, którego nie da się uruchomić, wywala bramkę. Świadome pominięcie:
# CHECK_<KROK>=0 — pełna tabela w CONTRIBUTING.md § Quality gate.
```

A pointer, not a copy: two tables would drift.

- [ ] **Step 3: Verify the table matches reality**

Run: `grep -oE 'CHECK_[A-Z_]+' scripts/check.sh | sort -u`

Every variable the script reads must appear in the CONTRIBUTING table, and every variable in the
table must appear here. Reconcile any difference — a table that lies is worse than no table.

- [ ] **Step 4: Commit**

```bash
git add CONTRIBUTING.md scripts/check.sh
git commit -m "docs: bramka jakości i jej furtki w CONTRIBUTING"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 5: Full gate and PR

- [ ] **Step 1: Sync the frontend to the lockfile**

Run: `npm --prefix frontend ci`

- [ ] **Step 2: Run the complete gate**

Run: `CHECK_DOCS_FASTPATH=0 bash scripts/check.sh`
Expected: `All checks passed — safe to push.`

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin fix/check-sh-silent-skips-279
```

Open it against `develop`. Reference `Closes #279` in the body. Say in the body that the guard was
verified in both directions — that a missing tool now fails, and that the policy test was seen
catching a deliberately reintroduced bare skip.

---

## Verification against the spec

| Spec section | Tasks |
| --- | --- |
| 1. One helper, one rule | 1 |
| 2. Twelve call sites | 2 |
| 3. `npm install` → `npm ci` | 2, 3 |
| 4. A regression test | 1, 3 |
| 5. Documentation | 4 |
| Testing | 1, 2, 3, 5 |
