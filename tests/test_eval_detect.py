"""Tests for eval/detect.py -- the matcher every number in EVIDENCE.md
§2-§5 is derived from.

Nothing here computes money the engine will act on, so these are not
money-path tests in the working agreement's sense. They are worse in one
specific way: a bug here does not break an audit, it publishes a false
claim about how well the audit works, in the document whose whole purpose
is not over-claiming. So each of the three outcomes a finding can have --
true positive, misclassified, false positive -- is pinned separately, and
the distinction between the last two is pinned hardest.
"""

from __future__ import annotations

from datetime import datetime

from core.exceptions import DiscrepancyClass
from core.models import IST, EntityType, Finding, Lane, RecordRef, Severity
from core.money import Money
from datagen.ground_truth import DataQualityFlag, DiscrepancyEntry, GroundTruth
from eval.detect import DATA_QUALITY_FLAG, NO_DISCREPANCY, NOT_DETECTED, evaluate, merge

PAY_1 = RecordRef(type=EntityType.PAYMENT, id="PAY-1")
PAY_2 = RecordRef(type=EntityType.PAYMENT, id="PAY-2")
FEE_1 = RecordRef(type=EntityType.FEE_LINE, id="FEE-1")
TAX_1 = RecordRef(type=EntityType.TAX_LINE, id="TAX-1")
CREDIT_1 = RecordRef(type=EntityType.BANK_CREDIT, id="BC-1")


def _finding(
    id_: str,
    discrepancy_class: DiscrepancyClass,
    evidence: list[RecordRef],
    paise: int = 1_000,
) -> Finding:
    return Finding(
        id=id_,
        audit_run_id="AUD-TEST",
        discrepancy_class=discrepancy_class,
        severity=Severity.MAJOR,
        amount_impact=Money(paise),
        evidence_ids=evidence,
        confidence=10_000,
        lane=Lane.PROPOSE,
        explanation="test",
    )


def _entry(code: str, discrepancy_class: DiscrepancyClass, records: list[RecordRef], paise: int) -> DiscrepancyEntry:
    return DiscrepancyEntry(
        code=code, discrepancy_class=discrepancy_class, records=records, amount_impact_paise=paise, detail={}
    )


def _truth(entries=(), flags=()) -> GroundTruth:
    return GroundTruth(
        run_id="test",
        seed=1,
        profile="test",
        generated_at=datetime.now(IST),
        discrepancies=list(entries),
        data_quality_flags=list(flags),
    )


# ---------------------------------------------------------------------------
# The three outcomes
# ---------------------------------------------------------------------------


def test_a_finding_that_touches_a_plant_and_names_its_class_is_a_true_positive():
    truth = _truth([_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1, PAY_1], 5_000)])
    findings = [_finding("F1", DiscrepancyClass.FEE_OVERCHARGE, [PAY_1, FEE_1], paise=5_000)]

    result = evaluate(findings, truth)

    assert result.true_positive_findings == 1
    assert result.false_positive_findings == 0
    assert result.misclassified_findings == 0
    assert result.detected_paise == 5_000
    assert result.value_recall == 1.0
    assert result.code_rows[0].code == "D01"
    assert result.code_rows[0].true_positives == 1


def test_a_finding_that_touches_nothing_planted_is_a_false_positive_and_costs_rupees():
    truth = _truth([_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1, PAY_1], 5_000)])
    findings = [_finding("F1", DiscrepancyClass.FEE_OVERCHARGE, [PAY_2], paise=777)]

    result = evaluate(findings, truth)

    assert result.false_positive_findings == 1
    assert result.false_positive_paise == 777
    assert result.confusion[NO_DISCREPANCY]["fee_overcharge"] == 1
    assert result.confusion["fee_overcharge"][NOT_DETECTED] == 1


def test_a_finding_on_a_real_plant_with_the_wrong_class_is_misclassified_not_falsely_claimed():
    """The D01 case: one planted wrong-MDR-tier fee changes the fee line AND
    the tax computed on it, so verify.py correctly emits a FEE_OVERCHARGE
    and a TAX_MISCALCULATION. Charging the second to §4's 'rupees falsely
    claimed' would bill the engine for noticing a real consequence of a
    real defect."""
    truth = _truth([_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1, PAY_1], 5_000)])
    findings = [
        _finding("F1", DiscrepancyClass.FEE_OVERCHARGE, [PAY_1, FEE_1], paise=4_000),
        _finding("F2", DiscrepancyClass.TAX_MISCALCULATION, [PAY_1, FEE_1, TAX_1], paise=1_000),
    ]

    result = evaluate(findings, truth)

    assert result.true_positive_findings == 1
    assert result.misclassified_findings == 1
    assert result.false_positive_findings == 0
    assert result.false_positive_paise == 0, "§4 must not absorb a misclassification"
    assert result.misclassified_paise == 1_000
    assert result.confusion["fee_overcharge"]["tax_miscalculation"] == 1


