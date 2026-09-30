"""Cross-cutting invariants asserted on every analysis pass."""
from __future__ import annotations

from typing import Any


class PollutionDetected(Exception):
    """The no-pollution invariant was violated.

    Either a foreground submission was persisted with kind != 'foreground'
    (forward leak — agent loses history for the job), or a background
    submission was persisted with kind == 'foreground' (reverse leak —
    agent sees a background row as if it were foreground evidence).
    """

    def __init__(self,
                 forward_leaked: list[dict[str, Any]],
                 reverse_leaked: list[dict[str, Any]]) -> None:
        n_fwd = len(forward_leaked)
        n_rev = len(reverse_leaked)
        super().__init__(
            f"no-pollution invariant violated: "
            f"{n_fwd} foreground row(s) with kind != 'foreground', "
            f"{n_rev} background row(s) with kind == 'foreground'. "
            "See no-pollution.csv for details."
        )
        self.forward_leaked = forward_leaked
        self.reverse_leaked = reverse_leaked


def assert_no_pollution(
    *,
    db: Any,
    foreground_job_names: list[str],
    background_job_names: list[str],
) -> tuple[bool, list[dict[str, Any]]]:
    """Run the bi-directional no-pollution check against THIS run's jobs.

    Returns (ok, all_leaked_rows). Raises PollutionDetected if either
    direction has leaks. ``all_leaked_rows`` is the concatenation of
    forward + reverse leaks with a synthetic ``leak_direction`` field
    ("forward" | "reverse") so the writer can dump them into one CSV.

    Background-load Jobs and calibration probes (iter-4b) legitimately
    carry kind='background'; they only count as leaks when they live in
    the *foreground* submission-log set. Calibration runs that never
    enter our submission-log won't be flagged.

    Empty lists short-circuit to (True, []) so a pilot run with no
    submitted jobs (rendered but not yet submitted) doesn't trip the
    invariant.
    """
    forward: list[dict[str, Any]] = []
    reverse: list[dict[str, Any]] = []

    if foreground_job_names:
        forward = list(db.run_query("no_pollution_invariant", foreground_job_names))
        for row in forward:
            row["leak_direction"] = "forward"

    if background_job_names:
        reverse = list(db.run_query("no_reverse_pollution_invariant", background_job_names))
        for row in reverse:
            row["leak_direction"] = "reverse"

    if forward or reverse:
        raise PollutionDetected(forward_leaked=forward, reverse_leaked=reverse)

    return True, []


def assert_join_coverage(
    *,
    foreground_count: int,
    db_request_count: int,
) -> bool:
    """Heuristic check: every applied foreground submission should
    resolve to one DB request row.

    Returns False when the counts mismatch (and the runner records this
    in summary.json + notes). Does NOT raise — a join gap is often
    informational (e.g. a Job that never reached
    /api/v1/decision/async because of a webhook denial).
    """
    return db_request_count >= foreground_count
