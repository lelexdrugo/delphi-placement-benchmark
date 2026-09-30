"""Offline fallback to the kube records persisted at collection time.

``placements.json`` (PP target) and ``job-status.json`` (aggregated
Job.status startTime / completionTime), written by
``collect.capture_kube_records`` while the Jobs and PPs still exist, let
the analyzer re-score a run after its campaign namespace is cleaned up.
Cases:

1. offline (``use_kube=False``) + persisted records reproduces the online
   analysis — intent compliance (both aggregates) and, byte for byte, the
   whole ``analysis/`` dir incl. completion-time, completion-decomposition,
   decision-latency and cost-proxy;
2. online with persisted records but every Job/PP gone → same outputs;
3. no persisted records → exactly the pre-existing behaviour;
4. persisted vs live disagreement (placement, start/completion time) →
   persisted value scored, one note per field;
5. a null persisted value falls back to live (late completion) and a
   backfill capture makes offline match again; each artifact works alone;
   manifest ``assigned_cluster`` / ``completed_at`` fallback; malformed
   artifact; PP provenance round-trip; decision latency stays DB-only.

kubectl is mocked at the analyzer's single seam (``kube._kubectl_json``)
and ``kube.subprocess`` is replaced by a recorder that fails the test at
teardown if any real process spawn was attempted (``run_analysis``
swallows enrichment exceptions, so a raising guard alone could be
silenced).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from pathlib import Path

import pytest
import yaml

from datetime import datetime, timezone

from experiments.orchestrator import collect as _collect
from experiments.orchestrator.analysis import kube as _kube
from experiments.orchestrator.analysis.db import StubDB
from experiments.orchestrator.analysis import loaders as _loaders
from experiments.orchestrator.analysis.runner import run_analysis

_CTX = "unique-logical-entrypoint"
_NS = "delphi-experiments"

_REGISTRY = {
    "clusters": {
        "edge-1": {"role": "edge", "arch": "arm64", "cost": "low", "rtt": "low",
                   "cpu_class": "low", "mem_class": "low"},
        "on-prem": {"role": "on-premises", "arch": "amd64", "cost": "low", "rtt": "low",
                    "cpu_class": "medium", "mem_class": "medium"},
        "public-cloud": {"role": "public-cloud", "arch": "amd64", "cost": "high",
                         "rtt": "medium", "cpu_class": "high", "mem_class": "medium"},
    },
    "intent_profiles": {
        "latency-sensitive": {
            "propagation_requirements": {"region": "edge", "latency": "low", "cost": "any"},
            "preferred_classes": ["edge"],
        },
        "cost-aware": {
            "propagation_requirements": {"region": "any", "latency": "any", "cost": "low"},
            "preferred_classes": ["on-premises", "edge"],
        },
    },
}

# job → (intent profile, completed?, live PP target). fg-3 never finished
# but its decision landed; bg-0 is a pinned background job.
_JOBS = {
    "fg-0": ("latency-sensitive", True, "edge-1"),        # hard 1, soft 1.0
    "fg-1": ("latency-sensitive", True, "public-cloud"),  # hard 0, soft 0.0
    "fg-2": ("cost-aware", True, "on-prem"),              # hard 1, soft 0.5
    "fg-3": ("cost-aware", False, "public-cloud"),        # unfinished
}
_BG = {"bg-0": "on-prem"}


def _pp_doc(job: str, cluster: str) -> dict:
    return {
        "metadata": {"name": f"pp-{job}", "generation": 1,
                     "creationTimestamp": "2026-09-22T10:00:05Z"},
        "spec": {"placement": {"clusterAffinity": {"clusterNames": [cluster]}}},
    }


def _job_doc(job: str, completed: bool) -> dict:
    status = {"startTime": "2026-09-22T10:00:10Z"}
    if completed:
        status["completionTime"] = "2026-09-22T10:01:10Z"
        status["succeeded"] = 1
    return {"metadata": {"name": job}, "status": status}


class _SubprocessRecorder:
    def __init__(self) -> None:
        self.attempts: list = []

    def run(self, *a, **k):
        self.attempts.append(a)
        raise AssertionError(f"test attempted a real subprocess: {a}")


@pytest.fixture
def no_real_kubectl(monkeypatch):
    rec = _SubprocessRecorder()
    monkeypatch.setattr(_kube, "subprocess", rec)
    yield rec
    assert rec.attempts == [], f"real kubectl attempted: {rec.attempts}"


@pytest.fixture
def cluster(monkeypatch, no_real_kubectl):
    """Mocked aggregator. ``state['pps']`` / ``state['jobs']`` are mutable
    so a test can 'clean up the namespace' mid-way."""
    state = {
        "pps": {**{j: _pp_doc(j, c) for j, (_, _, c) in _JOBS.items()},
                **{j: _pp_doc(j, c) for j, c in _BG.items()}},
        "jobs": {j: _job_doc(j, done) for j, (_, done, _) in _JOBS.items()},
        "calls": 0,
    }

    def fake_kubectl_json(args, *, timeout=30):
        state["calls"] += 1
        assert args[0:3] == ["--context", _CTX, "get"], args
        kind, target = args[3], args[4]
        if kind == "propagationpolicy":
            if target.startswith("pp-"):
                return state["pps"].get(target[3:])
            return {"items": []}
        if kind == "job":
            return state["jobs"].get(target)
        return None

    monkeypatch.setattr(_kube, "_kubectl_json", fake_kubectl_json)
    return state


def _build_run(tmp_path: Path, name: str = "run-a") -> tuple[Path, Path]:
    registry = tmp_path / "cluster-registry.yaml"
    registry.write_text(yaml.safe_dump(_REGISTRY), encoding="utf-8")
    run_dir = tmp_path / "runs" / name
    run_dir.mkdir(parents=True)
    manifest = {
        "run_id": name, "baseline": "heuristic-scorer",
        "submitted_at": "2026-09-22T10:00:00+00:00",
        "finished_at": "2026-09-22T10:05:00+00:00",
        "jobs": [{"name": j, "intent_profile": p, "workload_class": "cpu",
                  "assigned_cluster": None} for j, (p, _, _) in _JOBS.items()],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (run_dir / "submission-log.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["job_name", "role", "state_id", "applied", "completed", "failed",
                    "skipped_reason", "wall_clock_seconds", "applied_at_utc", "job_path"])
        for j, (_, done, _) in _JOBS.items():
            w.writerow([j, "foreground", "", 1, int(done), 0, "" if done else "final-status=TimedOut",
                        "0.1", "2026-09-22T10:00:00+00:00", "/dev/null"])
        for j in _BG:
            w.writerow([j, "background", "", 1, 0, 0, "", "0.1",
                        "2026-09-22T09:59:00+00:00", "/dev/null"])
    with (run_dir / "timeline-planned.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["job_index", "job_name", "planned_submit_offset_seconds",
                    "workload_class", "intent_profile"])
        for i, (j, (p, _, _)) in enumerate(_JOBS.items()):
            w.writerow([i, j, f"{i * 5:.3f}", "cpu", p])
    return run_dir, registry


def _clone(run_dir: Path, name: str) -> Path:
    dst = run_dir.parent / name
    shutil.copytree(run_dir, dst, ignore=shutil.ignore_patterns("analysis"))
    return dst


def _analyse(run_dir: Path, registry: Path, *, use_kube: bool) -> dict:
    rc = run_analysis(run_dir=run_dir, registry_path=registry,
                      baseline="heuristic-scorer", use_db=False, use_kube=use_kube)
    assert rc == 0
    return json.loads((run_dir / "analysis" / "summary.json").read_text(encoding="utf-8"))


def _ic(summary: dict) -> dict:
    return summary["baselines"]["heuristic-scorer"]["intent_compliance"]


def _csv(run_dir: Path, name: str) -> bytes:
    return (run_dir / "analysis" / name).read_bytes()


def _tree_digest(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.iterdir()) if p.is_file()}


# ---------------------------------------------------------------------------
# 1-2. persisted placement reproduces the online analysis
# ---------------------------------------------------------------------------

def test_offline_with_persisted_placements_reproduces_online(tmp_path, cluster):
    online_dir, registry = _build_run(tmp_path)
    offline_dir = _clone(online_dir, "run-a-offline")

    # Today's online path: no persisted placement, live PP lookup.
    online = _analyse(online_dir, registry, use_kube=True)

    # Collection time on the other copy, then the namespace is cleaned up.
    summary = _collect.capture_kube_records(offline_dir)
    assert summary["placements_captured"] == len(_JOBS) and summary["placements_missing"] == []
    assert summary["job_status_captured"] == len(_JOBS) and summary["job_status_completed"] == 3
    cluster["pps"].clear()
    cluster["jobs"].clear()
    calls_before = cluster["calls"]
    offline = _analyse(offline_dir, registry, use_kube=False)
    assert cluster["calls"] == calls_before            # truly offline

    ic_on, ic_off = _ic(online), _ic(offline)
    assert ic_off == ic_on
    # Completed-only and completion-aware both scored, and they differ.
    assert ic_off["status"] == "ok" and ic_off["n"] == 3
    assert ic_off["hard_pass_rate"] == pytest.approx(2 / 3)
    assert ic_off["soft_score_mean"] == pytest.approx(1.5 / 3)
    assert ic_off["hard_pass_rate_completion_aware"] == pytest.approx(2 / 4)
    assert ic_off["soft_score_mean_completion_aware"] == pytest.approx(1.5 / 4)
    assert (ic_off["n_submitted"], ic_off["n_completed"]) == (4, 3)
    assert _csv(offline_dir, "intent-compliance.csv") == _csv(online_dir, "intent-compliance.csv")
    assert _csv(offline_dir, "data-movement.csv") == _csv(online_dir, "data-movement.csv")
    assert offline["notes"] == online["notes"] == []


def test_online_with_persisted_placements_after_cleanup(tmp_path, cluster):
    run_dir, registry = _build_run(tmp_path)
    reference = _ic(_analyse(_clone(run_dir, "ref"), registry, use_kube=True))
    _collect.capture_kube_records(run_dir)
    cluster["pps"].clear()          # PPs GC'd with their Jobs...
    cluster["jobs"].clear()
    summary = _analyse(run_dir, registry, use_kube=True)   # ...but kube still "available"
    assert _ic(summary) == reference
    assert summary["notes"] == []


# ---------------------------------------------------------------------------
# 3. no persisted placement → unchanged behaviour
# ---------------------------------------------------------------------------

def test_without_persisted_placements_offline_is_unchanged(tmp_path, cluster):
    run_dir, registry = _build_run(tmp_path)
    jobs, _ = _loaders.build_job_records(run_dir)
    assert all(j.selected_cluster is None and j.persisted_cluster is None for j in jobs)

    ic = _ic(_analyse(run_dir, registry, use_kube=False))
    assert cluster["calls"] == 0
    assert ic["status"] == "insufficient_data"
    assert ic["hard_pass_rate"] is None and ic["n"] == 0
    # PR #91 fail-loud rule: completed but unscorable → pair is None, not 0.
    assert ic["hard_pass_rate_completion_aware"] is None
    assert ic["soft_score_mean_completion_aware"] is None
    assert (ic["n_submitted"], ic["n_completed"]) == (4, 3)
    rows = _csv(run_dir, "intent-compliance.csv").decode("utf-8").strip().splitlines()
    assert len(rows) == 1           # header only


def test_without_persisted_placements_online_uses_live_pp(tmp_path, cluster):
    run_dir, registry = _build_run(tmp_path)
    summary = _analyse(run_dir, registry, use_kube=True)
    rows = list(csv.DictReader(io.StringIO(_csv(run_dir, "intent-compliance.csv").decode("utf-8"))))
    assert {r["job_name"]: r["selected_cluster"] for r in rows} == {
        "fg-0": "edge-1", "fg-1": "public-cloud", "fg-2": "on-prem",
    }
    assert summary["notes"] == []
    assert not (run_dir / "placements.json").exists()   # the analyzer never writes it


def test_partial_persisted_placement_keeps_the_gap_loud(tmp_path, cluster):
    """A completed job missing from placements.json is not invented
    offline: the completion-aware pair falls back to None."""
    run_dir, registry = _build_run(tmp_path)
    del cluster["pps"]["fg-1"]
    _collect.capture_kube_records(run_dir)
    assert _loaders.load_placements(run_dir)["fg-1"]["selected_cluster"] is None
    summary = _analyse(run_dir, registry, use_kube=False)
    ic = _ic(summary)
    assert ic["n"] == 2 and ic["n_completed"] == 3
    assert ic["hard_pass_rate_completion_aware"] is None
    note = summary["baselines"]["heuristic-scorer"]["intent_compliance"].get("note", "")
    assert "skipped fg-1" in note


# ---------------------------------------------------------------------------
# 4. disagreement
# ---------------------------------------------------------------------------

def test_disagreement_scores_persisted_and_is_logged(tmp_path, cluster, capsys):
    run_dir, registry = _build_run(tmp_path)
    _collect.capture_kube_records(run_dir)
    # The PP is later re-materialised elsewhere (live now disagrees for fg-0).
    cluster["pps"]["fg-0"] = _pp_doc("fg-0", "public-cloud")

    summary = _analyse(run_dir, registry, use_kube=True)
    rows = {r["job_name"]: r for r in csv.DictReader(
        io.StringIO(_csv(run_dir, "intent-compliance.csv").decode("utf-8")))}
    assert rows["fg-0"]["selected_cluster"] == "edge-1"      # persisted wins
    assert rows["fg-0"]["hard_pass"] == "1"
    expected = ("placement disagreement for fg-0: persisted=edge-1 live=public-cloud; "
                "scored the persisted (collection-time) value")
    assert summary["notes"] == [expected]                    # exactly once
    assert expected in capsys.readouterr().err

    # Still byte-idempotent on re-run.
    first = _tree_digest(run_dir / "analysis")
    _analyse(run_dir, registry, use_kube=True)
    assert _tree_digest(run_dir / "analysis") == first


# ---------------------------------------------------------------------------
# 5. secondary source, malformed artifact, PP provenance
# ---------------------------------------------------------------------------

def test_manifest_assigned_cluster_is_a_secondary_source(tmp_path, cluster):
    run_dir, registry = _build_run(tmp_path)
    reference = _ic(_analyse(_clone(run_dir, "ref"), registry, use_kube=True))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["jobs"]:
        entry["assigned_cluster"] = _JOBS[entry["name"]][2]
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert not (run_dir / "placements.json").exists()
    assert _ic(_analyse(run_dir, registry, use_kube=False)) == reference


def test_malformed_placements_is_an_input_error(tmp_path, cluster, capsys):
    run_dir, registry = _build_run(tmp_path)
    (run_dir / "placements.json").write_text('{"schema_version": 1}', encoding="utf-8")
    rc = run_analysis(run_dir=run_dir, registry_path=registry, baseline="heuristic-scorer",
                      use_db=False, use_kube=False)
    assert rc == 1
    assert "malformed" in capsys.readouterr().err


def test_pp_provenance_round_trips_offline(tmp_path, cluster):
    """pp_created_at (cordon decision-time classification) and
    pp_generation come back offline exactly as the live lookup reads them."""
    run_dir, _ = _build_run(tmp_path)
    _collect.capture_kube_records(run_dir)
    jobs = {j.name: j for j in _loaders.build_job_records(run_dir)[0]}
    live = _kube.pp_creation_time(_pp_doc("fg-0", "edge-1"))
    assert jobs["fg-0"].pp_created_at == live
    assert jobs["fg-0"].pp_generation == 1
    assert jobs["fg-0"].persisted_cluster == jobs["fg-0"].selected_cluster == "edge-1"
    assert jobs["bg-0"].selected_cluster is None           # background never captured


# ---------------------------------------------------------------------------
# Job.status timestamps: completion-time / decomposition / cost proxy
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)


def _stub_db() -> StubDB:
    """Decision rows for every foreground job (decision latency is DB-only)."""
    db = StubDB()
    rows = []
    for i, j in enumerate(_JOBS):
        rows.append({
            "id": f"req-{i}", "job_name": j, "status": "COMPLETED", "kind": "foreground",
            "created_at": _T0, "last_attempt_at": _T0.replace(microsecond=(i + 1) * 1000),
            "clusters_score": {"edge-1": 0.5}, "reason": "stub",
        })
    db.set_rows("request_for_jobs", rows)
    db.set_rows("no_pollution_invariant", [])
    db.set_rows("no_reverse_pollution_invariant", [])
    return db


def _analyse_db(run_dir: Path, registry: Path, *, use_kube: bool) -> dict:
    rc = run_analysis(run_dir=run_dir, registry_path=registry, baseline="heuristic-scorer",
                      db=_stub_db(), use_db=True, use_kube=use_kube)
    assert rc == 0
    return json.loads((run_dir / "analysis" / "summary.json").read_text(encoding="utf-8"))


_TIMESTAMP_CSVS = ("completion-time.csv", "completion-decomposition.csv",
                   "decision-latency.csv", "cost-proxy.csv")


def test_offline_with_persisted_records_is_byte_identical_to_online(tmp_path, cluster):
    online_dir, registry = _build_run(tmp_path)
    offline_dir = _clone(online_dir, "run-a-offline")

    online = _analyse_db(online_dir, registry, use_kube=True)
    _collect.capture_kube_records(offline_dir)
    cluster["pps"].clear()
    cluster["jobs"].clear()
    calls_before = cluster["calls"]
    offline = _analyse_db(offline_dir, registry, use_kube=False)
    assert cluster["calls"] == calls_before            # truly offline

    for name in _TIMESTAMP_CSVS:
        assert _csv(offline_dir, name) == _csv(online_dir, name), name
    # Stronger: every analysis output, summary.json included.
    assert _tree_digest(offline_dir / "analysis") == _tree_digest(online_dir / "analysis")

    b = offline["baselines"]["heuristic-scorer"]
    assert b["completion_time"]["status"] == "ok" and b["completion_time"]["n"] == 3
    assert b["completion_time"]["median_s"] == pytest.approx(70.0)       # 10:00:00 -> 10:01:10
    cd = b["completion_decomposition"]
    assert cd["status"] == "ok" and cd["n_observed"] == 3
    assert cd["median_wait_s"] == pytest.approx(10.0)                    # -> startTime 10:00:10
    assert cd["median_exec_s"] == pytest.approx(60.0)
    assert cd["n_skipped_not_completed"] == 1
    assert b["cost_proxy"]["status"] == "ok" and b["cost_proxy"]["n"] == 3
    assert b["cost_proxy"]["total"] == pytest.approx(70.0 * (1.0 + 3.0 + 1.0))
    assert b["decision_latency"]["status"] == "ok" and b["decision_latency"]["n"] == 4
    assert offline["notes"] == online["notes"] == []


def test_without_persisted_records_timestamp_metrics_are_unchanged(tmp_path, cluster):
    """No artifacts: offline has no Job timestamps (pending /
    insufficient_data, as before this change); online reads them live."""
    run_dir, registry = _build_run(tmp_path)
    b = _analyse(_clone(run_dir, "off"), registry, use_kube=False)["baselines"]["heuristic-scorer"]
    assert b["completion_time"]["status"] == "insufficient_data"
    assert b["completion_decomposition"]["status"] == "pending"
    assert b["cost_proxy"]["status"] == "insufficient_data"
    assert cluster["calls"] == 0
    b = _analyse(run_dir, registry, use_kube=True)["baselines"]["heuristic-scorer"]
    assert (b["completion_time"]["n"], b["completion_decomposition"]["n_observed"],
            b["cost_proxy"]["n"]) == (3, 3, 3)


def test_decision_latency_stays_db_only(tmp_path, cluster):
    """The kube records do not (and cannot) supply decision latency: it is
    the decision_requests lifecycle in PostgreSQL. Without the DB it is
    insufficient_data online and offline alike."""
    run_dir, registry = _build_run(tmp_path)
    _collect.capture_kube_records(run_dir)
    for use_kube in (True, False):
        b = _analyse(run_dir, registry, use_kube=use_kube)["baselines"]["heuristic-scorer"]
        assert b["decision_latency"]["status"] == "insufficient_data"
        assert b["completion_decomposition"]["status"] == "ok"


def test_job_status_disagreement_scores_persisted_and_is_logged(tmp_path, cluster, capsys):
    run_dir, registry = _build_run(tmp_path)
    _collect.capture_kube_records(run_dir)
    # The live Job later reports a different completionTime for fg-0.
    cluster["jobs"]["fg-0"]["status"]["completionTime"] = "2026-09-22T10:09:59Z"

    summary = _analyse(run_dir, registry, use_kube=True)
    rows = {r["job_name"]: r for r in csv.DictReader(
        io.StringIO(_csv(run_dir, "completion-time.csv").decode("utf-8")))}
    assert rows["fg-0"]["k8s_completion_time"] == "2026-09-22T10:01:10+00:00"   # persisted
    expected = ("job-status completion_time disagreement for fg-0: "
                "persisted=2026-09-22T10:01:10+00:00 live=2026-09-22T10:09:59+00:00; "
                "scored the persisted (collection-time) value")
    assert summary["notes"] == [expected]
    assert expected in capsys.readouterr().err
    first = _tree_digest(run_dir / "analysis")
    _analyse(run_dir, registry, use_kube=True)
    assert _tree_digest(run_dir / "analysis") == first


def test_null_persisted_timestamp_falls_back_to_live_then_backfill(tmp_path, cluster):
    """fg-3 was still running when the submit tail captured it. Online, the
    live completionTime fills the gap (and upgrades completed, as before);
    offline cannot know it - until a backfill capture records it."""
    run_dir, registry = _build_run(tmp_path)
    _collect.capture_kube_records(run_dir)
    assert _loaders.load_job_status(run_dir)["fg-3"]["completion_time"] is None
    cluster["jobs"]["fg-3"] = _job_doc("fg-3", True)          # finished later

    online = _analyse(_clone(run_dir, "online"), registry, use_kube=True)
    assert online["notes"] == []                              # a null is not a disagreement
    assert online["baselines"]["heuristic-scorer"]["completion_time"]["n"] == 4
    assert _ic(online)["n_completed"] == 4
    offline = _analyse(_clone(run_dir, "offline-before"), registry, use_kube=False)
    assert offline["baselines"]["heuristic-scorer"]["completion_time"]["n"] == 3

    _collect.capture_kube_records(run_dir)                    # collect --capture-kube-records
    after = _clone(run_dir, "offline-after")
    cluster["pps"].clear()
    cluster["jobs"].clear()
    _analyse(after, registry, use_kube=False)
    online_dir = run_dir.parent / "online"
    for name in ("completion-time.csv", "completion-decomposition.csv", "cost-proxy.csv",
                 "intent-compliance.csv"):
        assert _csv(after, name) == _csv(online_dir, name), name


def test_each_artifact_works_alone(tmp_path, cluster):
    run_dir, registry = _build_run(tmp_path)
    _collect.capture_kube_records(run_dir)
    times_only = _clone(run_dir, "times-only")
    (times_only / "placements.json").unlink()
    place_only = _clone(run_dir, "place-only")
    (place_only / "job-status.json").unlink()

    b = _analyse(times_only, registry, use_kube=False)["baselines"]["heuristic-scorer"]
    assert b["completion_time"]["n"] == 3 and b["completion_decomposition"]["n_observed"] == 3
    assert b["intent_compliance"]["status"] == "insufficient_data"   # no placement
    assert b["cost_proxy"]["status"] == "insufficient_data"          # needs the target too

    b = _analyse(place_only, registry, use_kube=False)["baselines"]["heuristic-scorer"]
    assert b["intent_compliance"]["status"] == "ok"
    assert b["completion_time"]["status"] == "insufficient_data"     # no timestamps


def test_manifest_completed_at_is_a_secondary_source(tmp_path, cluster):
    run_dir, registry = _build_run(tmp_path)
    reference = _analyse(_clone(run_dir, "ref"), registry, use_kube=True)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["jobs"]:
        _, done, target = _JOBS[entry["name"]]
        entry["assigned_cluster"] = target
        entry["completed_at"] = "2026-09-22T10:01:10Z" if done else None
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    b = _analyse(run_dir, registry, use_kube=False)["baselines"]["heuristic-scorer"]
    assert b["completion_time"] == reference["baselines"]["heuristic-scorer"]["completion_time"]
    assert b["cost_proxy"] == reference["baselines"]["heuristic-scorer"]["cost_proxy"]
    # No startTime in the manifest: the decomposition stays pending, not invented.
    assert b["completion_decomposition"]["status"] == "pending"


def test_malformed_job_status_is_an_input_error(tmp_path, cluster, capsys):
    run_dir, registry = _build_run(tmp_path)
    (run_dir / "job-status.json").write_text('{"schema_version": 1}', encoding="utf-8")
    rc = run_analysis(run_dir=run_dir, registry_path=registry, baseline="heuristic-scorer",
                      use_db=False, use_kube=False)
    assert rc == 1
    assert "malformed" in capsys.readouterr().err
