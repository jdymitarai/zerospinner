"""Tests for Model Context Protocol (MCP) server and tool handlers."""

import json
from pathlib import Path
import pytest

from zerospinner.core.milestone import MILESTONE_TESTS_PASSED, MILESTONE_PR_CREATED
from zerospinner.mcp.server import (
    ZeroSpinnerMCPService,
    create_mcp_server,
    handle_emit_milestone,
    handle_status,
    handle_trip_breaker,
    handle_watch_session,
    handle_teardown,
)


def test_mcp_service_operations(temp_transcript: Path):
    service = ZeroSpinnerMCPService()

    # 1. Watch session
    res_watch = service.watch_session(str(temp_transcript), poll_interval=0.1)
    assert res_watch["status"] == "watching"
    assert res_watch["transcript_path"] == str(temp_transcript)
    assert service.watchdog is not None

    # 2. Emit milestone
    res_emit = service.emit_milestone(
        milestone_type=MILESTONE_TESTS_PASSED,
        payload="14 passed in 0.2s",
        metadata={"passed_count": 14},
    )
    assert res_emit["status"] == "emitted"
    assert res_emit["has_core_milestone"] is True

    # 3. Status check
    status = service.status()
    assert len(status["active_milestones"]) == 1
    assert status["is_tripped"] is False

    # 4. Trip breaker
    res_trip = service.trip_breaker(reason="USER_INTERVENTION", details="Test emergency stop")
    assert res_trip["status"] == "tripped"
    assert res_trip["trip_reason"] == "USER_INTERVENTION"
    assert service.status()["is_tripped"] is True

    service.watchdog.stop()


def test_zerospinner_watch_session(temp_transcript: Path):
    raw = handle_watch_session(str(temp_transcript))
    parsed = json.loads(raw)
    assert parsed["status"] == "watching"
    assert parsed["transcript_path"] == str(temp_transcript)


def test_zerospinner_emit_milestone():
    raw = handle_emit_milestone(MILESTONE_PR_CREATED, "https://github.com/org/repo/pull/1")
    parsed = json.loads(raw)
    assert parsed["status"] == "emitted"
    assert "milestone" in parsed


def test_zerospinner_status():
    raw = handle_status()
    parsed = json.loads(raw)
    assert "breaker" in parsed
    assert "active_milestones" in parsed
    assert "is_tripped" in parsed


def test_zerospinner_trip_breaker():
    raw = handle_trip_breaker("MANUAL_DEMO")
    parsed = json.loads(raw)
    assert parsed["status"] == "tripped"
    assert parsed["trip_reason"] == "MANUAL_DEMO"


def test_zerospinner_teardown():
    raw = handle_teardown(stop_cloud=False, reason="Test teardown")
    parsed = json.loads(raw)
    assert parsed["status"] == "teardown_complete"
    assert parsed["reason"] == "Test teardown"


def test_fastmcp_registration():
    server = create_mcp_server()
    assert server is not None
    assert server.name == "ZeroSpinner"
