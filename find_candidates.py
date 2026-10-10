#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import issue_selector

print("=================================================================", flush=True)
print("Scanning approved Google Bug Hunter repositories for valid tasks:", flush=True)
print("=================================================================", flush=True)

for item in issue_selector.APPROVED_REPOS:
    repo = item["upstream"]
    open_prs = issue_selector.check_repo_open_prs(repo)
    if open_prs >= 2:
        print(f"[-] {repo}: 2/2 OPEN PRs (Locked)", flush=True)
        continue

    print(f"[*] Scanning {repo} (open PRs: {open_prs}/2)...", flush=True)
    candidates = issue_selector.search_candidate_issues(repo, limit=8)
    print(f"[+] {repo}: {len(candidates)} collision-free security candidates", flush=True)
    for c in candidates[:3]:
        print(f"     #{c['number']}: {c['title']} (Score: {c['security_score']})", flush=True)

print("=================================================================", flush=True)
