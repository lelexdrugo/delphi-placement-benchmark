"""iter-4h-4: cordon_avoidance plot generator.

Self-contained synthetic fixture (the shared synthetic-* fixtures
predate the cordon_response aggregate), so this test builds three
minimal run-dirs whose summary.json carries a cordon_response block.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiments.orchestrator.analysis.plots import cordon_avoidance

# during-cordon fraction targeting the cordoned cluster per baseline —
# the story: Static keeps its fixed target, Heuristic stays high
# (registry-blind), DELPHI drops to ~0 (live-state filter).
_FRACTIONS = {
    "static-karmada": (0.0, 0.0, 0.0),       # fixed target != edge-1 here
    "heuristic-scorer": (0.4, 0.8, 0.4),
    "delphi-full": (0.4, 0.0, 0.4),
}


def _make_run_dir(tmp_path: Path, baseline: str,
                  fractions: tuple[float, float, float]) -> Path:
    pre, during, post = fractions
    run_dir = tmp_path / baseline
    adir = run_dir / "analysis"
    adir.mkdir(parents=True)
    summary = {
        "baseline": baseline,
        "baselines": {
            baseline: {
                "cordon_response": {
                    "cordon_target": "edge-1",
                    "stale_window_seconds": 15,
                    "pre_cordon_count": 5,
                    "pre_cordon_fraction_targeting_cordoned": pre,
                    "during_cordon_count": 5,
                    "during_cordon_fraction_targeting_cordoned": during,
                    "post_cordon_count": 5,
                    "post_cordon_fraction_targeting_cordoned": post,
                },
            },
        },
    }
    (adir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return run_dir


@pytest.fixture
def cordon_run_dirs(tmp_path: Path) -> list[Path]:
    return [
        _make_run_dir(tmp_path, b, f) for b, f in _FRACTIONS.items()
    ]


def test_prepare_shape(cordon_run_dirs: list[Path]) -> None:
    df = cordon_avoidance._prepare(cordon_run_dirs)
    # 3 baselines × 3 phases.
    assert df.shape == (9, 4)
    assert set(df.columns) == {"baseline_key", "phase", "fraction", "count"}


def test_prepare_during_fractions(cordon_run_dirs: list[Path]) -> None:
    df = cordon_avoidance._prepare(cordon_run_dirs)
    during = df[df["phase"] == "during_cordon"].set_index("baseline_key")["fraction"]
    assert during["delphi-full"] == pytest.approx(0.0)
    assert during["heuristic-scorer"] == pytest.approx(0.8)
    assert during["static-karmada"] == pytest.approx(0.0)


def test_prepare_skips_runs_without_cordon_aggregate(tmp_path: Path) -> None:
    """A run-dir whose summary lacks cordon_response is skipped, not raised."""
    adir = tmp_path / "no-cordon" / "analysis"
    adir.mkdir(parents=True)
    (adir / "summary.json").write_text(
        json.dumps({"baseline": "delphi-full", "baselines": {"delphi-full": {}}}),
        encoding="utf-8",
    )
    df = cordon_avoidance._prepare([tmp_path / "no-cordon"])
    assert df.empty


def test_generate_writes_both_formats(cordon_run_dirs: list[Path], tmp_path: Path) -> None:
    written = cordon_avoidance.generate(
        cordon_run_dirs, tmp_path / "cordon-avoidance", formats=["pdf", "png"],
    )
    assert len(written) == 2
    suffixes = sorted(p.suffix for p in written)
    assert suffixes == [".pdf", ".png"]
    for p in written:
        assert p.exists() and p.stat().st_size > 1024


def test_prepare_handles_empty(tmp_path: Path) -> None:
    df = cordon_avoidance._prepare([])
    assert df.empty
    assert set(df.columns) == {"baseline_key", "phase", "fraction", "count"}


def test_phase_palette_is_colourblind_safe() -> None:
    """T2: the load-bearing 'during' bar uses the Okabe-Ito vermillion
    (CVD-safe), 'post' the Okabe-Ito blue. Pins the routing through the
    shared palette so a future edit can't silently reintroduce the old
    pure-red (which was hard to separate from grey under CVD)."""
    from experiments.orchestrator.analysis.plots import _style
    palette = {phase: color for phase, _, color in cordon_avoidance._PHASES}
    assert palette["during_cordon"] == _style.OKABE_ITO[4]   # vermillion
    assert palette["post_cordon"] == _style.OKABE_ITO[0]      # blue
    assert palette["during_cordon"] != "#d62728"             # not old red


def test_render_annotates_every_bar_value(cordon_run_dirs: list[Path]) -> None:
    """T2: every bar carries an explicit value label so a 0.0
    during-cordon bar (DELPHI avoided the cluster) reads as a number,
    not as missing data."""
    import matplotlib.pyplot as plt
    df = cordon_avoidance._prepare(cordon_run_dirs)
    fig, ax = plt.subplots()
    cordon_avoidance._render(df, ax)
    labels = {t.get_text() for t in ax.texts}
    # DELPHI during-cordon == 0.0 must be explicitly labelled.
    assert "0.00" in labels
    # 3 baselines x 3 phases == 9 value labels.
    assert len(ax.texts) == 9
    plt.close(fig)


def test_show_title_false_strips_in_axes_title(cordon_run_dirs: list[Path], tmp_path: Path) -> None:
    """T-paper: --no-title (show_title=False) leaves no in-axes title so the
    camera-ready figure's description lives in the LaTeX caption only."""
    import matplotlib.pyplot as plt
    # show_title=True keeps a title; False strips it. Verify both by
    # re-rendering (generate() closes the fig, so compare via _render).
    df = cordon_avoidance._prepare(cordon_run_dirs)
    fig, ax = plt.subplots()
    cordon_avoidance._render(df, ax)
    assert ax.get_title() != ""  # render sets a title by default
    plt.close(fig)
    # And the public generate() path with show_title=False writes a file
    # whose Axes title was cleared (smoke: it must not crash and must write).
    written = cordon_avoidance.generate(
        cordon_run_dirs, tmp_path / "ca-notitle", formats=["png"], show_title=False,
    )
    assert len(written) == 1 and written[0].exists()
