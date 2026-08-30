# Engineering decisions

Real entries, real timestamps. Not a changelog — the reasoning behind choices
that weren't obvious from the code.

## 2026-08-23

**Built:** project skeleton (`core/`, `llm/`, `datagen/`, `eval/`, `chaos/`,
`cli/`, with docstring-only stubs where no logic was asked for yet);
`core/money.py`, written test-first; `tests/test_architecture.py`;
`llm/provider.py` plus `llm/providers/{gemini,cached,null}.py`;
`pyproject.toml`, `Makefile`; fixes to `.gitignore` and
`.claude/settings.json`. 101 tests passing, `guard_core.py` and `ruff` both
clean on everything written today.

**Money is integer paise, never float.** Alternative rejected: `Decimal`
throughout `core/` — rejected because CLAUDE.md requires `int` paise
specifically, and `Decimal` keeps float-adjacent footguns (precision
context, silent coercion) that `Money`'s entire job is to remove. `Decimal`
is permitted only at the parse boundary (`Money.from_rupees`) and must
convert to `Money` before leaving the parser. Enforced by
`scripts/guard_core.py` (fast, targeted) and `tests/test_architecture.py`
(blanket ban on any float literal or `float()` call anywhere under `core/`).

**`split_proportionally` takes `weights: Sequence[int]`, not
`Sequence[Money]`.** Alternative rejected: `Sequence[Money]` — rejected
because it couples the split algorithm to the `Money` type for no benefit; a
caller splitting a fee proportional to transaction amounts already has
`.paise` on hand and just passes `[t.paise for t in transactions]`.

**Added `hypothesis` (dev dependency).** Alternative rejected: a hand-rolled
loop over stdlib `random` seeds — rejected because it can't shrink failing
cases or search the input space the way `hypothesis` does, and the task
explicitly asked for a property test on `split_proportionally`. Not in
CLAUDE.md's dependency list — asked first, confirmed.

**Added `python-dotenv` (dependency).** Alternative rejected: a hand-rolled
~10-line `.env` parser — rejected in favor of the standard, better-tested
library; correctness of secret-loading isn't where to save one dependency.
Not in CLAUDE.md's dependency list — asked first, confirmed.

**Added `ruff` (dev dependency).** Alternatives rejected: `flake8` (more
setup for equivalent coverage) and stubbing the `lint` target out entirely
(defers a decision the Makefile already needed made). Not in CLAUDE.md's
dependency list — asked first, confirmed.

**Removed `.llm_cache/` from `.gitignore`.** Alternative rejected: move the
committed cache to a new path (e.g. `llm/providers/cache/`) and leave
`.llm_cache/` ignored — rejected to avoid a second cache-shaped directory
when `.llm_cache/` was already the name `CachedProvider`'s default pointed
at. The ignored `.llm_cache/` directly contradicted the invariant that the
cache "is committed to the repo so `make demo` runs with no API key." Asked
first, confirmed.

**`llm/providers/gemini.py` uses the Interactions API
(`client.interactions.create(...)`), not `client.models.generate_content(...)`.**
Alternative rejected: the older `generate_content` / `response_schema=`
pattern — rejected because `google-genai` >= 2.3.0's Interactions API is what
the current official docs point callers at; `generate_content` still exists
in the SDK but is the superseded path. Caught mid-session by a correction —
see Incidents. Every Gemini API error — a 429 surviving retries, or any
other API error — degrades to `ProviderUnavailable`, chained from the
original exception, so callers only ever need to catch the one exception
type the `LLMProvider` Protocol documents.

**Skipped `/verify-invariants` today.** It's an empty skill file — no
procedure defined yet. The invariants that apply to today's work (integer
paise, no float in `core/`, no `llm`/`datagen` imports in `core/`, cache
committed) are already covered by `tests/test_architecture.py` and
`tests/test_money.py`; `core/conserve.py` and `core/ledger.py` are still
stubs, so there's no conservation identity or ledger behavior yet to verify.

**Incidents:** none shipped. Two things caught and fixed before commit, not
after:
- Planned `llm/providers/gemini.py` around `client.models.generate_content()`
  with `response_schema=`, based on web research. Mid-session, corrected with
  a working code sample for the current Interactions API
  (`client.interactions.create()`); confirmed against the installed SDK
  (`google-genai==2.19.0`) and the official docs before writing the real
  implementation. No code shipped on the wrong API.
- First version of `test_429_retries_then_succeeds` asserted an exact
  `time.sleep()` call count without disabling the rate limiter's own pacing
  sleep, so it over-counted (4 calls seen, 2 expected). Caught by the test
  itself, before commit; fixed by setting `rpm=0` in the test's `GeminiConfig`
  to isolate backoff sleeps from rate-limiter pacing sleeps.

**Built (later same day):** `core/exceptions.py`'s `DiscrepancyClass`
taxonomy (12 members, reviewed by the user before anything else was
written); `core/models.py` (11 entities, `RecordRef`, `EntityType`, and the
`PaisaAmount`/`ISTDatetime` shared types); `core/ledger.py`'s `Ledger`
index. `tests/test_models.py` (23 tests) and `tests/test_ledger.py` (9
tests), both test-first. 133 tests passing total; `ruff` and
`guard_core.py` clean on everything written today.

**`Money` fields validate via `Annotated[Money, PlainValidator(...),
PlainSerializer(...)]` defined in `models.py`, not by teaching `Money`
itself a Pydantic schema.** Alternative rejected: add
`__get_pydantic_core_schema__` to `core/money.py` — rejected to avoid
touching an already-implemented, already-tested file for a need
`models.py` alone can satisfy. `PlainValidator` matters specifically
because it replaces Pydantic's schema for the field entirely — Pydantic's
own `int` coercion never gets a look at `paise`/`currency` (which could
silently accept a `bool` where `Money.__post_init__` rejects one);
validation is 100% delegated to `Money`.

**A naive (tz-less) datetime is rejected, never assumed to be IST.**
Alternative rejected: localize naive datetimes to IST on the assumption
that's what an ingest source without explicit tz info means — rejected as
an invented business rule with no ground to stand on; silently guessing a
timezone is exactly what invariant 4 (determinism) and the "don't invent a
business rule" working agreement rule out. Not asked, so not assumed —
callers must pass tz-aware datetimes.

**`RecordRef` is its own frozen `AssayModel`, not a tuple or a
`Money`-style dataclass.** Alternative rejected: plain `(EntityType, str)`
tuple — works as a dict key too, but doesn't nest into another Pydantic
model (`Finding.evidence_ids: list[RecordRef]`) without a custom
validator, and loses `.model_dump()` for free in the report JSON.

**`Ledger` dispatches via each model's `RECORD_TYPE: ClassVar[EntityType]`,
not `isinstance` chains.** Alternative rejected: `isinstance(record,
Payment) -> EntityType.PAYMENT` etc. inside `Ledger.__init__` — rejected
because it grows one branch per entity and silently drifts the moment an
entity type is added and someone forgets the branch; a `ClassVar` lookup
can't drift.

**`Finding`'s taxonomy field is named `discrepancy_class` in Python,
aliased to `"class"` for (de)serialization.** Not a preference — `class` is
a reserved word, so the literal field name from the spec is impossible as a
Python attribute. Alternative rejected: `class_` (trailing underscore) —
rejected as less readable; the alias already gives the exact wire name the
spec asked for, so nothing is lost. Flagged to the user; unresolved — they
may want it renamed everywhere instead of aliased.

**`Finding.confidence` (basis points, 0-10000), `Finding.severity`
(`minor`/`major`/`critical`), and `SettlementBatch.status` (`pending` /
`partially_settled` / `settled` / `short_settled` / `disputed` / `closed`)**
— none of these had values specified. Asked first via three targeted
questions rather than inventing a business rule; status ended up as the
union of the two proposed option sets per the user's answer.

**Ran `/verify-invariants`: 3 of 5 steps executable.** Architecture tests,
`guard_core.py` on changed files, and the full suite all pass. Steps 3-4
(`assay audit --profile clean`, twice, checking `unexplained == 0` and
report-hash determinism) can't run yet — there's no `assay` CLI entry
point, and `cli/`, `decompose.py`, `verify.py`, `conserve.py`, and
`datagen/` are all still stubs. Not a regression from today's work; nothing
to verify there yet.

**Incidents:** nothing shipped broken. Two things caught before commit:
- First versions of `tests/test_models.py` and `tests/test_ledger.py`
  failed `ruff` — `timezone.utc` instead of the `datetime.UTC` alias, an
  unsorted import block, and `dict(...)` calls ruff wants as literals.
  Caught by `make lint`, fixed before anything was committed.
- The `log-incident` skill file (`.claude/skills/log-incident/SKILL.md`)
  was found, while trying to use it, to contain a verbatim copy of the
  unrelated `new-discrepancy-class` skill's content (wrong frontmatter
  `name`, wrong procedure). Not fixed — out of scope for what was asked;
  flagged to the user instead of guessed around.

Also noted but not touched: `.claude/agents/chaos-engineer.md`,
`.claude/agents/payments-domain.md`, and three files under
`.claude/skills/` show pre-existing uncommitted changes from before this
session (including `verify-invariants/SKILL.md` itself, previously an
empty stub). Nothing in this session created or edited them.

## 2026-08-23 19:04 — Finding's taxonomy field name diverged from its serialized key
**Symptom:** `Finding.discrepancy_class` used `Field(alias="class")` plus
`model_config = ConfigDict(populate_by_name=True)`, so the Python attribute
was `discrepancy_class` but the wire/report key was "class" only when
`by_alias=True` was passed.
**Diagnosis:** `model_dump()`/`model_dump_json()` default to field names,
not aliases. Any future call site — the report writer, the LLM citation
schema, `eval/metrics.py` — only has to omit `by_alias=True` once for the
serialized key to silently become "discrepancy_class" instead of "class".
**First fix:** none attempted — flagged to the user as unresolved rather
than patched further.
**Whether it worked:** n/a.
**Final fix:** dropped the alias and `populate_by_name`. The field is
`discrepancy_class` everywhere, Python and JSON both. `class` is a reserved
word, so the literal name from the original spec was never reachable as a
Python attribute regardless.
**Guard added:** none — the divergence this caused is now structurally
impossible, since there is only one name to begin with.

## 2026-08-23 19:04 — ruff failures in the new test files
**Symptom:** `ruff check tests/test_models.py tests/test_ledger.py`
reported UP017 (`timezone.utc` instead of the `datetime.UTC` alias), I001
(unsorted import block), and two C408 (`dict(...)` calls ruff wants
rewritten as literals).
**Diagnosis:** written from habit, not from this repo's ruff config;
none of the four affected test correctness.
**First fix:** switched to the `datetime.UTC` alias, replaced the `dict()`
calls with dict literals, resorted the import block.
**Whether it worked:** yes.
**Final fix:** same as first fix.
**Guard added:** none new — `make lint` already catches this class of
issue; caught before anything was committed.

## 2026-08-23 19:04 — log-incident skill file had the wrong content
**Symptom:** `.claude/skills/log-incident/SKILL.md` contained
`new-discrepancy-class`'s procedure verbatim, including its frontmatter
`name: new-discrepancy-class`.
**Diagnosis:** the file had been overwritten with the wrong content at
some point before this session; unrelated to anything built today.
**First fix:** none attempted — flagged to the user instead of guessing at
the intended procedure.
**Whether it worked:** n/a.
**Final fix:** the user corrected the file directly; this entry follows
its real procedure.
**Guard added:** none — no automated check for skill-file content drift.

## 2026-08-23 22:15 — Build datagen/: synthetic settlement month + D01-D12 injector

