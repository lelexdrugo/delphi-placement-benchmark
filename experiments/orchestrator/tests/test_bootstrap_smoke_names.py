"""Regression suite for the smoke-bootstrap name budget.

Companion to `test_bootstrap_job_names.py`. The smoke variant uses
`spec_id: bsmoke` and a recommended `smoke-<UTC-ts>` run_id pattern;
this suite pins that combination against the production name builder
(``render._bootstrap_job_name``, used by ``render_bootstrap``): names
stay DNS-1123-valid and unique within and across states. Names longer
than 63 chars are cut and suffixed with a hash of the full name, so a
longer run_id pattern keeps them unique too (last test).
"""
from __future__ import annotations

import re
import string

from experiments.orchestrator.render import _bootstrap_job_name

SMOKE_SPEC_ID = "bsmoke"
SMOKE_RUN_ID_PATTERN = "smoke-20260101t000000z"  # ~21 chars; the
                                                  # canonical short
                                                  # smoke run_id
STATES = (
    "idle",
    "mixed-load",
    "skewed-edge-1-saturated",
    "skewed-on-prem-saturated",
)
WORKLOAD_CLASSES = ("cpu", "memory", "network")
INTENT_PROFILES = (
    "latency-sensitive", "cost-aware", "locality-aware", "balanced",
)
K = 1  # smoke k=1


def _coverage_pairs():
    """Mirror the orchestrator's k=1 expansion of the smoke coverage
    grid: 3 wc × 4 intent × k=1 = 12 jobs per state."""
    return [
        (wc, ip)
        for wc in WORKLOAD_CLASSES
        for ip in INTENT_PROFILES
        for _ in range(K)
    ]


def test_smoke_names_are_dns1123():
    allowed = set(string.ascii_lowercase + string.digits + "-")
    name = _bootstrap_job_name(
        spec_id=SMOKE_SPEC_ID,
        state_id="skewed-on-prem-saturated",  # the longest state_id
        run_id=SMOKE_RUN_ID_PATTERN,
        idx=11,
        wc="network",
    )
    assert len(name) <= 63, f"name too long: {name!r} ({len(name)})"
    assert set(name) <= allowed, f"non-DNS-1123 chars: {name!r}"
    assert not name.startswith("-") and not name.endswith("-")


def test_smoke_names_unique_within_state():
    """k=1 with 3*4 = 12 jobs per state must produce 12 distinct
    names even for the longest state_id."""
    pairs = _coverage_pairs()
    assert len(pairs) == 12

    for state_id in STATES:
        names = [
            _bootstrap_job_name(
                SMOKE_SPEC_ID, state_id, SMOKE_RUN_ID_PATTERN,
                idx, wc,
            )
            for idx, (wc, _) in enumerate(pairs)
        ]
        assert len(set(names)) == 12, (
            f"intra-state collision under state_id={state_id!r}: "
            f"{len(set(names))} unique of {len(names)}. "
            f"First few: {names[:5]}"
        )


def test_smoke_names_unique_across_states():
    """48 total smoke jobs (12 per state × 4 states) must yield 48
    distinct names."""
    seen: dict[str, tuple[str, int, str]] = {}
    for state_id in STATES:
        for idx in range(12):
            wc = WORKLOAD_CLASSES[idx % 3]
            n = _bootstrap_job_name(
                SMOKE_SPEC_ID, state_id, SMOKE_RUN_ID_PATTERN, idx, wc,
            )
            if n in seen:
                prev = seen[n]
                raise AssertionError(
                    f"cross-state collision: state={state_id} idx={idx} "
                    f"wc={wc} -> {n!r} already produced by {prev}."
                )
            seen[n] = (state_id, idx, wc)


def test_long_runid_pattern_stays_unique_via_hash_suffix():
    """A longer run_id (the `phase-a-smoke-<UTC-ts>` pattern, ~30
    chars) pushes most names past 63 chars, where plain truncation
    would cut the state_id and collide across states. The hash suffix
    keeps all 48 names distinct; the length check proves the test
    reaches the truncation path."""
    long_runid = "phase-a-smoke-20260101t000000z"  # ~30 chars
    hashed = re.compile(r"-[0-9a-f]{6}$")
    seen: dict[str, tuple[str, int, str]] = {}
    n_truncated = 0
    for state_id in STATES:
        for idx in range(12):
            wc = WORKLOAD_CLASSES[idx % 3]
            n = _bootstrap_job_name(
                SMOKE_SPEC_ID, state_id, long_runid, idx, wc,
            )
            assert len(n) <= 63, f"name too long: {n!r} ({len(n)})"
            assert n not in seen, (
                f"collision: state={state_id} idx={idx} wc={wc} -> "
                f"{n!r} already produced by {seen[n]}"
            )
            seen[n] = (state_id, idx, wc)
            if len(n) == 63 and hashed.search(n):
                n_truncated += 1
    assert n_truncated > 0, "no name reached the truncation path"
