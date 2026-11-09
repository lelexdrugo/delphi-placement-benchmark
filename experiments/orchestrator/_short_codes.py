"""Short codes and length helpers for Kubernetes object names.

Kubernetes Job names cap at 63 characters (DNS-1123 label). With the
per-experiment run_id and the cluster-state id both in the name, the
long-form rendered name
`delphi-<spec_id>-<baseline>-<state_id>-<run_id>-<idx>-<wc>` overflows
the 63-byte budget, and a plain truncation drops the tail, which is
where `state_id` lives; the analyzer needs that field to attribute a
Job to a cell.

The short-code mapping is applied at *name* construction time only.
Long forms continue to appear in labels and annotations so analyzer
queries and human `kubectl get -L ...` listings stay readable.

Single source of truth: adding a new baseline / spec / state requires
extending this module *and* the corresponding test
(`test_short_codes_complete.py`). The renderer fails fast on a missing
lookup so a missing code cannot silently truncate a name.

`fit_name` and `fit_label_value` keep any name or label value within
63 characters; an over-long value is cut and suffixed with a hash of
the full value, so two long values that differ only past the cut do
not collapse onto the same string. `assert_unique_names` rejects a
render that still produces duplicate Job names.

Coverage:
- baselines:        df / hs / sk
- spec_ids:         etb / eff
- state_ids:        idle / mdl / mdh / ed / pcd
- workload_classes: c / m / n
"""
from __future__ import annotations

import zlib
from collections import Counter
from typing import Iterable

# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

# Baselines. The long form is the `--baseline` value (case-insensitive)
# and, by convention, part of the run-id; the short form is what the
# renderer puts into the Kubernetes Job name. The baselines are mutually
# exclusive per the decision-maker's startup contract
# (DETERMINISTIC_TARGET_CLUSTER vs HEURISTIC_SCORER_ENABLED).
_BASELINES: dict[str, str] = {
    "delphi-full": "df",
    "heuristic-scorer": "hs",
    "static-karmada": "sk",
}

# Spec IDs. `effectiveness-taskbound` is the task-bound foreground
# spec. `effectiveness` is the legacy time-bound spec.
_SPEC_IDS: dict[str, str] = {
    "effectiveness-taskbound": "etb",
    "effectiveness": "eff",
}

# Cluster-state IDs. `idle` keeps its 4-char form (no shortening needed).
# The four disturbance states get explicit short codes so rendered
# names stay inside the 63-char DNS-1123 budget.
_STATE_IDS: dict[str, str] = {
    "idle": "idle",
    "mixed-disturbance-light": "mdl",
    "mixed-disturbance-heavy": "mdh",
    "edge-disturbance": "ed",
    "public-cloud-disturbance": "pcd",
}

# Workload classes. The existing renderer already uses single-character
# discriminators internally for some name fragments; this mapping
# centralises them so the name fragments cannot diverge.
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
    forms a human reader expects.
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


# ---------------------------------------------------------------------------
# Length helpers for names and label values
# ---------------------------------------------------------------------------

K8S_NAME_MAX = 63
_HASH_CHARS = 6


def _hash_suffix(value: str) -> str:
    """First six hex digits of the CRC-32 of `value` (stable across runs)."""
    return f"{zlib.crc32(value.encode('utf-8')) & 0xFFFFFFFF:08x}"[:_HASH_CHARS]


def fit_name(name: str, limit: int = K8S_NAME_MAX) -> str:
    """Return `name` unchanged if it fits in `limit` characters.

    A longer name is cut to `limit - 7` characters, stripped of trailing
    `-` / `.`, and suffixed with `-<6 hex digits>` of the CRC-32 of the
    full name, so the result is at most `limit` characters, still ends
    with an alphanumeric character, and two names that differ only past
    the cut stay distinct. Callers pass an already lowercased DNS-1123
    string.
    """
    if len(name) <= limit:
        return name
    head = name[: limit - _HASH_CHARS - 1].rstrip("-.")
    return f"{head}-{_hash_suffix(name)}"


def fit_label_value(value: str, limit: int = K8S_NAME_MAX) -> str:
    """Return a Kubernetes label value: at most `limit` characters,
    alphanumeric at both ends.

    A value that already satisfies both rules is returned unchanged.
    Otherwise the value is stripped of leading/trailing `-`, `_`, `.`,
    and, if still too long, cut and suffixed with `-<6 hex digits>` of
    the CRC-32 of the original value (as in `fit_name`).
    """
    def _alnum(c: str) -> bool:
        return c.isascii() and c.isalnum()

    if len(value) <= limit and (not value
                                or (_alnum(value[0]) and _alnum(value[-1]))):
        return value
    stripped = value.strip("-_.")
    if len(stripped) <= limit:
        return stripped
    head = stripped[: limit - _HASH_CHARS - 1].rstrip("-_.")
    return f"{head}-{_hash_suffix(value)}"


def assert_unique_names(names: Iterable[str], where: str) -> None:
    """Raise ValueError if `names` contains duplicates.

    Two Jobs rendered with the same name would overwrite each other on
    `kubectl apply`, so a render that produces duplicates fails here,
    before anything is submitted.
    """
    dupes = sorted(n for n, c in Counter(names).items() if c > 1)
    if dupes:
        raise ValueError(
            f"{where}: rendered {len(dupes)} duplicate Job name(s): "
            f"{dupes[:5]}{' ...' if len(dupes) > 5 else ''}"
        )


__all__ = [
    "LONG_TO_SHORT",
    "SHORT_TO_LONG",
    "UnknownLongForm",
    "baseline_short",
    "spec_id_short",
    "state_id_short",
    "workload_class_short",
    "short_to_long",
    "K8S_NAME_MAX",
    "fit_name",
    "fit_label_value",
    "assert_unique_names",
]
