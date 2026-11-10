# Area: agent

Owner: Batyrkhan. Branches: `agent/<topic>`, in the build clone.

The audit agent: the four checks, sample requests, follow-up drafts, the step log.

- Read source data only through `auditorium_contracts.Sources`. Use `LocalSources`
  on a dataset folder until the live connectors exist.
- Write a run folder that passes `python -m auditorium_contracts validate-run`.
  Call `verify_evidence` before you record a result. Never cite what you cannot resolve.
- Write the log with `LogWriter`. Drafts are never sent.
- You never see `sealed/`. You see only the aggregate scorecards in `evals/scorecards/`.
  Do not tune to individual transactions, and do not ask what was planted.
- The pitch example must pass: a $12,000 consulting bill, work done in December,
  recorded in January, no December entry, flagged `period_wrong`.

Record decisions in `agent/decisions/YYYY-MM-DD-short-title.md`: what, what else was
considered, why, what would change our mind. Root rules: ../CLAUDE.md.
