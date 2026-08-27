"""Tests for core/ledger.py.

Ledger is an in-memory index over a loaded batch: O(1) exists()/get() by
(type, id) so the LLM reference checker (invariant 6) has a fast, precise
thing to check citations against, plus by_settlement() for the records that
belong to a given settlement cycle.

The by_utr / by_type / fee_lines_for / tax_lines_for indexes exist for
core/decompose.py: BankCredit has no settlement_id, so its only structural
link to a SettlementBatch is the UTR, and FeeLine/TaxLine have no
settlement_id either -- they attach transitively via applies_to_id and
applies_to_fee_id.
"""

from datetime import UTC, date, datetime

from core.ledger import DuplicateRecordError, Ledger, journal_entries_for_auto_findings
from core.models import (
    BankCredit,
    BatchStatus,
    EntityType,
    FeeLine,
    FeeType,
    Finding,
    Lane,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
    SettlementBatch,
    Severity,
    TaxLine,
)
from core.money import Money

UTC_NOON = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _payment(id_, settlement_id=None):
    return Payment(
        id=id_,
        merchant_id="m_1",
        amount=Money(1000),
        method=PaymentMethod.UPI,
        network=None,
        card_type=None,
        is_international=False,
        mcc="5411",
        captured_at=UTC_NOON,
        settlement_id=settlement_id,
    )


def _refund(id_, payment_id, settlement_id=None):
    return Refund(
        id=id_,
        payment_id=payment_id,
        amount=Money(100),
        is_partial=False,
        created_at=UTC_NOON,
        settlement_id=settlement_id,
    )


def _fee_line(id_, applies_to_id):
    return FeeLine(
        id=id_,
        applies_to_id=applies_to_id,
        applies_to_type=EntityType.PAYMENT,
        fee_type=FeeType.MDR,
        computed_amount=Money(24),
        rule_id="rule_1",
    )


# ---------------------------------------------------------------------------
# exists() / get()
# ---------------------------------------------------------------------------


def test_exists_true_for_loaded_record():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.exists(RecordRef(type=EntityType.PAYMENT, id="pay_1")) is True


def test_exists_false_for_absent_id():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.exists(RecordRef(type=EntityType.PAYMENT, id="pay_absent")) is False


def test_exists_false_for_right_id_wrong_type():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.exists(RecordRef(type=EntityType.REFUND, id="pay_1")) is False


def test_get_returns_the_record():
    payment = _payment("pay_1")
    ledger = Ledger([payment])
    assert ledger.get(RecordRef(type=EntityType.PAYMENT, id="pay_1")) == payment


def test_get_returns_none_for_absent_ref():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.get(RecordRef(type=EntityType.PAYMENT, id="pay_absent")) is None


# ---------------------------------------------------------------------------
# by_settlement()
# ---------------------------------------------------------------------------


def test_by_settlement_groups_records_across_types():
    ledger = Ledger(
        [
            _payment("pay_1", settlement_id="batch_1"),
            _payment("pay_2", settlement_id="batch_1"),
            _refund("ref_1", payment_id="pay_1", settlement_id="batch_1"),
            _payment("pay_3", settlement_id="batch_2"),
        ]
    )
    refs = ledger.by_settlement("batch_1")
    assert set(refs) == {
        RecordRef(type=EntityType.PAYMENT, id="pay_1"),
        RecordRef(type=EntityType.PAYMENT, id="pay_2"),
        RecordRef(type=EntityType.REFUND, id="ref_1"),
    }


def test_by_settlement_empty_for_unknown_settlement_id():
    ledger = Ledger([_payment("pay_1", settlement_id="batch_1")])
    assert ledger.by_settlement("unknown_batch") == []


def test_by_settlement_excludes_records_without_settlement_id_field():
    ledger = Ledger([_fee_line("fee_1", applies_to_id="pay_1")])
    assert ledger.by_settlement("batch_1") == []
    assert ledger.exists(RecordRef(type=EntityType.FEE_LINE, id="fee_1")) is True


