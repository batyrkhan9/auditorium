#!/usr/bin/env python3
"""Claude Code SessionStart hook: tell the session who and where it is, and show the board."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import repo_rules as rr  # noqa: E402


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    root = rr.repo_root(data.get("cwd") or os.getcwd())
    if root is None:
        return
    rules = rr.load_rules(root)
    branch = rr.current_branch(root)
    area = rr.branch_area(branch, rules)
    sealed = rr.clone_is_sealed(root)

    clone = "SEALED (the answer key is readable here)" if sealed else "build (sealed/ is encrypted)"
    lines = [
        "AUDITORIUM SESSION",
        f"Person: {rr.person(root) or 'not set'} | Branch: {branch or 'detached'} | "
        f"Area: {area or 'none yet'} | Clone: {clone}",
    ]
    if area:
        paths = ", ".join(rules["areas"][area]["paths"]) or "anything no other area owns"
        lines.append(f"You may edit: {paths}. Other areas are off limits; ask with scripts/board request.")
    problem = rr.branch_problem(root, rules)
    if problem:
        lines.append(f"BEFORE YOU EDIT: {problem}")
    if rr.git("config", "--get", "core.hooksPath", cwd=str(root)) != ".githooks":
        lines.append("SETUP MISSING: git hooks are not installed in this clone. Run scripts/setup.")

    try:
        board = subprocess.run(
            [sys.executable, str(root / "scripts" / "board"), "status", "--area", area or "all"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        board = ""
    lines.append("")
    lines.append(board or "Board unavailable (is gh logged in?). Run scripts/board to retry.")
    print("\n".join(lines)[:9000])


if __name__ == "__main__":
    main()
