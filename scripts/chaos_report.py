"""Prints a pass/fail table from chaos/incidents/*.json.

Run after `pytest chaos/` (which `make chaos` already does) -- each
scenario test writes its own incident record via chaos/incident.py as it
runs; this script only reads them back into one table, the demo/
screen-recording artifact CLAUDE.md asks for.
"""

from __future__ import annotations

import sys

from chaos.incident import load_incidents


def main() -> int:
    incidents = load_incidents()
    if not incidents:
        print("no incidents recorded -- run `pytest chaos/` first")
        return 1

    id_width = max(len(i.scenario_id) for i in incidents)
    title_width = max(len(i.title) for i in incidents)
    rule = "-" * (id_width + title_width + 24)

    print(f"{'ID':<{id_width}}  {'SCENARIO':<{title_width}}  {'RESULT':<8}  {'MS':>8}")
    print(rule)

    failed = 0
    for incident in incidents:
        result = "PASS" if incident.passed else "FAIL"
        failed += 0 if incident.passed else 1
        print(
            f"{incident.scenario_id:<{id_width}}  {incident.title:<{title_width}}  "
            f"{result:<8}  {incident.wall_clock_ms:>8.1f}"
        )
        if incident.notes:
            print(f"{'':<{id_width}}    note: {incident.notes}")
        if not incident.passed and incident.error:
            print(f"{'':<{id_width}}    error: {incident.error.strip().splitlines()[-1]}")

    print(rule)
    print(f"{len(incidents)} scenarios, {len(incidents) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
