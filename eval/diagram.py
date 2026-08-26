"""The reliability diagram, rendered twice.

`docs/reliability.svg` is built here by string formatting -- no plotting
library, no font metrics, no rasteriser. It is byte-deterministic, so
regenerating EVIDENCE.md over unchanged results produces an unchanged file
and `git diff` stays honest about whether anything actually moved. It is
also readable as text, which matters for a file committed next to a
document whose subject is verifiability. This is the one EVIDENCE.md
embeds.

`docs/reliability.png` is matplotlib's rendering of the same numbers, for
contexts that will not display an SVG. It is NOT byte-stable across
matplotlib and freetype versions, so it is deliberately not the artifact
the document's determinism claim rests on.

A reliability diagram earns its place here by showing a specific thing: a
model that says "90% confident" should be right 90% of the time. The
diagonal is perfect calibration, points above it are underconfidence,
points below it are the dangerous direction -- claiming more certainty than
the evidence supports.

**Marker area is proportional to bin population, and that is not
decoration.** This engine's confidence distribution is extremely
concentrated: a typical sweep puts 99.9% of its labelled decisions in the
top bin and single-digit counts in several others. Drawn with uniform
markers, the curve lurches between 0.0 and 1.0 on bins holding one or three
points and looks alarming, while the expected calibration error -- which is
population-weighted -- is near zero. Both are true; the unweighted picture
is the misleading one. Sizing each marker by its bin's population makes the
diagram agree with the statistic printed beside it, and each point is
labelled with its own n so the reader can check rather than trust.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

from eval.harness import ConfidenceBin

DOCS_DIR = Path("docs")
SVG_PATH = DOCS_DIR / "reliability.svg"
PNG_PATH = DOCS_DIR / "reliability.png"

# Plot geometry, in SVG user units.
_W, _H = 720, 420
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 70, 30, 40, 60
_PLOT_W = _W - _PAD_L - _PAD_R
_PLOT_H = _H - _PAD_T - _PAD_B


def _x(fraction: float) -> float:
    return _PAD_L + fraction * _PLOT_W


def _y(fraction: float) -> float:
    return _PAD_T + (1.0 - fraction) * _PLOT_H


def _f(value: float) -> str:
    """Fixed 2dp. Every coordinate goes through this so the output cannot
    vary with float repr across platforms."""
    return f"{value:.2f}"


_MIN_MARKER_R = 2.5
_MAX_MARKER_R = 18.0

# SVG text-anchor -> matplotlib horizontalalignment, so both renderers place
# a label from one decision rather than two that can drift apart.
_MPL_ALIGNMENT = {"start": "left", "middle": "center", "end": "right"}


def _marker_radius(count: int, max_count: int) -> float:
    """Marker area scaled by bin population, on a log scale.

    Log, not linear: populations here span five orders of magnitude, and a
    linear scale would render every bin except the largest as a dot too
    small to see -- which is the opposite failure from uniform markers but
    just as unreadable. A floor keeps a one-point bin visible, because a
    bin that exists should be visible even when it barely matters.
    """
    if max_count <= 0:
        return _MIN_MARKER_R
    weight = math.log10(count + 1) / math.log10(max_count + 1)
    area = _MIN_MARKER_R**2 + weight * (_MAX_MARKER_R**2 - _MIN_MARKER_R**2)
    return math.sqrt(area)


def _label_anchor(mean_confidence: float) -> str:
    """Keep a point's n= label inside the plot.

    The top bin routinely sits at a mean confidence of 1.000 -- hard against
    the right edge -- and a centred label there is clipped. Mirrored at the
    left for symmetry, since the lowest bin sits at 0.000 just as often.
    """
    if mean_confidence >= 0.92:
        return "end"
    if mean_confidence <= 0.08:
        return "start"
    return "middle"


def render_svg(bins: Sequence[ConfidenceBin], *, ece: float, brier: float, total: int) -> str:
    populated = [b for b in bins if b.count]
    max_count = max((b.count for b in populated), default=1)
    parts: list[str] = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_W} {_H}" width="{_W}" height="{_H}" '
            'font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="11">'
        ),
        # An explicit background: this file is viewed on light and dark
        # backgrounds alike, and a transparent chart is unreadable on one
        # of them.
        f'<rect width="{_W}" height="{_H}" fill="#ffffff"/>',
        f'<text x="{_PAD_L}" y="24" font-size="14" fill="#111111">Reliability: predicted confidence vs observed accuracy</text>',
    ]

    for step in range(11):
        fraction = step / 10
        gx, gy = _x(fraction), _y(fraction)
        parts.append(
            f'<line x1="{_f(gx)}" y1="{_f(_PAD_T)}" x2="{_f(gx)}" y2="{_f(_PAD_T + _PLOT_H)}" stroke="#eeeeee"/>'
        )
        parts.append(
            f'<line x1="{_f(_PAD_L)}" y1="{_f(gy)}" x2="{_f(_PAD_L + _PLOT_W)}" y2="{_f(gy)}" stroke="#eeeeee"/>'
        )
        if step % 2 == 0:
            parts.append(
                f'<text x="{_f(gx)}" y="{_f(_PAD_T + _PLOT_H + 16)}" text-anchor="middle" fill="#555555">{fraction:.1f}</text>'
            )
            parts.append(
                f'<text x="{_f(_PAD_L - 8)}" y="{_f(gy + 4)}" text-anchor="end" fill="#555555">{fraction:.1f}</text>'
            )

    parts.append(
        f'<line x1="{_f(_x(0))}" y1="{_f(_y(0))}" x2="{_f(_x(1))}" y2="{_f(_y(1))}" '
        'stroke="#999999" stroke-dasharray="4 3"/>'
    )
    parts.append(
        f'<text x="{_f(_x(0.62))}" y="{_f(_y(0.68))}" fill="#999999" transform="rotate(-32 '
        f'{_f(_x(0.62))} {_f(_y(0.68))})">perfect calibration</text>'
    )

    if populated:
        points = " ".join(
            f"{_f(_x(b.mean_confidence))},{_f(_y(b.empirical_accuracy))}" for b in populated
        )
        # The connecting line is deliberately faint. It joins bins that can
        # differ in population by five orders of magnitude, so it suggests a
        # continuity the data does not have; the markers carry the meaning.
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="#1f4e8c" stroke-width="1" opacity="0.35"/>'
        )
        for bucket in populated:
            cx, cy = _x(bucket.mean_confidence), _y(bucket.empirical_accuracy)
            parts.append(
                f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(_marker_radius(bucket.count, max_count))}" '
                'fill="#1f4e8c" fill-opacity="0.75"/>'
            )
            parts.append(
                f'<text x="{_f(cx)}" y="{_f(cy - _marker_radius(bucket.count, max_count) - 5)}" '
                f'text-anchor="{_label_anchor(bucket.mean_confidence)}" font-size="9" '
                f'fill="#1f4e8c">n={bucket.count:,}</text>'
            )

    parts.append(
        f'<rect x="{_f(_PAD_L)}" y="{_f(_PAD_T)}" width="{_f(_PLOT_W)}" height="{_f(_PLOT_H)}" '
        'fill="none" stroke="#bbbbbb"/>'
    )
    parts.append(
        f'<text x="{_f(_PAD_L + _PLOT_W / 2)}" y="{_H - 18}" text-anchor="middle" fill="#333333">'
        "predicted confidence (raw, basis points / 10,000)</text>"
    )
    parts.append(
        f'<text x="16" y="{_f(_PAD_T + _PLOT_H / 2)}" text-anchor="middle" fill="#333333" '
        f'transform="rotate(-90 16 {_f(_PAD_T + _PLOT_H / 2)})">observed accuracy</text>'
    )
    parts.append(
        f'<text x="{_f(_PAD_L + _PLOT_W)}" y="24" text-anchor="end" fill="#333333">'
        f"ECE {ece:.4f}  Brier {brier:.4f}  n={total:,}</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_svg(bins: Sequence[ConfidenceBin], *, ece: float, brier: float, total: int, path: Path = SVG_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_svg(bins, ece=ece, brier=brier, total=total), encoding="utf-8")
    return path


def write_png(bins: Sequence[ConfidenceBin], *, ece: float, brier: float, total: int, path: Path = PNG_PATH) -> Path | None:
    """matplotlib's rendering of the same numbers.

    Returns None rather than raising if matplotlib is unavailable: the SVG
    is the artifact the document depends on, and a missing optional
    renderer must not be able to fail a `make evidence` run.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover -- matplotlib is a declared dependency
        return None

    populated = [b for b in bins if b.count]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    figure, axes = plt.subplots(figsize=(7.2, 4.2), dpi=140)
    max_count = max((b.count for b in populated), default=1)
    axes.plot([0, 1], [0, 1], linestyle="--", color="#999999", label="perfect calibration")
    if populated:
        axes.plot(
            [b.mean_confidence for b in populated],
            [b.empirical_accuracy for b in populated],
            color="#1f4e8c",
            alpha=0.35,
            linewidth=1,
        )
        # Marker area by population -- see the module docstring. Without
        # this the curve lurches on bins holding one point and contradicts
        # the population-weighted ECE printed in the title.
        axes.scatter(
            [b.mean_confidence for b in populated],
            [b.empirical_accuracy for b in populated],
            s=[(_marker_radius(b.count, max_count) * 2.2) ** 2 for b in populated],
            color="#1f4e8c",
            alpha=0.75,
            zorder=3,
            label="observed (marker area = bin n)",
        )
        for bucket in populated:
            axes.annotate(
                f"n={bucket.count:,}",
                (bucket.mean_confidence, bucket.empirical_accuracy),
                textcoords="offset points",
                xytext=(0, 12),
                ha=_MPL_ALIGNMENT[_label_anchor(bucket.mean_confidence)],
                fontsize=7,
                color="#1f4e8c",
            )
    axes.set_xlim(-0.02, 1.02)
    # Headroom so the per-point n= labels do not collide with the title.
    axes.set_ylim(-0.05, 1.12)
    axes.set_xlabel("predicted confidence (raw, basis points / 10,000)")
    axes.set_ylabel("observed accuracy")
    axes.set_title(f"Reliability — ECE {ece:.4f}, Brier {brier:.4f}, n={total:,}")
    legend = axes.legend(loc="lower right", fontsize=8, scatterpoints=1)
    for handle in legend.legend_handles:
        if hasattr(handle, "set_sizes"):
            handle.set_sizes([40])
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)
    return path
