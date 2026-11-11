# Demo video script (about 2:45)

Two presenters: Batyrkhan (the agent and the data) and Noriaki (live systems,
screen, score). About 390 spoken words, at a steady pace. The longer walkthrough
and the answers to expected questions are in [DEMO.md](DEMO.md).

## Dashboards: what to show

- **QuickBooks sandbox: yes, one cutaway of about 6 seconds.** The Ridgeline
  bill on its January date, and no December accrual. It proves the pitch
  example is on real books.
- **Ramp sandbox: yes, about 5 seconds.** The Bills list only. Card transactions
  come from the dataset, because Ramp's API cannot create them, so do not open
  them in Ramp.
- **Braintrust: no.** The committed scorecards were scored locally and say so.
  Show the score table instead.

## Before recording

Run everything first, so nothing on camera waits on a command. The agent will not
write into a folder that already holds a run: to redo a run, delete its folder
first (`rm -rf runs/video`).

```bash
uv run --env-file .env python -m auditorium_agent preflight data/company --live --claude --out runs/video
uv run python -m auditorium_agent sample data/company \
  --request docs/demo/sample-2026-03-followups.json --out runs/video-followups
uv run python -m auditorium_contracts validate-run runs/video
uv run python -m auditorium_ui runs/video
```

Open these browser tabs, in order:

1. The screen on `runs/video`.
2. The QuickBooks sandbox, on the Ridgeline Strategy Group LLC bill for $12,000.
3. QuickBooks journal entries for December 2025.
4. The Ramp sandbox Bills list.
5. A second screen on `runs/video-followups`, on the Drafts page
   (`uv run python -m auditorium_ui runs/video-followups --port 8001`).

Setup: terminal font 18pt or larger, notifications off. Record each voice
separately and cut it together.

## Script

### 1. The problem: 0:00 to 0:20 (Noriaki)

**On screen:** title card, then the README headline.

> "When a startup raises a Series B, investors require an audit. The auditor
> sends hundreds of document requests, and the finance team spends weeks
> digging through Ramp, QuickBooks and bank statements to answer them. We built
> Auditorium: an agent that gets the spending ready before the auditor asks."

### 2. Pre-flight: 0:20 to 0:45 (Batyrkhan)

**On screen:** the screen at http://127.0.0.1:8000/, no terminal.

- 0:20 to 0:28: the top of the page, "800 Results" and "of 800 check results
  across 200 transactions".
- 0:28 to 0:35: the per-check rows below it (receipt, approval, ledger_match,
  period).
- 0:35 to 0:40: the "Models: claude-opus-5-5" line.
- 0:40 to 0:45: click "fail 37" and scroll until Ridgeline Strategy Group is in
  view, which leads into scene 3.

Do not stop on "Follow-up drafts 0": pre-flight never drafts; scene 6 shows the
drafts.

> "Pre-flight goes through every card charge and vendor bill, 200 transactions
> here, and runs four checks on each. Is there a receipt? Was it approved by the
> right person? Does it match the books? Is it in the right period? Most of it
> is plain code. Claude is asked only where code can't settle it, which on this
> company is eleven questions."

### 3. The pitch example: 0:45 to 1:15 (Batyrkhan, cutaway Noriaki)

**On screen:** click the Ridgeline Strategy Group, $12,000, `period` result.
Show the summary, then the invoice evidence: the PDF with
"Service period: December 1 - December 19, 2025" highlighted.

> **Batyrkhan:** "This is the example from our pitch. A twelve-thousand-dollar
> consulting bill. The invoice says the work was done in December (click open the "Original file"). The books
> record it in January, and nobody accrued it in December. So it's in the wrong
> year, and the agent flags it, with the invoice line quoted."

**Cutaway (about 6 seconds):** the QuickBooks tab with the Ridgeline bill dated
01/16/2026, then December's journal entries with no Ridgeline line.

> **Noriaki:** "And that's the real QuickBooks sandbox, read live: January
> sixteenth, and no December accrual."

### 4. Evidence and sign-off: 1:15 to 1:40 (Noriaki)

**On screen:** the Ridgeline result. Move over the evidence list and its
"verified against" label. Type a reviewer name, choose **Accepted**, sign off,
then show the new event on the Log page.

> "Every result links to the real document or record, so an auditor can check
> it without trusting the agent. A reviewer signs off here, and the sign-off
> goes into a hash-chained log of every step, pinned to exactly what the
> reviewer saw."

### 5. Live sources: 1:40 to 1:55 (Noriaki)

**On screen:** the Ramp sandbox Bills list with Copperline's vendors. Then the
screen's "Sources: live or local" section, showing ramp and qbo as **Live** and
bank and company as **Local**. Do not dwell on the long Ramp detail text.

> "Bills come live from a Ramp sandbox, and the ledger from QuickBooks. Ramp's
> API can't create card transactions, so those come from our test company, and
> every run says which records came from where."

### 6. Sample request and drafts: 1:55 to 2:20 (Batyrkhan)

**On screen:** the Drafts page of `runs/video-followups`. Scroll through the
five drafts; stop on the email to Ingrid about the invoice for another bill.

> "When the auditor picks transactions to test, the agent builds a packet for
> each one: receipt, approval, ledger entry, bank payment. For anything a
> coworker can supply, it drafts the message. Here it's asking Ingrid for the
> right invoice, because the one attached is for a different bill. It's a
> draft, though. The agent never sends anything."

### 7. Proof and score: 2:20 to 2:40 (Noriaki)

**On screen:** the terminal running `validate-run runs/video`, which prints
`OK (verified against snapshot)`. Then a slide:

| | Caught | False alarms | Left for a person |
|---|---|---|---|
| 40 planted mistakes, 800 checks | 37 | 0 | 7 |

> "We test it against a company with forty planted mistakes, and the answer key
> was sealed from the people who built the agent. It catches thirty-seven, with
> zero false alarms, and leaves seven for a person rather than guessing. Every
> one of three thousand pieces of evidence is checked back against what was
> fetched."

Source: `evals/scorecards/run_20261111_035553.json` (with the Claude reader,
3,142 pieces of evidence, 0 unverified).

### 8. Close: 2:40 to 2:50 (Batyrkhan)

**On screen:** the README table "What we pitched and what we built", then an end
card with the repo URL.

> "Auditorium doesn't give an audit opinion. It does the evidence work, so
> people can decide. Everything's in the repo, with every decision written
> down."

## Do not say

- "Fully live from Ramp." Card transactions and the 2 unpaid bills come from the
  dataset.
- "Scored in Braintrust." The scorecards were scored locally.
- "Built on the Agent SDK." It calls the Claude API directly.
- Anything that suggests real customer data. The company is synthetic, and the
  screen says so.

## To cut it to 2:00

Drop scene 5, and fold one line of it into the scene 3 cutaway ("Ramp and
QuickBooks sandboxes, read live"). Shorten scene 4 to the sign-off click.
