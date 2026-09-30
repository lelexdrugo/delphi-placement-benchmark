"""kubectl-driven submission of rendered Job manifests.

Two entry points:

- ``dry_run`` performs server-side validation per file.

- ``submit_real`` actually applies the manifests through
  ``unique-logical-entrypoint``. iter-3a wires this so the operator can
  run real Phase-A bootstrap and Phase-B effectiveness runs.

Safety model. The development setup's kubectl guard inspects
only the commands an automated session types into a shell
— it does NOT intercept ``subprocess.run(["kubectl", ...])``
calls made by this Python module, because those are direct OS spawns. The
real safety gate is therefore ``acknowledged=True``, wired to the CLI
``--i-know-this-runs-real-jobs`` flag, and the discipline of keeping every
kubectl call inside one of two already-authorised shapes:

- ``apply -f <Job> --context unique-logical-entrypoint`` (only ``kind:
  Job``);
- ``delete job <name> --context unique-logical-entrypoint -n
  delphi-experiments`` (the experiments-cleanup carve-out documented in
  the development setup's safety rules).

Widening the kubectl surface here would silently bypass that safety
contract — any future change to this module must keep the same two
shapes. Nothing ever targets a member cluster
(``public-cloud``/``edge-1``/``edge-2``) or ``karmada-system``.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import cordon as _cordon
from . import warmup as _warmup

# Recorded into cordon-timestamps.yaml so the analyzer knows the
# decision-maker snapshot TTL that bounds the cordon reaction lag. Keep
# in sync with clusterSnapshotTTL in
# code/delphi-system/decision-maker/internal/service/cluster_status_service.go.
_CLUSTER_SNAPSHOT_TTL_SECONDS = 15


# ---------------------------------------------------------------------------
# Dry-run (iter-1; unchanged)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DryRunResult:
    job_path: Path
    ok: bool
    stdout: str
    stderr: str
    exit_code: int


def dry_run(job_paths: list[Path],
            context: str = "unique-logical-entrypoint") -> list[DryRunResult]:
    """Run `kubectl --context <ctx> apply --dry-run=server -f <job>` per file.

    Reports each outcome independently — one failure does not stop the
    others. Suitable for an offline render-then-validate pass.
    """
    results: list[DryRunResult] = []
    for path in job_paths:
        cmd = [
            "kubectl", "--context", context,
            "apply", "--dry-run=server", "-f", str(path),
        ]
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
            )
            results.append(DryRunResult(
                job_path=path,
                ok=(completed.returncode == 0),
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.returncode,
            ))
        except FileNotFoundError as exc:
            results.append(DryRunResult(
                job_path=path,
                ok=False,
                stdout="",
                stderr=f"kubectl not found on PATH: {exc}",
                exit_code=127,
            ))
    return results


# ---------------------------------------------------------------------------
# Real submission (iter-3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubmitResult:
    """One slot in the per-run submission log."""
    job_path: Path
    job_name: str
    role: str               # "foreground" | "background"
    state_id: str           # bootstrap state, or "" for simple runs
    applied: bool
    completed: bool         # True only if observed Complete / Succeeded
    failed: bool            # True if observed Failed
    skipped_reason: str     # non-empty when applied=False and reason known
    apply_stderr: str       # tail of stderr if apply failed
    wall_clock_seconds: float
    # iter-4a: UTC timestamp at which `kubectl apply` returned OK. The
    # analyzer's completion_time module uses this as the canonical
    # submission timestamp (combined with k8s job .status.completionTime
    # for the runtime delta). Empty string when applied=False.
    applied_at_utc: str = ""


def _kubectl(args: list[str],
             *, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Thin wrapper. Always includes a context — the safety hooks deny
    invocations without one.
    """
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True, text=True, check=False,
        timeout=timeout,
    )


def _apply_job(context: str, path: Path) -> tuple[bool, str]:
    proc = _kubectl([
        "--context", context, "apply", "-f", str(path),
    ])
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, ""


def _job_status(context: str, namespace: str, job_name: str) -> str:
    """Return one of: 'Complete', 'Failed', 'Active', 'Unknown'.

    Reads .status.conditions[*].type first. Pre-Complete jobs report
    'Active' if .status.active >= 1, else 'Unknown'. Errors return
    'Unknown' so the poll loop keeps trying.

    The kubectl call uses a 30s timeout and retries once on
    TimeoutExpired before giving up. A brief aggregator hiccup mid-poll
    used to surface as an unhandled TimeoutExpired that killed the
    entire orchestrator run (observed in iter-4a.1: 8/8 jobs applied,
    then one mid-poll timeout aborted the whole submit). Returning
    "Unknown" instead lets the next poll cycle continue.

    iter-4-refinements (B): completion is detected from the
    ``conditions[]`` array OR from the ``.status`` pod counters
    (``succeeded`` / ``failed`` / ``completionTime``) as a fallback.
    Karmada's *aggregated* Job status on ``unique-logical-entrypoint``
    can mirror ``.status.succeeded`` / ``.status.completionTime`` while
    the ``conditions[]`` array is still empty or stale. This is the
    **most likely** cause of the iter-4h-3 r3/hs hang, where the DB
    already showed 40 COMPLETED yet the poll never advanced (the failing
    aggregated JSON was not captured — the run was recovered via the DB —
    so a conditions-only detection gap is the reconstruction, not a
    direct observation). The counter fallback closes that gap without
    giving submit any DB dependency, and is a strict improvement
    regardless of the exact r3/hs trigger.
    Specs use ``completions: 1`` / ``backoffLimit: 0``, so a positive
    ``succeeded`` (or a present ``completionTime``) means Complete and a
    positive ``failed`` means Failed.
    """
    args = ["--context", context, "get", "job", job_name,
            "-n", namespace, "-o", "json"]
    proc = None
    for attempt in (1, 2):
        try:
            proc = _kubectl(args, timeout=30)
            break
        except subprocess.TimeoutExpired:
            if attempt == 2:
                return "Unknown"
    if proc is None or proc.returncode != 0:
        return "Unknown"
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return "Unknown"
    status = obj.get("status") or {}
    for cond in status.get("conditions") or []:
        if cond.get("status") != "True":
            continue
        ctype = cond.get("type", "")
        if ctype in ("Complete", "Failed"):
            return ctype
    # Counter/timestamp fallback for a partially-mirrored aggregated status.
    if (status.get("succeeded") or 0) >= 1 or status.get("completionTime"):
        return "Complete"
    if (status.get("failed") or 0) >= 1:
        return "Failed"
    if (status.get("active") or 0) >= 1:
        return "Active"
    return "Unknown"


