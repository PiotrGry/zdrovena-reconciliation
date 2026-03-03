"""
Analyzers gate: reads individual tool reports, produces gate decision JSON.
Called by pipeline-analyzers.yml and pipeline-full.yml.
"""

import argparse
import json
import sys
from pathlib import Path


THRESHOLDS = {
    "pylint_score":    (">=", 7.0),
    "radon_cc_avg":    ("<=", 10.0),
    "radon_mi_avg":    (">=", 50.0),
    "coverage_line":   (">=", 70.0),
    "bandit_high":     ("==", 0),
    "ruff_violations": ("<=", 20),
}


def _pylint_score(path: str) -> float:
    try:
        data = json.loads(Path(path).read_text())
        violations = len([m for m in data if m.get("type") in ("error", "warning", "convention")])
        return round(max(0.0, 10.0 - violations / 10.0), 2)
    except Exception:
        return 0.0


def _radon_cc_avg(path: str) -> float:
    try:
        data = json.loads(Path(path).read_text())
        complexities = [
            item["complexity"]
            for file_results in data.values()
            for item in file_results
            if "complexity" in item
        ]
        return round(sum(complexities) / len(complexities), 2) if complexities else 0.0
    except Exception:
        return 0.0


def _radon_mi_avg(path: str) -> float:
    try:
        data = json.loads(Path(path).read_text())
        mis = [v["mi"] for v in data.values() if isinstance(v, dict) and "mi" in v]
        return round(sum(mis) / len(mis), 2) if mis else 0.0
    except Exception:
        return 0.0


def _coverage_line(path: str) -> float:
    try:
        data = json.loads(Path(path).read_text())
        return round(data.get("totals", {}).get("percent_covered", 0.0), 2)
    except Exception:
        return 0.0


def _bandit_high(path: str) -> int:
    try:
        data = json.loads(Path(path).read_text())
        return sum(1 for r in data.get("results", []) if r.get("issue_severity") == "HIGH")
    except Exception:
        return 0


def _ruff_violations(path: str) -> int:
    try:
        data = json.loads(Path(path).read_text())
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


def _check(name: str, value: float) -> bool:
    op, threshold = THRESHOLDS[name]
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    return False


def main():
    p = argparse.ArgumentParser(description="Analyzers gate decision")
    p.add_argument("--ruff",      required=True)
    p.add_argument("--radon-cc",  required=True)
    p.add_argument("--radon-mi",  required=True)
    p.add_argument("--pylint",    required=True)
    p.add_argument("--coverage",  required=True)
    p.add_argument("--bandit",    required=True)
    p.add_argument("--output",    required=True)
    args = p.parse_args()

    metrics = {
        "pylint_score":    _pylint_score(args.pylint),
        "radon_cc_avg":    _radon_cc_avg(args.radon_cc),
        "radon_mi_avg":    _radon_mi_avg(args.radon_mi),
        "coverage_line":   _coverage_line(args.coverage),
        "bandit_high":     _bandit_high(args.bandit),
        "ruff_violations": _ruff_violations(args.ruff),
    }

    failures = [
        f"{name}={v} (required: {THRESHOLDS[name][0]}{THRESHOLDS[name][1]})"
        for name, v in metrics.items()
        if not _check(name, v)
    ]

    result = {
        "gate": "PASS" if not failures else "FAIL",
        "failures": failures,
        **metrics,
    }

    Path(args.output).write_text(json.dumps(result, indent=2))
    print(f"Analyzers gate: {result['gate']}")
    for f in failures:
        print(f"  ✗ {f}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
