# The Lockfile Must Govern CI and the Production Image — Design Spec

**Date:** 2026-08-28
**Status:** Approved
**Issue:** #278
**Scope:** make `uv.lock` the source of truth for CI installs and the production Docker image,
which today both resolve dependencies from scratch with `pip`.

---

## Problem

The repository maintains `uv.lock`, and exactly one context reads it.

| Context | Tool | Reads `uv.lock`? |
| --- | --- | --- |
| Local dev, `scripts/check.sh` | uv | yes |
| CI (`_quality-gate.yml`, `_full-test-suite.yml`, `mutation.yml`) | pip | **no** |
| `Dockerfile`, `Dockerfile.dev` | pip | **no** |

The production image is the sharpest case. `Dockerfile` does:

```dockerfile
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir ".[api,cloud,ksef]"
```

The lockfile is never copied into the build context, so every image build resolves transitive
dependencies afresh. Two builds a week apart can ship different versions of the same declared
range, and neither is required to match what was tested.

The consequence is that **a green pipeline does not establish what runs in production.** CI tests
one resolution, the image is built from another, and `uv.lock` — the artefact that is supposed to
pin both — governs neither.

This is the same failure mode this codebase has been correcting all week: a guarantee that reads
as real and is not. The `uv lock --check` step added to `scripts/check.sh` on 2026-08-27 closed the
local half; this closes the half that reaches customers.

### Evidence it already bites

Dependabot's PR #276 raised `ruff` in `pyproject.toml` and left `uv.lock` pinning the old version.
Nothing caught it, because nothing except local dev consulted the lock. That drift reached
`develop` and was only noticed by hand.

---

## Design

### 0. `--locked`, not `--frozen`

`uv sync --frozen` means "install from the lockfile without re-resolving". It does **not** check
that the lockfile still matches `pyproject.toml` — verified: with a deliberately edited
`pyproject.toml`, `--frozen` succeeds and `--locked` exits 1 with
`The lockfile at uv.lock needs to be updated, but --locked was provided`.

Every CI and image install therefore uses `--locked`. The build failing on a stale lock is the
point, not a side effect.

### 1. The production image

`Dockerfile` becomes multi-stage:

- **Builder** installs `uv`, copies `pyproject.toml` **and `uv.lock`**, and runs
  `uv sync --locked --no-dev --extra api --extra cloud --extra ksef` into a virtual environment.
- **Final** copies that environment only. `uv` does not remain in the shipped image.

Everything the current file is deliberate about is preserved: dependencies installed before
sources so the layer caches, the non-root `app` user, the healthcheck, and the second install step
that puts the `zdrovena` package itself on the path for the `/usr/local/bin/zdrovena` entrypoint
used by Container App Jobs.

This also moves toward issue #238, which wants a multi-stage image.

### 2. CI

`astral-sh/setup-uv` in the three workflows, and each `pip install -e` replaced by
`uv sync --locked` with the extras that install site already names.

Two traps this codebase has hit before, both applying here:

- **Never `--all-extras`.** It pulls the `iac` extra → checkov → ecdsa, whose unfixable Minerva CVE
  `pyproject.toml` deliberately keeps out of the set pip-audit scans. Extras are listed explicitly.
- **`dev` exists twice.** `[project.optional-dependencies] dev` holds pytest, ruff, hypothesis;
  `[dependency-groups] dev` holds bandit and pip-audit. The first is `--extra dev`, the second is
  `--group dev`. The security job needs both.

### 3. Three tools that are not in the lockfile

`pyright`, `mutmut` and an ad-hoc `azure-storage-blob` install are pulled in by CI steps today.
`pyright` and `mutmut` appear in neither `pyproject.toml` nor `uv.lock`; once a step installs from
the lock they would vanish from the environment.

They are CI tooling, not application libraries, so they do not belong in the project's
dependencies. They run through `uvx` (isolated, no effect on the project environment) **with
pinned versions**. Pinning matters: an unpinned tool giving one answer locally and another in CI is
exactly how an eslint rule violation reached CI on 2026-08-27 from a branch whose local lint was
green.

`azure-storage-blob` is already declared in `pyproject.toml`; that step's extra install is
redundant once the environment comes from the lock.

### 4. Deliberately unchanged

- **`Dockerfile.dev`** keeps its editable install — that is the point of a dev image. It gains only
  the lockfile-aware dependency step, so dev and prod resolve the same versions.
- **The two HIGH advisories named in the issue** (`aiohttp`, `gitpython`) are transitive through
  checkov, i.e. the `iac` extra, which the production image does not install. Once the lock governs
  the build they become visible and decidable on their own terms. Bumping versions is not mixed
  into a change of build mechanism.

---

## Testing

Declarative checks are not enough here; the bug is precisely that a configuration *looks* correct.

**The image matches the lockfile.** Build the image and compare its installed distributions against
`uv.lock`. Before the change they diverge — that divergence *is* the bug. After it, every installed
version is the locked one. This is the test that proves the fix rather than describing it.

**A stale lock stops the build.** With `pyproject.toml` edited so the lock no longer matches,
`uv sync --locked` exits non-zero. Verified by hand before it is asserted, then pinned as a test —
a guard nobody has watched fail is not known to work.

**CI installs no longer reach the network for resolution.** Each converted step runs `uv sync
--locked`, so a step that silently reverted to `pip install -e` would be visible in a policy test
over the workflow files, following the precedent of `tests/test_ci_staging_policy.py`.

**The extras stay explicit.** A policy test asserts no workflow or Dockerfile uses `--all-extras`,
so the `iac`/ecdsa exclusion cannot be undone by a convenience edit.

**Nothing about the running container changes.** The image still starts as the non-root `app` user,
still answers the healthcheck, and the `zdrovena` console entrypoint still resolves.
