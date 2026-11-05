"""``ai-coding agents list`` — the twelve agents of ТЗ §6 and their status (B-406)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from ai_coding.agents import CATALOG, plan_chain
from ai_coding.cli.console import stdout
from ai_coding.config.loader import find_config
from ai_coding.config.models import AICodingConfig
from ai_coding.config.validator import validate_file

app = typer.Typer(help="Agents (ТЗ §6).", no_args_is_help=True)


def _config(project: Path) -> AICodingConfig | None:
    path = find_config(project.resolve())
    if path is None:
        return None
    report = validate_file(path)
    return report.config if report.ok else None


@app.command("list")
def list_agents(
    project: Annotated[Path, typer.Option("--project", "-C", help="Project directory.")] = Path(),
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Agents, what they consume and produce, status and the model chain they would use."""
    config = _config(project)
    enabled = {a.value for a in config.agents.enabled} if config else None
    rows: list[dict[str, Any]] = []
    for spec in CATALOG:
        chain: list[str] = []
        notes: list[str] = []
        if spec.requires_llm and config is not None:
            plan = plan_chain(config, spec.name)
            chain = [f"{r.provider.value}/{r.model}" for r in plan.routes]
            notes = list(plan.skipped)
        rows.append({
            "name": spec.name.value,
            "status": "implemented" if spec.implemented else f"planned ({spec.sprint})",
            "inputs": spec.inputs,
            "outputs": spec.outputs,
            "requires_llm": spec.requires_llm,
            "enabled": None if enabled is None else spec.name.value in enabled,
            "models": chain,
            "notes": notes,
            "output_schema": (
                f"ai_coding/schemas/agents/{spec.name.value}.output.schema.json"
                if spec.implemented else None
            ),
        })  # fmt: skip
    if as_json:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    table = Table("Agent", "Status", "Input → Output", "LLM", "Models")
    for r in rows:
        llm = "yes" if r["requires_llm"] else "no (deterministic)"
        if r["enabled"] is False:
            llm += ", disabled"
        models = ", ".join(r["models"]) or ("—" if not r["requires_llm"] else "; ".join(r["notes"]))
        table.add_row(
            escape(str(r["name"])), escape(str(r["status"])),
            escape(f"{r['inputs']} → {r['outputs']}"), llm, escape(models),
        )  # fmt: skip
    stdout.print(table)
