# Agent Council Evolution Plan

Date: 2026-04-29

This plan describes how to evolve the current Agent Council prototype into a council runtime with a substantially different user experience, not just a better chat relay.

## 1. Product Target

The target experience should feel like opening an AI war room:

```text
agent-council up council.yaml
```

Then the user sees one control deck:

- user prompt input
- live room timeline
- participant cards
- task board
- file claims
- permission/control events
- shared memory panel
- raw TUI panes or headless logs as debug surfaces

The user should not manually copy the same prompt into five CLIs. The system should launch available participants, tell the user which ones failed, inject the room instructions, and keep the whole council observable.

## 2. Current Prototype Boundary

The current code already has useful assets:

- WebSocket broker
- A2A-style messages
- MCP shim
- tmux launcher
- terminal viewer
- file claims
- task create/update/handoff/verdict tools
- control frames
- simple status reporting
- config generation for multiple CLIs

But the current architecture is still channel-first:

- messages are persistent, but tasks/status/claims are mostly process memory
- roster is a generated config artifact, not a live runtime object
- agent inbox delivery state is not first-class
- PM Agent has no durable state contract yet
- shared memory and compaction are not first-class
- model/reasoning config is not tied to applied runtime capability
- tmux panes are treated too much like the product, not just a backend

The next step should not be "improve chat". It should be "create the council runtime above the channel".

## 3. New Core Abstraction

Introduce `CouncilSession` as the product object.

A council session owns:

- roster
- room event log
- per-agent inbox
- task board
- file claims
- agent status
- control frames
- memory snapshots
- compaction records
- launcher state
- provider session references

The existing channel should become one transport for this runtime, not the runtime itself.

Recommended module boundary:

```text
warroom/council/models.py       typed domain objects
warroom/council/store.py        durable SQLite store
warroom/council/runtime.py      session lifecycle and orchestration
warroom/council/launcher.py     tmux/headless backend launcher
warroom/council/memory.py       shared memory and compaction
warroom/council/pm_prompt.py    PM Agent contract and prompts
warroom/council/manifest.py     per-agent room/capability manifest
```

Keep `warroom/channel` as the low-level WebSocket/MCP transport until the new runtime is stable.

## 4. Claude Code Team Ideas To Copy

Copy the organization model:

- canonical roster
- task board
- per-agent mailbox
- explicit send-message tool
- idle/status events
- control messages separate from normal chat
- visible backend abstraction
- long-term memory separate from live transcript

Do not copy Claude-specific implementation assumptions:

- third-party CLIs will not understand Claude's `--agent-id` or `--team-name`
- third-party CLIs will not poll Claude's mailbox
- third-party CLIs will not expose Claude's permission bridge
- third-party CLIs may not support model/reasoning selection at launch

Agent Council should emulate the team protocol externally through MCP, tmux injection, and later headless adapters.

## 5. Disruptive UX Features

### One-command council launch

The user writes a roster config once:

```yaml
room: repo-redesign
workspace: /path/to/repo
participants:
  - actor: codex-xhigh
    provider: codex
    role: implementer
    model:
      desired: gpt-5.5
    reasoning:
      desired:
        reasoning_effort: xhigh
    backend: tmux-visible

  - actor: gemini-flash
    provider: gemini
    role: fast-researcher
    model:
      desired: gemini-2.5-flash
    backend: tmux-visible

  - actor: pm
    provider: codex
    role: pm
    model:
      desired: gpt-5.4
    reasoning:
      desired:
        reasoning_effort: low
```

The launcher records both desired and applied settings. If a CLI cannot apply a model or reasoning setting, Agent Council should show `manual_required` or `unsupported`, not pretend it worked.

### Control deck instead of chat window

The main interface should show:

- who is online
- who is listening
- who is running
- who is blocked
- who owns each task
- which files are claimed
- what the PM Agent thinks should happen next
- what the shared memory currently says
- which model/settings were actually applied

