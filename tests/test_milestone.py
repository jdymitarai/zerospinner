"""Tests for MilestoneDetector and MilestoneEvent."""

import pytest
from zerospinner.core.milestone import (
    MilestoneDetector,
    MilestoneEvent,
    MilestoneType,
    MILESTONE_TESTS_PASSED,
    MILESTONE_PR_CREATED,
    MILESTONE_BUILD_SUCCESS,
    MILESTONE_COMMIT_PUSHED,
    MILESTONE_CUSTOM,
)


def test_milestone_event_serialization():
    event = MilestoneEvent(
        milestone_type=MILESTONE_TESTS_PASSED,
        source="stdout",
        raw_text="12 passed in 0.3s",
        metadata={"passed_count": 12},
    )
    d = event.to_dict()
    assert d["milestone_type"] == MILESTONE_TESTS_PASSED
    assert d["metadata"]["passed_count"] == 12

    restored = MilestoneEvent.from_dict(d)
    assert restored.milestone_type == event.milestone_type
    assert restored.raw_text == event.raw_text
    assert restored.metadata == event.metadata
    assert str(restored) != ""


def test_detect_pytest():
    detector = MilestoneDetector()
    events = detector.scan_chunk("================= 15 passed in 0.42s =================")
    assert len(events) == 1
    assert events[0].milestone_type == MILESTONE_TESTS_PASSED
    assert events[0].metadata.get("passed_count") == 15
    assert detector.has_milestone(MILESTONE_TESTS_PASSED)


def test_detect_jest_and_vitest():
    detector = MilestoneDetector()
    text = "PASS src/calc.test.ts\nTests: 8 passed, 8 total\nTime: 1.2s"
    events = detector.scan_chunk(text)
    assert len(events) >= 1
    assert any(e.milestone_type == MILESTONE_TESTS_PASSED for e in events)


def test_detect_cargo_test():
    detector = MilestoneDetector()
    text = "test result: ok. 14 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"
    events = detector.scan_chunk(text)
    assert len(events) == 1
    assert events[0].milestone_type == MILESTONE_TESTS_PASSED
    assert events[0].metadata.get("passed_count") == 14


def test_detect_python_unittest():
    detector = MilestoneDetector()
    text = "Ran 6 tests in 0.005s\n\nOK"
    events = detector.scan_chunk(text)
    assert len(events) >= 1
    assert events[0].milestone_type == MILESTONE_TESTS_PASSED


def test_detect_github_pr():
    detector = MilestoneDetector()
    text = "Created pull request #108: Fix memory leak\nhttps://github.com/google/zerospinner/pull/108"
    events = detector.scan_chunk(text)
    assert len(events) == 1
    assert events[0].milestone_type == MILESTONE_PR_CREATED
    assert events[0].metadata.get("url") == "https://github.com/google/zerospinner/pull/108"


def test_detect_build_success():
    detector = MilestoneDetector()
    for sample in [
        "BUILD SUCCESSFUL in 3s",
        "[INFO] BUILD SUCCESS",
        "INFO: Build completed successfully, 12 total actions",
        "Finished release [optimized] target(s) in 4.2s",
        "webpack 5.0.0 built in 142ms",
    ]:
        detector.clear()
        events = detector.scan_chunk(sample)
        assert len(events) >= 1, f"Failed on {sample}"
        assert events[0].milestone_type == MILESTONE_BUILD_SUCCESS


def test_detect_commit_pushed():
    detector = MilestoneDetector()
    text = "To github.com:org/repo.git\n   a1b2c3d4e5f6..b2c3d4e5f6a1  main -> main"
    events = detector.scan_chunk(text)
    assert len(events) == 1
    assert events[0].milestone_type == MILESTONE_COMMIT_PUSHED


def test_fast_path_callbacks():
    detector = MilestoneDetector()
    recorded = []

    def cb(event: MilestoneEvent):
        recorded.append(event)

    detector.on_milestone(cb)
    detector.scan_chunk("9 passed in 0.12s")

    assert len(recorded) == 1
    assert recorded[0].milestone_type == MILESTONE_TESTS_PASSED


def test_callback_error_isolation():
    detector = MilestoneDetector()

    def buggy_cb(event: MilestoneEvent):
        raise RuntimeError("Explosion in callback")

    detector.on_milestone(buggy_cb)
    # Should not raise exception
    events = detector.scan_chunk("9 passed in 0.12s")
    assert len(events) == 1


def test_custom_pattern():
    detector = MilestoneDetector()
    detector.add_pattern(MILESTONE_CUSTOM, r"ALL_BENCHMARKS_COMPLETE")

    events = detector.scan_chunk("Task finished: ALL_BENCHMARKS_COMPLETE in 100ms")
    assert len(events) == 1
    assert events[0].milestone_type == MILESTONE_CUSTOM


def test_scan_transcript_entry():
    detector = MilestoneDetector()
    entry = {
        "role": "assistant",
        "content": "The test execution has concluded.",
        "result": {
            "stdout": "================ 4 passed in 0.05s ================"
        }
    }
    events = detector.scan_transcript_entry(entry)
    assert len(events) == 1
    assert events[0].milestone_type == MILESTONE_TESTS_PASSED


def test_edge_cases_empty_and_noise():
    detector = MilestoneDetector()
    assert detector.scan_chunk("") == []
    assert detector.scan_chunk("random log line without milestones") == []
    assert detector.latest_milestone() is None
