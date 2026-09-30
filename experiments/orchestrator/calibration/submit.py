"""Apply the rendered calibration probes through the Karmada aggregator
and wait for them to finish.

Safety model.
    Identical to experiments/orchestrator/submit.py:
    - every kubectl write call has the shape
      `kubectl --context unique-logical-entrypoint apply -f <Job> ...`,
      which the manifest-scope-check + kubectl-guard hooks already
      authorise for the `kind: Job`-only aggregator;
    - cleanup uses the experiments-cleanup carve-out:
      `kubectl --context unique-logical-entrypoint delete jobs -l
      delphi.experiments/run-id=<run-id> -n delphi-experiments`,
      a single-invocation form that the kubectl-guard accepts.
    - The agent-harness gate `--i-know-this-runs-real-jobs` is enforced
      at the CLI layer (cli.py) and re-checked here.

The submit step does NOT read probe stdout — that's the reduce step's
job. Submit stops as soon as every probe is Complete/Failed/TimedOut
or the deadline elapses.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import render as _render


@dataclass(frozen=True)
class ProbeSubmitResult:
    name: str
    source: str
    target: str
    replica: int
    applied: bool
    final_status: str            # Complete | Failed | TimedOut | apply-failed
    apply_stderr: str


def _kubectl(args: list[str], *, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True, text=True, check=False, timeout=timeout,
    )


def _apply(context: str, path: Path) -> tuple[bool, str]:
    proc = _kubectl([
        "--context", context, "apply", "-f", str(path),
    ])
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, ""


def _job_status(context: str, namespace: str, job_name: str) -> str:
    proc = _kubectl([
        "--context", context, "get", "job", job_name,
        "-n", namespace, "-o", "json",
    ], timeout=15)
    if proc.returncode != 0:
        return "Unknown"
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "Unknown"
    for cond in (obj.get("status") or {}).get("conditions") or []:
        if cond.get("status") != "True":
            continue
        ctype = cond.get("type", "")
        if ctype in ("Complete", "Failed"):
            return ctype
    if ((obj.get("status") or {}).get("active") or 0) >= 1:
        return "Active"
    return "Unknown"


def _wait(context: str, namespace: str, names: list[str], *,
          deadline_seconds: int, poll_seconds: int) -> dict[str, str]:
    pending = set(names)
    final: dict[str, str] = {}
    deadline = time.monotonic() + deadline_seconds
    print(f"[calib] waiting for {len(pending)} probe(s) (deadline {deadline_seconds}s)",
          flush=True)
    while pending and time.monotonic() < deadline:
        for n in list(pending):
            st = _job_status(context, namespace, n)
            if st in ("Complete", "Failed"):
                final[n] = st
                pending.discard(n)
        if pending:
            time.sleep(poll_seconds)
    for n in pending:
        final[n] = "TimedOut"
    return final


def _write_submit_log(run_dir: Path, results: list[ProbeSubmitResult]) -> Path:
    """Persist a small JSON the reduce step can consume."""
    path = run_dir / "submit-log.json"
    payload = [
        {
            "name": r.name,
            "source": r.source,
            "target": r.target,
            "replica": r.replica,
            "applied": r.applied,
            "final_status": r.final_status,
            "apply_stderr": r.apply_stderr,
        }
        for r in results
    ]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def submit_real(
    *,
    run_dir: Path,
    context: str = "unique-logical-entrypoint",
    namespace: str = "delphi-experiments",
    per_probe_deadline_seconds: int = 120,
    poll_seconds: int = 5,
    acknowledged: bool,
) -> int:
    """Apply each probe **sequentially** — apply, wait for
    Complete/Failed, then move on. iperf3-server is single-tenant
    (one client connection at a time, surfaces "the server is busy
    running a test" on parallel connects), so probe Jobs must not
    overlap on the target. The agent's earlier parallel-apply attempt
    produced 8/12 Failed runs for exactly this reason.

    Cost: ~`duration_seconds + Karmada propagation` per probe; for the
    default 12-probe / 5 s configuration the wall-clock total is well
    under five minutes. Returns 0 on full success, non-zero otherwise.
    """
    if not acknowledged:
        raise RuntimeError(
            "submit_real requires acknowledged=True "
            "(--i-know-this-runs-real-jobs on the CLI). Refusing to apply.")
    plan = _render.load_plan(run_dir)
    if plan.namespace != namespace:
        print(f"[calib] WARN: plan namespace={plan.namespace!r} differs from "
              f"--namespace {namespace!r}; using the plan value", file=sys.stderr)
        namespace = plan.namespace

    results: list[ProbeSubmitResult] = []
    for entry in plan.probes:
        path = run_dir / entry["path"]
        if not path.exists():
            results.append(ProbeSubmitResult(
                name=entry["name"], source=entry["source"],
                target=entry["target"], replica=entry["replica"],
                applied=False, final_status="apply-failed",
                apply_stderr=f"missing rendered file: {path}",
            ))
            continue

        ok, err = _apply(context, path)
        if not ok:
            print(f"[calib] APPLY FAILED {entry['name']}: {err[:200]}",
                  file=sys.stderr, flush=True)
            results.append(ProbeSubmitResult(
                name=entry["name"], source=entry["source"],
                target=entry["target"], replica=entry["replica"],
                applied=False, final_status="apply-failed",
                apply_stderr=err,
            ))
            continue

        print(f"[calib] applied {entry['name']} — waiting for completion",
              flush=True)
        statuses = _wait(
            context, namespace, [entry["name"]],
            deadline_seconds=per_probe_deadline_seconds,
            poll_seconds=poll_seconds,
        )
        final_status = statuses.get(entry["name"], "Unknown")
        print(f"[calib]   -> {final_status}", flush=True)
        results.append(ProbeSubmitResult(
            name=entry["name"], source=entry["source"],
            target=entry["target"], replica=entry["replica"],
            applied=True, final_status=final_status, apply_stderr="",
        ))

    log_path = _write_submit_log(run_dir, results)
    n_total = len(results)
    n_done = sum(1 for r in results if r.final_status == "Complete")
    n_failed = sum(1 for r in results if r.final_status == "Failed")
    n_timed = sum(1 for r in results if r.final_status == "TimedOut")
    n_apply = sum(1 for r in results if not r.applied)
    print(f"[calib] submit-log: {log_path}")
    print(f"[calib] applied={n_total - n_apply}/{n_total} "
          f"complete={n_done} failed={n_failed} timed-out={n_timed}")
    return 0 if (n_done == n_total) else 2


def cleanup_run(
    *,
    run_id: str,
    context: str = "unique-logical-entrypoint",
    namespace: str = "delphi-experiments",
    acknowledged: bool,
) -> int:
    """Single-invocation label-selector delete that fits the
    experiments-cleanup carve-out: only the calibration run-id, only
    Jobs, only delphi-experiments. PropagationPolicies are GC'd by the
    controller when the owning Job is deleted.
    """
    if not acknowledged:
        raise RuntimeError(
            "cleanup_run requires acknowledged=True "
            "(--i-know-this-runs-real-jobs on the CLI).")
    selector = f"delphi.experiments/run-id={run_id}"
    proc = _kubectl([
        "--context", context, "delete", "jobs",
        "-l", selector, "-n", namespace, "--ignore-not-found",
    ], timeout=60)
    if proc.returncode != 0:
        print((proc.stderr or proc.stdout).strip(), file=sys.stderr)
        return 2
    print(proc.stdout.strip() or f"[calib] deleted jobs with {selector}")
    return 0
