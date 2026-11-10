#!/usr/bin/env python3
"""Claude Code PreToolUse guard for this repo.

It stops a session before it does one of these:
- edits a path its branch's area does not own
- reads or works in the wrong clone for the sealed answer key
- reads a secrets file
- adds Claude attribution to a commit, pull request or issue
- skips the git hooks, or touches the git-crypt key

Pushes to main are not blocked. They ask the human first, because the team
rule is to talk before main changes.

The git pre-commit hook and CI repeat the ownership, sealed and secret checks,
so a write made through the shell is still caught before it lands.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import repo_rules as rr  # noqa: E402

READ_TOOLS = ("Read", "Grep", "Glob")

SECRET_FILE = re.compile(r"(^|/)(\.env(\.(?!example$)[^/]+)?|[^/]+\.(key|pem|p12))$")
SECRET_IN_COMMAND = re.compile(r"(?<![\w.-])\.env(?!\.example)(\.[\w-]+)?(?![\w-])|\.(pem|p12)\b")
FILE_READERS = re.compile(
    r"\b(cat|head|tail|less|more|bat|grep|rg|awk|sed|cut|sort|strings|xxd|od|base64|open|code|vim?|nano)\b"
)
KEY_HANDLING = re.compile(r"sealed\.key|git-crypt\s+(unlock|lock|export-key|init|add-gpg-user)")
ATTRIBUTION = re.compile(
    r"co-authored-by:[^\n]*(claude|anthropic)|generated with[^\n]{0,40}claude"
    r"|noreply@anthropic\.com|claude-session:",
    re.IGNORECASE,
)
SKIPS_HOOKS = re.compile(
    r"\bgit\b[^|;&]*\bcommit\b[^|;&]*--no-verify"
    r"|core\.hooksPath[\s=]+(?!\.githooks\b)\S|--unset(-all)?\s+core\.hooksPath"
)
GIT_PUSH = re.compile(r"\bgit\b[^|;&]*\bpush\b([^|;&]*)")
FORCE = re.compile(r"(^|\s)(-f|--force|--force-with-lease)\b")
# What a session on the wrong branch may still run in the sealed clone: enough to
# get onto the right branch, and nothing that prints file contents.
SEALED_SAFE_COMMAND = re.compile(
    r"^\s*(git\s+(status|switch|checkout|branch|fetch|pull|worktree|rev-parse|symbolic-ref)\b"
    r"|(\./)?scripts/(board|setup)\b)[^;&|`$<>]*$"
)


def decide(decision, reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def guard_bash(command, cwd):
    if ATTRIBUTION.search(command):
        decide(
            "deny",
            "The only authors of this repo are Batyrkhan and Noriaki. Remove the Claude "
            "attribution (Co-Authored-By, 'Generated with Claude Code', session link) and retry.",
        )
    if KEY_HANDLING.search(command):
        decide(
            "deny",
            "Sessions never handle the git-crypt key. A human runs scripts/sealed-clone "
            "once. See docs/SEALED.md.",
        )
    if SKIPS_HOOKS.search(command):
        decide("deny", "Don't skip or repoint the git hooks. Fix what the hook reports.")
    if SECRET_IN_COMMAND.search(command) and FILE_READERS.search(command):
        decide(
            "deny",
            "That command would read a secrets file. Variable names are in .env.example. "
            "Load values with 'uv run --env-file .env ...' and never print them.",
        )

    root = rr.repo_root(cwd)
    if root is None:
        return
    rules = rr.load_rules(root)
    if rr.clone_is_sealed(root):
        problem = rr.branch_problem(root, rules)
        if problem and not SEALED_SAFE_COMMAND.match(command):
            decide("deny", problem)

    push = GIT_PUSH.search(command)
    if push:
        args = push.group(1)
        to_main = rr.current_branch(root) == "main" or re.search(r"\bmain\b", args)
        if to_main and FORCE.search(args):
            decide("deny", "Never force-push main.")
        if to_main:
            decide(
                "ask",
                "This pushes to main. The team rule is to tell the other person first. "
                "Has that happened?",
            )


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    if tool == "Bash":
        guard_bash(tool_input.get("command") or "", cwd)
        return

    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    target = Path(raw) if raw else Path(cwd)
    if not target.is_absolute():
        target = Path(cwd) / target
    root = rr.repo_root(target)
    if root is None:
        return
    rules = rr.load_rules(root)
    rel = rr.relative(root, target) or ""
    sealed_clone = rr.clone_is_sealed(root)

    if sealed_clone:
        problem = rr.branch_problem(root, rules)
        if problem:
            decide("deny", problem)

    if SECRET_FILE.search(rel):
        decide(
            "deny",
            f"'{rel}' is a secrets file. Sessions don't read or write it. Variable names "
            "are in .env.example; a human fills in the values.",
        )

    if tool in READ_TOOLS:
        if rr.is_sealed_path(rel) and not sealed_clone:
            decide(
                "deny",
                f"'{rel}' is sealed and encrypted in this clone, so there is nothing to "
                "read. This is on purpose. See docs/SEALED.md.",
            )
        return

    problem = rr.edit_problem(root, rel, rules)
    if problem:
        decide("deny", problem)


if __name__ == "__main__":
    main()
