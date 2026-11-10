"""A local stand-in for Ramp's MCP server. It is not Ramp.

It serves a contract dataset folder over MCP (streamable HTTP) under Ramp's real
tool names and arguments, in the response shapes captured from Ramp's sandbox MCP
server on 2026-11-10 (connectors/tests/fixtures/ramp_sandbox_capture.json), so the
whole connector path runs for real offline: tool calls, paging, mapping, file
download, content check, sha256, cache. Bills, bill invoices and receipts were not
in the capture (the sandbox has none); those follow Ramp's Developer API shapes and
are unconfirmed, like the connector's reading of them.

It names itself `STANDIN_SERVER_NAME` in `initialize`, and `RampSources.describe()`
then reports Ramp as `local`, never `live`.

- The payloads are the inverse of `ramp.py`'s mapping. A Ramp field with no source
  in the contract record is left out, never made up. Approvals are not served:
  Ramp's MCP server exposes none for a transaction.
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
from decimal import Decimal
from pathlib import Path
from typing import Any

import uvicorn
from auditorium_contracts import (
    Dataset,
    Document,
    DocumentType,
    Person,
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


def _utc_midnight(value: Any) -> str | None:
    """A contract date as the UTC datetime Ramp writes card times in."""
    return f"{value.isoformat()}T00:00:00+00:00" if value else None


def _decimal(cents: int) -> str:
    """Cents as Ramp's `amount_value`: a decimal string in major units ("431.41")."""
    return str(Decimal(cents).scaleb(-2))


def _gl_selection(category: str | None) -> list[dict[str, Any]] | None:
    # The Developer API's selection shape; unconfirmed on the MCP server.
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


def _page(items: list[Any], cursor: str | None, size: int) -> tuple[list[Any], str | None]:
    """One page of `items` and the cursor of the next, None after the last."""
    start = int(cursor) if cursor else 0
    end = start + size
    return items[start:end], (str(end) if end < len(items) else None)


class RampPayloadBuilder:
    """A dataset's Ramp records in the shapes of Ramp's MCP server."""

    def __init__(self, dataset: Dataset, base_url: str):
        self.dataset = dataset
        self.base_url = base_url.rstrip("/")
        self.documents = documents = {d.id: d for d in dataset.documents}
        self.people = {p.id: p for p in dataset.people}
        self.cards = [t for t in dataset.transactions if t.kind is TransactionKind.CARD_EXPENSE]
        self.bills = [t for t in dataset.transactions if t.kind is TransactionKind.VENDOR_BILL]
        # (record_type, Ramp id) -> the dataset document served there.
        self.files: dict[tuple[str, str], Document] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        for card in self.cards:
            for document_id in card.document_ids:
                document = documents[document_id]
                key = ("receipt", document.source.record_id)
                self.files[key] = document
                self.receipts[key[1]] = {"id": key[1], "receipt_url": self.url(*key)}
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

    def transaction(self, card: SpendTransaction, submitted_items: bool) -> dict[str, Any]:
        """A `ramp_get_transactions` record. Receipts and accounting categories only
        when `submitted_items` was asked for, as Ramp's tool describes."""
        spender = self.people.get(card.spender_id or "")
        fields = {
            "transaction_uuid": card.source.record_id,
            "spent_by_user": spender.name if spender else None,
            "spent_by_user_uuid": _ramp_user(card.spender_id) if card.spender_id else None,
            "amount_value": _decimal(card.amount_cents),
            "amount_currency": "USD",
            "transaction_time": _utc_midnight(card.transaction_date),
            "settlement_date": _day(card.paid_date),
            "accounting_date": _utc_midnight(card.accounting_date),
            "merchant_name": card.vendor_name,
            "reason_or_justification": card.memo,
        }
        if submitted_items:
            fields["receipt_uuids"] = [
                self.documents[d].source.record_id for d in card.document_ids
            ]
            fields["accounting_categories"] = _gl_selection(card.category)
        return _present(fields)

    def bill(self, bill: SpendTransaction) -> dict[str, Any]:
        """A bill in the Developer API's shape (unconfirmed on the MCP server)."""
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
            }
        )

    def user(self, person: Person) -> dict[str, Any]:
        # The contract keeps one display name. Ramp splits first and last; rather than
        # guess the split, the whole name goes in first_name. Ramp's `role` is a
        # permission level, not the contract's job role, so it is left out.
        return _present(
            {
                "id": _ramp_user(person.id),
                "first_name": person.name,
                "email": person.email,
                "department_name": person.department,
            }
        )

    def org_chart(self, ramp_user: str) -> dict[str, Any]:
        """`ramp_get_org_chart` for one user: the user and their direct reports.
        `reporting_chain` is left out; its order is unconfirmed and nothing reads it."""
        person = self.people.get(person_id(ramp_user))
        if person is None:
            raise ValueError(f"no user {ramp_user!r}")
        reports = [p for p in self.people.values() if p.manager_id == person.id]
        return {"user": self.user(person), "direct_reports": [self.user(p) for p in reports]}


