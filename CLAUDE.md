# Assay — settlement audit engine

## What this is
Assay independently verifies payment-gateway settlements. Given a merchant's
transaction ledger, a settlement report, a bank statement and a contracted rate
card, it decomposes each bank credit into the exact transactions that produced
it, independently recomputes every deduction, and reports — in rupees — any
amount that cannot be explained.

The product question is not "did the rows match". It is: **was I paid the
correct amount, and can you prove it line by line.**

## Non-negotiable invariants
Architectural law. If a task appears to require breaking one, STOP and ask me
instead of working around it.

1. **Money is integer paise.** A `Money` value object wraps an `int`. Floats are
   forbidden everywhere under `core/`. `Decimal` may appear only inside parsers
   at the ingest boundary and must convert to `Money` before leaving the parser.

2. **No language model ever computes, adjusts, or approves an amount.** All
   arithmetic — fees, tax, netting, subset decomposition, rounding — is
   deterministic Python with unit tests. `core/` has zero imports from `llm/`.
   `tests/test_architecture.py` walks the AST of every module under `core/` and
   fails on any LLM import. That test is a project deliverable.

3. **Money conservation.** For every bank credit this identity holds exactly, in
   paise, with no tolerance:

       credit = settled_gross
              - refunds - fees - tax - chargebacks - adjustments
              + reversals
              + unexplained

   `unexplained` is a first-class, reportable bucket. Never rounded away, never
   silently absorbed. The system is structurally incapable of reporting a clean
   audit while rupees are missing. Assert at the end of every run.

4. **Determinism.** Same inputs + same contract version + same seed produce a
   byte-identical report carrying a SHA-256 of its own canonical JSON. LLM calls
   are cached to disk keyed by prompt hash, so replays never re-query.

5. **Ground truth is quarantined.** `datagen/` plants known discrepancies.
   Nothing under `core/`, `llm/` or `cli/` may import from `datagen/`. Only
   `eval/` reads ground truth. This is the first thing a sharp evaluator checks.

6. **Every LLM output is schema-validated and reference-checked.** Parse into a
   Pydantic model, then verify every record ID it cited actually exists. A
   response citing a non-existent ID is discarded and the item stays an
   exception. Log every rejection — those logs are demo material.

7. **Append-only ledger, idempotent writes.** Journal entries and audit events
   are append-only. Every write carries an idempotency key. Re-running an audit
   over the same batch must never double-post.

## Where LLMs ARE used — exactly three places
- `llm/contract_parser.py` — unstructured rate card to structured, effective-
  dated fee schedule. Validated, then compiled to deterministic rules. A human
  signs off once; from then on the arithmetic is pure code.
- `llm/adjudicator.py` — for residuals deterministic matching could not explain,
  propose a root-cause hypothesis with mandatory citations. It proposes; it
  never posts.
- `llm/narrator.py` — human-readable explanation of an already-computed finding.
  Numbers are injected from the computed result, never generated.

Anywhere else, the answer is no.

## LLM provider
Assay runs on the **Gemini free tier** (Flash for parsing and adjudication,
Flash-Lite for narration). But `llm/` never imports the Gemini SDK directly.

- `llm/provider.py` defines an `LLMProvider` Protocol: one method,
  `generate_structured(prompt, schema, model_hint) -> dict`, plus a
  `ProviderUnavailable` exception.
- `llm/providers/gemini.py` implements it using Gemini's structured-output mode
  with `response_schema` — constrained decoding, not prompt-and-pray.
- `llm/providers/cached.py` wraps any provider with a disk cache keyed by
  SHA-256 of (prompt + schema + model). Cache hits never call the network. The
  cache is committed to the repo so `make demo` runs with no API key.
- `llm/providers/null.py` raises `ProviderUnavailable` on every call. Chaos
  scenario 9 uses it to prove the audit still produces a number.

