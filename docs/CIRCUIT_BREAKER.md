# Circuit Breaker Specification & Operational Guide

The ZeroSpinner Circuit Breaker monitors agent activity and intercepts speculative over-refinement, runaway looping, and excessive resource expenditure.

## State Model

The circuit breaker transitions across three discrete states:

- `CLOSED`: Normal operational state. Agent tool invocations and commands execute without interruption.
- `TRIPPED`: Active when an invariant is violated (e.g. timeout expiration, repetitive failed calls, anti-matryoshka recursion threshold exceeded). Outbound actions are paused or halted.
- `HALF_OPEN`: Recovery and self-healing verification state. A limited probe window is permitted to confirm delivery or resolution.

## Monitored Failure Modes

1. **Anti-Matryoshka Nesting**: Detects unbounded agent-spawning depth to prevent cascade thrashing.
2. **Execution Stalls**: Intercepts background subagent tasks exceeding heartbeat limits.
3. **Over-Refinement Loops**: Prevents repetitive non-functional formatting cycles on stabilized deliverables.
