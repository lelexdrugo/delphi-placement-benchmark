"""Admission overhead: paired optimize-propagation=true vs false comparison.

iter-4a does not produce paired runs, so this returns
``status='pending paired-run protocol'`` with an empty rows list. The
interface is wired so iter-4d.1 can replace it without touching the
runner or writer.
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
            "false submissions. Paired-run wiring lands in iter-4d.1; the "
            "analyzer reads manifest.json.notes for a 'paired_admission_pair: "
            "<run-id>' marker to enable it."
        ),
    )
