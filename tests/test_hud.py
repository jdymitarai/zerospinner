"""Tests for Glassbox Terminal HUD rendering and component composition."""

from rich.console import Console

from zerospinner.core.breaker import CircuitBreaker, TripReason
from zerospinner.core.milestone import MilestoneDetector, MilestoneEvent, MILESTONE_TESTS_PASSED
from zerospinner.core.watchdog import TranscriptWatchdog, WatchdogAlert, WatchdogTelemetry
from zerospinner.ui.hud import GlassboxHUD


def test_hud_initialization_and_snapshot():
    detector = MilestoneDetector()
    breaker = CircuitBreaker()
    watchdog = TranscriptWatchdog(detector=detector)
    console = Console(record=True, width=120)

    hud = GlassboxHUD(
        console=console,
        detector=detector,
        watchdog=watchdog,
        breaker=breaker,
    )

    # Initial snapshot
    snapshot = hud.render_snapshot()
    assert "ZeroSpinner" in snapshot
    assert "Preemptive Kernel" in snapshot
    assert "Reached Milestones" in snapshot
    assert "Subagent Hierarchy" in snapshot


def test_hud_milestone_and_tree_update():
    detector = MilestoneDetector()
    breaker = CircuitBreaker(max_depth=2)
    hud = GlassboxHUD(detector=detector, breaker=breaker)

    breaker.register_subagent(agent_id="agent-1", role="lead_developer")
    event = MilestoneEvent(
        milestone_type=MILESTONE_TESTS_PASSED,
        raw_text="20 passed in 0.5s",
        metadata={"passed_count": 20},
    )
    detector.emit(event)

    hud.add_activity("Dispatched git push")
    snapshot = hud.render_snapshot()

    assert "lead_developer" in snapshot
    assert "MILESTONE_TESTS_PASSED" in snapshot
    assert "20 tests" in snapshot


def test_hud_breaker_trip_rendering():
    breaker = CircuitBreaker()
    hud = GlassboxHUD(breaker=breaker)

    breaker.trip(TripReason.SPECULATIVE_OVER_REFINEMENT, "Delivery preempted successfully")

    snapshot = hud.render_snapshot()
    assert "PREEMPTIVE DELIVERY" in snapshot or "TRIPPED" in snapshot


def test_hud_telemetry_rendering():
    hud = GlassboxHUD()
    telem = WatchdogTelemetry(
        lines_read=42,
        bytes_read=8192,
        tool_call_count=7,
        is_stalled=False,
        active_tool="run_command",
    )
    hud.update_telemetry(telem)

    snapshot = hud.render_snapshot()
    assert "42 lines" in snapshot
    assert "run_command" in snapshot
