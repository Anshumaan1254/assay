# Architecture

See the diagram in the [README](../README.md#architecture) for the shape of
this. This document is the sequence, written out.

## The sequence of an audit run

`assay audit --run-dir <dir>` reads four files from that directory —
`ledger.json`, `settlement_report.json`, `bank_statement.json`,
`rate_card.md` — and runs `cli/audit.py::run_audit()`, in this order.

1. **Hash the inputs.** SHA-256 of each of the four files' raw bytes, then
   a SHA-256 over that set of hashes (`input_hash`). `audit_run_id` is
   derived from it (`AUD-<input_hash[:12]>`), not random — re-running the
   same inputs produces the same run id, the same `Finding` ids, and the
   same idempotency keys.
2. **Load the ledger and bank credits** (`cli/loaders.py`): payments,
   refunds, chargebacks and adjustments from `ledger.json`; fee lines, tax
   lines and settlement batches from `settlement_report.json`; bank
   credits from `bank_statement.json`.
3. **Compile the contract — LLM boundary #1.** If no pre-compiled contract
   was pinned, `llm/contract_parser.py` asks the model to fill a
   `RateCardParse` from `rate_card.md`'s text, validates it against that
   schema, and `core/contract.py` compiles it into a deterministic
   `CompiledContract` — tiered MDR, flat fees, caps, GST-on-fee,
   effective-dated revisions, all in integer basis points. Neither a
   provider nor a pinned contract means the run stops here. It never
   fabricates a rate card to keep going.
4. **Decompose.** `core/decompose.py` resolves every bank credit into the
   exact transactions that produced it, through a three-tier cascade —
   structural UTR/batch join, then a bounded subset-sum search over what's
   left, then a batched min-cost assignment over whatever's still
   unresolved. Every proof carries its own evidence and is independently
   re-verified (`verify_proof`); the whole run is checked for
   reproducibility before anything downstream trusts it.
5. **Verify.** `core/verify.py` independently recomputes fee and tax for
   every resolved payment against the compiled contract and diffs it
   against what the settlement report actually charged — deterministic
   arithmetic, no model involved. A payment the contract has no clause for
   becomes a `ContractGap`, not a guessed fee.
6. **Conserve.** `core/conserve.py` asserts the identity below for every
   bank credit, and separately finds any ledger record that no credit
   claimed at all (`unclaimed_paise`) — otherwise a whole missing
   settlement batch would read as a clean audit.
7. **Adjudicate — LLM boundary #2**, only when a provider is configured.
   Whatever deterministic matching genuinely could not explain — a
   residual on an otherwise-resolved credit, an ambiguous or unresolved
   decomposition, an unclaimed record — becomes a `ResidualCase` with a
   bounded evidence pool. `llm/adjudicator.py` asks the model for a ranked
   hypothesis with mandatory citations, then checks every cited record id
   both against the ledger and against what was actually shown to it
   before trusting it. Findings from this boundary are always
   `Lane.PROPOSE`; `AUTO` is structurally unreachable for anything
   LLM-sourced, regardless of confidence. A provider failure or a
   schema-invalid batch response degrades the run with a labelled reason —
   never a crash, never a guess.
8. **Cluster and narrate — LLM boundary #3**, if a provider is configured.
   `core/exceptions.py` clusters findings deterministically and builds one
   dispute packet per cluster: the claim, the contract clause text, a
   worked recomputation, the full evidence list — every number already
   computed. `llm/narrator.py` asks the model for a plain-English
   paragraph around those numbers; any digit in the returned prose that
   isn't traceable back to the packet's own computed fields gets the
   narration rejected outright, not silently dropped.
9. **Post.** `core/ledger.py::journal_entries_for_auto_findings()` posts
   only `Lane.AUTO` findings — checked again at this point regardless of
   what the caller already filtered — keyed by
   `sha256(input_hash:finding_id)`, so re-running the same audit against
   its own prior postings adds nothing.
10. **Hash the report.** `AuditReport.compute_report_hash()` is a SHA-256
    of the canonical JSON of everything the audit *concluded* — proofs,
    findings, clusters, journal entries, conservation reports — with
    everything it merely *observed about the machine it ran on*
    (per-proof timings, wall clock, `posted_at`) stripped out first. Same
    inputs, same contract version, same calibration artifact, same seed:
    same hash, byte for byte. `assay replay <run_id>` re-runs the audit
    and asserts the recomputed hash matches the stored one.

## The conservation identity

For every bank credit, in integer paise, with no tolerance:

```
credit = settled_gross
       - refunds - fees - tax - chargebacks - adjustments
       + reversals
       + unexplained
```

`unexplained` is a first-class, reportable bucket — never rounded away,
never silently absorbed. It has two halves, and reporting only the first
would let a whole lost settlement batch read as clean:

- **residual on credits that exist** — a credit resolved to real records,
  but they don't quite net to it (`ConservationReport.unexplained_paise`,
  exactly the decomposition proof's own residual);
- **records no credit ever claimed at all**
  (`unclaimed_paise`/`unclaimed_records`) — a lost batch, a payment that
  never reached settlement.

`core/conserve.py::conserve()` reconstructs `credit_paise` from its own
buckets and raises `ConservationViolation` if it doesn't match exactly,
before returning a `ConservationReport`. `assay audit` asserts the identity
again across the whole run. On the clean synthetic profile — nothing
planted — every run reconciles to exactly ₹0.00 unaccounted: not rounded
to zero, not within a tolerance band. Zero.

**What this identity does not prove.** `unexplained_paise` is deliberately
`proof.residual_paise` and nothing else: money that does not net against
the settlement report's *own* claimed numbers. Whether those claimed
numbers are themselves correct against the contract is a different
question — `core/verify.py`'s, not this module's. A report can be
perfectly self-consistent (`unexplained_paise == 0`) while every fee on it
was computed off the wrong contract clause; `core/conserve.py` is
structurally incapable of seeing that, on purpose (its module docstring
says so directly). A gateway that mispriced every transaction and then
paid out exactly what its own — wrong — report claims conserves
perfectly. Catching that mispricing is step 5, `verify.py`'s independent
recomputation against the compiled contract, not this step.
