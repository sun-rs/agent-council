"""Terminal viewer for A2A channel messages.

Connects to the broker as actor="user", displays messages in real time,
and lets the user type to post messages.

Uses prompt_toolkit PromptSession + patch_stdout so async-printed messages
don't corrupt the input line.

Run:
    uv run python -m warroom.channel.viewer \
        --broker ws://127.0.0.1:9100 --room room1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from datetime import datetime

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.patch_stdout import patch_stdout

from warroom.channel.council_prompt import join_listen_prompt
from warroom.channel.ws_client import ChannelClient

COUNCIL_AGENT_PANES_ENV = "AGENT_COUNCIL_AGENT_PANES"
COUNCIL_AGENT_PANE_ACTORS_ENV = "AGENT_COUNCIL_AGENT_PANE_ACTORS"
COUNCIL_CONFIG_ENV = "AGENT_COUNCIL_CONFIG"
COUNCIL_AGENT_WINDOW_ENV = "AGENT_COUNCIL_AGENT_WINDOW"
COUNCIL_WORKDIR_ENV = "AGENT_COUNCIL_WORKDIR"
COUNCIL_LISTEN_DELAY_ENV = "AGENT_COUNCIL_LISTEN_DELAY_S"
COUNCIL_SESSION_ENV = "AGENT_COUNCIL_SESSION"
DEFAULT_SEND_ENTER_DELAY_S = 0.2

ACTOR_COLORS = {
    "claude": "ansicyan",
    "codex": "ansimagenta",
    "user": "ansiyellow",
    "system": "ansigreen",
}

# Match ``` code blocks (with optional language tag)
# Match ``` code blocks — allow any non-newline info string (c++, objective-c, etc.)
_CODE_BLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def _terminal_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _format_msg(msg: dict) -> None:
    """Print a single channel message with proper formatting.

    - Header line: [HH:MM:SS] actor:
    - Content lines indented under the header
    - Code blocks get a distinct color
    - Long lines word-wrapped to terminal width

    Defensively handles malformed payloads (missing/wrong-type fields).
    """
    # Defensive normalization (HIGH 3 fix)
    try:
        ts_raw = float(msg.get("ts", 0))
    except (TypeError, ValueError):
        ts_raw = 0.0
    try:
        ts = datetime.fromtimestamp(ts_raw).strftime("%H:%M:%S")
    except (OSError, ValueError):
        ts = "??:??:??"
    actor = str(msg.get("actor", "?"))
    content = str(msg.get("content", ""))
    color = ACTOR_COLORS.get(actor, "ansiwhite")

    # System messages
    if content.startswith("[system]"):
        color = "ansigreen"
        print_formatted_text(FormattedText([
            ("ansigreen", f"  [{ts}] "),
            ("ansigreen bold", f"{content}"),
        ]))
        return

    # Header line
    prefix = f"[{ts}] "
    indent = " " * len(prefix)
    wrap_width = max(_terminal_width() - len(indent) - 2, 40)

    print_formatted_text(FormattedText([
        ("ansigray bold", prefix),
        (f"{color} bold", f"{actor}:"),
    ]))

    # Split content into code blocks and text segments
    parts = _split_code_blocks(content)

    for is_code, lang, text in parts:
        if is_code:
            # Code block: dim border + content
            print_formatted_text(FormattedText([
                ("ansigray", f"{indent}  "),
                ("ansigray bold", f"```{lang}"),
            ]))
            for line in text.splitlines():
                print_formatted_text(FormattedText([
                    ("ansigray", f"{indent}  "),
                    ("ansiwhite", line),
                ]))
            print_formatted_text(FormattedText([
                ("ansigray", f"{indent}  "),
                ("ansigray bold", "```"),
            ]))
        else:
            # Regular text: word-wrap and indent
            for paragraph in text.split("\n"):
                paragraph = paragraph.strip()
                if not paragraph:
                    continue
                # Bullet points: keep as-is with indent
                # Detect list items: -, *, bullet, or numbered (1. 2. etc.)
                list_match = re.match(r'^(\s*(?:[-*]|\d+[.)]))\s+', paragraph)
                if list_match:
                    prefix_len = len(list_match.group(0))
                    wrapped = textwrap.fill(
                        paragraph, width=wrap_width,
                        initial_indent=f"{indent}  ",
                        subsequent_indent=f"{indent}  " + " " * prefix_len,
                    )
                else:
                    wrapped = textwrap.fill(
                        paragraph, width=wrap_width,
                        initial_indent=f"{indent}  ",
                        subsequent_indent=f"{indent}  ",
                    )
                print_formatted_text(FormattedText([("", wrapped)]))

    # Blank line after message for visual separation
    print_formatted_text(FormattedText([("", "")]))


def _split_code_blocks(content: str) -> list[tuple[bool, str, str]]:
    """Split content into (is_code, lang, text) segments.

    Returns alternating text/code segments. Text segments have is_code=False.
    """
    parts: list[tuple[bool, str, str]] = []
    last_end = 0
    for m in _CODE_BLOCK_RE.finditer(content):
        # Text before this code block
        before = content[last_end:m.start()].strip()
        if before:
            parts.append((False, "", before))
        parts.append((True, m.group(1), m.group(2).strip()))
        last_end = m.end()
    # Remaining text after last code block
    remaining = content[last_end:].strip()
    if remaining:
        parts.append((False, "", remaining))
    return parts


def _council_agent_panes() -> list[str]:
    raw = os.environ.get(COUNCIL_AGENT_PANES_ENV, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [item.strip() for item in raw.split(",") if item.strip()]
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str) and item.strip()]


def _council_agent_pane_actors() -> dict[str, str]:
    raw = os.environ.get(COUNCIL_AGENT_PANE_ACTORS_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(target): str(actor)
        for target, actor in parsed.items()
        if isinstance(target, str) and isinstance(actor, str)
    }


def _set_council_agent_panes(mapping: dict[str, str]) -> None:
    ordered_targets = list(mapping)
    os.environ[COUNCIL_AGENT_PANES_ENV] = json.dumps(
        ordered_targets, separators=(",", ":")
    )
    os.environ[COUNCIL_AGENT_PANE_ACTORS_ENV] = json.dumps(
        mapping, separators=(",", ":")
    )


def refresh_agent_panes_from_tmux(session: str, window: str) -> int:
    result = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-t",
            f"{session}:{window}",
            "-F",
            "#{pane_index}\t#{pane_title}",
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    mapping: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        index, sep, title = raw_line.partition("\t")
        actor = title.strip()
        if not sep or not index.strip() or not actor:
            continue
        mapping[f"{session}:{window}.{index.strip()}"] = actor
    if not mapping:
        raise RuntimeError(f"no titled agent panes found in {session}:{window}")
    _set_council_agent_panes(mapping)
    return len(mapping)


def _select_agent_panes(selector: str) -> list[str]:
    normalized = selector.strip()
    panes = _council_agent_panes()
    if not panes:
        raise RuntimeError(
            f"no agent panes configured; {COUNCIL_AGENT_PANES_ENV} is empty"
        )
    if not normalized or normalized in {"all", "*"}:
        return panes

    actor_by_target = _council_agent_pane_actors()
    matches = [
        target
        for target in panes
        if target == normalized
        or actor_by_target.get(target) == normalized
        or actor_by_target.get(target, "").lower() == normalized.lower()
    ]
    if matches:
        return matches

    known = ", ".join(actor_by_target.get(target, target) for target in panes)
    raise RuntimeError(f"unknown agent pane {normalized!r}; known: {known}")


def print_agent_panes() -> None:
    panes = _council_agent_panes()
    actor_by_target = _council_agent_pane_actors()
    if not panes:
        print("[viewer] no agent panes recorded yet")
        return
    for target in panes:
        print(f"[viewer] {actor_by_target.get(target, '?')} -> {target}")


async def send_bootstrap_prompt_to_missing_agent_panes(
    client: ChannelClient,
    room: str,
) -> tuple[int, list[str]]:
    state = await client.room_state(room)
    active = {
        str(agent.get("actor"))
        for agent in state.get("active_agents", [])
        if isinstance(agent, dict) and agent.get("actor")
    }
    panes = _council_agent_panes()
    if not panes:
        raise RuntimeError(
            f"no agent panes configured; {COUNCIL_AGENT_PANES_ENV} is empty"
        )
    actor_by_target = _council_agent_pane_actors()
    missing_targets = [
        target
        for target in panes
        if actor_by_target.get(target) and actor_by_target[target] not in active
    ]
    if not missing_targets:
        return 0, []
    sent = send_init_prompt_to_agent_panes(room, panes=missing_targets)
    return sent, [actor_by_target[target] for target in missing_targets]


def send_init_prompt_to_agent_panes(
    room: str,
    panes: list[str] | None = None,
    *,
    enter_delay_s: float = DEFAULT_SEND_ENTER_DELAY_S,
) -> int:
    targets = panes if panes is not None else _council_agent_panes()
    if not targets:
        raise RuntimeError(
            f"no agent panes configured; {COUNCIL_AGENT_PANES_ENV} is empty"
        )
    actor_by_target = _council_agent_pane_actors()
    for target in targets:
        prompt = join_listen_prompt(room, actor=actor_by_target.get(target))
        subprocess.run(["tmux", "send-keys", "-t", target, "-l", prompt], check=True)
        if enter_delay_s > 0:
            time.sleep(enter_delay_s)
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)
    return len(targets)


def exit_council_session() -> bool:
    session = os.environ.get(COUNCIL_SESSION_ENV, "").strip()
    if not session:
        return False
    subprocess.run(["tmux", "kill-session", "-t", session], check=True)
    return True


def _init_needs_workdir() -> bool:
    return bool(os.environ.get(COUNCIL_CONFIG_ENV, "").strip()) and not bool(
        os.environ.get(COUNCIL_WORKDIR_ENV, "").strip()
    )


def launch_configured_agent_window(room: str, workdir: str) -> int:
    config = os.environ.get(COUNCIL_CONFIG_ENV, "").strip()
    session = os.environ.get(COUNCIL_SESSION_ENV, "").strip()
    window = os.environ.get(COUNCIL_AGENT_WINDOW_ENV, "agents").strip() or "agents"
    if not config:
        raise RuntimeError(f"{COUNCIL_CONFIG_ENV} is not set")
    if not session:
        raise RuntimeError(f"{COUNCIL_SESSION_ENV} is not set")
    try:
        listen_delay_s = float(os.environ.get(COUNCIL_LISTEN_DELAY_ENV, "8"))
    except ValueError:
        listen_delay_s = 8.0

    from warroom.channel.tmux_council import launch_agents_from_toml

    skipped, _written, _commands = launch_agents_from_toml(
        config,
        workdir=workdir,
        session_name=session,
        window_name=window,
        auto_listen=True,
        listen_delay_s=listen_delay_s,
        dry_run=False,
    )
    for item in skipped:
        print(f"[viewer] skipped: {item}")
    try:
        pane_count = refresh_agent_panes_from_tmux(session, window)
    except Exception as e:
        print(f"[viewer] could not record agent panes: {e}", file=sys.stderr)
    else:
        print(f"[viewer] recorded {pane_count} agent pane(s)")
    return len(_commands)


def print_help() -> None:
    print(
        "\n".join(
            [
                "[viewer] Agent Council local commands:",
                "  /init [workdir]        launch configured TUI agents, then inject the council bootstrap prompt",
                "  /inject <agent|all>    re-inject the bootstrap prompt into one agent pane or all panes",
                "  /inject-missing        re-inject only panes whose actor has not joined the room yet",
                "  /panes                 show agent alias -> tmux pane mapping used by /inject",
                "  /exit                  kill the council tmux session when available; otherwise exit viewer",
                "  /help                  show this help",
                "",
                "[viewer] Any other text is posted to the room as your message.",
                "[viewer] Local / commands are not broadcast to agents.",
            ]
        )
    )


def handle_viewer_command(text: str, room: str) -> str | None:
    if text == "/help":
        print_help()
        return "handled"
    if text == "/init" or text.startswith("/init "):
        workdir_arg = text[len("/init"):].strip()
        configured_workdir = os.environ.get(COUNCIL_WORKDIR_ENV, "").strip()
        config = os.environ.get(COUNCIL_CONFIG_ENV, "").strip()
        if config:
            workdir = workdir_arg or configured_workdir
            if not workdir:
                print("[viewer] /init needs a workdir")
                return "needs_workdir"
            try:
                launch_configured_agent_window(room, workdir)
            except Exception as e:
                print(f"[viewer] /init failed: {e}", file=sys.stderr)
            else:
                print(f"[viewer] launched configured agents in {workdir}")
            return "handled"
        try:
            count = send_init_prompt_to_agent_panes(room)
        except Exception as e:
            print(f"[viewer] /init failed: {e}", file=sys.stderr)
        else:
            print(f"[viewer] injected council bootstrap prompt into {count} agent pane(s)")
        return "handled"
    if text == "/panes":
        print_agent_panes()
        return "handled"
    if text == "/inject" or text.startswith("/inject "):
        selector = text[len("/inject"):].strip()
        if not selector:
            print("[viewer] usage: /inject <agent|all>")
            return "handled"
        try:
            panes = _select_agent_panes(selector)
            count = send_init_prompt_to_agent_panes(room, panes=panes)
        except Exception as e:
            print(f"[viewer] /inject failed: {e}", file=sys.stderr)
        else:
            print(f"[viewer] injected council bootstrap prompt into {count} agent pane(s)")
        return "handled"
    if text == "/exit":
        try:
            killed = exit_council_session()
        except Exception as e:
            print(f"[viewer] /exit failed: {e}", file=sys.stderr)
            return "handled"
        if killed:
            print("[viewer] killed council tmux session")
        else:
            print("[viewer] exiting viewer")
        return "exit"
    if text.startswith("/"):
        print(f"[viewer] unknown local command: {text}. Type /help")
        return "handled"
    return None


async def _printer(client: ChannelClient, room: str) -> None:
    """Background task: receive broadcasts and print them."""
    while True:
        try:
            msg = await client.wait_new(room, timeout_s=3600)
        except ConnectionError:
            print("\n[viewer] broker connection lost", file=sys.stderr)
            break
        if msg is None:
            continue
        # HIGH 3 fix: catch rendering errors so one bad message
        # doesn't kill the entire printer loop
        try:
            _format_msg(msg)
        except Exception as e:
            print(f"  [viewer] render error: {e}", file=sys.stderr)


async def run_viewer(broker_url: str, room: str) -> None:
    client = ChannelClient(broker_url, actor="user")
    await client.connect()
    try:
        await client.join(room)
    except ConnectionError as e:
        print(f"[viewer] join failed: {e}", file=sys.stderr)
        await client.close()
        return

    print(f"[viewer] joined {room} on {broker_url}. Type messages below. Ctrl+C to exit.\n")

    printer_task = asyncio.create_task(_printer(client, room))
    session: PromptSession[str] = PromptSession()

    try:
        with patch_stdout():
            while True:
                try:
                    text = await session.prompt_async("> ")
                except (KeyboardInterrupt, EOFError):
                    break
                text = text.strip()
                if not text:
                    continue
                if text == "/init" and _init_needs_workdir():
                    try:
                        workdir = (await session.prompt_async("workdir> ")).strip()
                    except (KeyboardInterrupt, EOFError):
                        continue
                    if not workdir:
                        print("[viewer] workdir is required")
                        continue
                    text = f"/init {workdir}"
                if text.startswith("/"):
                    if text == "/inject-missing":
                        try:
                            _count, actors = await send_bootstrap_prompt_to_missing_agent_panes(
                                client, room
                            )
                        except Exception as e:
                            print(f"[viewer] /inject-missing failed: {e}", file=sys.stderr)
                        else:
                            if actors:
                                print(
                                    "[viewer] injected council bootstrap prompt into missing agent(s): "
                                    + ", ".join(actors)
                                )
                            else:
                                print("[viewer] no missing agents found")
                        continue
                    command_result = handle_viewer_command(text, room)
                    if command_result == "exit":
                        break
                    if command_result is not None:
                        continue
                try:
                    await client.post(room, content=text)
                except ConnectionError:
                    print("\n[viewer] broker connection lost", file=sys.stderr)
                    break
    finally:
        printer_task.cancel()
        try:
            await printer_task
        except (asyncio.CancelledError, Exception):
            pass
        await client.close()
        print("\n[viewer] bye")


def main() -> None:
    parser = argparse.ArgumentParser(description="A2A channel viewer")
    parser.add_argument("--broker", default="ws://127.0.0.1:9100")
    parser.add_argument("--room", default="room1")
    args = parser.parse_args()
    try:
        asyncio.run(run_viewer(args.broker, args.room))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
