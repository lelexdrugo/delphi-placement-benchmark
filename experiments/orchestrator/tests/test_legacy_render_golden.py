"""iter-4h-1 regression: legacy `effectiveness.yaml` renders byte-identical.

iter-4h-1 added `{%- if parameters.<KEY> is defined %}` guards around
`WORK_MODE`, `CPU_OPS`, `MEMORY_OPS`, and `DURATION` in
`job-cpu.yaml.j2` and `job-memory.yaml.j2`. The legacy
`effectiveness.yaml` declares `DURATION` for all three workload classes
and omits the new keys, so the rendered Jobs MUST be byte-identical
to a checked-in golden snapshot captured against the pre-edit
templates.

If this test fails:

- a YAML diff means the template change broke the contract — fix the
  template, do NOT re-bake the goldens;
- a missing-golden error means `effectiveness.yaml` grew a new
  workload class or distribution and the goldens need regeneration —
  re-run the bake step documented in
  `iter-4h-1-workmode-pivot.md` § S3.

A companion positive test below renders `effectiveness-taskbound.yaml`
(once it lands) and asserts the new env entries appear when the spec
declares them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Resolve the repo root from this file's location. The test runs from
# `experiments/orchestrator/tests/` so the repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SPEC = _REPO_ROOT / "experiments" / "specs" / "effectiveness.yaml"
_TASKBOUND_SPEC = _REPO_ROOT / "experiments" / "specs" / "effectiveness-taskbound.yaml"
_REGISTRY = _REPO_ROOT / "experiments" / "specs" / "cluster-registry.yaml"
_TEMPLATES = _REPO_ROOT / "experiments" / "templates"
_GOLDENS = Path(__file__).resolve().parent / "goldens" / "effectiveness"

_FIXTURE_IMAGES = {
    "stress_ng": "registry.golden.test/stress-ng-runner:golden",
    "net_stresser": "registry.golden.test/net-stresser:golden",
    "registry": "registry.golden.test",
    "run_id": "golden-legacy-effectiveness",
    "mode": "fixture",
    "source_tag": "golden",
    "prepared_at_utc": "2026-05-26T00:00:00Z",
}


def _write_images_json(directory: Path, run_id: str = "golden-legacy-effectiveness") -> None:
    images = dict(_FIXTURE_IMAGES)
    images["run_id"] = run_id
    (directory / "images.json").write_text(json.dumps(images), encoding="utf-8")


def test_legacy_effectiveness_renders_byte_identical(tmp_path):
    """Render `effectiveness.yaml` against the current templates and
    compare each produced Job YAML byte-for-byte against the checked-in
    goldens.

    Goldens were captured by running this same render pipeline against
    the *pre-iter-4h-1* templates (no `is defined` guards). The
    iter-4h-1 template edits MUST NOT change the output for a spec
    that does not declare the new keys.
    """
    from experiments.orchestrator.render import render  # noqa: PLC0415

    _write_images_json(tmp_path)

    jobs = render(
        spec_path=_SPEC,
        registry_path=_REGISTRY,
        run_id="golden-legacy-effectiveness",
        templates_dir=_TEMPLATES,
        output_dir=tmp_path,
    )

    assert len(jobs) == 40, f"expected 40 jobs, got {len(jobs)}"

    # Line endings are normalised on both sides: the renderer writes text mode,
    # so CRLF on Windows, while .gitattributes checks the goldens out with LF.
    def _bytes(path):
        return path.read_bytes().replace(b"\r\n", b"\n")

    mismatches = []
    for j in jobs:
        golden = _GOLDENS / j.path.name
        if not golden.exists():
            mismatches.append((j.path.name, "golden missing"))
            continue
        actual_bytes = _bytes(j.path)
        expected_bytes = _bytes(golden)
        if actual_bytes != expected_bytes:
            mismatches.append((j.path.name, "bytes differ"))

    timeline_actual = _bytes(tmp_path / "timeline-planned.csv")
    timeline_expected = _bytes(_GOLDENS / "timeline-planned.csv")
    if timeline_actual != timeline_expected:
        mismatches.append(("timeline-planned.csv", "bytes differ"))

    if mismatches:
        pytest.fail(
            f"{len(mismatches)} file(s) diverged from golden:\n"
            + "\n".join(f"  - {n}: {r}" for n, r in mismatches[:10])
            + ("\n  ... (more truncated)" if len(mismatches) > 10 else "")
        )


def test_taskbound_spec_renders_with_new_env_vars(tmp_path):
    """Companion positive: when a spec declares the new `WORK_MODE` /
    `CPU_OPS` / `MEMORY_OPS` keys, the templates render env entries
    for them; legacy `DURATION` is omitted when the spec omits it
    (CPU/memory blocks in `effectiveness-taskbound.yaml`).
    """
    if not _TASKBOUND_SPEC.exists():
        pytest.skip(
            f"{_TASKBOUND_SPEC} not yet present; this test activates once "
            "iter-4h-1's S6 lands."
        )

    from experiments.orchestrator.render import render  # noqa: PLC0415

    _write_images_json(tmp_path, run_id="taskbound-positive")

    jobs = render(
        spec_path=_TASKBOUND_SPEC,
        registry_path=_REGISTRY,
        run_id="taskbound-positive",
        templates_dir=_TEMPLATES,
        output_dir=tmp_path,
    )

    cpu_yaml = None
    memory_yaml = None
    network_yaml = None
    for j in jobs:
        if j.workload_class == "cpu" and cpu_yaml is None:
            cpu_yaml = j.path.read_text(encoding="utf-8")
        elif j.workload_class == "memory" and memory_yaml is None:
            memory_yaml = j.path.read_text(encoding="utf-8")
        elif j.workload_class == "network" and network_yaml is None:
            network_yaml = j.path.read_text(encoding="utf-8")

    assert cpu_yaml is not None, "no CPU job rendered from effectiveness-taskbound.yaml"
    assert memory_yaml is not None, "no Memory job rendered from effectiveness-taskbound.yaml"
    assert network_yaml is not None, "no Network job rendered from effectiveness-taskbound.yaml"

    # CPU: WORK_MODE + CPU_OPS present; DURATION absent (the spec omits it for CPU).
    assert 'name: WORK_MODE' in cpu_yaml
    assert 'value: "ops"' in cpu_yaml
    assert 'name: CPU_OPS' in cpu_yaml
    assert 'name: DURATION' not in cpu_yaml, (
        "CPU ops-mode job carries DURATION env var; spec omits it, "
        "and the `is defined` guard should drop it."
    )

    # Memory: WORK_MODE + MEMORY_OPS present; DURATION absent.
    assert 'name: WORK_MODE' in memory_yaml
    assert 'name: MEMORY_OPS' in memory_yaml
    assert 'name: DURATION' not in memory_yaml

    # Network: untouched by iter-4h-1's template change; MODE+BURST_SIZE+DURATION present.
    assert 'name: MODE' in network_yaml
    assert 'value: "burst-download"' in network_yaml
    assert 'name: BURST_SIZE' in network_yaml


def test_bake_helper_present():
    """The bake step is documented in S3 of the iter-4h-1 plan; the
    goldens directory must exist (otherwise the regression test
    silently passes against the missing-file error path)."""
    assert _GOLDENS.exists(), (
        f"goldens directory missing: {_GOLDENS}. "
        "Re-run the bake step from iter-4h-1-workmode-pivot.md § S3."
    )
    files = sorted(p.name for p in _GOLDENS.iterdir())
    yaml_files = [n for n in files if n.endswith(".yaml")]
    assert len(yaml_files) == 40, (
        f"expected 40 golden Job YAML files; found {len(yaml_files)}"
    )
    # timeline-planned.csv must also be present.
    assert "timeline-planned.csv" in files
