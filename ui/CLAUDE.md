# Area: ui

Owner: Noriaki. Branches: `ui/<topic>`, in the build clone.

The screen: results, the evidence behind each one, the step log, reviewer sign-off.

- Read run folders through `auditorium_contracts.load_run`. JSON Schemas for other
  languages are in `contracts/schema/`.
- Record sign-offs with `LogWriter.sign_off` so the log's hash chain stays intact.
- Show whether each source was live or local (`RunManifest.sources`).

Record decisions in `ui/decisions/YYYY-MM-DD-short-title.md`: what, what else was
considered, why, what would change our mind. Root rules: ../CLAUDE.md.
