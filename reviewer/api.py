"""FastAPI app for the reviewer UI. Read-only, every route a GET.

Reuses `cli/report.py::locate_run` for the run_id -> report.json lookup
rather than reimplementing it, so the UI and `assay report` can never
disagree about which report a run id refers to.
"""

from __future__ import annotations

import functools
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cli.audit import AuditReport
from cli.report import ReportArtifactMissing, ReportArtifactStale, RunNotFound, locate_run
from reviewer.derive import (
    BatchView,
    ClusterDetail,
    ClusterView,
    ConservationView,
    batch_view,
    cluster_detail,
    cluster_views,
    conservation_views,
)

WEB_DIST = Path(__file__).resolve().parent / "web" / "dist"

app = FastAPI(
    title="Assay Reviewer",
    description="Read-only lens over a computed settlement audit.",
    version="0.1.0",
)

# The Vite dev server runs on a different origin during development. The
# API serves already-public audit output and accepts no writes, so this is
# not loosening anything that could be abused -- but it is still scoped to
# localhost rather than "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _load(run_id: str) -> AuditReport:
    try:
        _run_dir, _path, report = locate_run(run_id)
    except RunNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except (ReportArtifactMissing, ReportArtifactStale) as error:
        # 409, not 404: the run genuinely exists and is recorded -- what is
        # wrong is the artifact backing it, which is a conflict between two
        # sources of truth rather than a missing resource.
        raise HTTPException(status_code=409, detail=str(error)) from None
    return report


class RunSummary(BaseModel):
    run_id: str
    started_at: str
    contract_version: str
    seed: int
    report_hash: str
    run_dir: str
    available: bool


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/runs", response_model=list[RunSummary])
def list_runs() -> list[RunSummary]:
    """Every audit run the store knows about, newest first.

    `available` reports whether that run's report.json is still on disk --
    a run whose directory was cleaned up is shown rather than hidden, so a
    missing artifact is visible instead of looking like a run that never
    happened.
    """
    from sqlmodel import Session, select

    from store.models import AuditRunRow
    from store.resumable import default_store_path, get_engine

    with Session(get_engine(default_store_path())) as session:
        rows = list(session.exec(select(AuditRunRow)).all())

    rows.sort(key=lambda r: r.started_at, reverse=True)
    return [
        RunSummary(
            run_id=row.id,
            started_at=row.started_at,
            contract_version=row.contract_version,
            seed=row.seed,
            report_hash=row.report_hash,
            run_dir=row.run_dir,
            available=(Path(row.run_dir) / "report.json").is_file(),
        )
        for row in rows
    ]


@app.get("/api/runs/{run_id}", response_model=BatchView)
def get_batch(run_id: str) -> BatchView:
    return batch_view(_load(run_id))


@app.get("/api/runs/{run_id}/clusters", response_model=list[ClusterView])
def get_clusters(run_id: str) -> list[ClusterView]:
    return cluster_views(_load(run_id))


@app.get("/api/runs/{run_id}/clusters/{cluster_id}", response_model=ClusterDetail)
def get_cluster(run_id: str, cluster_id: str) -> ClusterDetail:
    detail = cluster_detail(_load(run_id), cluster_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no cluster {cluster_id!r} in run {run_id!r}")
    return detail


@app.get("/api/runs/{run_id}/conservation", response_model=list[ConservationView])
def get_conservation(run_id: str) -> list[ConservationView]:
    return conservation_views(_load(run_id))


class ExplainResponse(BaseModel):
    record_id: str
    text: str


@functools.lru_cache(maxsize=4)
def _explain_context(run_dir: str):
    """Loading a run and compiling its contract costs a second or two, and
    the drill-down is the one screen a reviewer clicks repeatedly. Cached
    per run directory. The contract compile is a disk-cache hit in practice
    (llm/providers/cached.py), so this never reaches the network."""
    from cli.loaders import (
        default_provider,
        infer_merchant_id,
        load_bank_credits,
        load_calibration_artifact,
        load_contract,
        load_ledger,
    )

    path = Path(run_dir)
    ledger = load_ledger(path)
    return (
        ledger,
        load_bank_credits(path),
        load_contract(path, default_provider()),
        infer_merchant_id(ledger),
        load_calibration_artifact(),
    )


@app.get("/api/runs/{run_id}/explain/{record_id}", response_model=ExplainResponse)
def get_explain(run_id: str, record_id: str) -> ExplainResponse:
    """The single-transaction drill-down, mirroring `assay explain`.

    Deliberately returns `explain_record`'s own preformatted text rather
    than a parallel structured rendering: the CLI is the product, and two
    independent renderings of the same causal chain is two things that can
    disagree about it.
    """
    from cli.explain import explain_record
    from llm.provider import ProviderUnavailable

    report = _load(run_id)
    try:
        ledger, credits, contract, merchant_id, calibration = _explain_context(report.run_dir)
        text = explain_record(record_id, ledger, credits, contract, merchant_id, calibration=calibration)
    except ProviderUnavailable as error:
        raise HTTPException(status_code=503, detail=f"contract unavailable: {error}") from None
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    return ExplainResponse(record_id=record_id, text=text)


# Mounted last so it never shadows /api. Absent until `npm run build` has
# been run; the API is fully usable without it (that is how the Vite dev
# server consumes it).
if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
