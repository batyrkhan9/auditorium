"""The step log: one JSON object per line, append-only, each line chained to the last.

Both the agent and the screen write to it (the screen appends sign-offs), so
both must use `LogWriter`. Each event stores the hash of the one before it,
which means a line cannot be edited or removed later without `verify_log`
noticing.

A crash can leave the last line cut off. `LogWriter` then refuses to append, with
an error that names the fix, and `repair_log` puts the file right without
destroying anything.

Two writers can have the same log open. Every append takes an exclusive lock on
the file and reads the last line under that lock, so the chain cannot fork.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from .layout import RESULTS, RUN_MANIFEST
from .records import Record
from .results import CheckResult

GENESIS = "0" * 64


class LogAction(StrEnum):
    RUN_STARTED = "run_started"
    SOURCE_CALL = "source_call"
    DOCUMENT_READ = "document_read"
    MODEL_CALL = "model_call"
    CHECK_RECORDED = "check_recorded"
    PACKET_ASSEMBLED = "packet_assembled"
    DRAFT_CREATED = "draft_created"
    SIGNOFF = "signoff"
    NOTE = "note"
    ERROR = "error"
    RUN_FINISHED = "run_finished"


class LogEvent(Record):
    seq: int = Field(ge=1)
    ts: datetime
    run_id: str
    actor: Literal["agent", "reviewer", "system"]
    actor_id: str | None = Field(None, description="Model id, tool name, or the reviewer's name.")
    action: LogAction
    subject_ids: list[str] = Field(
        default_factory=list, description="Transactions, results, documents this step concerns."
    )
    detail: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def event_hash(event: dict[str, Any]) -> str:
    """SHA-256 of the event without its own `hash`, as compact JSON with sorted keys."""
    return _canonical_hash({key: value for key, value in event.items() if key != "hash"})


def result_hash(result: CheckResult) -> str:
    """SHA-256 of a result's content. A sign-off stores it, to pin what was signed.

    Fields left at their defaults are not hashed. So a field added to the contract
    later, with a default, leaves the hash of every result signed before it unchanged.
    """
    return _canonical_hash(result.model_dump(mode="json", exclude_defaults=True))


def read_log(path: Path | str) -> list[LogEvent]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [LogEvent.model_validate_json(line) for line in lines if line.strip()]


def verify_log(path: Path | str) -> list[str]:
    """Problems with the log's chain. An empty list means it is intact."""
    problems: list[str] = []
    previous = GENESIS
    lines = [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    for number, line in enumerate(lines, 1):
        try:
            raw = json.loads(line)
            LogEvent.model_validate(raw)
        except (ValueError, ValidationError) as error:
            problems.append(f"log line {number}: not a valid event ({error})")
            return problems
        if raw["seq"] != number:
            problems.append(f"log line {number}: seq is {raw['seq']}")
        if raw["prev_hash"] != previous:
            problems.append(f"log line {number}: does not follow the line before it")
        if raw["hash"] != event_hash(raw):
            problems.append(f"log line {number}: content does not match its hash")
        previous = raw["hash"]
    return problems


@contextmanager
def _exclusive(handle):
    """Hold an exclusive lock on an open file for the length of the block."""
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _last_line(handle) -> bytes | None:
    """The last non-empty line of a binary file, read from the end."""
    size = handle.seek(0, os.SEEK_END)
    block = 4096
    while size:
        start = max(0, size - block)
        handle.seek(start)
        lines = [line for line in handle.read(size - start).splitlines() if line.strip()]
        # Two lines in view means the last one is whole, not cut off at the block's edge.
        if start == 0 or len(lines) >= 2:
            return lines[-1] if lines else None
        block *= 4
    return None


class LogDamaged(Exception):
    """The log's last line is cut off or unreadable. Nothing is written until it is repaired."""


def _is_whole_event(line: bytes) -> bool:
    try:
        raw = json.loads(line)
        LogEvent.model_validate(raw)
    except (ValueError, ValidationError):
        return False
    return raw["hash"] == event_hash(raw)


def _tail(handle, path: Path) -> tuple[int, str]:
    """The seq and hash of the last event, or a LogDamaged error if the file does not end cleanly."""
    size = handle.seek(0, os.SEEK_END)
    if size == 0:
        return 0, GENESIS
    handle.seek(size - 1)
    ends_cleanly = handle.read(1) == b"\n"
    last = _last_line(handle)
    if not ends_cleanly or last is None or not _is_whole_event(last):
        handle.seek(0)
        line_number = len([line for line in handle.read().splitlines() if line.strip()])
        raise LogDamaged(
            f"{path}: line {line_number}, the last one, is cut off or not a valid event, most "
            "likely from a crash while it was being written. Nothing was appended. "
            f"To repair: python -m auditorium_contracts repair-log {path}"
        )
    raw = json.loads(last)
    return raw["seq"], raw["hash"]


def repair_log(path: Path | str) -> str:
    """Make a log writable again after a crash mid-write. Returns what was done.

    Only the end of the file is touched. A complete last event that merely lacks its
    line ending gets one. A cut-off or unreadable last line is removed and kept in
    `<log>.damaged`, so nothing is destroyed. Damage earlier in the file is not
    repaired: `verify_log` reports it, and that log should be treated as tampered.
    """
    path = Path(path)
    with path.open("r+b") as handle, _exclusive(handle):
        handle.seek(0)
        data = handle.read()
        lines = data.splitlines(keepends=True)
        if lines and not lines[-1].endswith(b"\n") and _is_whole_event(lines[-1]):
            handle.seek(0, os.SEEK_END)
            handle.write(b"\n")
            return "The last event was complete but had no line ending. Added it."
        removed = []
        while lines and not (lines[-1].endswith(b"\n") and _is_whole_event(lines[-1])):
            removed.insert(0, lines.pop())
        if not removed:
            return "The log ends cleanly. Nothing to repair."
        with path.with_name(path.name + ".damaged").open("ab") as kept:
            kept.writelines(removed)
        handle.seek(0)
        handle.truncate(sum(len(line) for line in lines))
    return (
        f"Removed {len(removed)} damaged line(s) from the end and saved them to "
        f"{path.name}.damaged. The step that was being written is not in the log; "
        "the agent must redo it."
    )


def _file_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class LogWriter:
    """Appends events to a run's log. Any number of writers may share one log."""

    def __init__(self, path: Path | str, run_id: str):
        self.path = Path(path)
        self.run_id = run_id

    def append(
        self,
        actor: Literal["agent", "reviewer", "system"],
        action: LogAction,
        *,
        subject_ids: list[str] | None = None,
        detail: dict[str, Any] | None = None,
        actor_id: str | None = None,
        ts: datetime | None = None,
    ) -> LogEvent:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle, _exclusive(handle):
            seq, last_hash = _tail(handle, self.path)
            event = LogEvent(
                seq=seq + 1,
                ts=ts or datetime.now(UTC),
                run_id=self.run_id,
                actor=actor,
                actor_id=actor_id,
                action=action,
                subject_ids=subject_ids or [],
                detail=detail or {},
                prev_hash=last_hash,
                hash="",
            )
            raw = event.model_dump(mode="json")
            raw["hash"] = event_hash(raw)
            handle.seek(0, os.SEEK_END)
            handle.write((json.dumps(raw, ensure_ascii=False) + "\n").encode("utf-8"))
            handle.flush()
        return LogEvent.model_validate(raw)

    def sign_off(
        self,
        reviewer: str,
        result_id: str,
        decision: Literal["accepted", "disputed"],
        comment: str = "",
        ts: datetime | None = None,
        *,
        result: CheckResult | None = None,
        result_sha256: str | None = None,
    ) -> LogEvent:
        """A reviewer's verdict on one result. The screen calls this.

        Pass `result_sha256`: the `result_hash` of the result as it was shown to the
        reviewer, taken when the page was rendered. That pins what they actually saw,
        and `validate_run` reports a result that no longer matches its latest sign-off.

        Pass `result` as well, the result as it is now, and a sign-off of something
        that changed while the reviewer was reading is refused. With `result` alone
        its current hash is pinned, which is only right if it was read just now.
        """
        detail = {"decision": decision, "comment": comment}
        if result is not None:
            if result.id != result_id:
                raise ValueError("the result passed is not the one being signed off")
            current = result_hash(result)
            if result_sha256 is not None and result_sha256 != current:
                raise ValueError(
                    f"result {result_id} changed after it was shown to the reviewer; "
                    "show it again before signing off"
                )
            result_sha256 = current
        if result_sha256 is not None:
            detail["result_sha256"] = result_sha256
        return self.append(
            "reviewer",
            LogAction.SIGNOFF,
            actor_id=reviewer,
            subject_ids=[result_id],
            detail=detail,
            ts=ts,
        )

    def sign_off_run(
        self,
        reviewer: str,
        decision: Literal["accepted", "disputed"],
        comment: str = "",
        ts: datetime | None = None,
    ) -> LogEvent:
        """A reviewer's verdict on the run as a whole.

        It pins the results and the run manifest as they are on disk now, by hash.
        `validate_run` reports a run sign-off made before the run finished, and one
        whose results or manifest have changed since.
        """
        folder = self.path.parent
        detail = {"scope": "run", "decision": decision, "comment": comment}
        for key, name in (("results_sha256", RESULTS), ("run_sha256", RUN_MANIFEST)):
            pinned = _file_hash(folder / name)
            if pinned:
                detail[key] = pinned
        return self.append(
            "reviewer",
            LogAction.SIGNOFF,
            actor_id=reviewer,
            subject_ids=[self.run_id],
            detail=detail,
            ts=ts,
        )
