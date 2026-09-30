"""Data-movement proxy — offline classification.

(intent.region × cluster.role) → {local:0, near:1, far:2, n/a:null}

This is paper-defined and stays in code so a unit test can pin the
table.
"""
from __future__ import annotations

from typing import Any, Optional

from ..models import AnalysisContext, ClusterLabels, IntentProfile, MetricResult, RunArtifacts


def _classify(req_region: str, cluster_role: str) -> tuple[str, Optional[float]]:
    """Return (class, score).

    Score is None when class is 'n/a' (request region was 'any' / empty).
    """
    rr = req_region.strip().lower()
    cr = cluster_role.strip().lower()
    if rr in ("", "any"):
        return "n/a", None
    if rr == cr:
        return "local", 0.0
    # Adjacency: edge ↔ on-premises and public-cloud ↔ on-premises are "near".
    near_pairs = {
        ("edge", "on-premises"), ("on-premises", "edge"),
        ("public-cloud", "on-premises"), ("on-premises", "public-cloud"),
    }
    if (rr, cr) in near_pairs:
        return "near", 1.0
    return "far", 2.0


def compute(art: RunArtifacts, _ctx: AnalysisContext) -> MetricResult:
    rows: list[dict[str, Any]] = []
    scores: list[float] = []
    histogram: dict[str, int] = {"local": 0, "near": 0, "far": 0, "n/a": 0}

    for j in art.foreground_jobs:
        if not j.completed or not j.selected_cluster or not j.intent_profile:
            continue
        cluster: ClusterLabels | None = art.registry.clusters.get(j.selected_cluster)
        profile: IntentProfile | None = art.registry.intent_profiles.get(j.intent_profile)
        if cluster is None or profile is None:
            continue
        req_region = str(profile.propagation_requirements.get("region", "any"))
        klass, score = _classify(req_region, cluster.role)
        histogram[klass] += 1
        if score is not None:
            scores.append(score)
        rows.append({
            "job_name": j.name,
            "intent_profile": j.intent_profile,
            "req_region": req_region,
            "selected_cluster": j.selected_cluster,
            "cluster_role": cluster.role,
            "movement_class": klass,
            "movement_score": score if score is not None else "",
        })

    mean_score = (sum(scores) / len(scores)) if scores else None
    aggregate: dict[str, Any] = {
        "mean_score": mean_score,
        "n_scored": len(scores),
        "n_n_a": histogram["n/a"],
        "histogram_local": histogram["local"],
        "histogram_near": histogram["near"],
        "histogram_far": histogram["far"],
        "formatted": (
            f"mean={mean_score:.2f} (n={len(scores)})"
            if mean_score is not None
            else "n/a"
        ),
    }
    status = "ok" if rows else "insufficient_data"
    return MetricResult(
        name="data_movement",
        rows=tuple(rows),
        aggregate=aggregate,
        status=status,
    )
