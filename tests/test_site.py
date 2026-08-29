"""Guards for `site/` -- the standalone landing page.

The page is static, ships to GitHub Pages, and quotes real rupee figures at
a skeptical reader. Three things therefore have to stay true, and none of
them is checked by a bundler:

  * the numbers it prints still balance under invariant 3;
  * it never reaches into `datagen/`, which holds the answers (invariant 5);
  * it does no arithmetic on money in the browser, which is the same
    position `reviewer/` takes and for the same reason -- an amount that
    was computed twice, once in Python and once in IEEE 754, is an amount
    with two possible values.

`runs/realistic-seed42/report.json` is a derived artifact and is gitignored,
so `site/src/data/audit.json` is the committed truth here. The test that
compares the two runs only when the report happens to be present.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from core.money import Money

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
BAKED = SITE / "src" / "data" / "audit.json"
REPORT = ROOT / "runs" / "realistic-seed42" / "report.json"

TERM_KEYS = (
    "settled_gross",
    "refunds",
    "fees",
    "tax",
    "chargebacks",
    "adjustments",
    "reversals",
    "unexplained",
)


@pytest.fixture(scope="module")
def baked() -> dict:
    assert BAKED.exists(), f"{BAKED} is missing -- run scripts/bake_site_data.py"
    return json.loads(BAKED.read_text(encoding="utf-8"))


def source_files() -> list[Path]:
    """Everything the bundler actually compiles."""
    return [
        path
        for pattern in ("*.ts", "*.tsx")
        for path in (SITE / "src").rglob(pattern)
    ]


def strip_comments(text: str) -> str:
    """Check code, not prose.

    Same trap as `tests/test_reviewer_api.py`: these modules deliberately
    document what they are forbidden to do, so a naive substring search
    matches the explanation and fails a file that is clean.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


# ── invariant 3, restated over what the page will actually print ────────


def test_baked_identity_balances_in_integer_paise(baked: dict) -> None:
    """The eight terms reconstruct the bank credit exactly, with no
    tolerance. If this fails the page is quoting a report that does not
    conserve, and it should not be published."""
    terms = {term["key"]: term["paise"] for term in baked["terms"]}
    assert set(terms) == set(TERM_KEYS)

    reconstructed = (
        terms["settled_gross"]
        - terms["refunds"]
        - terms["fees"]
        - terms["tax"]
        - terms["chargebacks"]
        - terms["adjustments"]
        + terms["reversals"]
        + terms["unexplained"]
    )
    assert reconstructed == baked["credit"]["paise"]


