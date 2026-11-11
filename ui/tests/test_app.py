import json
import re
from pathlib import Path
from urllib.parse import quote, unquote

import auditorium_contracts as c
import pytest
from auditorium_ui.app import CHANGED_SINCE_SHOWN, create_app
from auditorium_ui.demo import PREFLIGHT_RUN_ID, build_demo
from starlette.testclient import TestClient

PITCH = "ramp:bill:b_1001#period"
SIGNED = "ramp:bill:b_1001#receipt"  # the demo's one pinned sign-off
REAL_DATASET = Path(__file__).resolve().parents[2] / "data" / "company"


@pytest.fixture
def demo(tmp_path):
    dataset, preflight, sample = build_demo(tmp_path)
    return dataset, preflight, sample


@pytest.fixture
def client(demo):
    dataset, preflight, _ = demo
    return TestClient(create_app(preflight, dataset, demo=True))


def form_values(client, result_id=PITCH) -> dict:
    page = client.get("/result", params={"id": result_id}).text
    return dict(re.findall(r'name="(csrf|result_sha256)" value="([^"]+)"', page))


def csrf(client) -> str:
    return form_values(client)["csrf"]


def sign(client, **fields):
    data = {"result_id": PITCH, "reviewer": "A", "decision": "accepted", **fields}
    shown = form_values(client, data["result_id"]) if "result_id" not in fields else {}
    for key in ("csrf", "result_sha256"):
        if key not in data and key in shown:
            data[key] = shown[key]
    if "csrf" not in data:
        data["csrf"] = csrf(client)
    return client.post("/signoff", data=data, follow_redirects=False)


def run_form(client) -> dict:
    page = client.get("/").text
    return dict(re.findall(r'name="(csrf|results_sha256|run_sha256)" value="([^"]+)"', page))


def sign_run(client, **fields):
    data = {"reviewer": "Dana Reyes", "decision": "accepted", **run_form(client), **fields}
    return client.post("/signoff-run", data=data, follow_redirects=False)


def edit_manifest(run_dir, **changes):
    manifest = json.loads((run_dir / c.RUN_MANIFEST).read_text())
    manifest.update(changes)
    (run_dir / c.RUN_MANIFEST).write_text(json.dumps(manifest))


def edit_result(run_dir, result_id, **changes):
    results = c.read_jsonl(run_dir / c.RESULTS, c.CheckResult)
    c.write_jsonl(
        run_dir / c.RESULTS,
        [r.model_copy(update=changes) if r.id == result_id else r for r in results],
    )


def drop_last_log_lines(run_dir, count):
    log = run_dir / c.LOG
    lines = log.read_text().splitlines(keepends=True)
    log.write_text("".join(lines[:-count]))


# -- the demo ------------------------------------------------------------------


def test_demo_fixture_is_contract_valid(demo, tmp_path):
    dataset, preflight, sample = demo
    sources = c.LocalSources(dataset)
    assert c.validate_dataset(dataset) == []
    assert c.validate_run(preflight, sources) == []
    assert c.validate_run(sample, sources) == []
    assert c.validate_run(preflight) == []  # against its own snapshot
    assert (preflight / c.SOURCES_SNAPSHOT).is_dir()
    assert not (sample / c.SOURCES_SNAPSHOT).exists()
    # The demo's fingerprint events are exactly what log_source_fingerprints writes.
    expected = tmp_path / "expected.jsonl"
    c.log_source_fingerprints(c.LogWriter(expected, "x"), sources)
    want = [e.detail for e in c.read_log(expected)]
    for run_dir in (preflight, sample):
        got = [e.detail for e in c.read_log(run_dir / c.LOG) if "call" in e.detail]
        assert got == want
        manifest = c.load_run(run_dir).manifest
        assert manifest.dataset_path == str(dataset.resolve())
        assert manifest.synthetic_data and all(s.synthetic for s in manifest.sources)


