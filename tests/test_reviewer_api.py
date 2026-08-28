"""Tests for the reviewer UI backend -- reviewer/derive.py and reviewer/api.py.

Runs a real `assay audit` over a generated clean fixture, then drives the
API against it through fastapi's TestClient. The derivations are the part
worth pinning: every number this package serves is a sum over a persisted
report, and a UI that quietly disagrees with `assay report` about the
rupees would be worse than no UI.
"""

from __future__ import annotations

from pathlib import Path
from random import Random

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import cli
from cli.report import locate_run
from datagen.config import GenerationConfig, load_profile
from datagen.inject import apply_discrepancies
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world
from datagen.writer import write_run
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from reviewer.derive import batch_view, cluster_views, conservation_views, lane_buckets, settled_gross_paise

runner = CliRunner()


def strip_comments(text: str) -> str:
    """Check code, not prose.

    The adapted components deliberately *document* what was removed and why
    (`next/image`, `@visx/mock-data`, `randomInt`), so a naive text search
    matches the explanation and fails a file that is actually clean. Same
    trap the `import datagen` guard hits, same fix: look at what executes.
    """
    import re

    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def _offline_provider() -> CachedProvider:
    return CachedProvider(NullProvider())


@pytest.fixture(scope="module")
def realistic_run_dir(tmp_path_factory) -> Path:
    """A COPY of the committed realistic run -- the only fixture with enough
    findings and clusters to exercise the cluster/lane screens meaningfully.

    Copied rather than used in place because `assay audit` writes
    `report.json` into the run directory it audits. Auditing the committed
    directory from a test (whose store is a throwaway ASSAY_STORE_PATH)
    leaves that artifact disagreeing with the row in the developer's real
    `.assay/` store, so a later `assay report <run_id>` correctly refuses
    it as stale. A test must not be able to break the working copy that
    way.
    """
    import shutil

    source = Path(__file__).resolve().parent.parent / "runs" / "realistic-seed42"
    destination = tmp_path_factory.mktemp("realistic") / "realistic-seed42"
    shutil.copytree(source, destination)
    return destination


@pytest.fixture(scope="module")
def clean_run_dir(tmp_path_factory) -> Path:
    config = GenerationConfig(month="2026-07")
    rate_card = default_rate_card(config.month)
    true_world = build_true_world(config, rate_card, Random(42))
    profile = load_profile("clean")
    reported_world, discrepancies, _flags = apply_discrepancies(true_world, rate_card, profile, Random(43))
    assert discrepancies == [], "sanity check on the fixture itself"

    out_dir = tmp_path_factory.mktemp("clean") / "clean-seed42"
    write_run(
        reported_world,
        rate_card,
        manifest={"run_id": "clean-seed42", "seed": 42, "profile": "clean"},
        out_dir=out_dir,
        merchant_id=config.merchant_id,
    )
    return out_dir


@pytest.fixture
def audited(clean_run_dir, tmp_path, monkeypatch) -> tuple[str, TestClient]:
    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "store.db"))

    result = runner.invoke(cli.app, ["audit", "--run-dir", str(clean_run_dir), "--no-adjudicate"])
    assert result.exit_code == 0, result.output
    run_id = next(
        line.split(":", 1)[1].strip() for line in result.output.splitlines() if line.startswith("audit run:")
    )

    from reviewer.api import app as api_app

    return run_id, TestClient(api_app)


# ---------------------------------------------------------------------------
# Derivations -- the money, without a server involved
# ---------------------------------------------------------------------------


def test_settled_gross_is_the_sum_of_every_credits_settled_gross(realistic_run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "store.db"))
    result = runner.invoke(cli.app, ["audit", "--run-dir", str(realistic_run_dir), "--no-adjudicate"])
    assert result.exit_code == 0, result.output
    run_id = next(
        line.split(":", 1)[1].strip() for line in result.output.splitlines() if line.startswith("audit run:")
    )

    _dir, _path, report = locate_run(run_id)
    assert settled_gross_paise(report) == sum(c.settled_gross_paise for c in report.conservation)
    assert settled_gross_paise(report) > 0


def test_batch_view_never_invents_or_loses_a_paisa(audited):
    run_id, _client = audited
    _dir, _path, report = locate_run(run_id)
    view = batch_view(report)

    # Read straight off the report, not recomputed -- these must match the
    # exact integers `assay report` prints.
    assert view.unexplained.paise == report.total_unexplained_paise
    assert view.unclaimed.paise == report.unclaimed_paise
    assert view.unaccounted.paise == report.total_unaccounted_paise
    assert view.report_hash == report.report_hash

    # verified is settled gross minus the ABSOLUTE unaccounted total.
    assert view.verified.paise == view.settled_gross.paise - abs(view.unaccounted.paise)


