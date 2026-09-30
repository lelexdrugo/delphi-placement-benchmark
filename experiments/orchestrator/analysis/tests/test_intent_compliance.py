"""Table-driven test for the two-tier intent compliance formula.

This is the canonical pin for the metric's semantics. If anyone wants
to change the satisfaction map (ordinal direction, region matching,
preferred-class counting), they must update this table — which is the
point.
"""
from __future__ import annotations

import pytest

from experiments.orchestrator.analysis.metrics.intent_compliance import compute_per_job
from experiments.orchestrator.analysis.models import ClusterLabels, IntentProfile


def _cluster(name: str, role: str, rtt: str, cost: str) -> ClusterLabels:
    return ClusterLabels(
        name=name, role=role, arch="amd64",
        cost=cost, rtt=rtt, cpu_class="medium", mem_class="medium",
    )


def _profile(name: str, region: str, latency: str, cost: str,
             preferred: tuple[str, ...]) -> IntentProfile:
    return IntentProfile(
        name=name,
        propagation_requirements={
            "region": region, "latency": latency,
            "cost": cost, "weight": 1.0,
        },
        preferred_classes=preferred,
    )


# Clusters from the lab.
_EDGE_1 = _cluster("edge-1", role="edge", rtt="low", cost="low")
_EDGE_2 = _cluster("edge-2", role="edge", rtt="low", cost="low")
_ON_PREM = _cluster("on-prem", role="on-premises", rtt="low", cost="low")
_PUBLIC = _cluster("public-cloud", role="public-cloud", rtt="medium", cost="high")


# Intent profiles from cluster-registry.yaml.
_LATENCY_SENSITIVE = _profile(
    "latency-sensitive", region="edge", latency="low", cost="any",
    preferred=("edge",),
)
_COST_AWARE = _profile(
    "cost-aware", region="any", latency="any", cost="low",
    preferred=("on-premises", "edge"),
)
_LOCALITY_AWARE = _profile(
    "locality-aware", region="public-cloud", latency="any", cost="any",
    preferred=("public-cloud",),
)
_BALANCED = _profile(
    "balanced", region="any", latency="medium", cost="medium",
    preferred=("on-premises", "public-cloud", "edge"),
)


@pytest.mark.parametrize(
    "profile,cluster,expected_hard,expected_soft_num,expected_soft_den",
    [
        # latency-sensitive (region=edge, latency=low):
        # edge-1 satisfies all hard; soft 1/1 since preferred=[edge].
        (_LATENCY_SENSITIVE, _EDGE_1, 1, 1, 1),
        # latency-sensitive on public-cloud: region=edge required, public-cloud violates.
        (_LATENCY_SENSITIVE, _PUBLIC, 0, 0, 1),
        # latency-sensitive on on-prem: region=edge violated → hard=0.
        # soft 0/1 because preferred=[edge].
        (_LATENCY_SENSITIVE, _ON_PREM, 0, 0, 1),
        # cost-aware (region=any, cost=low): on-prem has cost=low → hard=1.
        # soft 1/2 since preferred=[on-premises, edge] and cluster.role=on-premises.
        (_COST_AWARE, _ON_PREM, 1, 1, 2),
        # cost-aware on edge-2: cost=low satisfied; soft 1/2 (edge in preferred).
        (_COST_AWARE, _EDGE_2, 1, 1, 2),
        # cost-aware on public-cloud: cost=high > low ⇒ hard=0.
        (_COST_AWARE, _PUBLIC, 0, 0, 2),
        # locality-aware (region=public-cloud): public-cloud cluster satisfies.
        # soft 1/1.
        (_LOCALITY_AWARE, _PUBLIC, 1, 1, 1),
        # locality-aware on edge-1: region mismatch → hard=0.
        (_LOCALITY_AWARE, _EDGE_1, 0, 0, 1),
        # balanced (region=any, latency=medium, cost=medium):
        # public-cloud has cost=high ⇒ hard=0; soft 1/3 since
        # preferred=[on-premises, public-cloud, edge] matches public-cloud.
        (_BALANCED, _PUBLIC, 0, 1, 3),
        # balanced on edge-1: latency=low ≤ medium, cost=low ≤ medium → hard=1.
        # soft 1/3 (edge matches one of three preferred classes).
        (_BALANCED, _EDGE_1, 1, 1, 3),
        # balanced on on-prem: same hard pass; soft 1/3.
        (_BALANCED, _ON_PREM, 1, 1, 3),
    ],
)
def test_compute_per_job(profile, cluster, expected_hard,
                         expected_soft_num, expected_soft_den):
    hard, soft = compute_per_job(profile, cluster)
    assert hard == expected_hard, f"hard pass for {profile.name}/{cluster.name}"
    assert soft == pytest.approx(expected_soft_num / expected_soft_den), \
        f"soft score for {profile.name}/{cluster.name}"
