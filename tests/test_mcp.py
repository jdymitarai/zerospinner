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


def test_mcp_handlers(temp_transcript: Path):
    # Test json string output of top-level handler functions
    raw_watch = handle_watch_session(str(temp_transcript))
    parsed_watch = json.loads(raw_watch)
    assert parsed_watch["status"] == "watching"

    raw_emit = handle_emit_milestone(MILESTONE_PR_CREATED, "https://github.com/org/repo/pull/1")
    parsed_emit = json.loads(raw_emit)
    assert parsed_emit["status"] == "emitted"

    raw_status = handle_status()
    parsed_status = json.loads(raw_status)
    assert "breaker" in parsed_status

    raw_trip = handle_trip_breaker("MANUAL_DEMO")
    parsed_trip = json.loads(raw_trip)
    assert parsed_trip["status"] == "tripped"


def test_fastmcp_registration():
    server = create_mcp_server()
    assert server is not None
    assert server.name == "ZeroSpinner"


def test_zerospinner_watch_session(temp_transcript: Path):
    """Dedicated test for zerospinner_watch_session MCP tool."""
    raw = handle_watch_session(str(temp_transcript), poll_interval=1.0)
    data = json.loads(raw)
    assert data["status"] == "watching"
    assert data["transcript_path"] == str(temp_transcript)


def test_zerospinner_emit_milestone():
    """Dedicated test for zerospinner_emit_milestone MCP tool."""
    raw = handle_emit_milestone(type=MILESTONE_TESTS_PASSED, payload="10 passed")
    data = json.loads(raw)
    assert data["status"] == "emitted"
    assert data["milestone"]["milestone_type"] == MILESTONE_TESTS_PASSED


def test_zerospinner_status():
    """Dedicated test for zerospinner_status MCP tool."""
    raw = handle_status()
    data = json.loads(raw)
    assert "breaker" in data
    assert "active_milestones" in data


def test_zerospinner_trip_breaker():
    """Dedicated test for zerospinner_trip_breaker MCP tool."""
    raw = handle_trip_breaker(reason="TEST_TRIP")
    data = json.loads(raw)
    assert data["status"] == "tripped"
    assert data["trip_reason"] == "TEST_TRIP"


def test_zerospinner_teardown():
    """Dedicated test for zerospinner_teardown MCP tool."""
    from zerospinner.mcp.server import handle_teardown
    raw = handle_teardown(stop_cloud=False, reason="Test teardown")
    data = json.loads(raw)
    assert data["status"] == "teardown_complete"
    assert data["reason"] == "Test teardown"