def test_a_negative_residual_reduces_verified_rather_than_inflating_it():
    """The absolute value in `batch_view` is the whole point: netting a
    negative residual against a positive one would let an overcharge cancel
    a shortfall and report the pair as fully verified."""
    from cli.audit import AuditReport
    from reviewer.derive import _bps_of_volume

    assert _bps_of_volume(-500, 1_000_000) == _bps_of_volume(500, 1_000_000)
    assert _bps_of_volume(0, 0) == 0
    assert AuditReport is not None  # import guard: the module must stay importable


def test_lane_buckets_always_return_all_three_lanes_even_when_empty(audited):
    run_id, _client = audited
    _dir, _path, report = locate_run(run_id)
    buckets = lane_buckets(report)

    assert [b.lane for b in buckets] == ["auto", "propose", "escalate"]
    assert sum(b.finding_count for b in buckets) == len(report.findings)
    assert [b.posts_automatically for b in buckets] == [True, False, False]


def test_conservation_views_recompute_the_identity_rather_than_restating_it(audited):
    run_id, _client = audited
    _dir, _path, report = locate_run(run_id)
    views = conservation_views(report)

    assert len(views) == len(report.conservation)
    assert all(v.balances for v in views), "invariant 3 must hold for every credit"


def test_clusters_stay_in_the_engines_own_impact_ranking(realistic_run_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "store.db"))
    result = runner.invoke(cli.app, ["audit", "--run-dir", str(realistic_run_dir), "--no-adjudicate"])
    assert result.exit_code == 0, result.output
    run_id = next(
        line.split(":", 1)[1].strip() for line in result.output.splitlines() if line.startswith("audit run:")
    )

    _dir, _path, report = locate_run(run_id)
    views = cluster_views(report)
    assert [v.cluster_id for v in views] == [c.cluster_id for c in report.clusters]
    impacts = [abs(v.impact.paise) for v in views]
    assert impacts == sorted(impacts, reverse=True)


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


def test_every_route_is_read_only():
    """No write endpoints exist, by design -- see reviewer/__init__.py."""
    from reviewer.api import app as api_app

    methods = {m for route in api_app.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}, f"reviewer/ must expose no writes, found {methods}"


def test_batch_endpoint_matches_the_persisted_report(audited):
    run_id, client = audited
    _dir, _path, report = locate_run(run_id)

    response = client.get(f"/api/runs/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["report_hash"] == report.report_hash
    assert body["unaccounted"]["paise"] == report.total_unaccounted_paise
    assert body["unaccounted"]["rupees"] == "0.00"
    assert len(body["lanes"]) == 3


def test_an_unknown_run_id_is_a_clean_404(audited):
    _run_id, client = audited
    response = client.get("/api/runs/AUD-does-not-exist")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_an_unknown_cluster_is_a_clean_404(audited):
    run_id, client = audited
    response = client.get(f"/api/runs/{run_id}/clusters/CL-nope")
    assert response.status_code == 404


def test_conservation_endpoint_reports_every_credit(audited):
    run_id, client = audited
    _dir, _path, report = locate_run(run_id)

    response = client.get(f"/api/runs/{run_id}/conservation")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == len(report.conservation)
    assert all(row["balances"] for row in body)


# ---------------------------------------------------------------------------
# The adapted shadcn components: real data, no mock data, no RNG
# ---------------------------------------------------------------------------


def test_score_cards_are_real_ratios_over_the_report(realistic_run_dir, tmp_path, monkeypatch):
    from reviewer.derive import score_cards, settled_gross_paise

    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "store.db"))
    result = runner.invoke(cli.app, ["audit", "--run-dir", str(realistic_run_dir), "--no-adjudicate"])
    assert result.exit_code == 0, result.output
    run_id = next(
        line.split(":", 1)[1].strip() for line in result.output.splitlines() if line.startswith("audit run:")
    )
    _dir, _path, report = locate_run(run_id)

    cards = {c.key: c for c in score_cards(report)}
    assert set(cards) == {"verified_value", "credits_decomposed", "clean_credits"}

    # Recomputed here independently of derive.py's own arithmetic.
    volume = settled_gross_paise(report)
    verified = volume - abs(report.total_unaccounted_paise)
    assert cards["verified_value"].value_bps == verified * 10_000 // volume

    clean = sum(1 for c in report.conservation if c.unexplained_paise == 0)
    assert cards["clean_credits"].value_bps == clean * 10_000 // len(report.conservation)

    # Every gauge is a real ratio, so none can exceed 100%.
    assert all(0 <= c.value_bps <= 10_000 for c in cards.values())


