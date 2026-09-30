"""Intent compliance — two-tier formula.

For every completed foreground job j:

    hard_pass(j) ∈ {0, 1}     — region is a hard gate (mirrors the
                                 Heuristic-Scorer's region_match contract
                                 at decision-maker/internal/service/
                                 heuristic_scorer.go from PR #18). latency
                                 and cost use ordinal ≤ on the
                                 low < medium < high ladder; 'any' always
                                 passes.

    soft_score(j) ∈ [0, 1]    — fraction of preferred_classes the
                                 selected cluster's role matches.

Aggregates per baseline:
    hard_pass_rate    = mean(hard_pass(j)) over completed foreground jobs
    soft_score_mean   = mean(soft_score(j)) over the same set

Reported separately in summary.json. The Heuristic-Scorer's
region-hard-gate means its hard_pass_rate is 1.0 by construction —
discrimination lives in soft_score_mean and cluster-state-dependent
runs.

Completion-aware aggregates (additive):

    hard_pass_rate_completion_aware
        = sum(hard_pass(j) over completed foreground jobs) / n_submitted
    soft_score_mean_completion_aware
        = sum(soft_score(j) over completed foreground jobs) / n_submitted

A submitted foreground job that did not complete counts as
non-compliant (hard 0, soft 0). The completed-only aggregates above
are unchanged — same keys, same values, same CSV rows — so every
previously registered run stays re-scorable; the completion-aware pair
exists because a completed-only denominator lets a policy whose
unfinished work drops out of the denominator look better than one that
finished everything.

Audit fields: ``n_submitted`` = every foreground job record of the run
(``len(art.foreground_jobs)``, i.e. every foreground row of
submission-log.csv) — deliberately NOT the ``applied`` subset that
``placement_success`` uses, because a job the policy never got to
finish is exactly what this aggregate must not drop. On every committed
run so far the two coincide. ``n_completed`` = completed foreground
jobs; ``completion_rate`` = n_completed / n_submitted.

Edge cases:
    - n_submitted > 0, n_completed == 0 → completion-aware = 0.0
      (completed-only keeps its None / insufficient_data behaviour);
    - n_submitted == 0 → completion-aware = 0.0, mirroring
      placement_success; n_submitted = 0 makes this visible;
    - a completed job that cannot be scored (no selected_cluster, e.g. an
      offline ``--no-kube`` pass, or an unknown profile/cluster) → the
      completion-aware pair is None: its numerator is incomplete, and
      silently counting a data gap as non-compliance would be wrong.
      ``n`` (scored rows) < ``n_completed`` flags this case.

The per-job CSV keeps completed-and-scored rows only: the plot helpers
and ``experiments/analysis/cost_ablation_compare.py`` average
``hard_pass`` over those rows, and adding zero rows for unfinished jobs
would silently change their meaning.
"""
from __future__ import annotations

from statistics import mean
from typing import Any

from ..models import AnalysisContext, ClusterLabels, ClusterRegistry, IntentProfile, MetricResult, RunArtifacts


_LATENCY_LADDER = ("low", "medium", "high")
_COST_LADDER = ("low", "medium", "high")


def _ordinal_le(req_value: str, cluster_value: str, ladder: tuple[str, ...]) -> bool:
    """Return True iff cluster_value <= req_value on the ladder.

    'any' on req side always passes. Unknown values fail closed (safer
    than silently accepting).
    """
    if req_value in ("", "any"):
        return True
    if req_value not in ladder or cluster_value not in ladder:
        return False
    return ladder.index(cluster_value) <= ladder.index(req_value)


def _region_satisfied(req_region: str, cluster_role: str) -> bool:
    if req_region in ("", "any"):
        return True
    return req_region.strip().lower() == cluster_role.strip().lower()


