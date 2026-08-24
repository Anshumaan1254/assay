"""Tests for core/decompose.py.

Money-critical: this module decides which transactions produced a bank
credit. Every downstream number -- recomputed fees, the conservation
identity, the unexplained bucket -- is computed over the record set it
returns. A wrong subset is a wrong audit.

Written before `core/decompose.py` exists -- expected to fail on collection
until that module is implemented.

The failure mode most of these tests exist to prevent is *silently
choosing*: when more than one transaction set explains a credit, the engine
must name the competitors and refuse to pick. A test that asserts only
"resolved correctly" would pass on an implementation that guesses.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from core.decompose import (
    SETTLEMENT_LAG_DAYS,
    DecompositionBudget,
    DecompositionOutcome,
    DecompositionProof,
    DecompositionReason,
    DecompositionTier,
    ProofTerm,
    decompose,
    decompose_all,
    verify_proof,
)
from core.ledger import Ledger
from core.models import (
    Adjustment,
    BankCredit,
    BatchStatus,
    Chargeback,
    EntityType,
    FeeLine,
    FeeType,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
    SettlementBatch,
    TaxLine,
)
from core.money import Money

MERCHANT = "MERCH-0001"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC)


def _make_batch(
    batch_id: str,
    utr: str,
    cycle_day: date,
    payments: Sequence[int],
    *,
    fees: Sequence[int] = (),
    taxes: Sequence[int] = (),
    merchant_id: str = MERCHANT,
) -> tuple[list, int]:
    """One settlement batch and every record that nets into it.

    `fees[i]` attaches to `payments[i]`; `taxes[i]` attaches to `fees[i]`.
    Ids are namespaced by batch_id so several batches can share a ledger.
    Returns (records, net_paise).
    """
    at = _at(cycle_day)
    records: list = []
    net = 0

    for i, paise in enumerate(payments):
        records.append(
            Payment(
                id=f"{batch_id}-PAY-{i}",
                merchant_id=merchant_id,
                amount=Money(paise),
                method=PaymentMethod.UPI,
                network=None,
                card_type=None,
                is_international=False,
                mcc="5411",
                captured_at=at,
                settlement_id=batch_id,
            )
        )
        net += paise

    for i, paise in enumerate(fees):
        records.append(
            FeeLine(
                id=f"{batch_id}-FEE-{i}",
                applies_to_id=f"{batch_id}-PAY-{i}",
                applies_to_type=EntityType.PAYMENT,
                fee_type=FeeType.MDR,
                computed_amount=Money(paise),
                rule_id="rule_1",
            )
        )
        net -= paise

    for i, paise in enumerate(taxes):
        records.append(
            TaxLine(
                id=f"{batch_id}-TAX-{i}",
                applies_to_fee_id=f"{batch_id}-FEE-{i}",
                tax_type="GST",
                rate_bps=1800,
                base_amount=Money(fees[i]),
                amount=Money(paise),
            )
        )
        net -= paise

    records.append(
        SettlementBatch(
            id=batch_id,
            merchant_id=merchant_id,
            cycle_start=at,
            cycle_end=at,
            expected_credit=Money(net),
            utr=utr,
            status=BatchStatus.SETTLED,
        )
    )
    return records, net


def _credit(
    id_: str,
    utr: str,
    paise: int,
    value_date: date,
    *,
    narration: str | None = None,
    currency: str = "INR",
) -> BankCredit:
    return BankCredit(
        id=id_,
        utr=utr,
        amount=Money(paise, currency),
        value_date=value_date,
        narration=narration if narration is not None else f"NEFT-HDFC0001234-Settlement {utr}",
    )


def _orphan_payment(id_: str, paise: int, day: date, merchant_id: str = MERCHANT) -> Payment:
    """A captured payment linked to no settlement -- datagen's D08 shape."""
    return Payment(
        id=id_,
        merchant_id=merchant_id,
        amount=Money(paise),
        method=PaymentMethod.UPI,
        network=None,
        card_type=None,
        is_international=False,
        mcc="5411",
        captured_at=_at(day),
        settlement_id=None,
    )


def _ref(type_: EntityType, id_: str) -> RecordRef:
    return RecordRef(type=type_, id=id_)


class _FakeClock:
    """Stand-in for time.monotonic_ns. Returns 0 for the first `trip_after`
    calls, then a value past any plausible budget. Integer-only: core/ may
    not contain a float literal (tests/test_architecture.py)."""

    def __init__(self, trip_after: int) -> None:
        self.calls = 0
        self.trip_after = trip_after

    def __call__(self) -> int:
        self.calls += 1
        return 10**18 if self.calls > self.trip_after else 0


DAY = date(2026, 7, 1)
VALUE_DATE = DAY + timedelta(days=2)


# ---------------------------------------------------------------------------
# Tier 1 -- structural join on the UTR
# ---------------------------------------------------------------------------


