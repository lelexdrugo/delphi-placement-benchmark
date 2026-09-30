"""Tests for the registry_degradation plot (iter-4h-3)."""
from __future__ import annotations

import json
from pathlib import Path

from experiments.orchestrator.analysis.plots import (
    registry_degradation,
    registry_degradation_compliance,
)


def _write_cell(root: Path, baseline: str, variant: str,
                hp: float, ct: float, cp: float) -> Path:
    """Write a minimal (baseline, variant) run-dir: summary.json only —
    the plot reads metrics + registry_diff.variant_id from there."""
    run_dir = root / f"{baseline}-{variant}"
    adir = run_dir / "analysis"
    adir.mkdir(parents=True)
    summary = {
        "baseline": baseline,
        "baselines": {
            baseline: {
                "registry_diff": {"variant_id": variant, "perturbed_cells": []},
                "intent_compliance": {"hard_pass_rate": hp},
                "completion_time": {"median_s": ct},
                "cost_proxy": {"total": cp},
            }
        },
    }
    (adir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return run_dir


def _campaign(root: Path) -> list[Path]:
    """3 baselines x 2 variants (r0, r2) with the pre-committed shape:
    Heuristic collapses r0->r2, Static + DELPHI flat."""
    cells = [
        ("static-karmada", "r0", 0.10, 70.0, 200.0),
        ("static-karmada", "r2", 0.10, 70.0, 200.0),
        ("heuristic-scorer", "r0", 0.75, 90.0, 250.0),
        ("heuristic-scorer", "r2", 0.65, 95.0, 240.0),
        ("delphi-full", "r0", 0.975, 100.0, 180.0),
        ("delphi-full", "r2", 0.975, 100.0, 180.0),
    ]
    return [_write_cell(root, *c) for c in cells]


def test_prepare_shape_and_order(tmp_path: Path):
    df = registry_degradation._prepare(_campaign(tmp_path))
    assert df.shape == (6, 6)
    assert set(df.columns) == {
        "variant", "baseline_key", "hard_pass_rate", "hard_pass_rate_completion_aware",
        "completion_median_s", "cost_proxy_total",
    }
    assert list(df["variant"].cat.categories) == ["r0", "r2"]


def test_heuristic_collapses_static_and_delphi_flat(tmp_path: Path):
    df = registry_degradation._prepare(_campaign(tmp_path))

    def hp(baseline, variant):
        sub = df[(df["baseline_key"] == baseline) & (df["variant"] == variant)]
        return sub["hard_pass_rate"].iloc[0]

    assert hp("heuristic-scorer", "r0") > hp("heuristic-scorer", "r2")
    assert hp("static-karmada", "r0") == hp("static-karmada", "r2")
    assert hp("delphi-full", "r0") == hp("delphi-full", "r2")


def test_generate_writes_file(tmp_path: Path):
    written = registry_degradation.generate(
        _campaign(tmp_path),
        tmp_path / "out" / "registry-degradation-tolerance",
        formats=["pdf"],
        synthetic_preview=True,
    )
    assert len(written) == 1
    assert written[0].stat().st_size > 1024
    assert ".SYNTHETIC." in written[0].name


def test_select_panels_subset_and_default():
    # default -> all three canonical panels, in order.
    full = registry_degradation._select_panels(None)
    assert [p[0] for p in full] == [
        "hard_pass_rate", "completion_median_s", "cost_proxy_total",
    ]
    # subset -> only the requested column, label is the corrected one.
    one = registry_degradation._select_panels(("hard_pass_rate",))
    assert len(one) == 1 and one[0][0] == "hard_pass_rate"
    # the completion panel is labelled "Completion time", not "Exec. runtime"
    # (it plots completion_time.median_s, not the exec-runtime decomposition).
    by_col = {p[0]: p[1] for p in full}
    assert "Completion time" in by_col["completion_median_s"]
    assert "Exec. runtime" not in by_col["completion_median_s"]


def test_compliance_variant_writes_single_panel_file(tmp_path: Path):
    written = registry_degradation_compliance.generate(
        _campaign(tmp_path),
        tmp_path / "out" / "registry-degradation-compliance",
        formats=["pdf"],
        synthetic_preview=True,
    )
    assert len(written) == 1
    assert written[0].stat().st_size > 1024
    assert ".SYNTHETIC." in written[0].name


def _run(root: Path, name: str, baseline: str, variant: str, hp_ca: float) -> Path:
    rd = root / name
    adir = rd / "analysis"
    adir.mkdir(parents=True)
    summary = {"run_id": name, "baseline": baseline, "baselines": {baseline: {
        "intent_compliance": {"hard_pass_rate": hp_ca, "hard_pass_rate_completion_aware": hp_ca},
        "registry_diff": {"variant_id": variant}}}}
    (adir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return rd


def test_render_draws_the_median_of_the_runs_not_the_first(tmp_path: Path):
    """k=3 per cell: the plotted point is the median, the bars span min-max."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dirs = [_run(tmp_path, f"hs-r0-{i}", "heuristic-scorer", "r0", v)
            for i, v in enumerate([0.9, 0.5, 0.7])]  # first run 0.9, median 0.7
    df = registry_degradation._prepare(dirs)
    fig, ax = plt.subplots()
    registry_degradation._render(df, [ax], registry_degradation._select_panels(
        ("hard_pass_rate_completion_aware",)))
    container = ax.containers[0]
    data_line = container.lines[0]
    assert [float(y) for y in data_line.get_ydata()] == [0.7]  # the median, not run 1 (0.9)
    lo, hi = container.lines[2][0].get_segments()[0][:, 1]
    assert (round(lo, 6), round(hi, 6)) == (0.5, 0.9)
    plt.close(fig)


def test_render_leaves_a_cell_with_a_missing_run_undrawn(tmp_path: Path):
    import math
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    a = _run(tmp_path, "df-r0-a", "delphi-full", "r0", 0.7)
    b = tmp_path / "df-r0-b"
    (b / "analysis").mkdir(parents=True)
    (b / "analysis" / "summary.json").write_text(json.dumps({"run_id": "b", "baseline": "delphi-full",
        "baselines": {"delphi-full": {"intent_compliance": {"hard_pass_rate_completion_aware": None},
                                      "registry_diff": {"variant_id": "r0"}}}}), encoding="utf-8")
    df = registry_degradation._prepare([a, b])
    fig, ax = plt.subplots()
    registry_degradation._render(df, [ax], registry_degradation._select_panels(
        ("hard_pass_rate_completion_aware",)))
    assert all(math.isnan(float(y)) for line in ax.lines for y in line.get_ydata())
    plt.close(fig)


def test_paper_panel_uses_the_paper_names(tmp_path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from experiments.orchestrator.analysis.plots import registry_degradation_compliance as rdc

    dirs = [_run(tmp_path, f"{b}-r0", b, "r0", 0.5)
            for b in ("static-karmada", "heuristic-scorer", "delphi-full")]
    captured = {}
    real_render = registry_degradation._render

    def spy(df, axes, spec, labels=None):
        real_render(df, axes, spec, labels)
        captured["legend"] = [t.get_text() for t in axes[0].get_legend().get_texts()]
    registry_degradation._render = spy
    try:
        rdc.generate(dirs, tmp_path / "fig", formats=["png"], show_title=False, height_in=1.6)
    finally:
        registry_degradation._render = real_render
        plt.close("all")
    assert captured["legend"] == ["Static", "Registry heuristic", "Live-state"]
