"""Tests for TranscriptWatchdog, stall detection, and loop deadlock identification."""

import json
import time
from pathlib import Path
import pytest

from zerospinner.core.milestone import MilestoneDetector, MILESTONE_TESTS_PASSED
from zerospinner.core.watchdog import TranscriptWatchdog, WatchdogAlert


def test_watchdog_tail_file(temp_transcript: Path):
    detector = MilestoneDetector()
    watchdog = TranscriptWatchdog(
        transcript_path=temp_transcript,
        poll_interval=0.05,
        detector=detector,
    )

    with open(temp_transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "Run tests"}) + "\n")
        f.write(json.dumps({"role": "assistant", "output": "5 passed in 0.1s"}) + "\n")

    lines_read = watchdog.step()
    assert lines_read == 2

    telemetry = watchdog.get_telemetry()
    assert telemetry.lines_read == 2
    assert detector.has_milestone(MILESTONE_TESTS_PASSED)


def test_watchdog_loop_deadlock_detection():
    detector = MilestoneDetector()
    watchdog = TranscriptWatchdog(loop_threshold=3, detector=detector)

    alerts = []
    watchdog.on_alert(lambda a: alerts.append(a))

    # Feed 3 identical tool calls
    entry = {
        "role": "assistant",
        "tool": "view_file",
        "input": {"path": "/app/main.py"},
    }
    watchdog.feed_entry(entry)
    assert not watchdog.get_telemetry().is_looping

    watchdog.feed_entry(entry)
    assert not watchdog.get_telemetry().is_looping

    watchdog.feed_entry(entry)
    telemetry = watchdog.get_telemetry()
    assert telemetry.is_looping
    assert len(alerts) == 1
    assert alerts[0].alert_type == "LOOP_DEADLOCK"
    assert "view_file" in alerts[0].details


def test_watchdog_stall_detection():
    detector = MilestoneDetector()
    watchdog = TranscriptWatchdog(stall_threshold_sec=0.1, detector=detector)

    alerts = []
    watchdog.on_alert(lambda a: alerts.append(a))

    watchdog.feed_entry({"tool_name": "bash", "args": {"cmd": "make build"}})
    assert not watchdog.get_telemetry().is_stalled

    # Wait past stall threshold
    time.sleep(0.15)
    watchdog.check_health()

    assert watchdog.get_telemetry().is_stalled
    assert len(alerts) >= 1
    assert alerts[0].alert_type == "STALL"


def test_watchdog_thread_lifecycle(temp_transcript: Path):
    watchdog = TranscriptWatchdog(transcript_path=temp_transcript, poll_interval=0.05)
    watchdog.start()
    assert watchdog._worker_thread is not None and watchdog._worker_thread.is_alive()

    # Append data while running in background
    with open(temp_transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps({"content": "hello world"}) + "\n")

    time.sleep(0.15)
    assert watchdog.get_telemetry().lines_read >= 1

    watchdog.stop()
    assert not watchdog._worker_thread.is_alive()


def test_watchdog_context_manager(temp_transcript: Path):
    with TranscriptWatchdog(transcript_path=temp_transcript, poll_interval=0.05) as watchdog:
        assert watchdog._worker_thread is not None and watchdog._worker_thread.is_alive()
    assert not watchdog._worker_thread.is_alive()


def test_partial_lines_buffering(temp_transcript: Path):
    watchdog = TranscriptWatchdog(transcript_path=temp_transcript, poll_interval=0.05)

    # Write incomplete line
    with open(temp_transcript, "a", encoding="utf-8") as f:
        f.write('{"content": "part')

    read1 = watchdog.step()
    assert read1 == 0

    # Complete line
    with open(temp_transcript, "a", encoding="utf-8") as f:
        f.write('ial finished"}\n')

    read2 = watchdog.step()
    assert read2 == 1
    assert watchdog.get_telemetry().lines_read == 1


def test_watchdog_periodic_report():
    reports = []
    watchdog = TranscriptWatchdog(report_interval_sec=0.05)
    watchdog.on_periodic_report(lambda telem: reports.append(telem))

    watchdog.feed_entry({"role": "user", "content": "Hello"})
    time.sleep(0.08)
    watchdog.check_health()

    assert len(reports) >= 1
    report = reports[0]
    assert report.lines_read == 1
    assert report.uptime_sec > 0
    assert "lines_per_min" in report.to_dict()
    assert "tokens_saved_estimate" in report.to_dict()

