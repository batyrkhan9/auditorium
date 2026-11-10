# Area: evals

Owner: Noriaki. Branches: `evals/<topic>`, in the SEALED clone.

The test that scores the agent against the answer key (AU-C 500 based, in Braintrust).

- Harness code lives here in `evals/`. Anything per-transaction (which ones the
  agent got wrong) goes in `sealed/eval/`, never here.
- Commit only aggregate `Scorecard` files to `evals/scorecards/`. No transaction ids.
- Use `verify_evidence` from the contract as the made-up-document check.
- When you run the agent, give it the public dataset only. It must not be able to
  read `sealed/`.
- Nothing from `sealed/` goes into issues, commit messages or anything unsealed.

Record decisions in `evals/decisions/YYYY-MM-DD-short-title.md`: what, what else was
considered, why, what would change our mind. Root rules: ../CLAUDE.md.