def test_a_plant_no_finding_touches_is_a_false_negative_in_both_tables():
    truth = _truth([_entry("D08", DiscrepancyClass.MISSING_TRANSACTION, [PAY_1], 9_000)])

    result = evaluate([], truth)

    assert result.code_rows[0].false_negatives == 1
    assert result.code_rows[0].true_positives == 0
    assert result.detected_paise == 0
    assert result.value_recall == 0.0
    assert result.confusion["missing_transaction"][NOT_DETECTED] == 1
    row = next(r for r in result.class_rows if r.discrepancy_class == "missing_transaction")
    assert row.false_negatives == 1
    assert row.recall == 0.0


# ---------------------------------------------------------------------------
# Value weighting -- §3's whole reason for existing
# ---------------------------------------------------------------------------


def test_value_recall_and_count_recall_can_disagree_sharply():
    """Missing one Rs.40,000 error is worse than missing forty Rs.100 ones,
    and the document has to make that visible rather than averaging it
    away."""
    entries = [_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1, PAY_1], 4_000_000)]
    entries += [
        _entry("D02", DiscrepancyClass.TAX_MISCALCULATION, [RecordRef(type=EntityType.TAX_LINE, id=f"TAX-{i}")], 10_000)
        for i in range(40)
    ]
    truth = _truth(entries)
    # Every small one detected; the single large one missed.
    findings = [
        _finding(f"F{i}", DiscrepancyClass.TAX_MISCALCULATION, [RecordRef(type=EntityType.TAX_LINE, id=f"TAX-{i}")])
        for i in range(40)
    ]

    result = evaluate(findings, truth)

    assert result.count_recall == 40 / 41
    assert result.value_recall == 400_000 / 4_400_000
    assert result.value_recall < result.count_recall / 2


# ---------------------------------------------------------------------------
# D09 -- a flag, not a discrepancy
# ---------------------------------------------------------------------------


def test_d09_is_reported_with_zero_recall_rather_than_quietly_excluded():
    truth = _truth(
        flags=[DataQualityFlag(code="D09", records=[CREDIT_1], amount_impact_paise=0, detail={})]
    )

    result = evaluate([], truth)

    row = next(r for r in result.code_rows if r.code == "D09")
    assert row.support == 1
    assert row.true_positives == 0
    assert row.planted_paise == 0
    assert result.confusion[DATA_QUALITY_FLAG][NOT_DETECTED] == 1


def test_d09_never_enters_the_macro_or_micro_averages():
    truth = _truth(
        [_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1, PAY_1], 5_000)],
        [DataQualityFlag(code="D09", records=[CREDIT_1], amount_impact_paise=0, detail={})],
    )
    findings = [_finding("F1", DiscrepancyClass.FEE_OVERCHARGE, [PAY_1, FEE_1], paise=5_000)]

    result = evaluate(findings, truth)

    assert {r.discrepancy_class for r in result.class_rows} == {m.value for m in DiscrepancyClass}
    assert DATA_QUALITY_FLAG not in {r.discrepancy_class for r in result.class_rows}
    assert result.micro.precision == 1.0
    assert result.macro.precision == 1.0


# ---------------------------------------------------------------------------
# Averages and pooling
# ---------------------------------------------------------------------------


def test_macro_average_skips_classes_that_were_never_planted_or_predicted():
    """Otherwise the average reports how many members the taxonomy happens
    to have, not how well the engine did."""
    truth = _truth([_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1, PAY_1], 5_000)])
    findings = [_finding("F1", DiscrepancyClass.FEE_OVERCHARGE, [PAY_1, FEE_1])]

    result = evaluate(findings, truth)

    assert result.macro.precision == 1.0
    assert result.macro.recall == 1.0
    assert result.macro.f1 == 1.0


def test_merge_pools_counts_rather_than_averaging_rates():
    """A run with three plants must not weigh as much as one with sixty."""
    sparse = evaluate(
        [_finding("F1", DiscrepancyClass.FEE_OVERCHARGE, [PAY_1])],
        _truth([_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [PAY_1], 1_000)]),
    )
    dense_entries = [
        _entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [RecordRef(type=EntityType.PAYMENT, id=f"P{i}")], 1_000)
        for i in range(10)
    ]
    dense = evaluate([], _truth(dense_entries))

    pooled = merge([sparse, dense])

    row = next(r for r in pooled.code_rows if r.code == "D01")
    assert row.support == 11
    assert row.true_positives == 1
    assert row.false_negatives == 10
    assert pooled.count_recall == 1 / 11


