"""The run viewer: one run folder, its results, the evidence behind them, the log, sign-off.

Reads the run folder on every request, so sign-offs and a run that is still being
written show up on reload. Evidence is checked with `verify_evidence` against one
named basis, and the screen always says which:

1. the run's own snapshot (`sources/`), once it matches the fingerprints the run
   logged when it fetched the data;
2. otherwise a local dataset (`--dataset`, or `dataset_path` from run.json when its
   `dataset_id` matches), but only if every source of the run was local;
3. otherwise nothing: a live run without a snapshot is "unchecked: live run", and a
   run with no dataset is "not checked".
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import auditorium_contracts as c
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .demo import DEMO_PREFIX

CHECKS = [check.value for check in c.CheckName]
STATUSES = [status.value for status in c.CheckStatus]
LIVE_UNCHECKED = "evidence not re-verified: the run used a live source and left no snapshot"
CHANGED_SINCE_SHOWN = (
    "This result changed after the page was shown to you, so the sign-off was refused. "
    "Read it again below, then sign off."
)


@dataclass(frozen=True)
class Verification:
    state: str  # "verified", "failed", "unchecked" or "live_unchecked"
    reason: str | None = None
    basis: str | None = None  # "snapshot" or "dataset" when the item was checked


@dataclass
class Basis:
    """What evidence is checked against, and why. Never just "verified"."""

    kind: str  # "snapshot", "dataset", "live" or "none"
    sources: c.LocalSources | None = None
    index: c.SourceIndex | None = None
    reason: str | None = None
    detail: str | None = None  # the snapshot folder or the dataset path


@dataclass
class SignoffState:
    """The latest sign-off of one result, and whether the result still matches it."""

    event: c.LogEvent
    state: str  # "pinned", "changed" or "unpinned"


@dataclass
class RunSignoff:
    event: c.LogEvent
    problems: list[str] = field(default_factory=list)


ATTENTION = "attention"  # a status filter: fail or needs_review
# Lower sorts first. Fail and needs_review are the work; a clean row sorts last.
SEVERITY = {"fail": 0, "needs_review": 1, "pass": 2, "not_applicable": 3}
SORTS = {
    "attention": "Needs attention first",
    "amount": "Amount, largest first",
    "date": "Date, newest first",
    "vendor": "Vendor, A to Z",
    "order": "Order in the run",
}


@dataclass
class TxRow:
    """One transaction on the results page: its four checks side by side.

    Built only from the run's results and, when the basis has them, the source
    transaction records. Nothing here is inferred: a missing record shows its id only.
    """

    transaction_id: str
    order: int
    transaction: c.SpendTransaction | None
    results: dict[str, c.CheckResult]  # check name -> result
    worst: str  # the most severe status among the checks
    lead: c.CheckResult  # the most severe result; the vendor links to it
    exceptions: list[c.CheckResult] = field(default_factory=list)  # fail, then needs_review
    evidence_total: int = 0
    evidence_verified: int = 0
    evidence_failed: int = 0
    signed: int = 0
    search: str = ""

    def matches(self, status: str, check: str) -> bool:
        return any(_result_matches(r, status, check) for r in self.results.values())


def _result_matches(result: c.CheckResult, status: str, check: str) -> bool:
    if check and result.check.value != check:
        return False
    if status == ATTENTION:
        return result.status.value in ("fail", "needs_review")
    return not status or result.status.value == status


def _transaction_rows(ctx: dict) -> list[TxRow]:
    grouped: dict[str, dict[str, c.CheckResult]] = {}
    for result in ctx["run"].results:
        grouped.setdefault(result.transaction_id, {})[result.check.value] = result
    rows = []
    for order, (tx_id, by_check) in enumerate(grouped.items()):
        ordered = sorted(
            by_check.values(), key=lambda r: (SEVERITY[r.status.value], CHECKS.index(r.check.value))
        )
        lead = ordered[0]
        states = [v.state for r in by_check.values() for v in ctx["verifications"][r.id]]
        tx = ctx["transactions"].get(tx_id)
        words = [
            tx_id,
            *(f"{r.summary} {' '.join(i.value for i in r.issue_codes)}" for r in by_check.values()),
        ]
        if tx is not None:
            words += [
                tx.vendor_name,
                _money(tx.amount_cents),
                str(tx.amount_cents / 100),
                str(tx.transaction_date),
                tx.invoice_number or "",
                tx.kind.value,
            ]
        rows.append(
            TxRow(
                transaction_id=tx_id,
                order=order,
                transaction=tx,
                results=by_check,
                worst=lead.status.value,
                lead=lead,
                exceptions=[r for r in ordered if r.status.value in ("fail", "needs_review")],
                evidence_total=len(states),
                evidence_verified=states.count("verified"),
                evidence_failed=states.count("failed"),
                signed=sum(r.id in ctx["signoff_state"] for r in by_check.values()),
                search=" ".join(words).lower(),
            )
        )
    return rows


def _sorted(rows: list[TxRow], sort: str) -> list[TxRow]:
    def amount(row: TxRow) -> int:
        return row.transaction.amount_cents if row.transaction else 0

    if sort == "amount":
        return sorted(rows, key=lambda r: (-amount(r), r.order))
    if sort == "date":
        return sorted(
            rows,
            key=lambda r: r.transaction.transaction_date.isoformat() if r.transaction else "",
            reverse=True,
        )
    if sort == "vendor":
        return sorted(
            rows,
            key=lambda r: ((r.transaction.vendor_name.lower() if r.transaction else "~"), r.order),
        )
    if sort == "order":
        return sorted(rows, key=lambda r: r.order)
    return sorted(rows, key=lambda r: (SEVERITY[r.worst], -amount(r), r.order))


def is_demo(manifest: c.RunManifest) -> bool:
    """The demo marks its own run and dataset ids, so it is labelled even without --demo."""
    return manifest.run_id.startswith(DEMO_PREFIX) and (manifest.dataset_id or "").startswith(
        DEMO_PREFIX
    )


def _read_log(path: Path) -> tuple[list[str], bool, list[str]]:
    """The log's readable lines, whether a half-written last line was left out, and damage.

    A final line with no newline after it that does not parse is the agent mid-write:
    it is left out quietly. Any other line that does not read as an event is damage.
    The page still renders the events before it and says what is wrong, and sign-off
    is refused, as `LogWriter` itself would refuse it.
    """
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    partial = False
    if lines and not text.endswith("\n"):
        try:
            c.LogEvent.model_validate_json(lines[-1])
        except ValueError:
            lines, partial = lines[:-1], True
    for number, line in enumerate(lines, 1):
        try:
            c.LogEvent.model_validate_json(line)
        except ValueError:
            after = len(lines) - number
            damage = [f"log line {number} is not a valid event"]
            if after:
                damage.append(
                    f"The {after} line(s) after it are not shown. Damage before the end of a "
                    "log is not crash damage and is not repaired: treat this log as tampered."
                )
            else:
                damage.append(
                    "If a crash cut it off: python -m auditorium_contracts repair-log "
                    f"{path}. Repair keeps the removed line in {path.name}.damaged."
                )
            return lines[: number - 1], partial, damage
    return lines, partial, []


def _verify_lines(lines: list[str]) -> list[str]:
    """`verify_log` on exactly the lines shown, not on a file that may have grown since."""
    with tempfile.TemporaryDirectory() as folder:
        snapshot = Path(folder) / c.LOG
        snapshot.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return c.verify_log(snapshot)


@dataclass
class Loaded:
    run: c.Run
    chain: list[str]  # verify_log problems in the lines shown
    partial: bool  # a half-written last line was left out
    damage: list[str]  # a line that is not an event; sign-off is refused
    file_hashes: dict[str, str]  # sha256 of results.jsonl and run.json as read


def _load_run(root: Path) -> Loaded:
    """Like `load_run` plus `verify_log`, but a half-written or damaged log line does not
    stop the page. Also hashes results.jsonl and run.json from the very bytes parsed, so a
    run sign-off can pin what the page showed."""
    lines, partial, damage = _read_log(root / c.LOG)
    log = [c.LogEvent.model_validate_json(line) for line in lines]

    def optional(name: str, model) -> tuple:
        return tuple(c.read_jsonl(root / name, model)) if (root / name).is_file() else ()

    manifest_bytes = (root / c.RUN_MANIFEST).read_bytes()
    results_bytes = (root / c.RESULTS).read_bytes()
    scorecard = root / c.SCORECARD
    run = c.Run(
        root=root,
        manifest=c.RunManifest.model_validate_json(manifest_bytes),
        results=tuple(
            c.CheckResult.model_validate_json(line)
            for line in results_bytes.decode("utf-8").splitlines()
            if line.strip()
        ),
        packets=optional(c.PACKETS, c.EvidencePacket),
        drafts=optional(c.DRAFTS, c.FollowUpDraft),
        log=tuple(log),
        scorecard=c.Scorecard.model_validate_json(scorecard.read_text("utf-8"))
        if scorecard.is_file()
        else None,
    )
    hashes = {
        "results_sha256": hashlib.sha256(results_bytes).hexdigest(),
        "run_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    return Loaded(run, _verify_lines(lines), partial, damage, hashes)


def _snapshot_problems(log: tuple[c.LogEvent, ...], snapshot: c.LocalSources) -> list[str]:
    """The snapshot against the fingerprints the run logged at fetch time.

    Same rule as the contract's `validate_run`: only `source_call` events with both
    `call` and `sha256` are fingerprints."""
    logged = {
        event.detail["call"]: event.detail["sha256"]
        for event in log
        if event.action is c.LogAction.SOURCE_CALL
        and "call" in event.detail
        and "sha256" in event.detail
    }
    if not logged:
        return ["the log holds no source fingerprints, so the snapshot cannot be tied to the fetch"]
    current = c.fingerprint_sources(snapshot)
    return [
        f"{call} in the snapshot is not what the run fetched"
        for call, digest in sorted(current.items())
        if logged.get(call) != digest
    ]


def _signoffs(run: c.Run, file_hashes: dict[str, str]):
    """Every sign-off by subject, the latest per result with its state, the latest run
    sign-off with its problems, and the list of problems for the banner.

    Only the latest sign-off of a result, or of the run, counts, as in `validate_run`.
    """
    by_subject: dict[str, list[c.LogEvent]] = defaultdict(list)
    finished_seq = None
    for event in run.log:
        if event.action is c.LogAction.RUN_FINISHED:
            finished_seq = event.seq
        if event.action is c.LogAction.SIGNOFF:
            for subject in event.subject_ids:
                by_subject[subject].append(event)
    results = {r.id: r for r in run.results}
    per_result: dict[str, SignoffState] = {}
    problems: list[str] = []
    for result_id, result in results.items():
        if not by_subject.get(result_id):
            continue
        latest = by_subject[result_id][-1]
        signed = latest.detail.get("result_sha256")
        if not signed:
            state = "unpinned"
        elif signed == c.result_hash(result):
            state = "pinned"
        else:
            state = "changed"
            problems.append(
                f"result {result_id}: changed after it was signed off (log line {latest.seq})"
            )
        per_result[result_id] = SignoffState(latest, state)
    run_signoff = None
    run_events = [
        e for e in by_subject.get(run.manifest.run_id, []) if e.detail.get("scope") == "run"
    ]
    if run_events:
        latest = run_events[-1]
        mine: list[str] = []
        if finished_seq is None or latest.seq < finished_seq:
            mine.append(f"log line {latest.seq}: the run was signed off before it finished")
        for key, name in (("results_sha256", c.RESULTS), ("run_sha256", c.RUN_MANIFEST)):
            pinned = latest.detail.get(key)
            if pinned and pinned != file_hashes[key]:
                mine.append(f"{name}: changed after the run was signed off")
        run_signoff = RunSignoff(latest, mine)
        problems += mine
    return by_subject, per_result, run_signoff, problems, finished_seq is not None


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
        self._datasets: dict[Path, tuple[c.LocalSources, c.SourceIndex]] = {}
        if self.dataset_dir:
            self._local(self.dataset_dir)  # a bad --dataset fails at start, not on a page
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

    def _local(self, folder: Path) -> tuple[c.LocalSources, c.SourceIndex]:
        """A dataset folder's sources and index, loaded once per folder."""
        key = Path(folder).resolve()
        if key not in self._datasets:
            sources = c.LocalSources(key)
            self._datasets[key] = (sources, c.SourceIndex(sources))
        return self._datasets[key]

    def dataset(self, manifest: c.RunManifest) -> tuple[c.LocalSources | None, str, str | None]:
        """The local dataset for this run: (sources, where it came from, why none was used).

        `--dataset` wins. Without it, run.json's `dataset_path` is used once the folder's
        `dataset_id` matches the run's: folders move, and a path alone proves nothing.
        """
        wanted = manifest.dataset_id
        if self.dataset_dir:
            sources, _ = self._local(self.dataset_dir)
            found = sources.dataset.manifest.dataset_id
            if wanted and found != wanted:
                return (
                    None,
                    "--dataset",
                    f"--dataset {self.dataset_dir} holds dataset {found}, but this run used "
                    f"{wanted}, so it was not used.",
                )
            return sources, "--dataset", None
        path = manifest.dataset_path
        origin = "run.json dataset_path"
        if not path:
            return None, "", "No --dataset was given and run.json has no dataset_path."
        if not wanted:
            return (
                None,
                origin,
                f"run.json names dataset_path {path} but no dataset_id to check it against, "
                "so it was not used. Pass --dataset to choose one.",
            )
        folder = Path(path)
        if not (folder / "manifest.json").is_file():
            return (
                None,
                origin,
                f"run.json names dataset_path {path}, which holds no dataset here. "
                "Pass --dataset to choose one.",
            )
        try:
            found = c.DatasetManifest.model_validate_json(
                (folder / "manifest.json").read_text("utf-8")
            ).dataset_id
            if found != wanted:
                return (
                    None,
                    origin,
                    f"run.json names dataset_path {path}, which now holds dataset {found}, "
                    f"not {wanted}, so it was not used.",
                )
            sources, _ = self._local(folder)
        except (OSError, ValueError) as error:
            return None, origin, f"dataset_path {path} could not be loaded: {error}"
        return sources, origin, None

    def basis(
        self, run: c.Run, dataset: c.LocalSources | None, dataset_note: str | None, live: bool
    ) -> tuple[Basis, list[str]]:
        """What evidence is checked against, and any problem with the run's snapshot."""
        snapshot_problems: list[str] = []
        folder = self.run_dir / c.SOURCES_SNAPSHOT
        if folder.is_dir():
            try:
                snapshot = c.LocalSources(folder)
                snapshot_problems = _snapshot_problems(run.log, snapshot)
            except (OSError, ValueError) as error:
                snapshot_problems = [f"the snapshot cannot be loaded ({error})"]
            if not snapshot_problems:
                # verified_against(root, None) is "snapshot": what the run fetched, unchanged.
                return (
                    Basis(
                        c.verified_against(self.run_dir, None),
                        snapshot,
                        c.SourceIndex(snapshot),
                        detail=str(folder),
                    ),
                    [],
                )
        if live:
            return Basis("live", reason=LIVE_UNCHECKED), snapshot_problems
        if dataset is not None:
            # verified_against(root, sources) says "the sources given": here, this dataset.
            return (
                Basis(
                    "dataset",
                    dataset,
                    self._local(dataset.dataset.root)[1],
                    detail=f"{dataset.dataset.manifest.dataset_id} at {dataset.dataset.root}",
                ),
                snapshot_problems,
            )
        reason = dataset_note or "no dataset given, so this was not checked"
        return Basis("none", reason=reason), snapshot_problems

    @staticmethod
    def verify(evidence, basis: Basis) -> Verification:
        if basis.kind == "live":
            return Verification("live_unchecked", basis.reason)
        if basis.index is None:
            return Verification("unchecked", basis.reason)
        try:
            problem = c.verify_evidence(evidence, basis.index)
        except Exception as error:  # a broken source file must not hide the item
            problem = f"could not check: {error}"
        if problem:
            return Verification("failed", problem, basis.kind)
        return Verification("verified", basis=basis.kind)

    def _where(self, loaded: Loaded) -> tuple[c.LocalSources | None, str, str | None, Basis, list]:
        manifest = loaded.run.manifest
        live = any(source.mode == "live" for source in manifest.sources)
        dataset, dataset_from, dataset_note = self.dataset(manifest)
        basis, snapshot_problems = self.basis(loaded.run, dataset, dataset_note, live)
        return dataset, dataset_from, dataset_note, basis, snapshot_problems

    def context(self) -> dict:
        loaded = _load_run(self.run_dir)
        run = loaded.run
        manifest = run.manifest
        live = any(source.mode == "live" for source in manifest.sources)
        dataset, dataset_from, dataset_note, basis, snapshot_problems = self._where(loaded)
        signoffs, signoff_state, run_signoff, signoff_problems, finished_logged = _signoffs(
            run, loaded.file_hashes
        )
        logged_status: dict[str, str] = {}
        for event in run.log:
            if event.action is c.LogAction.CHECK_RECORDED and "status" in event.detail:
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
            result.id: [self.verify(evidence, basis) for evidence in result.evidence]
            for result in run.results
        }
        failed = sum(v.state == "failed" for vs in verifications.values() for v in vs)
        # Records shown beside a result come from the basis; a live run's ids need not
        # exist in a local dataset, so without a snapshot it shows ids only.
        records = basis.sources or (dataset if not live else None)
        transactions = {t.id: t for t in records.transactions()} if records else {}
        in_progress = manifest.finished_at is None or loaded.partial
        blocked = in_progress or bool(loaded.chain) or bool(loaded.damage)
        return {
            "run": run,
            "manifest": manifest,
            "chain": loaded.chain,
            "log_damage": loaded.damage,
            "signoffs": signoffs,
            "signoff_state": signoff_state,
            "run_signoff": run_signoff,
            "signoff_problems": signoff_problems,
            "verifications": verifications,
            "evidence_failed": failed,
            "transactions": transactions,
            "basis": basis,
            "doc_sources": basis.sources is not None,
            "dataset": dataset.dataset if dataset else None,
            "dataset_from": dataset_from,
            "dataset_note": dataset_note,
            "snapshot_problems": snapshot_problems,
            "demo": self.demo or is_demo(manifest),
            "synthetic": [s.system.value for s in manifest.sources if s.synthetic],
            "live": live,
            "drift": drift,
            "log_partial": loaded.partial,
            "in_progress": in_progress,
            "can_sign": not blocked,
            "can_sign_run": not blocked and finished_logged,
            "finished_logged": finished_logged,
            "file_hashes": loaded.file_hashes,
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
        query = (request.query_params.get("q") or "").strip()
        sort = request.query_params.get("sort") or "attention"
        if status not in STATUSES and status != ATTENTION:
            status = ""
        if check not in CHECKS:
            check = ""
        if sort not in SORTS:
            sort = "attention"
        # Every row is rendered; the ones the filter leaves out are hidden, so the page
        # can refilter in place without asking the server again.
        all_rows = _sorted(_transaction_rows(ctx), sort)
        shown = {
            row.transaction_id
            for row in all_rows
            if row.matches(status, check) and (not query or query.lower() in row.search)
        }
        results = ctx["run"].results
        counts = Counter(r.status.value for r in results)
        matrix = {
            name: Counter(r.status.value for r in results if r.check.value == name)
            for name in CHECKS
        }
        evidence = Counter(v.state for vs in ctx["verifications"].values() for v in vs)
        return self.render(
            "results.html",
            page="results",
            rows=all_rows,
            shown=shown,
            filtering=bool(status or check),
            matches=lambda result: _result_matches(result, status, check),
            severity=SEVERITY,
            counts=counts,
            matrix=matrix,
            evidence_counts=evidence,
            evidence_total=sum(evidence.values()),
            signed=len(ctx["signoff_state"]),
            status=status,
            check=check,
            query=query,
            sort=sort,
            sorts=SORTS,
            checks=CHECKS,
            statuses=STATUSES,
            error=request.query_params.get("error"),
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
        siblings = sorted(
            (r for r in ctx["run"].results if r.transaction_id == result.transaction_id),
            key=lambda r: CHECKS.index(r.check.value),
        )
        # Step through the exceptions (fail, needs_review) in the order of the results page.
        queue = [
            r.id
            for row in _sorted(_transaction_rows(ctx), "attention")
            for r in sorted(row.results.values(), key=lambda r: CHECKS.index(r.check.value))
            if r.status.value in ("fail", "needs_review")
        ]
        position = queue.index(result.id) if result.id in queue else None
        if position is None:
            prev_id, next_id = None, (queue[0] if queue else None)
        else:
            prev_id = queue[position - 1] if position > 0 else None
            next_id = queue[position + 1] if position + 1 < len(queue) else None
        return self.render(
            "result.html",
            page="results",
            result=result,
            siblings=siblings,
            queue_size=len(queue),
            queue_position=position,
            prev_id=prev_id,
            next_id=next_id,
            # Taken now, from the result this page shows: the sign-off pins exactly this.
            shown_hash=c.result_hash(result),
            evidence=list(zip(result.evidence, ctx["verifications"][result.id], strict=True)),
            steps=steps,
            drafts=drafts,
            error=request.query_params.get("error"),
            **ctx,
        )

    async def packets(self, request: Request) -> Response:
        ctx = self.context()
        packets = [
            (packet, [(item, self.verify(item.evidence, ctx["basis"])) for item in packet.items])
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
        ctx = self.context()
        # The contract's own verdict on the whole folder. Only on this page: it re-reads
        # every document. It needs a complete log, and a snapshot or local sources.
        validation, validated_against = None, None
        if not (ctx["log_partial"] or ctx["log_damage"]) and ctx["basis"].sources is not None:
            given = None if ctx["basis"].kind == "snapshot" else ctx["basis"].sources
            validation = c.validate_run(self.run_dir, given)
            validated_against = ctx["basis"].kind
        return self.render(
            "log.html",
            page="log",
            actions=Counter(e.action.value for e in ctx["run"].log),
            validation=validation,
            validated_against=validated_against,
            **ctx,
        )

    # -- sign-off --------------------------------------------------------------

    def _forged(self, request: Request, form: dict) -> Response | None:
        if not _same_origin(request) or not secrets.compare_digest(form.get("csrf", ""), self.csrf):
            return self.render(
                "error.html",
                status_code=403,
                message="Sign-off refused: the form did not come from this page. Reload and try again.",
            )
        return None

    @staticmethod
    def _signing_problem(loaded: Loaded, reviewer: str, decision: str) -> str | None:
        if loaded.run.manifest.finished_at is None or loaded.partial:
            # The contract's file lock makes a concurrent append safe, but a reviewer
            # signs what the agent has finished, not a run that is still moving.
            return "The run is still in progress. Sign-off opens once it has finished."
        if loaded.damage:
            return "The step log is damaged, so a sign-off cannot be added to it. " + " ".join(
                loaded.damage
            )
        if loaded.chain:
            return "The log's chain is broken, so a sign-off cannot be added to it."
        if not reviewer:
            return "Enter your name to sign off."
        if decision not in ("accepted", "disputed"):
            return "Choose accepted or disputed."
        return None

    async def signoff(self, request: Request) -> Response:
        form = {k: v[0].strip() for k, v in parse_qs((await request.body()).decode()).items()}
        forged = self._forged(request, form)
        if forged:
            return forged
        result_id = form.get("result_id", "")
        back = f"/result?id={quote(result_id, safe='')}"
        loaded = _load_run(self.run_dir)
        current = next((r for r in loaded.run.results if r.id == result_id), None)
        if current is None:
            return self.not_found(f"result {result_id}")
        reviewer, decision = form.get("reviewer", ""), form.get("decision", "")
        shown = form.get("result_sha256", "")
        problem = self._signing_problem(loaded, reviewer, decision)
        if problem is None and not shown:
            problem = (
                "The form did not say which version of the result you read. Reload and try again."
            )
        if problem is None:
            writer = c.LogWriter(self.run_dir / c.LOG, loaded.run.manifest.run_id)
            try:
                writer.sign_off(
                    reviewer,
                    result_id,
                    decision,
                    form.get("note", ""),
                    result=current,
                    result_sha256=shown,
                )
            except c.LogDamaged as error:
                problem = f"Sign-off refused: {error}"
            except ValueError:
                problem = CHANGED_SINCE_SHOWN
        if problem:
            return RedirectResponse(f"{back}&error={quote(problem)}#signoff", status_code=303)
        return RedirectResponse(f"{back}#signoff", status_code=303)

    async def signoff_run(self, request: Request) -> Response:
        form = {k: v[0].strip() for k, v in parse_qs((await request.body()).decode()).items()}
        forged = self._forged(request, form)
        if forged:
            return forged
        loaded = _load_run(self.run_dir)
        reviewer, decision = form.get("reviewer", ""), form.get("decision", "")
        problem = self._signing_problem(loaded, reviewer, decision)
        finished = any(e.action is c.LogAction.RUN_FINISHED for e in loaded.run.log)
        if problem is None and not finished:
            problem = "The log has no run_finished event yet, so the run cannot be signed off."
        if problem is None and any(
            form.get(key, "") != digest for key, digest in loaded.file_hashes.items()
        ):
            problem = (
                "The results or run.json changed after the page was shown to you, so the "
                "sign-off was refused. Read the run again, then sign off."
            )
        if problem is None:
            writer = c.LogWriter(self.run_dir / c.LOG, loaded.run.manifest.run_id)
            try:
                writer.sign_off_run(reviewer, decision, form.get("note", ""))
            except c.LogDamaged as error:
                problem = f"Sign-off refused: {error}"
        if problem:
            return RedirectResponse(f"/?error={quote(problem)}#run-signoff", status_code=303)
        return RedirectResponse("/#run-signoff", status_code=303)

    # -- documents -------------------------------------------------------------

    def _document(self, request: Request) -> tuple[c.Document, Path] | None:
        """A document from the basis: the run's snapshot or a matching local dataset."""
        basis = self._where(_load_run(self.run_dir))[3]
        if basis.sources is None:
            return None
        doc_id = request.query_params.get("id", "")
        document = next((d for d in basis.sources.documents() if d.id == doc_id), None)
        if document is None:
            return None
        root = basis.sources.dataset.root.resolve()
        file = (root / document.path).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            return None
        return document, file

    async def document(self, request: Request) -> Response:
        found = self._document(request)
        if found is None:
            return self.not_found(
                "document (documents open from the run's snapshot or a matching local dataset)"
            )
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
            middleware=[Middleware(SecurityHeaders), Middleware(GZipMiddleware, minimum_size=2048)],
            routes=[
                Route("/", self.results),
                Route("/result", self.result),
                Route("/packets", self.packets),
                Route("/drafts", self.drafts),
                Route("/log", self.log),
                Route("/document", self.document),
                Route("/document/file", self.document_file),
                Route("/signoff", self.signoff, methods=["POST"]),
                Route("/signoff-run", self.signoff_run, methods=["POST"]),
            ],
        )


def create_app(
    run_dir: Path | str, dataset_dir: Path | str | None = None, demo: bool = False
) -> Starlette:
    return Viewer(run_dir, dataset_dir, demo).app()
