"""Repo rules shared by the Claude Code hooks, the git hooks and CI.

Stdlib only and Python 3.9 compatible, because this runs before any project
environment exists. `ownership.json` at the repo root is the data; this file is
the logic.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

GITCRYPT_MAGIC = b"\x00GITCRYPT\x00"
SEALED_DIR = "sealed/"
# An encrypted file that exists only so a clone can tell whether it holds the key.
SENTINEL = "sealed/SENTINEL"
# Files under sealed/ that are committed in plain text on purpose.
SEALED_PLAINTEXT = ("sealed/README.md",)


def git(*args, cwd=None):
    """Run git and return stdout, or '' if it fails."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def repo_root(start):
    """Top of the Auditorium checkout that contains `start`, or None if it is elsewhere."""
    path = Path(start)
    while not path.exists() and path != path.parent:
        path = path.parent
    if path.is_file():
        path = path.parent
    top = git("rev-parse", "--show-toplevel", cwd=str(path))
    if top and (Path(top) / "ownership.json").is_file():
        return Path(top)
    return None


def relative(root, path):
    """`path` as a POSIX path relative to `root`, or None if it is outside the checkout."""
    real_root = os.path.realpath(str(root))
    real_path = os.path.realpath(str(path))
    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        return None
    return os.path.relpath(real_path, real_root).replace(os.sep, "/")


def load_rules(root):
    return json.loads((Path(root) / "ownership.json").read_text())


def current_branch(root):
    return git("symbolic-ref", "--short", "-q", "HEAD", cwd=str(root))


def branch_area(branch, rules):
    """The area a branch belongs to: the part before the first slash, if it names an area."""
    head = branch.split("/", 1)[0] if "/" in branch else ""
    return head if head in rules["areas"] else None


def path_area(rel, rules):
    """The area that owns a path. Anything no area claims is shared."""
    best, best_len = "shared", -1
    for name, area in rules["areas"].items():
        for prefix in area["paths"]:
            if rel.startswith(prefix) and len(prefix) > best_len:
                best, best_len = name, len(prefix)
    return best


def person(root):
    return git("config", "--get", "auditorium.person", cwd=str(root)) or None


def clone_is_sealed(root):
    """True when this clone holds the git-crypt key, so sealed/ is readable here."""
    try:
        with open(Path(root) / SENTINEL, "rb") as handle:
            head = handle.read(len(GITCRYPT_MAGIC))
    except OSError:
        return False
    return head != GITCRYPT_MAGIC


def is_sealed_path(rel):
    return rel.startswith(SEALED_DIR) and rel not in SEALED_PLAINTEXT


def area_names(rules):
    return ", ".join(sorted(rules["areas"]))


def branch_problem(root, rules):
    """Why the current branch cannot be worked on in this clone by this person, or None."""
    branch = current_branch(root)
    area = branch_area(branch, rules)
    if area is None:
        shown = branch or "(detached HEAD)"
        return (
            f"Branch '{shown}' does not name an area. Work happens on '<area>/<topic>' "
            f"branches (areas: {area_names(rules)}). Run: git switch -c <area>/<topic>  "
            f"or rename this one: git branch -m <area>/<topic>"
        )
    who = person(root)
    if who is None:
        return "This clone has no owner set. Run: scripts/setup <batyrkhan|noriaki>"
    owner = rules["areas"][area]["owner"]
    if owner not in ("both", who):
        return (
            f"Area '{area}' belongs to {owner}, and this clone belongs to {who}. "
            f'Ask instead: scripts/board request {area} "what you need"'
        )
    sealed = clone_is_sealed(root)
    wants = rules["areas"][area]["clone"]
    if sealed and wants != "sealed":
        return (
            f"This is the sealed clone: the answer key is readable here. Area '{area}' "
            f"must be built in the build clone, or the score means nothing. Only "
            f"data/ and evals/ branches work here."
        )
    if not sealed and wants == "sealed":
        return (
            f"Area '{area}' works in the sealed clone, which this is not. Your human "
            f"creates it once with scripts/sealed-clone; open that folder instead."
        )
    return None


def edit_problem(root, rel, rules=None):
    """Why the current branch may not change `rel`, or None if it may."""
    rules = rules or load_rules(root)
    if rel in rules.get("anyone", []):
        return None
    problem = branch_problem(root, rules)
    if problem:
        return problem
    branch = current_branch(root)
    area = branch_area(branch, rules)
    target = path_area(rel, rules)
    if target != area:
        owner = rules["areas"][target]["owner"]
        return (
            f"'{rel}' belongs to area '{target}' (owner: {owner}). You are on '{branch}', "
            f"which may only change area '{area}'. Don't edit it. Ask instead: "
            f'scripts/board request {target} "what you need"'
        )
    return None
