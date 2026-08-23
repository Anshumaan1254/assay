"""Ground truth for one generated run: every planted discrepancy, the D09
data-quality flags, and the optional silent-corruption record. This is
exactly what tests/test_datagen_inject.py verifies against, and the only
shape eval/ is allowed to read (nothing else may import datagen/ at all --
enforced in tests/test_architecture.py).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from core.exceptions import DiscrepancyClass
from core.models import RecordRef


class DiscrepancyEntry(BaseModel):
    code: str  # "D01".."D08", "D10".."D12" -- D09 is a DataQualityFlag, not this
    discrepancy_class: DiscrepancyClass
    records: list[RecordRef]
    amount_impact_paise: int
    detail: dict[str, str | int]


class DataQualityFlag(BaseModel):
    code: str  # "D09"
    records: list[RecordRef]
    amount_impact_paise: int  # always 0 -- a flag, never a rupee loss
    detail: dict[str, str | int]


class SilentCorruption(BaseModel):
    record_type: str
    record_id: str
    field: str
    original_paise: int
    corrupted_paise: int
    delta_paise: int


class GroundTruth(BaseModel):
    run_id: str
    seed: int
    profile: str
    generated_at: datetime
    silent_corruption: SilentCorruption | None = None
    discrepancies: list[DiscrepancyEntry] = Field(default_factory=list)
    data_quality_flags: list[DataQualityFlag] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
