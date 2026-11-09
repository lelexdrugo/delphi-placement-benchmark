"""Entry point for `python -m experiments.orchestrator <subcommand>`.

This thin wrapper makes both forms work:

  python -m experiments.orchestrator analysis --help
  python -m experiments.orchestrator.cli analysis --help

Both invocations delegate to the same ``cli.main`` function, so
they are semantically identical.
"""
from __future__ import annotations

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
