# The Ramp sandbox holds our dataset's bills

Draft for both of us to agree (board #18, #19).

## What

The connectors load the dataset's bills (with their vendors and invoice files)
into the Ramp sandbox, stamped with their dataset id, the way the QuickBooks
loader already stamps ledger entries. A scored run then reads bills live from
Ramp, card transactions from the dataset, and the books live from QuickBooks.
The run says which records came from where. The agent still never writes.

## Why

Ramp's sandbox holds Ramp's own demo company (Save Time Inc). Our dataset is a
different, invented company (Copperline), so reading both live matches nothing
and every transaction fails ledger_match. Ramp's API can create bills but has no
way to create card transactions, so bills are the part we can make real. The
pitch example, the $12,000 consulting bill, is a bill, so it runs live end to end.

## Alternatives

- Keep Ramp read-only: scored runs use the dataset for all Ramp data, and live
  Ramp only appears in a demo on Save Time's data. Simpler, but the scored runs
  never touch Ramp.
- Build a second dataset from Save Time's transactions: live everything, but
  new planted mistakes and a second answer key (data area's call, #18).

## Costs

- Save Time's records stay in the sandbox; the connector reads only stamped bills.
- Bills created in Ramp get new ids; the eval maps them back through the stamp.
- Approvals on loaded bills only exist if someone approves them in Ramp.
- Needs write scopes on our Ramp sandbox app (bills, vendors).

## What would change our mind

A way to create card transactions in the sandbox, or Ramp giving us an empty
sandbox, or the loader distorting bills in a way the answer key can't absorb.