def _wait_for_jobs(context: str, namespace: str, job_names: list[str],
                   *, deadline_seconds: int, poll_seconds: int = 10,
                   stall_threshold_seconds: int = 120,
                   fail_fast: bool = False) -> dict[str, str]:
    """Poll the named Jobs until each reports Complete/Failed, or the
    deadline expires. Returns {job_name: final_status}.

    Uses ``kubectl get`` instead of ``kubectl wait`` because:
      1. multiple jobs in one call need a loop anyway;
      2. ``kubectl wait --for=delete`` is on the destructive blocklist —
         keeping every wait call away from that family is conservative.

    iter-4a.2 added three observability/early-exit features without
    changing the legacy return contract:

    - per-poll one-line progress summary (``t+Ns: K running / C
      complete / F failed (of N); oldest active: <name> Xs``);
    - state-transition prints when a Job first reaches Complete or
      Failed (``[OK]`` / ``[FAIL]`` with elapsed seconds);
    - stall detection: when a Job has been Active for
      ``stall_threshold_seconds`` without a terminal transition,
      ``_probe_stalled_job`` fires once and surfaces aggregator-side
      events + the Job's failed-pod counter so the operator can spot
      ImagePullBackOff / ErrImagePull / CrashLoopBackOff without
      waiting for the deadline. The probe is best-effort and never
      raises.

    Set ``fail_fast=True`` to break the wait loop as soon as the first
    Job reaches Failed instead of waiting for the rest to drain. This
    mirrors what an operator would do manually after the stall probe
    surfaces an image-pull failure: cut losses, rebuild, retry.
    """
    pending = set(job_names)
    results: dict[str, str] = {}
    first_active_seen: dict[str, float] = {}
    probed_stalled: set[str] = set()
    started = time.monotonic()
    deadline = started + deadline_seconds
    n_total = len(job_names)
    print(f"[submit] waiting for {n_total} job(s) to finish "
          f"(deadline {deadline_seconds}s, poll {poll_seconds}s, "
          f"stall_threshold {stall_threshold_seconds}s"
          f"{', fail-fast' if fail_fast else ''})", flush=True)

    while pending and time.monotonic() < deadline:
        new_terminal: list[tuple[str, str, float]] = []
        for name in list(pending):
            status = _job_status(context, namespace, name)
            if status == "Active":
                first_active_seen.setdefault(name, time.monotonic())
            if status in ("Complete", "Failed"):
                seen_at = first_active_seen.get(name, started)
                elapsed = time.monotonic() - seen_at
                results[name] = status
                new_terminal.append((name, status, elapsed))
                pending.discard(name)

        # Surface every fresh terminal transition immediately.
        for name, status, elapsed in new_terminal:
            marker = "[OK]" if status == "Complete" else "[FAIL]"
            print(f"[submit] {marker} {name} {status} (after {elapsed:.0f}s)",
                  flush=True)

        # Per-poll progress summary so the wait isn't a silent black box.
        n_complete = sum(1 for s in results.values() if s == "Complete")
        n_failed = sum(1 for s in results.values() if s == "Failed")
        t_elapsed = int(time.monotonic() - started)
        oldest_active_msg = ""
        if pending and first_active_seen:
            candidates = [n for n in pending if n in first_active_seen]
            if candidates:
                oldest_name = min(candidates,
                                  key=lambda n: first_active_seen[n])
                age = int(time.monotonic() - first_active_seen[oldest_name])
                oldest_active_msg = f"; oldest active: {oldest_name} {age}s"
        print(f"[submit] t+{t_elapsed}s: {len(pending)} running / "
              f"{n_complete} complete / {n_failed} failed (of {n_total})"
              f"{oldest_active_msg}",
              flush=True)

        # Stall probe — once per stalled job, never raises.
        if stall_threshold_seconds > 0:
            now = time.monotonic()
            for name in list(pending):
                seen_at = first_active_seen.get(name)
                if seen_at is None:
                    continue
                if name in probed_stalled:
                    continue
                age = now - seen_at
                if age >= stall_threshold_seconds:
                    _probe_stalled_job(context, namespace, name, age)
                    probed_stalled.add(name)

        # Fail-fast short-circuit. Survivors get marked TimedOut below.
        if fail_fast and n_failed > 0:
            print(f"[submit] fail-fast: aborting wait with {n_failed} "
                  f"failure(s) and {len(pending)} job(s) still running",
                  flush=True)
            break

        if pending and time.monotonic() < deadline:
            time.sleep(poll_seconds)

    for name in pending:
        results[name] = "TimedOut"
    return results


def _probe_stalled_job(context: str, namespace: str, name: str,
                       age_seconds: float) -> None:
    """Best-effort diagnostic snapshot for a Job stuck in Active.

    Pod-level events (ImagePullBackOff, ErrImagePull, CrashLoopBackOff)
    live on the propagated member cluster, not on
    ``unique-logical-entrypoint``. We probe what is cheaply reachable
    from the aggregator:

    - ``kubectl get events`` filtered to the Job — catches Job-level
      events including ``BackoffLimitExceeded`` that fire long before
      the Job rolls up to ``Failed``;
    - the Job's ``.status.failed`` and ``.status.active`` pod counters
      — a non-zero ``failed`` count while still Active is the canonical
      "pods are crashing / cannot pull" signal.

    Output is printed to stdout with ``[submit] STALL`` markers; the
    function never raises. Anything that fails just prints a short
    note and moves on.
    """
    try:
        print(f"[submit] STALL {name} has been active for "
              f"{age_seconds:.0f}s (>= stall threshold); probing",
              flush=True)
        # Aggregator events filtered to this Job.
        try:
            proc = _kubectl([
                "--context", context, "get", "events", "-n", namespace,
                "--field-selector",
                f"involvedObject.kind=Job,involvedObject.name={name}",
                "--sort-by", ".lastTimestamp",
                "-o",
                "custom-columns=LAST:.lastTimestamp,TYPE:.type,"
                "REASON:.reason,MSG:.message",
            ], timeout=15)
        except subprocess.TimeoutExpired:
            proc = None
        if proc is not None and proc.returncode == 0:
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            if lines:
                print(f"[submit] STALL   aggregator events for {name}:",
                      flush=True)
                for line in lines[-5:]:
                    print(f"[submit] STALL     {line}", flush=True)

        # Job pod counters.
        try:
            proc = _kubectl([
                "--context", context, "get", "job", name, "-n", namespace,
                "-o",
                "jsonpath={.status.active}|{.status.failed}|"
                "{.status.succeeded}",
            ], timeout=15)
        except subprocess.TimeoutExpired:
            proc = None
        if proc is not None and proc.returncode == 0:
            parts = (proc.stdout or "").split("|")
            active_n = parts[0].strip() or "0"
            failed_n = parts[1].strip() if len(parts) > 1 else "0"
            succ_n = parts[2].strip() if len(parts) > 2 else "0"
            print(f"[submit] STALL   {name} pods: active={active_n}, "
                  f"failed={failed_n}, succeeded={succ_n}",
                  flush=True)
            if failed_n.isdigit() and int(failed_n) > 0:
                print(f"[submit] STALL   {name} has failed pods while "
                      f"Job is still Active — likely image-pull or "
                      f"crash-loop on the member cluster.",
                      flush=True)
        print(f"[submit] STALL   tip: inspect events on the propagated "
              f"member cluster, e.g. "
              f"`kubectl --context <member> get events -n {namespace} "
              f"--field-selector involvedObject.kind=Pod` "
              f"(member listed in pp-{name}.spec.placement.clusterAffinity)",
              flush=True)
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[submit] STALL   probe error for {name}: {exc!r}",
              file=sys.stderr, flush=True)


