"""C05 -- rate revision mid-cycle.

core/contract.py's effective-dating is half-open ([effective_from,
effective_to), never inclusive-to), validated so a `supersedes` link must
be a clean, contiguous hand-over with no gap or overlap. This proves a
transaction captured the instant before a mid-cycle revision gets the OLD
rate and one captured at (or after) the revision instant gets the NEW
rate, against a hand-computed expected value -- not just "some fee was
applied."
"""

from __future__ import annotations

from datetime import date, datetime

from chaos.incident import chaos_scenario
from core.contract import AppliesWhen, CompiledContract, FeeRule, RateCardParse
from core.models import IST, FeeType, Payment, PaymentMethod
from core.money import Money

MERCHANT = "MERCH-0001"
REVISION_DATE = date(2026, 7, 16)

OLD_RATE_BPS = 100  # 1.00%, in force through July 15
NEW_RATE_BPS = 150  # 1.50%, in force from July 16

JUST_BEFORE = datetime(2026, 7, 15, 23, 59, 59, 999999, tzinfo=IST)
AT_REVISION = datetime(2026, 7, 16, 0, 0, 0, tzinfo=IST)

AMOUNT_PAISE = 100_000


def _contract() -> CompiledContract:
    old = FeeRule(
        rule_id="v1.upi",
        fee_type=FeeType.MDR,
        effective_from=date(2026, 7, 1),
        effective_to=REVISION_DATE,
        applies_when=AppliesWhen(method=PaymentMethod.UPI),
        rate_bps=OLD_RATE_BPS,
        taxes=[],
        source_quote="UPI at 1.00%, through July 15",
    )
    new = FeeRule(
        rule_id="v2.upi",
        fee_type=FeeType.MDR,
        effective_from=REVISION_DATE,
        supersedes="v1.upi",
        applies_when=AppliesWhen(method=PaymentMethod.UPI),
        rate_bps=NEW_RATE_BPS,
        taxes=[],
        source_quote="UPI revised to 1.50%, from July 16",
    )
    return CompiledContract.from_parse(RateCardParse(merchant_id=MERCHANT, rules=[old, new]))


def _payment(id_: str, captured_at: datetime) -> Payment:
    return Payment(
        id=id_,
        merchant_id=MERCHANT,
        amount=Money(AMOUNT_PAISE),
        method=PaymentMethod.UPI,
        network=None,
        card_type=None,
        is_international=False,
        mcc="5411",
        captured_at=captured_at,
        settlement_id="STL-1",
    )


def test_c05_a_mid_cycle_revision_splits_the_fee_correctly_at_the_boundary():
    with chaos_scenario(
        "C05",
        title="Rate revision mid-cycle",
        category="degraded_gracefully",
        failure_injected=f"a UPI MDR rate revises from {OLD_RATE_BPS}bps to {NEW_RATE_BPS}bps at {REVISION_DATE}",
        expected_behavior="effective dating produces the right split, matching a hand-computed expected fee",
    ) as scenario:
        contract = _contract()

        before = contract.fee_for(_payment("PAY-BEFORE", JUST_BEFORE), at=JUST_BEFORE)
        after = contract.fee_for(_payment("PAY-AFTER", AT_REVISION), at=AT_REVISION)

        expected_before = AMOUNT_PAISE * OLD_RATE_BPS // 10_000
        expected_after = AMOUNT_PAISE * NEW_RATE_BPS // 10_000

        assert before.total_fee == Money(expected_before)
        assert after.total_fee == Money(expected_after)
        assert before.total_fee != after.total_fee, "the fixture must actually straddle the revision"

        scenario.note(
            f"just-before={before.total_fee.to_rupees_str()} (rule v1.upi), "
            f"at-revision={after.total_fee.to_rupees_str()} (rule v2.upi)"
        )
