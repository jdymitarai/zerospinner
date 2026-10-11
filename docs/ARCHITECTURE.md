# ZeroSpinner Architecture Overview

ZeroSpinner is a high-reliability preemptive kernel and circuit breaker framework engineered for autonomous AI coding agents and long-running developer loops.

## Core Subsystems

### 1. Preemptive Watchdog Engine
Monitors agent activity, terminal streams, and background task progress in real time. It detects stalls, infinite loops, and unhandled hangs without blocking parent orchestration threads.

### 2. Milestone Detection Engine
Scans output streams across Bazel, Cargo, Go, CMake, Maven, and GitHub CLI workflows. It identifies concrete build events, test green bars, and pull request creations to trigger auto-teardown and notify developers.

### 3. Circuit Breaker & Safety Barrier
Maintains strict quota invariants, preventing agent hallucination loops, runaway compute credit expenditure, and concurrent pull request sprawl on upstream repositories.

### 4. Glassbox HUD
Provides real-time visibility into active subagents, cloud VM compute state, and heartbeat telemetry directly within terminal environments.
