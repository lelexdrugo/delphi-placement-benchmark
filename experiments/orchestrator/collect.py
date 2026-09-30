"""Run-manifest emission and post-run collection.

iter-1 shipped the manifest skeleton (planned timeline only).
iter-4a wires a minimal post-run pass that reads submission-log.csv
and updates manifest.json with per-job actual_submit_timestamp_utc
plus manifest-level submitted_at / finished_at. Member-cluster
polling (completion timestamps, sidecar metrics join) lands with the
paired-run protocol in iter-4d.1.

Kube-record persistence: ``capture_kube_records`` is the only function
in this module that contacts a cluster. Right after ``submit`` returns —
while every Job and its generated PropagationPolicy certainly still
exist, i.e. before the operator's end-of-run ``delete jobs`` removes
them — it performs the analyzer's own read-only ``kubectl get`` lookups
and writes ``runs/<id>/placements.json`` (PP target) and
``runs/<id>/job-status.json`` (aggregated Job.status startTime /
completionTime). ``collect_results`` stays cluster-free and mirrors them
into ``manifest.json.jobs[].assigned_cluster`` / ``completed_at``. The
analyzer prefers the persisted values and falls back to live ones, so a
run stays re-scorable from its own records after cleanup.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .analysis import kube as _kube
from .analysis import loaders as _loaders
from .render import RenderedJob


def emit_initial_manifest(output_dir: Path,
                          run_id: str,
                          spec_path: Path,
                          baseline: str,
                          jobs: list[RenderedJob],
                          cordon_window: Optional[dict[str, Any]] = None) -> Path:
    """Write the planned run manifest before any submission happens.

    This file is updated post-run by collect_results (iter-3). In
    iter-1 it captures the planned timeline only, so a reviewer can
    see what *would* run.

    iter-4h-4: when ``cordon_window`` is supplied (from a
    ``cordon-window.yaml`` passed to ``render --cordon-window``), it is
    recorded verbatim under the ``cordon_window`` key so ``submit_real``
    knows to drive the timed cordon/uncordon and the analyzer can
    attribute the planned window.
    """
    manifest = {
        "run_id": run_id,
        "spec": str(spec_path),
        "baseline": baseline,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "submitted_at": None,
        "finished_at": None,
        "jobs": [
            {
                "name": j.name,
                "spec_index": idx,
                "intent_profile": j.intent_profile,
                "workload_class": j.workload_class,
                "planned_submit_offset_seconds": j.planned_submit_offset_seconds,
                "actual_submit_timestamp_utc": None,
                "assigned_cluster": None,
                "completed_at": None,
                "exit": "not_run",
                "telemetry_join_key": j.name,
            }
            for idx, j in enumerate(jobs)
        ],
        "cluster_top_samples_path": None,
        "phase_a_seed_sha256": None,
        "decision_maker_image": None,
        "notes": [],
    }
    if cordon_window is not None:
        manifest["cordon_window"] = cordon_window
    path = output_dir / "manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def _read_submission_log(run_dir: Path) -> list[dict[str, str]]:
    path = run_dir / "submission-log.csv"
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_registry_variant_marker(output_dir: Path) -> dict[str, Any]:
    """Best-effort read of the iter-4h-3 registry-variant marker.

    set-registry-variant.ps1 writes experiments/runs/.registry-variant-active.json
    (sibling of every run-dir) recording the variant id + the SHA256 of
    the ConfigMap data it applied. collect_results stamps these into the
    run's manifest so the analyzer can attribute the run to a variant and
    verify the perturbation actually ran. Absent / malformed marker ⇒ {}
    (the run is treated as the un-perturbed reference)."""
    marker = output_dir.parent / ".registry-variant-active.json"
    if not marker.exists():
        return {}
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_min_max_utc(rows: list[dict[str, str]]) -> tuple[Optional[str], Optional[str]]:
    """Min and max applied_at_utc strings across rows. ISO-8601 sorts
    lexicographically as long as everything is in UTC, which is the
    contract submit.py enforces.
    """
    stamps = [r["applied_at_utc"] for r in rows
              if r.get("applied_at_utc")]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def _pp_record(pp: Optional[dict[str, Any]], recorded_at: str) -> dict[str, Any]:
    """One placements.json entry from a PropagationPolicy document.

    ``pp=None`` (no PP found for the Job at collection time, e.g. its
    decision never landed) yields a null ``selected_cluster``; the
    analyzer treats that exactly like an absent entry.
    """
    meta = (pp or {}).get("metadata") or {}
    created = meta.get("creationTimestamp")
    return {
        "selected_cluster": _kube.selected_cluster_from_pp(pp),
        "pp_name": meta.get("name") if pp else None,
        "pp_generation": _kube.pp_generation(pp) if pp else None,
        "pp_created_at": created if isinstance(created, str) else None,
        "recorded_at": recorded_at,
    }


# The Job.status fields the analyzer consumes (and nothing else: a field
# no reader uses would be unverifiable evidence). startTime feeds
# time_to_pod_spawn_s / execution_runtime_s; completionTime feeds
# completion_time, cost_proxy and the completed-upgrade.
_JOB_STATUS_FIELDS = (("start_time", "startTime"), ("completion_time", "completionTime"))


def _job_status_record(job: Optional[dict[str, Any]], recorded_at: str) -> dict[str, Any]:
    """One job-status.json entry from an aggregated Job document.

    Timestamps are stored verbatim (RFC 3339 strings) so the analyzer's
    offline parse is the same parse it applies to the live document.
    """
    status = (job or {}).get("status") or {}
    record: dict[str, Any] = {}
    for key, k8s_key in _JOB_STATUS_FIELDS:
        raw = status.get(k8s_key)
        record[key] = raw if isinstance(raw, str) else None
    record["recorded_at"] = recorded_at
    return record


def _merge_placement(name: str, prior: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """An existing non-null placement is never replaced (the earliest
    observation is closest to where the job ran); a null one is filled."""
    prior_cluster = prior.get("selected_cluster")
    if not prior_cluster:
        return record
    if record["selected_cluster"] and record["selected_cluster"] != prior_cluster:
        print(f"[collect] WARN placements: {name} live PP now targets "
              f"{record['selected_cluster']!r}, keeping recorded "
              f"{prior_cluster!r}", file=sys.stderr)
    return prior


def _merge_job_status(name: str, prior: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Per field: a recorded non-null value is never replaced, a null one
    is filled (e.g. completionTime of a job that finished after the
    submit tail). ``recorded_at`` moves only when something was filled."""
    if not prior:
        return record
    merged = dict(prior)
    filled = False
    for key, _ in _JOB_STATUS_FIELDS:
        old, new = prior.get(key), record.get(key)
        if old:
            if new and new != old:
                print(f"[collect] WARN job-status: {name} live {key} is now "
                      f"{new!r}, keeping recorded {old!r}", file=sys.stderr)
            continue
        if new:
            merged[key] = new
            filled = True
    if filled:
        merged["recorded_at"] = record["recorded_at"]
    return merged