def test_by_settlement_excludes_records_with_none_settlement_id():
    ledger = Ledger([_payment("pay_1", settlement_id=None)])
    assert ledger.by_settlement("batch_1") == []


def test_by_settlement_is_sorted():
    # Insertion order is not reproducible across input orderings, and
    # decompose sums these refs into a proof whose hash must be stable
    # (invariant 4). Sorted output makes the sum order deterministic.
    ledger = Ledger(
        [
            _payment("pay_3", settlement_id="batch_1"),
            _payment("pay_1", settlement_id="batch_1"),
            _payment("pay_2", settlement_id="batch_1"),
        ]
    )
    assert ledger.by_settlement("batch_1") == sorted(ledger.by_settlement("batch_1"), key=_sort_key)


# ---------------------------------------------------------------------------
# by_utr() -- the only structural link from a BankCredit to a SettlementBatch
# ---------------------------------------------------------------------------


def _batch(id_, utr, merchant_id="MERCH-0001"):
    return SettlementBatch(
        id=id_,
        merchant_id=merchant_id,
        cycle_start=UTC_NOON,
        cycle_end=UTC_NOON,
        expected_credit=Money(100_000),
        utr=utr,
        status=BatchStatus.SETTLED,
    )


def _bank_credit(id_, utr, paise=100_000, value_date=date(2026, 8, 25)):
    return BankCredit(
        id=id_,
        utr=utr,
        amount=Money(paise),
        value_date=value_date,
        narration=f"NEFT settlement {utr}",
    )


def _tax_line(id_, applies_to_fee_id, paise=425):
    return TaxLine(
        id=id_,
        applies_to_fee_id=applies_to_fee_id,
        tax_type="GST",
        rate_bps=1800,
        base_amount=Money(2_360),
        amount=Money(paise),
    )


def _sort_key(ref):
    return (ref.type, ref.id)


def test_by_utr_finds_the_batch():
    ledger = Ledger([_batch("STL-1", "HDFC0001")])
    assert ledger.by_utr("HDFC0001") == [RecordRef(type=EntityType.SETTLEMENT_BATCH, id="STL-1")]


def test_by_utr_returns_both_the_batch_and_the_credit():
    # Both entities carry a utr. by_utr does not filter by type -- callers
    # that want one kind say so, and decompose does.
    ledger = Ledger([_batch("STL-1", "HDFC0001"), _bank_credit("BC-1", "HDFC0001")])
    assert set(ledger.by_utr("HDFC0001")) == {
        RecordRef(type=EntityType.SETTLEMENT_BATCH, id="STL-1"),
        RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
    }


def test_by_utr_surfaces_two_batches_sharing_a_utr():
    # Must not silently dedupe. Two batches on one UTR is a real condition
    # and decompose refuses to resolve structurally when it sees it, rather
    # than picking whichever the index happened to keep.
    ledger = Ledger([_batch("STL-1", "HDFC0001"), _batch("STL-2", "HDFC0001")])
    refs = ledger.by_utr("HDFC0001")
    assert len(refs) == 2


def test_by_utr_empty_for_unknown_utr():
    assert Ledger([_batch("STL-1", "HDFC0001")]).by_utr("HDFC9999") == []


def test_by_utr_empty_for_empty_string():
    # A blank UTR is the "no structural key" case, not a wildcard that
    # matches every record lacking one.
    assert Ledger([_batch("STL-1", "HDFC0001")]).by_utr("") == []


def test_by_utr_does_not_index_records_without_a_utr_field():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.by_utr("HDFC0001") == []


def test_by_utr_is_sorted():
    ledger = Ledger([_batch("STL-3", "U"), _batch("STL-1", "U"), _batch("STL-2", "U")])
    assert ledger.by_utr("U") == sorted(ledger.by_utr("U"), key=_sort_key)


