"""Durable checkpoint/resume + journal storage for `assay audit`.

Everything under core/ is a pure, in-memory function -- nothing there ever
touches a disk or a network. That is deliberate (core/CLAUDE.md forbids new
dependencies there), but it means invariant 7's "append-only, idempotent
writes" claim has, until now, only ever been true within one Python
process's memory: cli/audit.py::run_audit() computes a full AuditReport in
one shot, and nothing is durable if the process is killed mid-way.

This package is the write side core/ledger.py's own docstring names as "a
separate concern... not implemented here yet": a SQLite-backed store
(SQLModel, already a declared dependency) that makes a killed-mid-audit
`assay audit` genuinely resumable, and makes the AUTO-lane journal a real,
durable, idempotency-key-enforced table rather than an in-memory list.

`store/` imports `core/`/`llm/` models to (de)serialize what it persists;
neither imports back.
"""
