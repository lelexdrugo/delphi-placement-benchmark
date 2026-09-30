"""iter-4h-3 registry-variant spec tests.

Pin that:
- r0-full parses to the same per-cluster labels as the reference
  cluster-registry.yaml (so the R0 campaign cell reproduces iter-4d);
- the treatment variants perturb exactly the cells the pre-committed
  interpretation (iter-4h-3 plan § 9) relies on.
"""
from __future__ import annotations

from pathlib import Path

from experiments.orchestrator.analysis.loaders import load_registry

_REPO = Path(__file__).resolve().parents[4]
_REFERENCE = _REPO / "experiments" / "specs" / "cluster-registry.yaml"
_VARIANTS = _REPO / "experiments" / "specs" / "cluster-registry-variants"


def _labels(path: Path):
    return load_registry(path).clusters


def test_r0_full_matches_reference_labels():
    ref = _labels(_REFERENCE)
    r0 = _labels(_VARIANTS / "r0-full.yaml")
    assert set(r0) == set(ref)
    for name in ref:
        assert r0[name] == ref[name], f"r0 label drift on {name}"


def test_r2_region_drift_flips_only_public_cloud_role():
    ref = _labels(_REFERENCE)
    r2 = _labels(_VARIANTS / "r2-region-drift.yaml")
    assert r2["public-cloud"].role == "cloud-zone-a"
    assert r2["public-cloud"].role != ref["public-cloud"].role
    # cost/rtt on public-cloud unchanged; the other clusters untouched.
    assert r2["public-cloud"].cost == ref["public-cloud"].cost
    assert r2["public-cloud"].rtt == ref["public-cloud"].rtt
    for name in ("on-prem", "edge-1", "edge-2"):
        assert r2[name] == ref[name]


def test_r3_compound_drift_flips_region_and_inverts_cost():
    ref = _labels(_REFERENCE)
    r3 = _labels(_VARIANTS / "r3-compound-drift.yaml")
    assert r3["public-cloud"].role == "cloud-zone-a"
    assert r3["public-cloud"].cost == "low"     # inverted high -> low
    assert r3["on-prem"].cost == "high"
    assert r3["edge-1"].cost == "high"
    assert r3["edge-2"].cost == "high"
    # rtt untouched on the edges — latency-sensitive must survive (§ 9.3).
    assert r3["edge-1"].rtt == ref["edge-1"].rtt == "low"
    assert r3["edge-2"].rtt == ref["edge-2"].rtt == "low"


def test_r1_missing_blanks_edge_cost_rtt_and_public_cloud_cpu_class():
    r1 = _labels(_VARIANTS / "r1-missing.yaml")
    assert r1["edge-1"].cost == "" and r1["edge-1"].rtt == ""
    assert r1["edge-2"].cost == "" and r1["edge-2"].rtt == ""
    assert r1["public-cloud"].cpu_class == ""   # non-consumed control
    # role/arch preserved so the region gate still resolves.
    assert r1["edge-1"].role == "edge"
    assert r1["public-cloud"].role == "public-cloud"
