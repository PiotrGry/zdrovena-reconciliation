# The Lockfile Must Govern CI and the Production Image — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** What CI tests and what the production image ships must both come from `uv.lock`.

**Architecture:** `astral-sh/setup-uv` plus `uv sync --locked` replaces every `pip install -e` in CI. The production `Dockerfile` becomes multi-stage: a builder with `uv` resolves from the lock, the final image copies only the resulting environment. CI tooling that is not a project dependency (`pyright`, `mutmut`) runs through pinned `uvx`. Policy tests stop the conversion from being quietly undone.

**Tech Stack:** GitHub Actions, Docker, uv, pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-uv-lock-in-ci-and-docker-design.md`
**Issue:** #278

---

## The two rules that govern every edit

1. **`--locked`, never `--frozen`.** `--frozen` installs from the lock without checking it still
   matches `pyproject.toml`; `--locked` fails when it does not. Verified by hand: with an edited
   `pyproject.toml`, `--frozen` succeeds and `--locked` exits 1. The build failing on a stale lock
   is the point.
2. **Never `--all-extras`.** It pulls the `iac` extra → checkov → ecdsa, whose unfixable Minerva CVE
   `pyproject.toml` deliberately keeps outside the set pip-audit scans. Every extra is named.

A third trap, specific to this repo: **`dev` exists twice.**
`[project.optional-dependencies] dev` holds pytest, ruff, hypothesis (→ `--extra dev`);
`[dependency-groups] dev` holds bandit and pip-audit (→ `--group dev`). The security job needs both.

---

## File Structure

**Modify:**
- `Dockerfile` — multi-stage, lock-driven
- `Dockerfile.dev` — same dependency source, editable install kept
- `.github/workflows/_quality-gate.yml` — three install sites
- `.github/workflows/mutation.yml` — one install site
- `.github/workflows/_full-test-suite.yml` — one install site
- `CHANGELOG.md`

**Create:**
- `tests/test_ci_dependency_policy.py` — policy guard, modelled on `tests/test_ci_staging_policy.py`

---

## Task 1: Prove the bug before fixing it

The claim is that the shipped image does not match the lockfile. Establish that as a measurement,
not an assertion — it is also the before-picture the final task compares against.

**Files:** none (investigation)

- [ ] **Step 1: Build the image as it is today**

```bash
docker build -t zdrovena-before:local .
```

If Docker is unavailable in this environment, say so in your report and skip to Task 2 — do not
fake the measurement. The comparison in Task 6 then becomes the only evidence, and your report
must say that plainly.

- [ ] **Step 2: List what it actually installed**

```bash
docker run --rm zdrovena-before:local python -m pip list --format=freeze > /tmp/before.txt
wc -l /tmp/before.txt
```

- [ ] **Step 3: Compare against the lockfile**

```bash
uv export --frozen --format requirements-txt --no-emit-project --no-hashes \
  | grep -vE '^\s*#|^\s*$' | sed 's/ ;.*//' | sort > /tmp/locked.txt
comm -23 <(sort /tmp/before.txt) /tmp/locked.txt | head -40
```

Record how many installed versions are **not** the locked ones. Expect a non-empty list — that
divergence is the bug this issue describes. Note the count in your report; Task 6 must bring it to
the set explainable by extras alone.

---

## Task 2: The production image

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Rewrite as multi-stage**

Read the current file first — every comment in it records a decision that must survive (dependency
layer before sources for caching; the `--no-deps .` step exists because the dependency layer is
built before `zdrovena/` is copied, so the console entrypoint used by Container App Jobs would
otherwise be missing).

```dockerfile
# ── Builder: resolve from uv.lock, not from a fresh pip resolution ───────────
FROM python:3.12-slim AS builder

# uv reads uv.lock; pip does not. Before this, every image build resolved
# dependencies afresh, so what shipped was not what CI tested (issue #278).
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies before sources — this layer caches until pyproject/uv.lock change.
# --locked, not --frozen: a lockfile that no longer matches pyproject.toml must
# fail the build rather than silently install something else.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-dev \
        --extra api --extra cloud --extra ksef

# Sources last — changes here do not invalidate the dependency layer.
COPY zdrovena/ zdrovena/
RUN uv sync --locked --no-editable --no-dev \
        --extra api --extra cloud --extra ksef

