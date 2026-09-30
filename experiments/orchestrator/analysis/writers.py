"""Deterministic CSV + summary.json emission.

Idempotency contract:
    re-running the analyzer on the same RunArtifacts produces a
    byte-identical analysis/ directory.

Mechanisms:
    - rows sorted by (state_id, workload_class, intent_profile, job_name)
      before write,
    - CSV column order pulled from a module-level tuple per metric
      (NOT csv.DictWriter's natural dict-key iteration),
    - json.dumps(sort_keys=True, indent=2, ensure_ascii=False).
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import MetricResult, Summary


# Per-metric stable column orders. Adding a new column means updating
# both the metric module AND this tuple — the test_runner_integration
# fixture catches drift.
_COLUMNS: dict[str, tuple[str, ...]] = {
    "placement_success": (
        "state_id", "workload_class", "intent_profile",
        "applied", "completed", "failed", "success_rate",
    ),
    "completion_time": (
        "job_name", "state_id", "workload_class", "intent_profile",
        "applied_at_utc", "k8s_completion_time", "selected_cluster",
        "duration_seconds",
    ),
    "completion_decomposition": (
        "job_name", "state_id", "workload_class", "intent_profile",
        "applied_at_utc", "k8s_start_time", "k8s_completion_time",
        "selected_cluster", "time_to_pod_spawn_s", "execution_runtime_s",
    ),
    "decision_latency": (
        "job_name", "state_id", "workload_class", "intent_profile",
        "db_status", "db_kind",
        "created_at", "last_attempt_at", "latency_ms",
    ),
    "admission_overhead": (
        "pair_id", "optimized_start_offset_s",
        "non_optimized_start_offset_s", "delta_s",
    ),
    "telemetry_overhead": (
        "pair_id", "with_sidecar_runtime_s",
        "no_sidecar_runtime_s", "delta_s",
    ),
    "intent_compliance": (
        "job_name", "intent_profile", "selected_cluster", "cluster_role",
        "cluster_rtt_label", "cluster_cost_label",
        "req_region", "req_latency", "req_cost",
        "preferred_classes",
        "hard_pass", "soft_score",
    ),
    "cost_proxy": (
        "job_name", "selected_cluster", "cluster_cost_label",
        "cost_value", "runtime_seconds", "cost_score",
    ),
    "data_movement": (
        "job_name", "intent_profile", "req_region",
        "selected_cluster", "cluster_role",
        "movement_class", "movement_score",
    ),
    "traceability": (
        "job_name", "intent_profile", "workload_class", "state_id",
        "selected_cluster", "completed",
        "runtime_seconds", "decision_latency_ms",
        "clusters_score", "reason",
    ),
    # iter-4h-4: only emitted for runs carrying cordon-timestamps.yaml
    # (the runner gates the metric in), so non-cordon runs are unaffected.
    "cordon_response": (
        "job_name", "intent_profile", "workload_class", "state_id",
        "applied_at_utc", "window_phase",
        "pp_created_at", "decision_phase", "decided_during_cordon",
        "selected_cluster", "cordon_target", "targets_cordoned", "completed",
    ),
}

_NO_POLLUTION_COLUMNS = (
    "leak_direction", "id", "job_name", "kind", "status", "created_at",
)


def _sort_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("state_id", "")),
        str(row.get("workload_class", "")),
        str(row.get("intent_profile", "")),
        str(row.get("job_name", "")),
    )


def _format_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        # Stable formatting; specific metric modules can format their
        # own values upstream if a different precision is wanted.
        return f"{v:.6g}"
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        for row in sorted(rows, key=_sort_key):
            writer.writerow([_format_cell(row.get(c, "")) for c in columns])


def write_metric_csvs(
    analysis_dir: Path,
    results: dict[str, MetricResult],
    no_pollution_rows: list[dict[str, Any]],
) -> dict[str, Path]:
    """Write one CSV per metric plus no-pollution.csv. Returns the
    path map for the summary.json's `csv_paths` section.
    """
    paths: dict[str, Path] = {}
    for name, result in results.items():
        cols = _COLUMNS.get(name)
        if cols is None:
            continue
        out = analysis_dir / f"{name.replace('_', '-')}.csv"
        _write_csv(out, cols, list(result.rows))
        paths[name] = out

    np_path = analysis_dir / "no-pollution.csv"
    _write_csv(np_path, _NO_POLLUTION_COLUMNS, list(no_pollution_rows))
    paths["no_pollution"] = np_path
    return paths


def _summary_to_dict(summary: Summary) -> dict[str, Any]:
    return {
        "run_id": summary.run_id,
        "baseline": summary.baseline,
        "no_pollution_ok": summary.no_pollution_ok,
        "join_coverage_ok": summary.join_coverage_ok,
        "submitted_at": summary.submitted_at.isoformat() if summary.submitted_at else None,
        "finished_at": summary.finished_at.isoformat() if summary.finished_at else None,
        "baselines": summary.baselines,
        "notes": list(summary.notes),
    }


def write_summary(analysis_dir: Path, summary: Summary) -> Path:
    analysis_dir.mkdir(parents=True, exist_ok=True)
    out = analysis_dir / "summary.json"
    out.write_text(
        json.dumps(_summary_to_dict(summary), sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out
