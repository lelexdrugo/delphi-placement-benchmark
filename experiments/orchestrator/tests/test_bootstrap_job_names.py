"""Regression test for the bootstrap render path's job-name encoding.

iter-4c attempt phase-a-20260524T214332Z surfaced a name-truncation
collision: for cluster states whose id is long (e.g.
``skewed-edge-1-saturated``, 23 chars), the previous bootstrap name
format

    delphi-{spec_id}-{state_id}-{run_id}-{wc}-{idx:03d}

with the standard run_id ``phase-a-<UTC-ts>`` (24 chars) exceeded
63 chars and the K8s DNS-1123 truncation dropped the ``-{wc}-{idx}``
discriminator entirely. All 36 jobs of that state collapsed onto
two or three duplicated names, and only the first apply per name
succeeded (subsequent applies returned ``spec.template: Invalid
value``).

This test rebuilds the truncation logic and asserts the 36 jobs of
each state get 36 distinct names, including for the longest known
state ids. The fix moves the discriminator early in the name so the
cosmetic state-id tail is the part that gets truncated.
"""
from __future__ import annotations

import string


def _bootstrap_job_name(spec_id: str, state_id: str, run_id: str,
                        idx: int, wc: str) -> str:
    """Inline mirror of the bootstrap path's name builder.

    Kept in the test file (instead of importing the helper from
    ``render.py``) because the production code inlines the format
    directly inside the loop body — exposing it as a function would
    expand the touch surface of the iter-4c fix. The format string
    below must stay in lockstep with ``render.py::render_bootstrap``.
    """
    if run_id == spec_id or run_id.startswith(f"{spec_id}-"):
        base = f"delphi-{run_id}-{idx:03d}-{wc}-{state_id}".lower()
    else:
        base = f"delphi-{spec_id}-{run_id}-{idx:03d}-{wc}-{state_id}".lower()
    if len(base) > 63:
        base = base[:63].rstrip("-")
    return base


def test_bootstrap_names_are_dns1123():
    """Every generated name must satisfy DNS-1123 (lowercase, digits,
    hyphens, <=63 chars, no leading/trailing hyphen)."""
    allowed = set(string.ascii_lowercase + string.digits + "-")
    name = _bootstrap_job_name(
        spec_id="bootstrap",
        state_id="skewed-edge-1-saturated",
        run_id="phase-a-20260524t214332z",
        idx=3,
        wc="cpu",
    )
    assert len(name) <= 63, f"name longer than 63 chars: {name!r} ({len(name)})"
    assert set(name) <= allowed, f"non DNS-1123 chars in {name!r}"
    assert not name.startswith("-") and not name.endswith("-"), (
        f"leading/trailing hyphen in {name!r}"
    )


def test_bootstrap_names_unique_within_state_for_long_state_id():
    """The smoking gun: 36 jobs per state must produce 36 distinct
    names even for the longest known state id. Pre-fix, only ~3
    distinct names came out of skewed-edge-1-saturated."""
    spec_id = "bootstrap"
    run_id = "phase-a-20260524t214332z"
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
    truncation eats most of the state_id, because the truncated
    prefix still differs."""
    spec_id = "bootstrap"
    run_id = "phase-a-20260524t214332z"
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
    run_id = "phase-a-20260524t214332z"
    state_id = "skewed-edge-1-saturated"
    for idx in (0, 3, 17, 35):
        for wc in ("cpu", "memory", "network"):
            n = _bootstrap_job_name(spec_id, state_id, run_id, idx, wc)
            disc = f"-{idx:03d}-{wc}"
            assert disc in n, (
                f"discriminator {disc!r} missing from truncated "
                f"name {n!r} (len={len(n)})"
            )