Free-tier limits are real: roughly 15 RPM and 1,500 requests/day on Flash.
Every call goes through a rate limiter with exponential backoff and jitter on
429. A 429 that survives retries degrades to an exception, never a crash and
never a guess. Assume these numbers change — read them from config, not code.

Swapping providers must be a one-line config change. This is worth doing well:
"the model boundary is an interface, not a vendor" is a real architectural point
and it goes in the README.

## Tech
Python 3.11, Pydantic v2, pytest, Typer, structlog, SQLModel over SQLite,
google-genai for the Gemini provider only.
FastAPI + React only for the reviewer UI, added late. The CLI is the product.
No pandas in `core/`. No new dependencies without asking me first.

## Reviewer UI
Deliberately **read-only**, and that is an architectural position, not a
missing feature. `core/ledger.py` posts journal entries for `Lane.AUTO`
findings only, and the engine has no approval or reviewer concept at all —
no `approved_by`, no `reviewed_at`, no single-finding posting path. A
working "approve this PROPOSE finding" button is therefore new money-path
code letting a human cause a posting the calibration explicitly declined to
certify. That is invariant 7's territory and gets its own test-first
change; until then `reviewer/` exposes no write routes and a test asserts
it (`tests/test_reviewer_api.py::test_every_route_is_read_only`).

The visual direction is deliberate and heavy: GSAP ScrollTrigger drives a
scrubbed, pinned assembly of the conservation identity, count-ups on the
headline figures, and staggered reveals throughout. The reasoning is that
the one screen a skeptical reader remembers should be the one where
invariant 3 assembles itself term by term in front of them. Motion never
substitutes for a number: every rupee figure is served as both exact
integer paise and a preformatted string, and the browser never does money
arithmetic. `prefers-reduced-motion` collapses every timeline to its end
state.

`reviewer/` must never import `datagen/` (invariant 5) and never compute an
amount — it sums and subtracts integer paise read off a signed report, and
nothing else.

## Repo layout
assay/
  core/          deterministic engine. NO llm imports, NO float money.
    money.py     Money value object
    models.py    domain entities
    contract.py  compiled, effective-dated fee schedule
    decompose.py bank credit -> transaction subset, with proof
    verify.py    independent recomputation + diff
    conserve.py  the conservation identity, asserted
    lanes.py     conformal-calibrated autonomy lanes
    exceptions.py taxonomy, clustering, pricing
    ledger.py    append-only, idempotent journal
  llm/           the three boundaries. All outputs validated.
  datagen/       synthetic generator + planted truth. QUARANTINED.
  eval/          metrics harness. Only module allowed to read ground truth.
  chaos/         failure injection scenarios
  store/         checkpoint/resume + durable AUTO-lane journal (SQLModel/SQLite)
  ingest/        real gateway API -> a run directory. Mirror of datagen/.
  cli/           typer app
  reviewer/      FastAPI read-only UI over a computed audit. NO write routes.
    derive.py    every figure the UI shows, as pure functions over a report
    api.py       GET-only routes; reuses cli/report.py's run_id lookup
    web/         Vite + React + TypeScript + GSAP ScrollTrigger
  site/          landing page, added 2 Sep
tests/
scripts/         guard_core.py and other hook scripts
DECISIONS.md     engineering log — real entries, real timestamps
EVIDENCE.md      generated accuracy report. Never hand-edited.
README.md
Makefile

## Working agreement
- **Test-first for anything touching money.** Write the failing test, show me,
  then implement.
- Small commits with real messages. Never a commit named "updates".
- When you hit an ambiguity in payments domain logic, ask. Do not invent a
  business rule and bury it in a function.
- Prefer boring, obvious code. Interviewers will read this.
- After each phase print: what you built, what you did not build, what you are
  unsure about.
- Do not add features I did not ask for. Scope creep is how this project dies.
- Default to Sonnet. Tell me when a task genuinely warrants Opus.