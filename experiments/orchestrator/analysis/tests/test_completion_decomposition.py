"""iter-4d-0b: completion_time decomposition (wait + execution)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from experiments.orchestrator.analysis.metrics.completion_decomposition import compute
from experiments.orchestrator.analysis.models import (
    AnalysisContext,
    ClusterLabels,
    ClusterRegistry,
    JobRecord,
    RunArtifacts,
)

_T0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
_CTX = AnalysisContext(use_db=False, use_kube=False)


def _registry() -> ClusterRegistry:
    return ClusterRegistry(
        clusters={
            "edge-1": ClusterLabels(
                name="edge-1", role="edge", arch="arm64",
                cost="low", rtt="low", cpu_class="low", mem_class="low",
            ),
            "public-cloud": ClusterLabels(
                name="public-cloud", role="public-cloud", arch="amd64",
                cost="high", rtt="medium", cpu_class="high", mem_class="medium",
            ),
        },
        intent_profiles={},
    )


def _job(name: str, *, applied: int, start: int | None, completion: int | None,
         selected: str | None, completed: bool = True) -> JobRecord:
    return JobRecord(
        name=name, role="foreground", state_id="idle",
        intent_profile="balanced", workload_class="cpu",
        applied=True, completed=completed,
        applied_at_utc=_T0 + timedelta(seconds=applied),
        k8s_start_time=(_T0 + timedelta(seconds=start)) if start is not None else None,
        k8s_completion_time=(_T0 + timedelta(seconds=completion)) if completion is not None else None,
        selected_cluster=selected,
    )


def _art(jobs: tuple[JobRecord, ...]) -> RunArtifacts:
    return RunArtifacts(
        run_id="run", run_dir=None, baseline="delphi-full",  # type: ignore[arg-type]
        layout="simple", submitted_at=_T0, finished_at=None,
        jobs=jobs, registry=_registry(),
    )


def test_splits_wait_and_execution_and_cost():
    jobs = (
        # wait 30, exec 60, edge-1 (cost low=1.0) -> cost_exec 60
        _job("a", applied=0, start=30, completion=90, selected="edge-1"),
        # wait 15, exec 100, public-cloud (cost high=3.0) -> cost_exec 300
        _job("b", applied=10, start=25, completion=125, selected="public-cloud"),
    )
    res = compute(_art(jobs), _CTX)

    assert res.status == "ok"
    by_name = {r["job_name"]: r for r in res.rows}
    assert by_name["a"]["time_to_pod_spawn_s"] == 30.0
    assert by_name["a"]["execution_runtime_s"] == 60.0
    assert by_name["b"]["time_to_pod_spawn_s"] == 15.0
    assert by_name["b"]["execution_runtime_s"] == 100.0

    agg = res.aggregate
    assert agg["n_observed"] == 2
    assert agg["median_wait_s"] == 22.5      # median(30, 15)
    assert agg["median_exec_s"] == 80.0      # median(60, 100)
    # Σ cost_value × execution_runtime: 1.0*60 + 3.0*100
    assert agg["cost_proxy_execution_based"] == 360.0
    assert agg["cost_proxy_execution_based_n"] == 2


def test_sum_matches_completion_time_by_construction():
    j = _job("a", applied=0, start=30, completion=90, selected="edge-1")
    res = compute(_art((j,)), _CTX)
    row = res.rows[0]
    # wait + exec == completion_time (runtime_seconds = completion - applied)
    assert row["time_to_pod_spawn_s"] + row["execution_runtime_s"] == j.runtime_seconds == 90.0


def test_pending_when_no_kube_timestamps():
    # Mirrors the synthetic fixtures (use_kube=False): no startTime.
    j = _job("a", applied=0, start=None, completion=None, selected="edge-1")
    res = compute(_art((j,)), _CTX)
    assert res.status == "pending"
    assert res.aggregate["n_observed"] == 0
    assert res.aggregate["n_skipped"] == 1
    assert res.aggregate["n_skipped_no_start"] == 1
    assert res.aggregate["cost_proxy_execution_based"] is None
    # the row is still emitted (with blank split cells) for traceability
    assert res.rows[0]["time_to_pod_spawn_s"] == ""


def test_not_completed_job_is_skipped():
    j = _job("a", applied=0, start=30, completion=90, selected="edge-1",
             completed=False)
    res = compute(_art((j,)), _CTX)
    assert res.status == "pending"
    assert res.aggregate["n_skipped_not_completed"] == 1
    assert res.aggregate["n_observed"] == 0


def test_cost_skipped_when_cluster_unknown():
    # selected cluster absent from registry -> exec still counted, cost not.
    j = _job("a", applied=0, start=10, completion=70, selected="mystery-cluster")
    res = compute(_art((j,)), _CTX)
    assert res.status == "ok"
    assert res.aggregate["n_observed"] == 1
    assert res.aggregate["cost_proxy_execution_based"] == 0.0
    assert res.aggregate["cost_proxy_execution_based_n"] == 0
