"""Tests for core/exceptions.py: clustering, pricing, dispute packets.

Money-critical: this is the module that turns N raw Findings into "37 rows,
one cause, Rs 12,400" -- a wrong clustering key either splits one real issue
into many exception-report rows (defeating the point) or silently merges two
genuinely different bugs into one (hiding one of them). Written test-first,
per the working agreement.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from core.contract import AppliesWhen, CompiledContract, FeeRule, RateCardParse, TaxBase, TaxTreatment
from core.exceptions import (
    Cluster,
    DiscrepancyClass,
    RecomputedArithmetic,
    build_dispute_packet,
    cluster_findings,
)
from core.ledger import Ledger
from core.models import (
    CardType,
    Chargeback,
    ChargebackStage,
    EntityType,
    FeeLine,
    FeeType,
    Finding,
    Lane,
    Payment,
    PaymentMethod,
    RecordRef,
    Severity,
    TaxLine,
)
from core.money import Money

MERCHANT = "MERCH-0001"
CAPTURED_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
AUDIT_RUN_ID = "RUN-1"


# ---------------------------------------------------------------------------
# Local builders. Each test file in this repo builds its own.
# ---------------------------------------------------------------------------


def _payment(id_: str, paise: int) -> Payment:
    return Payment(
        id=id_,
        merchant_id=MERCHANT,
        amount=Money(paise),
        method=PaymentMethod.CARD,
        network=None,
        card_type=CardType.CREDIT,
        is_international=False,
        mcc="5411",
        captured_at=CAPTURED_AT,
        settlement_id=f"STL-{id_}",
    )


def _fee_line(id_: str, applies_to_id: str, paise: int, rule_id: str) -> FeeLine:
    return FeeLine(
        id=id_,
        applies_to_id=applies_to_id,
        applies_to_type=EntityType.PAYMENT,
        fee_type=FeeType.MDR,
        computed_amount=Money(paise),
        rule_id=rule_id,
    )


def _tax_line(id_: str, applies_to_fee_id: str, base_paise: int, paise: int) -> TaxLine:
    return TaxLine(
        id=id_,
        applies_to_fee_id=applies_to_fee_id,
        tax_type="GST",
        rate_bps=1800,
        base_amount=Money(base_paise),
        amount=Money(paise),
    )


def _chargeback(id_: str, paise: int) -> Chargeback:
    return Chargeback(
        id=id_,
        payment_id="PAY-X",
        amount=Money(paise),
        reason_code="4855",
        stage=ChargebackStage.WON,
        raised_at=CAPTURED_AT,
        resolved_at=CAPTURED_AT,
        settlement_id=None,
    )


def _finding(
    id_: str,
    discrepancy_class: DiscrepancyClass,
    amount_paise: int,
    evidence_ids: list[RecordRef],
) -> Finding:
    return Finding(
        id=id_,
        audit_run_id=AUDIT_RUN_ID,
        discrepancy_class=discrepancy_class,
        severity=Severity.MAJOR,
        amount_impact=Money(amount_paise),
        evidence_ids=evidence_ids,
        confidence=9_981,
        lane=Lane.PROPOSE,
        explanation="test finding",
    )


def _overcharge_with_evidence(
    idx: int, gross_paise: int, delta_paise: int, rule_id: str = "card.credit.tier2"
) -> tuple[Finding, list]:
    """One FEE_OVERCHARGE finding plus the Payment/FeeLine records its
    evidence_ids cite, at a fixed bps-of-gross ratio (delta_paise / gross_paise)."""
    payment = _payment(f"PAY-{idx}", gross_paise)
    fee = _fee_line(f"FEE-{idx}", f"PAY-{idx}", 1, rule_id)
    evidence = [
        RecordRef(type=EntityType.PAYMENT, id=f"PAY-{idx}"),
        RecordRef(type=EntityType.FEE_LINE, id=f"FEE-{idx}"),
    ]
    finding = _finding(f"FND-{idx}", DiscrepancyClass.FEE_OVERCHARGE, delta_paise, evidence)
    return finding, [payment, fee]


def _two_tier_contract() -> CompiledContract:
    tier1 = FeeRule(
        rule_id="card.credit.tier1",
        fee_type=FeeType.MDR,
        effective_from=date(2026, 7, 1),
        applies_when=AppliesWhen(
            method=PaymentMethod.CARD, card_type=CardType.CREDIT, amount_min_paise=0, amount_max_paise=200_000
        ),
        rate_bps=180,
        taxes=[TaxTreatment(tax_type="GST", rate_bps=1800, base=TaxBase.FEE_AMOUNT)],
        source_quote="Credit card up to Rs.2,000: 1.80%",
    )
    tier2 = FeeRule(
        rule_id="card.credit.tier2",
        fee_type=FeeType.MDR,
        effective_from=date(2026, 7, 1),
        applies_when=AppliesWhen(method=PaymentMethod.CARD, card_type=CardType.CREDIT, amount_min_paise=200_000),
        rate_bps=160,
        taxes=[TaxTreatment(tax_type="GST", rate_bps=1800, base=TaxBase.FEE_AMOUNT)],
        source_quote="Credit card above Rs.2,000: 1.60%",
    )
    return CompiledContract.from_parse(RateCardParse(merchant_id=MERCHANT, rules=[tier1, tier2]))


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def test_findings_sharing_class_rule_id_and_delta_bucket_cluster_together():
    finding1, records1 = _overcharge_with_evidence(1, gross_paise=100_000, delta_paise=1_000)  # 100 bps
    finding2, records2 = _overcharge_with_evidence(2, gross_paise=200_000, delta_paise=2_000)  # 100 bps
    ledger = Ledger([*records1, *records2])

    clusters = cluster_findings([finding1, finding2], ledger, audit_run_id=AUDIT_RUN_ID)

    assert len(clusters) == 1
    assert isinstance(clusters[0], Cluster)
    assert clusters[0].count == 2
    assert set(clusters[0].finding_ids) == {"FND-1", "FND-2"}
    assert clusters[0].total_impact == Money(3_000)


def test_a_different_delta_bucket_on_the_same_rule_id_does_not_merge():
    finding1, records1 = _overcharge_with_evidence(1, gross_paise=100_000, delta_paise=1_000)  # 100 bps
    finding2, records2 = _overcharge_with_evidence(2, gross_paise=100_000, delta_paise=5_000)  # 500 bps
    ledger = Ledger([*records1, *records2])

    clusters = cluster_findings([finding1, finding2], ledger, audit_run_id=AUDIT_RUN_ID)

    assert len(clusters) == 2, "same rule_id and class but a genuinely different bug must not merge"


def test_clusters_are_sorted_strictly_by_money_descending():
    small, small_records = _overcharge_with_evidence(1, gross_paise=100_000, delta_paise=500, rule_id="rule.a")
    big, big_records = _overcharge_with_evidence(2, gross_paise=100_000, delta_paise=50_000, rule_id="rule.b")
    medium, medium_records = _overcharge_with_evidence(
        3, gross_paise=100_000, delta_paise=5_000, rule_id="rule.c"
    )
    ledger = Ledger([*small_records, *big_records, *medium_records])

    clusters = cluster_findings([small, big, medium], ledger, audit_run_id=AUDIT_RUN_ID)

    assert [c.total_impact.paise for c in clusters] == [50_000, 5_000, 500]


def test_a_finding_with_no_resolvable_rule_id_still_clusters_by_class_alone():
    chargeback = _chargeback("CB-1", 20_000)
    finding = _finding(
        "FND-1",
        DiscrepancyClass.CHARGEBACK_AMOUNT_MISMATCH,
        20_000,
        [RecordRef(type=EntityType.CHARGEBACK, id="CB-1")],
    )
    ledger = Ledger([chargeback])

    clusters = cluster_findings([finding], ledger, audit_run_id=AUDIT_RUN_ID)

    assert len(clusters) == 1
    assert clusters[0].rule_id is None
    assert clusters[0].discrepancy_class is DiscrepancyClass.CHARGEBACK_AMOUNT_MISMATCH
    assert clusters[0].total_impact == Money(20_000)


def test_rule_id_resolves_via_a_tax_line_evidence_even_without_a_direct_fee_line_ref():
    payment = _payment("PAY-1", 100_000)
    fee = _fee_line("FEE-1", "PAY-1", 18_000, "card.credit.tier1")
    tax = _tax_line("TAX-1", "FEE-1", 18_000, 3_240)
    finding = _finding(
        "FND-1",
        DiscrepancyClass.TAX_MISCALCULATION,
        500,
        [RecordRef(type=EntityType.PAYMENT, id="PAY-1"), RecordRef(type=EntityType.TAX_LINE, id="TAX-1")],
    )
    ledger = Ledger([payment, fee, tax])

    clusters = cluster_findings([finding], ledger, audit_run_id=AUDIT_RUN_ID)

    assert clusters[0].rule_id == "card.credit.tier1"


def test_37_rows_one_cause_collapses_to_a_single_priced_cluster():
    findings = []
    records = []
    for i in range(37):
        finding, recs = _overcharge_with_evidence(i, gross_paise=500_000, delta_paise=1_200)  # 24 bps every time
        findings.append(finding)
        records.extend(recs)
    ledger = Ledger(records)

    clusters = cluster_findings(findings, ledger, audit_run_id=AUDIT_RUN_ID)

    assert len(clusters) == 1
    assert clusters[0].count == 37
    assert clusters[0].total_impact == Money(37 * 1_200)


def test_cluster_id_is_deterministic_and_namespaced_to_the_audit_run():
    finding, records = _overcharge_with_evidence(1, gross_paise=100_000, delta_paise=1_000)
    ledger = Ledger(records)

    clusters_a = cluster_findings([finding], ledger, audit_run_id="RUN-A")
    clusters_b = cluster_findings([finding], ledger, audit_run_id="RUN-B")

    assert clusters_a[0].cluster_id != clusters_b[0].cluster_id
    assert "RUN-A" in clusters_a[0].cluster_id


# ---------------------------------------------------------------------------
# Dispute packets
# ---------------------------------------------------------------------------


def test_build_dispute_packet_resolves_the_contract_clause_text():
    contract = _two_tier_contract()
    finding, records = _overcharge_with_evidence(
        1, gross_paise=100_000, delta_paise=1_000, rule_id="card.credit.tier1"
    )
    ledger = Ledger(records)
    cluster = cluster_findings([finding], ledger, audit_run_id=AUDIT_RUN_ID)[0]

    packet = build_dispute_packet(cluster, [finding], contract)

    assert packet.contract_clause == "Credit card up to Rs.2,000: 1.80%"
    assert packet.total_impact == Money(1_000)
    assert packet.finding_ids == ["FND-1"]
    assert set(packet.evidence_ids) == set(finding.evidence_ids)


def test_dispute_packet_narration_status_defaults_to_unavailable():
    contract = _two_tier_contract()
    finding, records = _overcharge_with_evidence(
        1, gross_paise=100_000, delta_paise=1_000, rule_id="card.credit.tier1"
    )
    ledger = Ledger(records)
    cluster = cluster_findings([finding], ledger, audit_run_id=AUDIT_RUN_ID)[0]

    packet = build_dispute_packet(cluster, [finding], contract)

    assert packet.narration is None
    assert packet.narration_status == "unavailable"


def test_dispute_packet_claim_names_the_class_count_and_money():
    contract = _two_tier_contract()
    findings = []
    records = []
    for i in range(3):
        finding, recs = _overcharge_with_evidence(i, gross_paise=100_000, delta_paise=1_000, rule_id="card.credit.tier1")
        findings.append(finding)
        records.extend(recs)
    ledger = Ledger(records)
    cluster = cluster_findings(findings, ledger, audit_run_id=AUDIT_RUN_ID)[0]

    packet = build_dispute_packet(cluster, findings, contract)

    assert "3" in packet.claim
    assert "fee_overcharge" in packet.claim
    assert "30.00" in packet.claim or "30" in packet.claim


def test_dispute_packet_recomputed_arithmetic_carries_a_representative_example():
    contract = _two_tier_contract()
    finding, records = _overcharge_with_evidence(
        1, gross_paise=100_000, delta_paise=1_000, rule_id="card.credit.tier1"
    )
    ledger = Ledger(records)
    cluster = cluster_findings([finding], ledger, audit_run_id=AUDIT_RUN_ID)[0]

    packet = build_dispute_packet(cluster, [finding], contract)

    assert isinstance(packet.recomputed_arithmetic, RecomputedArithmetic)
    assert packet.recomputed_arithmetic.rule_id == "card.credit.tier1"
    assert packet.recomputed_arithmetic.rate_bps == 180
    assert packet.recomputed_arithmetic.representative_finding_id == "FND-1"


def test_dispute_packet_has_no_clause_when_rule_id_is_unresolvable():
    contract = _two_tier_contract()
    chargeback = _chargeback("CB-1", 20_000)
    finding = _finding(
        "FND-1",
        DiscrepancyClass.CHARGEBACK_AMOUNT_MISMATCH,
        20_000,
        [RecordRef(type=EntityType.CHARGEBACK, id="CB-1")],
    )
    ledger = Ledger([chargeback])
    cluster = cluster_findings([finding], ledger, audit_run_id=AUDIT_RUN_ID)[0]

    packet = build_dispute_packet(cluster, [finding], contract)

    assert packet.contract_clause is None
    assert packet.recomputed_arithmetic is None
