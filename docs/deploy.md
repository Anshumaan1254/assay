# Deploying

Two Vercel projects, one repository. They are told apart by **Root
Directory**, and nothing else:

| Project | Root Directory | Config | What deploys |
|---|---|---|---|
| landing | `site` | `site/vercel.json` | the static Vite SPA |
| reviewer | *(empty — repo root)* | `vercel.json` | `reviewer/api.py` as a Python function |

Both watch `main`. Leaving Root Directory unset gets you the reviewer,
because the root config is the FastAPI one.

This file exists because `vercel.json` cannot hold comments: Vercel validates
it against a schema and rejects any property outside it, including the `"//"`
key normally used for JSON comments. The reasoning that would otherwise live
beside each setting lives here.

## The landing page

Import the repo, set **Root Directory** to `site`, deploy. The build needs no
Python, no API key and no network beyond npm — every figure the page prints
is already committed in `site/src/data/audit.json`, baked from a signed
report by `scripts/bake_site_data.py`.

`SITE_BASE` overrides the base path if it is ever served from a subdirectory
(`SITE_BASE=/assay/ npm run build`). It defaults to `/`.

## The reviewer

Import the repo again, leave **Root Directory** empty, deploy. Vercel detects
the FastAPI preset on its own, because `pyproject.toml` declares the
entrypoint:

```toml
[tool.vercel]
entrypoint = "reviewer.vercel_app:app"
```

Without that it fails with *"No FastAPI entrypoint found"*: Vercel only
auto-detects a FastAPI `app` in files named `app`/`index`/`server`/`main`/
`wsgi`/`asgi` at the root or in `src/`, `app/`, `api/`, and this app lives at
`reviewer/api.py`.

**No environment variables are needed.** No `GEMINI_API_KEY`: the build
compiles the contract out of the committed `.llm_cache/` and never opens a
network connection.

### Why the entrypoint is a shim

`reviewer/vercel_app.py` wraps `reviewer/api.py`, which is unchanged and knows
nothing about Vercel (`tests/test_site.py` asserts that). The shim handles two
things that are free locally and broken on a serverless filesystem:

- **The audit store must be writable.** SQLAlchemy opens SQLite read-write
  even for a `SELECT`, and everything outside `/tmp` is read-only. The store
  bundled at build time is copied into `/tmp` on cold start, through the
  `ASSAY_STORE_PATH` override `store/` already honours.
- **The data has to exist.** `runs/**/report.json` and `.assay/` are
  gitignored derived artifacts, so a clone has neither.
  `scripts/vercel_reviewer_build.py` recomputes the audit during the build
  from the committed inputs instead of committing the outputs.

That build script passes a **relative** `--run-dir` deliberately: `run_dir` is
part of the report's hash payload, so an absolute path would make
`report_hash` depend on where the repository happens to be checked out.

### Dependencies

`requirements.txt`, not `pip install -e .`. An AST walk of the entrypoint's
import closure reaches nine packages; `scikit-learn` and `matplotlib` are
`eval/`-only and together would be most of a serverless bundle. The same walk
confirms the deployed app cannot reach `datagen/` — invariant 5 holds on a
public URL, not just in the test suite.

## The shared `.vercelignore`

There is exactly one, read from the repository root **before** Root Directory
is applied — so an entry there applies to both builds at once. `site/` was
once listed to slim the reviewer's bundle and deleted the landing project's
own `package.json` before its `npm ci` ran. `tests/test_site.py` now pins both
directions.

## Verifying a deploy

- `/api/health` → `200`
- `/api/runs` → one run, `report_hash` beginning `5bc692d4…`
- `/` → the reviewer UI

The build log should show the SPA build, then
`report.json: present | store: present`.
