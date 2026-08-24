"""Compiled, effective-dated fee schedule.

Two things live here, deliberately in one file.

The first is the **wire schema** an LLM must fill in when it reads an
unstructured rate card (`RateCardParse` and friends). It lives under
`core/` rather than under `llm/` because the dependency has to point this
way: `llm/contract_parser.py` imports this schema and hands it to the
provider for constrained decoding. The model is shaped by our domain; our
domain is not shaped around the model. Its enums are `core.models`' own
`PaymentMethod` / `Network` / `CardType` / `FeeType`, which is what makes
"a clause naming a network we do not recognise rejects the whole parse" a
property of validation rather than a hand-written check.

The second is the **engine** (`CompiledContract`). It is pure and
deterministic and knows nothing about how the schedule was produced. It
refuses ambiguity rather than resolving it: two clauses that could both
price the same transaction on the same date are a hard error, and a
transaction no clause covers raises rather than acquiring an invented fee.

Arithmetic is integer-only. Invariant 1 confines `Decimal` to the ingest
boundary and `core/money.py` already spends that allowance in
`from_rupees`, so basis-point maths here is done on ints with an explicit
rounding mode -- see `apply_bps`.

Conventions, applied without exception:
  * Every interval is half-open. `[amount_min_paise, amount_max_paise)`
    and `[effective_from, effective_to)`. `None` means unbounded. This is
    what makes "exactly midnight on the revision date" answerable.
  * Effective dating is resolved on the IST calendar date of the instant.
  * `cap_paise` caps the ad-valorem component only; a fixed fee is added
    on top, uncapped.
  * `mcc_pattern` is a prefix glob (digits, at most one trailing `*`),
    never a regex -- regex intersection is undecidable, so overlap
    detection could not then be exact.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from core.models import (
    IST,
    AssayModel,
    CardType,
    FeeType,
    ISTDatetime,
    Network,
    PaisaAmount,
    Payment,
    PaymentMethod,
)
from core.money import Money

_BPS_DENOMINATOR = 10_000
_RULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MCC_PATTERN_RE = re.compile(r"^\d+\*?$")


# ---------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------


class ContractError(Exception):
    """Base for every way a rate card can fail to become a usable schedule."""


class OverlappingRules(ContractError):
    """Two clauses could both price the same transaction on the same date.

    Never resolved by precedence, ordering or specificity -- reported, with
    both clauses quoted, so a human decides which one the contract meant.
    """


class ContractIncomplete(ContractError):
    """A gap in amount or date coverage for a method the card does price."""


class InvalidSupersession(ContractError):
    """A `supersedes` link that does not describe a clean hand-over."""


class NoApplicableRule(ContractError):
    """No clause covers this transaction. A reported exception, never a
    reason to guess a fee."""


class ContractNotSignedOff(ContractError):
    """The schedule has no human sign-off, or has changed since it got one."""


# ---------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------


class TaxBase(StrEnum):
    """Which base a tax clause multiplies.

    GST is always `FEE_AMOUNT`. `TRANSACTION_GROSS` exists so a card that
    genuinely says gross can be represented faithfully -- and so that
    "tax charged on the wrong base" is something the contract language can
    express, not merely something the verifier detects.
    """

    FEE_AMOUNT = "fee_amount"
    TRANSACTION_GROSS = "transaction_gross"


class RoundingMode(StrEnum):
    """How a basis-point product is reduced to whole paise.

    Never parsed from the rate card. A model choosing this would be a model
    choosing an amount, which invariant 2 forbids; it comes from config,
    defaults to HALF_UP, and is recorded in the contract version.
    """

    HALF_UP = "half_up"
    HALF_EVEN = "half_even"
    DOWN = "down"
    UP = "up"


def apply_bps(amount_paise: int, rate_bps: int, mode: RoundingMode) -> int:
    """`rate_bps` basis points of `amount_paise`, in whole paise.

    Pure integer arithmetic: no float, no Decimal, no division operator.
    Agrees exactly with a Decimal `quantize` at these magnitudes, which is
    what lets this engine and datagen's independent calculator be compared.
    """
    if rate_bps < 0:
        raise ValueError(f"rate_bps must be non-negative, got {rate_bps}")

    negative = amount_paise < 0
    whole, remainder = divmod(abs(amount_paise) * rate_bps, _BPS_DENOMINATOR)

    if remainder:
        twice = remainder * 2
        if mode is RoundingMode.HALF_UP:
            if twice >= _BPS_DENOMINATOR:
                whole += 1
        elif mode is RoundingMode.HALF_EVEN:
            if twice > _BPS_DENOMINATOR or (twice == _BPS_DENOMINATOR and whole % 2):
                whole += 1
        elif mode is RoundingMode.UP:
            whole += 1
        elif mode is RoundingMode.DOWN:
            pass
        else:  # pragma: no cover - StrEnum is exhaustive above
            raise ValueError(f"unhandled rounding mode {mode!r}")

    return -whole if negative else whole


# ---------------------------------------------------------------------
# the wire schema -- what the LLM fills in
# ---------------------------------------------------------------------


class AppliesWhen(AssayModel):
    """The predicate that decides whether a clause prices a transaction.

    Every field defaults to "any". Over-broad predicates are not dangerous
    here because two clauses that can both match are rejected outright.
    """

    method: PaymentMethod | None = None
    network: Network | None = None
    card_type: CardType | None = None
    is_international: bool | None = None
    mcc_pattern: str | None = None
    amount_min_paise: int = Field(default=0, ge=0)
    amount_max_paise: int | None = Field(default=None, ge=0)

    @field_validator("mcc_pattern")
    @classmethod
    def _prefix_glob_only(cls, value: str | None) -> str | None:
        if value is not None and not _MCC_PATTERN_RE.match(value):
            raise ValueError(
                f"mcc_pattern {value!r} must be digits with at most one trailing '*' (e.g. '5411' or '54*')"
            )
        return value

    @model_validator(mode="after")
    def _band_is_ordered(self) -> AppliesWhen:
        if self.amount_max_paise is not None and self.amount_max_paise <= self.amount_min_paise:
            raise ValueError(
                f"amount band [{self.amount_min_paise}, {self.amount_max_paise}) is empty or inverted"
            )
        return self


class TaxTreatment(AssayModel):
    """A tax clause, naming the base it applies to."""

    tax_type: str = Field(min_length=1)
    rate_bps: int = Field(ge=0)
    base: TaxBase


class FeeRule(AssayModel):
    """One priced clause of the rate card.

    A single clause can emit two fee components: `rate_bps` produces one
    typed `fee_type`, and `fixed_fee_paise` produces one typed
    `FeeType.FIXED`. That is how "1.75% + Rs.10" stays one clause in the
    document and becomes two lines in the ledger.
    """

    rule_id: str
    fee_type: FeeType
    effective_from: date
    effective_to: date | None = None
    supersedes: str | None = None
    applies_when: AppliesWhen
    rate_bps: int = Field(default=0, ge=0)
    fixed_fee_paise: int = Field(default=0, ge=0)
    cap_paise: int | None = Field(default=None, ge=0)
    taxes: list[TaxTreatment]
    source_quote: str

    @field_validator("rule_id", "supersedes")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        if value is not None and not _RULE_ID_RE.match(value):
            raise ValueError(f"rule id {value!r} must match {_RULE_ID_RE.pattern}")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> FeeRule:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError(
                f"rule {self.rule_id}: effective range "
                f"[{self.effective_from}, {self.effective_to}) is empty or inverted"
            )
        if self.cap_paise is not None and self.rate_bps == 0:
            raise ValueError(
                f"rule {self.rule_id}: cap_paise is set but rate_bps is 0, so the cap can never bind"
            )
        if self.supersedes == self.rule_id:
            raise ValueError(f"rule {self.rule_id}: cannot supersede itself")
        return self

    def emitted_fee_types(self) -> frozenset[FeeType]:
        """The component types this clause can produce. Overlap is scoped to
        these: a surcharge may legally stack on the MDR clause it sits
        beside, because they emit different types."""
        types: set[FeeType] = set()
        if self.rate_bps > 0:
            types.add(self.fee_type)
        if self.fixed_fee_paise > 0:
            types.add(FeeType.FIXED)
        return frozenset(types)


class DormantClause(AssayModel):
    """A clause the card states and then disclaims -- the TDS 194-O
    paragraph, typically.

    Captured so the round-trip render can say "we read this and it does not
    apply", rather than the model silently dropping it and nobody noticing
    the contract had a tax clause in it at all.
    """

    label: str
    stated_rate_bps: int | None = None
    reason_not_applicable: str
    source_quote: str


class RateCardParse(AssayModel):
    """The complete structured reading of one rate card document.

    This is the class handed to `LLMProvider.generate_structured`, so its
    JSON schema is what constrains decoding.
    """

    merchant_id: str
    currency: str = "INR"
    rules: list[FeeRule]
    dormant_clauses: list[DormantClause] = Field(default_factory=list)
    rounding_note: str | None = None
    """Any rounding language found in the document, as prose. Recorded for
    a human to read. Never consulted by the engine."""
    parser_notes: str = ""

    @model_validator(mode="after")
    def _references_resolve(self) -> RateCardParse:
        seen: set[str] = set()
        for r in self.rules:
            if r.rule_id in seen:
                raise ValueError(f"duplicate rule_id {r.rule_id!r}")
            seen.add(r.rule_id)
        for r in self.rules:
            if r.supersedes is not None and r.supersedes not in seen:
                raise ValueError(f"rule {r.rule_id!r} supersedes {r.supersedes!r}, which does not exist")
        return self


# ---------------------------------------------------------------------
# what fee_for returns
# ---------------------------------------------------------------------


class FeeComponent(AssayModel):
    """One computed fee line, carrying its own derivation.

    `uncapped_amount` and `cap_applied` are not decoration: a cap that was
    contractually due but not applied is a discrepancy class of its own,
    and it can only be named if the engine says what the cap would have
    done.
    """

    fee_type: FeeType
    amount: PaisaAmount
    rule_id: str
    rate_bps: int
    base_amount: PaisaAmount
    uncapped_amount: PaisaAmount
    cap_paise: int | None
    cap_applied: bool


class TaxComponent(AssayModel):
    tax_type: str
    rate_bps: int
    base: TaxBase
    base_amount: PaisaAmount
    amount: PaisaAmount
    rule_id: str
    on_fee_type: FeeType | None
    """The fee line this tax was levied on, or None for a tax levied on the
    transaction gross -- which belongs to the transaction, not to any one
    fee line."""


class FeeBreakdown(AssayModel):
    """Every paisa of fee and tax for one transaction, each traced to the
    clause that produced it."""

    payment_id: str
    at: ISTDatetime
    gross_amount: PaisaAmount
    contract_version: str
    rounding: RoundingMode
    matched_rule_ids: list[str]
    fees: list[FeeComponent]
    taxes: list[TaxComponent]

    @property
    def total_fee(self) -> Money:
        total = Money.zero(self.gross_amount.currency)
        for component in self.fees:
            total = total + component.amount
        return total

    @property
    def total_tax(self) -> Money:
        total = Money.zero(self.gross_amount.currency)
        for component in self.taxes:
            total = total + component.amount
        return total


class ContractSignOff(AssayModel):
    """A human's one-time confirmation that the compiled schedule reads the
    way the source document reads.

    Keyed by the schedule hash, so a replay of an unchanged contract finds
    the existing sign-off and never re-prompts -- determinism holds.
    """

    schedule_sha256: str
    rendered_sha256: str
    source_sha256: str
    signed_off_by: str
    signed_off_at: ISTDatetime


class ContractDocument(AssayModel):
    """The on-disk form of a compiled contract."""

    schema_version: int = 1
    rounding: RoundingMode
    source_sha256: str = ""
    parse: RateCardParse


# ---------------------------------------------------------------------
# predicate algebra -- used by both matching and overlap detection
# ---------------------------------------------------------------------


def _mcc_matches(pattern: str, mcc: str) -> bool:
    if pattern.endswith("*"):
        return mcc.startswith(pattern[:-1])
    return mcc == pattern


def _scalars_intersect(a: object | None, b: object | None) -> bool:
    return a is None or b is None or a == b


def _mcc_patterns_intersect(a: str | None, b: str | None) -> bool:
    """Two prefix globs can both match some MCC iff one prefix contains the
    other. Decidable precisely because the patterns are not regexes."""
    if a is None or b is None:
        return True
    a_prefix, a_glob = (a[:-1], True) if a.endswith("*") else (a, False)
    b_prefix, b_glob = (b[:-1], True) if b.endswith("*") else (b, False)
    if a_glob and b_glob:
        return a_prefix.startswith(b_prefix) or b_prefix.startswith(a_prefix)
    if a_glob:
        return b_prefix.startswith(a_prefix)
    if b_glob:
        return a_prefix.startswith(b_prefix)
    return a_prefix == b_prefix


def _bands_intersect(a_min: int, a_max: int | None, b_min: int, b_max: int | None) -> bool:
    low = max(a_min, b_min)
    if a_max is None:
        high = b_max
    elif b_max is None:
        high = a_max
    else:
        high = min(a_max, b_max)
    return high is None or low < high


def _dates_intersect(a_from: date, a_to: date | None, b_from: date, b_to: date | None) -> bool:
    low = max(a_from, b_from)
    if a_to is None:
        high = b_to
    elif b_to is None:
        high = a_to
    else:
        high = min(a_to, b_to)
    return high is None or low < high


def _predicates_intersect(a: AppliesWhen, b: AppliesWhen) -> bool:
    return (
        _scalars_intersect(a.method, b.method)
        and _scalars_intersect(a.network, b.network)
        and _scalars_intersect(a.card_type, b.card_type)
        and _scalars_intersect(a.is_international, b.is_international)
        and _mcc_patterns_intersect(a.mcc_pattern, b.mcc_pattern)
        and _bands_intersect(
            a.amount_min_paise, a.amount_max_paise, b.amount_min_paise, b.amount_max_paise
        )
    )


def _rule_applies(rule: FeeRule, payment: Payment, on: date) -> bool:
    if on < rule.effective_from:
        return False
    if rule.effective_to is not None and on >= rule.effective_to:
        return False

    where = rule.applies_when
    if where.method is not None and where.method != payment.method:
        return False
    if where.network is not None and where.network != payment.network:
        return False
    if where.card_type is not None and where.card_type != payment.card_type:
        return False
    if where.is_international is not None and where.is_international != payment.is_international:
        return False
    if where.mcc_pattern is not None and not _mcc_matches(where.mcc_pattern, payment.mcc):
        return False

    gross = payment.amount.paise
    if gross < where.amount_min_paise:
        return False
    return where.amount_max_paise is None or gross < where.amount_max_paise


# ---------------------------------------------------------------------
# compile-time validation
# ---------------------------------------------------------------------


def _validate_supersession(rules: list[FeeRule]) -> None:
    by_id = {r.rule_id: r for r in rules}
    for rule in rules:
        if rule.supersedes is None:
            continue
        prior = by_id[rule.supersedes]
        if prior.effective_to != rule.effective_from:
            raise InvalidSupersession(
                f"rule {rule.rule_id!r} supersedes {prior.rule_id!r} from {rule.effective_from}, "
                f"but {prior.rule_id!r} runs to {prior.effective_to} -- the hand-over is not contiguous"
            )
        if prior.applies_when != rule.applies_when:
            raise InvalidSupersession(
                f"rule {rule.rule_id!r} supersedes {prior.rule_id!r} but prices a different set of "
                f"transactions; a revision must keep applies_when identical"
            )
        if prior.fee_type != rule.fee_type:
            raise InvalidSupersession(
                f"rule {rule.rule_id!r} supersedes {prior.rule_id!r} but changes fee_type from "
                f"{prior.fee_type} to {rule.fee_type}"
            )


def _validate_no_overlap(rules: list[FeeRule]) -> None:
    ordered = sorted(rules, key=lambda r: r.rule_id)
    for i, left in enumerate(ordered):
        for right in ordered[i + 1 :]:
            if not (left.emitted_fee_types() & right.emitted_fee_types()):
                continue
            if not _dates_intersect(
                left.effective_from, left.effective_to, right.effective_from, right.effective_to
            ):
                continue
            if not _predicates_intersect(left.applies_when, right.applies_when):
                continue
            shared = sorted(t.value for t in left.emitted_fee_types() & right.emitted_fee_types())
            raise OverlappingRules(
                f"rules {left.rule_id!r} and {right.rule_id!r} both produce {', '.join(shared)} "
                f"for the same transaction on the same date. "
                f"{left.rule_id}: {left.source_quote!r}. {right.rule_id}: {right.source_quote!r}. "
                f"Two clauses cannot price one transaction -- resolve the rate card, do not guess."
            )


def _first_amount_gap(rules: list[FeeRule]) -> tuple[int, int | None] | None:
    covered_to = 0
    for rule in sorted(rules, key=lambda r: (r.applies_when.amount_min_paise, r.rule_id)):
        where = rule.applies_when
        if where.amount_min_paise > covered_to:
            return (covered_to, where.amount_min_paise)
        if where.amount_max_paise is None:
            return None
        covered_to = max(covered_to, where.amount_max_paise)
    return (covered_to, None)


def _validate_completeness(rules: list[FeeRule]) -> None:
    """Base pricing must be gapless; surcharges are conditional by nature.

    Only `FeeType.MDR` clauses carry a coverage obligation, and only on the
    amount and date axes, for each (method, card_type) the card prices. A
    gap on a narrower axis -- an MCC-scoped or network-scoped clause --
    surfaces at audit time as `NoApplicableRule`, which is a reported
    exception rather than an invented fee.
    """
    groups: dict[tuple[PaymentMethod | None, CardType | None], list[FeeRule]] = defaultdict(list)
    for rule in rules:
        if rule.fee_type is FeeType.MDR:
            groups[(rule.applies_when.method, rule.applies_when.card_type)].append(rule)

    for key in sorted(groups, key=lambda k: (k[0] or "", k[1] or "")):
        group = groups[key]
        scope = f"{key[0] or 'any method'}/{key[1] or 'any card type'}"
        boundaries = sorted({r.effective_from for r in group} | {r.effective_to for r in group if r.effective_to})

        for start in boundaries:
            active = [
                r
                for r in group
                if r.effective_from <= start and (r.effective_to is None or r.effective_to > start)
            ]
            if not active:
                raise ContractIncomplete(
                    f"{scope}: no clause is in force from {start} -- the schedule has a date gap"
                )
            gap = _first_amount_gap(active)
            if gap is not None:
                low, high = gap
                shown = high if high is not None else "unbounded"
                raise ContractIncomplete(
                    f"{scope} on {start}: no clause covers the amount band [{low}, {shown}) in paise"
                )


# ---------------------------------------------------------------------
# the compiled contract
# ---------------------------------------------------------------------


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CompiledContract:
    """A validated, effective-dated fee schedule.

    Constructing one is the validation: supersession links, then overlap,
    then completeness. Any of the three failing means no contract exists,
    which is the intended outcome -- an ambiguous rate card should stop an
    audit, not quietly produce plausible numbers.
    """

    def __init__(
        self,
        parse: RateCardParse,
        *,
        rounding: RoundingMode = RoundingMode.HALF_UP,
        source_sha256: str = "",
    ):
        rules = sorted(parse.rules, key=lambda r: r.rule_id)
        _validate_supersession(rules)
        _validate_no_overlap(rules)
        _validate_completeness(rules)

        self._parse = parse
        self._rules = rules
        self._rounding = rounding
        self._source_sha256 = source_sha256
        self._canonical_json = _canonical(
            {
                "rounding": rounding.value,
                "merchant_id": parse.merchant_id,
                "currency": parse.currency,
                "rules": [r.model_dump(mode="json") for r in rules],
                "dormant_clauses": [
                    c.model_dump(mode="json")
                    for c in sorted(parse.dormant_clauses, key=lambda c: (c.label, c.source_quote))
                ],
            }
        )
        self._schedule_sha256 = sha256_of(self._canonical_json)

    # -- construction --------------------------------------------------

    @classmethod
    def from_parse(
        cls,
        parse: RateCardParse,
        *,
        rounding: RoundingMode = RoundingMode.HALF_UP,
        source_sha256: str = "",
    ) -> CompiledContract:
        return cls(parse, rounding=rounding, source_sha256=source_sha256)

    @classmethod
    def load(cls, path: Path | str) -> CompiledContract:
        document = ContractDocument.model_validate_json(Path(path).read_text(encoding="utf-8"))
        return cls(document.parse, rounding=document.rounding, source_sha256=document.source_sha256)

    def save(self, path: Path | str) -> None:
        document = ContractDocument(
            rounding=self._rounding, source_sha256=self._source_sha256, parse=self._parse
        )
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document.model_dump_json(indent=2), encoding="utf-8")

    # -- identity ------------------------------------------------------

    def canonical_json(self) -> str:
        """The bytes the version id hashes: everything that changes an
        amount, and nothing that does not. Prose fields are excluded."""
        return self._canonical_json

    @property
    def schedule_sha256(self) -> str:
        return self._schedule_sha256

    @property
    def version_id(self) -> str:
        return f"rc-{self._schedule_sha256[:12]}"

    @property
    def source_sha256(self) -> str:
        """Provenance of the document this was parsed from. Deliberately not
        part of the version id: the version identifies the arithmetic, not
        the input's byte formatting."""
        return self._source_sha256

    @property
    def rounding(self) -> RoundingMode:
        return self._rounding

    @property
    def rules(self) -> list[FeeRule]:
        return list(self._rules)

    @property
    def dormant_clauses(self) -> list[DormantClause]:
        return list(self._parse.dormant_clauses)

    @property
    def merchant_id(self) -> str:
        return self._parse.merchant_id

    @property
    def uncovered_methods(self) -> set[PaymentMethod]:
        """Methods in our taxonomy that this card does not price at all.

        Reported rather than filled in. A transaction using one of these
        raises `NoApplicableRule` and becomes an exception in the audit.
        """
        return {
            method
            for method in PaymentMethod
            if not any(r.applies_when.method in (None, method) for r in self._rules)
        }

    @property
    def uncovered_card_types(self) -> set[CardType]:
        """Card types no clause prices, when the card method itself is priced.

        `uncovered_methods` looks at method alone, so it calls CARD covered
        the moment any card clause exists -- true, and yet a prepaid card
        may still have no clause. Reporting only the method would tell a
        human at sign-off that everything is priced while prepaid
        transactions raise at audit time.
        """
        if PaymentMethod.CARD in self.uncovered_methods:
            return set()
        return {
            card_type
            for card_type in CardType
            if not any(
                r.applies_when.method in (None, PaymentMethod.CARD)
                and r.applies_when.card_type in (None, card_type)
                # A clause scoped to international transactions cannot be
                # what prices an ordinary domestic card. Counting it would
                # make a card type look priced when only its surcharge is.
                and r.applies_when.is_international is not True
                for r in self._rules
            )
        }

    # -- the actual job ------------------------------------------------

    def fee_for(self, payment: Payment, at: datetime) -> FeeBreakdown:
        """Every fee and tax due on `payment` under the clauses in force at
        `at`, each traced to the clause that produced it."""
        if at.tzinfo is None:
            raise ValueError("fee_for requires a timezone-aware datetime; a naive one is ambiguous")
        on = at.astimezone(IST).date()

        matched = [r for r in self._rules if _rule_applies(r, payment, on)]
        if not matched:
            raise NoApplicableRule(
                f"no clause in contract {self.version_id} prices payment {payment.id} "
                f"(method={payment.method.value}, card_type={payment.card_type or 'n/a'}, "
                f"gross={payment.amount.to_rupees_str()}) on {on.isoformat()}"
            )

        fees: list[FeeComponent] = []
        taxes: list[TaxComponent] = []
        for rule in matched:
            components = self._components_for(rule, payment)
            fees.extend(components)
            for component in components:
                taxes.extend(self._fee_taxes_for(rule, component, payment))
            taxes.extend(self._gross_taxes_for(rule, payment))

        return FeeBreakdown(
            payment_id=payment.id,
            at=at,
            gross_amount=payment.amount,
            contract_version=self.version_id,
            rounding=self._rounding,
            matched_rule_ids=[r.rule_id for r in matched],
            fees=fees,
            taxes=taxes,
        )

    def _components_for(self, rule: FeeRule, payment: Payment) -> list[FeeComponent]:
        currency = payment.amount.currency
        components: list[FeeComponent] = []

        if rule.rate_bps > 0:
            uncapped = apply_bps(payment.amount.paise, rule.rate_bps, self._rounding)
            charged = uncapped
            cap_applied = False
            if rule.cap_paise is not None and uncapped > rule.cap_paise:
                charged = rule.cap_paise
                cap_applied = True
            # A cap that bit is evidence even when it zeroed the fee: a
            # verifier needs a component to compare a wrongly-charged
            # settlement line against. A fee that merely rounds to nothing
            # is a different case and still emits nothing, matching
            # datagen's `if mdr > 0`.
            if charged != 0 or cap_applied:
                components.append(
                    FeeComponent(
                        fee_type=rule.fee_type,
                        amount=Money(charged, currency),
                        rule_id=rule.rule_id,
                        rate_bps=rule.rate_bps,
                        base_amount=payment.amount,
                        uncapped_amount=Money(uncapped, currency),
                        cap_paise=rule.cap_paise,
                        cap_applied=cap_applied,
                    )
                )

        if rule.fixed_fee_paise > 0:
            fixed = Money(rule.fixed_fee_paise, currency)
            components.append(
                FeeComponent(
                    fee_type=FeeType.FIXED,
                    amount=fixed,
                    rule_id=rule.rule_id,
                    rate_bps=0,
                    base_amount=Money.zero(currency),
                    uncapped_amount=fixed,
                    cap_paise=None,
                    cap_applied=False,
                )
            )

        return components

    def _fee_taxes_for(
        self, rule: FeeRule, component: FeeComponent, payment: Payment
    ) -> list[TaxComponent]:
        """Taxes based on the fee line, computed once per component.

        Genuinely per-line: each fee this clause produced is a distinct
        taxable amount, which is how GST works and what datagen does.
        """
        currency = payment.amount.currency
        return [
            TaxComponent(
                tax_type=treatment.tax_type,
                rate_bps=treatment.rate_bps,
                base=treatment.base,
                base_amount=component.amount,
                amount=Money(
                    apply_bps(component.amount.paise, treatment.rate_bps, self._rounding), currency
                ),
                rule_id=rule.rule_id,
                on_fee_type=component.fee_type,
            )
            for treatment in rule.taxes
            if treatment.base is TaxBase.FEE_AMOUNT
        ]

    def _gross_taxes_for(self, rule: FeeRule, payment: Payment) -> list[TaxComponent]:
        """Taxes based on the transaction gross, computed once per clause.

        The gross belongs to the transaction, not to a fee line. Computing
        these per component would bill the identical base once for every
        line a clause emits -- so "1.75% + Rs.10 with TDS on gross", one
        clause producing two fee lines, would charge the TDS twice.
        """
        currency = payment.amount.currency
        return [
            TaxComponent(
                tax_type=treatment.tax_type,
                rate_bps=treatment.rate_bps,
                base=treatment.base,
                base_amount=payment.amount,
                amount=Money(
                    apply_bps(payment.amount.paise, treatment.rate_bps, self._rounding), currency
                ),
                rule_id=rule.rule_id,
                on_fee_type=None,
            )
            for treatment in rule.taxes
            if treatment.base is TaxBase.TRANSACTION_GROSS
        ]


