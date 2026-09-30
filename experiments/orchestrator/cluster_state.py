"""Render background-load and disturbance-stream Jobs from a cluster-state spec.

A cluster-state spec under `experiments/specs/cluster-states/` describes
how a particular state of the federated lab is materialised. Two
schemas are supported:

- **Single-pin (legacy, iter-2b/iter-3a)**: the spec carries a
  `background_jobs:` list of one-Job-per-cluster long-running pinned
  stressors. Rendered via `render_cluster_state`.
- **Disturbance-stream (iter-4h-2)**: the spec carries a
  `disturbance:` block with `target_clusters`, a `spawn` profile, a
  `per_job` default block, and per-target overrides. Rendered via
  `render_disturbance_stream` into N planned-arrival disturbance Jobs
  per target. Used for Phase-B's continuous-perturbation regime.

The two schemas are mutually exclusive: a spec carrying both
`background_jobs:` and `disturbance:` is rejected at render time.
This module owns the validation and rejection contract.

The pin-to-cluster annotation is honoured by the decision-maker
(iter-3a) — both schemas resolve to the named cluster directly,
bypassing the agent / heuristic / NATS path.
"""
from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import _short_codes


_DURATION_RE = re.compile(r"^(\d+)\s*([smhd]?)$")


@dataclass(frozen=True)
class RenderedBackgroundJob:
    name: str
    path: Path
    pin_to: str
    workload_class: str   # "cpu" or "memory"
    benchmark_type: str


@dataclass(frozen=True)
class RenderedDisturbanceJob:
    """One scheduled disturbance Job (iter-4h-2).

    Same structural shape as a single-pin background-load Job (pin-to-
    cluster, time-bound, kind=background), with an extra
    `planned_submit_offset_seconds` carried so the submit-thread can
    pace the spawn schedule and the analyzer can compare planned vs
    realised population in `analysis/disturbance-population.csv`.
    """
    name: str
    path: Path
    pin_to: str
    workload_class: str
    benchmark_type: str
    planned_submit_offset_seconds: float
    intensity_label: str
    disturbance_cell: str  # = f"{run_id}-{state_id}", single-key cleanup label


# Allowed spawn patterns; mirrors render._VALID_PATTERNS but kept local
# to avoid coupling the cluster_state module to render.py's private
# constants at module-import time. The renderer below imports
# `_arrival_offsets` locally inside the function for the same reason.
_VALID_DISTURBANCE_PATTERNS = {"uniform", "poisson", "burst"}
_VALID_DISTURBANCE_WORKLOAD_CLASSES = {"cpu", "memory"}  # network is Q1=A out-of-scope


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _duration_seconds(value: str) -> int:
    """Parse a stress-ng-style duration string like '60s', '5m', '2h'."""
    m = _DURATION_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"unparseable DURATION={value!r}")
    n = int(m.group(1))
    unit = m.group(2) or "s"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * mult


def _bg_job_name(state_id: str, run_id: str, idx: int,
                 pin_to: str, wc: str) -> str:
    base = f"delphi-bg-{state_id}-{pin_to}-{wc}-{idx:03d}-{run_id}"
    base = base.lower()
    # K8s name constraint: <=63 chars, DNS-1123 label.
    if len(base) > 63:
        base = base[:63].rstrip("-").rstrip(".")
    return base


