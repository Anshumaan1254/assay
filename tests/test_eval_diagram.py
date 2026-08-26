"""Tests for eval/diagram.py.

The SVG is a committed artifact that EVIDENCE.md embeds, and the document
claims it is byte-deterministic. That claim is worth a test: if it drifts,
every `make evidence` produces a spurious diff and `git diff` stops being a
useful signal about whether any number actually moved.

The marker-scaling tests are here because the diagram's job is to agree
with the statistic printed beside it. A reliability diagram over a
distribution this skewed -- 99.9% of decisions in one bin -- is actively
misleading with uniform markers, and "looks alarming while ECE is 0.003"
is a defect in a document about not over-claiming, even though no number
is wrong.
"""

from __future__ import annotations

from eval.diagram import _label_anchor, _marker_radius, render_svg, write_png, write_svg
from eval.harness import ConfidenceBin, empty_calibration_stats


def _bins(**counts: int) -> list[ConfidenceBin]:
    """Bins keyed by decile index, e.g. _bins(b0=19, b9=221_320)."""
    bins = empty_calibration_stats().bins
    for key, count in counts.items():
        index = int(key.removeprefix("b"))
        target = bins[index]
        target.count = count
        # Put each bin's mean at its own midpoint, as real data does.
        target.sum_confidence_bps = count * (target.bin_lo_bps + target.bin_hi_bps) // 2
        target.positives = count
    return bins


# ---------------------------------------------------------------------------
# Determinism -- the claim EVIDENCE.md makes about this file
# ---------------------------------------------------------------------------


def test_the_same_bins_render_byte_identical_svg():
    bins = _bins(b0=19, b6=33, b8=94, b9=221_320)
    first = render_svg(bins, ece=0.003, brier=0.0029, total=221_466)
    second = render_svg(bins, ece=0.003, brier=0.0029, total=221_466)
    assert first == second


def test_every_coordinate_is_fixed_precision_decimal():
    """The determinism claim rests on this: a raw float repr could differ
    across platforms or Python versions, and scientific notation for a very
    small coordinate would differ from a decimal one. Checked against the
    coordinate attributes themselves rather than by searching the whole
    document for "e-", which also matches `stroke-dasharray`."""
    import re

    svg = render_svg(_bins(b0=1, b9=221_320), ece=0.1, brier=0.1, total=221_321)
    coordinates = re.findall(r'\b(?:x|y|cx|cy|r|x1|y1|x2|y2|width|height)="([^"]+)"', svg)
    assert coordinates
    for value in coordinates:
        assert re.fullmatch(r"-?\d+(\.\d{2})?", value), f"{value!r} is not fixed-precision decimal"


def test_writing_the_svg_twice_leaves_the_file_unchanged(tmp_path):
    bins = _bins(b0=19, b9=221_320)
    path = tmp_path / "r.svg"
    write_svg(bins, ece=0.003, brier=0.003, total=221_339, path=path)
    first = path.read_bytes()
    write_svg(bins, ece=0.003, brier=0.003, total=221_339, path=path)
    assert path.read_bytes() == first


# ---------------------------------------------------------------------------
# Marker scaling -- making the picture agree with the statistic
# ---------------------------------------------------------------------------


def test_a_bin_holding_more_points_gets_a_bigger_marker():
    assert _marker_radius(221_320, 221_320) > _marker_radius(94, 221_320) > _marker_radius(1, 221_320)


def test_even_a_single_point_bin_stays_visible():
    """A bin that exists should be visible even when it barely matters --
    hiding it would be its own kind of dishonesty."""
    assert _marker_radius(1, 221_320) >= 2.0


def test_marker_radius_is_defined_when_every_bin_is_empty():
    assert _marker_radius(0, 0) > 0


def test_every_populated_bin_is_labelled_with_its_own_n():
    svg = render_svg(_bins(b0=19, b9=221_320), ece=0.0, brier=0.0, total=221_339)
    assert "n=19" in svg
    assert "n=221,320" in svg


def test_an_empty_bin_is_drawn_nowhere():
    svg = render_svg(_bins(b9=100), ece=0.0, brier=0.0, total=100)
    assert svg.count("<circle") == 1


# ---------------------------------------------------------------------------
# Label placement
# ---------------------------------------------------------------------------


def test_a_label_at_either_edge_is_anchored_inward():
    """The top bin routinely sits at a mean confidence of 1.000, hard
    against the frame; a centred label there is clipped."""
    assert _label_anchor(1.0) == "end"
    assert _label_anchor(0.0) == "start"
    assert _label_anchor(0.5) == "middle"


# ---------------------------------------------------------------------------
# The optional PNG
# ---------------------------------------------------------------------------


def test_the_png_is_written_and_is_not_empty(tmp_path):
    path = write_png(_bins(b0=19, b9=221_320), ece=0.003, brier=0.003, total=221_339, path=tmp_path / "r.png")
    assert path is not None
    assert path.stat().st_size > 0


def test_a_missing_matplotlib_costs_the_png_never_the_run(tmp_path, monkeypatch):
    """The SVG is what EVIDENCE.md depends on. An absent optional renderer
    must not be able to fail `make evidence`."""
    import builtins

    real_import = builtins.__import__

    def _no_matplotlib(name, *args, **kwargs):
        if name.startswith("matplotlib"):
            raise ImportError("simulated: matplotlib is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_matplotlib)

    assert write_png(_bins(b9=100), ece=0.0, brier=0.0, total=100, path=tmp_path / "r.png") is None
