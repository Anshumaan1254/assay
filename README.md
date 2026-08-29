# Assay

Settlement audit engine. Given a merchant's transaction ledger, a settlement
report, a bank statement and a contracted rate card, it decomposes each bank
credit into the exact transactions that produced it, independently
recomputes every deduction, and reports — in rupees — any amount that cannot
be explained. The product question is not "did the rows match." It is: was
I paid the correct amount, and can you prove it line by line.

## Live

| | |
|---|---|
| **Landing page** | <https://site-lake-two-15.vercel.app> — the scroll story: what settlement is, why nobody checks it, and the three payments this month that belonged to no bank credit |
| **Reviewer** | <https://assay-7o7a.vercel.app> — read-only lens over a computed audit: the conservation identity assembling term by term, per-run scorecards, drill-down to one transaction |
| **Source** | <https://github.com/Anshumaan1254/assay> |
| **Evidence** | [EVIDENCE.md](EVIDENCE.md) — generated, hash-checked, never hand-edited |

Both sites deploy from this repository as two Vercel projects, told apart
only by Root Directory. The reviewer builds its own audit during deployment
rather than committing derived artifacts — [`docs/deploy.md`](docs/deploy.md)
has the whole arrangement.

## The problem, in money terms

Payment gateways settle net, not gross: they deduct fees, tax, refunds,
chargebacks and reserve adjustments before the bank credit ever lands, and
hand the merchant a settlement report stating what they deducted and why.
Almost nobody outside the gateway recomputes that deduction independently —
doing it by hand means reading a rate card with tiered MDR, per-method flat
fees, caps, GST on fees and mid-month effective-dated revisions, then
applying it correctly across tens of thousands of transactions a month. A
single wrong basis point on one clause does not fail loudly. It compounds
silently across every transaction that clause touches, and by the time
anyone notices, months of statements have to be re-audited by hand to find
where it started. Assay makes that recomputation automatic, exact, and
disprovable: every rupee it reports is either backed by a cited transaction
or booked to a first-class `unexplained` bucket — never rounded away, never
silently absorbed.

## Quickstart

Requires Python 3.11: `pip install -e ".[dev]"` (or `make install`).

```
make demo
```

Generates a synthetic settlement month, compiles its rate card, audits it,
and scores the result against known ground truth — four steps, about 30
seconds, entirely from the committed `.llm_cache/`. **No `GEMINI_API_KEY`
required.** Verified before writing this down: no network calls, and
`determinism: MATCH` on the run this produces.

Or the same four steps in a container, on a machine that has never seen
this repository:

```
docker build -t assay . && docker run --network none assay demo
```

`--network none` is the point rather than a precaution: the container is
handed no environment variables and no route to the internet, and still
produces the complete audit — every model response comes from the
committed `.llm_cache/`. If it needed a key, this would fail.

Then look at one transaction's full causal chain, or the generated evidence
report:

```
assay explain PAY-000123
make evidence
```

## Beyond the demo

- **Full CLI:** `assay generate`, `contract compile`, `audit --run-dir`,
  `eval`, `report <run_id>`, `replay <run_id>` (re-runs an audit and asserts
  its report hash still matches — invariant 4, on demand), `explain
  <record_id>`, `evidence` (prints and hash-verifies the committed
  `EVIDENCE.md`), `chaos`, and `fetch` (pulls one real settlement month from
  Razorpay's API into a run directory — the one CLI command that touches a
  network by design).
- **Reviewer UI** (`make ui`): a FastAPI + React view over an
  already-computed audit — GSAP-driven assembly of the conservation
  identity, per-run scorecards, drill-down to a single finding. Read-only by
  architecture, not by omission: no approval route exists anywhere in it
  (`tests/test_reviewer_api.py::test_every_route_is_read_only`).
- **Two Vercel deployments, one repo** — the reviewer is live at
  <https://assay-7o7a.vercel.app>, and the landing page links straight at
  it. Told apart by Root Directory:
  `site` builds the landing page as a static SPA; the repo root builds
  `reviewer/api.py` as a Python function (`[tool.vercel] entrypoint` in
  pyproject.toml, via the `reviewer/vercel_app.py` shim -- `reviewer/api.py`
  itself knows nothing about Vercel, and a test asserts that). The reviewer's
  build recomputes its own audit from the committed inputs and `.llm_cache/`
  rather than committing derived artifacts.
