"""JSON Schema for the contract, so code that is not Python can follow it too.

The files in contracts/schema/ are generated from the models. A test fails if
they drift, so regenerate them whenever a model changes:

    uv run python -m auditorium_contracts export-schema
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from .log import LogEvent
from .records import (
    DatasetManifest,
    Document,
    LedgerEntry,
    Person,
    SampleRequest,
    SpendPolicy,
    SpendTransaction,
)
from .results import (
    CheckResult,
    EvidencePacket,
    ExpectedResult,
    FollowUpDraft,
    RunManifest,
    Scorecard,
)

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"

MODELS: tuple[type[BaseModel], ...] = (
    DatasetManifest,
    SpendTransaction,
    LedgerEntry,
    Document,
    Person,
    SpendPolicy,
    SampleRequest,
    RunManifest,
    CheckResult,
    EvidencePacket,
    FollowUpDraft,
    LogEvent,
    ExpectedResult,
    Scorecard,
)


def export_schemas(check: bool = False) -> list[str]:
    """Write the schema files. With `check`, write nothing and report what is stale."""
    problems = []
    SCHEMA_DIR.mkdir(exist_ok=True)
    for model in MODELS:
        path = SCHEMA_DIR / f"{model.__name__}.schema.json"
        text = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                problems.append(f"{path.name} is out of date: run export-schema")
        else:
            path.write_text(text, encoding="utf-8")
    return problems
