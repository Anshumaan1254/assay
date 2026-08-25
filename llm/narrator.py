"""Human-readable explanation of an already-computed finding. Numbers are
injected from the computed result, never generated.

The model is asked to write prose around a dispute packet's already-computed
claim, contract clause, and arithmetic -- it never invents an amount.
Enforced, not just asked for: every digit sequence the model's own narration
text contains must also appear among the packet's own computed numbers
(the rupee total, its whole-rupee form, and the finding count). A narration
that fails this check is rejected outright, the same discipline
llm/adjudicator.py applies to citations, just applied to numbers instead of
record ids. `core.exceptions` may be imported here (llm/ -> core/ is legal);
the reverse is not (invariant 2).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ValidationError

from core.exceptions import DisputePacket
from llm.provider import LLMProvider

DEFAULT_MODEL_HINT = "flash-lite"

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


class NarrationRejected(Exception):
    """The model's response either failed schema validation or contained a
    number not traceable to the dispute packet's own computed fields.
    Propagates -- no partial repair, mirroring AdjudicationRejected and
    ContractParseRejected: a bad narration is discarded outright, not
    patched."""


class NarrationResponse(BaseModel):
    narration: str


def _normalize(number: str) -> str:
    return number.replace(",", "")


def _allowed_numbers(packet: DisputePacket) -> set[str]:
    rupees = _normalize(packet.total_impact.to_rupees_str().lstrip("-"))
    whole, _, _frac = rupees.partition(".")
    return {rupees, whole, str(len(packet.finding_ids))}


def numbers_in_narration_are_grounded(narration: str, packet: DisputePacket) -> list[str]:
    """Every digit sequence in `narration` that is NOT traceable to one of
    `packet`'s own computed numbers. Empty means fully grounded."""
    allowed = _allowed_numbers(packet)
    return [match.group() for match in _NUMBER_RE.finditer(narration) if _normalize(match.group()) not in allowed]


def build_prompt(packet: DisputePacket) -> str:
    clause = packet.contract_clause or "no contract clause is directly traceable for this cluster"
    lines = [
        (
            "Write a one-paragraph, plain-English explanation of the settlement discrepancy below, "
            "for inclusion in a dispute packet sent to a payment gateway."
        ),
        (
            "Use ONLY the numbers given here. Do not compute, estimate, or invent any amount, "
            "percentage, or count not explicitly listed below."
        ),
        f"Claim: {packet.claim}",
        f"Discrepancy class: {packet.discrepancy_class.value}",
        f"Contract clause: {clause}",
        f"Total impact: {packet.total_impact.to_rupees_str()}",
        f"Number of findings: {len(packet.finding_ids)}",
    ]
    return "\n".join(lines)


def narrate_dispute_packet(
    packet: DisputePacket, provider: LLMProvider, model_hint: str = DEFAULT_MODEL_HINT
) -> DisputePacket:
    """Fill in `packet.narration` via `provider`. `ProviderUnavailable`
    propagates untouched -- the caller decides whether that means leaving
    `narration_status="unavailable"` or failing the run."""
    prompt = build_prompt(packet)
    raw = provider.generate_structured(prompt, NarrationResponse, model_hint)
    try:
        response = NarrationResponse.model_validate(raw)
    except ValidationError as exc:
        raise NarrationRejected(
            f"cluster {packet.cluster_id}: narration response failed schema validation"
        ) from exc

    offending = numbers_in_narration_are_grounded(response.narration, packet)
    if offending:
        raise NarrationRejected(
            f"cluster {packet.cluster_id}: narration contains numbers not present in the "
            f"computed finding: {offending}"
        )

    return packet.model_copy(update={"narration": response.narration, "narration_status": "generated"})
