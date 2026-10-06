"""Active transcript tailer and deadlock/stall watchdog for autonomous agents."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Union

from zerospinner.core.milestone import MilestoneDetector


@dataclass
class WatchdogAlert:
    """Alert raised by the watchdog for stalls, loops, or anomalies."""

    alert_type: str  # "STALL", "LOOP_DEADLOCK", "PARSE_ERROR"
    timestamp: float = field(default_factory=time.time)
    details: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_type": self.alert_type,
            "timestamp": self.timestamp,
            "details": self.details,
            "metadata": self.metadata,
        }


@dataclass
class WatchdogTelemetry:
    """Current health and progress telemetry snapshot."""

    lines_read: int = 0
    bytes_read: int = 0
    last_activity_ts: float = field(default_factory=time.time)
    tool_call_count: int = 0
    consecutive_repeats: int = 0
    is_stalled: bool = False
    is_looping: bool = False
    active_tool: Optional[str] = None
    alerts_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lines_read": self.lines_read,
            "bytes_read": self.bytes_read,
            "last_activity_ts": self.last_activity_ts,
            "tool_call_count": self.tool_call_count,
            "consecutive_repeats": self.consecutive_repeats,
            "is_stalled": self.is_stalled,
            "is_looping": self.is_looping,
            "active_tool": self.active_tool,
            "alerts_count": self.alerts_count,
        }


class TranscriptWatchdog:
    """Non-invasive active watcher tailing JSONL transcripts and process streams."""

    def __init__(
        self,
        transcript_path: Optional[Union[str, Path]] = None,
        poll_interval: float = 1.0,
        stall_threshold_sec: float = 30.0,
        loop_threshold: int = 3,
        detector: Optional[MilestoneDetector] = None,
    ) -> None:
        self.transcript_path = Path(transcript_path) if transcript_path else None
        self.poll_interval = max(0.01, poll_interval)
        self.stall_threshold_sec = max(0.01, stall_threshold_sec)
        self.loop_threshold = max(2, loop_threshold)
        self.detector = detector or MilestoneDetector()

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self._file_offset: int = 0
        self._line_buffer: str = ""
        self._lines_read: int = 0
        self._bytes_read: int = 0
        self._last_activity_ts: float = time.time()
        self._tool_call_count: int = 0
        self._consecutive_repeats: int = 0
        self._is_stalled: bool = False
        self._is_looping: bool = False
        self._active_tool: Optional[str] = None
        self._active_tool_start_ts: Optional[float] = None

        # Signatures of recent tool calls to detect exact repeats and cycling
        self._recent_signatures: Deque[str] = deque(maxlen=20)
        self._alerts: List[WatchdogAlert] = []
        self._alert_callbacks: List[Callable[[WatchdogAlert], None]] = []
        self._telemetry_callbacks: List[Callable[[WatchdogTelemetry], None]] = []

    def on_alert(self, callback: Callable[[WatchdogAlert], None]) -> None:
        """Register a callback for watchdog alerts."""
        with self._lock:
            self._alert_callbacks.append(callback)

    def on_telemetry(self, callback: Callable[[WatchdogTelemetry], None]) -> None:
        """Register a callback for telemetry updates."""
        with self._lock:
            self._telemetry_callbacks.append(callback)

    def start(self) -> None:
        """Start the watchdog background thread."""
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._watch_loop, name="ZeroSpinner-TranscriptWatchdog", daemon=True
            )
            self._worker_thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Stop the watchdog background thread gracefully."""
        self._stop_event.set()
        thread = None
        with self._lock:
            thread = self._worker_thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def __enter__(self) -> TranscriptWatchdog:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def get_telemetry(self) -> WatchdogTelemetry:
        """Return a snapshot of current telemetry data."""
        with self._lock:
            return WatchdogTelemetry(
                lines_read=self._lines_read,
                bytes_read=self._bytes_read,
                last_activity_ts=self._last_activity_ts,
                tool_call_count=self._tool_call_count,
                consecutive_repeats=self._consecutive_repeats,
                is_stalled=self._is_stalled,
                is_looping=self._is_looping,
                active_tool=self._active_tool,
                alerts_count=len(self._alerts),
            )

    def get_alerts(self) -> List[WatchdogAlert]:
        """Return history of all raised alerts."""
        with self._lock:
            return list(self._alerts)

    def feed_text(self, text: str, source: str = "stream") -> None:
        """Directly feed a chunk of raw text or stream output to the watchdog."""
        if not text:
            return

        now = time.time()
        with self._lock:
            self._bytes_read += len(text.encode("utf-8", errors="ignore"))
            self._last_activity_ts = now
            self._is_stalled = False

        # Scan for milestones via detector
        self.detector.scan_chunk(text, source=source)

    def feed_entry(self, entry: Dict[str, Any]) -> None:
        """Feed a single structured JSONL transcript entry."""
        now = time.time()
        with self._lock:
            self._lines_read += 1
            self._last_activity_ts = now
            self._is_stalled = False

        # Check for tool call invocations or outputs
        tool_info = self._extract_tool_info(entry)
        if tool_info:
            tool_name, tool_sig = tool_info
            self._handle_tool_call(tool_name, tool_sig)

        # Forward entry to detector
        self.detector.scan_transcript_entry(entry)

    def check_health(self) -> WatchdogTelemetry:
        """Evaluate stall status and emit alerts if stalled."""
        now = time.time()
        alert_to_emit: Optional[WatchdogAlert] = None

        with self._lock:
            idle_time = now - self._last_activity_ts
            if idle_time > self.stall_threshold_sec:
                if not self._is_stalled:
                    self._is_stalled = True
                    details = f"Agent stalled for {idle_time:.1f}s with no transcript progress."
                    if self._active_tool:
                        details += f" Active tool call: {self._active_tool}"
                    alert_to_emit = WatchdogAlert(
                        alert_type="STALL",
                        timestamp=now,
                        details=details,
                        metadata={
                            "idle_sec": idle_time,
                            "active_tool": self._active_tool,
                        },
                    )
                    self._alerts.append(alert_to_emit)
            else:
                self._is_stalled = False

        if alert_to_emit:
            self._notify_alert(alert_to_emit)

        telem = self.get_telemetry()
        self._notify_telemetry(telem)
        return telem

    def step(self) -> int:
        """Perform one poll step over the target transcript file. Returns number of lines read."""
        if not self.transcript_path:
            self.check_health()
            return 0

        if not self.transcript_path.exists():
            self.check_health()
            return 0

        lines_processed = 0
        try:
            with open(self.transcript_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._file_offset)
                new_data = f.read()
                current_pos = f.tell()
                self._file_offset = current_pos

            if new_data:
                combined = self._line_buffer + new_data
                lines = combined.split("\n")
                # Last segment might be incomplete
                self._line_buffer = lines[-1]
                complete_lines = lines[:-1]

                for raw_line in complete_lines:
                    line = raw_line.strip()
                    if not line:
                        continue
                    lines_processed += 1
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict):
                            self.feed_entry(entry)
                        else:
                            self.feed_text(line, source="transcript_line")
                    except json.JSONDecodeError:
                        # Fallback to plain text scan
                        self.feed_text(line, source="transcript_raw")
        except (OSError, IOError):
            # Guard against concurrent writes or file locks
            pass

        self.check_health()
        return lines_processed

    def _watch_loop(self) -> None:
        """Internal polling loop for background thread."""
        while not self._stop_event.is_set():
            self.step()
            self._stop_event.wait(self.poll_interval)

    def _extract_tool_info(self, entry: Dict[str, Any]) -> Optional[tuple[str, str]]:
        """Extract tool name and signature from various agent transcript formats."""
        # 1. Antigravity / Gemini / Claude Code style tool call
        name: Optional[str] = None
        args: Any = None

        if "tool_name" in entry:
            name = str(entry["tool_name"])
            args = entry.get("arguments") or entry.get("args")
        elif "tool" in entry:
            name = str(entry["tool"])
            args = entry.get("input") or entry.get("parameters")
        elif "type" in entry and entry["type"] in ("tool_use", "tool_call"):
            name = str(entry.get("name", "unknown_tool"))
            args = entry.get("input") or entry.get("arguments")
        elif "toolAction" in entry:
            name = str(entry.get("toolAction"))
            args = entry.get("toolSummary")
        elif "name" in entry and ("parameters" in entry or "input" in entry):
            name = str(entry["name"])
            args = entry.get("parameters") or entry.get("input")

        if not name:
            return None

        # Build stable signature
        sig_body = json.dumps(args, sort_keys=True, default=str) if args is not None else ""
        hasher = hashlib.sha256(f"{name}:{sig_body}".encode("utf-8", errors="ignore"))
        sig = f"{name}:{hasher.hexdigest()[:12]}"
        return name, sig

    def _handle_tool_call(self, tool_name: str, tool_sig: str) -> None:
        """Track tool invocation, detect loops or deadlocks."""
        alert_to_emit: Optional[WatchdogAlert] = None
        with self._lock:
            self._tool_call_count += 1
            self._active_tool = tool_name
            self._active_tool_start_ts = time.time()

            # Check for repetitive identical tool signatures
            if self._recent_signatures and self._recent_signatures[-1] == tool_sig:
                self._consecutive_repeats += 1
            else:
                self._consecutive_repeats = 1

            self._recent_signatures.append(tool_sig)

            # Check repeat deadlock
            if self._consecutive_repeats >= self.loop_threshold:
                self._is_looping = True
                alert_to_emit = WatchdogAlert(
                    alert_type="LOOP_DEADLOCK",
                    timestamp=time.time(),
                    details=(
                        f"Detected repetitive loop deadlock: tool '{tool_name}' "
                        f"called {self._consecutive_repeats} consecutive times with identical inputs."
                    ),
                    metadata={
                        "tool_name": tool_name,
                        "signature": tool_sig,
                        "consecutive_repeats": self._consecutive_repeats,
                    },
                )
                self._alerts.append(alert_to_emit)
            else:
                self._is_looping = False

        if alert_to_emit:
            self._notify_alert(alert_to_emit)

    def _notify_alert(self, alert: WatchdogAlert) -> None:
        with self._lock:
            callbacks = list(self._alert_callbacks)
        for cb in callbacks:
            try:
                cb(alert)
            except Exception:
                pass

    def _notify_telemetry(self, telem: WatchdogTelemetry) -> None:
        with self._lock:
            callbacks = list(self._telemetry_callbacks)
        for cb in callbacks:
            try:
                cb(telem)
            except Exception:
                pass
