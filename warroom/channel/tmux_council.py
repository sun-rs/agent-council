"""tmux launcher for a visible multi-CLI council console."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from warroom.channel.council_prompt import join_listen_prompt
from warroom.channel.council_config import (
    build_council_config_from_toml,
    build_mcp_json_config,
    parse_actor_list,
    write_materialized_council_config,
)


@dataclass(frozen=True)
class PaneSpec:
    name: str
    command: str


@dataclass(frozen=True)
class TmuxPlan:
    session_name: str
    panes: list[PaneSpec]
    skipped: list[str]
    room: str = "room1"


def load_council_config(path: str | Path) -> tuple[Path, dict]:
    config_path = Path(path)
    return config_path, json.loads(config_path.read_text())


def _shell_join(argv: Iterable[str]) -> str:
    return shlex.join(list(argv))


def _cd_and_run(cwd: str, command: str) -> str:
    return f"cd {shlex.quote(cwd)} && {command}"


def _hold(command: str) -> str:
    return f"{command}; printf '\\n[exit] press Enter to close pane... '; read _"


def _with_pane_banner(pane: PaneSpec, skipped: list[str] | None = None) -> str:
    lines = [f"[agent-council-pane] {pane.name}"]
    if skipped:
        lines.append("[agent-council] skipped:")
        lines.extend(f"  {item}" for item in skipped)
    banner = "\n".join(lines) + "\n\n"
    return f"printf '%s' {shlex.quote(banner)}; {pane.command}"


def _broker_host_port(broker: str) -> tuple[str, int]:
    parsed = urlparse(broker)
    if parsed.scheme != "ws" or not parsed.hostname or parsed.port is None:
        raise ValueError(f"run-council can only launch a local ws:// broker, got {broker!r}")
    return parsed.hostname, parsed.port


def _relative_config_path(config_dir: Path, agent: dict, key: str) -> Path | None:
    value = agent.get("configFiles", {}).get(key)
    if not value:
        return None
    return config_dir / value


def _mcp_server(agent: dict) -> dict:
    return agent["mcp"]


def _desired_model(agent: dict) -> str | None:
    model = agent.get("model")
    if isinstance(model, str):
        return model
    if not isinstance(model, dict):
        return None
    value = model.get("desired") or model.get("id") or model.get("name")
    return str(value) if value else None


def _reasoning_effort(agent: dict) -> str | None:
    reasoning = agent.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    desired = reasoning.get("desired")
    if not isinstance(desired, dict):
        return None
    value = (
        desired.get("reasoning_effort")
        or desired.get("effort")
        or desired.get("model_reasoning_effort")
    )
    return str(value) if value else None


def _thinking_enabled(agent: dict) -> bool | None:
    reasoning = agent.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    desired = reasoning.get("desired")
    if not isinstance(desired, dict):
        return None
    value = desired.get("thinking")
    return value if isinstance(value, bool) else None


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _codex_mcp_overrides(agent: dict) -> list[str]:
    server_name = agent.get("mcpServerName") or "channel"
    spec = _mcp_server(agent)
    return [
        "-c",
        f"mcp_servers.{server_name}.command={json.dumps(spec['command'])}",
        "-c",
        f"mcp_servers.{server_name}.args={json.dumps(spec['args'])}",
    ]


def _opencode_config(agent: dict) -> dict:
    server_name = agent.get("mcpServerName") or "channel"
    spec = _mcp_server(agent)
    return {
        "mcp": {
            server_name: {
                "type": "local",
                "command": [spec["command"], *spec["args"]],
                "environment": spec.get("env", {}),
                "enabled": True,
            }
        },
        "permission": "allow",
    }


def _merge_json_file(path: Path, patch: dict) -> None:
    current: dict = {}
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"cannot merge malformed JSON file {path}: {e}") from e
        if not isinstance(current, dict):
            raise ValueError(f"cannot merge non-object JSON file {path}")

    merged = dict(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")


def prepare_cli_configs(
    config_dir: Path,
    council: dict,
    *,
    actors: set[str] | None = None,
) -> list[Path]:
    """Write only the project configs required by CLIs that lack a config-file flag."""
    written: list[Path] = []
    workspace = council["workspace"]
    broker = council["broker"]
    project_dir = council["projectDir"]
    server_name = council.get("mcpServerName") or "channel"

    for agent in council.get("agents", []):
        if agent.get("enabled") is False:
            continue
        if actors is not None and agent.get("actor") not in actors:
            continue
        if agent.get("cli") != "gemini":
            continue
        actor = agent["actor"]
        agent_server_name = agent.get("mcpServerName") or server_name
        target = Path(workspace) / ".gemini" / "settings.json"
        _merge_json_file(
            target,
            build_mcp_json_config(
                actor,
                broker=broker,
                cwd=workspace,
                project_dir=project_dir,
                server_name=agent_server_name,
            ),
        )
        written.append(target)

    return written


def build_agent_command(config_dir: Path, council: dict, agent: dict) -> str:
    workspace = council["workspace"]
    cli = agent.get("cli")
    actor = agent["actor"]
    mcp_json = _relative_config_path(config_dir, agent, "mcpJson")
    model = _desired_model(agent)
    effort = _reasoning_effort(agent)

    if cli == "claude":
        if not mcp_json:
            raise ValueError(f"{actor}: missing mcpJson config")
        argv = [
            "claude",
            "--mcp-config",
            str(mcp_json),
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
        ]
        if model:
            argv.extend(["--model", model])
        if effort:
            argv.extend(["--effort", effort])
        return _cd_and_run(
            workspace,
            _shell_join(argv),
        )

    if cli == "codex":
        argv = [
            "codex",
            "--cd",
            workspace,
            "--ask-for-approval",
            "never",
            "--sandbox",
            "danger-full-access",
        ]
        if model:
            argv.extend(["--model", model])
        if effort:
            argv.extend(["-c", f"model_reasoning_effort={_toml_string(effort)}"])
        argv.extend(_codex_mcp_overrides(agent))
        return _shell_join(
            argv
        )

    if cli == "gemini":
        argv = [
            "gemini",
            "--approval-mode",
            "yolo",
            "--skip-trust",
            "--allowed-mcp-server-names",
            agent.get("mcpServerName") or "channel",
        ]
        if model:
            argv.extend(["--model", model])
        return _cd_and_run(
            workspace,
            _shell_join(argv),
        )

    if cli == "kimi":
        if not mcp_json:
            raise ValueError(f"{actor}: missing mcpJson config")
        argv = [
            "kimi",
            "--work-dir",
            workspace,
            "--yolo",
            "--mcp-config-file",
            str(mcp_json),
        ]
        if model:
            argv.extend(["--model", model])
        thinking = _thinking_enabled(agent)
        if thinking is True:
            argv.append("--thinking")
        elif thinking is False:
            argv.append("--no-thinking")
        return _shell_join(
            argv
        )

    if cli == "opencode":
        config_content = json.dumps(_opencode_config(agent), separators=(",", ":"))
        argv = [
            "env",
            f"OPENCODE_CONFIG_CONTENT={config_content}",
            "opencode",
        ]
        if model:
            argv.extend(["--model", model])
        argv.append(workspace)
        return _shell_join(
            argv
        )

    raise ValueError(f"{actor}: unsupported CLI profile {cli!r}")


def build_tmux_plan(
    config_path: str | Path,
    *,
    actors: str | Iterable[str] | None = None,
    session_name: str = "agent-council",
    skip_missing: bool = True,
) -> TmuxPlan:
    config_path, council = load_council_config(config_path)
    config_dir = config_path.parent
    selected = set(parse_actor_list(actors)) if actors else None

    project_dir = council["projectDir"]
    room = council.get("room") or "room1"
    broker = council.get("broker") or "ws://127.0.0.1:9100"
    db_path = str(config_dir / "warroom.db")
    broker_host, broker_port = _broker_host_port(broker)

    panes = [
        PaneSpec(
            "viewer",
            _cd_and_run(
                project_dir,
                _shell_join(
                    [
                        "uv",
                        "--directory",
                        project_dir,
                        "run",
                        "python",
                        "-m",
                        "warroom.channel.cli",
                        "start",
                        "--host",
                        broker_host,
                        "--port",
                        str(broker_port),
                        "--db",
                        db_path,
                        "--room",
                        room,
                    ]
                ),
            ),
        ),
    ]
    skipped: list[str] = []
    seen_selected: set[str] = set()

    for agent in council.get("agents", []):
        actor = agent["actor"]
        if selected is not None and actor not in selected:
            skipped.append(f"{actor}: not selected")
            continue
        if selected is not None:
            seen_selected.add(actor)
        if agent.get("enabled") is False:
            skipped.append(f"{actor}: disabled")
            continue
        cli = agent.get("cli")
        if not cli:
            skipped.append(f"{actor}: custom agent has no launch profile")
            continue
        if shutil.which(cli) is None:
            message = f"{actor}: {cli} not found"
            if skip_missing:
                skipped.append(message)
                continue
            raise FileNotFoundError(message)
        panes.append(PaneSpec(actor, build_agent_command(config_dir, council, agent)))

    if selected is not None:
        for actor in sorted(selected - seen_selected):
            skipped.append(f"{actor}: not present in council config")

    return TmuxPlan(session_name=session_name, panes=panes, skipped=skipped, room=room)


def _auto_listen_command(plan: TmuxPlan, delay_s: float) -> list[str] | None:
    agent_panes = plan.panes[1:]
    if not agent_panes:
        return None
    actions = [f"sleep {delay_s:g}"]
    for pane_index, pane in enumerate(agent_panes, start=1):
        target = f"{plan.session_name}:0.{pane_index}"
        prompt = join_listen_prompt(plan.room, actor=pane.name)
        actions.append(f"tmux send-keys -t {shlex.quote(target)} -l {shlex.quote(prompt)}")
        actions.append("sleep 0.2")
        actions.append(f"tmux send-keys -t {shlex.quote(target)} Enter")
    return ["tmux", "run-shell", "-b", "; ".join(actions)]


def _viewer_init_env_command(plan: TmuxPlan, command: str) -> str:
    agent_targets = [
        f"{plan.session_name}:0.{pane_index}"
        for pane_index, _pane in enumerate(plan.panes[1:], start=1)
    ]
    if not agent_targets:
        return command
    env = {
        "AGENT_COUNCIL_AGENT_PANES": json.dumps(agent_targets, separators=(",", ":")),
        "AGENT_COUNCIL_AGENT_PANE_ACTORS": json.dumps(
            {
                target: pane.name
                for target, pane in zip(agent_targets, plan.panes[1:], strict=True)
            },
            separators=(",", ":"),
        ),
        "AGENT_COUNCIL_ROOM": plan.room,
        "AGENT_COUNCIL_SESSION": plan.session_name,
    }
    exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    return f"export {exports}; {command}"


def tmux_script(
    plan: TmuxPlan,
    *,
    attach: bool = True,
    auto_listen: bool = False,
    listen_delay_s: float = 8.0,
) -> list[list[str]]:
    if not plan.panes:
        raise ValueError("tmux plan has no panes")

    first = plan.panes[0]
    agents = plan.panes[1:]
    commands: list[list[str]] = [
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            plan.session_name,
            "-n",
            "council",
            _hold(
                _with_pane_banner(
                    PaneSpec(first.name, _viewer_init_env_command(plan, first.command)),
                    plan.skipped,
                )
            ),
        ],
        ["tmux", "select-pane", "-t", f"{plan.session_name}:0", "-T", first.name],
        ["tmux", "set-option", "-t", plan.session_name, "focus-events", "on"],
        [
            "tmux",
            "set-window-option",
            "-t",
            f"{plan.session_name}:0",
            "main-pane-width",
            "50%",
        ],
    ]
    for index, pane in enumerate(agents):
        split_direction = "-h" if index == 0 else "-v"
        split_extra = ["-p", "50"] if index == 0 else []
        commands.append(
            [
                "tmux",
                "split-window",
                "-t",
                f"{plan.session_name}:0",
                split_direction,
                *split_extra,
                _hold(_with_pane_banner(pane)),
            ]
        )
        commands.append(
            ["tmux", "select-pane", "-t", f"{plan.session_name}:0", "-T", pane.name]
        )
        commands.append(["tmux", "select-layout", "-t", f"{plan.session_name}:0", "main-vertical"])
    if agents:
        commands.append(["tmux", "select-pane", "-t", f"{plan.session_name}:0.0"])
    if auto_listen:
        command = _auto_listen_command(plan, listen_delay_s)
        if command:
            commands.append(command)
    commands.append(["tmux", "set-option", "-t", plan.session_name, "remain-on-exit", "on"])
    if attach:
        commands.append(["tmux", "attach-session", "-t", plan.session_name])
    return commands


def render_tmux_commands(commands: list[list[str]]) -> str:
    return "\n".join(_shell_join(command) for command in commands)


def _tmux_session_exists(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _run_tmux_command(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        cmd = e.cmd if isinstance(e.cmd, list) else [str(e.cmd)]
        raise ValueError(f"tmux command failed ({e.returncode}): {_shell_join(cmd)}") from e


def _tmux_window_exists(session_name: str, window_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return False
    return window_name in {line.strip() for line in result.stdout.splitlines()}


def _agent_window_auto_listen_command(
    *,
    session_name: str,
    window_name: str,
    room: str,
    actors: list[str],
    delay_s: float,
) -> list[str] | None:
    if not actors:
        return None
    actions = [f"sleep {delay_s:g}"]
    for pane_index, actor in enumerate(actors):
        target = f"{session_name}:{window_name}.{pane_index}"
        prompt = join_listen_prompt(room, actor=actor)
        actions.append(f"tmux send-keys -t {shlex.quote(target)} -l {shlex.quote(prompt)}")
        actions.append("sleep 0.2")
        actions.append(f"tmux send-keys -t {shlex.quote(target)} Enter")
    return ["tmux", "run-shell", "-b", "; ".join(actions)]


def launch_agents_from_toml(
    config_path: str | Path,
    *,
    workdir: str,
    session_name: str = "agent-council",
    window_name: str = "agents",
    output_dir: str | Path | None = None,
    auto_listen: bool = True,
    listen_delay_s: float = 8.0,
    dry_run: bool = False,
) -> tuple[list[str], list[Path], list[list[str]]]:
    """Materialize TOML config and launch only the agent TUI window."""
    if not dry_run and shutil.which("tmux") is None:
        raise FileNotFoundError("tmux not found")

    config_path = Path(config_path)
    council = build_council_config_from_toml(config_path, workdir=workdir)
    out = Path(output_dir) if output_dir is not None else config_path.parent / ".agent-council"
    written = [] if dry_run else write_materialized_council_config(out, council, force=True)

    enabled_agents = [
        agent for agent in council.get("agents", [])
        if agent.get("enabled") is not False
    ]
    skipped: list[str] = []
    runnable: list[dict] = []
    for agent in enabled_agents:
        actor = agent["actor"]
        cli = agent.get("cli")
        if not cli:
            skipped.append(f"{actor}: custom agent has no launch profile")
            continue
        if shutil.which(cli) is None:
            skipped.append(f"{actor}: {cli} not found")
            continue
        runnable.append(agent)

    if not runnable:
        raise ValueError("no configured agent CLI is available to launch")
    if not dry_run:
        written.extend(
            prepare_cli_configs(
                out,
                council,
                actors={agent["actor"] for agent in runnable},
            )
        )

    first = runnable[0]
    commands: list[list[str]] = []
    if not dry_run and _tmux_window_exists(session_name, window_name):
        commands.append(["tmux", "kill-window", "-t", f"{session_name}:{window_name}"])
    elif dry_run:
        commands.append(["tmux", "kill-window", "-t", f"{session_name}:{window_name}"])

    commands.append(
        [
            "tmux",
            "new-window",
            "-d",
            "-t",
            session_name,
            "-n",
            window_name,
            _hold(
                _with_pane_banner(
                    PaneSpec(first["actor"], build_agent_command(out, council, first)),
                    skipped,
                )
            ),
        ]
    )
    commands.append(["tmux", "select-pane", "-t", f"{session_name}:{window_name}.0", "-T", first["actor"]])

    for agent in runnable[1:]:
        commands.append(
            [
                "tmux",
                "split-window",
                "-t",
                f"{session_name}:{window_name}",
                "-v",
                _hold(
                    _with_pane_banner(
                        PaneSpec(agent["actor"], build_agent_command(out, council, agent))
                    )
                ),
            ]
        )
        commands.append(["tmux", "select-pane", "-t", f"{session_name}:{window_name}", "-T", agent["actor"]])
        commands.append(["tmux", "select-layout", "-t", f"{session_name}:{window_name}", "tiled"])

    if auto_listen:
        listen_command = _agent_window_auto_listen_command(
            session_name=session_name,
            window_name=window_name,
            room=council.get("room") or "room1",
            actors=[agent["actor"] for agent in runnable],
            delay_s=listen_delay_s,
        )
        if listen_command:
            commands.append(listen_command)
    commands.append(["tmux", "select-window", "-t", f"{session_name}:{window_name}"])

    if not dry_run:
        for command in commands:
            _run_tmux_command(command)
    return skipped, written, commands


def run_tmux_council(
    config_path: str | Path,
    *,
    actors: str | Iterable[str] | None = None,
    session_name: str = "agent-council",
    attach: bool = True,
    dry_run: bool = False,
    skip_missing: bool = True,
    replace_existing: bool = False,
    auto_listen: bool = False,
    listen_delay_s: float = 8.0,
) -> tuple[TmuxPlan, list[Path], list[list[str]]]:
    if not dry_run and shutil.which("tmux") is None:
        raise FileNotFoundError("tmux not found")

    config_path, council = load_council_config(config_path)
    plan = build_tmux_plan(
        config_path,
        actors=actors,
        session_name=session_name,
        skip_missing=skip_missing,
    )
    commands = tmux_script(
        plan,
        attach=attach,
        auto_listen=auto_listen,
        listen_delay_s=listen_delay_s,
    )
    launched_actors = {pane.name for pane in plan.panes[1:]}
    written: list[Path] = []
    if not dry_run:
        if _tmux_session_exists(session_name):
            if not replace_existing:
                raise ValueError(
                    f"tmux session {session_name!r} already exists; "
                    f"attach with `tmux attach -t {session_name}`, "
                    f"remove it with `tmux kill-session -t {session_name}`, "
                    "or pass --replace"
                )
            _run_tmux_command(["tmux", "kill-session", "-t", session_name])
        written = prepare_cli_configs(
            config_path.parent,
            council,
            actors=launched_actors,
        )
        for command in commands:
            _run_tmux_command(command)
    return plan, written, commands