def test_results_page_shows_counts_and_labels(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "DEMO RUN" in page.text and "data-demo" in page.text
    assert 'data-synthetic="ramp qbo bank company"' in page.text
    assert page.text.count("data-synthetic-source") == 4
    assert PREFLIGHT_RUN_ID in page.text
    # One row per transaction, one cell per check: 5 transactions, 20 results.
    assert visible_rows(page.text) == 5
    assert page.text.count("data-result=") == 20
    assert "Northwind Advisory LLC" in page.text and "$12,000.00" in page.text
    assert 'data-evidence="snapshot"' in page.text
    assert "verified against snapshot" in page.text
    assert "not verified" not in page.text.lower()
    assert re.findall(r'data-result="([^"]+)"[^>]*data-signoff-state="(\w+)"', page.text) == [
        (SIGNED, "pinned")
    ]


def visible_rows(page: str) -> int:
    """Rows the filter keeps. Every row is rendered; the others carry `hidden` first."""
    return page.count("<tr data-tx=")


def lit_cells(page: str) -> list[str]:
    """Result cells in visible rows that the filter does not dim."""
    rows = re.findall(r"<tr data-tx=.*?</tr>", page, re.S)
    return [
        cell
        for row in rows
        for cell in re.findall(
            r'<a class="cell s-\w+(?: so[ \w-]*)?(?: drifted)?" data-result="([^"]+)"', row
        )
    ]


def test_filters(client):
    page = client.get("/", params={"status": "fail"}).text
    assert visible_rows(page) == 3
    assert page.count("data-result=") == 20  # hidden rows stay, for filtering in place
    assert len(lit_cells(page)) == 3
    page = client.get("/", params={"status": "fail", "check": "period"}).text
    assert lit_cells(page) == [PITCH]
    page = client.get("/", params={"status": "attention"}).text
    assert visible_rows(page) == len(
        {
            r.split("#")[0]
            for r in re.findall(
                r'data-result="([^"]+)" [^>]*data-status="(?:fail|needs_review)"', page
            )
        }
    )
    assert visible_rows(client.get("/", params={"q": "northwind"}).text) == 1
    empty = client.get("/", params={"q": "no such vendor"}).text
    assert visible_rows(empty) == 0 and 'id="tx-empty">' in empty


def test_rows_sort_with_exceptions_first(client):
    page = client.get("/").text
    worst = re.findall(r'<tr data-tx="[^"]+" class="w-(\w+)"', page)
    order = {"fail": 0, "needs_review": 1, "pass": 2, "not_applicable": 3}
    assert worst == sorted(worst, key=order.get) and worst[0] == "fail"
    by_amount = client.get("/", params={"sort": "amount"}).text
    amounts = [
        int(a) for a in re.findall(r'<tr data-tx="[^"]+" [^>]*data-amount="(\d+)"', by_amount)
    ]
    assert amounts == sorted(amounts, reverse=True)
    # An unknown filter value is ignored rather than hiding everything.
    assert visible_rows(client.get("/", params={"status": "bogus", "check": "x"}).text) == 5


def test_pitch_example_is_verified_against_the_snapshot(client):
    page = client.get("/result", params={"id": PITCH})
    assert page.status_code == 200
    assert "period_wrong" in page.text
    assert "Services rendered: December 1 - December 31, 2025" in page.text
    assert "2026-01" in page.text and "2025-12" in page.text
    assert page.text.count('data-verification="verified"') == 3
    assert page.text.count("Verified against snapshot") == 3
    assert ">Verified<" not in page.text  # never just "verified"
    assert 'data-kind="search"' in page.text and "<strong>nothing</strong>" in page.text


def test_sample_run_without_snapshot_is_verified_against_the_dataset(demo):
    dataset, _, sample = demo
    client = TestClient(create_app(sample, dataset))
    page = client.get("/result", params={"id": PITCH}).text
    assert 'data-evidence="dataset"' in page and "(from --dataset)" in page
    assert page.count("Verified against dataset") == 3
    assert "snapshot" not in client.get("/").text.split("<main>")[1].split("<table")[0].lower()


def test_unverifiable_evidence_is_flagged(demo):
    dataset, preflight, _ = demo
    edit_result(
        preflight,
        PITCH,
        evidence=[
            c.DocumentEvidence(
                document_id="doc_inv_2041", page=1, quote="Services rendered: January 2026"
            )
        ],
    )
    client = TestClient(create_app(preflight, dataset))
    page = client.get("/result", params={"id": PITCH})
    assert 'data-verification="failed"' in page.text
    assert "Not verified against snapshot" in page.text
    assert "quote not found" in page.text
    assert "1 evidence item(s) did not verify" in client.get("/").text


# -- which dataset, and why ----------------------------------------------------


def test_dataset_path_from_run_json_is_used_when_its_id_matches(demo):
    _, _, sample = demo
    page = TestClient(create_app(sample)).get("/result", params={"id": PITCH}).text
    assert 'data-evidence="dataset"' in page and "(from run.json dataset_path)" in page
    assert page.count("Verified against dataset") == 3


def test_dataset_path_with_another_dataset_is_not_used(demo, tmp_path):
    dataset, _, sample = demo
    other = tmp_path / "other"
    other.mkdir()
    manifest = json.loads((dataset / "manifest.json").read_text())
    (other / "manifest.json").write_text(json.dumps({**manifest, "dataset_id": "someone-else"}))
    edit_manifest(sample, dataset_path=str(other))
    page = TestClient(create_app(sample)).get("/result", params={"id": PITCH}).text
    assert 'data-evidence="unchecked"' in page
    assert "now holds dataset someone-else, not demo-larkspur, so it was not used" in page
    assert 'data-verification="verified"' not in page


def test_dataset_path_that_does_not_exist_is_explained(demo, tmp_path):
    _, _, sample = demo
    edit_manifest(sample, dataset_path=str(tmp_path / "moved"))
    page = TestClient(create_app(sample)).get("/").text
    assert 'data-evidence="unchecked"' in page and "which holds no dataset here" in page


def test_without_any_dataset_evidence_shows_not_checked(demo):
    _, _, sample = demo
    edit_manifest(sample, dataset_path=None)
    client = TestClient(create_app(sample))
    page = client.get("/result", params={"id": PITCH}).text
    assert 'data-verification="unchecked"' in page
    assert 'data-verification="verified"' not in page
    assert "Evidence not checked" in page and "run.json has no dataset_path" in page
    assert client.get("/document", params={"id": "doc_inv_2041"}).status_code == 404


def test_dataset_flag_for_another_dataset_is_not_used(demo):
    if not (REAL_DATASET / "manifest.json").is_file():
        pytest.skip("data/company is not in this checkout")
    _, _, sample = demo
    page = TestClient(create_app(sample, REAL_DATASET)).get("/").text
    assert 'data-evidence="unchecked"' in page
    assert "holds dataset copperline-fy2025-v1, but this run used demo-larkspur" in page


# -- the snapshot --------------------------------------------------------------


def test_snapshot_that_does_not_match_the_logged_fetch_is_not_used(demo):
    dataset, preflight, _ = demo
    ledger = preflight / c.SOURCES_SNAPSHOT / "ledger.jsonl"
    ledger.write_text(ledger.read_text().replace("310822", "318022"))
    assert any("ledger_entries" in p for p in c.validate_run(preflight))
    page = TestClient(create_app(preflight, dataset)).get("/").text
    assert 'data-snapshot="untrusted"' in page
    assert "ledger_entries in the snapshot is not what the run fetched" in page
    # Every source was local, so the dataset is still a fair basis, and is named.
    assert 'data-evidence="dataset"' in page and 'data-evidence="snapshot"' not in page


def test_live_run_with_a_snapshot_is_verified_against_it(demo):
    dataset, preflight, _ = demo
    edit_manifest(
        preflight,
        run_id="run-2026-02-02",
        dataset_id="larkspur",
        sources=[
            {"system": "qbo", "mode": "live", "detail": "sandbox", "synthetic": True},
            {"system": "bank", "mode": "local", "detail": "statements folder"},
        ],
    )
    client = TestClient(create_app(preflight, dataset))
    text = client.get("/").text
    assert re.findall(r'data-system="(\w+)" data-mode="(\w+)"', text) == [
        ("qbo", "live"),
        ("bank", "local"),
    ]
    assert text.count("data-synthetic-source") == 1 and 'data-synthetic="qbo"' in text
    assert "DEMO RUN" not in text
    assert 'data-evidence="snapshot"' in text and "live-unchecked" not in text
    # --dataset holds another id than the edited run, and is not needed anyway.
    assert "but this run used larkspur" in text


def test_live_run_without_snapshot_is_left_unchecked(demo):
    """The only place a LIVE badge appears: a non-demo manifest written by the test."""
    dataset, _, sample = demo
    edit_manifest(
        sample,
        run_id="run-2026-02-02",
        sources=[{"system": "qbo", "mode": "live", "detail": "sandbox"}],
    )
    client = TestClient(create_app(sample, dataset))
    text = client.get("/").text
    assert 'data-mode="live"' in text and "DEMO RUN" not in text
    assert "data-synthetic=" not in text and "data-synthetic-source" not in text
    assert 'data-evidence="live-unchecked"' in text
    assert "did not verify" not in text and "not verified" not in text.lower()
    detail = client.get("/result", params={"id": PITCH}).text
    assert detail.count('data-verification="live_unchecked"') == 3
    assert "Unchecked: live run" in detail
    assert 'data-verification="failed"' not in detail


def test_demo_is_honest_about_sources_and_labels_itself(demo):
    dataset, preflight, sample = demo
    for run_dir in (preflight, sample):
        run = c.load_run(run_dir)
        assert {s.mode for s in run.manifest.sources} == {"local"}
        calls = [e for e in run.log if e.action is c.LogAction.SOURCE_CALL]
        fingerprints = [e for e in calls if "call" in e.detail]
        searches = [e for e in calls if "call" not in e.detail]
        assert len(fingerprints) == 5 and searches
        assert all(e.detail["mode"] == "local" for e in searches)
        # Opened without --demo, the run still says it is a demo run on synthetic data.
        text = TestClient(create_app(run_dir, dataset)).get("/").text
        assert "DEMO RUN" in text and "data-synthetic=" in text
        assert 'data-mode="live"' not in text


def test_synthetic_data_without_demo_ids_is_not_called_a_demo(demo):
    dataset, preflight, _ = demo
    edit_manifest(preflight, run_id="run-1")
    text = TestClient(create_app(preflight, dataset)).get("/").text
    assert "DEMO RUN" not in text
    assert "Synthetic data." in text


# -- per-result sign-off -------------------------------------------------------


def test_signoff_pins_the_result_as_shown(client, demo):
    _, preflight, _ = demo
    before = len(c.read_log(preflight / c.LOG))
    response = sign(client, reviewer="Dana Reyes", note="Agreed, accrue in Dec.")
    assert response.status_code == 303 and "error=" not in response.headers["location"]
    events = c.read_log(preflight / c.LOG)
    assert len(events) == before + 1
    last = events[-1]
    assert last.action is c.LogAction.SIGNOFF and last.actor == "reviewer"
    assert last.actor_id == "Dana Reyes" and last.subject_ids == [PITCH]
    pitch = next(r for r in c.load_run(preflight).results if r.id == PITCH)
    assert last.detail == {
        "decision": "accepted",
        "comment": "Agreed, accrue in Dec.",
        "result_sha256": c.result_hash(pitch),
    }
    assert c.verify_log(preflight / c.LOG) == []
    page = client.get("/result", params={"id": PITCH}).text
    assert "Agreed, accrue in Dec." in page and 'data-signoff-state="pinned"' in page
    assert c.validate_run(preflight) == []


def test_signoff_refused_when_result_changed_since_shown(client, demo):
    _, preflight, _ = demo
    shown = form_values(client)
    edit_result(preflight, PITCH, summary="Rewritten while the reviewer was reading.")
    before = (preflight / c.LOG).read_text()
    response = sign(client, **shown)
    location = response.headers["location"]
    assert response.status_code == 303 and "error=" in location
    page = client.get(location).text
    assert CHANGED_SINCE_SHOWN.split(",")[0] in page
    assert (preflight / c.LOG).read_text() == before
    # Read again, it signs.
    assert "error=" not in sign(client).headers["location"]


def test_signoff_without_the_shown_hash_is_refused(client, demo):
    _, preflight, _ = demo
    before = (preflight / c.LOG).read_text()
    response = sign(client, result_sha256="")
    assert "error=" in response.headers["location"]
    assert (preflight / c.LOG).read_text() == before


def test_result_changed_after_signoff_is_shown_as_validate_run_reports_it(client, demo):
    _, preflight, _ = demo
    edit_result(preflight, SIGNED, summary="Quietly edited after the sign-off.")
    problems = [p for p in c.validate_run(preflight) if "signed off" in p]
    assert problems == [f"result {SIGNED}: changed after it was signed off"]
    listing = client.get("/").text
    assert 'data-signoff-problems="1"' in listing
    assert f"result {SIGNED}: changed after it was signed off" in listing
    assert 'data-signoff-state="changed"' in listing
    detail = client.get("/result", params={"id": SIGNED}).text
    assert 'data-signoff-state="changed"' in detail
    # Signing again clears it: only the latest sign-off counts.
    sign(client, result_id=SIGNED, **form_values(client, SIGNED))
    assert "data-signoff-problems" not in client.get("/").text
    assert not [p for p in c.validate_run(preflight) if "signed off" in p]


def test_signoff_without_hash_shows_as_unpinned(client, demo):
    _, preflight, _ = demo
    c.LogWriter(preflight / c.LOG, PREFLIGHT_RUN_ID).sign_off("Old screen", PITCH, "accepted")
    page = client.get("/result", params={"id": PITCH}).text
    assert 'data-signoff-state="unpinned"' in page and "not pinned" in page


def test_signoff_rejects_bad_input(client, demo):
    _, preflight, _ = demo
    before = (preflight / c.LOG).read_text()
    no_name = sign(client, reviewer=" ")
    assert no_name.status_code == 303 and "error=" in no_name.headers["location"]
    unknown = sign(client, result_id="nope#period", csrf=csrf(client))
    assert unknown.status_code == 404
    assert (preflight / c.LOG).read_text() == before


def test_broken_chain_is_shown_and_blocks_signoff(client, demo):
    _, preflight, _ = demo
    shown = form_values(client)
    log = preflight / c.LOG
    log.write_text(log.read_text().replace('"status": "fail"', '"status": "pass"', 1))
    assert c.verify_log(log)
    page = client.get("/log")
    assert 'data-chain="broken"' in page.text
    assert 'name="csrf"' not in client.get("/result", params={"id": PITCH}).text
    assert 'data-run-signoff-form="broken"' in client.get("/").text
    response = sign(client, **shown)
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert c.verify_log(log)  # nothing appended on top of the broken chain


def test_signoff_refused_while_run_in_progress(client, demo):
    _, preflight, _ = demo
    shown = form_values(client)
    hashes = run_form(client)
    edit_manifest(preflight, finished_at=None)
    before = (preflight / c.LOG).read_text()
    page = client.get("/result", params={"id": PITCH}).text
    assert 'data-signoff="in-progress"' in page and '<form method="post"' not in page
    listing = client.get("/").text
    assert 'data-run="in-progress"' in listing and 'data-run-signoff-form="in-progress"' in listing
    response = sign(client, **shown)
    assert response.status_code == 303 and "in%20progress" in response.headers["location"]
    response = sign_run(client, **hashes)
    assert response.status_code == 303 and "in%20progress" in response.headers["location"]
    assert (preflight / c.LOG).read_text() == before


def test_signoff_needs_csrf_token_and_same_origin(client, demo):
    _, preflight, _ = demo
    before = (preflight / c.LOG).read_text()
    assert sign(client, csrf="").status_code == 403
    assert sign(client, csrf="forged").status_code == 403
    assert sign_run(client, csrf="forged").status_code == 403
    shown = form_values(client)
    data = {"result_id": PITCH, "reviewer": "A", "decision": "accepted", **shown}
    cross = client.post(
        "/signoff", data=data, headers={"Origin": "http://evil.example"}, follow_redirects=False
    )
    assert cross.status_code == 403
    assert (preflight / c.LOG).read_text() == before
    same = client.post(
        "/signoff", data=data, headers={"Origin": "http://testserver"}, follow_redirects=False
    )
    assert same.status_code == 303 and "error=" not in same.headers["location"]


# -- run sign-off --------------------------------------------------------------


def test_run_signoff_pins_results_and_manifest(client, demo):
    _, preflight, _ = demo
    assert 'data-run-signoff-form="open"' in client.get("/").text
    response = sign_run(client, note="Ready for the auditor.")
    assert response.status_code == 303 and "error=" not in response.headers["location"]
    last = c.read_log(preflight / c.LOG)[-1]
    assert last.subject_ids == [PREFLIGHT_RUN_ID] and last.actor_id == "Dana Reyes"
    assert last.detail["scope"] == "run" and last.detail["comment"] == "Ready for the auditor."
    assert last.detail["results_sha256"] == c.sha256_file(preflight / c.RESULTS)
    assert last.detail["run_sha256"] == c.sha256_file(preflight / c.RUN_MANIFEST)
    assert c.validate_run(preflight) == []
    page = client.get("/").text
    assert 'data-run-signoff="ok"' in page and "Ready for the auditor." in page
    assert "whole run" in client.get("/log").text


def test_results_changed_after_run_signoff_are_shown_as_validate_run_reports_it(client, demo):
    _, preflight, _ = demo
    sign_run(client)
    edit_result(preflight, PITCH, summary="Edited after the run was signed off.")
    assert "results.jsonl: changed after the run was signed off" in c.validate_run(preflight)
    page = client.get("/").text
    assert 'data-run-signoff="problem"' in page
    assert "results.jsonl: changed after the run was signed off" in page
    assert 'data-signoff-problems="1"' in page


def test_run_signoff_refused_when_results_changed_since_shown(client, demo):
    _, preflight, _ = demo
    shown = run_form(client)
    edit_result(preflight, PITCH, summary="Changed while the reviewer was reading.")
    before = (preflight / c.LOG).read_text()
    response = sign_run(client, **shown)
    assert "error=" in response.headers["location"]
    assert "changed after the page was shown" in client.get(response.headers["location"]).text
    assert (preflight / c.LOG).read_text() == before


def test_run_signoff_refused_without_run_finished_event(client, demo):
    _, preflight, _ = demo
    drop_last_log_lines(preflight, 2)  # the demo sign-off and run_finished
    assert c.verify_log(preflight / c.LOG) == []
    assert 'data-run-signoff-form="in-progress"' in client.get("/").text
    before = (preflight / c.LOG).read_text()
    response = sign_run(client, **run_form(client) or {"csrf": csrf(client)})
    assert "run_finished" in client.get(response.headers["location"]).text
    assert (preflight / c.LOG).read_text() == before


def test_run_signed_off_before_it_finished_is_shown(client, demo):
    _, preflight, _ = demo
    drop_last_log_lines(preflight, 2)
    writer = c.LogWriter(preflight / c.LOG, PREFLIGHT_RUN_ID)
    writer.sign_off_run("Too early", "accepted")
    writer.append("system", c.LogAction.RUN_FINISHED)
    assert any("signed off before it finished" in p for p in c.validate_run(preflight))
    page = client.get("/").text
    assert 'data-run-signoff="problem"' in page
    assert "the run was signed off before it finished" in page


# -- the log -------------------------------------------------------------------


def test_result_changed_after_logging_is_flagged(client, demo):
    _, preflight, _ = demo
    edit_result(preflight, PITCH, status=c.CheckStatus.NEEDS_REVIEW)
    listing = client.get("/").text
    assert re.findall(r'data-result="([^"]+)" data-drift="(\w+)"', listing) == [(PITCH, "fail")]
    assert 'data-drift-count="1"' in listing
    detail = client.get("/result", params={"id": PITCH}).text
    assert 'data-drift="fail"' in detail and "Changed after it was logged" in detail
    assert "data-signoff-drift" in detail


def test_half_written_log_line_does_not_break_the_pages(client, demo):
    _, preflight, _ = demo
    shown = form_values(client)
    log = preflight / c.LOG
    events = len(c.read_log(log))
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f'{{"seq": {events + 1}, "ts": "2026-02-02T09:')
    for path in ("/", "/log", "/drafts"):
        page = client.get(path)
        assert page.status_code == 200
        assert "the step log could not be read fully" in page.text
    page = client.get("/log").text
    assert 'data-chain="intact"' in page and page.count("data-action=") == events
    assert 'data-validation="skipped"' in page
    before = log.read_text()
    assert "in%20progress" in sign(client, **shown).headers["location"]
    assert log.read_text() == before


