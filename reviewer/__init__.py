"""Reviewer UI backend: a read-only HTTP lens over an already-computed audit.

This package computes nothing about money. Every figure it serves is either
read straight off a persisted `AuditReport` (`report.json`, written by
`assay audit`) or is a sum/count over its fields, derived in `derive.py`
where it can be unit-tested without a server involved.

Deliberately read-only, this phase. `core/ledger.py:199` posts journal
entries for `Lane.AUTO` findings only, and there is no approval or reviewer
concept anywhere in the engine -- no `approved_by`, no `reviewed_at`, no
single-finding posting path. A working "approve this PROPOSE finding"
button would therefore be new money-path code letting a human cause a
posting the calibration explicitly declined to certify, which is invariant
7's territory and belongs in its own test-first change, not in a UI patch.
Until then this package has no write endpoints at all.
"""
