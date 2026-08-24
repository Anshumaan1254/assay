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
