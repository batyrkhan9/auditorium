# The viewer labels demo and live runs from the run itself, and never guesses

**Decided:**

- The demo run is all `local`, like every source it reads, and its log records
  local file reads. It marks itself with run and dataset ids that start with
  `demo-`. The screen shows the DEMO banner when both ids carry that prefix,
  with or without `--demo`.
- If any source in `run.json` was `live`, the screen does not check evidence
  against the local dataset. Every item shows "Unchecked: live run", not
  "Not verified".
- Sign-off is refused while `finished_at` is empty or the last log line is
  half-written. While the run is still going, the agent's `LogWriter` keeps the
  sequence number and the last hash in memory, so a sign-off appended then
  would fork the chain.

**Considered:**

- Keeping one fake `live` source in the demo so the LIVE badge shows. That
  breaks rule 6 (say what is live). The badge is now shown only in tests.
- A new manifest field for "demo". `RunManifest` forbids extra fields, and
  changing the contract needs both people to agree. An id prefix fits the
  contract as it is.
- Checking each evidence item against the system it came from, so the local
  parts of a mixed run still get checked. Evidence does not always name its
  system, and a live id that also exists locally would show a local value as
  "Verified". Checking nothing in a live run is cruder but never wrong.

**What would change our mind:** a contract field for demo runs, a live
`Sources` the screen can query to re-check live evidence, or a lock file on
the log that would let sign-off and the agent take turns safely.
