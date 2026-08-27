"""Structured incident logging for the chaos suite.

Each scenario test wraps its body in `chaos_scenario(...)`: it times the
block, writes one JSON file to `chaos/incidents/<scenario_id>.json`
describing what was injected, what was expected, and what actually
happened -- pass or fail -- and then re-raises whatever the block raised,
so pytest's own reporting is completely unaffected by this existing.
`make chaos` reads every incident file back and prints one pass/fail table,
which is the demo/screen-recording artifact CLAUDE.md asks for.

Not the append-only ledger: this is a report artifact, overwritten on every
run of the suite, never a money record. Idempotency does not apply here.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel

INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"

Category = Literal["money_corruption", "crash", "degraded_gracefully", "data_quality"]


class Incident(BaseModel):
    scenario_id: str
    title: str
    category: Category
    failure_injected: str
    expected_behavior: str
    passed: bool
    outcome: Literal["contained", "not_contained"]
    notes: str = ""
    money_impact_paise: int | None = None
    wall_clock_ms: float
    error: str = ""


def write_incident(incident: Incident) -> Path:
    INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = INCIDENTS_DIR / f"{incident.scenario_id}.json"
    path.write_text(incident.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_incidents() -> list[Incident]:
    if not INCIDENTS_DIR.is_dir():
        return []
    return [
        Incident.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(INCIDENTS_DIR.glob("*.json"))
    ]


class chaos_scenario:
    """`with chaos_scenario("C06", title=..., category=..., failure_injected=..., expected_behavior=...) as scenario:`

    Call `scenario.note(...)` inside the block to attach what was actually
    observed (a money amount, a specific reason code) before the block
    exits. The incident is written whether the block passes or raises;
    the exception, if any, is never suppressed -- pytest sees it exactly
    as it would without this wrapper.
    """

    def __init__(
        self,
        scenario_id: str,
        *,
        title: str,
        category: Category,
        failure_injected: str,
        expected_behavior: str,
    ) -> None:
        self.scenario_id = scenario_id
        self.title = title
        self.category = category
        self.failure_injected = failure_injected
        self.expected_behavior = expected_behavior
        self.notes = ""
        self.money_impact_paise: int | None = None

    def note(self, text: str = "", *, money_impact_paise: int | None = None) -> None:
        if text:
            self.notes = text
        if money_impact_paise is not None:
            self.money_impact_paise = money_impact_paise

    def __enter__(self) -> Self:
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_value, tb) -> bool:
        elapsed_ms = (time.monotonic() - self._start) * 1000
        passed = exc_type is None
        error = "" if passed else "".join(traceback.format_exception(exc_type, exc_value, tb))[-4000:]
        write_incident(
            Incident(
                scenario_id=self.scenario_id,
                title=self.title,
                category=self.category,
                failure_injected=self.failure_injected,
                expected_behavior=self.expected_behavior,
                passed=passed,
                outcome="contained" if passed else "not_contained",
                notes=self.notes,
                money_impact_paise=self.money_impact_paise,
                wall_clock_ms=elapsed_ms,
                error=error,
            )
        )
        return False
