"""Tests for ingest/mapping.py -- Razorpay's wire JSON to Assay's models.

Money-critical, written test-first per the working agreement. Every amount
Razorpay returns is already an integer in paise, so the whole mapping is
int-to-int with no Decimal and no float anywhere -- the tests below assert
exact paise, never an approximation.

The two things most worth pinning here are the ones a careless mapping
would get silently wrong:

  - the fee/tax convention. Razorpay documents the Payments API's `fee` as
    INCLUDING GST, while the recon report lists `fee` and `tax` as separate
    columns. Guessing wrong overstates or understates every fee line by the
    tax. `map_run` refuses to guess: each recon row publishes its own
    `credit`, and the arithmetic decides.
  - quarantine. A transaction Assay cannot faithfully represent (a card
    network outside its enum, a multi-component international fee) must be
    excluded and reported, never coerced into the nearest enum member or
    folded into a single blended fee line that would make `core/verify.py`
    emit findings about a fee it cannot actually check.
"""

from __future__ import annotations

from core.models import (
    AdjustmentKind,
    BatchStatus,
    CardType,
    FeeType,
    Network,
    PaymentMethod,
)
from core.money import Money
from ingest.mapping import FeeConvention, QuarantineReason, map_run

MERCHANT = "MERCH-0001"
MCC = "5411"

SETTLED_AT = 1_720_200_000
CREATED_AT = 1_720_000_000


def _recon_payment(
    entity_id: str = "pay_A1",
    amount: int = 100_000,
    fee: int = 1_500,
    tax: int = 270,
    credit: int | None = None,
    method: str = "upi",
    card_network: str | None = None,
    card_type: str | None = None,
    settlement_id: str = "setl_1",
    settlement_utr: str = "UTR0001",
    settled: bool = True,
) -> dict:
    return {
        "entity_id": entity_id,
        "type": "payment",
        "amount": amount,
        "debit": 0,
        "credit": amount - fee - tax if credit is None else credit,
        "currency": "INR",
        "fee": fee,
        "tax": tax,
        "settlement_id": settlement_id,
        "settlement_utr": settlement_utr,
        "method": method,
        "card_network": card_network,
        "card_type": card_type,
        "created_at": CREATED_AT,
        "settled_at": SETTLED_AT,
        "settled": settled,
    }


def _payment_entity(
    id_: str = "pay_A1",
    amount: int = 100_000,
    method: str = "upi",
    international: bool = False,
    card: dict | None = None,
) -> dict:
    return {
        "id": id_,
        "entity": "payment",
        "amount": amount,
        "currency": "INR",
        "status": "captured",
        "method": method,
        "international": international,
        "card": card,
        "created_at": CREATED_AT,
    }


def _settlement(
    id_: str = "setl_1",
    amount: int = 98_230,
    status: str = "processed",
    utr: str = "UTR0001",
) -> dict:
    return {
        "id": id_,
        "entity": "settlement",
        "amount": amount,
        "status": status,
        "fees": 0,
        "tax": 0,
        "utr": utr,
        "created_at": SETTLED_AT,
    }


def _map(recon, settlements=None, payments=None):
    return map_run(
        recon_rows=recon,
        settlements=settlements if settlements is not None else [_settlement()],
        payments=payments if payments is not None else [_payment_entity()],
        merchant_id=MERCHANT,
        mcc=MCC,
    )


# ---------------------------------------------------------------------------
# The happy path, in exact paise.
# ---------------------------------------------------------------------------


def test_a_domestic_upi_payment_maps_to_a_payment_fee_line_and_tax_line():
    run = _map([_recon_payment()])

    assert len(run.payments) == 1
    payment = run.payments[0]
    assert payment.id == "pay_A1"
    assert payment.amount == Money(100_000)
    assert payment.method is PaymentMethod.UPI
    assert payment.merchant_id == MERCHANT
    assert payment.mcc == MCC
    assert payment.settlement_id == "setl_1"

    assert len(run.fee_lines) == 1
    assert run.fee_lines[0].computed_amount == Money(1_500)
    assert run.fee_lines[0].fee_type is FeeType.MDR
    assert run.fee_lines[0].applies_to_id == "pay_A1"

    assert len(run.tax_lines) == 1
    assert run.tax_lines[0].base_amount == Money(1_500)
    assert run.tax_lines[0].amount == Money(270)


