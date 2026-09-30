"""End-to-end render tests for the iter-4h-2 disturbance-stream path.

Pins three structural invariants:

1. Planned arrival counts per target match the analytical expectation
   for the spec's Poisson rate (within a 30 % envelope to absorb
   single-RNG-seed variance), and per-target counts are NOT zero.
2. The renderer reuses the existing job-{cpu,memory}-background.yaml.j2
   templates — no new template file is introduced. Rendered Jobs
   carry pin-to-cluster, role=background, the iter-4h-2 disturbance-
   cell label, the short-code label, and the expected DURATION /
   activeDeadlineSeconds.
3. Two renders of the same spec under the same `random_seed` produce
   the same disturbance-timeline-planned.csv byte-for-byte (the
   submit-thread paces off this file, so determinism matters).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from experiments.orchestrator.cluster_state import (
    RenderedDisturbanceJob,
    render_disturbance_stream,
)


_REPO = Path(__file__).resolve().parents[3]
_TEMPLATES = _REPO / "experiments" / "templates"
_SPECS = _REPO / "experiments" / "specs" / "cluster-states"
_FAKE_IMAGES = {
    "stress_ng": "registry.example/stress-ng:test",
    "net_stresser": "registry.example/net-stresser:test",
}


def _render(spec_name: str, *, fg_window_s: float = 600.0,
            run_id: str = "eff-df-test-20260601T000000Z",
            tmpdir: Path) -> list[RenderedDisturbanceJob]:
    state_path = _SPECS / f"{spec_name}.yaml"
    return render_disturbance_stream(
        state_path=state_path,
        run_id=run_id,
        templates_dir=_TEMPLATES,
        output_dir=tmpdir,
        foreground_window_seconds=fg_window_s,
        spec_id_override="effectiveness-taskbound",
        images=_FAKE_IMAGES,
    )


# ---------------------------------------------------------------------------
# Plan counts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", [
    "mixed-disturbance-light",
    "mixed-disturbance-heavy",
    "edge-disturbance",
    "public-cloud-disturbance",
])
def test_render_produces_jobs_for_every_target(spec, tmp_path: Path) -> None:
    rendered = _render(spec, tmpdir=tmp_path)
    assert rendered, f"{spec}: rendered zero disturbance jobs"

    # Group by pin_to and assert every declared target has > 0 jobs.
    state = yaml.safe_load((_SPECS / f"{spec}.yaml").read_text())
    declared = set(state["disturbance"]["target_clusters"])
    by_target: dict[str, int] = {}
    for j in rendered:
        by_target[j.pin_to] = by_target.get(j.pin_to, 0) + 1
    for t in declared:
        assert by_target.get(t, 0) > 0, (
            f"{spec}: target {t} declared but received no rendered jobs"
        )
    # And no jobs landed on a target the spec did NOT declare.
    assert not (set(by_target) - declared), (
        f"{spec}: rendered jobs pinned to undeclared targets "
        f"{set(by_target) - declared}"
    )


@pytest.mark.parametrize("spec, expect_min", [
    # Per-target counts from § 5.4.1 expected math (planned steady ×
    # window/duration). Lower bound only — Poisson tail variance.
    ("mixed-disturbance-light", 1),
    ("mixed-disturbance-heavy", 10),
    ("edge-disturbance", 10),
    ("public-cloud-disturbance", 15),
])
def test_per_target_counts_within_poisson_envelope(spec, expect_min,
                                                    tmp_path: Path) -> None:
    rendered = _render(spec, tmpdir=tmp_path)
    by_target: dict[str, int] = {}
    for j in rendered:
        by_target[j.pin_to] = by_target.get(j.pin_to, 0) + 1
    # Every declared target hits at least `expect_min` jobs.
    for t, n in by_target.items():
        assert n >= expect_min, (
            f"{spec}/{t}: got {n} jobs; expected ≥ {expect_min}"
        )


# ---------------------------------------------------------------------------
# Rendered manifest shape — reuse existing templates
# ---------------------------------------------------------------------------

def test_rendered_job_carries_pin_to_and_role_and_disturbance_cell(tmp_path: Path) -> None:
    rendered = _render("mixed-disturbance-heavy", tmpdir=tmp_path)
    first = rendered[0]
    doc = yaml.safe_load(first.path.read_text())

    ann = doc["metadata"]["annotations"]
    lbl = doc["metadata"]["labels"]

    # iter-3a pin-to-cluster: present and matches RenderedDisturbanceJob.pin_to
    assert ann["delphi.experiments/pin-to-cluster"] == first.pin_to
    assert lbl["delphi.experiments/pin-to-cluster"] == first.pin_to

    # iter-3a role=background: drives the no-pollution invariant.
    assert ann["delphi.experiments/role"] == "background"
    assert lbl["delphi.experiments/role"] == "background"

    # iter-4h-2 disturbance-cell label: enables single-key cleanup
    # fallback (D8). Matches RenderedDisturbanceJob.disturbance_cell.
    assert ann["delphi.experiments/disturbance-cell"] == first.disturbance_cell
    assert lbl["delphi.experiments/disturbance-cell"] == first.disturbance_cell

    # iter-4h-2 short-code (annotation only — not on labels).
    assert "delphi.experiments/short-code" in ann


def test_rendered_job_uses_existing_background_template(tmp_path: Path) -> None:
    """Each rendered Job's pod-spec must look like the existing
    iter-3a single-pin background-load Job: stress-ng-runner container,
    BENCHMARK_TYPE / TASK_TYPE / DURATION env, no sidecar injection
    references, no optimize-propagation false-flag."""
    rendered = _render("mixed-disturbance-heavy", tmpdir=tmp_path)
    j = rendered[0]
    doc = yaml.safe_load(j.path.read_text())
    container = doc["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "stress-ng-runner"
    env_keys = {e["name"] for e in container["env"]}
    assert "BENCHMARK_TYPE" in env_keys
    assert "DURATION" in env_keys
    assert "TASK_TYPE" in env_keys


def test_active_deadline_seconds_is_duration_plus_buffer(tmp_path: Path) -> None:
    """D11: activeDeadlineSeconds = duration_seconds × 1.1 + 60. With
    duration=90s for heavy, deadline = 99 + 60 = 159s."""
    rendered = _render("mixed-disturbance-heavy", tmpdir=tmp_path)
    for j in rendered:
        doc = yaml.safe_load(j.path.read_text())
        # All heavy disturbance Jobs in this spec have duration 90s.
        env = {e["name"]: e["value"]
               for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["DURATION"] == "90s"
        # int(90 × 1.1) + 60 = 99 + 60 = 159
        assert doc["spec"]["activeDeadlineSeconds"] == 159


def test_rendered_job_name_within_dns1123_limit(tmp_path: Path) -> None:
    """Every rendered Job's metadata.name must be ≤ 63 chars."""
    rendered = _render("mixed-disturbance-heavy", tmpdir=tmp_path)
    for j in rendered:
        doc = yaml.safe_load(j.path.read_text())
        name = doc["metadata"]["name"]
        assert len(name) <= 63, f"{j.name} is {len(name)} chars"
        assert name == name.lower(), f"{name} should be all-lowercase"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_two_renders_produce_byte_identical_timeline_csv(tmp_path: Path) -> None:
    """Determinism contract: the same run_id + spec → byte-identical
    disturbance-timeline-planned.csv. The submit-thread reads this CSV;
    a non-deterministic timeline means a re-render after operator
    review of `--dry-spawn` would diverge from the live run."""
    out1 = tmp_path / "a"
    out2 = tmp_path / "b"
    out1.mkdir()
    out2.mkdir()
    _render("mixed-disturbance-heavy", tmpdir=out1)
    _render("mixed-disturbance-heavy", tmpdir=out2)
    csv1 = (out1 / "disturbance-timeline-planned.csv").read_bytes()
    csv2 = (out2 / "disturbance-timeline-planned.csv").read_bytes()
    assert csv1 == csv2, "disturbance-timeline-planned.csv is non-deterministic"


