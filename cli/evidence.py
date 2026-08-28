"""`assay evidence` -- prints the committed EVIDENCE.md and confirms it
matches its own declared body hash. Deliberately does NOT regenerate it:
that's what `make evidence`/`assay eval --evidence` are for, and doing it
implicitly from a bare `assay evidence` would be a slow, LLM-cache-touching
surprise.
"""

from __future__ import annotations

from pathlib import Path


class EvidenceMissing(Exception):
    """EVIDENCE.md does not exist yet."""


class EvidenceTampered(Exception):
    """EVIDENCE.md exists but its body no longer matches the hash its own
    banner declares -- either hand-edited, or the banner is missing/
    malformed entirely, which is the same claim: this is not, as it
    stands, a trustworthy generated document."""


def read_evidence(path: Path | None = None) -> tuple[str, str]:
    """(full document, actual body hash), or raises EvidenceMissing /
    EvidenceTampered. Reuses eval.evidence's own split()/body_hash() rather
    than re-parsing the banner a third time (scripts/check_evidence.py is
    the second, standalone-by-design copy; a third would only add a way
    for the two to drift).

    Imported lazily: eval.evidence transitively imports eval.sweep ->
    eval.harness -> datagen at module scope, so a module-level import here
    would put a path from the product's CLI package to the answers --
    invariant 5 -- the same reason `assay eval` imports eval.cli lazily.
    """
    from eval.evidence import EVIDENCE_PATH, body_hash, split

    target = path if path is not None else EVIDENCE_PATH
    if not target.is_file():
        raise EvidenceMissing(
            f"{target} does not exist -- run 'make evidence' or 'assay eval --evidence' to generate it"
        )

    document = target.read_text(encoding="utf-8")
    try:
        declared, body = split(document)
    except ValueError as error:
        raise EvidenceTampered(f"{target}: {error}") from error

    actual = body_hash(body)
    if actual != declared:
        raise EvidenceTampered(
            f"{target} has been edited by hand -- banner declares {declared}, body hashes to {actual}. "
            "Re-run 'make evidence' to regenerate it, or revert the edit."
        )
    return document, actual
