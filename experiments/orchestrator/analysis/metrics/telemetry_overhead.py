"""Telemetry overhead: paired sidecar-injected vs no-sidecar comparison.

Same pending-paired-run shape as admission_overhead: the paired-run
protocol is not implemented.
"""
from __future__ import annotations

from ..models import AnalysisContext, MetricResult, RunArtifacts


def compute(art: RunArtifacts, _ctx: AnalysisContext) -> MetricResult:
    return MetricResult(
        name="telemetry_overhead",
        rows=(),
        aggregate={
            "median_ms": None,
            "p95_ms": None,
            "n": 0,
            "status": "pending paired-run protocol",
        },
        status="pending",
        note=(
            "Telemetry overhead requires paired sidecar-injected vs "
            "non-injected submissions. The paired-run protocol is not "
            "implemented; no overhead value is reported."
        ),
    )
