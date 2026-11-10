#!/usr/bin/env python3
"""Repo checks, run by the git hooks and by CI.

    scripts/checks.py precommit        staged changes (the pre-commit hook)
    scripts/checks.py commit-msg FILE  the commit message and author (the commit-msg hook)
    scripts/checks.py sealed           everything under sealed/ is encrypted in git
    scripts/checks.py authors          every commit is by Batyrkhan or Noriaki, no attribution
    scripts/checks.py ownership        this branch only changed its own area
    scripts/checks.py secrets          no secrets in tracked files

Stdlib only and Python 3.9 compatible.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repo_rules as rr  # noqa: E402

ROOT = rr.repo_root(os.getcwd())

ATTRIBUTION = re.compile(
    r"co-authored-by:[^\n]*(claude|anthropic)|generated with[^\n]{0,40}claude"
    r"|noreply@anthropic\.com|claude-session:",
    re.IGNORECASE,
)
CO_AUTHOR = re.compile(r"^co-authored-by:.*<([^>]+)>", re.IGNORECASE | re.MULTILINE)
GITHUB_COMMITTER = "noreply@github.com"

SECRET_FILENAME = re.compile(r"(^|/)(\.env(\.(?!example$)[^/]+)?|[^/]+\.(key|pem|p12))$")
SECRET_PATTERNS = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{32,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "hard-coded secret",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|client[_-]?secret|refresh[_-]?token)"
            r"\b[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+_\-]{24,}"
        ),
    ),
]
# Put this on a line that looks like a secret and is not one.
ALLOW_MARKER = "not-a-secret"
SCAN_SKIP = re.compile(r"(^sealed/|^uv\.lock$|\.pdf$|\.png$|\.jpg$|^contracts/schema/)")


def git_bytes(*args):
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True).stdout


def git(*args):
    return rr.git(*args, cwd=str(ROOT))


def scan_text(label, text):
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                problems.append(f"{label}:{number}: looks like a {name}")
                break
    return problems


def sealed_problems(paths, blob_prefix):
    """Paths under sealed/ whose git blob is not git-crypt ciphertext."""
    problems = []
    for path in paths:
        if not rr.is_sealed_path(path):
            continue
        blob = git_bytes("cat-file", "blob", f"{blob_prefix}{path}")
        if not blob.startswith(rr.GITCRYPT_MAGIC):
            problems.append(
                f"{path}: would be committed in plain text. Everything under sealed/ must be "
                "encrypted. Commit sealed files from the sealed clone only (docs/SEALED.md)."
            )
    return problems


def check_precommit():
    rules = rr.load_rules(ROOT)
    branch = rr.current_branch(ROOT)
    merging = bool(git("rev-parse", "-q", "--verify", "MERGE_HEAD"))
    allow_main = os.environ.get("AUDITORIUM_ALLOW_MAIN") == "1"
    changed = [p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMRD").splitlines() if p]
    present = [p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if p]
    problems = []

    if branch == "main" and not merging and not allow_main:
        problems.append(
            "You are committing on main. Work on an '<area>/<topic>' branch. (A human who "
            "means it can set AUDITORIUM_ALLOW_MAIN=1.)"
        )
    if branch != "main" and not merging:
        seen = set()
        for path in changed:
            problem = rr.edit_problem(ROOT, path, rules)
            if problem and problem not in seen:
                seen.add(problem)
                problems.append(problem)

    problems += sealed_problems(present, ":")

    for path in present:
        if SECRET_FILENAME.search(path):
            problems.append(f"{path}: secrets files are never committed.")
    diff = git("diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR")
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and current:
            if not SCAN_SKIP.search(current):
                problems += scan_text(current, line[1:])
    return problems


def check_commit_msg(message_file):
    rules = rr.load_rules(ROOT)
    message = Path(message_file).read_text(errors="replace")
    message = "\n".join(line for line in message.splitlines() if not line.startswith("#"))
    problems = []
    if ATTRIBUTION.search(message):
        problems.append(
            "The commit message carries Claude attribution. The only authors are Batyrkhan "
            "and Noriaki. Remove the Co-Authored-By / 'Generated with' / session lines."
        )
    ident = git("var", "GIT_AUTHOR_IDENT")
    match = re.search(r"<([^>]*)>", ident)
    email = match.group(1).lower() if match else ""
    if email not in rr.allowed_emails(rules):
        problems.append(
            f"Author email '{email}' is not one of ours. Fix git config user.email, or add "
            "your email to ownership.json on a shared/ branch."
        )
    return problems


def check_sealed():
    tracked = [p for p in git("ls-files", "sealed").splitlines() if p]
    return sealed_problems(tracked, "HEAD:")


def check_authors():
    rules = rr.load_rules(ROOT)
    allowed = rr.allowed_emails(rules)
    log = git("log", "--format=%H%x1f%ae%x1f%ce%x1f%B%x1e")
    problems = []
    for entry in log.split("\x1e"):
        if not entry.strip():
            continue
        sha, author, committer, body = entry.strip().split("\x1f", 3)
        short = sha[:9]
        if author.lower() not in allowed:
            problems.append(f"{short}: author {author} is not Batyrkhan or Noriaki")
        if committer.lower() not in allowed and committer.lower() != GITHUB_COMMITTER:
            problems.append(f"{short}: committer {committer} is not Batyrkhan or Noriaki")
        if ATTRIBUTION.search(body):
            problems.append(f"{short}: commit message carries Claude attribution")
        for email in CO_AUTHOR.findall(body):
            if email.lower() not in allowed:
                problems.append(f"{short}: co-author {email} is not Batyrkhan or Noriaki")
    return problems


def check_ownership():
    rules = rr.load_rules(ROOT)
    branch = os.environ.get("GITHUB_REF_NAME") or rr.current_branch(ROOT)
    if branch == "main":
        return []
    area = rr.branch_area(branch, rules)
    if area is None:
        return [f"Branch '{branch}' is not named '<area>/<topic>' (areas: {rr.area_names(rules)})."]
    base = git("merge-base", "origin/main", "HEAD")
    if not base:
        return []
    problems = []
    for path in git("diff", "--name-only", f"{base}..HEAD").splitlines():
        if not path or path in rules.get("anyone", []):
            continue
        target = rr.path_area(path, rules)
        if target != area:
            problems.append(f"{path}: belongs to area '{target}', changed on a '{area}' branch")
    return problems


def check_secrets():
    problems = []
    for path in git("ls-files").splitlines():
        if not path:
            continue
        if SECRET_FILENAME.search(path):
            problems.append(f"{path}: secrets files are never committed.")
        if SCAN_SKIP.search(path):
            continue
        try:
            text = (ROOT / path).read_text()
        except (OSError, UnicodeDecodeError):
            continue
        problems += scan_text(path, text)
    return problems


CHECKS = {
    "precommit": check_precommit,
    "sealed": check_sealed,
    "authors": check_authors,
    "ownership": check_ownership,
    "secrets": check_secrets,
}


def main(argv):
    if ROOT is None:
        print("checks.py: not inside the Auditorium repo", file=sys.stderr)
        return 2
    if len(argv) >= 2 and argv[0] == "commit-msg":
        problems = check_commit_msg(argv[1])
    elif len(argv) == 1 and argv[0] in CHECKS:
        problems = CHECKS[argv[0]]()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for problem in problems:
        print(f"BLOCKED  {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
