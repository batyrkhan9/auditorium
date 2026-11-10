"""The run viewer: one run folder, its results, the evidence behind them, the log, sign-off.

Reads the run folder on every request, so sign-offs and a run that is still being
written show up on reload. Evidence is checked with `verify_evidence` when a
dataset folder is given; without one every item shows as not checked. A run that
used any live source is not re-checked against the local dataset, because live
ids need not exist there.
"""

from __future__ import annotations

import json
import re
import secrets
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import auditorium_contracts as c
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .demo import DEMO_PREFIX

CHECKS = [check.value for check in c.CheckName]
STATUSES = [status.value for status in c.CheckStatus]
LIVE_UNCHECKED = "evidence not re-verified against live sources"


@dataclass(frozen=True)
class Verification:
    state: str  # "verified", "failed", "unchecked" or "live_unchecked"
    reason: str | None = None


def is_demo(manifest: c.RunManifest) -> bool:
    """The demo marks its own run and dataset ids, so it is labelled even without --demo."""
    return manifest.run_id.startswith(DEMO_PREFIX) and (manifest.dataset_id or "").startswith(
        DEMO_PREFIX
    )


def _read_log(path: Path) -> tuple[list[str], bool]:
    """The log's complete lines, and whether a half-written last line was left out.

    Only a final line with no newline after it (the agent is mid-write) is
    tolerated. Any other line that does not parse still raises when parsed.
    """
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and not text.endswith("\n"):
        try:
            c.LogEvent.model_validate_json(lines[-1])
        except ValueError:
            return lines[:-1], True
    return lines, False


def _verify_lines(lines: list[str]) -> list[str]:
    """`verify_log` on exactly the lines shown, not on a file that may have grown since."""
    with tempfile.TemporaryDirectory() as folder:
        snapshot = Path(folder) / c.LOG
        snapshot.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return c.verify_log(snapshot)


def _load_run(root: Path) -> tuple[c.Run, list[str], bool]:
    """Like `load_run` plus `verify_log`, but a half-written last log line does not stop
    the page. Returns the run, the chain's problems and whether a line was left out."""
    lines, partial = _read_log(root / c.LOG)
    log = [c.LogEvent.model_validate_json(line) for line in lines]

    def optional(name: str, model) -> tuple:
        return tuple(c.read_jsonl(root / name, model)) if (root / name).is_file() else ()

    scorecard = root / c.SCORECARD
    run = c.Run(
        root=root,
        manifest=c.RunManifest.model_validate_json((root / c.RUN_MANIFEST).read_text("utf-8")),
        results=tuple(c.read_jsonl(root / c.RESULTS, c.CheckResult)),
        packets=optional(c.PACKETS, c.EvidencePacket),
        drafts=optional(c.DRAFTS, c.FollowUpDraft),
        log=tuple(log),
        scorecard=c.Scorecard.model_validate_json(scorecard.read_text("utf-8"))
        if scorecard.is_file()
        else None,
    )
    return run, _verify_lines(lines), partial


def _same_origin(request: Request) -> bool:
    """A browser sends Origin (or at least Referer) on a form POST; it must be this server."""
    host = request.headers.get("host", "")
    claimed = request.headers.get("origin") or request.headers.get("referer")
    if claimed is None:
        return True  # not a browser; the CSRF token still has to match
    return bool(host) and urlsplit(claimed).netloc == host


