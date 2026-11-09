"""Placement success rate per (state, workload_class, intent_profile)."""
from __future__ import annotations

from typing import Any

from ..models import AnalysisContext, MetricResult, RunArtifacts


def compute(art: RunArtifacts, _ctx: AnalysisContext) -> MetricResult:
    rows: list[dict[str, Any]] = []
    bucket: dict[tuple[str, str, str], dict[str, int]] = {}

    fg = art.foreground_jobs
    for j in fg:
        key = (j.state_id, j.workload_class, j.intent_profile)
        cell = bucket.setdefault(key, {"applied": 0, "completed": 0, "failed": 0})
        if j.applied:
            cell["applied"] += 1
        if j.completed:
            cell["completed"] += 1
        if j.failed:
            cell["failed"] += 1

    for (state_id, wc, ip), c in sorted(bucket.items()):
        applied = c["applied"]
        completed = c["completed"]
        rate = (completed / applied) if applied > 0 else 0.0
        rows.append({
            "state_id": state_id,
            "workload_class": wc,
            "intent_profile": ip,
            "applied": applied,
            "completed": completed,
            "failed": c["failed"],
            "success_rate": rate,
        })

    total_applied = sum(c["applied"] for c in bucket.values())
    total_completed = sum(c["completed"] for c in bucket.values())
    overall = (total_completed / total_applied) if total_applied > 0 else 0.0
    aggregate = {
        "value": overall,
        "n_applied": total_applied,
        "n_completed": total_completed,
    }
    return MetricResult(
        name="placement_success",
        rows=tuple(rows),
        aggregate=aggregate,
        status="ok" if total_applied > 0 else "insufficient_data",
    )