# ── Final: the environment only, no uv, no build tooling ─────────────────────
FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY zdrovena/ zdrovena/

ENV PATH="/app/.venv/bin:$PATH" APP_ENV=prod
EXPOSE 8000

# Non-root user — principle of least privilege
RUN useradd -r -s /bin/false app && chown -R app:app /app
USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "zdrovena.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`0.11.6` is the uv this environment runs (`uv --version`) — confirm the
`ghcr.io/astral-sh/uv:0.11.6` tag exists before relying on it, and never use `latest`. An unpinned
tool is how the eslint-plugin drift of 2026-08-27 happened.

`--no-editable` matters: the final stage copies `.venv` without `/app` source paths being an
editable link back into a directory the builder owned.

- [ ] **Step 2: Build it**

```bash
docker build -t zdrovena-after:local .
```
Expected: success. If `uv sync --locked` fails here, the lock is stale — run `uv lock`, commit it,
and say so in your report; do not switch to `--frozen`.

- [ ] **Step 3: Prove the container still works the way it did**

```bash
docker run --rm zdrovena-after:local python -c "import zdrovena.api.main; print('import ok')"
docker run --rm zdrovena-after:local which zdrovena
docker run --rm zdrovena-after:local id -un
```
Expected: the import succeeds, the `zdrovena` console entrypoint resolves (this is what the old
`--no-deps .` step existed for — if it is missing, the second `uv sync` is wrong), and the user is
`app`, not root.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "build: obraz produkcyjny instaluje z uv.lock, nie z nowego rozwiązania pip"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 3: `Dockerfile.dev`

**Files:**
- Modify: `Dockerfile.dev`

- [ ] **Step 1: Point its dependency step at the lock**

Keep the editable install — that is the purpose of a dev image. Change only where the dependency
versions come from, so dev and prod resolve identically:

```dockerfile
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --extra api --extra cloud --extra ksef --extra dev
ENV PATH="/app/.venv/bin:$PATH"
```

Read the current file before editing: it mounts sources for `--reload`, and whatever makes that
work must keep working. Use the same pinned uv tag as Task 2.

- [ ] **Step 2: Build and smoke it**

```bash
docker build -f Dockerfile.dev -t zdrovena-dev:local .
docker run --rm zdrovena-dev:local python -c "import pytest, zdrovena; print('dev deps ok')"
```

- [ ] **Step 3: Commit**

```bash
git add Dockerfile.dev
git commit -m "build: obraz deweloperski czyta ten sam lockfile co produkcyjny"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 4: CI

**Files:**
- Modify: `.github/workflows/_quality-gate.yml`, `.github/workflows/mutation.yml`, `.github/workflows/_full-test-suite.yml`

Five install sites. In each, replace `actions/setup-python` + `pip install` with
`astral-sh/setup-uv` + `uv sync --locked`, and run the following commands through `uv run` so they
use that environment.

Pin the `astral-sh/setup-uv` action to a commit SHA with a `# vX.Y.Z` comment — every other action
in these files is pinned that way and an unpinned one would stand out as the weak link.

- [ ] **Step 1: `_quality-gate.yml:73` — lint, typecheck, tests**

```yaml
      - uses: astral-sh/setup-uv@<sha>  # vX.Y.Z
        with:
          version: "0.11.6"
      - run: uv sync --locked --extra dev --extra api --extra cloud --extra report --extra ksef --extra pdf
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uvx pyright@1.1.409
      - run: uv run pytest tests/ -q --tb=short --cov=zdrovena --cov-fail-under=80
```

`pyright` is in neither `pyproject.toml` nor `uv.lock` — it is CI tooling, not an application
dependency, so it runs isolated through `uvx` with the version this repo uses locally (1.1.409).
Confirm that version against `pyright --version` before writing it.

Keep every surrounding step (checkout, caching, `if:` conditions) exactly as it is.

- [ ] **Step 2: `_quality-gate.yml:94` — security**

```yaml
      - uses: astral-sh/setup-uv@<sha>  # vX.Y.Z
        with:
          version: "0.11.6"
      - run: uv sync --locked --group dev --extra dev --extra api --extra cloud
```

Both `dev` selectors are required: `pip-audit` and `bandit` live in `[dependency-groups] dev`,
while the rest of the tooling lives in `[project.optional-dependencies] dev`. Then prefix the
existing `pip-audit` / `bandit` invocations with `uv run`.