**Built:** `datagen/ratecard.py` (tiered card MDR, flat UPI/netbanking/
wallet fees, a per-tier cap, an international surcharge, GST-on-fee, a
dormant TDS clause, an effective-dated mid-month revision, and a
markdown renderer); `datagen/timeline.py` and `datagen/sampling.py`
(calendar/weekly-pattern helpers, seeded log-normal amounts, weighted
method/card-type/network choice); `datagen/world.py` (the true-world
generator: payments, refunds, chargebacks, reserve/manual adjustments,
T+2 batching, narration corruption, and `compute_batch_net_paise` — the
one netting-formula implementation both the true-world assembly and the
injector's post-mutation recompute call into); `datagen/ground_truth.py`
and `InjectionProfile` in `datagen/config.py`; `datagen/inject.py` (all
12 D01-D12 injectors plus silent-corruption mode);
`datagen/{writer,summary,cli}.py`; three YAML profiles. Extended
`tests/test_architecture.py` to dynamically enumerate every top-level
package except `eval/`/`datagen/` (closing a gap where `chaos/` and any
future package went unchecked). Created `truth/` as an empty sibling
directory ahead of any generator code. 247 tests passing, all written
test-first for the money-critical modules (ratecard, world, inject,
silent-corruption) and shown failing before implementation existed.
Committed the canonical `realistic`-profile run at seed 42
(`runs/realistic-seed42/` + `truth/ground_truth.json`).

**D01-D12 reuse the existing `DiscrepancyClass` taxonomy; no new enum
member added.** Alternative rejected: run the `new-discrepancy-class`
skill's 5-step process to add D-code-named members — rejected after
close reading of all 11 existing members' docstrings showed every D-code
maps cleanly onto one already (several near-verbatim: D08's wording
almost identical to `MISSING_TRANSACTION`'s, D02 literally says "wrong
base", D05's `CHARGEBACK_AMOUNT_MISMATCH` docstring names "reversal on a
won dispute" outright). Adding members now would also be premature per
the skill's own instructions — it requires a coordinated
`core/verify.py` detection rule, and `verify.py` is still a stub. D09
(corrupted UTR) is the one class with no home in the taxonomy — every
existing member is money-impact by design — so it's a separate
`DataQualityFlag`, not a `Finding`-shaped entry.

**Two-track generation: a "true" world, then a "reported" world
`inject.py` deep-copies and mutates.** The merchant's ledger facts
(amounts, timestamps) are always true; only `settlement_id` linkage and
fee/tax/batch/bank-credit figures can diverge once a discrepancy is
planted. `datagen` computes its own rate-card arithmetic independently
of `core/contract.py` (still a stub) — depends on `core.models`/
`core.money` only, never the reverse.

**A won chargeback's reversal is a `MANUAL_CREDIT` `Adjustment`, not a
dedicated entity.** `core/models.py` has no "Reversal" record type, and
the conservation identity's `+ reversals` term needs something concrete
to back it. This is the only schema-compatible choice, and it's what
D05 omits to plant that class. Flagged here for whoever builds
`core/conserve.py` for real: it needs to either adopt this convention or
reconcile against it.

**The adjustment sign convention (`RESERVE_HOLD`/`MANUAL_DEBIT` subtract,
`RESERVE_RELEASE`/`MANUAL_CREDIT`/`FEE_WAIVER` add back) is
datagen-internal only.** `core/conserve.py` is still a stub and doesn't
have to agree with it yet — but it will need to, once built for real.

**EMI excluded from generation (0% weight); international payments are
CARD-only, 10% of card volume (~3% of total).** Neither was specified.
The method-mix spec (UPI 55/card 30/netbanking 10/wallet 5) already sums
to 100% without EMI. UPI/netbanking/wallet are domestic-only in practice
in India, so "3% international" only has somewhere to land on cards.

**Debit top-tier MDR cap lowered from Rs.150 to Rs.70 in
`datagen/ratecard.py`.** Found empirically: at Rs.150, the cap only
binds above ~Rs.21,400 gross — a population of 2 out of 5,200 payments
at seed 42, too thin for D03 (cap not applied) to plant into reliably.
Rs.70, close to the tier's own rate at its lower boundary, binds across
most of the tier instead of only rare outliers.

**`realistic.yaml`/`stress.yaml` D03 and D05 counts calibrated to the
empirically observed worst case across seeds 1-50, not the original
estimate.** D03 (debit, cap-binding) ranged 2-9 eligible; D05
(won-chargeback) ranged 0-7. Both profiles now request D03: 2, D05: 1 —
the observed floor. Even there, D05 can still hit a seed with zero won
chargebacks (~2% of seeds tested, e.g. seed 47) — a real property of a
~0.15% chargeback rate over one month, not a generator bug.
`InjectionProfileError` surfaces this explicitly rather than silently
under-delivering, which is the correct behavior; the fix was calibrating
the requested counts, not changing that behavior. Seed 42, used for the
committed demo run, is confirmed clean for both classes.

**Refund/chargeback event days are clamped to the generated month's last
day, not left to spill into a fictitious next month.** Mirrors the
reserve-release precedent already planned. Found necessary only after
running the generator at scale: an unclamped spillover day has zero
underlying payment gross to offset a refund/chargeback deduction,
producing settlement batches with a negative `expected_credit` — 12 of
45 batches, before the fix. After clamping: 31 batches (exactly the
month's day count), zero negative, and the cross-cycle refund rate
actually rose (98.6% vs the unclamped ~86% estimate), since late-month
refunds now reliably land on a different (earlier, in-month) day instead
of trailing off past month-end.

**Added `pyyaml` as a declared dependency.** Already present in the
project's conda env but not in `pyproject.toml`. Asked first between
this and hand-rolling a minimal parser for the profiles' flat `D01: 8`
shape; confirmed pyyaml.

**Committed the canonical `realistic`-profile run instead of gitignoring
all generated output.** Asked first between commit-and-mirror-
`.llm_cache/`'s precedent versus gitignore-and-regenerate-on-demand;
confirmed committing. `runs/` is gitignored in general so ad-hoc
dev/test runs aren't accidentally committed; `runs/realistic-seed42/`
alone is force-added past that ignore.

**`tests/test_architecture.py`'s datagen-quarantine check now enumerates
top-level packages dynamically instead of a hardcoded
`(CORE_DIR, LLM_DIR, CLI_DIR)` tuple.** The hardcoded version had already
let `chaos/` go unchecked; a future new package would have silently done
the same. Verified the mechanism itself, not just today's package list,
with a regression test that builds a throwaway package tree.

**The CLI has one command and is invoked without a subcommand name
(`python -m datagen.cli --profile ... --seed ...`), not `... generate
--profile ...`.** Typer/Click collapses a single-command app to its own
callback — standard behavior, not a bug. Accepted rather than forcing a
named subcommand for a CLI that doesn't need one yet; if a second
command is ever added, Typer requires names automatically again.

**Incidents:** nothing shipped broken. Two things actually broke and were
diagnosed from an observed failure — full entries immediately below.
Two more were latent bugs caught by design/code review before they ever
produced a wrong number, so they're not incidents by this log's own
definition (nothing was observed to fix a symptom of):
- `datagen/inject.py`'s D09 injector corrupts `BankCredit.utr`. The
  first draft of the batch<->bank-credit lookup re-matched by `utr`
  after the fact, which would have broken for any later injector
  recomputing a batch D09 had already touched. Found while designing
  the injector, before any code was written — fixed by resolving the
  pairing once, up front, from the unmutated world. Guard: `test_
  d09_does_not_break_later_injectors_batch_lookup`.
- `_inject_d02`'s eligible-fee-line list is a snapshot taken before its
  loop runs, so a payment with two eligible fee lines (NETBANKING/
  WALLET carry both MDR and FIXED) could receive two separate D02
  entries. Found by inspection while reviewing the D04 fix below, not
  by any failing test. Fixed by re-checking `touch.payments` inside the
  loop. Guard: `test_d02_never_touches_the_same_payment_twice_even_
  with_two_fee_lines`.

## 2026-08-23 22:15 — whole-world conservation test off by 69,526 paise
**Symptom:** `test_whole_world_credit_delta_reconciles_against_ground_truth`
failed: `assert (617398744 - 617837050) == -507842` (actual delta
-438306, expected -507842 at first, then -507832 after an unrelated sign
fix — gap stayed ~69,526 either way).
**Diagnosis:** D04 ("refund deducted twice") has no backing source
record by design — the ledger's `Refund` list is deliberately never
duplicated, so the double deduction is written directly onto the batch's
`expected_credit`/`BankCredit.amount` via `_adjust_batch_and_credit`.
Every other injector instead recomputes its batch from records via
`compute_batch_net_paise`. D04 runs 4th; whenever D05-D08 or D10-D12
later recomputed a batch D04 had already adjusted, the from-scratch
recompute silently overwrote D04's delta. All 3 D04 instances were lost
at the failing seed (23250 + 25833 + 20443 = 69526 — exactly the gap).
Found with an Opus subagent, given the debugging effort required to
isolate which of 12 injectors and ~50 batches was responsible.
**First fix:** none attempted before diagnosis — the subagent located
the root cause before proposing a fix.
**Whether it worked:** n/a.
**Final fix:** added `_CreditBook`, threaded through every injector in
place of the bare batch-to-credit dict. It carries
`unbacked_delta_by_batch`, which `_adjust_batch_and_credit` (D04) banks
into and `_recompute_batch_and_credit` (everything else) re-applies on
top of its from-scratch recompute. Order-independent by construction —
does not depend on which injector runs before which.
**Guard added:** `test_whole_world_credit_delta_reconciles_against_ground_truth`
itself (already existed; this incident is that test doing its job) plus
per-batch reconciliation checks in the individual D01/D04/D06/D08 tests.

## 2026-08-23 22:15 — same test's own sign table was wrong for D07
**Symptom:** after fixing the D04 bug above, the same test still failed:
`assert (617398744 - 617837050) == -507832`, gap reduced from 69,526 to
0 only after this second fix.
**Diagnosis:** the test computed each entry's whole-world sign as `1 if
discrepancy_class == FEE_UNDERCHARGE else -1`. `ROUND_HALF_EVEN` only
ever differs from `ROUND_HALF_UP` at an even N.5 tie (an odd-N.5 tie
already rounds to the same even N+1 under both modes, so it never
registers as a candidate) — at an even N.5, half-even always rounds
*down* to N. A lower reported tax means a *higher* reported net, the
opposite direction from every other always-one-direction class in the
sign table. `ROUNDING_DRIFT`, unlike `FEE_OVERCHARGE`/`FEE_UNDERCHARGE`,
isn't directionally named, so the blanket "not-FEE_UNDERCHARGE means -1"
heuristic silently mis-signed every D07 entry.
**First fix:** none — found and understood in the same pass as the D04
diagnosis above.
**Whether it worked:** n/a.
**Final fix:** replaced the blanket heuristic with a fixed per-code sign
table (`D02/D03/D04/D05/D08/D11/D12: -1, D07: +1`), keeping only D01 and
D10 (genuinely bidirectional per instance) reading their sign from
`discrepancy_class`.
**Guard added:** none new — the existing property test now asserts the
correct thing; no separate regression test needed since this was a bug
in the test's own logic, not in shipped generator code.

## 2026-08-24

**Built:** `core/contract.py` (the LLM-facing wire schema `RateCardParse` plus
the deterministic `CompiledContract` engine); `llm/contract_parser.py`;
`tests/test_contract.py` (89) and `tests/test_contract_parser.py` (21), both
written before the implementation and confirmed failing first. Repaired
`llm/providers/gemini.py` (see incident below) and wired `LLM_CACHE_DIR`,
`LLM_MAX_RETRIES`, `LLM_RETRY_BACKOFF_BASE` into config. 393 tests passing;
`guard_core.py`, `test_architecture.py` and `ruff` clean on everything
touched.

**The wire schema lives in `core/contract.py`, not in `llm/`.** Alternative
rejected: define it beside the parser in `llm/contract_parser.py` — rejected
because `core/` must load a compiled contract without importing `llm/`
(invariant 2), so the schema would have to be duplicated. `llm/` importing
`core/` is legal; the reverse is not. The model is shaped by the domain
schema, not the other way round.

**Overlap is scoped per emitted fee component type, not globally.**
Alternative rejected: any two rules matching one transaction is an error, as
originally specified — rejected because the +2% international surcharge
matches every international card transaction the MDR clause also matches, so
a global rule makes surcharges inexpressible. The workaround (folding
international into every card rule as a duplicated variant) doubles the card
rule count and leaves 380bps traceable to no single clause in the document.
Two clauses emitting the same component type remain a hard error.

**`cap_paise` caps the ad-valorem component only; a fixed fee is added on
top, uncapped.** Alternative rejected: cap the rule's whole fee — rejected
because once the cap binds, the fixed fee's own amount is unrecoverable from
the total. Not decidable from the document, and untestable against the
current dataset (the capped debit slab has `fixed_fee_paise=0`), so it was
asked rather than assumed.

**Completeness means gapless amount and date coverage within declared
scope.** Alternative rejected: every `PaymentMethod` x `CardType` must have a
rule — rejected because the real rate card prices no EMI, so strict coverage
would require inventing an EMI rate the document does not contain. Uncovered
methods are reported at compile time; a transaction using one raises
`NoApplicableRule` and becomes an exception, never a guessed fee.

**Every interval is half-open: `[amount_min, amount_max)` and
`[effective_from, effective_to)`.** Alternative rejected: inclusive
`effective_to` — rejected because "exactly midnight on the revision date"
then has two defensible answers. Half-open also matches `MDRTier`'s existing
`[min_paise, max_paise)` convention in `datagen/ratecard.py`.

**`mcc_pattern` is a prefix glob (digits, at most one trailing `*`), not a
regex.** Alternative rejected: regex — rejected because regex intersection is
undecidable in general, so overlap detection could not be exact. Two prefix
globs intersect iff one is a prefix of the other.

**Rounding is not parsed from the document.** Alternative rejected: let the
model emit a rounding mode — rejected under invariant 2: a model choosing a
rounding mode is a model choosing an amount, and it is the highest-leverage
thing it could get wrong. Rounding comes from config, defaults to half-up,
and is part of the contract version. Rounding language found in the document
lands in `rounding_note`, which the engine never reads.

**`supersedes` is validated, never inferred.** Alternative rejected: close a
dangling `effective_to` automatically when a later rule prices the same
transactions — rejected because that is the engine inventing a business rule.
A dangling one surfaces as an overlap and is reported. The live parse
confirmed the model closes both revised slabs correctly, so the strict
reading costs nothing in practice.

**Basis-point arithmetic is integer-only.** Alternative rejected: `Decimal`
plus `quantize`, as `datagen/ratecard.py` uses — rejected because invariant 1
confines `Decimal` to ingest parsers and `core/money.py` already spends that
allowance in `from_rupees`. `divmod(paise * bps, 10_000)` with an explicit
tie rule is exact at these magnitudes and agrees with the Decimal path, which
is what makes the differential test against datagen meaningful.

**`FeeBreakdown` returns new component types rather than reusing
`core.models.FeeLine` / `TaxLine`.** Alternative rejected: add `rule_id` to
`TaxLine` — rejected because adding a field, even an optional one, changes
the serialized JSON and breaks the byte-identical committed run under
`runs/realistic-seed42`.

**`version_id` hashes rounding mode, rules and dormant clauses; it excludes
`source_sha256` and the prose fields.** Alternative rejected: include the
source document hash — rejected because the version identifies the
arithmetic, not the input's byte formatting; a reflowed document that parses
to the same schedule is the same contract. Rounding mode is included because
changing it changes every computed amount.

**Retry, backoff and cache location read `LLM_*` env vars; RPM keeps
`GEMINI_*`.** Alternative rejected: one vendor prefix for all of them —
rejected because retrying and caching a prompt hash are not vendor concepts;
any provider behind the Protocol does both the same way. Requests per minute
is a Gemini quota and keeps the vendor name.

## 2026-08-24 15:10 — every Gemini call was unconstrained prose and looked like structured output

**Symptom:** first live call against the real API:
`json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`.
`interaction.output_text` held `"Here are three Indian payment methods with a
one-line description for each:\n\n1. **Unified Payments Interface (UPI):**
An instant real-time payment system..."` — prose, with a JSON schema attached
to the request.

**Diagnosis:** two independent faults. (a) `generate_structured` passed
`response_format={"type": "text", "mime_type": ..., "schema": ...}`, but
`types.TextResponseFormat` has exactly two fields, `mime_type` and
`jsonSchema`; the `schema` key was dropped with no error. (b) Corrected to
`jsonSchema`, the Interactions API still returned prose — it does not enforce
`response_format` for this model. Found by dumping the returned `Interaction`
object, then inspecting `types.TextResponseFormat.model_fields`. Every call
this project would ever have made was prompt-and-pray, and nothing at the
call site could tell: the failure is silent by construction. No test caught
it because every provider test fakes the client.

**First fix:** moved the schema to the `jsonSchema` key on the same
Interactions call.

**Whether it worked:** no. Still prose. Ruled the Interactions API out for
structured output.

**Final fix:** `client.models.generate_content` with
`GenerateContentConfig(response_mime_type="application/json",
response_schema=...)`, which does enforce the schema — proven by it returning
HTTP 400 naming our own field paths. That exposed a second fault: Pydantic
emits `additionalProperties` (from `extra="forbid"`), `$defs`/`$ref`, and
`anyOf: [..., null]`, and the API rejects the first outright. Added
`to_gemini_schema()` to inline refs, strip rejected keys, and rewrite
nullable unions as `nullable: true`. The domain schema under `core/` stays
strict; only the wire payload is narrowed. Added `_decode` so a response that
is not a JSON object degrades to `ProviderUnavailable` rather than a
`JSONDecodeError`.

**Guard added:** `test_the_schema_is_sent_as_a_response_schema` asserts the
schema reaches the request config — the exact silent drop that caused this.
Plus `test_a_non_json_response_degrades_to_provider_unavailable`,
`test_an_empty_response_degrades_to_provider_unavailable`, and six tests over
`to_gemini_schema` covering ref inlining, `additionalProperties` removal,
nullable rewriting, and refusal of recursive schemas. Residual risk accepted:
all of these use fakes, so a change in the API contract would again pass the
suite. Only a live call catches that class of fault, and there is currently
no scheduled one.

## 2026-08-24 16:05 — a provider test passed only because .env was empty

**Symptom:** `test_missing_api_key_raises_provider_unavailable` started
failing (`DID NOT RAISE ProviderUnavailable`) the moment a real key was put
in `.env`.

**Diagnosis:** the test called `monkeypatch.chdir(tmp_path)` on the
assumption that `load_dotenv()` resolves relative to the cwd. It resolves
from the calling module's directory, so it walked up and found the repo's own
`.env` regardless. The test had never asserted what it claimed to; it passed
only while that file happened to be empty.

**First fix:** none — the cause was evident from the failure.

**Whether it worked:** n/a.

**Final fix:** stub `llm.providers.gemini.load_dotenv` to a no-op so the test
asserts the config logic rather than the developer's filesystem.

**Guard added:** none — the repaired test is its own guard.

## 2026-08-24 17:05 — a tax on transaction gross was billed once per fee line

**Symptom:** found by review, not by a failing test. A clause charging
`1.75% + Rs.10` with a tax based on `TaxBase.TRANSACTION_GROSS` emits two fee
components, and `_taxes_for` ran once per component. Reproduced against the
real class: gross Rs.1,000, TDS at 1800bps, two `TaxComponent`s of 18,000
paise each, `total_tax` 36,000 paise where 18,000 is correct. Rs.180
overcharged on a schema-legal contract.

**Diagnosis:** `fee_for` looped `for component in ...: taxes.extend(
_taxes_for(rule, component, payment))`, and `_taxes_for` selected the base per
treatment — `component.amount` for `FEE_AMOUNT`, `payment.amount` for
`TRANSACTION_GROSS`. Per-component is right for `FEE_AMOUNT`, because each fee
line is a distinct taxable amount. It is wrong for `TRANSACTION_GROSS`,
because the gross belongs to the transaction, not to a fee line, so repeating
it per component multiplies the same base. No validator caught it: this is
not an overlap between two rules, it is one rule applying one treatment
twice. No test caught it either — `TRANSACTION_GROSS` was never exercised
anywhere in the suite.

**First fix:** none — the reproduction made the cause plain.

**Whether it worked:** n/a.

**Final fix:** split `_taxes_for` into `_fee_taxes_for` (per component, filters
to `FEE_AMOUNT`) and `_gross_taxes_for` (once per matched rule, filters to
`TRANSACTION_GROSS`). `TaxComponent.on_fee_type` became `FeeType | None`, with
None meaning the tax is levied on the transaction rather than on any one fee
line. A gross-based tax now applies whenever its clause matched, including
when the clause charges no fee at all.

**Guard added:**
`test_a_gross_based_tax_is_charged_once_however_many_fee_lines_the_clause_emits`,
`test_a_gross_based_tax_is_not_attributed_to_any_one_fee_line`,
`test_a_gross_based_tax_applies_even_when_the_clause_charges_no_fee`, and
`test_a_fee_based_tax_stays_per_fee_line` as the control that per-line GST did
not regress. Not reachable from the current rate card, which taxes only fee
amounts — so nothing in the demo data would ever have surfaced it.

## 2026-08-24 17:20 — a cap that zeroed a fee erased its own evidence

**Symptom:** found by review. With `cap_paise=0` and `rate_bps>0` (both
schema-legal), `_components_for` computed `charged=0`, `cap_applied=True`,
then dropped the component at `if charged != 0`. Verified: `fees == []`, so
`uncapped_amount` and `cap_applied` vanished.

**Diagnosis:** the `charged != 0` guard exists to mirror datagen's
`if mdr > 0`, which suppresses a fee line that rounds to nothing. It cannot
tell that case apart from a cap that deliberately waived the fee. The paise
were not lost — zero is zero — but `FeeComponent`'s stated purpose is to make
"a cap contractually due but not applied" nameable, and a verifier had no
component left to compare a wrongly-charged settlement line against.

**First fix:** none.

**Whether it worked:** n/a.

**Final fix:** guard is now `charged != 0 or cap_applied`. A fee that merely
rounds to nothing still emits nothing, preserving datagen parity.

**Guard added:** `test_a_cap_of_zero_still_reports_that_the_cap_bit`, plus
`test_a_fee_rounding_to_zero_emits_no_component` to pin the case that must
keep emitting nothing.

## 2026-08-24 17:35 — the sign-off render told a human prepaid cards were priced

**Symptom:** found by review. `uncovered_methods` reported only `emi`, so
`render_schedule` printed "Not priced by this card: emi" — while
`fee_for` on a prepaid card raised `NoApplicableRule`.

**Diagnosis:** `uncovered_methods` tested coverage at method granularity
(`r.applies_when.method in (None, method)`), so CARD counted as covered as
soon as any card clause existed. Card slabs are scoped to credit and debit;
prepaid has no clause. The failure mode is the bad one: the document a human
signs states the schedule is complete when it is not.

**First fix:** added `uncovered_card_types` checking, per card type, whether
any rule with `method in (None, CARD)` and `card_type in (None, ct)` exists.

**Whether it worked:** no. It reported prepaid as covered, because the
international surcharge clause has `card_type=None` (meaning any) and so
matched every card type. A surcharge is not what prices a card.

**Final fix:** the same check, additionally requiring
`applies_when.is_international is not True`, so a clause scoped to
international transactions cannot stand in for base pricing.
`render_schedule` now lists unpriced card types beside unpriced methods.
`uncovered_methods` was deliberately left at method granularity: the
live-parsed contract types UPI's flat fee as `fixed` rather than `mdr`, so
narrowing that property by fee type would misreport UPI as unpriced.

**Guard added:** `test_prepaid_cards_are_reported_as_unpriced`,
`test_priced_card_types_are_not_reported_as_unpriced`,
`test_render_names_the_unpriced_card_type`,
`test_a_card_with_no_card_type_has_no_clause`.

## 2026-08-24 17:45 — three gaps where a test proved less than it appeared to

**Symptom:** found by review; no defect behind any of them, but each left a
branch where a regression would pass silently.

**Diagnosis:** (a) the only half-even tie test used 600 paise at 175bps =
10.5, where the whole part 10 is even, so half-even and truncation give the
same answer — deleting the `whole % 2` clause in `apply_bps` left all 110
tests passing. (b) The real card's slab edges (200,000 and 1,000,000 paise)
appeared only in the datagen differential test, which the file's own docstring
disclaims as not the correctness proof. (c) Every overlap test drove the
conflict through the amount or MCC axis, leaving `_dates_intersect`'s
genuinely-offset branch unexercised.

**First fix:** none — these are test gaps, not code faults. Behaviour verified
correct before writing each assertion.

**Whether it worked:** n/a.

**Final fix:** none to shipped code.

**Guard added:** `test_half_even_rounds_up_at_an_odd_tie` (1,800 paise at
175bps = 31.5, odd whole part, must round up to 32 while DOWN gives 31);
`test_golden_at_the_real_tier_boundaries` (seven hand-computed literals at
199,999/200,000 and 999,999/1,000,000 for both card types);
`test_the_addendum_leaves_tiers_two_and_three_untouched`;
`test_partially_offset_date_ranges_overlap`; `test_zero_gross_payment_emits_no_fee`.

## 2026-08-24 17:55 — the flash-lite default named a model that does not exist

**Symptom:** listing the models this project's API key can reach returned no
`gemini-3.7-flash-lite`, which was `GeminiConfig.model_flash_lite`'s default.
`gemini-3.7-flash` does exist, so the parser and adjudicator paths were
unaffected; the narrator would have failed on its first call.

**Diagnosis:** found while checking which model names the key accepts, after
the structured-output repair. Available lite models were
`gemini-2.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite` and
`gemini-flash-lite-latest`. Nothing has ever called the `flash-lite` hint —
`llm/narrator.py` is still a stub — so no test or run had exercised it. The
default was wrong from the day it was written and would have stayed wrong
until the first narration.

**First fix:** none.

**Whether it worked:** n/a.

**Final fix:** default changed to `gemini-flash-lite-latest`, with `.env` set
to match. A floating alias was chosen over pinning a version: pinning is
exactly what broke here, and a retired generation should degrade to the
current one rather than to an error. Accepted cost: the model behind the
alias can change under us, so a narration prompt could shift output without a
code change. Acceptable for narration, which generates no amounts; it would
not be acceptable for the contract parser, where `GEMINI_MODEL_FLASH` stays
pinned to an explicit version.

**Guard added:** `test_flash_lite_default_is_a_model_that_exists` asserts the
default. It compares a string, not the live model list, so it catches a
regression of this value but not the underlying risk of a default that stops
resolving. Only a live call catches that, and there is no scheduled one.

## 2026-08-24 22:35 — Build core/decompose.py: three-tier cascade, proof, verifier

**Built:** `core/decompose.py` (three-tier cascade, `DecompositionProof`,
`verify_proof`), `core/conserve.py` (the sign convention only), four new
`core/ledger.py` indexes, `core/contract.py`'s `_canonical` promoted to a
public `canonical_json`. `tests/test_decompose.py` (49), `tests/test_conserve.py`
(21), `tests/test_ledger.py` (+20). 510 tests passing, `guard_core.py` clean,
`ruff` adds no new findings.

On the committed run (`runs/realistic-seed42`, 17,707 records, 31 credits):
25 resolve structurally, 6 at subset-sum, none unresolved or ambiguous, no
record claimed twice, every proof verifies. The 6 are exactly the credits
D09 corrupted the UTR of, and each recovered its *true* batch — checked
against `truth/ground_truth.json` ad-hoc, not in a test. 29 of 31 net
exactly; 2 carry a residual, −101,501 paise in total, which is the money
`unexplained` will have to account for.

**A structural hit never cascades, even when the arithmetic disagrees.** If
the UTR names a batch whose records do not net to the credit, tier 1 still
resolves and the proof carries a non-zero `residual_paise`. Alternative
rejected: fall through to tier 2 on an arithmetic mismatch — rejected
because tier 2 would then go looking for some *other* subset that nets
exactly, find one, and the shortfall would disappear from the report. That
inverts the product: a short settlement is the finding, not a matching
failure. Confidence stays 10,000 in that case, because a UTR join is certain
about *which records* regardless of whether the amount is right; whether the
amount is right is `verify.py`'s question.

**The atom of a decomposition is a settlement unit, not a record.** A unit
is a whole batch, or a payment with its own fee and tax lines, or a
standalone refund/chargeback/adjustment. Alternative rejected: subset-sum
over individual records — rejected because a credit in the committed run
would face ~168 payments and 2^168 subsets, so the node budget would trip on
every credit, every time. It is also semantically wrong: you cannot include
a payment's fee without the payment. At unit granularity the six hard
credits expanded 15–81 nodes against a 200,000 budget.

**Tier 1 matches the UTR by exact equality only.** Alternative rejected:
accept a unique prefix match, which would resolve all six D09 credits at
tier 1 — rejected because a truncated UTR is corrupt data, and repairing it
inside the most-trusted tier hides a data-quality problem behind a confident
answer. Falling through is the visible behaviour, and tier 2 recovers all
six anyway.

**A split settlement resolves at tier 2, not tier 3.** When several credits
name one UTR, that batch is exploded into payment-level sub-units and
subset-sum searches those. Alternative rejected: handle splits with the
Hungarian method as originally scoped — rejected because assignment is 1:1
and structurally cannot express "this credit is payments 0 and 1". Tier 3
keeps the case it can actually express: choosing which credit goes with
which unit when no subset sums exactly. Whole-batch and sub-unit granularity
are never offered together — the whole always equals the sum of its parts,
so every split credit would report as ambiguous.

**Ambiguity is reported with the competitors named and `terms` left empty.**
Two subsets hitting the target, or an assignment whose runner-up costs
exactly the same, both produce `AMBIGUOUS`. An ambiguous proof carrying an
answer fails verification. This is the behaviour the module exists for:
silently picking one would produce a clean report over the wrong records
with nothing anywhere to indicate it.

**A zero-margin assignment is a tie, and a tie is ambiguous for every credit
in it.** This does double duty. It is the confidence signal — a low margin
routes to `ESCALATE` — and it removes the one place `scipy`'s undocumented
tie-breaking could make a report differ across BLAS builds, which invariant
4 would not survive. Reporting the whole assignment as ambiguous rather than
just the tied pair is deliberately conservative: a tie anywhere means the
pairing as a whole could have come out differently.

**`scipy` and `numpy` added to `pyproject.toml`.** Both were already in the
conda env but undeclared; asked first, confirmed. `linear_sum_assignment`
converts to float64 internally, so tier 3's costs are integers capped well
under 2^53 and every tie is detected exactly rather than trusted. Alternative
rejected: a hand-rolled integer Jonker-Volgenant — rejected on the ask, not
on the merits; the tie check makes the float64 conversion a non-issue.

**The node budget is the binding bound; the wall clock is a safety net.**
They bail out with different reasons on purpose. `NODE_BUDGET_EXHAUSTED` is
reproducible across machines; `TIME_BUDGET_EXHAUSTED` is not, and a run that
trips it is **not** covered by invariant 4's byte-identical guarantee.
Collapsing them into one "budget exhausted" reason would make a
non-deterministic run indistinguishable from a deterministic one in the
report. `eval/` should assert zero `TIME_BUDGET_EXHAUSTED` in any run it
scores.

**`elapsed_ns` is on the proof but excluded from `proof_hash`.** A declared
`TELEMETRY_FIELDS` set is stripped before canonical JSON. Timing is worth
observing and cannot be hashed without breaking invariant 4. Integer
nanoseconds via `time.monotonic_ns`, never `time.monotonic` — a float
literal anywhere under `core/` fails `test_core_has_no_float_literals`.

**Per-tier confidence is a fixed placeholder, not an invented score:**
10,000 / 8,000 / 6,000 bps. `core/lanes.py` (conformal-calibrated lanes) is
the module that will calibrate this properly; inventing a formula here would
put an uncalibrated number in front of a reviewer. The raw features the
calibration needs — `candidate_count`, `subset_size`, `nodes_expanded`,
`assignment_margin` — travel on the proof instead.

**`core/conserve.py` adopts `datagen`'s sign convention rather than
re-deriving one.** DECISIONS.md 2026-08-23 flagged both the adjustment sign
tables and "a won chargeback's reversal is a `MANUAL_CREDIT` `Adjustment`"
for whoever built this. Both adopted. That second one is why chargebacks
subtract at *every* stage including `WON`: the reversal is a separate
record, so a positive won chargeback would credit it twice. `core/` may not
import `datagen/`, so this is a reimplementation;
`test_signed_sum_equals_the_independently_written_batch_formula` reconciles
the two against a third, independently written formula rather than calling
either.

**`signed_paise` raises for non-contributing types instead of returning 0.**
A silent zero would let a mis-typed record vanish from a sum, and would let
a bank credit be cited as a term in its own decomposition — which nets to
zero and "balances" while explaining nothing.

**`settlement_lag_days` (default 2) is on the budget, not in the code.** Tier
3's date cost measures distance from the *expected* credit date, not from
the cycle close, because a settlement lands T+2 by design and comparing
against the raw cycle date systematically favours the wrong batch. It is a
matching hint only and touches no arithmetic; no amount anywhere depends on
it.

**Not built:** `verify.py`, `lanes.py`, and the rest of `conserve.py` — only
`signed_paise` landed there. No loader in `core/` for the run JSON (tests
load it themselves). No `Finding` construction: turning an ambiguous or
unresolved proof into a `Finding` with a `DiscrepancyClass` is `verify.py`'s
job and needs the taxonomy skill's five coordinated steps.

**Untested in anger:** tier 3 never fires on the committed run — 25 credits
resolve structurally and the other 6 at subset-sum — so the Hungarian path
has unit tests behind it and no production exercise. The first real
many-to-many statement will be its first real test.

## 2026-08-24 23:10 — Settlement lag out of the code, invariant 4 gated in eval/

Two follow-ups on the decomposition build, both raised in review.

**`settlement_lag_days` is configuration, not a constant.** T+2 is the Indian
standard but it is a contracted arrangement, not a law of arithmetic — a
merchant on T+1 or T+3 is ordinary, and a hardcoded 2 would silently
mis-price tier 3's date cost for them. `SETTLEMENT_LAG_DAYS = 2` is now a
named module constant in `core/decompose.py`, `SETTLEMENT_LAG_DAYS=2` is in
`.env`, and `DecompositionBudget.from_env()` reads it, mirroring
`GeminiConfig.from_env()`.

**Configuration is asked for, never inhaled.** `DecompositionBudget()` still
returns the declared defaults and ignores the environment entirely; only the
explicit `from_env()` reads it. Alternative rejected: read the environment
inside `__init__` so every construction picks it up — rejected because it
would make every run depend on the shell it was launched from, and invariant
4's byte-identical guarantee could not then be checked at all. The same trap
that bit `llm/providers/gemini.py` applies to the tests here: `load_dotenv()`
resolves its path from the calling module's location, not the cwd, so the
fallback test stubs it out rather than asserting the developer's filesystem.

Only the settlement lag is wired to the environment. The other budget fields
describe this machine's patience, not the merchant's arrangement, so they
stay in code until one of them actually needs to vary by deployment.

**`eval/determinism.py`: a run containing a wall-clock timeout must not be
scored.** A correction to the framing this was raised with — the
`TIME_BUDGET_EXHAUSTED` reason is *not* deterministic given the same seed.
Whether any individual credit trips it depends on machine speed and load, so
it is not merely the count that varies across machines; which credits time
out varies too. The conclusion stands and the fix is the same: assert zero,
not "assert the same".

The distinction the module rests on is between the two budgets.
`NODE_BUDGET_EXHAUSTED` is a decision about the search — the same inputs
reach it at the same point anywhere, so a run that exhausts it is still
reproducible, it just reproducibly gives up. `TIME_BUDGET_EXHAUSTED` is a
decision about this machine on this day. `check_reproducibility` reports
both; only the second sets `reproducible=False`.

**`assert_reproducible` raises rather than returning a flag.** Alternative
rejected: return the report and let callers decide — rejected because the
failure mode is an accuracy number computed over an unreproducible run,
which is worse than no number: it looks like evidence.

**Why this could not live in `verify_proof`.** A timed-out proof verifies
cleanly and its `proof_hash` is entirely self-consistent, because the hash
covers what the proof *says*, not what the machine was doing while it said
it. Nothing local to the proof reveals the problem — only the reason code
does, and only when you are looking at the whole run. `IRREPRODUCIBLE_REASONS`
is a frozenset of one rather than an inline comparison, so a future
machine-dependent bound has an obvious home.

`eval/determinism.py` reads no ground truth. It lives in `eval/` because it
scores a run rather than computing one; the package's quarantine privilege
is unused here.

**Built:** `eval/determinism.py`, `tests/test_eval_determinism.py` (16),
5 config tests in `tests/test_decompose.py`. 531 tests passing, `guard_core.py`
clean, `ruff` adds no new findings.

## 2026-08-25 01:15 — core/verify.py built, core/conserve.py's identity finished

`core/verify.py` was a 1-line stub; built in full. Recomputes fee/tax per
payment via `CompiledContract.fee_for()`, aggregated by `fee_type` rather
than 1:1 line matching. `core/conserve.py` had only the sign convention;
added `ConservationReport`, `ConservationViolation`, `conserve()`,
`conserve_all()`, `total_unexplained_paise()`. `unexplained_paise` is
exactly `proof.residual_paise`; `conserve()` self-checks by reconstructing
`credit_paise` from its own buckets before returning.

**`unexplained_paise` stays orthogonal to verify.py, never absorbs its
deltas.** Rejected: substitute verify.py's contract-correct fee/tax into
the identity so `unexplained` reflects contract-recomputation deltas
directly. Rejected because that couples `conserve()` to `verify()`'s
output — it could no longer run on a proof alone — and because orthogonal
is what makes D01 (wrong MDR tier, settlement math internally consistent)
provable as the case `conserve.py` alone structurally cannot catch:
`unexplained=0` on a D01 credit, nonzero `Finding` from `verify.py` on the
same credit, asserted together in one test.

**Clean-profile proof generated at test time, not committed.** Rejected
committing a fixture (`runs/clean-seed*`, mirroring `realistic-seed42`) —
a fixed seed already makes it fully reproducible; no reason to add another
~15-20K-record JSON file to the repo for that.

**`Finding.severity`/`lane` hardcoded to MAJOR/PROPOSE, always.** Rejected
a paise-threshold or percentage-of-credit severity scale — `core/lanes.py`
doesn't exist yet, and `decompose.py` already set the precedent
(`CONFIDENCE_BY_TIER` hardcoded per tier) of not inventing an uncalibrated
score ahead of the module whose job that is.

**Chargeback reversal matched ledger-wide, not within the same proof.**
Traced `datagen/world.py` directly: a WON chargeback's reversal is booked
on its resolution day, a 3-10 day lag that routinely lands it in a
different settlement cycle — a different credit/proof — than the
chargeback itself. Proof-local matching would have false-positived a
`CHARGEBACK_AMOUNT_MISMATCH` on nearly every WON chargeback in otherwise
clean data.

**No new orchestration module for "wire them" (decompose+verify+conserve
together).** Rejected `core/audit.py`. Only `verify.py`/`conserve.py` were
asked for; the three pipelines are called in sequence at each call site
instead.

**Realistic-run number reported via a light regression-guard test plus a
one-time terminal run, not a committed script.** Rejected `scripts/`
entry — `cli/` is where a real "run the audit" entry point belongs later,
and this repo already declines to half-build one early (`demo: not
implemented yet` in the `Makefile`).

**Built:** `core/verify.py`, `ConservationReport`/`conserve()`/`conserve_all()`/
`total_unexplained_paise()` in `core/conserve.py`, `tests/test_verify.py` (16),
9 tests added to `tests/test_conserve.py`. 555 tests passing at this point.
Clean profile (~5,200 payments) reconciles to `unexplained=0`, `verify()=[]`
on every credit. Committed `realistic-seed42`: `unexplained` totals
-Rs 1,015.01 (unchanged from `decompose.py` alone — `conserve.py` adds no new
residual), `verify()` finds 48 discrepancies (~Rs 4,972.32), including the
profile's one planted D05 case.

## 2026-08-25 01:25 — chargeback reversal check silently passed on shared amounts

**Symptom:** money-auditor review, reviewing the diff before commit: two
credits, each with a WON chargeback of the same amount, competing for one
real `MANUAL_CREDIT` reversal in the ledger, would both pass. Reproduced
directly — pre-fix logic against that fixture printed
`pre-fix behavior (fresh Counter per proof): []`, i.e. neither chargeback
was flagged, though only one of the two actually had a reversal.

**Diagnosis:** `_chargeback_findings` built its `Counter` of available
`MANUAL_CREDIT` amounts fresh inside every call. `verify_all()` calls
`verify()` once per proof with no shared state between calls, so each
proof's chargeback check saw the reversal as untouched and available,
regardless of what a prior proof in the same run had already claimed.

**First fix:** thread one `Counter`, built once by `verify_all()`, through
every `verify()` call in a run via a new optional `reversal_amounts`
parameter; `verify()` still builds its own fresh pool when called alone.

**Whether it worked:** yes.

**Final fix:** as above — `_reversal_amount_pool(ledger)` extracted once,
passed through `verify()` to `_chargeback_findings()`, consumed
(decremented) in sorted-proof, sorted-chargeback-ref order for determinism.

**Guard added:**
`test_two_won_chargebacks_of_the_same_amount_in_different_proofs_still_need_two_separate_reversals`
in `tests/test_verify.py`, asserting `verify_all()` over two such proofs
flags exactly the one without a reversal.

## 2026-08-25 02:15 — a settlement batch with no bank credit audited clean

**Symptom:** payments-domain review, immediately after the fix above:
`total_unexplained_paise` only sums residual on credits that exist.
Reproduced directly — a fixture with one paid batch (STL-A) and one
formed-but-never-credited batch (STL-B, Rs 2,941 net, deliberately no
`BankCredit`) printed `total_unexplained_paise: 0`.

**Diagnosis:** `decompose_all` iterates the bank statement's credit list,
never the ledger's records. A batch whose credit never arrived produces no
proof at all, so no `ConservationReport` is ever computed for it — the run
reports as fully conserved while an entire batch's money is simply absent
from every sum.

**First fix:** add a run-level check over the whole ledger, independent of
which credits showed up — `unclaimed_records()`/`unclaimed_paise()` in
`core/conserve.py`, diffing every money-contributing ledger ref against the
union of refs claimed by any proof in the run.

**Whether it worked:** yes.

**Final fix:** as above.

**Guard added:**
`test_a_settlement_batch_with_no_bank_credit_at_all_reports_clean_by_total_unexplained_alone`
and `test_unclaimed_paise_is_zero_when_every_ledger_record_was_claimed` in
`tests/test_conserve.py`. Validated against the committed `realistic-seed42`
run, not just the synthetic fixture: `unclaimed_paise` surfaced Rs 4,511.37
across 3 payments — the profile's three planted D08 instances (payment
never reaches any settlement), previously invisible to every existing check.

**Not fixed today, named but out of scope (fix-one-issue-at-a-time
instruction each round):** `Ledger`'s duplicate-record-id collision (last
write wins in `_by_ref`, join indexes double-append regardless — a
duplicate fee-line id could double-count); `decompose.py`'s tier-1 vs
tier-2/3 inconsistency in which record types get fee/tax lines attached
(dormant — `datagen` never emits a non-`Payment` fee line, so nothing
exercises it yet); gross-based tax stacking uninhibited across two
overlapping-but-permitted fee clauses in `core/contract.py`;
`CompiledContract.fee_for()` pinning fee versioning to payment capture
date rather than settlement/billing date; no property-based or fuzz
testing anywhere over N proofs sharing one ledger (`hypothesis` is a
dependency used in exactly one file, `tests/test_money.py`) — the class of
test that would have caught the 01:25 incident before a review had to find
it by reading code, not just after.

**Built:** `unclaimed_records()`, `unclaimed_paise()` in `core/conserve.py`.
2 more tests in `tests/test_conserve.py`. 557 tests passing, `ruff` and
`guard_core.py` clean. `/verify-invariants` run afterward: architecture
tests pass, guard on changed files passes, live clean-profile audit gives
`unexplained=0`/`unclaimed=0`/`verify()=[]`, two independent runs over the
same seed hash identical, full suite green.

## 2026-08-25 16:11 — the first real calibration fit gave AUTO a threshold of 0

**Symptom:** the first live run of `eval/calibrate_lanes.py` against 14
synthetic seeds wrote `auto_min_calibrated_bps=0` to the calibration
artifact — meaning every non-LLM item, regardless of its own calibrated
confidence, would route to `AUTO`.

**Diagnosis:** `derive_auto_threshold` pooled all 5 raw-confidence sources
into one flat list before sweeping Clopper-Pearson bounds. `core/verify.py`
contributed ~171,652 near-perfect cells (its raw confidence is a constant
10,000, so isotonic regression collapses it to one point) that numerically
dominated the pool. Even with `llm/adjudicator.py`'s 146 points at only 25%
accuracy mixed in, the pooled failure rate still cleared the 0.5%-at-95%
target — a bad minority source's risk statistically laundered through a
good majority source's volume. Found by inspecting the fitted artifact's
per-source `n`/`n_positive` after the first live run looked suspicious
(`auto_min=0` is not a threshold, it's the absence of one).

**First fix:** changed `derive_auto_threshold` to require every source's
own Clopper-Pearson bound to hold independently at each candidate
threshold (`dict[SourceKind, list[tuple[int, bool]]]` instead of one flat
list), not a pooled bound.

**Whether it worked:** partially. Re-fitting correctly rejected
threshold=0, but surfaced a second bug: `fit_calibration_artifact` clamped
the "unreachable" sentinel (10_001) down to 10_000 via `min(x, 10_000)` to
satisfy `LaneThresholds.auto_min_calibrated_bps`'s `Field(ge=0, le=10_000)`
— silently turning "no threshold could be certified" into "the threshold
is exactly 10_000". `core/decompose.py`'s ASSIGNMENT-tier proofs (9 samples
across 14 seeds, all correct, calibrating to exactly 10_000, but far too
few to certify 0.5%/95%) would have reached `AUTO` anyway.

**Final fix:** made `LaneThresholds.auto_min_calibrated_bps` genuinely
nullable (`int | None`); `None` means AUTO is structurally unreachable, and
`core/lanes.py::assign_lane` checks for it explicitly rather than comparing
against a number that was never really 10,000.

**Guard added:** `tests/test_eval_calibrate_lanes.py::test_derive_auto_threshold_is_not_diluted_by_a_much_larger_accurate_source`
(reproduces the exact dilution scenario) and
`::test_fit_calibration_artifact_reports_auto_unreachable_as_none_not_clamped`;
`tests/test_lanes.py::test_auto_min_none_makes_auto_unreachable_even_for_maximum_confidence_non_llm_items`;
`tests/test_eval_calibration.py::test_no_non_llm_proof_or_finding_reaches_auto_when_unreachable`
validates the unreachable state end-to-end against held-out seeds (15-20)
the fit never saw.

The committed `calibration/lane_calibration.v1.json` now honestly reports
`AUTO` as unreachable. This is the correct answer given current data, not
a residual bug: tier-3 (ASSIGNMENT) proofs are rare — 9 samples in 14
seeds — and getting enough of them to certify a 0.5%/95% guarantee would
need far more synthetic seeds than this pass generated. No amount of
pooling should have papered over that.

## 2026-08-25 16:49 — Build llm/adjudicator.py and core/lanes.py: eight decisions, one incident

**Built:** `llm/adjudicator.py` — for the three residual shapes nothing in
the engine turned into a `Finding` before (a conservation residual on any
proof outcome including `RESOLVED`, an `AMBIGUOUS`/`UNRESOLVED` proof, an
unclaimed ledger record), batches residuals to an `LLMProvider`,
reference-checks citations against both `Ledger.exists()` and the shown
evidence pool, arithmetically re-verifies via `signed_paise`, and emits one
`Finding` per residual on `lane=PROPOSE`. `core/verify.py::fee_tax_cells`,
extracted as a superset of `_fee_and_tax_findings` (no behaviour change).
`core/lanes.py` — applies a committed `CalibrationArtifact` in integer
basis points only. `eval/labels.py`, `eval/metrics.py` — ground-truth
matching and calibration diagnostics. `eval/calibrate_lanes.py` — fits the
artifact via isotonic regression per source and a Clopper-Pearson lane
threshold sweep. `calibration/lane_calibration.v1.json` — the committed
artifact, fit against synthetic seeds 1-14 plus a live, cached Gemini pass
for the adjudicator source, validated against held-out seeds 15-20. Full
suite 641 passing, 1 skipped (expected — the AUTO-reachable coverage test
skips because AUTO is currently unreachable), `ruff` clean. `/verify-invariants`
run twice, both clean. Two independent reviews run via the Agent tool
(money-auditor, test-writer): money-auditor confirmed no model output ever
reaches a rupee amount and `AUTO` is unreachable for LLM-sourced items at
two independent enforcement points; test-writer found the coverage gaps
that led to decision 8 below, all now closed.

**The adjudicator's actual input is three residual shapes, not a `Finding`
requiring `UNKNOWN`.** Root `CLAUDE.md`'s literal wording names findings
"classified UNKNOWN"; no such `DiscrepancyClass` member exists, and nothing
in the engine constructed an `UNRECONCILED_RESIDUAL` `Finding` either.
Alternative rejected: inventing a taxonomy member, or narrowing scope to
only `AMBIGUOUS`/`UNRESOLVED` proofs — rejected because a `RESOLVED` proof
can still leave a non-zero residual (a structural hit never cascades), and
`verify.py` never looks at that residual at all.

**A citation must exist in the ledger AND have been shown in that
residual's own evidence pool.** Alternative rejected: existence alone, the
literal invariant-6 wording — rejected because it would accept a real but
irrelevant record the model never saw as if verified, which is worse than
no check at all: it looks verified.

**Calibration fitting lives entirely in `eval/`; `core/lanes.py` only
applies a pre-fit artifact, in integers.** Not really an alternative so
much as a constraint: `core/`'s float ban is absolute (zero exceptions,
not just money-adjacent) and `core/` cannot import `datagen/`, so fitting
against ground truth with floats could not have lived there regardless.

