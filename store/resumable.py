"""Checkpoint/resume orchestration above cli.audit.run_audit().

`run_audit()` itself is untouched -- its signature and pure, stateless
behaviour stay exactly what eval/harness.py and eval/sweep.py already call
directly and repeatedly (eval/sweep.py's own determinism check relies on
two back-to-back, uncheckpointed calls). `resume_or_run()` is a separate
entry point, used only by `assay audit`, that reuses run_audit()'s own
building blocks (`cli.audit.decompose_phase`, `cli.audit._adjudicate`, and
every already-public core/ function run_audit() itself calls) so the two
never drift into two different sequencings of the same pipeline.

**What gets checkpointed, and why only these two phases.** decompose+its
integrity gates and adjudication are the only genuinely expensive and/or
network-bound phases; everything else (conserve, verify, clustering,
dispute packets, journal posting, report hashing) is cheap, pure, and
deterministic, so recomputing it costs nothing and avoids a second code
path that has to stay in sync with `AuditReport.compute_report_hash`.

**The actual money-safety guarantee.** `journal_entries_for_auto_findings`
is already a pure, idempotent function -- the new part here is that its
`already_posted` argument is now loaded from a real, durable table (not an
in-memory list that vanishes with the process), and the newly-computed
entries are INSERTed in the *same transaction* as the checkpoint's
phase="journal_done" update. If the process is killed between those two
things landing and the transaction committing, NEITHER lands: a resume
sees phase still at "adjudicate_done", reloads `already_posted=[]` (since
nothing committed), and journal_entries_for_auto_findings deterministically
recomputes and re-inserts the identical entries. Exactly-once posting falls
out of (idempotent pure function) + (atomic transaction) composing
together -- neither alone is sufficient.

**A documented limitation, not a silently-dropped one.** This assumes
`adjudicate`/`calibration`/`merchant_id`/`contract` don't change across
resume attempts for the same audit_run_id. run_audit() itself makes no
promise about what happens if you call it twice with different flags
either; resume_or_run() inherits that same contract rather than trying to
detect and react to a changed caller decision.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from cli.audit import AuditReport, _adjudicate, _git_commit, _read_seed, decompose_phase, hash_inputs
from cli.loaders import infer_merchant_id, load_bank_credits, load_contract, load_ledger
from core.conserve import conserve_all, total_unexplained_paise, unclaimed_paise, unclaimed_records
from core.contract import CompiledContract, canonical_json, sha256_of
from core.decompose import DecompositionProof
from core.exceptions import build_dispute_packet, cluster_findings
from core.lanes import CalibrationArtifact
from core.ledger import journal_entries_for_auto_findings
from core.models import IST, JournalEntry
from core.money import Money
from core.verify import verify_all
from eval.determinism import ReproducibilityReport
from llm.adjudicator import AdjudicationRun, findings_from_adjudication_run
from llm.provider import LLMProvider
from store.models import (
    PHASE_ADJUDICATE_DONE,
    PHASE_DECOMPOSE_DONE,
    PHASE_JOURNAL_DONE,
    PHASE_REPORT_DONE,
    AuditCheckpoint,
    AuditRunRow,
    JournalEntryRow,
)

FALLBACK_STORE_PATH = Path(".assay/audit_store.db")

_PHASE_ORDER = [PHASE_DECOMPOSE_DONE, PHASE_ADJUDICATE_DONE, PHASE_JOURNAL_DONE, PHASE_REPORT_DONE]


_TEST_PAUSE_ENV_VAR = "ASSAY_TEST_PAUSE_AFTER_DECOMPOSE_SECONDS"


def _test_pause_hook() -> None:
    """Test-only synchronization point, never set in production. A small
    fixture dataset can finish its whole pipeline before a chaos test's own
    poll-then-kill loop ever observes the decompose checkpoint, making a
    real OS-level "killed mid-audit" test flaky through no fault of the
    kill logic itself. chaos/test_c02_kill_and_resume.py sets this env var
    to reliably widen that window instead of needing a dataset large
    enough to make decompose slow on its own. Absent, this is a no-op."""
    raw = os.environ.get(_TEST_PAUSE_ENV_VAR)
    if raw:
        time.sleep(float(raw))


class CheckpointConflict(Exception):
    """A stored checkpoint's own input_hash disagrees with what run_dir's
    files hash to right now, under the same audit_run_id. audit_run_id is
    only the first 12 hex characters of input_hash, so this can only mean
    an astronomically unlikely prefix collision -- refuses to resume with
    the wrong proofs rather than silently trusting a 12-hex-char match."""


def default_store_path() -> Path:
    """Resolved per call, same pattern as llm/providers/cached.py's
    default_cache_dir() -- a test can redirect ASSAY_STORE_PATH without
    reloading this module."""
    load_dotenv()
    return Path(os.environ.get("ASSAY_STORE_PATH") or FALLBACK_STORE_PATH)


_ENGINES: dict[str, object] = {}


def get_engine(path: Path):
    """A real file, always -- never :memory:. The chaos scenario this
    package exists for spans two separate `python -m cli audit` processes
    (one killed, one resumed), which share a filesystem path but never a
    memory space, so an in-memory database would silently defeat the one
    thing this module is for."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    engine = _ENGINES.get(key)
    if engine is not None:
        return engine
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.commit()
    SQLModel.metadata.create_all(engine)
    _ENGINES[key] = engine
    return engine


