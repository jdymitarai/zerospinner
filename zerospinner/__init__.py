"""ZeroSpinner: The Preemptive Kernel & Circuit Breaker for Autonomous AI Agents."""

from zerospinner.core.breaker import BreakerState, CircuitBreaker, TripReason
from zerospinner.core.milestone import MilestoneDetector, MilestoneEvent
from zerospinner.core.watchdog import (
    TranscriptWatchdog,
    WatchdogAlert,
    WatchdogTelemetry,
    ZeroSpinnerWatchdog,
)

__version__ = "0.1.0"
__all__ = [
    "CircuitBreaker",
    "BreakerState",
    "TripReason",
    "MilestoneDetector",
    "MilestoneEvent",
    "TranscriptWatchdog",
    "ZeroSpinnerWatchdog",
    "WatchdogAlert",
    "WatchdogTelemetry",
]
