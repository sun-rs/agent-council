"""Project-level council configuration generation.

This is intentionally a thin layer over the shared MCP shim. The canonical
input is a list of actors; CLI-specific files are generated as artifacts so the
future supervisor can launch each CLI without asking users to hand-type MCP
commands.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from warroom.channel.agent_profiles import (
    KNOWN_AGENT_PROFILES,
    build_mcp_spec,
    get_agent_profile,
    validate_actor,
)


CONFIG_VERSION = 1
DEFAULT_COUNCIL_ACTORS = ("claude", "codex", "gemini", "kimi", "opencode")
DEFAULT_MCP_SERVER_NAME = "channel"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_ROOT_CONFIG_KEYS = {
    "room",
    "broker",
    "workdir",
    "project_dir",
    "mcp_server_name",
    "tmux",
    "agents",
    "tui",
}


@dataclass(frozen=True)
class ParsedAgentRef:
    actor: str
    profile_key: str
    model: str | None = None


def parse_agent_ref(value: str) -> ParsedAgentRef:
    """Parse simple actors plus model@cli and legacy cli@model shorthand."""
    normalized = validate_actor(str(value))
    if "@" not in normalized:
        lower = normalized.lower()
        profile_key = lower if lower in KNOWN_AGENT_PROFILES else normalized
        actor = profile_key if profile_key in KNOWN_AGENT_PROFILES else normalized
        return ParsedAgentRef(actor=actor, profile_key=profile_key)

    left, right = normalized.split("@", 1)
    if not left or not right:
        raise ValueError("agent shorthand must look like model@cli")

    left_key = left.lower()
    right_key = right.lower()
    if right_key in KNOWN_AGENT_PROFILES:
        # Preferred form: model@cli.
        return ParsedAgentRef(
            actor=f"{left}@{right_key}",
            profile_key=right_key,
            model=left,
        )
    if left_key in KNOWN_AGENT_PROFILES:
        # Legacy form: cli@model. Normalize to model@cli.
        return ParsedAgentRef(
            actor=f"{right}@{left_key}",
            profile_key=left_key,
            model=right,
        )
    return ParsedAgentRef(actor=normalized, profile_key=normalized)


def split_actor_model(actor: str) -> tuple[str, str | None]:
    """Return (profile key, model) for model@cli or legacy cli@model shorthand."""
    parsed = parse_agent_ref(actor)
    return parsed.profile_key, parsed.model


def infer_profile_key(actor: str) -> str:
    """Infer the known profile key from an actor or provider@model shorthand."""
    return parse_agent_ref(actor).profile_key


def safe_instance_name(value: str) -> str:
    """Make a stable identifier safe for MCP server names and generated files."""
    cleaned = _SAFE_NAME_RE.sub("_", value).strip("._-")
    return cleaned or "agent"


def mcp_server_name_for_actor(actor: str, default: str = DEFAULT_MCP_SERVER_NAME) -> str:
    """Use the legacy server name for simple actors, unique names for instances."""
    if "@" not in actor:
        return default
    return f"{default}_{safe_instance_name(actor)}"


def _default_actor_for_model(model: str, cli: str) -> str:
    raw = f"{model}@{cli}"
    try:
        return validate_actor(raw)
    except ValueError:
        return f"{safe_instance_name(model)}@{cli}"


def parse_actor_list(actors: str | Iterable[str] | None) -> list[str]:
    """Parse a comma/space separated actor list, preserving order."""
    if actors is None:
        raw = list(DEFAULT_COUNCIL_ACTORS)
    elif isinstance(actors, str):
        raw = actors.replace(",", " ").split()
    else:
        raw = list(actors)

    parsed: list[str] = []
    seen: set[str] = set()
    for actor in raw:
        normalized = parse_agent_ref(str(actor)).actor
        if normalized in seen:
            continue
        seen.add(normalized)
        parsed.append(normalized)

    if not parsed:
        raise ValueError("at least one actor is required")
    return parsed


def _stdio_server_config(actor: str, broker: str, cwd: str, project_dir: str) -> dict:
    spec = build_mcp_spec(
        actor=actor,
        broker=broker,
        cwd=cwd,
        project_dir=project_dir,
    )
    return {
        "type": "stdio",
        "command": spec["command"],
        "args": spec["args"],
        "env": spec.get("env", {}),
    }


def build_mcp_json_config(
    actor: str,
    *,
    broker: str,
    cwd: str,
    project_dir: str,
    server_name: str = DEFAULT_MCP_SERVER_NAME,
) -> dict:
    """Build the common Claude/Kimi/Gemini-style MCP JSON shape."""
    validate_actor(server_name)
    return {
        "mcpServers": {
            server_name: _stdio_server_config(
                actor=actor,
                broker=broker,
                cwd=cwd,
                project_dir=project_dir,
            )
        }
    }


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_array(values: list[str]) -> str:
    if not values:
        return "[]"
    body = ",\n".join(f"  {_toml_string(value)}" for value in values)
    return f"[\n{body}\n]"


def build_codex_config_toml(
    actor: str,
    *,
    broker: str,
    cwd: str,
    project_dir: str,
    server_name: str = DEFAULT_MCP_SERVER_NAME,
) -> str:
    """Build a Codex `config.toml` snippet for the channel MCP server."""
    validate_actor(server_name)
    spec = build_mcp_spec(
        actor=actor,
        broker=broker,
        cwd=cwd,
        project_dir=project_dir,
    )
    return "\n".join(
        [
            f"[mcp_servers.{server_name}]",
            f"command = {_toml_string(spec['command'])}",
            f"args = {_toml_array(spec['args'])}",
            "",
        ]
    )


def build_opencode_config(
    actor: str,
    *,
    broker: str,
    cwd: str,
    project_dir: str,
    server_name: str = DEFAULT_MCP_SERVER_NAME,
) -> dict:
    """Build an OpenCode config fragment for a local MCP server."""
    validate_actor(server_name)
    spec = build_mcp_spec(
        actor=actor,
        broker=broker,
        cwd=cwd,
        project_dir=project_dir,
    )
    return {
        "mcp": {
            server_name: {
                "type": "local",
                "command": [spec["command"], *spec["args"]],
                "environment": spec.get("env", {}),
                "enabled": True,
            }
        }
    }


def build_council_config(
    *,
    actors: str | Iterable[str] | None = None,
    broker: str = "ws://127.0.0.1:9100",
    cwd: str | None = None,
    room: str = "room1",
    project_dir: str | None = None,
    server_name: str = DEFAULT_MCP_SERVER_NAME,
    reasoning_effort: str | None = None,
) -> dict:
    workspace = os.path.abspath(cwd or os.getcwd())
    project_root = os.path.abspath(
        project_dir or str(Path(__file__).resolve().parents[2])
    )
    validate_actor(server_name)

    agent_entries: list[dict] = []
    for actor in parse_actor_list(actors):
        parsed_ref = parse_agent_ref(actor)
        actor = parsed_ref.actor
        profile_key = parsed_ref.profile_key
        model_from_actor = parsed_ref.model
        profile = get_agent_profile(profile_key)
        common_config = f"mcp/{actor}.mcp.json"
        config_files: dict[str, str] = {"mcpJson": common_config}
        cli = profile.cli if profile else None
        agent_server_name = mcp_server_name_for_actor(actor, server_name)
        model_config: dict = {"selection": "manual-in-tui"}
        if model_from_actor:
            model_config = {
                "selection": "configured",
                "desired": model_from_actor,
            }
        reasoning_config: dict = {"desired": {}}
        if reasoning_effort:
            reasoning_config["desired"]["reasoning_effort"] = reasoning_effort

        if cli == "claude":
            config_files["claudeProjectMcpJson"] = common_config
        elif cli == "codex":
            config_files["codexConfigToml"] = f"mcp/{actor}.codex.config.toml"
        elif cli == "gemini":
            config_files["geminiSettingsJson"] = f"mcp/{actor}.gemini.settings.json"
        elif cli == "kimi":
            config_files["kimiMcpConfigFile"] = common_config
        elif cli == "opencode":
            config_files["opencodeJson"] = f"mcp/{actor}.opencode.json"

        agent_entries.append(
            {
                "actor": actor,
                "enabled": True,
                "profile": profile.actor if profile else "custom",
                "label": profile.label if profile else actor,
                "cli": cli,
                "accessMode": "max",
                "model": model_config,
                "reasoning": reasoning_config,
                "mcpServerName": agent_server_name,
                "mcp": build_mcp_spec(
                    actor=actor,
                    broker=broker,
                    cwd=workspace,
                    project_dir=project_root,
                ),
                "configFiles": config_files,
            }
        )

    return {
        "version": CONFIG_VERSION,
        "room": room,
        "broker": broker,
        "workspace": workspace,
        "projectDir": project_root,
        "mcpServerName": server_name,
        "agents": agent_entries,
        "interaction": {
            "userSurface": "web-or-terminal-viewer",
            "note": "Agents are background CLI participants connected through MCP; the user speaks from the Agent Council viewer.",
        },
    }


def _agent_entry_from_spec(
    spec: dict,
    *,
    broker: str,
    workspace: str,
    project_root: str,
    server_name: str,
    group_cli: str | None = None,
) -> dict:
    ref = str(spec.get("id") or spec.get("actor") or spec.get("ref") or "").strip()
    alias = str(spec.get("alias") or "").strip()
    model = spec.get("model_id") or spec.get("model")

    parsed = parse_agent_ref(ref) if ref else None
    profile_key = str(
        spec.get("cli")
        or spec.get("provider")
        or group_cli
        or (parsed.profile_key if parsed else "")
    ).lower()
    if not profile_key:
        raise ValueError("agent entry requires a CLI group, cli, provider, or model@cli id")

    profile = get_agent_profile(profile_key)
    cli = profile.cli if profile else profile_key
    if model is None and parsed is not None:
        model = parsed.model

    if alias:
        actor = validate_actor(alias)
    elif ref and parsed is not None:
        actor = parsed.actor
    elif model:
        actor = _default_actor_for_model(str(model), cli)
    else:
        actor = validate_actor(cli)

    agent_server_name = (
        f"{server_name}_{safe_instance_name(actor)}"
        if group_cli or alias
        else mcp_server_name_for_actor(actor, server_name)
    )

    config_files: dict[str, str] = {"mcpJson": f"mcp/{actor}.mcp.json"}
    if cli == "claude":
        config_files["claudeProjectMcpJson"] = config_files["mcpJson"]
    elif cli == "codex":
        config_files["codexConfigToml"] = f"mcp/{actor}.codex.config.toml"
    elif cli == "gemini":
        config_files["geminiSettingsJson"] = f"mcp/{actor}.gemini.settings.json"
    elif cli == "kimi":
        config_files["kimiMcpConfigFile"] = config_files["mcpJson"]
    elif cli == "opencode":
        config_files["opencodeJson"] = f"mcp/{actor}.opencode.json"

    desired: dict = {}
    for source_key, target_key in (
        ("reasoning_effort", "reasoning_effort"),
        ("effort", "reasoning_effort"),
        ("thinking", "thinking"),
        ("thinking_budget", "thinking_budget"),
        ("thinking_level", "thinking_level"),
        ("variant", "variant"),
    ):
        if source_key in spec:
            desired[target_key] = spec[source_key]

    return {
        "actor": actor,
        "enabled": bool(spec.get("enabled", True)),
        "profile": profile.actor if profile else "custom",
        "label": str(spec.get("label") or alias or (f"{model}@{cli}" if model else actor)),
        "cli": cli,
        "role": str(spec.get("role", "")),
        "accessMode": str(spec.get("access_mode") or spec.get("accessMode") or "max"),
        "model": (
            {"selection": "configured", "desired": str(model)}
            if model
            else {"selection": "manual-in-tui"}
        ),
        "reasoning": {"desired": desired},
        "mcpServerName": agent_server_name,
        "mcp": build_mcp_spec(
            actor=actor,
            broker=broker,
            cwd=workspace,
            project_dir=project_root,
        ),
        "configFiles": config_files,
    }


def _iter_agent_specs_from_toml(raw: dict) -> list[tuple[dict, str | None]]:
    specs: list[tuple[dict, str | None]] = []

    legacy_agents = raw.get("agents")
    if isinstance(legacy_agents, list):
        for item in legacy_agents:
            if isinstance(item, str):
                specs.append(({"id": item}, None))
            elif isinstance(item, dict):
                specs.append((item, None))
            else:
                raise ValueError("[[agents]] entries must be TOML tables")

    def add_group(cli: str, group: object) -> None:
        if not isinstance(group, dict):
            return
        if group.get("enabled") is False:
            return
        group_agents = group.get("agents")
        if not isinstance(group_agents, list):
            return
        defaults = {
            key: value
            for key, value in group.items()
            if key != "agents" and key not in ("enabled",)
        }
        for item in group_agents:
            if not isinstance(item, dict):
                raise ValueError(f"[[{cli}.agents]] entries must be TOML tables")
            merged = {**defaults, **item}
            specs.append((merged, cli))

    for cli in DEFAULT_COUNCIL_ACTORS:
        add_group(cli, raw.get(cli))

    tui_groups = raw.get("tui")
    if isinstance(tui_groups, dict):
        for cli in DEFAULT_COUNCIL_ACTORS:
            add_group(cli, tui_groups.get(cli))
        for cli, group in tui_groups.items():
            if cli not in DEFAULT_COUNCIL_ACTORS:
                add_group(str(cli), group)

    for key, value in raw.items():
        if key in _ROOT_CONFIG_KEYS or key in DEFAULT_COUNCIL_ACTORS:
            continue
        if isinstance(value, dict) and isinstance(value.get("agents"), list):
            add_group(str(key), value)

    return specs


def _validate_unique_agents(agents: list[dict]) -> None:
    seen: dict[str, str] = {}
    for agent in agents:
        actor = agent["actor"]
        previous = seen.get(actor)
        if previous is not None:
            raise ValueError(f"duplicate agent alias/actor {actor!r}")
        seen[actor] = actor


def build_council_config_from_toml(
    config_path: str | Path,
    *,
    workdir: str | None = None,
) -> dict:
    path = Path(config_path)
    raw = tomllib.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a TOML table")

    room = str(raw.get("room") or "room1")
    broker = str(raw.get("broker") or "ws://127.0.0.1:9100")
    workspace_value = workdir or raw.get("workdir")
    if not workspace_value:
        raise ValueError("workdir is required in config.toml or /init <workdir>")
    workspace = os.path.abspath(str(workspace_value))
    project_root = os.path.abspath(str(raw.get("project_dir") or path.parent))
    server_name = str(raw.get("mcp_server_name") or DEFAULT_MCP_SERVER_NAME)
    validate_actor(server_name)

    agent_specs = _iter_agent_specs_from_toml(raw)
    if not agent_specs:
        raise ValueError(
            "config.toml requires at least one grouped [[codex.agents]] "
            "or legacy [[agents]] entry"
        )

    agents = []
    for spec, group_cli in agent_specs:
        agents.append(
            _agent_entry_from_spec(
                spec,
                broker=broker,
                workspace=workspace,
                project_root=project_root,
                server_name=server_name,
                group_cli=group_cli,
            )
        )
    _validate_unique_agents(agents)

    return {
        "version": CONFIG_VERSION,
        "room": room,
        "broker": broker,
        "workspace": workspace,
        "projectDir": project_root,
        "mcpServerName": server_name,
        "agents": agents,
        "interaction": {
            "userSurface": "tmux-viewer-first",
            "note": "start.py launches viewer/server first; /init materializes and launches agent TUIs.",
        },
    }


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_materialized_council_config(
    output_dir: str | Path,
    council: dict,
    *,
    force: bool = True,
) -> list[Path]:
    out = Path(output_dir)
    planned: list[Path] = [out / "council.json"]
    for agent in council.get("agents", []):
        actor = agent["actor"]
        planned.append(out / "mcp" / f"{actor}.mcp.json")
        config_files = agent.get("configFiles", {})
        for key in ("codexConfigToml", "geminiSettingsJson", "opencodeJson"):
            if key in config_files:
                planned.append(out / config_files[key])

    existing = [path for path in planned if path.exists()]
    if existing and not force:
        existing_list = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"refusing to overwrite existing config: {existing_list}")

    (out / "mcp").mkdir(parents=True, exist_ok=True)
    _write_json(out / "council.json", council)

    for agent in council.get("agents", []):
        actor = agent["actor"]
        server_name = agent.get("mcpServerName") or council.get("mcpServerName") or DEFAULT_MCP_SERVER_NAME
        common = build_mcp_json_config(
            actor,
            broker=council["broker"],
            cwd=council["workspace"],
            project_dir=council["projectDir"],
            server_name=server_name,
        )
        _write_json(out / "mcp" / f"{actor}.mcp.json", common)

        cli = agent.get("cli")
        if cli == "codex":
            (out / "mcp" / f"{actor}.codex.config.toml").write_text(
                build_codex_config_toml(
                    actor,
                    broker=council["broker"],
                    cwd=council["workspace"],
                    project_dir=council["projectDir"],
                    server_name=server_name,
                )
            )
        elif cli == "gemini":
            _write_json(out / "mcp" / f"{actor}.gemini.settings.json", common)
        elif cli == "opencode":
            _write_json(
                out / "mcp" / f"{actor}.opencode.json",
                build_opencode_config(
                    actor,
                    broker=council["broker"],
                    cwd=council["workspace"],
                    project_dir=council["projectDir"],
                    server_name=server_name,
                ),
            )

    return planned


def write_council_config(
    output_dir: str | Path,
    *,
    actors: str | Iterable[str] | None = None,
    broker: str = "ws://127.0.0.1:9100",
    cwd: str | None = None,
    room: str = "room1",
    project_dir: str | None = None,
    server_name: str = DEFAULT_MCP_SERVER_NAME,
    reasoning_effort: str | None = None,
    force: bool = False,
) -> list[Path]:
    """Write `.agent-council` council config artifacts and return created paths."""
    out = Path(output_dir)
    workspace = os.path.abspath(cwd or os.getcwd())
    project_root = os.path.abspath(
        project_dir or str(Path(__file__).resolve().parents[2])
    )
    parsed_actors = parse_actor_list(actors)

    planned: list[Path] = [out / "council.json"]
    for actor in parsed_actors:
        planned.append(out / "mcp" / f"{actor}.mcp.json")
        profile = get_agent_profile(infer_profile_key(actor))
        if profile and profile.cli == "codex":
            planned.append(out / "mcp" / f"{actor}.codex.config.toml")
        elif profile and profile.cli == "gemini":
            planned.append(out / "mcp" / f"{actor}.gemini.settings.json")
        elif profile and profile.cli == "opencode":
            planned.append(out / "mcp" / f"{actor}.opencode.json")

    existing = [path for path in planned if path.exists()]
    if existing and not force:
        existing_list = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"refusing to overwrite existing config: {existing_list}")

    (out / "mcp").mkdir(parents=True, exist_ok=True)

    council = build_council_config(
        actors=parsed_actors,
        broker=broker,
        cwd=workspace,
        room=room,
        project_dir=project_root,
        server_name=server_name,
        reasoning_effort=reasoning_effort,
    )
    _write_json(out / "council.json", council)

    for actor in parsed_actors:
        common = build_mcp_json_config(
            actor,
            broker=broker,
            cwd=workspace,
            project_dir=project_root,
            server_name=mcp_server_name_for_actor(actor, server_name),
        )
        _write_json(out / "mcp" / f"{actor}.mcp.json", common)

        profile = get_agent_profile(infer_profile_key(actor))
        agent_server_name = mcp_server_name_for_actor(actor, server_name)
        if profile and profile.cli == "codex":
            (out / "mcp" / f"{actor}.codex.config.toml").write_text(
                build_codex_config_toml(
                    actor,
                    broker=broker,
                    cwd=workspace,
                    project_dir=project_root,
                    server_name=agent_server_name,
                )
            )
        elif profile and profile.cli == "gemini":
            _write_json(out / "mcp" / f"{actor}.gemini.settings.json", common)
        elif profile and profile.cli == "opencode":
            _write_json(
                out / "mcp" / f"{actor}.opencode.json",
                build_opencode_config(
                    actor,
                    broker=broker,
                    cwd=workspace,
                    project_dir=project_root,
                    server_name=agent_server_name,
                ),
            )

    return planned
