"""Unit tests for ``submit._job_status`` JSON -> status mapping.

iter-4-refinements (B): pins the conditions-first detection plus the
counter/timestamp fallback that makes completion detection robust to a
partially-mirrored aggregated Karmada Job status (the iter-4h-3 r3/hs
stall, where DB showed COMPLETED and .status.succeeded was 1 but the
conditions[] array was empty, hung the poll to the deadline).
"""
from __future__ import annotations

import json
import subprocess

from experiments.orchestrator import submit as _submit


def _patch_kubectl(monkeypatch, status_obj: dict | None, *, returncode: int = 0):
    """Make _kubectl return a CompletedProcess wrapping {"status": status_obj}."""
    payload = json.dumps({"status": status_obj}) if status_obj is not None else "{}"

    def fake_kubectl(_args, *, timeout=None):
        return subprocess.CompletedProcess(
            args=["kubectl"], returncode=returncode, stdout=payload, stderr="",
        )

    monkeypatch.setattr(_submit, "_kubectl", fake_kubectl)


def _status(monkeypatch, status_obj, **kw) -> str:
    _patch_kubectl(monkeypatch, status_obj, **kw)
    return _submit._job_status("unique-logical-entrypoint", "delphi-experiments", "j")


def test_conditions_complete(monkeypatch):
    assert _status(monkeypatch, {
        "conditions": [{"type": "Complete", "status": "True"}],
        "succeeded": 1,
    }) == "Complete"


def test_conditions_failed(monkeypatch):
    assert _status(monkeypatch, {
        "conditions": [{"type": "Failed", "status": "True"}],
        "failed": 1,
    }) == "Failed"


def test_fallback_succeeded_without_conditions(monkeypatch):
    """The iter-4h-3 r3/hs case: succeeded=1 but conditions[] empty/stale."""
    assert _status(monkeypatch, {"succeeded": 1}) == "Complete"


def test_fallback_completion_time_without_conditions(monkeypatch):
    """A present completionTime (even if odd/skewed) means Complete; we
    check presence, never arithmetic."""
    assert _status(monkeypatch, {
        "completionTime": "2026-05-25T21:09:35Z",
    }) == "Complete"


def test_fallback_failed_without_conditions(monkeypatch):
    assert _status(monkeypatch, {"failed": 1}) == "Failed"


def test_active_without_conditions(monkeypatch):
    assert _status(monkeypatch, {"active": 1}) == "Active"


def test_succeeded_wins_over_active_when_both_present(monkeypatch):
    """A terminal succeeded count outranks a lingering active counter."""
    assert _status(monkeypatch, {"active": 1, "succeeded": 1}) == "Complete"


def test_empty_status_is_unknown(monkeypatch):
    assert _status(monkeypatch, {}) == "Unknown"


def test_nonzero_returncode_is_unknown(monkeypatch):
    assert _status(monkeypatch, {"succeeded": 1}, returncode=1) == "Unknown"


def test_timeout_twice_is_unknown(monkeypatch):
    def always_timeout(_args, *, timeout=None):
        raise subprocess.TimeoutExpired(cmd="kubectl", timeout=timeout or 30)

    monkeypatch.setattr(_submit, "_kubectl", always_timeout)
    assert _submit._job_status("unique-logical-entrypoint", "delphi-experiments", "j") == "Unknown"
