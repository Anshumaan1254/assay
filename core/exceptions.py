"""Taxonomy, clustering, pricing.

Adding a member here is one of five coordinated steps — see
.claude/skills/new-discrepancy-class/SKILL.md. Do all five or none.
"""

from __future__ import annotations

from enum import StrEnum


class DiscrepancyClass(StrEnum):
    """The settlement discrepancies this engine can name.

    Each member is a real-world cause, not a symptom — `core/verify.py`'s
    detection rules and `datagen/inject.py`'s planted-truth injectors both
    key off these values, so they must stay stable once in use.
    """

    FEE_OVERCHARGE = "fee_overcharge"
    """Gateway charged more fee (MDR/fixed/international/EMI) than the
    contracted rate card produces for that transaction."""

    FEE_UNDERCHARGE = "fee_undercharge"
    """Gateway charged less fee than the contract specifies — favorable to
    the merchant, still reported: it signals a stale or misapplied rate."""

    TAX_MISCALCULATION = "tax_miscalculation"
    """Tax on a fee line does not equal rate_bps * base_amount for that
    line, e.g. GST applied at the wrong slab or against the wrong base."""

    MISSING_TRANSACTION = "missing_transaction"
    """A payment present in the merchant ledger for this cycle never
    appears in the settlement report or bank credit."""

    DUPLICATE_SETTLEMENT = "duplicate_settlement"
    """The same payment was credited more than once across settlement
    batches or bank credits."""

    REFUND_AMOUNT_MISMATCH = "refund_amount_mismatch"
    """The refund amount deducted in the settlement does not match the
    Refund record's amount on the merchant ledger."""

    CHARGEBACK_AMOUNT_MISMATCH = "chargeback_amount_mismatch"
    """The chargeback deduction, or reversal on a won dispute, does not
    match the ledger's Chargeback amount and stage."""

    UNRELEASED_RESERVE = "unreleased_reserve"
    """A reserve_hold adjustment has no matching reserve_release after the
    contracted hold period."""

    UNDOCUMENTED_ADJUSTMENT = "undocumented_adjustment"
    """A manual_credit, manual_debit, or fee_waiver adjustment appears in
    the settlement with no reason traceable to a ledger record or contract
    clause."""

    FX_MARKUP_VARIANCE = "fx_markup_variance"
    """For an is_international payment, the currency-conversion markup
    charged does not match the contracted FX rate."""

    ROUNDING_DRIFT = "rounding_drift"
    """Sub-paisa rounding choices accumulate to a small non-zero difference
    that no single fee or tax line explains on its own."""

    UNRECONCILED_RESIDUAL = "unreconciled_residual"
    """After every other class has been checked, a non-zero amount remains
    in the conservation identity's `unexplained` bucket (invariant 3)."""
