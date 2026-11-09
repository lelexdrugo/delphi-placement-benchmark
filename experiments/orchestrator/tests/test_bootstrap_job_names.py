"""Regression test for the bootstrap render path's job-name encoding.

For cluster states whose id is long (e.g. ``skewed-edge-1-saturated``,
23 chars), a name format that puts the state id before the per-job
discriminator,

    delphi-{spec_id}-{state_id}-{run_id}-{wc}-{idx:03d}

with the standard run_id ``phase-a-<UTC-ts>`` (24 chars) exceeds
63 chars, and the K8s DNS-1123 truncation drops the ``-{wc}-{idx}``
discriminator entirely: all 36 jobs of that state collapse onto
two or three duplicated names, and only the first apply per name
succeeds (subsequent applies return ``spec.template: Invalid value``).

These tests call the production name builder
(``render._bootstrap_job_name``) and assert the 36 jobs of each state
get 36 distinct names, including for the longest known state ids. The
builder puts the discriminator early in the name so the cosmetic
state-id tail is the part that gets truncated.
"""
from __future__ import annotations

import string

from experiments.orchestrator.render import _bootstrap_job_name

# Synthetic run id with the length of the standard `phase-a-<UTC-ts>`.
_RUN_ID = "phase-a-20260101t000000z"


def test_bootstrap_names_are_dns1123():
    """Every generated name must satisfy DNS-1123 (lowercase, digits,
    hyphens, <=63 chars, no leading/trailing hyphen)."""
    allowed = set(string.ascii_lowercase + string.digits + "-")
    name = _bootstrap_job_name(
        spec_id="bootstrap",
        state_id="skewed-edge-1-saturated",
        run_id=_RUN_ID,
        idx=3,
        wc="cpu",
    )
    assert len(name) <= 63, f"name longer than 63 chars: {name!r} ({len(name)})"
    assert set(name) <= allowed, f"non DNS-1123 chars in {name!r}"
    assert not name.startswith("-") and not name.endswith("-"), (
        f"leading/trailing hyphen in {name!r}"
    )


def test_bootstrap_names_unique_within_state_for_long_state_id():
    """36 jobs per state must produce 36 distinct names even for the
    longest known state id."""
    spec_id = "bootstrap"
    run_id = _RUN_ID
    workload_classes = ["cpu", "memory", "network"]
    intent_profiles = ["latency-sensitive", "cost-aware",
                       "locality-aware", "balanced"]
    k = 3  # coverage criterion
    # The coverage criterion expands the (wc x intent) Cartesian
    # k times, so we emit 36 jobs per state.
    pairs = [(wc, ip)
             for wc in workload_classes
             for ip in intent_profiles
             for _ in range(k)]
    assert len(pairs) == 36

    for state_id in ("idle", "mixed-load",
                     "skewed-edge-1-saturated",
                     "skewed-on-prem-saturated"):
        names = [
            _bootstrap_job_name(spec_id, state_id, run_id, idx, wc)
            for idx, (wc, _) in enumerate(pairs)
        ]
        assert len(set(names)) == 36, (
            f"name collision under state_id={state_id!r}: "
            f"{len(set(names))} unique of {len(names)} jobs. "
            f"First few: {names[:5]}"
        )


def test_bootstrap_names_unique_across_states():
    """Cross-state uniqueness: two states must not collide even if
    truncation eats most of the state_id."""
    spec_id = "bootstrap"
    run_id = _RUN_ID
    states = ("idle", "mixed-load",
              "skewed-edge-1-saturated",
              "skewed-on-prem-saturated")
    seen: dict[str, tuple[str, int, str]] = {}
    for state_id in states:
        for idx in range(36):
            wc = ["cpu", "memory", "network"][idx % 3]
            n = _bootstrap_job_name(spec_id, state_id, run_id, idx, wc)
            if n in seen:
                prev = seen[n]
                raise AssertionError(
                    f"cross-state collision: state={state_id} idx={idx} "
                    f"wc={wc} -> {n!r} already produced by {prev}"
                )
            seen[n] = (state_id, idx, wc)


def test_bootstrap_names_preserve_discriminator_under_truncation():
    """Even when the full name exceeds 63 chars and gets truncated,
    every emitted name must still carry the ``-{idx:03d}-{wc}``
    discriminator so two distinct (idx, wc) tuples never collapse."""
    spec_id = "bootstrap"
    run_id = _RUN_ID
    state_id = "skewed-edge-1-saturated"
    for idx in (0, 3, 17, 35):
        for wc in ("cpu", "memory", "network"):
            n = _bootstrap_job_name(spec_id, state_id, run_id, idx, wc)
            disc = f"-{idx:03d}-{wc}"
            assert disc in n, (
                f"discriminator {disc!r} missing from truncated "
                f"name {n!r} (len={len(n)})"
            )
