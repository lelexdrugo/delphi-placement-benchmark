"""Decompose end-to-end completion_time into wait + execution.

`completion_time` (applied_at_utc → k8s_completion_time) bundles the
asynchronous decision wait with the time the workload actually ran. This
metric splits it:

- ``time_to_pod_spawn_s`` = ``k8s_start_time − applied_at_utc``. Time to
  pod spawn: from submission (``applied_at_utc``, when ``kubectl apply``
  of the Job returned) to the Job's ``status.startTime`` as reported at
  the Karmada entry point, aggregated from the selected member's Job. It
  contains an asynchronous backend's decision wait, PropagationPolicy
  materialization and propagation, and, under a cordon, the time a job
  placed on the cordoned member is held until the lift. Pod scheduling,
  image pull and container start come after ``startTime`` and fall
  inside ``execution_runtime_s``.
- ``execution_runtime_s`` = ``k8s_completion_time − k8s_start_time`` —
  the work that ran on the member.

These sum to the existing ``completion_time.duration_seconds`` by
construction.

Data source: both ``startTime`` and ``completionTime`` are present on the
**aggregated Job.status** at ``unique-logical-entrypoint`` and are already
loaded onto ``JobRecord`` by the runner, so the split needs no
member-cluster reach and works for the private edges too.
``applied_at_utc`` is taken on the operator's clock while ``startTime``
comes from the control plane, so the two clocks should be
NTP-synchronised. Do NOT expect
``median_wait_s ≈ decision_latency.median_ms / 1000``:
``decision_latency`` is the DB attempt lifecycle (which can include
requeues firing after the pod completed); this metric is wall-clock.

The metric is additive — on a run with no kube enrichment (e.g. the
synthetic fixtures) every job lacks ``k8s_start_time`` so it emits
``status='pending'`` with empty observed aggregates and is byte-stable.

Also emits ``cost_proxy_execution_based`` (Σ cost_value × execution
runtime) as the execution-based companion to the existing
``cost_proxy`` (runtime/completion based).
"""
from __future__ import annotations

from statistics import median
from typing import Any

from ..models import AnalysisContext, MetricResult, RunArtifacts
from .cost_proxy import COST_VALUE


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
    waits: list[float] = []
    execs: list[float] = []
    n_skipped = 0
    n_skipped_no_start = 0
    n_skipped_not_completed = 0
    cost_exec_total = 0.0
    cost_exec_n = 0

    for j in art.foreground_jobs:
        wait = j.time_to_pod_spawn_seconds
        runtime = j.execution_runtime_seconds
        rows.append({
            "job_name": j.name,
            "state_id": j.state_id,
            "workload_class": j.workload_class,
            "intent_profile": j.intent_profile,
            "applied_at_utc": j.applied_at_utc.isoformat() if j.applied_at_utc else "",
            "k8s_start_time": j.k8s_start_time.isoformat() if j.k8s_start_time else "",
            "k8s_completion_time": j.k8s_completion_time.isoformat() if j.k8s_completion_time else "",
            "selected_cluster": j.selected_cluster or "",
            "time_to_pod_spawn_s": wait if wait is not None else "",
            "execution_runtime_s": runtime if runtime is not None else "",
        })

        # An observation needs both endpoints and a completed job.
        if not j.completed:
            n_skipped += 1
            n_skipped_not_completed += 1
            continue
        if wait is None or runtime is None:
            n_skipped += 1
            n_skipped_no_start += 1
            continue

        waits.append(wait)
        execs.append(runtime)

        # Execution-based cost proxy (companion to cost_proxy.total).
        if j.selected_cluster:
            cluster = art.registry.clusters.get(j.selected_cluster)
            if cluster is not None and cluster.cost in COST_VALUE:
                cost_exec_total += COST_VALUE[cluster.cost] * runtime
                cost_exec_n += 1

    n_observed = len(waits)
    if n_observed == 0:
        return MetricResult(
            name="completion_decomposition",
            rows=tuple(rows),
            aggregate={
                "median_wait_s": None,
                "p95_wait_s": None,
                "median_exec_s": None,
                "p95_exec_s": None,
                "n_observed": 0,
                "n_skipped": n_skipped,
                "n_skipped_no_start": n_skipped_no_start,
                "n_skipped_not_completed": n_skipped_not_completed,
                "cost_proxy_execution_based": None,
                "cost_proxy_execution_based_n": 0,
            },
            status="pending",
            note=(
                "no foreground job carries both Job.status.startTime and "
                "completionTime (kube enrichment off, jobs cleaned up, or "
                "startTime not aggregated) — completion_time still available"
            ),
        )

    aggregate = {
        "median_wait_s": median(waits),
        "p95_wait_s": _percentile(waits, 0.95),
        "median_exec_s": median(execs),
        "p95_exec_s": _percentile(execs, 0.95),
        "n_observed": n_observed,
        "n_skipped": n_skipped,
        "n_skipped_no_start": n_skipped_no_start,
        "n_skipped_not_completed": n_skipped_not_completed,
        "cost_proxy_execution_based": cost_exec_total,
        "cost_proxy_execution_based_n": cost_exec_n,
        "formatted": (
            f"wait median={median(waits):.1f}s / "
            f"exec median={median(execs):.1f}s (n={n_observed})"
        ),
    }
    return MetricResult(
        name="completion_decomposition",
        rows=tuple(rows),
        aggregate=aggregate,
        status="ok",
    )
