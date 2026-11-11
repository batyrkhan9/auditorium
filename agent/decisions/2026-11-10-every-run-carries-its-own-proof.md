# Every run fingerprints its sources and saves a copy, before any check runs

**Decided.** Both modes now start the same way (`runs.py`):

1. `run_started` goes into the log.
2. A sha256 of each kind of record the sources returned is logged.
3. A copy of what was fetched is saved into the run folder as `sources/`.
4. Only then do the checks run.

Each kind of record is fetched from the sources once per run and reused for all
four steps, so the fingerprints, the checks and the snapshot cannot disagree, and
a live source is not called several times over.

At the end, `run.json` records where the dataset was, which dataset it was, and
the commit of the agent's own code, taken from where the code lives rather than
from wherever the command was started.

**Why before the checks.** The log is hash-chained, so the fingerprints cannot
be added or changed afterwards. That is what lets anyone verify a run later from
its folder alone and know the snapshot is the data the checks actually saw. A
snapshot written after the run, by something other than the agent, cannot give
that.

**What it proves and what it does not.** "Verified against snapshot" means the
evidence matches what the run fetched, unchanged since. It does not mean the
fetch was honest; only checking against the live sources shows that. The
contract makes every tool say which one it did.

**Live sources.** `--live` reads Ramp and QuickBooks through the connectors
where credentials are set, and the dataset folder supplies the rest. The run
prints which systems were live, and `run.json` records it per system.

**Two things the checks now do differently** (contract v0.2):

- Approvals the source could not report (`None`) are `needs_review`, never
  "approval missing". An empty list still means it was never sent for approval.
- A pending approval on a paid transaction is still a failure, and the summary
  says the request is pending instead of saying there is none.

**Committed runs.** `agent/runs/` holds one pre-flight run and one sample run on
the scored dataset, for the screen. Their `sources/` snapshots are left out of
git because the same data is in `data/company`.

**Checked.** Launched from outside the repo with `--live` on the scored dataset:
800 results, fingerprints logged straight after `run_started`, a snapshot that
matches them, `dataset_path` and `git_commit` filled, and validation clean
against the snapshot.

**Not checked.** A run with QuickBooks or Ramp actually live: this machine has
no credentials for either, so in that test every source fell back to the
dataset and said so.

**What would change our mind.** If snapshots of live runs grow large, save only
the documents that evidence cites instead of every file.
