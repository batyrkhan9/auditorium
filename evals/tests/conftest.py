"""A tiny made-up dataset for the harness tests. Nothing here comes from sealed/."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

RECEIPT = "Test Diner\nReceipt\nTotal charged: $120.00\n"


def write_dataset(root: Path) -> Path:
    (root / "documents").mkdir(parents=True)
    (root / "documents" / "r1.txt").write_text(RECEIPT, "utf-8")
    sha = hashlib.sha256(RECEIPT.encode()).hexdigest()

    def jsonl(name: str, rows: list[dict]) -> None:
        (root / name).write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")

    (root / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "evals-fixture",
                "company_name": "Fixture Co",
                "contract_version": "0.2.0",
                "fiscal_year_start": "2025-01-01",
                "fiscal_year_end": "2025-12-31",
                "coverage_end": "2026-02-28",
                "synthetic": True,
            }
        ),
        "utf-8",
    )
    txns = []
    for n, amount in ((1, 12000), (2, 3000), (3, 900000)):
        txns.append(
            {
                "id": f"ramp:card:t{n}",
                "kind": "card_expense",
                "source": {"system": "ramp", "record_type": "transaction", "record_id": f"t{n}"},
                "vendor_name": f"Vendor {n}",
                "amount_cents": amount,
                "transaction_date": "2025-06-01",
                "spender_id": "p1",
                "document_ids": ["doc_r1"] if n == 1 else [],
                "approvals": [],
            }
        )
    jsonl("transactions.jsonl", txns)
    jsonl(
        "ledger.jsonl",
        [
            {
                "id": "qbo:Purchase:1",
                "source": {"system": "qbo", "record_type": "Purchase", "record_id": "1"},
                "entry_type": "expense",
                "txn_date": "2025-06-01",
                "vendor_name": "Vendor 1",
                "total_cents": 12000,
                "lines": [],
            }
        ],
    )
    jsonl(
        "documents.jsonl",
        [
            {
                "id": "doc_r1",
                "type": "receipt",
                "source": {"system": "ramp", "record_type": "transaction", "record_id": "t1"},
                "origin": "external",
                "path": "documents/r1.txt",
                "media_type": "text/plain",
                "sha256": sha,
            }
        ],
    )
    jsonl("people.jsonl", [{"id": "p1", "name": "Pat Example", "role": "engineer"}])
    (root / "policy.json").write_text(
        json.dumps(
            {
                "id": "policy",
                "effective_from": "2025-01-01",
                "receipt_required_from_cents": 7500,
                "approval_rules": [],
            }
        ),
        "utf-8",
    )
    return root


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    return write_dataset(tmp_path / "dataset")
