"""Properties of the production image that must survive future edits (#238).

Each of these was verified once by hand against a built image. A test is what
keeps them true: the expensive properties here are exactly the ones that fail
silently -- an image that is 100 MB fatter still starts, and a package copied
over its installed self still imports.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _text() -> str:
    """Instructions only. Comments explain what we deliberately do NOT do, so
    matching against them would make every explanation a policy violation."""
    return "\n".join(
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _final_stage() -> str:
    """Everything after the last FROM — the layers that actually ship."""
    stages = _text().split("\nFROM ")
    return stages[-1]


def test_the_build_is_multi_stage():
    assert len(re.findall(r"^FROM ", _text(), re.MULTILINE)) >= 2


def test_the_final_stage_carries_no_build_tooling():
    """uv resolves the lockfile in the builder and has no business shipping."""
    final = _final_stage()

    assert "uv sync" not in final
    assert "ghcr.io/astral-sh/uv" not in final


def test_the_final_stage_does_not_shadow_the_installed_package():
    """`uv sync --no-editable` installs into the venv; WORKDIR is on sys.path.

    A `COPY zdrovena/` in the final stage puts a second copy at /app/zdrovena
    which wins the import — so the artifact that was built, scanned and verified
    is not the code that runs.
    """
    final = _final_stage()

    assert not re.search(r"^COPY\s+zdrovena/", final, re.MULTILINE)


def test_ownership_is_set_while_copying_not_afterwards():
    """`chown -R` on the venv rewrites every file into a second full copy.

    Measured at 107 MB of a 318 MB image before this was fixed.
    """
    assert "chown -R" not in _text()
    assert "COPY --from=builder --chown=app:app" in _text()


def test_the_container_runs_as_a_non_root_user():
    final = _final_stage()

    assert re.search(r"^USER app", final, re.MULTILINE)
    assert "useradd" in final


def test_the_image_keeps_its_healthcheck():
    assert "HEALTHCHECK" in _final_stage()


def test_the_packaged_cli_entrypoint_is_reachable():
    """Container App Jobs invoke `zdrovena`, which lives in the venv's bin."""
    assert 'ENV PATH="/app/.venv/bin:$PATH"' in _final_stage()
