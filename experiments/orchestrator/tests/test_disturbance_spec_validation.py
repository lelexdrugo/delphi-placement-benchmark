"""Spec-validation tests for iter-4h-2 disturbance-stream cluster-state YAMLs.

The renderer rejects malformed specs at load time rather than producing
partial trees. These tests pin the rejection contract:

- single-pin and disturbance-stream are mutually exclusive,
- target_clusters must be non-empty,
- workload_classes restricted to {cpu, memory} (Q1=A),
- spawn.pattern must be one of {poisson, uniform, burst},
- poisson / uniform require mean_inter_arrival_seconds,
- burst requires burst_size + rest_seconds,
- spawn.max_concurrent_disturbance_jobs is required,
- per_job.duration_seconds is required,
- per_target_overrides keys must be a subset of target_clusters.
"""
from __future__ import annotations

import pytest

from experiments.orchestrator.cluster_state import (
    validate_state_spec,
    _merge_target_overrides,
)


_BASE_DISTURBANCE = {
    "intensity_label": "heavy",
    "target_clusters": ["on-prem", "edge-1"],
    "workload_classes": ["cpu", "memory"],
    "spawn": {
        "pattern": "poisson",
        "mean_inter_arrival_seconds": 30.0,
        "jitter_seconds": 4.0,
        "max_concurrent_disturbance_jobs": 5,
    },
    "per_job": {
        "duration_seconds": 90,
        "cpu_load": "1",
        "memory_load": "512M",
    },
}


def _spec(**override):
    """Build a fresh full disturbance spec for a test."""
    import copy
    d = copy.deepcopy(_BASE_DISTURBANCE)
    return {
        "state_id": "test-state",
        "disturbance": {**d, **override} if override else d,
    }


def test_valid_disturbance_spec_passes() -> None:
    validate_state_spec(_spec(), source="test")


def test_mutual_exclusion_with_background_jobs() -> None:
    spec = _spec()
    spec["background_jobs"] = [{"template": "x", "pin_to": "y",
                                "parameters": {"DURATION": "60s"}}]
    with pytest.raises(ValueError, match="mutually exclusive"):
        validate_state_spec(spec, source="test")


def test_empty_target_clusters_rejected() -> None:
    spec = _spec()
    spec["disturbance"]["target_clusters"] = []
    with pytest.raises(ValueError, match="target_clusters is empty"):
        validate_state_spec(spec, source="test")


def test_network_workload_class_rejected_in_q1a() -> None:
    spec = _spec()
    spec["disturbance"]["workload_classes"] = ["network"]
    with pytest.raises(ValueError, match="network"):
        validate_state_spec(spec, source="test")


def test_unknown_spawn_pattern_rejected() -> None:
    spec = _spec()
    spec["disturbance"]["spawn"]["pattern"] = "exponential-snake"
    with pytest.raises(ValueError, match="pattern"):
        validate_state_spec(spec, source="test")


def test_poisson_requires_mean() -> None:
    spec = _spec()
    del spec["disturbance"]["spawn"]["mean_inter_arrival_seconds"]
    with pytest.raises(ValueError, match="mean_inter_arrival_seconds"):
        validate_state_spec(spec, source="test")


def test_burst_requires_burst_size_and_rest() -> None:
    spec = _spec()
    spec["disturbance"]["spawn"]["pattern"] = "burst"
    spec["disturbance"]["spawn"]["mean_inter_arrival_seconds"] = 0
    # burst_size and rest_seconds missing
    with pytest.raises(ValueError, match="burst_size|rest_seconds"):
        validate_state_spec(spec, source="test")


def test_max_concurrent_required() -> None:
    spec = _spec()
    del spec["disturbance"]["spawn"]["max_concurrent_disturbance_jobs"]
    with pytest.raises(ValueError, match="max_concurrent_disturbance_jobs"):
        validate_state_spec(spec, source="test")


def test_duration_required() -> None:
    spec = _spec()
    del spec["disturbance"]["per_job"]["duration_seconds"]
    with pytest.raises(ValueError, match="duration_seconds"):
        validate_state_spec(spec, source="test")


def test_unknown_per_target_override_target_rejected() -> None:
    spec = _spec()
    spec["disturbance"]["per_target_overrides"] = {
        "on-prem": {"cpu_load": "2"},
        "ghost-cluster": {"cpu_load": "2"},  # typo: not in target_clusters
    }
    with pytest.raises(ValueError, match="ghost-cluster|not in target_clusters"):
        validate_state_spec(spec, source="test")


def test_idle_state_with_no_keys_is_ok() -> None:
    """An idle spec carries neither background_jobs nor disturbance.
    The validator must accept that as a no-op (the legacy idle.yaml
    case)."""
    validate_state_spec({"state_id": "idle"}, source="test")


# ---------------------------------------------------------------------------
# Per-target override merging
# ---------------------------------------------------------------------------

def test_overrides_win_over_scalar_defaults() -> None:
    d = _BASE_DISTURBANCE.copy()
    d["per_target_overrides"] = {
        "on-prem": {
            "mean_inter_arrival_seconds": 22.0,
            "max_concurrent_disturbance_jobs": 6,
            "cpu_load": "1",
            "memory_load": "2G",
        }
    }
    merged = _merge_target_overrides(d, "on-prem")
    # Per-target wins.
    assert merged["mean_inter_arrival_seconds"] == 22.0
    assert merged["max_concurrent_disturbance_jobs"] == 6
    assert merged["memory_load"] == "2G"
    # Defaults survive when no per-target override exists.
    assert merged["duration_seconds"] == 90
    assert merged["jitter_seconds"] == 4.0


def test_no_overrides_uses_pure_defaults() -> None:
    d = _BASE_DISTURBANCE.copy()
    merged = _merge_target_overrides(d, "edge-1")
    assert merged["mean_inter_arrival_seconds"] == 30.0
    assert merged["duration_seconds"] == 90
    assert merged["memory_load"] == "512M"
