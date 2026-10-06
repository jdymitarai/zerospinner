"""Tests for ZeroSpinner Watchdog auto-teardown and completion lifecycle."""

import json
import os
import sys
import time
from pathlib import Path
import pytest

from zerospinner.core.milestone import (
    MilestoneDetector,
    MilestoneEvent,
    MILESTONE_PR_CREATED,
    MILESTONE_TESTS_PASSED,
)
from zerospinner.core.watchdog import TranscriptWatchdog
from zerospinner.mcp.server import handle_teardown, ZeroSpinnerMCPService


def test_watchdog_completion_callback(temp_transcript: Path):
    """Verify that when a PR is created, TranscriptWatchdog triggers on_completion."""
    detector = MilestoneDetector()
    watchdog = TranscriptWatchdog(
        transcript_path=temp_transcript,
        poll_interval=0.05,
        detector=detector,
    )

    completed_events = []
    watchdog.on_completion(lambda evt: completed_events.append(evt))

    # Append test pass (should NOT trigger completion callback)
    with open(temp_transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps({"content": "Ran 5 tests: 5 passed in 0.2s"}) + "\n")

    watchdog.step()
    assert len(completed_events) == 0

    # Append PR creation (SHOULD trigger completion callback)
    with open(temp_transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps({"content": "Created PR: https://github.com/openxla/xla/pull/50104"}) + "\n")

    watchdog.step()
    assert len(completed_events) == 1
    assert completed_events[0].milestone_type == MILESTONE_PR_CREATED
    assert "50104" in completed_events[0].raw_text


def test_mcp_teardown_handler():
    """Verify zerospinner_teardown handler executes cleanly without errors."""
    raw_res = handle_teardown(stop_cloud=False, reason="Unit test teardown")
    res = json.loads(raw_res)
    assert res["status"] == "teardown_complete"
    assert res["reason"] == "Unit test teardown"
    assert "subagents_killed" in res


def test_daemon_stop_flag_lifecycle(tmp_path: Path, monkeypatch):
    """Verify stop flag detection in watchdog daemon."""
    import watchdog_daemon

    test_pid_file = tmp_path / ".test.pid"
    test_stop_file = tmp_path / ".test.stop"

    test_pid_file.write_text("99999", encoding="utf-8")
    test_stop_file.write_text("stop", encoding="utf-8")

    monkeypatch.setattr(watchdog_daemon, "PID_FILE", test_pid_file)
    monkeypatch.setattr(watchdog_daemon, "STOP_FLAG_FILE", test_stop_file)

    # Perform teardown (with stop_cloud=False to avoid touching gcloud in unit test)
    ok = watchdog_daemon.perform_teardown(reason="Test teardown", stop_cloud=False)
    assert ok is True
    assert not test_pid_file.exists()
    assert not test_stop_file.exists()
