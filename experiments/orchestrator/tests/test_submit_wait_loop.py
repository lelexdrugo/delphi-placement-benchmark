"""Unit tests for ``submit._wait_for_jobs`` state machine.

iter-4a.2 added per-poll progress, transition prints, stall probing,
and an opt-in fail-fast short-circuit. These tests pin the behaviour
of each branch without touching kubectl or the cluster: we monkeypatch
``_job_status`` to drive the state machine through scripted state
sequences and capture the function's return value plus stdout.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout

from experiments.orchestrator import submit as _submit


class _FakeClock:
    """Deterministic monotonic-time substitute.

    Each call to ``time.monotonic()`` returns the next value from the
    pre-baked sequence. Once exhausted, the last value is repeated so
    long-tail asserts never explode. ``sleep`` is a no-op so the
    real-world poll_seconds delay collapses to zero.
    """

    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)
        self._i = 0

    def monotonic(self) -> float:
        if self._i >= len(self._ticks):
            return self._ticks[-1]
        v = self._ticks[self._i]
        self._i += 1
        return v

    def sleep(self, _seconds: float) -> None:
        return None


def _patch_clock(monkeypatch, ticks: list[float]) -> _FakeClock:
    clock = _FakeClock(ticks)
    monkeypatch.setattr(_submit.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_submit.time, "sleep", clock.sleep)
    return clock


def _scripted_status(script: dict[str, list[str]]):
    """Build a ``_job_status`` replacement that pops one status per call.

    ``script`` maps job_name -> list of statuses in poll order. Once a
    job's list is exhausted, the last reported status is repeated. Any
    name not in the script returns ``"Unknown"``.
    """
    counters: dict[str, int] = {name: 0 for name in script}

    def fake_status(_context: str, _namespace: str, job_name: str) -> str:
        seq = script.get(job_name)
        if not seq:
            return "Unknown"
        i = min(counters[job_name], len(seq) - 1)
        counters[job_name] = i + 1
        return seq[i]

    return fake_status


def _silenced_probe(_context, _namespace, _name, _age):
    """Replace _probe_stalled_job in tests so we don't try kubectl."""
    print(f"[submit] STALL (probe called for {_name})", flush=True)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_all_complete_returns_complete(monkeypatch):
    """Happy path: every job reaches Complete on the second poll."""
    _patch_clock(monkeypatch, [0.0, 0.1, 1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(
        _submit, "_job_status",
        _scripted_status({
            "job-a": ["Active", "Complete"],
            "job-b": ["Active", "Complete"],
        }),
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        result = _submit._wait_for_jobs(
            "unique-logical-entrypoint", "delphi-experiments",
            ["job-a", "job-b"],
            deadline_seconds=60, poll_seconds=1,
            stall_threshold_seconds=120, fail_fast=False,
        )

    out = buf.getvalue()
    assert result == {"job-a": "Complete", "job-b": "Complete"}
    assert "[OK] job-a Complete" in out
    assert "[OK] job-b Complete" in out
    # Per-poll progress summary must appear.
    assert "running" in out and "complete" in out


def test_mixed_terminal_reports_each_transition(monkeypatch):
    """Complete and Failed both surface as state-transition lines."""
    _patch_clock(monkeypatch, [0.0, 0.1, 1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(
        _submit, "_job_status",
        _scripted_status({
            "job-ok":    ["Active", "Complete"],
            "job-bad":   ["Active", "Failed"],
        }),
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        result = _submit._wait_for_jobs(
            "unique-logical-entrypoint", "delphi-experiments",
            ["job-ok", "job-bad"],
            deadline_seconds=60, poll_seconds=1,
            stall_threshold_seconds=120, fail_fast=False,
        )

    out = buf.getvalue()
    assert result == {"job-ok": "Complete", "job-bad": "Failed"}
    assert "[OK] job-ok Complete" in out
    assert "[FAIL] job-bad Failed" in out


def test_stall_probe_fires_once_per_stalled_job(monkeypatch):
    """A Job stuck in Active beyond stall_threshold triggers _probe_stalled_job exactly once."""
    # Plenty of ticks so the probe has room to fire across multiple polls.
    _patch_clock(monkeypatch, [
        0.0,  # started
        0.1,  # first inner loop
        1.0,  # first poll done
        2.0,
        3.0,
        130.0,  # stall threshold (120s) exceeded
        131.0,
        132.0,
        200.0,
        201.0,
        202.0,
    ])
    monkeypatch.setattr(
        _submit, "_job_status",
        _scripted_status({"job-stuck": ["Active"] * 20}),
    )

    probe_calls: list[tuple[str, float]] = []

    def fake_probe(_ctx, _ns, name, age):
        probe_calls.append((name, age))
        print(f"[submit] STALL (probe called for {name})", flush=True)

    monkeypatch.setattr(_submit, "_probe_stalled_job", fake_probe)

    buf = io.StringIO()
    with redirect_stdout(buf):
        result = _submit._wait_for_jobs(
            "unique-logical-entrypoint", "delphi-experiments",
            ["job-stuck"],
            deadline_seconds=180, poll_seconds=1,
            stall_threshold_seconds=120, fail_fast=False,
        )

    # The job never reached terminal — orchestrator times out at deadline.
    assert result == {"job-stuck": "TimedOut"}
    # Probe was called for the stalled job, exactly once.
    assert len(probe_calls) == 1
    assert probe_calls[0][0] == "job-stuck"
    assert probe_calls[0][1] >= 120  # age at probe time


def test_stall_probe_disabled_when_threshold_zero(monkeypatch):
    """stall_threshold_seconds=0 disables the probe entirely."""
    _patch_clock(monkeypatch, [0.0, 0.1, 1.0, 200.0, 201.0, 202.0, 203.0])
    monkeypatch.setattr(
        _submit, "_job_status",
        _scripted_status({"job-stuck": ["Active"] * 5}),
    )

    probe_calls: list = []

    def fake_probe(_ctx, _ns, name, age):
        probe_calls.append(name)

    monkeypatch.setattr(_submit, "_probe_stalled_job", fake_probe)

    with redirect_stdout(io.StringIO()):
        _submit._wait_for_jobs(
            "unique-logical-entrypoint", "delphi-experiments",
            ["job-stuck"],
            deadline_seconds=180, poll_seconds=1,
            stall_threshold_seconds=0, fail_fast=False,
        )

    assert probe_calls == []


def test_fail_fast_breaks_on_first_failure(monkeypatch):
    """fail_fast=True stops the wait loop after the first Failed and survivors are TimedOut."""
    _patch_clock(monkeypatch, [0.0, 0.1, 1.0, 2.0, 3.0, 4.0, 5.0])
    monkeypatch.setattr(
        _submit, "_job_status",
        _scripted_status({
            "job-fast-fail": ["Active", "Failed"],
            "job-still-running": ["Active", "Active", "Active"],
        }),
    )
    monkeypatch.setattr(_submit, "_probe_stalled_job", _silenced_probe)

    buf = io.StringIO()
    with redirect_stdout(buf):
        result = _submit._wait_for_jobs(
            "unique-logical-entrypoint", "delphi-experiments",
            ["job-fast-fail", "job-still-running"],
            deadline_seconds=60, poll_seconds=1,
            stall_threshold_seconds=120, fail_fast=True,
        )

    out = buf.getvalue()
    # Failed job goes to Failed; survivor marked TimedOut by the fail-fast exit.
    assert result["job-fast-fail"] == "Failed"
    assert result["job-still-running"] == "TimedOut"
    assert "fail-fast: aborting wait" in out


def test_progress_summary_includes_oldest_active(monkeypatch):
    """The per-poll line names the oldest still-active job (operator-targetable signal)."""
    _patch_clock(monkeypatch, [0.0, 0.1, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    monkeypatch.setattr(
        _submit, "_job_status",
        _scripted_status({
            "job-quick": ["Active", "Complete"],
            "job-slow":  ["Active", "Active", "Active", "Complete"],
        }),
    )
    monkeypatch.setattr(_submit, "_probe_stalled_job", _silenced_probe)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _submit._wait_for_jobs(
            "unique-logical-entrypoint", "delphi-experiments",
            ["job-quick", "job-slow"],
            deadline_seconds=60, poll_seconds=1,
            stall_threshold_seconds=120, fail_fast=False,
        )

    out = buf.getvalue()
    # After job-quick completes, the only remaining active is job-slow,
    # and the summary should call it out by name.
    assert "oldest active: job-slow" in out
