"""Warm-up and stabilisation helpers for background-load specs.

Iter-2b introduces the cluster-state axis. After submitting the
background-load Jobs that materialise a non-idle cluster state, the
orchestrator must wait long enough for the load to ramp up to a
reproducible steady state before submitting foreground Jobs. This
module provides:

- `wait_warmup(seconds)`: trivial sleep with progress logging.
- `stabilisation_check(...)`: polls `kubectl top node` until measured
  node CPU is within ±10% of the declared target for ≥30 seconds,
  then returns. Returns 'stabilised', 'timeout', or 'skipped'.

Both are operator-time helpers; iter-2b ships them but they are wired
into the actual submission pipeline only in iter-3 (where the `submit`
subcommand is implemented).
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import Literal


def wait_warmup(seconds: int) -> None:
    """Block for `seconds` seconds, logging progress every 15s."""
    if seconds <= 0:
        return
    start = time.monotonic()
    deadline = start + seconds
    next_tick = start + 15
    print(f"[warmup] sleeping {seconds}s", flush=True)
    while time.monotonic() < deadline:
        remaining = max(0, int(deadline - time.monotonic()))
        if time.monotonic() >= next_tick:
            print(f"[warmup] {remaining}s remaining", flush=True)
            next_tick = time.monotonic() + 15
        time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))
    print("[warmup] done", flush=True)


def _kubectl_top_node_cpu_percent(context: str) -> float | None:
    """Return the highest CPU% across nodes in the given context, or
    None if metrics-server is unavailable."""
    try:
        proc = subprocess.run(
            ["kubectl", "--context", context, "top", "node", "--no-headers"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # Each line: NAME    CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%
    best = 0.0
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        cpu_pct = parts[2].rstrip("%")
        try:
            v = float(cpu_pct)
        except ValueError:
            continue
        best = max(best, v)
    return best


def stabilisation_check(
    context: str,
    target_load_percent: int,
    *,
    timeout_seconds: int = 300,
    poll_seconds: int = 5,
    band_percent: float = 10.0,
    confirm_seconds: int = 30,
) -> Literal["stabilised", "timeout", "skipped"]:
    """Poll `kubectl top node` until the measured CPU is within
    target±band for at least `confirm_seconds`. Returns:

    - 'stabilised' if the band was held long enough,
    - 'timeout'    if `timeout_seconds` elapsed without confirmation,
    - 'skipped'    if metrics-server is not available (kubectl top failed).
    """
    if target_load_percent <= 0:
        return "skipped"

    low = max(0.0, target_load_percent - band_percent)
    high = target_load_percent + band_percent

    # First probe: if metrics-server is unavailable we cannot poll;
    # report skipped immediately and let the caller decide.
    first = _kubectl_top_node_cpu_percent(context)
    if first is None:
        print(f"[stabilisation] {context}: kubectl top node unavailable, skipped", file=sys.stderr)
        return "skipped"

    print(
        f"[stabilisation] {context}: target {target_load_percent}% ±{band_percent}% "
        f"(must hold {confirm_seconds}s)",
        flush=True,
    )

    deadline = time.monotonic() + timeout_seconds
    in_band_since: float | None = None
    while time.monotonic() < deadline:
        cpu = _kubectl_top_node_cpu_percent(context)
        if cpu is None:
            return "skipped"
        in_band = low <= cpu <= high
        now = time.monotonic()
        if in_band:
            if in_band_since is None:
                in_band_since = now
            elif now - in_band_since >= confirm_seconds:
                print(f"[stabilisation] {context}: stabilised at {cpu:.1f}%", flush=True)
                return "stabilised"
        else:
            in_band_since = None
        time.sleep(poll_seconds)

    print(f"[stabilisation] {context}: timed out after {timeout_seconds}s", file=sys.stderr)
    return "timeout"
