"""Tests for store/resumable.py -- checkpoint/resume for `assay audit`.

Money-critical, written test-first per the working agreement. The property
that actually matters is invariant 7's "never double-post": a process
killed after decompose but before journal-posting completes must, on
restart, neither redo the (already-persisted) decompose work nor post a
journal entry twice. `chaos/` has the real OS-level subprocess-kill version
of this; the tests here simulate the kill in-process (raising partway
through resume_or_run's own sequencing) so the DB-state assertions can be
made directly and fast, without spawning a process.
"""

from __future__ import annotations

from pathlib import Path
from random import Random

import pytest
from sqlmodel import Session, select

from cli.audit import run_audit
from datagen.config import GenerationConfig, load_profile
from datagen.inject import apply_discrepancies
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world
from datagen.writer import write_run
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from store import resumable
from store.models import PHASE_DECOMPOSE_DONE, PHASE_REPORT_DONE, AuditCheckpoint, JournalEntryRow
from store.resumable import CheckpointConflict, get_engine, resume_or_run

MERCHANT = "MERCH-0001"


def _offline_provider() -> CachedProvider:
    return CachedProvider(NullProvider())


@pytest.fixture(scope="module")
def clean_run_dir(tmp_path_factory) -> Path:
    config = GenerationConfig(month="2026-07")
    rate_card = default_rate_card(config.month)
    true_world = build_true_world(config, rate_card, Random(42))
    profile = load_profile("clean")
    reported_world, discrepancies, _flags = apply_discrepancies(true_world, rate_card, profile, Random(43))
    assert discrepancies == [], "sanity check on the fixture itself"

    out_dir = tmp_path_factory.mktemp("clean") / "clean-seed42"
    write_run(
        reported_world,
        rate_card,
        manifest={"run_id": "clean-seed42", "seed": 42, "profile": "clean"},
        out_dir=out_dir,
        merchant_id=config.merchant_id,
    )
    return out_dir


def _checkpoints(store_path: Path) -> list[AuditCheckpoint]:
    with Session(get_engine(store_path)) as session:
        return list(session.exec(select(AuditCheckpoint)).all())


def _journal_rows(store_path: Path) -> list[JournalEntryRow]:
    with Session(get_engine(store_path)) as session:
        return list(session.exec(select(JournalEntryRow)).all())


@pytest.mark.timeout(120)
def test_a_fresh_run_matches_run_audits_own_report_hash(clean_run_dir, tmp_path):
    reference = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT)
    resumed = resume_or_run(
        clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=tmp_path / "store.db"
    )
    assert resumed.report_hash == reference.report_hash


@pytest.mark.timeout(120)
def test_a_fresh_run_leaves_exactly_one_report_done_checkpoint(clean_run_dir, tmp_path):
    store_path = tmp_path / "store.db"
    resume_or_run(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path)

    rows = _checkpoints(store_path)
    assert len(rows) == 1
    assert rows[0].phase == PHASE_REPORT_DONE


@pytest.mark.timeout(120)
def test_a_failure_after_decompose_leaves_a_decompose_done_checkpoint_only(clean_run_dir, tmp_path, monkeypatch):
    store_path = tmp_path / "store.db"

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated kill after decompose committed")

    monkeypatch.setattr(resumable, "_adjudicate", _boom)

    with pytest.raises(RuntimeError, match="simulated kill"):
        resume_or_run(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path)

    rows = _checkpoints(store_path)
    assert len(rows) == 1
    assert rows[0].phase == PHASE_DECOMPOSE_DONE
    assert _journal_rows(store_path) == [], "nothing must be posted before decompose even finishes"


