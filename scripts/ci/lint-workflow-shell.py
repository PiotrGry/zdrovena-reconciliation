#!/usr/bin/env python3
"""Validate the inline shell in GitHub Actions ``run:`` blocks.

Each ``run:`` block is extracted with a real YAML parser and checked on its own
with ``bash -n`` (and optionally shellcheck). Blocks are never concatenated:
gluing unrelated snippets together produces bogus "unexpected end of file"
errors, which is exactly what the previous regex-based extractor did.

GitHub expressions (``${{ ... }}``) are replaced with a placeholder word before
the check, because they are not valid shell.

Usage: lint-workflow-shell.py [--shellcheck] WORKFLOW.yml [...]
Exit code: 0 when every block parses, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# ``${{ ... }}`` may span lines (e.g. a long fromJSON call), so match lazily
# across newlines instead of stopping at the first closing brace.
GH_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
PLACEHOLDER = "GH_EXPR"

# ``shell:`` values that mean "this is POSIX shell we can parse".
BASH_SHELLS = ("bash", "sh")


@dataclass
class RunBlock:
    job: str
    step: str
    line: int  # 1-based line in the workflow where the script body starts
    script: str


def _child(node: yaml.Node, key: str) -> yaml.Node | None:
    """Return the value node for `key` in a mapping node, if present."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value_node
    return None


def _scalar(node: yaml.Node | None) -> str | None:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def collect_run_blocks(path: Path) -> list[RunBlock]:
    """Extract every bash ``run:`` block from a workflow file."""
    root = yaml.compose(path.read_text())
    jobs = _child(root, "jobs") if root is not None else None
    if not isinstance(jobs, yaml.MappingNode):
        return []

    blocks: list[RunBlock] = []
    for job_key, job_node in jobs.value:
        job_name = _scalar(job_key) or "<unnamed job>"
        steps = _child(job_node, "steps")
        if not isinstance(steps, yaml.SequenceNode):
            continue  # reusable-workflow job (uses:) — no inline shell

        for index, step_node in enumerate(steps.value):
            run_node = _child(step_node, "run")
            if not isinstance(run_node, yaml.ScalarNode):
                continue

            # A step-level shell: overrides the workflow default; anything that
            # is not bash/sh (python, pwsh, ...) is not ours to parse.
            shell = _scalar(_child(step_node, "shell"))
            if shell is not None and not shell.startswith(BASH_SHELLS):
                continue

            # For a block scalar the mark sits on the `|` indicator, so the
            # script body starts on the next line. Plain scalars start in place.
            body_offset = 2 if run_node.style in ("|", ">") else 1
            blocks.append(
                RunBlock(
                    job=job_name,
                    step=_scalar(_child(step_node, "name")) or f"step[{index}]",
                    line=run_node.start_mark.line + body_offset,
                    script=run_node.value,
                )
            )
    return blocks


def _remap_line(message: str, block: RunBlock) -> str:
    """Rewrite `line N` in a checker message to the workflow's own line."""

    def shift(match: re.Match[str]) -> str:
        return f"{match.group(1)}{block.line + int(match.group(2)) - 1}"

    return re.sub(r"(line )(\d+)", shift, message)


def check_block(block: RunBlock, use_shellcheck: bool) -> list[str]:
    """Return the errors found in one run block (empty when it is clean)."""
    script = GH_EXPRESSION.sub(PLACEHOLDER, block.script)
    errors: list[str] = []

    parsed = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    if parsed.returncode != 0:
        errors.append(_remap_line(parsed.stderr.strip(), block))
        return errors  # shellcheck adds nothing useful once bash cannot parse

    if use_shellcheck:
        # Severity `error` only: these are genuine breakage (bad redirects,
        # unterminated quotes), not the style warnings CI would drown in.
        checked = subprocess.run(
            ["shellcheck", "--shell=bash", "--severity=error", "--format=gcc", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        if checked.returncode != 0:
            for raw in checked.stdout.strip().splitlines():
                # gcc format is `-:LINE:COL: error: message`
                parts = raw.split(":", 3)
                if len(parts) == 4 and parts[1].isdigit():
                    raw = f"line {block.line + int(parts[1]) - 1}:{parts[3]}"
                errors.append(raw)

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflows", nargs="+", type=Path)
    parser.add_argument(
        "--shellcheck",
        action="store_true",
        help="also run shellcheck (severity=error) on each block",
    )
    args = parser.parse_args()

    failed = False
    total = 0
    for path in args.workflows:
        if not path.is_file():
            print(f"  missing: {path}", file=sys.stderr)
            failed = True
            continue

        blocks = collect_run_blocks(path)
        total += len(blocks)
        for block in blocks:
            errors = check_block(block, args.shellcheck)
            if not errors:
                continue
            failed = True
            print(f"  {path}:{block.line} [{block.job} / {block.step}]", file=sys.stderr)
            for error in errors:
                print(f"      {error}", file=sys.stderr)

    if not failed:
        print(f"  {len(args.workflows)} workflows, {total} run blocks parse cleanly")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
