"""Tests for the LLM contract boundary.

The parser's whole job is to be untrusting. It hands a schema to a provider,
takes back a dict it assumes nothing about, validates it, reference-checks
it, and either produces a compiled contract or refuses -- naming the clause
that failed. There is no fallback rate card and no repair step.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest

from core.contract import IST, CompiledContract, RateCardParse, RoundingMode, sha256_of
from core.models import FeeType, PaymentMethod
from llm.contract_parser import (
    ContractParseRejected,
    build_prompt,
    compile_rate_card,
    parse_rate_card,
)
from llm.provider import ProviderUnavailable
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from tests.support import RecordingProvider

DOCUMENT = "# Merchant Rate Card - MERCH-0001\n\nUPI: flat Rs.2.00/txn processing fee.\n"


def good_response() -> dict:
    return {
        "merchant_id": "MERCH-0001",
        "currency": "INR",
        "rules": [
            {
                "rule_id": "upi",
                "fee_type": "fixed",
                "effective_from": "2026-07-01",
                "effective_to": None,
                "supersedes": None,
                "applies_when": {"method": "upi"},
                "rate_bps": 0,
                "fixed_fee_paise": 200,
                "cap_paise": None,
                "taxes": [{"tax_type": "GST", "rate_bps": 1800, "base": "fee_amount"}],
                "source_quote": "UPI: flat Rs.2.00/txn processing fee.",
            },
            {
                "rule_id": "upi.mdr",
                "fee_type": "mdr",
                "effective_from": "2026-07-01",
                "applies_when": {"method": "upi"},
                "rate_bps": 0,
                "taxes": [],
                "source_quote": "UPI: no MDR.",
            },
        ],
        "dormant_clauses": [],
        "rounding_note": None,
        "parser_notes": "",
    }


# ---------------------------------------------------------------------
# the boundary itself
# ---------------------------------------------------------------------


def test_the_schema_class_is_handed_to_the_provider():
    """Constrained decoding, not prompt-and-pray: the provider gets the
    Pydantic class so it can shape `response_format.schema`."""
    provider = RecordingProvider(good_response())
    parse_rate_card(DOCUMENT, provider)

    (prompt, schema, model_hint) = provider.calls[0]
    assert schema is RateCardParse
    assert model_hint == "flash"
    assert DOCUMENT in prompt


def test_prompt_describes_the_task_not_the_json_shape():
    """The shape is the schema's job. If the prompt started spelling out
    field names, the two would drift apart silently."""
    prompt = build_prompt(DOCUMENT)

    assert "paise" in prompt and "basis points" in prompt
    for shape_talk in ("```json", '{"', "JSON object", "respond with json"):
        assert shape_talk.lower() not in prompt.lower()


def test_a_good_response_parses():
    parse = parse_rate_card(DOCUMENT, RecordingProvider(good_response()))

    assert isinstance(parse, RateCardParse)
    assert parse.merchant_id == "MERCH-0001"
    assert parse.rules[0].fixed_fee_paise == 200


def test_provider_unavailable_propagates():
    """No fallback rate card, ever. An absent model means no contract, not
    a guessed one."""
    provider = RecordingProvider(ProviderUnavailable("rate limited past retries"))

    with pytest.raises(ProviderUnavailable):
        parse_rate_card(DOCUMENT, provider)


def test_null_provider_yields_no_contract():
    with pytest.raises(ProviderUnavailable):
        parse_rate_card(DOCUMENT, NullProvider())


def test_contract_parser_does_not_import_the_gemini_sdk():
    """`llm/` talks to the Protocol, never to a vendor SDK."""
    source = Path("llm/contract_parser.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "google" not in imported
    assert "genai" not in imported


# ---------------------------------------------------------------------
# rejection: schema-invalid responses name the failing clause
# ---------------------------------------------------------------------


def test_out_of_enum_network_rejects_the_whole_parse_and_names_the_clause():
    response = good_response()
    response["rules"][0]["applies_when"] = {"method": "card", "network": "diners club"}
    response["rules"][0]["source_quote"] = "Diners Club cards attract 2.40% MDR."

    with pytest.raises(ContractParseRejected) as excinfo:
        parse_rate_card(DOCUMENT, RecordingProvider(response))

    message = str(excinfo.value)
    assert "Diners Club cards attract 2.40% MDR." in message
    assert "upi" in message  # and the rule_id it came from


def test_out_of_enum_method_rejects():
    response = good_response()
    response["rules"][0]["applies_when"] = {"method": "cryptocurrency"}

    with pytest.raises(ContractParseRejected):
        parse_rate_card(DOCUMENT, RecordingProvider(response))


def test_missing_required_field_rejects():
    response = good_response()
    del response["rules"][0]["source_quote"]

    with pytest.raises(ContractParseRejected):
        parse_rate_card(DOCUMENT, RecordingProvider(response))


def test_dangling_supersedes_reference_rejects():
    """Invariant 6's reference check, applied to the only kind of reference
    a rate card parse can make."""
    response = good_response()
    response["rules"][0]["supersedes"] = "no.such.rule"

    with pytest.raises(ContractParseRejected, match="no.such.rule"):
        parse_rate_card(DOCUMENT, RecordingProvider(response))


def test_regex_shaped_mcc_pattern_rejects():
    response = good_response()
    response["rules"][0]["applies_when"] = {"method": "upi", "mcc_pattern": "54[0-9]+"}

    with pytest.raises(ContractParseRejected):
        parse_rate_card(DOCUMENT, RecordingProvider(response))


def test_rejection_is_logged_with_the_offending_clause(capsys):
    """Invariant 6 asks for every rejection to be logged -- those logs are
    the evidence that the engine discarded a bad answer rather than acting
    on it."""
    response = good_response()
    response["rules"][0]["applies_when"] = {"method": "upi", "network": "diners club"}
    response["rules"][0]["source_quote"] = "Diners Club: 2.40%."

    with pytest.raises(ContractParseRejected):
        parse_rate_card(DOCUMENT, RecordingProvider(response))

    logged = capsys.readouterr().out
    assert "contract_parse_rejected" in logged
    assert "Diners Club" in logged


def test_a_response_that_is_not_a_dict_rejects():
    with pytest.raises(ContractParseRejected):
        parse_rate_card(DOCUMENT, RecordingProvider(["not", "a", "mapping"]))  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# compile: parse -> validated schedule
# ---------------------------------------------------------------------


def test_compile_produces_a_usable_contract():
    contract = compile_rate_card(DOCUMENT, RecordingProvider(good_response()))

    assert isinstance(contract, CompiledContract)
    assert contract.version_id.startswith("rc-")
    assert contract.rounding is RoundingMode.HALF_UP


def test_compile_records_the_source_document_hash():
    contract = compile_rate_card(DOCUMENT, RecordingProvider(good_response()))

    assert contract.source_sha256 == sha256_of(DOCUMENT)


def test_compile_honours_the_configured_rounding_mode():
    contract = compile_rate_card(
        DOCUMENT, RecordingProvider(good_response()), rounding=RoundingMode.HALF_EVEN
    )

    assert contract.rounding is RoundingMode.HALF_EVEN


def test_compile_surfaces_contract_errors_rather_than_returning_a_bad_schedule():
    """A structurally valid parse can still be an unusable contract. The
    ContractError must reach the caller intact."""
    from core.contract import ContractError

    response = good_response()
    response["rules"].append(
        {
            "rule_id": "upi.duplicate",
            "fee_type": "fixed",
            "effective_from": "2026-07-01",
            "applies_when": {"method": "upi"},
            "fixed_fee_paise": 300,
            "taxes": [],
            "source_quote": "a second, contradictory UPI clause",
        }
    )

    with pytest.raises(ContractError):
        compile_rate_card(DOCUMENT, RecordingProvider(response))


def test_the_compiled_contract_prices_a_transaction_end_to_end():
    from core.models import Payment
    from core.money import Money

    contract = compile_rate_card(DOCUMENT, RecordingProvider(good_response()))
    payment = Payment(
        id="pay_1",
        merchant_id="MERCH-0001",
        amount=Money(50_000),
        method=PaymentMethod.UPI,
        network=None,
        card_type=None,
        is_international=False,
        mcc="5411",
        captured_at=datetime(2026, 7, 10, 12, 0, tzinfo=IST),
        settlement_id=None,
    )
    breakdown = contract.fee_for(payment, datetime(2026, 7, 10, 12, 0, tzinfo=IST))

    assert breakdown.total_fee == Money(200)
    assert breakdown.total_tax == Money(36)
    assert breakdown.fees[0].fee_type is FeeType.FIXED


# ---------------------------------------------------------------------
# caching -- the existing CachedProvider, not a second cache
# ---------------------------------------------------------------------


def test_a_cache_hit_never_reaches_the_inner_provider(tmp_path: Path):
    inner = RecordingProvider(good_response())
    cached = CachedProvider(inner, cache_dir=tmp_path)

    first = parse_rate_card(DOCUMENT, cached)
    second = parse_rate_card(DOCUMENT, cached)

    assert len(inner.calls) == 1
    assert first == second


def test_a_replay_from_cache_works_with_a_dead_provider(tmp_path: Path):
    """What `make demo` relies on: the cache is committed, so a replay needs
    no API key and no network."""
    CachedProvider(RecordingProvider(good_response()), cache_dir=tmp_path).generate_structured(
        build_prompt(DOCUMENT), RateCardParse, "flash"
    )

    offline = CachedProvider(NullProvider(), cache_dir=tmp_path)
    assert parse_rate_card(DOCUMENT, offline).merchant_id == "MERCH-0001"


def test_a_changed_document_misses_the_cache(tmp_path: Path):
    inner = RecordingProvider(good_response())
    cached = CachedProvider(inner, cache_dir=tmp_path)

    parse_rate_card(DOCUMENT, cached)
    parse_rate_card(DOCUMENT + "\nAddendum: wallet 1.90%.\n", cached)

    assert len(inner.calls) == 2
