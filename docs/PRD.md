# Auditorium: what we are building

Short on purpose. Every session reads this before it works. If something here is
wrong, fix it on a `shared/` branch. Don't work around it.

## The problem

Once a startup raises a Series B or C, investors usually require an audit. The
auditor sends a PBC ("provided by client") list: hundreds of requests for
documents. The finance team spends weeks pulling files from the card platform,
the accounting system and the bank, checking them by hand, and chasing coworkers
for what is missing.

## The product

Auditorium is an AI agent that gets a company's spending ready for the audit. It
covers card expenses and vendor bills. It works in two modes.

**Pre-flight, before the auditor asks for anything.** It goes through every
transaction and runs four checks:

| Check | Question | It fails when |
|---|---|---|
| `receipt` | Is there a receipt (card) or a vendor invoice (bill)? | One was required and none is attached, or the attached one belongs to something else |
| `approval` | Was it approved, if the rules required approval? | A rule applied and the right person did not approve it |
| `ledger_match` | Does it match the entry in the accounting system? | QuickBooks has no matching entry, or the entry differs |
| `period` | Is it recorded in the right period? | The books place it in a different period from when the goods or service were delivered, and nothing else (an accrual, for example) corrects that |

Each check ends as `pass`, `fail`, `needs_review` or `not_applicable`. Failures
are flagged before the auditor finds them.

**Sample requests, when the auditor picks transactions to test.** It pulls the
documents for each picked transaction through Ramp's MCP server, QuickBooks and
bank statements, lines them up, and drafts a Slack or email message for anything
missing.

## Rules we never trade away

1. **Never make up a document, a quote or a record.** Every piece of evidence
   must resolve to something that exists in the sources. A check with no
   evidence is not a result.
2. **Every check links to the real documents and records**, so the auditor can
   verify it without trusting the agent.
3. **A log records every step**, and a reviewer signs off in that log.
4. **Follow-ups are drafts.** The agent never sends a message.
5. **False alarms count against us.** Flagging a clean transaction is a failure,
   not caution. When the evidence is unclear, the answer is `needs_review`.
6. **Say what is live.** If a real connection does not work we fall back to local
   data, and the run output and the README say so.

## What we promised Ramp

- Working code on Ramp's demo MCP server and a QuickBooks Online sandbox, tested
  against planted mistakes.
- The example from our pitch must work: a $12,000 consulting bill, work done in
  December, recorded in January, and nobody booked it in December another way.
  It gets flagged as recorded in the wrong year.
- We submit this repo plus a working demo, and Ramp engineers will ask us to
  explain our decisions. Write decisions down as you make them.

## Tools

This is the list from our pitch. Where the build uses something else, the README
says what and why, under "What we pitched and what we built".

- Claude Sonnet 5.5 and Opus 5.5 through the Claude Agent SDK
- Ramp's MCP server (the public demo server for the build)
- A QuickBooks Online sandbox through Intuit's MCP server
- PDF reading for the PBC list, bank statements and invoices
- A fake company dataset, with planted mistakes mixed into clean transactions so
  we can measure false alarms. Its ledger is loaded into the QuickBooks sandbox
  and its bills into the Ramp sandbox
- A test set based on AU-C 500 (audit evidence) that scores each result and
  checks the agent never makes up a document, run in Braintrust
- Slack and email drafts for follow-ups
- A log of every step with a place for the reviewer to sign off

## Who builds what

| Area | Owner | What it is |
|---|---|---|
| `agent` | Batyrkhan | The audit agent: the four checks, sample requests, drafts, the log |
| `data` | Batyrkhan | The fake company, the planted mistakes, the answer key |
| `connectors` | Noriaki | Ramp and QuickBooks connections, loading test data into both sandboxes |
| `evals` | Noriaki | The test that scores the agent |
| `ui` | Noriaki | The screen |
| `shared` | both | The data contract, README, demo, repo rules |

## Out of scope

Assumptions made at setup. Change them on a `shared/` branch if they are wrong.

- Sending messages, or writing anything to a real company's Ramp or books. The
  agent itself never writes anywhere. The connectors' loaders write our test
  data into the QuickBooks sandbox and the Ramp sandbox, and nowhere else. Ramp's
  API can create bills, vendors and users but not card transactions, so in the
  Ramp sandbox only the dataset's bills are ours; card transactions come from
  the dataset, and each run says which records came from where.
- Spending other than card expenses and vendor bills.
- Currencies other than USD, and more than one legal entity.
- An audit opinion. The agent prepares evidence. People decide.
