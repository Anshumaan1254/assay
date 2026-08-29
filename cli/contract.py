"""`assay contract compile <ratecard.md>` -- exposes llm/contract_parser.py's
compile_rate_card() as its own verb, and the pinning helper `assay audit
--contract` uses to skip recompilation against an already-signed contract.

cli/loaders.py:load_contract() already calls compile_rate_card() internally
for `assay audit`/`assay explain`; this module is not a second copy of that
logic, only different path plumbing for a caller that hands in an arbitrary
rate-card path instead of a run directory.
"""

from __future__ import annotations

from pathlib import Path

import typer

from cli.loaders import default_provider
from core.contract import CompiledContract, ContractError, RoundingMode, sha256_of
from llm.contract_parser import ContractParseRejected, compile_rate_card
from llm.provider import LLMProvider, ProviderUnavailable

contract_app = typer.Typer(help="Compile a rate card into a signed, deterministic fee schedule.")


@contract_app.callback()
def _callback() -> None:
    """A no-op callback so `compile` stays a named subcommand instead of
    Typer collapsing this single-command sub-app into a bare
    `assay contract <file>` -- see cli/__init__.py's own callback docstring
    for the same pattern applied to `explain`."""


class PinnedContractMismatch(Exception):
    """A `--contract` path was given, but its `source_sha256` does not match
    the run directory's current `rate_card.md`. Refusing rather than
    auditing against the wrong rate card, which would be a silent
    money-correctness bug."""


def compile_ratecard_file(
    ratecard: Path,
    provider: LLMProvider,
    *,
    out: Path | None = None,
    rounding: RoundingMode = RoundingMode.HALF_UP,
) -> tuple[CompiledContract, Path]:
    document = ratecard.read_text(encoding="utf-8")
    contract = compile_rate_card(document, provider, rounding=rounding)
    target = out if out is not None else ratecard.with_suffix(".contract.json")
    contract.save(target)
    return contract, target


def load_pinned_contract(path: Path, run_dir: Path) -> CompiledContract:
    contract = CompiledContract.load(path)
    current_source_sha256 = sha256_of((run_dir / "rate_card.md").read_text(encoding="utf-8"))
    if contract.source_sha256 != current_source_sha256:
        raise PinnedContractMismatch(
            f"{path}: was compiled from a rate card whose source_sha256 is "
            f"{contract.source_sha256}, but {run_dir}/rate_card.md currently hashes to "
            f"{current_source_sha256} -- refusing to audit against a stale or mismatched pin"
        )
    return contract


@contract_app.command("compile")
def compile_contract(
    ratecard: Path = typer.Argument(  # noqa: B008 -- this is Typer's own documented pattern
        ..., help="Path to the rate card, as markdown."
    ),
    out: Path = typer.Option(  # noqa: B008 -- this is Typer's own documented pattern
        None,
        "--out",
        help="Where to write the compiled contract. Defaults to <ratecard>.contract.json.",
    ),
) -> None:
    """Read a rate card and compile it into a deterministic, effective-dated
    fee schedule -- the same recomputation baseline `assay audit` builds
    internally, exposed here as its own step so it can be inspected and
    signed off before an audit ever pins it (`assay audit --contract`)."""
    try:
        contract, target = compile_ratecard_file(ratecard, default_provider(), out=out)
    except (ContractParseRejected, ContractError, ProviderUnavailable) as error:
        typer.echo(f"assay contract compile: refused -- {error}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"version:          {contract.version_id}")
    typer.echo(f"rounding:         {contract.rounding.value}")
    typer.echo(f"rules:            {len(contract.rules)}")
    typer.echo(f"dormant clauses:  {len(contract.dormant_clauses)}")
    typer.echo(f"uncovered:        {sorted(m.value for m in contract.uncovered_methods)}")
    typer.echo(f"source sha256:    {contract.source_sha256}")
    typer.echo(f"schedule sha256:  {contract.schedule_sha256}")
    typer.echo(f"wrote:            {target}")
