"""Did the audit find what was planted, and did it claim anything that
wasn't there.

This and eval/labels.py / eval/calibrate_lanes.py are the only modules
allowed to read datagen/ ground truth (invariant 5). Everything below is
comparison logic over RecordRefs and DiscrepancyClasses -- no generation,
no engine.

**The matching rule, stated once.**

A `Finding` and a planted `DiscrepancyEntry` *touch* when they share at
least one `RecordRef`. Touching is about records only; class agreement is a
separate question, and keeping the two separate is what makes every number
below well-defined:

  - an entry is **detected** when some finding touches it AND carries its
    discrepancy class. Class agreement is required, because finding the
    right rupees for the wrong reason is not a detection you could act on;
  - a finding is a **false positive** only when it touches NO planted entry
    at all -- it points at records where nothing was planted. §4's "rupees
    falsely claimed" is exactly this set's money;
  - a finding that touches a planted entry but names a different class is
    **misclassified**, not a false positive. It found real money for the
    wrong reason. Counting that as "falsely claimed" would overstate §4,
    and quietly counting it as a success would overstate §2. It gets its
    own cell in the confusion matrix and its own line in the report.

That distinction is not a technicality. One planted D01 (wrong MDR tier)
changes a fee line *and* the tax computed on it, so `core/verify.py`
correctly emits two findings: a FEE_OVERCHARGE and a TAX_MISCALCULATION.
Only the first matches D01's own class. Calling the second a false positive
would charge the engine for correctly noticing a real consequence of a real
defect.

**Why two tables.**

`DiscrepancyEntry.code` (D01..D12) is finer than `DiscrepancyClass`: five
codes (D01, D03, D10, D11, D12) all plant FEE_OVERCHARGE/FEE_UNDERCHARGE,
and D04 and D06 both plant REFUND_AMOUNT_MISMATCH. A true positive is
attributable to a code, through the records it touched. A false positive is
not -- there is no fact of the matter about which code a spurious fee
finding "should" have been. So support/TP/FN/recall are reported per code,
where they are exactly defined, and precision/F1 per class, where they are.
Neither table contains an invented number.

**D09 is a flag, not a discrepancy.** It corrupts a UTR and costs zero
rupees, and `datagen/` models it as a `DataQualityFlag` rather than a
`DiscrepancyEntry` for that reason. Nothing in `core/verify.py` detects it,
so its row reads support = N, TP = 0. It is carried through the confusion
matrix under its own `data_quality_flag` pseudo-class and excluded from the
macro and micro averages, so it neither inflates nor silently vanishes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel

from core.exceptions import DiscrepancyClass
from core.models import Finding, RecordRef
from datagen.ground_truth import GroundTruth

# Sentinel row/column labels in the confusion matrix. Not DiscrepancyClass
# members: neither is a discrepancy, and adding them to the taxonomy would
# put them in front of `core/verify.py` and `datagen/inject.py`.
NO_DISCREPANCY = "no_discrepancy"
NOT_DETECTED = "not_detected"
DATA_QUALITY_FLAG = "data_quality_flag"

# llm/adjudicator.py's own id scheme. A finding carrying this prefix was
# proposed by a model; anything else came from core/verify.py. Attributing
# a false positive to the boundary that produced it is the difference
# between "the system over-claims" and "one component over-claims".
ADJUDICATED_FINDING_PREFIX = "FND-ADJ-"


class PlantedItem(BaseModel):
    """One planted thing, whatever `datagen/` modelled it as. Flattens
    DiscrepancyEntry and DataQualityFlag into the one shape the matcher
    needs, so D09 travels through the same code path as everything else."""

    code: str
    true_class: str  # a DiscrepancyClass value, or DATA_QUALITY_FLAG
    records: list[RecordRef]
    amount_impact_paise: int

    @property
    def is_flag(self) -> bool:
        return self.true_class == DATA_QUALITY_FLAG


class CodeRow(BaseModel):
    """§2 table A and §3: exactly defined per D-code."""

    code: str
    classes: list[str]
    support: int
    true_positives: int
    false_negatives: int
    planted_paise: int
    detected_paise: int

    @property
    def recall(self) -> float:
        return self.true_positives / self.support if self.support else 0.0

    @property
    def value_recall(self) -> float:
        return self.detected_paise / self.planted_paise if self.planted_paise else 0.0


class ClassRow(BaseModel):
    """§2 table B: precision is only definable at this granularity."""

    discrepancy_class: str
    support: int
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        return 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0


class Averages(BaseModel):
    precision: float
    recall: float
    f1: float


class DetectionResult(BaseModel):
    code_rows: list[CodeRow]
    class_rows: list[ClassRow]
    macro: Averages
    micro: Averages

    findings_total: int
    true_positive_findings: int
    misclassified_findings: int
    false_positive_findings: int

    false_positive_paise: int
    misclassified_paise: int
    planted_paise: int
    detected_paise: int

    # Where the false positives came from. Measured, not asserted: "43
    # findings claimed rupees that were not there" is a number; "38 of them
    # came from one boundary" is the number that tells you what to fix.
    false_positive_by_class: dict[str, int] = {}
    false_positive_paise_by_class: dict[str, int] = {}
    false_positive_from_adjudicator: int = 0
    false_positive_paise_from_adjudicator: int = 0

    # true class (or NO_DISCREPANCY) -> predicted class (or NOT_DETECTED) -> count
    confusion: dict[str, dict[str, int]]

    @property
    def value_recall(self) -> float:
        return self.detected_paise / self.planted_paise if self.planted_paise else 0.0

    @property
    def count_recall(self) -> float:
        support = sum(row.support for row in self.code_rows)
        detected = sum(row.true_positives for row in self.code_rows)
        return detected / support if support else 0.0


def planted_items(ground_truth: GroundTruth) -> list[PlantedItem]:
    """Every planted thing, in a fixed order so matching is reproducible."""
    items = [
        PlantedItem(
            code=entry.code,
            true_class=entry.discrepancy_class.value,
            records=list(entry.records),
            amount_impact_paise=entry.amount_impact_paise,
        )
        for entry in ground_truth.discrepancies
    ]
    items.extend(
        PlantedItem(
            code=flag.code,
            true_class=DATA_QUALITY_FLAG,
            records=list(flag.records),
            amount_impact_paise=flag.amount_impact_paise,
        )
        for flag in ground_truth.data_quality_flags
    )
    return sorted(items, key=lambda item: (item.code, _first_record_key(item.records)))


def _first_record_key(records: Sequence[RecordRef]) -> tuple[str, str]:
    if not records:
        return ("", "")
    ordered = sorted(records, key=lambda r: (r.type.value, r.id))
    return (ordered[0].type.value, ordered[0].id)


def _index_by_record(items: Sequence[PlantedItem]) -> dict[RecordRef, list[int]]:
    index: dict[RecordRef, list[int]] = defaultdict(list)
    for position, item in enumerate(items):
        for ref in item.records:
            index[ref].append(position)
    return index


def evaluate(findings: Sequence[Finding], ground_truth: GroundTruth) -> DetectionResult:
    """Score one run's findings against what was actually planted in it."""
    items = planted_items(ground_truth)
    by_record = _index_by_record(items)

    detected_by_item: dict[int, bool] = dict.fromkeys(range(len(items)), False)
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    true_positive_findings = 0
    misclassified_findings = 0
    false_positive_findings = 0
    false_positive_paise = 0
    misclassified_paise = 0
    false_positive_by_class: dict[str, int] = defaultdict(int)
    false_positive_paise_by_class: dict[str, int] = defaultdict(int)
    false_positive_from_adjudicator = 0
    false_positive_paise_from_adjudicator = 0

    for finding in sorted(findings, key=lambda f: f.id):
        predicted = finding.discrepancy_class.value
        touched = _touched_items(finding, by_record)

        if not touched:
            confusion[NO_DISCREPANCY][predicted] += 1
            false_positive_findings += 1
            false_positive_paise += finding.amount_impact.paise
            false_positive_by_class[predicted] += 1
            false_positive_paise_by_class[predicted] += finding.amount_impact.paise
            if finding.id.startswith(ADJUDICATED_FINDING_PREFIX):
                false_positive_from_adjudicator += 1
                false_positive_paise_from_adjudicator += finding.amount_impact.paise
            continue

        # Prefer an item whose class this finding actually agrees with; the
        # matrix should show a correct call as correct even when the finding
        # also happens to touch some unrelated planted record.
        agreeing = [position for position in touched if items[position].true_class == predicted]
        if agreeing:
            position = agreeing[0]
            detected_by_item[position] = True
            confusion[items[position].true_class][predicted] += 1
            true_positive_findings += 1
            continue

        position = touched[0]
        confusion[items[position].true_class][predicted] += 1
        misclassified_findings += 1
        misclassified_paise += finding.amount_impact.paise

    for position, item in enumerate(items):
        if not detected_by_item[position]:
            confusion[item.true_class][NOT_DETECTED] += 1

    return DetectionResult(
        code_rows=_code_rows(items, detected_by_item),
        class_rows=(class_rows := _class_rows(confusion)),
        macro=_macro(class_rows),
        micro=_micro(class_rows),
        findings_total=len(findings),
        true_positive_findings=true_positive_findings,
        misclassified_findings=misclassified_findings,
        false_positive_findings=false_positive_findings,
        false_positive_paise=false_positive_paise,
        misclassified_paise=misclassified_paise,
        false_positive_by_class=dict(sorted(false_positive_by_class.items())),
        false_positive_paise_by_class=dict(sorted(false_positive_paise_by_class.items())),
        false_positive_from_adjudicator=false_positive_from_adjudicator,
        false_positive_paise_from_adjudicator=false_positive_paise_from_adjudicator,
        planted_paise=sum(item.amount_impact_paise for item in items),
        detected_paise=sum(
            item.amount_impact_paise for position, item in enumerate(items) if detected_by_item[position]
        ),
        confusion={true: dict(predicted) for true, predicted in sorted(confusion.items())},
    )


