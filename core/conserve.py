"""The conservation identity, asserted.

Only one piece of it is built so far: the sign convention. `decompose.py`
needs it before the full identity exists, because "which records produced
this credit" is unanswerable without knowing which way each record moves
money.

The convention is `datagen/world.py`'s, adopted deliberately rather than
re-derived. DECISIONS.md (2026-08-23) flagged two datagen-internal choices
for whoever built this module:

  - the adjustment sign tables (`RESERVE_HOLD`/`MANUAL_DEBIT` subtract;
    `RESERVE_RELEASE`/`MANUAL_CREDIT`/`FEE_WAIVER` add back), and
  - a won chargeback's reversal being a `MANUAL_CREDIT` `Adjustment`
    rather than a dedicated entity.

Both are adopted here. The second is why chargebacks subtract at *every*
stage including `WON`: the reversal is a separate record, so making a won
chargeback positive would credit it twice. `core/` may not import
`datagen/` (invariant 5), so this is a reimplementation, not a reuse --
`tests/test_conserve.py` reconciles the two against an independently
written formula.

Summing `signed_paise` over a batch's records reproduces the identity in
CLAUDE.md invariant 3:

    gross - refunds - fees - tax - chargebacks + add_back - subtract
"""

from __future__ import annotations

from core.models import AdjustmentKind, AssayModel, EntityType

ADD_BACK_KINDS = frozenset(
    {
        AdjustmentKind.RESERVE_RELEASE,
        AdjustmentKind.MANUAL_CREDIT,
        AdjustmentKind.FEE_WAIVER,
    }
)

SUBTRACT_KINDS = frozenset(
    {
        AdjustmentKind.RESERVE_HOLD,
        AdjustmentKind.MANUAL_DEBIT,
    }
)

# Adjustments are absent: their direction is a property of the kind, not of
# the type. Every other contributing type has a fixed direction.
SIGN_BY_TYPE = {
    EntityType.PAYMENT: 1,
    EntityType.REFUND: -1,
    EntityType.CHARGEBACK: -1,
    EntityType.FEE_LINE: -1,
    EntityType.TAX_LINE: -1,
}

# FeeLine calls its money field `computed_amount`; TaxLine carries both
# `base_amount` and `amount` and only the tax itself is deducted. Naming the
# field per type beats a getattr(record, "amount") that would raise on one
# and quietly overstate the other.
MONEY_FIELD_BY_TYPE = {
    EntityType.PAYMENT: "amount",
    EntityType.REFUND: "amount",
    EntityType.CHARGEBACK: "amount",
    EntityType.ADJUSTMENT: "amount",
    EntityType.FEE_LINE: "computed_amount",
    EntityType.TAX_LINE: "amount",
}


def _sign_of(record: AssayModel) -> int:
    record_type = record.RECORD_TYPE
    if record_type is EntityType.ADJUSTMENT:
        if record.kind in ADD_BACK_KINDS:
            return 1
        if record.kind in SUBTRACT_KINDS:
            return -1
        raise ValueError(f"adjustment kind {record.kind!r} has no declared sign")
    sign = SIGN_BY_TYPE.get(record_type)
    if sign is None:
        raise ValueError(f"{record_type!r} makes no defined contribution to a settlement net")
    return sign


def money_of(record: AssayModel):
    """The single Money field that a record contributes, unsigned."""
    field = MONEY_FIELD_BY_TYPE.get(record.RECORD_TYPE)
    if field is None:
        raise ValueError(f"{record.RECORD_TYPE!r} makes no defined contribution to a settlement net")
    return getattr(record, field)


def signed_paise(record: AssayModel) -> int:
    """This record's signed contribution to a settlement net, in paise.

    Raises `ValueError` for types that make no contribution
    (`SETTLEMENT_BATCH`, `BANK_CREDIT`, `AUDIT_RUN`, `FINDING`,
    `JOURNAL_ENTRY`). Returning 0 for those would let a mis-typed record
    disappear from a sum with nothing to show for it -- and would let a
    bank credit be cited as a term in its own decomposition, which nets to
    zero and "balances" while explaining nothing.
    """
    return _sign_of(record) * money_of(record).paise
