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
| Repo setup, shared data contract | Done |
| Audit agent | Not started |
| Fake company data and answer key | Not started |
| Ramp connection | Not started. The demo URL from our research, `https://mcp.ramp.com/mcp-apps/demo/mcp`, returned 404 on 2026-11-09. `https://mcp.ramp.com/mcp` answers but needs OAuth. |
| QuickBooks sandbox connection | Not started |
| Eval | Not started |
| Screen | Not started |

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
