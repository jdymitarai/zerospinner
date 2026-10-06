"""Test configuration and fixtures for ZeroSpinner test suite."""

import sys
from pathlib import Path
import pytest

# Ensure zerospinner is directly importable
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@pytest.fixture
def temp_transcript(tmp_path):
    """Fixture providing a temporary JSONL transcript file path."""
    file_path = tmp_path / "agent_transcript.jsonl"
    file_path.touch()
    return file_path
