"""Fails if EVIDENCE.md was edited by hand.

EVIDENCE.md is generated. Its banner carries the SHA-256 of everything
below it, so a single changed character is detectable, and this script is
what does the detecting -- wired into `make guard` so it runs with the rest
of the invariant suite.

Why this exists at all: an accuracy report is only worth reading if nobody
can quietly improve a number in it. The generator has no incentive to
flatter; a person editing the file later might. Making the edit *fail* is
cheaper than trusting it not to happen.

Standalone by design -- no imports from eval/, so it runs in a bare CI
checkout with nothing installed. The banner format is duplicated here
rather than imported for exactly that reason, and
tests/test_eval_evidence.py asserts the two agree.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

EVIDENCE_PATH = Path(__file__).resolve().parent.parent / "EVIDENCE.md"
BANNER_START = "<!-- GENERATED FILE"
BANNER_END = "-->"
HASH_KEY = "body-sha256:"


def check(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"{path.name} does not exist -- run `make evidence`"

    document = path.read_text(encoding="utf-8")
    if not document.startswith(BANNER_START):
        return False, (
            f"{path.name} has no generated-file banner. It is a generated document; "
            "regenerate it with `make evidence` rather than writing it by hand."
        )

    marker = document.find(BANNER_END)
    if marker == -1:
        return False, f"{path.name}'s banner is not terminated"

    header, body = document[:marker], document[marker + len(BANNER_END) :]
    body = body.removeprefix("\n")

    declared = ""
    for line in header.splitlines():
        if HASH_KEY in line:
            declared = line.split(HASH_KEY)[1].strip()
            break
    if not declared:
        return False, f"{path.name}'s banner carries no {HASH_KEY}"

    actual = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if actual != declared:
        return False, (
            f"{path.name} has been edited by hand.\n"
            f"  banner declares: {declared}\n"
            f"  body hashes to:  {actual}\n"
            "EVIDENCE.md is generated from a real run and must never be hand-edited. "
            "Re-run `make evidence` to regenerate it, or revert the edit."
        )
    return True, f"{path.name} matches its declared body hash ({actual[:12]}...)"


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else EVIDENCE_PATH
    ok, message = check(path)
    print(("OK: " if ok else "FAIL: ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
