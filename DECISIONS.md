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