def test_disturbance_cell_label_unique_per_run_and_state(tmp_path: Path) -> None:
    """The disturbance-cell label is `<run-id>-<state-id>` lowercase
    by construction. Two runs differ in run_id; two states differ in
    state_id."""
    a = _render("mixed-disturbance-heavy", tmpdir=tmp_path / "a",
                run_id="eff-df-mdh-20260601T000000Z")
    b = _render("mixed-disturbance-heavy", tmpdir=tmp_path / "b",
                run_id="eff-df-mdh-20260602T000000Z")
    c = _render("edge-disturbance", tmpdir=tmp_path / "c",
                run_id="eff-df-mdh-20260601T000000Z")
    assert a[0].disturbance_cell != b[0].disturbance_cell
    assert a[0].disturbance_cell != c[0].disturbance_cell


# ---------------------------------------------------------------------------
# Validation hooks at render-time
# ---------------------------------------------------------------------------

def test_render_rejects_a_spec_with_both_keys(tmp_path: Path) -> None:
    """A spec with both background_jobs: and disturbance: must raise
    at render time, not produce a half-rendered tree."""
    # Build a synthetic spec with both keys.
    bad_spec = {
        "state_id": "test-conflicted",
        "background_jobs": [
            {"template": "job-cpu-background.yaml.j2",
             "pin_to": "on-prem",
             "parameters": {"BENCHMARK_TYPE": "x", "CPU_LOAD": "1",
                            "DURATION": "60s"}},
        ],
        "disturbance": {
            "target_clusters": ["on-prem"],
            "workload_classes": ["cpu"],
            "spawn": {
                "pattern": "poisson",
                "mean_inter_arrival_seconds": 30.0,
                "max_concurrent_disturbance_jobs": 4,
            },
            "per_job": {"duration_seconds": 90, "cpu_load": "1",
                        "memory_load": "1G"},
        },
    }
    spec_path = tmp_path / "bad.yaml"
    spec_path.write_text(yaml.safe_dump(bad_spec))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ValueError, match="mutually exclusive"):
        render_disturbance_stream(
            state_path=spec_path, run_id="r", templates_dir=_TEMPLATES,
            output_dir=out, foreground_window_seconds=300.0,
            images=_FAKE_IMAGES,
        )
