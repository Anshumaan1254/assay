"""Structured rate card: tiered MDR, fixed fees, per-tier caps, an
international surcharge, GST, a dormant TDS clause, and an effective-dated
mid-month revision.

This is datagen's own reference calculator, independent of core/contract.py
(still a stub — nothing here imports from core.contract). It computes both
the "true" fee/tax lines datagen/world.py assembles and, via
datagen/inject.py, the deliberately wrong ones planted for D01/D02/D03/D10.

All arithmetic is Decimal + explicit rounding mode, never float, even though
core/'s float ban doesn't reach datagen/ — the entire point of this dataset
is exact-paise correctness that stays distinguishable from the deliberately
injected D07 rounding-drift case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from core.models import IST, CardType, PaymentMethod

_BPS_DENOMINATOR = Decimal(10_000)


def _bps_of(amount_paise: int, bps: int, rounding: str) -> int:
    raw = (Decimal(amount_paise) * Decimal(bps) / _BPS_DENOMINATOR).quantize(Decimal(1), rounding=rounding)
    return int(raw)


def mdr_raw_amount(gross_paise: int, bps: int, rounding: str = ROUND_HALF_UP) -> int:
    """bps of gross, rounded, with no cap applied — the figure a cap (if any)
    would clamp. Also what D03's injector uses directly to produce the
    uncapped-when-it-shouldn't-be-uncapped amount."""
    return _bps_of(gross_paise, bps, rounding)


def gst_amount(base_paise: int, gst_bps: int, rounding: str = ROUND_HALF_UP) -> int:
    """GST on `base_paise` — always the fee amount, never the transaction
    gross. D02's injector is exactly this function called with the wrong
    `base_paise`."""
    return _bps_of(base_paise, gst_bps, rounding)


@dataclass(frozen=True)
class MDRTier:
    min_paise: int
    max_paise: int | None  # None = unbounded; half-open [min_paise, max_paise)
    bps: int
    cap_paise: int | None = None


def mdr_amount(gross_paise: int, tier: MDRTier, rounding: str = ROUND_HALF_UP) -> int:
    amount = mdr_raw_amount(gross_paise, tier.bps, rounding)
    if tier.cap_paise is not None:
        amount = min(amount, tier.cap_paise)
    return amount


@dataclass(frozen=True)
class MethodFeeRule:
    method: PaymentMethod
    card_type: CardType | None
    tiers: tuple[MDRTier, ...]
    fixed_fee_paise: int
    international_surcharge_bps: int = 0

    def tier_for(self, gross_paise: int) -> MDRTier:
        for tier in self.tiers:
            if gross_paise >= tier.min_paise and (tier.max_paise is None or gross_paise < tier.max_paise):
                return tier
        raise ValueError(f"no tier in {self.method}/{self.card_type} matches gross_paise={gross_paise}")


@dataclass(frozen=True)
class TDSClause:
    rate_bps: int
    active: bool
    applies_to: str


def compute_tds(gross_paise: int, clause: TDSClause) -> int:
    """Pure arithmetic — rate_bps of gross. Deliberately ignores `active`:
    whether this figure gets wired into any output is the caller's decision,
    not this function's."""
    return _bps_of(gross_paise, clause.rate_bps, ROUND_HALF_UP)


@dataclass(frozen=True)
class RateCardVersion:
    version_id: str
    effective_from: datetime
    gst_bps: int
    fee_rules: tuple[MethodFeeRule, ...]
    tds: TDSClause

    def rule_for(self, method: PaymentMethod, card_type: CardType | None) -> MethodFeeRule:
        for rule in self.fee_rules:
            if rule.method == method and rule.card_type == card_type:
                return rule
        raise KeyError(f"no fee rule for method={method}, card_type={card_type} in {self.version_id}")


@dataclass(frozen=True)
class RateCard:
    versions: tuple[RateCardVersion, ...]  # any order; version_for sorts by effective_from

    def version_for(self, at: datetime) -> RateCardVersion:
        applicable = [v for v in self.versions if v.effective_from <= at]
        if not applicable:
            raise ValueError(f"no rate card version is effective at {at.isoformat()}")
        return max(applicable, key=lambda v: v.effective_from)


def _card_rule(card_type: CardType, tier1_bps: int) -> MethodFeeRule:
    if card_type is CardType.CREDIT:
        tiers = (
            MDRTier(min_paise=0, max_paise=200_000, bps=tier1_bps),
            MDRTier(min_paise=200_000, max_paise=1_000_000, bps=160),
            MDRTier(min_paise=1_000_000, max_paise=None, bps=140),
        )
    else:
        tiers = (
            MDRTier(min_paise=0, max_paise=200_000, bps=tier1_bps),
            MDRTier(min_paise=200_000, max_paise=1_000_000, bps=80),
            MDRTier(min_paise=1_000_000, max_paise=None, bps=70, cap_paise=15_000),
        )
    return MethodFeeRule(
        method=PaymentMethod.CARD,
        card_type=card_type,
        tiers=tiers,
        fixed_fee_paise=0,
        international_surcharge_bps=200,
    )


def _flat_rule(method: PaymentMethod, bps: int, fixed_fee_paise: int) -> MethodFeeRule:
    return MethodFeeRule(
        method=method,
        card_type=None,
        tiers=(MDRTier(min_paise=0, max_paise=None, bps=bps),),
        fixed_fee_paise=fixed_fee_paise,
    )


_TDS_CLAUSE = TDSClause(
    rate_bps=100,
    active=False,
    applies_to="e-commerce marketplace operator transactions under Section 194-O",
)