# ---------------------------------------------------------------------------
# by_type()
# ---------------------------------------------------------------------------


def test_by_type_returns_only_that_type():
    ledger = Ledger([_payment("pay_1"), _refund("ref_1", "pay_1"), _payment("pay_2")])
    assert ledger.by_type(EntityType.PAYMENT) == [
        RecordRef(type=EntityType.PAYMENT, id="pay_1"),
        RecordRef(type=EntityType.PAYMENT, id="pay_2"),
    ]


def test_by_type_empty_for_absent_type():
    assert Ledger([_payment("pay_1")]).by_type(EntityType.CHARGEBACK) == []


def test_by_type_is_sorted():
    ledger = Ledger([_payment("pay_3"), _payment("pay_1"), _payment("pay_2")])
    ids = [ref.id for ref in ledger.by_type(EntityType.PAYMENT)]
    assert ids == ["pay_1", "pay_2", "pay_3"]


# ---------------------------------------------------------------------------
# fee_lines_for() / tax_lines_for() -- the transitive join. FeeLine and
# TaxLine have no settlement_id, so by_settlement() will never return them;
# a decomposition that only used by_settlement() would omit every deduction.
# ---------------------------------------------------------------------------


def test_fee_lines_for_finds_fees_on_a_payment():
    ledger = Ledger([_payment("pay_1"), _fee_line("fee_1", "pay_1"), _fee_line("fee_2", "pay_1")])
    assert ledger.fee_lines_for(RecordRef(type=EntityType.PAYMENT, id="pay_1")) == [
        RecordRef(type=EntityType.FEE_LINE, id="fee_1"),
        RecordRef(type=EntityType.FEE_LINE, id="fee_2"),
    ]


def test_fee_lines_for_matches_on_type_as_well_as_id():
    # A dispute fee applies to a CHARGEBACK; a payment with a colliding id
    # must not pick it up. applies_to_type exists precisely for this.
    fee = FeeLine(
        id="fee_1",
        applies_to_id="shared_id",
        applies_to_type=EntityType.CHARGEBACK,
        fee_type=FeeType.DISPUTE_FEE,
        computed_amount=Money(50_000),
        rule_id="rule_cb",
    )
    ledger = Ledger([fee])
    assert ledger.fee_lines_for(RecordRef(type=EntityType.PAYMENT, id="shared_id")) == []
    assert ledger.fee_lines_for(RecordRef(type=EntityType.CHARGEBACK, id="shared_id")) == [
        RecordRef(type=EntityType.FEE_LINE, id="fee_1")
    ]


def test_fee_lines_for_empty_when_none_attach():
    ledger = Ledger([_payment("pay_1"), _fee_line("fee_1", "pay_2")])
    assert ledger.fee_lines_for(RecordRef(type=EntityType.PAYMENT, id="pay_1")) == []


def test_tax_lines_for_finds_taxes_on_a_fee():
    ledger = Ledger([_fee_line("fee_1", "pay_1"), _tax_line("tax_1", "fee_1")])
    assert ledger.tax_lines_for(RecordRef(type=EntityType.FEE_LINE, id="fee_1")) == [
        RecordRef(type=EntityType.TAX_LINE, id="tax_1")
    ]


def test_tax_lines_for_empty_for_a_non_fee_ref():
    ledger = Ledger([_fee_line("fee_1", "pay_1"), _tax_line("tax_1", "fee_1")])
    assert ledger.tax_lines_for(RecordRef(type=EntityType.PAYMENT, id="fee_1")) == []


def test_fee_and_tax_lines_are_sorted():
    ledger = Ledger([_fee_line("fee_3", "pay_1"), _fee_line("fee_1", "pay_1"), _fee_line("fee_2", "pay_1")])
    ids = [ref.id for ref in ledger.fee_lines_for(RecordRef(type=EntityType.PAYMENT, id="pay_1"))]
    assert ids == ["fee_1", "fee_2", "fee_3"]


