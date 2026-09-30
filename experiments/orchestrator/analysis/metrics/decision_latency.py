"""Decision latency: created_at -> last_attempt_at on COMPLETED foreground rows.

Driven entirely by db.run_query('decision_latency_foreground', names).
The query mirrors the production filter
(status='COMPLETED' AND kind='foreground') at decision_repository.go.
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
    latencies_ms: list[float] = []

    for j in art.foreground_jobs:
        lat = j.decision_latency_ms
        rows.append({
            "job_name": j.name,
            "state_id": j.state_id,
            "workload_class": j.workload_class,
            "intent_profile": j.intent_profile,
            "db_status": j.db_status,
            "db_kind": j.db_kind,
            "created_at": j.db_created_at.isoformat() if j.db_created_at else "",
            "last_attempt_at": j.db_last_attempt_at.isoformat() if j.db_last_attempt_at else "",
            "latency_ms": lat if lat is not None else "",
        })
        if lat is not None and j.db_status == "COMPLETED":
            latencies_ms.append(lat)

    if not latencies_ms:
        return MetricResult(
            name="decision_latency",
            rows=tuple(rows),
            aggregate={"median_ms": None, "p95_ms": None, "max_ms": None, "n": 0},
            status="insufficient_data",
            note="no COMPLETED foreground DB rows (DB unreachable or rows missing?)",
        )

    med = median(latencies_ms)
    p95 = _percentile(latencies_ms, 0.95)
    mx = max(latencies_ms)
    return MetricResult(
        name="decision_latency",
        rows=tuple(rows),
        aggregate={
            "median_ms": med,
            "p95_ms": p95,
            "max_ms": mx,
            "n": len(latencies_ms),
            "formatted": f"median={med:.1f}ms / p95={p95:.1f}ms / max={mx:.1f}ms",
        },
        status="ok",
    )