def money_views(node: object) -> list[dict]:
    """Every `{paise, rupees, display}` triple anywhere in the document."""
    found: list[dict] = []
    if isinstance(node, dict):
        if {"paise", "rupees", "display"} <= set(node):
            found.append(node)
        for value in node.values():
            found.extend(money_views(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(money_views(value))
    return found


def test_every_amount_agrees_with_its_own_exact_paise(baked: dict) -> None:
    """The string the browser prints is the integer the engine computed.

    Checked both ways: `rupees` is what `Money` produces for that paise
    value, and `display` is `rupees` with grouping and a symbol added and
    nothing else changed. A comma in the wrong place is a different number.
    """
    views = money_views(baked)
    assert len(views) >= 10, "expected the document to carry many amounts"

    for view in views:
        assert view["rupees"] == Money(view["paise"]).to_rupees_str()
        plain = view["display"].replace("₹", "").replace(",", "")
        assert plain == view["rupees"], f"display {view['display']!r} is not {view['rupees']!r}"


def test_shard_allocation_is_conserved(baked: dict) -> None:
    """Cubes are allocated by largest remainder, so the bins hold every cube
    -- a bin one short of its share reads as a rounding bug."""
    allocated = sum(term["shards"] for term in baked["terms"])
    assert allocated == baked["shards"]["total"]


def test_unclaimed_ids_are_named(baked: dict) -> None:
    """The page names the payments no credit claimed. If that list is empty
    while the unclaimed total is not zero, the page is asserting something
    it cannot show."""
    unaccounted = baked["unaccounted"]
    if unaccounted["unclaimed"]["paise"] != 0:
        assert unaccounted["unclaimed_ids"], "unclaimed rupees with no records named"


@pytest.mark.skipif(not REPORT.exists(), reason="report.json is derived and gitignored")
def test_baked_data_is_not_stale(baked: dict) -> None:
    """When the reference report is present, the committed bake must be the
    bake of *that* report."""
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert baked["provenance"]["report_hash"] == report["report_hash"]
    assert baked["credit"]["paise"] == sum(row["credit_paise"] for row in report["conservation"])


# ── invariant 5: the page never sees ground truth ───────────────────────


def test_site_never_imports_datagen() -> None:
    """`datagen/` plants the answers. A user-facing page with a path to them
    is the first thing a sharp reader checks for."""
    offenders = []
    for path in source_files():
        code = strip_comments(path.read_text(encoding="utf-8"))
        if re.search(r"""\bfrom\s+['"][^'"]*datagen""", code) or re.search(
            r"""\bimport\s*\(\s*['"][^'"]*datagen""", code
        ):
            offenders.append(path.relative_to(ROOT))
    assert not offenders, f"site/ must not import datagen: {offenders}"


# ── the browser does no money arithmetic ────────────────────────────────


def test_site_does_no_money_arithmetic() -> None:
    """Paise are read and printed, never divided.

    Narrow on purpose: it pins the specific ways a rupee figure would get
    turned back into a float -- dividing by 100, or parsing the string form
    -- rather than pretending to detect arithmetic in general. The page has
    no test runner of its own, and adding a DOM one to catch this was not
    worth the dependency.
    """
    offenders = []
    for path in source_files():
        code = strip_comments(path.read_text(encoding="utf-8"))
        if re.search(r"/\s*100\b", code):
            offenders.append((path.relative_to(ROOT), "divides by 100"))
        if "parseFloat" in code:
            offenders.append((path.relative_to(ROOT), "parseFloat"))
        if re.search(r"\.(rupees|display)\s*\)?\s*[-+*/]", code):
            offenders.append((path.relative_to(ROOT), "arithmetic on a rupee string"))
        if re.search(r"Number\s*\(\s*[^)]*\.(rupees|display)", code):
            offenders.append((path.relative_to(ROOT), "Number() on a rupee string"))
    assert not offenders, f"site/ must not compute money: {offenders}"


def test_baked_data_is_generated_not_handwritten(baked: dict) -> None:
    """The marker the bake script writes. Its absence means someone edited
    the numbers by hand, which is exactly the failure this file exists to
    make loud."""
    assert "bake_site_data.py" in baked["_generated_by"]


# ── the pasted components, and what they must keep doing ────────────────


def test_parallax_layers_exist() -> None:
    """`parallax-scrolling.tsx` names four layer files. They are generated by
    `scripts/gen_parallax_layers.py`, not committed by hand, so a missing one
    is a silently blank layer rather than a build error."""
    layers = SITE / "public" / "parallax"
    missing = [n for n in ("layer-1.svg", "layer-2.svg", "layer-3.svg", "layer-4.svg")
               if not (layers / n).exists()]
    assert not missing, f"run scripts/gen_parallax_layers.py: {missing}"


def test_only_one_smooth_scroll_instance_is_ever_constructed() -> None:
    """Both pasted components originally built their own Lenis. Two of them
    hijacking the wheel and both driving ScrollTrigger is not smoother --
    it is two objects disagreeing about the scroll position. They share the
    refcounted singleton in lib/smooth-scroll.ts instead, and that module is
    the only place allowed to call the constructor."""
    offenders = []
    for path in source_files():
        if path.name == "smooth-scroll.ts":
            continue
        code = strip_comments(path.read_text(encoding="utf-8"))
        if re.search(r"new\s+Lenis\s*\(", code):
            offenders.append(path.relative_to(ROOT))
    assert not offenders, f"only lib/smooth-scroll.ts may construct Lenis: {offenders}"


def test_the_hero_can_always_be_escaped() -> None:
    """`scroll-locked-video-hero.tsx` pins the body with position:fixed. As
    pasted, it only ever released that on unmount, so nothing below it was
    reachable. The release paths are the difference between a hero and a
    trap, and there is no other test that would notice them going away."""
    code = strip_comments(
        (SITE / "src" / "components" / "ui" / "scroll-locked-video-hero.tsx").read_text(
            encoding="utf-8"
        )
    )
    assert code.count("releaseLock()") >= 3, "expected wheel, touch and keyboard release paths"
    assert "keydown" in code, "keyboard users need a way past the locked hero"
    assert "reduceMotion) engageLock()" in code, "reduced motion must never lock the page"


def test_reviewer_is_linked_and_unmodified() -> None:
    """The page has to lead somewhere. It links the reviewer by source URL
    because the reviewer is a FastAPI app over a computed audit and has no
    static address -- and crucially it does not import or re-implement any
    of it."""
    audit_ts = (SITE / "src" / "lib" / "audit.ts").read_text(encoding="utf-8")
    assert "tree/main/reviewer" in audit_ts

    offenders = [
        path.relative_to(ROOT)
        for path in source_files()
        if re.search(r"""from\s+['"][^'"]*\breviewer\b""", strip_comments(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, f"site/ must not import from reviewer/: {offenders}"


# ── the two Vercel deployments ──────────────────────────────────────────


def test_landing_and_reviewer_deploy_separately() -> None:
    """Two Vercel projects, one repo. They are told apart only by Root
    Directory, so each config has to describe its own app and nothing else --
    a root config that built the site would deploy the landing page over the
    reviewer's domain."""
    root = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    landing = json.loads((SITE / "vercel.json").read_text(encoding="utf-8"))

    # Root = the FastAPI reviewer: no static output directory, and its build
    # is the Python one.
    assert "outputDirectory" not in root, "the reviewer is a function, not a static site"
    assert "reviewer" in root["buildCommand"]
    assert "reviewer/vercel_app.py" in root["functions"]

    # site/ = the static landing page.
    assert landing["outputDirectory"] == "dist"
    assert landing["framework"] == "vite"


def test_reviewer_entrypoint_is_declared_for_vercel() -> None:
    """Vercel only auto-detects a FastAPI `app` in app/index/server/main/
    wsgi/asgi at the root or in src//app//api/. This app lives at
    reviewer/api.py, which matches none of those -- without the explicit
    entrypoint the build fails with 'No FastAPI entrypoint found'."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entrypoint = config["tool"]["vercel"]["entrypoint"]
    assert entrypoint == "reviewer.vercel_app:app"

    module = ROOT / "reviewer" / "vercel_app.py"
    assert module.is_file(), "the declared entrypoint does not exist"
    assert "from reviewer.api import app" in module.read_text(encoding="utf-8")


def test_reviewer_api_itself_is_untouched_by_the_deployment() -> None:
    """The deployment shim exists precisely so `reviewer/api.py` does not
    have to know it is on Vercel. If serverless concerns leak into the app,
    the local `make ui` path and the deployed one have diverged."""
    api = (ROOT / "reviewer" / "api.py").read_text(encoding="utf-8")
    for leak in ("vercel", "/tmp", "ASSAY_STORE_PATH"):
        assert leak not in api.lower(), f"deployment concern {leak!r} leaked into reviewer/api.py"


def test_deployed_reviewer_cannot_reach_ground_truth() -> None:
    """Invariant 5, over what actually ships. Walks the import closure of the
    Vercel entrypoint and asserts `datagen` is not in it -- the deployment is
    a public URL, and a path from it to the planted answers is the first
    thing worth checking."""
    import ast

    local = {"core", "llm", "cli", "store", "reviewer", "ingest", "eval", "chaos", "datagen"}
    seen: set[str] = set()
    stack = ["reviewer/vercel_app.py"]

    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        path = ROOT / current
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.split(".")[0] not in local:
                    continue
                as_module = ROOT / (name.replace(".", "/") + ".py")
                as_package = ROOT / name.replace(".", "/") / "__init__.py"
                if as_module.is_file():
                    stack.append(str(as_module.relative_to(ROOT)).replace("\\", "/"))
                elif as_package.is_file():
                    stack.append(str(as_package.relative_to(ROOT)).replace("\\", "/"))

    reached_datagen = [m for m in seen if m.startswith("datagen/")]
    assert not reached_datagen, f"the deployed reviewer can import datagen: {reached_datagen}"
