#!/usr/bin/env python3
"""iter-4i cost-grounding ablation — per-intent compliance comparison figure.

Reads two run-dirs' `analysis/intent-compliance.csv` (DELPHI-registry-blind =
flag OFF, DELPHI-cost-aware = flag ON, same image + spec + seed, only the
`DELPHI_ENABLE_COST_GROUNDING` env differs) and emits the RQ3 cost-grounding
figure: per-intent `hard_pass` rate + an isolated cost-component pass rate
(does the selected cluster's cost label satisfy the requested cost ceiling on
the low<medium<high ladder, treating `any` as always-satisfied).

The cost signal is expected to lift the cost-axis intents (`cost-aware`,
`balanced`); region-gated intents (`latency-sensitive`, `locality-aware`)
should be unchanged. Run with one arm (just --costaware) to inspect a single
run; with both arms to render the comparison.

Usage:
    python experiments/analysis/cost_ablation_compare.py \
        --costaware experiments/runs/ca-<UTC> \
        --regblind  experiments/runs/rb-<UTC> \
        --out experiments/figs/iter-4i/cost-grounding-ablation

Standalone (csv + matplotlib only); not wired into the orchestrator CLI yet.
"""
from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict

_COST_LADDER = {"low": 0, "medium": 1, "high": 2}
# Stable intent order; cost-axis intents first (the ones cost-grounding targets).
_INTENT_ORDER = ["cost-aware", "balanced", "latency-sensitive", "locality-aware"]


def _cost_component_pass(req_cost: str, cluster_cost: str) -> bool:
    """True iff the selected cluster's cost satisfies the requested ceiling.

    `any` ceiling always passes. Unknown labels fail closed.
    """
    req = (req_cost or "").strip().lower()
    if req in ("", "any"):
        return True
    c = _COST_LADDER.get((cluster_cost or "").strip().lower())
    r = _COST_LADDER.get(req)
    if c is None or r is None:
        return False
    return c <= r


def _load(run_dir: str) -> list[dict]:
    path = os.path.join(run_dir, "analysis", "intent-compliance.csv")
    if not os.path.isfile(path):
        raise SystemExit(f"missing {path} — run `analysis` on {run_dir} first")
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def _per_intent(rows: list[dict]) -> dict[str, dict]:
    """intent -> {n, hard_pass_rate, cost_pass_rate}."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # hard, cost, n
    for r in rows:
        intent = r.get("intent_profile", "?")
        a = agg[intent]
        a[2] += 1
        try:
            if float(r.get("hard_pass", 0)) >= 1.0:
                a[0] += 1
        except ValueError:
            pass
        if _cost_component_pass(r.get("req_cost", ""), r.get("cluster_cost_label", "")):
            a[1] += 1
    out = {}
    for intent, (hard, cost, n) in agg.items():
        out[intent] = {
            "n": n,
            "hard_pass": hard,
            "cost_pass": cost,
            "hard_pass_rate": hard / n if n else 0.0,
            "cost_pass_rate": cost / n if n else 0.0,
        }
    return out


def _ordered_intents(*per_intent_maps: dict) -> list[str]:
    seen = set()
    for m in per_intent_maps:
        seen |= set(m)
    ordered = [i for i in _INTENT_ORDER if i in seen]
    ordered += sorted(i for i in seen if i not in _INTENT_ORDER)
    return ordered


def _print_table(arms: dict[str, dict]) -> None:
    intents = _ordered_intents(*arms.values())
    print("\nPer-intent hard_pass / cost-component pass (n):")
    header = "  intent             " + "".join(f"| {a:>22} " for a in arms)
    print(header)
    for intent in intents:
        cells = ""
        for a, pm in arms.items():
            d = pm.get(intent)
            cells += (f"| hard {d['hard_pass_rate']:.2f} cost {d['cost_pass_rate']:.2f} (n={d['n']:2d}) "
                      if d else "| " + " " * 22)
        print(f"  {intent:18s} {cells}")


def _plot(arms: dict[str, dict], out_stem: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    intents = _ordered_intents(*arms.values())
    arm_names = list(arms)
    x = np.arange(len(intents))
    width = 0.8 / max(len(arm_names), 1)
    # colorblind-safe (Okabe-Ito): orange = registry-blind, blue = cost-aware.
    palette = {"DELPHI-registry-blind": "#E69F00", "DELPHI-cost-aware": "#0072B2"}
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, metric, count_key, title in (
        (axes[0], "hard_pass_rate", "hard_pass", "Two-tier hard_pass"),
        (axes[1], "cost_pass_rate", "cost_pass", "Cost-component compliance"),
    ):
        for i, a in enumerate(arm_names):
            xs = x + (i - (len(arm_names) - 1) / 2) * width
            vals = [arms[a].get(intent, {}).get(metric, 0.0) for intent in intents]
            ax.bar(xs, vals, width, label=a, color=palette.get(a, None),
                   edgecolor="black", linewidth=0.4)
            # annotate each bar with pass/n so the reader sees the absolute counts
            for xi, intent in zip(xs, intents):
                d = arms[a].get(intent)
                if not d:
                    continue
                v = d.get(metric, 0.0)
                ax.text(xi, v + 0.02, f"{d[count_key]}/{d['n']}",
                        ha="center", va="bottom", fontsize=7, rotation=90)
        ax.set_title(title)
        ax.set_xticks(x)
        ax.set_xticklabels(intents, rotation=20, ha="right")
        ax.set_ylim(0, 1.12)
        ax.grid(axis="y", alpha=0.3)
    axes[0].set_ylabel("pass rate")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.925),
               ncol=2, fontsize=9, framealpha=0.9)
    fig.suptitle("iter-4i cost grounding — per-intent compliance (DELPHI-Full, N=1, idle, task-bound)")
    fig.text(0.5, 0.005,
             "N=1; region-gated intents (latency/locality) unaffected by design. The 'balanced' difference "
             "is a 3-job count difference within crew non-determinism — not a significance claim. "
             "Panels coincide where cost is the binding sub-gate.",
             ha="center", va="bottom", fontsize=7.5, style="italic", wrap=True)
    fig.tight_layout(rect=(0, 0.06, 1, 0.96))
    os.makedirs(os.path.dirname(out_stem) or ".", exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(f"{out_stem}.{ext}", dpi=150)
        print(f"wrote {out_stem}.{ext}")
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--costaware", required=True, help="run-dir of the flag-ON arm")
    ap.add_argument("--regblind", default=None, help="run-dir of the flag-OFF arm (optional for single-arm inspection)")
    ap.add_argument("--out", default="experiments/figs/iter-4i/cost-grounding-ablation")
    ap.add_argument("--no-plot", action="store_true", help="print the table only")
    args = ap.parse_args()

    arms: dict[str, dict] = {}
    if args.regblind:
        arms["DELPHI-registry-blind"] = _per_intent(_load(args.regblind))
    arms["DELPHI-cost-aware"] = _per_intent(_load(args.costaware))

    _print_table(arms)
    if not args.no_plot:
        _plot(arms, args.out)


if __name__ == "__main__":
    main()
