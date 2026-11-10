"""Build evidence from the record itself, so the cited value is always the real one."""

from __future__ import annotations

import json

from auditorium_contracts import RecordEvidence
from pydantic import BaseModel


def record_evidence(record_type: str, record: BaseModel, path: str, note: str) -> RecordEvidence:
    """Evidence for one field of a source record. `path` is dotted, e.g. 'approvals.0.approver_id'."""
    value = record.model_dump(mode="json")
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return RecordEvidence(
        record_type=record_type,
        record_id=record.id,
        field=path,
        value=value if isinstance(value, str) else json.dumps(value),
        note=note,
    )