def render_cluster_state(state_path: Path,
                         run_id: str,
                         templates_dir: Path,
                         output_dir: Path,
                         namespace: str = "delphi-experiments",
                         spec_id_override: str | None = None,
                         images: dict[str, str] | None = None) -> list[RenderedBackgroundJob]:
    """Render the background-load Jobs declared in a cluster-state spec.

    `output_dir` is the parent; files land in `output_dir/background/*.yaml`.
    The `namespace` argument defaults to `delphi-experiments` for parity
    with foreground jobs; cluster-state specs are conceptually attached
    to a parent foreground run so their Jobs share the namespace.

    `images` is the resolved per-experiment image manifest (iter-4c.2).
    Required for rendering: every template references {{ images.<key> }}.
    Caller is render.py::render_bootstrap, which loads images.json once
    per run and threads it through. We keep the signature
    backwards-compatible (Optional with a clear ValueError fallback)
    rather than making it Mandatory so legacy callers fail loudly
    instead of mysteriously hitting StrictUndefined inside Jinja.
    """
    if images is None:
        raise ValueError(
            "render_cluster_state requires an `images` mapping "
            "(iter-4c.2). Caller should load it via "
            "render._load_images(<run-output-dir>)."
        )
    state = _load_yaml(state_path)
    state_id = state["state_id"]
    spec_id = spec_id_override or state_id
    bg_jobs = state.get("background_jobs") or []

    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    out_dir = output_dir / "background"
    out_dir.mkdir(parents=True, exist_ok=True)

    rendered: list[RenderedBackgroundJob] = []
    for idx, entry in enumerate(bg_jobs):
        template = entry["template"]
        pin_to = entry["pin_to"]
        params = entry["parameters"]
        # Workload class is derived from the template name (job-<wc>-background.yaml.j2).
        wc = "memory" if "memory" in template else "cpu"
        # activeDeadlineSeconds: DURATION + 10% buffer, rounded up.
        try:
            dur = _duration_seconds(params["DURATION"])
        except ValueError as exc:
            raise ValueError(f"cluster-state spec {state_path}: {exc}") from exc
        deadline = int(dur * 1.1) + 60

        tmpl = env.get_template(template)
        name = _bg_job_name(state_id, run_id, idx, pin_to, wc)
        ctx = {
            "job_name": name,
            "namespace": namespace,
            "spec_id": spec_id,
            "run_id": run_id,
            "state_id": state_id,
            "pin_to": pin_to,
            "parameters": params,
            "active_deadline_seconds": deadline,
            "images": images,
        }
        rendered_yaml = tmpl.render(**ctx)
        out_path = out_dir / f"{idx:03d}-{name}.yaml"
        out_path.write_text(rendered_yaml, encoding="utf-8")
        rendered.append(RenderedBackgroundJob(
            name=name,
            path=out_path,
            pin_to=pin_to,
            workload_class=wc,
            benchmark_type=params.get("BENCHMARK_TYPE", ""),
        ))

    return rendered


# ---------------------------------------------------------------------------
# iter-4h-2: disturbance-stream rendering
# ---------------------------------------------------------------------------

def validate_state_spec(state: dict[str, Any], *, source: Path | str) -> None:
    """Validate a parsed cluster-state spec dict.

    Enforces the mutual-exclusion contract between single-pin and
    disturbance-stream specs (iter-4h-2 plan § 5). The renderer
    dispatches on which key is present — both being set is
    structurally ambiguous and rejected here so the caller does not
    silently render half the spec.
    """
    has_bg = bool(state.get("background_jobs"))
    has_disturbance = "disturbance" in state and state.get("disturbance") is not None
    if has_bg and has_disturbance:
        raise ValueError(
            f"cluster-state spec {source!s}: cannot carry both "
            f"`background_jobs:` (single-pin) and `disturbance:` "
            f"(disturbance-stream). Pick one — they are mutually "
            f"exclusive per the iter-4h-2 plan § 5."
        )
    if not has_bg and not has_disturbance:
        # Idle states are allowed to be empty (no background; no
        # disturbance). Nothing to validate.
        return
    if has_disturbance:
        _validate_disturbance_block(state["disturbance"], source=source)
    # has_bg branch: the legacy renderer below validates per-entry as
    # it iterates; no extra check here.


