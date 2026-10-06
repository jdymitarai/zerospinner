"""Tests for CircuitBreaker, anti-Matryoshka governor, and speculative over-refinement prevention."""

import time
import pytest

from zerospinner.core.breaker import BreakerState, CircuitBreaker, TripReason
from zerospinner.core.milestone import MilestoneEvent, MILESTONE_TESTS_PASSED, MILESTONE_PR_CREATED


def test_depth_limit_enforcement():
    breaker = CircuitBreaker(max_depth=2)

    # Coordinator (Depth 1)
    allowed1, err1 = breaker.register_subagent(agent_id="coord", parent_id=None, role="coordinator")
    assert allowed1 is True
    assert err1 is None

    # Worker (Depth 2)
    allowed2, err2 = breaker.register_subagent(agent_id="worker-1", parent_id="coord", role="worker")
    assert allowed2 is True
    assert err2 is None

    # Sub-worker (Depth 3) -> Should exceed max_depth=2 and trip breaker!
    allowed3, err3 = breaker.register_subagent(agent_id="subworker-1", parent_id="worker-1", role="subworker")
    assert allowed3 is False
    assert "exceeded max nesting depth 2" in err3
    assert breaker.is_tripped()
    assert breaker.trip_reason == TripReason.DEPTH_EXCEEDED.value


def test_speculative_over_refinement_interception():
    breaker = CircuitBreaker(max_depth=3, stop_on_core_milestone=True)

    # Register coordinator and worker
    breaker.register_subagent(agent_id="coord", parent_id=None, role="coordinator")
    breaker.register_subagent(agent_id="worker", parent_id="coord", role="worker")

    # Record that tests passed (Core milestone reached!)
    event = MilestoneEvent(milestone_type=MILESTONE_TESTS_PASSED, raw_text="10 passed in 0.5s")
    breaker.record_milestone(event)
    assert breaker.has_core_milestone()

    # Now coordinator attempts to launch redundant speculative reviewer / improvement worker
    allowed, err = breaker.register_subagent(
        agent_id="worker-polish",
        parent_id="coord",
        role="improvement_reviewer",
    )

    # Breaker must block this speculative worker and trigger preemptive delivery!
    assert allowed is False
    assert breaker.is_tripped()
    assert breaker.trip_reason == TripReason.SPECULATIVE_OVER_REFINEMENT.value
    assert "Preemptive Delivery Triggered" in err


def test_token_budget_enforcement():
    breaker = CircuitBreaker(token_budget=5000)

    assert breaker.record_tokens(3000) is True
    assert not breaker.is_tripped()

    # Exceeding budget
    assert breaker.record_tokens(2500) is False
    assert breaker.is_tripped()
    assert breaker.trip_reason == TripReason.TOKEN_BUDGET_EXCEEDED.value


def test_time_budget_enforcement():
    breaker = CircuitBreaker(time_budget_sec=0.1)

    time.sleep(0.15)
    is_tripped, reason = breaker.check_health()
    assert is_tripped is True
    assert reason == TripReason.TIME_BUDGET_EXCEEDED.value


def test_trip_callbacks():
    breaker = CircuitBreaker()
    captured = []

    def on_trip_cb(reason: str, details: str):
        captured.append((reason, details))

    breaker.on_trip(on_trip_cb)
    breaker.trip(TripReason.MANUAL_OVERRIDE, "User pressed emergency brake")

    time.sleep(0.05)  # Callbacks run in thread
    assert len(captured) == 1
    assert captured[0][0] == TripReason.MANUAL_OVERRIDE.value
    assert "emergency brake" in captured[0][1]


def test_summary_serialization():
    breaker = CircuitBreaker(max_depth=2, token_budget=10_000)
    breaker.register_subagent("root", None, role="coordinator")
    breaker.record_tokens(1500)

    summary = breaker.get_summary()
    assert summary["state"] == "CLOSED"
    assert summary["tokens_used"] == 1500
    assert summary["token_budget"] == 10_000
    assert summary["max_depth_limit"] == 2
    assert "root" in summary["subagents"]
