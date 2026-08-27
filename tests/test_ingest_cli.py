"""Tests for ingest/cli.py -- fetch to a run directory, end to end.

The claim this file exists to prove is the whole point of the package:
Razorpay-shaped JSON goes in, and what comes out is a run directory
`assay audit` consumes with no knowledge of where it came from. So the
last test here does not inspect the files -- it runs a real audit over
them and checks the conservation identity holds, which is the only way to
know the mapping produced records that actually reconcile.

No network: a stub standing in for RazorpayClient returns fixture dicts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest.cli import BANK_STATEMENT_SOURCE, fetch_run

REPO_ROOT = Path(__file__).resolve().parent.parent
# Reused only for its rate card, which the committed .llm_cache/ already
# has a compiled entry for -- so the audit below runs fully offline.
RATE_CARD = REPO_ROOT / "runs" / "realistic-seed42" / "rate_card.md"

MERCHANT = "MERCH-0001"
MCC = "5411"
# Inside the committed rate card's effective window (2026-07). Dating a
# fixture outside it does not fail loudly -- core/verify.py correctly
# reports a ContractGap per payment and prices nothing -- so the dates
# here are load-bearing, not decoration.
CREATED_AT = 1_782_887_400  # 2026-07-01 12:00 IST
SETTLED_AT = 1_783_060_200  # 2026-07-03 12:00 IST


def _recon(entity_id: str, amount: int, fee: int, tax: int) -> dict:
    return {
        "entity_id": entity_id,
        "type": "payment",
        "amount": amount,
        "debit": 0,
        "credit": amount - fee - tax,
        "currency": "INR",
        "fee": fee,
        "tax": tax,
        "settlement_id": "setl_1",
        "settlement_utr": "UTR00012345",
        "method": "upi",
        "card_network": None,
        "card_type": None,
        "created_at": CREATED_AT,
        "settled_at": SETTLED_AT,
        "settled": True,
    }


def _payment(id_: str, amount: int) -> dict:
    return {
        "id": id_,
        "entity": "payment",
        "amount": amount,
        "currency": "INR",
        "status": "captured",
        "method": "upi",
        "international": False,
        "card": None,
        "created_at": CREATED_AT,
    }


ROWS = [_recon("pay_1", 100_000, 1_500, 270), _recon("pay_2", 250_000, 3_750, 675)]
PAYMENTS = [_payment("pay_1", 100_000), _payment("pay_2", 250_000)]
NET = sum(r["credit"] for r in ROWS)


class _StubClient:
    """Stands in for RazorpayClient with fixture responses."""

    def __init__(self, recon=None, settlements=None, payments=None, test_mode=True):
        self._recon = ROWS if recon is None else recon
        self._settlements = settlements
        self._payments = PAYMENTS if payments is None else payments
        self.request_count = 3
        self.closed = False

        class _Config:
            is_test_mode = test_mode

        self.config = _Config()

    def fetch_recon(self, year, month, day=None):
        return self._recon

    def fetch_settlements(self, from_ts, to_ts):
        if self._settlements is not None:
            return self._settlements
        return [
            {
                "id": "setl_1",
                "entity": "settlement",
                "amount": NET,
                "status": "processed",
                "fees": 0,
                "tax": 0,
                "utr": "UTR00012345",
                "created_at": SETTLED_AT,
            }
        ]

    def fetch_payments(self, from_ts, to_ts):
        return self._payments

    def close(self):
        self.closed = True


def _fetch(tmp_path: Path, client: _StubClient | None = None) -> tuple:
    out = tmp_path / "razorpay-run"
    run = fetch_run(
        year=2025, month=7, day=None, merchant_id=MERCHANT, mcc=MCC,
        rate_card=RATE_CARD, out_dir=out, client=client or _StubClient(),
    )
    return run, out


# ---------------------------------------------------------------------------
# The four files, in the shape cli/loaders.py expects.
# ---------------------------------------------------------------------------


def test_fetch_writes_the_four_input_files_plus_a_manifest(tmp_path):
    _run, out = _fetch(tmp_path)

    for name in ("ledger.json", "settlement_report.json", "bank_statement.json", "rate_card.md"):
        assert (out / name).is_file(), name
    assert (out / "manifest.json").is_file()


def test_the_ledger_and_settlement_report_carry_the_mapped_records(tmp_path):
    _run, out = _fetch(tmp_path)

    ledger = json.loads((out / "ledger.json").read_text(encoding="utf-8"))
    report = json.loads((out / "settlement_report.json").read_text(encoding="utf-8"))
    statement = json.loads((out / "bank_statement.json").read_text(encoding="utf-8"))

    assert [p["id"] for p in ledger["payments"]] == ["pay_1", "pay_2"]
    assert ledger["chargebacks"] == []
    assert len(report["fee_lines"]) == 2
    assert len(report["tax_lines"]) == 2
    assert [b["id"] for b in report["batches"]] == ["setl_1"]
    assert statement["bank_credits"][0]["amount"]["paise"] == NET


def test_the_manifest_records_the_bank_statement_limitation(tmp_path):
    _run, out = _fetch(tmp_path)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["bank_statement_source"] == BANK_STATEMENT_SOURCE
    assert manifest["source"] == "razorpay"
    assert manifest["mode"] == "test"
    assert manifest["seed"] == 0, "cli/audit.py::_read_seed expects this key"


def test_a_live_key_is_recorded_as_live_mode(tmp_path):
    _run, out = _fetch(tmp_path, client=_StubClient(test_mode=False))
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["mode"] == "live"


def test_quarantined_rows_are_written_out_for_inspection(tmp_path):
    # The transfer sits in its own settlement, so it quarantines without
    # taking the audited payout with it. Put it in setl_1 instead and the
    # whole payout drops -- see
    # test_quarantining_a_transaction_never_invents_unexplained_money.
    transfer = {**_recon("trf_1", 1_000, 0, 0), "type": "transfer", "settlement_id": "setl_other"}
    run, out = _fetch(tmp_path, client=_StubClient(recon=[*ROWS, transfer]))

    quarantined = json.loads((out / "quarantined.json").read_text(encoding="utf-8"))
    assert [q["entity_id"] for q in quarantined] == ["trf_1"]
    assert run.mapped_count == 2, "the untouched settlement is still audited in full"

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["quarantined_by_reason"] == {"unsupported_type": 1}
    assert manifest["unaudited_paise"] == 0


def test_a_missing_rate_card_is_refused_before_anything_is_written(tmp_path):
    out = tmp_path / "run"
    with pytest.raises(Exception, match="rate card"):
        fetch_run(
            year=2025, month=7, day=None, merchant_id=MERCHANT, mcc=MCC,
            rate_card=tmp_path / "nope.md", out_dir=out, client=_StubClient(),
        )
    assert not out.exists()


def test_a_client_the_caller_supplied_is_not_closed_by_fetch_run(tmp_path):
    client = _StubClient()
    _fetch(tmp_path, client=client)
    assert client.closed is False, "fetch_run only closes a client it created itself"


# ---------------------------------------------------------------------------
# The claim: what comes out is auditable.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(180)
def test_the_fetched_run_directory_audits_cleanly_end_to_end(tmp_path):
    """Not a file-shape check -- a real audit over the fetched directory.

    Conservation is the assertion that matters: if the mapping emitted
    records that do not net to the bank credit, every audit built on this
    integration would report phantom unexplained rupees.
    """
    from cli.audit import run_audit
    from llm.providers.cached import CachedProvider
    from llm.providers.null import NullProvider

    _run, out = _fetch(tmp_path)

    report = run_audit(out, CachedProvider(NullProvider()), merchant_id=MERCHANT)

    assert report.report_hash
    assert len(report.proofs) == 1
    assert report.total_unaccounted_paise == 0, (
        "the mapped records must net exactly to the credit Razorpay reported"
    )
    # 2 payments + 2 fee lines + 2 tax lines + 1 settlement batch + 1 bank credit.
    assert report.record_count == 8
    assert report.contract_gaps == [], "every fetched payment should be priceable by the contract"
    # The fee lines are genuinely checked, not merely present. The rate
    # card prices UPI as a flat Rs.2.00 FIXED fee with no MDR, while these
    # rows report a 150bps MDR -- so a working recompute must object.
    assert report.findings, "a real contract recompute over real-shaped records must produce findings"
    classes = {f.discrepancy_class.value for f in report.findings}
    assert "fee_overcharge" in classes


@pytest.mark.timeout(180)
def test_quarantining_a_transaction_never_invents_unexplained_money(tmp_path):
    """The invariant the earlier end-to-end test was too weak to catch.

    Every other test in this file maps 100% of rows, so
    `total_unaccounted_paise == 0` passed for the wrong reason. Add one
    international card -- which the mapping cannot represent -- and the
    old code emitted the full payout as a bank credit against partial
    records, reporting the quarantined transaction's whole value as
    unexplained. Rs 4,858.40 of money that was never missing.
    """
    from cli.audit import run_audit
    from llm.providers.cached import CachedProvider
    from llm.providers.null import NullProvider

    intl_row = {**_recon("pay_intl", 500_000, 12_000, 2_160),
                "method": "card", "card_network": "Visa", "card_type": "credit"}
    intl_payment = {**_payment("pay_intl", 500_000), "method": "card", "international": True,
                    "card": {"network": "Visa", "type": "credit"}}
    rows = [*ROWS, intl_row]
    net = sum(r["credit"] for r in rows)
    settlements = [{"id": "setl_1", "entity": "settlement", "amount": net, "status": "processed",
                    "fees": 0, "tax": 0, "utr": "UTR00012345", "created_at": SETTLED_AT}]

    run, out = _fetch(
        tmp_path,
        client=_StubClient(recon=rows, settlements=settlements, payments=[*PAYMENTS, intl_payment]),
    )

    assert run.unaudited_paise == net, "the whole payout is declared not-looked-at"

    report = run_audit(out, CachedProvider(NullProvider()), merchant_id=MERCHANT)

    assert report.total_unaccounted_paise == 0, (
        "a transaction ingest declined to map must never appear as unexplained money"
    )
    assert report.proofs == [], "no credit is offered that the mapped records cannot fully explain"


@pytest.mark.timeout(180)
def test_a_payment_outside_the_contracts_effective_window_is_a_gap_not_a_crash(tmp_path):
    """Real fetched data routinely predates the rate card you have. That
    must report a gap naming the transaction, never abort the audit --
    the same NoApplicableRule path chaos scenario C12 covers, reached here
    through the actual ingest pipeline rather than a hand-built proof.
    """
    from cli.audit import run_audit
    from llm.providers.cached import CachedProvider
    from llm.providers.null import NullProvider

    stale = [{**row, "created_at": 1_751_337_000} for row in ROWS]  # 2025, before the card
    stale_payments = [{**p, "created_at": 1_751_337_000} for p in PAYMENTS]
    _run, out = _fetch(tmp_path, client=_StubClient(recon=stale, payments=stale_payments))

    report = run_audit(out, CachedProvider(NullProvider()), merchant_id=MERCHANT)

    assert report.report_hash, "the audit still produces a number"
    assert len(report.contract_gaps) == 2
    assert {g.payment_ref.id for g in report.contract_gaps} == {"pay_1", "pay_2"}
    assert report.findings == [], "no fee is invented for a transaction the contract cannot price"
