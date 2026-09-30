"""reduce_cells: every value is the median over the runs of a cell."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from experiments.orchestrator.analysis import reduce_cells as rc


def _run(root: Path, name: str, passes: list[int], waits: list[float],
         cordon: float | None = None) -> Path:
    rd = root / name
    a = rd / "analysis"
    a.mkdir(parents=True)
    n = len(passes)
    proj = {"intent_compliance": {"hard_pass_rate": sum(passes) / n,
                                  "hard_pass_rate_completion_aware": sum(passes) / 40,
                                  "completion_rate": n / 40},
            "completion_decomposition": {"median_wait_s": sorted(waits)[len(waits) // 2]}}
    if cordon is not None:
        proj["cordon_response"] = {"during_cordon_strict_fraction_targeting_cordoned": cordon}
    (a / "summary.json").write_text(json.dumps({"baselines": {"delphi-full": proj}}), encoding="utf-8")
    with (a / "intent-compliance.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["job_name", "hard_pass"])
        for i, p in enumerate(passes):
            w.writerow([f"j{i}", p])
    with (a / "completion-decomposition.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["job_name", "time_to_pod_spawn_s"])
        for i, t in enumerate(waits):
            w.writerow([f"j{i}", t])
    return rd


def test_median_over_runs_differs_from_pooling_jobs(tmp_path: Path):
    # Run sizes differ, so pooling jobs weights the big run: pooled 11/20 = 0.55,
    # per-run values 0.5, 0.8, 0.4 -> median 0.5.
    runs = [_run(tmp_path, "a", [1] * 5 + [0] * 5, [10.0] * 10),
            _run(tmp_path, "b", [1] * 4 + [0] * 1, [30.0] * 5),
            _run(tmp_path, "c", [1] * 2 + [0] * 3, [20.0] * 5)]
    r = rc.reduce_values(rc.per_run_values(runs, "hard_pass_rate"))
    assert (r["median"], r["min"], r["max"], r["n_runs"]) == (0.5, 0.4, 0.8, 3)
    assert rc.pooled_hard_pass_completed_only(runs) == 11 / 20
    assert r["median"] != rc.pooled_hard_pass_completed_only(runs)
    w = rc.reduce_values(rc.per_run_values(runs, "median_time_to_pod_spawn_s"))
    assert w["median"] == 20.0 and rc.pooled_median_time_to_pod_spawn(runs) == 15.0


def test_a_run_without_a_value_blocks_the_median(tmp_path: Path):
    r = rc.reduce_values([0.5, None, 0.7])
    assert r["median"] is None and r["missing"] == 1 and r["n_runs"] == 3


def test_a_metric_absent_from_every_run_is_not_applicable(tmp_path: Path):
    runs = [_run(tmp_path, "a", [1, 0], [1.0, 2.0]), _run(tmp_path, "b", [1, 1], [1.0, 2.0])]
    r = rc.reduce_values(rc.per_run_values(runs, "cordon_strict_during_fraction"))
    assert r == {"n_runs": 2, "applicable": False, "missing": 0}


def test_cli_writes_reduction_and_flags_missing(tmp_path: Path):
    runs = [_run(tmp_path, "a", [1, 0], [1.0, 2.0], cordon=0.0),
            _run(tmp_path, "b", [1, 1], [1.0, 2.0], cordon=0.1)]
    cells = tmp_path / "cells.json"
    cells.write_text(json.dumps({"cor-df": [str(p) for p in runs]}), encoding="utf-8")
    out = tmp_path / "reduced.json"
    assert rc.main(["--cells", str(cells), "--out", str(out)]) == 0
    red = json.loads(out.read_text(encoding="utf-8"))
    assert red["cor-df"]["cordon_strict_during_fraction"]["median"] == 0.05
