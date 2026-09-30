"""Coverage and round-trip tests for `_short_codes.py` (iter-4h-2).

The module is a single source of truth for the short-code mapping used
to keep rendered Kubernetes Job names within the 63-char DNS-1123 limit
under multi-state expansion. These tests pin three invariants:

1. Every long form iter-4h-2 plans to render has a short code.
2. The long->short and short->long mappings are mutual inverses.
3. The renderer-facing helpers fail fast on a missing long form (so a
   future iter that forgets to extend the mapping cannot silently
   truncate a name).

If iter-4h-3 / iter-4h-4 / iter-4d.3 introduce new baselines or specs,
the coverage list below is extended in the same PR.
"""
from __future__ import annotations

import pytest

from experiments.orchestrator import _short_codes


# ---------------------------------------------------------------------------
# iter-4h-2 required coverage
# ---------------------------------------------------------------------------

# These are the long forms the iter-4h-2 plan references in:
# - the campaign matrix (§7.2: three baselines, five state_ids)
# - the foreground spec (§5: effectiveness-taskbound)
# - the workload classes the renderer can emit (job-{cpu,memory}-background)
_REQUIRED_BASELINES = ("delphi-full", "heuristic-scorer", "static-karmada")
_REQUIRED_SPEC_IDS = ("effectiveness-taskbound", "effectiveness")
_REQUIRED_STATE_IDS = (
    "idle",
    "mixed-disturbance-light",
    "mixed-disturbance-heavy",
    "edge-disturbance",
    "public-cloud-disturbance",
)
_REQUIRED_WORKLOAD_CLASSES = ("cpu", "memory", "network")


def test_every_required_long_form_has_a_short_code() -> None:
    """The renderer fails fast on a missing lookup. This test catches
    that failure at unit-test time so it cannot surface mid-run."""
    for b in _REQUIRED_BASELINES:
        assert _short_codes.baseline_short(b), f"baseline {b} → empty short"
    for s in _REQUIRED_SPEC_IDS:
        assert _short_codes.spec_id_short(s), f"spec_id {s} → empty short"
    for st in _REQUIRED_STATE_IDS:
        assert _short_codes.state_id_short(st), f"state_id {st} → empty short"
    for w in _REQUIRED_WORKLOAD_CLASSES:
        assert _short_codes.workload_class_short(w), f"wc {w} → empty short"


def test_unknown_long_form_raises_unknown_long_form() -> None:
    """A typo in a baseline / spec / state / workload-class name must
    surface as a clear failure with the right category in the message —
    not as a silent missing-key crash deep inside Jinja."""
    with pytest.raises(_short_codes.UnknownLongForm, match="baseline"):
        _short_codes.baseline_short("typo-baseline")
    with pytest.raises(_short_codes.UnknownLongForm, match="spec_id"):
        _short_codes.spec_id_short("typo-spec")
    with pytest.raises(_short_codes.UnknownLongForm, match="state_id"):
        _short_codes.state_id_short("typo-state")
    with pytest.raises(_short_codes.UnknownLongForm, match="workload_class"):
        _short_codes.workload_class_short("typo-wc")


# ---------------------------------------------------------------------------
# Round-trip and shape
# ---------------------------------------------------------------------------

def test_long_to_short_to_long_is_identity() -> None:
    """For every long form in the merged dict, round-tripping through
    SHORT_TO_LONG returns the same string. Catches a future addition
    that accidentally collides two long forms onto the same short
    code."""
    for long_form, short_form in _short_codes.LONG_TO_SHORT.items():
        round_tripped = _short_codes.SHORT_TO_LONG[short_form]
        assert round_tripped == long_form, (
            f"round-trip fails for {long_form!r}: short={short_form!r} → "
            f"long={round_tripped!r}"
        )


def test_short_to_long_unknown_short_raises() -> None:
    with pytest.raises(_short_codes.UnknownLongForm, match="not recognised"):
        _short_codes.short_to_long("zzz")


def test_short_codes_are_dns1123_label_safe() -> None:
    """DNS-1123 label rule: lowercase alphanumeric or '-'; must start and
    end with alphanumeric; max 63 chars. The short codes feed straight
    into Kubernetes Job names, so any unsafe character would break
    server-side validation."""
    import re
    pat = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    for short_form in _short_codes.LONG_TO_SHORT.values():
        assert pat.match(short_form), (
            f"short code {short_form!r} is not DNS-1123-label-safe"
        )
        assert len(short_form) <= 8, (
            f"short code {short_form!r} is unusually long; keep them ≤8 "
            f"to leave room for the rest of the name"
        )


def test_no_short_code_collisions_at_import_time() -> None:
    """The module's _invert helper raises on duplicate short codes at
    import time. This test is mostly a documentation hook — if it ever
    fails, the import already would have failed earlier — but it keeps
    the contract visible to a reviewer adding a new entry."""
    seen: set[str] = set()
    for short_form in _short_codes.LONG_TO_SHORT.values():
        assert short_form not in seen, (
            f"duplicate short code {short_form!r}; extend the mapping with "
            f"a distinct code"
        )
        seen.add(short_form)