def test_damaged_log_line_is_shown_and_blocks_signoff(client, demo):
    _, preflight, _ = demo
    shown = form_values(client)
    log = preflight / c.LOG
    events = len(c.read_log(log))
    with log.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    for path in ("/", "/log", "/result?id=ramp%3Abill%3Ab_1001%23period"):
        page = client.get(path)
        assert page.status_code == 200
        assert 'data-log="damaged"' in page.text and "repair-log" in page.text
    page = client.get("/log").text
    assert 'data-chain="damaged"' in page and page.count("data-action=") == events
    assert 'data-signoff="damaged"' in client.get("/result", params={"id": PITCH}).text
    before = log.read_text()
    response = sign(client, **shown)
    assert "damaged" in client.get(response.headers["location"]).text
    assert log.read_text() == before


def test_log_damaged_from_the_writer_is_shown_not_raised(client, demo):
    """A whole last event with no line ending reads fine, but LogWriter refuses to append."""
    _, preflight, _ = demo
    shown = form_values(client)
    log = preflight / c.LOG
    log.write_text(log.read_text().rstrip("\n"))
    before = log.read_text()
    response = sign(client, **shown)
    assert response.status_code == 303 and "error=" in response.headers["location"]
    page = client.get(response.headers["location"]).text
    assert "Sign-off refused" in page and "repair-log" in page
    assert log.read_text() == before
    response = sign_run(client)
    assert "repair-log" in client.get(response.headers["location"]).text
    assert log.read_text() == before