def test_exact_case_resolves_structurally():
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000, 300_000], fees=[11_800, 7_080], taxes=[2_124, 1_274])
    credit = _credit("BC-1", "UTR-1", net, VALUE_DATE)
    ledger = Ledger([*records, credit])

    proof = decompose(credit, ledger, merchant_id=MERCHANT)

    assert proof.tier is DecompositionTier.STRUCTURAL
    assert proof.outcome is DecompositionOutcome.RESOLVED
    assert proof.residual_paise == 0
    assert proof.sum_paise == net
    assert proof.confidence == 10_000
    assert proof.reason is None


def test_structural_proof_cites_every_contributing_record_and_only_those():
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000], fees=[11_800], taxes=[2_124])
    credit = _credit("BC-1", "UTR-1", net, VALUE_DATE)
    ledger = Ledger([*records, credit])

    proof = decompose(credit, ledger, merchant_id=MERCHANT)

    assert {t.ref for t in proof.terms} == {
        _ref(EntityType.PAYMENT, "STL-1-PAY-0"),
        _ref(EntityType.FEE_LINE, "STL-1-FEE-0"),
        _ref(EntityType.TAX_LINE, "STL-1-TAX-0"),
    }
    # Neither the batch nor the credit is a term: the batch is a grouping,
    # not a money movement, and including the credit would net to zero and
    # "balance" while explaining nothing.
    cited_types = {t.ref.type for t in proof.terms}
    assert EntityType.SETTLEMENT_BATCH not in cited_types
    assert EntityType.BANK_CREDIT not in cited_types


def test_fee_and_tax_lines_are_reached_transitively_not_via_settlement_id():
    # FeeLine and TaxLine have no settlement_id. An implementation built
    # only on Ledger.by_settlement() would omit every deduction and report
    # the gross as the credit.
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000], fees=[11_800], taxes=[2_124])
    credit = _credit("BC-1", "UTR-1", net, VALUE_DATE)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT)

    assert proof.sum_paise == 500_000 - 11_800 - 2_124
    assert proof.sum_paise != 500_000, "deductions were not applied"


def test_short_settled_batch_resolves_with_a_residual_and_does_not_cascade():
    # The decision that matters most in this module: a structural link that
    # nets to the wrong number is still the right record set. Cascading here
    # would let tier 2 find some other subset that happens to net exactly,
    # and the shortfall -- the actual finding -- would vanish from the report.
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000], fees=[11_800], taxes=[2_124])
    credit = _credit("BC-1", "UTR-1", net - 5_000, VALUE_DATE)
    ledger = Ledger([*records, credit])

    proof = decompose(credit, ledger, merchant_id=MERCHANT)

    assert proof.tier is DecompositionTier.STRUCTURAL
    assert proof.outcome is DecompositionOutcome.RESOLVED
    assert proof.residual_paise == -5_000
    assert proof.tiers_attempted == [DecompositionTier.STRUCTURAL]
    assert proof.confidence == 10_000, "a UTR join is certain even when the amount is short"


def test_residual_sign_follows_the_conservation_identity():
    # credit = signed_sum + unexplained, so residual = credit - signed_sum.
    # An over-payment is positive, a short-payment negative.
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    credit = _credit("BC-1", "UTR-1", net + 900, VALUE_DATE)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT)

    assert proof.residual_paise == 900
    assert proof.sum_paise + proof.residual_paise == proof.credit_paise


def test_blank_utr_falls_through_to_tier_two():
    records, net = _make_batch("STL-1", "", DAY, [500_000])
    credit = _credit("BC-1", "", net, VALUE_DATE)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT)

    assert DecompositionTier.STRUCTURAL in proof.tiers_attempted
    assert proof.tier is not DecompositionTier.STRUCTURAL


def test_two_batches_sharing_a_utr_do_not_resolve_structurally():
    # Picking either one would be a silent choice.
    a, net_a = _make_batch("STL-1", "UTR-DUP", DAY, [500_000])
    b, _ = _make_batch("STL-2", "UTR-DUP", DAY, [400_000])
    credit = _credit("BC-1", "UTR-DUP", net_a, VALUE_DATE)

    proof = decompose(credit, Ledger([*a, *b, credit]), merchant_id=MERCHANT)

    assert proof.tier is not DecompositionTier.STRUCTURAL
    assert DecompositionReason.STRUCTURAL_KEY_AMBIGUOUS in (proof.reason, *_tier_reasons(proof))


def _tier_reasons(proof: DecompositionProof) -> tuple:
    """Reasons recorded for tiers that were attempted and declined."""
    return tuple(proof.declined_reasons)


def test_a_credit_in_a_different_currency_is_never_decomposed():
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    credit = _credit("BC-1", "UTR-1", net, VALUE_DATE, currency="USD")

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT)

    assert proof.outcome is DecompositionOutcome.UNRESOLVED
    assert proof.reason is DecompositionReason.CURRENCY_MISMATCH
    assert proof.terms == []


