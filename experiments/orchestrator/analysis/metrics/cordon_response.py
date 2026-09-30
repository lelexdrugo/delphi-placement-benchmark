"""iter-4h-4: cordon response — did each baseline keep targeting a
cordoned cluster?

Reads the orchestrator-written ``cordon-timestamps.yaml`` (auto-written
by ``submit_real`` when it ran the cordon thread) and classifies every
foreground decision as ``pre_cordon`` / ``during_cordon`` / ``post_cordon``
by its submission timestamp, then records whether its PropagationPolicy
targeted the cordoned cluster.

Headline metric: per baseline, the fraction of ``during_cordon`` decisions
that still targeted the cordoned cluster. Expected — Static-Karmada
insensitive (its fixed target), Heuristic-Scorer > 0 (registry-blind,
keeps scoring the cordoned cluster), DELPHI-Full ≈ 0 (live-state filter
drops it within one snapshot TTL).

Two classifications are emitted (iter-4h-4): by **submission** time
(``applied_at_utc``) and by **decision** time (``pp_created_at`` ≈ when the
PolicyPolicy materialised). The decision-time view is the headline because
it removes the crew-pipeline-drain confound: a job *submitted* during the
cordon but *decided* after uncordon read a restored snapshot and may
legitimately target the cluster, so it must not count as a during-cordon
hit. ``during_cordon_decision_fraction_targeting_cordoned`` is the
placement-faithful number; the ``cluster-state-timeline`` (available_clusters)
series is the airtight corroboration.

**Absence invariant**: with no ``cordon-timestamps.yaml`` the metric
returns ``pending`` with zero rows and no side effects. The runner only
adds this metric to the pipeline when the file exists, so non-cordon
runs are byte-identical and the existing idempotency fixtures are
untouched; this guard is a second layer.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ..loaders import _parse_utc
from ..models import AnalysisContext, MetricResult, RunArtifacts

TIMESTAMPS_NAME = "cordon-timestamps.yaml"

_PHASES = ("pre_cordon", "during_cordon", "post_cordon")


def _pending(note: str) -> MetricResult:
    return MetricResult(
        name="cordon_response", rows=(), aggregate={}, status="pending", note=note,
    )


def compute(art: RunArtifacts, _ctx: AnalysisContext) -> MetricResult:
    ts_path = art.run_dir / TIMESTAMPS_NAME
    if not ts_path.exists():
        return _pending("no cordon-timestamps.yaml (non-cordon run)")

    import yaml
    try:
        data = yaml.safe_load(ts_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - defensive
        return _pending(f"unreadable cordon-timestamps.yaml: {exc}")

    target = str(data.get("cordon_target") or "")
    start = _parse_utc(data.get("cordon_actual_start_timestamp"))
    end = _parse_utc(data.get("cordon_actual_end_timestamp"))
    if not target or start is None or end is None:
        return _pending(
            "cordon-timestamps.yaml missing target / start / end "
            "(cordon may not have fired)")

    try:
        ttl = int(data.get("snapshot_ttl_seconds", 0) or 0)
    except (TypeError, ValueError):
        ttl = 0
    stale_cutoff = start + timedelta(seconds=ttl)

    def phase(t: datetime) -> str:
        if t < start:
            return "pre_cordon"
        if t < end:
            return "during_cordon"
        return "post_cordon"

    rows: list[dict[str, Any]] = []
    # Two classifications per phase:
    #  - submission-time (by applied_at_utc): "of jobs SUBMITTED during the
    #    cordon, how many targeted it" — simple but confounded by the
    #    decision pipeline latency (a job submitted during the cordon may be
    #    DECIDED after uncordon, when the target is available again).
    #  - decision-time (by pp_created_at ≈ when the decision materialised):
    #    "of decisions MADE during the cordon, how many targeted it" — the
    #    placement-faithful view, since a decision made while the target was
    #    cordoned read a snapshot without it and so cannot pick it. iter-4h-4
    #    headline. Residual at the cordon-onset boundary: a decision whose PP
    #    lands within ~one crew-kickoff of cordon-start read a *pre*-cordon
    #    snapshot, so it is a pre-cordon decision wearing a during-cordon PP
    #    timestamp; documented, small, and corroborated by the
    #    cluster-state-timeline (available_clusters) series.
    counts = {p: {"count": 0, "targeting": 0} for p in _PHASES}
    dcounts = {p: {"count": 0, "targeting": 0} for p in _PHASES}
    during_excl_stale = {"count": 0, "targeting": 0}
    # `strict`: decisions unambiguously made while the cluster was cordoned
    # — both SUBMITTED and DECIDED (PP created) inside [start, end]. This
    # excludes both boundary artifacts (pre-submitted jobs whose PP lands
    # after cordon-onset, and during-submitted jobs decided after uncordon),
    # so it is the placement-faithful headline. Needs pp_created_at.
    during_strict = {"count": 0, "targeting": 0}

    for j in art.foreground_jobs:
        if j.applied_at_utc is None:
            continue
        ph = phase(j.applied_at_utc)
        dph = phase(j.pp_created_at) if j.pp_created_at is not None else ""
        targets_cordoned = bool(j.selected_cluster) and j.selected_cluster == target
        decided_within = (
            j.pp_created_at is not None and start <= j.pp_created_at < end
        )
        strict = ph == "during_cordon" and decided_within
        rows.append({
            "job_name": j.name,
            "intent_profile": j.intent_profile,
            "workload_class": j.workload_class,
            "state_id": j.state_id,
            "applied_at_utc": j.applied_at_utc,
            "window_phase": ph,
            "pp_created_at": j.pp_created_at,
            "decision_phase": dph,
            "decided_during_cordon": strict,
            "selected_cluster": j.selected_cluster or "",
            "cordon_target": target,
            "targets_cordoned": targets_cordoned,
            "completed": j.completed,
        })
        counts[ph]["count"] += 1
        if targets_cordoned:
            counts[ph]["targeting"] += 1
        if dph in dcounts:
            dcounts[dph]["count"] += 1
            if targets_cordoned:
                dcounts[dph]["targeting"] += 1
        if strict:
            during_strict["count"] += 1
            if targets_cordoned:
                during_strict["targeting"] += 1
        if ph == "during_cordon" and j.applied_at_utc >= stale_cutoff:
            during_excl_stale["count"] += 1
            if targets_cordoned:
                during_excl_stale["targeting"] += 1

    def frac(n: int, d: int) -> Optional[float]:
        return (n / d) if d > 0 else None

    aggregate: dict[str, Any] = {
        "cordon_target": target,
        "stale_window_seconds": ttl,
    }
    for p in _PHASES:
        c = counts[p]
        aggregate[f"{p}_count"] = c["count"]
        aggregate[f"{p}_n_targeting_cordoned"] = c["targeting"]
        aggregate[f"{p}_fraction_targeting_cordoned"] = frac(c["targeting"], c["count"])
        # decision-time classification (by pp_created_at)
        d = dcounts[p]
        aggregate[f"{p}_decision_count"] = d["count"]
        aggregate[f"{p}_decision_n_targeting_cordoned"] = d["targeting"]
        aggregate[f"{p}_decision_fraction_targeting_cordoned"] = frac(d["targeting"], d["count"])
    # `during_cordon_fraction_targeting_cordoned` (submission-time) and
    # `during_cordon_decision_fraction_targeting_cordoned` (decision-time,
    # the iter-4h-4 headline) were both set in the loop above.
    aggregate["during_cordon_excl_stale_count"] = during_excl_stale["count"]
    aggregate["during_cordon_excl_stale_fraction"] = frac(
        during_excl_stale["targeting"], during_excl_stale["count"])
    # strict (submitted AND decided within the cordon) — the headline.
    aggregate["during_cordon_strict_count"] = during_strict["count"]
    aggregate["during_cordon_strict_n_targeting_cordoned"] = during_strict["targeting"]
    aggregate["during_cordon_strict_fraction_targeting_cordoned"] = frac(
        during_strict["targeting"], during_strict["count"])

    status = "ok" if any(counts[p]["count"] for p in _PHASES) else "insufficient_data"
    return MetricResult(
        name="cordon_response",
        rows=tuple(rows),
        aggregate=aggregate,
        status=status,
    )
