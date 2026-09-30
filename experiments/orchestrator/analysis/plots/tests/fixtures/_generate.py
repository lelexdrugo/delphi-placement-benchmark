"""Hand-craft three synthetic baseline run-dirs for plot tests.

Run me with the venv's Python: ``.venv/Scripts/python.exe _generate.py``
from this directory. The script is committed alongside its output so
the fixtures are reproducible — a reviewer can re-run the generator
and diff against the committed CSVs.

These fixtures are **invented**. Do not cite. See README.md.

Story we want the plots to show:

- ``static-karmada`` pins every Job to the same cluster
  (``public-cloud``). It survives idle cleanly (success=1.0) but
  fails ``latency-sensitive`` jobs that demand region=edge
  (hard_pass=0). Under saturated cluster-states the chosen cluster's
  queue length pushes completion_time up sharply.
- ``heuristic-scorer`` honours the region hard-filter so
  hard_pass=1.0 everywhere. It avoids the saturated cluster when it
  can. Completion times are stable but cost_score is higher because
  it has no notion of cost vs latency trade-off.
- ``delphi-full`` matches Heuristic on intent and beats it on
  completion_time under
  skewed-edge-1-saturated where history + similarity steer placement
  off the hot edge. Decision latency is two orders of magnitude
  larger (the agent talks to the LLM).
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

FIXTURE_DIR = Path(__file__).parent

STATES: tuple[str, ...] = (
    "idle",
    "mixed-load",
    "skewed-edge-1-saturated",
    "skewed-on-prem-saturated",
)
CLASSES: tuple[str, ...] = ("cpu", "memory", "network")
PROFILES: tuple[str, ...] = ("balanced", "cost-aware", "latency-sensitive", "locality-aware")

# Profile -> (region, latency, cost, preferred_classes).
PROFILE_REQ: dict[str, tuple[str, str, str, str]] = {
    "balanced": ("any", "medium", "medium", "on-premises|public-cloud|edge"),
    "cost-aware": ("any", "high", "low", "edge|on-premises"),
    "latency-sensitive": ("edge", "low", "any", "edge"),
    "locality-aware": ("on-premises", "low", "medium", "on-premises"),
}

# Cluster registry mirror — keep in sync with experiments/specs/cluster-registry.yaml.
# (name, role, rtt_label, cost_label, cost_value).
CLUSTERS: dict[str, tuple[str, str, str, int]] = {
    "on-prem":      ("on-premises",  "low",    "low",    1),
    "public-cloud": ("public-cloud", "medium", "high",   3),
    "edge-1":       ("edge",         "low",    "low",    1),
    "edge-2":       ("edge",         "low",    "low",    1),
}


@dataclass(frozen=True)
class BaselineSpec:
    """Per-baseline knobs that shape every CSV."""
    key: str
    display: str
    # Cluster chosen for a given (state, class, profile). Drives
    # selected_cluster on every per-job CSV.
    selector: Callable[[str, str, str], str]
    # Per-state base completion seconds; jitter added per (class, profile).
    base_completion_s: dict[str, float]
    # Per-state decision_latency_ms median.
    base_decision_ms: dict[str, float]
    # Fraction of "latency-sensitive" jobs that get correctly placed on edge.
    latency_sensitive_edge_hit: float
    # Whether saturated states degrade completion_time on this baseline.
    state_degrades: bool
    # Per-state success rate (1.0 unless the baseline genuinely fails some).
    success_rate_by_state: dict[str, float]


def _static_karmada_selector(state: str, cls: str, profile: str) -> str:
    """Static-Karmada always picks public-cloud regardless of intent."""
    return "public-cloud"


def _heuristic_selector(state: str, cls: str, profile: str) -> str:
    """Heuristic-Scorer honours region; routes around saturated host."""
    if profile == "latency-sensitive":
        return "edge-2" if state == "skewed-edge-1-saturated" else "edge-1"
    if profile == "locality-aware":
        return "on-prem"
    if profile == "cost-aware":
        return "edge-2" if state == "skewed-edge-1-saturated" else "edge-1"
    # balanced
    if state == "skewed-on-prem-saturated":
        return "public-cloud"
    if state == "skewed-edge-1-saturated":
        return "public-cloud"
    return "on-prem"


def _delphi_full_selector(state: str, cls: str, profile: str) -> str:
    """DELPHI-Full: same as heuristic but smarter under skewed states.

    Difference: under skewed-edge-1-saturated, latency-sensitive
    workloads stay on edge-2 (matches heuristic), but cost-aware on a
    network workload sometimes goes to edge-1 if history says it's
    finished there fast (we just stamp edge-2 to keep simple).

    The point of this fixture: small but visible delta on the
    hard_pass tail.
    """
    base = _heuristic_selector(state, cls, profile)
    return base


def _build_spec_static() -> BaselineSpec:
    return BaselineSpec(
        key="static-karmada",
        display="Static-Karmada",
        selector=_static_karmada_selector,
        base_completion_s={
            "idle": 95.0,
            "mixed-load": 110.0,
            # static pins to public-cloud which is never saturated in our skew specs,
            # so its completion time is not heavily affected.
            "skewed-edge-1-saturated": 112.0,
            "skewed-on-prem-saturated": 115.0,
        },
        base_decision_ms={
            "idle": 1.0,
            "mixed-load": 1.0,
            "skewed-edge-1-saturated": 1.0,
            "skewed-on-prem-saturated": 1.0,
        },
        latency_sensitive_edge_hit=0.0,  # never picks edge -> always fails region gate
        state_degrades=False,
        success_rate_by_state={s: 1.0 for s in STATES},
    )


def _build_spec_heuristic() -> BaselineSpec:
    return BaselineSpec(
        key="heuristic-scorer",
        display="Heuristic-Scorer",
        selector=_heuristic_selector,
        base_completion_s={
            "idle": 92.0,
            "mixed-load": 100.0,
            "skewed-edge-1-saturated": 108.0,
            "skewed-on-prem-saturated": 105.0,
        },
        base_decision_ms={
            "idle": 12.0,
            "mixed-load": 14.0,
            "skewed-edge-1-saturated": 15.0,
            "skewed-on-prem-saturated": 14.0,
        },
        latency_sensitive_edge_hit=1.0,
        state_degrades=True,
        success_rate_by_state={s: 1.0 for s in STATES},
    )


def _build_spec_delphi() -> BaselineSpec:
    return BaselineSpec(
        key="delphi-full",
        display="DELPHI-Full",
        selector=_delphi_full_selector,
        base_completion_s={
            "idle": 92.0,
            "mixed-load": 98.0,
            "skewed-edge-1-saturated": 101.0,  # better than heuristic — history-aware
            "skewed-on-prem-saturated": 102.0,
        },
        base_decision_ms={
            "idle": 28000.0,
            "mixed-load": 31000.0,
            "skewed-edge-1-saturated": 33000.0,
            "skewed-on-prem-saturated": 32000.0,
        },
        latency_sensitive_edge_hit=0.97,
        state_degrades=True,
        success_rate_by_state={s: 1.0 for s in STATES},
    )


def _jitter(base: float, state: str, cls: str, profile: str) -> float:
    """Deterministic per-cell jitter so distributions have width.

    Pure function of the cell coordinates so re-running the generator
    produces byte-identical CSVs.
    """
    seed = (
        STATES.index(state) * 1000
        + CLASSES.index(cls) * 100
        + PROFILES.index(profile) * 10
    )
    return round(base + (seed % 13) - 6 + 0.7 * (seed % 5), 3)


def _hard_pass(profile: str, selected_cluster: str, state: str, spec: BaselineSpec) -> int:
    """Two-tier intent compliance: hard gate is region match."""
    role = CLUSTERS[selected_cluster][0]
    req_region = PROFILE_REQ[profile][0]
    if req_region == "any":
        return 1
    if req_region == "edge":
        # latency-sensitive: hard_pass depends on baseline's edge_hit chance.
        # We mark deterministically: 0 if static picks public-cloud, 1 if heuristic/DELPHI pick edge.
        if profile == "latency-sensitive" and spec.latency_sensitive_edge_hit < 0.5:
            return 0
        return 1 if role == "edge" else 0
    if req_region == "on-premises":
        return 1 if role == "on-premises" else 0
    return 1


def _soft_score(profile: str) -> float:
    prefs = PROFILE_REQ[profile][3].split("|")
    # One of the profile's preferred classes matched, on average.
    return round(1.0 / len(prefs), 6)


def _job_name(spec: BaselineSpec, idx: int, cls: str, state: str) -> str:
    return f"synthetic-{spec.key}-{idx:03d}-{cls}-{state[:18]}"


def _write_summary(adir: Path, spec: BaselineSpec, rows_intent: list[dict]) -> None:
    """Emit summary.json shaped like the analyzer's _build_baseline_summary."""
    completed_total = len(rows_intent)
    hard_pass_rate = sum(int(r["hard_pass"]) for r in rows_intent) / completed_total
    soft_mean = sum(float(r["soft_score"]) for r in rows_intent) / completed_total
    completion_values = [
        spec.base_completion_s[s] for s in STATES
    ]
    decision_values = [spec.base_decision_ms[s] for s in STATES]
    summary = {
        "baseline": spec.key,
        "baselines": {
            spec.key: {
                "admission_overhead": {
                    "median_ms": (38.0 if spec.key == "static-karmada"
                                  else 41.0 if spec.key == "heuristic-scorer"
                                  else 44.0),
                    "p95_ms": (52.0 if spec.key == "static-karmada"
                               else 56.0 if spec.key == "heuristic-scorer"
                               else 61.0),
                    "n": 48,
                    "status": "ok",
                    "note": "SYNTHETIC — admission overhead requires paired runs (iter-4d.1).",
                },
                "completion_time": {
                    "median_s": round(sum(completion_values) / 4, 3),
                    "p95_s": round(max(completion_values), 3),
                    "max_s": round(max(completion_values) + 4, 3),
                    "n": 48,
                    "status": "ok",
                    "formatted": "SYNTHETIC",
                },
                "cost_proxy": {
                    "total": (12_000.0 if spec.key == "static-karmada"
                              else 7_800.0 if spec.key == "heuristic-scorer"
                              else 7_500.0),
                    "value_map_low": 1.0,
                    "value_map_medium": 2.0,
                    "value_map_high": 3.0,
                    "n": 48,
                    "status": "ok",
                },
                "data_movement": {
                    "mean_score": 0.0,
                    "histogram_local": 48,
                    "histogram_near": 0,
                    "histogram_far": 0,
                    "n_scored": 48,
                    "n_n_a": 0,
                    "n": 48,
                    "status": "ok",
                    "formatted": "SYNTHETIC",
                },
                "decision_latency": {
                    "median_ms": round(sum(decision_values) / 4, 3),
                    "p95_ms": round(max(decision_values), 3),
                    "max_ms": round(max(decision_values) + 1000, 3),
                    "n": 48,
                    "status": "ok",
                    "formatted": "SYNTHETIC",
                },
                "intent_compliance": {
                    "hard_pass_rate": round(hard_pass_rate, 6),
                    "soft_score_mean": round(soft_mean, 6),
                    "n": 48,
                    "status": "ok",
                    "formatted": f"hard={hard_pass_rate:.2f} / soft={soft_mean:.2f}",
                },
                "placement_success": {
                    "n_applied": 48,
                    "n_completed": 48,
                    "value": 1.0,
                    "status": "ok",
                },
                "telemetry_overhead": {
                    "median_ms": (24.0 if spec.key == "static-karmada"
                                  else 26.0 if spec.key == "heuristic-scorer"
                                  else 29.0),
                    "p95_ms": (34.0 if spec.key == "static-karmada"
                               else 36.0 if spec.key == "heuristic-scorer"
                               else 41.0),
                    "n": 48,
                    "status": "ok",
                    "note": "SYNTHETIC — telemetry overhead requires paired runs (iter-4d.1).",
                },
                "traceability": {
                    "n_rows": 48,
                    "n_with_reason": 48,
                    "n_with_score_map": 48,
                    "status": "ok",
                },
                "most_frequent_selected_cluster": (
                    "public-cloud" if spec.key == "static-karmada" else "on-prem"
                ),
            }
        },
        "no_pollution_ok": True,
        "join_coverage_ok": True,
        "submitted_at": None,
        "finished_at": None,
        "run_id": f"synthetic-{spec.key}",
        "notes": ["SYNTHETIC fixture — invented numbers for plot-shape testing."],
    }
    (adir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, header: list[str], rows: Iterable[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _emit_baseline(spec: BaselineSpec) -> None:
    out = FIXTURE_DIR / f"synthetic-{spec.key}" / "analysis"
    out.mkdir(parents=True, exist_ok=True)

    placement_rows: list[dict] = []
    completion_rows: list[dict] = []
    decision_rows: list[dict] = []
    intent_rows: list[dict] = []
    cost_rows: list[dict] = []
    movement_rows: list[dict] = []
    traceability_rows: list[dict] = []

    t0 = datetime(2026, 5, 24, 23, 0, 0, tzinfo=timezone.utc)
    idx = 0
    for state in STATES:
        for cls in CLASSES:
            for profile in PROFILES:
                idx += 1
                selected = spec.selector(state, cls, profile)
                role, rtt, cost_label, cost_value = CLUSTERS[selected]
                req_region, req_latency, req_cost, prefs = PROFILE_REQ[profile]

                completion_s = _jitter(spec.base_completion_s[state], state, cls, profile)
                decision_ms = round(_jitter(spec.base_decision_ms[state], state, cls, profile), 3)
                hard = _hard_pass(profile, selected, state, spec)
                soft = _soft_score(profile)

                jname = _job_name(spec, idx, cls, state)
                applied = t0 + timedelta(seconds=10 * idx)
                completed = applied + timedelta(seconds=completion_s)
                applied_str = applied.isoformat(timespec="microseconds")
                completed_str = completed.replace(microsecond=0).isoformat()

                placement_rows.append({
                    "state_id": state,
                    "workload_class": cls,
                    "intent_profile": profile,
                    "applied": 1,
                    "completed": 1,
                    "failed": 0,
                    "success_rate": spec.success_rate_by_state[state],
                })
                completion_rows.append({
                    "job_name": jname,
                    "state_id": state,
                    "workload_class": cls,
                    "intent_profile": profile,
                    "applied_at_utc": applied_str,
                    "k8s_completion_time": completed_str,
                    "selected_cluster": selected,
                    "duration_seconds": completion_s,
                })
                decision_rows.append({
                    "job_name": jname,
                    "state_id": state,
                    "workload_class": cls,
                    "intent_profile": profile,
                    "db_status": "COMPLETED",
                    "db_kind": "foreground",
                    "created_at": applied_str,
                    "last_attempt_at": (applied + timedelta(milliseconds=decision_ms)).isoformat(timespec="microseconds"),
                    "latency_ms": decision_ms,
                })
                intent_rows.append({
                    "job_name": jname,
                    "intent_profile": profile,
                    "selected_cluster": selected,
                    "cluster_role": role,
                    "cluster_rtt_label": rtt,
                    "cluster_cost_label": cost_label,
                    "req_region": req_region,
                    "req_latency": req_latency,
                    "req_cost": req_cost,
                    "preferred_classes": prefs,
                    "hard_pass": hard,
                    "soft_score": soft,
                })
                cost_rows.append({
                    "job_name": jname,
                    "selected_cluster": selected,
                    "cluster_cost_label": cost_label,
                    "cost_value": cost_value,
                    "runtime_seconds": completion_s,
                    "cost_score": round(cost_value * completion_s, 3),
                })
                # Movement: when region=any we leave it n/a; otherwise classify.
                if req_region == "any":
                    move_class, move_score = "n/a", ""
                else:
                    move_class = "local" if (
                        (req_region == "edge" and role == "edge") or
                        (req_region == "on-premises" and role == "on-premises")
                    ) else "far"
                    move_score = 0 if move_class == "local" else 2
                movement_rows.append({
                    "job_name": jname,
                    "intent_profile": profile,
                    "req_region": req_region,
                    "selected_cluster": selected,
                    "cluster_role": role,
                    "movement_class": move_class,
                    "movement_score": move_score,
                })
                # Traceability: synthesize a clusters_score string and reason blurb.
                score_map = {
                    "on-prem": 0.6 if selected == "on-prem" else 0.3,
                    "public-cloud": 0.8 if selected == "public-cloud" else 0.3,
                    "edge-1": 0.7 if selected == "edge-1" else 0.4,
                    "edge-2": 0.7 if selected == "edge-2" else 0.4,
                }
                score_map[selected] = max(score_map.values()) + 0.1
                score_blob = ";".join(
                    f"{k}={score_map[k]:.2f}"
                    for k in sorted(score_map, key=lambda kk: (-score_map[kk], kk))
                )
                reason = (
                    f"SYNTHETIC reason for {spec.display}: state={state} class={cls} "
                    f"profile={profile} selected={selected} (hard_pass={hard})."
                )
                traceability_rows.append({
                    "job_name": jname,
                    "intent_profile": profile,
                    "workload_class": cls,
                    "state_id": state,
                    "selected_cluster": selected,
                    "completed": 1,
                    "runtime_seconds": completion_s,
                    "decision_latency_ms": decision_ms,
                    "clusters_score": score_blob,
                    "reason": reason,
                })

    _write_csv(out / "placement-success.csv",
               ["state_id", "workload_class", "intent_profile",
                "applied", "completed", "failed", "success_rate"],
               placement_rows)
    _write_csv(out / "completion-time.csv",
               ["job_name", "state_id", "workload_class", "intent_profile",
                "applied_at_utc", "k8s_completion_time", "selected_cluster",
                "duration_seconds"],
               completion_rows)
    _write_csv(out / "decision-latency.csv",
               ["job_name", "state_id", "workload_class", "intent_profile",
                "db_status", "db_kind", "created_at", "last_attempt_at",
                "latency_ms"],
               decision_rows)
    _write_csv(out / "intent-compliance.csv",
               ["job_name", "intent_profile", "selected_cluster",
                "cluster_role", "cluster_rtt_label", "cluster_cost_label",
                "req_region", "req_latency", "req_cost",
                "preferred_classes", "hard_pass", "soft_score"],
               intent_rows)
    _write_csv(out / "cost-proxy.csv",
               ["job_name", "selected_cluster", "cluster_cost_label",
                "cost_value", "runtime_seconds", "cost_score"],
               cost_rows)
    _write_csv(out / "data-movement.csv",
               ["job_name", "intent_profile", "req_region",
                "selected_cluster", "cluster_role",
                "movement_class", "movement_score"],
               movement_rows)
    _write_csv(out / "traceability.csv",
               ["job_name", "intent_profile", "workload_class", "state_id",
                "selected_cluster", "completed", "runtime_seconds",
                "decision_latency_ms", "clusters_score", "reason"],
               traceability_rows)

    _write_summary(out, spec, intent_rows)


def main() -> None:
    for spec_fn in (_build_spec_static, _build_spec_heuristic, _build_spec_delphi):
        _emit_baseline(spec_fn())
    print("emitted synthetic baselines under", FIXTURE_DIR)


if __name__ == "__main__":
    main()
