"""
Aggregate all quality reports into a single JSON artifact per commit.
Called by pipeline-full.yml.

Output schema:
{
  "commit": "abc123",
  "ref": "main",
  "gate": "PASS|FAIL",
  "failures": [...],
  "qse": { "gate": ..., "qse4": ..., "threshold": ..., "failures": [...], "defects": {...} },
  "analyzers": { "gate": ..., "pylint_score": ..., "radon_cc_avg": ..., ... },
  "tests": { "gate": ..., "coverage_line": ..., "tests_passed": ..., "tests_failed": ... }
}
"""

import argparse
import json
import sys
from pathlib import Path


ANALYZER_THRESHOLDS = {
    "pylint_score":    (">=", 7.0),
    "radon_cc_avg":    ("<=", 10.0),
    "radon_mi_avg":    (">=", 50.0),
    "coverage_line":   (">=", 70.0),
    "bandit_high":     ("==", 0),
    "ruff_violations": ("<=", 20),
}


def _load(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def _load_list(path: str) -> list:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _qse_section(path: str) -> dict:
    data = _load(path)
    if not data:
        return {"gate": "ERROR", "qse4": 0.0, "threshold": 0.80,
                "failures": ["QSE report missing"], "defects": {}}
    rep = data.get("report", {})
    return {
        "gate":      data.get("gate", "ERROR"),
        "qse4":      data.get("qse4", 0.0),
        "qse_test":  rep.get("qse_test", 0.0),
        "qse_combined": rep.get("qse_combined", 0.0),
        "threshold": data.get("threshold", 0.80),
        "failures":  data.get("failures", []),
        "defects":   rep.get("defects", {}),
        "metrics":   rep.get("metrics", {}),
    }


def _pylint_score(data: list) -> float:
    violations = len([m for m in data if m.get("type") in ("error", "warning", "convention")])
    return round(max(0.0, 10.0 - violations / 10.0), 2)


def _radon_cc_avg(data: dict) -> float:
    complexities = [
        item["complexity"]
        for file_results in data.values()
        for item in file_results
        if isinstance(item, dict) and "complexity" in item
    ]
    return round(sum(complexities) / len(complexities), 2) if complexities else 0.0


def _radon_mi_avg(data: dict) -> float:
    mis = [v["mi"] for v in data.values() if isinstance(v, dict) and "mi" in v]
    return round(sum(mis) / len(mis), 2) if mis else 0.0


def _check(name: str, value: float) -> bool:
    op, threshold = ANALYZER_THRESHOLDS[name]
    if op == ">=":   return value >= threshold
    if op == "<=":   return value <= threshold
    if op == "==":   return value == threshold
    return False


def _analyzers_section(args) -> dict:
    ruff_data    = _load_list(args.ruff)
    radon_cc     = _load(args.radon_cc)
    radon_mi     = _load(args.radon_mi)
    pylint_data  = _load_list(args.pylint)
    coverage     = _load(args.coverage)
    bandit       = _load(args.bandit)

    metrics = {
        "pylint_score":    _pylint_score(pylint_data),
        "radon_cc_avg":    _radon_cc_avg(radon_cc),
        "radon_mi_avg":    _radon_mi_avg(radon_mi),
        "coverage_line":   round(coverage.get("totals", {}).get("percent_covered", 0.0), 2),
        "bandit_high":     sum(1 for r in bandit.get("results", [])
                               if r.get("issue_severity") == "HIGH"),
        "ruff_violations": len(ruff_data),
    }

    failures = [
        f"{name}={v} (required: {ANALYZER_THRESHOLDS[name][0]}{ANALYZER_THRESHOLDS[name][1]})"
        for name, v in metrics.items()
        if not _check(name, v)
    ]

    return {
        "gate": "PASS" if not failures else "FAIL",
        "failures": failures,
        **metrics,
    }


def _tests_section(args) -> dict:
    pytest_data = _load(args.pytest)
    coverage    = _load(args.coverage)

    summary = pytest_data.get("summary", {})
    passed  = summary.get("passed", 0)
    failed  = summary.get("failed", 0)
    errors  = summary.get("errors", 0)
    total   = summary.get("total", 0)

    cov_line = round(coverage.get("totals", {}).get("percent_covered", 0.0), 2)
    cov_branch = round(coverage.get("totals", {}).get("percent_covered_display",
                  coverage.get("totals", {}).get("percent_covered", 0.0)), 2)

    failures = []
    if failed > 0 or errors > 0:
        failures.append(f"{failed} test(s) failed, {errors} error(s)")
    if cov_line < 70.0:
        failures.append(f"coverage={cov_line}% below 70%")

    return {
        "gate":          "PASS" if not failures else "FAIL",
        "failures":      failures,
        "tests_passed":  passed,
        "tests_failed":  failed,
        "tests_errors":  errors,
        "tests_total":   total,
        "coverage_line": cov_line,
        "coverage_branch": cov_branch,
    }


def main():
    p = argparse.ArgumentParser(description="Aggregate full quality report")
    p.add_argument("--qse",       required=True)
    p.add_argument("--ruff",      required=True)
    p.add_argument("--radon-cc",  required=True)
    p.add_argument("--radon-mi",  required=True)
    p.add_argument("--pylint",    required=True)
    p.add_argument("--coverage",  required=True)
    p.add_argument("--bandit",    required=True)
    p.add_argument("--pytest",    required=True)
    p.add_argument("--commit",    default="unknown")
    p.add_argument("--ref",       default="unknown")
    p.add_argument("--output",    required=True)
    args = p.parse_args()

    qse       = _qse_section(args.qse)
    analyzers = _analyzers_section(args)
    tests     = _tests_section(args)

    all_failures = (
        [f"[QSE] {f}"       for f in qse["failures"]] +
        [f"[analyzers] {f}" for f in analyzers["failures"]] +
        [f"[tests] {f}"     for f in tests["failures"]]
    )

    result = {
        "commit":    args.commit,
        "ref":       args.ref,
        "gate":      "PASS" if not all_failures else "FAIL",
        "failures":  all_failures,
        "qse":       qse,
        "analyzers": analyzers,
        "tests":     tests,
    }

    Path(args.output).write_text(json.dumps(result, indent=2))

    gate = result["gate"]
    print(f"Full quality gate: {gate}")
    for f in all_failures:
        print(f"  ✗ {f}")
    sys.exit(0 if gate == "PASS" else 1)


if __name__ == "__main__":
    main()
