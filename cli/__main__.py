"""Entry point for `python -m cli ...` -- what every Makefile target uses,
since a recipe can't assume the installed `assay` console script is on PATH.
"""

from __future__ import annotations

from cli import main

if __name__ == "__main__":
    main()