def _write_artifact(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def capture_kube_records(
    run_dir: Path,
    *,
    context: str = "unique-logical-entrypoint",
    namespace: str = "delphi-experiments",
    job_names: Optional[list[str]] = None,
    fetch_pp: Optional[Callable[[str, str, str], Optional[dict[str, Any]]]] = None,
    fetch_job: Optional[Callable[[str, str, str], Optional[dict[str, Any]]]] = None,
    now: Optional[Callable[[], datetime]] = None,
) -> dict[str, Any]:
    """Persist everything the analyzer would otherwise read live via kubectl.

    For each foreground Job it performs the analyzer's own two read-only
    lookups (``analysis.kube.get_propagation_policy`` and
    ``analysis.kube.get_job`` — ``kubectl --context <ctx> get
    propagationpolicy|job … -o json``), so offline and online resolve the
    same values, and writes two sibling artifacts:

    ``placements.json`` — the PP target::

        {"schema_version": 1,
         "source": "PropagationPolicy.spec.placement.clusterAffinity.clusterNames[0]",
         "context": "<ctx>", "namespace": "<ns>",
         "placements": {"<job>": {"selected_cluster": "edge-1" | null,
                                  "pp_name": "pp-<job>" | null,
                                  "pp_generation": 1 | null,
                                  "pp_created_at": "<RFC3339>" | null,
                                  "recorded_at": "<UTC ISO>"}}}

    ``job-status.json`` — the aggregated Job.status timestamps::

        {"schema_version": 1,
         "source": "Job.status (aggregated, Karmada entrypoint)",
         "context": "<ctx>", "namespace": "<ns>",
         "jobs": {"<job>": {"start_time": "<RFC3339>" | null,
                            "completion_time": "<RFC3339>" | null,
                            "recorded_at": "<UTC ISO>"}}}

    Two files rather than one: each maps to one Kubernetes object with its
    own provenance, and they have different lifecycles — a placement is
    final once the PP exists, while a completionTime can legitimately
    arrive after this capture (a job that timed out in ``submit``) and be
    filled in by a later backfill.

    Job names default to the applied foreground rows of
    ``submission-log.csv`` (every layout writes it; background/disturbance
    Jobs are pinned, never scored, and deleted by ``submit`` before this
    runs).

    Merge rule (re-running is safe, e.g. ``collect --capture-kube-records``
    after cleanup): a recorded non-null value is never replaced — only
    warned about if the live one differs — and null values may be filled.

    Never raises: a failed snapshot must not cost the run its
    submission-log / manifest collection. kubectl missing or a lookup
    timeout aborts the remaining lookups (the cluster is unreachable;
    serial 30 s timeouts would stall the tail of a campaign) and is
    reported on stderr. Returns a small summary dict.
    """
    get_pp = fetch_pp or _kube.get_propagation_policy
    get_job = fetch_job or _kube.get_job
    clock = now or (lambda: datetime.now(timezone.utc))
    summary: dict[str, Any] = {
        "placements_captured": 0, "placements_missing": [],
        "job_status_captured": 0, "job_status_completed": 0, "job_status_missing": [],
        "aborted": None, "paths": [],
    }
    try:
        if job_names is None:
            job_names = [
                r["job_name"] for r in _read_submission_log(run_dir)
                if r.get("role") == "foreground"
                and r.get("applied") in ("1", "true", "True")
            ]
        names = sorted(set(job_names))
        if not names:
            print(f"[collect] kube records: no applied foreground jobs in {run_dir}; "
                  f"nothing to capture", file=sys.stderr)
            return summary

        try:
            placements = dict(_loaders.load_placements(run_dir))
            job_status = dict(_loaders.load_job_status(run_dir))
        except ValueError as exc:
            print(f"[collect] WARN kube records: {exc}; refusing to overwrite it",
                  file=sys.stderr)
            summary["aborted"] = str(exc)
            return summary

        failed = object()   # sentinel: this lookup raised (≠ "not found")
        for name in names:
            fetched: dict[str, Any] = {}
            for kind, fetch in (("pp", get_pp), ("job", get_job)):
                try:
                    fetched[kind] = fetch(context, namespace, name)
                except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                    summary["aborted"] = f"{type(exc).__name__}: {exc}"
                    break
                except Exception as exc:  # noqa: BLE001 - best-effort per lookup
                    print(f"[collect] WARN kube records: {kind} lookup failed for "
                          f"{name}: {exc}", file=sys.stderr)
                    fetched[kind] = failed
            if summary["aborted"]:
                print(f"[collect] WARN kube records: lookup aborted at {name} "
                      f"({summary['aborted']}); remaining jobs not captured",
                      file=sys.stderr)
                break
            stamp = clock().isoformat()

            if fetched["pp"] is not failed:
                placements[name] = _merge_placement(
                    name, placements.get(name) or {}, _pp_record(fetched["pp"], stamp))
            if (placements.get(name) or {}).get("selected_cluster"):
                summary["placements_captured"] += 1
            else:
                summary["placements_missing"].append(name)

            if fetched["job"] is not failed:
                job_status[name] = _merge_job_status(
                    name, job_status.get(name) or {}, _job_status_record(fetched["job"], stamp))
            rec = job_status.get(name) or {}
            if rec.get("start_time") or rec.get("completion_time"):
                summary["job_status_captured"] += 1
                if rec.get("completion_time"):
                    summary["job_status_completed"] += 1
            else:
                summary["job_status_missing"].append(name)

        header = {"context": context, "namespace": namespace}
        for filename, key, records, doc_extra in (
            (_loaders.PLACEMENTS_FILENAME, "placements", placements, {
                "schema_version": _loaders.PLACEMENTS_SCHEMA_VERSION,
                "source": "PropagationPolicy.spec.placement.clusterAffinity.clusterNames[0]",
            }),
            (_loaders.JOB_STATUS_FILENAME, "jobs", job_status, {
                "schema_version": _loaders.JOB_STATUS_SCHEMA_VERSION,
                "source": "Job.status (aggregated, Karmada entrypoint)",
            }),
        ):
            if not records:
                print(f"[collect] WARN kube records: nothing captured; "
                      f"{filename} not written", file=sys.stderr)
                continue
            path = run_dir / filename
            _write_artifact(path, {**header, **doc_extra, key: records})
            summary["paths"].append(str(path))

        n = len(names)
        if summary["paths"]:
            print(f"[collect] placements: {summary['placements_captured']}/{n} "
                  f"foreground job(s) recorded in {run_dir / _loaders.PLACEMENTS_FILENAME}",
                  flush=True)
            print(f"[collect] job-status: {summary['job_status_captured']}/{n} "
                  f"foreground job(s) recorded ({summary['job_status_completed']} with "
                  f"completionTime) in {run_dir / _loaders.JOB_STATUS_FILENAME}",
                  flush=True)
        if summary["placements_missing"]:
            print(f"[collect] WARN placements: no PropagationPolicy target for "
                  f"{len(summary['placements_missing'])} job(s): "
                  f"{', '.join(summary['placements_missing'])}", file=sys.stderr)
        if summary["job_status_missing"]:
            print(f"[collect] WARN job-status: no Job.status timestamps for "
                  f"{len(summary['job_status_missing'])} job(s): "
                  f"{', '.join(summary['job_status_missing'])}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - never fail the run tail
        summary["aborted"] = f"{type(exc).__name__}: {exc}"
        print(f"[collect] WARN kube records: capture failed: {summary['aborted']}",
              file=sys.stderr)
    return summary


def collect_results(output_dir: Path) -> dict[str, Any]:
    """Post-run manifest update.

    Reads submission-log.csv (written by submit.py) and overlays:
    - per-job actual_submit_timestamp_utc,
    - per-job assigned_cluster / completed_at, mirrored from
      placements.json / job-status.json when ``capture_kube_records``
      recorded them (left untouched otherwise),
    - manifest-level submitted_at (min applied_at_utc) and finished_at
      (current UTC instant — best-effort closure; replace with the
      cluster-side latest completionTime when paired-run telemetry
      lands in iter-4d.1).

    Returns the new manifest dict. Writes manifest.json in-place. No
    cluster contact happens here — the kube records are captured
    beforehand by ``capture_kube_records``.
    """
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no manifest.json at {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sub_log = _read_submission_log(output_dir)
    try:
        placements = _loaders.load_placements(output_dir)
    except ValueError as exc:
        print(f"[collect] WARN: {exc}; assigned_cluster not mirrored", file=sys.stderr)
        placements = {}
    try:
        job_status = _loaders.load_job_status(output_dir)
    except ValueError as exc:
        print(f"[collect] WARN: {exc}; completed_at not mirrored", file=sys.stderr)
        job_status = {}

    by_name = {r["job_name"]: r for r in sub_log}
    for entry in manifest.get("jobs", []) or []:
        name = entry.get("name")
        placed = (placements.get(name) or {}).get("selected_cluster")
        if placed:
            entry["assigned_cluster"] = placed
        finished = (job_status.get(name) or {}).get("completion_time")
        if finished:
            entry["completed_at"] = finished
        row = by_name.get(name)
        if not row:
            continue
        ts = row.get("applied_at_utc") or ""
        if ts:
            entry["actual_submit_timestamp_utc"] = ts
        entry["exit"] = (
            "succeeded" if row.get("completed") in ("1", "true", "True")
            else "failed" if row.get("failed") in ("1", "true", "True")
            else "applied"
        )

    submitted_at, _ = _parse_min_max_utc(sub_log)
    if submitted_at and not manifest.get("submitted_at"):
        manifest["submitted_at"] = submitted_at
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()

    # iter-4h-3: stamp the active registry variant (if any) from the
    # set-registry-variant marker so the analyzer can attribute this run
    # to a variant and verify the perturbation ran (registry_diff).
    marker = _read_registry_variant_marker(output_dir)
    if marker.get("variant_id") and not manifest.get("registry_variant"):
        manifest["registry_variant"] = marker["variant_id"]
        if marker.get("variant_path"):
            manifest["registry_variant_path"] = marker["variant_path"]
        if marker.get("sha_active"):
            manifest["registry_sha_active"] = marker["sha_active"]

    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest
