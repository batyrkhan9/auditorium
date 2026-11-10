import json
import re

import auditorium_contracts as c
import pytest
from auditorium_ui.app import create_app
from auditorium_ui.demo import PREFLIGHT_RUN_ID, build_demo
from starlette.testclient import TestClient

PITCH = "ramp:bill:b_1001#period"


@pytest.fixture
def demo(tmp_path):
    dataset, preflight, sample = build_demo(tmp_path)
    return dataset, preflight, sample


@pytest.fixture
def client(demo):
    dataset, preflight, _ = demo
    return TestClient(create_app(preflight, dataset, demo=True))


def test_demo_fixture_is_contract_valid(demo):
    dataset, preflight, sample = demo
    sources = c.LocalSources(dataset)
    assert c.validate_dataset(dataset) == []
    assert c.validate_run(preflight, sources) == []
    assert c.validate_run(sample, sources) == []


def test_results_page_shows_counts_and_demo_label(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "DEMO DATA" in page.text
    assert PREFLIGHT_RUN_ID in page.text
    assert page.text.count("data-result=") == 20
    assert "Northwind Advisory LLC" in page.text and "$12,000.00" in page.text
    assert "not verified" not in page.text.lower()


def test_filters(client):
    page = client.get("/", params={"status": "fail"})
    assert page.text.count("data-result=") == 3
    page = client.get("/", params={"status": "fail", "check": "period"})
    assert re.findall(r'data-result="([^"]+)"', page.text) == [PITCH]


def test_pitch_example_detail_shows_verified_evidence(client):
    page = client.get("/result", params={"id": PITCH})
    assert page.status_code == 200
    assert "period_wrong" in page.text
    assert "Services rendered: December 1 - December 31, 2025" in page.text
    assert "2026-01" in page.text and "2025-12" in page.text
    assert page.text.count('data-verification="verified"') == 3
    assert 'data-kind="search"' in page.text and "<strong>nothing</strong>" in page.text


def test_unverifiable_evidence_is_flagged(demo):
    dataset, preflight, _ = demo
    results = c.read_jsonl(preflight / c.RESULTS, c.CheckResult)
    bad = results[3].model_copy(
        update={
            "evidence": [
                c.DocumentEvidence(
                    document_id="doc_inv_2041", page=1, quote="Services rendered: January 2026"
                )
            ]
        }
    )
    c.write_jsonl(preflight / c.RESULTS, [*results[:3], bad, *results[4:]])
    client = TestClient(create_app(preflight, dataset))
    page = client.get("/result", params={"id": PITCH})
    assert 'data-verification="failed"' in page.text
    assert "quote not found" in page.text
    assert "1 evidence item(s) did not verify" in client.get("/").text


def test_without_dataset_evidence_shows_not_checked(demo):
    _, preflight, _ = demo
    client = TestClient(create_app(preflight))
    page = client.get("/result", params={"id": PITCH})
    assert 'data-verification="unchecked"' in page.text
    assert 'data-verification="verified"' not in page.text
    assert "Evidence not checked" in page.text


def test_badges_come_from_the_manifest(demo):
    dataset, preflight, _ = demo
    manifest = json.loads((preflight / c.RUN_MANIFEST).read_text())
    manifest["sources"] = [
        {"system": "qbo", "mode": "live", "detail": "sandbox"},
        {"system": "bank", "mode": "local", "detail": "statements folder"},
    ]
    (preflight / c.RUN_MANIFEST).write_text(json.dumps(manifest))
    text = TestClient(create_app(preflight, dataset)).get("/").text
    assert re.findall(r'data-system="(\w+)" data-mode="(\w+)"', text) == [
        ("qbo", "live"),
        ("bank", "local"),
    ]
    assert "DEMO DATA" not in text


def test_signoff_appends_a_chained_event(client, demo):
    _, preflight, _ = demo
    before = len(c.read_log(preflight / c.LOG))
    response = client.post(
        "/signoff",
        data={
            "result_id": PITCH,
            "reviewer": "Dana Reyes",
            "decision": "accepted",
            "note": "Agreed, accrue in Dec.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    events = c.read_log(preflight / c.LOG)
    assert len(events) == before + 1
    last = events[-1]
    assert last.action is c.LogAction.SIGNOFF and last.actor == "reviewer"
    assert last.actor_id == "Dana Reyes" and last.subject_ids == [PITCH]
    assert last.detail == {"decision": "accepted", "comment": "Agreed, accrue in Dec."}
    assert c.verify_log(preflight / c.LOG) == []
    page = client.get("/result", params={"id": PITCH})
    assert "Agreed, accrue in Dec." in page.text


def test_signoff_rejects_bad_input(client, demo):
    _, preflight, _ = demo
    before = (preflight / c.LOG).read_text()
    no_name = client.post(
        "/signoff",
        data={"result_id": PITCH, "reviewer": " ", "decision": "accepted"},
        follow_redirects=False,
    )
    assert no_name.status_code == 303 and "error=" in no_name.headers["location"]
    unknown = client.post(
        "/signoff", data={"result_id": "nope#period", "reviewer": "A", "decision": "accepted"}
    )
    assert unknown.status_code == 404
    assert (preflight / c.LOG).read_text() == before


def test_broken_chain_is_shown_and_blocks_signoff(client, demo):
    _, preflight, _ = demo
    log = preflight / c.LOG
    log.write_text(log.read_text().replace('"status": "fail"', '"status": "pass"', 1))
    assert c.verify_log(log)
    page = client.get("/log")
    assert 'data-chain="broken"' in page.text
    response = client.post(
        "/signoff",
        data={"result_id": PITCH, "reviewer": "A", "decision": "accepted"},
        follow_redirects=False,
    )
    assert "error=" in response.headers["location"]
    assert c.verify_log(log)  # nothing appended on top of the broken chain


def test_log_page_shows_intact_chain(client):
    page = client.get("/log")
    assert page.status_code == 200
    assert 'data-chain="intact"' in page.text
    assert 'data-action="run_started"' in page.text


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
    drafts = client.get("/drafts")
    assert drafts.text.count("data-draft=") == 2


def test_document_view_and_file(client):
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
    assert client.get("/document", params={"id": "nope"}).status_code == 404


def test_unknown_result_is_404(client):
    assert client.get("/result", params={"id": "nope"}).status_code == 404