def test_log_page_shows_intact_chain_and_validation(client):
    page = client.get("/log")
    assert page.status_code == 200
    assert 'data-chain="intact"' in page.text
    assert 'data-action="run_started"' in page.text
    assert 'data-validation="0"' in page.text and "against the snapshot" in page.text


# -- other pages ---------------------------------------------------------------


def test_pages_refuse_framing(client):
    for path in ("/", "/log", "/result?id=ramp%3Abill%3Ab_1001%23period"):
        headers = client.get(path).headers
        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]


def test_drafts_are_labelled_and_cannot_be_sent(client):
    page = client.get("/drafts")
    assert page.status_code == 200
    assert "Draft · not sent" in page.text
    assert "Delta Air Lines" in page.text
    assert "<button" not in page.text and "<form" not in page.text


def test_sample_mode_packets(demo):
    dataset, _, sample = demo
    client = TestClient(create_app(sample, dataset, demo=True))
    page = client.get("/packets")
    assert page.status_code == 200
    assert page.text.count("data-packet=") == 3
    assert "not matched" in page.text and "Bayside Events" in page.text
    assert 'data-verification="failed"' not in page.text
    assert "Verified against dataset" in page.text
    drafts = client.get("/drafts")
    assert drafts.text.count("data-draft=") == 2