def test_amounts_are_exact_integer_paise_never_rounded():
    # A deliberately un-round fee: nothing in the mapping may quantise it.
    run = _map([_recon_payment(amount=99_999, fee=1_777, tax=319)])
    assert run.payments[0].amount.paise == 99_999
    assert run.fee_lines[0].computed_amount.paise == 1_777
    assert run.tax_lines[0].amount.paise == 319


def test_timestamps_become_ist_datetimes():
    run = _map([_recon_payment()])
    captured = run.payments[0].captured_at
    assert captured.tzinfo is not None
    assert captured.utcoffset().total_seconds() == 5.5 * 3600


# ---------------------------------------------------------------------------
# The fee/tax convention, decided by the data rather than assumed.
# ---------------------------------------------------------------------------


def test_fee_excluding_tax_is_detected_from_the_rows_own_credit():
    # credit == amount - fee - tax, so `fee` is the pre-tax fee.
    run = _map([_recon_payment(amount=100_000, fee=1_500, tax=270, credit=98_230)])

    assert run.fee_convention is FeeConvention.FEE_EXCLUDES_TAX
    assert run.fee_lines[0].computed_amount == Money(1_500)
    assert run.tax_lines[0].base_amount == Money(1_500)
    assert run.tax_lines[0].amount == Money(270)


def test_fee_including_tax_is_detected_and_split_back_out():
    # credit == amount - fee, so `fee` already contains the tax and the
    # pre-tax fee is fee - tax. Emitting FeeLine(1_770) here would
    # overstate every fee in the run by exactly the GST.
    run = _map([_recon_payment(amount=100_000, fee=1_770, tax=270, credit=98_230)])

    assert run.fee_convention is FeeConvention.FEE_INCLUDES_TAX
    assert run.fee_lines[0].computed_amount == Money(1_500)
    assert run.tax_lines[0].base_amount == Money(1_500)
    assert run.tax_lines[0].amount == Money(270)


def test_a_row_whose_credit_matches_neither_convention_is_quarantined():
    run = _map([_recon_payment(amount=100_000, fee=1_500, tax=270, credit=55_555)])

    assert run.payments == []
    assert [q.reason for q in run.quarantined] == [QuarantineReason.CREDIT_DOES_NOT_RECONCILE]


def test_the_tax_lines_derived_rate_matches_the_amounts_it_describes():
    # rate_bps is required by TaxLine but Razorpay reports only amounts.
    # It is derived from the two exact amounts, never used as an input to
    # any comparison (core/verify.py diffs tax AMOUNTS), and must describe
    # them faithfully: 270 on 1_500 is 18%.
    run = _map([_recon_payment(fee=1_500, tax=270)])
    assert run.tax_lines[0].rate_bps == 1_800


# ---------------------------------------------------------------------------
# Quarantine: what Assay cannot faithfully represent, it must not invent.
# ---------------------------------------------------------------------------


def test_an_international_payment_is_quarantined_as_multi_component():
    recon = [_recon_payment(method="card", card_network="Visa", card_type="credit")]
    payments = [
        _payment_entity(method="card", international=True, card={"network": "Visa", "type": "credit"})
    ]
    run = _map(recon, payments=payments)

    assert run.payments == []
    assert run.fee_lines == []
    assert [q.reason for q in run.quarantined] == [QuarantineReason.MULTI_COMPONENT_FEE]


def test_a_card_network_outside_assays_enum_is_quarantined_not_coerced():
    for network in ("Maestro", "Diners Club", "Unknown"):
        recon = [_recon_payment(method="card", card_network=network, card_type="credit")]
        payments = [
            _payment_entity(method="card", card={"network": network, "type": "credit"})
        ]
        run = _map(recon, payments=payments)

        assert run.payments == [], f"{network} must not be coerced into a Network member"
        assert [q.reason for q in run.quarantined] == [QuarantineReason.UNREPRESENTABLE_CARD_NETWORK]


def test_a_known_card_network_maps_through():
    recon = [_recon_payment(method="card", card_network="MasterCard", card_type="debit")]
    payments = [_payment_entity(method="card", card={"network": "MasterCard", "type": "debit"})]
    run = _map(recon, payments=payments)

    assert run.payments[0].network is Network.MASTERCARD
    assert run.payments[0].card_type is CardType.DEBIT


def test_an_emi_payment_is_quarantined_as_multi_component():
    recon = [_recon_payment(method="emi", card_network="Visa", card_type="credit")]
    payments = [_payment_entity(method="emi", card={"network": "Visa", "type": "credit"})]
    run = _map(recon, payments=payments)

    assert run.payments == []
    assert [q.reason for q in run.quarantined] == [QuarantineReason.MULTI_COMPONENT_FEE]