The `pip install --upgrade pip` line above it (with its CVE comment) becomes dead once nothing uses
pip — remove it and note the removal in the commit message so the CVE rationale is not lost
silently.

- [ ] **Step 3: `_quality-gate.yml:155` — API contract drift**

```yaml
      - uses: astral-sh/setup-uv@<sha>  # vX.Y.Z
        with:
          version: "0.11.6"
      - run: uv sync --locked --extra api --extra cloud --extra report --extra ksef --extra pdf
```

This job then runs `scripts/check-api-contracts.sh`, which calls `python3 scripts/export-openapi.py`
and needs the project importable. Check whether that script invokes `python3` directly — if so it
will miss the uv environment unless the step activates it. Either run the script under `uv run`, or
export `PATH` to include `.venv/bin`. Verify by reading the script, not by assuming.

- [ ] **Step 4: `mutation.yml:33`**

```yaml
      - uses: astral-sh/setup-uv@<sha>  # vX.Y.Z
        with:
          version: "0.11.6"
      - run: uv sync --locked --extra dev
      - run: uvx mutmut@<pinned> run
```

`mutmut` is CI tooling and in neither the project nor the lock. Pin whatever major version the
existing step expects — read the surrounding steps for the arguments it is called with.

- [ ] **Step 5: `_full-test-suite.yml:355` — staging seed**

That step installs `azure-storage-blob>=12.19.0` and `azure-identity>=1.16.0` ad hoc, and does not
install the project at all. Both packages are already declared in `pyproject.toml`'s `cloud` extra
(`azure-storage-blob>=12.24.0`, `azure-identity>=1.18.0`), so the lock can supply them:

```yaml
      - run: uv sync --locked --extra cloud
```

and run `scripts/ci/seed-staging.sh` with `.venv/bin` on `PATH`. Read that script first to see how
it invokes Python — if it calls `python3`, the environment must be on `PATH` or the call wrapped in
`uv run`.

Note the version floors differ (`>=12.19.0` ad hoc versus `>=12.24.0` declared). Using the lock
raises the floor, which is the correct direction; say so in your report.

- [ ] **Step 6: Verify the workflows still parse**

```bash
for f in .github/workflows/_quality-gate.yml .github/workflows/mutation.yml .github/workflows/_full-test-suite.yml; do
  python3 -c "import yaml,sys; yaml.safe_load(open('$f')); print('ok $f')"
done
```

- [ ] **Step 7: Confirm no pip install remains in the converted files**

```bash
grep -n "pip install" .github/workflows/_quality-gate.yml .github/workflows/mutation.yml .github/workflows/_full-test-suite.yml
```
Expected: no matches.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/
git commit -m "ci: instalacje z uv.lock zamiast świeżego rozwiązania pip"
```

Append this trailer to the commit message body, and record in the body that the
`pip install --upgrade pip` CVE line was removed because nothing uses pip any more:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 5: A policy guard

Without it the conversion is a one-off — the same reasoning that made the `check.sh` guard
necessary two days ago.

**Files:**
- Create: `tests/test_ci_dependency_policy.py`

- [ ] **Step 1: Write the test**

Model it on `tests/test_ci_staging_policy.py` (read the file as text, assert on contents).

```python
"""Policy tests for how CI and the images install dependencies.

uv.lock only means something if the things that matter read it. CI and both
Dockerfiles used to install with pip, which ignores the lockfile, so a green
pipeline did not establish what shipped (issue #278).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CONVERTED = [
    WORKFLOWS / "_quality-gate.yml",
    WORKFLOWS / "mutation.yml",
    WORKFLOWS / "_full-test-suite.yml",
]
DOCKERFILES = [REPO_ROOT / "Dockerfile", REPO_ROOT / "Dockerfile.dev"]


class TestInstallsComeFromTheLockfile:
    def test_no_converted_workflow_installs_with_pip(self):
        offenders = [
            path.name for path in CONVERTED if "pip install" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], (
            f"pip ignores uv.lock, so these installs would not match what ships: {offenders}"
        )

    def test_no_dockerfile_installs_with_pip(self):
        offenders = [
            path.name for path in DOCKERFILES if "pip install" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_every_sync_is_locked_not_frozen(self):
        """--frozen installs from the lock without checking it still matches
        pyproject.toml. --locked fails when it does not, which is the point."""
        for path in CONVERTED + DOCKERFILES:
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "uv sync" not in line:
                    continue
                assert "--locked" in line, f"{path.name}: uv sync without --locked: {line.strip()}"
                assert "--frozen" not in line, (
                    f"{path.name}: --frozen is not a check: {line.strip()}"
                )

    def test_nothing_uses_all_extras(self):
        """--all-extras pulls the iac extra -> checkov -> ecdsa, whose unfixable
        Minerva CVE pyproject deliberately keeps out of pip-audit's scope."""
        for path in CONVERTED + DOCKERFILES:
            assert "--all-extras" not in path.read_text(encoding="utf-8"), path.name

    def test_the_lockfile_reaches_the_image_build_context(self):
        # The old Dockerfile copied pyproject.toml but never uv.lock, so the
        # lock could not have governed the build even in principle.
        for path in DOCKERFILES:
            assert "uv.lock" in path.read_text(encoding="utf-8"), path.name
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_ci_dependency_policy.py -v`
Expected: PASS, given Tasks 2-4 are done.

- [ ] **Step 3: Prove the guard catches a regression**

Temporarily reintroduce the anti-pattern, watch the test fail, then revert:

```bash
printf '\n# RUN pip install something\n' >> Dockerfile
.venv/bin/python -m pytest tests/test_ci_dependency_policy.py -k dockerfile -q
git checkout Dockerfile
.venv/bin/python -m pytest tests/test_ci_dependency_policy.py -q
```
Expected: FAIL naming `Dockerfile`, then PASS after the revert. A guard nobody has seen fail is not
known to work.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ci_dependency_policy.py
git commit -m "test(ci): instalacje muszą pochodzić z uv.lock"
```