**`scikit-learn` added to `pyproject.toml` for `IsotonicRegression`, asked
and approved.** Alternative rejected: hand-rolling isotonic regression
(PAVA) — decided the approved dependency was simpler and more standard
than reimplementing a well-known algorithm for one call site.

**`fee_tax_cells` is a refactor of `verify.py`, not a second
reimplementation inside `eval/labels.py`.** Alternative rejected:
`eval/labels.py` walks payments/fee-lines independently, touching nothing
in `verify.py` — rejected because two copies of the same fee/tax
comparison logic drifting apart is exactly the class of bug this project
has already been burned by once (the documented `Ledger` collision).

**Rewiring `verify.py`/`decompose.py` to actually call `assign_lane` is
deferred, not done this session.** Alternative rejected: wire it in now —
rejected to keep this already-large change reviewable, matching the
existing precedent against a new orchestration module ("Rejected
`core/audit.py`", above).

**`ADJUDICATOR_HYPOTHESIS` calibration comes from a real, small, cached
live Gemini pass — not a placeholder identity mapping, not omitted.**
~14 batched calls across the 14 calibration seeds. Alternatives rejected:
ship `calibrated_bps = raw_bps` unfitted, or leave the source out of the
artifact entirely — rejected both because a real API key was already
configured and the cost was one-time and cacheable; there was no reason to
ship a fake number when a real one was this cheap.

**`findings_from_adjudication_run` emits one `Finding` per residual, not
one per accepted hypothesis.** Built the other way first — one `Finding`
per accepted hypothesis, mirroring `decompose.py`'s "don't collapse
competing explanations" precedent — and shipped it that way through most
of the session. Reversed after a money-auditor review found it let a
single residual's amount be claimed multiple times the moment anything
sums `Finding.amount_impact` over a run's findings, which is the natural
thing a report does. Runner-up hypotheses stay on `AdjudicationResult`,
already logged; they never become independent money claims. This is the
one place this session's own first instinct was wrong, not just a gap —
`decompose.py`'s precedent was for storing alternatives as metadata on
*one* proof, not for minting several proofs that each claim the same
credit, and the difference didn't surface until a reviewer asked "what
happens when something sums this."

**Incident:** see 16:11 above (`derive_auto_threshold` pooled all sources
into one bound, then a threshold-clamping bug turned "unreachable" into a
number that looked real) — not duplicated here.

**Not built:** the Phase C rewiring (decision above), an `EVIDENCE.md`
generator (`api_call_count` is tracked and returned, nothing writes it to
a file yet), `llm/narrator.py` (still a stub, out of scope this session).

**Unsure about:** hypothesis *quality* — schema/reference/arithmetic
correctness is fully tested, but only 14 seeds' worth of real model output
exists so far, and `ADJUDICATION_BATCH_SIZE=8`'s tradeoff against
`CachedProvider`'s per-call cache granularity is untuned against real
free-tier throughput.

## 2026-08-25 23:14 — Phase C lane wiring; exceptions.py clustering/pricing/
dispute packets; AUTO-lane posting; `assay explain`

**Built:** Phase C — `core/lanes.py`'s `assign_lane_for_verify_finding`/
`assign_lane_for_proof` wired into `core/verify.py` and `core/decompose.py`
via a new optional `calibration: CalibrationArtifact | None = None`
parameter on `verify()`/`verify_all()`/`decompose()`/`decompose_all()`,
defaulting to `None` (the old hardcoded MAJOR/PROPOSE/10_000 placeholder),
so every existing test kept passing untouched. `DecompositionProof` gained
`lane_assignment: LaneAssignment | None`, excluded from `proof_hash` via
`TELEMETRY_FIELDS` (same precedent as `elapsed_ns`). `core/exceptions.py`'s
`cluster_findings()` (deterministic clustering by discrepancy_class +
resolved rule_id + a 50-bps-bucketed delta signature, ranked strictly by
money) and `build_dispute_packet()` (claim, contract clause text via
`rule_id` → `FeeRule.source_quote`, a representative recomputed-arithmetic
example, full evidence list). `llm/narrator.py` built from a 2-line stub:
`narrate_dispute_packet()`, with every digit sequence in the model's
narration checked against the packet's own computed numbers and rejected
(`NarrationRejected`) if any is ungrounded. `core/ledger.py`'s
`journal_entries_for_auto_findings()` — a pure function, idempotency key
`sha256(input_hash:finding_id)`, no persistence layer built. `cli/loaders.py`
and `cli/explain.py`, `cli/__init__.py`'s first real Typer app,
`[project.scripts] assay = "cli:main"` in `pyproject.toml`. 682 tests
passing (641 at session start), `ruff`/`guard_core.py` clean on everything
touched.

Live-checked via the installed `assay explain` command (real
`GeminiProvider`, committed cache hit, no network call) against
`runs/realistic-seed42`: prints the full causal chain — contract clause
text, recomputed vs. reported fee/tax, which credit, decomposition
tier/outcome, calibrated lane/confidence, proof hash, every `Finding`
citing the record. An unknown record id and a clean payment both checked
live too.

**With the committed calibration artifact, a live run posts zero AUTO-lane
journal entries.** Not a bug: `calibration/lane_calibration.v1.json`'s
`auto_min_calibrated_bps` is `null` (16:11 above — honestly reporting AUTO
as currently uncertifiable). Checked directly against the committed run: 48
real findings, all calibrate to `propose`, `journal_entries_for_auto_findings`
returns `[]`. Phase C's wiring is real and correct; nothing in this
session's work makes AUTO reachable, because nothing should until a re-fit
certifies it.

**`DecompositionProof.lane_assignment` lives on the proof itself, and
`decompose()`/`decompose_all()` take an optional `calibration` parameter
rather than always loading one.** Asked first (three-way question):
rejected leaving the proof schema alone and calling `assign_lane_for_proof`
separately downstream — `core/lanes.py` was already built with exactly this
`assign_lane_for_proof(proof, artifact) -> LaneAssignment` signature in
anticipation of this, and there is nowhere else for a per-proof calibrated
verdict to live. `None`-default keeps the ~750 existing lines of
`tests/test_decompose.py` untouched.

**AUTO-lane posting is a pure function; no persistence layer was built.**
Asked first: rejected standing up real `sqlmodel`/SQLite storage now (the
first real use of that already-declared, currently-unused dependency) in
favor of keeping the money-critical posting logic pure and unit-testable —
idempotency tested by re-running the function against its own prior output
and asserting zero new entries — deferring the storage medium to a later,
separate task.

**The double-entry chart of accounts
(`discrepancy_receivable:<class>`/`settlement_suspense`) is an explicit
placeholder, not a real one.** Asked first; flagged here the same way
`Finding.severity`/`lane` were flagged and left for later refinement on
2026-08-23 — nothing in the codebase specifies a real chart of accounts.

**`RecomputedArithmetic` carries the contract's rate formula plus one
representative worked example, not a full reported-vs-recomputed pair.**
Alternative rejected: recompute reported-vs-recomputed via the
representative finding's own payment and fee lines — rejected because it
would require `core/exceptions.py` to either duplicate `core/verify.py`'s
fee/tax comparison logic (the exact drift risk 16:49 above already
flagged once) or import `core.verify` directly, which is circular
(`core/verify.py` already imports `core.exceptions.DiscrepancyClass`). The
rule's own formula plus the representative finding's already-computed
delta is checkable without either problem.

**Clustering key is `(discrepancy_class, rule_id, delta_bucket)`,
`delta_bucket` being the finding's impact as integer bps of its first
resolvable payment's gross, rounded to the nearest 50 bps.** Not asked — a
reversible implementation choice, not a payments-domain business rule.
Chosen so "the same clause misapplied at slightly different amounts"
collapses into one cluster (tested: 37 synthetic findings at a fixed bps
ratio collapse to one `Cluster` with `count == 37`) while a different bug
sharing the same `rule_id` at a different bps ratio does not merge.

**Incident:** see below — a latent circular import between
`core/models.py` and `core/exceptions.py`, exposed (not caused) by this
session's new code.

**Not built:** an `assay audit` command (only `explain` was asked for this
session); real persistence for posted journal entries; `EVIDENCE.md`
generation (unchanged, out of scope).

**Unsure about:** whether `RecomputedArithmetic`'s "one representative
example" reading is what "the recomputed arithmetic" was meant to promise —
flagged above rather than re-litigated unasked. The 50-bps delta-bucket
width is untuned against real data beyond `tests/test_exceptions.py`'s
synthetic cases; a real run's clusters haven't been eyeballed for over- or
under-merging.

## 2026-08-25 23:05 — core/models.py and core/exceptions.py had a latent circular import

**Symptom:** `tests/test_exceptions.py` failed to collect:
`ImportError: cannot import name 'CompiledContract' from partially
initialized module 'core.contract' (most likely due to a circular import)`,
the moment `core/exceptions.py` gained a module-level
`from core.contract import CompiledContract`.

**Diagnosis:** `core/models.py` has imported `from core.exceptions import
DiscrepancyClass` at its own top since 2026-08-23 (`Finding
.discrepancy_class`'s field type) — one-directional, harmless because
nothing in `core/exceptions.py` needed anything back from `core/models.py`.
This session's new clustering code genuinely needs `AssayModel`,
`PaisaAmount`, `EntityType`, `RecordRef` from `core.models` to build its own
Pydantic models and do real ledger lookups, which closed the cycle:
whichever of `core.models`/`core.exceptions` a test imported first, the
other was only partially initialized by the time the reverse import ran.

**First fix:** none attempted before diagnosis — traced directly from the
traceback.

**Whether it worked:** n/a.

**Final fix:** `core/models.py`'s `from core.exceptions import
DiscrepancyClass` moved from the top-level import block to immediately
before `class Finding(AssayModel):`, the only place it's used, so
`AssayModel`/`EntityType`/`RecordRef`/`PaisaAmount` are already defined by
the time it runs regardless of import order. `core/exceptions.py`'s own new
imports moved to after its `DiscrepancyClass` enum for the same reason;
`CompiledContract`/`Ledger`/`Finding` — used only as function-parameter type
hints inside `core/exceptions.py`, never instantiated or used as a Pydantic
field type there — became `TYPE_CHECKING`-only, removing two more legs of
the cycle entirely.

**Guard added:** none dedicated — the failure mode is structural (an
`ImportError` at collection time), not a value that could silently regress.
Exercised by the full 682-test suite passing clean afterward, including
files on both sides of the import ordering.

## 2026-08-25 23:28 — a duplicate finding id in one posting call could double-post

**Symptom:** found by a money-auditor review of `journal_entries_for_auto_findings`,
not a failing test in the wild. Asked to independently verify "can re-running
the same audit ever double-post"; the reviewer traced the dedup set and found
it is seeded once from `already_posted` and never updated as entries are
built within the same call.

**Diagnosis:** `posted_keys = {entry.idempotency_key for entry in
already_posted}` ran once before the loop. A `findings` argument naming the
same finding id twice — an upstream merge bug, or a caller passing one
finding in twice — passed the `key in posted_keys` check both times, since
neither iteration's key was ever added to the set. Two `JournalEntry` rows,
same `id`, same `idempotency_key`, were appended in one call. The function's
own docstring already re-checks the AUTO-lane filter "regardless of what the
caller already filtered" — this was the same class of caller-misuse the
function was supposed to be defensive against, just not actually covered for
duplicate ids. No existing test caught it: every idempotency test called the
function twice with `already_posted` threaded through correctly, none passed
a repeated id within one call.

**First fix:** none — the reproduction (two `Finding` objects sharing id
`FND-1`, different `amount_impact`, both AUTO lane, one call) made the cause
plain immediately.

**Whether it worked:** n/a.

**Final fix:** `posted_keys.add(key)` right after a new entry is appended,
inside the loop — so a duplicate id later in the same `findings` iterable is
caught by the same check that already covers cross-call reruns.

**Guard added:**
`test_a_duplicate_finding_id_within_one_call_does_not_double_post` in
`tests/test_ledger.py` — two findings sharing id `FND-1` (deliberately with
different `amount_impact`, so a bug would also be visible as which amount
"won"), asserts exactly one entry with one distinct `idempotency_key`.
Confirmed failing (2 entries) before the fix, passing after. Full 682-test
suite, `tests/test_architecture.py`, and `guard_core.py` all re-run clean
after the fix.

## 2026-08-26 00:21 — decompose_all interleaved tiers per credit; its own docstring said it didn't

**Symptom:** found by a `payments-domain` skeptical review, not a failing
test in the wild. Asked directly: "the assumption that breaks first against
real production data" and "three places money could be silently lost or
double-counted." The reviewer traced `decompose_all`'s own docstring —
*"A structural sweep over every credit first claims the records it can
account for with certainty; only then does subset-sum run, over the
residue"* — against what the code actually did, and found the two didn't
match.

**Diagnosis:** `decompose_all`'s single loop called `decompose()` once per
credit in id order, and `decompose()` itself cascades tier 1 (structural)
*then* tier 2 (subset-sum) for that one credit before the loop moves on.
That is "structural-then-subset-sum, per credit, in id order" — not "every
credit's structural sweep, then every remaining credit's subset-sum sweep."
A credit with no UTR (a dropped UTR — D09's own shape) that sorts earlier by
id runs its tier-2 subset-sum search against a pool that still contains a
later credit's own batch, because that later credit hasn't had its
structural turn yet. If the earlier credit's amount happens to sum against
that batch, it claims it — confidently, at tier 2, `RESOLVED`, low residual.
The later credit then finds its own batch already claimed, falls through to
a worse tier, and reports a fabricated discrepancy on the wrong credit,
often sign-flipped. Reproduced independently (not just taking the review's
word for it) before touching any code: two batches, STL-A (₹1,000.00, UTR
intact) and STL-B (₹995.00, UTR intact); BC-1 (id sorts first, UTR dropped,
amount coincidentally equal to STL-B's net) and BC-2 (id sorts second, UTR
intact, points at STL-B). Before the fix: BC-1 claimed STL-B at tier 2,
`residual=0`, `confidence=8000`; BC-2 lost its own batch, fell to tier 3,
paired with STL-A's leftover payment, reported `residual=-500` (₹5.00) on
the wrong credit. Nothing raised; both proofs individually verify.

This is the exact failure the module's own docstring (lines 16-28,
2026-08-24 22:35 above) says the design exists to make impossible: *"a clean
report over the wrong records, with nothing anywhere to indicate it."* It
did not show up on `runs/realistic-seed42` only because that dataset gives
every credit a distinct UTR pointing at a distinct batch (D09 corrupts 6
UTRs, but each corrupted credit's true batch happens to be uncontested by
anything else) — a one-credit-per-batch bijection no real gateway's
settlement files guarantee.

**First fix:** none attempted before diagnosis — the docstring/code mismatch
and the independent reproduction made the cause and the fix shape plain
together.

**Whether it worked:** n/a.

**Final fix:** `decompose_all` now runs two explicit, complete phases
instead of one interleaved loop. Phase 1 calls `_tier_structural` for every
credit, in id order, claiming as it goes; only credits that don't resolve
structurally (and aren't a terminal reason) carry over. Phase 2 then calls
`_tier_subset_sum` for exactly that residue, against the pool phase 1 left
behind. Tier 3 (assignment) is unchanged — it already reads from the
finished `proofs`/`claimed` state, not from the old loop's shape. The public
`decompose()` function itself is untouched: a standalone call against one
credit still cascades tier 1 then tier 2 in one call, which is correct and
separately tested behavior for examining a single credit in isolation. Only
`decompose_all`'s orchestration changed.

Re-ran the reproduction after the fix: BC-2 now resolves structurally to
its own batch (`residual=0`, `confidence=10000`); BC-1 falls to assignment
and reports the real `residual=-500` on the credit that actually has it.
Re-ran the committed `runs/realistic-seed42` decomposition directly (not
just via pytest) to confirm the fix doesn't disturb the one dataset this
project has real numbers for: tier counts (25 structural / 6 subset-sum),
outcome counts (31 resolved), zero double-claims, and total residual
(−101,501 paise) are all byte-identical to the pre-fix numbers recorded
2026-08-24 22:35 — confirming those 6 D09 credits' true batches were never
actually contested, so the bug was real and latent, not something the
demo data was already exercising.

**Guard added:**
`test_a_lower_id_credits_subset_sum_never_preempts_a_higher_id_credits_structural_claim`
in `tests/test_decompose.py`, built from the same reproduction. Confirmed
failing (`BC-2` resolved at `ASSIGNMENT`, not `STRUCTURAL`) before the fix,
passing after. Full 684-test suite, `guard_core.py`, and `ruff` all re-run
clean.

**Not fixed today, named by the same review, out of scope for a single
most-severe-issue fix:** a currency check missing before `cluster_findings`
sums `amount_impact.paise` across findings that could carry different
currencies; `core/contract.py` trusting the LLM's rate-card *reading*
with no independent second check beyond schema/overlap/completeness
validation, and no test asserting a compiled contract reproduces the
undisputed majority of a real settlement's own fees; `core/exceptions.py`'s
`rule_id` resolution reading the gateway's own self-reported rule id
(`fee_line.rule_id`) rather than the contract's own
`FeeBreakdown.matched_rule_ids`, which on the committed run's actual
generator output means every dispute packet's `contract_clause` and
`recomputed_arithmetic` are `None` — the two namespaces never intersect;
`core/verify.py`'s WON-chargeback-reversal match by amount alone, with no
use of `Adjustment.reason`/`settlement_id`, plausible to false-negative on
real data where round rupee amounts recur; `core/ledger.py`'s
`JournalEntry.id` (`f"JNL-{finding.id}"`, one-per-finding) versus its
`idempotency_key` (`sha256(input_hash:finding_id)`, one-per-(run,finding))
disagreeing about uniqueness, meaning two audit runs over a corrected
settlement file for the same finding produce two journal rows with the same
id and different keys, both intended-idempotent, actually duplicating the
posted amount; `core/verify.py`'s `_IdSeq` producing colliding `Finding` ids
across two proofs for one credit (documented as impossible by a
"BankCredit ids are unique" argument the function does not itself enforce);
`core/decompose.py` overwriting a good proof on a duplicated `BankCredit`
row in the input rather than rejecting the duplicate; `core/contract.py`
never checking `payment.amount.currency` against the rate card's declared
currency, so a foreign-currency payment gets priced by whichever INR-paise
band its minor-unit amount happens to fall into. All flagged to the user
directly, none guessed around.

## 2026-08-26 11:30 — `assay audit`, `assay eval`, `make evidence`: a generated EVIDENCE.md

**Built:** `cli/audit.py` — the end-to-end runner the engine never had
(`run_audit()` → `AuditReport`, canonical-JSON `report_hash`, derived
`audit_run_id`, honest degradation when the adjudicator is unreachable),
written test-first; `assay audit` and `assay eval` on the Typer app;
provider instrumentation (`llm/telemetry.py`, hit/miss counters on
`CachedProvider`, real `usage_metadata` plus 429/backoff counters on
`GeminiProvider`, `<hash>.usage.json` cache sidecars); citation-level
counters and a `check_references` switch on `llm/adjudicator.py`;
`eval/detect.py` (ground-truth matcher), `eval/harness.py` (the sweep),
`eval/ablate.py` (section 13), `eval/diagram.py` (SVG + PNG),
`eval/sweep.py`, `eval/evidence.py`, `eval/cli.py`;
`scripts/check_evidence.py`; the `evidence` and `eval` Makefile targets;
`matplotlib` added to `pyproject.toml` (asked first). 772 tests passing
(684 at session start), `ruff` and `guard_core.py` clean on everything
touched.

**`assay audit` takes a run directory, never a `--profile`.** The
verify-invariants skill has called `assay audit --profile clean` since
2026-08-23, and that command cannot exist: resolving a profile name means
generating data, which means `cli/` importing `datagen/` — invariant 5.
Flagged rather than worked around; the skill now says
`assay audit --run-dir runs/clean-seed42` and names the generator command
that produces it.

**`assay eval` reaches `eval/` through a function-local import.** Asked
first, three ways. `datagen/cli.py`'s own docstring says it was kept out of
`cli/` precisely "so quarantine can never be accidentally violated by a
shared import", which pointed away from a literal `assay eval` command.
Chosen: register the command, import `eval.cli` inside the function body,
and pin the property with a new test that walks `cli/`'s **module-level**
import graph transitively and asserts `datagen` is unreachable. Invariant
5's letter is about imports; its spirit is that the audit engine must never
see the answers, and the audit path (`assay audit`, `cli/audit.py`,
`core/`) has no path to `datagen/` at all.

**That guard test was written twice.** The first version walked the graph
at *package* granularity and immediately reported a leak: `cli/audit.py`
imports `eval.determinism` (which reads no ground truth and lives in
`eval/` only because it scores a run), and `eval/harness.py` imports
`datagen`. Python does not work that way — `import eval.determinism`
executes `eval/__init__.py` and that one module, not its siblings.
Rewritten to be module-precise, with a test proving it does *not* blame a
sibling that was never imported. A guard that cries wolf is a guard
somebody eventually deletes.

**The report hash excludes `elapsed_ns` but keeps `lane_assignment`,
diverging from `DecompositionProof.TELEMETRY_FIELDS`.** The plan said reuse
that frozenset. Reversed while writing it: the two hashes answer different
questions. `proof_hash` asks "why is this credit these transactions", which
must not move when a calibration artifact is supplied. `report_hash` asks
"what did this audit conclude", and a calibrated lane is part of the
conclusion — the report's own `findings` already carry one, so excluding
the proofs' copy would leave the hash inconsistent about whether the
artifact is an input. It is one; `calibration_sha256` records which.

**Two tables in section 2, not one.** Asked first.
`DiscrepancyEntry.code` (D01–D12) is finer than `DiscrepancyClass`:
D01/D03/D10/D11/D12 all plant a fee over/undercharge and D04/D06 both plant
a refund mismatch. A true positive is attributable to a code through the
records it touched; a false positive is not — there is no fact of the matter
about which code a spurious fee finding "should" have been. Rejected:
dividing class-level FPs among the codes that share the class (fractional
counts, an arbitrary attribution rule in the one column a judge scans
first). Recall lives in the per-code table where it is exact; precision in
the per-class table where it is.

**A finding on a real defect with the wrong class is a misclassification,
not a false positive.** One planted D01 changes a fee line *and* the tax on
it, so `core/verify.py` correctly emits a FEE_OVERCHARGE and a
TAX_MISCALCULATION, and only the first matches D01's own class. Charging
the second to "rupees falsely claimed" would bill the engine for noticing a
real consequence of a real defect; silently counting it as a success would
overstate recall. It gets its own counter, its own confusion-matrix cell,
and its own line in the report. This is the single most consequential
definition in `eval/detect.py` and it is stated in the module docstring and
in the document itself.

**No ablation required a change to `core/`.** The obvious implementation of
"without tier 2 and 3 decomposition" is a `max_tier` parameter on
`decompose_all` — an eval-only switch on the money path. Not needed:
`decompose_all` completes its structural phase over every credit before any
credit falls to tier 2 (the 2026-08-26 00:21 fix), so a structural result
never depends on what the later tiers did, and filtering the finished
proofs reproduces exactly the run tier 1 alone would have produced. The
other three ablations are a finding filter, a lane recomputation in
`eval/`, and the `check_references=False` keyword.

**Tokens are measured, not estimated — where a sidecar exists.** Asked
first. `CachedProvider` now writes `<hash>.usage.json` next to a cache entry
when the wrapped provider reports usage, and reads it back on a hit, so a
replay reports the *measured* tokens from the call that populated it. The
cache key is unchanged, so every already-committed entry stays valid; those
have no sidecar and fall back to a byte-length estimate that is labelled
`estimated` all the way out to the report. A token count that cannot say
which kind it is would be worse than none.

**`adjudication_degraded_kind` is a fixed vocabulary, not a message to
match.** The first version of `eval/sweep.py` counted schema rejections by
testing `"rejected" in report.adjudication_degraded_reason`. Replaced with a
`Literal["provider_unavailable", "schema_rejected"]` field: an absent
provider and a rejected response mean different things, section 10 counts
them separately, and a substring test is a silent mis-count the first time
an exception's wording changes.

**EVIDENCE.md carries the SHA-256 of its own body.**
`scripts/check_evidence.py` recomputes it and fails on any hand edit, wired
into `make guard`. It restates the banner format rather than importing it
from `eval/evidence.py` so it runs in a bare CI checkout with nothing
installed; a test asserts the two agree, because that duplication is a
drift risk. A generated accuracy report nobody can quietly touch up is the
whole point.

**Assumptions are printed next to the numbers they produced.** Paid-tier
token prices (section 10) and a manual reconciliation rate (section 11) are
things this project does not measure and cannot. They are named constants
in `eval/sweep.py`, and the rendered document states each one beside its
figure. The manual rate is deliberately generous to the human: a
pessimistic one would flatter the tool.

**`eval.cli render` exists so the document and the measurements are
separate concerns.** Re-renders EVIDENCE.md from a committed
`eval/results/sweep.json` in a second, rather than re-running 18 audits to
fix a table heading. It also means the committed EVIDENCE.md is provably a
rendering of the committed sweep, not of some other run.

**Incident:** see below — the "without the reference checker" ablation
crashed `_coverage_bps`, exposing that it depended on its caller having
already reference-checked.

**Not built:** a refund verifier. This is the eval's largest single
finding and it is deliberately left alone: `core/verify.py` checks fee,
tax and chargeback reversals and has no refund check at all, so D04
(refund deducted twice) and D06 (refund deducted when not due) are missed
entirely — every planted instance, across every profile and seed. Building
a new detector is a different task from measuring the ones that exist, and
it touches the money path. Reported in the document rather than quietly
fixed. Also not built: persistence for posted journal entries; contract
sign-off gating in `run_audit`; a `demo` Makefile target.

**Unsure about:** whether the false-positive definition (a finding that
touches no planted record) is the one a payments reviewer would choose —
the alternative, "any finding a merchant could not successfully dispute",
is not computable from planted truth. The assumed paid-tier prices are
plausible list rates, not quotes. And 6 seeds per profile is enough to see
the shape of the per-class numbers and not enough for a tight interval on
D03/D05, which plant as few as one instance per month.

## 2026-08-26 11:30 — the reference-checker ablation crashed on the citation it was built to allow

**Symptom:** `test_disabling_the_reference_checker_lets_a_fabricated_citation_through`
and `test_even_an_unchecked_fabricated_citation_cannot_produce_an_amount`
both failed with `AttributeError: 'NoneType' object has no attribute
'RECORD_TYPE'`, raised from `core/conserve.py`'s `_sign_of` via
`llm/adjudicator.py`'s `_coverage_bps`.

**Diagnosis:** `_coverage_bps` did `record = ledger.get(ref)` and then
`signed_paise(record)` inside a `try` that caught only `ValueError` — the
"valid but non-contributing citation" case. `Ledger.get` returns `None` for
a ref that does not exist, and `signed_paise(None)` raises `AttributeError`,
not `ValueError`. The function was silently depending on its caller having
already removed nonexistent refs, which `_process_hypothesis` did do. So the
bug was unreachable on the audit path and became reachable the moment the
ablation asked what would happen if citations were trusted — which is
exactly the question the ablation exists to ask.

**First fix:** none attempted before diagnosis; the traceback named the
line.

**Whether it worked:** n/a.

**Final fix:** `_coverage_bps` skips a ref the ledger cannot resolve,
explicitly, with a comment saying why it must not depend on its caller. The
parameter was renamed `valid_cited` → `cited`, because it is no longer
entitled to assume they are valid. Invariant 2 still holds in the ablated
path: an unresolvable citation contributes 0, so even a trusted fabricated
citation cannot produce an amount — asserted directly by the second test.

**Guard added:** both tests above, in `tests/test_adjudicator.py`. Confirmed
failing (`AttributeError`) before the fix, passing after.

## 2026-08-27 01:20 — the adjudicator's evidence pool offered a proof's own already-counted terms as evidence for its own residual

**Symptom:** tracing the false positives EVIDENCE.md's §4 reported (100% of them adjudicator-sourced) to a specific example: finding `FND-ADJ-...-RES-BC-000010` cited `ADJ-RH-000010`, a `reserve_hold` adjustment, as an `undocumented_adjustment` explaining a residual — at 10,000 bps ("arithmetic coverage"). `ADJ-RH-000010` was already claimed by that same credit's own proof; it was one of the records already summed into `sum_paise` when the proof was built.

**Diagnosis:** `llm/adjudicator.py::_decomposition_evidence_pool`, for a RESOLVED proof, returned `[term.ref for term in proof.terms]` — the proof's own claimed records — as the candidate evidence pool for that proof's own residual. This is circular by construction: `residual_paise = credit_paise - sum(term.signed_paise)` is defined as exactly the part the terms do NOT explain, so a term already inside that sum cannot also be evidence for the part outside it. `_coverage_bps` then does exactly what it is supposed to do — sum `signed_paise` over the cited (valid, reference-checked) refs and report how much of the residual that covers — and correctly reports 100% for a citation whose own magnitude happens to be large enough, with no way to know the citation was drawn from the sum that produced the residual in the first place. The reference checker (invariant 6) is not implicated: the citation was real and in the shown pool; the pool itself was wrong.

**First fix:** none attempted before diagnosis — re-reading `_decomposition_evidence_pool` against the definition of `residual_paise` made the circularity evident directly.

**Whether it worked:** n/a.

**Final fix:** a RESOLVED proof's evidence pool is now unconditionally empty — nothing else is known to be relevant to its residual, so `residuals_from_run` reports it as `skipped_no_evidence` (an honest "we don't know" is the same principle that already governs an empty pool elsewhere in this module) rather than sending a model a pool it can never validly cite. Generalized the same rule to AMBIGUOUS proofs' competing candidates: a candidate NOT claimed by THIS proof may have been claimed by a DIFFERENT proof in the same run, in which case it is exactly as accounted-for and excluded the same way. `residuals_from_run` now computes `claimed = {term.ref for proof in proofs for term in proof.terms}` once, matching `core.conserve.unclaimed_records`'s own definition, and threads it through. No signature change was needed for callers — `residuals_from_run`'s existing `proofs` argument was already enough.

Checked empirically on the committed run (`runs/realistic-seed42`): before the fix, `assay audit` posted 5 residuals to the adjudicator (1 live API call after cache warm-up); after, 0 of those 5 are RESOLVED-proof residuals with only self-referential evidence, all correctly reported as unexplainable by the model rather than guessed at.

**Guard added:** `tests/test_adjudicator.py` — `test_a_resolved_proofs_own_terms_are_never_offered_as_evidence_for_its_own_residual`, `test_an_ambiguous_proofs_competing_candidate_already_claimed_by_another_proof_is_excluded`, and a regression test that genuinely unclaimed competing candidates are unaffected. The first existing test this replaced (`test_residuals_from_run_builds_one_case_per_nonzero_conservation_residual`) was directly asserting the old, circular behavior (`evidence_pool == proof's own terms`) and was rewritten rather than left pinning a bug.

## 2026-08-27 01:20 — `core/verify.py` gained a refund duplicate-deduction check; D06 stays undetected, on purpose

**Built:** `core/verify.py::_refund_findings`, wired into `verify()`. Catches D04's shape (a refund deducted from a settlement an extra time, with no ledger record to show for it): when a RESOLVED proof's residual is negative and its magnitude exactly equals ONE of the proof's own REFUND terms, unambiguously, report `REFUND_AMOUNT_MISMATCH`. Two refunds sharing the residual's exact amount, or a residual shared with another discrepancy on the same proof, are left unexplained rather than guessed — the same ambiguity-beats-guessing discipline `core/decompose.py` already applies to a subset-sum tie. Confirmed against the real committed run: `REF-000118`, ₹198.73, matches ground truth's own D04 entry exactly (code, class, amount, batch).

**This crosses a boundary the module's own docstring had explicitly drawn.** Before today, `core/verify.py`'s module docstring said, in so many words: "NOT in scope: REFUND_AMOUNT_MISMATCH as a per-line check — there is no independently recomputable 'correct' refund amount ... to diff against." That statement is still true and unchanged — `_refund_findings` is not a per-line diff against a recomputed value, the way the fee/tax check is. It is the same amount-matching pattern the chargeback-reversal check just above it already uses (match by amount, because the defect leaves no other trace), applied to a residual instead of a ledger-wide pool. CLAUDE.md's working agreement is explicit — "When you hit an ambiguity in payments domain logic, ask. Do not invent a business rule and bury it in a function" — and finding a prior, deliberate, documented exclusion of exactly this question raised the bar past a routine judgment call. Proceeded anyway, on the strength of: the user's own explicit instruction to close this exact gap after being shown EVIDENCE.md's §14 finding it; the mechanism being a generalization of an already-reviewed pattern in the same file, not a new one; and the reasoning, scope, and one real limitation (below) being written down in full rather than buried. Flagged here rather than silently worked around.

**D06 (a refund attributed to the wrong settlement batch) is not attempted.** Unlike D04, it leaves NO residual at all — both the true and the wrong batch fully "verify" against their own (corrupted) record sets, which is precisely the "clean report over the wrong records" failure `core/decompose.py`'s own module docstring names as the reason its tiers run in two complete phases rather than interleaved. There is no per-credit arithmetic signal to check. The one rule that suggests itself — "a refund's settlement_id must match its own payment's" — is not safe: `datagen/config.py`'s `refund_lag_days` spans up to 14 days, routinely crossing a monthly cycle boundary, so an ordinary, correct late refund would trip that rule and produce a false positive on legitimate data. No check is implemented for this class; `eval/evidence.py`'s §14 table says so with this same reasoning rather than the previous (now-inaccurate for D04) blanket "no refund check" line.

**Guard added:** `tests/test_verify.py` — six new tests (exact match, zero residual, opposite-direction residual, no match, two same-amount refunds, coexistence with a fee mismatch on the same proof) plus a calibration test matching the existing chargeback-calibration pattern. `core/verify.py`'s own module docstring rewritten to state both new rules' scope and reasoning where the fee/tax and chargeback rules already state theirs.

**Re-verification after both fixes**, `runs/realistic-seed42`:
- deterministic-engine findings: 48 → 49 (the new, real refund catch)
- `tests/test_cli_audit.py`'s pinned "committed run matches DECISIONS.md" count updated 48 → 49, with the reasoning cross-referenced here
- `tests/test_cli_audit.py`'s degraded-vs-available comparison test could no longer rely on the committed disk cache (evidence pools changed shape, so cached prompts miss) and was rewritten against a small in-test fake provider (`_AlwaysExplainsProvider`) that answers any residual's own rendered pool, rather than depending on what happened to be cached from a prior session

## 2026-08-27 23:10 — `core/decompose.py` could double-cite a duplicated fee/tax line into one proof's terms

**Symptom:** none observed in a live run — found by a `chaos-engineer` subagent review requested before building the chaos/ suite's "duplicate settlement file ingested twice" scenario, tracing the already-acknowledged (2026-08-26 00:21 entry) "`Ledger`'s duplicate-record-id collision" gap through to a specific, previously-unnoticed consequence in `decompose.py`.

**Diagnosis:** `core/ledger.py::Ledger.__init__` did `self._by_ref[ref] = record` unconditionally: a second record sharing a `(type, id)` silently last-write-won in the lookup dict, while every join index (`_by_type`, `_by_settlement`, `_by_utr`, `_fee_lines_by_target`, `_tax_lines_by_fee`) still appended the ref a second time regardless of the collision. If a duplicated `FeeLine` reached `Ledger()` this way, `ledger.fee_lines_for(payment_ref)` returned that ref twice. `core/decompose.py::_sub_units_of_batch` (split-settlement) and `_orphan_units` (unsettled payments) both fold `fee_lines_for()`'s result straight into a `DecompositionProof`'s `terms` with no dedup — unlike `_batch_member_refs` (the whole-batch path), which already does `sorted(set(refs))` — so the duplicated line's amount would be double-counted in `sum_paise`, silently changing the money a proof claims a bank credit resolves to. `core/decompose.py::verify_proof()` already re-derives every term from the ledger and explicitly checks `if term.ref in seen` for exactly this — but it was never called anywhere in `cli/audit.py::run_audit()`, only from tests and `eval/determinism.py`'s own docstring. The one check that would have caught this in a real run was dead code on the production path.

**First fix:** none attempted before diagnosis.

**Whether it worked:** n/a.

**Final fix:** three pieces, all test-first.
1. `Ledger.__init__` now checks every incoming record against what is already indexed under its `(type, id)`. Identical content (a byte-identical re-ingestion — e.g. a settlement file handed to the loader twice) is deduped: skipped before touching any index, logged via `structlog` (`duplicate_record_ingested`). Conflicting content under the same id raises a new `DuplicateRecordError` naming the ref, rather than guessing which copy is real. This makes the `decompose.py` double-citation exposure structurally impossible — a duplicated record can no longer enter `_by_ref` or any derived index twice — with no change needed in `decompose.py` itself.
2. `core/decompose.py` gained `ProofIntegrityViolation`; `cli/audit.py::run_audit()` now calls `verify_proof()` on every proof immediately after `decompose_all()`, raising it (uncaught — the engine's own arithmetic can't be trusted, same class as `core/conserve.py`'s `ConservationViolation`, not a business condition to degrade from) on any failure. Reactivates the existing tamper/duplicate-citation defense in production, independent of the `Ledger` fix, for any future proof-construction path that bypasses a clean `Ledger`.
3. Same review, same acknowledged gap: `JournalEntry.id` (was `f"JNL-{finding.id}"` alone) now incorporates the idempotency key (`f"JNL-{finding.id}::{key[:8]}"`) — closes the named case where two runs over a *corrected* settlement file (same `finding.id`, different `input_hash`, hence a legitimately different `idempotency_key`) produced two journal rows sharing an `.id` but disagreeing `.idempotency_key`, a primary-key collision on two genuinely distinct postings.

**Guard added:** `tests/test_ledger.py` — `test_an_identical_duplicate_record_is_deduped_not_double_indexed`, `test_an_identical_duplicate_fee_line_does_not_get_cited_twice` (the direct regression test for the `decompose.py` exposure), `test_two_records_sharing_an_id_with_conflicting_content_raises`, `test_two_different_input_hashes_never_collide_on_journal_entry_id`. `tests/test_cli_audit.py::test_a_tampered_proof_aborts_the_run_rather_than_reporting_a_wrong_number` monkeypatches `decompose_all` to return a proof with one term's `signed_paise` incremented by 1 paise post-hoc and asserts `run_audit()` raises `ProofIntegrityViolation` rather than completing. A `money-auditor` review of the diff before commit confirmed the fix (Pydantic frozen-model equality is exact for the `AssayModel` subclasses involved; the dedup `continue` fires before any index is touched, so no partially-populated-index state is reachable) and separately caught `eval/ablate.py::structural_only_findings` still doing `list(verify_all(...))` against `verify_all`'s new two-tuple return (an unrelated, same-session change) — fixed, with a new regression test, same pass.

## 2026-08-27 23:10 — built the `chaos/` failure-injection suite (12 scenarios + 9b + one more the review surfaced)

**Built:** 18 pytest tests under `chaos/`, each writing a structured pass/fail record via a new `chaos/incident.py` (`chaos_scenario` context manager → `chaos/incidents/<id>.json`), read back into one table by a new `scripts/chaos_report.py`, wired into a new `make chaos` target. Covers: C01 duplicate settlement record (identical → deduped, conflicting → `DuplicateRecordError`), C02 process killed mid-audit (real subprocess `SIGKILL`/`TerminateProcess`, not an in-process simulation) + C02b a truncated cache file on a killed mid-write, C03 out-of-order chargeback reversal, C04 refund landing in a later settlement cycle, C05 mid-cycle rate revision against a hand-computed fee, C06 truncated UTR falling through tiers, C07 a fabricated LLM citation end-to-end through a real audit, C08 malformed-JSON-then-valid retry (+ C08B persistent malformed response) end-to-end, C09 LLM entirely unavailable end-to-end, C09b sustained/recovering 429 rate limiting, C10 float contamination caught by `scripts/guard_core.py`, C11 a silent 1-paise corruption (both the recomputable-field and non-recomputable-field shapes), C12 a payment with no covering contract clause.

Three of the twelve requested scenarios (2, 8, 12) had no real graceful behaviour to test yet — building them honestly required new production code, not just tests:
- **Scenario 2** (checkpoint/resume): new top-level `store/` package (`store/models.py`, `store/resumable.py`) — SQLite via `sqlmodel` (already a declared dependency). `store/resumable.py::resume_or_run()` wraps `cli.audit.decompose_phase()`/`cli.audit._adjudicate()` (both reused unchanged, so `run_audit()` itself stays byte-for-byte untouched — `eval/harness.py`/`eval/sweep.py` call it directly and repeatedly, including sweep's own back-to-back determinism check) with a durable `audit_checkpoints` table (phases `decompose_done → adjudicate_done → journal_done → report_done`) and a durable `journal_entries` table with `idempotency_key UNIQUE` — the actual double-post guard, not just the pure function's in-memory dedup. `cli/__init__.py`'s `audit` command now calls `resume_or_run()` instead of `run_audit()` directly.
- **Scenario 8** (malformed-JSON retry): new `llm/provider.py::MalformedResponse(ProviderUnavailable)`; `llm/providers/gemini.py::_decode`'s three raise sites now raise it specifically; new `llm/providers/retrying.py::RetryingProvider` (parallel to `CachedProvider`, not baked into `GeminiProvider` — keeps the provider boundary vendor-agnostic) retries only `MalformedResponse` with the same backoff+jitter formula (extracted into `llm/providers/backoff.py`, shared with `GeminiProvider`'s own 429 loop so the two never compound into an unpredictable total attempt count). `cli/loaders.py::default_provider()` now returns `CachedProvider(RetryingProvider(GeminiProvider()))`.
- **Scenario 12** (missing contract clause): `core/verify.py` gained `ContractGap` (not a `Finding`/`DiscrepancyClass` — there is no computable `amount_impact`, and inventing one would be exactly the "fall back to a default rate" the scenario forbids); `fee_tax_cells()` catches `NoApplicableRule` per payment and appends a gap instead of aborting the proof. `verify()`/`verify_all()`/`fee_tax_cells()` all changed return shape to include `gaps` alongside `findings`/`cells`, cascading through `cli/audit.py` (new `AuditReport.contract_gaps` field), `cli/explain.py`, `eval/harness.py`, `eval/calibrate_lanes.py`.

A fourth gap the review surfaced but wasn't in the original 12: `llm/providers/cached.py`'s cache-hit read and `core/contract.py::load()`/`load_signoff()` all raised a raw `JSONDecodeError`/`ValidationError` on a truncated file (the realistic shape of "killed mid-write" for a *file*, as opposed to scenario 2's process-level kill). Fixed: a torn cache entry now degrades to a re-fetch (self-healing); a torn compiled-contract file raises a new, named `CorruptContractArtifact`; a torn sign-off file is treated as no sign-off (re-prompts a human, matches the function's existing "or None" contract for a missing/stale one).

**Not built:** a `--retry-adjudication` flag to force-invalidate a stale degraded checkpoint; checkpoint-row retention/pruning; protection against two concurrent audits of the same `audit_run_id` beyond what SQLite's own locking already gives. `resume_or_run()` assumes `adjudicate`/`calibration`/`merchant_id`/`contract` don't change across resume attempts for the same `audit_run_id` — undocumented if it did, matching `run_audit()`'s own lack of a promise about being called twice with different flags.

**Unsure about:** whether `chaos/`'s 18-scenario count (vs. the requested 12) should be trimmed back to exactly 12 for the demo, or whether C02b/C08B/C09b's split-out sub-cases read as scope creep rather than thoroughness.

## 2026-08-27 23:55 — `ingest/`: real Razorpay data into a run directory

**Built:** a new top-level `ingest/` package — `razorpay.py` (REST client), `mapping.py` (vendor JSON → `core.models`), `cli.py` (`assay fetch`) — plus `httpx` as a declared dependency. It writes the same four files `datagen/writer.py` writes, so `assay audit --run-dir` consumes a fetched month with no knowledge of where it came from and `run_audit()` is unchanged.

**The API stays on the far side of the file boundary, deliberately.** A live API is not a reproducible input, so wiring it into `run_audit()` would have cost invariant 4. `ingest` fetches once and writes files; the audit then replays those files byte-identically forever. This is the same shape as `datagen/` — synthetic run dirs there, real ones here, one audit engine behind both.

**Three decisions taken to the user rather than assumed, all four of which were load-bearing:**

1. **The bank statement is the gateway's own claim, and the manifest says so.** `bank_statement.json` is built from `/v1/settlements` — Razorpay attesting what Razorpay paid. Every fee, tax, refund and adjustment finding is unaffected, because `core/verify.py` recomputes those against the contract independently; but an underpayment Razorpay *reported correctly* is invisible to a run built this way. The run manifest carries `bank_statement_source: "gateway_self_reported"` and `assay fetch` prints the limitation, so a report can never quietly claim an independence it does not have. Supplying a real bank statement is the open follow-up.
2. **Multi-component fees are quarantined, not blended.** Razorpay reports one `fee` per transaction; the contract recomputes MDR and INTERNATIONAL (or EMI subvention) separately, and `core/verify.py` diffs fees aggregated *by FeeType*. A blended fee typed as MDR would emit a pair of equal-and-opposite fictional findings on every international transaction. International and EMI rows are therefore excluded with a named reason rather than mapped — an honest "not audited, here is why" over a confident wrong number, the same discipline `core/decompose.py` applies to an ambiguous subset-sum.
3. **Card networks outside Assay's enum are quarantined, not coerced.** Razorpay returns Maestro, Diners Club and Unknown; `core.models.Network` has four members and none of them fit. Coercing to the nearest would be inventing data about money.

**The fee/tax convention is read from the data, not assumed.** Razorpay documents the Payments API's `fee` as *including* GST while the recon report carries `fee` and `tax` as separate columns — and getting it backwards misstates every fee line in a run by exactly the tax. `detect_fee_convention()` reads each row's own published `credit`: `credit == amount - fee - tax` means `fee` is pre-tax, `credit == amount - fee` means it already contains it. Only rows with a non-zero tax discriminate; a row agreeing with neither is quarantined rather than forced. This was the one place a plausible guess would have produced a silently wrong number in every direction at once.

**Not fetchable, by nature rather than omission:** `rate_card.md` (no gateway exposes your *negotiated* contract as data — precisely why `llm/contract_parser.py` exists; `--rate-card` supplies it and `fetch` refuses to run without one) and `mcc` (a merchant-level attribute, absent from every endpoint, so a required CLI option rather than a per-payment invention).

**Not built:** chargebacks (`/v1/disputes` is a separate integration and not enabled on all accounts); a real bank-statement parser; `Adjustment.kind` beyond manual credit/debit inferred from the direction the money moved (Razorpay reports no sub-type, so a reserve hold and a manual debit are indistinguishable here and the manual form is recorded rather than a guessed reserve).

**Guard added:** `tests/test_ingest_mapping.py` (22 tests — exact-paise mapping, both fee conventions and the row that matches neither, every quarantine reason, and a netting test asserting the mapped records sum exactly to the settlement amount), `tests/test_ingest_razorpay.py` (16 tests through a real `httpx.MockTransport` — auth header, pagination including the recon endpoint's larger page size, a bounded 429 retry, fail-fast on non-429, and two tests asserting the key secret reaches neither an exception message nor a transport error), `tests/test_ingest_cli.py` (9 tests, ending in a real `run_audit()` over a fetched directory asserting `total_unaccounted_paise == 0` — the only assertion that actually proves the mapping produces records that reconcile).

**Verified against the live API**, read-only, with the test-mode credentials in `.env`: all three endpoints authenticated and returned HTTP 200 over 3 requests. Zero records, as expected for a test account with no transactions — so the mapping is proven against fixtures and the transport is proven against the real service, but the two have not yet been proven together on real data. Creating test transactions in Razorpay to close that gap is a write to an external account and was not done.

**Incident found while writing the end-to-end test, in this session's own earlier work:** the first version of the fetched-run audit reported zero findings, which I had asserted should be non-empty. The cause was a fixture dated 2025-07 against a rate card effective 2026-07 — so no clause was in force and `core/verify.py` reported two `ContractGap`s and priced nothing. That is the Part-2 `NoApplicableRule` fix from earlier today behaving exactly as designed on real-shaped ingested data, reached through a path chaos scenario C12 only covers with a hand-built proof. Fixture dates corrected, and the behaviour pinned deliberately in `test_a_payment_outside_the_contracts_effective_window_is_a_gap_not_a_crash` rather than left as an accident.

## 2026-08-28 00:15 — ingest reported ₹4,858.40 as unexplained money that was never missing

**Symptom:** a probe run — one ordinary UPI payment and one international card payment in the same settlement — audited to `total_unaccounted_paise: 485840`. Nothing was missing. That is exactly the net contribution of the international payment, which `ingest/mapping.py` had quarantined as unrepresentable.

**Diagnosis:** quarantine was decided per **row**; the bank credit was emitted per **settlement**. So a settlement with one unmappable transaction produced a credit for the full payout against only the records that mapped, and `core/conserve.py` correctly reported the difference as `unexplained_paise`. The bucket the whole product asks to be trusted — "never rounded away, never silently absorbed" — was carrying a tooling artefact, which also means a real shortfall of similar size would have been unfindable next to it. Any merchant taking international cards would see most of every payout quarantined and the report would read as a catastrophe. Found by writing the probe during a review, not by a failing test: every ingest test written the day before mapped 100% of rows, so the end-to-end test asserting `total_unaccounted_paise == 0` passed for the wrong reason and would have passed with this bug present. Second instance of the same class, found the same way: rows whose settlement produced no credit at all (unprocessed, or settled outside the fetch window) were emitted as records no credit claimed, landing in the other phantom bucket, `unclaimed_paise`.

**First fix:** considered reducing the `BankCredit` amount by the quarantined net, and rejected it before writing any code — that fabricates a payout that never happened. Razorpay paid ₹5,840.70; the tool may not claim it paid ₹984.30. Also considered emitting the credit with the quarantined amount recorded alongside it for consumers to subtract, and rejected that too: it still hands `decompose.py` a partially-explained credit, and every reader of `total_unexplained_paise` (EVIDENCE.md, the exception report, the AUTO lane) would have to remember the correction. One right number beats N call sites remembering to subtract.

**Whether it worked:** n/a — neither was implemented.

**Final fix:** a settlement is audited whole or not at all. `map_run` now maps settlements first to learn which produce a credit, stages mapped records per settlement instead of into flat lists, and drops any settlement with at least one unmappable row — its batch, its credit, and its already-cleanly-mapped transactions — quarantining those under a new `SETTLEMENT_INCOMPLETE`. The dropped payout is reported as `MappedRun.unaudited_paise`, carried into the run manifest and printed by `assay fetch` as "NOT AUDITED". Deliberately a different claim from unexplained: not "we looked and cannot account for it" but "we did not look, here is how much." Re-ran the original probe: 485,840 phantom paise → 0, with 584,070 paise reported as unaudited.

**Guard added:** `tests/test_ingest_mapping.py` — `test_a_settlement_with_one_unmappable_row_is_dropped_whole` (the direct regression: the cleanly-mapped payment goes too, and `unaudited_paise` carries the payout), `test_one_settlements_bad_row_never_costs_a_different_settlement`, `test_a_row_whose_settlement_produced_no_credit_is_not_left_unclaimed`. `tests/test_ingest_cli.py::test_quarantining_a_transaction_never_invents_unexplained_money` runs a real `run_audit()` over a fetched directory containing a quarantined transaction and asserts `total_unaccounted_paise == 0` — the invariant nobody had written down: **quarantining a transaction must never change the unexplained total.** `test_quarantined_rows_are_written_out_for_inspection` was pinning the old behaviour (its transfer row shared `setl_1`, so under the fix the whole payout correctly drops); rewritten to put the transfer in its own settlement, preserving its original intent.

**Reviewed and NOT fixed today, one issue at a time, in severity order:**
- `ingest/mapping.py::_int()` truncates a float silently: `int(1500.7) == 1500`. Razorpay returns integers today; the first money field that arrives as a JSON float is truncated at the ingest boundary before `Money` ever sees it. Invariant 1's ban and `tests/test_architecture.py`'s AST walk both cover `core/` only, and `ingest/` is outside both.
- `detect_fee_convention()` settles a run-wide money question by bare majority, with no threshold, no minimum sample, and a silent tie-break to `FEE_EXCLUDES_TAX`. An account genuinely mixing conventions has half its rows misread and the report never says so.
- `PAYMENT_LOOKBACK = 35 days` is a guess. Real merchants have T+7 cycles, reserves released 90+ days out, disputes resolving months later. The assumption most likely to break first against production data.
- `mcc` is one CLI constant stamped on every payment; a merchant with more than one business category gets MCC-scoped clauses pricing the wrong transactions.
- `Ledger`'s identical-duplicate dedup (added yesterday) would swallow a gateway that genuinely charged the same fee line twice under the same id. Checked `datagen`'s D11: it allocates a fresh fee id, so no planted discrepancy is hidden and the eval numbers are unaffected. Real-data risk only.

**Where a model's output reaches a number, since it was asked:** `llm/contract_parser.py` is the material exposure, and larger than "a human signs off once" implies — the compiled contract **is** the recomputation baseline, so every `FEE_OVERCHARGE` is `reported − recomputed` where `recomputed` traces to a `rate_bps` a model extracted from prose. A misparsed 1.75% as 1.95% produces confident, precise, wrong findings across every transaction, undetectable downstream because the arithmetic is flawless over a poisoned input. Second path: `core/lanes.py`'s calibration artifact is fitted on `datagen`-labelled runs, so AUTO-lane thresholds — which gate automatic journal posting — are calibrated against synthetic error distributions. `llm/adjudicator.py` is clean: it proposes, cites, is reference-checked, and never posts.

## 2026-08-29 00:20 — `assay report <run_id>` served a report.json belonging to a different run

**Symptom:** pointing the new reviewer UI at the audit store showed `report_hash 9effcbcd…` for run `AUD-dd4ba0399a94`, while `AuditRunRow.report_hash` for that same run_id recorded `5bc692d4…`. Both numbers were real; they described different runs.

**Diagnosis:** the run_id → run_dir mapping lives in the audit store, the artifact lives in the run directory, and nothing tied the two together. `cli/report.py::locate_run` looked up the row, read `<run_dir>/report.json`, and returned it without checking it was that row's report. The two drift whenever the same run_dir is audited again into a *different* `ASSAY_STORE_PATH` — which my own test suite was doing, auditing the committed `runs/realistic-seed42` in place with a throwaway store, rewriting the artifact while the developer's real `.assay/` store kept the original hash. So `assay report` and `assay replay` could both print a report whose own embedded hash contradicted the run they were asked for: the exact substitution the hashing exists to make impossible. Found by reading the UI's output against the store, not by a failing test — every existing test wrote both row and artifact in one invocation, where they cannot disagree.

**First fix:** none attempted. The check is one comparison and there was no plausible alternative worth trying.

**Whether it worked:** n/a.

**Final fix:** `locate_run` now compares the artifact's `report_hash` to the store's and raises a new `ReportArtifactStale`; `assay report`, `assay replay` and the reviewer API all refuse on it. Both values are written from the same report object in the same `assay audit` invocation, so in normal operation they cannot differ — a difference means the artifact is not that run's. Separately, `tests/test_reviewer_api.py` now copies the committed run directory to `tmp_path` before auditing it, so no test can rewrite a working-copy fixture again.

**Guard added:** `tests/test_cli_report.py::test_report_refuses_an_artifact_that_belongs_to_a_different_run` and `tests/test_cli_replay.py::test_replay_refuses_an_artifact_that_belongs_to_a_different_run`. `test_replay_fails_cleanly_when_the_stored_hash_was_tampered_with` was rewritten: it exercised replay's FAIL branch by editing report.json, which the new guard now catches earlier and which was testing artifact substitution rather than the non-determinism that branch is about. It now diverges the recomputation itself.

## 2026-08-29 00:35 — all three score cards disappeared from the reviewer page

**Symptom:** after wiring the gauge sweep to fire on scroll, the "Headline ratios" section rendered its heading and lede and then nothing. No console error. The API returned all three cards correctly (`/api/runs/<id>/scores` → 200, three objects).

**Diagnosis:** I gated the card's *mount* on an `IntersectionObserver`, and observed a wrapper carrying `display: contents`. That property generates no layout box, `IntersectionObserver` observes boxes, so the callback never fired, `inView` stayed `false`, and the card was never rendered. The `contents` wrapper was itself load-bearing for an earlier fix — it makes the card the direct grid child so `h-full` reaches it — so the two changes were individually correct and only broke in combination.

**First fix:** none — the diagnosis was the fix.

**Whether it worked:** n/a.

**Final fix:** the card always renders and starts at `opacity-0`; only the animation is gated on `inView`, and the observer targets the grid, which has a real box. Same visible behaviour, no mount dependency on a callback that may never fire.

**Guard added:** `tests/test_reviewer_api.py::test_score_cards_are_never_mounted_conditionally_on_being_in_view`, which asserts the component contains no `{inView && ` and that the observer targets the grid. Narrow, and honestly so: it pins this specific trap rather than the general class, because the repo has no DOM test runner and adding one to catch it was not worth the dependency.

## 2026-08-29 16:02 — README overstated what the conservation identity proves

**Symptom:** README.md's headline results claimed "conservation holds to the paisa" as evidence of correct payment, and Limitations said the identity "holds exactly on data engineered to break it."

**Diagnosis:** flagged by a `payments-domain` review of the finished README. `core/conserve.py`'s own module docstring (lines 129-136) states `unexplained_paise` checks only the settlement report's self-consistency, not correctness against the contract — a report reconciles perfectly while every fee on it was computed off the wrong clause. Confirmed against EVIDENCE.md: the fee/tax mispricing classes (D01/D02/D03/D10/D11/D12, ₹30,872.45) plus the misattributed-refund class D06 (₹121,623.29) — ₹152,495.74 of ₹415,863.64 planted, 36.7% — leave `unexplained_paise` at exactly ₹0.00. The Limitations line also silently cited the clean profile's ₹0.00 rather than the engineered profiles' actual ₹47,857.95 unaccounted (EVIDENCE.md §7).

**First fix:** none attempted — the docstring plus the EVIDENCE.md numbers made the correction plain immediately.

**Whether it worked:** n/a.

**Final fix:** rewrote the headline bullet and the Limitations line in README.md, and added the same bound to docs/architecture.md's conservation section: conservation is a self-consistency check on the report's own numbers, not a correctness check against the contract; `core/verify.py` is what catches a bank credit computed correctly off a wrong contract clause.

**Guard added:** none — this is prose, not code; no test enforces README/docs accuracy. Accepted risk: a later edit could reintroduce the same overclaim with nothing to catch it.

## 2026-08-30 01:52 — Vercel rejected vercel.json before any build line ran

**Symptom:** `Invalid request: should NOT have additional property `//`. Please remove it.` on the project import screen. No build log.

**Diagnosis:** the root `vercel.json` used `"//"` as a comment key to explain why the two projects differ only by Root Directory. Vercel validates `vercel.json` against a schema and rejects any property outside it. `$schema` is allowed; `//` is not.

**First fix:** none attempted — the error names the property.

**Whether it worked:** n/a.

**Final fix:** removed the key. The reasoning it carried moved to `docs/deploy.md`, which can hold prose.

**Guard added:** `tests/test_site.py::test_vercel_configs_carry_no_comment_keys`, checking both configs against the properties Vercel accepts.

## 2026-08-30 00:02 — .vercelignore deleted the landing page's own package.json

**Symptom:** landing build failed 3s in, at `Running "install" command: 'npm ci'`. Log line 7: `Found .vercelignore (repository root)` / `Removed 156 ignored files`.

**Diagnosis:** `site/` was listed in `.vercelignore` to slim the reviewer's function bundle. There is one `.vercelignore` and Vercel applies it at upload, before Root Directory — so it also removed `site/package.json`, and `npm ci` ran with no input.

**First fix:** removed `site/` from `.vercelignore`.

**Whether it worked:** yes.

**Final fix:** as above. Verified by copying only the committed `site/` files into an empty directory and running `npm ci && npm run build` — 85 packages, build in 813ms — confirming the lockfile was sound and the deleted directory was the whole failure.

**Guard added:** `tests/test_site.py::test_vercelignore_keeps_both_projects_buildable`, asserting neither `site/` nor the reviewer build's inputs (`runs/`, `.llm_cache/`, `calibration/`, `datagen/`) are ignored.

## 2026-08-30 00:46 — build died on the Python version, and would have died again on fastapi

**Symptom:** `error: The requested interpreter resolved to Python 3.12.14, which is incompatible with the project's Python requirement: ==3.11.* (from project.requires-python)`, from `uv lock`.

**Diagnosis:** Vercel's Python runtime offers 3.12/3.13/3.14 and no 3.11; `requires-python = ">=3.11,<3.12"` cannot resolve. The same log showed `Installing required dependencies from pyproject.toml`, whose base dependencies exclude `fastapi` — it sits in the `dev`/`ui` extras because the CLI must stay installable without a web stack. The function would have failed to import after the version error cleared.

**First fix:** widened `requires-python` to `>=3.11,<3.13`.

**Whether it worked:** partially — it cleared the version error but not the missing `fastapi`.

**Final fix:** widened the range and pointed `installCommand` at `requirements.txt`, which carries the AST-computed import closure of the entrypoint. Verified by resolving that file against 3.12 with wheels only: 43 packages, `fastapi` present, `scikit-learn` and `matplotlib` absent.

**Guard added:** none for the version — `requires-python` is now a claim about two interpreters while the suite only runs 3.11. Accepted risk, recorded in `docs/deploy.md`: the deployed reviewer is the only thing running 3.12.

## 2026-08-30 01:16 — misdiagnosed a 236 MB bundle as npm's, on no measurement

**Symptom:** `Error: Total bundle size (236.18 MB) exceeds the maximum function size (225 MB).` after a build that otherwise ran clean end to end.

**Diagnosis:** first guess was `reviewer/web/node_modules` — 145 MB that `npm ci` recreates during the build, after `.vercelignore` has been applied. Excluding it via `excludeFiles` produced a build reporting `236.18 MB` again, byte for byte, which disproved the guess: it was never counted. Measuring the installed dependencies instead gave ~227 MB, matching the bundle almost exactly. scipy 118 MB and numpy 35 MB dominate, and neither is optional — `core/decompose.py` imports both at module scope for the tier-3 solver, reached from the reviewer through `cli.audit`.

**First fix:** `excludeFiles` on `reviewer/web/node_modules/**`.

**Whether it worked:** no. Identical reported size; the theory was wrong and cost a build cycle.

**Final fix:** prune `tests/`, `test/` and `__pycache__/` out of site-packages in `scripts/vercel_reviewer_build.py` after the audit runs — ~120 MB. Deployed at https://assay-7o7a.vercel.app.

**Guard added:** `tests/test_site.py::test_bundle_is_pruned_and_the_prune_cannot_escape_the_project`, pinning that `testing` is never pruned (`numpy.testing` is public API) and that the prune refuses any virtualenv outside the project, so running the build script on a developer machine deletes nothing.

## 2026-08-30 00:20 — the build made report_hash depend on the checkout path

**Symptom:** `tests/test_site.py::test_baked_data_is_not_stale` failed: the regenerated report carried `01cf67bf…` where the committed site data quotes `5bc692d4…`. Same seed, same inputs, same contract, same git sha.

**Diagnosis:** `scripts/vercel_reviewer_build.py` passed `--run-dir` as an absolute path. `run_dir` is inside the report's hash payload — `TELEMETRY_FIELDS` excludes timing and `git_sha`, not the run directory — so the hash became a function of where the repository sits on disk.

**First fix:** none attempted — the staleness test named the field by failing.

**Whether it worked:** n/a.

**Final fix:** the build script passes `runs/realistic-seed42` relative. Hash returned to `5bc692d4…`.

**Guard added:** none beyond the existing staleness test, which caught this unprompted. Noted separately: the deployed reviewer still reports a different `input_hash` from a Windows checkout, because git normalises line endings and the run inputs are text — the inputs genuinely differ byte-for-byte across platforms. Unfixed, and a real bound on invariant 4's portability claim.

## 2026-08-30 03:29 — a committed cache entry disarmed the chaos scenario that matters most

**Symptom:** wiring `assay chaos` into CI, the suite reported `18 scenarios,
17 passed, 1 failed`. C09 -- "LLM API entirely unavailable" -- failed on
`assert report.adjudication_degraded is True`, `assert False is True`, in
2.4s. Reproduced on a clean `git worktree` at HEAD, so it predated the CI
work. `.llm_cache/` had no uncommitted changes.

**Diagnosis:** C09 built its provider as `CachedProvider(NullProvider())`
and passed it to `resume_or_run`. Instrumenting the provider chain showed
`cache hits: 2, NullProvider reached: 0` -- the audit's adjudication prompt
hit a committed cache entry, so the injected provider was never called and
nothing degraded. The scenario's own stated injection ("every
generate_structured() call raises ProviderUnavailable") had stopped
happening. The wrapper was there for a real reason -- the contract compile
is a separate LLM boundary and the cache must serve it -- but it also
covered the adjudicator, which is the boundary under test. Whenever the
adjudication response for `runs/realistic-seed42` was recorded into
`.llm_cache/`, C09 stopped testing anything and kept passing until an
unrelated change moved the number it asserted on.

**First fix:** none attempted. The instrumented call counts named the cause
directly.

**Whether it worked:** n/a.

**Final fix:** split the two boundaries. The contract is compiled first
through `CachedProvider(NullProvider())`, exactly as before, and the audit
then runs against a **bare** `NullProvider()` with that contract passed in
via `resume_or_run(..., contract=contract)`. A cache hit cannot intercept a
provider it never wraps.

**Guard added:** none beyond the fix itself, which is self-guarding:
`adjudication_degraded` can only become True by `ProviderUnavailable`
propagating out of the injected provider, so the existing assertion now
proves the provider was reached. Noted separately, and not fixed: the same
shape is possible in any scenario that wraps its injected provider in
`CachedProvider` -- C01 and C07 both do -- and a future committed cache
entry could disarm those the same way, silently.

## 2026-08-30 03:52 — the evidence gate masked a wall clock in a table and missed the same number in a sentence

**Symptom:** the first CI run went 5/6. `evidence is real` failed on a
one-line diff after a full 18-run sweep on a Linux runner:

    -235 seconds. The assumption is stated so it
    +37 seconds. The assumption is stated so it

**Diagnosis:** `scripts/evidence_diff.py` masks environment-dependent
values by table-row label, and §11's `Wall clock (audit time only)` row was
masked correctly. But §11 also restates that measurement inside a sentence
— `eval/evidence.py:782`, "2,340 analyst-hours, done in {N} seconds" — and
prose carries no label to match on. 235s is the author's Windows box; 37s
is a GitHub runner. It is machine speed, and the gate treated it as a
finding.

**First fix:** none attempted. The diff named the line.

**Whether it worked:** n/a.

**Final fix:** a `MASKED_PROSE` list of anchored regexes beside
`MASKED_LABELS`, each masking only the varying number and keeping the
sentence, so a reworded claim still surfaces as a difference.

**Guard added:** four tests in `tests/test_evidence_diff.py` pinning the
mask in both directions — that the two wall clocks compare equal, that the
sentence and the analyst-hours figure survive masking, and that rewording
the sentence is still a difference. The analyst-hours number is derived
from the record count, not the clock, so masking it would have hidden a
real regression.

**Worth recording:** the run this failed on is the strongest evidence the
gate works. Every measured number reproduced on a machine with a different
OS, CPU and Python build — value-weighted recall 55.2%, count recall 67.0%,
zero false positives, determinism MATCH, clean profile 0 paise — and the
only thing that moved was how fast the box was. That is what EVIDENCE.md
claims, verified by something other than the machine that wrote it.