def build_server(dataset: Dataset, base_url: str) -> MCPServer:
    payloads = RampPayloadBuilder(dataset, base_url)
    server = MCPServer(
        name=STANDIN_SERVER_NAME,
        title="Auditorium Ramp stand-in (not Ramp)",
        description=f"serving dataset {dataset.manifest.dataset_id}",
        instructions="A local stand-in for Ramp's MCP server, serving a test dataset. Not Ramp.",
    )
    tools = {key: call.name for key, call in DEFAULT_TOOLS.items()}

    # `rationale` has no default, as on Ramp, so a call without one fails.

    @server.tool(name=tools["transactions"])
    def get_transactions(
        rationale: str,
        transactions_to_retrieve: str,
        state: str | None = None,
        page_size: int = 50,
        next_page_cursor: str | None = None,
        include_count: bool = True,
        details_to_include_in_response: list[str] | None = None,
    ) -> dict[str, Any]:
        """Card transactions in the dataset, a page at a time."""
        if transactions_to_retrieve != ALL_TRANSACTIONS:
            raise ValueError(f"only transactions_to_retrieve={ALL_TRANSACTIONS!r} is served")
        if state not in (None, "all"):
            raise ValueError("the dataset does not say which state a transaction is in")
        submitted = "submitted_items" in (details_to_include_in_response or ())
        records = [payloads.transaction(t, submitted) for t in payloads.cards]
        page, cursor = _page(records, next_page_cursor, page_size)
        answer = {"transactions": page, "next_page_cursor": cursor}
        return {**answer, "total_count": len(records)} if include_count else answer

    @server.tool(name=tools["bills"])
    def list_bills(
        rationale: str,
        limit: int = 10,
        page_cursor: str | None = None,
        include_paid: bool = True,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Vendor bills in the dataset, a page at a time."""
        records = [payloads.bill(b) for b in payloads.bills if include_paid or not b.paid_date]
        page, cursor = _page(records, page_cursor, limit)
        return {"bills": page, "total_found": len(records), "next_page_cursor": cursor}

    @server.tool(name=tools["bill_invoices"])
    def get_bill_invoices(rationale: str, bill_id: str) -> dict[str, Any]:
        """A bill's invoice files, with download URLs on this server."""
        if bill_id not in payloads.attachments:
            raise ValueError(f"no bill {bill_id!r}")
        return {"attachments": payloads.attachments[bill_id]}

    @server.tool(name=tools["receipts"])
    def get_receipt_details(rationale: str, receipt_uuids: list[str]) -> dict[str, Any]:
        """The receipts asked for, with download URLs on this server."""
        return {"receipts": [payloads.receipts[r] for r in receipt_uuids if r in payloads.receipts]}

    @server.tool(name=tools["users"])
    def list_users(
        rationale: str,
        page_size: int = 20,
        page_cursor: str | None = None,
        include_agents: bool = False,
    ) -> dict[str, Any]:
        """The dataset's people. Like Ramp, the cursor is the last user id of a page."""
        users = [payloads.user(p) for p in payloads.people.values()]
        ids = [u["id"] for u in users]
        start = ids.index(page_cursor) + 1 if page_cursor else 0
        page = users[start : start + page_size]
        more = start + page_size < len(users)
        return {"users": page, "next_page": page[-1]["id"] if more else None}

    @server.tool(name=tools["org_chart"])
    def get_org_chart(rationale: str, user_id: str | None = None) -> dict[str, Any]:
        """A user's direct reports. There is no signed-in user, so `user_id` is needed."""
        if not user_id:
            raise ValueError("the stand-in has no current user; pass user_id")
        return payloads.org_chart(user_id)

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