# ---------------------------------------------------------------------
# round-trip render and sign-off
# ---------------------------------------------------------------------


def _describe_scope(where: AppliesWhen) -> str:
    parts = [
        f"method={where.method.value}" if where.method else "method=any",
    ]
    if where.card_type:
        parts.append(f"card_type={where.card_type.value}")
    if where.network:
        parts.append(f"network={where.network.value}")
    if where.is_international is not None:
        parts.append(f"international={'yes' if where.is_international else 'no'}")
    if where.mcc_pattern:
        parts.append(f"mcc={where.mcc_pattern}")
    high = where.amount_max_paise
    parts.append(f"amount=[{where.amount_min_paise}, {high if high is not None else 'unbounded'}) paise")
    return ", ".join(parts)


def render_schedule(contract: CompiledContract) -> str:
    """Re-render the compiled schedule as prose, for the one-time human
    sign-off.

    This is the round trip that matters: a person reads this next to the
    original document and confirms they say the same thing. Everything the
    engine will actually do is visible here, including the rounding mode
    and the clauses that were read and found not to apply.
    """
    lines = [
        f"Contract {contract.version_id} - merchant {contract.merchant_id}",
        f"Rounding: {contract.rounding.value} (at the paisa)",
        f"Schedule SHA-256: {contract.schedule_sha256}",
        "",
        f"{len(contract.rules)} priced clause(s):",
    ]

    for rule in contract.rules:
        until = rule.effective_to.isoformat() if rule.effective_to else "open-ended"
        lines.append("")
        lines.append(f"  [{rule.rule_id}] {rule.fee_type.value}")
        lines.append(f"    in force : {rule.effective_from.isoformat()} up to (not including) {until}")
        lines.append(f"    applies  : {_describe_scope(rule.applies_when)}")

        charges = []
        if rule.rate_bps:
            charge = f"{rule.rate_bps} bps of gross"
            if rule.cap_paise is not None:
                charge += f", capped at {rule.cap_paise} paise (the ad-valorem part only)"
            charges.append(charge)
        if rule.fixed_fee_paise:
            charges.append(f"{rule.fixed_fee_paise} paise fixed, per transaction")
        lines.append(f"    charges  : {'; '.join(charges) if charges else 'nil'}")

        for treatment in rule.taxes:
            lines.append(
                f"    tax      : {treatment.tax_type} @ {treatment.rate_bps} bps on {treatment.base.value}"
            )
        if rule.supersedes:
            lines.append(f"    replaces : {rule.supersedes}")
        lines.append(f"    source   : {rule.source_quote}")

    if contract.dormant_clauses:
        lines.append("")
        lines.append("Declared but not applicable:")
        for clause in contract.dormant_clauses:
            rate = f"{clause.stated_rate_bps} bps" if clause.stated_rate_bps is not None else "unstated rate"
            lines.append(f"  [{clause.label}] {rate} - {clause.reason_not_applicable}")
            lines.append(f"    source   : {clause.source_quote}")

    unpriced = sorted(m.value for m in contract.uncovered_methods)
    unpriced += sorted(f"card/{c.value}" for c in contract.uncovered_card_types)
    lines.append("")
    lines.append(f"Not priced by this card: {', '.join(unpriced) if unpriced else 'nothing'}")
    lines.append("Transactions matching none of the clauses above are reported as exceptions,")
    lines.append("never given a fee.")
    lines.append("")
    return "\n".join(lines)


