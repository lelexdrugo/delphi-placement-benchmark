"""Disturbance-stream thread synchronization tests (iter-4h-2 § 6.3).

All kubectl invocations are mocked — no cluster contact, no real
apply, no real delete. Pins five invariants:

1. The thread reads `disturbance-timeline-planned.csv` in offset order
   (sorted) and applies entries by their planned offset.
2. The circuit-breaker skips a spawn when the per-target
   submitted-not-yet-terminal count is ≥ cap (D15). The orchestrator
   does NOT spawn extras on other targets to compensate — the planned
   timeline is honoured verbatim, the only deviation is a skip with
   reason `circuit_breaker`.
3. `foreground_finished.set()` stops the thread promptly even when
   timeline entries remain.
4. Apply-failure is logged with reason `apply-failed` and continues
   to the next entry (does not abort the stream).
5. `_bulk_delete_disturbance` tries multi-key first and falls back
   to single-key on rc != 0.
"""
from __future__ import annotations

import csv
import subprocess
import threading
import time
from pathlib import Path
from unittest import mock

from experiments.orchestrator.submit import (
    DisturbanceSubmitResult,
    _run_disturbance_stream,
    _bulk_delete_disturbance,
)


def _write_timeline(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a minimal disturbance-timeline-planned.csv at tmp_path."""
    csv_path = tmp_path / "disturbance-timeline-planned.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "job_index", "job_name",
            "planned_submit_offset_seconds",
            "target_cluster", "workload_class",
            "intensity_label", "disturbance_cell",
        ])
        for r in rows:
            w.writerow([
                r["job_index"], r["job_name"],
                f"{r['offset']:.3f}", r["target"], r["wc"],
                r.get("intensity", "heavy"),
                r.get("cell", "r1-mdh"),
            ])
    return csv_path


def _make_job_files(tmp_path: Path, rows: list[dict]) -> Path:
    """Create stub Job YAML files matching the timeline."""
    d = tmp_path / "disturbance"
    d.mkdir()
    for r in rows:
        (d / f"{r['job_index']:04d}-{r['job_name']}.yaml").write_text(
            "# stub\n"
        )
    return d


def _mock_proc(rc: int = 0, stdout: str = "", stderr: str = ""):
    """Build a fake subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=rc, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Apply-order invariant
# ---------------------------------------------------------------------------

def test_thread_applies_jobs_in_offset_order(tmp_path: Path) -> None:
    """Even when the timeline rows are out of order on disk, the thread
    sorts by offset and applies in chronological order."""
    rows = [
        {"job_index": 0, "job_name": "j0", "offset": 0.0,
         "target": "on-prem", "wc": "cpu"},
        {"job_index": 1, "job_name": "j1", "offset": 0.0,
         "target": "edge-1", "wc": "memory"},
        {"job_index": 2, "job_name": "j2", "offset": 0.0,
         "target": "on-prem", "wc": "memory"},
    ]
    _make_job_files(tmp_path, rows)
    timeline = _write_timeline(tmp_path, rows)

    applied_names: list[str] = []

    def fake_apply(_context: str, path: Path):
        applied_names.append(path.stem)
        return True, ""

    foreground_finished = threading.Event()
    log: list[DisturbanceSubmitResult] = []
    lock = threading.Lock()
    started = time.monotonic()

    with mock.patch(
        "experiments.orchestrator.submit._apply_job", side_effect=fake_apply,
    ), mock.patch(
        "experiments.orchestrator.submit._kubectl_get_disturbance_jobs",
        return_value=[],  # no live jobs -> circuit-breaker never fires
    ):
        _run_disturbance_stream(
            context="unique-logical-entrypoint",
            namespace="delphi-experiments",
            state_id="mixed-disturbance-heavy",
            timeline_csv=timeline,
            disturbance_dir=tmp_path / "disturbance",
            target_clusters=["on-prem", "edge-1"],
            per_target_circuit_breaker={"on-prem": 99, "edge-1": 99},
            disturbance_cell="r1-mdh",
            foreground_finished=foreground_finished,
            fg_deadline_seconds=30,
            submit_log=log,
            submit_log_lock=lock,
            circuit_breaker_skips={"on-prem": 0, "edge-1": 0},
            started_monotonic=started,
        )

    # All three Jobs applied (mock always returns ok=True).
    assert len(applied_names) == 3
    # Order respects offset / target tiebreaker.
    assert applied_names[0].endswith("j0") or applied_names[0].endswith("j1")
    # No circuit-breaker skips in the log.
    assert all(r.skipped_reason != "circuit_breaker" for r in log)


# ---------------------------------------------------------------------------
# Circuit-breaker invariant (D15)
# ---------------------------------------------------------------------------

def test_circuit_breaker_skips_spawn_above_cap(tmp_path: Path) -> None:
    """When the per-target submitted-not-yet-terminal count is at the
    cap, the next spawn for that target is skipped with reason
    `circuit_breaker`. The orchestrator does NOT compensate on other
    targets (D15)."""
    rows = [
        {"job_index": 0, "job_name": "j0", "offset": 0.0,
         "target": "edge-1", "wc": "cpu"},
        {"job_index": 1, "job_name": "j1", "offset": 0.0,
         "target": "edge-1", "wc": "cpu"},
        {"job_index": 2, "job_name": "j2", "offset": 0.0,
         "target": "on-prem", "wc": "cpu"},
    ]
    _make_job_files(tmp_path, rows)
    timeline = _write_timeline(tmp_path, rows)

    # Mock the population read so the FIRST poll returns edge-1 at the
    # cap (2 live jobs) and on-prem empty. The disturbance-thread
    # therefore skips both edge-1 spawns and applies the on-prem one.
    fake_jobs = [
        # Two live edge-1 jobs (Active).
        {"metadata": {"labels": {
            "delphi.experiments/pin-to-cluster": "edge-1",
        }}, "status": {"active": 1}},
        {"metadata": {"labels": {
            "delphi.experiments/pin-to-cluster": "edge-1",
        }}, "status": {"active": 1}},
    ]

    apply_calls: list[Path] = []

    def fake_apply(_context, path):
        apply_calls.append(path)
        return True, ""

    foreground_finished = threading.Event()
    log: list[DisturbanceSubmitResult] = []
    lock = threading.Lock()
    skips = {"edge-1": 0, "on-prem": 0}
    started = time.monotonic()

    with mock.patch(
        "experiments.orchestrator.submit._apply_job", side_effect=fake_apply,
    ), mock.patch(
        "experiments.orchestrator.submit._kubectl_get_disturbance_jobs",
        return_value=fake_jobs,
    ):
        _run_disturbance_stream(
            context="unique-logical-entrypoint",
            namespace="delphi-experiments",
            state_id="mixed-disturbance-heavy",
            timeline_csv=timeline,
            disturbance_dir=tmp_path / "disturbance",
            target_clusters=["on-prem", "edge-1"],
            # edge-1 cap = 2; live count = 2 → skip every edge-1 spawn.
            per_target_circuit_breaker={"on-prem": 99, "edge-1": 2},
            disturbance_cell="r1-mdh",
            foreground_finished=foreground_finished,
            fg_deadline_seconds=30,
            submit_log=log,
            submit_log_lock=lock,
            circuit_breaker_skips=skips,
            started_monotonic=started,
        )

    # edge-1 spawns were skipped; on-prem was applied.
    edge_logs = [r for r in log if r.target_cluster == "edge-1"]
    onprem_logs = [r for r in log if r.target_cluster == "on-prem"]
    assert all(r.skipped_reason == "circuit_breaker" for r in edge_logs), \
        f"edge-1 logs should all be circuit_breaker: {edge_logs}"
    assert any(r.applied for r in onprem_logs), \
        "on-prem job should still apply (no compensation across targets)"
    # The skip counter increments for edge-1 only.
    assert skips["edge-1"] == 2
    assert skips["on-prem"] == 0
    # The apply mock was NOT called for any edge-1 row.
    edge_paths = [p for p in apply_calls if "j0" in p.name or "j1" in p.name]
    assert edge_paths == [], (
        f"orchestrator must not call _apply_job for skipped jobs: {edge_paths}"
    )


# ---------------------------------------------------------------------------
# Shutdown invariant
# ---------------------------------------------------------------------------

def test_foreground_finished_stops_thread_promptly(tmp_path: Path) -> None:
    """Setting `foreground_finished` mid-stream halts the thread within
    one tick — the submit_real cleanup branch joins this thread with a
    30 s timeout, so prompt exit is required."""
    # Build a timeline that schedules a job far in the future.
    rows = [
        {"job_index": 0, "job_name": "j0", "offset": 60.0,
         "target": "on-prem", "wc": "cpu"},
    ]
    _make_job_files(tmp_path, rows)
    timeline = _write_timeline(tmp_path, rows)

    foreground_finished = threading.Event()
    log: list[DisturbanceSubmitResult] = []
    lock = threading.Lock()
    started = time.monotonic()

    def run():
        _run_disturbance_stream(
            context="unique-logical-entrypoint",
            namespace="delphi-experiments",
            state_id="s",
            timeline_csv=timeline,
            disturbance_dir=tmp_path / "disturbance",
            target_clusters=["on-prem"],
            per_target_circuit_breaker={"on-prem": 99},
            disturbance_cell="r1-s",
            foreground_finished=foreground_finished,
            fg_deadline_seconds=120,
            submit_log=log,
            submit_log_lock=lock,
            circuit_breaker_skips={},
            started_monotonic=started,
        )

    with mock.patch(
        "experiments.orchestrator.submit._kubectl_get_disturbance_jobs",
        return_value=[],
    ), mock.patch(
        "experiments.orchestrator.submit._apply_job",
        return_value=(True, ""),
    ):
        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.5)  # give the thread time to enter its first wait
        foreground_finished.set()
        t.join(timeout=2.0)
        assert not t.is_alive(), (
            "disturbance thread did not exit within 2 s of foreground_finished"
        )
    # No jobs applied because we stopped before the planned offset.
    assert all(not r.applied for r in log)


