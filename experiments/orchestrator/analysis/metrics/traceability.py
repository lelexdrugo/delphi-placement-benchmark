"""Traceability records joining intent + score map + reason + outcome.

Powers proposal/06's "Traceability Examples" subsection. iter-3b's
decision-maker persists clusters_score and reason for every completed
request, so the analyzer can render representative decision traces
without re-querying live state.
"""
from __future__ import annotations

import json
from typing import Any

from ..models import AnalysisContext, MetricResult, RunArtifacts


def _normalise_score_map(raw: Any) -> dict[str, float]:
    """clusters_score arrives as a dict from psycopg's JSONB decode or a
    string when the row is loaded from a stub fixture. Tolerate both.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items()}
    return {}


def compute(art: RunArtifacts, _ctx: AnalysisContext) -> MetricResult:
    rows: list[dict[str, Any]] = []
    for j in art.foreground_jobs:
        score_map = _normalise_score_map(j.db_clusters_score)
        score_blob = ";".join(
            f"{k}={v:.2f}"
            for k, v in sorted(score_map.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        rows.append({
            "job_name": j.name,
            "intent_profile": j.intent_profile,
            "workload_class": j.workload_class,
            "state_id": j.state_id,
            "selected_cluster": j.selected_cluster or "",
            "completed": int(j.completed),
            "runtime_seconds": j.runtime_seconds if j.runtime_seconds is not None else "",
            "decision_latency_ms": j.decision_latency_ms if j.decision_latency_ms is not None else "",
            "clusters_score": score_blob,
            "reason": (j.db_reason or "").replace("\n", " ").strip(),
        })

    aggregate = {
        "n_rows": len(rows),
        "n_with_reason": sum(1 for r in rows if r["reason"]),
        "n_with_score_map": sum(1 for r in rows if r["clusters_score"]),
    }
    return MetricResult(
        name="traceability",
        rows=tuple(rows),
        aggregate=aggregate,
        status="ok" if rows else "insufficient_data",
    )
