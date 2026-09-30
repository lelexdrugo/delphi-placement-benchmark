"""Population-sampler unit tests (iter-4h-2 § 5.4).

Pins the analysis/disturbance-population.csv contract: header order,
column count, value semantics for `n_active_or_pending` (Karmada
`Job.status.active` aggregated per target), and write-format under a
mocked kubectl-get-jobs response sequence.

The sampler is normally a thread; these tests poke at the
`_per_target_population` helper directly (synchronous) and verify the
CSV writing path end-to-end with a mocked `_kubectl_get_disturbance_jobs`.
"""
from __future__ import annotations

import csv
import threading
from pathlib import Path
from unittest import mock

from experiments.orchestrator.submit import (
    _per_target_population,
    _disturbance_population_sampler,
)


def _job(target: str, *, active: int = 0, succeeded: int = 0,
         failed: int = 0) -> dict:
    """Build a minimal Karmada-aggregated Job dict for the sampler."""
    return {
        "metadata": {
            "labels": {"delphi.experiments/pin-to-cluster": target}
        },
        "status": {
            "active": active,
            "succeeded": succeeded,
            "failed": failed,
        },
    }


# ---------------------------------------------------------------------------
# _per_target_population shape
# ---------------------------------------------------------------------------

def test_empty_input_returns_empty_dict() -> None:
    assert _per_target_population([]) == {}


def test_jobs_without_pin_to_cluster_label_are_ignored() -> None:
    jobs = [{"metadata": {"labels": {}}, "status": {"active": 1}}]
    assert _per_target_population(jobs) == {}


def test_jobs_aggregate_by_pin_to_cluster() -> None:
    jobs = [
        _job("on-prem", active=1),
        _job("on-prem", succeeded=1),
        _job("edge-1", active=2),
        _job("edge-1", failed=1),
    ]
    pop = _per_target_population(jobs)
    assert pop["on-prem"] == {
        "submitted": 2, "active_or_pending": 1,
        "completed": 1, "failed": 0,
    }
    assert pop["edge-1"] == {
        "submitted": 2, "active_or_pending": 2,
        "completed": 0, "failed": 1,
    }


def test_running_and_pending_are_indistinguishable_in_aggregate() -> None:
    """`.status.active` on the Karmada-aggregated Job is the count of
    pods that exist on the member — Running AND Pending combined.
    Karmada does NOT propagate per-pod status back to the
    `unique-logical-entrypoint` (verified empirically 2026-05-26 on
    this lab). The sampler MUST report `active_or_pending` not
    `active`."""
    jobs = [_job("on-prem", active=3)]  # 3 might be 3 Running, 0 Pending,
                                          # 0 Running + 3 Pending, or any mix.
    pop = _per_target_population(jobs)
    assert pop["on-prem"]["active_or_pending"] == 3


# ---------------------------------------------------------------------------
# CSV write contract
# ---------------------------------------------------------------------------

def test_sampler_writes_expected_header_and_columns(tmp_path: Path) -> None:
    sample_csv = tmp_path / "disturbance-population.csv"
    stop = threading.Event()
    skips = {"on-prem": 2, "edge-1": 0}

    # Fixed job snapshot: one Active per target.
    fake_jobs = [_job("on-prem", active=4), _job("edge-1", active=2)]

    # Mock _kubectl_get_disturbance_jobs to return the fixed snapshot
    # once, then stop the sampler so the test terminates promptly.
    call_count = {"n": 0}

    def fake_get(*_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 1:
            stop.set()  # next wait() will return immediately
        return fake_jobs

    with mock.patch(
        "experiments.orchestrator.submit._kubectl_get_disturbance_jobs",
        side_effect=fake_get,
    ):
        _disturbance_population_sampler(
            context="unique-logical-entrypoint",
            namespace="delphi-experiments",
            state_id="mixed-disturbance-heavy",
            disturbance_cell="r1-mdh",
            target_clusters=["on-prem", "edge-1"],
            sample_csv=sample_csv,
            stop_event=stop,
            started_monotonic=0.0,
            sample_interval_s=0.01,  # short so the test is fast
            circuit_breaker_skips=skips,
        )

    rows = list(csv.DictReader(sample_csv.open()))
    # First call should have written 2 rows (one per target).
    assert len(rows) >= 2

    header = list(rows[0].keys())
    assert header == [
        "state_id", "target_cluster", "t_offset_s",
        "n_submitted_cumulative", "n_active_or_pending",
        "n_completed", "n_failed",
        "spawn_skips_circuit_breaker",
    ], f"header drift detected: {header}"

    on_prem = next(r for r in rows if r["target_cluster"] == "on-prem")
    edge_1 = next(r for r in rows if r["target_cluster"] == "edge-1")

    # Q5=B contract: n_active_or_pending = .status.active aggregated.
    assert int(on_prem["n_active_or_pending"]) == 4
    assert int(edge_1["n_active_or_pending"]) == 2

    # circuit_breaker_skips wiring through to the CSV per target.
    assert int(on_prem["spawn_skips_circuit_breaker"]) == 2
    assert int(edge_1["spawn_skips_circuit_breaker"]) == 0

    # The state_id round-trips.
    assert on_prem["state_id"] == "mixed-disturbance-heavy"


def test_sampler_exits_when_stop_event_set(tmp_path: Path) -> None:
    """The sampler is launched as a daemon thread inside submit_real;
    its loop must terminate as soon as `stop_event` is set, even if
    no kubectl call has yet returned."""
    sample_csv = tmp_path / "p.csv"
    stop = threading.Event()
    stop.set()  # already stopped before the sampler starts

    with mock.patch(
        "experiments.orchestrator.submit._kubectl_get_disturbance_jobs",
        return_value=[],
    ):
        _disturbance_population_sampler(
            context="unique-logical-entrypoint",
            namespace="delphi-experiments",
            state_id="idle",
            disturbance_cell="r1-idle",
            target_clusters=["on-prem"],
            sample_csv=sample_csv,
            stop_event=stop,
            started_monotonic=0.0,
            sample_interval_s=10.0,
            circuit_breaker_skips={},
        )

    # CSV is created with the header even on an immediate exit.
    rows = list(csv.DictReader(sample_csv.open()))
    assert rows == []  # no samples written
