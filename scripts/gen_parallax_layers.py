"""Draw the four parallax layers for site/public/parallax/.

A ticker wall: chart frames, candlesticks, sparklines and quote panels, in
the reviewer's palette. Three depths plus a foreground, drawn as separate
scenes rather than one image scaled three times -- parallax only reads as
depth when the layers are actually different drawings, and near objects are
bigger, sparser and sharper than far ones.

Seeded, so the wall is identical on every regeneration. Regenerate with:

    python scripts/gen_parallax_layers.py

The page will prefer `site/public/parallax/custom-layer-2.png` over the
generated mid layer if that file exists, so a real screenshot can be dropped
in without touching any code.
"""

from __future__ import annotations

import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "public" / "parallax"

W, H = 1600, 900

INK = "#e8ebf0"
DIM = "#8b94a3"
LINE = "#2a313d"
FAINT = "#1c212a"
GOLD = "#e8b44a"

TICKERS = [
    "SETTLED GROSS", "REFUNDS", "FEES", "TAX", "CHARGEBACKS",
    "ADJUSTMENTS", "REVERSALS", "MDR TIER 2", "GST 18%", "UTR JOIN",
    "SUBSET-SUM", "BANK CREDIT", "NET PAYOUT", "RESERVE HOLD",
]


def walk(rng: random.Random, x: float, y: float, w: float, h: float, steps: int, drift: float) -> str:
    """A price line: random walk with a mild trend, clamped into the box."""
    points = []
    value = 0.5
    for i in range(steps):
        value += (rng.random() - 0.5) * 0.22 + drift
        value = min(0.94, max(0.06, value))
        points.append(f"{x + w * i / (steps - 1):.1f},{y + h * (1 - value):.1f}")
    return " ".join(points)


def candles(rng: random.Random, x: float, y: float, w: float, h: float, n: int, colour: str, sw: float) -> list[str]:
    out = []
    step = w / n
    value = 0.5
    for i in range(n):
        value = min(0.9, max(0.1, value + (rng.random() - 0.5) * 0.2))
        span = rng.uniform(0.04, 0.16)
        cx = x + step * i + step / 2
        top = y + h * (1 - min(0.96, value + span))
        bottom = y + h * (1 - max(0.04, value - span))
        out.append(
            f'<line x1="{cx:.1f}" y1="{top:.1f}" x2="{cx:.1f}" y2="{bottom:.1f}" '
            f'stroke="{colour}" stroke-width="{sw:.2f}"/>'
        )
    return out


def panel(
    rng: random.Random,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    stroke: str,
    plot: str,
    sw: float,
    label: bool,
    grid: bool,
    kind: str,
) -> list[str]:
    parts = [
        (
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{sw:.2f}"/>'
        )
    ]
    if grid:
        for i in range(1, 4):
            gy = y + h * i / 4
            parts.append(
                f'<line x1="{x:.1f}" y1="{gy:.1f}" x2="{x + w:.1f}" y2="{gy:.1f}" '
                f'stroke="{stroke}" stroke-width="{sw * 0.6:.2f}"/>'
            )
    inner_x, inner_y = x + w * 0.06, y + h * (0.28 if label else 0.1)
    inner_w, inner_h = w * 0.88, h * (0.6 if label else 0.8)

    if kind == "candles":
        parts += candles(rng, inner_x, inner_y, inner_w, inner_h, max(6, int(w / 14)), plot, sw * 1.4)
    else:
        parts.append(
            f'<polyline points="{walk(rng, inner_x, inner_y, inner_w, inner_h, max(14, int(w / 7)), rng.uniform(-0.012, 0.016))}" '
            f'fill="none" stroke="{plot}" stroke-width="{sw * 1.25:.2f}" stroke-linejoin="round"/>'
        )

    if label:
        # Tick marks rather than real glyphs at the far depths: text at 4px
        # renders as mush and costs a font, while a row of rules reads as
        # "there is data here" at every size.
        parts.append(
            f'<rect x="{x + w * 0.06:.1f}" y="{y + h * 0.11:.1f}" width="{w * 0.34:.1f}" '
            f'height="{max(1.5, h * 0.035):.1f}" fill="{stroke}"/>'
        )
        parts.append(
            f'<rect x="{x + w * 0.7:.1f}" y="{y + h * 0.11:.1f}" width="{w * 0.22:.1f}" '
            f'height="{max(1.5, h * 0.035):.1f}" fill="{stroke}"/>'
        )
    return parts


def head(extra: str = "") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" fill="none">{extra}'
    )


