"""Traceability metric — joins clusters_score + reason from DB."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from experiments.orchestrator.analysis.metrics.traceability import compute
from experiments.orchestrator.analysis.models import (
    AnalysisContext, ClusterLabels, ClusterRegistry, IntentProfile,
    JobRecord, RunArtifacts,
)


def _registry() -> ClusterRegistry:
    return ClusterRegistry(
        clusters={
            "edge-1": ClusterLabels(
                name="edge-1", role="edge", arch="arm64",
                cost="low", rtt="low",
                cpu_class="low", mem_class="low",
            ),
        },
        intent_profiles={
            "latency-sensitive": IntentProfile(
                name="latency-sensitive",
                propagation_requirements={
                    "region": "edge", "latency": "low", "cost": "any",
                },
                preferred_classes=("edge",),
            ),
        },
    )


def _job(name: str = "fg-1") -> JobRecord:
    t0 = datetime(2026, 5, 23, 18, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 23, 18, 0, 32, tzinfo=timezone.utc)
    return JobRecord(
        name=name,
        role="foreground",
        state_id="idle",
        intent_profile="latency-sensitive",
        workload_class="cpu",
        applied=True,
        completed=True,
        applied_at_utc=t0,
        k8s_completion_time=t1,
        selected_cluster="edge-1",
        db_request_id="req-1",
        db_status="COMPLETED",
        db_kind="foreground",
        db_created_at=t0,
        db_last_attempt_at=t0,
        db_clusters_score={"edge-1": 1.0, "edge-2": 0.5, "public-cloud": 0.0},
        db_reason="heuristic: edge-1 selected (score=1.000)",
    )


def _artifacts(jobs: list[JobRecord]) -> RunArtifacts:
    from pathlib import Path
    return RunArtifacts(
        run_id="r",
        run_dir=Path("/tmp/r"),
        baseline="heuristic-scorer",
        layout="simple",
        submitted_at=None,
        finished_at=None,
        jobs=tuple(jobs),
        registry=_registry(),
    )


def test_traceability_emits_score_blob_sorted_descending():
    art = _artifacts([_job()])
    result = compute(art, AnalysisContext())
    assert result.status == "ok"
    assert len(result.rows) == 1
    row = result.rows[0]
    # Sorted by descending score, then ascending cluster name (stable lex).
    assert row["clusters_score"].startswith("edge-1=1.00;"), row["clusters_score"]
    assert "public-cloud=0.00" in row["clusters_score"]
    assert row["reason"].startswith("heuristic: edge-1 selected")
    assert int(row["completed"]) == 1


def test_traceability_handles_string_jsonb():
    """psycopg may return clusters_score as a parsed dict; a fixture
    DB may return it as a JSON string. Both must be tolerated.
    """
    j = _job()
    j = replace(j, db_clusters_score='{"edge-1": 1.0, "on-prem": 0.5}')
    art = _artifacts([j])
    result = compute(art, AnalysisContext())
    assert "edge-1=1.00" in result.rows[0]["clusters_score"]
    assert "on-prem=0.50" in result.rows[0]["clusters_score"]


def test_traceability_handles_missing_score_map():
    j = _job()
    j = replace(j, db_clusters_score=None, db_reason=None)
    art = _artifacts([j])
    result = compute(art, AnalysisContext())
    assert result.rows[0]["clusters_score"] == ""
    assert result.rows[0]["reason"] == ""
    assert result.aggregate["n_with_score_map"] == 0
    assert result.aggregate["n_with_reason"] == 0