def _phase_at_least(checkpoint: AuditCheckpoint | None, phase: str) -> bool:
    if checkpoint is None:
        return False
    return _PHASE_ORDER.index(checkpoint.phase) >= _PHASE_ORDER.index(phase)


def _serialize_proofs(proofs: list[DecompositionProof]) -> str:
    return canonical_json([p.model_dump(mode="json") for p in proofs])


def _deserialize_proofs(payload: str) -> list[DecompositionProof]:
    return [DecompositionProof.model_validate(p) for p in json.loads(payload)]


def _row_to_journal_entry(row: JournalEntryRow) -> JournalEntry:
    return JournalEntry(
        id=row.id,
        debit_account=row.debit_account,
        credit_account=row.credit_account,
        amount=Money(row.amount_paise, row.amount_currency),
        narration=row.narration,
        idempotency_key=row.idempotency_key,
        posted_at=row.posted_at,
    )


def _entry_to_row(entry: JournalEntry, audit_run_id: str) -> JournalEntryRow:
    return JournalEntryRow(
        id=entry.id,
        audit_run_id=audit_run_id,
        idempotency_key=entry.idempotency_key,
        debit_account=entry.debit_account,
        credit_account=entry.credit_account,
        amount_paise=entry.amount.paise,
        amount_currency=entry.amount.currency,
        narration=entry.narration,
        posted_at=entry.posted_at.isoformat(),
    )


def _get_or_create_checkpoint(session: Session, audit_run_id: str, input_hash: str) -> AuditCheckpoint:
    checkpoint = session.get(AuditCheckpoint, audit_run_id)
    now = datetime.now(IST).isoformat()
    if checkpoint is None:
        return AuditCheckpoint(
            audit_run_id=audit_run_id,
            input_hash=input_hash,
            phase=PHASE_DECOMPOSE_DONE,  # overwritten by the caller before commit
            created_at=now,
            updated_at=now,
        )
    return checkpoint


