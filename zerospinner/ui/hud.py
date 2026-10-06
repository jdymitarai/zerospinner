"""Glassbox Terminal HUD: Live interactive dashboard for autonomous agent execution."""

from __future__ import annotations

import io
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from zerospinner.core.breaker import BreakerState, CircuitBreaker
from zerospinner.core.milestone import MilestoneDetector, MilestoneEvent
from zerospinner.core.watchdog import TranscriptWatchdog, WatchdogAlert, WatchdogTelemetry


class GlassboxHUD:
    """Glassbox Terminal HUD for monitoring AI agent executions.

    Renders live agent hierarchy, detected milestones, circuit breaker state,
    and telemetry gauges in real-time, eliminating the opaque infinite spinner.
    """

    def __init__(
        self,
        console: Optional[Console] = None,
        title: str = "ZeroSpinner Glassbox HUD",
        detector: Optional[MilestoneDetector] = None,
        watchdog: Optional[TranscriptWatchdog] = None,
        breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.console = console or Console()
        self.title = title
        self.detector = detector
        self.watchdog = watchdog
        self.breaker = breaker

        self._lock = threading.Lock()
        self._activities: Deque[str] = deque(maxlen=8)
        self._alerts: Deque[str] = deque(maxlen=5)
        self._milestones: List[MilestoneEvent] = []
        self._telemetry: Optional[WatchdogTelemetry] = None
        self._breaker_summary: Dict[str, Any] = {}

        self._start_time = time.time()
        self._live: Optional[Live] = None

        # Wire up listeners if components are provided
        if self.detector:
            self.detector.on_milestone(self.add_milestone)
            if self.breaker:
                self.detector.on_milestone(self.breaker.record_milestone)
        if self.watchdog:
            self.watchdog.on_alert(self.add_alert)
            self.watchdog.on_telemetry(self.update_telemetry)
        if self.breaker:
            self.breaker.on_trip(self._on_breaker_trip)

    def add_activity(self, text: str) -> None:
        """Add an activity log entry."""
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            self._activities.append(f"[{ts}] {text}")

    def add_alert(self, alert: WatchdogAlert) -> None:
        """Add a watchdog alert message."""
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            self._alerts.append(f"[{ts}] [ALERT:{alert.alert_type}] {alert.details}")

    def add_milestone(self, event: MilestoneEvent) -> None:
        """Record a milestone occurrence."""
        with self._lock:
            self._milestones.append(event)
            ts = time.strftime("%H:%M:%S")
            self._activities.append(f"[{ts}] [MILESTONE] {event.milestone_type} ({event.raw_text[:40]})")

    def update_telemetry(self, telem: WatchdogTelemetry) -> None:
        """Update telemetry metrics snapshot."""
        with self._lock:
            self._telemetry = telem

    def _on_breaker_trip(self, reason: str, details: str) -> None:
        """Handle breaker trip callback."""
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            self._alerts.append(f"[{ts}] [BREAKER TRIPPED] {reason}")
            self._activities.append(f"[{ts}] [BREAKER] {details}")

    def generate_header(self) -> Panel:
        """Render top status banner with circuit breaker health."""
        with self._lock:
            breaker_state = self.breaker.state if self.breaker else BreakerState.CLOSED
            trip_reason = self.breaker.trip_reason if self.breaker else None
            is_stalled = self._telemetry.is_stalled if self._telemetry else False
            is_looping = self._telemetry.is_looping if self._telemetry else False

        status_text = Text()
        if breaker_state == BreakerState.TRIPPED:
            if "SPECULATIVE" in (trip_reason or ""):
                status_text.append("[PREEMPTIVE DELIVERY]", style="bold white on dark_green")
                status_text.append(f"  Breaker Tripped: {trip_reason}", style="bold green")
            else:
                status_text.append("[TRIPPED]", style="bold white on red")
                status_text.append(f"  Reason: {trip_reason}", style="bold red")
        elif is_looping:
            status_text.append("[LOOP DEADLOCK DETECTED]", style="bold yellow on black")
        elif is_stalled:
            status_text.append("[AGENT STALLED]", style="bold magenta on black")
        else:
            status_text.append("[RUNNING]", style="bold green")
            status_text.append("  Kernel Healthy", style="dim green")

        elapsed = time.time() - self._start_time
        mins, secs = divmod(int(elapsed), 60)
        time_str = f"{mins:02d}:{secs:02d}"

        header_table = Table.grid(expand=True)
        header_table.add_column(justify="left", ratio=3)
        header_table.add_column(justify="center", ratio=4)
        header_table.add_column(justify="right", ratio=3)

        title_text = Text.assemble(("ZeroSpinner", "bold cyan"), (" | Preemptive Kernel", "dim"))
        time_text = Text(f"Elapsed: {time_str}", style="bold white")

        header_table.add_row(title_text, status_text, time_text)

        border_style = (
            "green"
            if breaker_state == BreakerState.CLOSED
            else ("yellow" if "SPECULATIVE" in (trip_reason or "") else "red")
        )
        return Panel(header_table, border_style=border_style, padding=(0, 1))

    def generate_milestone_panel(self) -> Panel:
        """Render achieved milestones."""
        with self._lock:
            milestones = list(self._milestones)

        table = Table(box=None, expand=True, show_header=True, header_style="bold cyan")
        table.add_column("Status", width=8)
        table.add_column("Milestone", width=24)
        table.add_column("Details", style="dim")

        if not milestones:
            table.add_row("[WAIT]", "Waiting for milestones...", "Scanning stdout and transcript stream")
        else:
            for m in milestones:
                icon = "[OK]"
                style = "bold green"
                if m.milestone_type == "MILESTONE_TESTS_PASSED":
                    icon = "[TESTS]"
                elif m.milestone_type == "MILESTONE_PR_CREATED":
                    icon = "[PR]"
                elif m.milestone_type == "MILESTONE_BUILD_SUCCESS":
                    icon = "[BUILD]"
                elif m.milestone_type == "MILESTONE_COMMIT_PUSHED":
                    icon = "[PUSH]"

                detail = m.raw_text[:50]
                if "passed_count" in m.metadata:
                    detail = f"{m.metadata['passed_count']} tests passed"
                elif "url" in m.metadata:
                    detail = m.metadata["url"]

                table.add_row(Text(icon, style=style), Text(m.milestone_type, style=style), detail)

        return Panel(table, title="[bold][*] Reached Milestones[/bold]", border_style="cyan")

    def generate_subagent_tree(self) -> Panel:
        """Render active agent hierarchy and depth governor."""
        root_tree = Tree("[Agent] Coordinator (Root, Depth 0)")

        with self._lock:
            if self.breaker and self.breaker.subagents:
                nodes = dict(self.breaker.subagents)
                max_depth = self.breaker.max_depth
            else:
                nodes = {}
                max_depth = 2

        if not nodes:
            root_tree.add("[dim italic]No child subagents spawned (Direct Execution)[/dim italic]")
        else:
            node_map: Dict[str, Any] = {}
            for agent_id, node in nodes.items():
                status_color = "green" if node.status == "active" else ("yellow" if node.status == "completed" else "red")
                label = f"[{status_color}]{node.role}[/{status_color}] (id: {agent_id}, d={node.depth}/{max_depth})"
                if node.pid:
                    label += f" [dim]PID:{node.pid}[/dim]"

                # Attach to root or parent
                if node.parent_id and node.parent_id in node_map:
                    branch = node_map[node.parent_id].add(label)
                else:
                    branch = root_tree.add(label)
                node_map[agent_id] = branch

        return Panel(root_tree, title="[bold][+] Subagent Hierarchy (Anti-Matryoshka)[/bold]", border_style="blue")

    def generate_telemetry_panel(self) -> Panel:
        """Render telemetry and budget consumption gauges."""
        with self._lock:
            telem = self._telemetry
            tokens_used = self.breaker.tokens_used if self.breaker else 0
            token_budget = self.breaker.token_budget if self.breaker else 100_000
            time_budget = self.breaker.time_budget_sec if self.breaker else 600.0

        elapsed = time.time() - self._start_time
        time_pct = min(100.0, (elapsed / time_budget) * 100) if time_budget > 0 else 0
        token_pct = min(100.0, (tokens_used / token_budget) * 100) if token_budget > 0 else 0

        table = Table(box=None, expand=True)
        table.add_column("Metric", style="bold", width=18)
        table.add_column("Value", width=22)
        table.add_column("Limit", width=18)

        table.add_row(
            "Elapsed Time",
            f"{elapsed:.1f}s ({time_pct:.0f}%)",
            f"{time_budget:.0f}s limit",
        )
        table.add_row(
            "Tokens Consumed",
            f"{tokens_used:,} ({token_pct:.0f}%)",
            f"{token_budget:,} max",
        )
        if telem:
            table.add_row(
                "Transcript Stream",
                f"{telem.lines_read} lines / {telem.bytes_read / 1024:.1f} KB",
                f"{telem.tool_call_count} tool calls",
            )
            if telem.active_tool:
                table.add_row("Active Tool", f"[cyan]{telem.active_tool}[/cyan]", "In progress")

        return Panel(table, title="[bold][#] Telemetry & Budget Governor[/bold]", border_style="magenta")

    def generate_activity_panel(self) -> Panel:
        """Render recent activity and alert feed."""
        with self._lock:
            alerts = list(self._alerts)
            activities = list(self._activities)

        items: List[Text] = []
        for alert in alerts:
            items.append(Text(alert, style="bold yellow"))
        for act in activities[-6:]:
            items.append(Text(act, style="white"))

        if not items:
            items.append(Text("No events recorded yet.", style="dim"))

        feed_group = Group(*items)
        return Panel(feed_group, title="[bold][>] Glassbox Event Stream[/bold]", border_style="white")

    def render_layout(self) -> Layout:
        """Compose the full grid layout for the HUD."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=11),
        )
        layout["main"].split_row(
            Layout(name="milestones", ratio=1),
            Layout(name="agents", ratio=1),
        )
        layout["footer"].split_row(
            Layout(name="telemetry", ratio=1),
            Layout(name="activity", ratio=1),
        )

        layout["header"].update(self.generate_header())
        layout["milestones"].update(self.generate_milestone_panel())
        layout["agents"].update(self.generate_subagent_tree())
        layout["telemetry"].update(self.generate_telemetry_panel())
        layout["activity"].update(self.generate_activity_panel())

        return layout

    def render_snapshot(self) -> str:
        """Render a plain-text string representation of current HUD state."""
        buffer = io.StringIO()
        snap_console = Console(file=buffer, force_terminal=False, color_system=None, width=140)
        snap_console.print(self.render_layout())
        return buffer.getvalue()

    def start(self, refresh_per_second: float = 4.0) -> Live:
        """Start the interactive live display."""
        self._live = Live(
            self.render_layout(),
            console=self.console,
            refresh_per_second=refresh_per_second,
            transient=False,
        )
        self._live.start()
        return self._live

    def update(self) -> None:
        """Update live view content."""
        if self._live:
            self._live.update(self.render_layout())

    def stop(self) -> None:
        """Stop interactive live display."""
        if self._live:
            self._live.stop()
            self._live = None