def _touched_items(finding: Finding, by_record: dict[RecordRef, list[int]]) -> list[int]:
    positions: list[int] = []
    seen: set[int] = set()
    for ref in finding.evidence_ids:
        for position in by_record.get(ref, ()):
            if position not in seen:
                seen.add(position)
                positions.append(position)
    return sorted(positions)


def _code_rows(items: Sequence[PlantedItem], detected: dict[int, bool]) -> list[CodeRow]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for position, item in enumerate(items):
        grouped[item.code].append(position)

    rows: list[CodeRow] = []
    for code in sorted(grouped):
        positions = grouped[code]
        rows.append(
            CodeRow(
                code=code,
                classes=sorted({items[p].true_class for p in positions}),
                support=len(positions),
                true_positives=sum(1 for p in positions if detected[p]),
                false_negatives=sum(1 for p in positions if not detected[p]),
                planted_paise=sum(items[p].amount_impact_paise for p in positions),
                detected_paise=sum(items[p].amount_impact_paise for p in positions if detected[p]),
            )
        )
    return rows


def _class_rows(confusion: dict[str, dict[str, int]]) -> list[ClassRow]:
    """Textbook derivation off the confusion matrix, over the real taxonomy
    only. NO_DISCREPANCY is a row (a finding on nothing) and NOT_DETECTED a
    column (a plant nothing found); neither is a class, so neither gets a
    row of its own here. DATA_QUALITY_FLAG is excluded for the same reason:
    no detector claims it, so a precision for it would be 0/0 dressed up as
    a result."""
    rows: list[ClassRow] = []
    for member in DiscrepancyClass:
        name = member.value
        true_positives = confusion.get(name, {}).get(name, 0)
        false_positives = sum(
            counts.get(name, 0) for true_class, counts in confusion.items() if true_class != name
        )
        false_negatives = sum(
            count for predicted, count in confusion.get(name, {}).items() if predicted != name
        )
        rows.append(
            ClassRow(
                discrepancy_class=name,
                support=true_positives + false_negatives,
                true_positives=true_positives,
                false_positives=false_positives,
                false_negatives=false_negatives,
            )
        )
    return rows


