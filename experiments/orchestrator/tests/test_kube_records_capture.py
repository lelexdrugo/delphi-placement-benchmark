"""Collection-time persistence of what the analyzer reads live
(``collect.capture_kube_records`` → ``placements.json`` + ``job-status.json``).

Every cluster interaction is mocked: the analyzer's kubectl seam
(``analysis.kube._kubectl_json``) is replaced by a fake serving
PropagationPolicy and Job documents, and ``analysis.kube.subprocess`` is
swapped for a recorder that fails the test at teardown if any real process
spawn was attempted (capture deliberately swallows exceptions, so a
raising guard alone could be silenced).
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from experiments.orchestrator import cli as _cli
from experiments.orchestrator import collect as _collect
from experiments.orchestrator import submit as _submit
from experiments.orchestrator.analysis import kube as _kube

_CTX = "unique-logical-entrypoint"
_NS = "delphi-experiments"
_FIXED_NOW = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 9, 22, 11, 0, 0, tzinfo=timezone.utc)


def _pp(job: str, cluster: str, *, generation: int = 1) -> dict:
    return {
        "metadata": {"name": f"pp-{job}", "generation": generation,
                     "creationTimestamp": "2026-09-22T09:59:30Z"},
        "spec": {"placement": {"clusterAffinity": {"clusterNames": [cluster]}}},
    }


def _job(job: str, start: str | None = "2026-09-22T09:59:40Z",
         completion: str | None = "2026-09-22T10:00:40Z") -> dict:
    status: dict = {}
    if start:
        status["startTime"] = start
    if completion:
        status["completionTime"] = completion
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
def fake_kube(monkeypatch, no_real_kubectl):
    """Serve PPs / Jobs from dicts through the analyzer's kubectl seam and
    record every kubectl argv."""
    state = {"pps": {}, "jobs": {}, "calls": []}

    def fake_kubectl_json(args, *, timeout=30):
        state["calls"].append(list(args))
        assert args[0:2] == ["--context", _CTX], args
        assert args[2] == "get", f"non-read kubectl verb: {args}"
        kind, target = args[3], args[4]
        if kind == "propagationpolicy" and target.startswith("pp-"):
            return state["pps"].get(target[3:])
        if kind == "propagationpolicy":  # namespace list fallback
            return {"items": []}
        if kind == "job":
            return state["jobs"].get(target)
        return None

    monkeypatch.setattr(_kube, "_kubectl_json", fake_kubectl_json)
    return state


def _write_submission_log(run_dir: Path, rows: list[tuple[str, str, int, int]]) -> None:
    """rows: (job_name, role, applied, completed)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "submission-log.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["job_name", "role", "state_id", "applied", "completed", "failed",
                    "skipped_reason", "wall_clock_seconds", "applied_at_utc", "job_path"])
        for name, role, applied, completed in rows:
            w.writerow([name, role, "", applied, completed, 0, "", "0.1",
                        "2026-09-22T09:59:00+00:00" if applied else "", "/dev/null"])


def _write_manifest(run_dir: Path, names: list[str]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_dir.name, "baseline": "Heuristic-Scorer",
        "submitted_at": None, "finished_at": None, "notes": [],
        "jobs": [{"name": n, "assigned_cluster": None, "completed_at": None,
                  "exit": "not_run", "actual_submit_timestamp_utc": None} for n in names],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True),
                                           encoding="utf-8")


def _doc(run_dir: Path, name: str) -> dict:
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


def _placements(run_dir: Path) -> dict:
    return _doc(run_dir, "placements.json")["placements"]


def _job_status(run_dir: Path) -> dict:
    return _doc(run_dir, "job-status.json")["jobs"]


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------

