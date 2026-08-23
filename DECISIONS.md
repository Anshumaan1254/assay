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

**Incidents:** none shipped broken — all four caught and fixed before
commit, three during test-first development and one via a failing
property test:
- `datagen/inject.py`'s D09 injector corrupts `BankCredit.utr`. The
  first draft of `_recompute_batch_and_credit` re-matched a batch to its
  bank credit by `utr` after the fact, which would have silently broken
  (or crashed) for any later injector recomputing a batch D09 had
  already touched, given D09 runs before D10-D12 in the fixed D01..D12
  order. Found during design, before any code was written — fixed by
  resolving the batch-to-credit pairing once, up front, from the
  unmutated world, and added a regression test
  (`test_d09_does_not_break_later_injectors_batch_lookup`) exercising a
  dense multi-class profile.
- The whole-world conservation property test
  (`test_whole_world_credit_delta_reconciles_against_ground_truth`)
  failed by exactly 69,526 paise. Root cause (found with an Opus
  subagent, given the required debugging effort): D04 ("refund deducted
  twice") has no backing source record by design — the ledger's `Refund`
  list is deliberately never duplicated, so its double-deduction is
  written directly onto the batch total. Every other injector recomputes
  its batch from records via `compute_batch_net_paise`, which silently
  erased D04's delta whenever a later injector (D05/D06/D07/D08/D10/
  D11/D12) recomputed a batch D04 had already adjusted — 3 of 3 D04
  instances lost at the failing seed. Fixed with a small `_CreditBook`
  that banks these "unbacked" deltas per batch and re-applies them on
  every recompute, regardless of injection order — order-independent by
  construction, so a future D13 recomputing a batch can't reintroduce
  the same bug.
- Same debugging pass surfaced a second, latent bug: `_inject_d02`'s
  eligible-fee-line list is a snapshot taken before its loop runs, so a
  payment with two eligible fee lines (NETBANKING/WALLET carry both MDR
  and FIXED) could receive two separate D02 entries — hadn't triggered
  in any test yet, found by inspection while reviewing the D04 fix.
  Fixed by re-checking `touch.payments` inside the loop; added a
  regression test at a high enough count (400) to reliably exercise it.
- The same property test's own expectation logic had a sign bug, found
  while diagnosing the above: `ROUND_HALF_EVEN` only ever differs from
  `ROUND_HALF_UP` at an even N.5 tie, where half-even always rounds
  *down* — so D07 (rounding drift) always *reduces* reported tax,
  *increasing* reported net, the opposite direction from every other
  always-one-direction class. `ROUNDING_DRIFT`, unlike
  `FEE_OVERCHARGE`/`FEE_UNDERCHARGE`, isn't directionally named, so a
  blanket "not-FEE_UNDERCHARGE means -1" sign heuristic was wrong for it
  specifically. Fixed in the test's expectation logic, not the
  generator — the generator's numbers were correct throughout.
**Guard added:** the three regression tests named above, all committed.
