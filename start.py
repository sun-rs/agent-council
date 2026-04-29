from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.toml"


def _shell_join(argv: list[str]) -> str:
    return shlex.join(argv)


def _load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    data = tomllib.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a TOML table")
    return data


def _broker_host_port(raw: str) -> tuple[str, int]:
    parsed = urlparse(raw)
    if parsed.scheme != "ws" or parsed.hostname is None or parsed.port is None:
        raise ValueError(f"broker must be ws://host:port, got {raw!r}")
    return parsed.hostname, parsed.port


def _export_env(env: dict[str, str], command: str) -> str:
    exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    return f"export {exports}; {command}"


def _viewer_command(*, config_path: Path, config: dict, session: str, agent_window: str) -> str:
    room = str(config.get("room") or "room1")
    broker = str(config.get("broker") or "ws://127.0.0.1:9100")
    host, port = _broker_host_port(broker)
    db_path = ROOT / ".agent-council" / "warroom.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    env = {
        "AGENT_COUNCIL_CONFIG": str(config_path),
        "AGENT_COUNCIL_SESSION": session,
        "AGENT_COUNCIL_AGENT_WINDOW": agent_window,
        "AGENT_COUNCIL_ROOM": room,
    }
    tmux_cfg = config.get("tmux") if isinstance(config.get("tmux"), dict) else {}
    if tmux_cfg.get("listen_delay_s") is not None:
        env["AGENT_COUNCIL_LISTEN_DELAY_S"] = str(tmux_cfg["listen_delay_s"])
    if config.get("workdir"):
        env["AGENT_COUNCIL_WORKDIR"] = str(config["workdir"])

    command = _shell_join(
        [
            "uv",
            "--directory",
            str(ROOT),
            "run",
            "python",
            "-m",
            "warroom.channel.cli",
            "start",
            "--host",
            host,
            "--port",
            str(port),
            "--db",
            str(db_path),
            "--room",
            room,
        ]
    )
    return f"cd {shlex.quote(str(ROOT))} && {_export_env(env, command)}"


def _agent_placeholder_command(config_path: Path) -> str:
    text = (
        "[agent-council agents]\n\n"
        "This window is reserved for agent TUIs.\n"
        "Go to the viewer window and type /init.\n"
        "If config.toml has no workdir, /init will ask for one.\n\n"
        f"config: {config_path}\n"
    )
    return f"printf '%s' {shlex.quote(text)}; exec ${'{'}SHELL:-/bin/zsh{'}'}"


def _run(command: list[str], *, dry_run: bool) -> None:
    if dry_run:
        print(_shell_join(command))
        return
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the staged Agent Council console")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Root TOML config")
    parser.add_argument("--session", default="", help="tmux session name override")
    parser.add_argument("--agent-window", default="", help="tmux agent window name override")
    parser.add_argument("--no-attach", action="store_true", help="Create tmux session without attaching")
    parser.add_argument("--no-replace", action="store_true", help="Do not kill an existing tmux session")
    parser.add_argument("--dry-run", action="store_true", help="Print tmux commands without running")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    try:
        config = _load_config(config_path)
    except Exception as e:
        print(f"[agent-council] {e}", file=sys.stderr)
        raise SystemExit(2)

    tmux_cfg = config.get("tmux") if isinstance(config.get("tmux"), dict) else {}
    session = args.session or str(tmux_cfg.get("session") or "agent-council")
    agent_window = args.agent_window or str(tmux_cfg.get("agent_window") or "agents")

    commands: list[list[str]] = []
    if not args.no_replace:
        commands.append(["tmux", "kill-session", "-t", session])
    commands.extend(
        [
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session,
                "-n",
                "viewer",
                _viewer_command(
                    config_path=config_path,
                    config=config,
                    session=session,
                    agent_window=agent_window,
                ),
            ],
            [
                "tmux",
                "new-window",
                "-d",
                "-t",
                session,
                "-n",
                agent_window,
                _agent_placeholder_command(config_path),
            ],
            ["tmux", "select-window", "-t", f"{session}:viewer"],
            ["tmux", "set-option", "-t", session, "remain-on-exit", "on"],
        ]
    )
    if not args.no_attach:
        commands.append(["tmux", "attach-session", "-t", session])

    for index, command in enumerate(commands):
        try:
            _run(command, dry_run=args.dry_run)
        except subprocess.CalledProcessError as e:
            # kill-session is intentionally best-effort in replace mode.
            if index == 0 and command[:2] == ["tmux", "kill-session"] and not args.no_replace:
                continue
            raise


if __name__ == "__main__":
    main()