def test_document_view_and_file_come_from_the_snapshot(client, demo):
    _, preflight, _ = demo
    page = client.get(
        "/document",
        params={
            "id": "doc_inv_2041",
            "page": 1,
            "quote": "Services rendered: December 1 - December 31, 2025",
        },
    )
    assert page.status_code == 200
    assert "<mark>Services rendered: December 1 - December 31, 2025</mark>" in page.text
    assert "matches the file" in page.text
    file = client.get("/document/file", params={"id": "doc_inv_2041"})
    assert file.status_code == 200 and file.headers["content-type"].startswith("text/plain")
    assert "Invoice INV-2041" in file.text
    assert file.headers["content-security-policy"].startswith("sandbox")
    assert client.get("/document", params={"id": "nope"}).status_code == 404


def test_unknown_result_is_404(client):
    assert client.get("/result", params={"id": "nope"}).status_code == 404


def test_result_page_steps_through_exceptions_and_lists_sibling_checks(client):
    listing = client.get("/").text
    # Rows come exceptions first; within a row, cells are in check order.
    cells = re.findall(r'data-result="([^"]+)"[^>]*data-status="(\w+)"', listing)
    queue = [rid for rid, status in cells if status in ("fail", "needs_review")]
    assert PITCH in queue
    first = client.get("/result", params={"id": queue[0]}).text
    assert f"Exception 1 of {len(queue)}" in first and 'rel="prev"' not in first
    page = client.get("/result", params={"id": PITCH}).text
    position = queue.index(PITCH)
    assert f"Exception {position + 1} of {len(queue)}" in page
    if position + 1 < len(queue):
        assert f'href="/result?id={quote(queue[position + 1], safe="")}" rel="next"' in page
    # The checks on the same transaction, this one marked as the current page.
    siblings = re.findall(r'<a href="/result\?id=([^"]+)"( aria-current="page")?>', page)
    assert len(siblings) == 4
    assert [unquote(s) for s, current in siblings if current] == [PITCH]
    # A clean result is not in the queue; it offers the first exception instead.
    clean = next(rid for rid, status in cells if status == "pass")
    assert "First exception" in client.get("/result", params={"id": clean}).text


