# Demo walkthrough

A script for showing Auditorium in about five minutes, and the answers to the
questions we expect. Everything here runs on `main` from a fresh clone, with no
credentials, on the made-up company in `data/company`. Draft: both of us agree
before this is the plan.

## Before you start

```bash
scripts/setup batyrkhan        # or noriaki; once per clone
```

The company is **Copperline, Inc.**, fiscal year 2025, records through February
2026. It is synthetic and every screen says so. 200 transactions (130 card
charges, 70 vendor bills), 255 ledger entries, 197 documents as PDFs.

## 1. Pre-flight: check everything before the auditor asks (2 minutes)

```bash
uv run python -m auditorium_agent preflight data/company --out runs/demo
```

It prints one line per check and ends with `800 results ... (run folder valid)`:
four checks on each of 200 transactions.

**Show the pitch example.** Find *Ridgeline Strategy Group LLC*, $12,000:

> Work done in December 2025 is first recorded in January 2026, with no accrual
> in the year it was done. It is recorded in the wrong year.

Say what stands behind it: the invoice line with the service dates, the ledger
entry's date, and a search of December journal entries that found none for that
vendor. Then show the contrast: a December bill that *was* accrued passes, and
the agent says which journal entry did it.

**Then open it in the screen:**

```bash
uv run python -m auditorium_ui runs/demo
```

Click the Ridgeline result. Each piece of evidence shows the document page and
the quoted line, or the record field and its value, and what it was verified
against. Sign it off as a reviewer; the sign-off lands in the log.

## 2. Sample request: the auditor picks transactions (1 minute)

```bash
uv run python -m auditorium_agent sample data/company \
  --request data/company/samples/sample-2026-02.json --out runs/demo-sample
```

Twelve lines from the auditor become twelve packets: receipt or invoice,
approval, the policy rule, the ledger entry, and the bank statement line for
paid bills. Two packets list a bank payment as missing. Point out that the
request is written the way an auditor writes it, from the ledger, with no
internal ids.

## 3. The proof nothing was made up (1 minute)

```bash
uv run python -m auditorium_contracts validate-run runs/demo
```

`OK (verified against snapshot)`. Explain in one breath: at the start of every
run the agent logs a fingerprint of the data it fetched and saves a copy. The
log is hash-chained, so neither can be changed afterwards. This command
re-checks every quote and every record value against that copy. It says
"against snapshot" on purpose: it proves the evidence matches what was fetched,
not that the fetch was honest. Only a check against the live systems does that.

## 4. Where Claude comes in (1 minute, needs an API key)

```bash
uv run --env-file .env python -m auditorium_agent preflight data/company --claude --out runs/demo-claude
```

The checks are plain code. Claude is asked only where code cannot settle a
result: an invoice that states its service dates in a sentence, or a vendor name
with no document to tie it to the books. On this dataset that is 11 requests,
about 6 cents. Every answer about a document must quote a line, and the agent
throws the answer away if the line is not on the page.

## What is live and what is not

Say this plainly; it is in the README too.

- **QuickBooks:** a real sandbox, loaded with this dataset's ledger, read back
  live. Add `--live` with credentials set.
- **Ramp:** a real Ramp sandbox, read through the Developer API. The dataset's
  68 paid bills are loaded there and read back live. The 2 unpaid bills and the
  130 card transactions come from the dataset, because Ramp's API cannot create
  card transactions. Every run records which records came from where. (Noriaki
  to confirm this paragraph and the MCP path; the README status table is the
  source.)
- **Without credentials,** as in the commands above, everything reads from the
  dataset folder, and `run.json` says `local` for each system.

## The score

Scored by a harness that holds the answer key; the sessions that built the agent
never saw it. The dataset has 40 planted mistakes among 800 checks.

The agent on `main`, with the Claude reader on:

| Check | Planted mistakes caught | Missed | False alarms | Left for a person |
|---|---|---|---|---|
| receipt | 9 of 10 | 0 | 0 | 5 |
| approval | 12 of 12 | 0 | 0 | 0 |
| ledger match | 10 of 10 | 0 | 0 | 0 |
| period | 6 of 8 | 1 | 0 | 2 |

37 of 40 caught, 1 missed, no false alarms, 7 results left for a person. All
3,142 pieces of evidence verified. Without the Claude reader: 35 of 40 caught,
the same single miss, no false alarms, 17 left for a person.

**How to say it honestly.**

- These numbers are from a local scoring run on 2026-11-11 and are **not yet the
  published scorecard**. The files in `evals/scorecards/` are for an older agent
  (35 caught, 6 false alarms, 48 left for a person). Replace this section when
  the new scorecard is committed.
- The agent was built without seeing the answers, but it was improved several
  times using the totals from this same dataset. Say so if asked how the score
  was reached. There is no second, untouched dataset.
- "Left for a person" is neither a catch nor a false alarm. Two of the planted
  mistakes are in that pile, not flagged: one receipt and one period.

## Questions we expect, and where the answer is written down

`scripts/decisions` lists every decision with its reasons. The ones most likely
to come up:

| Question | Short answer | File |
|---|---|---|
| How do you know the agent did not make things up? | Every piece of evidence is a page and a quote, or a field and a value, and one shared function re-checks all of them. | `decisions/2026-11-09-repo-setup.md`, item 5 |
| How do you know the score is honest? | The answer key is encrypted in the repo; agent sessions worked in a clone without the key. | `docs/SEALED.md` |
| Why is most of it plain code and not an LLM? | Comparing fields is exact, free and repeatable. A model is used only where the outcome depends on reading. | `agent/decisions/2026-11-09-claude-reads-only-what-code-cannot-settle.md` |
| Why the Claude API and not the Agent SDK you pitched? | Each question is one request with one structured answer; there is no loop for an agent to run. | same file |
| Why does a November cost booked in December pass? | The year-end question is cut-off across fiscal years; month slips inside the year do not change the annual numbers. | `agent/decisions/2026-11-09-period-check-judges-by-fiscal-year.md` |
| What about false alarms? | Unclear cases are `needs_review`, not `fail`: tips, prepayments, work across the year end. | the receipt, period and refunds decisions in `agent/decisions/` |
| Could a model answer change the result between runs? | Only the few results it is asked about, and its quotes are verified each time. The cap is 50 requests a run. | Claude reader decision |
| What does "verified" mean on the screen? | It always says against what: the snapshot or the live sources. | `docs/CONTRACT.md`, "Verifying a run later" |
| Why are the card transactions not live from Ramp? | Ramp's API can create bills but not card transactions, so only the bills could be loaded. Each run says which records came from where. | README status table |

## If something goes wrong on the day

- A command fails on credentials: drop `--live` and `--claude`. Everything above
  except section 4 runs offline.
- The screen will not start: the two runs in `agent/runs/` are already on
  `main`; open one with
  `uv run python -m auditorium_ui agent/runs/copperline-preflight --dataset data/company`.
