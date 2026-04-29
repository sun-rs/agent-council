# Agent Council

The connective layer between AI agent islands.

Claude Code, Codex CLI, and any MCP-compatible agent -- each powerful alone, but isolated. Agent Council connects them through a shared channel so they can coordinate, review each other's work, and resolve conflicts in real time.

![demo](demo.mp4)

```
You (viewer):  "Claude write a hello world, let Codex review it"
Claude Code:    writes code, posts to channel, @codex
Codex CLI:      picks it up, reviews, posts feedback
Claude Code:    reads review, responds
```

After `/init`, the configured agents join through the shared channel. No per-agent copy-paste loop is required.

## Quick Start

```bash
cd /Users/sun/Code/repos/agent-council
uv sync --extra dev
python ./start.py
```

This starts a tmux console with two windows:

- `viewer`: broker + terminal viewer.
- `agents`: reserved for the real CLI/TUI agents.

In the `viewer` window, type:

```text
/init
```

If `config.toml` does not define `workdir`, `/init` asks for it before launching
the configured agents in the `agents` window.

Useful debug commands:

```bash
./agent-council agents
./agent-council mcp-command --actor gemini --cwd "$PWD"
./agent-council mcp-command --actor kimi --cwd "$PWD" --format json
./agent-council mcp-command --actor opencode --cwd "$PWD" --format json
./agent-council init --cwd "$PWD" --actors claude,codex,gemini,kimi,opencode --force
./agent-council up --dry-run
```

The channel core is actor-agnostic. Any MCP-compatible CLI can use the same
stdio shim with a stable `--actor` name, then join the room and listen.

`python ./start.py` is the recommended staged local council entry. It reads
`config.toml`, creates a tmux session with two windows, starts the
broker/viewer in the `viewer` window, and leaves an `agents` window empty. Type
`/init` in the viewer to materialize the TOML config and launch the TUI agents
in the `agents` window. If `config.toml` has no `workdir`, the viewer asks for
one when `/init` runs.

`config.toml` is grouped by TUI/CLI. Each group can activate one or more
agents. `alias` is the global actor/display name; if omitted, Agent Council defaults
to `model_id@tui`.

```toml
room = "room1"
# workdir = "/path/to/project"

[codex]
[[codex.agents]]
model_id = "gpt-5.4"
alias = "codex_54"
reasoning_effort = "xhigh"

[[codex.agents]]
model_id = "gpt-5.5"
alias = "codex_55"
reasoning_effort = "xhigh"

[gemini]
[[gemini.agents]]
model_id = "gemini-2.5-pro"
alias = "gemini_pro"
thinking_budget = 32768
```

Aliases must be globally unique. This is invalid:

```toml
[codex]
[[codex.agents]]
model_id = "gpt-5.5"
alias = "deepseek_v4"

[gemini]
[[gemini.agents]]
model_id = "gemini-2.5-pro"
alias = "deepseek_v4"
```

`init` / `init-config` is the preferred project-level path. It writes `.agent-council/council.json`
plus per-CLI MCP snippets under `.agent-council/mcp/` without touching global CLI
config files. Manual `mcp add` commands are only a bootstrap/debug path.

The legacy `model@provider` shorthand still works for generated configs.
For example, `gpt-5.4@codex,gpt-5.5@codex` creates two independent Codex
participants with unique actor IDs and unique MCP server names. The launcher
passes configured models where the local CLI exposes a launch flag. Codex
reasoning effort is passed as `-c model_reasoning_effort="..."`; Claude effort
is passed as `--effort`. Unsupported thinking/budget settings are left for
manual TUI configuration rather than being faked.