# ---------------------------------------------------------------------------
# Tier 2 -- constrained subset reconstruction
# ---------------------------------------------------------------------------


def test_truncated_utr_resolves_at_tier_two():
    # datagen's D09 truncates BankCredit.utr on 6 of 31 credits. Tier 1 uses
    # exact equality only -- repairing a corrupt key with a prefix match
    # inside the most-trusted tier would hide the data-quality problem.
    records, net = _make_batch("STL-1", "HDFC000123420260701000000", DAY, [500_000], fees=[11_800])
    credit = _credit("BC-1", "HDFC00012342", net, VALUE_DATE)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT)

    assert proof.tiers_attempted == [DecompositionTier.STRUCTURAL, DecompositionTier.SUBSET_SUM]
    assert proof.tier is DecompositionTier.SUBSET_SUM
    assert proof.outcome is DecompositionOutcome.RESOLVED
    assert proof.residual_paise == 0
    assert proof.confidence == 8_000
    assert {t.ref for t in proof.terms} == {
        _ref(EntityType.PAYMENT, "STL-1-PAY-0"),
        _ref(EntityType.FEE_LINE, "STL-1-FEE-0"),
    }


def test_missing_settlement_id_resolves_from_orphan_payments():
    orphans = [
        _orphan_payment("PAY-A", 120_000, DAY),
        _orphan_payment("PAY-B", 250_000, DAY),
        _orphan_payment("PAY-C", 999_999, DAY),
    ]
    credit = _credit("BC-1", "UTR-UNKNOWN", 120_000 + 250_000, VALUE_DATE)

    proof = decompose(credit, Ledger([*orphans, credit]), merchant_id=MERCHANT)

    assert proof.tier is DecompositionTier.SUBSET_SUM
    assert proof.outcome is DecompositionOutcome.RESOLVED
    assert {t.ref for t in proof.terms} == {
        _ref(EntityType.PAYMENT, "PAY-A"),
        _ref(EntityType.PAYMENT, "PAY-B"),
    }
    assert proof.subset_size == 2


def test_merged_credit_resolves_to_two_whole_batches():
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000], fees=[11_800])
    b, net_b = _make_batch("STL-2", "UTR-2", DAY + timedelta(days=1), [300_000], fees=[7_080])
    credit = _credit("BC-MERGED", "UTR-NEW", net_a + net_b, VALUE_DATE + timedelta(days=1))

    proof = decompose(credit, Ledger([*a, *b, credit]), merchant_id=MERCHANT)

    assert proof.tier is DecompositionTier.SUBSET_SUM
    assert proof.outcome is DecompositionOutcome.RESOLVED
    assert proof.subset_size == 2
    assert proof.residual_paise == 0
    assert {t.ref for t in proof.terms} == {
        _ref(EntityType.PAYMENT, "STL-1-PAY-0"),
        _ref(EntityType.FEE_LINE, "STL-1-FEE-0"),
        _ref(EntityType.PAYMENT, "STL-2-PAY-0"),
        _ref(EntityType.FEE_LINE, "STL-2-FEE-0"),
    }


def test_negative_units_are_searchable():
    # A standalone refund is a negative unit. A positive-only subset-sum DP
    # silently cannot reach a target that requires subtracting one.
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    refund = Refund(
        id="REF-STANDALONE",
        payment_id="STL-1-PAY-0",
        amount=Money(25_000),
        is_partial=False,
        created_at=_at(DAY),
        settlement_id=None,
    )
    credit = _credit("BC-1", "UTR-NEW", net_a - 25_000, VALUE_DATE)

    proof = decompose(credit, Ledger([*a, refund, credit]), merchant_id=MERCHANT)

    assert proof.outcome is DecompositionOutcome.RESOLVED
    assert _ref(EntityType.REFUND, "REF-STANDALONE") in {t.ref for t in proof.terms}


def test_candidates_outside_the_date_window_are_not_considered():
    far = date(2026, 1, 1)
    records, net = _make_batch("STL-OLD", "UTR-OLD", far, [500_000])
    credit = _credit("BC-1", "UTR-NEW", net, VALUE_DATE)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT)

    assert proof.outcome is DecompositionOutcome.UNRESOLVED
    assert proof.reason is DecompositionReason.NO_CANDIDATES_IN_WINDOW


def test_another_merchants_batch_is_never_a_candidate():
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000], merchant_id="MERCH-OTHER")
    credit = _credit("BC-1", "UTR-NEW", net, VALUE_DATE)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT)

    assert proof.outcome is DecompositionOutcome.UNRESOLVED
    assert proof.reason is DecompositionReason.NO_CANDIDATES_IN_WINDOW


