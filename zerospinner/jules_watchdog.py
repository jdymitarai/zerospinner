#!/usr/bin/env python3
"""
ZeroSpinner - Google Jules Autonomous Cloud Watchdog (自動回拉看門狗).

Continuously monitors Google Jules cloud microVM sessions, automatically detects
session completion, pulls git unified diffs to the local patch registry, runs
Antigravity Eight-Shield Fortress audits (Semgrep and Google Auditor), compares
parallel candidates, and prepares winning patches for review or PR creation.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add paths
CURRENT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = CURRENT_DIR.parent
WORKSPACE_DIR = PACKAGE_ROOT.parent
TOOLS_DIR = WORKSPACE_DIR / "tools"
for p in (str(CURRENT_DIR), str(PACKAGE_ROOT), str(WORKSPACE_DIR), str(TOOLS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# UTF-8 encoding support
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import google_auditor
except ImportError:
    google_auditor = None

try:
    import sandbox_guard
except ImportError:
    sandbox_guard = None

try:
    import semgrep_scanner
except ImportError:
    semgrep_scanner = None

PATCHES_DIR = WORKSPACE_DIR / "patches"
STATE_FILE = PACKAGE_ROOT / "jules_watchdog_state.json"
ECHELON_STATE_FILE = PACKAGE_ROOT / "echelon_state.json"
PID_FILE = PACKAGE_ROOT / ".jules_watchdog.pid"
STOP_FLAG_FILE = PACKAGE_ROOT / ".jules_watchdog.stop"


@dataclass
class JulesSessionInfo:
    session_id: str
    description: str
    repo: str
    last_active: str
    status: str
    issue_number: Optional[int] = None
    pulled: bool = False
    patch_path: Optional[str] = None
    pulled_at: Optional[float] = None
    lines_added: int = 0
    lines_deleted: int = 0
    has_test: bool = False
    touched_files: List[str] = field(default_factory=list)
    semgrep_findings: int = 0
    google_style_errors: int = 0
    audit_verdict: str = "PENDING"
    surgical_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> JulesSessionInfo:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def get_known_repos() -> List[str]:
    """Retrieves list of connected repositories from jules CLI or fallback list."""
    try:
        res = subprocess.run(
            ["jules", "remote", "list", "--repo"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            shell=True,
        )
        if res.returncode == 0:
            repos = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if repos:
                return repos
    except Exception:
        pass

    return [
        "jdymitarai/abseil-cpp",
        "jdymitarai/sandboxed-api",
        "jdymitarai/protobuf",
        "jdymitarai/re2",
        "jdymitarai/leveldb",
        "jdymitarai/draco",
        "jdymitarai/snappy",
        "jdymitarai/flatbuffers",
        "jdymitarai/benchmark",
        "jdymitarai/googletest",
        "jdymitarai/LiteRT",
    ]


def resolve_full_repo_name(short_or_truncated: str, known_repos: List[str]) -> str:
    """Resolves truncated repo names (e.g. jdymitarai/sandboxed-…) to full name."""
    clean = short_or_truncated.strip().rstrip("…").rstrip(".")
    if not clean or clean == "jdymitarai" or clean == "jdymitarai/":
        return short_or_truncated.strip()

    # Exact or prefix match
    for r in known_repos:
        if r.lower() == clean.lower() or r.lower().startswith(clean.lower()):
            return r

    # Substring match
    for r in known_repos:
        if clean.lower() in r.lower():
            return r

    return short_or_truncated.strip()


def parse_issue_number(description: str) -> Optional[int]:
    """Extracts issue number from session description string."""
    m = re.search(r"#(\d+)", description)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def parse_session_list(raw_text: str, known_repos: Optional[List[str]] = None) -> List[JulesSessionInfo]:
    """Parses tabular output of jules remote list --session into structured objects."""
    if known_repos is None:
        known_repos = get_known_repos()

    sessions: List[JulesSessionInfo] = []
    lines = raw_text.splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("ID") or "Description" in stripped:
            continue

        # Look for leading numeric ID
        m = re.match(r"^(\d{15,25})\s+(.+)$", stripped)
        if not m:
            continue

        session_id = m.group(1)
        rest = m.group(2).strip()

        # Parse from the end: Status, Last active, Repo, Description
        # Status options: Completed, In Progress, Planning, Failed, Cancelled, Awaiting User Feedback
        status_match = re.search(r"\b(Completed|In Progress|Planning|Failed|Cancelled|Awaiting User [a-zA-Z]+)\s*$", rest, re.IGNORECASE)
        status = ""
        if status_match:
            status = status_match.group(1).title()
            rest = rest[:status_match.start()].strip()

        # Last active pattern: e.g. 7m34s ago, 10s ago, 1h ago
        time_match = re.search(r"(\d+[smhd](\d+[smhd])?\s+ago)\s*$", rest, re.IGNORECASE)
        last_active = ""
        if time_match:
            last_active = time_match.group(1).strip()
            rest = rest[:time_match.start()].strip()

        # Repo pattern: e.g. jdymitarai/sandboxed-… or jdymitarai/abseil-cpp
        repo_match = re.search(r"([a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-…]+)\s*$", rest)
        repo_raw = ""
        description = rest
        if repo_match:
            repo_raw = repo_match.group(1).strip()
            description = rest[:repo_match.start()].strip()

        full_repo = resolve_full_repo_name(repo_raw, known_repos)
        issue_num = parse_issue_number(description)

        sessions.append(
            JulesSessionInfo(
                session_id=session_id,
                description=description,
                repo=full_repo,
                last_active=last_active,
                status=status or "Running",
                issue_number=issue_num,
            )
        )

    return sessions


def pull_session_diff(session_id: str) -> Optional[str]:
    """Pulls git unified diff from remote Jules session using jules remote pull."""
    try:
        cmd = ["jules", "remote", "pull", "--session", str(session_id)]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=True,
        )
        if res.returncode == 0 and res.stdout and "diff --git" in res.stdout:
            return res.stdout
        return None
    except Exception as e:
        print(f"[JulesWatchdog] Error pulling session {session_id}: {e}", flush=True)
        return None


def parse_diff_metrics(diff_text: str) -> Tuple[int, int, bool, List[str]]:
    """Analyzes unified diff for lines added, lines deleted, test coverage, and touched files."""
    lines_added = 0
    lines_deleted = 0
    has_test = False
    touched_files: List[str] = []

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            rel_file = line[6:].strip()
            touched_files.append(rel_file)
            lower = rel_file.lower()
            if "_test." in lower or "test_" in lower or "tests/" in lower:
                has_test = True
        elif line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_deleted += 1

    return lines_added, lines_deleted, has_test, touched_files


def find_local_repo_dir(repo_name: str) -> Optional[Path]:
    """Finds matching local repository clone under workspace."""
    base_name = repo_name.split("/")[-1]
    candidates = [
        WORKSPACE_DIR / base_name,
        WORKSPACE_DIR / "repos" / base_name,
    ]
    for c in candidates:
        if c.exists() and (c / ".git").exists():
            return c
    return None


def run_eight_shield_audit(repo_dir: Path, patch_file: Path, touched_files: List[str]) -> Tuple[int, int, str]:
    """
    Applies the patch inside the local repo temporarily, runs Semgrep security scans
    and Google Auditor checks on the touched files, and restores clean git state.
    """
    semgrep_count = 0
    style_error_count = 0

    # Step 1: Check git status is clean before applying
    status_res = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=10,
    )
    is_initially_clean = status_res.returncode == 0 and not status_res.stdout.strip()

    applied_temporarily = False
    if is_initially_clean:
        # Try applying patch cleanly
        apply_res = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file)],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=20,
        )
        applied_temporarily = apply_res.returncode == 0

    try:
        # Step 2: Audit touched files
        for rel_path in touched_files:
            target_file = repo_dir / rel_path
            if not target_file.exists() or not target_file.is_file():
                continue

            # Semgrep Security Scan
            try:
                findings = semgrep_scanner.scan_file(target_file) if semgrep_scanner else []
                semgrep_count += len(findings)
            except Exception:
                pass

            # Google Auditor for C/C++ files
            if target_file.suffix in [".cc", ".cpp", ".cxx", ".h", ".hpp"]:
                try:
                    style_res = google_auditor.google_style_check(str(target_file)) if google_auditor else {}
                    if not style_res.get("passed", False):
                        style_error_count += len(style_res.get("violations", []))
                except Exception:
                    pass
    finally:
        # Step 3: Always restore clean git state if we applied the patch temporarily
        if applied_temporarily:
            subprocess.run(["git", "checkout", "--", "."], cwd=str(repo_dir), capture_output=True, timeout=15)
            subprocess.run(["git", "clean", "-fd"], cwd=str(repo_dir), capture_output=True, timeout=15)

    if semgrep_count > 0:
        verdict = "SECURITY_ALERT"
    elif style_error_count > 0:
        verdict = "STYLE_WARNINGS"
    else:
        verdict = "QUALIFIED"

    return semgrep_count, style_error_count, verdict


def compute_surgical_score(info: JulesSessionInfo) -> float:
    """Computes Karpathy surgical score: rewards minimal changes, tests, and zero flaws."""
    score = 100.0
    if info.audit_verdict == "QUALIFIED":
        score += 50.0
    elif info.audit_verdict == "STYLE_WARNINGS":
        score += 20.0
    elif info.audit_verdict == "SECURITY_ALERT":
        score -= 200.0

    if info.has_test:
        score += 40.0

    # Karpathy surgical penalty: deduct for bloated diffs
    total_diff_lines = info.lines_added + info.lines_deleted
    score -= min(total_diff_lines * 0.2, 50.0)

    # Penalties for flaws
    score -= info.semgrep_findings * 50.0
    score -= info.google_style_errors * 5.0

    return round(score, 2)


class JulesWatchdog:
    """Supervises Jules cloud microVM sessions, auto-pulls patches, and conducts audits."""

    def __init__(self, poll_interval: float = 10.0) -> None:
        self.poll_interval = poll_interval
        self.state: Dict[str, JulesSessionInfo] = {}
        self.known_repos = get_known_repos()
        self.load_state()

    def load_state(self) -> None:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    for k, v in raw.items():
                        self.state[k] = JulesSessionInfo.from_dict(v)
            except Exception as e:
                print(f"[JulesWatchdog] Warning loading state: {e}", flush=True)

    def save_state(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                serializable = {k: v.to_dict() for k, v in self.state.items()}
                json.dump(serializable, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[JulesWatchdog] Warning saving state: {e}", flush=True)

    def sync_to_echelon_state(self) -> None:
        """Synchronizes tracked sessions and best candidate info with echelon_state.json."""
        if not ECHELON_STATE_FILE.exists():
            return

        try:
            with open(ECHELON_STATE_FILE, "r", encoding="utf-8") as f:
                ech_data = json.load(f)

            active_list = ech_data.get("active_sessions", [])
            for item in active_list:
                iss_num = item.get("issue_number")
                # Find corresponding completed sessions
                matching = [
                    s for s in self.state.values()
                    if s.issue_number == iss_num and s.status == "Completed" and s.pulled
                ]
                if matching:
                    matching.sort(key=lambda s: s.surgical_score, reverse=True)
                    best = matching[0]
                    item["watchdog_status"] = "PATCH_PULLED_AND_AUDITED"
                    item["best_session_id"] = best.session_id
                    item["best_score"] = best.surgical_score
                    item["best_patch"] = best.patch_path
                    item["audit_verdict"] = best.audit_verdict
                    item["completed_candidates"] = len(matching)

            with open(ECHELON_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(ech_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def check_once(self) -> List[JulesSessionInfo]:
        """Runs a single inspection pass over all remote Jules sessions."""
        try:
            res = subprocess.run(
                ["jules", "remote", "list", "--session"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                shell=True,
            )
            if res.returncode != 0:
                print(f"[JulesWatchdog] Error querying Jules sessions: {res.stderr.strip()}", flush=True)
                return []
            raw_text = res.stdout
        except Exception as e:
            print(f"[JulesWatchdog] Command failed: {e}", flush=True)
            return []

        remote_sessions = parse_session_list(raw_text, self.known_repos)
        pulled_this_pass: List[JulesSessionInfo] = []

        for rs in remote_sessions:
            sid = rs.session_id
            existing = self.state.get(sid)

            if existing:
                # Update status and activity
                existing.status = rs.status
                existing.last_active = rs.last_active
                if rs.description and not existing.description:
                    existing.description = rs.description
                if rs.issue_number and not existing.issue_number:
                    existing.issue_number = rs.issue_number
                target_info = existing
            else:
                self.state[sid] = rs
                target_info = rs

            # Auto-Pull on Completed or Awaiting User Feedback sessions
            if (target_info.status == "Completed" or "Awaiting" in target_info.status) and not target_info.pulled:
                print("\n" + "=" * 70, flush=True)
                print(f"🎯 [JulesWatchdog] Cloud Session Completed/Ready: #{target_info.session_id}", flush=True)
                print(f"   Repo: {target_info.repo} | Issue: #{target_info.issue_number}", flush=True)
                print(f"   Task: {target_info.description[:60]}...", flush=True)
                print("   Action: Initiating automatic pull (自動回拉) to local registry...", flush=True)

                diff_text = pull_session_diff(sid)
                if diff_text:
                    repo_folder = target_info.repo.replace("/", "_")
                    save_dir = PATCHES_DIR / repo_folder
                    save_dir.mkdir(parents=True, exist_ok=True)
                    patch_file = save_dir / f"jules_{sid}.patch"
                    patch_file.write_text(diff_text, encoding="utf-8")

                    added, deleted, has_test, touched = parse_diff_metrics(diff_text)
                    target_info.pulled = True
                    target_info.patch_path = str(patch_file)
                    target_info.pulled_at = time.time()
                    target_info.lines_added = added
                    target_info.lines_deleted = deleted
                    target_info.has_test = has_test
                    target_info.touched_files = touched

                    print(f"📥 [JulesWatchdog] Diff successfully pulled to: {patch_file.name}", flush=True)
                    print(f"   Diff Metrics: +{added} / -{deleted} lines | Regression Test: {'YES' if has_test else 'NO'}", flush=True)
                    print(f"   Touched Files: {', '.join(touched[:3])}{'...' if len(touched) > 3 else ''}", flush=True)

                    # Conduct Eight-Shield Fortress Static Audits
                    local_repo = find_local_repo_dir(target_info.repo)
                    if local_repo:
                        print(f"🛡️  [JulesWatchdog] Running Eight-Shield Fortress Audits on {local_repo.name}...", flush=True)
                        semgrep_flaws, style_errors, verdict = run_eight_shield_audit(local_repo, patch_file, touched)
                        target_info.semgrep_findings = semgrep_flaws
                        target_info.google_style_errors = style_errors
                        target_info.audit_verdict = verdict
                        print(f"   Audit Verdict: {verdict} (Semgrep Flaws: {semgrep_flaws}, Style Errors: {style_errors})", flush=True)
                    else:
                        target_info.audit_verdict = "NO_LOCAL_CLONE"
                        print("ℹ️  [JulesWatchdog] Local clone not found, skipping local static checks.", flush=True)

                    target_info.surgical_score = compute_surgical_score(target_info)
                    print(f"🏆 [JulesWatchdog] Karpathy Surgical Score: {target_info.surgical_score}", flush=True)
                    print("=" * 70 + "\n", flush=True)

                    pulled_this_pass.append(target_info)

        self.save_state()
        self.sync_to_echelon_state()
        return pulled_this_pass

    def auto_replenish(self, target_active_topics: int = 2) -> None:
        """
        Automatically replenishes active tasks when available slots exist:
        1. Checks how many active tasks are currently in echelon_state.json.
        2. If active tasks < target_active_topics:
           - Iterates over eligible candidate repos.
           - Checks repo concurrency quota (< 2 open PRs authored by @me).
           - Searches candidate issues passing the 5 Google Bug Hunters gates.
           - Selects top candidate and automatically dispatches 2 parallel Jules cloud microVMs.
           - Registers the new task in echelon_state.json.
        """
        if not ECHELON_STATE_FILE.exists():
            return

        try:
            with open(ECHELON_STATE_FILE, "r", encoding="utf-8") as f:
                ech_data = json.load(f)
        except Exception:
            return

        active_list = ech_data.get("active_sessions", [])
        if len(active_list) >= target_active_topics:
            return

        print(f"\n🔄 [JulesWatchdog Auto-Replenish] Active topics ({len(active_list)}/{target_active_topics}) has free capacity.", flush=True)
        print("   Searching for eligible Google Bug Hunter tasks to auto-replenish...", flush=True)

        try:
            import issue_selector
        except ImportError:
            return

        completed_prs = ech_data.get("completed_prs", [])
        completed_issues = {
            (item.get("repo"), item.get("issue_number")) for item in completed_prs
        }
        active_repos = {item.get("repo") for item in active_list}

        pool = [
            ("google/benchmark", "jdymitarai/benchmark"),
            ("google/nsjail", "jdymitarai/nsjail"),
            ("google/double-conversion", "jdymitarai/double-conversion"),
            ("google/glog", "jdymitarai/glog"),
            ("google/protobuf", "jdymitarai/protobuf"),
        ]

        for upstream_repo, fork_repo in pool:
            if upstream_repo in active_repos:
                continue

            open_prs = issue_selector.check_repo_open_prs(upstream_repo)
            if open_prs >= 2:
                continue

            candidates = issue_selector.search_candidate_issues(upstream_repo, limit=10)
            valid_cands = [
                c for c in candidates
                if (upstream_repo, c.get("number")) not in completed_issues
                and c.get("security_score", 0) > 0
            ]

            if not valid_cands:
                continue

            best_iss = valid_cands[0]
            iss_num = best_iss["number"]
            iss_title = best_iss["title"]

            print(f"🚀 [JulesWatchdog Auto-Replenish] Selected Candidate: #{iss_num} in {upstream_repo} : '{iss_title}'", flush=True)
            print("   Dispatching 2 parallel Jules cloud microVMs...", flush=True)

            prompt = (
                f"Fix Issue #{iss_num}: {iss_title} in {upstream_repo}.\n"
                f"Investigate root cause and apply minimal surgical fix.\n"
                f"Adhere strictly to Google C++ Style Guide and Karpathy defensive principles.\n"
                f"Add regression unit tests to prevent future regressions."
            )

            cmd = [
                "jules", "remote", "new",
                "--repo", fork_repo,
                "--session", prompt,
                "--parallel", "2"
            ]

            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=True)
            if res.returncode == 0:
                sids = re.findall(r"ID:\s*(\d{15,25})", res.stdout)
                if not sids:
                    sids = re.findall(r"(\d{15,25})", res.stdout)

                new_entry = {
                    "repo": upstream_repo,
                    "fork_repo": fork_repo,
                    "issue_number": iss_num,
                    "title": iss_title,
                    "dispatched_at": time.time(),
                    "session_ids": sids[:2],
                    "status": "RUNNING",
                    "watchdog_status": "DISPATCHED",
                    "best_session_id": None,
                    "best_score": None,
                    "best_patch": None,
                    "audit_verdict": "PENDING",
                    "completed_candidates": 0,
                }
                active_list.append(new_entry)
                ech_data["active_sessions"] = active_list
                with open(ECHELON_STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(ech_data, f, indent=2, ensure_ascii=False)

                print(f"✅ [JulesWatchdog Auto-Replenish] Dispatched microVMs: {sids[:2]}. Added to active queue.", flush=True)
                break

    def run_loop(self) -> None:
        """Runs the continuous monitoring loop."""
        print("=" * 75, flush=True)
        print("🐾 ZeroSpinner - Google Jules Autonomous Cloud Watchdog Active", flush=True)
        print(f"    Process ID (PID)    : {os.getpid()}", flush=True)
        print(f"    Patch Directory     : {PATCHES_DIR}", flush=True)
        print(f"    Polling Interval    : {self.poll_interval}s", flush=True)
        print("=" * 75 + "\n", flush=True)

        iteration = 0
        while True:
            iteration += 1
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")

            if STOP_FLAG_FILE.exists():
                print("\n[JulesWatchdog] Stop flag detected (.jules_watchdog.stop). Exiting cleanly...", flush=True)
                try:
                    STOP_FLAG_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                break

            pulled = self.check_once()

            # Auto-replenish if capacity allows (runs every 6 iterations ~ 1 minute)
            if iteration % 6 == 1:
                try:
                    self.auto_replenish()
                except Exception as e:
                    print(f"[JulesWatchdog] Replenish warning: {e}", flush=True)

            # Compute summary stats
            total = len(self.state)
            completed = sum(1 for s in self.state.values() if s.status == "Completed")
            in_prog = sum(1 for s in self.state.values() if s.status == "In Progress")
            planning = sum(1 for s in self.state.values() if s.status == "Planning")
            pulled_count = sum(1 for s in self.state.values() if s.pulled)

            print(
                f"[{now_str}] [JulesWatchdog Heartbeat #{iteration:04d}] "
                f"Active: {planning + in_prog} (Planning: {planning}, In Progress: {in_prog}) | "
                f"Completed: {completed} | "
                f"Pulled: {pulled_count}/{completed}",
                flush=True,
            )

            time.sleep(self.poll_interval)


def print_status() -> None:
    """Displays formatted status of all tracked sessions and pull audit states."""
    dog = JulesWatchdog()
    dog.check_once()

    print("=" * 80, flush=True)
    print("🐾 Google Jules Autonomous Cloud Watchdog Status", flush=True)
    print("=" * 80, flush=True)

    if not dog.state:
        print("No sessions tracked yet. Run with --once or start daemon to discover.", flush=True)
        return

    # Group by Issue Number
    grouped: Dict[Any, List[JulesSessionInfo]] = {}
    for s in dog.state.values():
        key = s.issue_number or s.repo
        grouped.setdefault(key, []).append(s)

    for key, sessions in grouped.items():
        repo = sessions[0].repo
        iss_str = f"Issue #{key}" if isinstance(key, int) else f"Target: {key}"
        print(f"\n📂 [{repo}] {iss_str} ({len(sessions)} parallel session(s)):")
        sessions.sort(key=lambda x: x.surgical_score, reverse=True)

        for idx, s in enumerate(sessions):
            pulled_tag = "📥 PULLED" if s.pulled else "⏳ NOT PULLED"
            score_tag = f"Score: {s.surgical_score}" if s.pulled else ""
            test_tag = "Has Tests: YES" if s.has_test else "No Tests"
            best_mark = " 🌟 [RECOMMENDED WINNER]" if idx == 0 and s.pulled and s.status == "Completed" else ""

            print(f"  [{idx + 1}] Session: {s.session_id} | Status: {s.status} | {pulled_tag}{best_mark}")
            if s.pulled:
                print(f"      Metrics: +{s.lines_added} / -{s.lines_deleted} lines | {test_tag} | {score_tag}")
                print(f"      Verdict: {s.audit_verdict} (Vulns: {s.semgrep_findings}, Style Errors: {s.google_style_errors})")
                if s.patch_path:
                    print(f"      Patch  : {s.patch_path}")

    print("\n" + "=" * 80 + "\n", flush=True)


def stop_daemon() -> None:
    """Signals running watchdog daemon to stop."""
    STOP_FLAG_FILE.write_text(str(time.time()), encoding="utf-8")
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            print(f"[JulesWatchdog] Terminated watchdog process (PID: {pid}).", flush=True)
        except Exception:
            pass
        finally:
            PID_FILE.unlink(missing_ok=True)
    print("[JulesWatchdog] Stop signal transmitted successfully.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="ZeroSpinner Google Jules Autonomous Cloud Watchdog")
    parser.add_argument("--once", action="store_true", help="Perform single inspection, pull completed, and exit")
    parser.add_argument("--status", action="store_true", help="Show all tracked sessions and pull audit status")
    parser.add_argument("--stop", action="store_true", help="Stop running watchdog daemon")
    parser.add_argument("--poll", type=float, default=10.0, help="Polling interval in seconds")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon process")

    args = parser.parse_args()

    if args.stop:
        stop_daemon()
        sys.exit(0)

    if args.status:
        print_status()
        sys.exit(0)

    if args.once:
        dog = JulesWatchdog()
        pulled = dog.check_once()
        print(f"[JulesWatchdog] Single inspection pass complete. Pulled {len(pulled)} new session(s).", flush=True)
        sys.exit(0)

    # Write PID
    pid = os.getpid()
    PID_FILE.write_text(str(pid), encoding="utf-8")

    def cleanup():
        if PID_FILE.exists():
            try:
                if PID_FILE.read_text(encoding="utf-8").strip() == str(pid):
                    PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass

    atexit.register(cleanup)

    dog = JulesWatchdog(poll_interval=args.poll)
    try:
        dog.run_loop()
    except KeyboardInterrupt:
        print("\n[JulesWatchdog] Interrupted by user. Exiting cleanly...", flush=True)
        cleanup()
        sys.exit(0)


if __name__ == "__main__":
    main()
