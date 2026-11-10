"""A local stand-in for Ramp's MCP server. It is not Ramp.

Nobody on the team has a Ramp account, so we cannot reach Ramp's sandbox MCP
server. This serves a contract dataset folder over MCP (streamable HTTP) in the
payload shapes `ramp.py` reads, which follow Ramp's Developer API spec
(https://docs.ramp.com/openapi/developer-api.json), so the whole connector path
runs for real: tool calls, mapping, file download, content check, sha256, cache.

It names itself `STANDIN_SERVER_NAME` in `initialize`, and `RampSources.describe()`
then reports Ramp as `local`, never `live`.

- The payloads are the inverse of `ramp.py`'s mapping. A Ramp field with no source
  in the contract record is left out, never made up.
- Any bearer token is accepted; there is no auth. It listens on 127.0.0.1 by default.
- Files are served from the same server at `/files/<record_type>/<record_id>`.

    uv run python -m auditorium_connectors.ramp_standin <dataset_dir> [--port 8765] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from auditorium_contracts import (
    Dataset,
    Document,
    DocumentType,
    SpendTransaction,
    TransactionKind,
    load_dataset,
)
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

from .ramp import DEFAULT_TOOLS, STANDIN_SERVER_NAME, person_id

ALL_TRANSACTIONS = DEFAULT_TOOLS["transactions"].arguments["transactions_to_retrieve"]
USER_PREFIX = person_id("")


def _ramp_user(contract_person_id: str) -> str:
    """The Ramp user id behind a contract Person.id (`ramp.person_id` reversed)."""
    return contract_person_id.removeprefix(USER_PREFIX)


def _present(fields: dict[str, Any]) -> dict[str, Any]:
    """`fields` without the ones the contract record has no value for."""
    return {key: value for key, value in fields.items() if value not in (None, [], {})}


def _day(value: Any) -> str | None:
    return value.isoformat() if value else None


def _gl_selection(category: str | None) -> list[dict[str, Any]] | None:
    if not category:
        return None
    return [{"name": category, "category_info": {"type": "GL_ACCOUNT"}}]


def _attachment_type(document: Document) -> str | None:
    """The Ramp attachment type that maps back to the same document type and origin.
    Other combinations get none rather than a type the dataset doesn't state."""
    if document.type is DocumentType.INVOICE:
        return "INVOICE"
    if document.type is DocumentType.OTHER and document.origin == "internal":
        return "FILE"
    return None


class RampPayloadBuilder:
    """A dataset's Ramp records in the shapes of Ramp's Developer API."""

    def __init__(self, dataset: Dataset, base_url: str):
        self.dataset = dataset
        self.base_url = base_url.rstrip("/")
        self.documents = documents = {d.id: d for d in dataset.documents}
        self.cards = [t for t in dataset.transactions if t.kind is TransactionKind.CARD_EXPENSE]
        self.bills = [t for t in dataset.transactions if t.kind is TransactionKind.VENDOR_BILL]
        # (record_type, Ramp id) -> the dataset document served there.
        self.files: dict[tuple[str, str], Document] = {}
        self.receipts: list[dict[str, Any]] = []
        for card in self.cards:
            for document_id in card.document_ids:
                document = documents[document_id]
                key = ("receipt", document.source.record_id)
                if key not in self.files:
                    self.files[key] = document
                    self.receipts.append(
                        {
                            "id": document.source.record_id,
                            "receipt_url": self.url(*key),
                            "transaction_id": card.source.record_id,
                        }
                    )
        self.attachments: dict[str, list[dict[str, Any]]] = {}
        for bill in self.bills:
            attachments = []
            for document_id in bill.document_ids:
                document = documents[document_id]
                key = ("bill_attachment", document.source.record_id)
                self.files[key] = document
                attachments.append(
                    _present(
                        {
                            "id": document.source.record_id,
                            "attachment_type": _attachment_type(document),
                            "url": self.url(*key),
                        }
                    )
                )
            self.attachments[bill.source.record_id] = attachments

    def url(self, record_type: str, record_id: str) -> str:
        return f"{self.base_url}/files/{record_type}/{record_id}"

    def transaction(self, card: SpendTransaction) -> dict[str, Any]:
        return _present(
            {
                "id": card.source.record_id,
                "merchant_name": card.vendor_name,
                "user_transaction_time": _day(card.transaction_date),
                "accounting_date": _day(card.accounting_date),
                "settlement_date": _day(card.paid_date),
                "entity_amount": {"currency": "USD", "value": card.amount_cents},
                "memo": card.memo,
                "accounting_field_selections": _gl_selection(card.category),
                "card_holder": _present(
                    {
                        "user_id": _ramp_user(card.spender_id) if card.spender_id else None,
                        "department_name": card.department,
                    }
                ),
                "receipts": [self.documents[d].source.record_id for d in card.document_ids],
            }
        )

    def bill(self, bill: SpendTransaction) -> dict[str, Any]:
        return _present(
            {
                "id": bill.source.record_id,
                "vendor": {"name": bill.vendor_name},
                "amount": {
                    "amount": bill.amount_cents,
                    "currency_code": "USD",
                    "minor_unit_conversion_rate": 100,
                },
                "issued_at": _day(bill.transaction_date),
                "accounting_date": _day(bill.accounting_date),
                "due_at": _day(bill.due_date),
                "paid_at": _day(bill.paid_date),
                "invoice_number": bill.invoice_number,
                "memo": bill.memo,
                "accounting_field_selections": _gl_selection(bill.category),
                "bill_owner": {"id": _ramp_user(bill.spender_id)} if bill.spender_id else None,
                "attachments": self.attachments[bill.source.record_id],
            }
        )

    def users(self) -> list[dict[str, Any]]:
        # The contract keeps one display name. Ramp splits first and last; rather than
        # guess the split, the whole name goes in first_name. Ramp's `role` is a
        # permission level, not the contract's job role, so it is left out.
        return [
            _present(
                {
                    "id": _ramp_user(person.id),
                    "first_name": person.name,
                    "email": person.email,
                    "department_id": person.department,
                    "manager_id": _ramp_user(person.manager_id) if person.manager_id else None,
                }
            )
            for person in self.dataset.people
        ]

    def departments(self) -> list[dict[str, Any]]:
        # The contract has department names only, so the name is also the id.
        names = sorted({p.department for p in self.dataset.people if p.department})
        return [{"id": name, "name": name} for name in names]

    def approvals(self, entity_type: str, entity_ids: list[str]) -> list[dict[str, Any]]:
        """One chain per requested id the dataset holds. Unknown ids are left out, which
        the connector refuses rather than read as "no approvals"."""
        pool = {"TRANSACTION": self.cards, "BILL": self.bills}.get(entity_type.upper())
        if pool is None:
            raise ValueError(f"entity_type must be TRANSACTION or BILL, not {entity_type!r}")
        by_id = {t.source.record_id: t for t in pool}
        return [
            {
                "entity_id": entity_id,
                "entity_type": entity_type.lower(),
                "approval_history": [
                    {
                        "user_id": _ramp_user(a.approver_id),
                        "step_status": a.decision.upper(),
                        "action_at": a.decided_at.isoformat(),
                    }
                    for a in by_id[entity_id].approvals
                ],
            }
            for entity_id in entity_ids
            if entity_id in by_id
        ]


