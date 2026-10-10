#!/usr/bin/env python3
"""
ZeroSpinner - Google Bug Hunters 5-Gate Issue Selector
Discovers and scores unassigned, collision-free vulnerability issues across
Google Bug Hunters Tier 1 and OSS-Fuzz repositories according to official rules.
"""

import json
import re
import subprocess
from typing import List, Dict, Any, Optional

# Official Google Bug Hunters Scope & Approved Repositories
APPROVED_REPOS = [
    # Core Infrastructure Data Parsers (3x Memory Safety Multiplier)
    {"upstream": "libjxl/libjxl", "fork": "jdymitarai/libjxl", "parser": True},
    {"upstream": "pnggroup/libpng", "fork": "jdymitarai/libpng", "parser": True},
    {"upstream": "libjpeg-turbo/libjpeg-turbo", "fork": "jdymitarai/libjpeg-turbo", "parser": True},
    {"upstream": "aomediacodec/libavif", "fork": "jdymitarai/libavif", "parser": True},
    {"upstream": "google/double-conversion", "fork": "jdymitarai/double-conversion", "parser": True},
    
    # Tier 1 Essential Infrastructure & Libraries
    {"upstream": "google/nsjail", "fork": "jdymitarai/nsjail", "parser": False},
    {"upstream": "c-ares/c-ares", "fork": "jdymitarai/c-ares", "parser": False},
    {"upstream": "nih-at/libzip", "fork": "jdymitarai/libzip", "parser": False},
    {"upstream": "lz4/lz4", "fork": "jdymitarai/lz4", "parser": False},
    {"upstream": "facebook/zstd", "fork": "jdymitarai/zstd", "parser": False},
    {"upstream": "google/fuzztest", "fork": "jdymitarai/fuzztest", "parser": False},
    {"upstream": "google/libprotobuf-mutator", "fork": "jdymitarai/libprotobuf-mutator", "parser": False},
    {"upstream": "google/highwayhash", "fork": "jdymitarai/highwayhash", "parser": False},
    {"upstream": "google/benchmark", "fork": "jdymitarai/benchmark", "parser": False},
    {"upstream": "google/protobuf", "fork": "jdymitarai/protobuf", "parser": True},
    {"upstream": "google/sentencepiece", "fork": "jdymitarai/sentencepiece", "parser": True},
    {"upstream": "google/jsonnet", "fork": "jdymitarai/jsonnet", "parser": True},
]

# Permanently blacklisted dead, unresponsive, or Copybara-only repositories
BLACKLISTED_REPOS = {
    "google/googletest",
    "google/snappy",
    "google/flatbuffers",
    "google/osv-scalibr",
    "google/osv-scanner",
    "google/go-containerregistry",
    "google/oss-fuzz",
    "jax-ml/jax",
    "openxla/xla",
    "google/or-tools",
    "google-deepmind/optax",
    "grpc/grpc",
}

APPROVED_REPOS = [r for r in APPROVED_REPOS if r["upstream"] not in BLACKLISTED_REPOS]

# Vulnerability attack scenario keyword weights
SECURITY_KEYWORDS = {
    r"\bheap-buffer-overflow\b": 100,
    r"\boob write\b": 100,
    r"\bout-of-bounds write\b": 100,
    r"\buse-after-free\b": 100,
    r"\buse after free\b": 100,
    r"\buaf\b": 90,
    r"\bdouble free\b": 90,
    r"\boob read\b": 80,
    r"\bout-of-bounds read\b": 80,
    r"\bheap out of bounds\b": 80,
    r"\bstack-overflow\b": 80,
    r"\bstack overflow\b": 80,
    r"\binteger overflow\b": 75,
    r"\bsigned overflow\b": 75,
    r"\bunderflow\b": 70,
    r"\bcwe-190\b": 75,
    r"\bcwe-125\b": 80,
    r"\bcwe-787\b": 100,
    r"\bcwe-416\b": 90,
    r"\bcwe-476\b": 50,
    r"\bnull pointer dereference\b": 50,
    r"\bnull-dereference\b": 50,
    r"\bsegfault\b": 60,
    r"\bsigsegv\b": 60,
    r"\bdenial of service\b": 60,
    r"\bdeadlock\b": 50,
}

def check_repo_open_prs(repo: str) -> int:
    """Gate 2: Counts OPEN PRs opened by @me in the target repository."""
    if repo in BLACKLISTED_REPOS:
        return 999
    try:
        cmd = ["gh", "pr", "list", "--repo", repo, "--author", "@me", "--state", "open", "--json", "number"]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            return len(data)
    except Exception:
        pass
    return 0


def check_issue_has_open_pr(repo: str, issue_number: int) -> bool:
    """Gate 4: Verifies if any developer has an OPEN PR addressing this issue."""
    try:
        cmd = ["gh", "pr", "list", "--repo", repo, "--search", str(issue_number), "--state", "open", "--json", "number"]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            return len(data) > 0
    except Exception:
        pass
    return False


DISQUALIFIED_PATTERNS = [
    r"\b(?:slower|slow|faster|speedup|speed-up|benchmark|throughput|latency)\b",
    r"\b(?:performance\s+regression|performance\s+optimization|perf\s+improvement)\b",
    r"\b(?:floating[- ]point\s+rounding|rounding\s+error|precision\s+issue)\b",
    r"\b(?:ui\s+state|cosmetic|formatting|typo|spelling)\b",
    r"\b(?:feature\s+request|enhancement|add\s+support\s+for)\b",
    r"\b(?:import|bazel|cmake|readme|docs|documentation|packaging|setup\.py)\b",
]

def calculate_security_score(title: str, body: str) -> int:
    """Gate 5: Scores the issue based on verified attack scenario impact. Returns 0 if disqualified."""
    t_lower = title.lower()
    text = f"{title}\n{body}".lower()

    # Disqualify non-security issues immediately
    if any(re.search(pat, t_lower) for pat in DISQUALIFIED_PATTERNS):
        return 0

    score = 0
    for pattern, weight in SECURITY_KEYWORDS.items():
        if re.search(pattern, text):
            score = max(score, weight)
    return score


def search_candidate_issues(repo: str, limit: int = 15) -> List[Dict[str, Any]]:
    """
    Searches candidate issues in the repo passing:
    Gate 1: In approved Google Bug Hunters scope
    Gate 3: assignees is empty (unassigned)
    Gate 4: Zero active PR collision
    Gate 5: Clear security impact score > 0
    """
    if repo in BLACKLISTED_REPOS:
        return []
    candidates = []
    try:
        # Search issues with open state and no assignees
        cmd = [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--search", "no:assignee",
            "--limit", str(limit),
            "--json", "number,title,body,assignees,comments"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        if res.returncode != 0:
            return []

        issues = json.loads(res.stdout)
        for iss in issues:
            # Gate 3: Unassigned
            assignees = iss.get("assignees", [])
            if assignees:
                continue

            num = iss.get("number")
            title = iss.get("title", "")
            body = iss.get("body", "")

            # Gate 5: Security Score
            score = calculate_security_score(title, body)
            if score <= 0:
                continue

            # Gate 4: Zero PR collision
            if check_issue_has_open_pr(repo, num):
                continue

            candidates.append({
                "number": num,
                "title": title,
                "body": body,
                "security_score": score,
                "repo": repo
            })

    except Exception:
        pass

    # Sort candidates by security score descending
    candidates.sort(key=lambda x: x.get("security_score", 0), reverse=True)
    return candidates
