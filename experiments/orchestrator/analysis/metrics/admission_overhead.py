"""Admission overhead: paired optimize-propagation=true vs false comparison.

The paired-run protocol is not implemented, so this returns
``status='pending paired-run protocol'`` with an empty rows list. The
interface matches the other metrics, so an implementation can replace it
without touching the runner or writer.
"""
from __future__ import annotations

from ..models import AnalysisContext, MetricResult, RunArtifacts


def compute(art: RunArtifacts, _ctx: AnalysisContext) -> MetricResult:
    return MetricResult(
        name="admission_overhead",
        rows=(),
        aggregate={
            "median_ms": None,
            "p95_ms": None,
            "n": 0,
            "status": "pending paired-run protocol",
        },
        status="pending",
        note=(
            "Admission overhead requires paired optimize-propagation=true vs "
            "false submissions. The paired-run protocol is not implemented; "
            "no overhead value is reported."
        ),
    )