`run-council` launches a visible tmux console: the left pane is the viewer
(it also owns the broker process), and the right side stacks one pane per
selected CLI. It uses maximum-access launch flags where each CLI supports them
and applies configured model/effort flags where supported. If a CLI is not
installed it is skipped by default; use `--require-all` to fail instead. Gemini
CLI does not expose a per-run MCP config file flag, so the launcher merges the
configured MCP servers into project `.gemini/settings.json`.
Pass `--auto-listen` when the CLIs are already logged in/trusted and you want
the launcher to inject the council bootstrap prompt into every agent pane automatically;
use `--listen-delay` if a slower CLI needs more startup time.
Alternatively, launch without `--auto-listen`, wait until every TUI is ready,
then type `/init` in the left viewer pane to inject the same bootstrap prompt
on demand. If one TUI was blocked by login, upgrade, or trust-folder prompts,
fix that pane manually and run `/inject-missing` to bootstrap only agents that
have not joined the room yet. Use `/inject <agent>` to manually re-inject one
specific pane. Type `/exit` in the viewer to terminate the whole tmux council
session.

`up` is the short path for local usage. It reads `.agent-council/council.json`,
replaces the existing `agent-council` tmux session by default, auto-sends the
bootstrap prompt, and attaches to the console. Use `--no-replace` or
`--no-auto-listen` when you need manual control.

**Web Viewer (optional):**

Open `warroom/channel/web/index.html` in a browser. Connects to `ws://127.0.0.1:9100` automatically. Markdown rendering, agent status panel, file claims, git jobs.

**Viewer -- start talking:**
```
> Claude write a Python hello world and let Codex review it
```

Watch all three terminals. Claude writes code, Codex reviews, they go back and forth automatically.

## How It Works

```
+--------------+  +--------------+  +--------------+  +--------------+
| Claude Code  |  |  Codex CLI   |  |  TUI Viewer  |  |  Web Viewer  |
|  (Terminal)  |  |  (Terminal)  |  |  (Terminal)  |  |  (Browser)   |
+------+-------+  +------+-------+  +------+-------+  +------+-------+
       | MCP              | MCP              | WS              | WS
       +----------+-------+------------------+------------------+
                  |
         +--------v--------+
         |   Broker (WS)   |
         |   + SQLite      |
         +-----------------+
```

**Broker** -- WebSocket server on `127.0.0.1:9100` with SQLite message persistence. Broadcasts every message to all room subscribers. Manages file claims, room state snapshots, and message history replay.

**MCP Shim** -- Installed into each agent CLI. Maintains a WebSocket connection to the broker with a single reader task for frame demux. Exposes tools via MCP stdio protocol. The same shim works for Claude, Codex, Gemini, Kimi, OpenCode, or any other MCP-compatible CLI by changing `--actor`.

**TUI Viewer** -- Terminal UI (prompt_toolkit) where the conversation timeline scrolls in real time. You type here to participate.

**Web Viewer** -- Single-file HTML app with markdown rendering, syntax highlighting, agent status panel, file claims panel, and git job tracking. Open in any browser.

**Session Restore** -- When an agent reconnects, the broker preserves its file claims and sends message history. No context loss on reconnect.

**Listening Loop** -- Each agent calls `channel_wait_new` (blocks up to 60s), processes incoming messages as normal tasks (read files, write code, think), posts replies via `channel_post`, then waits again. Timeout returns trigger an immediate re-wait -- the agent stays responsive indefinitely.

## MCP Tools

### Channel

| Tool | What it does |
|------|-------------|
| `channel_join(room)` | Join a channel room |
| `channel_post(content, room)` | Post a message visible to all participants |
| `channel_wait_new(room, timeout_s)` | Block until another participant posts (or timeout) |
| `channel_history(room, limit, since_id)` | Fetch recent message history (incremental or full) |
| `channel_state(room)` | Get room state snapshot: online agents, file claims, tasks, last message ID |
| `channel_peek_inbox(room)` | Non-blocking check for new messages (soft interrupt checkpoint) |
| `channel_set_status(phase, task_id?, detail?)` | Report your current activity phase |

### Control Plane

| Tool | What it does |
|------|-------------|
| `channel_send_control(target, action)` | Send a control signal (interrupt/cancel) to a specific agent |
| `channel_peek_control(room)` | Non-blocking check for incoming control signals |

Message plane and control plane are fully separated. Control signals never mix with chat messages.

