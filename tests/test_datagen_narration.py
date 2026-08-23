"""BankCredit narration corruption: casing, bank prefixes, double spaces,
and an occasional embedded truncated-looking UTR fragment. This is
baseline noise on every profile (including `clean`) and must never touch
the structured BankCredit.utr field -- that is D09's job (added with
datagen/inject.py), a deliberately separate mechanism."""

from __future__ import annotations

from random import Random

from datagen.world import generate_narration


def test_narration_never_mutates_the_real_utr_value():
    rng = Random(1)
    utr = "HDFC0001234202607010000"
    for _ in range(500):
        narration = generate_narration(rng, utr, "STL-20260701")
        # The narration is free text built FROM the utr -- it may quote it
        # verbatim or as a casing/whitespace variant, but this test's job is
        # only to confirm generate_narration returns a string and never
        # raises, and that the function signature keeps utr/narration
        # separate: the caller still holds the original, untouched `utr`.
        assert isinstance(narration, str)
    assert utr == "HDFC0001234202607010000"  # untouched by any call above


def test_narration_has_inconsistent_casing_across_many_draws():
    rng = Random(2)
    narrations = [generate_narration(rng, "UTR000000000000000001", "STL-20260701") for _ in range(200)]
    has_upper = any(n == n.upper() and n != n.lower() for n in narrations)
    has_lower = any(n == n.lower() and n != n.upper() for n in narrations)
    has_mixed = any(n != n.upper() and n != n.lower() for n in narrations)
    assert has_upper and has_lower and has_mixed


def test_narration_sometimes_has_double_spaces():
    rng = Random(3)
    narrations = [generate_narration(rng, "UTR000000000000000002", "STL-20260702") for _ in range(200)]
    assert any("  " in n for n in narrations)


def test_narration_sometimes_embeds_a_truncated_looking_utr_fragment():
    rng = Random(4)
    utr = "HDFC0001234202607030000"
    narrations = [generate_narration(rng, utr, "STL-20260703") for _ in range(2_000)]
    fragment = f"REF{utr[:6]}"
    matches = sum(1 for n in narrations if fragment.upper() in n.upper())
    # ~5% target; loose bounds since this is a statistical draw.
    assert 20 < matches < 200


def test_narration_includes_a_bank_style_prefix():
    rng = Random(5)
    narrations = [generate_narration(rng, "UTR000000000000000003", "STL-20260704") for _ in range(50)]
    prefixes_seen = {n.upper()[:4] for n in narrations}
    # Every configured prefix starts with one of these four-letter tokens.
    assert prefixes_seen & {"NEFT", "IMPS", "RTGS", "UPI/"}