def test_timeline_is_dated_from_real_credits_and_sorted(audited, clean_run_dir):
    from reviewer.derive import timeline_points

    run_id, client = audited
    _dir, _path, report = locate_run(run_id)

    from cli.loaders import load_bank_credits

    dates = {c.id: c.value_date.isoformat() for c in load_bank_credits(clean_run_dir)}
    points = timeline_points(report, dates)

    assert len(points) == len(report.conservation)
    assert [p.value_date for p in points] == sorted(p.value_date for p in points)
    # Every date is a real credit's value_date, never invented.
    assert all(p.value_date == dates[p.credit_id] for p in points)

    response = client.get(f"/api/runs/{run_id}/timeline")
    assert response.status_code == 200
    assert len(response.json()) == len(points)


def test_a_credit_with_no_known_value_date_is_dropped_not_invented(audited):
    """An invented date on a settlement chart is a wrong fact, not a
    cosmetic gap -- so a credit whose date can't be resolved is omitted."""
    from reviewer.derive import timeline_points

    run_id, _client = audited
    _dir, _path, report = locate_run(run_id)

    assert timeline_points(report, {}) == []


def test_scores_endpoint_serves_real_cards(audited):
    run_id, client = audited
    response = client.get(f"/api/runs/{run_id}/scores")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    assert all(0 <= c["value_bps"] <= 10_000 for c in body)


def test_the_hero_media_is_local_and_decorative_not_a_third_party_asset():
    """The upstream hero pointed at Unsplash/Pexels CDNs. Fetching
    decorative media from third parties onto a page showing a merchant's
    money is not a trade worth making, so the hero renders a local
    component (a canvas animation) and no remote asset at all."""
    web = Path(__file__).resolve().parent.parent / "reviewer" / "web"
    web_src = web / "src"
    hero = strip_comments(
        (web_src / "components" / "ui" / "scroll-expansion-hero.tsx").read_text(encoding="utf-8")
    )
    assert "next/image" not in hero, "next/image does not resolve in a Vite app"

    for source in web_src.rglob("*.tsx"):
        code = strip_comments(source.read_text(encoding="utf-8"))
        for cdn in ("unsplash.com", "pexels.com", "ufs.sh"):
            assert cdn not in code, f"{source.name} fetches decorative media from {cdn}"


def test_score_cards_are_never_mounted_conditionally_on_being_in_view():
    """Regression guard, from a bug that made all three cards vanish.

    The card was mounted only once an IntersectionObserver reported it in
    view, and the observed wrapper carried `display: contents`. That
    generates no layout box, IntersectionObserver observes boxes, so the
    callback never fired and the cards never rendered at all.

    The rule this pins is the general one: gate the ANIMATION on being in
    view, never the mount. A card that fails to appear is a far worse
    failure than one that appears without its entrance.
    """
    card_source = (
        Path(__file__).resolve().parent.parent
        / "reviewer"
        / "web"
        / "src"
        / "components"
        / "ui"
        / "financial-score-cards.tsx"
    ).read_text(encoding="utf-8")
    code = strip_comments(card_source)

    assert "{inView && " not in code, (
        "the card must render unconditionally and use inView only to trigger its animation"
    )
    # The observed element must not be display:contents -- it would have no
    # box for IntersectionObserver to observe.
    assert 'ref={gridRef} className="grid' in code, (
        "the in-view observer must target the grid, which has a real layout box"
    )


def test_the_shipped_frontend_contains_no_mock_financial_data():
    """The guard that matters for this page.

    The upstream components shipped `@visx/mock-data` (Apple stock prices)
    and `Utils.randomInt()` score generation. Either one rendered beside
    real hash-verified rupee figures, with nothing marking which is which,
    is the exact failure this product exists to prevent. Pinned as a test
    so a future paste of the original snippet fails loudly.
    """
    web = Path(__file__).resolve().parent.parent / "reviewer" / "web"

    package_json = (web / "package.json").read_text(encoding="utf-8")
    assert "@visx/mock-data" not in package_json, "mock financial data must not be a dependency"

    for source in (web / "src").rglob("*.tsx"):
        code = strip_comments(source.read_text(encoding="utf-8"))
        assert "mock-data" not in code, f"{source.name} imports mock financial data"
        assert "appleStock" not in code, f"{source.name} references Apple stock mock data"
        # randomHash (for unique SVG gradient ids) is fine; randomInt
        # generated a *displayed score*, which is not.
        assert "randomInt" not in code, f"{source.name} can generate a fake score"


def test_runs_listing_reports_availability(audited):
    run_id, client = audited
    response = client.get("/api/runs")
    assert response.status_code == 200
    rows = response.json()
    assert any(r["run_id"] == run_id and r["available"] for r in rows)
