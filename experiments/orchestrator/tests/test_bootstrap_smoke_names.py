"""Regression suite for the smoke-bootstrap name budget.

Companion to `test_bootstrap_job_names.py`. The smoke variant uses
`spec_id: bsmoke` and a recommended `smoke-<UTC-ts>` run_id pattern;
this test pins that combination against the DNS-1123 truncation logic
in ``render.py::render_bootstrap`` so a future change to either knob
cannot silently re-introduce the cross-state collision the smoke is
designed to detect.

If a future PR lengthens `spec_id` or changes the run_id pattern,
this suite breaks loudly before the spec ships.
"""
from __future__ import annotations

import string


def _bootstrap_job_name(spec_id: str, state_id: str, run_id: str,
                        idx: int, wc: str) -> str:
    # Mirrors the bootstrap path in ``render.py``. Kept inline (rather
    # than imported) so the test pins the contract by value, not by
    # implementation.
    if run_id == spec_id or run_id.startswith(f"{spec_id}-"):
        base = f"delphi-{run_id}-{idx:03d}-{wc}-{state_id}".lower()
    else:
        base = f"delphi-{spec_id}-{run_id}-{idx:03d}-{wc}-{state_id}".lower()
    if len(base) > 63:
        base = base[:63].rstrip("-")
    return base


SMOKE_SPEC_ID = "bsmoke"
SMOKE_RUN_ID_PATTERN = "smoke-20260524t233000z"  # ~21 chars; the
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
    distinct names. This is the smoking-gun assertion for the
    spec_id+run_id budget."""
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
                    f"wc={wc} -> {n!r} already produced by {prev}. "
                    f"Spec_id or run_id has grown past the safe budget; "
                    f"shorten before merging."
                )
            seen[n] = (state_id, idx, wc)


def test_long_runid_pattern_does_collide():
    """Reverse guard: verify that an operator-mistake run_id (the
    naive `phase-a-smoke-<UTC-ts>` pattern) DOES collide under the
    `bsmoke` spec_id, so the README warning is load-bearing. If this
    test starts failing because names no longer collide, the render
    code grew a hash/abbreviation that we should propagate to the
    README so the pattern restriction can be relaxed."""
    bad_runid = "phase-a-smoke-20260524t233000z"  # ~30 chars
    seen: dict[str, tuple[str, int, str]] = {}
    collisions = 0
    for state_id in STATES:
        for idx in range(12):
            wc = WORKLOAD_CLASSES[idx % 3]
            n = _bootstrap_job_name(
                SMOKE_SPEC_ID, state_id, bad_runid, idx, wc,
            )
            if n in seen and seen[n][0] != state_id:
                collisions += 1
            seen[n] = (state_id, idx, wc)
    assert collisions > 0, (
        "the 'phase-a-smoke-<ts>' run_id pattern was expected to "
        "collide with spec_id='bsmoke' but did not. The render code "
        "may have grown a hash/abbreviation — update the smoke spec "
        "header and README to remove the run_id restriction."
    )