def test_a_transfer_row_is_quarantined_as_an_unsupported_type():
    row = {**_recon_payment(entity_id="trf_1"), "type": "transfer"}
    run = _map([row])

    assert run.payments == []
    assert [q.reason for q in run.quarantined] == [QuarantineReason.UNSUPPORTED_TYPE]


def test_an_unsettled_row_is_quarantined():
    run = _map([_recon_payment(settled=False, settlement_id=None, settlement_utr=None)])

    assert run.payments == []
    assert [q.reason for q in run.quarantined] == [QuarantineReason.UNSETTLED]


def test_a_recon_row_with_no_matching_payment_entity_is_quarantined():
    # international/card live only on the payment entity; without it the
    # single-component decision cannot be made at all.
    run = _map([_recon_payment(entity_id="pay_MISSING")], payments=[])

    assert run.payments == []
    assert [q.reason for q in run.quarantined] == [QuarantineReason.MISSING_PAYMENT_DETAIL]


def test_quarantined_rows_record_the_id_so_the_gap_is_nameable():
    run = _map([_recon_payment(entity_id="pay_ODD", credit=1)])
    assert run.quarantined[0].entity_id == "pay_ODD"


# ---------------------------------------------------------------------------
# Refunds and adjustments.
# ---------------------------------------------------------------------------


def test_a_refund_row_maps_to_a_refund():
    row = {
        **_recon_payment(entity_id="rfnd_1"),
        "type": "refund",
        "payment_id": "pay_A1",
        "debit": 25_000,
        "credit": 0,
        "amount": 25_000,
        "fee": 0,
        "tax": 0,
    }
    run = _map([_recon_payment(), row])

    assert len(run.refunds) == 1
    assert run.refunds[0].id == "rfnd_1"
    assert run.refunds[0].payment_id == "pay_A1"
    assert run.refunds[0].amount == Money(25_000)


def test_an_adjustment_takes_its_kind_from_the_direction_of_the_money():
    credit_row = {
        **_recon_payment(entity_id="adj_C"),
        "type": "adjustment", "debit": 0, "credit": 5_000, "amount": 5_000, "fee": 0, "tax": 0,
    }
    debit_row = {
        **_recon_payment(entity_id="adj_D"),
        "type": "adjustment", "debit": 7_000, "credit": 0, "amount": 7_000, "fee": 0, "tax": 0,
    }
    run = _map([_recon_payment(), credit_row, debit_row])

    by_id = {a.id: a for a in run.adjustments}
    assert by_id["adj_C"].kind is AdjustmentKind.MANUAL_CREDIT
    assert by_id["adj_D"].kind is AdjustmentKind.MANUAL_DEBIT


# ---------------------------------------------------------------------------
# Settlements: batches and the stand-in bank statement.
# ---------------------------------------------------------------------------


def test_a_processed_settlement_becomes_a_batch_and_a_bank_credit():
    run = _map([_recon_payment()])

    assert len(run.batches) == 1
    assert run.batches[0].id == "setl_1"
    assert run.batches[0].utr == "UTR0001"
    assert run.batches[0].status is BatchStatus.SETTLED
    assert run.batches[0].expected_credit == Money(98_230)

    assert len(run.bank_credits) == 1
    assert run.bank_credits[0].utr == "UTR0001"
    assert run.bank_credits[0].amount == Money(98_230)


def test_a_settlement_no_recon_row_references_is_left_out_entirely():
    # A payout at a month boundary settling the previous month's
    # transactions. We hold none of its records, so emitting the credit
    # would report its whole amount as unexplained -- a fetch-window
    # artefact presented as missing money.
    run = _map([_recon_payment()], settlements=[_settlement(), _settlement(id_="setl_OTHER", utr="UTR9999")])

    assert [b.id for b in run.batches] == ["setl_1"]
    assert [c.utr for c in run.bank_credits] == ["UTR0001"]


def test_a_failed_settlement_produces_no_bank_credit():
    # No money moved, so there is nothing to decompose against. Recorded
    # rather than silently dropped.
    run = _map([_recon_payment()], settlements=[_settlement(status="failed")])

    assert run.bank_credits == []
    assert run.batches == []
    assert any(q.reason is QuarantineReason.SETTLEMENT_NOT_PROCESSED for q in run.quarantined)


# ---------------------------------------------------------------------------
# The whole point: the mapped records must satisfy invariant 3.
# ---------------------------------------------------------------------------