def resume_or_run(
    run_dir: Path | str,
    provider: LLMProvider | None = None,
    *,
    merchant_id: str | None = None,
    calibration: CalibrationArtifact | None = None,
    contract: CompiledContract | None = None,
    adjudicate: bool = True,
    store_path: Path | str | None = None,
) -> AuditReport:
    """Audit one run directory, resuming from a durable checkpoint if a
    prior, interrupted invocation over these exact same input files left
    one. See the module docstring for what "resuming" actually skips.
    """
    start_ns = time.monotonic_ns()
    run_dir = Path(run_dir)
    engine = get_engine(Path(store_path) if store_path is not None else default_store_path())

    input_hashes = hash_inputs(run_dir)
    input_hash = sha256_of(canonical_json(input_hashes))
    audit_run_id = f"AUD-{input_hash[:12]}"

    with Session(engine) as session:
        checkpoint = session.get(AuditCheckpoint, audit_run_id)
        if checkpoint is not None and checkpoint.input_hash != input_hash:
            raise CheckpointConflict(
                f"{audit_run_id}: a stored checkpoint's input_hash does not match run_dir's current "
                "files -- this can only be a 12-hex-character audit_run_id prefix collision"
            )

        if contract is not None:
            prior_run = session.get(AuditRunRow, audit_run_id)
            if prior_run is not None and prior_run.contract_version != contract.version_id:
                raise CheckpointConflict(
                    f"{audit_run_id}: a previously completed audit of this run_dir used contract "
                    f"{prior_run.contract_version!r}; a different contract {contract.version_id!r} was "
                    "pinned for this run -- refusing to reuse this run_dir's checkpoint/journal "
                    "history under a different contract, since audit_run_id (and so the journal's "
                    "idempotency keys) is derived only from run_dir's input files, not the contract"
                )

        ledger = load_ledger(run_dir)
        credits = load_bank_credits(run_dir)
        if merchant_id is None:
            merchant_id = infer_merchant_id(ledger)
        if contract is None:
            if provider is None:
                raise ValueError(
                    "resume_or_run needs either a provider (to compile the rate card) or an "
                    "already-compiled contract; an audit with no contract has nothing to recompute "
                    "deductions against"
                )
            contract = load_contract(run_dir, provider)

        if _phase_at_least(checkpoint, PHASE_DECOMPOSE_DONE):
            if sha256_of(checkpoint.proofs_json) != checkpoint.proofs_sha256:
                raise CheckpointConflict(
                    f"{audit_run_id}: checkpointed proofs failed their own stored integrity hash"
                )
            proofs = _deserialize_proofs(checkpoint.proofs_json)
            reproducibility = ReproducibilityReport.model_validate_json(checkpoint.reproducibility_json)
        else:
            proofs, reproducibility = decompose_phase(
                credits, ledger, merchant_id=merchant_id, calibration=calibration
            )
            row = _get_or_create_checkpoint(session, audit_run_id, input_hash)
            proofs_json = _serialize_proofs(proofs)
            row.phase = PHASE_DECOMPOSE_DONE
            row.proofs_json = proofs_json
            row.proofs_sha256 = sha256_of(proofs_json)
            row.reproducibility_json = reproducibility.model_dump_json()
            row.updated_at = datetime.now(IST).isoformat()
            session.add(row)
            session.commit()
            checkpoint = row
            _test_pause_hook()

        conservation = conserve_all(proofs, ledger)
        unclaimed = unclaimed_records(ledger, proofs)
        findings, contract_gaps = verify_all(
            proofs, ledger, contract, audit_run_id=audit_run_id, calibration=calibration
        )
        findings = list(findings)

        adjudication: AdjudicationRun | None = None
        degraded_kind: str | None = None
        degraded_reason: str | None = None
        if adjudicate and provider is not None:
            if _phase_at_least(checkpoint, PHASE_ADJUDICATE_DONE):
                degraded_kind = checkpoint.adjudication_degraded_kind
                degraded_reason = checkpoint.adjudication_degraded_reason
                if checkpoint.adjudication_json is not None:
                    adjudication = AdjudicationRun.model_validate_json(checkpoint.adjudication_json)
                    findings.extend(findings_from_adjudication_run(adjudication, audit_run_id))
            else:
                adjudication, adjudicated_findings, degraded_kind, degraded_reason = _adjudicate(
                    proofs, conservation, ledger, unclaimed, provider, audit_run_id
                )
                findings.extend(adjudicated_findings)
                row = _get_or_create_checkpoint(session, audit_run_id, input_hash)
                row.phase = PHASE_ADJUDICATE_DONE
                row.adjudication_json = adjudication.model_dump_json() if adjudication is not None else None
                row.adjudication_degraded_kind = degraded_kind
                row.adjudication_degraded_reason = degraded_reason
                row.updated_at = datetime.now(IST).isoformat()
                session.add(row)
                session.commit()
                checkpoint = row

        findings.sort(key=lambda f: f.id)
        clusters = cluster_findings(findings, ledger, audit_run_id=audit_run_id)
        packets = [build_dispute_packet(cluster, findings, contract) for cluster in clusters]

        started_at = datetime.now(IST)
        posted_at = started_at

        existing_rows = session.exec(
            select(JournalEntryRow).where(JournalEntryRow.audit_run_id == audit_run_id)
        ).all()
        already_posted = [_row_to_journal_entry(r) for r in existing_rows]

        if _phase_at_least(checkpoint, PHASE_JOURNAL_DONE):
            journal_entries = already_posted
        else:
            new_entries = journal_entries_for_auto_findings(
                findings, input_hash=input_hash, already_posted=already_posted, posted_at=posted_at
            )
            row = _get_or_create_checkpoint(session, audit_run_id, input_hash)
            row.phase = PHASE_JOURNAL_DONE
            row.updated_at = datetime.now(IST).isoformat()
            for entry in new_entries:
                session.add(_entry_to_row(entry, audit_run_id))
            session.add(row)
            session.commit()  # new journal rows + phase="journal_done", one transaction
            checkpoint = row
            journal_entries = already_posted + new_entries

        unexplained = total_unexplained_paise(conservation)
        unclaimed_total = unclaimed_paise(ledger, proofs)

        report = AuditReport(
            audit_run_id=audit_run_id,
            run_dir=str(run_dir),
            seed=_read_seed(run_dir),
            merchant_id=merchant_id,
            contract_version=contract.version_id,
            contract_source_sha256=contract.source_sha256,
            calibration_sha256=calibration.artifact_sha256 if calibration is not None else None,
            rounding_policy=contract.rounding.value,
            input_hashes=input_hashes,
            input_hash=input_hash,
            proofs=proofs,
            conservation=conservation,
            unclaimed=unclaimed,
            total_unexplained_paise=unexplained,
            unclaimed_paise=unclaimed_total,
            total_unaccounted_paise=unexplained + unclaimed_total,
            findings=findings,
            contract_gaps=contract_gaps,
            clusters=clusters,
            dispute_packets=packets,
            journal_entries=journal_entries,
            posted_at=posted_at,
            adjudication=adjudication,
            adjudication_degraded=degraded_kind is not None,
            adjudication_degraded_kind=degraded_kind,
            adjudication_degraded_reason=degraded_reason,
            reproducibility=reproducibility,
            record_count=len(ledger),
            started_at=started_at.isoformat(),
            wall_clock_ns=time.monotonic_ns() - start_ns,
            git_sha=_git_commit(),
        )
        report = report.model_copy(update={"report_hash": report.compute_report_hash()})

        row = _get_or_create_checkpoint(session, audit_run_id, input_hash)
        row.phase = PHASE_REPORT_DONE
        row.report_hash = report.report_hash
        row.updated_at = datetime.now(IST).isoformat()
        session.add(row)
        session.merge(
            AuditRunRow(
                id=audit_run_id,
                started_at=report.started_at,
                contract_version=report.contract_version,
                input_hashes_json=canonical_json(input_hashes),
                seed=report.seed,
                report_hash=report.report_hash,
            )
        )
        session.commit()

        return report
