"""Append-only, idempotent journal.

Ledger, here, is the read side: an in-memory index over one loaded batch,
O(1) lookup by (type, id) and by settlement_id. It's what invariant 6's LLM
reference checker calls exists() against, and what decompose.py/verify.py
will use to pull the records a settlement cycle actually touched.

The append-only, idempotent-write side of invariant 7 (posting JournalEntry
rows without double-posting on a re-run) is a separate concern from this
read index and isn't implemented here yet.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from core.models import AssayModel, RecordRef


class Ledger:
    def __init__(self, records: Iterable[AssayModel]) -> None:
        self._by_ref: dict[RecordRef, AssayModel] = {}
        self._by_settlement: dict[str, list[RecordRef]] = defaultdict(list)

        for record in records:
            ref = RecordRef(type=record.RECORD_TYPE, id=record.id)
            self._by_ref[ref] = record

            settlement_id = getattr(record, "settlement_id", None)
            if settlement_id is not None:
                self._by_settlement[settlement_id].append(ref)

    def exists(self, ref: RecordRef) -> bool:
        return ref in self._by_ref

    def get(self, ref: RecordRef) -> AssayModel | None:
        return self._by_ref.get(ref)

    def by_settlement(self, settlement_id: str) -> list[RecordRef]:
        return list(self._by_settlement.get(settlement_id, []))
