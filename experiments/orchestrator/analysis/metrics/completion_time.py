"""End-to-end completion time per foreground job.

duration = k8s_completion_time - applied_at_utc, where:
- applied_at_utc comes from submission-log.csv (iter-4a's submit.py extension);
- k8s_completion_time comes from kubectl get job -o json on
  unique-logical-entrypoint (Karmada mirrors member status into the
  aggregator), via kube.get_job + kube.job_completion_time.

Reports median / p95 / max over completed jobs. Never reports mean
(the harness's noise discipline).
"""
from __future__ import annotations

from statistics import median
from typing import Any

from ..models import AnalysisContext, MetricResult, RunArtifacts


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    k = (len(values) - 1) * p
    f = int(k)
    c = min(f + 1, len(values) - 1)
    return values[f] + (values[c] - values[f]) * (k - f)


def compute(art: RunArtifacts, _ctx: AnalysisContext) -> MetricResult:
    rows: list[dict[str, Any]] = []
    durations: list[float] = []
    notes: list[str] = []

    for j in art.foreground_jobs:
        d = j.runtime_seconds
        rows.append({
            "job_name": j.name,
            "state_id": j.state_id,
            "workload_class": j.workload_class,
            "intent_profile": j.intent_profile,
            "applied_at_utc": j.applied_at_utc.isoformat() if j.applied_at_utc else "",
            "k8s_completion_time": j.k8s_completion_time.isoformat() if j.k8s_completion_time else "",
            "selected_cluster": j.selected_cluster or "",
            "duration_seconds": d if d is not None else "",
        })
        if d is not None and j.completed:
            durations.append(d)

    if not durations:
        return MetricResult(
            name="completion_time",
            rows=tuple(rows),
            aggregate={"median_s": None, "p95_s": None, "max_s": None, "n": 0},
            status="insufficient_data",
            note="no completed foreground jobs with both timestamps",
        )

    med = median(durations)
    p95 = _percentile(durations, 0.95)
    mx = max(durations)
    aggregate = {
        "median_s": med,
        "p95_s": p95,
        "max_s": mx,
        "n": len(durations),
        "formatted": f"median={med:.1f}s / p95={p95:.1f}s / max={mx:.1f}s",
    }
    return MetricResult(
        name="completion_time",
        rows=tuple(rows),
        aggregate=aggregate,
        status="ok",
        note="; ".join(notes),
    )