class SecurityHeaders:
    """Refuse framing on every response, so a sign-off cannot be clickjacked."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _money(cents: int | None) -> str:
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def _highlight(text: str, wanted: str | None) -> list[tuple[str, bool]]:
    """Split text into (chunk, is_quote) parts, matching the quote loosely on whitespace and case."""
    words = (wanted or "").split()
    if not words:
        return [(text, False)]
    pattern = re.compile(r"\s+".join(re.escape(word) for word in words), re.IGNORECASE)
    parts: list[tuple[str, bool]] = []
    position = 0
    for match in pattern.finditer(text):
        parts.append((text[position : match.start()], False))
        parts.append((match.group(0), True))
        position = match.end()
    parts.append((text[position:], False))
    return parts


class Viewer:
    def __init__(
        self, run_dir: Path | str, dataset_dir: Path | str | None = None, demo: bool = False
    ):
        self.run_dir = Path(run_dir)
        self.dataset_dir = Path(dataset_dir) if dataset_dir else None
        self.demo = demo
        self.csrf = secrets.token_urlsafe(32)
        self.sources = c.LocalSources(self.dataset_dir) if self.dataset_dir else None
        self.index = c.SourceIndex(self.sources) if self.sources else None
        self.env = Environment(
            loader=PackageLoader("auditorium_ui", "templates"),
            autoescape=select_autoescape(["html"]),
        )
        self.env.filters["money"] = _money
        self.env.filters["q"] = lambda value: quote(str(value), safe="")
        self.env.filters["tojson_compact"] = lambda value: json.dumps(
            value, ensure_ascii=False, sort_keys=True
        )

    # -- reading ---------------------------------------------------------------

    def verify(self, evidence, live: bool = False) -> Verification:
        if live:
            return Verification("live_unchecked", LIVE_UNCHECKED)
        if self.index is None:
            return Verification("unchecked", "no dataset given, so this was not checked")
        try:
            problem = c.verify_evidence(evidence, self.index)
        except Exception as error:  # a broken source file must not hide the item
            problem = f"could not check: {error}"
        return Verification("failed", problem) if problem else Verification("verified")

    def context(self) -> dict:
        run, chain, log_partial = _load_run(self.run_dir)
        live = any(source.mode == "live" for source in run.manifest.sources)
        signoffs: dict[str, list[c.LogEvent]] = defaultdict(list)
        logged_status: dict[str, str] = {}
        for event in run.log:
            if event.action is c.LogAction.SIGNOFF:
                for subject in event.subject_ids:
                    signoffs[subject].append(event)
            elif event.action is c.LogAction.CHECK_RECORDED and "status" in event.detail:
                for subject in event.subject_ids:
                    logged_status[subject] = str(event.detail["status"])
        # A result whose status differs from the one its check_recorded event logged
        # was changed after it was logged; the log does not back it.
        drift = {
            r.id: logged_status[r.id]
            for r in run.results
            if r.id in logged_status and logged_status[r.id] != r.status.value
        }
        verifications = {
            result.id: [self.verify(evidence, live) for evidence in result.evidence]
            for result in run.results
        }
        failed = sum(v.state == "failed" for vs in verifications.values() for v in vs)
        unchecked = sum(v.state == "unchecked" for vs in verifications.values() for v in vs)
        transactions = {}
        if self.sources:
            transactions = {t.id: t for t in self.sources.transactions()}
        return {
            "run": run,
            "manifest": run.manifest,
            "chain": chain,
            "signoffs": signoffs,
            "verifications": verifications,
            "evidence_failed": failed,
            "evidence_unchecked": unchecked,
            "transactions": transactions,
            "dataset": self.sources.dataset if self.sources else None,
            "demo": self.demo or is_demo(run.manifest),
            "live": live,
            "drift": drift,
            "log_partial": log_partial,
            "in_progress": run.manifest.finished_at is None or log_partial,
            "csrf": self.csrf,
        }

    def render(self, template: str, status_code: int = 200, **values) -> HTMLResponse:
        html = self.env.get_template(template).render(**values)
        return HTMLResponse(html, status_code=status_code)

    def not_found(self, what: str) -> HTMLResponse:
        return self.render("error.html", status_code=404, message=f"Not found: {what}")

    # -- pages -----------------------------------------------------------------

    async def results(self, request: Request) -> Response:
        ctx = self.context()
        status = request.query_params.get("status") or ""
        check = request.query_params.get("check") or ""
        rows = [
            r
            for r in ctx["run"].results
            if (not status or r.status.value == status) and (not check or r.check.value == check)
        ]
        counts = Counter(r.status.value for r in ctx["run"].results)
        matrix = {
            name: Counter(r.status.value for r in ctx["run"].results if r.check.value == name)
            for name in CHECKS
        }
        signed = sum(1 for r in ctx["run"].results if ctx["signoffs"].get(r.id))
        return self.render(
            "results.html",
            page="results",
            rows=rows,
            counts=counts,
            matrix=matrix,
            signed=signed,
            status=status,
            check=check,
            checks=CHECKS,
            statuses=STATUSES,
            **ctx,
        )

    async def result(self, request: Request) -> Response:
        ctx = self.context()
        wanted = request.query_params.get("id", "")
        result = next((r for r in ctx["run"].results if r.id == wanted), None)
        if result is None:
            return self.not_found(f"result {wanted}")
        steps = [
            e
            for e in ctx["run"].log
            if result.id in e.subject_ids or result.transaction_id in e.subject_ids
        ]
        drafts = [d for d in ctx["run"].drafts if result.id in d.result_ids]
        return self.render(
            "result.html",
            page="results",
            result=result,
            evidence=list(zip(result.evidence, ctx["verifications"][result.id], strict=True)),
            steps=steps,
            drafts=drafts,
            error=request.query_params.get("error"),
            **ctx,
        )

    async def packets(self, request: Request) -> Response:
        ctx = self.context()
        packets = [
            (packet, [(item, self.verify(item.evidence, ctx["live"])) for item in packet.items])
            for packet in ctx["run"].packets
        ]
        sample = None
        if ctx["dataset"] and ctx["manifest"].sample_id:
            sample = next(
                (s for s in ctx["dataset"].samples if s.id == ctx["manifest"].sample_id), None
            )
        lines = {item.line: item for item in sample.items} if sample else {}
        return self.render("packets.html", page="packets", packets=packets, lines=lines, **ctx)

    async def drafts(self, request: Request) -> Response:
        return self.render("drafts.html", page="drafts", **self.context())

    async def log(self, request: Request) -> Response:
        return self.render("log.html", page="log", **self.context())

    async def signoff(self, request: Request) -> Response:
        form = {k: v[0].strip() for k, v in parse_qs((await request.body()).decode()).items()}
        if not _same_origin(request) or not secrets.compare_digest(form.get("csrf", ""), self.csrf):
            return self.render(
                "error.html",
                status_code=403,
                message="Sign-off refused: the form did not come from this page. Reload and try again.",
            )
        result_id = form.get("result_id", "")
        back = f"/result?id={quote(result_id, safe='')}"
        run, chain, log_partial = _load_run(self.run_dir)
        if result_id not in {r.id for r in run.results}:
            return self.not_found(f"result {result_id}")
        reviewer, decision = form.get("reviewer", ""), form.get("decision", "")
        problem = None
        if run.manifest.finished_at is None or log_partial:
            # The agent's LogWriter keeps seq and the last hash in memory; appending
            # now would fork the chain.
            problem = "The run is still in progress. Sign-off opens once it has finished."
        elif not reviewer:
            problem = "Enter your name to sign off."
        elif decision not in ("accepted", "disputed"):
            problem = "Choose accepted or disputed."
        elif chain:
            problem = "The log's chain is broken, so a sign-off cannot be added to it."
        if problem:
            return RedirectResponse(f"{back}&error={quote(problem)}#signoff", status_code=303)
        writer = c.LogWriter(self.run_dir / c.LOG, run.manifest.run_id)
        writer.sign_off(reviewer, result_id, decision, form.get("note", ""))
        return RedirectResponse(f"{back}#signoff", status_code=303)

    def _document(self, request: Request) -> tuple[c.Document, Path] | None:
        if not self.sources:
            return None
        doc_id = request.query_params.get("id", "")
        document = next((d for d in self.sources.documents() if d.id == doc_id), None)
        if document is None:
            return None
        root = self.dataset_dir.resolve()
        file = (root / document.path).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            return None
        return document, file

    async def document(self, request: Request) -> Response:
        found = self._document(request)
        if found is None:
            return self.not_found("document (start the viewer with --dataset to open documents)")
        document, file = found
        try:
            pages = list(c.page_texts(file))
            read_error = None
        except Exception as error:
            pages, read_error = [], str(error)
        quote_text = request.query_params.get("quote")
        try:
            focus = int(request.query_params.get("page", "0"))
        except ValueError:
            focus = 0
        return self.render(
            "document.html",
            page="",
            document=document,
            hash_ok=c.sha256_file(file) == document.sha256,
            pages=[
                _highlight(text, quote_text if n == focus else None)
                for n, text in enumerate(pages, 1)
            ],
            focus=focus,
            read_error=read_error,
            **self.context(),
        )

    async def document_file(self, request: Request) -> Response:
        found = self._document(request)
        if found is None:
            return self.not_found("document file")
        document, file = found
        headers = {
            "X-Content-Type-Options": "nosniff",
            # Whatever the file holds, it runs no script and has no origin.
            "Content-Security-Policy": "sandbox; frame-ancestors 'none'",
        }
        if document.media_type == "application/pdf":
            return FileResponse(file, media_type="application/pdf", headers=headers)
        if document.media_type.startswith("text/"):
            # Never serve a dataset file as HTML.
            return FileResponse(file, media_type="text/plain; charset=utf-8", headers=headers)
        return FileResponse(file, filename=file.name, headers=headers)

    def app(self) -> Starlette:
        return Starlette(
            middleware=[Middleware(SecurityHeaders)],
            routes=[
                Route("/", self.results),
                Route("/result", self.result),
                Route("/packets", self.packets),
                Route("/drafts", self.drafts),
                Route("/log", self.log),
                Route("/document", self.document),
                Route("/document/file", self.document_file),
                Route("/signoff", self.signoff, methods=["POST"]),
            ],
        )


def create_app(
    run_dir: Path | str, dataset_dir: Path | str | None = None, demo: bool = False
) -> Starlette:
    return Viewer(run_dir, dataset_dir, demo).app()
