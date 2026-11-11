# Claude reads only what plain code cannot settle, and must quote the document

**Decided.** The checks stay plain code. Claude is asked in exactly two places:

1. **Service dates.** A bill is recorded just after year end and the patterns
   find no service period on the invoice. Without an answer the result is
   `needs_review`. Claude is asked when the work was done.
2. **Merchant names.** A receipt shows the right amount but does not name the
   vendor the way the card statement does ("SQ *BLUEBTL OAK" against "Blue
   Bottle Coffee"). Claude is asked whether they are the same business.

Every answer must carry one line quoted from the document. `Books` looks for
that line on that page and throws the whole answer away if it is not there.
The evidence note names the model, and for dates it says plainly that the line
is verified and the dates are the model's reading of it.

It is off unless the run is started with `--claude`.

**Why only two places.** Most transactions are settled by comparing fields, and
code does that exactly, for free, and the same way every time. A model call per
transaction would cost more, run slower, and make the score move between runs.
Asking only where the outcome depends on the answer means a model call always
buys something: a `needs_review` becomes a `pass` or a `fail`.

**Why the quote rule.** "Never make up a document" has to hold for the model
too. A quoted line that is checked against the file cannot be invented. What
the model adds is the reading of that line, and a reviewer can judge that in
seconds because the line is in front of them.

**Why Claude gets the page text, not the PDF.** It reads the same extracted text
the verifier compares against, so an honest quote always verifies.

**Why the Claude API directly, not the Agent SDK.** Each question is one
request with one structured answer. There is no loop, no tool use, nothing for
an agent to decide. Our pitch listed the Agent SDK; if we want it in the build,
the place it fits is sample mode (deciding what to fetch next from live
sources), not these two readings. This is a call for the two of us, and worth
having an answer ready when Ramp asks.

**Model.** `claude-opus-5-5` at low effort by default; set
`AUDITORIUM_READER_MODEL=claude-sonnet-5-5` to use Sonnet. Our pitch names both.
Requests opt in to the API's server-side fallback, so a declined request is
retried on another model instead of failing. The log records which model
actually answered and the tokens used.

**Failure.** No credentials, or a model that does not exist: the run stops
before it starts. An API error or a refusal during a run: logged, and that one
result stays `needs_review`.

**Checked live once**, on 2026-11-10, on the 19-transaction sample: three
requests, all answered by `claude-opus-5-5`, 2,775 tokens in and 223 out, about
1.6 cents at list price. Both quotes verified against the documents, and for
the invoice with no service dates it answered that none were stated. Run again
the same day on the PDF version of the sample with the same three outcomes.
Not yet run on the scored dataset.

**Spend is capped twice.** The reader stops after 50 requests in a run
(`--max-claude-calls`), and the API key should come from a Console workspace
with its own monthly spend limit, which no code can exceed.

**What would change our mind.** If the scored dataset shows many `needs_review`
results that a model could settle elsewhere (free-text sample requests, scanned
receipts), add those as further narrow questions under the same quote rule.