# ---------------------------------------------------------------------------
# __len__ / __iter__ -- the proof verifier walks the whole ledger
# ---------------------------------------------------------------------------


def test_len_counts_loaded_records():
    assert len(Ledger([_payment("pay_1"), _refund("ref_1", "pay_1")])) == 2


def test_len_of_empty_ledger():
    assert len(Ledger([])) == 0


def test_iter_yields_every_ref_sorted():
    ledger = Ledger([_refund("ref_1", "pay_1"), _payment("pay_1")])
    assert list(ledger) == [
        RecordRef(type=EntityType.PAYMENT, id="pay_1"),
        RecordRef(type=EntityType.REFUND, id="ref_1"),
    ]


# ---------------------------------------------------------------------------
# Duplicate-record ingestion -- a settlement file handed to the loader twice
# must not silently corrupt the join indexes (last-write-wins in _by_ref
# while every index still double-appends), and two DIFFERENT records
# sharing an id must never be resolved by guessing which one is real.
# ---------------------------------------------------------------------------


def test_an_identical_duplicate_record_is_deduped_not_double_indexed(capsys):
    payment = _payment("pay_1", settlement_id="batch_1")
    ledger = Ledger([payment, payment.model_copy()])

    assert len(ledger) == 1
    assert ledger.by_settlement("batch_1") == [RecordRef(type=EntityType.PAYMENT, id="pay_1")]
    assert ledger.by_type(EntityType.PAYMENT) == [RecordRef(type=EntityType.PAYMENT, id="pay_1")]
    logged = capsys.readouterr().out
    assert "duplicate_record_ingested" in logged


def test_an_identical_duplicate_fee_line_does_not_get_cited_twice():
    # The shape of the real bug this guards: a duplicated FeeLine reaching
    # decompose.py via fee_lines_for() twice would double-count its amount
    # in a proof's sum_paise. Deduping at ingestion is what makes that
    # structurally impossible without any change in core/decompose.py.
    fee = _fee_line("fee_1", "pay_1")
    ledger = Ledger([_payment("pay_1"), fee, fee.model_copy()])
    assert ledger.fee_lines_for(RecordRef(type=EntityType.PAYMENT, id="pay_1")) == [
        RecordRef(type=EntityType.FEE_LINE, id="fee_1")
    ]


def test_two_records_sharing_an_id_with_conflicting_content_raises():
    conflicting = _payment("pay_1", settlement_id="batch_1").model_copy(update={"amount": Money(9_999)})
    try:
        Ledger([_payment("pay_1", settlement_id="batch_1"), conflicting])
        assert False, "expected DuplicateRecordError"
    except DuplicateRecordError as error:
        assert "pay_1" in str(error)


# ---------------------------------------------------------------------------
# journal_entries_for_auto_findings -- AUTO-lane double-entry posting.
# Pure function: findings + input_hash + already-posted entries in, new
# JournalEntry rows out. No I/O here -- persistence is a caller's job.
# ---------------------------------------------------------------------------

POSTED_AT = UTC_NOON


def _finding(id_="FND-1", amount_paise=1_000, lane=Lane.AUTO, discrepancy_class="fee_overcharge"):
    return Finding(
        id=id_,
        audit_run_id="RUN-1",
        discrepancy_class=discrepancy_class,
        severity=Severity.MAJOR,
        amount_impact=Money(amount_paise),
        evidence_ids=[],
        confidence=9_500,
        lane=lane,
        explanation="test finding",
    )


def test_an_auto_lane_finding_posts_a_journal_entry():
    finding = _finding(lane=Lane.AUTO)

    entries = journal_entries_for_auto_findings([finding], input_hash="HASH-1", posted_at=POSTED_AT)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.amount == Money(1_000)
    assert entry.debit_account == "discrepancy_receivable:fee_overcharge"
    assert entry.credit_account == "settlement_suspense"
    assert entry.posted_at == POSTED_AT