def _macro(rows: Sequence[ClassRow]) -> Averages:
    """Unweighted mean over classes that were actually planted or predicted.
    A class with no support and no prediction contributes nothing rather
    than a free 0.0, which would drag the average by how many taxonomy
    members happen to exist."""
    present = [r for r in rows if r.support or r.true_positives or r.false_positives]
    if not present:
        return Averages(precision=0.0, recall=0.0, f1=0.0)
    n = len(present)
    return Averages(
        precision=sum(r.precision for r in present) / n,
        recall=sum(r.recall for r in present) / n,
        f1=sum(r.f1 for r in present) / n,
    )


def _micro(rows: Sequence[ClassRow]) -> Averages:
    true_positives = sum(r.true_positives for r in rows)
    false_positives = sum(r.false_positives for r in rows)
    false_negatives = sum(r.false_negatives for r in rows)
    precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) else 0.0
    recall = true_positives / (true_positives + false_negatives) if (true_positives + false_negatives) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return Averages(precision=precision, recall=recall, f1=f1)


def _sum_dicts(dicts) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for entry in dicts:
        for key, value in entry.items():
            totals[key] += value
    return dict(sorted(totals.items()))


def merge(results: Sequence[DetectionResult]) -> DetectionResult:
    """Pool several runs into one result, by summing counts rather than
    averaging rates -- a sweep's headline number must not let a run with
    three plants weigh as much as one with sixty."""
    if not results:
        raise ValueError("cannot merge zero detection results")

    code_totals: dict[str, CodeRow] = {}
    for result in results:
        for row in result.code_rows:
            existing = code_totals.get(row.code)
            if existing is None:
                code_totals[row.code] = row.model_copy(deep=True)
                continue
            code_totals[row.code] = CodeRow(
                code=row.code,
                classes=sorted(set(existing.classes) | set(row.classes)),
                support=existing.support + row.support,
                true_positives=existing.true_positives + row.true_positives,
                false_negatives=existing.false_negatives + row.false_negatives,
                planted_paise=existing.planted_paise + row.planted_paise,
                detected_paise=existing.detected_paise + row.detected_paise,
            )

    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for result in results:
        for true_class, predictions in result.confusion.items():
            for predicted, count in predictions.items():
                confusion[true_class][predicted] += count

    class_rows = _class_rows(confusion)
    return DetectionResult(
        code_rows=[code_totals[code] for code in sorted(code_totals)],
        class_rows=class_rows,
        macro=_macro(class_rows),
        micro=_micro(class_rows),
        findings_total=sum(r.findings_total for r in results),
        true_positive_findings=sum(r.true_positive_findings for r in results),
        misclassified_findings=sum(r.misclassified_findings for r in results),
        false_positive_findings=sum(r.false_positive_findings for r in results),
        false_positive_paise=sum(r.false_positive_paise for r in results),
        misclassified_paise=sum(r.misclassified_paise for r in results),
        false_positive_by_class=_sum_dicts(r.false_positive_by_class for r in results),
        false_positive_paise_by_class=_sum_dicts(r.false_positive_paise_by_class for r in results),
        false_positive_from_adjudicator=sum(r.false_positive_from_adjudicator for r in results),
        false_positive_paise_from_adjudicator=sum(r.false_positive_paise_from_adjudicator for r in results),
        planted_paise=sum(r.planted_paise for r in results),
        detected_paise=sum(r.detected_paise for r in results),
        confusion={true: dict(predicted) for true, predicted in sorted(confusion.items())},
    )
