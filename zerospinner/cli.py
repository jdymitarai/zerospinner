"""Standalone command-line interface `zspin` for ZeroSpinner."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from rich.console import Console

from zerospinner.core.breaker import BreakerState, CircuitBreaker, TripReason
from zerospinner.core.milestone import MilestoneDetector, MilestoneEvent
from zerospinner.core.watchdog import TranscriptWatchdog
from zerospinner.ui.hud import GlassboxHUD


def create_parser() -> argparse.ArgumentParser:
    """Create command-line argument parser for zspin."""
    parser = argparse.ArgumentParser(
        prog="zspin",
        description="ZeroSpinner: Preemptive Kernel & Circuit Breaker for Autonomous AI Agents",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="ZeroSpinner subcommands")

    # Subcommand: watch
    watch_parser = subparsers.add_parser("watch", help="Tails JSONL agent transcripts with real-time HUD")
    watch_parser.add_argument("transcript_path", type=str, help="Path to JSONL transcript file to watch")
    watch_parser.add_argument("--poll", type=float, default=1.0, help="Polling interval in seconds (default: 1.0)")
    watch_parser.add_argument("--stall-sec", type=float, default=30.0, help="Stall detection threshold (default: 30.0)")

    # Subcommand: run
    run_parser = subparsers.add_parser("run", help="Wrap and supervise agent command execution with HUD & breaker")
    run_parser.add_argument("command", nargs=argparse.REMAINDER, help="Agent command to execute and supervise")
    run_parser.add_argument("--max-depth", type=int, default=2, help="Max subagent depth limit (default: 2)")
    run_parser.add_argument("--time-budget", type=float, default=600.0, help="Max runtime in seconds (default: 600)")
    run_parser.add_argument("--no-hud", action="store_true", help="Run without live TUI (output logs directly)")

    # Subcommand: mcp
    subparsers.add_parser("mcp", help="Run Model Context Protocol (MCP) server over standard I/O")

    # Subcommand: demo
    demo_parser = subparsers.add_parser("demo", help="Run simulated autonomous agent execution demo")
    demo_parser.add_argument("--fast", action="store_true", help="Speed up simulation delays for automated testing")
    demo_parser.add_argument("--headless", action="store_true", help="Render static snapshots instead of interactive Live")

    return parser


def run_watch(args: argparse.Namespace) -> int:
    """Execute `zspin watch` command."""
    transcript_file = Path(args.transcript_path)
    console = Console()

    console.print(f"[bold cyan]ZeroSpinner[/bold cyan] watching transcript: [green]{transcript_file}[/green]")

    detector = MilestoneDetector()
    breaker = CircuitBreaker()
    watchdog = TranscriptWatchdog(
        transcript_path=transcript_file,
        poll_interval=args.poll,
        stall_threshold_sec=args.stall_sec,
        detector=detector,
    )
    hud = GlassboxHUD(console=console, detector=detector, watchdog=watchdog, breaker=breaker)

    watchdog.start()
    live = hud.start()
    try:
        while True:
            time.sleep(0.25)
            hud.update()
            if breaker.is_tripped():
                time.sleep(1.0)
                break
    except KeyboardInterrupt:
        hud.add_activity("Interrupted by user.")
    finally:
        hud.stop()
        watchdog.stop()
        console.print("[dim]ZeroSpinner watchdog stopped.[/dim]")

    return 0


def run_exec(args: argparse.Namespace) -> int:
    """Execute `zspin run` command."""
    cmd = args.command
    # If the user specified '--', strip it
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if not cmd:
        print("Error: No command specified to run.", file=sys.stderr)
        return 1

    console = Console()
    detector = MilestoneDetector()
    breaker = CircuitBreaker(
        max_depth=args.max_depth,
        time_budget_sec=args.time_budget,
    )
    watchdog = TranscriptWatchdog(detector=detector)
    hud = GlassboxHUD(console=console, detector=detector, watchdog=watchdog, breaker=breaker)

    # Spawn process
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            shell=(sys.platform == "win32" and isinstance(cmd, str)),
        )
    except Exception as e:
        console.print(f"[bold red]Failed to execute command '{cmd}': {e}[/bold red]")
        return 1

    # Register root process in breaker
    breaker.register_subagent(agent_id="root-process", pid=proc.pid, role="root_runner")

    live = None
    if not args.no_hud and sys.stdout.isatty():
        live = hud.start()

    def stream_reader(pipe, is_stderr: bool):
        source = "stderr" if is_stderr else "stdout"
        for line in iter(pipe.readline, ""):
            watchdog.feed_text(line, source=source)
            hud.add_activity(f"[{source}] {line.strip()[:60]}")
            if breaker.is_tripped():
                break
        pipe.close()

    t_out = threading.Thread(target=stream_reader, args=(proc.stdout, False), daemon=True)
    t_err = threading.Thread(target=stream_reader, args=(proc.stderr, True), daemon=True)
    t_out.start()
    t_err.start()

    exit_code = 0
    try:
        while proc.poll() is None:
            time.sleep(0.1)
            breaker.check_health()
            if live:
                hud.update()
            if breaker.is_tripped():
                hud.add_activity(f"[ALERT] Terminating command due to breaker trip: {breaker.trip_reason}")
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                exit_code = 2
                break
        if not breaker.is_tripped():
            exit_code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        exit_code = 130
    finally:
        if live:
            hud.stop()
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)

    console.print(f"\n[bold]Execution completed with exit code: {exit_code}[/bold]")
    if breaker.is_tripped():
        console.print(f"[bold yellow]Breaker Result: {breaker.trip_reason} - {breaker.trip_details}[/bold yellow]")
    return exit_code


def run_mcp(args: argparse.Namespace) -> int:
    """Execute `zspin mcp` server."""
    from zerospinner.mcp.server import run_mcp_server

    try:
        run_mcp_server()
        return 0
    except Exception as e:
        print(f"Error starting MCP server: {e}", file=sys.stderr)
        return 1


def run_demo(args: argparse.Namespace) -> int:
    """Execute `zspin demo` interactive simulated agent session."""
    delay = 0.05 if args.fast else 0.8
    console = Console(safe_box=True)

    detector = MilestoneDetector()
    breaker = CircuitBreaker(max_depth=2, stop_on_core_milestone=True)
    watchdog = TranscriptWatchdog(detector=detector)
    hud = GlassboxHUD(console=console, detector=detector, watchdog=watchdog, breaker=breaker)

    live = None
    if not args.headless:
        live = hud.start()

    def tick(msg: str):
        hud.add_activity(msg)
        if live:
            hud.update()
        else:
            console.print(f"[dim]{msg}[/dim]")
        time.sleep(delay)

    try:
        tick("[LAUNCH] Launching Autonomous Coding Agent (Claude Code Coordinator)...")
        breaker.register_subagent(agent_id="agent-coord", parent_id=None, role="coordinator")

        tick("[REPO] Coordinator analyzing repository structure and files...")
        watchdog.feed_text("Inspecting src/main.py and tests/test_core.py", source="stdout")

        tick("[AGENT] Coordinator spawning Worker Subagent-1 (Coding Worker)...")
        allowed, err = breaker.register_subagent(
            agent_id="subagent-1", parent_id="agent-coord", role="coding_worker"
        )
        tick(f"Worker Subagent-1 registered (allowed={allowed})")

        tick("[TESTS] Subagent-1 running test suite: pytest tests/...")
        watchdog.feed_text(
            "tests/test_core.py::test_init PASSED\n"
            "tests/test_core.py::test_eval PASSED\n"
            "============================== 18 passed in 0.42s ==============================\n",
            source="stdout",
        )

        tick("[MILESTONE] MILESTONE_TESTS_PASSED detected by ZeroSpinner!")
        breaker.complete_subagent("subagent-1")

        tick("[WARN] Agent Coordinator attempts redundant speculative improvement loop...")
        tick("Spawning Subagent-2 (Speculative Reviewer & Refiner)...")

        allowed, err = breaker.register_subagent(
            agent_id="subagent-2", parent_id="agent-coord", role="speculative_refiner"
        )

        if not allowed:
            tick(f"[ALERT] CircuitBreaker intercepted: {breaker.trip_reason}")
            tick("[DELIVERY] ZeroSpinner Preemptive Delivery Activated!")
        else:
            tick("Subagent-2 spawned.")

        time.sleep(delay * 2)

    finally:
        if live:
            hud.stop()

    summary = breaker.get_summary()
    console.print("\n" + "=" * 60)
    console.print("[bold green]ZeroSpinner Demo Summary[/bold green]")
    console.print(f"Breaker State: [bold]{summary['state']}[/bold]")
    console.print(f"Trip Reason:   [bold yellow]{summary['trip_reason']}[/bold yellow]")
    console.print(f"Details:       {summary['trip_details']}")
    console.print(f"Milestones:    {len(summary['milestones'])} achieved")
    console.print("=" * 60 + "\n")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint."""
    parser = create_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0

    if not args.subcommand:
        parser.print_help()
        return 0

    if args.subcommand == "watch":
        return run_watch(args)
    elif args.subcommand == "run":
        return run_exec(args)
    elif args.subcommand == "mcp":
        return run_mcp(args)
    elif args.subcommand == "demo":
        return run_demo(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
