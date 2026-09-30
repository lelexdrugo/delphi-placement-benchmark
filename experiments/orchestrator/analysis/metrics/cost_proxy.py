"""Cost proxy = sum_j cost_value[cluster.cost] × runtime_seconds(j).

Mapping kept inside this module so a unit test pins it. Documented in
the paper as policy normalization, NOT a monetary claim.
"""
from __future__ import annotations

from typing import Any

from ..models import AnalysisContext, MetricResult, RunArtifacts


COST_VALUE = {"low": 1.0, "medium": 2.0, "high": 3.0}


def compute(art: RunArtifacts, _ctx: AnalysisContext) -> MetricResult:
    rows: list[dict[str, Any]] = []
    total = 0.0
    n = 0
    notes: list[str] = []

    for j in art.foreground_jobs:
        if not j.completed or not j.selected_cluster:
            continue
        cluster = art.registry.clusters.get(j.selected_cluster)
        if cluster is None or cluster.cost not in COST_VALUE:
            notes.append(f"skipped {j.name}: unknown cluster.cost label")
            continue
        runtime = j.runtime_seconds
        if runtime is None:
            continue
        cv = COST_VALUE[cluster.cost]
        score = cv * runtime
        total += score
        n += 1
        rows.append({
            "job_name": j.name,
            "selected_cluster": j.selected_cluster,
            "cluster_cost_label": cluster.cost,
            "cost_value": cv,
            "runtime_seconds": runtime,
            "cost_score": score,
        })

    aggregate = {
        "total": total,
        "n": n,
        "value_map_low": COST_VALUE["low"],
        "value_map_medium": COST_VALUE["medium"],
        "value_map_high": COST_VALUE["high"],
    }
    status = "ok" if n > 0 else "insufficient_data"
    return MetricResult(
        name="cost_proxy",
        rows=tuple(rows),
        aggregate=aggregate,
        status=status,
        note="; ".join(notes),
    )
