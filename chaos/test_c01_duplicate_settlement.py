"""C01 -- duplicate settlement file ingested twice.

Before this project's own review fixed it, `core/ledger.py::Ledger.__init__`
did `self._by_ref[ref] = record` unconditionally: a repeated record
silently last-write-won in the lookup table while every join index still
double-appended it regardless -- which could let a duplicated FeeLine get
cited twice in one proof's terms, inflating sum_paise. The fix: a repeated
(type, id) with IDENTICAL content is deduped (one copy kept, nothing
double-indexed, logged); a repeated id with CONFLICTING content raises
rather than guessing which copy is real.

This test duplicates one real record inside a settlement file on disk --
the literal shape of a file ingested twice -- and proves the audit still
completes cleanly with zero double-posted journal entries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chaos.incident import chaos_scenario
from core.ledger import DuplicateRecordError, Ledger
from core.models import Payment
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from store.resumable import resume_or_run

MERCHANT = "MERCH-0001"
REPO_ROOT = Path(__file__).resolve().parent.parent
# The committed fixture, not datagen -- chaos/ is under the same invariant
# 5 quarantine as core/, llm/, cli/: it must never import datagen.
CLEAN_RUN_DIR = REPO_ROOT / "runs" / "clean-seed42"


def _offline_provider() -> CachedProvider:
    return CachedProvider(NullProvider())


def _duplicate_one_payment(run_dir: Path, dest: Path) -> str:
    """Copies run_dir to dest, then duplicates the first payment record in
    ledger.json in place -- the literal shape of a settlement file that
    got ingested/merged twice. Returns the duplicated payment's id."""
    import shutil

    shutil.copytree(run_dir, dest)
    ledger_path = dest / "ledger.json"
    ledger_json = json.loads(ledger_path.read_text(encoding="utf-8"))
    duplicated_id = ledger_json["payments"][0]["id"]
    ledger_json["payments"].append(dict(ledger_json["payments"][0]))  # byte-identical duplicate
    ledger_path.write_text(json.dumps(ledger_json), encoding="utf-8")
    return duplicated_id


def test_c01_an_identical_duplicate_record_is_deduped_and_never_double_posted(tmp_path):
    with chaos_scenario(
        "C01",
        title="Duplicate settlement file ingested twice",
        category="money_corruption",
        failure_injected="one Payment record appears twice, byte-identical, in ledger.json",
        expected_behavior=(
            "Ledger dedupes the identical duplicate rather than double-indexing it; the audit "
            "completes normally and posts no double journal entries"
        ),
    ) as scenario:
        duplicated_dir = tmp_path / "duplicated-run"
        duplicated_id = _duplicate_one_payment(CLEAN_RUN_DIR, duplicated_dir)

        reference = resume_or_run(
            CLEAN_RUN_DIR, _offline_provider(), merchant_id=MERCHANT, store_path=tmp_path / "ref-store.db"
        )
        duplicated = resume_or_run(
            duplicated_dir, _offline_provider(), merchant_id=MERCHANT, store_path=tmp_path / "dup-store.db"
        )

        assert duplicated.record_count == reference.record_count, (
            "the duplicate must be deduped, not silently counted twice"
        )
        assert duplicated.total_unaccounted_paise == reference.total_unaccounted_paise
        assert len(duplicated.journal_entries) == len(reference.journal_entries)

        scenario.note(f"duplicated payment {duplicated_id}: record_count unchanged at {duplicated.record_count}")


def test_c01_a_conflicting_duplicate_is_refused_rather_than_guessed():
    with chaos_scenario(
        "C01B",
        title="Duplicate record id with conflicting content",
        category="money_corruption",
        failure_injected="two Payment records share an id but disagree on amount",
        expected_behavior="Ledger refuses to build (DuplicateRecordError) rather than last-write-wins",
    ) as scenario:
        payment_kwargs = {
            "id": "PAY-1",
            "merchant_id": MERCHANT,
            "method": "upi",
            "network": None,
            "card_type": None,
            "is_international": False,
            "mcc": "5411",
            "captured_at": "2026-07-10T12:00:00+05:30",
            "settlement_id": "STL-1",
        }
        original = Payment.model_validate({**payment_kwargs, "amount": {"paise": 1_000, "currency": "INR"}})
        conflicting = Payment.model_validate({**payment_kwargs, "amount": {"paise": 9_999, "currency": "INR"}})

        with pytest.raises(DuplicateRecordError, match="PAY-1"):
            Ledger([original, conflicting])

        scenario.note("conflicting duplicate correctly raised DuplicateRecordError rather than picking one")
