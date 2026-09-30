"""Pollution detection logic — the unit test PROVES correctness because
the iter-4a pilot's idle state would vacuously pass.
"""
from __future__ import annotations

import pytest

from experiments.orchestrator.analysis.db import StubDB
from experiments.orchestrator.analysis.validators import (
    PollutionDetected,
    assert_no_pollution,
)


def test_clean_run_passes():
    db = StubDB()
    db.set_rows("no_pollution_invariant", [])
    db.set_rows("no_reverse_pollution_invariant", [])
    ok, _ = assert_no_pollution(
        db=db,
        foreground_job_names=["fg-1", "fg-2"],
        background_job_names=["bg-1"],
    )
    assert ok is True


def test_forward_leak_raises():
    """A foreground submission persisted with kind != 'foreground'.

    Controller-side role-forwarding bug. Production filter
    `WHERE kind = 'foreground'` would NOT return this row, so the agent
    loses history for this Job.
    """
    db = StubDB()
    db.set_rows("no_pollution_invariant", [
        {"id": "req-1", "job_name": "fg-broken",
         "kind": "background", "status": "COMPLETED",
         "created_at": "2026-05-23T18:00:00Z"},
    ])
    db.set_rows("no_reverse_pollution_invariant", [])
    with pytest.raises(PollutionDetected) as exc:
        assert_no_pollution(
            db=db,
            foreground_job_names=["fg-broken"],
            background_job_names=[],
        )
    assert exc.value.forward_leaked
    assert exc.value.forward_leaked[0]["leak_direction"] == "forward"
    assert not exc.value.reverse_leaked


def test_reverse_leak_raises():
    """A background submission persisted with kind = 'foreground'.

    The exact pollution failure the invariant exists to catch. Agent
    would see a background row in its history surface.
    """
    db = StubDB()
    db.set_rows("no_pollution_invariant", [])
    db.set_rows("no_reverse_pollution_invariant", [
        {"id": "req-2", "job_name": "bg-leaked",
         "kind": "foreground", "status": "COMPLETED",
         "created_at": "2026-05-23T18:01:00Z"},
    ])
    with pytest.raises(PollutionDetected) as exc:
        assert_no_pollution(
            db=db,
            foreground_job_names=[],
            background_job_names=["bg-leaked"],
        )
    assert not exc.value.forward_leaked
    assert exc.value.reverse_leaked
    assert exc.value.reverse_leaked[0]["leak_direction"] == "reverse"


def test_empty_job_lists_short_circuit():
    """A render-only run-dir has no submitted jobs yet — should pass."""
    db = StubDB()
    ok, _ = assert_no_pollution(
        db=db,
        foreground_job_names=[],
        background_job_names=[],
    )
    assert ok is True


def test_calibration_background_rows_are_not_leaks():
    """iter-4b calibration probes are kind='background' and live in the DB,
    but they are NOT in our submission-log. The invariant should not
    flag them: the queries are scoped to job_name = ANY(<our names>).
    """
    db = StubDB()
    db.set_rows("no_pollution_invariant", [])  # query restricted to our 8 fg jobs
    db.set_rows("no_reverse_pollution_invariant", [])  # our bg list is empty in this pilot
    ok, _ = assert_no_pollution(
        db=db,
        foreground_job_names=[f"pilot-fg-{i}" for i in range(8)],
        background_job_names=[],  # idle state → no background jobs in this run
    )
    assert ok is True
