"""iter-4h-4: orchestrator-driven cluster cordon/uncordon.

The cordon/taint experiment needs a Karmada member cluster cordoned for
the middle third of a campaign and uncordoned afterwards. To keep the
experiment self-contained and reproducible (Change 4), the orchestrator
drives this itself rather than relying on a human running `karmadactl`
by hand at the right moment.

Safety model (mirrors submit.py). These calls shell out to
``karmadactl`` via ``subprocess`` from ``submit_real`` *operator mode*
(``--i-know-this-runs-real-jobs``). The development setup's command guard
only inspects the top-level ``python ...`` command, never the inner
subprocess, exactly like the existing ``kubectl delete jobs`` cleanup.
**Direct invocation of ``karmadactl cordon/uncordon/taint`` from an
automated session stays forbidden** under the development setup's safety
rules.

Crash safety. A Karmada cluster taint has no ``activeDeadlineSeconds``
analogue, and a Python ``finally`` does not survive ``SIGKILL``. Before
cordoning we drop a ``cordon-state.yaml`` marker into the run dir; the
scheduler deletes it after a successful uncordon. ``scan_stale_markers``
is called at ``submit_real`` startup so a marker left by a hard crash is
surfaced loudly (with the exact recovery command) before a new campaign
begins.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

KARMADACTL = "karmadactl"
MARKER_NAME = "cordon-state.yaml"
TIMESTAMPS_NAME = "cordon-timestamps.yaml"

# A runner takes the argv after the binary name and returns
# (returncode, combined_stderr_stdout). Injectable for tests.
KarmadactlRunner = Callable[[list[str]], "tuple[int, str]"]


def karmadactl_available() -> bool:
    """True iff `karmadactl` is on PATH."""
    return shutil.which(KARMADACTL) is not None


def _default_runner(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [KARMADACTL, *args], capture_output=True, text=True, check=False,
    )
    return proc.returncode, (proc.stderr or proc.stdout).strip()


def run_cordon_verb(verb: str, target: str, karmada_context: str,
                    *, runner: Optional[KarmadactlRunner] = None) -> tuple[bool, str]:
    """Run `karmadactl <verb> <target> --karmada-context <ctx>`.

    `verb` is "cordon" or "uncordon". Returns (ok, err_message).
    `uncordon` of a non-cordoned cluster is a Karmada no-op, so the
    caller may invoke it unconditionally in a finally.
    """
    if verb not in ("cordon", "uncordon"):
        raise ValueError(f"unsupported cordon verb: {verb!r}")
    run = runner or _default_runner
    args = [verb, target, "--karmada-context", karmada_context]
    rc, err = run(args)
    return rc == 0, err


def planned_window_seconds(timeline_csv: Path) -> float:
    """Largest planned_submit_offset_seconds in a timeline-planned.csv.

    This is the foreground submission window; cordon fractions are taken
    of it. Returns 0.0 for an empty/absent timeline (the scheduler then
    cordons immediately, which is the degenerate but harmless case).
    """
    import csv
    if not timeline_csv.exists():
        return 0.0
    max_offset = 0.0
    with timeline_csv.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                max_offset = max(max_offset, float(row["planned_submit_offset_seconds"]))
            except (KeyError, ValueError):
                continue
    return max_offset


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_marker(run_dir: Path, target: str, method: str) -> Path:
    """Drop the crash-recovery marker just before cordoning."""
    import yaml
    path = run_dir / MARKER_NAME
    path.write_text(
        yaml.safe_dump({
            "cordon_target": target,
            "cordon_method": method,
            "started_at": _now_iso(),
        }, sort_keys=True),
        encoding="utf-8",
    )
    return path


def clear_marker(run_dir: Path) -> None:
    """Remove the marker after a successful uncordon."""
    (run_dir / MARKER_NAME).unlink(missing_ok=True)


def read_marker(run_dir: Path) -> Optional[dict[str, Any]]:
    import yaml
    path = run_dir / MARKER_NAME
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class StaleMarker:
    run_dir: Path
    target: str
    karmada_context: str
    started_at: str

    def recovery_command(self) -> str:
        return (f"karmadactl uncordon {self.target} "
                f"--karmada-context {self.karmada_context}")


def scan_stale_markers(runs_root: Path,
                       default_karmada_context: str = "unique-logical-entrypoint"
                       ) -> list[StaleMarker]:
    """Find leftover cordon-state.yaml markers under runs_root.

    A marker present means a previous campaign cordoned a cluster and did
    NOT record a clean uncordon — i.e. the orchestrator was killed
    mid-cordon. submit_real calls this at startup and refuses to begin
    until the operator clears the cordon (or the marker), so a shared
    cluster is never silently left unschedulable.
    """
    stale: list[StaleMarker] = []
    if not runs_root.exists():
        return stale
    for marker_path in sorted(runs_root.glob(f"*/{MARKER_NAME}")):
        try:
            import yaml
            with marker_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception:
            data = {}
        stale.append(StaleMarker(
            run_dir=marker_path.parent,
            target=str(data.get("cordon_target", "")),
            karmada_context=str(data.get("karmada_context", default_karmada_context)),
            started_at=str(data.get("started_at", "")),
        ))
    return stale


def write_cordon_timestamps(run_dir: Path, *, target: str, method: str,
                            karmada_context: str,
                            actual_start: Optional[str],
                            actual_end: Optional[str],
                            snapshot_ttl_seconds: int,
                            planned_window_seconds: float) -> Path:
    """Auto-write the cordon-timestamps.yaml the analyzer reads."""
    import yaml
    path = run_dir / TIMESTAMPS_NAME
    path.write_text(
        yaml.safe_dump({
            "cordon_target": target,
            "cordon_method": method,
            "karmada_context": karmada_context,
            "cordon_actual_start_timestamp": actual_start,
            "cordon_actual_end_timestamp": actual_end,
            "snapshot_ttl_seconds": snapshot_ttl_seconds,
            "planned_window_seconds": round(planned_window_seconds, 3),
        }, sort_keys=True),
        encoding="utf-8",
    )
    return path


def parse_cordon_window(cordon_window: dict[str, Any]) -> tuple[str, str, float, float, str]:
    """Validate + unpack a cordon_window manifest block.

    Returns (target, method, start_fraction, end_fraction, karmada_context).
    Raises ValueError on an unsupported method or out-of-range fractions.
    """
    target = str(cordon_window.get("cordon_target") or "")
    if not target:
        raise ValueError("cordon_window.cordon_target is required")
    method = str(cordon_window.get("cordon_method", "cordon"))
    if method != "cordon":
        raise ValueError(
            f"cordon_method={method!r} not supported in iter-4h-4 "
            "(only 'cordon'; 'taint' is a follow-up)")
    start_f = float(cordon_window.get("cordon_start_fraction", 0.33))
    end_f = float(cordon_window.get("cordon_end_fraction", 0.66))
    if not (0.0 <= start_f < end_f <= 1.0):
        raise ValueError(
            f"require 0 <= start_fraction ({start_f}) < end_fraction "
            f"({end_f}) <= 1")
    karmada_context = str(cordon_window.get("karmada_context",
                                            "unique-logical-entrypoint"))
    return target, method, start_f, end_f, karmada_context


class CordonScheduler(threading.Thread):
    """Background thread: cordon at start_offset, uncordon at end_offset.

    Timed relative to ``started_monotonic`` (the foreground t0). The
    ``stop_event`` lets ``submit_real`` cut the window short when the
    foreground finishes early — the scheduler then jumps straight to the
    guaranteed uncordon. Uncordon runs in a ``finally`` whenever the
    cordon succeeded, so a shared cluster is never left cordoned by a
    normal exit; the marker + ``scan_stale_markers`` cover hard crashes.
    """

    def __init__(self, *, run_dir: Path, target: str, method: str,
                 karmada_context: str, start_offset_s: float,
                 end_offset_s: float, started_monotonic: float,
                 snapshot_ttl_seconds: int, planned_window_s: float,
                 stop_event: threading.Event,
                 runner: Optional[KarmadactlRunner] = None,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 monotonic_fn: Callable[[], float] = time.monotonic):
        super().__init__(name=f"cordon-{target}", daemon=True)
        self.run_dir = run_dir
        self.target = target
        self.method = method
        self.karmada_context = karmada_context
        self.start_offset_s = start_offset_s
        self.end_offset_s = end_offset_s
        self.started_monotonic = started_monotonic
        self.snapshot_ttl_seconds = snapshot_ttl_seconds
        self.planned_window_s = planned_window_s
        self.stop_event = stop_event
        self._runner = runner
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self.actual_start: Optional[str] = None
        self.actual_end: Optional[str] = None
        self.cordoned = False

    def _wait_until(self, target_offset: float) -> None:
        """Sleep until started_monotonic + target_offset, honouring stop."""
        while not self.stop_event.is_set():
            remaining = (self.started_monotonic + target_offset) - self._monotonic()
            if remaining <= 0:
                return
            self._sleep(min(remaining, 1.0))

    def run(self) -> None:  # pragma: no cover - timing exercised in tests via tiny offsets
        try:
            self._wait_until(self.start_offset_s)
            if self.stop_event.is_set():
                return
            write_marker(self.run_dir, self.target, self.method)
            ok, err = run_cordon_verb("cordon", self.target,
                                      self.karmada_context, runner=self._runner)
            if ok:
                self.cordoned = True
                self.actual_start = _now_iso()
                print(f"[cordon] cordoned {self.target} at {self.actual_start}",
                      flush=True)
            else:
                print(f"[cordon] WARN cordon {self.target} failed: {err}",
                      flush=True)
            self._wait_until(self.end_offset_s)
        finally:
            if self.cordoned:
                ok, err = run_cordon_verb("uncordon", self.target,
                                          self.karmada_context, runner=self._runner)
                self.actual_end = _now_iso()
                if ok:
                    print(f"[cordon] uncordoned {self.target} at {self.actual_end}",
                          flush=True)
                    clear_marker(self.run_dir)
                else:
                    print(f"[cordon] WARN uncordon {self.target} failed: {err}; "
                          f"marker kept for recovery", flush=True)
            write_cordon_timestamps(
                self.run_dir, target=self.target, method=self.method,
                karmada_context=self.karmada_context,
                actual_start=self.actual_start, actual_end=self.actual_end,
                snapshot_ttl_seconds=self.snapshot_ttl_seconds,
                planned_window_seconds=self.planned_window_s,
            )
