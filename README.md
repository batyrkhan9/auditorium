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
| Audit agent | On main: pre-flight with all four checks, sample-request mode (evidence packets and follow-up drafts), and PDFs. Every run logs a fingerprint of what it fetched and saves a copy, so it can be verified later from its own folder. An optional Claude reader (`--claude`, off by default, at most 50 requests a run) handles what plain code cannot settle. Also runs with Ramp and QuickBooks live from its own command line (`--live`); the results match a run on the dataset folder. |
| Fake company data and answer key | Dataset v1 on main (`data/company`, copperline-fy2025-v1, contract v0.2). Answer key sealed. |
| Ramp connection | Live against Ramp's sandbox through the Developer API (client credentials). The dataset's 68 paid bills are loaded into the sandbox and read back live. The 2 unpaid bills and the 130 card transactions come from the dataset, because Ramp's API can't create card transactions; each run says so. MCP path (`https://demo-mcp.ramp.com/mcp`) and an offline stand-in server also work. |
| QuickBooks sandbox connection | Live. The dataset's 255 ledger entries are loaded into the sandbox and read back with 0 missing. |
| Eval | Harness on main; it scores a run against the sealed answer key and writes aggregate scorecards to `evals/scorecards/`. Latest scorecards are for main at 727a793, without and with the Claude reader, and with Ramp and QuickBooks live; see [docs/DEMO.md](docs/DEMO.md). Sample-request mode is not scored yet. |
| Screen | Done for run folders: results, evidence with what it was verified against, step log, pinned reviewer sign-off. Two real agent runs on the dataset are in `agent/runs/` and load in it. |

## What we pitched and what we built

Where the build differs from the pitch, this is the difference and why.

| We said | We built | Why |
|---|---|---|
| Claude Sonnet 5.5 and Opus 5.5 through the Claude Agent SDK | The four checks are plain code. Claude Opus 5.5 is called through the Claude API only for results code cannot settle: 11 requests on the dataset. Sonnet 5.5 can be selected. | Comparing fields is exact, free and repeatable. Each model question is one request with one structured answer, so there is no loop for an agent to run. [Decision](agent/decisions/2026-11-09-claude-reads-only-what-code-cannot-settle.md) |
| Ramp's public demo MCP server | A Ramp sandbox read through the Developer API. The MCP path (`demo-mcp.ramp.com`) and an offline stand-in server also work. The dataset's 68 paid bills are live; their requester, approvals and paid date come from the dataset through a stamp on each bill. The 2 unpaid bills and the card transactions come from the dataset. | The Developer API runs with no browser login, and it returns approvals; Ramp's MCP server gives no route from a transaction to its approvals. Ramp's API can create bills but not card transactions, and it cannot hold our approval history. [Decision](connectors/decisions/2026-11-10-ramp-developer-api.md), [loader](connectors/decisions/2026-11-10-ramp-bill-loader.md) |
| A QuickBooks sandbox through Intuit's MCP server | A QuickBooks sandbox, live, through Intuit's REST API. | The MCP server wraps the same API and would add Node and a subprocess. [Decision](connectors/decisions/2026-11-09-quickbooks-access.md) |
| PDF reading for the PBC list, bank statements and invoices | Invoices, receipts and bank statements are read as PDFs. There is no PBC list file: the auditor's selection arrives as a structured request. | The dataset models the sample request, which is what the agent acts on. |
| A dataset matched to the Ramp demo transactions | Our own made-up company, Copperline, Inc., loaded into both sandboxes. | We could not build on demo data we do not control, and planted mistakes need a known clean baseline. |
| A test set based on AU-C 500, run in Braintrust | A scoring harness whose metrics follow AU-C 500. Braintrust logging is built in and optional; the committed scorecards were scored locally. | [Decision](evals/decisions/2026-11-10-metrics-follow-au-c-500.md) |
| Slack and email drafts | Drafts written into the run folder, with a channel and a recipient. Nothing is sent and there is no Slack or email connection. | Follow-ups are a person's to send. |
| A log of every step with reviewer sign-off | A hash-chained log. Sign-off happens in the screen and is pinned to what the reviewer was shown. | |
| The pitch example is flagged | It is: Ridgeline Strategy Group LLC, $12,000, work done in December, recorded in January, no accrual. | |

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