- **Landing page** (`make site`, deployed on Vercel): a standalone scroll
  story for the project — a scroll-locked video hero, then the argument one
  line at a time, a pinned clip-path reveal, and a parallax ticker wall.
  Shares `reviewer/`'s palette and links through to it. Every figure it
  prints is baked from a signed report by `scripts/bake_site_data.py` and
  guarded by `tests/test_site.py`; the browser does no arithmetic on money.
- **Chaos suite** (`make chaos`): 12 failure-injection scenarios run
  end-to-end and scored from the incident each one logs — a duplicate
  settlement, a killed-and-resumed audit, an out-of-order chargeback, a
  cross-cycle refund, a truncated UTR, a fabricated transaction id, a
  malformed or rate-limited or entirely unavailable LLM response, float
  contamination, silent data corruption, a contract gap.

## How well does it work

[**EVIDENCE.md**](EVIDENCE.md) is generated by `make evidence` from a real
run over synthetic settlement months with known planted discrepancies;
`scripts/check_evidence.py` fails the build if it's ever hand-edited. Full
numbers, all fourteen sections, are there — the headline:

- **0 false-positive findings** across 18 audited months (1,400 findings
  total, §4) — the engine claimed ₹0.00 that was not there.
- **55.2% value-weighted recall** of planted rupees, 67.0% by count (§2–3)
  — the gap means the errors it misses run larger than average, not smaller.
- **Conservation holds to the paisa** on every clean-profile run: ₹0.00
  unaccounted — not rounded, not within tolerance (§7). Read that for
  exactly what it is: a self-consistency check on the settlement report's
  *own* claimed numbers, not proof those numbers are contractually
  correct. A gateway that prices every transaction off the wrong MDR tier
  and then pays out exactly what its own report claims conserves
  perfectly — ₹152,495.74 of the ₹415,863.64 planted this sweep (every fee
  and tax mispricing class, 36.7%) leaves `unexplained` at ₹0.00.
  `core/verify.py`'s independent recomputation against the contract is
  what catches that; conservation catches the other shape — money that
  moved and cannot be placed — and reports it rather than absorbing it:
  ₹47,857.95 on the engineered profiles.
- **Nothing auto-posts.** The conformal fit could not certify a 50bps error
  rate at any threshold for every confidence source, so `AUTO` is
  structurally unreachable and every finding sits at `PROPOSE` (§9). A
  fixed 0.9 threshold *would* have auto-posted — see the ablation, §13.
- **The model touches 0.03% of records** (108 of 320,749, §10) and is
  checked twice — schema validation, then a reference check against every
  cited record id. Zero fabricated ids caught in this sweep, zero
  rejections; read that as a fact about this sweep's output, not proof the
  check is unneeded.

## Architecture

![Assay architecture: input files feed a deterministic core pipeline — contract, decompose, verify, conserve, lanes, exceptions, ledger — running left to right in a shaded core/ panel. A separate shaded llm/ panel holds the only three model boundaries — contract_parser, adjudicator, narrator — each crossing back into the core panel only as already-validated, reference-checked data: a compiled contract, a PROPOSE-lane finding, or a narration string built from numbers already computed. tests/test_architecture.py enforces zero LLM imports and zero float literals anywhere under core/.](docs/architecture-diagram.svg)

The full sequence of an audit run, and the conservation identity written
out: [docs/architecture.md](docs/architecture.md).

## Where I chose not to use AI

Four places a model was the obvious reach, and why each one was wrong.

**Fee arithmetic.** A model reading "1.75% + ₹10, capped at ₹70" and
producing a rupee figure is a model computing money. The failure mode is
silent, per-transaction drift — a slightly-wrong basis point that never
raises an error, it just compounds. `llm/contract_parser.py` gets the model
only as far as a structured, schema-validated rate schedule
(`RateCardParse`); `core/contract.py` compiles that into a
`CompiledContract` and does every multiplication in integer basis points,
in Python, with tests pinned to the real tier boundaries. The model reads
the card once. It never sees a transaction.

**Subset decomposition** — which transactions produced a given bank credit.
This is exactly the kind of combinatorial reasoning where a model produces
a confident, plausible-looking wrong answer, and a wrong subset looks
identical to a right one unless someone re-derives it by hand. The failure
mode is a clean report over the wrong records, with nothing anywhere to
indicate it. `core/decompose.py` is a deterministic three-tier cascade
(structural UTR join, bounded subset-sum search, batched assignment) that
attaches a verifiable proof to every answer and reports `AMBIGUOUS` —
naming the competing candidates — rather than picking, when two subsets
explain a credit equally well.

