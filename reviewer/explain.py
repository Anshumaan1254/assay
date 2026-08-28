"""The single-transaction drill-down, as structured data.

`cli/explain.py` renders the same causal chain as text for the terminal.
This module renders it as objects for the browser, and the two are kept
honest by calling the *same* underlying engine functions --
`decompose_all`, `contract.fee_for`, `fee_tax_cells`, `verify` -- rather
than one re-deriving what the other computed. Nothing here decides an
amount; it labels and formats what those functions return.

The earlier version of this endpoint returned `explain_record`'s
preformatted text and the page showed it in a <pre>. That was faithful but
unreadable: a wall of JSON and terminal columns on a screen a reviewer is
supposed to make a decision from. `tests/test_reviewer_explain.py` pins
that the structured view and the text view agree on the numbers, which is
the risk a second rendering actually carries.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from core.contract import CompiledContract
from core.decompose import DecompositionOutcome, DecompositionProof, decompose_all
from core.lanes import CalibrationArtifact
from core.ledger import Ledger
from core.models import BankCredit, EntityType, Payment, RecordRef
from core.verify import fee_tax_cells, verify
from reviewer.derive import EvidenceRef, MoneyView, rupees_display

AUDIT_RUN_ID = "EXPLAIN"


class Fact(BaseModel):
    """One labelled field of the raw record, formatted for reading."""

    label: str
    value: str
    mono: bool = False


class Clause(BaseModel):
    rule_id: str
    quote: str


class RecomputeRow(BaseModel):
    """What the gateway charged against what the contract says it should
    have. `delta` is reported - recomputed, so positive means overcharged."""

    label: str
    reported: MoneyView
    recomputed: MoneyView
    delta: MoneyView
    agrees: bool


class Settlement(BaseModel):
    credit_id: str
    tier: str
    outcome: str
    reason: str | None
    lane: str | None
    confidence_bps: int
    calibrated: bool
    proof_hash: str


class ExplainFinding(BaseModel):
    discrepancy_class: str
    lane: str
    confidence_bps: int
    impact: MoneyView
    explanation: str


class ExplainView(BaseModel):
    record_id: str
    found: bool
    record_type: str | None = None
    headline: str | None = None
    note: str | None = None
    facts: list[Fact] = []
    contract_fee: MoneyView | None = None
    contract_tax: MoneyView | None = None
    reported_fee: MoneyView | None = None
    reported_tax: MoneyView | None = None
    clauses: list[Clause] = []
    recompute: list[RecomputeRow] = []
    contract_gap: str | None = None
    settlement: Settlement | None = None
    findings: list[ExplainFinding] = []
    evidence: list[EvidenceRef] = []


_LABELS = {
    "id": "Reference",
    "merchant_id": "Merchant",
    "amount": "Amount",
    "method": "Method",
    "network": "Network",
    "card_type": "Card type",
    "is_international": "International",
    "mcc": "Merchant category",
    "captured_at": "Captured",
    "settlement_id": "Settlement",
    "value_date": "Value date",
    "utr": "UTR",
    "narration": "Narration",
    "computed_amount": "Amount",
    "applies_to_id": "Applies to",
    "applies_to_fee_id": "Applies to fee",
    "fee_type": "Fee type",
    "tax_type": "Tax type",
    "rate_bps": "Rate",
    "stage": "Stage",
    "reason": "Reason",
    "kind": "Kind",
}

_MONO_FIELDS = {"id", "merchant_id", "settlement_id", "utr", "applies_to_id", "applies_to_fee_id", "mcc"}


def _humanize(key: str, value: object) -> str:
    """A single dumped field, as something a person reads.

    Money arrives as {"paise": int, "currency": str} and is shown in
    rupees; nothing is divided here, `rupees_display` formats the integer.
    """
    if value is None:
        return "—"
    if isinstance(value, dict) and "paise" in value:
        return rupees_display(int(value["paise"]))
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if key.endswith("_bps") and isinstance(value, int):
        whole, frac = divmod(value, 100)
        return f"{whole}.{frac:02d}%"
    if isinstance(value, str) and key.endswith(("_at", "_date")):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return parsed.strftime("%d %b %Y, %H:%M") if "T" in value else parsed.strftime("%d %b %Y")
    if isinstance(value, str):
        return value.replace("_", " ") if value.islower() and "_" in value else value
    return str(value)


def _facts(record) -> list[Fact]:
    dumped = record.model_dump(mode="json")
    facts = []
    for key, value in dumped.items():
        if isinstance(value, (list, dict)) and not (isinstance(value, dict) and "paise" in value):
            continue
        facts.append(
            Fact(
                label=_LABELS.get(key, key.replace("_", " ").capitalize()),
                value=_humanize(key, value),
                mono=key in _MONO_FIELDS,
            )
        )
    return facts


def _find_ref(ledger: Ledger, record_id: str) -> RecordRef | None:
    for ref in ledger:
        if ref.id == record_id:
            return ref
    return None


def _proof_claiming(ref: RecordRef, proofs) -> DecompositionProof | None:
    for proof in proofs:
        if any(term.ref == ref for term in proof.terms):
            return proof
    return None


def explain_structured(
    record_id: str,
    ledger: Ledger,
    credits: list[BankCredit],
    contract: CompiledContract,
    merchant_id: str,
    calibration: CalibrationArtifact | None = None,
) -> ExplainView:
    ref = _find_ref(ledger, record_id)
    if ref is None:
        return ExplainView(
            record_id=record_id,
            found=False,
            note=f"No record {record_id} in this run's ledger.",
        )

    record = ledger.get(ref)
    view = ExplainView(
        record_id=record_id,
        found=True,
        record_type=ref.type.value.replace("_", " "),
        headline=f"{ref.type.value.replace('_', ' ')} {ref.id}",
        facts=_facts(record),
    )

    if ref.type is EntityType.PAYMENT:
        payment: Payment = record
        breakdown = contract.fee_for(payment, at=payment.captured_at)
        view.contract_fee = MoneyView.of(breakdown.total_fee.paise)
        view.contract_tax = MoneyView.of(breakdown.total_tax.paise)
        view.clauses = [
            Clause(rule_id=rule.rule_id, quote=rule.source_quote)
            for rule_id in breakdown.matched_rule_ids
            for rule in contract.rules
            if rule.rule_id == rule_id
        ]
        reported_fee = sum(
            ledger.get(fee_ref).computed_amount.paise for fee_ref in ledger.fee_lines_for(ref)
        )
        reported_tax = sum(
            ledger.get(tax_ref).amount.paise
            for fee_ref in ledger.fee_lines_for(ref)
            for tax_ref in ledger.tax_lines_for(fee_ref)
        )
        view.reported_fee = MoneyView.of(reported_fee)
        view.reported_tax = MoneyView.of(reported_tax)

    proofs = decompose_all(credits, ledger, merchant_id=merchant_id, calibration=calibration)
    proof = _proof_claiming(ref, proofs)

    if proof is None:
        from core.conserve import unclaimed_records

        if ref in unclaimed_records(ledger, proofs):
            view.note = "No settlement credit claimed this record in this run."
        else:
            view.note = "This record type does not take part in settlement decomposition."
        return view

    view.settlement = Settlement(
        credit_id=proof.credit_ref.id,
        tier=proof.tier.value.replace("_", " "),
        outcome=proof.outcome.value,
        reason=proof.reason.value.replace("_", " ") if proof.reason is not None else None,
        lane=proof.lane_assignment.lane.value if proof.lane_assignment is not None else None,
        confidence_bps=(
            proof.lane_assignment.calibrated_confidence_bps
            if proof.lane_assignment is not None
            else proof.confidence
        ),
        calibrated=proof.lane_assignment is not None,
        proof_hash=proof.proof_hash,
    )

    if proof.outcome is not DecompositionOutcome.RESOLVED:
        return view

    if ref.type is EntityType.PAYMENT:
        all_cells, gaps = fee_tax_cells(proof, ledger, contract)
        for cell in (c for c in all_cells if c.payment_ref == ref):
            delta = cell.reported_paise - cell.recomputed_paise
            view.recompute.append(
                RecomputeRow(
                    label=f"{cell.kind.capitalize()} · {cell.fee_type.value.replace('_', ' ')}",
                    reported=MoneyView.of(cell.reported_paise),
                    recomputed=MoneyView.of(cell.recomputed_paise),
                    delta=MoneyView.of(delta),
                    agrees=delta == 0,
                )
            )
        gap = next((g for g in gaps if g.payment_ref == ref), None)
        if gap is not None:
            view.contract_gap = gap.reason

    findings, _gaps = verify(proof, ledger, contract, audit_run_id=AUDIT_RUN_ID, calibration=calibration)
    for finding in (f for f in findings if ref in f.evidence_ids):
        view.findings.append(
            ExplainFinding(
                discrepancy_class=finding.discrepancy_class.value.replace("_", " "),
                lane=finding.lane.value,
                confidence_bps=finding.confidence,
                impact=MoneyView.of(finding.amount_impact.paise),
                explanation=finding.explanation,
            )
        )
        view.evidence.extend(
            EvidenceRef(type=e.type.value, id=e.id) for e in finding.evidence_ids if e != ref
        )

    # De-duplicate evidence refs while keeping first-seen order.
    seen: set[tuple[str, str]] = set()
    unique = []
    for e in view.evidence:
        if (e.type, e.id) not in seen:
            seen.add((e.type, e.id))
            unique.append(e)
    view.evidence = unique

    return view
