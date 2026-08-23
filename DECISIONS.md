# Engineering decisions

Real entries, real timestamps. Not a changelog — the reasoning behind choices
that weren't obvious from the code.

## 2026-08-23

**Money is integer paise, never float.** `core/money.py`'s `Money` value
object wraps an `int` paise count plus a currency string. `Decimal` is
permitted only at the parse boundary (`Money.from_rupees`), and must convert
to `Money` before leaving the parser. This is the foundation every other
invariant in this project depends on — a settlement audit that can't prove
its own arithmetic is worthless. Enforced by `scripts/guard_core.py` (fast,
targeted) and `tests/test_architecture.py` (blanket ban on any float literal
or `float()` call anywhere under `core/`).

**`split_proportionally` takes `weights: Sequence[int]`, not
`Sequence[Money]`.** The task didn't specify a type. Typing it as `Money`
would couple the split algorithm to the `Money` type when the underlying
operation is purely proportional-integer allocation; a caller splitting a fee
proportional to transaction amounts just passes `[t.paise for t in
transactions]`. Keeps `Money` from needing to know about collections of
itself.

**Added `hypothesis` (dev dependency).** The task requires a property test
proving `split_proportionally` always re-sums to the original for random
paise and random weight vectors, including negative paise (refunds). Not in
CLAUDE.md's dependency list — asked first, confirmed.

**Added `python-dotenv` (dependency).** `llm/providers/gemini.py` reads
`GEMINI_API_KEY` from `.env`. Not in CLAUDE.md's dependency list — asked
first, confirmed, rather than hand-rolling a `.env` parser.

**Added `ruff` (dev dependency).** The Makefile's `lint` target needs a
linter and none was named. Asked first, confirmed.

**Removed `.llm_cache/` from `.gitignore`.** It was ignored, which directly
contradicted the invariant that the LLM disk cache "is committed to the repo
so `make demo` runs with no API key." `.llm_cache/` is the real, tracked
cache path `llm/providers/cached.py` writes to. Asked first, confirmed.

**`llm/providers/gemini.py` uses the Interactions API
(`client.interactions.create(...)`), not `client.models.generate_content(...)`.**
The latter is the older pattern; `google-genai` >= 2.3.0 exposes
`client.interactions.create(model=, input=, response_format={"schema": ...})`
for structured output, confirmed against the current official docs. Every
Gemini API error — a 429 that survives retries, or any other API error —
degrades to `ProviderUnavailable`, chained from the original exception, so
callers only ever need to catch the one exception type the `LLMProvider`
Protocol documents.