**Discrepancy classification.** Labelling a finding `FEE_UNDERCHARGE` versus
`TAX_MISCALCULATION` from a probabilistic model turns a claim a merchant
takes to a gateway into a soft guess instead of an exact diff. The failure
mode is a wrong or invented classification sitting behind a real rupee
number. `core/verify.py` independently recomputes fee and tax against the
compiled contract and diffs deterministically; only what deterministic
matching genuinely cannot explain — a residual, an ambiguous proof, a
record no credit ever claimed — reaches `llm/adjudicator.py`, and even
there every cited record id is checked against the ledger before its
hypothesis is trusted.

**Posting approval.** Autonomy over money movement is the highest-leverage
place a model could be wrong. `core/lanes.py` caps any LLM-sourced item at
`PROPOSE`/`ESCALATE` — `AUTO` is structurally unreachable for it regardless
of confidence — and the conformal calibration itself refuses to certify a
threshold it can't back statistically (the committed artifact currently
reports `AUTO` as `unreachable` for every source, not clamped to a number
that only looks like one). `core/ledger.py` posts `Lane.AUTO` findings
only, and the reviewer UI has no approval path at all — no `approved_by`,
no `reviewed_at`, no single-finding posting route.

`tests/test_architecture.py` is the proof underneath all four: it walks the
AST of every module under `core/` and fails the build on an LLM import or a
single float literal, no exceptions.

## What broke

Real entries from [DECISIONS.md](DECISIONS.md), which logs incidents with a
timestamp, not just the fixes that ended up working.

