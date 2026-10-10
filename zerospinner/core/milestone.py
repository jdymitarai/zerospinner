"""Milestone detection and fast-path preemptive notification for agent executions."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Pattern, Union

# String constants for milestone identifiers
MILESTONE_TESTS_PASSED = "MILESTONE_TESTS_PASSED"
MILESTONE_PR_CREATED = "MILESTONE_PR_CREATED"
MILESTONE_BUILD_SUCCESS = "MILESTONE_BUILD_SUCCESS"
MILESTONE_COMMIT_PUSHED = "MILESTONE_COMMIT_PUSHED"
MILESTONE_CUSTOM = "MILESTONE_CUSTOM"


class MilestoneType(str, Enum):
    """Supported milestone categories."""

    TESTS_PASSED = MILESTONE_TESTS_PASSED
    PR_CREATED = MILESTONE_PR_CREATED
    BUILD_SUCCESS = MILESTONE_BUILD_SUCCESS
    COMMIT_PUSHED = MILESTONE_COMMIT_PUSHED
    CUSTOM = MILESTONE_CUSTOM


@dataclass
class MilestoneEvent:
    """Represents a detected milestone occurrence."""

    milestone_type: str
    timestamp: float = field(default_factory=time.time)
    source: str = "stdout"
    raw_text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert milestone event to dictionary format."""
        return {
            "milestone_type": self.milestone_type,
            "timestamp": self.timestamp,
            "source": self.source,
            "raw_text": self.raw_text,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MilestoneEvent:
        """Create milestone event from dictionary format."""
        return cls(
            milestone_type=data.get("milestone_type", MILESTONE_CUSTOM),
            timestamp=data.get("timestamp", time.time()),
            source=data.get("source", "unknown"),
            raw_text=data.get("raw_text", ""),
            metadata=data.get("metadata", {}),
        )

    def __str__(self) -> str:
        meta_str = f" ({self.metadata})" if self.metadata else ""
        return f"[{self.milestone_type}] at {self.timestamp:.2f} via {self.source}{meta_str}"


class MilestoneDetector:
    """Scans agent output streams, logs, and JSONL transcripts for key milestones."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[MilestoneEvent], None]] = []
        self._history: List[MilestoneEvent] = []

        # Standard heuristics regex patterns
        self._patterns: Dict[str, List[Pattern[str]]] = {
            MILESTONE_TESTS_PASSED: [
                # pytest: "5 passed in 0.23s", "==== 12 passed in 1.45s ===="
                re.compile(r"(?:={3,}\s*)?(\d+)\s+passed(?:,\s*\d+\s+warnings?)?.*in\s+([\d\.]+)s", re.IGNORECASE),
                # jest / vitest / mocha: "Tests: 12 passed, 12 total"
                re.compile(r"Tests:\s+(\d+)\s+passed,\s*(\d+)\s+total", re.IGNORECASE),
                # go test: "PASS" or "ok  github.com/... 0.123s"
                re.compile(r"^PASS$", re.MULTILINE),
                re.compile(r"^ok\s+([^\s]+)\s+([\d\.]+)s", re.MULTILINE),
                # cargo test: "test result: ok. 10 passed; 0 failed"
                re.compile(r"test result:\s+ok\.\s+(\d+)\s+passed;\s*(\d+)\s+failed", re.IGNORECASE),
                # python unittest: "Ran 8 tests in 0.012s\n\nOK"
                re.compile(r"Ran\s+(\d+)\s+tests?\s+in\s+([\d\.]+)s\s*\n\s*OK", re.IGNORECASE),
                re.compile(r"^OK(?:\s*\(skipped=\d+\))?$", re.MULTILINE),
            ],
            MILESTONE_PR_CREATED: [
                # GitHub PR URL anywhere in line
                re.compile(r"https?://github\.com/([\w\.-]+)/([\w\.-]+)/pull/(\d+)", re.IGNORECASE),
                # GitLab MR URL anywhere in line
                re.compile(r"https?://gitlab\.com/([\w\.-]+)/([\w\.-]+)/-/merge_requests/(\d+)", re.IGNORECASE),
                # gh pr create output: "Created pull request #42 (title)"
                re.compile(r"(?:created|opened)\s+(?:new\s+)?pull\s+request\s+#?(\d+)", re.IGNORECASE),
            ],
            MILESTONE_BUILD_SUCCESS: [
                # Gradle: BUILD SUCCESSFUL in 2s
                re.compile(r"BUILD SUCCESSFUL\s+in\s+([\w\s\.]+)", re.IGNORECASE),
                # Maven: [INFO] BUILD SUCCESS
                re.compile(r"\[INFO\]\s+BUILD\s+SUCCESS", re.IGNORECASE),
                # Bazel: Build completed successfully
                re.compile(r"(?:INFO:\s*)?Build completed successfully", re.IGNORECASE),
                # Cargo: Finished release [optimized] target(s)
                re.compile(r"Finished\s+[\w\-]+\s+(?:\[[\w\s\+]+\]\s+)?target\(s\)", re.IGNORECASE),
                # Webpack / Vite / Rollup: built in 234ms
                re.compile(r"built in\s+([\d\.]+(?:ms|s))", re.IGNORECASE),
                # CMake: [100%] Built target
                re.compile(r"\[100%\]\s+Built\s+target\s+(\w+)", re.IGNORECASE),
                # Generic ninja / make: build succeeded
                re.compile(r"build\s+succeeded", re.IGNORECASE),
            ],
            MILESTONE_COMMIT_PUSHED: [
                # git push: "To github.com:owner/repo.git"
                re.compile(r"To\s+(?:https://|git@)[\w\.-]+[:/][\w\.-]+(?:/[\w\.-]+)+\.git", re.IGNORECASE),
                # git commit: "[main 1a2b3c4] commit title"
                re.compile(r"\[([a-zA-Z0-9_\-\./]+)\s+([a-f0-9]{7,40})\]", re.IGNORECASE),
                # push refspec: "a1b2c3d..e4f5a6b main -> main"
                re.compile(r"([a-f0-9]{7,40})\.\.([a-f0-9]{7,40})\s+(\S+)\s+->\s+(\S+)", re.IGNORECASE),
            ],
        }

    def on_milestone(self, callback: Callable[[MilestoneEvent], None]) -> None:
        """Register a callback for fast-path notification upon milestone detection."""
        with self._lock:
            self._callbacks.append(callback)

    def add_pattern(self, milestone_type: str, pattern: Union[str, Pattern[str]]) -> None:
        """Add a custom regex pattern to detect a specific milestone."""
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        with self._lock:
            if milestone_type not in self._patterns:
                self._patterns[milestone_type] = []
            self._patterns[milestone_type].append(compiled)

    def scan_line(self, line: str, source: str = "stdout") -> List[MilestoneEvent]:
        """Scan a single line of text for milestones."""
        return self.scan_chunk(line, source=source)

    def scan_chunk(self, text: str, source: str = "stdout") -> List[MilestoneEvent]:
        """Scan a chunk of output text for milestone patterns."""
        if not text:
            return []

        matched_events: List[MilestoneEvent] = []

        with self._lock:
            active_patterns = {k: list(v) for k, v in self._patterns.items()}

        for milestone_type, patterns in active_patterns.items():
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    metadata: Dict[str, Any] = {"matched_pattern": pattern.pattern}
                    groups = match.groups()
                    if groups:
                        metadata["groups"] = groups

                    # Specific metadata extractions
                    if milestone_type == MILESTONE_TESTS_PASSED and groups:
                        try:
                            metadata["passed_count"] = int(groups[0])
                        except (ValueError, TypeError):
                            pass
                    elif milestone_type == MILESTONE_PR_CREATED:
                        metadata["url"] = match.group(0)
                        if groups:
                            metadata["pr_number"] = groups[-1]
                    elif milestone_type == MILESTONE_COMMIT_PUSHED and len(groups) >= 2:
                        metadata["commit"] = groups[1]

                    event = MilestoneEvent(
                        milestone_type=milestone_type,
                        timestamp=time.time(),
                        source=source,
                        raw_text=match.group(0).strip(),
                        metadata=metadata,
                    )
                    matched_events.append(event)
                    self.emit(event)
                    # Once a milestone type is matched in this chunk, avoid duplicate events of same type from other patterns
                    break

        return matched_events

    def scan_transcript_entry(self, entry: Dict[str, Any]) -> List[MilestoneEvent]:
        """Scan a structured agent transcript entry (e.g. Claude Code or Antigravity JSONL)."""
        if not isinstance(entry, dict):
            return []

        collected_texts: List[tuple[str, str]] = []

        # Common transcript keys
        if "content" in entry and isinstance(entry["content"], str):
            collected_texts.append((entry["content"], "transcript_content"))

        # Tool calls or execution outputs
        if "output" in entry and isinstance(entry["output"], str):
            collected_texts.append((entry["output"], "tool_output"))

        # Message lists or tool calls nested inside messages
        if "message" in entry and isinstance(entry["message"], dict):
            msg = entry["message"]
            if "content" in msg and isinstance(msg["content"], str):
                collected_texts.append((msg["content"], "message_content"))
            if "parts" in msg and isinstance(msg["parts"], list):
                for part in msg["parts"]:
                    if isinstance(part, dict) and "text" in part:
                        collected_texts.append((part["text"], "part_text"))

        # Tool execution results in Anthropic/OpenAI JSON
        if "result" in entry:
            res = entry["result"]
            if isinstance(res, str):
                collected_texts.append((res, "tool_result"))
            elif isinstance(res, dict) and "stdout" in res:
                collected_texts.append((str(res["stdout"]), "tool_stdout"))

        events: List[MilestoneEvent] = []
        for text, src in collected_texts:
            new_events = self.scan_chunk(text, source=src)
            events.extend(new_events)

        return events

    def emit(self, event: MilestoneEvent) -> None:
        """Record and preemptively dispatch a milestone event to all registered listeners."""
        with self._lock:
            self._history.append(event)
            callbacks = list(self._callbacks)

        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                # Defensive isolation: callback failures must never disrupt agent monitoring
                pass

    def has_milestone(self, milestone_type: str) -> bool:
        """Check if a specific milestone type has been detected."""
        with self._lock:
            return any(e.milestone_type == milestone_type for e in self._history)

    def get_milestones(self, milestone_type: Optional[str] = None) -> List[MilestoneEvent]:
        """Retrieve detected milestones, optionally filtered by type."""
        with self._lock:
            if milestone_type is None:
                return list(self._history)
            return [e for e in self._history if e.milestone_type == milestone_type]

    def latest_milestone(self) -> Optional[MilestoneEvent]:
        """Return the most recently detected milestone event, or None."""
        with self._lock:
            return self._history[-1] if self._history else None

    def clear(self) -> None:
        """Reset detected history."""
        with self._lock:
            self._history.clear()