def test_claimed_records_are_not_offered_to_a_later_credit():
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    credit = _credit("BC-2", "UTR-NEW", net_a, VALUE_DATE)
    ledger = Ledger([*a, credit])
    claimed = frozenset({_ref(EntityType.PAYMENT, "STL-1-PAY-0")})

    proof = decompose(credit, ledger, merchant_id=MERCHANT, claimed=claimed)

    assert proof.outcome is DecompositionOutcome.UNRESOLVED
    assert proof.reason is DecompositionReason.NO_CANDIDATES_IN_WINDOW


def test_no_subset_sums_to_the_credit():
    records, _ = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    credit = _credit("BC-1", "UTR-NEW", 123_457, VALUE_DATE)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT)

    assert proof.outcome is DecompositionOutcome.UNRESOLVED
    assert proof.reason is DecompositionReason.NO_SUBSET_SUMS_TO_CREDIT
    assert proof.terms == []


# ---------------------------------------------------------------------------
# Ambiguity -- the case this module exists to get right
# ---------------------------------------------------------------------------


def test_two_subsets_matching_the_target_is_reported_never_chosen():
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    b, net_b = _make_batch("STL-2", "UTR-2", DAY, [500_000])
    assert net_a == net_b, "fixture must be genuinely ambiguous"
    credit = _credit("BC-1", "UTR-NEW", net_a, VALUE_DATE)

    proof = decompose(credit, Ledger([*a, *b, credit]), merchant_id=MERCHANT)

    assert proof.outcome is DecompositionOutcome.AMBIGUOUS
    assert proof.reason is DecompositionReason.MULTIPLE_SUBSETS_MATCH
    assert proof.terms == [], "an ambiguous proof must not contain an answer"
    assert len(proof.competing) >= 2


def test_ambiguous_proof_lists_the_competing_candidates():
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    b, _ = _make_batch("STL-2", "UTR-2", DAY, [500_000])
    credit = _credit("BC-1", "UTR-NEW", net_a, VALUE_DATE)

    proof = decompose(credit, Ledger([*a, *b, credit]), merchant_id=MERCHANT)

    competitors = [frozenset(c.refs) for c in proof.competing]
    assert frozenset({_ref(EntityType.PAYMENT, "STL-1-PAY-0")}) in competitors
    assert frozenset({_ref(EntityType.PAYMENT, "STL-2-PAY-0")}) in competitors
    assert all(c.sum_paise == net_a for c in proof.competing)


def test_ambiguity_between_a_whole_batch_and_a_combination_is_reported():
    # The subtle one: one batch nets to the credit, and two orphan payments
    # also net to it. Both are defensible; neither may be picked.
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    orphans = [_orphan_payment("PAY-A", 200_000, DAY), _orphan_payment("PAY-B", 300_000, DAY)]
    assert net_a == 500_000
    credit = _credit("BC-1", "UTR-NEW", 500_000, VALUE_DATE)

    proof = decompose(credit, Ledger([*a, *orphans, credit]), merchant_id=MERCHANT)

    assert proof.outcome is DecompositionOutcome.AMBIGUOUS
    assert proof.terms == []


# ---------------------------------------------------------------------------
# Budgets -- degrade to an exception, never hang, never guess
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_timeout_degrades_to_an_exception_rather_than_hanging():
    records = []
    for i in range(6):
        batch, _ = _make_batch(f"STL-{i}", f"UTR-{i}", DAY, [100_000 + i])
        records.extend(batch)
    credit = _credit("BC-1", "UTR-NEW", 999_999_999, VALUE_DATE)
    clock = _FakeClock(trip_after=1)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT, clock=clock)

    assert proof.outcome is DecompositionOutcome.UNRESOLVED
    assert proof.reason is DecompositionReason.TIME_BUDGET_EXHAUSTED
    assert proof.terms == []


def test_node_budget_exhaustion_is_reported_distinctly_from_a_timeout():
    # Two different bailouts with two different meanings: the node budget is
    # reproducible across machines, the wall clock is not. Collapsing them
    # into one reason would make a non-deterministic run indistinguishable
    # from a deterministic one in the report.
    records = []
    total = 0
    for i in range(12):
        batch, net = _make_batch(f"STL-{i}", f"UTR-{i}", DAY, [100_000 + i])
        records.extend(batch)
        total += net
    # One paise short of "take everything": reachable enough that the search
    # genuinely explores, so the budget is what stops it rather than the
    # feasibility pruning.
    credit = _credit("BC-1", "UTR-NEW", total - 1, VALUE_DATE)
    budget = DecompositionBudget(max_nodes=3)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT, budget=budget)

    assert proof.outcome is DecompositionOutcome.UNRESOLVED
    assert proof.reason is DecompositionReason.NODE_BUDGET_EXHAUSTED
    assert proof.terms == []