def _delete_jobs(context: str, namespace: str, job_names: list[str]) -> list[str]:
    """Delete each named Job. Returns names that failed to delete.

    Each name is its own kubectl invocation: the kubectl guard authorises
    `kubectl --context unique-logical-entrypoint delete jobs <name|--all|-l>
    -n delphi-experiments` (single kubectl call only). Looping over names
    keeps every call to that exact shape.
    """
    failures: list[str] = []
    for name in job_names:
        proc = _kubectl([
            "--context", context, "delete", "job", name,
            "-n", namespace, "--ignore-not-found",
        ], timeout=30)
        if proc.returncode != 0:
            failures.append(name)
            print(f"[submit] WARN: failed to delete background Job {name}: "
                  f"{(proc.stderr or proc.stdout).strip()}", file=sys.stderr)
    return failures


def _read_planned_timeline(csv_path: Path) -> list[tuple[int, str, float]]:
    """Read a timeline-planned.csv and return [(job_index, job_name, offset_s), ...]
    sorted by offset.
    """
    entries: list[tuple[int, str, float]] = []
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            entries.append((
                int(row["job_index"]),
                row["job_name"],
                float(row["planned_submit_offset_seconds"]),
            ))
    entries.sort(key=lambda e: e[2])
    return entries


def _foreground_path(jobs_dir: Path, idx: int, job_name: str) -> Path:
    # render.py emits files as f"{idx:03d}-{name}.yaml".
    return jobs_dir / f"{idx:03d}-{job_name}.yaml"


def _submit_foreground_with_timeline(
    *,
    context: str,
    namespace: str,
    jobs_dir: Path,
    timeline_csv: Path,
    fg_deadline_seconds: int,
    poll_seconds: int,
    state_id: str = "",
    stall_threshold_seconds: int = 120,
    fail_fast: bool = False,
) -> tuple[list[SubmitResult], list[str]]:
    """Apply foreground Jobs paced by their planned offsets, then wait.

    Returns (per-job results, job_name list) so the caller can record
    them and then choose whether to delete background-load Jobs.
    """
    entries = _read_planned_timeline(timeline_csv)
    results: list[SubmitResult] = []
    submitted_names: list[str] = []

    t0 = time.monotonic()
    for idx, name, offset in entries:
        path = _foreground_path(jobs_dir, idx, name)
        if not path.exists():
            results.append(SubmitResult(
                job_path=path, job_name=name, role="foreground",
                state_id=state_id, applied=False, completed=False, failed=False,
                skipped_reason=f"rendered file missing: {path}",
                apply_stderr="", wall_clock_seconds=0.0,
            ))
            continue

        # Pace to the planned offset relative to t0.
        target = t0 + offset
        wait = target - time.monotonic()
        if wait > 0:
            time.sleep(wait)

        apply_start = time.monotonic()
        ok, err = _apply_job(context, path)
        apply_dt = time.monotonic() - apply_start
        # iter-4a: capture apply-time UTC the moment kubectl returns OK
        # (only when ok=True; failed applies have no submission timestamp).
        applied_ts = datetime.now(timezone.utc).isoformat() if ok else ""
        if ok:
            submitted_names.append(name)
            print(f"[submit] fg t+{offset:7.1f}s applied {name}", flush=True)
        else:
            print(f"[submit] fg t+{offset:7.1f}s APPLY FAILED {name}: {err[:200]}",
                  file=sys.stderr, flush=True)
        results.append(SubmitResult(
            job_path=path, job_name=name, role="foreground",
            state_id=state_id, applied=ok, completed=False, failed=False,
            skipped_reason="" if ok else "apply-failed",
            apply_stderr="" if ok else err,
            wall_clock_seconds=apply_dt,
            applied_at_utc=applied_ts,
        ))

    statuses = _wait_for_jobs(
        context=context, namespace=namespace,
        job_names=submitted_names,
        deadline_seconds=fg_deadline_seconds,
        poll_seconds=poll_seconds,
        stall_threshold_seconds=stall_threshold_seconds,
        fail_fast=fail_fast,
    )

    # Annotate the foreground results with their final status. We rebuild
    # the list in-place since SubmitResult is frozen.
    final: list[SubmitResult] = []
    for r in results:
        if r.role == "foreground" and r.applied:
            st = statuses.get(r.job_name, "Unknown")
            final.append(SubmitResult(
                job_path=r.job_path, job_name=r.job_name, role=r.role,
                state_id=r.state_id, applied=True,
                completed=(st == "Complete"),
                failed=(st == "Failed"),
                skipped_reason="" if st in ("Complete", "Failed") else f"final-status={st}",
                apply_stderr=r.apply_stderr,
                wall_clock_seconds=r.wall_clock_seconds,
                applied_at_utc=r.applied_at_utc,
            ))
        else:
            final.append(r)
    return final, submitted_names


def _submit_background(
    *,
    context: str,
    namespace: str,  # noqa: ARG001 — kept for parity with foreground signature
    background_dir: Path,
    state_id: str,
) -> tuple[list[SubmitResult], list[str]]:
    """Apply every rendered background-load Job. Returns (per-job results,
    list of applied job names) — the caller is responsible for deleting
    these at end-of-state.
    """
    results: list[SubmitResult] = []
    applied_names: list[str] = []
    if not background_dir.exists():
        return results, applied_names

    for path in sorted(background_dir.glob("*.yaml")):
        # Job name is everything after the leading "NNN-" prefix.
        stem = path.stem
        name = stem.split("-", 1)[1] if "-" in stem else stem

        apply_start = time.monotonic()
        ok, err = _apply_job(context, path)
        apply_dt = time.monotonic() - apply_start
        applied_ts = datetime.now(timezone.utc).isoformat() if ok else ""
        if ok:
            applied_names.append(name)
            print(f"[submit] bg applied {name}", flush=True)
        else:
            print(f"[submit] bg APPLY FAILED {name}: {err[:200]}",
                  file=sys.stderr, flush=True)
        results.append(SubmitResult(
            job_path=path, job_name=name, role="background",
            state_id=state_id, applied=ok, completed=False, failed=False,
            skipped_reason="" if ok else "apply-failed",
            apply_stderr="" if ok else err,
            wall_clock_seconds=apply_dt,
            applied_at_utc=applied_ts,
        ))
    return results, applied_names


def _write_submission_log(run_dir: Path, results: list[SubmitResult]) -> Path:
    """Persist a CSV that the analyser can join against timeline-planned.csv.

    iter-4a adds the `applied_at_utc` column so the analyzer's
    completion_time metric has a canonical submission timestamp.
    """
    path = run_dir / "submission-log.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "job_name", "role", "state_id",
            "applied", "completed", "failed",
            "skipped_reason", "wall_clock_seconds",
            "applied_at_utc", "job_path",
        ])
        for r in results:
            w.writerow([
                r.job_name, r.role, r.state_id,
                int(r.applied), int(r.completed), int(r.failed),
                r.skipped_reason, f"{r.wall_clock_seconds:.3f}",
                r.applied_at_utc, str(r.job_path),
            ])
    return path


