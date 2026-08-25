"""`assay explain <record_id>`: the complete causal chain for one
transaction -- what it was, which rules applied, what it should have netted,
what it did net, which credit it landed in, and the proof.

The honest exception vocabulary applies here too: an unresolved outcome
names its `DecompositionReason`, never a generic message.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from core.conserve import unclaimed_records
from core.contract import CompiledContract
from core.decompose import DecompositionOutcome, DecompositionProof, decompose_all
from core.lanes import CalibrationArtifact
from core.ledger import Ledger
from core.models import BankCredit, EntityType, Payment, RecordRef
from core.money import Money
from core.verify import fee_tax_cells, verify

AUDIT_RUN_ID = "EXPLAIN"


def _find_ref(ledger: Ledger, record_id: str) -> RecordRef | None:
    for ref in ledger:
        if ref.id == record_id:
            return ref
    return None


def _payment_fee_tax_lines(payment_ref: RecordRef, ledger: Ledger, contract: CompiledContract) -> list[str]:
    payment: Payment = ledger.get(payment_ref)
    breakdown = contract.fee_for(payment, at=payment.captured_at)
    lines = [
        (
            f"Contract recomputes: fee {breakdown.total_fee.to_rupees_str()}, "
            f"tax {breakdown.total_tax.to_rupees_str()} (rules: {', '.join(breakdown.matched_rule_ids)})"
        )
    ]
    for rule_id in breakdown.matched_rule_ids:
        for rule in contract.rules:
            if rule.rule_id == rule_id:
                lines.append(f"  clause [{rule_id}]: {rule.source_quote}")

    reported_fee = sum(ledger.get(ref).computed_amount.paise for ref in ledger.fee_lines_for(payment_ref))
    reported_tax = sum(
        ledger.get(tax_ref).amount.paise
        for fee_ref in ledger.fee_lines_for(payment_ref)
        for tax_ref in ledger.tax_lines_for(fee_ref)
    )
    lines.append(
        f"Settlement reports: fee {Money(reported_fee).to_rupees_str()}, "
        f"tax {Money(reported_tax).to_rupees_str()}"
    )
    return lines


def _proof_claiming(ref: RecordRef, proofs: Sequence[DecompositionProof]) -> DecompositionProof | None:
    for proof in proofs:
        if any(term.ref == ref for term in proof.terms):
            return proof
    return None


def explain_record(
    record_id: str,
    ledger: Ledger,
    credits: list[BankCredit],
    contract: CompiledContract,
    merchant_id: str,
    calibration: CalibrationArtifact | None = None,
) -> str:
    ref = _find_ref(ledger, record_id)
    if ref is None:
        return f"record {record_id!r} was not found in this run's ledger."

    record = ledger.get(ref)
    lines = [f"Record: {ref.type.value} {ref.id}", json.dumps(record.model_dump(mode="json"), indent=2)]

    if ref.type is EntityType.PAYMENT:
        lines.extend(_payment_fee_tax_lines(ref, ledger, contract))

    proofs = decompose_all(credits, ledger, merchant_id=merchant_id, calibration=calibration)
    proof = _proof_claiming(ref, proofs)

    if proof is None:
        if ref in unclaimed_records(ledger, proofs):
            lines.append("Not claimed by any settlement credit's proof in this run.")
        else:
            lines.append("This record type does not participate in settlement decomposition.")
        return "\n".join(lines)

    lines.append(f"Settled in credit: {proof.credit_ref.id}")
    lines.append(f"Decomposition tier: {proof.tier.value}, outcome: {proof.outcome.value}")
    if proof.reason is not None:
        lines.append(f"Reason: {proof.reason.value}")
    if proof.lane_assignment is not None:
        lines.append(
            f"Lane: {proof.lane_assignment.lane.value} "
            f"(calibrated confidence {proof.lane_assignment.calibrated_confidence_bps} bps)"
        )
    else:
        lines.append(f"Raw confidence: {proof.confidence} bps (no calibration artifact loaded)")
    lines.append(f"Proof hash: {proof.proof_hash}")

    if proof.outcome is not DecompositionOutcome.RESOLVED:
        return "\n".join(lines)

    if ref.type is EntityType.PAYMENT:
        cells = [cell for cell in fee_tax_cells(proof, ledger, contract) if cell.payment_ref == ref]
        for cell in cells:
            lines.append(
                f"  {cell.kind} {cell.fee_type.value}: reported {Money(cell.reported_paise).to_rupees_str()}, "
                f"recomputed {Money(cell.recomputed_paise).to_rupees_str()}"
            )

    findings = verify(proof, ledger, contract, audit_run_id=AUDIT_RUN_ID, calibration=calibration)
    relevant = [f for f in findings if ref in f.evidence_ids]
    if relevant:
        lines.append("Findings citing this record:")
        for finding in relevant:
            lines.append(
                f"  - {finding.discrepancy_class.value}: {finding.amount_impact.to_rupees_str()} "
                f"[{finding.lane.value}, {finding.confidence} bps] {finding.explanation}"
            )
    else:
        lines.append("No discrepancy found for this record.")

    return "\n".join(lines)