def test_too_many_candidates_is_refused_before_the_search_starts():
    records = []
    for i in range(60):
        batch, _ = _make_batch(f"STL-{i}", f"UTR-{i}", DAY, [100_000 + i])
        records.extend(batch)
    credit = _credit("BC-1", "UTR-NEW", 999_999_999, VALUE_DATE)
    budget = DecompositionBudget(max_candidates=40)

    proof = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT, budget=budget)

    assert proof.outcome is DecompositionOutcome.UNRESOLVED
    assert proof.reason is DecompositionReason.TOO_MANY_CANDIDATES


# ---------------------------------------------------------------------------
# Tier 3 -- cost-based assignment over the residue
# ---------------------------------------------------------------------------


def test_split_settlement_resolves_by_exploding_the_shared_batch():
    # One batch paid out as two credits sharing its UTR. Hungarian is 1:1
    # and structurally cannot express "credit = payments 0 and 1", so the
    # shared key instead makes tier 2 search the batch's payment sub-units.
    # Amounts chosen so each split is the *only* subset that reaches it --
    # [500, 300, 200, 100] would let 800_000 be either 500+300 or
    # 500+200+100, which the engine would (correctly) call ambiguous.
    records, _ = _make_batch("STL-1", "UTR-1", DAY, [500_000, 300_000, 250_000, 110_000])
    first = _credit("BC-1", "UTR-1", 800_000, VALUE_DATE)
    second = _credit("BC-2", "UTR-1", 360_000, VALUE_DATE)
    ledger = Ledger([*records, first, second])

    proofs = {p.credit_ref.id: p for p in decompose_all([first, second], ledger, merchant_id=MERCHANT)}

    assert proofs["BC-1"].tier is DecompositionTier.SUBSET_SUM
    assert proofs["BC-1"].outcome is DecompositionOutcome.RESOLVED
    assert {t.ref for t in proofs["BC-1"].terms} == {
        _ref(EntityType.PAYMENT, "STL-1-PAY-0"),
        _ref(EntityType.PAYMENT, "STL-1-PAY-1"),
    }
    assert proofs["BC-2"].outcome is DecompositionOutcome.RESOLVED
    assert {t.ref for t in proofs["BC-2"].terms} == {
        _ref(EntityType.PAYMENT, "STL-1-PAY-2"),
        _ref(EntityType.PAYMENT, "STL-1-PAY-3"),
    }
    assert proofs["BC-1"].residual_paise == 0
    assert proofs["BC-2"].residual_paise == 0


def test_assignment_pairs_credits_to_units_when_no_subset_sums_exactly():
    # Amounts are perturbed so tier 2 finds nothing; date and narration
    # decide the pairing. A greedy nearest-amount matcher would pair both
    # credits with the same unit.
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    b, net_b = _make_batch("STL-2", "UTR-2", DAY + timedelta(days=3), [500_050])
    first = _credit("BC-1", "UTR-X", net_a - 7, VALUE_DATE, narration="NEFT Settlement STL-1")
    second = _credit("BC-2", "UTR-Y", net_b - 7, VALUE_DATE + timedelta(days=3), narration="NEFT Settlement STL-2")
    ledger = Ledger([*a, *b, first, second])

    proofs = {p.credit_ref.id: p for p in decompose_all([first, second], ledger, merchant_id=MERCHANT)}

    assert proofs["BC-1"].tier is DecompositionTier.ASSIGNMENT
    assert proofs["BC-1"].outcome is DecompositionOutcome.RESOLVED
    assert _ref(EntityType.PAYMENT, "STL-1-PAY-0") in {t.ref for t in proofs["BC-1"].terms}
    assert _ref(EntityType.PAYMENT, "STL-2-PAY-0") in {t.ref for t in proofs["BC-2"].terms}
    assert proofs["BC-1"].residual_paise == -7


def test_assignment_records_its_cost_and_margin_over_the_runner_up():
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    b, net_b = _make_batch("STL-2", "UTR-2", DAY + timedelta(days=3), [900_000])
    first = _credit("BC-1", "UTR-X", net_a - 7, VALUE_DATE, narration="Settlement STL-1")
    second = _credit("BC-2", "UTR-Y", net_b - 7, VALUE_DATE + timedelta(days=3), narration="Settlement STL-2")
    ledger = Ledger([*a, *b, first, second])

    proofs = decompose_all([first, second], ledger, merchant_id=MERCHANT)

    for proof in proofs:
        assert proof.assignment_cost is not None
        assert proof.assignment_margin is not None
        assert proof.assignment_margin > 0


