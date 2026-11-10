# Area: data

Owner: Batyrkhan. Branches: `data/<topic>`, in the SEALED clone.

The fake company, the planted mistakes and the answer key.

- The generator and the planting logic live in `sealed/datagen/`. The answer key
  (`ExpectedResult` lines, one per transaction and check) lives in `sealed/answer_key/`.
- The public dataset goes in `data/company/` and must pass
  `python -m auditorium_contracts validate-dataset data/company`.
- The public dataset must not give the answers away: no hint fields, no telling
  file names, ids or ordering.
- Decisions that reveal planting go in `sealed/datagen/decisions/`, not here.
- Nothing from `sealed/` goes into issues, commit messages or anything unsealed.

Record decisions in `data/decisions/YYYY-MM-DD-short-title.md`: what, what else was
considered, why, what would change our mind. Root rules: ../CLAUDE.md.
