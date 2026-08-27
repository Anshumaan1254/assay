"""SQLModel tables backing checkpoint/resume and the durable AUTO-lane
journal.

Three tables, kept separate on purpose:

- `AuditCheckpoint` — resumability metadata. Transient: only needed until
  an audit_run_id reaches `report_done`, after which nothing reads it
  again (a fresh run over the same, unchanged inputs recomputes the same
  audit_run_id and just overwrites its own prior checkpoint).
- `JournalEntry` — the actual money-safety table. `idempotency_key` is
  UNIQUE at the database layer, not just deduplicated by the caller's own
  in-memory set (`core.ledger.journal_entries_for_auto_findings` already
  does that) — a caller bug that forgets to load `already_posted` fails
  loudly with an IntegrityError here, instead of silently double-posting.
- `AuditRun` — the small, permanent audit-trail row
  `cli.audit.AuditReport.audit_run_record()` already knows how to build;
  it was simply never persisted anywhere until this package existed.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

# Every timestamp below is stored as an ISO-8601 string (datetime.isoformat()
# / core.models.ISTDatetime's own str form), never a native SQL datetime
# column -- SQLite has no real datetime type, and SQLAlchemy's roundtrip of
# a timezone-AWARE datetime through it is not reliable enough to trust for
# a value that gets fed straight back into a pydantic ISTDatetime field
# (which raises on a naive datetime). cli/audit.py::AuditReport already
# stores `started_at` the same way, for the same reason.

# decompose_done -> adjudicate_done -> journal_done -> report_done.
# A resume skips exactly the phases already _done; report_done is the
# terminal phase, after which the checkpoint row is inert history.
PHASE_DECOMPOSE_DONE = "decompose_done"
PHASE_ADJUDICATE_DONE = "adjudicate_done"
PHASE_JOURNAL_DONE = "journal_done"
PHASE_REPORT_DONE = "report_done"


class AuditCheckpoint(SQLModel, table=True):
    __tablename__ = "audit_checkpoints"

    audit_run_id: str = Field(primary_key=True)
    input_hash: str
    phase: str

    proofs_json: str | None = None
    proofs_sha256: str | None = None
    reproducibility_json: str | None = None

    adjudication_json: str | None = None
    adjudication_degraded_kind: str | None = None
    adjudication_degraded_reason: str | None = None

    report_hash: str | None = None

    created_at: str
    updated_at: str


class JournalEntryRow(SQLModel, table=True):
    __tablename__ = "journal_entries"

    id: str = Field(primary_key=True)
    audit_run_id: str = Field(index=True)
    idempotency_key: str = Field(unique=True, index=True)
    debit_account: str
    credit_account: str
    amount_paise: int
    amount_currency: str
    narration: str
    posted_at: str


class AuditRunRow(SQLModel, table=True):
    __tablename__ = "audit_runs"

    id: str = Field(primary_key=True)
    started_at: str
    contract_version: str
    input_hashes_json: str
    seed: int
    report_hash: str
