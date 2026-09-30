# Synthetic fixtures — DO NOT CITE

These directories hold **hand-crafted, fabricated** summary.json + per-metric
CSVs used to exercise the iter-4g plot generators *before* real cross-baseline
data lands in iter-4d.

Concretely:

- `synthetic-static-karmada/analysis/`
- `synthetic-heuristic/analysis/`
- `synthetic-delphi-full/analysis/`

Every file under these paths is **invented for plot-shape testing**.
None of the numbers are observed measurements. None of these results may be
cited, quoted, or referenced in the paper, the presentation, or any external
material.

Once iter-4d's three real baseline `summary.json`s exist
(`experiments/runs/phase-b-effectiveness-{static-karmada,heuristic,delphi-full}-<ts>/`),
the same CLI commands re-render the figures from real data; these fixtures
remain only as the test substrate.

Filenames of the previews emitted from these fixtures carry the literal
`.SYNTHETIC` suffix (e.g. `placement-effectiveness.SYNTHETIC.pdf`) and live
under `docs/.../figs/_synthetic-previews/` — they cannot resolve via
`\includegraphics{figs/placement-effectiveness}` by accident.