Append this trailer to the commit message body:
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

---

## Task 6: The measurement that proves it, docs, gate and PR

- [ ] **Step 1: Compare the image against the lockfile**

Repeat Task 1's measurement against the new image:

```bash
docker run --rm zdrovena-after:local python -m pip list --format=freeze > /tmp/after.txt
comm -23 <(sort /tmp/after.txt) /tmp/locked.txt
```

Every remaining line must be explainable — `pip`/`setuptools`/`wheel` that the base image ships,
and nothing else. Any application dependency at an unlocked version means the build is still not
governed by the lock; investigate before proceeding.

Put the before/after counts in the PR body. This is the evidence the issue asks for.

- [ ] **Step 2: Changelog**

Under `## Unreleased` → `### Fixed`:

```markdown
- **build**: CI i obraz produkcyjny instalują teraz z `uv.lock`. Wcześniej oba używały pipa, który
  lockfile'a nie czyta — `Dockerfile` nawet go nie kopiował do kontekstu budowy, więc każdy build
  rozwiązywał zależności od nowa i zielony pipeline nie ustalał, co działa na produkcji. `uv sync
  --locked` (nie `--frozen`: to pierwsze sprawdza, że lock zgadza się z `pyproject.toml`, drugie
  nie) plus obraz multi-stage, w którym `uv` zostaje w warstwie budującej. Narzędzia CI spoza
  projektu (`pyright`, `mutmut`) idą przez `uvx` z przypiętymi wersjami. (#278)
```

- [ ] **Step 3: Full gate**

```bash
npm --prefix frontend ci
CHECK_DOCS_FASTPATH=0 bash scripts/check.sh
```
Expected: `All checks passed — safe to push.`

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin fix/uv-lock-in-ci-and-docker-278
```

Against `develop`, `Closes #278` in the body, with the before/after divergence counts from Step 1.

Say explicitly in the body that **CI running green on this PR is itself part of the evidence** —
the converted workflows are exercised by the PR that converts them.

Flag for the reviewer: the two HIGH advisories the issue names (`aiohttp`, `gitpython`) are
transitive through checkov, i.e. the `iac` extra, which the production image does not install.
They become visible and decidable once the lock governs the build; bumping them is deliberately
not mixed into this change.

---

## Verification against the spec

| Spec section | Tasks |
| --- | --- |
| 0. `--locked`, not `--frozen` | 2, 3, 4, 5 |
| 1. The production image | 2 |
| 2. CI | 4 |
| 3. Tools not in the lockfile | 4 |
| 4. Deliberately unchanged | 3 |
| Testing | 1, 2, 5, 6 |