def test_merging_zero_results_refuses_rather_than_returning_an_empty_success():
    import pytest

    with pytest.raises(ValueError, match="cannot merge zero"):
        merge([])


# ---------------------------------------------------------------------------
# Matching details
# ---------------------------------------------------------------------------


def test_one_plant_is_detected_once_even_when_several_findings_touch_it():
    truth = _truth([_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1, PAY_1], 5_000)])
    findings = [
        _finding("F1", DiscrepancyClass.FEE_OVERCHARGE, [PAY_1]),
        _finding("F2", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1]),
    ]

    result = evaluate(findings, truth)

    assert result.code_rows[0].true_positives == 1, "support is 1, so detection cannot exceed 1"
    assert result.detected_paise == 5_000, "the plant's money is counted once, not twice"


def test_a_finding_prefers_the_plant_whose_class_it_actually_agrees_with():
    """A finding citing a payment touched by two different plants should be
    scored against the one it actually named, not whichever sorts first."""
    truth = _truth(
        [
            _entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [PAY_1], 1_000),
            _entry("D06", DiscrepancyClass.REFUND_AMOUNT_MISMATCH, [PAY_1], 2_000),
        ]
    )
    findings = [_finding("F1", DiscrepancyClass.REFUND_AMOUNT_MISMATCH, [PAY_1])]

    result = evaluate(findings, truth)

    assert result.true_positive_findings == 1
    assert result.misclassified_findings == 0
    detected = {row.code: row.true_positives for row in result.code_rows}
    assert detected == {"D01": 0, "D06": 1}


def test_the_result_is_independent_of_the_order_findings_arrive_in():
    truth = _truth(
        [
            _entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1, PAY_1], 5_000),
            _entry("D02", DiscrepancyClass.TAX_MISCALCULATION, [TAX_1], 500),
        ]
    )
    findings = [
        _finding("F1", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1]),
        _finding("F2", DiscrepancyClass.TAX_MISCALCULATION, [TAX_1]),
        _finding("F3", DiscrepancyClass.ROUNDING_DRIFT, [PAY_2]),
    ]

    forward = evaluate(findings, truth)
    backward = evaluate(list(reversed(findings)), truth)

    assert forward.model_dump() == backward.model_dump()


# ---------------------------------------------------------------------------
# Attributing a false positive to the boundary that produced it
# ---------------------------------------------------------------------------


def test_false_positives_are_attributed_to_the_boundary_that_produced_them():
    """"The system over-claims" and "one component over-claims" are very
    different findings, and §4 has to be able to tell them apart."""
    truth = _truth([_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1], 5_000)])
    findings = [
        _finding("FND-AUD-1-BC-1-001", DiscrepancyClass.FEE_OVERCHARGE, [PAY_2], paise=100),
        _finding("FND-ADJ-AUD-1-RES-BC-9", DiscrepancyClass.UNDOCUMENTED_ADJUSTMENT, [PAY_2], paise=900),
    ]

    result = evaluate(findings, truth)

    assert result.false_positive_findings == 2
    assert result.false_positive_paise == 1_000
    assert result.false_positive_from_adjudicator == 1
    assert result.false_positive_paise_from_adjudicator == 900
    assert result.false_positive_by_class == {"fee_overcharge": 1, "undocumented_adjustment": 1}
    assert result.false_positive_paise_by_class == {"fee_overcharge": 100, "undocumented_adjustment": 900}


def test_the_boundary_attribution_survives_merging():
    truth = _truth([_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1], 5_000)])
    one = evaluate(
        [_finding("FND-ADJ-AUD-1-RES-BC-1", DiscrepancyClass.UNDOCUMENTED_ADJUSTMENT, [PAY_2], paise=300)],
        truth,
    )
    two = evaluate(
        [_finding("FND-ADJ-AUD-2-RES-BC-2", DiscrepancyClass.UNDOCUMENTED_ADJUSTMENT, [PAY_2], paise=700)],
        truth,
    )

    pooled = merge([one, two])

    assert pooled.false_positive_from_adjudicator == 2
    assert pooled.false_positive_paise_from_adjudicator == 1_000
    assert pooled.false_positive_by_class == {"undocumented_adjustment": 2}


def test_a_misclassification_is_never_attributed_as_a_false_positive():
    truth = _truth([_entry("D01", DiscrepancyClass.FEE_OVERCHARGE, [FEE_1], 5_000)])
    findings = [_finding("FND-ADJ-AUD-1-RES-BC-1", DiscrepancyClass.TAX_MISCALCULATION, [FEE_1], paise=900)]

    result = evaluate(findings, truth)

    assert result.misclassified_findings == 1
    assert result.false_positive_from_adjudicator == 0
    assert result.false_positive_by_class == {}
