# When the books and the card statement name a vendor differently, the receipt settles it first

**The problem.** A card statement shows "SQ *JUNIPER COFFEE". The books record
the same amount, a day later, under "Juniper Coffee Roasters". Plain code cannot
tell that those are one business, so ledger match left it for a person, and the
period check could not run because the entry was not confirmed as this charge's.

**Decided.** Such a pairing is settled once, before the checks run, in this order:

1. **By the attached document.** If the receipt attached to the charge names the
   business the books record it under, it is settled. No model is involved.
2. **By the model reader**, only when no attached document names it and the run
   was started with `--claude`. The reader is asked whether the two names are
   one business.
3. **Otherwise** it stays `needs_review`, as before.

The receipt check uses the same link the other way round: a receipt that shows
the amount and names the business the books record this charge under is
accepted, without asking the model.

**Why the receipt first.** Three independent things then agree: the charge's
amount and date in Ramp, the entry in the books, and a document attached to the
charge that names the books' vendor. The coded statement name is the least
reliable of the lot. This is evidence anyone can check, it costs nothing, and it
gives the same answer every run.

**Why the model is allowed without a quote here.** For documents, the reader
must quote a line that is then verified. Here there is no document, so the
anchor is the two names themselves, each cited as a record field. The result
says a model judged them and shows both names for the reviewer.

**What this does not catch.** If the receipt and the books agree with each other
but both are for a different business than the card statement's, this passes it.
That would take two systems being wrong in the same way.

**Measured on the scored dataset** (public outputs only; whether they match the
answer key is for the eval to say):

| Left for a person | Before | Now | Now with `--claude` |
|---|---|---|---|
| ledger match | 20 | 2 | 0 |
| receipt | 17 | 3 | 3 |
| period | 10 | 10 | 2 |
| period, could not run | 24 | 6 | 4 |

A `--claude` run now makes 11 requests where it made 23, about 6 cents.

**Considered.** Asking the model for all 20. It would have worked, at a cost per
run and with an answer that can vary. The receipt was already there.

**What would change our mind.** If the eval shows passes here that the answer
key calls failures, require the model to agree as well before settling by
document.
