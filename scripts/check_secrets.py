#!/usr/bin/env python3
"""
Project Aegis — Pre-Commit Secret Scanner

Scans repository files or git staged changes for sensitive credentials,
API keys, private keys, and authentication tokens before committing.

SAFETY INVARIANT:
Never prints matched secrets or confidential line contents to standard output
or log files to prevent secondary leakage during CI/CD or terminal inspections.
"""

import argparse
import os
import re
import subprocess
import sys
from typing import List, Tuple

# Pre-compiled high-risk secret patterns
# Note: Patterns are constructed safely to avoid false positives on standard code
SECRET_PATTERNS = [
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Google OAuth Token", re.compile(r"ya29\.[0-9A-Za-z\-_]{20,}")),
    ("GitHub Personal Access Token", re.compile(r"gh[pousr]_[0-9a-zA-Z]{36}")),
    ("Slack API Token", re.compile(r"xox[baprs]-[0-9a-zA-Z]{10,48}")),
    ("AWS Access Key ID", re.compile(r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}")),
    ("Generic Private Key Header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# Paths to ignore from secret scanning (build artifacts, virtualenvs, git metadata)
IGNORE_DIRS = {
    ".git",
    ".gradle",
    ".idea",
    ".dart_tool",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    ".venv",
    "venv",
    "node_modules",
}

# Specific files that legitimately contain scrubber definitions (e.g., regexes)
WHITELIST_SCRUBBER_FILES = {
    os.path.normpath("backend/app/services/audit_trail_service.py"),
    os.path.normpath("scripts/check_secrets.py"),
}


def get_staged_files() -> List[str]:
    """Returns a list of files staged in git index."""
    try:
        output = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            text=True,
            errors="ignore",
        )
        return [f.strip() for f in output.splitlines() if f.strip()]
    except Exception:
        return []


def get_all_tracked_files() -> List[str]:
    """Returns a list of all tracked files in git repository."""
    try:
        output = subprocess.check_output(
            ["git", "ls-files"],
            text=True,
            errors="ignore",
        )
        return [f.strip() for f in output.splitlines() if f.strip()]
    except Exception:
        return []


def should_ignore(file_path: str) -> bool:
    """Checks if a file should be ignored based on path segments or extension."""
    norm = os.path.normpath(file_path)
    parts = norm.split(os.sep)
    if any(p in IGNORE_DIRS for p in parts):
        return True
    if norm in WHITELIST_SCRUBBER_FILES:
        return True
    # Ignore binary files by extension
    binary_exts = {".png", ".jpg", ".jpeg", ".ico", ".db", ".sqlite", ".bin", ".tar", ".gz", ".zip"}
    _, ext = os.path.splitext(norm)
    if ext.lower() in binary_exts:
        return True
    return False


def scan_file(file_path: str) -> List[Tuple[str, int]]:
    """
    Scans a single file for secret patterns.
    Returns a list of (pattern_name, line_number) detections.
    NEVER returns or stores the matched secret string.
    """
    findings = []
    if not os.path.isfile(file_path):
        return findings

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as fp:
            for line_no, line in enumerate(fp, start=1):
                for pattern_name, pattern in SECRET_PATTERNS:
                    if pattern.search(line):
                        findings.append((pattern_name, line_no))
    except Exception:
        pass

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Project Aegis Secret Scanner")
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Scan only git staged files instead of all repository files",
    )
    args = parser.parse_args()

    if args.staged:
        files = get_staged_files()
        mode_desc = "staged git files"
    else:
        files = get_all_tracked_files()
        mode_desc = "tracked repository files"

    scanned_count = 0
    total_violations = 0

    for f in files:
        if should_ignore(f):
            continue
        scanned_count += 1
        violations = scan_file(f)
        for pattern_name, line_no in violations:
            total_violations += 1
            # Redacted notification: never outputs the matched text
            print(
                f"[SECURITY SCAN ERROR] Potential secret pattern '{pattern_name}' detected "
                f"in {f}:{line_no} (content redacted for security)."
            )

    if total_violations > 0:
        print(
            f"\n[SCAN FAILED] {total_violations} potential secret(s) detected across {scanned_count} {mode_desc}."
        )
        print("Please remove raw credentials before committing. Use dynamic fixtures or environment variables.")
        return 1

    print(f"[SECURITY SCAN OK] Scanned {scanned_count} {mode_desc}. Zero secrets detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
