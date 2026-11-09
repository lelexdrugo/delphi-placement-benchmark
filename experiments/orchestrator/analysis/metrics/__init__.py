"""One module per metric. Each exports compute(art, ctx) -> MetricResult."""
from __future__ import annotations

from .admission_overhead import compute as compute_admission_overhead
from .completion_decomposition import compute as compute_completion_decomposition
from .completion_time import compute as compute_completion_time
from .cordon_response import compute as compute_cordon_response
from .cost_proxy import compute as compute_cost_proxy
from .data_movement import compute as compute_data_movement
from .decision_latency import compute as compute_decision_latency
from .intent_compliance import compute as compute_intent_compliance
from .placement_success import compute as compute_placement_success
from .telemetry_overhead import compute as compute_telemetry_overhead
from .traceability import compute as compute_traceability

__all__ = [
    "compute_admission_overhead",
    "compute_completion_decomposition",
    "compute_completion_time",
    "compute_cordon_response",
    "compute_cost_proxy",
    "compute_data_movement",
    "compute_decision_latency",
    "compute_intent_compliance",
    "compute_placement_success",
    "compute_telemetry_overhead",
    "compute_traceability",
]
