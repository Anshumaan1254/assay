"""Prove the committed EVIDENCE.md is a real, current measurement.

`scripts/check_evidence.py` proves nobody hand-edited EVIDENCE.md -- its
banner carries a SHA-256 of its own body. It cannot prove the document is
still TRUE: an EVIDENCE.md generated twenty commits ago, before a change
that broke detection, passes that check untouched.

This checks the other half. CI regenerates EVIDENCE.md from a real sweep
and runs this, which compares the regenerated document against the
committed one and fails on any difference in a MEASURED number.

Environment-dependent rows are masked, because they cannot match and their
mismatching says nothing: a Linux runner is not Windows 10 (AMD64), a
second sweep is not 482.2s to the tenth, and every run has its own
timestamp and git commit. Two rows are masked for a subtler reason worth
stating -- the input-file SHA-256 table and the determinism hashes are
byte-level facts about the checkout, and git normalises line endings, so
runs/*.json genuinely differ between a Windows and a Linux clone (see
DECISIONS.md 2026-08-30 00:20). What is NOT masked is everything the
engine concluded: precision, recall, false positives, calibration,
ablations, contract version, and the determinism VERDICT itself.

Standalone by design -- no imports from eval/, so it runs in a bare CI
checkout with nothing installed, exactly like check_evidence.py.

    python scripts/evidence_diff.py                  # vs git HEAD
    python scripts/evidence_diff.py --baseline old.md
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = REPO_ROOT / "EVIDENCE.md"

# Two-column table rows whose value describes the machine, the clock or the
# checkout rather than the engine's behaviour.
MASKED_LABELS = frozenset({
    # Section 1 -- reproduction provenance
    "Run timestamp",
    "Git commit",
    "Python",
    "Platform",
    "Wall clock (whole sweep)",
    # Section 10 -- LLM transport telemetry. Which calls were served from
    # the committed cache and which reached the network depends on cache
    # state, not on what the engine decided.
    "Cache hit rate",
    "Calls that reached the network",
    "429s observed",
    "Time spent in backoff",
    "Rate-limit config this ran under",
    "Tokens (prompt / response / total)",
    "Tokens per 1,000 records",
    # Section 11 -- throughput, i.e. how fast this particular box is
    "Wall clock (audit time only)",
    "Records/sec",
    "First run, cold cache",
    "A run served entirely from cache",
    # Section 12 -- the hash VALUES vary with line endings and run_dir
    # separator across platforms. "Verdict" and "Replay matches, live API
    # calls made" are deliberately NOT masked: that the two runs agree, and
    # that zero network calls were made, must hold everywhere.
    "Run 1 report hash",
    "Run 2 report hash",
    "Replay hash (cache only, no network)",
    # The model at each boundary is read from an untracked .env
    "`llm/contract_parser.py`",
    "`llm/adjudicator.py`",
    "`llm/narrator.py`",
    # Section 1's input-file digests: raw bytes, so CRLF vs LF diverges
    "`bank_statement.json`",
    "`ledger.json`",
    "`rate_card.md`",
    "`settlement_report.json`",
})

ROW = re.compile(r"^\|\s*(?P<label>[^|]+?)\s*\|\s*(?P<value>.*?)\s*\|\s*$")
BANNER_HASH = re.compile(r"^(\s*body-sha256:)\s*[0-9a-f]{64}\s*(-->)?\s*$")

# Environment-dependent numbers that appear in PROSE rather than in a table
# row, so MASKED_LABELS cannot reach them. Section 11 restates its wall
# clock inside a sentence (eval/evidence.py:782), and a wall clock is the
# speed of whichever box ran the sweep: 235s on the machine that generated
# the committed document, 37s on a CI runner. Each entry masks only the
# varying number and keeps the sentence, so a rewording still shows up as
# a difference.
MASKED_PROSE = (
    re.compile(r"^(?P<before>)[\d.,]+(?P<after> seconds\. The assumption is stated so it)$"),
)


def mask(document: str) -> list[str]:
    """The document with environment-dependent values replaced, as lines."""
    masked: list[str] = []
    for line in document.splitlines():
        banner = BANNER_HASH.match(line)
        if banner:
            masked.append(f"{banner.group(1)} <masked>{banner.group(2) or ''}")
            continue
        row = ROW.match(line)
        if row and row.group("label") in MASKED_LABELS:
            masked.append(f"| {row.group('label')} | <masked> |")
            continue
        prose = next((m for m in (p.match(line) for p in MASKED_PROSE) if m), None)
        if prose:
            masked.append(f"{prose.group('before')}<masked>{prose.group('after')}")
            continue
        masked.append(line)
    return masked


def committed_evidence() -> str:
    # encoding is explicit: text=True alone decodes with the locale codepage,
    # which on a Windows console is cp1252 and turns every rupee sign and em
    # dash in the document into a replacement character -- producing a diff
    # of hundreds of lines that says nothing about the engine.
    result = subprocess.run(
        ["git", "show", "HEAD:EVIDENCE.md"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"evidence_diff: cannot read HEAD:EVIDENCE.md -- {result.stderr.strip()}")
    return result.stdout


def _use_utf8_streams() -> None:
    """EVIDENCE.md is full of rupee signs and em dashes, and this script
    prints slices of it. Windows' default console codepage cannot encode
    either, so an unfixed stream turns a real failure report into a
    UnicodeEncodeError. Same treatment as cli/__init__.py's own
    _use_utf8_streams, duplicated rather than imported because this script
    must keep running in a bare checkout with nothing installed. Never
    raises: failing to upgrade the encoding must not break the check.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower() != "utf-8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main() -> int:
    _use_utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="File to compare against. Defaults to EVIDENCE.md at git HEAD.",
    )
    parser.add_argument(
        "--current",
        type=Path,
        default=EVIDENCE,
        help="The freshly regenerated document. Defaults to ./EVIDENCE.md.",
    )
    args = parser.parse_args()

    if not args.current.is_file():
        raise SystemExit(f"evidence_diff: {args.current} does not exist -- run `make evidence` first")

    baseline = (
        args.baseline.read_text(encoding="utf-8") if args.baseline else committed_evidence()
    )
    current = args.current.read_text(encoding="utf-8")

    delta = list(
        difflib.unified_diff(
            mask(baseline),
            mask(current),
            fromfile="EVIDENCE.md (committed)",
            tofile="EVIDENCE.md (regenerated)",
            lineterm="",
        )
    )

    if not delta:
        print(
            "OK: the regenerated EVIDENCE.md matches the committed one in every "
            f"measured number ({len(MASKED_LABELS)} environment rows masked)."
        )
        return 0

    print("FAIL: the committed EVIDENCE.md does not reproduce.", file=sys.stderr)
    print(
        "Every difference below is a measured value, not an environment one.\n"
        "Either the engine's behaviour changed and EVIDENCE.md is stale "
        "(regenerate and commit it), or something regressed.\n",
        file=sys.stderr,
    )
    for line in delta:
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
