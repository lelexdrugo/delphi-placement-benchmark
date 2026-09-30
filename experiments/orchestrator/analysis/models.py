"""Typed dataclasses shared across the analyzer subpackage.

Frozen because every analyzer pass treats its inputs as a snapshot of
the universe at analysis time. Mutation would silently invalidate
idempotency.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Cluster registry + intent profiles (loaded from experiments/specs/cluster-registry.yaml)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClusterLabels:
    """Normalized labels for one cluster, as written in cluster-registry.yaml."""
    name: str
    role: str             # "edge" | "on-premises" | "public-cloud"
    arch: str             # "amd64" | "arm64"
    cost: str             # "low" | "medium" | "high"
    rtt: str              # "low" | "medium" | "high"
    cpu_class: str
    mem_class: str


@dataclass(frozen=True)
class IntentProfile:
    """One row from cluster-registry.yaml's intent_profiles section."""
    name: str
    propagation_requirements: dict[str, Any]  # latency/cost/region/weight
    preferred_classes: tuple[str, ...]


@dataclass(frozen=True)
class ClusterRegistry:
    """Aggregate of clusters + intent profiles."""
    clusters: dict[str, ClusterLabels]
    intent_profiles: dict[str, IntentProfile]


# ---------------------------------------------------------------------------
# Per-run artifacts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JobRecord:
    """One foreground or background Job's analysis-time projection.

    Combines submission-log fields, manifest fields, and resolved
    references to DB rows and Kubernetes objects. Built lazily by
    loaders + runner; every field has a sensible null value so the
    analyzer can report ``pending`` rather than crash when a source
    is unavailable.
    """
    name: str
    role: str                                   # "foreground" | "background" | "pending"
    state_id: str                               # "" for simple runs
    intent_profile: str                         # "" for background
    workload_class: str                         # "" if unknown

    # From submission-log.csv (iter-4a):
    applied: bool = False
    completed: bool = False
    failed: bool = False
    applied_at_utc: Optional[datetime] = None
    wall_clock_seconds: float = 0.0
    skipped_reason: str = ""

    # Effective placement every metric reads. Resolution order (runner):
    # persisted_cluster (collection time) → live_cluster (analysis time).
    selected_cluster: Optional[str] = None      # PropagationPolicy.spec.placement.clusterAffinity.clusterNames[0]
    # From runs/<id>/placements.json (or manifest.json.assigned_cluster),
    # recorded at collection time while the PropagationPolicy existed:
    persisted_cluster: Optional[str] = None
    # From kubectl get -o json (kube.py), at analysis time:
    live_cluster: Optional[str] = None
    # Effective Job.status timestamps every metric reads. Resolution
    # (runner): persisted_* (job-status.json, collection time) → live_*.
    k8s_start_time: Optional[datetime] = None
    k8s_completion_time: Optional[datetime] = None
    persisted_start_time: Optional[datetime] = None
    persisted_completion_time: Optional[datetime] = None
    live_start_time: Optional[datetime] = None
    live_completion_time: Optional[datetime] = None
    pp_generation: int = 0                      # > 1 implies re-materialization
    pp_created_at: Optional[datetime] = None    # PP creationTimestamp ≈ decision-landed time (iter-4h-4 cordon decision-time classification)

    # From decision_requests table (db.py):
    db_request_id: Optional[str] = None
    db_status: str = ""
    db_kind: str = ""
    db_created_at: Optional[datetime] = None
    db_last_attempt_at: Optional[datetime] = None
    db_clusters_score: Optional[dict[str, float]] = None
    db_reason: Optional[str] = None

    # Convenience helpers
    @property
    def runtime_seconds(self) -> Optional[float]:
        if self.applied_at_utc and self.k8s_completion_time:
            return (self.k8s_completion_time - self.applied_at_utc).total_seconds()
        return None

    @property
    def decision_latency_ms(self) -> Optional[float]:
        if self.db_created_at and self.db_last_attempt_at:
            return (self.db_last_attempt_at - self.db_created_at).total_seconds() * 1000.0
        return None

    @property
    def time_to_pod_spawn_seconds(self) -> Optional[float]:
        """Asynchronous wait the controller absorbs: from Job apply to the
        member Job being acknowledged (decision loop + PP materialization +
        pod schedule). Uses aggregated ``Job.status.startTime`` — see
        iter-4d-0b plan § "Feasibility update" for the Job-vs-Pod startTime
        impurity (consistent across baselines)."""
        if self.applied_at_utc and self.k8s_start_time:
            return (self.k8s_start_time - self.applied_at_utc).total_seconds()
        return None

    @property
    def execution_runtime_seconds(self) -> Optional[float]:
        """Time the workload actually ran on the member: from
        ``Job.status.startTime`` to ``Job.status.completionTime``. This is
        the placement-quality signal under task-bound stressors, with the
        async decision wait removed."""
        if self.k8s_start_time and self.k8s_completion_time:
            return (self.k8s_completion_time - self.k8s_start_time).total_seconds()
        return None


@dataclass(frozen=True)
class RunArtifacts:
    """Top-level container the runner passes to every metric module."""
    run_id: str
    run_dir: Path
    baseline: str                               # "static-karmada" | "heuristic-scorer" | "delphi-full" | ablation variants
    layout: str                                 # "simple" | "bootstrap"
    submitted_at: Optional[datetime]
    finished_at: Optional[datetime]
    jobs: tuple[JobRecord, ...]                 # foreground + background, sorted by name
    registry: ClusterRegistry
    notes: tuple[str, ...] = ()

    @property
    def foreground_jobs(self) -> tuple[JobRecord, ...]:
        return tuple(j for j in self.jobs if j.role == "foreground")

    @property
    def background_jobs(self) -> tuple[JobRecord, ...]:
        return tuple(j for j in self.jobs if j.role == "background")


@dataclass(frozen=True)
class AnalysisContext:
    """Knobs and resource-handles for one analysis pass."""
    use_db: bool = True
    use_kube: bool = True
    skip_pollution_check: bool = False
    # Pre-resolved Kubernetes contexts/namespaces:
    aggregator_context: str = "unique-logical-entrypoint"
    aggregator_namespace: str = "delphi-experiments"


# ---------------------------------------------------------------------------
# Metric results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricResult:
    """One metric's reduction output.

    rows:      list[dict] — per-cell rows that the writer dumps to CSV.
    aggregate: dict       — the summary scalars that land in summary.json.
    status:    "ok" | "pending" | "insufficient_data" | "error"
    note:      free-form one-line context (e.g. why pending).
    """
    name: str
    rows: tuple[dict[str, Any], ...]
    aggregate: dict[str, Any]
    status: str = "ok"
    note: str = ""


@dataclass(frozen=True)
class Summary:
    """The aggregate object written to summary.json."""
    run_id: str
    baseline: str
    no_pollution_ok: Optional[bool]                 # None when --no-db
    join_coverage_ok: Optional[bool]
    submitted_at: Optional[datetime]
    finished_at: Optional[datetime]
    baselines: dict[str, dict[str, Any]]            # nested: baseline -> metric -> aggregate
    notes: tuple[str, ...] = ()
