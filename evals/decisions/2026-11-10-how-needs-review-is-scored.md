# needs_review is neither a hit nor a false alarm, and is reported on its own

The contract leaves this to evals (docs/CONTRACT.md, "Not decided yet").

**Decided.** The key only says pass, fail or not_applicable. When the agent says
`needs_review`:

- on a transaction the key calls clean (pass or not_applicable), it is **not a
  false alarm**. PRD rule 5 tells the agent to say `needs_review` when the
  evidence is unclear, so punishing it as a false alarm would push the agent
  toward guessing.
- on a transaction the key calls a fail, it is **not a hit**. Recall counts only
  a verified `fail`. We also report recall counting review
  ("recall_counting_review"), because a person would still have looked at it.
- it is counted in `CheckScore.needs_review` and reported as a rate, overall and
  on clean transactions. A high rate on clean ones is the cost: reviewer time.

`pass` and `not_applicable` both count as "said it is clean" for the fail
metrics. Whether the status matches exactly is reported separately as
`status_agreement`, because the contract scores on transaction, check and status.

**Considered.**
1. needs_review on a clean transaction as a false positive. Too harsh: it is the
   answer the PRD asks for when evidence is unclear.
2. needs_review on a failed transaction as a true positive. Too generous: an
   agent that said needs_review on everything would score perfect recall.
3. Half credit. A made-up number that hides both effects in one figure.

**Why.** Two numbers that can each be read on their own (recall, review rate)
beat one blended number. The agent can't game one without the other moving.

**What would change our mind.** An auditor telling us how they weigh a flagged
item against an item queued for review. Then a weighted cost could replace the
separate rates.
