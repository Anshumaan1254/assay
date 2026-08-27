"""Razorpay's wire JSON to Assay's domain models.

Pure functions over already-fetched dicts -- no network, no I/O, no
clock. That separation is what makes this module (the part that touches
money) exhaustively unit-testable against fixtures, while
`ingest/razorpay.py` (the part that touches the network) has no money
logic to get wrong.

Every amount Razorpay returns is already an integer in paise, so the whole
mapping is int-to-int: no Decimal, no float, no rounding, nothing for
invariant 1 to catch. `Money(row["amount"])` is the entire conversion.

**Nothing is coerced to fit.** Razorpay reports card networks Assay's
`Network` enum cannot express (Maestro, Diners Club, Unknown), and blends
a multi-component fee into a single number for international and EMI
transactions. Both are quarantined -- excluded from the run directory with
a named reason -- rather than mapped to the nearest enum member or emitted
as one blended fee line. A blended fee line would be actively harmful:
`core/verify.py` diffs fees aggregated by `FeeType`, so a blended fee typed
as MDR against a contract that recomputes MDR and INTERNATIONAL separately
produces a pair of equal-and-opposite findings that net to zero and are
both fictional. An honest "not audited, here is why" beats a confident
wrong number, which is the same discipline `core/decompose.py` applies to
an ambiguous subset-sum.

**The fee/tax convention is read, not assumed.** Razorpay documents the
Payments API's `fee` as including GST, while the recon report carries
`fee` and `tax` as separate columns. Getting this backwards misstates
every fee line in the run by exactly the tax. Rather than pick one, this
module reads each row's own published `credit`: a row where
`credit == amount - fee - tax` says `fee` is pre-tax, one where
`credit == amount - fee` says `fee` already contains it. Only rows with a
non-zero tax can tell the two apart, and a row agreeing with neither is
quarantined rather than forced.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from core.models import (
    IST,
    Adjustment,
    AdjustmentKind,
    BankCredit,
    BatchStatus,
    CardType,
    Chargeback,
    EntityType,
    FeeLine,
    FeeType,
    Network,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
    SettlementBatch,
    TaxLine,
)
from core.money import Money

# Razorpay's `card_network` spellings, mapped to the four networks Assay
# models. Maestro, Diners Club and Unknown are deliberately absent: they
# have no faithful representation, so they quarantine rather than coerce.
_NETWORKS = {
    "visa": Network.VISA,
    "mastercard": Network.MASTERCARD,
    "rupay": Network.RUPAY,
    "american express": Network.AMEX,
    "amex": Network.AMEX,
}

_CARD_TYPES = {
    "credit": CardType.CREDIT,
    "debit": CardType.DEBIT,
    "prepaid": CardType.PREPAID,
}

_METHODS = {
    "card": PaymentMethod.CARD,
    "upi": PaymentMethod.UPI,
    "netbanking": PaymentMethod.NETBANKING,
    "wallet": PaymentMethod.WALLET,
    "emi": PaymentMethod.EMI,
}

# Methods whose contracted price is more than one component, so a single
# blended `fee` cannot be diffed per-FeeType against the contract. EMI
# carries a subvention on top of MDR; an international card carries a
# cross-border surcharge (detected per-payment, not per-method, below).
_MULTI_COMPONENT_METHODS = frozenset({PaymentMethod.EMI})

_GST_BPS = 1_800


class QuarantineReason(StrEnum):
    """Fixed vocabulary. A free-text reason could not be counted, and the
    count is the point: `ingest` reports how much of a month it declined
    to audit, so a clean report over 60% of the transactions is never
    mistaken for a clean report."""

    UNSUPPORTED_TYPE = "unsupported_type"
    UNSUPPORTED_METHOD = "unsupported_method"
    UNSETTLED = "unsettled"
    MISSING_PAYMENT_DETAIL = "missing_payment_detail"
    CREDIT_DOES_NOT_RECONCILE = "credit_does_not_reconcile"
    MULTI_COMPONENT_FEE = "multi_component_fee"
    UNREPRESENTABLE_CARD_NETWORK = "unrepresentable_card_network"
    UNREPRESENTABLE_CARD_TYPE = "unrepresentable_card_type"
    SETTLEMENT_NOT_PROCESSED = "settlement_not_processed"
    SETTLEMENT_INCOMPLETE = "settlement_incomplete"


class FeeConvention(StrEnum):
    FEE_EXCLUDES_TAX = "fee_excludes_tax"
    FEE_INCLUDES_TAX = "fee_includes_tax"


class QuarantinedRow(BaseModel):
    entity_id: str
    row_type: str
    reason: QuarantineReason
    detail: str


class MappedRun(BaseModel):
    payments: list[Payment]
    refunds: list[Refund]
    chargebacks: list[Chargeback]
    adjustments: list[Adjustment]
    fee_lines: list[FeeLine]
    tax_lines: list[TaxLine]
    batches: list[SettlementBatch]
    bank_credits: list[BankCredit]
    quarantined: list[QuarantinedRow]
    fee_convention: FeeConvention
    # The net rupee value of settlements this mapping declined to attempt.
    # NOT unexplained money -- money nobody looked at. Reported separately
    # and loudly for exactly that reason; see `map_run`'s docstring.
    unaudited_paise: int = 0

    @property
    def mapped_count(self) -> int:
        return len(self.payments) + len(self.refunds) + len(self.chargebacks) + len(self.adjustments)


def _ist(unix_seconds: Any) -> datetime:
    return datetime.fromtimestamp(int(unix_seconds), tz=IST)


def _int(value: Any) -> int:
    """Razorpay's money fields are integers already; a null is zero. Never
    float() -- a float here would be a silent precision bug in exactly the
    place invariant 1 exists to prevent."""
    return 0 if value is None else int(value)


def detect_fee_convention(recon_rows: list[dict[str, Any]]) -> FeeConvention:
    """Which convention this account's recon report follows.

    Only a row with a non-zero tax discriminates: when tax is zero,
    `amount - fee` and `amount - fee - tax` are the same number and the row
    is consistent with both. With no discriminating row anywhere, the
    choice is immaterial (there is no tax to misplace) and the pre-tax
    reading is returned.
    """
    excludes = includes = 0
    for row in recon_rows:
        if row.get("type") != "payment":
            continue
        amount, fee, tax = _int(row.get("amount")), _int(row.get("fee")), _int(row.get("tax"))
        if tax == 0:
            continue
        credit = _int(row.get("credit"))
        if amount - fee - tax == credit:
            excludes += 1
        elif amount - fee == credit:
            includes += 1
    # A tie, or no evidence either way, resolves to the pre-tax reading;
    # rows that then fail to reconcile against it are quarantined
    # individually rather than silently re-interpreted.
    return FeeConvention.FEE_INCLUDES_TAX if includes > excludes else FeeConvention.FEE_EXCLUDES_TAX


def _fee_and_tax(row: dict[str, Any], convention: FeeConvention) -> tuple[int, int]:
    """The pre-tax fee and the tax on it, whichever way Razorpay reported."""
    fee, tax = _int(row.get("fee")), _int(row.get("tax"))
    if convention is FeeConvention.FEE_INCLUDES_TAX:
        return fee - tax, tax
    return fee, tax


def map_run(
    *,
    recon_rows: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
    payments: list[dict[str, Any]],
    merchant_id: str,
    mcc: str,
) -> MappedRun:
    """Every settled transaction Assay can faithfully represent, plus a
    named reason for every one it cannot.

    `mcc` is a parameter because no Razorpay endpoint returns it -- it is
    a property of the merchant's business category, not of a transaction,
    and inventing a per-payment value would be exactly the kind of made-up
    business rule this project refuses.

    **A settlement is audited whole or not at all.** A bank credit is the
    full payout; the records mapped out of it are what explain that payout.
    Emitting the credit while quarantining even one of its transactions
    makes the difference surface as `unexplained_paise` in the audit --
    money reported as unaccounted-for that is not missing at all, merely
    unmapped. That is the worst failure this codebase has, because
    `unexplained` is the one number the whole product asks to be trusted,
    and a real shortfall becomes unfindable once that bucket carries
    tooling artefacts. So a settlement with any unmappable row is dropped
    entire -- its batch, its credit, and its already-mapped transactions --
    and its payout is counted in `unaudited_paise` instead, which is a
    different claim: not "we cannot account for this", but "we did not
    look at this, here is how much."
    """
    convention = detect_fee_convention(recon_rows)
    by_payment_id = {p["id"]: p for p in payments}

    quarantined: list[QuarantinedRow] = []
    # Per settlement, so an unmappable row can take its whole payout with
    # it rather than leaving a partially-explained credit behind.
    staged: dict[str, dict[str, list]] = {}
    incomplete: set[str] = set()

    def _stage(settlement_id: str) -> dict[str, list]:
        return staged.setdefault(
            settlement_id,
            {"payments": [], "refunds": [], "adjustments": [], "fee_lines": [], "tax_lines": []},
        )

    def drop(row: dict[str, Any], reason: QuarantineReason, detail: str) -> None:
        quarantined.append(
            QuarantinedRow(
                entity_id=str(row.get("entity_id", "")),
                row_type=str(row.get("type", "")),
                reason=reason,
                detail=detail,
            )
        )
        settlement_id = row.get("settlement_id")
        if settlement_id:
            incomplete.add(str(settlement_id))

    batches, credits = _map_settlements(settlements, recon_rows, merchant_id, quarantined)
    creditable = {batch.id for batch in batches}

    for row in recon_rows:
        row_type = row.get("type")
        entity_id = str(row.get("entity_id", ""))
        settlement_id = row.get("settlement_id")

        if row_type not in ("payment", "refund", "adjustment"):
            drop(row, QuarantineReason.UNSUPPORTED_TYPE, f"row type {row_type!r} has no Assay entity")
            continue
        if not row.get("settled") or not settlement_id:
            drop(row, QuarantineReason.UNSETTLED, "not attached to a settlement, so no credit to audit")
            continue
        if str(settlement_id) not in creditable:
            # Its payout produced no bank credit (unprocessed, or settled
            # outside the fetched window), so there is nothing for this row
            # to be decomposed against. Emitting it anyway would leave an
            # unclaimed record, which lands in the audit's OTHER phantom
            # bucket, `unclaimed_paise`.
            drop(
                row,
                QuarantineReason.SETTLEMENT_INCOMPLETE,
                f"settlement {settlement_id} produced no auditable bank credit",
            )
            continue

        bucket = _stage(str(settlement_id))

        if row_type == "payment":
            mapped = _map_payment(row, by_payment_id.get(entity_id), convention, merchant_id, mcc, drop)
            if mapped is None:
                continue
            payment, fee_line, tax_line = mapped
            bucket["payments"].append(payment)
            if fee_line is not None:
                bucket["fee_lines"].append(fee_line)
            if tax_line is not None:
                bucket["tax_lines"].append(tax_line)

        elif row_type == "refund":
            bucket["refunds"].append(
                Refund(
                    id=entity_id,
                    payment_id=str(row.get("payment_id") or ""),
                    amount=Money(_int(row.get("amount"))),
                    # Razorpay's recon report does not say whether a refund
                    # was partial; the flag is descriptive only (nothing in
                    # core/ branches on it) so it is reported as False
                    # rather than guessed from an amount comparison that
                    # would need the payment's own gross to be meaningful.
                    is_partial=False,
                    created_at=_ist(row.get("created_at")),
                    settlement_id=str(settlement_id),
                )
            )

        else:  # adjustment
            credit, debit = _int(row.get("credit")), _int(row.get("debit"))
            bucket["adjustments"].append(
                Adjustment(
                    id=entity_id,
                    # Razorpay reports no adjustment sub-type, so the only
                    # honest reading is the direction the money actually
                    # moved. A reserve hold and a manual debit are
                    # indistinguishable here; both are recorded as the
                    # manual form rather than a guessed reserve.
                    kind=AdjustmentKind.MANUAL_CREDIT if credit >= debit else AdjustmentKind.MANUAL_DEBIT,
                    amount=Money(_int(row.get("amount"))),
                    reason=str(row.get("description") or "razorpay adjustment"),
                    created_at=_ist(row.get("created_at")),
                    settlement_id=str(settlement_id),
                )
            )

    # Every settlement that lost a row loses its whole payout, including
    # the rows that mapped cleanly. Partially-explained credits are the
    # thing this refuses to emit.
    by_settlement = {batch.id: batch for batch in batches}
    unaudited_paise = 0
    for settlement_id in sorted(incomplete & staged.keys()):
        bucket = staged.pop(settlement_id)
        batch = by_settlement.get(settlement_id)
        if batch is not None:
            unaudited_paise += batch.expected_credit.paise
        for kind, records in bucket.items():
            for record in records:
                if kind in ("fee_lines", "tax_lines"):
                    continue  # reported under the payment they hang off
                quarantined.append(
                    QuarantinedRow(
                        entity_id=record.id,
                        row_type=record.RECORD_TYPE.value,
                        reason=QuarantineReason.SETTLEMENT_INCOMPLETE,
                        detail=(
                            f"mapped cleanly, but settlement {settlement_id} had at least one row this "
                            "tool cannot represent, so the whole payout is left unaudited rather than "
                            "reported as partly unexplained"
                        ),
                    )
                )

    audited = staged.keys()
    return MappedRun(
        payments=[p for sid in sorted(audited) for p in staged[sid]["payments"]],
        refunds=[r for sid in sorted(audited) for r in staged[sid]["refunds"]],
        chargebacks=[],  # /v1/disputes is a separate integration; see ingest/cli.py
        adjustments=[a for sid in sorted(audited) for a in staged[sid]["adjustments"]],
        fee_lines=[f for sid in sorted(audited) for f in staged[sid]["fee_lines"]],
        tax_lines=[t for sid in sorted(audited) for t in staged[sid]["tax_lines"]],
        batches=[b for b in batches if b.id in staged],
        bank_credits=[c for c in credits if c.id.removeprefix("BC-") in staged],
        quarantined=quarantined,
        fee_convention=convention,
        unaudited_paise=unaudited_paise,
    )


def _map_payment(
    row: dict[str, Any],
    detail: dict[str, Any] | None,
    convention: FeeConvention,
    merchant_id: str,
    mcc: str,
    drop,
) -> tuple[Payment, FeeLine | None, TaxLine | None] | None:
    entity_id = str(row.get("entity_id", ""))

    if detail is None:
        drop(
            row,
            QuarantineReason.MISSING_PAYMENT_DETAIL,
            "no /v1/payments entity, so `international` is unknown and the fee cannot be scoped",
        )
        return None

    amount = _int(row.get("amount"))
    fee, tax = _fee_and_tax(row, convention)
    if amount - fee - tax != _int(row.get("credit")):
        drop(
            row,
            QuarantineReason.CREDIT_DOES_NOT_RECONCILE,
            f"amount {amount} - fee {fee} - tax {tax} != credit {_int(row.get('credit'))}",
        )
        return None

    method = _METHODS.get(str(row.get("method") or "").lower())
    if method is None:
        drop(row, QuarantineReason.UNSUPPORTED_METHOD, f"method {row.get('method')!r} is not an Assay PaymentMethod")
        return None

    is_international = bool(detail.get("international"))
    if is_international or method in _MULTI_COMPONENT_METHODS:
        drop(
            row,
            QuarantineReason.MULTI_COMPONENT_FEE,
            "contract price is more than one component (international surcharge or EMI subvention), "
            "and Razorpay reports a single blended fee that cannot be diffed per fee type",
        )
        return None

    network = card_type = None
    if method is PaymentMethod.CARD:
        card = detail.get("card") or {}
        raw_network = str(card.get("network") or row.get("card_network") or "").lower()
        network = _NETWORKS.get(raw_network)
        if network is None:
            drop(
                row,
                QuarantineReason.UNREPRESENTABLE_CARD_NETWORK,
                f"card network {raw_network or 'missing'!r} has no Assay Network member",
            )
            return None
        raw_card_type = str(card.get("type") or row.get("card_type") or "").lower()
        card_type = _CARD_TYPES.get(raw_card_type)
        if card_type is None:
            drop(
                row,
                QuarantineReason.UNREPRESENTABLE_CARD_TYPE,
                f"card type {raw_card_type or 'missing'!r} has no Assay CardType member",
            )
            return None

    settlement_id = str(row.get("settlement_id"))
    payment = Payment(
        id=entity_id,
        merchant_id=merchant_id,
        amount=Money(amount),
        method=method,
        network=network,
        card_type=card_type,
        is_international=is_international,
        mcc=mcc,
        captured_at=_ist(row.get("created_at")),
        settlement_id=settlement_id,
    )

    fee_line = tax_line = None
    if fee > 0:
        fee_line = FeeLine(
            id=f"FEE-{entity_id}",
            applies_to_id=entity_id,
            applies_to_type=EntityType.PAYMENT,
            # Razorpay does not break its fee down, and every transaction
            # that reaches here has been scoped to a single-component
            # price, so MDR is the whole of it -- not an assumption, a
            # consequence of the quarantine above.
            fee_type=FeeType.MDR,
            computed_amount=Money(fee),
            # Razorpay never says which contractual clause it applied.
            # A fixed, honest placeholder rather than a guessed rule id:
            # core/exceptions.py clusters findings by rule_id, so every
            # Razorpay-sourced finding clusters together, which is a
            # degradation but a truthful one.
            rule_id="razorpay-reported",
        )
    if tax > 0:
        tax_line = TaxLine(
            id=f"TAX-{entity_id}",
            applies_to_fee_id=f"FEE-{entity_id}",
            tax_type="GST",
            # Derived from two exact amounts, never an input to any
            # comparison -- core/verify.py diffs tax AMOUNTS. Present
            # because TaxLine requires it, and it must describe the
            # amounts faithfully rather than assert a rate they don't show.
            rate_bps=(tax * 10_000 // fee) if fee > 0 else _GST_BPS,
            base_amount=Money(fee),
            amount=Money(tax),
        )
    return payment, fee_line, tax_line


def _map_settlements(
    settlements: list[dict[str, Any]],
    recon_rows: list[dict[str, Any]],
    merchant_id: str,
    quarantined: list[QuarantinedRow],
) -> tuple[list[SettlementBatch], list[BankCredit]]:
    """A processed settlement becomes both the batch it settles and the
    stand-in bank credit for it.

    Emitting the same payout as both sides is exactly the limitation the
    package docstring names: the "bank statement" here is the gateway's own
    claim. `ingest/cli.py` stamps `bank_statement_source` into the run
    manifest so the audit can never quietly present this as independent
    corroboration.
    """
    spans: dict[str, list[int]] = {}
    for row in recon_rows:
        settlement_id = row.get("settlement_id")
        if not settlement_id:
            continue
        created = _int(row.get("created_at"))
        spans.setdefault(str(settlement_id), []).append(created)

    batches: list[SettlementBatch] = []
    credits: list[BankCredit] = []

    for settlement in settlements:
        settlement_id = str(settlement.get("id"))
        if settlement_id not in spans:
            # Settled entirely outside the fetched recon window -- most
            # often a payout at a month boundary covering the previous
            # month's transactions. Emitting a credit with no records to
            # decompose it into would report its whole amount as
            # unexplained, which is a fetch-window artefact, not a finding.
            continue
        status = str(settlement.get("status") or "")
        if status != "processed":
            quarantined.append(
                QuarantinedRow(
                    entity_id=settlement_id,
                    row_type="settlement",
                    reason=QuarantineReason.SETTLEMENT_NOT_PROCESSED,
                    detail=f"settlement status {status!r}: no money moved, so there is no credit to decompose",
                )
            )
            continue

        created_at = _int(settlement.get("created_at"))
        # The real cycle this batch covers, taken from the transactions in
        # it rather than collapsed to the payout instant.
        row_times = spans.get(settlement_id) or [created_at]
        amount = _int(settlement.get("amount"))
        utr = str(settlement.get("utr") or "")

        batches.append(
            SettlementBatch(
                id=settlement_id,
                merchant_id=merchant_id,
                cycle_start=_ist(min(row_times)),
                cycle_end=_ist(max(row_times)),
                expected_credit=Money(amount),
                utr=utr,
                status=BatchStatus.SETTLED,
            )
        )
        credits.append(
            BankCredit(
                id=f"BC-{settlement_id}",
                utr=utr,
                amount=Money(amount),
                value_date=_ist(created_at).date(),
                # decompose.py's tier-3 fallback tokenises the narration,
                # so the UTR and settlement id are both worth carrying.
                narration=f"Razorpay settlement {settlement_id} UTR {utr}",
            )
        )

    return batches, credits


def unclaimed_reference(entity_id: str) -> RecordRef:
    """Convenience for callers reporting a quarantined payment by ref."""
    return RecordRef(type=EntityType.PAYMENT, id=entity_id)