def test_capture_persists_applied_foreground_records(tmp_path, fake_kube):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [
        ("fg-a", "foreground", 1, 1),
        ("fg-b", "foreground", 1, 0),     # applied, still running: recorded without completion
        ("fg-c", "foreground", 1, 1),     # no PP and no Job at collection time
        ("fg-d", "foreground", 0, 0),     # never applied: not looked up
        ("bg-a", "background", 1, 1),     # pinned background: never scored
    ])
    fake_kube["pps"] = {"fg-a": _pp("fg-a", "edge-1"), "fg-b": _pp("fg-b", "public-cloud", generation=2),
                        "bg-a": _pp("bg-a", "on-prem")}
    fake_kube["jobs"] = {"fg-a": _job("fg-a"), "fg-b": _job("fg-b", completion=None),
                         "bg-a": _job("bg-a")}

    summary = _collect.capture_kube_records(run_dir, now=lambda: _FIXED_NOW)

    assert (summary["placements_captured"], summary["placements_missing"]) == (2, ["fg-c"])
    assert (summary["job_status_captured"], summary["job_status_completed"]) == (2, 1)
    assert summary["job_status_missing"] == ["fg-c"]
    assert summary["aborted"] is None

    pdoc = _doc(run_dir, "placements.json")
    assert (pdoc["schema_version"], pdoc["context"], pdoc["namespace"]) == (1, _CTX, _NS)
    assert pdoc["source"] == "PropagationPolicy.spec.placement.clusterAffinity.clusterNames[0]"
    assert sorted(pdoc["placements"]) == ["fg-a", "fg-b", "fg-c"]
    assert pdoc["placements"]["fg-a"] == {
        "selected_cluster": "edge-1", "pp_name": "pp-fg-a", "pp_generation": 1,
        "pp_created_at": "2026-09-22T09:59:30Z", "recorded_at": _FIXED_NOW.isoformat(),
    }
    assert pdoc["placements"]["fg-b"]["pp_generation"] == 2
    assert pdoc["placements"]["fg-c"]["selected_cluster"] is None

    jdoc = _doc(run_dir, "job-status.json")
    assert (jdoc["schema_version"], jdoc["context"], jdoc["namespace"]) == (1, _CTX, _NS)
    assert jdoc["source"] == "Job.status (aggregated, Karmada entrypoint)"
    assert sorted(jdoc["jobs"]) == ["fg-a", "fg-b", "fg-c"]
    assert jdoc["jobs"]["fg-a"] == {                         # verbatim RFC 3339 strings
        "start_time": "2026-09-22T09:59:40Z", "completion_time": "2026-09-22T10:00:40Z",
        "recorded_at": _FIXED_NOW.isoformat(),
    }
    assert jdoc["jobs"]["fg-b"]["completion_time"] is None
    assert jdoc["jobs"]["fg-c"] == {"start_time": None, "completion_time": None,
                                    "recorded_at": _FIXED_NOW.isoformat()}

    # Only read-only gets, only for applied foreground jobs.
    targets = {(c[3], c[4]) for c in fake_kube["calls"] if c[4] != "-n"}
    assert targets == {("propagationpolicy", f"pp-{n}") for n in ("fg-a", "fg-b", "fg-c")} | {
        ("job", n) for n in ("fg-a", "fg-b", "fg-c")}


def test_capture_output_is_deterministic(tmp_path, fake_kube):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [("fg-b", "foreground", 1, 1), ("fg-a", "foreground", 1, 1)])
    fake_kube["pps"] = {"fg-a": _pp("fg-a", "edge-1"), "fg-b": _pp("fg-b", "edge-2")}
    fake_kube["jobs"] = {"fg-a": _job("fg-a"), "fg-b": _job("fg-b")}
    _collect.capture_kube_records(run_dir, now=lambda: _FIXED_NOW)
    first = {n: (run_dir / n).read_bytes() for n in ("placements.json", "job-status.json")}
    _collect.capture_kube_records(run_dir, now=lambda: _FIXED_NOW)
    assert {n: (run_dir / n).read_bytes() for n in first} == first


