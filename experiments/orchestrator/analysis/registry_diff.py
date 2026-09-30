"""registry_diff — iter-4h-3 registry-degradation provenance.

Computes the per-(cluster, field) difference between the ACTIVE registry
variant (the perturbed ConfigMap mounted into the decision-maker for a
run) and the REFERENCE ground-truth registry
(experiments/specs/cluster-registry.yaml). The result is emitted at
``summary.json.baselines.<baseline>.registry_diff`` so the
registry-degradation figure is interpretable: it records exactly which
labels were perturbed and whether the Heuristic-Scorer consumes them.

Two invariants this module preserves (iter-4h-3 plan §§ 1.3, 5):

- The REFERENCE is also what ``intent_compliance`` scores against —
  never the variant. This module only *describes* the perturbation; it
  changes no metric.
- The output is deterministic (cells sorted by ``(cluster, field)``)
  so the analyzer's byte-identical-rerun idempotency contract holds.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from .loaders import load_registry
from .models import ClusterRegistry

# The six categorical label fields load_registry reads, in fixed order
# so perturbed_cells is deterministic.
_FIELDS: tuple[str, ...] = ("role", "arch", "cost", "rtt", "cpu_class", "mem_class")

# Fields the Heuristic-Scorer's Score() actually consumes
# (heuristic_scorer.go:59-61 — regionMatch(Role), labelMatch(RTT),
# labelMatch(Cost)). Everything else is a control variable: perturbing
# it cannot change the Heuristic's decision.
_CONSUMED: frozenset[str] = frozenset({"role", "rtt", "cost"})

_VARIANT_ID_RE = re.compile(r"^(r\d+)")


def _sha256_file(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _variant_id(active_path: Optional[Path], manifest: dict[str, Any]) -> str:
    """Short variant id (e.g. ``r2``). Manifest wins; else parse the
    filename prefix; else ``full`` when no variant ran."""
    mid = manifest.get("registry_variant")
    if mid:
        return str(mid)
    if active_path is None:
        return "full"
    m = _VARIANT_ID_RE.match(active_path.stem)
    return m.group(1) if m else active_path.stem


def compute_registry_diff(
    *,
    reference: ClusterRegistry,
    reference_path: Path,
    active_path: Optional[Path],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Return the registry_diff dict for one run.

    ``reference`` is the already-loaded ground-truth registry;
    ``reference_path`` is its file (for the SHA); ``active_path`` is the
    committed variant YAML that was mounted for this run (None ⇒ the run
    used the un-perturbed registry); ``manifest`` may carry
    ``registry_variant`` / ``registry_sha_active`` written by the
    orchestrator from the set-registry-variant marker (collect.py).
    """
    sha_reference = _sha256_file(reference_path)
    runtime_sha = manifest.get("registry_sha_active")

    if active_path is None:
        return {
            "variant_id": _variant_id(None, manifest),
            "sha_reference": sha_reference,
            "sha_active": None,
            "sha_active_runtime": runtime_sha,
            "sha_match": None,
            "perturbed_cells": [],
        }

    active = load_registry(active_path)
    sha_active = _sha256_file(active_path)

    cells: list[dict[str, Any]] = []
    for cname in sorted(set(reference.clusters) | set(active.clusters)):
        ref_c = reference.clusters.get(cname)
        act_c = active.clusters.get(cname)
        for field in _FIELDS:
            ref_v = getattr(ref_c, field, "") if ref_c else ""
            act_v = getattr(act_c, field, "") if act_c else ""
            if ref_v != act_v:
                cells.append({
                    "cluster": cname,
                    "field": field,
                    "reference_value": ref_v,
                    "active_value": act_v,
                    "consumed_by_heuristic": field in _CONSUMED,
                })

    if runtime_sha is None:
        sha_match: Optional[bool] = None
    else:
        sha_match = runtime_sha == sha_active

    return {
        "variant_id": _variant_id(active_path, manifest),
        "sha_reference": sha_reference,
        "sha_active": sha_active,
        "sha_active_runtime": runtime_sha,
        "sha_match": sha_match,
        "perturbed_cells": cells,
    }
