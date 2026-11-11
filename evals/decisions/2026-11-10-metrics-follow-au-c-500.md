# Each metric answers one AU-C 500 question

AU-C 500 asks whether audit evidence is **sufficient** (enough of it) and
**appropriate** (relevant and reliable). The eval reports one number for each
question it can answer mechanically.

| AU-C 500 | What we measure | Where |
|---|---|---|
| Reliability: the evidence exists and is what it says | `verify_evidence` on every piece of every result, against the data the run used. Any failure is fabricated or unresolvable evidence and is printed first. Must be 0. | `Scorecard.evidence_unverified` |
| Reliability: external evidence is stronger than internal | The mix of evidence: document (external / internal origin), record, search | `evidence_by_kind` in the metrics file |
| Relevance: the evidence supports the conclusion about the assertion | Precision and recall on `fail`, per check. receipt and approval speak to occurrence (and authorization), ledger_match to accuracy and completeness, period to cut-off. | `per_check`, `precision_on_fail`, `recall_on_fail` |
| Relevance: a clean item is not reported as an exception | False-alarm rate on transactions the key calls clean (PRD rule 5) | `false_alarm_rate_on_clean` |
| Sufficiency: there is a result, with evidence, for every item | Results missing per check; a result with no verifiable evidence earns no credit | `results_missing`, outcome `unsupported` |
| Sufficiency: the agent knows when it does not have enough | needs_review rate (see the needs_review decision) | `needs_review_rate` |

Issue codes are secondary: on a true positive, does the agent's code agree with
the key's (`issue_code_agreement_on_tp`).

**Considered.** One accuracy figure per check. Rejected: with few planted
failures among many clean transactions, "everything passes" already scores
above 90%, and it says nothing about false alarms or made-up evidence.

**What would change our mind.** Ramp's engineers or an auditor asking for a
specific materiality weighting (by amount), which we could add as a second view.
