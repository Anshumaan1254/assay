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
