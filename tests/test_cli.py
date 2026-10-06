"""Tests for zspin command line interface."""

import sys
from unittest.mock import patch
import pytest

from zerospinner.cli import create_parser, main, run_demo


def test_cli_parser_subcommands():
    parser = create_parser()

    # Watch parser
    args_watch = parser.parse_args(["watch", "transcript.jsonl", "--poll", "2.5"])
    assert args_watch.subcommand == "watch"
    assert args_watch.transcript_path == "transcript.jsonl"
    assert args_watch.poll == 2.5

    # Run parser
    args_run = parser.parse_args(["run", "--max-depth", "3", "--", "pytest", "tests/"])
    assert args_run.subcommand == "run"
    assert args_run.max_depth == 3
    assert args_run.command == ["--", "pytest", "tests/"]

    # MCP parser
    args_mcp = parser.parse_args(["mcp"])
    assert args_mcp.subcommand == "mcp"

    # Demo parser
    args_demo = parser.parse_args(["demo", "--fast", "--headless"])
    assert args_demo.subcommand == "demo"
    assert args_demo.fast is True
    assert args_demo.headless is True


def test_cli_demo_execution():
    parser = create_parser()
    args = parser.parse_args(["demo", "--fast", "--headless"])
    exit_code = run_demo(args)
    assert exit_code == 0


def test_cli_main_help():
    with patch("sys.stdout") as mock_stdout:
        exit_code = main(["--help"])
        assert exit_code == 0


def test_cli_run_simple_command():
    exit_code = main(["run", "--no-hud", "--", sys.executable, "-c", "print('hello from subagent')"])
    assert exit_code == 0