def test_the_mapped_records_net_exactly_to_the_settlement_amount():
    """If this fails, every audit built on this mapping reports phantom
    unexplained rupees that are really a mapping bug."""
    rows = [
        _recon_payment(entity_id="pay_1", amount=100_000, fee=1_500, tax=270),
        _recon_payment(entity_id="pay_2", amount=50_000, fee=750, tax=135),
    ]
    payments = [_payment_entity(id_="pay_1"), _payment_entity(id_="pay_2", amount=50_000)]
    net = (100_000 - 1_500 - 270) + (50_000 - 750 - 135)
    run = _map(rows, settlements=[_settlement(amount=net)], payments=payments)

    gross = sum(p.amount.paise for p in run.payments)
    fees = sum(f.computed_amount.paise for f in run.fee_lines)
    taxes = sum(t.amount.paise for t in run.tax_lines)
    refunds = sum(r.amount.paise for r in run.refunds)

    assert gross - fees - taxes - refunds == run.bank_credits[0].amount.paise == net


def test_a_settlement_with_one_unmappable_row_is_dropped_whole():
    """The bug this pins: emitting the full payout as a bank credit while
    quarantining one of its transactions makes the difference surface as
    `unexplained_paise` in the audit -- money reported as unaccounted-for
    that is not missing, only unmapped. Every earlier test in this file
    mapped 100% of rows and would have passed with that bug present."""
    ok = _recon_payment(entity_id="pay_ok", amount=100_000, fee=1_500, tax=270)
    intl = _recon_payment(entity_id="pay_intl", amount=500_000, fee=12_000, tax=2_160,
                          method="card", card_network="Visa", card_type="credit")
    payments = [
        _payment_entity(id_="pay_ok"),
        _payment_entity(id_="pay_intl", amount=500_000, method="card", international=True,
                        card={"network": "Visa", "type": "credit"}),
    ]
    net = (100_000 - 1_500 - 270) + (500_000 - 12_000 - 2_160)
    run = _map([ok, intl], settlements=[_settlement(amount=net)], payments=payments)

    assert run.payments == [], "the cleanly-mapped payment goes too; a partial payout is not auditable"
    assert run.bank_credits == [], "no credit may be emitted that the mapped records cannot fully explain"
    assert run.batches == []
    assert run.unaudited_paise == net, "the payout is reported as not-looked-at, not as unexplained"

    reasons = {q.reason for q in run.quarantined}
    assert QuarantineReason.MULTI_COMPONENT_FEE in reasons
    assert QuarantineReason.SETTLEMENT_INCOMPLETE in reasons
    assert {q.entity_id for q in run.quarantined} == {"pay_ok", "pay_intl"}


def test_one_settlements_bad_row_never_costs_a_different_settlement():
    good = _recon_payment(entity_id="pay_good", settlement_id="setl_good", settlement_utr="UTR-GOOD")
    bad = _recon_payment(entity_id="pay_bad", settlement_id="setl_bad", settlement_utr="UTR-BAD",
                         method="card", card_network="Diners Club", card_type="credit")
    payments = [
        _payment_entity(id_="pay_good"),
        _payment_entity(id_="pay_bad", method="card", card={"network": "Diners Club", "type": "credit"}),
    ]
    settlements = [
        _settlement(id_="setl_good", amount=98_230, utr="UTR-GOOD"),
        _settlement(id_="setl_bad", amount=98_230, utr="UTR-BAD"),
    ]
    run = _map([good, bad], settlements=settlements, payments=payments)

    assert [p.id for p in run.payments] == ["pay_good"]
    assert [c.utr for c in run.bank_credits] == ["UTR-GOOD"]
    assert run.unaudited_paise == 98_230


def test_a_row_whose_settlement_produced_no_credit_is_not_left_unclaimed():
    # A payout settled outside the fetched window. Emitting its
    # transactions would leave records no credit claims, which lands in
    # the audit's other phantom bucket, `unclaimed_paise`.
    run = _map([_recon_payment()], settlements=[])

    assert run.payments == []
    assert [q.reason for q in run.quarantined] == [QuarantineReason.SETTLEMENT_INCOMPLETE]


def test_mapping_an_empty_month_is_not_an_error():
    run = map_run(recon_rows=[], settlements=[], payments=[], merchant_id=MERCHANT, mcc=MCC)
    assert run.payments == []
    assert run.bank_credits == []
    assert run.quarantined == []
