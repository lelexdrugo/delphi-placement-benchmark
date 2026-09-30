"""Reduce repeated runs of a cell to median and min-max (Mid4CC).

A *cell* is one (policy x condition) combination; each cell is run k times.
Every reported value is the **median over the runs of a cell** of a per-run
value read from that run's ``analysis/summary.json``, together with the
**min-max over the same runs**. Jobs are never pooled across runs: jobs
within a run are not independent (the history query has no run-window
bound, so a job that completes early becomes history for later jobs of the
same run), so the run is the unit of repetition.

``pooled_*`` helpers exist only to *demonstrate* the difference on real
multi-run cells; nothing reported is computed from them.

Usage::

    python -m experiments.orchestrator.analysis.reduce_cells \
        --cells cells.json --out reduced.json

where ``cells.json`` maps a cell name to its run directories::

    {"eff-df": ["experiments/runs/<run-a>", "experiments/runs/<run-b>", ...], ...}
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import median
from typing import Any, Callable, Optional

# Per-run value extractors: (summary.json baseline projection) -> float | None.
PerRun = Callable[[dict[str, Any]], Optional[float]]

METRICS: dict[str, PerRun] = {
    "hard_pass_rate_completion_aware":
        lambda b: (b.get("intent_compliance") or {}).get("hard_pass_rate_completion_aware"),
    "hard_pass_rate":
        lambda b: (b.get("intent_compliance") or {}).get("hard_pass_rate"),
    "completion_rate":
        lambda b: (b.get("intent_compliance") or {}).get("completion_rate"),
    "median_time_to_pod_spawn_s":
        lambda b: (b.get("completion_decomposition") or {}).get("median_wait_s"),
    "cordon_strict_during_fraction":
        lambda b: (b.get("cordon_response") or {}).get(
            "during_cordon_strict_fraction_targeting_cordoned"),
}


def _projection(run_dir: Path) -> dict[str, Any]:
    summary = json.loads((run_dir / "analysis" / "summary.json").read_text(encoding="utf-8"))
    baselines = summary.get("baselines") or {}
    if len(baselines) != 1:
        raise ValueError(f"{run_dir}: expected one baseline projection, found {list(baselines)}")
    return next(iter(baselines.values()))


def per_run_values(run_dirs: list[Path], metric: str) -> list[Optional[float]]:
    fn = METRICS[metric]
    return [fn(_projection(Path(d))) for d in run_dirs]


def reduce_values(values: list[Optional[float]]) -> dict[str, Any]:
    """Median and min-max over per-run values. A missing value is a stop sign."""
    if not values:
        return {"n_runs": 0, "median": None, "min": None, "max": None, "missing": 0}
    present = [float(v) for v in values if v is not None]
    missing = len(values) - len(present)
    if not present:
        # The metric does not apply to this cell (e.g. cordon response
        # outside the cordon cells): absent from every run.
        return {"n_runs": len(values), "applicable": False, "missing": 0}
    if missing:
        # A run whose value is absent cannot be silently dropped from a
        # median of k: that would report k-1 runs as k.
        return {"n_runs": len(values), "median": None, "min": None, "max": None,
                "missing": missing, "per_run": values}
    return {"n_runs": len(values), "median": median(present), "min": min(present),
            "max": max(present), "missing": 0, "per_run": present}


def reduce_cells(cells: dict[str, list[str]], metrics: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cell, dirs in cells.items():
        paths = [Path(d) for d in dirs]
        out[cell] = {m: reduce_values(per_run_values(paths, m)) for m in metrics}
        out[cell]["run_dirs"] = [str(p) for p in paths]
    return out


# --- demonstration only: statistics over jobs pooled across runs ----------

def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def pooled_hard_pass_completed_only(run_dirs: list[Path]) -> Optional[float]:
    """hard_pass over the completed jobs of all runs taken together."""
    passes = total = 0
    for d in run_dirs:
        for r in _rows(Path(d) / "analysis" / "intent-compliance.csv"):
            v = str(r.get("hard_pass", "")).strip().lower()
            if v in ("true", "1", "1.0", "false", "0", "0.0"):
                total += 1
                passes += v in ("true", "1", "1.0")
    return passes / total if total else None


def pooled_median_time_to_pod_spawn(run_dirs: list[Path]) -> Optional[float]:
    waits: list[float] = []
    for d in run_dirs:
        path = Path(d) / "analysis" / "completion-decomposition.csv"
        if not path.exists():
            return None
        for r in _rows(path):
            try:
                waits.append(float(r["time_to_pod_spawn_s"]))
            except (KeyError, TypeError, ValueError):
                continue
    return median(waits) if waits else None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cells", required=True, help="JSON mapping cell -> list of run dirs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--metrics", default="hard_pass_rate_completion_aware,completion_rate,"
                                         "median_time_to_pod_spawn_s,cordon_strict_during_fraction")
    a = ap.parse_args(argv)
    metrics_arg = a.metrics or ""
    cells = json.loads(Path(a.cells).read_text(encoding="utf-8"))
    reduced = reduce_cells(cells, [m for m in metrics_arg.split(",") if m])
    Path(a.out).write_text(json.dumps(reduced, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    missing = [(c, m) for c, v in reduced.items() for m, r in v.items()
               if isinstance(r, dict) and r.get("missing")]
    if missing:
        sys.stderr.write(f"runs without a value (no median reported): {missing}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