def compute_per_job(profile: IntentProfile,
                    cluster: ClusterLabels) -> tuple[int, float]:
    """Pure function — primary unit-test target.

    Returns (hard_pass, soft_score).
    """
    req = profile.propagation_requirements
    req_region = str(req.get("region", "any"))
    req_latency = str(req.get("latency", "any"))
    req_cost = str(req.get("cost", "any"))

    region_ok = _region_satisfied(req_region, cluster.role)
    latency_ok = _ordinal_le(req_latency, cluster.rtt, _LATENCY_LADDER)
    cost_ok = _ordinal_le(req_cost, cluster.cost, _COST_LADDER)

    hard_pass = 1 if (region_ok and latency_ok and cost_ok) else 0

    preferred = profile.preferred_classes or ()
    if not preferred:
        soft_score = 0.0
    else:
        matches = sum(1 for c in preferred if c.strip().lower() == cluster.role.strip().lower())
        soft_score = matches / len(preferred)

    return hard_pass, soft_score


def _completion_aware(hard_values: list[int], soft_values: list[float],
                      *, n_submitted: int, n_completed: int) -> dict[str, Any]:
    """Additive completion-aware keys (see module docstring).

    Denominator is every submitted foreground job; unfinished jobs
    contribute 0. None when a completed job could not be scored (the
    numerator would be incomplete); 0.0 when nothing completed.
    """
    completion_rate = (n_completed / n_submitted) if n_submitted > 0 else 0.0
    if len(hard_values) < n_completed:
        hpr_ca: float | None = None
        ssm_ca: float | None = None
    elif n_submitted == 0:
        hpr_ca = 0.0
        ssm_ca = 0.0
    else:
        hpr_ca = sum(hard_values) / n_submitted
        ssm_ca = sum(soft_values) / n_submitted
    return {
        "hard_pass_rate_completion_aware": hpr_ca,
        "soft_score_mean_completion_aware": ssm_ca,
        "n_submitted": n_submitted,
        "n_completed": n_completed,
        "completion_rate": completion_rate,
    }


def compute(art: RunArtifacts, _ctx: AnalysisContext) -> MetricResult:
    rows: list[dict[str, Any]] = []
    hard_values: list[int] = []
    soft_values: list[float] = []
    notes: list[str] = []

    registry: ClusterRegistry = art.registry

    foreground = art.foreground_jobs
    n_submitted = len(foreground)
    n_completed = sum(1 for j in foreground if j.completed)

    for j in foreground:
        if not j.completed:
            continue
        if not j.intent_profile or not j.selected_cluster:
            notes.append(f"skipped {j.name}: missing intent or selected_cluster")
            continue
        profile = registry.intent_profiles.get(j.intent_profile)
        cluster = registry.clusters.get(j.selected_cluster)
        if profile is None or cluster is None:
            notes.append(f"skipped {j.name}: unknown profile or cluster")
            continue

        hp, ss = compute_per_job(profile, cluster)
        hard_values.append(hp)
        soft_values.append(ss)
        rows.append({
            "job_name": j.name,
            "intent_profile": j.intent_profile,
            "selected_cluster": j.selected_cluster,
            "cluster_role": cluster.role,
            "cluster_rtt_label": cluster.rtt,
            "cluster_cost_label": cluster.cost,
            "req_region": str(profile.propagation_requirements.get("region", "any")),
            "req_latency": str(profile.propagation_requirements.get("latency", "any")),
            "req_cost": str(profile.propagation_requirements.get("cost", "any")),
            "preferred_classes": "|".join(profile.preferred_classes),
            "hard_pass": hp,
            "soft_score": ss,
        })

    completion_aware = _completion_aware(
        hard_values, soft_values,
        n_submitted=n_submitted, n_completed=n_completed,
    )

    if not hard_values:
        return MetricResult(
            name="intent_compliance",
            rows=tuple(rows),
            aggregate={
                "hard_pass_rate": None,
                "soft_score_mean": None,
                "n": 0,
                "formatted": "n/a",
                **completion_aware,
            },
            status="insufficient_data",
            note="; ".join(notes),
        )

    hpr = mean(hard_values)
    ssm = mean(soft_values) if soft_values else 0.0
    return MetricResult(
        name="intent_compliance",
        rows=tuple(rows),
        aggregate={
            "hard_pass_rate": hpr,
            "soft_score_mean": ssm,
            "n": len(hard_values),
            "formatted": f"hard={hpr:.2f} / soft={ssm:.2f}",
            **completion_aware,
        },
        status="ok",
        note="; ".join(notes),
    )
