"""Tests for eval/determinism.py.

The run-level guard on invariant 4. A decomposition that bailed out on the
wall clock did not necessarily do the same thing it would do on another
machine, so a report containing one carries no byte-identical guarantee --
even though its own proof_hash is perfectly self-consistent.

Written before `eval/determinism.py` exists -- expected to fail on
collection until that module is implemented.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from core.decompose import (
    DecompositionBudget,
    DecompositionReason,
    DecompositionTier,
    decompose,
)
from core.ledger import Ledger
from core.models import BankCredit, BatchStatus, EntityType, Payment, PaymentMethod, SettlementBatch
from core.money import Money
from eval.determinism import (
    NonDeterministicRun,
    ReproducibilityReport,
    assert_reproducible,
    check_reproducibility,
)

UTC_NOON = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
DAY = date(2026, 7, 1)
VALUE_DATE = date(2026, 7, 3)
MERCHANT = "MERCH-0001"


class _FakeClock:
    def __init__(self, trip_after: int) -> None:
        self.calls = 0
        self.trip_after = trip_after

    def __call__(self) -> int:
        self.calls += 1
        return 10**18 if self.calls > self.trip_after else 0


def _world(count: int, *, utr_matches: bool = True):
    records: list = []
    total = 0
    for i in range(count):
        batch_id = f"STL-{i}"
        records.append(
            Payment(
                id=f"{batch_id}-PAY-0",
                merchant_id=MERCHANT,
                amount=Money(100_000 + i),
                method=PaymentMethod.UPI,
                network=None,
                card_type=None,
                is_international=False,
                mcc="5411",
                captured_at=UTC_NOON,
                settlement_id=batch_id,
            )
        )
        records.append(
            SettlementBatch(
                id=batch_id,
                merchant_id=MERCHANT,
                cycle_start=UTC_NOON,
                cycle_end=UTC_NOON,
                expected_credit=Money(100_000 + i),
                utr=f"UTR-{i}",
                status=BatchStatus.SETTLED,
            )
        )
        total += 100_000 + i
    utr = "UTR-0" if utr_matches else "UTR-NOWHERE"
    credit = BankCredit(
        id="BC-1",
        utr=utr,
        amount=Money(100_000 if utr_matches else total - 1),
        value_date=VALUE_DATE,
        narration="NEFT settlement",
    )
    return credit, Ledger([*records, credit])


def _clean_proof():
    credit, ledger = _world(1)
    return decompose(credit, ledger, merchant_id=MERCHANT), ledger


def _timed_out_proof():
    credit, ledger = _world(6, utr_matches=False)
    proof = decompose(credit, ledger, merchant_id=MERCHANT, clock=_FakeClock(trip_after=1))
    assert proof.reason is DecompositionReason.TIME_BUDGET_EXHAUSTED, "fixture did not time out"
    return proof


def _node_bound_proof():
    credit, ledger = _world(12, utr_matches=False)
    proof = decompose(credit, ledger, merchant_id=MERCHANT, budget=DecompositionBudget(max_nodes=3))
    assert proof.reason is DecompositionReason.NODE_BUDGET_EXHAUSTED, "fixture did not exhaust nodes"
    return proof


# ---------------------------------------------------------------------------
# check_reproducibility
# ---------------------------------------------------------------------------


def test_a_clean_run_is_reproducible():
    proof, _ = _clean_proof()
    report = check_reproducibility([proof])

    assert report.reproducible is True
    assert report.timed_out == []
    assert report.total == 1


def test_a_timed_out_proof_makes_the_run_irreproducible():
    report = check_reproducibility([_timed_out_proof()])

    assert report.reproducible is False
    assert report.timed_out == ["BC-1"]


def test_a_node_bound_proof_does_not_make_the_run_irreproducible():
    # The distinction the whole module rests on. Exhausting the node budget
    # is a decision the same inputs reach on any machine; exhausting the
    # wall clock is a decision this machine reached today. Only the second
    # costs us invariant 4.
    report = check_reproducibility([_node_bound_proof()])

    assert report.reproducible is True
    assert report.timed_out == []
    assert report.node_bound == ["BC-1"]


def test_report_counts_every_proof_it_saw():
    clean, _ = _clean_proof()
    report = check_reproducibility([clean, _timed_out_proof(), _node_bound_proof()])

    assert report.total == 3
    assert report.timed_out == ["BC-1"]
    assert report.node_bound == ["BC-1"]


def test_credit_ids_are_sorted_so_the_report_itself_is_stable():
    timed_out = _timed_out_proof()
    a = timed_out.model_copy(update={"credit_ref": timed_out.credit_ref.model_copy(update={"id": "BC-9"})})
    b = timed_out.model_copy(update={"credit_ref": timed_out.credit_ref.model_copy(update={"id": "BC-2"})})

    assert check_reproducibility([a, b]).timed_out == ["BC-2", "BC-9"]


def test_an_empty_run_is_vacuously_reproducible():
    report = check_reproducibility([])
    assert report.reproducible is True
    assert report.total == 0


def test_the_report_is_serialisable():
    proof, _ = _clean_proof()
    payload = check_reproducibility([proof]).model_dump(mode="json")
    assert payload["reproducible"] is True
    assert payload["total"] == 1


# ---------------------------------------------------------------------------
# assert_reproducible -- the gate eval/ calls
# ---------------------------------------------------------------------------


def test_assert_reproducible_passes_a_clean_run():
    proof, _ = _clean_proof()
    assert_reproducible([proof])  # must not raise


def test_assert_reproducible_raises_on_a_timeout():
    with pytest.raises(NonDeterministicRun):
        assert_reproducible([_timed_out_proof()])


def test_the_raised_error_names_the_offending_credits():
    with pytest.raises(NonDeterministicRun) as excinfo:
        assert_reproducible([_timed_out_proof()])

    assert "BC-1" in str(excinfo.value)


def test_assert_reproducible_allows_a_node_bound_run():
    assert_reproducible([_node_bound_proof()])  # must not raise


# ---------------------------------------------------------------------------
# Why a proof_hash is not enough on its own
# ---------------------------------------------------------------------------


def test_a_timed_out_proof_still_hashes_consistently():
    # The trap this module exists to close: a timed-out proof is perfectly
    # self-consistent and verifies fine. Nothing about the proof itself
    # reveals that another machine would have produced a different one --
    # only the reason code does.
    from core.decompose import verify_proof

    credit, ledger = _world(6, utr_matches=False)
    proof = decompose(credit, ledger, merchant_id=MERCHANT, clock=_FakeClock(trip_after=1))

    assert verify_proof(proof, ledger).ok is True
    assert proof.proof_hash
    assert check_reproducibility([proof]).reproducible is False


def test_report_is_typed_not_a_bare_dict():
    proof, _ = _clean_proof()
    assert isinstance(check_reproducibility([proof]), ReproducibilityReport)


# ---------------------------------------------------------------------------
# The tier a proof reached is irrelevant -- only the reason matters
# ---------------------------------------------------------------------------


def test_a_structural_resolution_is_always_reproducible():
    proof, _ = _clean_proof()
    assert proof.tier is DecompositionTier.STRUCTURAL
    assert check_reproducibility([proof]).reproducible is True


def test_only_the_time_budget_reason_is_treated_as_irreproducible():
    # Guards against a future reason code being lumped in by accident.
    proof, _ = _clean_proof()
    for reason in DecompositionReason:
        tampered = proof.model_copy(update={"reason": reason})
        expected = reason is not DecompositionReason.TIME_BUDGET_EXHAUSTED
        assert check_reproducibility([tampered]).reproducible is expected, reason


def test_bank_credit_type_is_what_the_report_keys_on():
    proof, _ = _clean_proof()
    assert proof.credit_ref.type is EntityType.BANK_CREDIT
