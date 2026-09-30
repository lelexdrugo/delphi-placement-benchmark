"""iter-4g: matplotlib plot generators for the paper's Section 6.

Each module under this package exposes:

- ``_prepare(run_dirs) -> pandas.DataFrame``  pure data assembly,
- ``_render(df, ax, *, synthetic=False)``     matplotlib side-effects,
- ``generate(run_dirs, out_path, *, formats, synthetic_preview)``
  the orchestrator entry point that ties the two together.

Regression tests pin ``_prepare``, not the PDF bytes, because
matplotlib's PDF output is not byte-deterministic across versions
(font subsetting, embedded timestamps). See ``tests/`` for the D3
pattern.
"""
