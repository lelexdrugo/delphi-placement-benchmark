"""Entry point for `python -m experiments.orchestrator <subcommand>`.

iter-4a added this thin wrapper so both forms work:

  python -m experiments.orchestrator analysis --help
  python -m experiments.orchestrator.cli analysis --help

The longer ``.cli`` form remains valid and is what iter-4b's
RUNBOOK section documents — see
``experiments/calibration/MERGE-NOTES.md`` for the rationale.
Both invocations delegate to the same ``cli.main`` function, so
they are semantically identical.
"""
from __future__ import annotations

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
