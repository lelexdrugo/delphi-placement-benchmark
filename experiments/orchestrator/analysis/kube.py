"""kubectl read-only wrappers used by the analyzer.

Reads only. Every subprocess call passes ``--context <ctx>`` so the
hooks accept it. No mutations, no deletes.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any, Optional

from .loaders import _parse_utc


def _kubectl_json(args: list[str], *, timeout: int = 30) -> Optional[dict[str, Any]]:
    proc = subprocess.run(
        ["kubectl", *args],
        capture_output=True, text=True, check=False, timeout=timeout,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def get_job(context: str, namespace: str, name: str) -> Optional[dict[str, Any]]:
    """`kubectl --context <ctx> get job <name> -n <ns> -o json`."""
    return _kubectl_json([
        "--context", context, "get", "job", name, "-n", namespace, "-o", "json",
    ])


def get_propagation_policy(context: str, namespace: str, job_name: str) -> Optional[dict[str, Any]]:
    """`kubectl --context <ctx> get propagationpolicy pp-<jobname> -n <ns> -o json`.

    The DELPHI controller materializes PPs as ``pp-<jobname>``. Falls
    back to a label selector if the well-known name isn't found.
    """
    pp_name = f"pp-{job_name}"
    doc = _kubectl_json([
        "--context", context, "get", "propagationpolicy", pp_name, "-n", namespace, "-o", "json",
    ])
    if doc is not None:
        return doc
    # fallback: list PPs and find one with a matching owner / resourceSelector.
    lst = _kubectl_json([
        "--context", context, "get", "propagationpolicy", "-n", namespace, "-o", "json",
    ])
    if not lst:
        return None
    for item in lst.get("items", []) or []:
        meta = item.get("metadata") or {}
        for ref in (meta.get("ownerReferences") or []):
            if ref.get("name") == job_name and ref.get("kind") == "Job":
                return item
        spec = item.get("spec") or {}
        for sel in (spec.get("resourceSelectors") or []):
            if sel.get("kind") == "Job" and sel.get("name") == job_name:
                return item
    return None


def selected_cluster_from_pp(pp: Optional[dict[str, Any]]) -> Optional[str]:
    """Read PropagationPolicy.spec.placement.clusterAffinity.clusterNames[0]."""
    if not pp:
        return None
    spec = pp.get("spec") or {}
    placement = spec.get("placement") or {}
    aff = placement.get("clusterAffinity") or {}
    names = aff.get("clusterNames") or []
    return names[0] if names else None


def pp_generation(pp: Optional[dict[str, Any]]) -> int:
    if not pp:
        return 0
    meta = pp.get("metadata") or {}
    return int(meta.get("generation") or 0)


def pp_creation_time(pp: Optional[dict[str, Any]]) -> Optional[datetime]:
    """PropagationPolicy ``.metadata.creationTimestamp``.

    A proxy for *decision-landed time*: the controller materialises the PP
    shortly after the decision result is applied. iter-4h-4 uses it to
    classify a cordon decision by when it was *made* (not when the Job was
    submitted), which removes the decision-latency confound at the cordon
    boundaries — see cordon_response.compute.
    """
    if not pp:
        return None
    raw = (pp.get("metadata") or {}).get("creationTimestamp")
    if isinstance(raw, str):
        return _parse_utc(raw)
    return None


def job_completion_time(job: Optional[dict[str, Any]]) -> Optional[datetime]:
    if not job:
        return None
    status = job.get("status") or {}
    raw = status.get("completionTime")
    if raw is None:
        return None
    if isinstance(raw, str):
        return _parse_utc(raw)
    return None


def job_start_time(job: Optional[dict[str, Any]]) -> Optional[datetime]:
    if not job:
        return None
    status = job.get("status") or {}
    raw = status.get("startTime")
    if raw is None:
        return None
    if isinstance(raw, str):
        return _parse_utc(raw)
    return None


def now_utc() -> datetime:
    """Indirection for tests."""
    return datetime.now(timezone.utc)