Raw terminal output should be available, but not be the main information architecture.

### PM Agent as policy layer

PM Agent should not edit code by default. It should operate through explicit tools:

- create/update tasks
- assign owners/reviewers
- request handoff
- ask an agent to wait
- request compact
- request interrupt
- summarize state
- escalate to human

The PM Agent should receive compact state, not the full transcript.

### Inbox delivery instead of raw broadcast

Each agent should have a durable inbox:

```text
event -> delivery policy -> per-agent inbox -> delivered/read state
```

Default delivery rule:

- user messages go to PM and selected participants
- PM assignments go to target agents
- task handoffs go to reviewer and PM
- status events go to viewer and PM
- raw debate is not automatically injected into every agent

This is how Agent Council avoids fivefold token explosion.

### Session continuity

Resuming a council should not require remembering five provider session IDs manually.

Agent Council should store:

- council session id
- roster version
- provider session reference if available
- last delivered room sequence per agent
- memory snapshot version
- task board state
- active claims

If a provider session cannot resume, Agent Council starts a fresh one and bootstraps it from shared memory plus recent high-value events.

## 6. Runtime Phases

### Phase 1: Durable Council Runtime

Goal: make Agent Council stateful and recoverable.

Implement durable tables for:

- council_sessions
- council_agents
- room_events
- agent_inbox
- deliveries
- tasks
- task_events
- file_claims
- agent_status
- control_events
- memory_snapshots

Move current in-memory tasks/status/claims into the store.

### Phase 2: Roster-driven launcher

Goal: one config launches the whole room.

Implement:

- `agent-council init`
- `agent-council up`
- `agent-council attach`
- `agent-council down`
- actor ids independent from provider names
- multiple same-provider participants
- desired/applied model config
- skip unavailable agents without failing the whole council

### Phase 3: Control deck

Goal: replace the feeling of a chat relay with a command center.

Start with terminal UI if faster, then web UI.

Must show:

- roster and applied config
- status board
- task board
- room timeline
- control events
- claims
- memory
- raw pane/log drawer

### Phase 4: PM Agent

Goal: make coordination agentic but constrained.

Implement PM Agent manifest:

- capabilities
- forbidden actions
- authority level
- compact room state
- task board state
- context budget estimates
- escalation rules

Default authority:

```text
cooperative
```

No physical interrupt without human confirmation until the actuator is reliable.

### Phase 5: Shared memory and compaction

Goal: long council sessions without context collapse.

Implement:

- `shared_memory.md`
- structured `memory.json`
- manual compact
- PM-requested compact
- per-agent bootstrap summaries
- raw archive retained but not blindly injected

Memory should store decisions, assumptions, file facts, task outcomes, unresolved questions, and next actions.

### Phase 6: Headless adapters

Goal: web-first automation without losing the TUI debug path.

Adapter order:

1. Kimi wire protocol
2. OpenCode ACP/server
3. Codex app-server
4. Claude SDK
5. Gemini stream-json/resume path

Visible TUI remains the debug/operator backend.

## 7. Non-goals For The Next Step

Avoid:

- full autonomous infinite debate
- every-token cross-agent streaming
- complex relevance ranking
- pretending unsupported model settings worked
- parsing raw terminal text as the primary state source
- giving PM Agent direct write authority by default
- replacing the whole current channel stack before the new runtime proves itself

## 8. Recommended Next Slice

The next slice should be narrow but foundational:

```text
Durable CouncilSession + roster + inbox + taskboard
```

Concrete deliverable:

- new `warroom/council` package
- SQLite schema for council runtime state
- migration path from current channel DB
- config loader for `council.yaml`
- runtime state snapshot API
- viewer shows roster/tasks/status from durable state
- existing MCP tools write through the durable store

This slice changes the product foundation without trying to solve every provider adapter at once.

Once this exists, PM Agent and WebUI become natural additions instead of fragile prompt tricks around tmux.