def test_a_tied_assignment_is_ambiguous_not_an_arbitrary_pick():
    # Two units indistinguishable on every feature. scipy's tie-breaking is
    # undocumented, so trusting it here would make the report depend on the
    # BLAS build -- and would be a silent choice besides.
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    b, _ = _make_batch("STL-2", "UTR-2", DAY, [500_000])
    first = _credit("BC-1", "UTR-X", net_a - 7, VALUE_DATE, narration="Settlement")
    second = _credit("BC-2", "UTR-Y", net_a - 7, VALUE_DATE, narration="Settlement")
    ledger = Ledger([*a, *b, first, second])

    proofs = decompose_all([first, second], ledger, merchant_id=MERCHANT)

    assert all(p.outcome is DecompositionOutcome.AMBIGUOUS for p in proofs)
    assert all(p.reason is DecompositionReason.ASSIGNMENT_TIE for p in proofs)
    assert all(p.terms == [] for p in proofs)


def test_decompose_all_never_lets_two_credits_claim_the_same_record():
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000], fees=[11_800])
    b, net_b = _make_batch("STL-2", "UTR-2", DAY, [300_000], fees=[7_080])
    first = _credit("BC-1", "UTR-1", net_a, VALUE_DATE)
    second = _credit("BC-2", "UTR-2", net_b, VALUE_DATE)
    ledger = Ledger([*a, *b, first, second])

    proofs = decompose_all([first, second], ledger, merchant_id=MERCHANT)

    claimed: set[RecordRef] = set()
    for proof in proofs:
        refs = {t.ref for t in proof.terms}
        assert not (refs & claimed), f"{proof.credit_ref.id} re-claims {refs & claimed}"
        claimed |= refs


def test_decompose_all_returns_one_proof_per_credit_in_input_order():
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    b, net_b = _make_batch("STL-2", "UTR-2", DAY, [300_000])
    second = _credit("BC-2", "UTR-2", net_b, VALUE_DATE)
    first = _credit("BC-1", "UTR-1", net_a, VALUE_DATE)

    proofs = decompose_all([second, first], Ledger([*a, *b, second, first]), merchant_id=MERCHANT)

    assert [p.credit_ref.id for p in proofs] == ["BC-2", "BC-1"]


# ---------------------------------------------------------------------------
# The proof verifies independently -- only the proof and the ledger
# ---------------------------------------------------------------------------


def _resolved_proof() -> tuple[DecompositionProof, Ledger]:
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000, 300_000], fees=[11_800], taxes=[2_124])
    credit = _credit("BC-1", "UTR-1", net, VALUE_DATE)
    ledger = Ledger([*records, credit])
    return decompose(credit, ledger, merchant_id=MERCHANT), ledger


def test_an_untampered_proof_verifies():
    proof, ledger = _resolved_proof()
    result = verify_proof(proof, ledger)
    assert result.ok is True, result.failures
    assert result.failures == []


def test_verification_needs_nothing_but_the_proof_and_the_ledger():
    # The credit is recovered from the ledger via credit_ref. If the
    # verifier needed the BankCredit passed in, a caller could hand it a
    # doctored one and the check would pass.
    proof, ledger = _resolved_proof()
    assert verify_proof(proof, ledger).ok is True


def test_tampering_with_a_single_signed_amount_fails_verification():
    proof, ledger = _resolved_proof()
    terms = [t.model_copy(update={"signed_paise": t.signed_paise + 1}) if i == 0 else t for i, t in enumerate(proof.terms)]
    tampered = proof.model_copy(update={"terms": terms})

    assert verify_proof(tampered, ledger).ok is False


def test_tampering_that_keeps_the_sum_intact_still_fails():
    # +1 on one term and -1 on another leaves sum_paise and residual_paise
    # correct. Only re-deriving each term from the ledger catches it.
    proof, ledger = _resolved_proof()
    terms = list(proof.terms)
    terms[0] = terms[0].model_copy(update={"signed_paise": terms[0].signed_paise + 1})
    terms[1] = terms[1].model_copy(update={"signed_paise": terms[1].signed_paise - 1})
    tampered = proof.model_copy(update={"terms": terms})

    assert sum(t.signed_paise for t in tampered.terms) == tampered.sum_paise
    assert verify_proof(tampered, ledger).ok is False


def test_citing_a_record_that_does_not_exist_fails_verification():
    proof, ledger = _resolved_proof()
    ghost = proof.terms[0].model_copy(update={"ref": _ref(EntityType.PAYMENT, "PAY-GHOST")})
    tampered = proof.model_copy(update={"terms": [*proof.terms, ghost]})

    assert verify_proof(tampered, ledger).ok is False


def test_dropping_a_term_fails_verification():
    proof, ledger = _resolved_proof()
    tampered = proof.model_copy(update={"terms": proof.terms[1:]})

    assert verify_proof(tampered, ledger).ok is False


def test_duplicating_a_term_fails_verification():
    proof, ledger = _resolved_proof()
    tampered = proof.model_copy(update={"terms": [*proof.terms, proof.terms[0]]})

    assert verify_proof(tampered, ledger).ok is False


