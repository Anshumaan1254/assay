"""Append-only, idempotent journal.

Ledger, here, is the read side: an in-memory index over one loaded batch,
O(1) lookup by (type, id) and by settlement_id. It's what invariant 6's LLM
reference checker calls exists() against, and what decompose.py/verify.py
will use to pull the records a settlement cycle actually touched.

Four more indexes exist for decompose.py, each because a join it needs is
not expressible with by_settlement():

  - by_utr: BankCredit has no settlement_id. Its only structural link to a
    SettlementBatch is the UTR.
  - by_type: enumerating every batch, or every unlinked payment, without a
    linear scan per credit.
  - fee_lines_for / tax_lines_for: FeeLine and TaxLine have no
    settlement_id either. They attach transitively, via applies_to_id and
    applies_to_fee_id. A decomposition built only on by_settlement() would
    omit every deduction and report the gross.

Every lookup returns refs sorted by (type, id). Insertion order is not
reproducible across input orderings, and decompose sums these refs into a
proof whose SHA-256 has to be stable (invariant 4).

The append-only, idempotent-write side of invariant 7 (posting JournalEntry
rows without double-posting on a re-run) is a separate concern from this
read index and isn't implemented here yet.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator

from core.models import AssayModel, EntityType, RecordRef


def _sort_key(ref: RecordRef) -> tuple[str, str]:
    return (ref.type.value, ref.id)


class Ledger:
    def __init__(self, records: Iterable[AssayModel]) -> None:
        self._by_ref: dict[RecordRef, AssayModel] = {}
        self._by_settlement: dict[str, list[RecordRef]] = defaultdict(list)
        self._by_utr: dict[str, list[RecordRef]] = defaultdict(list)
        self._by_type: dict[EntityType, list[RecordRef]] = defaultdict(list)
        self._fee_lines_by_target: dict[RecordRef, list[RecordRef]] = defaultdict(list)
        self._tax_lines_by_fee: dict[RecordRef, list[RecordRef]] = defaultdict(list)

        for record in records:
            ref = RecordRef(type=record.RECORD_TYPE, id=record.id)
            self._by_ref[ref] = record
            self._by_type[record.RECORD_TYPE].append(ref)

            settlement_id = getattr(record, "settlement_id", None)
            if settlement_id is not None:
                self._by_settlement[settlement_id].append(ref)

            # An empty UTR is the "no structural key" case, not a wildcard
            # that would match every record lacking one.
            utr = getattr(record, "utr", None)
            if utr:
                self._by_utr[utr].append(ref)

            if record.RECORD_TYPE is EntityType.FEE_LINE:
                self._fee_lines_by_target[record.applies_to_ref].append(ref)
            elif record.RECORD_TYPE is EntityType.TAX_LINE:
                fee_ref = RecordRef(type=EntityType.FEE_LINE, id=record.applies_to_fee_id)
                self._tax_lines_by_fee[fee_ref].append(ref)

        for index in (
            self._by_settlement,
            self._by_utr,
            self._by_type,
            self._fee_lines_by_target,
            self._tax_lines_by_fee,
        ):
            for refs in index.values():
                refs.sort(key=_sort_key)

        self._all_refs: list[RecordRef] = sorted(self._by_ref, key=_sort_key)

    def exists(self, ref: RecordRef) -> bool:
        return ref in self._by_ref

    def get(self, ref: RecordRef) -> AssayModel | None:
        return self._by_ref.get(ref)

    def by_settlement(self, settlement_id: str) -> list[RecordRef]:
        return list(self._by_settlement.get(settlement_id, []))

    def by_utr(self, utr: str) -> list[RecordRef]:
        """Every record carrying this UTR -- both the SettlementBatch and the
        BankCredit, when both are loaded.

        Returns a list rather than an optional on purpose: two batches
        sharing a UTR is a real condition, and a caller that silently kept
        whichever one the index happened to hold would be making exactly the
        kind of arbitrary choice decompose exists to refuse.
        """
        if not utr:
            return []
        return list(self._by_utr.get(utr, []))

    def by_type(self, entity_type: EntityType) -> list[RecordRef]:
        return list(self._by_type.get(entity_type, []))

    def fee_lines_for(self, ref: RecordRef) -> list[RecordRef]:
        """Fee lines charged against this record. Matches on type as well as
        id: a dispute fee applies to a CHARGEBACK, and a payment with a
        colliding id must not pick it up."""
        return list(self._fee_lines_by_target.get(ref, []))

    def tax_lines_for(self, fee_ref: RecordRef) -> list[RecordRef]:
        return list(self._tax_lines_by_fee.get(fee_ref, []))

    def __len__(self) -> int:
        return len(self._by_ref)

    def __iter__(self) -> Iterator[RecordRef]:
        return iter(self._all_refs)
