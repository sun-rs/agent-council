"""warroom start — one command to launch broker + viewer.

Usage:
    uv run warroom start
    uv run warroom start --no-viewer
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from warroom.channel.agent_profiles import (
    build_mcp_command,
    format_mcp_spec_json,
    get_agent_profile,
    list_agent_profiles,
    validate_actor,
)
from warroom.channel.broker_server import serve as serve_broker
from warroom.channel.council_config import (
    DEFAULT_COUNCIL_ACTORS,
    write_council_config,
)
from warroom.channel.tmux_council import render_tmux_commands, run_tmux_council


async def _start(host: str, port: int, db_path: str, room: str, no_viewer: bool) -> None:
    stop = asyncio.Event()

    def _on_signal(*_: object) -> None:
        stop.set()

    try:
        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        pass

    # Start broker
    ready = asyncio.Event()
    bound: list[int] = []
    broker_task = asyncio.create_task(serve_broker(
        host=host, port=port, db_path=db_path,
        stop_event=stop, ready_event=ready, bound_port_box=bound,
    ))

    # MED 1 fix: race ready.wait against broker_task so a startup crash
    # surfaces immediately instead of waiting 5s then misreporting.
    ready_task = asyncio.create_task(ready.wait())
    done, _pending = await asyncio.wait(
        {ready_task, broker_task},
        timeout=5.0,
        return_when=asyncio.FIRST_COMPLETED,
    )

    if broker_task in done:
        # Broker crashed during startup — surface the real exception
        ready_task.cancel()
        try:
            await broker_task  # raises the original error
        except Exception as e:
            print(f"[agent-council] broker crashed on startup: {e}", file=sys.stderr)
        return

    if ready_task not in done:
        # Timeout — neither ready nor crashed
        print("[agent-council] broker failed to start (timeout)", file=sys.stderr)
        ready_task.cancel()
        stop.set()
        broker_task.cancel()
        try:
            await broker_task
        except (asyncio.CancelledError, Exception):
            pass
        return

    ready_task = None  # done, no longer needed

    real_port = bound[0] if bound else port
    broker_url = f"ws://{host}:{real_port}"
    print(f"[agent-council] broker ready on {broker_url}")

    if no_viewer:
        print("[agent-council] waiting for agents to connect... (Ctrl+C to stop)")
        await stop.wait()
    else:
        from warroom.channel.viewer import run_viewer
        print(f"[agent-council] starting viewer for {room}...\n")
        viewer_task = asyncio.create_task(run_viewer(broker_url, room))
        stop_task = asyncio.create_task(stop.wait())

        # Wait for either viewer exit or stop signal
        done, pending = await asyncio.wait(
            {viewer_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        stop.set()

        # LOW 3 fix: inspect done to observe viewer exceptions
        if viewer_task in done:
            try:
                viewer_task.result()  # raises if viewer crashed
            except Exception as e:
                print(f"[agent-council] viewer error: {e}", file=sys.stderr)

        # Cancel remaining tasks
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # MED 2 fix: don't use wait_for (it cancels on timeout, skipping cleanup).
    # Instead: set stop, wait with timeout, only cancel after that.
    stop.set()
    try:
        done, _ = await asyncio.wait({broker_task}, timeout=3.0)
        if broker_task not in done:
            broker_task.cancel()
            try:
                await broker_task
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        broker_task.cancel()
        try:
            await broker_task
        except (asyncio.CancelledError, Exception):
            pass

    print("[agent-council] stopped")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-council",
        description="Agent Council — the connective layer between AI agent islands",
    )
    sub = parser.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="Start broker + viewer")
    start_p.add_argument("--host", default="127.0.0.1")
    start_p.add_argument("--port", type=int, default=9100)
    start_p.add_argument("--db", default=os.path.join(os.path.expanduser("~"), ".warroom.db"))
    start_p.add_argument("--room", default="room1")
    start_p.add_argument("--no-viewer", action="store_true", help="Start broker only (headless)")

    sub.add_parser("agents", help="List known CLI agent profiles")

    mcp_p = sub.add_parser("mcp-command", help="Print the MCP shim command for an actor")
    mcp_p.add_argument("--actor", required=True, help="Stable actor name, e.g. claude/codex/gemini")
    mcp_p.add_argument("--broker", default="ws://127.0.0.1:9100", help="Broker WebSocket URL")
    mcp_p.add_argument("--cwd", default="", help="Repo root for git operations")
    mcp_p.add_argument("--format", choices=("command", "json"), default="command")

    init_p = sub.add_parser(
        "init-config",
        aliases=["init"],
        help="Generate a project-level council config and per-CLI MCP snippets",
    )
    init_p.add_argument("--out", default=".agent-council", help="Output directory")
    init_p.add_argument(
        "--actors",
        default=",".join(DEFAULT_COUNCIL_ACTORS),
        help="Comma/space separated actor list",
    )
    init_p.add_argument("--broker", default="ws://127.0.0.1:9100", help="Broker WebSocket URL")
    init_p.add_argument("--cwd", default=os.getcwd(), help="Workspace root for agents")
    init_p.add_argument("--room", default="room1", help="Room name")
    init_p.add_argument("--server-name", default="channel", help="MCP server name")
    init_p.add_argument(
        "--reasoning-effort",
        default="",
        help="Optional default reasoning effort for generated agents, e.g. xhigh",
    )
    init_p.add_argument("--force", action="store_true", help="Overwrite existing generated files")

    run_p = sub.add_parser(
        "run-council",
        help="Launch a visible tmux console for the configured council",
    )
    run_p.add_argument("--config", default=".agent-council/council.json", help="Council config path")
    run_p.add_argument("--actors", default="", help="Optional comma/space separated actor subset")
    run_p.add_argument("--session", default="agent-council", help="tmux session name")
    run_p.add_argument("--no-attach", action="store_true", help="Create tmux session without attaching")
    run_p.add_argument("--dry-run", action="store_true", help="Print tmux commands without running them")
    run_p.add_argument(
        "--replace",
        action="store_true",
        help="Kill an existing tmux session with the same name before launching",
    )
    run_p.add_argument(
        "--auto-listen",
        action="store_true",
        help="After launch, inject the council bootstrap prompt into every agent pane",
    )
    run_p.add_argument(
        "--listen-delay",
        type=float,
        default=8.0,
        help="Seconds to wait before --auto-listen sends prompts",
    )
    run_p.add_argument(
        "--require-all",
        action="store_true",
        help="Fail if a selected CLI binary is not installed",
    )

    up_p = sub.add_parser(
        "up",
        help="Launch the configured council with practical defaults",
    )
    up_p.add_argument("--config", default=".agent-council/council.json", help="Council config path")
    up_p.add_argument("--actors", default="", help="Optional comma/space separated actor subset")
    up_p.add_argument("--session", default="agent-council", help="tmux session name")
    up_p.add_argument("--no-attach", action="store_true", help="Create tmux session without attaching")
    up_p.add_argument("--dry-run", action="store_true", help="Print tmux commands without running them")
    up_p.add_argument("--no-replace", action="store_true", help="Do not replace an existing tmux session")
    up_p.add_argument("--no-auto-listen", action="store_true", help="Do not auto-inject the council bootstrap prompt")
    up_p.add_argument(
        "--listen-delay",
        type=float,
        default=8.0,
        help="Seconds to wait before auto-listen sends prompts",
    )
    up_p.add_argument(
        "--require-all",
        action="store_true",
        help="Fail if a selected CLI binary is not installed",
    )

    args = parser.parse_args()

    if args.command == "start":
        try:
            asyncio.run(_start(args.host, args.port, args.db, args.room, args.no_viewer))
        except KeyboardInterrupt:
            pass
    elif args.command == "agents":
        for profile in list_agent_profiles():
            print(f"{profile.actor:<10} {profile.label:<14} cli={profile.cli:<10} {profile.notes}")
        print("\nAny MCP-compatible CLI can also use a custom --actor name.")
    elif args.command == "mcp-command":
        actor = validate_actor(args.actor)
        profile = get_agent_profile(actor)
        if profile:
            print(f"# {profile.label} ({profile.actor})")
        else:
            print(f"# custom actor: {actor}")
        if args.format == "json":
            print(format_mcp_spec_json(actor=actor, broker=args.broker, cwd=args.cwd or None))
        else:
            print(build_mcp_command(actor=actor, broker=args.broker, cwd=args.cwd or None))
    elif args.command in ("init-config", "init"):
        try:
            paths = write_council_config(
                args.out,
                actors=args.actors,
                broker=args.broker,
                cwd=args.cwd,
                room=args.room,
                server_name=args.server_name,
                reasoning_effort=args.reasoning_effort or None,
                force=args.force,
            )
        except (FileExistsError, ValueError) as e:
            print(f"[agent-council] {e}", file=sys.stderr)
            print("[agent-council] pass --force to overwrite generated files", file=sys.stderr)
            raise SystemExit(2)

        print(f"[agent-council] wrote council config to {os.path.abspath(args.out)}")
        for path in paths:
            print(f"  {path}")
    elif args.command == "run-council":
        try:
            plan, written, commands = run_tmux_council(
                args.config,
                actors=args.actors or None,
                session_name=args.session,
                attach=not args.no_attach,
                dry_run=args.dry_run,
                skip_missing=not args.require_all,
                replace_existing=args.replace,
                auto_listen=args.auto_listen,
                listen_delay_s=args.listen_delay,
            )
        except (FileNotFoundError, ValueError) as e:
            print(f"[agent-council] {e}", file=sys.stderr)
            raise SystemExit(2)

        if args.dry_run:
            print(render_tmux_commands(commands))
        else:
            for path in written:
                print(f"[agent-council] wrote CLI project config: {path}")
        if plan.skipped:
            print("[agent-council] skipped:")
            for item in plan.skipped:
                print(f"  {item}")
    elif args.command == "up":
        try:
            plan, written, commands = run_tmux_council(
                args.config,
                actors=args.actors or None,
                session_name=args.session,
                attach=not args.no_attach,
                dry_run=args.dry_run,
                skip_missing=not args.require_all,
                replace_existing=not args.no_replace,
                auto_listen=not args.no_auto_listen,
                listen_delay_s=args.listen_delay,
            )
        except (FileNotFoundError, ValueError) as e:
            print(f"[agent-council] {e}", file=sys.stderr)
            raise SystemExit(2)

        if args.dry_run:
            print(render_tmux_commands(commands))
        else:
            print(f"[agent-council] council up: {plan.session_name}")
            for path in written:
                print(f"[agent-council] wrote CLI project config: {path}")
        if plan.skipped:
            print("[agent-council] skipped:")
            for item in plan.skipped:
                print(f"  {item}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
