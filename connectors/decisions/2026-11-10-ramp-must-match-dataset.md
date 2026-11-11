# Ramp is live only if it holds the dataset's Ramp transactions

Decided 2026-11-10.

**What happened.** Ramp's sandbox holds Ramp's own demo business, Save Time Inc
(1,413 card transactions dated 2026). Our dataset `copperline-fy2025-v1` is an
invented company, and its ledger is what we loaded into the QuickBooks sandbox.
`connect_sources` read Save Time from live Ramp and Copperline's books from live
QuickBooks. Nothing could match, so every transaction would fail `ledger_match`:
a false alarm on every clean transaction.

**What.** After a live Ramp read, `connect_sources` matches the dataset's
transactions whose `source.system` is `ramp` against what Ramp returned, by
`(record_type, record_id)`.
- None found: Ramp holds a different company. Ramp falls back to the dataset, and
  `describe()` says so: "Ramp holds a different company (0 of 200 Ramp transactions
  of dataset copperline-fy2025-v1 found among 1419 read from Ramp); using the dataset".
- Some found: Ramp stays live, and `detail` says "N of M Ramp transactions missing,
  K Ramp transactions not from the dataset".
- The dataset has no Ramp transactions: nothing to compare, Ramp stays live.

This is the rule QuickBooks already follows: no stamped dataset entry there means
local, any overlap means live with the gap counted.

**Considered.**
- A config flag that says which company Ramp holds. A flag can be wrong; the ids
  cannot.
- A threshold for partial overlap (for example live only above 90%). Any number
  would be a guess today, and one shared rule for Ramp and QuickBooks is easier to
  explain. The gap is in `detail`, so a reviewer sees it.
- Matching by vendor, amount and date. Two different companies can share those;
  Ramp ids cannot.

**Why.** PRD rule 5: false alarms count against us. Rule 6: say what is live. A
Ramp that holds another company is live but not about our audit, so using it would
only produce failures that are not real.

**Also.** `qbo load --dry-run` is offline. It used to print "would load N" even
when every entry was already in QuickBooks. It now prints "plan: N entries ...;
QuickBooks not checked (a real load skips entries already stamped as loaded)".

**What would change our mind.** Copperline's transactions loaded into a Ramp
sandbox, which makes this check pass on its own. Or a partial overlap that produces
false alarms in a run, which would argue for a threshold.