def test_altering_the_residual_fails_verification():
    proof, ledger = _resolved_proof()
    tampered = proof.model_copy(update={"residual_paise": proof.residual_paise + 100})

    assert verify_proof(tampered, ledger).ok is False


def test_repointing_the_credit_ref_fails_verification():
    proof, ledger = _resolved_proof()
    tampered = proof.model_copy(update={"credit_ref": _ref(EntityType.BANK_CREDIT, "BC-ABSENT")})

    assert verify_proof(tampered, ledger).ok is False


def test_altering_the_tier_or_confidence_fails_verification_via_the_hash():
    # These fields pass every arithmetic check -- the hash is what catches
    # an escalation of a guess into a certainty.
    proof, ledger = _resolved_proof()
    tampered = proof.model_copy(
        update={"tier": DecompositionTier.ASSIGNMENT, "confidence": 10_000}
    )

    assert verify_proof(tampered, ledger).ok is False


def test_reordering_terms_fails_verification():
    proof, ledger = _resolved_proof()
    tampered = proof.model_copy(update={"terms": list(reversed(proof.terms))})

    assert verify_proof(tampered, ledger).ok is False


def test_an_ambiguous_proof_carrying_an_answer_fails_verification():
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    b, _ = _make_batch("STL-2", "UTR-2", DAY, [500_000])
    credit = _credit("BC-1", "UTR-NEW", net_a, VALUE_DATE)
    ledger = Ledger([*a, *b, credit])
    proof = decompose(credit, ledger, merchant_id=MERCHANT)
    assert proof.outcome is DecompositionOutcome.AMBIGUOUS

    answer = ProofTerm(ref=_ref(EntityType.PAYMENT, "STL-1-PAY-0"), signed_paise=net_a)
    smuggled = proof.model_copy(update={"terms": [answer]})

    assert verify_proof(smuggled, ledger).ok is False


def test_failures_name_what_went_wrong():
    proof, ledger = _resolved_proof()
    tampered = proof.model_copy(update={"residual_paise": proof.residual_paise + 100})

    result = verify_proof(tampered, ledger)
    assert result.failures, "a failed verification must say why"
    assert all(isinstance(f, str) and f for f in result.failures)


# ---------------------------------------------------------------------------
# Determinism (invariant 4)
# ---------------------------------------------------------------------------


def _hashable(proof: DecompositionProof) -> dict:
    payload = proof.model_dump(mode="json")
    for field in DecompositionProof.TELEMETRY_FIELDS:
        payload.pop(field, None)
    return payload


def test_two_runs_over_the_same_inputs_produce_identical_proofs():
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000, 300_000], fees=[11_800], taxes=[2_124])
    credit = _credit("BC-1", "UTR-1", net, VALUE_DATE)
    ledger = Ledger([*records, credit])

    first = decompose(credit, ledger, merchant_id=MERCHANT)
    second = decompose(credit, ledger, merchant_id=MERCHANT)

    assert _hashable(first) == _hashable(second)
    assert first.proof_hash == second.proof_hash


def test_record_insertion_order_does_not_change_the_proof():
    records, net = _make_batch("STL-1", "UTR-1", DAY, [500_000, 300_000], fees=[11_800], taxes=[2_124])
    credit = _credit("BC-1", "UTR-1", net, VALUE_DATE)

    forward = decompose(credit, Ledger([*records, credit]), merchant_id=MERCHANT)
    backward = decompose(credit, Ledger([credit, *reversed(records)]), merchant_id=MERCHANT)

    assert forward.proof_hash == backward.proof_hash


def test_elapsed_time_is_observed_but_never_hashed():
    proof, _ = _resolved_proof()
    assert proof.elapsed_ns >= 0
    assert "elapsed_ns" in DecompositionProof.TELEMETRY_FIELDS

    slower = proof.model_copy(update={"elapsed_ns": proof.elapsed_ns + 1_000_000})
    assert _hashable(slower) == _hashable(proof)


def test_proof_hash_is_recomputable_from_the_proof_alone():
    proof, ledger = _resolved_proof()
    # verify_proof recomputes it; a proof whose stored hash disagrees with
    # its own content is rejected.
    assert verify_proof(proof, ledger).ok is True
    assert len(proof.proof_hash) == 64


# ---------------------------------------------------------------------------
# The settlement lag is configuration, not a constant
# ---------------------------------------------------------------------------


def test_settlement_lag_defaults_to_the_indian_standard():
    assert SETTLEMENT_LAG_DAYS == 2
    assert DecompositionBudget().settlement_lag_days == 2