def test_capture_never_replaces_a_recorded_placement(tmp_path, fake_kube, capsys):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [("fg-a", "foreground", 1, 1), ("fg-b", "foreground", 1, 1)])
    fake_kube["pps"] = {"fg-a": _pp("fg-a", "edge-1")}       # fg-b decision not landed yet
    _collect.capture_kube_records(run_dir, now=lambda: _FIXED_NOW)
    assert _placements(run_dir)["fg-b"]["selected_cluster"] is None

    # Later re-capture: fg-a's PP now points elsewhere, fg-b's PP appeared.
    fake_kube["pps"] = {"fg-a": _pp("fg-a", "public-cloud"), "fg-b": _pp("fg-b", "edge-2")}
    _collect.capture_kube_records(run_dir, now=lambda: _LATER)
    doc = _placements(run_dir)
    assert doc["fg-a"]["selected_cluster"] == "edge-1"                 # earliest kept
    assert doc["fg-a"]["recorded_at"] == _FIXED_NOW.isoformat()
    assert doc["fg-b"]["selected_cluster"] == "edge-2"                 # null filled
    assert "keeping recorded 'edge-1'" in capsys.readouterr().err

    # After cleanup every PP is gone: a re-capture must not erase anything.
    fake_kube["pps"] = {}
    _collect.capture_kube_records(run_dir, now=lambda: _LATER)
    doc = _placements(run_dir)
    assert (doc["fg-a"]["selected_cluster"], doc["fg-b"]["selected_cluster"]) == ("edge-1", "edge-2")


def test_capture_job_status_merges_per_field(tmp_path, fake_kube, capsys):
    """A job still running at the submit tail gets its completionTime
    filled by a later backfill; recorded values are never replaced."""
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [("fg-a", "foreground", 1, 0)])
    fake_kube["pps"] = {"fg-a": _pp("fg-a", "edge-1")}
    fake_kube["jobs"] = {"fg-a": _job("fg-a", completion=None)}
    _collect.capture_kube_records(run_dir, now=lambda: _FIXED_NOW)
    assert _job_status(run_dir)["fg-a"]["completion_time"] is None

    # Backfill: completion arrived; the live startTime also (oddly) moved.
    fake_kube["jobs"] = {"fg-a": _job("fg-a", start="2026-09-22T09:59:59Z",
                                      completion="2026-09-22T10:05:00Z")}
    _collect.capture_kube_records(run_dir, now=lambda: _LATER)
    rec = _job_status(run_dir)["fg-a"]
    assert rec["start_time"] == "2026-09-22T09:59:40Z"          # recorded value kept
    assert rec["completion_time"] == "2026-09-22T10:05:00Z"     # null filled
    assert rec["recorded_at"] == _LATER.isoformat()             # moved because it changed
    assert "keeping recorded '2026-09-22T09:59:40Z'" in capsys.readouterr().err

    # After cleanup (Job gone) nothing is erased and recorded_at stays.
    fake_kube["jobs"] = {}
    _collect.capture_kube_records(run_dir, now=lambda: datetime(2026, 9, 23, tzinfo=timezone.utc))
    assert _job_status(run_dir)["fg-a"] == rec


def test_capture_kubectl_missing_is_non_fatal(tmp_path, capsys):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [("fg-a", "foreground", 1, 1), ("fg-b", "foreground", 1, 1)])
    calls = []

    def no_kubectl(_ctx, _ns, name):
        calls.append(name)
        raise FileNotFoundError("kubectl")

    summary = _collect.capture_kube_records(run_dir, fetch_pp=no_kubectl, fetch_job=no_kubectl)
    assert calls == ["fg-a"]                        # aborts after the first failure
    assert summary["aborted"].startswith("FileNotFoundError")
    assert not (run_dir / "placements.json").exists()
    assert not (run_dir / "job-status.json").exists()
    assert "lookup aborted" in capsys.readouterr().err