def signoff_path(contract: CompiledContract, directory: Path | str) -> Path:
    return Path(directory) / f"{contract.version_id}.signoff.json"


def record_signoff(
    contract: CompiledContract,
    directory: Path | str,
    *,
    signed_off_by: str,
    signed_off_at: datetime,
) -> ContractSignOff:
    """Store a human's confirmation of this exact schedule.

    Deliberately non-interactive: `core/` never prompts. A caller shows
    `render_schedule` to a person, gets their confirmation, and calls this.
    """
    signoff = ContractSignOff(
        schedule_sha256=contract.schedule_sha256,
        rendered_sha256=sha256_of(render_schedule(contract)),
        source_sha256=contract.source_sha256,
        signed_off_by=signed_off_by,
        signed_off_at=signed_off_at,
    )
    target = signoff_path(contract, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(signoff.model_dump_json(indent=2), encoding="utf-8")
    return signoff


def load_signoff(contract: CompiledContract, directory: Path | str) -> ContractSignOff | None:
    """The sign-off for this exact schedule, or None.

    Because the lookup is keyed by the schedule hash, replaying an
    unchanged contract finds the existing record and never re-prompts.
    """
    path = signoff_path(contract, directory)
    if not path.exists():
        return None
    signoff = ContractSignOff.model_validate_json(path.read_text(encoding="utf-8"))
    if signoff.schedule_sha256 != contract.schedule_sha256:
        return None
    if signoff.rendered_sha256 != sha256_of(render_schedule(contract)):
        return None
    return signoff


def require_signoff(contract: CompiledContract, directory: Path | str) -> ContractSignOff:
    signoff = load_signoff(contract, directory)
    if signoff is None:
        raise ContractNotSignedOff(
            f"contract {contract.version_id} has no valid human sign-off in {directory}. "
            f"A rate card must be confirmed once by a person before any audit uses its arithmetic."
        )
    return signoff
