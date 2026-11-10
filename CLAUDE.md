# Auditorium

Two people, Batyrkhan and Noriaki, each run several Claude Code sessions in this
repo at the same time. The rules below keep those sessions from colliding. Hooks
enforce most of them. When a hook stops you, its message says what to do
instead. Do that; don't look for a way around it.

@docs/PRD.md

## 1. Your branch decides your area

Branches are named `<area>/<topic>`. The prefix decides what you may edit.

| Area | Owner | May edit | Works in |
|---|---|---|---|
| `agent` | Batyrkhan | `agent/` | build clone |
| `data` | Batyrkhan | `data/`, `sealed/datagen/`, `sealed/answer_key/` | sealed clone |
| `connectors` | Noriaki | `connectors/` | build clone |
| `evals` | Noriaki | `evals/`, `sealed/eval/` | sealed clone |
| `ui` | Noriaki | `ui/` | build clone |
| `shared` | both | everything else: `contracts/`, `docs/`, `scripts/`, root files | build clone |

- Start work with `git switch -c <area>/<topic>` from an up-to-date `main`. If
  the app gave you a branch with some other name, rename it:
  `git branch -m <area>/<topic>`.
- One area per branch. If you need a change in another area, don't make it. Ask
  for it (section 3).
- Don't commit on `main`. Merging to `main` happens after the two humans have
  talked, so do it only when your human tells you to.
- `ownership.json` is the source of truth for this table. `uv.lock` is generated
  and any area may update it.

## 2. Look at the board before you start

The board is this repo's GitHub issues. A summary prints when a session starts.

```
scripts/board                      # requests, in progress, open, recently done
scripts/board claim 12             # before you start a task
scripts/board add agent "Title"    # work that has no task yet
scripts/board done 12 --note "what changed"
```

If someone has claimed a task, it is theirs. Pick another or ask your human.

## 3. Asking the other side for something

```
scripts/board request connectors "What you need" --body "Why, and what done looks like"
```

Then keep going on something else, or build against the contract with a stub.
Requests addressed to your area show at session start. Answer them before you
pick up new work.

## 4. One shared data format

Anything that crosses an area boundary uses the models in `contracts/`. Read
[docs/CONTRACT.md](docs/CONTRACT.md) before you produce or consume shared data.
Never agree a private format between two areas. To change the contract, use a
`shared/` branch, label the task `contract-change`, and both humans agree before
it merges.

## 5. The answer key is sealed

`sealed/` holds the planted-mistake generator, the answer key and the
per-transaction scores. It is encrypted in git. Only the sealed clone can read
it, and only `data/` and `evals/` branches work there.

- In an `agent`, `connectors`, `ui` or `shared` session: you cannot read
  `sealed/`. Don't try to unlock it, and don't ask anyone what is in it. The
  score only means something if the agent was built blind.
- In a `data` or `evals` session: never copy what you learn there into anything
  unsealed. That means issues, pull requests, commit messages, decision files
  outside `sealed/`, memory, and the public dataset in `data/company/`.
- Full rules: [docs/SEALED.md](docs/SEALED.md).

## 6. Write down decisions with their reasons

Ramp engineers will ask us why. When you choose between real alternatives, add
one file to your area's `decisions/` folder, named `YYYY-MM-DD-short-title.md`:
what was decided, what else was considered, why, and what would change our mind.
`scripts/decisions` lists them all. Keep each one short enough to say out loud.

## 7. No secrets in the repo

Keys and tokens live in `.env`, which git ignores. `.env.example` lists the
names. Never read `.env`, never print a secret, never paste one into code, a
test, a commit or an issue. This repo is public.

## 8. The only authors are Batyrkhan and Noriaki

No `Co-Authored-By` lines, no "Generated with Claude Code", no session links,
in commits, pull requests or issues. Settings turn this off and a commit hook
rejects it. Commit with the git identity already configured on the machine.

## Commands

```
scripts/setup <batyrkhan|noriaki>   # once per clone
uv sync --all-packages              # Python environment (3.12)
uv run pytest                       # tests
uv run ruff check . && uv run ruff format .
uv run python -m auditorium_contracts --help   # validate a dataset or a run
```
