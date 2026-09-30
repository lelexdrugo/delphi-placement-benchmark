"""iter-4h-4: cordon_response metric + runner gating + idempotency."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from experiments.orchestrator.analysis.metrics.cordon_response import compute
from experiments.orchestrator.analysis.models import (
    AnalysisContext,
    ClusterRegistry,
    JobRecord,
    RunArtifacts,
)
from experiments.orchestrator.analysis.runner import run_analysis

_T0 = datetime(2026, 5, 30, 11, 0, 0, tzinfo=timezone.utc)


def _empty_registry() -> ClusterRegistry:
    return ClusterRegistry(clusters={}, intent_profiles={})


def _write_timestamps(run_dir: Path, *, target: str, start_off: int, end_off: int,
                      ttl: int) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "cordon-timestamps.yaml").write_text(
        yaml.safe_dump({
            "cordon_target": target,
            "cordon_method": "cordon",
            "cordon_actual_start_timestamp": (_T0 + timedelta(seconds=start_off)).isoformat(),
            "cordon_actual_end_timestamp": (_T0 + timedelta(seconds=end_off)).isoformat(),
            "snapshot_ttl_seconds": ttl,
            "planned_window_seconds": float(end_off),
        }),
        encoding="utf-8",
    )


def _job(name: str, offset_s: int, selected: str | None,
         decision_off_s: int | None = None) -> JobRecord:
    # decision_off_s defaults to the submission offset (lag-free); set it
    # explicitly to model the crew pipeline latency (decision lands later).
    pp_off = offset_s if decision_off_s is None else decision_off_s
    return JobRecord(
        name=name, role="foreground", state_id="idle",
        intent_profile="balanced", workload_class="cpu",
        applied=True, completed=True,
        applied_at_utc=_T0 + timedelta(seconds=offset_s),
        pp_created_at=_T0 + timedelta(seconds=pp_off),
        selected_cluster=selected,
    )


def test_compute_classifies_and_counts(tmp_path: Path):
    run_dir = tmp_path / "run"
    # cordon window [T+10, T+30); ttl 3s.
    _write_timestamps(run_dir, target="edge-1", start_off=10, end_off=30, ttl=3)
    jobs = (
        _job("pre", 0, "edge-1"),                # pre_cordon, targets cordoned
        _job("during-hit", 15, "edge-1"),        # during, targets cordoned
        _job("during-miss", 20, "public-cloud"), # during, does not target
        _job("post", 40, "edge-1"),              # post
    )
    art = RunArtifacts(
        run_id="run", run_dir=run_dir, baseline="heuristic-scorer",
        layout="simple", submitted_at=_T0, finished_at=None,
        jobs=jobs, registry=_empty_registry(),
    )
    res = compute(art, AnalysisContext(use_db=False, use_kube=False))

    assert res.status == "ok"
    agg = res.aggregate
    assert agg["cordon_target"] == "edge-1"
    assert agg["pre_cordon_count"] == 1
    assert agg["during_cordon_count"] == 2
    assert agg["post_cordon_count"] == 1
    # 1 of 2 during-cordon decisions targeted the cordoned cluster.
    assert agg["during_cordon_fraction_targeting_cordoned"] == 0.5
    # both during jobs are past the 3s stale window (T+15, T+20 >= T+13).
    assert agg["during_cordon_excl_stale_count"] == 2
    assert agg["during_cordon_excl_stale_fraction"] == 0.5
    # rows carry the per-job phase + target flag.
    by_name = {r["job_name"]: r for r in res.rows}
    assert by_name["during-hit"]["window_phase"] == "during_cordon"
    assert by_name["during-hit"]["targets_cordoned"] is True
    assert by_name["during-miss"]["targets_cordoned"] is False
    assert by_name["pre"]["window_phase"] == "pre_cordon"
    assert by_name["post"]["window_phase"] == "post_cordon"


def test_decision_time_classification_removes_boundary_latency(tmp_path: Path):
    """A job SUBMITTED during the cordon but DECIDED after uncordon (the
    crew-pipeline-drain artifact) is during-cordon by submission time but
    post-cordon by decision time, so it does NOT inflate the decision-time
    during fraction — the iter-4h-4 headline metric."""
    run_dir = tmp_path / "run"
    _write_timestamps(run_dir, target="edge-1", start_off=10, end_off=30, ttl=3)
    jobs = (
        # submitted during (T+25) but PP lands post-uncordon (T+45): read a
        # restored snapshot → legitimately targets edge-1.
        _job("during-submit-post-decide", 25, "edge-1", decision_off_s=45),
        # genuinely decided during the cordon → avoids edge-1.
        _job("during-decide-miss", 15, "public-cloud", decision_off_s=22),
    )
    art = RunArtifacts(
        run_id="run", run_dir=run_dir, baseline="delphi-full",
        layout="simple", submitted_at=_T0, finished_at=None,
        jobs=jobs, registry=_empty_registry(),
    )
    agg = compute(art, AnalysisContext(use_db=False, use_kube=False)).aggregate
    # Submission-time: both submitted during → 1/2 target edge-1 = 0.5 (confounded).
    assert agg["during_cordon_count"] == 2
    assert agg["during_cordon_fraction_targeting_cordoned"] == 0.5
    # Decision-time: only the genuinely-during decision counts; the
    # post-decided edge-1 job is classified post → during decision fraction 0.
    assert agg["during_cordon_decision_count"] == 1
    assert agg["during_cordon_decision_fraction_targeting_cordoned"] == 0.0
    assert agg["post_cordon_decision_count"] == 1
    assert agg["post_cordon_decision_n_targeting_cordoned"] == 1
    # Strict (submitted AND decided within the window): only the
    # during-decide-miss job qualifies (submitted T+15, PP T+22 ∈ [10,30));
    # the post-decided edge-1 job is excluded (PP T+45 ∉ window). So the
    # strict during fraction targeting the cordoned cluster is a clean 0.
    assert agg["during_cordon_strict_count"] == 1
    assert agg["during_cordon_strict_fraction_targeting_cordoned"] == 0.0


def test_absence_returns_pending(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    art = RunArtifacts(
        run_id="run", run_dir=run_dir, baseline="delphi-full",
        layout="simple", submitted_at=_T0, finished_at=None,
        jobs=(_job("a", 0, "edge-1"),), registry=_empty_registry(),
    )
    res = compute(art, AnalysisContext(use_db=False, use_kube=False))
    assert res.status == "pending"
    assert res.rows == ()
    assert res.aggregate == {}


def _build_pipeline_fixture(tmp_path: Path) -> tuple[Path, Path]:
    registry_path = tmp_path / "cluster-registry.yaml"
    registry_path.write_text(yaml.safe_dump({"clusters": {}, "intent_profiles": {}}),
                             encoding="utf-8")
    run_dir = tmp_path / "runs" / "cordon-fixture"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": "cordon-fixture",
        "baseline": "delphi-full",
        "submitted_at": "2026-05-30T11:00:00Z",
        "finished_at": "2026-05-30T11:10:00Z",
        "cordon_window": {"cordon_target": "edge-1",
                          "cordon_start_fraction": 0.33,
                          "cordon_end_fraction": 0.66},
        "jobs": [],
    }, indent=2), encoding="utf-8")
    with (run_dir / "submission-log.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["job_name", "role", "state_id", "applied", "completed",
                    "failed", "skipped_reason", "wall_clock_seconds",
                    "applied_at_utc", "job_path"])
        w.writerow(["fg-001", "foreground", "idle", 1, 1, 0, "", "0.1",
                    "2026-05-30T11:00:05+00:00", "/dev/null"])
        w.writerow(["fg-002", "foreground", "idle", 1, 1, 0, "", "0.1",
                    "2026-05-30T11:05:00+00:00", "/dev/null"])
    _write_timestamps(run_dir, target="edge-1", start_off=120, end_off=240, ttl=15)
    return run_dir, registry_path


def _hash_dir(analysis_dir: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(analysis_dir.iterdir()) if p.is_file()
    }


def test_pipeline_includes_metric_and_is_idempotent(tmp_path: Path):
    run_dir, registry = _build_pipeline_fixture(tmp_path)

    rc1 = run_analysis(run_dir=run_dir, registry_path=registry,
                       baseline="delphi-full", use_db=False, use_kube=False)
    assert rc1 == 0
    analysis_dir = run_dir / "analysis"
    # The gated metric ran → its CSV + summary projection exist.
    assert (analysis_dir / "cordon-response.csv").exists()
    summary = json.loads((analysis_dir / "summary.json").read_text())
    assert "cordon_response" in summary["baselines"]["delphi-full"]

    first = _hash_dir(analysis_dir)
    rc2 = run_analysis(run_dir=run_dir, registry_path=registry,
                       baseline="delphi-full", use_db=False, use_kube=False)
    assert rc2 == 0
    assert first == _hash_dir(analysis_dir), "cordon run analysis/ not idempotent"