def _load_state_spec(state_path: Path) -> dict[str, Any]:
    import yaml  # local import — keep top-level fast for `--help`
    with state_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_manifest_cordon_window(run_dir: Path) -> Optional[dict[str, Any]]:
    """Return manifest.json's `cordon_window` block, or None (iter-4h-4)."""
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cw = manifest.get("cordon_window")
    return cw if isinstance(cw, dict) else None


# ---------------------------------------------------------------------------
# iter-4h-2: disturbance-stream submission + population sampler
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DisturbanceSubmitResult:
    """One slot in the per-state disturbance-submission log.

    Distinct from SubmitResult because disturbance Jobs have no
    foreground deadline semantics and their `completed`/`failed` are
    captured at end-of-state from the population sampler, not from a
    per-Job wait. The submit-thread only records *whether the apply
    happened* (and why it was skipped if it didn't).
    """
    job_path: Path
    job_name: str
    target_cluster: str
    workload_class: str
    planned_offset_s: float
    applied: bool
    skipped_reason: str        # circuit_breaker | apply-failed | shutdown
    apply_stderr: str
    applied_at_utc: str


def _read_disturbance_timeline(csv_path: Path) -> list[dict[str, Any]]:
    """Read the renderer's disturbance-timeline-planned.csv as a list of
    dicts sorted by offset (the file is already sorted but a defensive
    re-sort costs ~nothing and protects against hand-edited CSVs)."""
    rows: list[dict[str, Any]] = []
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append({
                "job_index": int(r["job_index"]),
                "job_name": r["job_name"],
                "planned_submit_offset_seconds": float(
                    r["planned_submit_offset_seconds"]
                ),
                "target_cluster": r["target_cluster"],
                "workload_class": r["workload_class"],
                "intensity_label": r.get("intensity_label", ""),
                "disturbance_cell": r.get("disturbance_cell", ""),
            })
    rows.sort(key=lambda r: r["planned_submit_offset_seconds"])
    return rows