def test_from_env_reads_the_settlement_lag(monkeypatch):
    monkeypatch.setattr("core.decompose.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("SETTLEMENT_LAG_DAYS", "3")

    assert DecompositionBudget.from_env().settlement_lag_days == 3


def test_from_env_falls_back_to_the_declared_default(monkeypatch):
    # load_dotenv() resolves its path from the calling module's location,
    # not the cwd, so it would find the repo's own .env and this test would
    # assert the developer's filesystem instead of the fallback logic. The
    # same trap is logged against llm/providers/gemini.py.
    monkeypatch.setattr("core.decompose.load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("SETTLEMENT_LAG_DAYS", raising=False)

    assert DecompositionBudget.from_env().settlement_lag_days == SETTLEMENT_LAG_DAYS


def test_the_plain_constructor_ignores_the_environment(monkeypatch):
    # A budget that absorbed ambient environment would make every run depend
    # on the shell it was launched from, and invariant 4 could not be
    # checked. Configuration is asked for, never inhaled.
    monkeypatch.setenv("SETTLEMENT_LAG_DAYS", "9")

    assert DecompositionBudget().settlement_lag_days == SETTLEMENT_LAG_DAYS


def test_the_settlement_lag_changes_matching_but_never_an_amount():
    # It shapes tier 3's date cost. If it ever moved a rupee, this would
    # differ.
    a, net_a = _make_batch("STL-1", "UTR-1", DAY, [500_000])
    credit = _credit("BC-1", "UTR-1", net_a, VALUE_DATE)
    ledger = Ledger([*a, credit])

    lagged = decompose(credit, ledger, merchant_id=MERCHANT, budget=DecompositionBudget(settlement_lag_days=9))
    normal = decompose(credit, ledger, merchant_id=MERCHANT)

    assert lagged.sum_paise == normal.sum_paise
    assert lagged.residual_paise == normal.residual_paise


# ---------------------------------------------------------------------------
# End to end against the committed run
# ---------------------------------------------------------------------------


def _load_committed_run() -> tuple[list[BankCredit], Ledger]:
    """Loads runs/realistic-seed42. Deliberately test-side: core/ has no
    loader and this change does not add one. Ground truth under truth/ is
    not read -- only eval/ may do that (invariant 5)."""
    run = REPO_ROOT / "runs" / "realistic-seed42"
    ledger_json = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    report_json = json.loads((run / "settlement_report.json").read_text(encoding="utf-8"))
    statement_json = json.loads((run / "bank_statement.json").read_text(encoding="utf-8"))

    credits = [BankCredit.model_validate(r) for r in statement_json["bank_credits"]]
    records: list = list(credits)
    for key, model in (
        ("payments", Payment),
        ("refunds", Refund),
        ("chargebacks", Chargeback),
        ("adjustments", Adjustment),
    ):
        records.extend(model.model_validate(r) for r in ledger_json[key])
    for key, model in (("fee_lines", FeeLine), ("tax_lines", TaxLine), ("batches", SettlementBatch)):
        records.extend(model.model_validate(r) for r in report_json[key])
    return credits, Ledger(records)


@pytest.mark.timeout(120)
def test_committed_run_decomposes_without_double_claiming_any_record():
    credits, ledger = _load_committed_run()

    proofs = decompose_all(credits, ledger, merchant_id=MERCHANT)

    assert len(proofs) == len(credits)
    claimed: set[RecordRef] = set()
    for proof in proofs:
        refs = {t.ref for t in proof.terms}
        overlap = refs & claimed
        assert not overlap, f"{proof.credit_ref.id} re-claims {sorted(r.id for r in overlap)}"
        claimed |= refs


@pytest.mark.timeout(120)
def test_committed_run_resolves_structurally_exactly_where_the_utr_is_intact():
    credits, ledger = _load_committed_run()
    batch_utrs = {
        ledger.get(ref).utr for ref in ledger.by_type(EntityType.SETTLEMENT_BATCH)
    }
    intact = [c for c in credits if c.utr in batch_utrs]
    assert 0 < len(intact) < len(credits), "the committed run should contain both intact and D09-corrupted UTRs"

    proofs = {p.credit_ref.id: p for p in decompose_all(credits, ledger, merchant_id=MERCHANT)}

    for credit in credits:
        proof = proofs[credit.id]
        if credit.utr in batch_utrs:
            assert proof.tier is DecompositionTier.STRUCTURAL, credit.id
        else:
            assert proof.tier is not DecompositionTier.STRUCTURAL, credit.id


@pytest.mark.timeout(120)
def test_every_proof_from_the_committed_run_verifies():
    credits, ledger = _load_committed_run()

    for proof in decompose_all(credits, ledger, merchant_id=MERCHANT):
        result = verify_proof(proof, ledger)
        assert result.ok is True, f"{proof.credit_ref.id}: {result.failures}"


@pytest.mark.timeout(120)
def test_committed_run_is_byte_identical_across_two_decompositions():
    credits, ledger = _load_committed_run()

    first = [p.proof_hash for p in decompose_all(credits, ledger, merchant_id=MERCHANT)]
    second = [p.proof_hash for p in decompose_all(credits, ledger, merchant_id=MERCHANT)]

    assert first == second