def far_layer() -> str:
    """Layer 1 -- deepest. Dense, tiny, nearly dissolved into the ground."""
    rng = random.Random(1042)
    parts = [head(), '<g opacity="0.5">']
    for _ in range(46):
        w = rng.uniform(70, 150)
        h = rng.uniform(44, 88)
        x = rng.uniform(-40, W - w + 40)
        y = rng.uniform(-30, H - h + 30)
        parts += panel(
            rng, x, y, w, h,
            stroke=FAINT, plot=LINE, sw=0.7, label=False, grid=False,
            kind="line" if rng.random() > 0.35 else "candles",
        )
    parts += ["</g></svg>"]
    return "".join(parts)


def mid_layer() -> str:
    """Layer 2 -- the middle distance. Legible frames, still quiet."""
    rng = random.Random(2718)
    parts = [head(), '<g opacity="0.85">']
    for _ in range(17):
        w = rng.uniform(180, 330)
        h = rng.uniform(110, 195)
        x = rng.uniform(-60, W - w + 60)
        y = rng.uniform(-40, H - h + 40)
        parts += panel(
            rng, x, y, w, h,
            stroke=LINE, plot=DIM, sw=1.0, label=True, grid=True,
            kind="line" if rng.random() > 0.4 else "candles",
        )
    parts += ["</g></svg>"]
    return "".join(parts)


def near_layer() -> str:
    """Layer 4 -- foreground. Few, large, sharp, and the only gold on the
    wall: one row that will not reconcile."""
    rng = random.Random(3141)
    parts = [head(), '<g opacity="0.95">']

    boxes = [
        (-70, 70, 520, 330),
        (1090, 430, 560, 350),
        (330, 620, 430, 300),
        (860, -60, 420, 270),
    ]
    for index, (x, y, w, h) in enumerate(boxes):
        parts += panel(
            rng, x, y, w, h,
            stroke="#39414d", plot=INK, sw=1.6, label=True, grid=True,
            kind="candles" if index % 2 else "line",
        )

    # The unexplained residual: the single accent element in the whole scene.
    gx, gy, gw, gh = 1090, 430, 560, 350
    parts.append(
        f'<rect x="{gx}" y="{gy + gh * 0.72:.1f}" width="{gw}" height="{gh * 0.14:.1f}" '
        f'fill="{GOLD}" opacity="0.10"/>'
    )
    parts.append(
        f'<line x1="{gx}" y1="{gy + gh * 0.72:.1f}" x2="{gx}" y2="{gy + gh * 0.86:.1f}" '
        f'stroke="{GOLD}" stroke-width="3"/>'
    )
    parts.append(
        f'<rect x="{gx + 26:.1f}" y="{gy + gh * 0.775:.1f}" width="150" height="5" fill="{GOLD}"/>'
    )
    parts.append(
        f'<rect x="{gx + gw - 190:.1f}" y="{gy + gh * 0.775:.1f}" width="164" height="5" fill="{GOLD}"/>'
    )
    parts += ["</g></svg>"]
    return "".join(parts)


def strip_layer() -> str:
    """A quote strip that sits behind the title, so the headline has
    something structured to read against rather than empty ground."""
    rng = random.Random(1618)
    parts = [head(), '<g opacity="0.6">']
    rows, cols = 7, 5
    for r in range(rows):
        for c in range(cols):
            x = 40 + c * (W - 80) / cols
            y = 40 + r * (H - 80) / rows
            w = (W - 80) / cols - 26
            h = (H - 80) / rows - 20
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                f'fill="none" stroke="{FAINT}" stroke-width="0.9"/>'
            )
            parts.append(
                f'<rect x="{x + 12:.1f}" y="{y + 12:.1f}" width="{w * 0.42:.1f}" height="3" fill="{LINE}"/>'
            )
            parts.append(
                f'<polyline points="{walk(rng, x + 12, y + 30, w - 24, h - 46, 20, 0.004)}" '
                f'fill="none" stroke="{LINE}" stroke-width="1.1"/>'
            )
    parts += ["</g></svg>"]
    return "".join(parts)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    written = {
        "layer-1.svg": far_layer(),
        "layer-2.svg": mid_layer(),
        "layer-3.svg": strip_layer(),
        "layer-4.svg": near_layer(),
    }
    for name, svg in written.items():
        path = OUT / name
        path.write_text(svg, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}  ({len(svg) // 1024} kB)")
    print(f"tickers available for future labelling: {len(TICKERS)}")


if __name__ == "__main__":
    main()
