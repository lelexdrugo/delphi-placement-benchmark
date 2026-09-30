"""Shared helpers for figure emission.

Two responsibilities:

- ``save_figure``: write a Figure to PDF / PNG / both under the
  caller-supplied path, returning the list of written files.
- ``add_synthetic_watermark``: stamp a Figure with an unmissable
  "SYNTHETIC — DO NOT CITE" overlay so a P3-track preview cannot be
  mistaken for citable data.

The watermark is intentionally invasive: a diagonal red overlay
covering the centre of the figure, plus a footer line. Both survive
file rename (the bytes are in the PDF/PNG itself, not the filename).
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.figure import Figure


VALID_FORMATS: frozenset[str] = frozenset({"pdf", "png"})


def clear_titles(fig: Figure) -> None:
    """Strip in-axes titles + suptitle from a Figure.

    Paper figures carry their description in the LaTeX ``\\caption``, not
    in an in-axes title that would otherwise duplicate it (and often
    encodes iteration-specific phrasing). Generators call this when
    ``show_title=False`` so the same generator serves both the analysis
    preview (title on) and the camera-ready figure (title off).
    """
    for ax in fig.axes:
        ax.set_title("")
    # suptitle lives on a private attr; setting an empty one is a no-op
    # when none exists and clears it when it does.
    if getattr(fig, "_suptitle", None) is not None:
        fig.suptitle("")


def add_synthetic_watermark(fig: Figure, *, footer: str | None = None) -> None:
    """Stamp ``fig`` with a SYNTHETIC overlay + footer.

    The overlay text sits centred over the axes area at ~18% opacity
    so the chart remains readable but no reader can miss the warning.
    The footer (also red) restates the caveat in print-readable size.
    """
    fig.text(
        0.5, 0.5,
        "SYNTHETIC\nDO NOT CITE",
        ha="center", va="center",
        rotation=30,
        fontsize=44,
        color="red",
        alpha=0.18,
        weight="bold",
        transform=fig.transFigure,
        zorder=1000,
    )
    footer_text = footer or "synthetic data — not for citation"
    # Footer above the figure (under suptitle) so it doesn't overlap
    # axis labels on the bottom edge.
    fig.text(
        0.99, 0.985,
        footer_text,
        ha="right", va="top",
        fontsize=7,
        color="red",
        alpha=0.8,
        transform=fig.transFigure,
        zorder=1000,
    )


def save_figure(
    fig: Figure,
    out_path: Path,
    *,
    formats: list[str],
    synthetic_preview: bool = False,
) -> list[Path]:
    """Write ``fig`` to PDF / PNG / both.

    Args:
        fig: the Figure to write.
        out_path: target stem. The suffix (``.pdf``, ``.png``,
            ``.SYNTHETIC.pdf``...) is appended by this helper.
        formats: subset of {"pdf", "png"}.
        synthetic_preview: when True, inject ``.SYNTHETIC`` before
            the extension so a glob like ``*.pdf`` in the proposal's
            figs/ folder will not match preview output.

    Returns the list of files written, in the order ``formats`` was
    given.
    """
    invalid = set(formats) - VALID_FORMATS
    if invalid:
        raise ValueError(f"unsupported format(s) {sorted(invalid)}; allow {sorted(VALID_FORMATS)}")

    # Build the stem manually so a ".SYNTHETIC" segment is not
    # treated as a Path suffix and overwritten by with_suffix("pdf").
    stem_name = out_path.name
    if stem_name.lower().endswith((".pdf", ".png")):
        stem_name = stem_name.rsplit(".", 1)[0]
    if synthetic_preview and not stem_name.endswith(".SYNTHETIC"):
        stem_name = stem_name + ".SYNTHETIC"
    parent = out_path.parent
    parent.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for fmt in formats:
        target = parent / f"{stem_name}.{fmt}"
        fig.savefig(target, format=fmt)
        written.append(target)
    plt.close(fig)
    return written
