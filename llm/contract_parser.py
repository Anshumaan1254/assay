"""Unstructured rate card to structured, effective-dated fee schedule.
Validated, then compiled to deterministic rules. A human signs off once.

This module is deliberately thin, and deliberately untrusting.

It never sees a vendor SDK: it takes an `LLMProvider` and hands it
`core.contract.RateCardParse`, so the provider can constrain decoding to
that schema rather than the prompt asking for a shape and hoping. The
prompt describes the reading task and says nothing about field names --
if it did, the prompt and the schema would drift apart silently.

What comes back is a dict this module assumes nothing about. It is
validated, its one kind of internal reference (`supersedes`) is checked,
and then it is compiled. Any failure names the clause that caused it and
is logged. There is no repair step and no fallback rate card: if the model
is unavailable or its answer will not validate, the result is no contract,
which is the honest outcome.

Caching is not implemented here. `llm/providers/cached.py` already keys a
disk cache on SHA-256 of (prompt + schema + model); wrapping a provider in
it is all this needs, and a second cache would be a second thing to get
wrong.
"""

from __future__ import annotations

from collections.abc import Mapping

import structlog
from pydantic import ValidationError

from core.contract import (
    CompiledContract,
    RateCardParse,
    RoundingMode,
    sha256_of,
)
from llm.provider import LLMProvider

logger = structlog.get_logger(__name__)

DEFAULT_MODEL_HINT = "flash"


class ContractParseRejected(Exception):
    """The model's answer was not a usable reading of the rate card.

    Carries the offending clause verbatim, because "the parse failed" is
    not actionable and "this table row produced a network we do not
    recognise" is.
    """


_PROMPT = """You are reading a merchant payment-gateway rate card and recording every priced
clause it contains, so that a deterministic engine can recompute settlement fees
from it later without consulting a language model again.

How to read it:

* Record only what the document states. If the card does not price a payment
  method, do not create a clause for it. An absent clause is a fact about the
  contract; an invented one is a defect.
* Amounts are in paise: 1 rupee is 100 paise, so "Rs.70" is 7000. Rates are in
  basis points: 1% is 100 basis points, so "1.75%" is 175.
* A flat "Rs.10 per transaction" is a fixed fee of 1000, not a rate.
* An amount band runs from its lower bound inclusive to its upper bound
  exclusive. "Up to Rs.2,000" is 0 up to 200000. "Rs.2,000 - Rs.10,000" is
  200000 up to 1000000. "Above Rs.10,000" starts at 1000000 with no upper bound.
* A percentage and a flat fee stated together, like "1.75% + Rs.10", are one
  clause carrying both figures -- not two clauses.
* A cap, like "capped at Rs.70 per transaction", limits the percentage part
  only. A flat fee stated alongside it is added on top.
* A surcharge that stacks on top of another charge, like an international
  loading, is its own clause, scoped to the transactions it applies to.
* Every clause quotes, verbatim, the sentence or table row it came from.

Effective dating:

* A clause runs from its effective date, inclusive.
* When an addendum revises an existing clause, close the original by setting
  its end date to the addendum's effective date, and point the new clause at
  the original it replaces.
* Leave every clause the addendum does not mention open-ended and untouched.
  Most of a rate card does not change when one slab is revised.

Two things not to do:

* A clause the document states and then disclaims -- a tax that does not apply
  to this merchant, for instance -- is recorded as a dormant clause together
  with the stated reason. Do not silently drop it; a reader needs to see that
  it was read and found inapplicable.
* Say nothing about how to round. If the document discusses rounding, put that
  text in the rounding note. Rounding is the engine's decision, not yours.

Identifiers are lowercase and describe what a clause is, like
card.credit.tier1 -- not where it happens to sit in the document.

The rate card follows.

---
{document}
---
"""


def build_prompt(document: str) -> str:
    return _PROMPT.format(document=document.strip())


def _clause_context(raw: object, loc: tuple) -> str:
    """Point at the clause a validation error came from.

    A rate card is a document a person can go and look at, so an error that
    names the offending row is worth more than one that names a field path.
    """
    if not isinstance(raw, Mapping):
        return ""
    rules = raw.get("rules")
    if not (len(loc) >= 2 and loc[0] == "rules" and isinstance(loc[1], int)):
        return ""
    if not isinstance(rules, list) or not 0 <= loc[1] < len(rules):
        return ""
    rule = rules[loc[1]]
    if not isinstance(rule, Mapping):
        return ""
    rule_id = rule.get("rule_id", f"#{loc[1]}")
    quote = rule.get("source_quote", "<clause quoted no source text>")
    return f" (rule {rule_id!r}, from clause: {quote!r})"


def _describe(raw: object, error: ValidationError) -> list[str]:
    described = []
    for item in error.errors():
        loc = tuple(item["loc"])
        where = ".".join(str(part) for part in loc) or "<document>"
        described.append(f"{where}: {item['msg']}{_clause_context(raw, loc)}")
    return described


def parse_rate_card(
    document: str,
    provider: LLMProvider,
    *,
    model_hint: str = DEFAULT_MODEL_HINT,
) -> RateCardParse:
    """Read `document` into a validated `RateCardParse`.

    `ProviderUnavailable` is allowed to propagate untouched: an absent model
    means no contract, never a guessed one.
    """
    prompt = build_prompt(document)
    document_sha256 = sha256_of(document)

    raw = provider.generate_structured(prompt, RateCardParse, model_hint)

    try:
        parse = RateCardParse.model_validate(raw)
    except ValidationError as error:
        reasons = _describe(raw, error)
        logger.warning(
            "contract_parse_rejected",
            document_sha256=document_sha256,
            model_hint=model_hint,
            error_count=len(reasons),
            reasons=reasons,
        )
        raise ContractParseRejected(
            f"the model's reading of the rate card was rejected and no contract was produced "
            f"({len(reasons)} problem(s)): " + "; ".join(reasons)
        ) from error

    logger.info(
        "contract_parse_accepted",
        document_sha256=document_sha256,
        model_hint=model_hint,
        rule_count=len(parse.rules),
        dormant_clause_count=len(parse.dormant_clauses),
    )
    return parse


def compile_rate_card(
    document: str,
    provider: LLMProvider,
    *,
    rounding: RoundingMode = RoundingMode.HALF_UP,
    model_hint: str = DEFAULT_MODEL_HINT,
) -> CompiledContract:
    """Read `document` and compile it into a deterministic fee schedule.

    A `ContractError` from compilation is not caught. A rate card whose
    clauses overlap or leave a gap has no single correct reading, and that
    must stop an audit rather than quietly produce plausible numbers.
    """
    parse = parse_rate_card(document, provider, model_hint=model_hint)
    contract = CompiledContract.from_parse(
        parse, rounding=rounding, source_sha256=sha256_of(document)
    )
    logger.info(
        "contract_compiled",
        contract_version=contract.version_id,
        rounding=rounding.value,
        rule_count=len(contract.rules),
        uncovered_methods=sorted(m.value for m in contract.uncovered_methods),
    )
    return contract
