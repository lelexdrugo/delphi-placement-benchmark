"""Unit tests for registry_diff.compute_registry_diff (iter-4h-3)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from experiments.orchestrator.analysis.loaders import load_registry
from experiments.orchestrator.analysis.registry_diff import compute_registry_diff

_REPO = Path(__file__).resolve().parents[4]
_REFERENCE = _REPO / "experiments" / "specs" / "cluster-registry.yaml"
_VARIANTS = _REPO / "experiments" / "specs" / "cluster-registry-variants"


@pytest.fixture
def reference():
    return load_registry(_REFERENCE)


def _diff(reference, variant_file, manifest=None):
    return compute_registry_diff(
        reference=reference,
        reference_path=_REFERENCE,
        active_path=(_VARIANTS / variant_file) if variant_file else None,
        manifest=manifest or {},
    )


def test_full_no_variant(reference):
    d = _diff(reference, None)
    assert d["variant_id"] == "full"
    assert d["perturbed_cells"] == []
    assert d["sha_active"] is None
    assert d["sha_match"] is None
    assert len(d["sha_reference"]) == 64


def test_r0_no_perturbed_cells(reference):
    d = _diff(reference, "r0-full.yaml")
    assert d["variant_id"] == "r0"
    assert d["perturbed_cells"] == []


def test_r2_single_consumed_role_cell(reference):
    d = _diff(reference, "r2-region-drift.yaml")
    assert d["variant_id"] == "r2"
    assert d["perturbed_cells"] == [{
        "cluster": "public-cloud", "field": "role",
        "reference_value": "public-cloud", "active_value": "cloud-zone-a",
        "consumed_by_heuristic": True,
    }]


def test_r3_cells_are_sorted_and_consumed(reference):
    d = _diff(reference, "r3-compound-drift.yaml")
    cells = d["perturbed_cells"]
    # Sorted by (cluster, field); within public-cloud, role precedes cost
    # because _FIELDS lists role before cost.
    assert [(c["cluster"], c["field"]) for c in cells] == [
        ("edge-1", "cost"), ("edge-2", "cost"), ("on-prem", "cost"),
        ("public-cloud", "role"), ("public-cloud", "cost"),
    ]
    assert all(c["consumed_by_heuristic"] for c in cells)


def test_r1_marks_control_field_not_consumed(reference):
    d = _diff(reference, "r1-missing.yaml")
    by = {(c["cluster"], c["field"]): c for c in d["perturbed_cells"]}
    assert by[("public-cloud", "cpu_class")]["consumed_by_heuristic"] is False
    assert by[("edge-1", "cost")]["consumed_by_heuristic"] is True
    assert by[("edge-1", "cost")]["active_value"] == ""
    # 4 consumed edge cells (cost/rtt x2) + 1 control = 5.
    assert len(d["perturbed_cells"]) == 5


def test_sha_match_from_manifest(reference):
    sha = hashlib.sha256((_VARIANTS / "r2-region-drift.yaml").read_bytes()).hexdigest()
    d = _diff(reference, "r2-region-drift.yaml", manifest={"registry_sha_active": sha})
    assert d["sha_active"] == sha
    assert d["sha_match"] is True
    d2 = _diff(reference, "r2-region-drift.yaml",
               manifest={"registry_sha_active": "deadbeef"})
    assert d2["sha_match"] is False


def test_variant_id_from_manifest_wins(reference):
    d = _diff(reference, "r2-region-drift.yaml",
              manifest={"registry_variant": "r2-custom"})
    assert d["variant_id"] == "r2-custom"
