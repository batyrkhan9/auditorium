# sealed/

Everything in this folder except this file is encrypted in git with git-crypt.
It holds the planted-mistake generator, the answer key and the per-transaction
scores. Sessions that build the agent work in a clone without the key, so they
never see any of it. See ../docs/SEALED.md.

- `datagen/`     the generator and how mistakes are planted (data area)
- `answer_key/`  expected result for every transaction and check (data area)
- `eval/`        per-transaction scoring output (evals area)
