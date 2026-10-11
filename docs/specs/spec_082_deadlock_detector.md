# Subsystem Specification: Deadlock Detector

**Identifier**: `ZSP-0082`
**Module**: `zerospinner.core.deadlock_detector`

## Architectural Overview
Deadlock detection heuristic across concurrent workers.

## Contract Invariants
1. Guarantees deterministic state convergence during agent execution.
2. Emits structured telemetry pulses to the Glassbox HUD subsystem.
3. Intercepts anomalous latency spikes before downstream pipeline starvation.
