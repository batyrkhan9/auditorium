# Repo setup for two people running parallel Claude Code sessions

Decided at setup, 2026-11-09. Each line: the call, then why.

1. **Area = branch prefix, enforced by hooks.** A branch named `agent/x` may only
   change `agent/`. A Claude Code hook blocks other edits, the git pre-commit
   hook repeats it, CI repeats it again. Instructions alone get ignored under
   load; a hook that says what to do instead does not.
2. **Answer key encrypted with git-crypt, two clones.** Agent sessions work in a
   clone with no key, so the plaintext is not on their disk or in their git
   history. Considered: hooks that block reads (a stray grep leaks it, and the
   repo is public) and a second private repo (two repos to keep in step, and
   the submission is one repo).
3. **The contract is code, not a document.** Pydantic models plus validators for
   a dataset folder and a run folder. Areas integrate by passing the validator,
   not by reading each other's code.
4. **`Sources` is the seam between connectors and agent**, with a local-folder
   implementation in the contract. The agent can be built before any live
   connection exists, and the honest fallback is the same code path.
5. **Evidence is mechanically verifiable.** Document evidence is a page and a
   quote; record evidence is a field and a value. One shared `verify_evidence`
   is the definition of "did not make it up" for both the agent and the eval.
6. **Source records hold only what Ramp and QuickBooks really provide.** No
   service-period field, no ledger-to-transaction link. Otherwise the checks
   would be reading the answer.
7. **The log is hash-chained JSONL** written through one `LogWriter`, because
   both the agent and the screen append to it and an auditor should be able to
   tell it was not edited.
8. **The board is GitHub issues** through `scripts/board`. Live for both
   machines with no merge conflicts, which a status file in the repo cannot be.
9. **Decisions are one file each**, per area, so parallel sessions never
   conflict on a shared log.
10. **Python 3.12 with one uv workspace.** One environment and one lockfile;
    each area owns its own `pyproject.toml`. The Agent SDK, Braintrust and PDF
    tooling are all first-class in Python.
11. **Attribution off in three places:** Claude Code settings, a commit-msg
    hook, and a CI check on every commit's author.
12. **No GitHub branch protection.** We work on our own branches and talk
    before main changes. Sessions cannot commit on main, and a push to main
    asks the human first.

What would change our mind: if the hooks slow real work down, loosen the
ownership check to a warning before removing it.
