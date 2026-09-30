"""PostgreSQL access from the analyzer.

Wraps a single psycopg connection per analysis pass. Reads named
queries from ``queries.sql`` so that every SQL string lives in one
place (and every divergence from the production filter shape is
immediately visible by diff).

Idempotent port-forward integration: when ``auto_portforward=True``
the constructor invokes ``experiments/db/port-forward.ps1`` (or the
``.sh`` sibling on POSIX) before opening the connection. The helper
is itself idempotent — re-runs are cheap.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import psycopg  # type: ignore[import-not-found]
    _HAS_PSYCOPG = True
except ImportError:  # pragma: no cover - optional dep
    psycopg = None  # type: ignore[assignment]
    _HAS_PSYCOPG = False

try:
    from dotenv import dotenv_values  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dep
    dotenv_values = None  # type: ignore[assignment]


_NAME_RE = re.compile(r"^-- name:\s*(\w+)\s*$", re.MULTILINE)


def _read_queries(sql_path: Path) -> dict[str, str]:
    """Parse a queries.sql file into {name: sql} entries.

    Splits on ``-- name: <id>`` markers. Text before the first marker
    is ignored (header / docstring). Each query keeps every comment
    line that follows its marker (so SQL hints stay intact).
    """
    raw = sql_path.read_text(encoding="utf-8")
    pieces: dict[str, str] = {}
    parts = _NAME_RE.split(raw)
    # split returns [pre, name1, body1, name2, body2, ...]
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        pieces[name] = body
    return pieces


def _load_env(env_file: Optional[Path]) -> dict[str, str]:
    """Return key/value pairs from a .env file, falling back to os.environ."""
    if env_file is None or not env_file.exists():
        return {k: v for k, v in os.environ.items()}
    if dotenv_values is None:
        # Minimal fallback parser when python-dotenv is not installed.
        out: dict[str, str] = {}
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
        return out
    raw = dotenv_values(env_file)
    return {k: v or "" for k, v in raw.items()}


def _run_portforward_helper(repo_root: Path) -> int:
    """Best-effort idempotent port-forward.

    On Windows uses PowerShell + port-forward.ps1. On POSIX uses the
    .sh sibling. Errors are surfaced but not raised — the connection
    attempt will fail loudly if the helper didn't establish the
    tunnel.
    """
    helper_dir = repo_root / "experiments" / "db"
    if platform.system() == "Windows":
        helper = helper_dir / "port-forward.ps1"
        if not helper.exists():
            return 0
        cmd = ["pwsh", "-NoProfile", "-File", str(helper)]
    else:
        helper = helper_dir / "port-forward.sh"
        if not helper.exists():
            return 0
        cmd = ["bash", str(helper)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(
            f"[analysis.db] port-forward helper exit={proc.returncode}\n"
            f"stderr: {proc.stderr.strip()}\n"
        )
    return proc.returncode


def _conninfo(env: dict[str, str]) -> str:
    host = env.get("DB_HOST") or "localhost"
    port = env.get("DB_PORT") or "5432"
    db   = env.get("DB_NAME") or "jobmetricsdb"
    user = env.get("DB_USER") or "pguser"
    pw   = env.get("DB_PASSWORD") or ""
    # libpq URI-style conninfo so passwords with special chars are tolerated
    # via psycopg's parsing.
    return (
        f"host={host} port={port} dbname={db} user={user} password={pw}"
    )


class AnalyzerDB:
    """Thin wrapper exposing only ``run_query``."""

    def __init__(self,
                 *,
                 queries_path: Path,
                 env_file: Optional[Path] = None,
                 auto_portforward: bool = True,
                 repo_root: Optional[Path] = None) -> None:
        if not _HAS_PSYCOPG:
            raise RuntimeError(
                "psycopg is required for DB-backed analysis. Install with "
                "`pip install 'psycopg[binary]>=3.1'` or pass --no-db.")
        self._queries = _read_queries(queries_path)
        self._env = _load_env(env_file)
        if auto_portforward and repo_root is not None:
            _run_portforward_helper(repo_root)
        self._conn = psycopg.connect(_conninfo(self._env))

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def run_query(self, name: str, *binds: Any) -> list[dict[str, Any]]:
        sql = self._queries.get(name)
        if sql is None:
            raise KeyError(f"unknown query: {name!r}")
        with self.cursor() as cur:
            cur.execute(sql, binds)
            cols = [d.name for d in (cur.description or [])]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


class StubDB:
    """No-op DB used when --no-db is set or psycopg is missing."""

    def __init__(self) -> None:
        self._stub_rows: dict[str, list[dict[str, Any]]] = {}

    def set_rows(self, name: str, rows: list[dict[str, Any]]) -> None:
        """Used by unit tests to inject canned responses."""
        self._stub_rows[name] = rows

    def close(self) -> None:
        return None

    def run_query(self, name: str, *binds: Any) -> list[dict[str, Any]]:  # noqa: ARG002
        return list(self._stub_rows.get(name, []))