def _validate_disturbance_block(disturbance: dict[str, Any], *,
                                source: Path | str) -> None:
    """Sanity-check a `disturbance:` block before rendering.

    Catches schema mistakes the operator might introduce when hand-
    editing the per-target overrides after the calibration smoke. The
    actual stress-ng `cpu_load` / `memory_load` semantics are validated
    later by the stressor entrypoint; here we only check structural
    correctness.
    """
    targets = disturbance.get("target_clusters") or []
    if not targets:
        raise ValueError(
            f"{source!s}: disturbance.target_clusters is empty; a "
            f"disturbance-stream state must touch at least one cluster"
        )
    classes = disturbance.get("workload_classes") or []
    unknown = set(classes) - _VALID_DISTURBANCE_WORKLOAD_CLASSES
    if unknown:
        raise ValueError(
            f"{source!s}: disturbance.workload_classes={classes!r} "
            f"contains classes not supported in iter-4h-2: "
            f"{sorted(unknown)}. iter-4h-2 plan Q1=A restricts to "
            f"[cpu, memory]; add network disturbance in a follow-up "
            f"iter."
        )
    spawn = disturbance.get("spawn") or {}
    pattern = spawn.get("pattern")
    if pattern not in _VALID_DISTURBANCE_PATTERNS:
        raise ValueError(
            f"{source!s}: disturbance.spawn.pattern={pattern!r}; must "
            f"be one of {sorted(_VALID_DISTURBANCE_PATTERNS)}"
        )
    if pattern in ("poisson", "uniform"):
        if "mean_inter_arrival_seconds" not in spawn:
            raise ValueError(
                f"{source!s}: disturbance.spawn.pattern={pattern!r} "
                f"requires mean_inter_arrival_seconds"
            )
    if pattern == "burst":
        for k in ("burst_size", "rest_seconds"):
            if k not in spawn:
                raise ValueError(
                    f"{source!s}: disturbance.spawn.pattern='burst' "
                    f"requires {k}"
                )
    if "max_concurrent_disturbance_jobs" not in spawn:
        raise ValueError(
            f"{source!s}: disturbance.spawn.max_concurrent_disturbance_jobs "
            f"is required (used as a circuit-breaker; not a population "
            f"target — see iter-4h-2 plan § 5.4)"
        )
    per_job = disturbance.get("per_job") or {}
    if "duration_seconds" not in per_job:
        raise ValueError(
            f"{source!s}: disturbance.per_job.duration_seconds is required"
        )
    # per_target_overrides is OPTIONAL — if absent, all targets use the
    # scalar defaults from `spawn` and `per_job`. When present, we walk
    # it to surface typos in target names early.
    overrides = disturbance.get("per_target_overrides") or {}
    unknown_targets = set(overrides) - set(targets)
    if unknown_targets:
        raise ValueError(
            f"{source!s}: disturbance.per_target_overrides has entries "
            f"for clusters not in target_clusters: "
            f"{sorted(unknown_targets)}. Either add them to "
            f"target_clusters or remove the orphan overrides."
        )


def _merge_target_overrides(disturbance: dict[str, Any],
                            target: str) -> dict[str, Any]:
    """Return the effective per-target spawn+per_job parameters.

    Merges `disturbance.spawn` + `disturbance.per_job` defaults with
    the optional `disturbance.per_target_overrides[<target>]` entries.
    Per-target values win over the scalar defaults. Returned dict is
    flat and contains every key the renderer needs.
    """
    spawn = dict(disturbance.get("spawn") or {})
    per_job = dict(disturbance.get("per_job") or {})
    overrides = (disturbance.get("per_target_overrides") or {}).get(target, {}) or {}
    out: dict[str, Any] = {**spawn, **per_job}
    out.update(overrides)
    return out


def _disturbance_job_name(short_state: str, short_target: str, short_wc: str,
                          idx: int, run_id: str) -> str:
    """Construct a DNS-1123-safe disturbance Job name within 63 bytes.

    Layout (length-budget analysis is in iter-4h-2 plan § 6.0):
        delphi-bg-<short_state>-<short_target>-<short_wc>-<NNNN>-<run_id>

    Heavy state may spawn hundreds of disturbance Jobs per cell, so the
    index field is 4 digits. Truncation drops the tail (run_id suffix)
    last, which preserves the cluster/index discriminators.
    """
    base = (
        f"delphi-bg-{short_state}-{short_target}-{short_wc}-"
        f"{idx:04d}-{run_id}"
    )
    base = base.lower()
    if len(base) > 63:
        base = base[:63].rstrip("-").rstrip(".")
    return base