def default_rate_card(month: str = "2026-07") -> RateCard:
    """The illustrative Indian PA/PG rate card used for the realistic-profile
    generated month: tiered card MDR, flat UPI/netbanking/wallet fees, an
    international surcharge, 18% GST, a dormant TDS clause, and a mid-month
    revision effective the 16th that moves credit up and debit down (so D10
    can produce both FEE_OVERCHARGE and FEE_UNDERCHARGE instances)."""
    year, mon = (int(part) for part in month.split("-"))

    def rules(credit_tier1_bps: int, debit_tier1_bps: int) -> tuple[MethodFeeRule, ...]:
        return (
            _card_rule(CardType.CREDIT, credit_tier1_bps),
            _card_rule(CardType.DEBIT, debit_tier1_bps),
            _flat_rule(PaymentMethod.UPI, bps=0, fixed_fee_paise=200),
            _flat_rule(PaymentMethod.NETBANKING, bps=175, fixed_fee_paise=1_000),
            _flat_rule(PaymentMethod.WALLET, bps=190, fixed_fee_paise=300),
        )

    v1 = RateCardVersion(
        version_id="v1",
        effective_from=datetime(year, mon, 1, tzinfo=IST),
        gst_bps=1_800,
        fee_rules=rules(credit_tier1_bps=180, debit_tier1_bps=90),
        tds=_TDS_CLAUSE,
    )
    v2 = RateCardVersion(
        version_id="v2",
        effective_from=datetime(year, mon, 16, tzinfo=IST),
        gst_bps=1_800,
        fee_rules=rules(credit_tier1_bps=195, debit_tier1_bps=85),
        tds=_TDS_CLAUSE,
    )
    return RateCard(versions=(v1, v2))


def _rupees(paise: int) -> str:
    return f"{paise / 100:,.2f}"


_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _format_date(dt: datetime) -> str:
    # datetime.strftime's day-without-leading-zero code ("%-d"/"%#d") is
    # platform-specific (glibc vs MSVCRT) — format by hand instead.
    return f"{dt.day} {_MONTH_NAMES[dt.month - 1]} {dt.year}"


def render_markdown(rate_card: RateCard, merchant_id: str = "MERCH-0001") -> str:
    """Renders as prose + tables, deliberately a little rough — the way an
    actual business-team rate-card document reads, since this is what
    llm/contract_parser.py will eventually have to parse."""
    v1, v2 = sorted(rate_card.versions, key=lambda v: v.effective_from)
    credit1 = v1.rule_for(PaymentMethod.CARD, CardType.CREDIT)
    debit1 = v1.rule_for(PaymentMethod.CARD, CardType.DEBIT)
    upi = v1.rule_for(PaymentMethod.UPI, None)
    netbanking = v1.rule_for(PaymentMethod.NETBANKING, None)
    wallet = v1.rule_for(PaymentMethod.WALLET, None)
    credit1_v2 = v2.rule_for(PaymentMethod.CARD, CardType.CREDIT)
    debit1_v2 = v2.rule_for(PaymentMethod.CARD, CardType.DEBIT)

    c_t1, c_t2, c_t3 = credit1.tiers
    d_t1, d_t2, d_t3 = debit1.tiers

    effective_date = _format_date(v1.effective_from)
    revision_date = _format_date(v2.effective_from)

    return f"""# Merchant Rate Card - {merchant_id}

Effective {effective_date} unless superseded below. All fees exclusive of GST.

## Card MDR

| Card Type | Slab | Rate |
|---|---|---|
| Credit | Up to Rs.{_rupees(c_t1.max_paise)} | {c_t1.bps / 100:.2f}% |
| Credit | Rs.{_rupees(c_t2.min_paise)} - Rs.{_rupees(c_t2.max_paise)} | {c_t2.bps / 100:.2f}% |
| Credit | Above Rs.{_rupees(c_t3.min_paise)} | {c_t3.bps / 100:.2f}% |
| Debit  | Up to Rs.{_rupees(d_t1.max_paise)} | {d_t1.bps / 100:.2f}% |
| Debit  | Rs.{_rupees(d_t2.min_paise)} - Rs.{_rupees(d_t2.max_paise)} | {d_t2.bps / 100:.2f}% |
| Debit  | Above Rs.{_rupees(d_t3.min_paise)} | {d_t3.bps / 100:.2f}%, capped at Rs.{_rupees(d_t3.cap_paise)} per transaction |

## UPI / Netbanking / Wallet
UPI: no MDR, flat Rs.{_rupees(upi.fixed_fee_paise)}/txn processing fee.
Netbanking: {netbanking.tier_for(0).bps / 100:.2f}% + Rs.{_rupees(netbanking.fixed_fee_paise)}/txn.
Wallet: {wallet.tier_for(0).bps / 100:.2f}% + Rs.{_rupees(wallet.fixed_fee_paise)}/txn.

## International transactions
+{credit1.international_surcharge_bps / 100:.2f}% surcharge on gross, card transactions only.

## Taxes
GST @ {v1.gst_bps / 100:.0f}% applies on all fee lines above.

## TDS (Section 194-O)
Not applicable - merchant is not classified as an e-commerce marketplace
operator under the current registration on file. ({v1.tds.applies_to})

---
### Addendum - effective {revision_date}
Credit card slab 1 (up to Rs.{_rupees(c_t1.max_paise)}) revised from {c_t1.bps / 100:.2f}% to {credit1_v2.tier_for(0).bps / 100:.2f}%.
Debit card slab 1 (up to Rs.{_rupees(d_t1.max_paise)}) revised from {d_t1.bps / 100:.2f}% to {debit1_v2.tier_for(0).bps / 100:.2f}%.
All other rates unchanged.
"""
