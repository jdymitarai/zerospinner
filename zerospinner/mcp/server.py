"""Standard MCP (Model Context Protocol) Server for ZeroSpinner."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from zerospinner.core.breaker import BreakerState, CircuitBreaker, TripReason
from zerospinner.core.milestone import MilestoneDetector, MilestoneEvent, MilestoneType
from zerospinner.core.watchdog import TranscriptWatchdog

logger = logging.getLogger(__name__)


class ZeroSpinnerMCPService:
    """Service backend managing engine components for MCP tool calls."""

    def __init__(self) -> None:
        self.detector = MilestoneDetector()
        self.breaker = CircuitBreaker()
        self.watchdog: Optional[TranscriptWatchdog] = None

        # Wire milestone detector to circuit breaker
        self.detector.on_milestone(self.breaker.record_milestone)

    def watch_session(self, transcript_path: str, poll_interval: float = 2.0) -> Dict[str, Any]:
        """Start or re-target active transcript watchdog for a session."""
        if self.watchdog is not None:
            self.watchdog.stop()

        self.watchdog = TranscriptWatchdog(
            transcript_path=transcript_path,
            poll_interval=poll_interval,
            detector=self.detector,
        )

        # Wire stall/loop alerts to circuit breaker
        self.watchdog.on_alert(
            lambda alert: self.breaker.trip(
                TripReason.STALL_TIMEOUT if alert.alert_type == "STALL" else TripReason.LOOP_DEADLOCK,
                alert.details,
            )
        )

        self.watchdog.start()
        return {
            "status": "watching",
            "transcript_path": transcript_path,
            "poll_interval": poll_interval,
            "message": f"ZeroSpinner watchdog attached to transcript '{transcript_path}'.",
        }

    def emit_milestone(
        self,
        milestone_type: str,
        payload: str = "",
        source: str = "mcp_tool",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Preemptively emit a milestone event."""
        event = MilestoneEvent(
            milestone_type=milestone_type,
            source=source,
            raw_text=payload,
            metadata=metadata or {},
        )
        self.detector.emit(event)
        self.breaker.record_milestone(event)

        return {
            "status": "emitted",
            "milestone": event.to_dict(),
            "has_core_milestone": self.breaker.has_core_milestone(),
            "breaker_state": self.breaker.state.value,
        }

    def status(self) -> Dict[str, Any]:
        """Retrieve full health, telemetry, and circuit breaker status."""
        breaker_summary = self.breaker.get_summary()
        telemetry_summary = self.watchdog.get_telemetry().to_dict() if self.watchdog else None

        return {
            "breaker": breaker_summary,
            "telemetry": telemetry_summary,
            "active_milestones": [m.to_dict() for m in self.detector.get_milestones()],
            "is_tripped": self.breaker.is_tripped(),
        }

    def trip_breaker(self, reason: str = "MANUAL_OVERRIDE", details: str = "Operator manual trip") -> Dict[str, Any]:
        """Immediately trip the circuit breaker and terminate speculative subagents."""
        self.breaker.trip(reason, details)
        return {
            "status": "tripped",
            "trip_reason": reason,
            "trip_details": details,
            "subagents_terminated": len(self.breaker.subagents),
        }

    def teardown(self, stop_cloud: bool = True, reason: str = "Teardown requested") -> Dict[str, Any]:
        """Tear down all background processes and optionally stop cloud VMs."""
        if self.watchdog is not None:
            self.watchdog.stop()
        subagents_killed = self.breaker.terminate_subagents()
        cloud_report = {}
        if stop_cloud:
            try:
                import subprocess
                import sys
                from pathlib import Path

                dispatch_py = Path("c:/ai/dispatch.py")
                if dispatch_py.exists():
                    res = subprocess.run(
                        [sys.executable, str(dispatch_py), "--stop"],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    cloud_report = {"output": res.stdout.strip()[:200]}
            except Exception as e:
                cloud_report = {"error": str(e)}
        return {
            "status": "teardown_complete",
            "reason": reason,
            "subagents_killed": subagents_killed,
            "cloud_teardown": cloud_report,
        }


# Global singleton instance for MCP server execution
_SERVICE = ZeroSpinnerMCPService()


def get_service() -> ZeroSpinnerMCPService:
    """Return the global ZeroSpinner MCP service instance."""
    return _SERVICE


def handle_watch_session(transcript_path: str, poll_interval: float = 2.0) -> str:
    """Tool handler: zerospinner_watch_session."""
    result = _SERVICE.watch_session(transcript_path, poll_interval)
    return json.dumps(result, indent=2)


def handle_emit_milestone(type: str, payload: str = "", source: str = "mcp_tool") -> str:
    """Tool handler: zerospinner_emit_milestone."""
    result = _SERVICE.emit_milestone(milestone_type=type, payload=payload, source=source)
    return json.dumps(result, indent=2)


def handle_status() -> str:
    """Tool handler: zerospinner_status."""
    result = _SERVICE.status()
    return json.dumps(result, indent=2)


def handle_trip_breaker(reason: str = "MANUAL_OVERRIDE") -> str:
    """Tool handler: zerospinner_trip_breaker."""
    result = _SERVICE.trip_breaker(reason=reason, details="Operator requested immediate stop and delivery.")
    return json.dumps(result, indent=2)


def handle_teardown(stop_cloud: bool = True, reason: str = "Teardown requested") -> str:
    """Tool handler: zerospinner_teardown."""
    result = _SERVICE.teardown(stop_cloud=stop_cloud, reason=reason)
    return json.dumps(result, indent=2)


def create_mcp_server():
    """Create MCP server instance supporting both modern mcp 2.x MCPServer and legacy FastMCP."""
    try:
        try:
            from mcp.server.mcpserver import MCPServer
            mcp = MCPServer("ZeroSpinner")
            is_v2 = True
        except ImportError:
            from mcp.server.fastmcp import FastMCP
            mcp = FastMCP("ZeroSpinner", dependencies=["zerospinner", "rich"])
            is_v2 = False

        # Try importing ToolAnnotations for M8ven hints
        try:
            from mcp.types import ToolAnnotations
        except ImportError:
            ToolAnnotations = None

        def _ann(read_only: bool, destructive: bool, idempotent: bool, open_world: bool):
            if ToolAnnotations is not None:
                try:
                    return ToolAnnotations(
                        readOnlyHint=read_only,
                        destructiveHint=destructive,
                        idempotentHint=idempotent,
                        openWorldHint=open_world,
                    )
                except Exception:
                    pass
            return None

        # 1. zerospinner_watch_session
        ann1 = _ann(read_only=False, destructive=False, idempotent=True, open_world=False)
        kw1 = {"annotations": ann1} if ann1 and is_v2 else {}
        @mcp.tool(**kw1)
        def zerospinner_watch_session(transcript_path: str, poll_interval: float = 2.0) -> str:
            """Attach ZeroSpinner watchdog to an active agent transcript file to monitor stalls and loops."""
            return handle_watch_session(transcript_path, poll_interval)

        # 2. zerospinner_emit_milestone
        ann2 = _ann(read_only=False, destructive=False, idempotent=False, open_world=False)
        kw2 = {"annotations": ann2} if ann2 and is_v2 else {}
        @mcp.tool(**kw2)
        def zerospinner_emit_milestone(type: str, payload: str = "", source: str = "agent") -> str:
            """Preemptively emit a milestone (e.g. MILESTONE_TESTS_PASSED, MILESTONE_PR_CREATED) to trigger fast delivery."""
            return handle_emit_milestone(type, payload, source)

        # 3. zerospinner_status
        ann3 = _ann(read_only=True, destructive=False, idempotent=True, open_world=False)
        kw3 = {"annotations": ann3} if ann3 and is_v2 else {}
        @mcp.tool(**kw3)
        def zerospinner_status() -> str:
            """Query real-time ZeroSpinner status: circuit breaker state, telemetry, and subagent hierarchy."""
            return handle_status()

        # 4. zerospinner_trip_breaker
        ann4 = _ann(read_only=False, destructive=True, idempotent=True, open_world=False)
        kw4 = {"annotations": ann4} if ann4 and is_v2 else {}
        @mcp.tool(**kw4)
        def zerospinner_trip_breaker(reason: str = "MANUAL_OVERRIDE") -> str:
            """Immediately trip the circuit breaker, stopping infinite spinner and speculative subagent loops."""
            return handle_trip_breaker(reason)

        # 5. zerospinner_teardown
        ann5 = _ann(read_only=False, destructive=True, idempotent=True, open_world=False)
        kw5 = {"annotations": ann5} if ann5 and is_v2 else {}
        @mcp.tool(**kw5)
        def zerospinner_teardown(stop_cloud: bool = True, reason: str = "Teardown requested") -> str:
            """Tear down all background processes, release cloud compute resources, and clean up completely."""
            return handle_teardown(stop_cloud=stop_cloud, reason=reason)

        return mcp
    except ImportError:
        logger.warning("mcp package is not installed; MCP server cannot be initialized.")
        return None


def run_mcp_server() -> None:
    """Run MCP server over standard I/O."""
    server = create_mcp_server()
    if server is None:
        raise RuntimeError("The 'mcp' package is required to run the MCP server. Install via pip install zerospinner[mcp].")
    server.run(transport="stdio")