def render_disturbance_stream(state_path: Path,
                              run_id: str,
                              templates_dir: Path,
                              output_dir: Path,
                              foreground_window_seconds: float,
                              *,
                              namespace: str = "delphi-experiments",
                              spec_id_override: str | None = None,
                              images: dict[str, str] | None = None,
                              disturbance_tail_seconds: float = 60.0,
                              ) -> list[RenderedDisturbanceJob]:
    """Render a disturbance-stream cluster-state spec (iter-4h-2 § 6.1).

    Inputs
    ------
    state_path : path to e.g. `mixed-disturbance-heavy.yaml`. The spec
        must carry a `disturbance:` block; absence raises ValueError.
    run_id : the experiment run id (used in the rendered Job's
        labels/annotations and in the disturbance-cell label).
    templates_dir : path to `experiments/templates/`. The function
        reuses the existing `job-cpu-background.yaml.j2` and
        `job-memory-background.yaml.j2` templates — no new templates
        are introduced (iter-4h-2 plan § 6.1).
    output_dir : per-state output directory. Files land in
        `<output_dir>/disturbance/*.yaml` and the planned timeline in
        `<output_dir>/disturbance-timeline-planned.csv`.
    foreground_window_seconds : the duration of the foreground spec's
        arrival window. The disturbance generator stops emitting new
        Jobs after `foreground_window_seconds + disturbance_tail_seconds`
        so the stream ends shortly after the last foreground arrival.
    images : the per-experiment image manifest (iter-4c.2).
    disturbance_tail_seconds : grace at the end of the foreground
        window during which disturbance still spawns (so the last
        foreground Job sees disturbance pressure during its own
        execution). Default 60 s.

    Output
    ------
    Returns a list of `RenderedDisturbanceJob` in the order their
    planned-submit offsets occur. Writes one YAML per disturbance Job
    plus a `disturbance-timeline-planned.csv` for the submit-thread
    and the analyzer.
    """
    if images is None:
        raise ValueError(
            "render_disturbance_stream requires an `images` mapping "
            "(iter-4c.2). Caller should load it via "
            "render._load_images(<run-output-dir>)."
        )

    state = _load_yaml(state_path)
    validate_state_spec(state, source=state_path)
    if "disturbance" not in state or not state["disturbance"]:
        raise ValueError(
            f"{state_path}: render_disturbance_stream requires a "
            f"`disturbance:` block; this spec carries none (use "
            f"render_cluster_state for single-pin specs)."
        )

    state_id = state["state_id"]
    spec_id = spec_id_override or state_id
    disturbance = state["disturbance"]
    target_clusters: list[str] = list(disturbance["target_clusters"])
    workload_classes: list[str] = list(disturbance["workload_classes"])
    intensity_label = str(disturbance.get("intensity_label") or "")
    disturbance_cell = f"{run_id}-{state_id}".lower()

    # Per-target deterministic RNG. Each target's arrival timeline is
    # independent, but the global seed is `random_seed` (defaulting to
    # 0). Mixing the target name into the seed makes per-target
    # timelines stable under spec re-renders.
    global_seed = int(state.get("random_seed", 0))

    # Local import to avoid module-level circular dependency with
    # render.py (which uses `_arrival_offsets` for the foreground
    # temporal_profile generation).
    from .render import _arrival_offsets

    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    out_dir = output_dir / "disturbance"
    out_dir.mkdir(parents=True, exist_ok=True)

    spawn_window = foreground_window_seconds + disturbance_tail_seconds

    # ---- 1. Generate the per-target timeline ----------------------
    # The planned list is global (one row per Job) so the timeline CSV
    # is one sorted file the submit-thread can read top-to-bottom.
    planned: list[dict[str, Any]] = []  # each entry: target, offset, params
    for t_idx, target in enumerate(target_clusters):
        params = _merge_target_overrides(disturbance, target)
        per_target_rng = random.Random(global_seed * 100003 + (t_idx + 1) * 7)
        # Build a temporal_profile dict matching render._arrival_offsets'
        # expected shape. We don't know how many jobs to plan in advance
        # for poisson/uniform, so we generate offsets until one falls
        # outside the spawn window, then drop the tail.
        mean = float(params["mean_inter_arrival_seconds"])
        jitter = float(params.get("jitter_seconds", 0.0) or 0.0)
        pattern = params["pattern"]
        # Estimate n_jobs ≈ window / mean × 1.5 (50 % overshoot to cover
        # Poisson tail variance), then trim by offset.
        upper_n = max(1, int((spawn_window / mean) * 2 + 4))
        temporal = {
            "pattern": pattern,
            "mean_inter_arrival_seconds": mean,
            "jitter_seconds": jitter,
            "burst_size": params.get("burst_size"),
            "rest_seconds": params.get("rest_seconds"),
        }
        offsets = _arrival_offsets(temporal, upper_n, per_target_rng)
        # Round-robin workload class per spawn (deterministic; the per-
        # target RNG drives a randomised choice if the operator wants it
        # — for now stay deterministic for reproducibility).
        wc_cycle_rng = random.Random(global_seed * 100019 + (t_idx + 1) * 13)
        for offset in offsets:
            if offset > spawn_window:
                break
            wc = wc_cycle_rng.choice(workload_classes)
            planned.append({
                "target": target,
                "offset": float(offset),
                "wc": wc,
                "params": params,
            })

    # Sort the global timeline by offset so the submit-thread reads
    # one chronologically-ordered list. Ties break on target name for
    # determinism.
    planned.sort(key=lambda r: (r["offset"], r["target"]))

    # ---- 2. Render each disturbance Job ---------------------------
    short_state = _short_codes.state_id_short(state_id)
    rendered: list[RenderedDisturbanceJob] = []
    for idx, entry in enumerate(planned):
        target = entry["target"]
        wc = entry["wc"]
        offset = entry["offset"]
        params = entry["params"]

        # `pin_to` is the long-form cluster name — kubeconfig context.
        short_target = target  # cluster names already short (≤ 12 chars)
        short_wc = _short_codes.workload_class_short(wc)
        name = _disturbance_job_name(short_state, short_target, short_wc,
                                     idx, run_id)

        # Build template parameters block. The existing background
        # templates expect `parameters.BENCHMARK_TYPE`,
        # `parameters.CPU_LOAD` or `parameters.MEMORY_LOAD`, and
        # `parameters.DURATION`. We materialise that shape from the
        # merged per-target params.
        duration_s = int(params["duration_seconds"])
        tmpl_params: dict[str, Any] = {
            "BENCHMARK_TYPE": (
                f"disturbance/{state_id}/{target}/{wc}/{intensity_label}"
            ),
            "DURATION": f"{duration_s}s",
        }
        if wc == "cpu":
            tmpl_params["CPU_LOAD"] = str(params["cpu_load"])
            template_name = "job-cpu-background.yaml.j2"
        elif wc == "memory":
            tmpl_params["MEMORY_LOAD"] = str(params["memory_load"])
            template_name = "job-memory-background.yaml.j2"
        else:
            # Should never reach here — validation rejects other
            # classes — but fail loudly if it does.
            raise ValueError(
                f"{state_path}: disturbance workload class {wc!r} has "
                f"no template; only cpu/memory supported in iter-4h-2"
            )

        # iter-3a / iter-4c: activeDeadlineSeconds = duration × 1.1 +
        # 60 s. Declarative safety net so the Job dies even if the
        # orchestrator crashes mid-run.
        deadline = int(duration_s * 1.1) + 60

        tmpl = env.get_template(template_name)
        ctx = {
            "job_name": name,
            "namespace": namespace,
            "spec_id": spec_id,
            "run_id": run_id,
            "state_id": state_id,
            "pin_to": target,
            "parameters": tmpl_params,
            "active_deadline_seconds": deadline,
            "images": images,
            # iter-4h-2-specific labels (templates guard via `is defined`)
            "disturbance_cell": disturbance_cell,
            "short_code": f"{short_state}-{short_target}-{short_wc}",
        }
        rendered_yaml = tmpl.render(**ctx)
        out_path = out_dir / f"{idx:04d}-{name}.yaml"
        out_path.write_text(rendered_yaml, encoding="utf-8")

        rendered.append(RenderedDisturbanceJob(
            name=name,
            path=out_path,
            pin_to=target,
            workload_class=wc,
            benchmark_type=tmpl_params["BENCHMARK_TYPE"],
            planned_submit_offset_seconds=offset,
            intensity_label=intensity_label,
            disturbance_cell=disturbance_cell,
        ))

    # ---- 3. Emit disturbance-timeline-planned.csv -----------------
    timeline_path = output_dir / "disturbance-timeline-planned.csv"
    with timeline_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "job_index", "job_name",
            "planned_submit_offset_seconds",
            "target_cluster", "workload_class",
            "intensity_label", "disturbance_cell",
        ])
        for idx, j in enumerate(rendered):
            writer.writerow([
                idx, j.name,
                f"{j.planned_submit_offset_seconds:.3f}",
                j.pin_to, j.workload_class,
                j.intensity_label, j.disturbance_cell,
            ])

    return rendered
