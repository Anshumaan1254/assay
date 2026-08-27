"""Real gateway data in, a run directory out.

The mirror image of `datagen/`: that package writes a synthetic run
directory from a seed, this one writes a real one from a payment gateway's
API. Both produce the same four files `assay audit` already consumes
(`ledger.json`, `settlement_report.json`, `bank_statement.json`,
`rate_card.md`), so the audit engine itself is unchanged, still purely
file-based, and still deterministic: fetch once, then replay that run
directory forever and get a byte-identical report (invariant 4). A live
API is not a reproducible input, so it is deliberately kept on the far
side of the file boundary rather than wired into `run_audit()`.

Quarantine rules, same as every other package outside `eval/`: nothing
here imports `datagen/`, and nothing in `core/` imports this.

**What a gateway API structurally cannot give you.** Two of the four files
are not fetchable, and that is a fact about the problem, not a gap in this
module:

  - `rate_card.md` is your *negotiated* contract. No gateway exposes it as
    structured data -- it lives in a PDF or an email from your account
    manager. That is precisely why `llm/contract_parser.py` exists. You
    supply it; this package copies it into the run directory.
  - `bank_statement.json` should come from your *bank*. What a gateway's
    settlements API reports is the gateway's own claim about what it paid
    you, so an audit built on it is checking Razorpay against Razorpay: it
    still catches every fee, tax, refund and adjustment error, because
    `core/verify.py` recomputes those against your contract independently
    -- but it cannot catch an underpayment the gateway reported correctly.
    That limitation is recorded in the run manifest as
    `bank_statement_source`, so a report can never silently claim an
    independence it does not have.
"""
