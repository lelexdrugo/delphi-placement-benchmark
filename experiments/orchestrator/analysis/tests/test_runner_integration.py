"""End-to-end runner with StubDB + fixture run-dir + no kubectl.

Validates:
- the full pipeline runs to exit-0 on a clean fixture;
- analysis/ output is byte-identical on re-run (idempotency);
- summary.json contains the expected per-baseline projection shape.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from experiments.orchestrator.analysis.db import StubDB
from experiments.orchestrator.analysis.runner import run_analysis


_REGISTRY_FIXTURE = {
    "clusters": {
        "edge-1": {
            "role": "edge", "arch": "arm64",
            "cost": "low", "rtt": "low",
            "cpu_class": "low", "mem_class": "low",
        },
        "public-cloud": {
            "role": "public-cloud", "arch": "amd64",
            "cost": "high", "rtt": "medium",
            "cpu_class": "high", "mem_class": "medium",
        },
    },
    "intent_profiles": {
        "latency-sensitive": {
            "propagation_requirements": {
                "region": "edge", "latency": "low",
                "cost": "any", "weight": 1.0,
            },
            "preferred_classes": ["edge"],
        },
        "cost-aware": {
            "propagation_requirements": {
                "region": "any", "latency": "any",
                "cost": "low", "weight": 1.0,
            },
            "preferred_classes": ["on-premises", "edge"],
        },
    },
}


def _build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal valid run-dir + registry. Returns (run_dir, registry)."""
    registry_path = tmp_path / "cluster-registry.yaml"
    registry_path.write_text(yaml.safe_dump(_REGISTRY_FIXTURE), encoding="utf-8")

    run_dir = tmp_path / "runs" / "pilot-fixture-1"
    run_dir.mkdir(parents=True)

    # manifest.json
    manifest = {
        "run_id": "pilot-fixture-1",
        "baseline": "static-karmada",
        "submitted_at": "2026-05-23T18:00:00Z",
        "finished_at": "2026-05-23T18:05:00Z",
        "jobs": [
            {"name": "fg-001", "role": "foreground",
             "intent_profile": "latency-sensitive", "workload_class": "cpu",
             "actual_submit_timestamp_utc": "2026-05-23T18:00:00Z"},
            {"name": "fg-002", "role": "foreground",
             "intent_profile": "cost-aware", "workload_class": "memory",
             "actual_submit_timestamp_utc": "2026-05-23T18:00:15Z"},
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # submission-log.csv
    with (run_dir / "submission-log.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "job_name", "role", "state_id", "applied", "completed", "failed",
            "skipped_reason", "wall_clock_seconds", "applied_at_utc", "job_path",
        ])
        w.writerow(["fg-001", "foreground", "idle", 1, 1, 0, "", "0.15",
                    "2026-05-23T18:00:00+00:00", "/dev/null"])
        w.writerow(["fg-002", "foreground", "idle", 1, 1, 0, "", "0.20",
                    "2026-05-23T18:00:15+00:00", "/dev/null"])

    # timeline-planned.csv
    with (run_dir / "timeline-planned.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["job_index", "job_name", "planned_submit_offset_seconds",
                    "workload_class", "intent_profile"])
        w.writerow([0, "fg-001", "0.000", "cpu", "latency-sensitive"])
        w.writerow([1, "fg-002", "15.000", "memory", "cost-aware"])

    return run_dir, registry_path


def _build_stub_db() -> StubDB:
    """Pretend the decision-maker recorded both jobs as COMPLETED foreground.

    Includes clusters_score + reason so the traceability metric exercises
    the join path.
    """
    db = StubDB()
    t0 = datetime(2026, 5, 23, 18, 0, 0, tzinfo=timezone.utc)
    db.set_rows("request_for_jobs", [
        {
            "id": "req-001", "job_name": "fg-001",
            "status": "COMPLETED", "kind": "foreground",
            "created_at": t0, "last_attempt_at": t0,
            "clusters_score": {"edge-1": 1.0, "public-cloud": 0.0},
            "reason": "heuristic: edge-1 (score=1.0)",
        },
        {
            "id": "req-002", "job_name": "fg-002",
            "status": "COMPLETED", "kind": "foreground",
            "created_at": t0, "last_attempt_at": t0,
            "clusters_score": {"edge-1": 0.5, "public-cloud": 0.5},
            "reason": "heuristic: tied, lex edge-1",
        },
    ])
    db.set_rows("decision_latency_foreground", [
        {"job_name": "fg-001", "created_at": t0, "last_attempt_at": t0,
         "latency_ms": 1.0},
        {"job_name": "fg-002", "created_at": t0, "last_attempt_at": t0,
         "latency_ms": 2.5},
    ])
    db.set_rows("no_pollution_invariant", [])
    db.set_rows("no_reverse_pollution_invariant", [])
    return db


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_runner_exits_zero_on_clean_fixture(tmp_path: Path):
    run_dir, registry = _build_fixture(tmp_path)
    db = _build_stub_db()
    rc = run_analysis(
        run_dir=run_dir,
        registry_path=registry,
        baseline="static-karmada",
        db=db,
        use_db=True,
        use_kube=False,
    )
    assert rc == 0
    analysis_dir = run_dir / "analysis"
    assert (analysis_dir / "summary.json").exists()
    assert (analysis_dir / "placement-success.csv").exists()
    assert (analysis_dir / "no-pollution.csv").exists()


