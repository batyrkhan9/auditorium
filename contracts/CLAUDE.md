# Shared: the data contract

Owned by both. Change it only on a `shared/` branch, with a task labelled
`contract-change`, after both humans agree. Read ../docs/CONTRACT.md first.

After changing a model: `uv run python -m auditorium_contracts export-schema`
and `uv run pytest contracts`.
