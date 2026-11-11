# Auditorium

An AI agent that gets a startup's spending ready for its audit. Before the
auditor asks for anything, it checks every card expense and vendor bill: is
there a receipt, was it approved, does it match the books, is it in the right
period. When the auditor picks transactions to test, it pulls the documents,
lines them up and drafts follow-ups for anything missing. Every check links to
the real documents, and a log records every step.

Built by Batyrkhan and Noriaki for the BBS x Ramp hackathon build round.
Product brief: [docs/PRD.md](docs/PRD.md).

## Status

Honest and current. Update it as things land.

| Piece | State |
|---|---|
| Repo setup, shared data contract | Done. Contract v0.2.0 on main. |
| Audit agent | On main: pre-flight with all four checks, sample-request mode (evidence packets and follow-up drafts), and PDFs. Every run logs a fingerprint of what it fetched and saves a copy, so it can be verified later from its own folder. An optional Claude reader (`--claude`, off by default, at most 50 requests a run) handles what plain code cannot settle. Not yet run with QuickBooks or Ramp live from the agent's own command line. |
| Fake company data and answer key | Dataset v1 on main (`data/company`, copperline-fy2025-v1, contract v0.2). Answer key sealed. |
| Ramp connection | Live against Ramp's sandbox through the Developer API (client credentials). The dataset's 68 paid bills are loaded into the sandbox and read back live. The 2 unpaid bills and the 130 card transactions come from the dataset, because Ramp's API can't create card transactions; each run says so. MCP path (`https://demo-mcp.ramp.com/mcp`) and an offline stand-in server also work. |
| QuickBooks sandbox connection | Live. The dataset's 255 ledger entries are loaded into the sandbox and read back with 0 missing. |
| Eval | Harness on main; it scores a run against the sealed answer key and writes aggregate scorecards to `evals/scorecards/`. The latest scorecard is for an older agent than the one on main; see [docs/DEMO.md](docs/DEMO.md). Sample-request mode is not scored yet. |
| Screen | Done for run folders: results, evidence with what it was verified against, step log, pinned reviewer sign-off. Two real agent runs on the dataset are in `agent/runs/` and load in it. |

## Run it

No credentials needed. Everything reads the made-up company in `data/company`.

```bash
uv run python -m auditorium_agent preflight data/company --out runs/demo   # four checks on 200 transactions
uv run python -m auditorium_contracts validate-run runs/demo                # re-check every piece of evidence
uv run python -m auditorium_ui runs/demo                                    # look at it
```

The full walkthrough, the score and the questions we expect: [docs/DEMO.md](docs/DEMO.md).

## Start here

Needs: git, Python 3, [uv](https://docs.astral.sh/uv/), [gh](https://cli.github.com/)
(logged in), git-crypt (`brew install uv gh git-crypt`).

```bash
git clone https://github.com/batyrkhan9/auditorium.git
cd auditorium
scripts/setup noriaki        # or: scripts/setup batyrkhan
scripts/board                # what is open, in progress, done
git switch -c connectors/ramp-demo   # branch name = <area>/<topic>
```

For `data` and `evals` work you also need the sealed clone. Get the key file
from Batyrkhan privately, then:

```bash
scripts/sealed-clone path/to/sealed.key   # creates ../auditorium-sealed
```

## How the repo is organised

| Folder | Owner | What |
|---|---|---|
| `agent/` | Batyrkhan | the audit agent |
| `data/`, `sealed/datagen/`, `sealed/answer_key/` | Batyrkhan | fake company, planted mistakes, answer key |
| `connectors/` | Noriaki | Ramp and QuickBooks |
| `evals/`, `sealed/eval/` | Noriaki | the test that scores the agent |
| `ui/` | Noriaki | the screen |
| `contracts/`, `docs/`, `scripts/`, root | both | shared data format and repo rules |

- Rules every Claude Code session follows: [CLAUDE.md](CLAUDE.md)
- The shared data format: [docs/CONTRACT.md](docs/CONTRACT.md)
- Why the agent never sees the answer key: [docs/SEALED.md](docs/SEALED.md)
- Decisions and reasons: `scripts/decisions`
