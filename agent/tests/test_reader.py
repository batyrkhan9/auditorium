"""The model reader, tested without the network: a fake stands in for Claude."""

from datetime import date
from pathlib import Path

from auditorium_agent.preflight import run_preflight
from auditorium_agent.receipt_reader import Line
from auditorium_agent.service_period import ServicePeriod
from auditorium_contracts import LocalSources, load_run, validate_run

SAMPLE = Path(__file__).parents[1] / "examples" / "sample"
IRONWOOD_LINE = (
    "This invoice covers the design sprint we ran for you in the last two weeks of December 2025."
)


class FakeReader:
    model = "fake-model"

    def __init__(self, quote=IRONWOOD_LINE):
        self.quote = quote
        self.asked = []

    def service_period(self, pages):
        self.asked.append("service_period")
        if "Ironwood" not in pages[0]:
            return None
        return ServicePeriod(
            date(2025, 12, 18), date(2025, 12, 31), 1, self.quote, read_by=self.model
        )

    def merchant(self, vendor_name, pages):
        self.asked.append("merchant")
        return Line(1, "Blue Bottle Coffee") if "BLUEBTL" in vendor_name else None

    def same_business(self, statement_name, books_name):
        self.asked.append("same_business")
        return (statement_name, books_name) == ("AMZN Mktp US", "Amazon")


def run(tmp_path, reader):
    sources = LocalSources(SAMPLE)
    manifest = sources.dataset.manifest
    results = run_preflight(
        sources,
        tmp_path / "run",
        fiscal_year_start=manifest.fiscal_year_start,
        fiscal_year_end=manifest.fiscal_year_end,
        reader=reader,
    )
    by_id = {r.id.split(":")[-1]: r for r in results}
    return sources, by_id, load_run(tmp_path / "run")


def test_reader_settles_what_plain_code_could_not(tmp_path):
    reader = FakeReader()
    sources, results, saved = run(tmp_path, reader)

    period = results["b_1012#period"]
    assert period.status.value == "fail" and period.expected_period == "2025-12"
    assert period.evidence[0].quote == IRONWOOD_LINE and "fake-model" in period.evidence[0].note

    receipt = results["c_2007#receipt"]
    assert receipt.status.value == "pass" and "fake-model" in receipt.rationale

    assert saved.manifest.models == ["fake-model"]
    assert validate_run(tmp_path / "run", sources) == []


def test_reader_is_asked_only_where_the_answer_matters(tmp_path):
    reader = FakeReader()
    _, results, saved = run(tmp_path, reader)
    # Two bills are recorded just after year end with no dates the patterns can read, and one
    # receipt shows the right amount under another name. Nothing else reaches the model.
    # ...and one charge the books record under another name, with no receipt to settle it.
    assert sorted(reader.asked) == ["merchant", "same_business", "service_period", "service_period"]
    calls = [event for event in saved.log if event.action.value == "model_call"]
    assert len(calls) == 4
    # The reader had nothing to say about Brightside, so it stays for a person.
    assert results["b_1004#period"].status.value == "needs_review"


def test_a_quote_that_is_not_in_the_document_is_thrown_away(tmp_path):
    reader = FakeReader(quote="Services rendered in December 2025.")  # the invoice never says this
    sources, results, saved = run(tmp_path, reader)
    assert results["b_1012#period"].status.value == "needs_review"
    discarded = [e for e in saved.log if "discarded" in str(e.detail.get("outcome"))]
    assert len(discarded) == 1 and discarded[0].subject_ids == ["doc_b_1012"]
    assert validate_run(tmp_path / "run", sources) == []


def test_without_a_reader_nothing_is_asked(tmp_path):
    _, results, saved = run(tmp_path, None)
    assert results["b_1012#period"].status.value == "needs_review"
    assert results["c_2007#receipt"].status.value == "needs_review"
    assert saved.manifest.models == []


def stub_client(answer, stop_reason="end_turn"):
    """Stands in for the Anthropic client: records the request, returns a canned response."""
    from types import SimpleNamespace

    response = SimpleNamespace(
        model="claude-opus-5-5",
        stop_reason=stop_reason,
        parsed_output=answer,
        usage=SimpleNamespace(input_tokens=120, output_tokens=40),
    )
    sent = {}

    def parse(**kwargs):
        sent.update(kwargs)
        return response

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))
    return client, sent


def test_claude_reader_builds_the_request_and_reads_the_answer():
    from auditorium_agent.reader import ClaudeReader, _ServicePeriodAnswer

    answer = _ServicePeriodAnswer(
        stated=True, start_date="2025-12-18", end_date="2025-12-31", page=1, quote=IRONWOOD_LINE
    )
    client, sent = stub_client(answer)
    reader = ClaudeReader(model="claude-opus-5-5", client=client)
    period = reader.service_period(("Ironwood Design Studio\n" + IRONWOOD_LINE,))

    assert (period.start, period.end) == (date(2025, 12, 18), date(2025, 12, 31))
    assert period.read_by == "claude-opus-5-5"
    assert sent["model"] == "claude-opus-5-5" and sent["fallbacks"] == "default"
    assert sent["output_config"] == {"effort": "low"}
    assert '<page number="1">' in sent["messages"][0]["content"]
    assert reader.last_call["input_tokens"] == 120


def test_claude_reader_gives_no_answer_on_refusal_or_bad_dates():
    from auditorium_agent.reader import ClaudeReader, _ServicePeriodAnswer

    client, _ = stub_client(None, stop_reason="refusal")
    assert ClaudeReader(model="claude-opus-5-5", client=client).service_period(("x",)) is None

    backwards = _ServicePeriodAnswer(
        stated=True, start_date="2025-12-31", end_date="2025-12-01", page=1, quote="x"
    )
    client, _ = stub_client(backwards)
    assert ClaudeReader(model="claude-opus-5-5", client=client).service_period(("x",)) is None

    client, sent = stub_client(None)
    ClaudeReader(model="claude-haiku-5-5", client=client).service_period(("x",))
    assert "fallbacks" not in sent  # that model has no server-side fallback


def test_claude_reader_stops_at_its_call_limit():
    from auditorium_agent.reader import ClaudeReader, _ServicePeriodAnswer

    answer = _ServicePeriodAnswer(stated=False, start_date="", end_date="", page=0, quote="")
    client, sent = stub_client(answer)
    reader = ClaudeReader(model="claude-opus-5-5", client=client, max_calls=2)
    for _ in range(5):
        reader.service_period(("x",))
    assert reader.calls == 2  # three requests were never sent
    assert "call limit of 2 reached" in reader.last_call["error"]


def test_vendor_names_with_no_document_are_put_to_the_reader(tmp_path):
    sources, results, saved = run(tmp_path, FakeReader())
    ledger = results["c_2009#ledger_match"]
    assert ledger.status.value == "pass" and "fake-model" in ledger.rationale
    # With the entry confirmed as this charge's, the period check can now run on it.
    assert results["c_2009#period"].status.value == "pass"
    call = next(e for e in saved.log if e.detail.get("purpose") == "vendor_name")
    assert call.detail["outcome"] == "same business"
    assert call.detail["statement_name"] == "AMZN Mktp US"
    # The charge whose receipt names the business never reached the reader.
    assert results["c_2008#ledger_match"].status.value == "pass"
    assert "fake-model" not in results["c_2008#ledger_match"].rationale
    assert validate_run(tmp_path / "run", sources) == []