**2026-08-24 15:10 — every Gemini call was silently unconstrained prose.**
The first live call against the real API returned *"Here are three Indian
payment methods..."* instead of JSON — a schema was attached to the
request, but nothing about the response respected it, and nothing at the
call site could tell. *First fix:* moved the schema to what looked like the
right field on the same API call. **It didn't work** — still prose, which
ruled out that whole API path (the Interactions API doesn't enforce
`response_format` for this model at all). *Final fix:* switched to
`generate_content` with `response_schema`, which does enforce it — proven
by it now returning HTTP 400 naming the project's own field paths, which
exposed a second, real bug (Pydantic's `additionalProperties`/`$ref` output
isn't valid Gemini schema, so it needed its own adapter). Guard: six tests
over that schema adapter, plus tests asserting a non-JSON or empty response
degrades to `ProviderUnavailable` instead of a raw `JSONDecodeError`.

**2026-08-25 16:11 — the first calibration fit gave `AUTO` a threshold of
zero.** Fitting the conformal bound over all five confidence sources pooled
into one number: `core/verify.py`'s ~171,652 near-perfect points
statistically absorbed the adjudicator's 146 much worse ones, and the
pooled failure rate cleared the safety target anyway. *First fix:* require
every source's own statistical bound to hold independently. **It only
partially worked** — it correctly rejected the zero threshold, but surfaced
a second bug: the "no threshold could be certified" sentinel was being
silently clamped to 10,000, turning "uncertifiable" into "certified at
maximum confidence." *Final fix:* made the threshold genuinely nullable, so
`None` means `AUTO` is structurally unreachable rather than a number that
only looks like one. That's the state committed today — `AUTO` is honestly
unreachable for every source, which is a correct answer given current data,
not a residual bug.

**2026-08-26 00:21 — the decomposition engine's own docstring described
behavior it didn't have.** A skeptical domain review asked directly: what's
the assumption that breaks first against real data. The reviewer traced
`decompose_all`'s docstring — every credit's structural match first, *then*
subset-sum over what's left — against what the code actually did, which was
interleave both tiers per credit. Against any dataset without a clean
one-credit-per-batch bijection, an earlier credit's subset-sum search could
claim a later credit's own batch before that later credit got its
structural turn — reporting a fabricated, often sign-flipped discrepancy on
the wrong credit, the exact failure the module exists to make impossible.
It never showed up on the committed demo data only because that dataset
happens not to contest any batch across credits. Fixed by running two
explicit, complete phases instead of one interleaved loop; re-running the
committed reference run afterward confirmed byte-identical proof counts and
residuals — so the bug was real and latent, not something the demo data had
ever exercised.

## Limitations

Plainly. Every number in `EVIDENCE.md` is measured against a synthetic month
`datagen/` generated and then deliberately broke — the ground truth is known
because this project planted it. That proves the engine finds what
`datagen/` knows how to hide. It does not prove the engine finds what a real
gateway does wrong.

**What it does prove:** the arithmetic is right, the conservation identity
accounts for every paisa it cannot place — reporting ₹47,857.95 on the
engineered profiles rather than absorbing it, ₹0.00 on the clean one —
decomposition resolves real many-to-many credit/batch structure and
reports ambiguity instead of guessing, and the model boundary is small
enough to measure and hasn't fabricated a citation in any sweep run so
far. Conservation is a self-consistency check, not a correctness check —
see the caveat above and in `docs/architecture.md`; it is `core/verify.py`
that catches a bank credit computed correctly off a wrong contract clause.

**What would change against production data:**
- Real rate cards are addenda-in-email messy, not clean markdown.
  `llm/contract_parser.py` is schema/overlap/completeness-validated;
  nothing yet independently checks that it *read the card correctly*
  against a real settlement's own fees.
- The synthetic generator gives most credits a distinct UTR pointing at a
  distinct batch. Real settlement files don't guarantee that, so tiers 2
  and 3 (subset-sum, assignment) would carry more of the real load than
  they do here.
- The won-chargeback-reversal match is by amount alone, ignoring
  `Adjustment.reason`/`settlement_id`. Round-rupee amounts recur in
  production; this would false-negative.
- Currency is assumed INR throughout; `core/contract.py` never checks a
  payment's currency against the rate card's declared one.

**Known gaps:** two discrepancy classes have no detector at all — a refund
attributed to the wrong batch (₹121,623.29 planted, 0% recall by design:
both the true and the wrong batch fully verify against their own corrupted
records) and sub-paisa rounding drift, which surfaces as a tax
miscalculation instead of its own class. Together they're the largest share
of the distance between 55.2% value-recall and 100%. The compiled contract used by
every audited run has no recorded human sign-off — `core/contract.py`'s
`require_signoff` exists and is tested, but `run_audit` doesn't gate on one
yet, and a production deployment must.

**What I'd build next:** a real detector for the wrong-batch-refund class
(needs an actual cross-cycle-attribution business rule, not a guess);
durable persistence for posted `AUTO`-lane journal entries — currently a
pure function, by design, until that's built for real; more calibration
seeds, since the sparsest planted classes are as few as one instance per
synthetic month, too thin for a tight confidence interval; and the reviewer
UI's own approval path, which invariant 7 explicitly declines to build
until it earns its own test-first change.

## Repo map

```
core/          deterministic engine — NO llm imports, NO float money
  money.py       Money value object, integer paise
  contract.py    compiled, effective-dated fee schedule
  decompose.py   bank credit -> transaction subset, with proof
  verify.py      independent recomputation + diff
  conserve.py    the conservation identity, asserted
  lanes.py       conformal-calibrated autonomy lanes
  exceptions.py  taxonomy, clustering, dispute packets
  ledger.py      append-only, idempotent journal
llm/            the three boundaries above — every output schema-validated
  providers/     gemini.py, cached.py (disk cache, committed), null.py
datagen/        synthetic generator + planted truth — QUARANTINED, nothing
                 outside eval/ may import it
eval/           metrics harness; the only module allowed to read ground truth
chaos/          12 failure-injection scenarios — see "Beyond the demo"
store/          checkpoint/resume + durable AUTO-lane journal (SQLite)
ingest/         real gateway API -> a run directory (`assay fetch`, Razorpay)
cli/            the typer app — full command list above
reviewer/       FastAPI read-only UI over a computed audit, no write routes
  api.py          GET-only routes
  web/            Vite + React + TypeScript + GSAP ScrollTrigger
site/           standalone landing page -> Vercel; static, no API
  src/data/       audit.json, baked from a signed report — never hand-edited
  src/components/ui/  the scroll components; GSAP ScrollTrigger + Lenis
tests/          ~55 test modules
docs/           this diagram, the reliability plots, architecture.md
calibration/    the committed, versioned conformal calibration artifact
runs/           committed reference run (realistic-seed42) + truth/
DECISIONS.md    engineering log — real entries, real timestamps
EVIDENCE.md     generated accuracy report — never hand-edited
```

## What existed before 23 Aug

Nothing. The repository's history starts at `2026-08-23 16:27`, with a
license file and empty config placeholders. The first ~76 minutes are pure
bootstrap — four commits, no logic. Everything else — the engine under
`core/`, all three LLM boundaries, `datagen/`, `eval/`, `chaos/`, `cli/`,
`store/`, `ingest/`, and the reviewer UI — was built in the 72 commits that
follow, across the roughly six days between 2026-08-23 and 2026-08-29.
`DECISIONS.md` is the day-by-day record of that window, in real time,
including the parts that didn't work on the first try.
