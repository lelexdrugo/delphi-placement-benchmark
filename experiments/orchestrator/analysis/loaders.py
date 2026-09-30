"""File-only IO: reads runs/<id>/* + cluster-registry.yaml.

Returns immutable dataclasses; never mutates anything on disk. Kept
free of DB / kubectl side-effects so the runner can compose loaders
deterministically and so unit tests can drive them with tiny
fixtures.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import ClusterLabels, ClusterRegistry, IntentProfile, JobRecord


def load_registry(registry_path: Path) -> ClusterRegistry:
    """Read cluster-registry.yaml and return a ClusterRegistry."""
    with registry_path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)

    clusters: dict[str, ClusterLabels] = {}
    for name, data in (doc.get("clusters") or {}).items():
        clusters[name] = ClusterLabels(
            name=name,
            role=str(data.get("role", "")),
            arch=str(data.get("arch", "")),
            cost=str(data.get("cost", "")),
            rtt=str(data.get("rtt", "")),
            cpu_class=str(data.get("cpu_class", "")),
            mem_class=str(data.get("mem_class", "")),
        )

    intent_profiles: dict[str, IntentProfile] = {}
    for name, data in (doc.get("intent_profiles") or {}).items():
        intent_profiles[name] = IntentProfile(
            name=name,
            propagation_requirements=dict(data.get("propagation_requirements") or {}),
            preferred_classes=tuple(data.get("preferred_classes") or ()),
        )

    return ClusterRegistry(clusters=clusters, intent_profiles=intent_profiles)


def _parse_utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    # tolerate trailing 'Z' (RFC 3339) — datetime.fromisoformat handles
    # offset-aware strings since Python 3.11.
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_manifest(run_dir: Path) -> dict[str, Any]:
    """Read manifest.json. Returns empty dict if absent (degraded mode)."""
    path = run_dir / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_submission_log(run_dir: Path) -> list[dict[str, Any]]:
    """Read submission-log.csv into a list of raw rows.

    Returns [] when the file is absent (e.g. a render-only run-dir).
    The runner converts these rows + manifest fields into JobRecord
    instances.
    """
    path = run_dir / "submission-log.csv"
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_timeline(run_dir: Path) -> dict[str, dict[str, str]]:
    """Read timeline-planned.csv per job_name → dict of fields.

    Used to recover (workload_class, intent_profile) per job when
    submission-log records the bare role/state only.
    """
    path = run_dir / "timeline-planned.csv"
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    with path.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[row["job_name"]] = row
    return out


# Persisted placement artifact (written at collection time by
# ``collect.capture_kube_records`` while each Job's PropagationPolicy still
# exists; see that function for the schema). The analyzer reads it so a
# run stays re-scorable after its campaign namespace is cleaned up.
PLACEMENTS_FILENAME = "placements.json"
PLACEMENTS_SCHEMA_VERSION = 1
# Sibling artifact, same capture step: the aggregated Job.status
# timestamps the analyzer otherwise reads live (startTime →
# time_to_pod_spawn / execution runtime; completionTime → completion
# time, cost proxy, and the completed-upgrade).
JOB_STATUS_FILENAME = "job-status.json"
JOB_STATUS_SCHEMA_VERSION = 1


def _load_keyed_artifact(run_dir: Path, filename: str, key: str) -> dict[str, dict[str, Any]]:
    """Read ``<run_dir>/<filename>`` → ``doc[key]`` as ``{job_name: record}``.

    Returns ``{}`` when the file is absent (every run recorded before the
    artifact existed). A present-but-malformed file raises ``ValueError``:
    silently ignoring it would turn a recorded value into a data gap.
    """
    path = run_dir / filename
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"malformed {path}: {exc}") from exc
    records = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(records, dict):
        raise ValueError(f"malformed {path}: missing {key!r} object")
    return {str(k): v for k, v in records.items() if isinstance(v, dict)}


def load_placements(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Read ``placements.json`` → ``{job_name: record}`` ({} if absent)."""
    return _load_keyed_artifact(run_dir, PLACEMENTS_FILENAME, "placements")