def test_a_propose_lane_finding_never_posts():
    finding = _finding(lane=Lane.PROPOSE)
    entries = journal_entries_for_auto_findings([finding], input_hash="HASH-1", posted_at=POSTED_AT)
    assert entries == []


def test_an_escalate_lane_finding_never_posts():
    finding = _finding(lane=Lane.ESCALATE)
    entries = journal_entries_for_auto_findings([finding], input_hash="HASH-1", posted_at=POSTED_AT)
    assert entries == []


def test_non_auto_lane_findings_never_post_even_if_caller_forgot_to_filter():
    findings = [_finding("FND-1", lane=Lane.AUTO), _finding("FND-2", lane=Lane.PROPOSE)]
    entries = journal_entries_for_auto_findings(findings, input_hash="HASH-1", posted_at=POSTED_AT)
    assert len(entries) == 1
    assert entries[0].id.startswith("JNL-FND-1::")


def test_a_duplicate_finding_id_within_one_call_does_not_double_post():
    # An upstream merge bug (or a caller passing the same finding twice)
    # must not double-post within a single call -- the dedup guarantee
    # can't depend solely on already_posted being threaded correctly across
    # calls, the same way the AUTO-lane filter isn't trusted to the caller.
    finding = _finding("FND-1", amount_paise=1_000, lane=Lane.AUTO)
    same_id_different_amount = _finding("FND-1", amount_paise=9_999, lane=Lane.AUTO)

    entries = journal_entries_for_auto_findings(
        [finding, same_id_different_amount], input_hash="HASH-1", posted_at=POSTED_AT
    )

    assert len(entries) == 1
    assert len({e.idempotency_key for e in entries}) == 1


def test_rerunning_the_same_audit_produces_zero_new_entries():
    finding = _finding(lane=Lane.AUTO)

    first = journal_entries_for_auto_findings([finding], input_hash="HASH-1", posted_at=POSTED_AT)
    second = journal_entries_for_auto_findings(
        [finding], input_hash="HASH-1", already_posted=first, posted_at=POSTED_AT
    )

    assert first != []
    assert second == []


def test_two_different_input_hashes_do_not_collide_on_idempotency_key():
    finding = _finding(lane=Lane.AUTO)

    run1 = journal_entries_for_auto_findings([finding], input_hash="HASH-1", posted_at=POSTED_AT)
    run2 = journal_entries_for_auto_findings(
        [finding], input_hash="HASH-2", already_posted=run1, posted_at=POSTED_AT
    )

    assert run2 != [], "a different audit run's idempotency key must not be shadowed by another run's"
    assert run1[0].idempotency_key != run2[0].idempotency_key


def test_entries_are_sorted_by_finding_id_for_determinism():
    findings = [_finding("FND-2", lane=Lane.AUTO), _finding("FND-1", lane=Lane.AUTO)]
    entries = journal_entries_for_auto_findings(findings, input_hash="HASH-1", posted_at=POSTED_AT)
    ids = [e.id for e in entries]
    assert ids[0].startswith("JNL-FND-1::")
    assert ids[1].startswith("JNL-FND-2::")


def test_two_different_input_hashes_never_collide_on_journal_entry_id():
    # The DECISIONS.md-flagged gap: JournalEntry.id used to be f"JNL-{finding.id}"
    # alone, so two runs over a corrected settlement file (different
    # input_hash, same finding.id) produced two rows sharing an id but
    # disagreeing idempotency_key -- a primary-key collision on two
    # genuinely different postings. The id must now vary exactly when the
    # idempotency key varies.
    finding = _finding(lane=Lane.AUTO)
    run1 = journal_entries_for_auto_findings([finding], input_hash="HASH-1", posted_at=POSTED_AT)
    run2 = journal_entries_for_auto_findings(
        [finding], input_hash="HASH-2", already_posted=run1, posted_at=POSTED_AT
    )
    assert run1[0].id != run2[0].id
