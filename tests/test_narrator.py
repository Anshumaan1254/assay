"""Tests for llm/narrator.py.

Trust-critical, not money-critical: no arithmetic happens here, but the
model's prose could still smuggle in a number nobody computed. Every digit
sequence the model writes must be traceable back to the dispute packet's own
already-computed fields -- the reference-checking discipline invariant 6
asks for, applied to numbers instead of citations.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.exceptions import DisputePacket
from core.money import Money
from llm.narrator import (
    NarrationRejected,
    NarrationResponse,
    build_prompt,
    narrate_dispute_packet,
    numbers_in_narration_are_grounded,
)
from llm.provider import ProviderUnavailable
from tests.support import RecordingProvider

CLUSTER_ID = "CLU-RUN-1-fee_overcharge-card.credit.tier1-100"


def _packet(total_paise: int = 12_400_00, finding_ids: list[str] | None = None) -> DisputePacket:
    finding_ids = finding_ids if finding_ids is not None else ["FND-1", "FND-2", "FND-3"]
    return DisputePacket(
        cluster_id=CLUSTER_ID,
        discrepancy_class="fee_overcharge",
        claim=f"Rs {total_paise / 100:.2f} fee_overcharge across {len(finding_ids)} findings",
        contract_clause="Credit card up to Rs.2,000: 1.80%",
        recomputed_arithmetic=None,
        evidence_ids=[],
        finding_ids=finding_ids,
        total_impact=Money(total_paise),
    )


# ---------------------------------------------------------------------------
# numbers_in_narration_are_grounded
# ---------------------------------------------------------------------------


def test_a_narration_using_only_the_packets_own_numbers_is_grounded():
    packet = _packet(total_paise=12_400_00, finding_ids=["FND-1", "FND-2", "FND-3"])
    narration = "Across 3 findings the gateway overcharged fees totalling Rs 12400.00."

    assert numbers_in_narration_are_grounded(narration, packet) == []


def test_a_narration_using_the_whole_rupee_amount_without_cents_is_grounded():
    packet = _packet(total_paise=12_400_00)
    narration = "The merchant is owed Rs 12400 across these transactions."

    assert numbers_in_narration_are_grounded(narration, packet) == []


def test_a_narration_inventing_a_number_is_not_grounded():
    packet = _packet(total_paise=12_400_00, finding_ids=["FND-1", "FND-2", "FND-3"])
    narration = "Across 3 findings the gateway overcharged fees totalling Rs 50000.00."

    offenders = numbers_in_narration_are_grounded(narration, packet)

    assert offenders == ["50000.00"]


# ---------------------------------------------------------------------------
# narrate_dispute_packet
# ---------------------------------------------------------------------------


def test_narrate_dispute_packet_fills_narration_and_status_when_well_grounded():
    packet = _packet(total_paise=12_400_00, finding_ids=["FND-1", "FND-2", "FND-3"])
    provider = RecordingProvider({"narration": "Across 3 findings the gateway overcharged fees by Rs 12400.00."})

    result = narrate_dispute_packet(packet, provider)

    assert result.narration == "Across 3 findings the gateway overcharged fees by Rs 12400.00."
    assert result.narration_status == "generated"
    # The original packet is untouched -- AssayModel is frozen.
    assert packet.narration is None
    assert packet.narration_status == "unavailable"


def test_narrate_dispute_packet_sends_the_schema_and_model_hint():
    packet = _packet()
    provider = RecordingProvider({"narration": "Rs 12400.00 across 3 findings."})

    narrate_dispute_packet(packet, provider, model_hint="flash-lite")

    assert len(provider.calls) == 1
    prompt, schema, model_hint = provider.calls[0]
    assert schema is NarrationResponse
    assert model_hint == "flash-lite"
    assert packet.claim in prompt


def test_narrate_dispute_packet_rejects_a_narration_that_invents_a_number():
    packet = _packet(total_paise=12_400_00, finding_ids=["FND-1", "FND-2", "FND-3"])
    provider = RecordingProvider({"narration": "The merchant is owed Rs 99999.00 across these transactions."})

    with pytest.raises(NarrationRejected):
        narrate_dispute_packet(packet, provider)


def test_narrate_dispute_packet_rejects_a_schema_invalid_response():
    packet = _packet()
    provider = RecordingProvider({"not_narration": "oops"})

    with pytest.raises(NarrationRejected):
        narrate_dispute_packet(packet, provider)


def test_provider_unavailable_propagates_untouched():
    packet = _packet()
    provider = RecordingProvider(ProviderUnavailable("no api key configured"))

    with pytest.raises(ProviderUnavailable):
        narrate_dispute_packet(packet, provider)


def test_build_prompt_mentions_the_claim_and_total_impact():
    packet = _packet(total_paise=12_400_00)

    prompt = build_prompt(packet)

    assert packet.claim in prompt
    assert "12400.00" in prompt


def test_narration_response_requires_the_narration_field():
    with pytest.raises(ValidationError):
        NarrationResponse.model_validate({})