def test_capture_timeout_keeps_what_was_captured(tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [(f"fg-{i}", "foreground", 1, 1) for i in range(3)])

    def flaky_job(_ctx, _ns, name):
        if name == "fg-1":
            raise subprocess.TimeoutExpired(cmd="kubectl", timeout=30)
        return _job(name)

    summary = _collect.capture_kube_records(
        run_dir, fetch_pp=lambda _c, _n, name: _pp(name, "edge-1"), fetch_job=flaky_job)
    assert summary["aborted"].startswith("TimeoutExpired")
    assert sorted(_placements(run_dir)) == ["fg-0"]
    assert sorted(_job_status(run_dir)) == ["fg-0"]


def test_capture_one_lookup_failing_keeps_the_other(tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [("fg-a", "foreground", 1, 1)])

    def broken(_ctx, _ns, _name):
        raise RuntimeError("aggregator hiccup")

    summary = _collect.capture_kube_records(
        run_dir, fetch_pp=lambda _c, _n, name: _pp(name, "edge-1"), fetch_job=broken)
    assert summary["aborted"] is None
    assert _placements(run_dir)["fg-a"]["selected_cluster"] == "edge-1"
    assert not (run_dir / "job-status.json").exists()
    assert summary["job_status_missing"] == ["fg-a"]


def test_capture_refuses_to_overwrite_malformed_artifact(tmp_path, fake_kube):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [("fg-a", "foreground", 1, 1)])
    (run_dir / "job-status.json").write_text("{not json", encoding="utf-8")
    summary = _collect.capture_kube_records(run_dir)
    assert "malformed" in summary["aborted"]
    assert (run_dir / "job-status.json").read_text(encoding="utf-8") == "{not json"
    assert not (run_dir / "placements.json").exists()


# ---------------------------------------------------------------------------
# manifest mirror (collect_results stays cluster-free)
# ---------------------------------------------------------------------------

def test_collect_results_mirrors_assigned_cluster_and_completed_at(tmp_path, fake_kube):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [("fg-a", "foreground", 1, 1), ("fg-b", "foreground", 1, 1)])
    _write_manifest(run_dir, ["fg-a", "fg-b"])
    fake_kube["pps"] = {"fg-a": _pp("fg-a", "edge-1")}
    fake_kube["jobs"] = {"fg-a": _job("fg-a"), "fg-b": _job("fg-b", completion=None)}
    _collect.capture_kube_records(run_dir)
    n_calls = len(fake_kube["calls"])

    manifest = _collect.collect_results(run_dir)
    assert len(fake_kube["calls"]) == n_calls          # collect_results is cluster-free
    by_name = {j["name"]: j for j in manifest["jobs"]}
    assert by_name["fg-a"]["assigned_cluster"] == "edge-1"
    assert by_name["fg-a"]["completed_at"] == "2026-09-22T10:00:40Z"
    assert by_name["fg-b"]["assigned_cluster"] is None   # nothing recorded → stays null
    assert by_name["fg-b"]["completed_at"] is None