def load_job_status(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Read ``job-status.json`` → ``{job_name: record}`` ({} if absent)."""
    return _load_keyed_artifact(run_dir, JOB_STATUS_FILENAME, "jobs")


def detect_layout(run_dir: Path) -> str:
    """Return "bootstrap" or "simple"."""
    return "bootstrap" if (run_dir / "bootstrap.json").exists() else "simple"


def load_bootstrap(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "bootstrap.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_job_records(run_dir: Path) -> tuple[list[JobRecord], dict[str, Any]]:
    """Compose JobRecord list from runs/<id>/* without DB or kubectl.

    DB and Kube data are layered on top by the runner. This function
    returns the (job, manifest) pair so the runner can also reference
    manifest-level submitted_at / finished_at without re-reading.
    """
    manifest = load_manifest(run_dir)
    sub_log = load_submission_log(run_dir)

    # Build per-name lookup from timeline + bootstrap per-state timelines.
    timeline_by_name: dict[str, dict[str, str]] = {}
    if detect_layout(run_dir) == "simple":
        timeline_by_name.update(load_timeline(run_dir))
    else:
        states_dir = run_dir / "states"
        if states_dir.exists():
            for state_dir in states_dir.iterdir():
                tl = state_dir / "timeline-planned.csv"
                if tl.exists():
                    with tl.open("r", newline="", encoding="utf-8") as fh:
                        for row in csv.DictReader(fh):
                            timeline_by_name[row["job_name"]] = row

    # Build manifest per-job lookup so we can pull
    # actual_submit_timestamp_utc when collect.py has populated it.
    manifest_by_name: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("jobs", []) or []:
        manifest_by_name[entry["name"]] = entry

    # Persisted placement (collection time). placements.json is
    # authoritative; manifest.json.assigned_cluster (which collect mirrors
    # from it) is the secondary source. Both are absent on every run
    # recorded before this artifact existed, which then load exactly as
    # before (selected_cluster=None until kube enrichment).
    placements = load_placements(run_dir)
    # Persisted Job.status timestamps (job-status.json; completion also
    # mirrored into manifest.json.completed_at). Same precedence idea.
    job_status = load_job_status(run_dir)

    jobs: list[JobRecord] = []
    seen = set()
    for row in sub_log:
        name = row["job_name"]
        seen.add(name)
        tl = timeline_by_name.get(name, {})
        mf = manifest_by_name.get(name, {})
        pl = placements.get(name, {})
        persisted = (str(pl.get("selected_cluster") or "")
                     or str(mf.get("assigned_cluster") or "")) or None
        pl_from_artifact = bool(pl.get("selected_cluster"))
        js = job_status.get(name, {})
        persisted_start = _parse_utc(js.get("start_time"))
        persisted_completion = (_parse_utc(js.get("completion_time"))
                                or _parse_utc(mf.get("completed_at")))

        applied_at = _parse_utc(row.get("applied_at_utc") or "") or _parse_utc(mf.get("actual_submit_timestamp_utc"))

        jobs.append(JobRecord(
            name=name,
            role=row.get("role", ""),
            state_id=row.get("state_id", ""),
            intent_profile=tl.get("intent_profile", "") or mf.get("intent_profile", ""),
            workload_class=tl.get("workload_class", "") or mf.get("workload_class", ""),
            applied=row.get("applied") in ("1", "true", "True"),
            # Same rule the kube enrichment applies to a live completionTime.
            completed=(row.get("completed") in ("1", "true", "True")
                       or persisted_completion is not None),
            failed=row.get("failed") in ("1", "true", "True"),
            applied_at_utc=applied_at,
            wall_clock_seconds=float(row.get("wall_clock_seconds", "0") or 0.0),
            skipped_reason=row.get("skipped_reason", ""),
            selected_cluster=persisted,
            persisted_cluster=persisted,
            # PP provenance travels with the persisted target it belongs to.
            pp_generation=int(pl.get("pp_generation") or 0) if pl_from_artifact else 0,
            pp_created_at=_parse_utc(pl.get("pp_created_at")) if pl_from_artifact else None,
            k8s_start_time=persisted_start,
            k8s_completion_time=persisted_completion,
            persisted_start_time=persisted_start,
            persisted_completion_time=persisted_completion,
        ))

    # Any manifest job not in submission-log: include with role=pending so
    # the analyzer can report the gap.
    for name, entry in manifest_by_name.items():
        if name in seen:
            continue
        jobs.append(JobRecord(
            name=name,
            role=str(entry.get("role", "pending")) or "pending",
            state_id=str(entry.get("state_id", "")),
            intent_profile=str(entry.get("intent_profile", "")),
            workload_class=str(entry.get("workload_class", "")),
        ))

    jobs.sort(key=lambda j: (j.state_id, j.workload_class, j.intent_profile, j.name))
    return jobs, manifest