@pytest.mark.timeout(120)
def test_a_resume_after_a_kill_does_not_redo_decompose_and_still_reaches_the_same_hash(
    clean_run_dir, tmp_path, monkeypatch
):
    store_path = tmp_path / "store.db"
    reference = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT)

    real_decompose_phase = resumable.decompose_phase
    calls = {"n": 0}

    def _counting_decompose_phase(*args, **kwargs):
        calls["n"] += 1
        return real_decompose_phase(*args, **kwargs)

    monkeypatch.setattr(resumable, "decompose_phase", _counting_decompose_phase)
    monkeypatch.setattr(resumable, "_adjudicate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("killed")))

    with pytest.raises(RuntimeError, match="killed"):
        resume_or_run(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path)
    assert calls["n"] == 1

    monkeypatch.undo()  # "restart": the real decompose_phase/_adjudicate are back

    resumed = resume_or_run(
        clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path
    )

    assert calls["n"] == 1, "decompose must not be recomputed on a resume that finds a checkpoint"
    assert resumed.report_hash == reference.report_hash


@pytest.mark.timeout(120)
def test_running_twice_over_unchanged_inputs_never_posts_a_journal_entry_twice(clean_run_dir, tmp_path):
    store_path = tmp_path / "store.db"

    first = resume_or_run(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path)
    rows_after_first = _journal_rows(store_path)

    second = resume_or_run(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path)
    rows_after_second = _journal_rows(store_path)

    assert [r.id for r in rows_after_first] == [r.id for r in rows_after_second]
    assert len(rows_after_second) == len({r.idempotency_key for r in rows_after_second})
    assert [e.id for e in first.journal_entries] == [e.id for e in second.journal_entries]


@pytest.mark.timeout(120)
def test_a_corrupted_checkpointed_proof_is_detected_on_the_next_resume(clean_run_dir, tmp_path):
    store_path = tmp_path / "store.db"
    engine = get_engine(store_path)

    with pytest.MonkeyPatch.context() as mp:
        def _boom(*args, **kwargs):
            raise RuntimeError("killed")

        mp.setattr(resumable, "_adjudicate", _boom)
        with pytest.raises(RuntimeError, match="killed"):
            resume_or_run(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path)

    with Session(engine) as session:
        row = session.exec(select(AuditCheckpoint)).one()
        row.proofs_json = row.proofs_json.replace('"tier"', '"TIER"')  # corrupt without changing length shape
        session.add(row)
        session.commit()

    with pytest.raises(CheckpointConflict, match="integrity"):
        resume_or_run(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path)


@pytest.mark.timeout(120)
def test_reaudit_with_a_different_pinned_contract_refuses_rather_than_mixing_stale_journal_entries(
    clean_run_dir, tmp_path
):
    """assay audit --contract (added alongside this test) lets a caller pin
    an arbitrary CompiledContract, which can differ across two calls even
    though audit_run_id -- derived only from run_dir's 4 input files -- does
    not. Without a guard, a second run under a different (but still
    individually valid) pinned contract would recompute findings/clusters
    fresh under the new contract while resume_or_run's phase_at_least(...,
    PHASE_JOURNAL_DONE) branch (store/resumable.py) reuses the FIRST run's
    already-posted journal entries verbatim -- an internally inconsistent
    report where findings and postings disagree about which contract
    priced them. Refusing is correct: there is no single right way to
    reconcile two different contracts' journal history for one run_dir."""
    from cli.contract import compile_ratecard_file
    from core.contract import CompiledContract, RateCardParse

    store_path = tmp_path / "store.db"

    v1, _ = compile_ratecard_file(clean_run_dir / "rate_card.md", _offline_provider(), out=tmp_path / "v1.json")
    first = resume_or_run(
        clean_run_dir, _offline_provider(), merchant_id=MERCHANT, contract=v1, store_path=store_path
    )
    assert first.contract_version == v1.version_id

    v2 = CompiledContract.from_parse(RateCardParse(merchant_id=MERCHANT, rules=[]), source_sha256=v1.source_sha256)
    assert v2.version_id != v1.version_id, "sanity check: the fixture must actually differ from v1"

    with pytest.raises(CheckpointConflict, match="contract"):
        resume_or_run(
            clean_run_dir, _offline_provider(), merchant_id=MERCHANT, contract=v2, store_path=store_path
        )


@pytest.mark.timeout(120)
def test_an_input_hash_mismatch_under_the_same_audit_run_id_is_a_checkpoint_conflict(clean_run_dir, tmp_path):
    # Simulates the only way this could happen: an astronomically unlikely
    # 12-hex-char audit_run_id prefix collision between two different
    # input_hash values. Constructed directly rather than found by chance.
    store_path = tmp_path / "store.db"
    resume_or_run(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path)

    engine = get_engine(store_path)
    with Session(engine) as session:
        row = session.exec(select(AuditCheckpoint)).one()
        row.input_hash = "0" * 64
        session.add(row)
        session.commit()

    with pytest.raises(CheckpointConflict, match="collision"):
        resume_or_run(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, store_path=store_path)
