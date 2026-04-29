"""Known CLI agent profiles for the shared channel MCP shim.

The channel core is actor-agnostic: any MCP-compatible CLI can connect as any
stable actor name. Profiles only document common CLIs and generate the stdio
MCP command they all share.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path


_ACTOR_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AgentProfile:
    actor: str
    label: str
    cli: str
    notes: str


KNOWN_AGENT_PROFILES: dict[str, AgentProfile] = {
    "claude": AgentProfile(
        actor="claude",
        label="Claude Code",
        cli="claude",
        notes="MCP-capable. Use the channel MCP server and inject the council bootstrap prompt.",
    ),
    "codex": AgentProfile(
        actor="codex",
        label="Codex CLI",
        cli="codex",
        notes="MCP-capable. Register the channel server, then inject the council bootstrap prompt.",
    ),
    "gemini": AgentProfile(
        actor="gemini",
        label="Gemini CLI",
        cli="gemini",
        notes="MCP-capable when configured with a stdio MCP server.",
    ),
    "kimi": AgentProfile(
        actor="kimi",
        label="Kimi CLI",
        cli="kimi",
        notes="Use if the local Kimi CLI exposes MCP/ACP tool integration.",
    ),
    "opencode": AgentProfile(
        actor="opencode",
        label="OpenCode",
        cli="opencode",
        notes="Use if the local OpenCode setup exposes MCP-compatible tools.",
    ),
}


def validate_actor(actor: str) -> str:
    """Return a normalized actor id or raise ValueError."""
    normalized = actor.strip()
    if not normalized:
        raise ValueError("actor is required")
    if not _ACTOR_RE.match(normalized):
        raise ValueError(
            "actor may only contain letters, numbers, underscore, dash, dot, or @"
        )
    return normalized


def list_agent_profiles() -> list[AgentProfile]:
    return [KNOWN_AGENT_PROFILES[key] for key in sorted(KNOWN_AGENT_PROFILES)]


def get_agent_profile(actor: str) -> AgentProfile | None:
    return KNOWN_AGENT_PROFILES.get(validate_actor(actor).lower())


def build_mcp_spec(
    actor: str,
    broker: str = "ws://127.0.0.1:9100",
    cwd: str | None = None,
    project_dir: str | None = None,
) -> dict:
    """Build a stdio MCP command spec usable by MCP-capable CLIs."""
    normalized = validate_actor(actor)
    project_root = project_dir or str(_PROJECT_ROOT)
    args = [
        "--directory",
        project_root,
        "run",
        "python",
        "-m",
        "warroom.channel.mcp_shim",
        "--actor",
        normalized,
        "--broker",
        broker,
    ]
    if cwd:
        args.extend(["--cwd", cwd])
    return {
        "name": "channel",
        "command": "uv",
        "args": args,
        "env": {},
    }


def build_mcp_command(
    actor: str,
    broker: str = "ws://127.0.0.1:9100",
    cwd: str | None = None,
    project_dir: str | None = None,
) -> str:
    spec = build_mcp_spec(
        actor=actor,
        broker=broker,
        cwd=cwd,
        project_dir=project_dir,
    )
    return shlex.join([spec["command"], *spec["args"]])


def format_mcp_spec_json(
    actor: str,
    broker: str = "ws://127.0.0.1:9100",
    cwd: str | None = None,
    project_dir: str | None = None,
) -> str:
    return json.dumps(
        build_mcp_spec(
            actor=actor,
            broker=broker,
            cwd=cwd,
            project_dir=project_dir,
        ),
        indent=2,
        sort_keys=True,
    )