def test_summary_has_baseline_projection(tmp_path: Path):
    run_dir, registry = _build_fixture(tmp_path)
    db = _build_stub_db()
    run_analysis(run_dir=run_dir, registry_path=registry,
                 baseline="static-karmada", db=db,
                 use_db=True, use_kube=False)
    summary = json.loads((run_dir / "analysis" / "summary.json").read_text())
    assert summary["baseline"] == "static-karmada"
    assert "static-karmada" in summary["baselines"]
    proj = summary["baselines"]["static-karmada"]
    assert "placement_success" in proj
    assert proj["placement_success"]["value"] == pytest.approx(1.0)
    assert proj["placement_success"]["n_applied"] == 2
    # decision_latency from the stub DB
    assert proj["decision_latency"]["median_ms"] is not None
    # intent_compliance with selected_cluster falling back to None →
    # kube enrichment skipped, so this metric is insufficient_data.
    assert proj["intent_compliance"]["status"] in ("insufficient_data", "ok")
    # admission/telemetry stay pending in iter-4a.
    assert proj["admission_overhead"]["status"] == "pending"
    assert proj["telemetry_overhead"]["status"] == "pending"


def test_idempotency_byte_identical_on_rerun(tmp_path: Path):
    """Run the analyzer twice; assert byte-identical analysis/ outputs."""
    run_dir, registry = _build_fixture(tmp_path)

    rc1 = run_analysis(run_dir=run_dir, registry_path=registry,
                       baseline="static-karmada", db=_build_stub_db(),
                       use_db=True, use_kube=False)
    assert rc1 == 0
    hashes_first = {
        p.name: _file_sha256(p)
        for p in sorted((run_dir / "analysis").iterdir())
        if p.is_file()
    }

    rc2 = run_analysis(run_dir=run_dir, registry_path=registry,
                       baseline="static-karmada", db=_build_stub_db(),
                       use_db=True, use_kube=False)
    assert rc2 == 0
    hashes_second = {
        p.name: _file_sha256(p)
        for p in sorted((run_dir / "analysis").iterdir())
        if p.is_file()
    }

    assert hashes_first == hashes_second, (
        "analysis/ outputs differ between runs — idempotency broken"
    )


def _write_variant_fixture(tmp_path: Path) -> Path:
    """An active registry variant that flips public-cloud.role
    (region-vocabulary-drift) relative to the fixture reference."""
    variant = {
        "clusters": {
            "edge-1": {
                "role": "edge", "arch": "arm64",
                "cost": "low", "rtt": "low",
                "cpu_class": "low", "mem_class": "low",
            },
            "public-cloud": {
                "role": "cloud-zone-a", "arch": "amd64",   # region drift
                "cost": "high", "rtt": "medium",
                "cpu_class": "high", "mem_class": "medium",
            },
        },
    }
    path = tmp_path / "r2-region-drift.yaml"
    path.write_text(yaml.safe_dump(variant), encoding="utf-8")
    return path


def test_registry_diff_in_summary_and_idempotent(tmp_path: Path):
    """iter-4h-3: --registry-variant populates
    baselines.<bl>.registry_diff with the perturbed (consumed) cell, the
    reference still scores compliance, and the analysis dir is
    byte-identical on rerun."""
    run_dir, registry = _build_fixture(tmp_path)
    variant = _write_variant_fixture(tmp_path)

    rc1 = run_analysis(run_dir=run_dir, registry_path=registry,
                       registry_variant_path=variant,
                       baseline="static-karmada", db=_build_stub_db(),
                       use_db=True, use_kube=False)
    assert rc1 == 0
    summary = json.loads((run_dir / "analysis" / "summary.json").read_text())
    rd = summary["baselines"]["static-karmada"]["registry_diff"]
    assert rd["variant_id"] == "r2"
    assert rd["perturbed_cells"] == [{
        "cluster": "public-cloud", "field": "role",
        "reference_value": "public-cloud", "active_value": "cloud-zone-a",
        "consumed_by_heuristic": True,
    }]
    hashes_first = {
        p.name: _file_sha256(p)
        for p in sorted((run_dir / "analysis").iterdir()) if p.is_file()
    }

    rc2 = run_analysis(run_dir=run_dir, registry_path=registry,
                       registry_variant_path=variant,
                       baseline="static-karmada", db=_build_stub_db(),
                       use_db=True, use_kube=False)
    assert rc2 == 0
    hashes_second = {
        p.name: _file_sha256(p)
        for p in sorted((run_dir / "analysis").iterdir()) if p.is_file()
    }
    assert hashes_first == hashes_second, "registry_diff broke idempotency"


def test_pollution_returns_exit_2(tmp_path: Path):
    run_dir, registry = _build_fixture(tmp_path)
    db = _build_stub_db()
    # Inject a reverse leak: claim fg-001 was persisted as foreground but
    # actually one of the recorded background submissions came back as
    # foreground. To simulate, set the reverse_pollution_invariant rows.
    db.set_rows("no_reverse_pollution_invariant", [
        {"id": "leaked", "job_name": "fg-001",
         "kind": "foreground", "status": "COMPLETED",
         "created_at": datetime(2026, 5, 23, 18, 0, 0, tzinfo=timezone.utc)},
    ])
    # We need a background submission name to trigger the reverse check.
    # Add one to the submission log by re-creating the fixture row.
    with (run_dir / "submission-log.csv").open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["bg-001", "background", "idle", 1, 1, 0, "", "0.15",
                    "2026-05-23T18:00:00+00:00", "/dev/null"])

    rc = run_analysis(run_dir=run_dir, registry_path=registry,
                      baseline="static-karmada", db=db,
                      use_db=True, use_kube=False)
    assert rc == 2
    # Pollution rows in the CSV
    np_csv = (run_dir / "analysis" / "no-pollution.csv").read_text(encoding="utf-8")
    assert "reverse" in np_csv
