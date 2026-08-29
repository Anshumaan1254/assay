"""Vercel entrypoint for the reviewer.

`reviewer/api.py` is unchanged and stays the real application. This module
exists only to make it survive a serverless filesystem, and it is the module
`[tool.vercel] entrypoint` in pyproject.toml points at.

Two things have to be true on Vercel that are free locally:

1. **The audit store must be writable.** `locate_run` opens
   `.assay/audit_store.db` through SQLAlchemy, which opens SQLite read-write
   even for a pure SELECT. Everything outside `/tmp` on a Vercel Function is
   read only, so the database bundled at build time is copied to `/tmp` on
   cold start and `ASSAY_STORE_PATH` is pointed at the copy. `store/` already
   reads that variable per call, so nothing in the engine changes.

2. **The data has to exist at all.** `runs/**/report.json` and `.assay/` are
   gitignored derived artifacts, so a clone has neither. They are produced
   during the build instead -- see `scripts/vercel_reviewer_build.py` --
   which keeps the repository's convention intact: the deployment builds its
   own evidence rather than committing it.

If the copy fails the app still imports and serves; it reports "no audit to
review", which is the honest failure and the one `reviewer/api.py` already
handles.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_STORE = REPO_ROOT / ".assay" / "audit_store.db"
RUNTIME_STORE = Path("/tmp/assay/audit_store.db")


def _stage_store() -> None:
    """Copy the bundled store somewhere writable, once per cold start."""
    if os.environ.get("ASSAY_STORE_PATH"):
        return  # explicitly configured; leave it alone
    if not BUNDLED_STORE.is_file():
        return  # nothing to stage -- the app will say so
    try:
        RUNTIME_STORE.parent.mkdir(parents=True, exist_ok=True)
        # Copy when missing OR when the bundled store is newer. "Only if
        # missing" looks equivalent -- a Vercel cold start gets an empty /tmp
        # -- but it silently serves a stale database anywhere /tmp outlives a
        # rebuild, and the failure is a 409 blaming the report rather than the
        # copy. Comparing mtime costs nothing and cannot be wrong.
        stale = (
            not RUNTIME_STORE.is_file()
            or BUNDLED_STORE.stat().st_mtime > RUNTIME_STORE.stat().st_mtime
        )
        if stale:
            shutil.copy2(BUNDLED_STORE, RUNTIME_STORE)
        os.environ["ASSAY_STORE_PATH"] = str(RUNTIME_STORE)
    except OSError:
        # A read-only /tmp would be surprising, but a background copy is not
        # worth taking the whole app down for.
        pass


_stage_store()

# Imported after the environment is staged: `store/` resolves the path per
# call, but importing first would be relying on that rather than stating it.
from reviewer.api import app

__all__ = ["app"]
