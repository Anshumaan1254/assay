# Assay as a container: `docker run --network none assay demo` reproduces
# the full audit on a machine that has never seen this repository, with no
# environment variables and no network. That is the README's central
# claim -- no GEMINI_API_KEY required -- made falsifiable by a stranger.
#
# Multi-stage so pip, its wheels and the build backend never reach the
# final image; only the finished virtualenv does.

# ── build ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# 3.11 deliberately, matching pyproject.toml's own note: 3.12 is permitted
# only because Vercel's runtime forces it on the deployed reviewer, and is
# not a tested target.

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# binutils for `strip` below. Builder-stage only -- none of it is copied
# into the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends binutils \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
COPY . .

# The whole project, not a requirements file. requirements.txt in this repo
# is the Vercel function's AST-computed import closure -- it deliberately
# omits scikit-learn and matplotlib, and installing from it would produce a
# container that cannot run `assay eval`.
RUN pip install .

# Drop dependency test suites and bytecode caches. scipy and numpy alone are
# ~150 MB and roughly a third of the pair is their own tests, which nothing
# imports in normal use. Same rule as scripts/vercel_reviewer_build.py's
# prune_site_packages(): directories named exactly `tests` or `test`, never
# `testing` -- numpy.testing is public API and deleting it breaks imports.
RUN find /opt/venv -type d \( -name tests -o -name test -o -name __pycache__ \) \
        -prune -exec rm -rf {} +

# Debug symbols in the compiled extensions are the single largest item in
# the image: 261 shared objects, 207 MB unstripped, and scipy/numpy/sklearn
# are most of it. --strip-unneeded keeps every symbol needed for dynamic
# relocation and discards the rest; it does not touch Python bytecode or
# any pure-Python module.
RUN find /opt/venv/lib -name '*.so' -o -name '*.so.*' \
        | xargs -r strip --strip-unneeded 2>/dev/null; true

# pip, setuptools and wheel are build-time tools. Nothing in the runtime
# image installs packages, and leaving them in ships a package manager
# inside a container whose whole point is that it needs no network.
RUN rm -rf /opt/venv/lib/python3.11/site-packages/pip \
           /opt/venv/lib/python3.11/site-packages/pip-* \
           /opt/venv/lib/python3.11/site-packages/setuptools \
           /opt/venv/lib/python3.11/site-packages/setuptools-* \
           /opt/venv/lib/python3.11/site-packages/pkg_resources \
           /opt/venv/lib/python3.11/site-packages/wheel \
           /opt/venv/lib/python3.11/site-packages/wheel-* \
           /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.11 \
           /opt/venv/bin/wheel

# Every stripped and pruned dependency, imported for real. A broken strip
# or an over-eager prune fails the build here rather than at someone
# else's first `docker run`.
RUN python -c "import numpy.testing, scipy.optimize, sklearn.isotonic, matplotlib, \
    pydantic, typer, structlog, sqlmodel, google.genai, httpx, yaml, dotenv; \
    from scipy.optimize import linear_sum_assignment; \
    print('post-prune imports OK')"

# ── runtime ──────────────────────────────────────────────────────────────
FROM python:3.11-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv

# WORKDIR is /app and must be writable. Almost everything the engine reads
# resolves relative to the working directory, not to the installed package:
#   .llm_cache/    llm/providers/cached.py, which also mkdir's it on a pure
#                  cache hit, so a read-only tree fails even offline
#   calibration/   core/lanes.py, hash-checked
#   runs/, truth/, .assay/, eval/results/  written by the demo
WORKDIR /app
COPY . .

# Fail the build, not the first `docker run`, if the two directories the
# offline guarantee depends on were excluded from the context.
RUN test -f calibration/lane_calibration.v1.json \
 && test "$(ls .llm_cache/*.json | wc -l)" -gt 0 \
 && echo "cache entries: $(ls .llm_cache/*.json | wc -l)"

ENTRYPOINT ["assay"]
CMD ["--help"]