def _kubectl_get_disturbance_jobs(context: str, namespace: str,
                                  disturbance_cell: str) -> Optional[list[dict[str, Any]]]:
    """Return the Karmada-aggregated Job objects for the current
    disturbance cell, filtered by the single-key label.

    Returns None on a kubectl error so the caller can decide whether
    to retry or skip the poll cycle. Empty list is a valid result
    (no Jobs created yet — common in the first ~spawn-interval seconds
    after t=0).
    """
    args = [
        "--context", context, "get", "jobs", "-n", namespace,
        "-l", f"delphi.experiments/disturbance-cell={disturbance_cell}",
        "-o", "json",
    ]
    try:
        proc = _kubectl(args, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        return None


def _per_target_population(jobs: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Aggregate Job.status into per-target counters.

    Returns {target_cluster: {active_or_pending, completed, failed,
    submitted}}.

    `.status.active` on the Karmada-aggregated Job is the count of
    Pods that exist on the member cluster — Running AND Pending
    combined. Karmada does NOT propagate per-Pod status back to the
    aggregator on this lab (verified empirically 2026-05-26 with
    `kubectl --context unique-logical-entrypoint get pods -n
    delphi-experiments` returning empty .items), so the Running-vs-
    Pending split is unobservable and we record the union.
    """
    counters: dict[str, dict[str, int]] = {}
    for j in jobs:
        labels = ((j.get("metadata") or {}).get("labels") or {})
        target = labels.get("delphi.experiments/pin-to-cluster", "")
        if not target:
            continue
        c = counters.setdefault(target, {
            "submitted": 0,
            "active_or_pending": 0,
            "completed": 0,
            "failed": 0,
        })
        c["submitted"] += 1
        status = j.get("status") or {}
        active = int(status.get("active", 0) or 0)
        succeeded = int(status.get("succeeded", 0) or 0)
        failed = int(status.get("failed", 0) or 0)
        c["active_or_pending"] += active
        c["completed"] += succeeded
        c["failed"] += failed
    return counters


def _wait_population_steady_state(
    *,
    context: str,
    namespace: str,
    disturbance_cell: str,
    target_clusters: list[str],
    min_active_per_target: int,
    confirm_seconds: int,
    timeout_seconds: int,
    stop_event: threading.Event,
    mode: str = "population",
) -> str:
    """Block until every target's active count holds ≥ min for ≥ confirm.

    iter-4h-2 § 5.4: this replaces the legacy `stabilisation_check`
    semantics for disturbance specs. Returns one of:
      - "stabilised"  band held long enough
      - "timeout"     timeout_seconds expired without confirmation
      - "skipped"     mode != "population"
      - "shutdown"    stop_event was set mid-wait

    Polls every 5 s. Read-only `kubectl get jobs` — no hook concern.
    """
    if mode != "population":
        return "skipped"
    if not target_clusters:
        return "skipped"

    print(
        f"[disturbance] population steady-state check: target "
        f"≥{min_active_per_target} per cluster for {confirm_seconds}s "
        f"(timeout {timeout_seconds}s)", flush=True,
    )
    started = time.monotonic()
    deadline = started + timeout_seconds
    in_band_since: Optional[float] = None
    while time.monotonic() < deadline:
        if stop_event.is_set():
            return "shutdown"
        jobs = _kubectl_get_disturbance_jobs(context, namespace, disturbance_cell)
        if jobs is None:
            # transient kubectl error; retry next poll
            time.sleep(5)
            continue
        counters = _per_target_population(jobs)
        in_band = all(
            counters.get(t, {}).get("active_or_pending", 0) >= min_active_per_target
            for t in target_clusters
        )
        now = time.monotonic()
        if in_band:
            if in_band_since is None:
                in_band_since = now
            elif now - in_band_since >= confirm_seconds:
                summary = ", ".join(
                    f"{t}={counters.get(t,{}).get('active_or_pending',0)}"
                    for t in target_clusters
                )
                print(f"[disturbance] population stabilised ({summary})", flush=True)
                return "stabilised"
        else:
            in_band_since = None
        time.sleep(5)
    return "timeout"


def _disturbance_population_sampler(
    *,
    context: str,
    namespace: str,
    state_id: str,
    disturbance_cell: str,
    target_clusters: list[str],
    sample_csv: Path,
    stop_event: threading.Event,
    started_monotonic: float,
    sample_interval_s: float = 30.0,
    circuit_breaker_skips: dict[str, int],
) -> None:
    """Background thread: sample per-target population every `interval`.

    Writes one row per `(state_id, target_cluster, t_offset_s, …)` to
    `sample_csv`. Snapshots `circuit_breaker_skips` (a live counter the
    disturbance-stream thread increments) so the CSV captures the
    cumulative skip count at each sample.
    """
    with sample_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "state_id", "target_cluster", "t_offset_s",
            "n_submitted_cumulative", "n_active_or_pending",
            "n_completed", "n_failed",
            "spawn_skips_circuit_breaker",
        ])
        while not stop_event.is_set():
            t_offset = time.monotonic() - started_monotonic
            jobs = _kubectl_get_disturbance_jobs(
                context, namespace, disturbance_cell,
            )
            if jobs is not None:
                counters = _per_target_population(jobs)
                for t in target_clusters:
                    c = counters.get(t, {})
                    w.writerow([
                        state_id, t, f"{t_offset:.1f}",
                        c.get("submitted", 0),
                        c.get("active_or_pending", 0),
                        c.get("completed", 0),
                        c.get("failed", 0),
                        circuit_breaker_skips.get(t, 0),
                    ])
                fh.flush()
            # Wait for either the next sample or shutdown — whichever first.
            if stop_event.wait(timeout=sample_interval_s):
                break


def _run_disturbance_stream(
    *,
    context: str,
    namespace: str,
    state_id: str,
    timeline_csv: Path,
    disturbance_dir: Path,
    target_clusters: list[str],
    per_target_circuit_breaker: dict[str, int],
    disturbance_cell: str,
    foreground_finished: threading.Event,
    fg_deadline_seconds: int,
    submit_log: list[DisturbanceSubmitResult],
    submit_log_lock: threading.Lock,
    circuit_breaker_skips: dict[str, int],
    started_monotonic: float,
) -> None:
    """Background thread (D10): apply disturbance Jobs per the planned
    timeline at t=0.

    Stops when ANY of:
      - last timeline entry has been processed,
      - `foreground_finished` event is set (foreground submission done),
      - `fg_deadline_seconds` has elapsed from `started_monotonic`.

    Circuit-breaker (D15): if the submitted-but-not-yet-terminal count
    on a target exceeds its `per_target_circuit_breaker` cap, the next
    scheduled spawn for that target is **skipped** (not deferred — the
    plan is to absorb pathological runaway spawning, not to compensate
    for queued Jobs). The skip is logged in `circuit_breaker_skips`
    (per-target counter, shared with the population sampler).
    """
    entries = _read_disturbance_timeline(timeline_csv)
    if not entries:
        print(f"[disturbance] {state_id}: timeline is empty; thread exits",
              flush=True)
        return

    deadline = started_monotonic + fg_deadline_seconds
    n_applied = 0
    n_skipped = 0
    last_population_check = 0.0
    cached_population: dict[str, dict[str, int]] = {}

    for entry in entries:
        if foreground_finished.is_set() or time.monotonic() > deadline:
            break

        target = entry["target_cluster"]
        job_name = entry["job_name"]
        planned_offset = entry["planned_submit_offset_seconds"]
        target_offset = started_monotonic + planned_offset
        wait_s = target_offset - time.monotonic()
        if wait_s > 0:
            # Wait in small slices so foreground_finished is honoured promptly.
            ended = foreground_finished.wait(timeout=wait_s)
            if ended:
                break
            if time.monotonic() > deadline:
                break

        # Circuit-breaker (D15): only refresh the population reading
        # every 10s — the cost is one kubectl call and reading more
        # often than the disturbance arrival rate is wasteful.
        if time.monotonic() - last_population_check > 10.0:
            jobs = _kubectl_get_disturbance_jobs(
                context, namespace, disturbance_cell,
            )
            if jobs is not None:
                cached_population = _per_target_population(jobs)
                last_population_check = time.monotonic()

        cap = per_target_circuit_breaker.get(target, 999_999)
        live_count = cached_population.get(target, {}).get("submitted", 0) - \
            cached_population.get(target, {}).get("completed", 0) - \
            cached_population.get(target, {}).get("failed", 0)
        if live_count >= cap:
            circuit_breaker_skips[target] = circuit_breaker_skips.get(target, 0) + 1
            n_skipped += 1
            with submit_log_lock:
                submit_log.append(DisturbanceSubmitResult(
                    job_path=disturbance_dir / f"{entry['job_index']:04d}-{job_name}.yaml",
                    job_name=job_name,
                    target_cluster=target,
                    workload_class=entry["workload_class"],
                    planned_offset_s=planned_offset,
                    applied=False,
                    skipped_reason="circuit_breaker",
                    apply_stderr="",
                    applied_at_utc="",
                ))
            print(
                f"[disturbance] CIRCUIT-BREAKER fired for {target} at "
                f"t+{(time.monotonic()-started_monotonic):.0f}s "
                f"(live={live_count}, cap={cap}); skipped {job_name}",
                flush=True,
            )
            continue

        job_path = disturbance_dir / f"{entry['job_index']:04d}-{job_name}.yaml"
        if not job_path.exists():
            with submit_log_lock:
                submit_log.append(DisturbanceSubmitResult(
                    job_path=job_path, job_name=job_name,
                    target_cluster=target,
                    workload_class=entry["workload_class"],
                    planned_offset_s=planned_offset,
                    applied=False,
                    skipped_reason=f"rendered file missing: {job_path}",
                    apply_stderr="",
                    applied_at_utc="",
                ))
            continue

        ok, err = _apply_job(context, job_path)
        applied_ts = datetime.now(timezone.utc).isoformat() if ok else ""
        if ok:
            n_applied += 1
            # Locally optimistic update so the next loop's circuit-
            # breaker check doesn't double-spawn within the 10 s
            # population-cache window.
            c = cached_population.setdefault(target, {
                "submitted": 0, "active_or_pending": 0,
                "completed": 0, "failed": 0,
            })
            c["submitted"] += 1
            c["active_or_pending"] += 1
        else:
            print(
                f"[disturbance] APPLY FAILED for {job_name} on {target}: "
                f"{err[:200]}", file=sys.stderr, flush=True,
            )

        with submit_log_lock:
            submit_log.append(DisturbanceSubmitResult(
                job_path=job_path, job_name=job_name,
                target_cluster=target,
                workload_class=entry["workload_class"],
                planned_offset_s=planned_offset,
                applied=ok,
                skipped_reason="" if ok else "apply-failed",
                apply_stderr="" if ok else err,
                applied_at_utc=applied_ts,
            ))

    print(
        f"[disturbance] {state_id} stream ended: "
        f"applied={n_applied} skipped={n_skipped} (cb skips per target: "
        f"{dict(circuit_breaker_skips)})",
        flush=True,
    )


def _bulk_delete_disturbance(
    *,
    context: str,
    namespace: str,
    run_id: str,
    state_id: str,
    disturbance_cell: str,
) -> bool:
    """End-of-state cleanup via the experiments-cleanup carve-out
    (D8). Tries the multi-key selector first; falls back to the
    single-key disturbance-cell label.

    Returns True if either form returned exit 0.
    """
    multi_key = (
        f"delphi.experiments/role=background,"
        f"delphi.experiments/run-id={run_id},"
        f"delphi.experiments/state-id={state_id}"
    )
    single_key = f"delphi.experiments/disturbance-cell={disturbance_cell}"
    for selector in (multi_key, single_key):
        try:
            proc = _kubectl([
                "--context", context, "delete", "jobs",
                "-l", selector, "-n", namespace, "--ignore-not-found",
            ], timeout=120)
        except subprocess.TimeoutExpired:
            print(f"[disturbance] cleanup timed out for selector "
                  f"{selector!r}", file=sys.stderr, flush=True)
            continue
        if proc.returncode == 0:
            print(
                f"[disturbance] bulk cleanup via {selector!r}: "
                f"{(proc.stdout or '').strip()}", flush=True,
            )
            return True
        print(
            f"[disturbance] bulk cleanup failed for selector {selector!r}: "
            f"{(proc.stderr or proc.stdout).strip()}",
            file=sys.stderr, flush=True,
        )
    return False


def _write_disturbance_submission_log(run_dir: Path, state_id: str,
                                      log: list[DisturbanceSubmitResult]) -> Path:
    """Persist the per-state disturbance submission log."""
    path = run_dir / "states" / state_id / "disturbance-submission-log.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "job_name", "target_cluster", "workload_class",
            "planned_offset_s", "applied", "skipped_reason",
            "applied_at_utc", "apply_stderr", "job_path",
        ])
        for r in log:
            w.writerow([
                r.job_name, r.target_cluster, r.workload_class,
                f"{r.planned_offset_s:.3f}", int(r.applied),
                r.skipped_reason, r.applied_at_utc, r.apply_stderr,
                str(r.job_path),
            ])
    return path


def _extract_per_target_circuit_breakers(state_spec: dict[str, Any]
                                          ) -> tuple[dict[str, int], list[str]]:
    """Pull max_concurrent_disturbance_jobs per target out of the spec.

    Returns (per_target_cap_dict, target_clusters_list). Per-target
    overrides win over the scalar `spawn.max_concurrent_disturbance_jobs`
    default. Caller uses the caps for the disturbance-stream circuit-
    breaker (D15).
    """
    disturbance = state_spec.get("disturbance") or {}
    targets: list[str] = list(disturbance.get("target_clusters") or [])
    spawn = disturbance.get("spawn") or {}
    default_cap = int(spawn.get("max_concurrent_disturbance_jobs", 20))
    overrides = disturbance.get("per_target_overrides") or {}
    per_target_cap: dict[str, int] = {}
    for t in targets:
        cap = overrides.get(t, {}).get("max_concurrent_disturbance_jobs",
                                       default_cap)
        per_target_cap[t] = int(cap)
    return per_target_cap, targets


def _submit_multistate(
    *,
    run_dir: Path,
    context: str,
    namespace: str,
    fg_deadline_seconds: int,
    poll_seconds: int,
    cluster_states_dir: Optional[Path],
    keep_disturbance: bool,
    stall_threshold_seconds: int,
    fail_fast: bool,
) -> int:
    """iter-4h-2 multistate submit orchestrator (§ 6.3).

    For each state in `multistate.json`:
      - if `disturbance:` present → start disturbance + sampler
        threads, foreground waits for steady-state, run foreground,
        signal stop, join, log, optional cleanup.
      - if `background_jobs:` present → existing single-pin path.
      - else (idle) → foreground only.

    Returns 0 / 2 with the same semantics as the bootstrap path.
    """
    manifest = json.loads(
        (run_dir / "multistate.json").read_text(encoding="utf-8")
    )
    states_dir_runtime = run_dir / "states"
    run_id = manifest["run_id"]
    if cluster_states_dir is None:
        try:
            cluster_states_dir = (
                run_dir.parents[2] / "specs" / "cluster-states"
            ).resolve()
        except IndexError:
            cluster_states_dir = None

    all_results: list[SubmitResult] = []
    any_failure = False

    for state_entry in manifest["states"]:
        state_id = state_entry["state_id"]
        state_out = states_dir_runtime / state_id
        spec_yaml = (cluster_states_dir / f"{state_id}.yaml"
                     if cluster_states_dir else None)
        state_spec = (_load_state_spec(spec_yaml)
                      if spec_yaml and spec_yaml.exists() else {})
        print(f"\n[submit] === multistate state {state_id} ===", flush=True)

        has_disturbance = bool(state_entry.get("has_disturbance"))
        has_background = (
            not has_disturbance and (state_out / "background").exists()
            and any((state_out / "background").glob("*.yaml"))
        )

        warmup_s = int(state_spec.get("warmup_seconds", 0) or 0)
        disturbance_cell = f"{run_id}-{state_id}".lower()

        # ---------- disturbance-stream branch ----------------------
        disturbance_thread: Optional[threading.Thread] = None
        sampler_thread: Optional[threading.Thread] = None
        foreground_finished = threading.Event()
        submit_log_lock = threading.Lock()
        disturbance_log: list[DisturbanceSubmitResult] = []
        circuit_breaker_skips: dict[str, int] = {}
        target_clusters: list[str] = []
        started_monotonic = time.monotonic()
        # bg_names is populated below only on the single-pin branch; we
        # initialise here so the cleanup `elif` does not see an
        # unbound name on the disturbance / idle branches.
        bg_names: list[str] = []

        if has_disturbance:
            per_target_cap, target_clusters = _extract_per_target_circuit_breakers(
                state_spec
            )
            for t in target_clusters:
                circuit_breaker_skips.setdefault(t, 0)

            timeline_csv = state_out / "disturbance-timeline-planned.csv"
            disturbance_dir = state_out / "disturbance"
            if not timeline_csv.exists() or not disturbance_dir.exists():
                print(
                    f"[submit] state {state_id}: multistate.json says "
                    f"has_disturbance but neither "
                    f"{timeline_csv} nor {disturbance_dir} exists; "
                    f"skipping disturbance for this state",
                    file=sys.stderr,
                )
            else:
                # Sampler writes to analysis/ (created on demand).
                analysis_dir = state_out / "analysis"
                analysis_dir.mkdir(parents=True, exist_ok=True)
                sample_csv = analysis_dir / "disturbance-population.csv"

                disturbance_thread = threading.Thread(
                    target=_run_disturbance_stream,
                    kwargs=dict(
                        context=context,
                        namespace=namespace,
                        state_id=state_id,
                        timeline_csv=timeline_csv,
                        disturbance_dir=disturbance_dir,
                        target_clusters=target_clusters,
                        per_target_circuit_breaker=per_target_cap,
                        disturbance_cell=disturbance_cell,
                        foreground_finished=foreground_finished,
                        fg_deadline_seconds=fg_deadline_seconds,
                        submit_log=disturbance_log,
                        submit_log_lock=submit_log_lock,
                        circuit_breaker_skips=circuit_breaker_skips,
                        started_monotonic=started_monotonic,
                    ),
                    name=f"disturbance-{state_id}",
                    daemon=True,
                )
                sampler_thread = threading.Thread(
                    target=_disturbance_population_sampler,
                    kwargs=dict(
                        context=context,
                        namespace=namespace,
                        state_id=state_id,
                        disturbance_cell=disturbance_cell,
                        target_clusters=target_clusters,
                        sample_csv=sample_csv,
                        stop_event=foreground_finished,
                        started_monotonic=started_monotonic,
                        circuit_breaker_skips=circuit_breaker_skips,
                    ),
                    name=f"sampler-{state_id}",
                    daemon=True,
                )
                disturbance_thread.start()
                sampler_thread.start()

                # Wait for the foreground delay floor: max(warmup_seconds,
                # population steady-state). The disturbance thread runs
                # in parallel from t=0 (D10).
                if warmup_s > 0:
                    _warmup.wait_warmup(warmup_s)
                check = state_spec.get("disturbance_steady_state_check") or {}
                if check.get("enabled", False):
                    outcome = _wait_population_steady_state(
                        context=context,
                        namespace=namespace,
                        disturbance_cell=disturbance_cell,
                        target_clusters=target_clusters,
                        min_active_per_target=int(check.get(
                            "min_active_per_target", 1)),
                        confirm_seconds=int(check.get("confirm_seconds", 30)),
                        timeout_seconds=int(check.get("timeout_seconds", 300)),
                        stop_event=foreground_finished,
                        mode=str(check.get("mode", "population")),
                    )
                    print(f"[submit] {state_id} steady-state check: "
                          f"{outcome}", flush=True)

        elif has_background:
            # Single-pin fallback. Use the existing iter-3a path.
            bg_results, applied_bg_names = _submit_background(
                context=context, namespace=namespace,
                background_dir=state_out / "background",
                state_id=state_id,
            )
            bg_names = applied_bg_names
            all_results.extend(bg_results)
            if any(not r.applied for r in bg_results):
                any_failure = True
            if warmup_s > 0:
                _warmup.wait_warmup(warmup_s)
            # No disturbance threads; bg_names captured for cleanup.
        else:
            # Idle: just wait the warmup (usually 0) and proceed.
            if warmup_s > 0:
                _warmup.wait_warmup(warmup_s)

        # ---------- foreground (common to all branches) ------------
        fg_results, _fg_names = _submit_foreground_with_timeline(
            context=context, namespace=namespace,
            jobs_dir=state_out / "foreground",
            timeline_csv=state_out / "timeline-planned.csv",
            fg_deadline_seconds=fg_deadline_seconds,
            poll_seconds=poll_seconds,
            state_id=state_id,
            stall_threshold_seconds=stall_threshold_seconds,
            fail_fast=fail_fast,
        )
        all_results.extend(fg_results)
        if any(r.applied and not r.completed for r in fg_results):
            any_failure = True

        # ---------- shutdown disturbance threads ------------------
        if disturbance_thread is not None:
            foreground_finished.set()
            disturbance_thread.join(timeout=30)
            if disturbance_thread.is_alive():
                print(f"[submit] WARN: disturbance thread for {state_id} "
                      f"did not exit within 30s", file=sys.stderr)
        if sampler_thread is not None:
            # foreground_finished doubles as the sampler's stop signal.
            sampler_thread.join(timeout=15)
            if sampler_thread.is_alive():
                print(f"[submit] WARN: sampler thread for {state_id} "
                      f"did not exit within 15s", file=sys.stderr)

        # Per-state disturbance submission log (regardless of thread
        # outcome — the log captures what the orchestrator tried to do).
        if has_disturbance:
            log_path = _write_disturbance_submission_log(
                run_dir, state_id, disturbance_log,
            )
            print(f"[submit] disturbance submission log: {log_path}",
                  flush=True)

        # ---------- end-of-state cleanup --------------------------
        if has_disturbance and not keep_disturbance:
            _bulk_delete_disturbance(
                context=context, namespace=namespace,
                run_id=run_id, state_id=state_id,
                disturbance_cell=disturbance_cell,
            )
        elif has_background and not keep_disturbance and bg_names:
            _delete_jobs(context=context, namespace=namespace,
                         job_names=bg_names)

    # ---- finalise overall submission log -------------------------
    log_path = _write_submission_log(run_dir, all_results)
    print(f"\n[submit] submission log: {log_path}")
    n_total = len(all_results)
    n_completed = sum(1 for r in all_results if r.completed)
    n_failed = sum(1 for r in all_results if r.failed)
    n_applied = sum(1 for r in all_results if r.applied)
    print(f"[submit] applied={n_applied}/{n_total} "
          f"completed={n_completed} failed={n_failed}")
    return 0 if not any_failure else 2


def submit_real(
    *,
    run_dir: Path,
    context: str,
    namespace: str,
    acknowledged: bool,
    fg_deadline_seconds: int = 1800,
    poll_seconds: int = 10,
    cluster_states_dir: Optional[Path] = None,
    keep_background: bool = False,
    keep_disturbance: bool = False,
    stall_threshold_seconds: int = 120,
    fail_fast: bool = False,
) -> int:
    """Apply a rendered run for real.

    Decides between three layouts:

    - **simple** layout (``run_dir/jobs/*.yaml`` +
      ``run_dir/timeline-planned.csv``): foreground-only run. Apply each
      Job according to its planned offset, wait for completion.

    - **bootstrap** layout (``run_dir/states/<state>/{background,foreground}``
      + per-state timeline + ``bootstrap.json``): expand by state.
      For each state:
        1. apply background-load Jobs,
        2. ``warmup_seconds`` then optional ``stabilisation_check``,
        3. apply foreground jobs paced by the per-state timeline,
        4. wait for foreground completion,
        5. ``kubectl delete`` the background Jobs we created
           (``keep_background=True`` skips this — operator can clean up later).

    - **multistate** layout (iter-4h-2; ``run_dir/multistate.json`` plus
      ``run_dir/states/<state>/{foreground,disturbance}``): expand by
      state. For each state:
        1. start the disturbance-stream thread at t=0 (D10),
        2. start the population sampler thread at t=0,
        3. foreground thread waits ``max(warmup_seconds,
           population_steady_state)`` before its first apply,
        4. apply foreground jobs paced by the per-state timeline,
        5. wait for foreground completion,
        6. signal both background threads to stop,
        7. single label-selector ``kubectl delete`` for cleanup
           (D8; ``keep_disturbance=True`` skips this).

    Returns 0 on success, non-zero if any apply or any job timed out.
    The contract for the safety model:

    - every apply targets ``--context <context>`` (default
      ``unique-logical-entrypoint``) with a ``kind: Job`` manifest;
    - every delete targets ``--context <context>`` with verb ``delete``
      and resource ``jobs`` in namespace ``delphi-experiments``
      (matches the carve-out in that contract).

    Background and disturbance Jobs ALSO carry
    ``spec.activeDeadlineSeconds`` so they die on their own if the
    orchestrator crashes mid-run.
    """
    if not acknowledged:
        raise RuntimeError(
            "submit_real requires acknowledged=True "
            "(--i-know-this-runs-real-jobs on the CLI). Refusing to apply.")
    if not run_dir.exists():
        print(f"run directory not found: {run_dir}", file=sys.stderr)
        return 1

    # iter-4h-4 crash-recovery: refuse to start if a previous campaign
    # left a cordon-state marker (orchestrator killed mid-cordon could
    # have left a shared member cluster cordoned).
    stale_markers = _cordon.scan_stale_markers(run_dir.parent)
    if stale_markers:
        for m in stale_markers:
            print(f"[submit] REFUSING TO START: stale cordon marker in "
                  f"{m.run_dir} (target={m.target!r}, started={m.started_at!r}). "
                  f"A previous run may have left {m.target!r} cordoned. Recover "
                  f"with:\n    {m.recovery_command()}\n"
                  f"then delete {m.run_dir / _cordon.MARKER_NAME}.",
                  file=sys.stderr)
        return 1

    all_results: list[SubmitResult] = []
    any_failure = False

    # iter-4h-2 multistate layout — detected first because its manifest
    # file is distinct from the bootstrap one and the two layouts
    # would otherwise look similar (both have a `states/` subdir).
    multistate_manifest = run_dir / "multistate.json"
    if multistate_manifest.exists():
        return _submit_multistate(
            run_dir=run_dir,
            context=context,
            namespace=namespace,
            fg_deadline_seconds=fg_deadline_seconds,
            poll_seconds=poll_seconds,
            cluster_states_dir=cluster_states_dir,
            keep_disturbance=keep_disturbance or keep_background,
            stall_threshold_seconds=stall_threshold_seconds,
            fail_fast=fail_fast,
        )

    bootstrap_manifest = run_dir / "bootstrap.json"
    if bootstrap_manifest.exists():
        # Bootstrap layout.
        bootstrap = json.loads(bootstrap_manifest.read_text(encoding="utf-8"))
        states_dir_runtime = run_dir / "states"
        if cluster_states_dir is None:
            # Resolve at the standard location so we can read warmup_seconds.
            cluster_states_dir = (run_dir.parents[2] / "specs" / "cluster-states").resolve()
            # parents: run_dir, runs/, experiments/ . But that's brittle if
            # run_dir was passed from outside; fall back to no stabilisation
            # if the path doesn't exist.

        for state_entry in bootstrap["states"]:
            state_id = state_entry["state_id"]
            state_out = states_dir_runtime / state_id
            spec_yaml = cluster_states_dir / f"{state_id}.yaml"
            state_spec = _load_state_spec(spec_yaml) if spec_yaml.exists() else {}
            print(f"\n[submit] === state {state_id} ===", flush=True)

            # 1. background-load Jobs
            bg_results, bg_names = _submit_background(
                context=context, namespace=namespace,
                background_dir=state_out / "background",
                state_id=state_id,
            )
            all_results.extend(bg_results)
            if any(not r.applied for r in bg_results):
                any_failure = True
                print(f"[submit] bg apply failures in state {state_id}; "
                      f"continuing with foreground anyway", file=sys.stderr)

            # 2. warmup + stabilisation
            warmup_s = int(state_spec.get("warmup_seconds", 0) or 0)
            if warmup_s > 0:
                _warmup.wait_warmup(warmup_s)
            if state_spec.get("stabilisation_check") and state_spec.get("target_clusters"):
                target_load = int(state_spec.get("target_load_percent", 0) or 0)
                # stabilisation is per-cluster; the spec lists target clusters.
                # Member-cluster context names match cluster names.
                for cluster in state_spec.get("target_clusters", []):
                    outcome = _warmup.stabilisation_check(
                        context=cluster,
                        target_load_percent=target_load,
                    )
                    print(f"[submit] stabilisation on {cluster}: {outcome}", flush=True)

            # 3. foreground
            fg_results, _fg_names = _submit_foreground_with_timeline(
                context=context, namespace=namespace,
                jobs_dir=state_out / "foreground",
                timeline_csv=state_out / "timeline-planned.csv",
                fg_deadline_seconds=fg_deadline_seconds,
                poll_seconds=poll_seconds,
                state_id=state_id,
                stall_threshold_seconds=stall_threshold_seconds,
                fail_fast=fail_fast,
            )
            all_results.extend(fg_results)
            if any(r.applied and not r.completed for r in fg_results):
                any_failure = True

            # 4. cleanup background
            if bg_names and not keep_background:
                _delete_jobs(context=context, namespace=namespace,
                             job_names=bg_names)
    else:
        # Simple layout.
        timeline_csv = run_dir / "timeline-planned.csv"
        jobs_dir = run_dir / "jobs"
        if not timeline_csv.exists() or not jobs_dir.exists():
            print(f"neither bootstrap.json nor timeline-planned.csv present at {run_dir}",
                  file=sys.stderr)
            return 1

        # iter-4h-4: optional orchestrator-driven cordon window. When the
        # manifest declares `cordon_window`, fire karmadactl
        # cordon/uncordon at fractions of the planned submission window,
        # in parallel with the foreground submission.
        cordon_scheduler = None
        cordon_stop: Optional[threading.Event] = None
        cordon_window = _load_manifest_cordon_window(run_dir)
        if cordon_window is not None:
            if not _cordon.karmadactl_available():
                print("[submit] REFUSING TO START: manifest declares a "
                      "cordon_window but `karmadactl` is not on PATH. Install "
                      "karmadactl (Karmada CLI) and retry — the cordon "
                      "experiment cannot run without it.", file=sys.stderr)
                return 1
            try:
                target, method, start_f, end_f, kctx = _cordon.parse_cordon_window(
                    cordon_window)
            except ValueError as exc:
                print(f"[submit] invalid cordon_window in manifest: {exc}",
                      file=sys.stderr)
                return 1
            window_s = _cordon.planned_window_seconds(timeline_csv)
            cordon_stop = threading.Event()
            cordon_started_monotonic = time.monotonic()
            cordon_scheduler = _cordon.CordonScheduler(
                run_dir=run_dir, target=target, method=method,
                karmada_context=kctx,
                start_offset_s=start_f * window_s,
                end_offset_s=end_f * window_s,
                started_monotonic=cordon_started_monotonic,
                snapshot_ttl_seconds=_CLUSTER_SNAPSHOT_TTL_SECONDS,
                planned_window_s=window_s,
                stop_event=cordon_stop,
            )
            cordon_scheduler.start()
            print(f"[submit] cordon scheduled: {target} "
                  f"cordon@t+{start_f * window_s:.0f}s "
                  f"uncordon@t+{end_f * window_s:.0f}s "
                  f"(planned window {window_s:.0f}s)", flush=True)

        try:
            fg_results, _ = _submit_foreground_with_timeline(
                context=context, namespace=namespace,
                jobs_dir=jobs_dir, timeline_csv=timeline_csv,
                fg_deadline_seconds=fg_deadline_seconds,
                poll_seconds=poll_seconds,
                state_id="",
                stall_threshold_seconds=stall_threshold_seconds,
                fail_fast=fail_fast,
            )
        finally:
            # Always signal + join the cordon thread so the guaranteed
            # uncordon (in its finally) runs before submit returns.
            if cordon_scheduler is not None and cordon_stop is not None:
                cordon_stop.set()
                cordon_scheduler.join(timeout=120)
                if cordon_scheduler.is_alive():
                    print(f"[submit] WARN: cordon scheduler still alive; check "
                          f"{run_dir / _cordon.MARKER_NAME} and uncordon manually",
                          file=sys.stderr)
        all_results.extend(fg_results)
        if any(r.applied and not r.completed for r in fg_results):
            any_failure = True

    log_path = _write_submission_log(run_dir, all_results)
    print(f"\n[submit] submission log: {log_path}")
    n_total = len(all_results)
    n_completed = sum(1 for r in all_results if r.completed)
    n_failed = sum(1 for r in all_results if r.failed)
    n_applied = sum(1 for r in all_results if r.applied)
    print(f"[submit] applied={n_applied}/{n_total} "
          f"completed={n_completed} failed={n_failed}")
    return 0 if not any_failure else 2