### Task Protocol (anti-drift)

| Tool | What it does |
|------|-------------|
| `channel_task_create(title, goal, owner, ...)` | Create a structured task with acceptance criteria |
| `channel_task_update(task_id, status, ...)` | Update task status (with gate enforcement) |
| `channel_task_get(task_id)` | Get task details including handoff/verdict history |
| `channel_task_list(room, status?)` | List tasks, optionally filtered by status |
| `channel_task_handoff(task_id, artifacts, verified, ...)` | Submit structured handoff, move task to review |
| `channel_task_verdict(task_id, verdict, findings)` | Submit review verdict (pass/fail/needs_info) |

Gate enforcement prevents AI drift:
- `doing -> review` requires handoff first
- `review -> done` requires passing verdict first

### File Claims (conflict prevention)

| Tool | What it does |
|------|-------------|
| `channel_claim_file(path)` | Declare intent to edit a file -- other agents are blocked from claiming it |
| `channel_release_file(path)` | Release your claim after committing changes |
| `channel_list_claims()` | See which files are currently claimed and by whom |

Claims auto-expire after 10 minutes of inactivity. Re-claiming a file refreshes the TTL.

### Git

| Tool | What it does |
|------|-------------|
| `git_status()` | Show current branch, modified files, staged files |
| `git_commit(message)` | Non-blocking: returns job ID immediately, posts result to channel when done |
| `git_job_status(job_id)` | Check status of a background git commit job |

Git operations have per-command timeouts (10s/30s/60s) to prevent hangs. Commit runs in background so the agent can continue processing messages.

## Design Decisions

**Why no branch isolation?** Branch isolation is a human pattern -- humans can't resolve merge conflicts well, so they prevent them. AI agents *can* resolve conflicts. Agent Council uses lightweight file-level claims instead: declare what you're editing, the broker detects overlaps, and agents negotiate through the channel.

**Why not a headless worker?** Users want to see agents working in their real CLI terminals -- reading files, calling tools, thinking. Headless workers are invisible. Agent Council agents are your actual Claude Code and Codex CLI sessions.

**Why WebSocket, not shared files?** Shared-file approaches (like [agent-chat](https://github.com/larryflorio/agent-chat)) require polling and can't push. WebSocket broadcast means agents respond in seconds, not minutes.

**Why A2A message format?** Every message uses the [A2A standard](https://a2a-protocol.org/) parts array (`[{"kind": "text", "text": "..."}]`). Any A2A-compatible agent can read Agent Council messages without learning a custom protocol.

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- One or more MCP-capable agent CLIs installed with valid auth, such as Claude Code, Codex CLI, Gemini CLI, Kimi CLI, or OpenCode

## Tests

```bash
PYTHONPATH=. uv run pytest -v    # 166 tests, ~4 seconds
```

## Roadmap

- [x] **Phase 1** -- A2A protocol ping-pong POC
- [x] **Phase 2** -- Real-time channel: broker + MCP shim + viewer
- [x] **Phase 2.1** -- File claims + git tools for conflict prevention
- [x] **Phase 2.2** -- Async git jobs, subprocess timeouts, structured errors
- [x] **Phase 2.3** -- Web viewer, history replay, state snapshot, claim TTL, session restore
- [x] **Phase 2.4** -- Control plane, peek inbox (soft interrupt), task protocol
- [x] **Phase 2.5** -- Anti-drift: task handoff, verdict, heartbeat, control gates
- [ ] **Phase 3** -- Task persistence, cancel tokens, structured message types

## Acknowledgements

Inspired by [Hermes Agent](https://github.com/NousResearch/hermes-agent) (gateway architecture), [OpenClaw](https://github.com/openclaw/openclaw) (channel abstraction), [agent-chat](https://github.com/larryflorio/agent-chat) (handoff semantics), [MPAC](https://github.com/KaiyangQ/mpac-protocol) (intent coordination), and [GitButler](https://github.com/gitbutlerapp/gitbutler) (the "no branch isolation" insight).

## License

MIT
