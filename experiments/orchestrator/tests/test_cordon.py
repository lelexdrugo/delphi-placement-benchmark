"""iter-4h-4: cordon.py — helpers + the timed cordon scheduler.

The scheduler runs with a fake karmadactl runner and tiny offsets so no
real cluster is touched and the test is fast.
"""
from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

import pytest
import yaml

from experiments.orchestrator import cordon as _cordon


def _timeline(tmp_path: Path, offsets: list[float]) -> Path:
    path = tmp_path / "timeline-planned.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["job_index", "job_name", "planned_submit_offset_seconds"])
        for i, off in enumerate(offsets):
            w.writerow([i, f"fg-{i}", f"{off:.3f}"])
    return path


def test_planned_window_seconds(tmp_path: Path):
    assert _cordon.planned_window_seconds(tmp_path / "missing.csv") == 0.0
    tl = _timeline(tmp_path, [0.0, 15.0, 7.0, 30.0])
    assert _cordon.planned_window_seconds(tl) == 30.0


def test_parse_cordon_window_valid():
    target, method, sf, ef, ctx = _cordon.parse_cordon_window({
        "cordon_target": "edge-1",
        "cordon_start_fraction": 0.33,
        "cordon_end_fraction": 0.66,
        "karmada_context": "unique-logical-entrypoint",
    })
    assert (target, method, sf, ef, ctx) == (
        "edge-1", "cordon", 0.33, 0.66, "unique-logical-entrypoint")


@pytest.mark.parametrize("bad", [
    {"cordon_start_fraction": 0.3, "cordon_end_fraction": 0.6},          # no target
    {"cordon_target": "edge-1", "cordon_method": "taint"},               # unsupported method
    {"cordon_target": "edge-1", "cordon_start_fraction": 0.7,
     "cordon_end_fraction": 0.3},                                        # start >= end
])
def test_parse_cordon_window_rejects(bad):
    with pytest.raises(ValueError):
        _cordon.parse_cordon_window(bad)


def test_marker_lifecycle_and_scan(tmp_path: Path):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    _cordon.write_marker(run_dir, "edge-1", "cordon")
    assert (run_dir / _cordon.MARKER_NAME).exists()
    assert _cordon.read_marker(run_dir)["cordon_target"] == "edge-1"

    # A present marker is surfaced by the stale scan over the runs root.
    stale = _cordon.scan_stale_markers(tmp_path / "runs")
    assert len(stale) == 1
    assert stale[0].target == "edge-1"
    assert "uncordon edge-1" in stale[0].recovery_command()

    _cordon.clear_marker(run_dir)
    assert not (run_dir / _cordon.MARKER_NAME).exists()
    assert _cordon.scan_stale_markers(tmp_path / "runs") == []


def test_run_cordon_verb_rejects_bad_verb():
    with pytest.raises(ValueError):
        _cordon.run_cordon_verb("delete", "edge-1", "ctx",
                                runner=lambda args: (0, ""))


def test_scheduler_cordons_then_uncordons(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_runner(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        return 0, ""

    stop = threading.Event()
    sched = _cordon.CordonScheduler(
        run_dir=run_dir, target="edge-1", method="cordon",
        karmada_context="unique-logical-entrypoint",
        start_offset_s=0.0, end_offset_s=0.05,
        started_monotonic=time.monotonic(),
        snapshot_ttl_seconds=15, planned_window_s=0.1,
        stop_event=stop, runner=fake_runner,
    )
    sched.start()
    sched.join(timeout=5)
    assert not sched.is_alive()

    # cordon first, then uncordon, both against the right cluster/context.
    verbs = [c[0] for c in calls]
    assert verbs == ["cordon", "uncordon"]
    assert calls[0][:2] == ["cordon", "edge-1"]
    assert "unique-logical-entrypoint" in calls[0]

    # timestamps file auto-written; marker cleared after clean uncordon.
    ts = yaml.safe_load((run_dir / _cordon.TIMESTAMPS_NAME).read_text())
    assert ts["cordon_target"] == "edge-1"
    assert ts["cordon_actual_start_timestamp"] is not None
    assert ts["cordon_actual_end_timestamp"] is not None
    assert not (run_dir / _cordon.MARKER_NAME).exists()


def test_scheduler_keeps_marker_when_uncordon_fails(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)

    def failing_uncordon(args: list[str]) -> tuple[int, str]:
        if args[0] == "uncordon":
            return 1, "boom"
        return 0, ""

    stop = threading.Event()
    sched = _cordon.CordonScheduler(
        run_dir=run_dir, target="edge-1", method="cordon",
        karmada_context="unique-logical-entrypoint",
        start_offset_s=0.0, end_offset_s=0.02,
        started_monotonic=time.monotonic(),
        snapshot_ttl_seconds=15, planned_window_s=0.1,
        stop_event=stop, runner=failing_uncordon,
    )
    sched.start()
    sched.join(timeout=5)
    # uncordon failed → marker is kept for crash recovery.
    assert (run_dir / _cordon.MARKER_NAME).exists()