def test_collect_results_without_artifacts_is_unchanged(tmp_path, fake_kube):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [("fg-a", "foreground", 1, 1)])
    _write_manifest(run_dir, ["fg-a"])
    manifest = _collect.collect_results(run_dir)
    assert manifest["jobs"][0]["assigned_cluster"] is None
    assert manifest["jobs"][0]["completed_at"] is None
    assert manifest["jobs"][0]["exit"] == "succeeded"
    assert fake_kube["calls"] == []


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def _submit_args(**overrides) -> argparse.Namespace:
    base = dict(run_id="r1", runs="runs", cluster_states_dir="specs/cluster-states",
                context=_CTX, namespace=_NS, fg_deadline_seconds=60, poll_seconds=1,
                keep_background=False, keep_disturbance=False,
                stall_threshold_seconds=0, fail_fast=False,
                i_know_this_runs_real_jobs=True)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_submit_tail_persists_records_before_collect(tmp_path, fake_kube, monkeypatch):
    """`submit` (the harness step right after the jobs finish, while every
    Job and PP still exists) writes both artifacts and the manifest mirror."""
    run_dir = tmp_path / "runs" / "r1"
    _write_manifest(run_dir, ["fg-a", "fg-b"])

    def fake_submit_real(*, run_dir, **_kw):
        _write_submission_log(run_dir, [("fg-a", "foreground", 1, 1), ("fg-b", "foreground", 1, 0)])
        return 2   # a timed-out job: capture must still run

    monkeypatch.setattr(_submit, "submit_real", fake_submit_real)
    fake_kube["pps"] = {"fg-a": _pp("fg-a", "edge-1"), "fg-b": _pp("fg-b", "edge-2")}
    fake_kube["jobs"] = {"fg-a": _job("fg-a"), "fg-b": _job("fg-b", completion=None)}

    rc = _cli._cmd_submit(_submit_args(), tmp_path)

    assert rc == 2
    doc = _placements(run_dir)
    assert (doc["fg-a"]["selected_cluster"], doc["fg-b"]["selected_cluster"]) == ("edge-1", "edge-2")
    js = _job_status(run_dir)
    assert js["fg-a"]["completion_time"] == "2026-09-22T10:00:40Z"
    assert js["fg-b"]["start_time"] == "2026-09-22T09:59:40Z" and js["fg-b"]["completion_time"] is None
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [j["assigned_cluster"] for j in manifest["jobs"]] == ["edge-1", "edge-2"]
    assert [j["completed_at"] for j in manifest["jobs"]] == ["2026-09-22T10:00:40Z", None]


def test_submit_tail_survives_capture_failure(tmp_path, monkeypatch, no_real_kubectl):
    run_dir = tmp_path / "runs" / "r1"
    _write_manifest(run_dir, ["fg-a"])

    def fake_submit_real(*, run_dir, **_kw):
        _write_submission_log(run_dir, [("fg-a", "foreground", 1, 1)])
        return 0

    def boom(*_a, **_k):
        raise RuntimeError("aggregator exploded")

    monkeypatch.setattr(_submit, "submit_real", fake_submit_real)
    monkeypatch.setattr(_kube, "get_propagation_policy", boom)
    monkeypatch.setattr(_kube, "get_job", boom)

    assert _cli._cmd_submit(_submit_args(), tmp_path) == 0
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["jobs"][0]["exit"] == "succeeded"          # collection still happened
    assert manifest["jobs"][0]["assigned_cluster"] is None


def test_collect_command_is_cluster_free_unless_asked(tmp_path, fake_kube):
    run_dir = tmp_path / "runs" / "r1"
    _write_submission_log(run_dir, [("fg-a", "foreground", 1, 1)])
    _write_manifest(run_dir, ["fg-a"])
    fake_kube["pps"] = {"fg-a": _pp("fg-a", "edge-1")}
    fake_kube["jobs"] = {"fg-a": _job("fg-a")}
    args = argparse.Namespace(run_id="r1", runs="runs", capture_kube_records=False,
                              context=_CTX, namespace=_NS)

    assert _cli._cmd_collect(args, tmp_path) == 0
    assert fake_kube["calls"] == []
    assert not (run_dir / "placements.json").exists()
    assert not (run_dir / "job-status.json").exists()

    args.capture_kube_records = True
    assert _cli._cmd_collect(args, tmp_path) == 0
    assert _placements(run_dir)["fg-a"]["selected_cluster"] == "edge-1"
    assert _job_status(run_dir)["fg-a"]["completion_time"] == "2026-09-22T10:00:40Z"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["jobs"][0]["assigned_cluster"] == "edge-1"
    assert manifest["jobs"][0]["completed_at"] == "2026-09-22T10:00:40Z"


def test_collect_parser_exposes_capture_flag():
    parser = _cli._build_parser()
    args = parser.parse_args(["collect", "--run-id", "r1", "--capture-kube-records"])
    assert args.capture_kube_records is True
    assert (args.context, args.namespace) == (_CTX, _NS)
    assert parser.parse_args(["collect", "--run-id", "r1"]).capture_kube_records is False
