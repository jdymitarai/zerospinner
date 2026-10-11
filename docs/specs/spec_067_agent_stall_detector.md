# Subsystem Specification: Agent Stall Detector

**Identifier**: `ZSP-0067`
**Module**: `zerospinner.core.agent_stall_detector`

## Architectural Overview
Agent activity cadence and silence threshold tracking.

## Contract Invariants
1. Guarantees deterministic state convergence during agent execution.
2. Emits structured telemetry pulses to the Glassbox HUD subsystem.
3. Intercepts anomalous latency spikes before downstream pipeline starvation.
