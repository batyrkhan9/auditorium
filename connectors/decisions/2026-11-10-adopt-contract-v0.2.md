# Connectors on contract v0.2

**Decided.** The connectors fill the v0.2 fields from what the sources really say:

- **Approvals.** `None` when Ramp cannot say: always on the MCP path, and on the
  Developer API path when the approvals scope or endpoint is refused. `[]` only when
  the approvals endpoint answered and holds no chain for the record. A chain still
  `PENDING` adds one pending step with no decision time. It names the approver when
  `next_approvers` has exactly one; with several or none it names nobody, and `raw`
  keeps the list.
- **People.** `platform_role` is Ramp's `role`. `role` comes from the company record
  with the same email (case-insensitive); Ramp's id and manager stay. No match, or an
  email two company records share, leaves `role` empty.
- **Synthetic.** True for Ramp's sandbox hosts (`demo-api`, `demo-mcp.ramp.com`), the
  stand-in, a QuickBooks `sandbox` company, and any local source whose manifest says
  so. `LocalFallback` now keeps the manifest's flag. It used to rebuild `SourceUse` and
  drop it.
- **QuickBooks reads.** Each line gets `account_type` from one `Account` query per
  client, matched by id and then by name. Journal lines get their own `vendor_name`.
  The entry gets `department`, and a Purchase gets `payment_type`. A line's
  `department` is only its own: Bill and Purchase lines have none.
- **QuickBooks loader.** A new account gets the dataset's `account_type`. Only a line
  without one falls back to the name guess. `payment_type` comes from the entry and
  never from `raw`. A guess stays only for pre-v0.2 data. The loader skips an entry
  when QuickBooks already holds one of its accounts with another type.
- **Mixed Ramp path.** `describe()` keeps what the run's data is (live bills, dataset
  cards and people) apart from what the read of the whole Ramp business found. The
  second group is mostly Ramp's own records, which the run ignores (111 PENDING
  chains, one 404 receipt). It is now labelled as such.

**Still not expressible in v0.2** (deferred in its round 3):
- A document Ramp lists but cannot serve. It stays off its transaction and is named
  in `describe()`, as before (see `2026-11-10-ramp-files-ramp-cannot-serve.md`).
- Values taken from the dataset in place of Ramp's. They are still recorded under
  `raw["auditorium_replaced_from_dataset"]`, which `describe()` now names.

**Considered.** Treating "no chain from the approvals endpoint" as `None`. We didn't:
the endpoint answered, and Ramp makes a chain for anything sent for approval. One
pending step per next approver. We didn't do that either: it would read as several
steps when Ramp means one step that any of them can approve.

**Found live (2026-11-10).** In the QuickBooks sandbox, "Accumulated Depreciation" is
an `Expense` account. The old loader guessed that type, and the dataset says
`Fixed Asset`. So 14 depreciation journal entries read back with that one line
typed differently from the dataset. Fixing it means editing the account in
QuickBooks, which is a sandbox write and a separate task.

**What would change our mind.** A captured Ramp chain whose `next_approvers` turns out
to mean sequential steps. Or a v0.2.x field for unavailable documents or for values
from the dataset.