def test_pages_are_gzipped_for_large_runs(client):
    response = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"
    assert response.headers["x-frame-options"] == "DENY"


# -- the scored dataset on main ------------------------------------------------


@pytest.fixture
def real_run(tmp_path):
    """An empty run over data/company: no results, so nothing is claimed about it.

    No agent run exists yet. This only shows the screen loads the real dataset, opens
    its documents and labels it, through --dataset and through dataset_path.
    """
    if not (REAL_DATASET / "manifest.json").is_file():
        pytest.skip("data/company is not in this checkout")
    sources = c.LocalSources(REAL_DATASET)
    dataset = sources.dataset.manifest
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    c.write_json(
        run_dir / c.RUN_MANIFEST,
        c.RunManifest(
            run_id="ui-test-empty-run",
            mode="preflight",
            started_at="2026-11-10T00:00:00Z",
            finished_at="2026-11-10T00:01:00Z",
            period_start=dataset.fiscal_year_start,
            period_end=dataset.fiscal_year_end,
            dataset_id=dataset.dataset_id,
            dataset_path=str(REAL_DATASET),
            sources=sources.describe(),
            contract_version=c.CONTRACT_VERSION,
        ),
    )
    (run_dir / c.RESULTS).write_text("")
    log = c.LogWriter(run_dir / c.LOG, "ui-test-empty-run")
    log.append("system", c.LogAction.RUN_STARTED)
    c.log_source_fingerprints(log, sources)
    log.append("system", c.LogAction.RUN_FINISHED)
    return run_dir, sources


@pytest.mark.parametrize("given", [True, False], ids=["--dataset", "dataset_path"])
def test_real_dataset_loads(real_run, given):
    run_dir, sources = real_run
    client = TestClient(create_app(run_dir, REAL_DATASET if given else None))
    page = client.get("/").text
    assert 'data-evidence="dataset"' in page and "copperline-fy2025-v1" in page
    assert "Synthetic data." in page and "DEMO RUN" not in page
    log = client.get("/log").text
    assert 'data-validation="0"' in log and "against the dataset" in log
    document = next(d for d in sources.documents() if d.media_type == "application/pdf")
    view = client.get("/document", params={"id": document.id})
    assert view.status_code == 200 and "matches the file" in view.text
    file = client.get("/document/file", params={"id": document.id})
    assert file.headers["content-type"] == "application/pdf"
