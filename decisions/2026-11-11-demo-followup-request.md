# A second sample request for the demo, kept out of the dataset

**Decided.** The demo uses a second auditor request,
`docs/demo/sample-2026-03-followups.json`, so it can show follow-up drafts. The
dataset's own request (`data/company/samples/sample-2026-02.json`) lacks only
bank payments, which no coworker can supply, so it produces no drafts.

**Considered.** Adding the request to `data/company/samples/`. The dataset
generator deletes that folder every time it runs, so a hand-added file would be
lost. Changing the generator to write a second request would also mean writing
a second sample answer key, for a request that exists only for the demo.

**Why.** The seven lines were picked from the agent's own pre-flight results on
`main`, not from the answer key: two missing receipts, one invoice that belongs
to another bill, two missing approvals, and two clean transactions. Each line is
written from the ledger entry, as an auditor would write it. Because it was
chosen by looking at the agent's output, it is not scored and is not evidence of
anything beyond "the drafts work".

**What would change our mind.** If sample-request mode gets scored, a second
request belongs in the generator, with its own answer key.