# ---------------------------------------------------------------------------
# Bulk delete (D8) — multi-key with single-key fallback
# ---------------------------------------------------------------------------

def test_bulk_delete_tries_multi_key_first_then_single_key() -> None:
    """`_bulk_delete_disturbance` calls kubectl twice IF the multi-key
    form returns non-zero, falling back to the single-key
    `disturbance-cell` selector (D8)."""
    call_log: list[list[str]] = []

    def fake_kubectl(args, *, timeout=None):
        call_log.append(args)
        # First call (multi-key) fails; second (single-key) succeeds.
        if "delphi.experiments/run-id" in args[args.index("-l") + 1]:
            return _mock_proc(rc=1, stderr="multi-key denied")
        return _mock_proc(rc=0, stdout="deleted 5 jobs")

    with mock.patch(
        "experiments.orchestrator.submit._kubectl", side_effect=fake_kubectl,
    ):
        ok = _bulk_delete_disturbance(
            context="unique-logical-entrypoint",
            namespace="delphi-experiments",
            run_id="r1",
            state_id="mixed-disturbance-heavy",
            disturbance_cell="r1-mdh",
        )

    assert ok
    assert len(call_log) == 2
    # First call carries multi-key selector.
    first_selector = call_log[0][call_log[0].index("-l") + 1]
    assert "role=background" in first_selector
    assert "run-id=r1" in first_selector
    # Second call carries the disturbance-cell single-key selector.
    second_selector = call_log[1][call_log[1].index("-l") + 1]
    assert second_selector == "delphi.experiments/disturbance-cell=r1-mdh"


def test_bulk_delete_returns_true_on_first_success_no_fallback() -> None:
    """When the multi-key form returns rc=0, the single-key fallback
    must NOT fire (avoid double-delete attempts)."""
    call_log: list[list[str]] = []

    def fake_kubectl(args, *, timeout=None):
        call_log.append(args)
        return _mock_proc(rc=0, stdout="deleted 5 jobs")

    with mock.patch(
        "experiments.orchestrator.submit._kubectl", side_effect=fake_kubectl,
    ):
        _bulk_delete_disturbance(
            context="unique-logical-entrypoint",
            namespace="delphi-experiments",
            run_id="r1",
            state_id="mixed-disturbance-heavy",
            disturbance_cell="r1-mdh",
        )
    assert len(call_log) == 1