def build_server(dataset: Dataset, base_url: str) -> MCPServer:
    payloads = RampPayloadBuilder(dataset, base_url)
    server = MCPServer(
        name=STANDIN_SERVER_NAME,
        title="Auditorium Ramp stand-in (not Ramp)",
        description=f"serving dataset {dataset.manifest.dataset_id}",
        instructions="A local stand-in for Ramp's MCP server, serving a test dataset. Not Ramp.",
    )
    tools = {key: call.name for key, call in DEFAULT_TOOLS.items()}

    @server.tool(name=tools["transactions"])
    def get_transactions(transactions_to_retrieve: str = ALL_TRANSACTIONS) -> dict[str, Any]:
        """All card transactions in the dataset."""
        if transactions_to_retrieve != ALL_TRANSACTIONS:
            raise ValueError(f"only transactions_to_retrieve={ALL_TRANSACTIONS!r} is served")
        return {"data": [payloads.transaction(t) for t in payloads.cards]}

    @server.tool(name=tools["bills"])
    def get_bills() -> dict[str, Any]:
        """All vendor bills in the dataset."""
        return {"data": [payloads.bill(b) for b in payloads.bills]}

    @server.tool(name=tools["receipts"])
    def get_receipts() -> dict[str, Any]:
        """Receipts attached to card transactions, with download URLs on this server."""
        return {"data": payloads.receipts}

    @server.tool(name=tools["users"])
    def get_all_reduced_users() -> dict[str, Any]:
        """The dataset's people."""
        return {"data": payloads.users()}

    @server.tool(name=tools["departments"])
    def get_departments() -> dict[str, Any]:
        """Department names the dataset's people belong to."""
        return {"data": payloads.departments()}

    @server.tool(name=tools["approvals"])
    def get_approvals(entity_type: str, entity_ids: list[str]) -> dict[str, Any]:
        """Approval chains for card transactions (TRANSACTION) or bills (BILL)."""
        return {"data": payloads.approvals(entity_type, entity_ids)}

    @server.custom_route("/files/{record_type}/{record_id}", methods=["GET"])
    async def file(request: Request) -> Response:
        key = (request.path_params["record_type"], request.path_params["record_id"])
        document = payloads.files.get(key)
        if document is None:
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(dataset.root / document.path, media_type=document.media_type)

    return server


def build_app(dataset_dir: Path | str, base_url: str, host: str = "127.0.0.1") -> Starlette:
    server = build_server(load_dataset(dataset_dir), base_url)
    return server.streamable_http_app(host=host)


def _bind(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.bind((host, port))
    return sock


def _base_url(host: str, port: int) -> str:
    return f"http://{f'[{host}]' if ':' in host else host}:{port}"


@contextmanager
def serving(dataset_dir: Path | str, host: str = "127.0.0.1", port: int = 0) -> Iterator[str]:
    """Runs the stand-in in a background thread and yields its MCP URL. Port 0 picks
    a free one."""
    sock = _bind(host, port)
    base = _base_url(host, sock.getsockname()[1])
    app = build_app(dataset_dir, base, host)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the Ramp stand-in did not start")
            time.sleep(0.02)
        yield f"{base}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m auditorium_connectors.ramp_standin",
        description="Serve a contract dataset as a stand-in for Ramp's MCP server. Not Ramp.",
    )
    parser.add_argument("dataset_dir", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (this machine only)")
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset_dir)
    sock = _bind(args.host, args.port)
    base = _base_url(args.host, sock.getsockname()[1])
    app = build_server(dataset, base).streamable_http_app(host=args.host)
    print(
        f"Ramp stand-in (NOT Ramp) serving dataset {dataset.manifest.dataset_id} at {base}/mcp\n"
        "Any bearer token is accepted; there is no authentication.\n"
        f"Connect with RAMP_MCP_URL={base}/mcp RAMP_MCP_TOKEN=standin. "
        "describe() will report Ramp as local.",
        flush=True,
    )
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Warning: listening on {args.host}, not only this machine, with no authentication.",
            flush=True,
        )
    uvicorn.Server(uvicorn.Config(app, log_level="info")).run(sockets=[sock])


if __name__ == "__main__":
    main()
