"""C02 -- process killed mid-audit.

A real OS-level kill, not an in-process simulation: the property under
test is what happens to a *process*, including its SQLite file handle and
WAL file, when the OS reclaims it mid-transaction -- exactly the class of
bug an in-process monkeypatch can't expose. `assay audit` is launched as a
real subprocess, killed once its decompose checkpoint has durably
committed but before the rest of the pipeline finishes, then relaunched
identically. The resumed run must not redo decompose and must not
double-post a single journal entry.

store/resumable.py's own tests (tests/test_store_resumable.py) cover the
same money-safety property in-process, fast, for every phase boundary; this
is the one, slower, end-to-end proof that a real kill behaves the same way.

C02b (the related gap the same review surfaced): a process killed mid-write
to the committed LLM cache or a compiled contract file used to leave a
truncated file that crashed the *next* run with a raw parse error, instead
of degrading cleanly. Tested here alongside C02 since it's the other real
"killed mid-write" shape -- see llm/providers/cached.py and
core/contract.py's own dedicated tests (tests/test_provider.py,
tests/test_contract.py) for the unit-level proof; this is the one-line
chaos/ pointer to it for the incident log.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlmodel import Session, select

from chaos.incident import chaos_scenario
from cli.audit import hash_inputs
from cli.loaders import load_calibration_artifact
from core.contract import canonical_json, sha256_of
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from store.models import PHASE_DECOMPOSE_DONE, AuditCheckpoint, JournalEntryRow
from store.resumable import _TEST_PAUSE_ENV_VAR, get_engine, resume_or_run

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = REPO_ROOT / "runs" / "clean-seed42"  # zero-discrepancy: no adjudication call needed either


def _audit_run_id(run_dir: Path) -> str:
    input_hashes = hash_inputs(run_dir)
    input_hash = sha256_of(canonical_json(input_hashes))
    return f"AUD-{input_hash[:12]}"


def _wait_for_phase(store_path: Path, audit_run_id: str, phase: str, timeout: float) -> bool:
    engine = get_engine(store_path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with Session(engine) as session:
            row = session.get(AuditCheckpoint, audit_run_id)
        if row is not None and row.phase == phase:
            return True
        time.sleep(0.05)
    return False


def _audit_argv(run_dir: Path) -> list[str]:
    # `cli` has no __main__.py (only the installed `assay` console-script
    # entry point, cli:main) -- invoking main() directly via -c is more
    # portable across environments than locating that installed script.
    return [
        sys.executable,
        "-c",
        "import sys; from cli import main; main()",
        "audit",
        "--run-dir",
        str(run_dir),
        "--json",
    ]


def _spawn_audit(run_dir: Path, store_path: Path, *, pause_seconds: float | None) -> subprocess.Popen:
    env = dict(os.environ)
    env["ASSAY_STORE_PATH"] = str(store_path)
    # A cache-only run never touches the network -- runs/clean-seed42's
    # rate card is already in the committed .llm_cache/, and this profile
    # plants zero discrepancies, so there is nothing for the adjudicator to
    # be asked about either. The key is never actually used to authenticate
    # anything; GeminiProvider only needs it to be non-empty to construct.
    env.setdefault("GEMINI_API_KEY", "fake-key-never-used-cache-only")
    if pause_seconds is not None:
        env[_TEST_PAUSE_ENV_VAR] = str(pause_seconds)
    return subprocess.Popen(
        _audit_argv(run_dir),
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.mark.timeout(180)
def test_c02_a_killed_process_resumes_without_redoing_decompose_or_double_posting(tmp_path):
    with chaos_scenario(
        "C02",
        title="Process killed mid-audit",
        category="crash",
        failure_injected="the `assay audit` subprocess is killed right after its decompose checkpoint commits",
        expected_behavior=(
            "a restart resumes from the checkpoint: decompose is not redone, and the journal is "
            "posted exactly once, matching an uninterrupted reference run's report_hash"
        ),
    ) as scenario:
        store_path = tmp_path / "store.db"
        audit_run_id = _audit_run_id(RUN_DIR)

        process = _spawn_audit(RUN_DIR, store_path, pause_seconds=5.0)
        try:
            reached = _wait_for_phase(store_path, audit_run_id, PHASE_DECOMPOSE_DONE, timeout=30)
            assert reached, "decompose checkpoint never appeared -- can't test the kill without it"
            process.kill()
            process.wait(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)

        engine = get_engine(store_path)
        with Session(engine) as session:
            row = session.get(AuditCheckpoint, audit_run_id)
            journal_before_resume = list(session.exec(select(JournalEntryRow)).all())
        assert row is not None
        assert row.phase == PHASE_DECOMPOSE_DONE, "nothing past decompose should have committed before the kill"
        assert journal_before_resume == [], "nothing must be posted before decompose even finishes"

        resumed = subprocess.run(
            _audit_argv(RUN_DIR),
            cwd=str(REPO_ROOT),
            env={**os.environ, "ASSAY_STORE_PATH": str(store_path), "GEMINI_API_KEY": "fake-key-never-used-cache-only"},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        # structlog's own log lines share stdout with the --json payload;
        # the report is the one well-formed JSON object at the end. --json
        # emits hash_payload() itself (report_hash is a TELEMETRY_FIELDS
        # exclusion, so it's never a key in this payload) -- rehashing it
        # the same way AuditReport.compute_report_hash() does is the actual
        # byte-identical comparison invariant 4 is about.
        resumed_payload = json.loads(resumed.stdout[resumed.stdout.index("{") :])
        resumed_hash = sha256_of(canonical_json(resumed_payload))

        reference = resume_or_run(
            RUN_DIR,
            CachedProvider(NullProvider()),
            calibration=load_calibration_artifact(),
            store_path=tmp_path / "reference-store.db",
        )

        assert resumed_hash == reference.report_hash

        with Session(engine) as session:
            journal_after_resume = list(session.exec(select(JournalEntryRow)).all())
        assert len(journal_after_resume) == len(reference.journal_entries)
        assert len({r.idempotency_key for r in journal_after_resume}) == len(journal_after_resume)

        scenario.note(
            f"killed at phase={PHASE_DECOMPOSE_DONE}; resumed report_hash matches an uninterrupted "
            f"reference run; {len(journal_after_resume)} journal entries posted exactly once"
        )


class _FlakyOnceProvider:
    def __init__(self, result: dict) -> None:
        self.calls = 0
        self._result = result

    def generate_structured(self, prompt, schema, model_hint) -> dict:
        self.calls += 1
        return self._result


def test_c02b_a_truncated_cache_file_degrades_to_a_refetch_not_a_crash(tmp_path):
    """The other "killed mid-write" shape the same review surfaced: not the
    audit process itself, but a file it reads. core/contract.py's own
    CorruptContractArtifact path (a killed write to a compiled-contract or
    sign-off file) has its dedicated unit coverage in tests/test_contract.py;
    this is the LLM-cache half of the same gap, self-contained here.
    """
    from pydantic import BaseModel

    from llm.providers.cached import CachedProvider

    class _Schema(BaseModel):
        value: str

    with chaos_scenario(
        "C02b",
        title="Truncated LLM cache file on a killed mid-write",
        category="crash",
        failure_injected="a committed .llm_cache/ entry exists but its content is torn mid-write",
        expected_behavior="CachedProvider treats it as a miss and re-fetches, never a raw JSONDecodeError",
    ) as scenario:
        inner = _FlakyOnceProvider({"value": "fresh"})
        provider = CachedProvider(inner, cache_dir=tmp_path)
        provider.generate_structured("prompt", _Schema, "flash")
        assert inner.calls == 1

        cache_file = next(tmp_path.glob("*.json"))
        cache_file.write_text('{"value": "fre', encoding="utf-8")  # torn mid-write

        result = provider.generate_structured("prompt", _Schema, "flash")

        assert result == {"value": "fresh"}
        assert inner.calls == 2, "a corrupt entry must be re-fetched, not crash the caller"
        scenario.note("torn cache entry re-fetched cleanly; see tests/test_contract.py for the contract-file half")
