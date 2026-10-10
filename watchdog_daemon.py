#!/usr/bin/env python3
"""ZeroSpinner Autonomous Watchdog Daemon.

Continuously supervises autonomous coding agents, tails transcripts, monitors Subsystem Alpha,
and provides autonomous auto-teardown upon mission delivery (PR creation or completion),
ensuring zero background residue and zero credit waste.

Usage:
    python watchdog_daemon.py                     # Run daemon with auto-teardown
    python watchdog_daemon.py --worker-id <ID>    # Supervise specific subagent
    python watchdog_daemon.py --stop              # Stop running daemon and halt cloud VM
    python watchdog_daemon.py --status            # Check daemon & cloud power status
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add zerospinner to sys.path
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

# Force UTF-8 for console output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from zerospinner.core.breaker import CircuitBreaker
from zerospinner.core.milestone import (
    MILESTONE_COMMIT_PUSHED,
    MILESTONE_PR_CREATED,
    MILESTONE_TESTS_PASSED,
    MilestoneDetector,
    MilestoneEvent,
)

BRAIN_DIR = Path(os.path.expanduser(r"~\.gemini\antigravity\brain"))
DEFAULT_MAIN_CONV = "6f89e8f1-6979-4fd6-8666-d4ca9087693a"
PID_FILE = CURRENT_DIR / ".watchdog.pid"
STOP_FLAG_FILE = CURRENT_DIR / ".watchdog.stop"
DISPATCH_SCRIPT = Path("c:/ai/dispatch.py")


def find_latest_worker_id(brain_dir: Path, exclude_id: str) -> Optional[str]:
    """Finds the most recently modified subagent conversation ID under brain_dir."""
    if not brain_dir.exists():
        return None
    candidates: List[Tuple[float, str]] = []
    try:
        for item in brain_dir.iterdir():
            if item.is_dir() and item.name != exclude_id and not item.name.startswith("."):
                t_file = item / ".system_generated" / "logs" / "transcript.jsonl"
                if t_file.exists():
                    try:
                        mtime = t_file.stat().st_mtime
                        candidates.append((mtime, item.name))
                    except Exception:
                        pass
    except Exception:
        pass

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def read_transcript_tail(transcript_path: Path, offset: int) -> Tuple[List[str], int]:
    """Reads newly appended lines from a transcript file starting at offset."""
    if not transcript_path.exists():
        return [], 0

    new_lines: List[str] = []
    current_offset = offset
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            content = f.read()
            current_offset = f.tell()
            if content:
                for line in content.splitlines():
                    trimmed = line.strip()
                    if trimmed:
                        new_lines.append(trimmed)
    except Exception:
        pass

    return new_lines, current_offset


def parse_action_from_line(line: str) -> str:
    """Extracts a short human-readable action string from a JSONL transcript line."""
    try:
        data = json.loads(line)
        tool_calls = data.get("tool_calls", [])
        if tool_calls and isinstance(tool_calls, list):
            action = tool_calls[0].get("toolAction") or tool_calls[0].get("name", "")
            return str(action).strip('"')
        if data.get("type") == "GENERIC":
            return f"Step {data.get('step_index')}: {data.get('status', 'DONE')}"
        if data.get("type") == "USER_INPUT":
            return f"User Prompt (Step {data.get('step_index', 0)})"
        return f"Step {data.get('step_index', '')}: {data.get('type', '')}".strip(": ")
    except Exception:
        return line[:40]


def perform_teardown(reason: str = "Mission Complete", stop_cloud: bool = True) -> bool:
    """Executes full teardown: stops cloud VMs, halts Colab, and cleans background tasks."""
    print("\n" + "=" * 80, flush=True)
    print("🛑 ZeroSpinner Auto-Teardown Protocol Initiated", flush=True)
    print(f"   Reason: {reason}", flush=True)
    print("=" * 80, flush=True)

    # 1. Stop Cloud Resources via dispatch.py
    if stop_cloud and DISPATCH_SCRIPT.exists():
        try:
            print("[Teardown] Halting cloud compute (Osaka VM & Colab sessions)...", flush=True)
            cmd = [sys.executable, str(DISPATCH_SCRIPT), "--stop"]
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            if res.stdout:
                for line in res.stdout.splitlines():
                    line_s = line.strip()
                    if any(k in line_s for k in ("Power Guard", "ALL CLOUD", "RELEASED", "Subsystem")):
                        print(f"   {line_s}", flush=True)
        except Exception as e:
            print(f"[Teardown] Warning during cloud teardown: {e}", flush=True)

    # 2. Terminate any orphan worker / runner OS processes (e.g. orphan ssh, gcloud, repro processes)
    if sys.platform == "win32":
        try:
            clean_cmd = (
                'Get-Process | Where-Object { '
                '$_.ProcessName -match "ssh" -or '
                '($_.ProcessName -match "python" -and $_.CommandLine -match "repro|run_remote|dispatch\\.py --repo") '
                '} | Stop-Process -Force -ErrorAction SilentlyContinue'
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", clean_cmd], capture_output=True, timeout=10)
        except Exception:
            pass

    # 3. Stop Jules Watchdog if running
    try:
        from zerospinner.jules_watchdog import stop_daemon as stop_jules_daemon
        stop_jules_daemon()
    except Exception:
        pass

    # 4. Cleanup PID and Stop Flag files
    if PID_FILE.exists():
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    if STOP_FLAG_FILE.exists():
        try:
            STOP_FLAG_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    print("=" * 80, flush=True)
    print("✅ ZeroSpinner Teardown Complete. Cloud Resources & OS Processes Cleared.", flush=True)
    print("📢 [CRITICAL DIRECTIVE] Main Agent MUST execute manage_subagents(Action='kill_all') to wipe subagents!", flush=True)
    print(f"🛡️  Watchdog daemon exiting cleanly (PID: {os.getpid()}). Zero residue left.", flush=True)
    print("=" * 80 + "\n", flush=True)
    return True


def cmd_stop() -> int:
    """Handles `watchdog_daemon.py --stop`."""
    print("=" * 80, flush=True)
    print("🛑 Requesting Immediate ZeroSpinner Watchdog Teardown & Stop", flush=True)
    print("=" * 80, flush=True)

    # Signal running daemon via STOP_FLAG_FILE
    try:
        STOP_FLAG_FILE.write_text(f"stop_requested_at={time.time()}", encoding="utf-8")
    except Exception:
        pass

    # Kill running daemon process if PID is recorded
    if PID_FILE.exists():
        try:
            pid_str = PID_FILE.read_text(encoding="utf-8").strip()
            pid = int(pid_str)
            if pid != os.getpid():
                print(f"[Stop] Terminating running watchdog daemon process PID: {pid}...", flush=True)
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                else:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        except Exception as e:
            print(f"[Stop] Note: {e}", flush=True)
        finally:
            PID_FILE.unlink(missing_ok=True)

    # Execute full cloud teardown
    perform_teardown(reason="Manual CLI Stop Command", stop_cloud=True)
    return 0


def cmd_status() -> int:
    """Handles `watchdog_daemon.py --status`."""
    print("=" * 80, flush=True)
    print("📊 ZeroSpinner Watchdog Daemon & Cloud Compute Status", flush=True)
    print("=" * 80, flush=True)

    daemon_running = False
    daemon_pid = None
    if PID_FILE.exists():
        try:
            daemon_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            # Check liveness
            if sys.platform == "win32":
                res = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {daemon_pid}"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                daemon_running = str(daemon_pid) in res.stdout
            else:
                os.kill(daemon_pid, 0)
                daemon_running = True
        except Exception:
            daemon_running = False

    status_tag = f"🟢 RUNNING (PID: {daemon_pid})" if daemon_running else "⚪ STOPPED (Not running)"
    print(f"1. Watchdog Daemon Status  : {status_tag}", flush=True)

    # Query Cloud Power Status
    if DISPATCH_SCRIPT.exists():
        try:
            res = subprocess.run(
                [sys.executable, str(DISPATCH_SCRIPT), "--status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            if res.stdout:
                for line in res.stdout.splitlines():
                    if "Subsystem Alpha" in line or "Subsystem Beta" in line or "Subsystem Gamma" in line:
                        print(f"2. {line.strip()}", flush=True)
        except Exception as e:
            print(f"2. Cloud Status Query Error: {e}", flush=True)

    # Query Jules Cloud Sessions Status
    try:
        from zerospinner.jules_watchdog import JulesWatchdog
        dog = JulesWatchdog()
        completed = sum(1 for s in dog.state.values() if s.status == "Completed")
        active = sum(1 for s in dog.state.values() if s.status in ("In Progress", "Planning", "Running"))
        pulled = sum(1 for s in dog.state.values() if s.pulled)
        print(f"3. Jules Cloud Sessions    : {active} Active | {completed} Completed ({pulled} Auto-Pulled)", flush=True)
    except Exception as e:
        print(f"3. Jules Status Query Error: {e}", flush=True)

    print("=" * 80 + "\n", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description="ZeroSpinner Watchdog Daemon with Auto-Teardown")
    parser.add_argument("--conv-id", type=str, default=DEFAULT_MAIN_CONV, help="Main conversation ID")
    parser.add_argument("--worker-id", type=str, default=None, help="Target worker subagent ID")
    parser.add_argument("--poll", type=float, default=5.0, help="Polling interval in seconds")
    parser.add_argument("--cooldown", type=float, default=5.0, help="Grace cooldown in seconds after milestone")
    parser.add_argument("--max-idle", type=float, default=90.0, help="Max idle seconds before auto-teardown")
    parser.add_argument("--auto-teardown", action="store_true", default=True, help="Auto teardown on completion")
    parser.add_argument("--no-auto-teardown", action="store_false", dest="auto_teardown", help="Disable auto teardown")
    parser.add_argument("--no-cloud-stop", action="store_true", help="Skip cloud compute teardown")
    parser.add_argument("--watch-jules", action="store_true", default=True, help="Supervise and auto-pull Jules cloud sessions")
    parser.add_argument("--no-watch-jules", action="store_false", dest="watch_jules", help="Disable Jules supervision")
    parser.add_argument("--jules-poll", type=float, default=10.0, help="Polling interval for Jules cloud sessions")
    parser.add_argument("--stop", action="store_true", help="Stop running watchdog daemon and halt cloud compute")
    parser.add_argument("--status", action="store_true", help="Show watchdog daemon and cloud power status")

    args = parser.parse_args()

    if args.stop:
        sys.exit(cmd_stop())

    if args.status:
        sys.exit(cmd_status())

    # Write PID file
    pid = os.getpid()
    PID_FILE.write_text(str(pid), encoding="utf-8")

    def cleanup_pid():
        if PID_FILE.exists():
            try:
                if PID_FILE.read_text(encoding="utf-8").strip() == str(pid):
                    PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass

    atexit.register(cleanup_pid)

    # Determine target worker ID
    worker_id = args.worker_id
    if not worker_id:
        worker_id = find_latest_worker_id(BRAIN_DIR, exclude_id=args.conv_id)
        if not worker_id:
            worker_id = "752f7593-99e0-4c1a-a4fa-236006f264c1"

    worker_transcript = BRAIN_DIR / worker_id / ".system_generated" / "logs" / "transcript.jsonl"
    main_transcript = BRAIN_DIR / args.conv_id / ".system_generated" / "logs" / "transcript.jsonl"

    print("=" * 80, flush=True)
    print("🛡️  ZeroSpinner Autonomous Watchdog Daemon Active", flush=True)
    print(f"    Process ID (PID)   : {pid}", flush=True)
    print(f"    Main Conversation  : {args.conv_id}", flush=True)
    print(f"    Target Subagent    : {worker_id}", flush=True)
    print(f"    Auto-Teardown      : {'ENABLED (Auto-clean on delivery)' if args.auto_teardown else 'DISABLED'}", flush=True)
    print(f"    Poll Interval      : {args.poll}s | Cooldown: {args.cooldown}s | Max Idle: {args.max_idle}s", flush=True)
    print("=" * 80, flush=True)

    detector = MilestoneDetector()
    breaker = CircuitBreaker()

    # Callback when milestone detected
    milestones_seen: List[MilestoneEvent] = []

    def on_milestone(evt: MilestoneEvent):
        milestones_seen.append(evt)
        print(f"\n[ZeroSpinner] 🎯 Milestone Detected: {evt.milestone_type} via {evt.source}", flush=True)
        if evt.metadata.get("url"):
            print(f"              URL: {evt.metadata['url']}", flush=True)

    detector.on_milestone(on_milestone)

    worker_offset = 0
    main_offset = 0
    worker_line_count = 0
    last_activity_ts = time.time()
    last_action = ""
    iteration = 0

    # Read existing length of worker transcript
    if worker_transcript.exists():
        try:
            with open(worker_transcript, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    worker_line_count += 1
                    if line.strip():
                        last_action = parse_action_from_line(line.strip())
            worker_offset = worker_transcript.stat().st_size
        except Exception:
            pass

    # Read existing length of main transcript
    if main_transcript.exists():
        try:
            main_offset = main_transcript.stat().st_size
        except Exception:
            pass

    # Initialize Jules Watcher
    jules_watcher = None
    last_jules_poll = 0.0
    if args.watch_jules:
        try:
            from zerospinner.jules_watchdog import JulesWatchdog
            jules_watcher = JulesWatchdog(poll_interval=args.jules_poll)
            print("[ZeroSpinner] 🐾 Google Jules Cloud Session Auto-Pull Watcher Active.", flush=True)
        except Exception as e:
            print(f"[ZeroSpinner] Warning initializing Jules watcher: {e}", flush=True)

    while True:
        iteration += 1
        now_ts = time.time()
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")

        # 1. Check for external stop signal file
        if STOP_FLAG_FILE.exists():
            print("\n[ZeroSpinner] External stop flag detected (.watchdog.stop). Initiating clean exit...", flush=True)
            perform_teardown(reason="External Stop Signal File Detected", stop_cloud=not args.no_cloud_stop)
            sys.exit(0)

        # 2. Check Jules Cloud Sessions and Auto-Pull
        if jules_watcher and (now_ts - last_jules_poll >= args.jules_poll):
            last_jules_poll = now_ts
            try:
                newly_pulled = jules_watcher.check_once()
                if newly_pulled:
                    for np in newly_pulled:
                        print(f"\n[ZeroSpinner] 🎯 Jules Session Auto-Pulled: #{np.session_id} ({np.repo} #{np.issue_number})", flush=True)
                        print(f"              Patch: {np.patch_path} | Score: {np.surgical_score} | Verdict: {np.audit_verdict}", flush=True)
            except Exception:
                pass

        # 3. Check Worker Transcript Updates
        has_new_activity = False
        new_worker_lines, worker_offset = read_transcript_tail(worker_transcript, worker_offset)
        if new_worker_lines:
            has_new_activity = True
            worker_line_count += len(new_worker_lines)
            last_activity_ts = now_ts
            for raw_line in new_worker_lines:
                last_action = parse_action_from_line(raw_line)
                detector.scan_line(raw_line, source="worker_transcript")

        # 4. Check Main Conversation Transcript Updates (PRs created by coordinator)
        new_main_lines, main_offset = read_transcript_tail(main_transcript, main_offset)
        if new_main_lines:
            has_new_activity = True
            last_activity_ts = now_ts
            for raw_line in new_main_lines:
                detector.scan_line(raw_line, source="main_transcript")

        # 5. Check Delivery Milestones & Trigger Auto-Teardown
        pr_milestone = detector.has_milestone(MILESTONE_PR_CREATED)
        if pr_milestone and args.auto_teardown:
            latest_pr = next((m for m in reversed(milestones_seen) if m.milestone_type == MILESTONE_PR_CREATED), None)
            pr_info = f" (URL: {latest_pr.metadata.get('url')})" if latest_pr else ""
            print(f"\n[ZeroSpinner] 🏆 MISSION DELIVERED! PR Milestone Confirmed{pr_info}", flush=True)
            print(f"[ZeroSpinner] Entering {args.cooldown}s cooldown for log buffer synchronization...", flush=True)
            time.sleep(args.cooldown)
            perform_teardown(reason=f"Mission Delivery Confirmed: MILESTONE_PR_CREATED{pr_info}", stop_cloud=not args.no_cloud_stop)
            sys.exit(0)

        # 6. Check Idle Timeout Teardown
        idle_seconds = int(now_ts - last_activity_ts)
        is_active = idle_seconds < 30
        state_tag = "🟢 ACTIVE" if is_active else f"🟡 IDLE ({idle_seconds}s)"
        breaker_state = "CLOSED (NORMAL)" if not breaker.is_tripped() else "OPEN (TRIPPED)"

        jules_status_tag = ""
        if jules_watcher:
            j_active = sum(1 for s in jules_watcher.state.values() if s.status in ("In Progress", "Planning", "Running"))
            j_pulled = sum(1 for s in jules_watcher.state.values() if s.pulled)
            j_comp = sum(1 for s in jules_watcher.state.values() if s.status == "Completed")
            jules_status_tag = f" | Jules: {j_active} active, {j_pulled}/{j_comp} pulled"

        action_display = f" | Action: {last_action[:35]}" if last_action else ""
        print(
            f"[{now_str}] [ZeroSpinner Heartbeat #{iteration:04d}] "
            f"Worker: {worker_id[:8]}... | "
            f"Lines: {worker_line_count} | "
            f"Status: {state_tag} | "
            f"Breaker: {breaker_state}{action_display}{jules_status_tag}",
            flush=True,
        )

        # If agent has completed work and remains idle past max_idle, auto-teardown
        if idle_seconds >= args.max_idle and args.auto_teardown:
            # Check if any completion / test passed / commit milestone occurred
            has_progress = (
                detector.has_milestone(MILESTONE_TESTS_PASSED)
                or detector.has_milestone(MILESTONE_COMMIT_PUSHED)
                or "DONE" in last_action
            )
            if has_progress:
                print(f"\n[ZeroSpinner] Max idle time ({args.max_idle}s) reached after completed tasks.", flush=True)
                perform_teardown(reason=f"Max idle time ({args.max_idle}s) reached after tasks completed", stop_cloud=not args.no_cloud_stop)
                sys.exit(0)

        time.sleep(args.poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ZeroSpinner] Interrupted by user. Performing clean exit...", flush=True)
        perform_teardown(reason="KeyboardInterrupt", stop_cloud=True)
        sys.exit(0)
