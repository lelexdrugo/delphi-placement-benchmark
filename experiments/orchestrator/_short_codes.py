"""Short-code mapping for Kubernetes Job names (iter-4h-2).

Kubernetes Job names cap at 63 characters DNS-1123. Combined with
iter-4c.2's immutable per-experiment image tag (~30 chars in run_id)
and the iter-4h-2 multi-state expansion (state_id in the suffix per the
iter-4c long-name fix), the long-form rendered name
`delphi-<spec_id>-<baseline>-<state_id>-<run_id>-<idx>-<wc>` overflows
the 63-byte budget. iter-4c.1 added a truncation that drops the tail,
which is exactly where `state_id` lives — the analyzer needs that
field to attribute a Job to a cell.

The fix is a short-code mapping applied at *name* construction time
only. Long forms continue to appear in labels and annotations so
analyzer queries and human `kubectl get -L ...` listings stay readable.

Single source of truth — adding a new baseline / spec / state requires
extending this module *and* the corresponding test
(`test_short_codes_complete.py`). The renderer fails fast on a missing
lookup so a future iter that forgets to add a code cannot silently
truncate a name.

Coverage as of iter-4h-2:
- baselines:        df / hs / sk
- spec_ids:         etb / eff
- state_ids:        idle / mdl / mdh / ed / pcd
- workload_classes: c / m / n
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

# Baselines. The long form is what `experiments/runtime/set-baseline.ps1`
# writes into the run-id; the short form is what the renderer puts into
# the Kubernetes Job name. Mutually-exclusive baselines per the
# decision-maker's startup contract (DETERMINISTIC_TARGET_CLUSTER vs
# HEURISTIC_SCORER_ENABLED).
_BASELINES: dict[str, str] = {
    "delphi-full": "df",
    "heuristic-scorer": "hs",
    "static-karmada": "sk",
}

# Spec IDs. `effectiveness-taskbound` is the iter-4h-1 task-bound
# foreground spec (consumed by iter-4h-2/-3/-4). `effectiveness` is the
# legacy time-bound spec preserved for iter-4d.1 paired admission-
# overhead runs.
_SPEC_IDS: dict[str, str] = {
    "effectiveness-taskbound": "etb",
    "effectiveness": "eff",
}

# Cluster-state IDs. `idle` keeps its 4-char form (no shortening needed).
# The four iter-4h-2 disturbance states get explicit short codes so
# rendered names stay inside the 63-char DNS-1123 budget.
_STATE_IDS: dict[str, str] = {
    "idle": "idle",
    "mixed-disturbance-light": "mdl",
    "mixed-disturbance-heavy": "mdh",
    "edge-disturbance": "ed",
    "public-cloud-disturbance": "pcd",
}

# Workload classes. The existing renderer already uses single-character
# discriminators internally for some name fragments; this mapping
# centralises them so a future iter cannot diverge.
_WORKLOAD_CLASSES: dict[str, str] = {
    "cpu": "c",
    "memory": "m",
    "network": "n",
}


# Merged long-to-short for the round-trip test and for generic lookups.
LONG_TO_SHORT: dict[str, str] = {
    **_BASELINES,
    **_SPEC_IDS,
    **_STATE_IDS,
    **_WORKLOAD_CLASSES,
}


def _invert(mapping: dict[str, str]) -> dict[str, str]:
    """Build the inverse of a long->short mapping, refusing collisions.

    Collisions would make `short_to_long(...)` ambiguous; we fail at
    import time so a new addition with a duplicate short code surfaces
    immediately rather than producing silent name drift.
    """
    inverse: dict[str, str] = {}
    for long, short in mapping.items():
        if short in inverse:
            raise RuntimeError(
                f"_short_codes: collision on short code {short!r}: "
                f"both {inverse[short]!r} and {long!r} map to it. "
                f"Extend the mapping with a distinct short code."
            )
        inverse[short] = long
    return inverse


SHORT_TO_LONG: dict[str, str] = _invert(LONG_TO_SHORT)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

class UnknownLongForm(KeyError):
    """Raised when a renderer is asked to look up a long form that has no
    short code. The message lists the category the caller asked about so
    the operator can extend the right sub-dict in this module.
    """


def baseline_short(long_form: str) -> str:
    """Return the short code for a baseline name; raise on miss.

    Case-insensitive on the long form so callers can pass either
    `delphi-full` or `DELPHI-Full` (the CLI mixes cases for the
    operator-facing argparse choices).
    """
    key = long_form.lower()
    if key not in _BASELINES:
        raise UnknownLongForm(
            f"baseline {long_form!r} has no short code. Known baselines: "
            f"{sorted(_BASELINES)}. Extend `_BASELINES` in "
            f"experiments/orchestrator/_short_codes.py."
        )
    return _BASELINES[key]


def spec_id_short(long_form: str) -> str:
    """Return the short code for a spec id; raise on miss. Case-insensitive."""
    key = long_form.lower()
    if key not in _SPEC_IDS:
        raise UnknownLongForm(
            f"spec_id {long_form!r} has no short code. Known spec ids: "
            f"{sorted(_SPEC_IDS)}. Extend `_SPEC_IDS` in "
            f"experiments/orchestrator/_short_codes.py."
        )
    return _SPEC_IDS[key]


def state_id_short(long_form: str) -> str:
    """Return the short code for a cluster-state id; raise on miss. Case-insensitive."""
    key = long_form.lower()
    if key not in _STATE_IDS:
        raise UnknownLongForm(
            f"state_id {long_form!r} has no short code. Known state ids: "
            f"{sorted(_STATE_IDS)}. Extend `_STATE_IDS` in "
            f"experiments/orchestrator/_short_codes.py."
        )
    return _STATE_IDS[key]


def workload_class_short(long_form: str) -> str:
    """Return the short code for a workload class; raise on miss. Case-insensitive."""
    key = long_form.lower()
    if key not in _WORKLOAD_CLASSES:
        raise UnknownLongForm(
            f"workload_class {long_form!r} has no short code. Known classes: "
            f"{sorted(_WORKLOAD_CLASSES)}. Extend `_WORKLOAD_CLASSES` in "
            f"experiments/orchestrator/_short_codes.py."
        )
    return _WORKLOAD_CLASSES[key]


def short_to_long(short_form: str) -> str:
    """Inverse lookup; raise on miss.

    Used by the analyzer to render short-code labels back into the long
    forms the paper / human reader expects.
    """
    if short_form not in SHORT_TO_LONG:
        raise UnknownLongForm(
            f"short code {short_form!r} not recognised. Known short codes: "
            f"{sorted(SHORT_TO_LONG)}. If this is a new addition, extend the "
            f"matching `_BASELINES`/`_SPEC_IDS`/`_STATE_IDS`/"
            f"`_WORKLOAD_CLASSES` dict in "
            f"experiments/orchestrator/_short_codes.py."
        )
    return SHORT_TO_LONG[short_form]


__all__ = [
    "LONG_TO_SHORT",
    "SHORT_TO_LONG",
    "UnknownLongForm",
    "baseline_short",
    "spec_id_short",
    "state_id_short",
    "workload_class_short",
    "short_to_long",
]
