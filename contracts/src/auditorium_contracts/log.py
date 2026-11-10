"""The step log: one JSON object per line, append-only, each line chained to the last.

Both the agent and the screen write to it (the screen appends sign-offs), so
both must use `LogWriter`. Each event stores the hash of the one before it,
which means a line cannot be edited or removed later without `verify_log`
noticing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from .records import Record

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


def event_hash(event: dict[str, Any]) -> str:
    """SHA-256 of the event without its own `hash`, as compact JSON with sorted keys."""
    body = {key: value for key, value in event.items() if key != "hash"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


class LogWriter:
    """Appends events to a run's log. Opening an existing log continues its chain."""

    def __init__(self, path: Path | str, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self._seq = 0
        self._last = GENESIS
        if self.path.exists():
            events = read_log(self.path)
            if events:
                self._seq, self._last = events[-1].seq, events[-1].hash

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
        event = LogEvent(
            seq=self._seq + 1,
            ts=ts or datetime.now(UTC),
            run_id=self.run_id,
            actor=actor,
            actor_id=actor_id,
            action=action,
            subject_ids=subject_ids or [],
            detail=detail or {},
            prev_hash=self._last,
            hash="",
        )
        raw = event.model_dump(mode="json")
        raw["hash"] = event_hash(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(raw, ensure_ascii=False) + "\n")
        self._seq, self._last = raw["seq"], raw["hash"]
        return LogEvent.model_validate(raw)

    def sign_off(
        self,
        reviewer: str,
        result_id: str,
        decision: Literal["accepted", "disputed"],
        comment: str = "",
        ts: datetime | None = None,
    ) -> LogEvent:
        """A reviewer's verdict on one result. The screen calls this."""
        return self.append(
            "reviewer",
            LogAction.SIGNOFF,
            actor_id=reviewer,
            subject_ids=[result_id],
            detail={"decision": decision, "comment": comment},
            ts=ts,
        )
