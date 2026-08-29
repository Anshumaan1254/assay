"""Build the reviewer for its Vercel deployment.

Runs after `pip install -r requirements.txt` and before the function is
bundled. Two jobs:

1. **Build the front end.** `reviewer/api.py` mounts `reviewer/web/dist` at
   "/" when it exists, so without this the deployment serves the JSON API and
   nothing else. Vercel promotes the mounted directory to its CDN at build
   time, so the SPA is served from the edge rather than through Python.

2. **Produce the audit the reviewer reads.** `runs/**/report.json` and
   `.assay/audit_store.db` are gitignored derived artifacts -- correctly, they
   are regenerable -- so a fresh clone has neither and the deployed app would
   have nothing to show. Rather than committing them (which would break that
   convention, and would bake in Windows path separators from whichever
   machine ran it last), the audit is recomputed here from the committed
   inputs and the committed `.llm_cache/`. No API key, no network.

Deliberately does NOT run `assay generate`: `runs/realistic-seed42/`'s inputs
are already committed, so the generator -- the one package that holds planted
ground truth -- never has to run in a deployment build at all.

The audit is best-effort. If it fails, the build still succeeds and the
deployment serves its own "no audit to review" screen, which is a far better
outcome than a red deploy for a data-preparation step.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Relative on purpose -- see RUN_DIR_ARG below.
RUN_DIR = ROOT / "runs" / "realistic-seed42"
RUN_DIR_ARG = "runs/realistic-seed42"
WEB = ROOT / "reviewer" / "web"


def run(command: list[str], *, cwd: Path, optional: bool = False) -> bool:
    printable = " ".join(command)
    print(f"\n$ {printable}", flush=True)
    try:
        subprocess.run(command, cwd=cwd, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        if optional:
            print(f"  !! skipped: {exc}", flush=True)
            return False
        raise


def build_front_end() -> None:
    npm = shutil.which("npm")
    if npm is None:
        print("!! npm not found -- deploying the API without the SPA", flush=True)
        return
    # `npm ci` needs the lockfile; fall back to install if it is ever absent.
    install = ["ci"] if (WEB / "package-lock.json").is_file() else ["install"]
    run([npm, *install], cwd=WEB)
    run([npm, "run", "build"], cwd=WEB)


def build_audit() -> None:
    if not RUN_DIR.is_dir():
        print(f"!! {RUN_DIR} is missing -- nothing to audit", flush=True)
        return

    python = sys.executable
    # The audit writes the store next to the build, and vercel_app.py copies
    # it to /tmp at runtime. Stated explicitly so the build does not depend on
    # the default resolving the same way in a container.
    env_store = ROOT / ".assay" / "audit_store.db"
    env_store.parent.mkdir(parents=True, exist_ok=True)
    os.environ["ASSAY_STORE_PATH"] = str(env_store)

    # Relative paths, not absolute. `run_dir` is part of the report's hash
    # payload, so passing an absolute path makes `report_hash` depend on where
    # the repository happens to be checked out -- the build would produce a
    # different hash than any developer running the same command, and the
    # staleness check in tests/test_site.py would fail against it. Invariant 4
    # is about the same inputs producing the same bytes; the path the build
    # runs from is not an input.
    ok = run(
        [python, "-m", "cli", "contract", "compile", f"{RUN_DIR_ARG}/rate_card.md"],
        cwd=ROOT,
        optional=True,
    )
    if ok:
        run([python, "-m", "cli", "audit", "--run-dir", RUN_DIR_ARG], cwd=ROOT, optional=True)

    report = RUN_DIR / "report.json"
    print(
        f"\nreport.json: {'present' if report.is_file() else 'MISSING'}"
        f"  |  store: {'present' if env_store.is_file() else 'MISSING'}",
        flush=True,
    )


def main() -> None:
    build_front_end()
    build_audit()
    print("\nreviewer build complete", flush=True)


if __name__ == "__main__":
    main()
