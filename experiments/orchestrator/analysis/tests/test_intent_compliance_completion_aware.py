"""Completion-aware intent-compliance aggregates.

Synthetic edge cases on hand-built ``JobRecord``s: all completed, none
completed, partial completion, background exclusion, and a
completed-but-unscorable job.

A second layer replays measured run-dirs through the real loader,
metric, runner and writers. Those run-dirs are not published with this
repository, so that layer is not included here either; see the README.

No test here touches a cluster or a database.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from experiments.orchestrator.analysis.metrics.intent_compliance import compute
from experiments.orchestrator.analysis.models import (
    AnalysisContext,
    ClusterLabels,
    ClusterRegistry,
    IntentProfile,
    JobRecord,
    RunArtifacts,
)


_CTX = AnalysisContext(use_db=False, use_kube=False)

# Keys the completed-only aggregate carried before this change. Their
# values must not move.
_LEGACY_KEYS = ("hard_pass_rate", "soft_score_mean", "n", "formatted")
_NEW_KEYS = (
    "hard_pass_rate_completion_aware",
    "soft_score_mean_completion_aware",
    "n_submitted",
    "n_completed",
    "completion_rate",
)


# ---------------------------------------------------------------------------
# Synthetic edge cases
# ---------------------------------------------------------------------------

def _registry() -> ClusterRegistry:
    clusters = {
        "edge-1": ClusterLabels("edge-1", "edge", "arm64", "low", "low", "low", "low"),
        "public-cloud": ClusterLabels(
            "public-cloud", "public-cloud", "amd64", "high", "medium", "high", "medium",
        ),
    }
    profiles = {
        # edge-1 → hard 1, soft 1.0; public-cloud → hard 0, soft 0.0
        "latency-sensitive": IntentProfile(
            "latency-sensitive",
            {"region": "edge", "latency": "low", "cost": "any"},
            ("edge",),
        ),
        # edge-1 → hard 1, soft 0.5; public-cloud → hard 0, soft 0.0
        "cost-aware": IntentProfile(
            "cost-aware",
            {"region": "any", "latency": "any", "cost": "low"},
            ("on-premises", "edge"),
        ),
    }
    return ClusterRegistry(clusters=clusters, intent_profiles=profiles)


def _job(name: str, *, completed: bool, cluster: str | None = "edge-1",
         profile: str = "latency-sensitive", role: str = "foreground") -> JobRecord:
    return JobRecord(
        name=name, role=role, state_id="", intent_profile=profile,
        workload_class="cpu", applied=True, completed=completed,
        selected_cluster=cluster,
    )


def _art(jobs: list[JobRecord]) -> RunArtifacts:
    return RunArtifacts(
        run_id="synthetic", run_dir=Path("."), baseline="delphi-full",
        layout="simple", submitted_at=None, finished_at=None,
        jobs=tuple(jobs), registry=_registry(),
    )


def test_all_completed_aggregates_coincide():
    res = compute(_art([
        _job("fg-0", completed=True, cluster="edge-1"),
        _job("fg-1", completed=True, cluster="public-cloud"),
        _job("fg-2", completed=True, cluster="edge-1", profile="cost-aware"),
        _job("fg-3", completed=True, cluster="edge-1"),
    ]), _CTX)
    agg = res.aggregate
    assert res.status == "ok"
    assert agg["n_submitted"] == agg["n_completed"] == agg["n"] == 4
    assert agg["completion_rate"] == pytest.approx(1.0)
    assert agg["hard_pass_rate"] == pytest.approx(3 / 4)
    assert agg["hard_pass_rate_completion_aware"] == pytest.approx(agg["hard_pass_rate"])
    assert agg["soft_score_mean"] == pytest.approx(2.5 / 4)
    assert agg["soft_score_mean_completion_aware"] == pytest.approx(agg["soft_score_mean"])


def test_none_completed_completion_aware_is_zero_legacy_unchanged():
    res = compute(_art([_job(f"fg-{i}", completed=False) for i in range(5)]), _CTX)
    agg = res.aggregate
    # Completed-only keeps its pre-existing behaviour exactly.
    assert res.status == "insufficient_data"
    assert res.rows == ()
    assert {k: agg[k] for k in _LEGACY_KEYS} == {
        "hard_pass_rate": None, "soft_score_mean": None, "n": 0, "formatted": "n/a",
    }
    # Completion-aware is a defined 0.0, not None / NaN.
    assert agg["hard_pass_rate_completion_aware"] == 0.0
    assert agg["soft_score_mean_completion_aware"] == 0.0
    assert agg["n_submitted"] == 5
    assert agg["n_completed"] == 0
    assert agg["completion_rate"] == 0.0


def test_partial_completion_penalises_unfinished_jobs():
    res = compute(_art([
        _job("fg-0", completed=True, cluster="edge-1"),        # hard 1, soft 1.0
        _job("fg-1", completed=True, cluster="public-cloud"),  # hard 0, soft 0.0
        _job("fg-2", completed=False),
        _job("fg-3", completed=False),
    ]), _CTX)
    agg = res.aggregate
    assert agg["hard_pass_rate"] == pytest.approx(0.5)
    assert agg["soft_score_mean"] == pytest.approx(0.5)
    assert agg["n"] == 2
    assert agg["hard_pass_rate_completion_aware"] == pytest.approx(0.25)
    assert agg["soft_score_mean_completion_aware"] == pytest.approx(0.25)
    assert (agg["n_submitted"], agg["n_completed"]) == (4, 2)
    assert agg["completion_rate"] == pytest.approx(0.5)
    # Per-job CSV rows stay completed-only.
    assert sorted(r["job_name"] for r in res.rows) == ["fg-0", "fg-1"]


def test_background_jobs_excluded_from_both_aggregates():
    res = compute(_art([
        _job("fg-0", completed=True, cluster="edge-1"),
        _job("fg-1", completed=False),
        # Background jobs that would pass if they were counted.
        _job("bg-0", completed=True, cluster="edge-1", role="background"),
        _job("bg-1", completed=True, cluster="edge-1", role="background"),
        _job("bg-2", completed=False, role="background"),
    ]), _CTX)
    agg = res.aggregate
    assert (agg["n_submitted"], agg["n_completed"], agg["n"]) == (2, 1, 1)
    assert agg["hard_pass_rate"] == pytest.approx(1.0)
    assert agg["hard_pass_rate_completion_aware"] == pytest.approx(0.5)
    assert agg["soft_score_mean_completion_aware"] == pytest.approx(0.5)
    assert [r["job_name"] for r in res.rows] == ["fg-0"]


def test_completed_but_unscorable_job_makes_completion_aware_unknown():
    """A completed job without a selected_cluster (offline --no-kube pass,
    or a garbage-collected PropagationPolicy) is a data gap, not a
    policy failure: the completion-aware pair is None rather than a
    silently deflated number."""
    res = compute(_art([
        _job("fg-0", completed=True, cluster="edge-1"),
        _job("fg-1", completed=True, cluster=None),
        _job("fg-2", completed=False),
    ]), _CTX)
    agg = res.aggregate
    assert agg["hard_pass_rate"] == pytest.approx(1.0)
    assert agg["n"] == 1
    assert (agg["n_submitted"], agg["n_completed"]) == (3, 2)
    assert agg["hard_pass_rate_completion_aware"] is None
    assert agg["soft_score_mean_completion_aware"] is None
    assert "skipped fg-1" in res.note


def test_no_foreground_jobs():
    res = compute(_art([]), _CTX)
    agg = res.aggregate
    assert res.status == "insufficient_data"
    assert agg["hard_pass_rate"] is None
    assert (agg["n_submitted"], agg["n_completed"]) == (0, 0)
    assert agg["hard_pass_rate_completion_aware"] == 0.0
    assert agg["completion_rate"] == 0.0
