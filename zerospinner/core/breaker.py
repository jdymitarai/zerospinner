"""Anti-Matryoshka circuit breaker and speculative over-refinement governor."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from zerospinner.core.milestone import (
    MILESTONE_PR_CREATED,
    MILESTONE_TESTS_PASSED,
    MilestoneEvent,
)

logger = logging.getLogger(__name__)


class BreakerState(str, Enum):
    """Circuit breaker operational state."""

    CLOSED = "CLOSED"  # Healthy, running normally
    TRIPPED = "TRIPPED"  # Breaker triggered, execution intercepted
    HALF_OPEN = "HALF_OPEN"  # Verifying delivery or probing recovery


class TripReason(str, Enum):
    """Reasons for triggering the circuit breaker."""

    DEPTH_EXCEEDED = "DEPTH_EXCEEDED"
    SPECULATIVE_OVER_REFINEMENT = "SPECULATIVE_OVER_REFINEMENT"
    TOKEN_BUDGET_EXCEEDED = "TOKEN_BUDGET_EXCEEDED"
    TIME_BUDGET_EXCEEDED = "TIME_BUDGET_EXCEEDED"
    STALL_TIMEOUT = "STALL_TIMEOUT"
    LOOP_DEADLOCK = "LOOP_DEADLOCK"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"


@dataclass
class SubagentNode:
    """Node representing an agent in the execution hierarchy."""

    agent_id: str
    parent_id: Optional[str] = None
    depth: int = 1
    pid: Optional[int] = None
    role: str = "worker"
    spawn_time: float = field(default_factory=time.time)
    status: str = "active"  # "active", "completed", "terminated"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "pid": self.pid,
            "role": self.role,
            "spawn_time": self.spawn_time,
            "status": self.status,
            "metadata": self.metadata,
        }


class CircuitBreaker:
    """Anti-Matryoshka governor and execution circuit breaker.

    Enforces depth limits, token/time budgets, and intercepts speculative
    over-refinement loops once core milestones have been accomplished.
    """

    def __init__(
        self,
        max_depth: int = 2,
        time_budget_sec: float = 600.0,
        token_budget: int = 150_000,
        stop_on_core_milestone: bool = True,
        core_milestones: Optional[Set[str]] = None,
        kill_subagents_on_trip: bool = True,
    ) -> None:
        self.max_depth = max(1, max_depth)
        self.time_budget_sec = max(0.01, time_budget_sec)
        self.token_budget = max(1, token_budget)
        self.stop_on_core_milestone = stop_on_core_milestone
        self.core_milestones: Set[str] = core_milestones or {
            MILESTONE_TESTS_PASSED,
            MILESTONE_PR_CREATED,
        }
        self.kill_subagents_on_trip = kill_subagents_on_trip

        self._lock = threading.Lock()
        self.state: BreakerState = BreakerState.CLOSED
        self.trip_reason: Optional[str] = None
        self.trip_details: str = ""
        self.trip_timestamp: Optional[float] = None
        self.start_time: float = time.time()
        self.tokens_used: int = 0

        self.subagents: Dict[str, SubagentNode] = {}
        self.milestones_achieved: List[MilestoneEvent] = []
        self._trip_callbacks: List[Callable[[str, str], None]] = []

    def on_trip(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback invoked when the breaker trips: callback(reason, details)."""
        with self._lock:
            self._trip_callbacks.append(callback)

    def is_tripped(self) -> bool:
        """Check if breaker is currently tripped."""
        with self._lock:
            return self.state == BreakerState.TRIPPED

    def has_core_milestone(self) -> bool:
        """Check if at least one core milestone has been achieved."""
        with self._lock:
            return any(m.milestone_type in self.core_milestones for m in self.milestones_achieved)

    def record_milestone(self, event: MilestoneEvent) -> None:
        """Record an achieved milestone."""
        with self._lock:
            self.milestones_achieved.append(event)

    def record_tokens(self, count: int) -> bool:
        """Record token consumption. Returns True if within budget, False if tripped."""
        with self._lock:
            self.tokens_used += count
            if self.tokens_used > self.token_budget and self.state == BreakerState.CLOSED:
                self._do_trip(
                    TripReason.TOKEN_BUDGET_EXCEEDED,
                    f"Token usage {self.tokens_used} exceeded budget {self.token_budget}.",
                )
                return False
            return self.state == BreakerState.CLOSED

    def register_subagent(
        self,
        agent_id: str,
        parent_id: Optional[str] = None,
        pid: Optional[int] = None,
        role: str = "worker",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Register a subagent spawn attempt.

        Returns (allowed: bool, rejection_reason: Optional[str]).
        """
        with self._lock:
            # 1. Check if already tripped
            if self.state == BreakerState.TRIPPED:
                return False, f"Breaker already tripped: {self.trip_reason} ({self.trip_details})"

            # Calculate nesting depth
            parent_node = self.subagents.get(parent_id) if parent_id else None
            depth = (parent_node.depth + 1) if parent_node else 1

            # 2. Check Depth Violation (Matryoshka Limit)
            if depth > self.max_depth:
                reason = TripReason.DEPTH_EXCEEDED
                details = (
                    f"Subagent '{agent_id}' exceeded max nesting depth {self.max_depth} "
                    f"(attempted depth: {depth}, parent: '{parent_id}')."
                )
                self._do_trip(reason, details)
                return False, details

            # 3. Check Speculative Over-Refinement
            # If a core milestone is already achieved and an improvement/refinement worker is spawned
            is_refinement = any(
                keyword in role.lower() or keyword in agent_id.lower()
                for keyword in ("improve", "review", "refine", "polish", "subagent", "eval", "auditor")
            )
            has_core = any(m.milestone_type in self.core_milestones for m in self.milestones_achieved)

            if self.stop_on_core_milestone and has_core and is_refinement:
                reason = TripReason.SPECULATIVE_OVER_REFINEMENT
                milestone_names = ", ".join(
                    m.milestone_type for m in self.milestones_achieved if m.milestone_type in self.core_milestones
                )
                details = (
                    f"Preemptive Delivery Triggered: Core milestone(s) [{milestone_names}] achieved. "
                    f"Blocked redundant speculative refinement worker '{agent_id}' ({role})."
                )
                self._do_trip(reason, details)
                return False, details

            # Allowed - record node
            node = SubagentNode(
                agent_id=agent_id,
                parent_id=parent_id,
                depth=depth,
                pid=pid,
                role=role,
                spawn_time=time.time(),
                status="active",
                metadata=metadata or {},
            )
            self.subagents[agent_id] = node
            return True, None

    def complete_subagent(self, agent_id: str) -> None:
        """Mark a subagent as completed."""
        with self._lock:
            if agent_id in self.subagents:
                self.subagents[agent_id].status = "completed"

    def check_health(self) -> Tuple[bool, Optional[str]]:
        """Periodic budget and health check. Returns (is_tripped, reason)."""
        now = time.time()
        with self._lock:
            if self.state == BreakerState.TRIPPED:
                return True, self.trip_reason

            elapsed = now - self.start_time
            if elapsed > self.time_budget_sec:
                reason = TripReason.TIME_BUDGET_EXCEEDED
                details = f"Execution elapsed time {elapsed:.1f}s exceeded budget {self.time_budget_sec:.1f}s."
                self._do_trip(reason, details)
                return True, reason

            return False, None

    def trip(self, reason: Union[TripReason, str], details: str = "") -> None:
        """Manually trip the circuit breaker."""
        with self._lock:
            self._do_trip(reason, details)

    def _do_trip(self, reason: Union[TripReason, str], details: str) -> None:
        """Internal helper to change state to TRIPPED and execute cleanup. Must hold self._lock."""
        if self.state == BreakerState.TRIPPED:
            return

        self.state = BreakerState.TRIPPED
        self.trip_reason = str(reason.value if isinstance(reason, TripReason) else reason)
        self.trip_details = details
        self.trip_timestamp = time.time()

        callbacks = list(self._trip_callbacks)

        # Release lock before triggering callbacks and process terminations to prevent deadlock
        threading.Thread(
            target=self._post_trip_actions,
            args=(self.trip_reason, self.trip_details, callbacks),
            daemon=True,
        ).start()

    def _post_trip_actions(
        self,
        reason: str,
        details: str,
        callbacks: List[Callable[[str, str], None]],
    ) -> None:
        """Execute notifications and subagent process terminations outside lock."""
        for cb in callbacks:
            try:
                cb(reason, details)
            except Exception as e:
                logger.error(f"Error in breaker trip callback: {e}")

        if self.kill_subagents_on_trip:
            self.terminate_subagents()

    def terminate_subagents(self) -> int:
        """Terminate all registered active child processes/subagents."""
        killed_count = 0
        with self._lock:
            targets = [
                (node.agent_id, node.pid)
                for node in self.subagents.values()
                if node.status == "active" and node.pid is not None
            ]

        for agent_id, pid in targets:
            if pid is None or pid <= 0:
                continue
            try:
                # Platform-independent termination
                if sys.platform == "win32":
                    import subprocess
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    os.kill(pid, signal.SIGTERM)
                killed_count += 1
            except Exception:
                pass

            with self._lock:
                if agent_id in self.subagents:
                    self.subagents[agent_id].status = "terminated"

        return killed_count

    def get_summary(self) -> Dict[str, Any]:
        """Return a structured summary of the breaker's status."""
        with self._lock:
            elapsed = time.time() - self.start_time
            max_current_depth = max((n.depth for n in self.subagents.values()), default=0)
            return {
                "state": self.state.value,
                "trip_reason": self.trip_reason,
                "trip_details": self.trip_details,
                "trip_timestamp": self.trip_timestamp,
                "elapsed_sec": round(elapsed, 2),
                "time_budget_sec": self.time_budget_sec,
                "tokens_used": self.tokens_used,
                "token_budget": self.token_budget,
                "subagent_count": len(self.subagents),
                "max_current_depth": max_current_depth,
                "max_depth_limit": self.max_depth,
                "milestones_count": len(self.milestones_achieved),
                "milestones": [m.to_dict() for m in self.milestones_achieved],
                "subagents": {k: v.to_dict() for k, v in self.subagents.items()},
            }
