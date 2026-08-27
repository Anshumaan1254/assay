"""`assay fetch` -- one month of real Razorpay data into a run directory.

Writes the same four files `datagen/writer.py` writes, so the output is
interchangeable with a generated run and `assay audit --run-dir ...` needs
no knowledge of where it came from.

Three of the four are fetched. `rate_card.md` is not, and cannot be: no
gateway exposes your negotiated rate card as data, which is the entire
reason `llm/contract_parser.py` exists. It is supplied with --rate-card
and copied in verbatim, and the command refuses to run without one rather
than writing a run directory that `assay audit` would later reject.
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta
from pathlib import Path

import typer

from core.models import IST
from core.money import Money
from ingest.mapping import MappedRun, map_run
from ingest.razorpay import RazorpayClient, RazorpayUnavailable

app = typer.Typer(help="Fetch real settlement data into an auditable run directory.")

# A settlement can be created days after the cycle it pays out, and a
# payment captured weeks before the month it settles in. Both windows are
# padded rather than clipped to the month: a settlement no recon row
# references is dropped by the mapping anyway, and an unreferenced payment
# is simply never looked up, so padding costs fetch time and nothing else.
# Under-fetching, by contrast, silently quarantines real transactions.
SETTLEMENT_PAD = timedelta(days=7)
PAYMENT_LOOKBACK = timedelta(days=35)

BANK_STATEMENT_SOURCE = "gateway_self_reported"


def _month_bounds(year: int, month: int, day: int | None) -> tuple[datetime, datetime]:
    if day is not None:
        start = datetime(year, month, day, tzinfo=IST)
        return start, start + timedelta(days=1) - timedelta(seconds=1)
    last = calendar.monthrange(year, month)[1]
    return datetime(year, month, 1, tzinfo=IST), datetime(year, month, last, 23, 59, 59, tzinfo=IST)


def _dump(models) -> list[dict]:
    return [m.model_dump(mode="json") for m in models]


def write_run_dir(run: MappedRun, out_dir: Path, rate_card: Path, manifest: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "ledger.json").write_text(
        json.dumps(
            {
                "payments": _dump(run.payments),
                "refunds": _dump(run.refunds),
                "chargebacks": _dump(run.chargebacks),
                "adjustments": _dump(run.adjustments),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "settlement_report.json").write_text(
        json.dumps(
            {
                "fee_lines": _dump(run.fee_lines),
                "tax_lines": _dump(run.tax_lines),
                "batches": _dump(run.batches),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "bank_statement.json").write_text(
        json.dumps({"bank_credits": _dump(run.bank_credits)}, indent=2), encoding="utf-8"
    )
    (out_dir / "rate_card.md").write_text(rate_card.read_text(encoding="utf-8"), encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    # Not an input to the audit -- a record of what this fetch declined to
    # represent, so the quarantine count is inspectable rather than only a
    # number printed once at fetch time.
    (out_dir / "quarantined.json").write_text(
        json.dumps([q.model_dump(mode="json") for q in run.quarantined], indent=2), encoding="utf-8"
    )


def fetch_run(
    *,
    year: int,
    month: int,
    day: int | None,
    merchant_id: str,
    mcc: str,
    rate_card: Path,
    out_dir: Path,
    client: RazorpayClient | None = None,
) -> MappedRun:
    """Fetch, map, and write. Returns the mapped run for the caller to
    report on."""
    if not rate_card.is_file():
        raise typer.BadParameter(
            f"--rate-card {rate_card} does not exist. A gateway API cannot supply your negotiated "
            "rate card; export it to markdown and pass it here."
        )

    owned = client is None
    client = client or RazorpayClient()
    try:
        start, end = _month_bounds(year, month, day)
        recon = client.fetch_recon(year, month, day)
        settlements = client.fetch_settlements(
            int((start - SETTLEMENT_PAD).timestamp()), int((end + SETTLEMENT_PAD).timestamp())
        )
        payments = client.fetch_payments(
            int((start - PAYMENT_LOOKBACK).timestamp()), int(end.timestamp())
        )
        is_test_mode = client.config.is_test_mode
        request_count = client.request_count
    finally:
        if owned:
            client.close()

    run = map_run(
        recon_rows=recon,
        settlements=settlements,
        payments=payments,
        merchant_id=merchant_id,
        mcc=mcc,
    )

    by_reason: dict[str, int] = {}
    for quarantined in run.quarantined:
        by_reason[quarantined.reason.value] = by_reason.get(quarantined.reason.value, 0) + 1

    run_id = f"razorpay-{year:04d}-{month:02d}" + (f"-{day:02d}" if day else "")
    manifest = {
        "run_id": run_id,
        "source": "razorpay",
        "mode": "test" if is_test_mode else "live",
        "year": year,
        "month": month,
        "day": day,
        "merchant_id": merchant_id,
        "mcc": mcc,
        "fetched_at": datetime.now(IST).isoformat(),
        "api_requests": request_count,
        # The load-bearing disclosure. `bank_statement.json` here is built
        # from Razorpay's own /v1/settlements, so the credit side is the
        # gateway's claim about what it paid, not independent evidence
        # from a bank. Every fee, tax, refund and adjustment finding is
        # unaffected (core/verify.py recomputes those against the
        # contract), but an underpayment the gateway reported correctly is
        # invisible to a run built this way.
        "bank_statement_source": BANK_STATEMENT_SOURCE,
        "fee_convention": run.fee_convention.value,
        "mapped_records": run.mapped_count,
        "quarantined_total": len(run.quarantined),
        "quarantined_by_reason": by_reason,
        # Money this fetch did not attempt to audit, because its payout
        # contained at least one transaction the mapping cannot represent.
        # Emphatically NOT the audit's `unexplained` bucket: that one means
        # "we looked and cannot account for it", this one means "we did not
        # look". Conflating the two is what makes an audit report unusable.
        "unaudited_paise": run.unaudited_paise,
        # cli/audit.py::_read_seed expects this key; there is no RNG in a
        # real fetch, so it is 0 rather than absent.
        "seed": 0,
    }

    write_run_dir(run, out_dir, rate_card, manifest)
    return run


@app.command()
def fetch(
    year: int = typer.Option(..., help="Four-digit year of the settlement month."),
    month: int = typer.Option(..., min=1, max=12, help="Month, 1-12."),
    day: int | None = typer.Option(None, min=1, max=31, help="Restrict to one day of that month."),
    merchant_id: str = typer.Option(..., help="Your merchant id, as it should appear on every record."),
    mcc: str = typer.Option(..., help="Your merchant category code. Razorpay's API does not report it."),
    rate_card: Path = typer.Option(  # noqa: B008 -- Typer's own documented pattern
        ..., help="Your negotiated rate card, as markdown. Not fetchable; see --help."
    ),
    out: Path = typer.Option(  # noqa: B008 -- Typer's own documented pattern
        ..., help="Run directory to write (the same shape `assay audit --run-dir` consumes)."
    ),
) -> None:
    """Fetch one month of Razorpay settlements into an auditable run directory."""
    try:
        run = fetch_run(
            year=year, month=month, day=day, merchant_id=merchant_id,
            mcc=mcc, rate_card=rate_card, out_dir=out,
        )
    except RazorpayUnavailable as error:
        typer.echo(f"assay fetch: refused -- {error}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"wrote {out}")
    typer.echo(f"  mapped records:   {run.mapped_count}")
    typer.echo(f"  bank credits:     {len(run.bank_credits)}")
    typer.echo(f"  fee convention:   {run.fee_convention.value}")
    if run.unaudited_paise:
        typer.echo(
            f"  NOT AUDITED:      {Money(run.unaudited_paise).to_rupees_str()} across settlements "
            "containing a transaction this tool cannot represent"
        )
    if run.quarantined:
        typer.echo(f"  quarantined:      {len(run.quarantined)} (see quarantined.json)")
        counts: dict[str, int] = {}
        for quarantined in run.quarantined:
            counts[quarantined.reason.value] = counts.get(quarantined.reason.value, 0) + 1
        for reason, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            typer.echo(f"      {count:>5}  {reason}")
    typer.echo("")
    typer.echo(f"bank statement source: {BANK_STATEMENT_SOURCE} -- this run cannot detect an")
    typer.echo("underpayment Razorpay reported correctly. Supply a real bank statement for that.")
    typer.echo("")
    typer.echo(f"next: assay audit --run-dir {out}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
