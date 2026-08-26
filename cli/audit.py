"""One audit, end to end: four input files in, a hashed report out.

This module is the spine the engine never had. `decompose_all`,
`conserve_all`, `verify_all`, `adjudicate_residuals`, `cluster_findings`,
`build_dispute_packet` and `journal_entries_for_auto_findings` were each
built and tested independently; nothing ran them in sequence, and
`AuditRun.report_hash` -- invariant 4's byte-identical SHA-256 -- was
computed nowhere in the repo. It is computed here.

It lives in `cli/` rather than `core/` because an audit's output includes
`llm.adjudicator.AdjudicationRun`, and `core/` may never import `llm/`
(invariant 2). "The CLI is the product", so the product's spine belongs in
the product's package. It contains no arithmetic of its own: every number
on the report comes back from a `core/` function that already has its own
unit tests. What this module adds is sequencing, hashing, and the honest
handling of a missing model.

**What the report hash covers, and what it deliberately does not.**

`report_hash` is `sha256_of(canonical_json(...))` over the report's own
content, reusing `core.contract`'s single hash pair rather than introducing
a second one -- the same discipline as `proof_hash` and
`artifact_content_hash`. Three things are excluded, each for the same
reason: they are observations about *this machine on this day*, not about
what the audit concluded.

  - every proof's `elapsed_ns`,
  - the run's own `started_at` and `wall_clock_ns`,
  - each journal entry's `posted_at`,
  - LLM telemetry (call counts, cache hits, token usage).

`DecompositionProof.lane_assignment` is NOT excluded, even though
`DecompositionProof.TELEMETRY_FIELDS` excludes it from `proof_hash`. The
two hashes answer different questions. A proof's hash asks "why is this
credit these transactions", which does not depend on any calibration
artifact -- so the artifact must not perturb it. A report's hash asks "what
did this audit conclude", and the calibrated lane is part of the
conclusion: the report's own `findings` already carry a calibrated
`lane`/`confidence`, so excluding the proofs' copy would leave the hash
inconsistent about whether the calibration artifact is an input. It is one,
and `calibration_sha256` records which one.

**`audit_run_id` is derived, not generated.** It is
`AUD-<first 12 hex of input_hash>`, so the same inputs produce the same run
id, which produces the same `Finding` ids, which produces the same
idempotency keys -- invariant 7's "re-running an audit over the same batch
must never double-post" falls out of the id scheme rather than relying on a
caller to pass the same random id twice.

**A missing model degrades; it never guesses and never crashes.** The
adjudicator is the only boundary an audit consults, and it only ever
proposes hypotheses about residuals deterministic matching already failed
to explain. `ProviderUnavailable` and `AdjudicationRejected` are caught
here and recorded as a degradation, with its reason. Every finding from
`verify_all` and the whole conservation identity are unaffected. Not asking
at all (`provider=None`) is recorded separately from asking and being
refused -- EVIDENCE.md counts degradations, and conflating the two would
inflate that count.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal

from cli.loaders import infer_merchant_id, load_bank_credits, load_contract, load_ledger
from core.conserve import (
    ConservationReport,
    conserve_all,
    total_unexplained_paise,
    unclaimed_paise,
    unclaimed_records,
)
from core.contract import CompiledContract, canonical_json, sha256_of
from core.decompose import DecompositionOutcome, DecompositionProof, DecompositionTier, decompose_all
from core.exceptions import Cluster, DisputePacket, build_dispute_packet, cluster_findings
from core.lanes import CalibrationArtifact
from core.ledger import Ledger, journal_entries_for_auto_findings
from core.models import IST, AssayModel, AuditRun, Finding, JournalEntry, RecordRef
from core.money import Money
from core.verify import verify_all
from eval.determinism import ReproducibilityReport, assert_reproducible
from llm.adjudicator import (
    AdjudicationRejected,
    AdjudicationRun,
    adjudicate_residuals,
    findings_from_adjudication_run,
    residuals_from_run,
)
from llm.provider import LLMProvider, ProviderUnavailable

INPUT_FILENAMES = ("ledger.json", "settlement_report.json", "bank_statement.json", "rate_card.md")

# A wall-clock instant that is not a fact about the audit's conclusions.
# Journal entries are stamped with it and it is excluded from the hash; see
# the module docstring.
_POSTED_AT_FIELD = "posted_at"


class AuditReport(AssayModel):
    """Everything one audit concluded, plus the telemetry it observed while
    concluding it. The two are kept in one object but separated by
    `TELEMETRY_FIELDS`, which is what `compute_report_hash` excludes."""

    TELEMETRY_FIELDS: ClassVar[frozenset[str]] = frozenset({"started_at", "wall_clock_ns", "report_hash"})

    audit_run_id: str
    run_dir: str
    seed: int
    merchant_id: str

    contract_version: str
    contract_source_sha256: str
    calibration_sha256: str | None

    input_hashes: dict[str, str]
    input_hash: str

    proofs: list[DecompositionProof]
    conservation: list[ConservationReport]
    unclaimed: list[RecordRef]

    total_unexplained_paise: int
    unclaimed_paise: int
    total_unaccounted_paise: int

    findings: list[Finding]
    clusters: list[Cluster]
    dispute_packets: list[DisputePacket]
    journal_entries: list[JournalEntry]
    posted_at: datetime

    adjudication: AdjudicationRun | None
    adjudication_degraded: bool
    # A fixed vocabulary, not a message to be pattern-matched. The two
    # degradations mean different things -- an absent provider says nothing
    # about the model, a rejected response says a great deal -- and
    # EVIDENCE.md §10 counts them separately. Sniffing for a substring in
    # `adjudication_degraded_reason` would be a silent mis-count the first
    # time an exception's wording changed.
    adjudication_degraded_kind: Literal["provider_unavailable", "schema_rejected"] | None = None
    adjudication_degraded_reason: str | None

    reproducibility: ReproducibilityReport
    record_count: int

    started_at: str
    wall_clock_ns: int

    report_hash: str = ""

    def hash_payload(self) -> dict:
        """The report's content, with every wall-clock observation removed.
        See the module docstring for why each exclusion is there."""
        payload = self.model_dump(mode="json", exclude=set(self.TELEMETRY_FIELDS))
        payload["proofs"] = [
            proof.model_dump(mode="json", exclude={"elapsed_ns"}) for proof in self.proofs
        ]
        payload["journal_entries"] = [
            entry.model_dump(mode="json", exclude={_POSTED_AT_FIELD}) for entry in self.journal_entries
        ]
        payload.pop(_POSTED_AT_FIELD, None)
        return payload

    def compute_report_hash(self) -> str:
        return sha256_of(canonical_json(self.hash_payload()))

    def audit_run_record(self) -> AuditRun:
        """The append-only `AuditRun` row for this audit. Built on demand
        rather than stored on the report, because it carries `report_hash`
        and would otherwise have to exist before the hash it contains."""
        return AuditRun(
            id=self.audit_run_id,
            started_at=datetime.fromisoformat(self.started_at),
            contract_version=self.contract_version,
            input_hashes=self.input_hashes,
            seed=self.seed,
            report_hash=self.report_hash,
        )

    def summary_lines(self) -> list[str]:
        resolved = sum(1 for p in self.proofs if p.outcome is DecompositionOutcome.RESOLVED)
        tiers = [p.tier for p in self.proofs]
        lines = [
            f"audit run:        {self.audit_run_id}",
            f"contract:         {self.contract_version}",
            f"records:          {self.record_count}",
            f"bank credits:     {len(self.proofs)} ({resolved} resolved)",
            f"  structural:     {tiers.count(DecompositionTier.STRUCTURAL)}",
            f"  subset-sum:     {tiers.count(DecompositionTier.SUBSET_SUM)}",
            f"  assignment:     {tiers.count(DecompositionTier.ASSIGNMENT)}",
            f"findings:         {len(self.findings)} in {len(self.clusters)} clusters",
            f"unexplained:      {Money(self.total_unexplained_paise).to_rupees_str()}",
            f"unclaimed:        {Money(self.unclaimed_paise).to_rupees_str()}",
            f"total unaccounted:{Money(self.total_unaccounted_paise).to_rupees_str()}",
            f"journal entries:  {len(self.journal_entries)} posted (AUTO lane only)",
        ]
        if self.adjudication_degraded:
            lines.append(f"adjudication:     DEGRADED -- {self.adjudication_degraded_reason}")
        elif self.adjudication is not None:
            lines.append(
                f"adjudication:     {self.adjudication.residuals_submitted} residuals, "
                f"{self.adjudication.api_call_count} API calls"
            )
        else:
            lines.append("adjudication:     skipped (no provider supplied)")
        lines.append(f"report hash:      {self.report_hash}")
        return lines


def hash_inputs(run_dir: Path) -> dict[str, str]:
    """SHA-256 of each of the four input files, read as bytes.

    Bytes, not text: a report that claims to hash its inputs must hash what
    is actually on disk, including the line endings -- which on Windows is
    not a hypothetical difference.
    """
    return {name: sha256_of_bytes((run_dir / name).read_bytes()) for name in INPUT_FILENAMES}


def sha256_of_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_seed(run_dir: Path) -> int:
    """Provenance only. The audit engine contains no RNG -- `seed` describes
    which synthetic dataset this run's inputs came from, when a manifest
    happens to say so, and is 0 for real inputs that have no such notion."""
    manifest = run_dir / "manifest.json"
    if not manifest.is_file():
        return 0
    try:
        return int(json.loads(manifest.read_text(encoding="utf-8")).get("seed", 0))
    except (ValueError, TypeError):
        return 0


def _adjudicate(
    proofs: list[DecompositionProof],
    conservation: list[ConservationReport],
    ledger: Ledger,
    unclaimed: list[RecordRef],
    provider: LLMProvider,
    audit_run_id: str,
) -> tuple[AdjudicationRun | None, list[Finding], str | None, str | None]:
    """Hypotheses for what deterministic matching could not explain, or an
    honest reason there are none. Never raises."""
    try:
        build = residuals_from_run(proofs, conservation, ledger, unclaimed=unclaimed)
        if not build.cases:
            return (
                AdjudicationRun(
                    results=[],
                    skipped_no_evidence=build.skipped_no_evidence,
                    api_call_count=0,
                    residuals_submitted=0,
                ),
                [],
                None,
                None,
            )
        run = adjudicate_residuals(
            build.cases, ledger, provider, skipped_no_evidence=build.skipped_no_evidence
        )
    except ProviderUnavailable as error:
        return None, [], "provider_unavailable", f"provider unavailable: {error}"
    except AdjudicationRejected as error:
        return None, [], "schema_rejected", f"model response rejected: {error}"
    return run, findings_from_adjudication_run(run, audit_run_id), None, None


def run_audit(
    run_dir: Path | str,
    provider: LLMProvider | None = None,
    *,
    merchant_id: str | None = None,
    calibration: CalibrationArtifact | None = None,
    contract: CompiledContract | None = None,
    adjudicate: bool = True,
) -> AuditReport:
    """Audit one run directory end to end.

    `provider` is used for two things: compiling the rate card (a cached
    parse, in practice always a cache hit) and adjudicating residuals.
    `None` means neither -- the caller must then supply `contract`, or this
    raises, because an audit with no contract has nothing to recompute
    against and a fabricated one would be worse than no answer.

    `calibration` is optional. Without it, findings carry the uncalibrated
    placeholder confidence/lane that `core/verify.py` has always used, and
    nothing can reach AUTO. With it, `core/lanes.py` decides the lane.
    """
    started_at = datetime.now(IST)
    start_ns = time.monotonic_ns()
    run_dir = Path(run_dir)

    input_hashes = hash_inputs(run_dir)
    input_hash = sha256_of(canonical_json(input_hashes))
    audit_run_id = f"AUD-{input_hash[:12]}"

    ledger = load_ledger(run_dir)
    credits = load_bank_credits(run_dir)
    if merchant_id is None:
        merchant_id = infer_merchant_id(ledger)
    if contract is None:
        if provider is None:
            raise ValueError(
                "run_audit needs either a provider (to compile the rate card) or an already-compiled "
                "contract; an audit with no contract has nothing to recompute deductions against"
            )
        contract = load_contract(run_dir, provider)

    proofs = decompose_all(credits, ledger, merchant_id=merchant_id, calibration=calibration)
    reproducibility = assert_reproducible(proofs)

    conservation = conserve_all(proofs, ledger)
    unclaimed = unclaimed_records(ledger, proofs)

    findings = list(verify_all(proofs, ledger, contract, audit_run_id=audit_run_id, calibration=calibration))

    adjudication: AdjudicationRun | None = None
    degraded_kind: str | None = None
    degraded_reason: str | None = None
    if adjudicate and provider is not None:
        adjudication, adjudicated_findings, degraded_kind, degraded_reason = _adjudicate(
            proofs, conservation, ledger, unclaimed, provider, audit_run_id
        )
        findings.extend(adjudicated_findings)

    findings.sort(key=lambda f: f.id)
    clusters = cluster_findings(findings, ledger, audit_run_id=audit_run_id)
    packets = [build_dispute_packet(cluster, findings, contract) for cluster in clusters]

    posted_at = started_at
    journal_entries = journal_entries_for_auto_findings(
        findings, input_hash=input_hash, posted_at=posted_at
    )

    unexplained = total_unexplained_paise(conservation)
    unclaimed_total = unclaimed_paise(ledger, proofs)

    report = AuditReport(
        audit_run_id=audit_run_id,
        run_dir=str(run_dir),
        seed=_read_seed(run_dir),
        merchant_id=merchant_id,
        contract_version=contract.version_id,
        contract_source_sha256=contract.source_sha256,
        calibration_sha256=calibration.artifact_sha256 if calibration is not None else None,
        input_hashes=input_hashes,
        input_hash=input_hash,
        proofs=proofs,
        conservation=conservation,
        unclaimed=unclaimed,
        total_unexplained_paise=unexplained,
        unclaimed_paise=unclaimed_total,
        total_unaccounted_paise=unexplained + unclaimed_total,
        findings=findings,
        clusters=clusters,
        dispute_packets=packets,
        journal_entries=journal_entries,
        posted_at=posted_at,
        adjudication=adjudication,
        adjudication_degraded=degraded_kind is not None,
        adjudication_degraded_kind=degraded_kind,
        adjudication_degraded_reason=degraded_reason,
        reproducibility=reproducibility,
        record_count=len(ledger),
        started_at=started_at.isoformat(),
        wall_clock_ns=time.monotonic_ns() - start_ns,
    )
    return report.model_copy(update={"report_hash": report.compute_report_hash()})
