# Agent Council Runtime Design

Date: 2026-04-29

This document records the current design discussion for the next Agent Council shared-memory runtime. The central question is whether Agent Council should keep the existing MCP `listen` model, where each CLI agent blocks on a channel tool, or move toward a RAH-style headless session model controlled by a central council controller.

## 1. Goal

Agent Council should become an agent council system, not just a chatbot relay.

The target behavior:

- A user asks one question in a shared room.
- Multiple agents from different providers can participate: Claude, Codex, Gemini, Kimi, OpenCode, and later others.
- Agents know they are in a council room.
- Agents can see relevant prior statements from other agents.
- Agents can read files, write code, run commands, search, ask for permissions, and report tool progress.
- The user can observe each agent's status and output.
- The system can preserve and compact shared memory across long discussions.
- The runtime should not depend on manually operating five TUI windows in production.

The difficult part is not starting five CLIs. The difficult part is shared context, scheduling, synchronization, observability, permissions, context budget, and recovery.

## 2. Terms

### Room

The canonical shared event log for one council topic.

Every user message, agent final answer, selected milestone, permission event, and shared memory update is appended to the room with a monotonically increasing sequence number.

### Agent

A logical council participant such as `codex-xhigh`, `claude-reviewer`, or `gemini-researcher`.

An agent has:

- provider
- model
- reasoning/thinking config
- workspace
- permission policy
- provider session id
- last seen room sequence
- private memory
- status

### Provider session

The actual provider-side conversation/thread/session. This is not necessarily a visible TUI.

Examples:

- Codex `app-server` thread
- Claude SDK live/resume session
- Kimi `--wire` session
- OpenCode session through `serve`/ACP
- Gemini noninteractive `stream-json` turn plus resume state

### Controller

The Agent Council process that owns the room, agent status, delivery policy, scheduling, memory, and front-end event stream.

### Blocking listen

The current MCP channel style where an agent calls something like `channel_wait_new(room="room1")` and stays blocked until a message arrives.

## 3. RAH Source Findings

RAH's WebUI sessions are mostly not native TUI sessions. RAH uses a runtime daemon plus provider adapters.

Important architectural observations:

- RAH has a `ProviderAdapter` abstraction for starting sessions, resuming sessions, sending input, closing sessions, permissions, model lists, and model switching.
- RAH normalizes provider events into structured events such as assistant messages, tool calls, observations, permissions, runtime status, and usage.
- RAH's front-end displays normalized activity cards, not raw terminal screens by default.
- RAH still supports terminal wrappers, but those are a separate path. They are useful for attaching to or controlling terminal sessions, not the main WebUI session path.

Provider-specific strategies seen in RAH:

- Claude uses the Anthropic Claude agent SDK in headless query mode.
- Codex starts `codex app-server` and talks through JSON-RPC.
- Gemini uses noninteractive `gemini --output-format stream-json --prompt ...` per turn, with resume/session handling.
- Kimi starts `kimi --wire` and talks through JSON-RPC.
- OpenCode starts `opencode serve` and `opencode acp`.

This proves that a no-visible-TUI runtime is feasible, but it also shows that each provider has a different transport shape. Agent Council should not force every provider into one fake universal transport.

## 3.1 Claude Code Agent Team Source Findings

Claude Code's Agent Team is useful as an organization model, but not as a directly reusable multi-provider runtime.

The important design is not "many model APIs under one scheduler". It is a team operating system:

- team roster
- shared task list
- per-agent mailbox
- explicit send-message tool
- idle/status notifications
- separated control messages
- backend abstraction for visible panes or in-process agents
- optional durable team memory sync

The strongest lesson is that Claude Code does not broadcast every teammate's raw transcript to every other teammate. It shares structured state and explicit messages. This is the correct direction for Agent Council because it reduces context explosion, lowers synchronization ambiguity, and makes recovery possible.

Portable ideas Agent Council should copy:

- Use a canonical roster similar to Claude Code's team file. Each council member should have a stable actor id, display name, provider, CLI, model, desired reasoning config, applied launch config, cwd, worktree, backend, pane/session id, permission mode, and active status.
- Use a shared task board instead of relying on free-form chat. Tasks need owner, status, dependencies, claims, handoff state, and acceptance criteria.
- Use a mailbox/event-log layer instead of terminal text scraping as the coordination primitive. Messages should have sender, target, sequence number, type, summary, delivered/read state, and optional control metadata.
- Deliver messages at safe boundaries. Claude Code queues mailbox messages while a teammate is busy and injects them when the teammate is idle. Agent Council should do the same where possible instead of forcing every running CLI to absorb every event immediately.
- Separate control plane messages from normal chat. Permission requests, mode changes, compact requests, shutdown, pause, interrupt, and resume must not be normal room text.
- Treat visible terminal panes as an operator/debug backend, not as the only state store. The durable source of truth should be Agent Council's room event log and roster.
- Keep long-term shared memory separate from the live transcript. Shared memory should be compacted decisions, task state, files touched, unresolved questions, and durable lessons, not the whole debate.

Non-portable parts:

- Claude Code can launch Claude Code teammates with internal flags such as agent id, team name, model, parent session, and permission mode. Other CLIs will not understand these flags.
- Claude Code teammates natively poll Claude Code's mailbox. Codex, Gemini, Kimi, and OpenCode will not do that unless Agent Council exposes equivalent behavior through MCP listen, headless adapters, or terminal injection.
- Claude Code's in-process teammate runner is Claude-specific. It is not a general multi-provider primitive.
- Claude Code's permission bridge is deeply integrated with Claude Code UI and tool confirmation internals. Agent Council needs its own provider-neutral permission/control protocol.

Agent Council should therefore copy the organization model, not the implementation coupling:

```text
Claude Code Team concept      -> Agent Council equivalent
team file                     -> council roster
task list                     -> task board
teammate mailbox              -> room event log + per-agent inbox
SendMessage tool              -> channel_post / channel_send
inbox poller                  -> delivery scheduler
idle notification             -> agent status event
permission bridge             -> provider-neutral control plane
team memory sync              -> shared memory + compacted room digest
tmux/iTerm/in-process backend -> tmux-visible / headless / web-pty backend
```

This means the near-term Agent Council runtime can remain CLI-first, but it should stop treating "five CLIs in five panes" as the architecture. The architecture should be the council protocol. Tmux panes are only one backend for running participants and observing failures.

## 4. Candidate Runtime Models

### Option A: TUI plus MCP blocking listen

Each CLI/TUI is launched, configured with the same Agent Council MCP server, then instructed to join a room and repeatedly call a blocking listen tool.

Flow:

```text
User -> Agent Council room
Agent TUI -> MCP channel_wait_new blocks
Message arrives -> TUI wakes -> model responds -> posts back through MCP
```

Benefits:

- Very close to the original Agent Council idea.
- Agents feel like they are physically present in a shared room.
- Easy to debug visually when TUI panes are visible.
- MCP gives one provider-neutral way to expose room tools.
- Agents can theoretically decide to wait, respond, ignore, or coordinate by themselves.

Costs:

- Requires multiple TUI windows or terminal processes.
- Fragile around focus, injected prompts, Enter behavior, terminal size, upgrades, trust-folder prompts, login prompts, and provider-specific TUI changes.
- A blocking `listen` call is still a tool call inside one model turn. If it blocks forever, the agent is not truly idle; it is stuck inside a provider/tool execution path.
- Different CLIs have different timeouts, cancellation behavior, tool-call semantics, and resume behavior.
- Gemini-style per-turn noninteractive CLIs are a poor fit for long-lived blocking listen.
- If an agent is already generating, reading files, or running commands, it usually cannot naturally absorb new room messages mid-generation.
- The system can easily create runaway loops: one agent posts, all agents wake, each posts, all agents wake again.
- Context usage is harder to control because room traffic may be repeatedly injected by the agent itself.
- Hard to implement reliable global scheduling, quorum, throttling, and cancellation because the control plane is distributed across CLIs.

Best use:

- Debug mode.
- Experimental "native council room" mode.
- Provider behavior inspection.
- Manual demos.

Not recommended as production main path.

### Option B: Headless persistent sessions plus controller-mediated room

Agent Council starts or resumes one headless provider session per agent. The Controller owns the room event log and schedules turns. Agents do not block on MCP listen. They receive room deltas when the Controller decides they should act.

Flow:

```text
User -> room event seq=N
Controller selects agents
Controller builds per-agent input from:
  - room snapshot
  - deltas since agent.last_seen_seq
  - shared memory
  - current agent statuses
  - task instruction
Controller sends input to each provider session
Provider streams response/tool events
Controller writes selected outputs back to room
```

Benefits:

- No need to manually operate multiple TUI windows.
- Provider sessions can still be persistent; this is not stateless one-shot prompting.
- Controller has a single source of truth for room state, agent state, permissions, timeouts, and memory.
- Easy to show status in WebUI: idle, running, waiting permission, failed, completed, stale, interrupted.
- Easier to implement context budget management because each delivery is explicit.
- Easier to recover and resume because `agent_id -> provider_session_id -> last_seen_seq` is stored centrally.
- Easier to support multiple provider transports.
- Easier to avoid runaway broadcast loops.
- Fits RAH's proven adapter/event-normalization model.

Costs:

- More runtime logic must be implemented in Agent Council.
- Agents do not literally self-listen; the Controller simulates subscription by delivering room deltas.
- If implemented naively as "wait for everyone, then forward results", latency can be high.
- Requires careful scheduling policy to preserve council feel.
- Requires prompt design so agents understand room state, other agents' status, and their role.

Best use:

- Production main path.
- WebUI-first council runtime.
- Long-running shared-memory discussions.
- Multi-provider orchestration.

Recommended as the primary architecture.

### Option C: Full barrier rounds

This is a simple version of Option B. User asks a question; all selected agents run; Agent Council waits until all finish or timeout; then forwards the completed outputs to everyone in the next round.

Flow:

```text
Round 1:
  all agents read seq<=10
  all agents answer
Barrier:
  wait for all or timeout
Round 2:
  all agents read other answers
```

Benefits:

- Simple to reason about.
- No mid-round synchronization complexity.
- Each agent's answer has a clear snapshot boundary.
- Easy to implement first.

Costs:

- High latency if one provider is slow.
- Feels less like a live meeting.
- Fast agents wait for slow agents.
- User may not get useful synthesis until the barrier completes.
- Not ideal for coding tasks where one agent may finish a useful file read quickly and another agent could immediately review it.

Best use:

- Initial MVP.
- Deliberation mode.
- Formal review mode.
- Cases where consistency matters more than speed.

Not sufficient as the only long-term mode.

### Option D: Event-driven controller with virtual listen

This is a possible refinement of Option B, but it must not become a large hardcoded "AI manager" made of brittle backend rules.

The useful part of this option is the durable control plane: room sequence numbers, agent status, permissions, timeouts, interrupts, and recoverability. The risky part is trying to encode high-level meeting judgment in dead code.

Agents do not call blocking MCP listen. Instead, Agent Council gives each agent a virtual subscription:

- `last_seen_seq`
- `interests`
- `dependencies`
- `status`
- `delivery_policy`

The Controller wakes idle agents when useful events arrive, but it does not force running agents to absorb every message immediately.

Flow:

```text
Agent A completes -> room seq=21
Controller checks subscribers:
  Agent B idle and interested -> deliver seq 21 now
  Agent C running -> defer seq 21 until C finishes
  Agent D waiting for A -> wake D
  Agent E not relevant -> skip
```

Benefits:

- Keeps the good part of listen: agents can react to new events.
- Avoids the bad part of blocking listen: provider tool calls do not sit open forever.
- Allows low latency without full all-agent barriers.
- Lets Agent Council expose other agents' statuses explicitly in each prompt.
- Supports debounce, quorum, interrupt, dependency waits, and manual routing.

Costs:

- Requires a real scheduler.
- Requires event relevance and delivery policy.
- Requires careful UI so the user understands why an agent was or was not woken.
- Can become overengineered if it tries to decide all high-level discussion policy with fixed rules.
- Risks losing the human-in-the-loop flexibility that makes TUI-based agent work practical.

Best use:

- Reliable substrate for automation.
- Durable event log, status tracking, interrupts, permission handling, and replay.

Recommended as the control-plane substrate, not necessarily as the sole high-level scheduler.

### Option E: Hybrid with debug/HITL TUI

Use a structured Controller for durability, but keep TUI/tmux council as a first-class human intervention console, not merely an afterthought.

Flow:

```text
Production:
  WebUI -> Agent Council Controller -> headless adapters

HITL / Debug:
  tmux -> native CLIs -> MCP listen
```

Benefits:

- Practical migration path.
- Keeps visibility into native provider behavior.
- Useful when an adapter breaks because provider CLI behavior changed.
- Useful for inspecting login, trust-folder, upgrade, and permission prompts.

Costs:

- Two paths to maintain.
- If the TUI path becomes required for all production use, remote/headless automation remains limited.

Recommended as a support and human-override mode.

### Option F: TUI plus MCP listen plus AI PM Agent

This option keeps the existing MCP blocking-listen room as the primary coordination surface, but introduces a dedicated AI scheduler, also called PM Agent, Router Agent, or Scrum Master Agent.

The PM Agent is not a coding agent. It is a room manager.

Flow:

```text
User posts task
Worker agents listen in room
PM Agent listens in room
PM Agent assigns work through @mentions and task tools
Worker agents only respond to PM/user-directed messages
PM Agent watches progress and sends control signals when needed
Human can interrupt through TUI/viewer at any time
```

Existing Agent Council tools already point in this direction:

- `channel_send_control(target, action)` sends interrupt/cancel style control signals.
- `channel_peek_control(room)` separates control frames from chat messages.
- `channel_task_create(...)` creates structured tasks.
- `channel_task_handoff(...)` forces structured handoff and review.
- File claim tools reduce multi-agent write conflicts.

Current caveat:

`channel_send_control` provides a separated control frame path. That is necessary but not sufficient for physical interruption. To truly stop a runaway TUI, the tmux/TUI integration must consume the control frame and send the appropriate key sequence, such as Ctrl+C or an escape/cancel command, to the target pane. This should be treated as a hard engineering requirement before PM Agent authority is trusted.

Benefits:

- Preserves the strongest property of the current system: human-in-the-loop physical control.
- The user can directly enter a TUI pane and press Ctrl+C, type corrective instructions, or recover from provider-specific UI prompts.
- High-level scheduling is handled by an LLM instead of a large pile of brittle Python rules.
- PM Agent can reason over messy natural-language outputs, not just structured events.
- Better match for agentic work, where exceptions and ambiguous situations are normal.
- Compatible with current MCP/channel assets.
- Easy to degrade from "AI PM controls the room" to "human controls the room".

Costs:

- The PM Agent can make bad decisions.
- The PM Agent can become another source of loops or noise unless tightly constrained.
- The system still depends on each CLI's ability to remain in the listen loop.
- Blocking listen still inherits provider timeout, tool-call, and TUI fragility.
- Observability is strong for humans watching tmux, but weaker for automated WebUI unless terminal output is captured and normalized.
- Full remote automation remains harder than with headless adapters.

Important design constraint:

The PM Agent should not be given unlimited authority. It should operate through a small set of explicit tools:

```text
channel_task_create
channel_task_handoff
channel_send_control
channel_post
channel_claim_file / channel_release_file
```

It should not casually perform code edits itself. Its job is to assign, interrupt, wait, summarize, and ask for human help.

Recommended PM Agent behavior:

- Keep outputs short.
- Prefer explicit @mentions.
- Avoid starting multiple agents on the same file without claims.
- Use task handoff instead of free-form "I am done" messages.
- Interrupt only when there is clear drift, contradiction, timeout, or user instruction.
- Escalate to the human when stuck.
- Keep a visible room status summary.

Best use:

- Near-term Agent Council direction.
- Local workstation council.
- Coding sessions where human override matters.
- Situations where provider CLIs are changing faster than adapters can stabilize.

Not enough by itself:

- Durable replay.
- Provider-independent structured event history.
- WebUI-only usage.
- Fully unattended automation.

Recommended as the near-term HITL-first architecture.

## 4.1 Control Plane vs Policy Plane

The sharper distinction is not "dead code versus AI". Agent Council needs both, but for different jobs.

Dead code should own the control plane:

- WebSocket framing
- room event persistence
- message sequence numbers
- actor identity
- control frame delivery
- file claims
- task state
- permission records
- process lifecycle
- timeouts
- audit logs
- crash recovery

AI should own the policy plane:

- who should work next
- whether an answer is drifting
- whether one agent should wait for another
- whether a contradiction needs debate
- when to ask a human
- how to summarize messy progress
- how to route work between specialized agents

This avoids both extremes:

- Too much dead code creates a brittle scheduler that cannot handle agent ambiguity.
- Too much AI without a hard control plane creates a chaotic chatroom with no guarantees.

The practical architecture is:

```text
hard control plane + soft AI PM policy
```

## 4.2 PM Agent Capability Contract

The PM Agent should understand the council channel, not tmux.

It should receive a compact capability and state manifest at startup and during periodic status turns:

```yaml
pm_capabilities:
  can_post: true
  can_create_task: true
  can_update_task: true
  can_request_handoff: true
  can_send_soft_control: true
  can_request_physical_interrupt: false
  can_compact_request: true
  can_start_agent: false
  can_edit_files: false

room:
  id: room1
  last_seq: 182
  compacted_up_to_seq: 150
  shared_memory_version: 7

agents:
  - actor: codex-xhigh
    role: implementer
    provider: codex
    model: gpt-5.5
    phase: coding
    task_id: t-004
    last_seen_seq: 170
    context:
      remaining_pct: 18
      confidence: estimated
    controls:
      soft_interrupt: true
      physical_interrupt: human_confirm

  - actor: gemini-flash
    role: fast-researcher
    provider: gemini
    model: gemini-2.5-flash
    phase: idle
    context:
      remaining_pct: unknown
      confidence: unknown
```

The PM Agent should make policy decisions from this state:

- ask an agent to stop and hand off
- ask a reviewer to inspect a handoff
- request room compaction
- request a fresh session for an agent whose context is polluted or nearly full
- escalate to the human when the action is risky

The PM Agent should not read full transcript by default. Its normal input should contain:

- room state
- active tasks
- agent statuses
- file claims
- context budget report
- recent high-value events
- shared memory digest
- alerts generated by Agent Council

It should not receive:

- raw token deltas
- full stdout logs
- full historical debate
- full file contents
- raw tmux pane output unless explicitly requested for debugging

This keeps PM Agent small-context and durable. If PM Agent becomes the largest transcript reader, it will fail before the worker agents.

### PM Agent Authority Levels

PM Agent authority should be staged:

```text
observer:
  summarize state, suggest actions, no control calls

cooperative:
  create/update tasks, @mention agents, send soft control frames

operator:
  request physical interruption through a Agent Council actuator

autonomous:
  limited physical actions without confirmation, only after trust is proven
```

Recommended default for MVP:

```text
observer or cooperative
```

Physical control should require human confirmation until the actuator is reliable and PM behavior is predictable.

## 5. The Synchronization Problem

The core concern is valid:

If five agents are working at different speeds, when should their outputs be sent to each other?

Bad solution:

```text
Every token delta from every agent is broadcast immediately to every other agent.
```

This is noisy, expensive, unstable, and often impossible because running agents cannot reliably absorb new context mid-generation.

Also bad as the only strategy:

```text
Wait for all agents to finish, then forward all outputs.
```

This is simple but can have high latency.

Recommended solution:

```text
Use explicit consistency boundaries.
```

An agent turn should be based on a clear room snapshot:

```text
agent_turn.input_snapshot_seq = 42
agent response is valid for room state up to seq 42
```

If new events arrive while the agent is running, the Controller can choose one of four actions:

- Defer: deliver new events after the agent finishes.
- Ignore: do not deliver if irrelevant.
- Interrupt: stop the agent and restart with the new critical context.
- Side-channel status: show the user and room that new information exists, but do not mutate the running turn.

This is similar to database isolation. We should prefer clear snapshot boundaries over fake realtime consistency.

## 6. Event Delivery Policy

Agent Council should not treat all events equally.

### Default events written to the room

- User message
- Agent final answer
- Agent milestone summary
- Important tool result summary
- Permission requested/resolved
- Shared memory update
- Controller decision
- Error/failure/timeout

### Events shown in UI but not always delivered to other agents

- Token deltas
- Raw command stdout
- Raw MCP JSON-RPC frames
- Verbose debug logs
- Provider heartbeat
- Repeated progress updates

### Events normally not delivered to other agents

- Full raw terminal output
- Full raw MCP protocol frames
- Provider startup noise
- Login/account/rate limit notifications unless they affect the task

### Milestone examples

For long tasks, an agent can publish milestones:

```text
plan_created
file_scan_completed
tool_started
tool_completed
intermediate_finding
final_answer
```

Other agents can react to milestones without waiting for every provider to fully finish.

## 7. Scheduler Modes

Agent Council should support multiple scheduling policies instead of hardcoding one behavior.

### Fast mode

Trigger next useful agent as soon as one relevant result arrives.

Use when:

- User wants speed.
- One strong agent can start reviewing another's result immediately.
- The council is doing coding/debugging.

Risk:

- Some agents may respond before seeing slower agents' work.

### Balanced mode

Wait for a quorum, such as 2 of 5 or 3 of 5 agents, then continue.

Use when:

- User wants a tradeoff between latency and deliberation.
- Agents have similar roles.

Risk:

- Need clear UI explaining that not all agents have responded yet.

### Deliberation mode

Wait for all required agents or timeout.

Use when:

- Formal debate.
- Architecture decision.
- High-stakes review.

Risk:

- Slowest provider dominates latency.

### Manual mode

User explicitly routes:

```text
Ask Codex to respond to Claude.
Ask Gemini to summarize Kimi and OpenCode.
Ask only OpenCode to implement.
```

Use when:

- User wants direct control.
- The topic is complex.

Risk:

- More user involvement.

## 8. Agent State Model

Each agent should have central state:

```yaml
agent_id: codex-xhigh
provider: codex
model: gpt-5.5
reasoning:
  reasoning_effort: xhigh
workspace: /path/to/repo
provider_session_id: thread_abc
status: running
last_seen_seq: 18
last_delivered_seq: 21
current_turn:
  id: turn_123
  input_snapshot_seq: 21
  started_at: 2026-04-29T12:00:00Z
dependencies:
  waiting_for: []
capabilities:
  interrupt: true
  permissions: true
  tool_events: true
```

Important statuses:

- `idle`
- `running`
- `waiting_permission`
- `waiting_input`
- `waiting_dependency`
- `failed`
- `stale`
- `offline`
- `completed`

Agents should be told the visible statuses of other agents in their prompt.

Example injected state:

```text
Council state:
- Claude: running, reading source files, started 48s ago
- Codex: completed at seq=18
- Gemini: waiting_permission for file read
- Kimi: idle, last_seen_seq=14
- OpenCode: running tests
```

This addresses the concern that without shared MCP listen, agents do not know each other's status. They can know it because Agent Council injects the state from the canonical room/controller.

## 8.1 Agent Roster and Launch Profiles

The council must not treat `actor`, `provider`, and `model` as the same thing.

Current Agent Council config is too rigid:

- `actor=gemini` implies the Gemini CLI profile.
- known actors are deduped by name.
- model selection is currently `manual-in-tui`.
- multiple participants from the same CLI are awkward because a custom actor has no launch profile.

The new design should separate identity from implementation:

```text
actor:
  unique room identity, e.g. gemini-flash, gemini-pro, codex-reviewer

provider:
  model/provider family, e.g. gemini, codex, claude, kimi, opencode

cli:
  local executable/runtime, e.g. gemini, codex, claude

role:
  council role, e.g. pm, implementer, reviewer, researcher, summarizer

model:
  concrete model id or manual/default selection policy

reasoning:
  provider/model-specific thinking settings
```

This allows:

```yaml
agents:
  - actor: pm
    role: pm
    provider: codex
    cli: codex
    model:
      mode: configured
      id: gpt-5.4-mini
    reasoning:
      reasoning_effort: low

  - actor: gemini-flash
    role: fast-researcher
    provider: gemini
    cli: gemini
    model:
      mode: configured
      id: gemini-2.5-flash

  - actor: gemini-pro
    role: deep-reviewer
    provider: gemini
    cli: gemini
    model:
      mode: configured-best-effort
      id: gemini-3.1-pro-preview
    reasoning:
      thinking_level: high
    notes:
      - Current Gemini TUI exposes --model, but not a launch flag for thinking_level.
      - The launcher should warn that thinking_level requires manual TUI configuration or a future adapter.

  - actor: codex-xhigh
    role: implementer
    provider: codex
    cli: codex
    model:
      mode: configured
      id: gpt-5.5
    reasoning:
      reasoning_effort: xhigh

  - actor: claude-manual
    role: architect
    provider: claude
    cli: claude
    model:
      mode: manual-in-tui
```

### Model Selection Modes

Each participant should choose one of these modes:

```yaml
model:
  mode: configured
  id: gpt-5.5
```

Agent Council must pass the model/reasoning flags when launching the CLI. If the adapter does not know how to apply the requested setting, launch should fail clearly unless the agent explicitly allows fallback.

```yaml
model:
  mode: configured-best-effort
  id: gemini-2.5-pro
```

Agent Council tries to pass the model. If unsupported, it warns and launches in manual/default mode.

```yaml
model:
  mode: manual-in-tui
```

Agent Council launches the TUI and the user selects `/model` or equivalent manually.

```yaml
model:
  mode: provider-default
```

Agent Council does not pass model flags and intentionally uses the CLI default.

Recommended default:

- PM Agent should use `configured`, because its behavior depends heavily on cost, speed, and context.
- Worker agents can use `configured` when the CLI adapter supports it.
- `manual-in-tui` should remain available for models or providers whose flags are unstable.

### Reasoning Settings

Reasoning settings should be provider/model-specific. Do not force them into one universal field.

Examples:

```yaml
reasoning:
  reasoning_effort: xhigh
```

```yaml
reasoning:
  thinking_budget: 4096
```

```yaml
reasoning:
  thinking_level: high
```

```yaml
reasoning:
  variant: plan
```

The config should allow unknown future fields while the launcher adapter decides what it can actually apply.

Validation should be strict at the boundary:

- Unknown provider-specific reasoning fields can be stored.
- Applying them to a CLI requires a provider adapter that knows the mapping.
- If `model.mode=configured`, unsupported required fields should fail fast.
- If `model.mode=configured-best-effort`, unsupported fields should become visible warnings.

### TUI Launch Capability Matrix

Do not assume every provider setting can be injected when starting a visible TUI. The launcher must distinguish:

- `launch_arg`: supported by the CLI command line.
- `launch_config`: supported through a per-session config/profile override.
- `manual_tui`: must be selected inside the TUI, for example with `/model`.
- `headless_only`: supported by a headless/SDK/ACP path but not by visible TUI launch.
- `unsupported`: not available in the current adapter.

Current local CLI observations:

```text
Claude TUI:
  model: launch_arg (--model)
  effort: launch_arg (--effort)

Codex TUI:
  model: launch_arg (--model)
  reasoning_effort: launch_config candidate (-c model_reasoning_effort=...), not a direct help-listed flag

Gemini TUI:
  model: launch_arg (--model)
  thinking_budget: manual_tui or unsupported in visible TUI launch
  thinking_level: manual_tui or unsupported in visible TUI launch

Kimi TUI:
  model: launch_arg (--model)
  thinking: launch_arg (--thinking / --no-thinking)

OpenCode TUI:
  model: launch_arg (--model provider/model)
  variant: not visible in base TUI help; opencode run supports --variant
```

This means a roster can record desired settings, but the launcher must also record actual applied settings:

```yaml
desired:
  model: gemini-3.1-pro-preview
  reasoning:
    thinking_level: high
applied:
  model:
    status: applied
    method: launch_arg
  reasoning:
    thinking_level:
      status: manual_required
      reason: Gemini TUI exposes no launch flag for this setting.
```

The system must never silently pretend an unsupported reasoning setting was applied.

### Launch Adapter Responsibility

Each CLI needs a small launch adapter:

```text
CouncilAgentConfig -> argv/env/project config files
```

The adapter must answer:

- which model flag to use
- which reasoning fields are supported
- whether settings are command-line flags, config file fields, or TUI-only
- whether multiple instances can share the same workspace config safely
- whether per-actor MCP config must be isolated

The adapter must not infer model settings from `actor`.

Bad:

```text
actor == "gemini" -> use Gemini default
```

Good:

```text
actor == "gemini-flash"
provider == "gemini"
cli == "gemini"
model.id == "gemini-2.5-flash"
```

### Multiple Same-CLI Participants

Multiple participants from the same CLI are required.

Example:

```text
gemini-flash  -> Gemini CLI, cheap fast model
gemini-pro    -> Gemini CLI, deep model
codex-low     -> Codex CLI, low effort
codex-xhigh   -> Codex CLI, xhigh effort
```

Requirements:

- `actor` must be unique in the room.
- `provider` and `cli` may repeat.
- MCP shim must receive the unique `--actor`.
- generated config file names must use actor id, not provider id.
- tmux pane title must use actor id.
- viewer color/status must key by actor id.
- PM Agent must reason over actor id and role, not provider name only.

This is a blocker for useful councils. A council of one `gemini` is not enough; a useful council may need `gemini-flash` for quick broad search and `gemini-pro` for slower deep review.

### Current Implementation Gap

Current code should evolve from:

```text
parse_actor_list(["gemini"]) -> known profile gemini
```

to:

```text
load roster entries from council.json
actor is unique
profile/provider/cli is explicit
selection filters by actor id
```

`parse_actor_list` can remain as a legacy shortcut, but the canonical path should become roster-driven.

## 9. Why Blocking Listen Does Not Fully Solve Realtime Awareness

Blocking listen helps an idle agent receive the next message. It does not solve mid-turn awareness.

If an agent is currently:

- generating a response
- reading files
- editing code
- running tests
- waiting for permission

then a new room message from another agent does not automatically become part of that agent's current reasoning context.

To make it aware, the system must still choose:

- wait until the current turn ends
- interrupt the current turn
- send a follow-up turn
- ignore the event

That is a scheduler problem. MCP listen moves the scheduler problem into each provider's tool loop, where it is harder to control.

## 10. Frontend Observability

The front-end should show enough detail for trust and debugging, but should not default to raw protocol noise.

Recommended default UI:

- Shared room timeline
- Per-agent status column
- Per-agent current turn
- Agent final answers
- Milestone cards
- Tool call cards
- MCP call cards
- Permission cards
- Error/timeout cards
- Token/context usage when available
- "Agent saw up to seq N" indicator

Recommended debug UI:

- Raw provider event drawer
- Raw MCP JSON-RPC frame drawer
- Native terminal/TUI pane only in debug mode
- Adapter logs
- Scheduler decisions

RAH's UI direction is useful here: normalize provider details into tool/observation/permission cards, with raw data available on expansion or debug mode.

## 11. MCP Usage in the New Architecture

MCP should not disappear.

Recommended MCP roles:

- Provider tools exposed to agents.
- Agent Council tools exposed to debug TUI agents.
- Optional external integrations.
- Compatibility with existing CLI tool ecosystems.

Not recommended:

- Making `channel_wait_new` the production inter-agent synchronization backbone.

Better replacement:

```text
Virtual listen = Controller-managed subscriptions and last_seen_seq.
```

This preserves the useful semantics of listening while keeping scheduling and state in Agent Council.

## 12. Context Budget Strategy

Without strict context policy, a council will consume tokens much faster than a single-agent chat.

Recommended policy:

- Do not forward all raw room history to every agent.
- Track `last_seen_seq` per agent.
- Deliver only relevant deltas since `last_seen_seq`.
- Include a shared memory summary.
- Include role-specific instructions.
- Include only selected tool results.
- Compact older room history into markdown or structured memory.
- Store raw logs separately from agent-visible memory.

Possible memory layers:

```text
raw_events
  complete append-only event log

room_transcript
  user and agent-visible messages

shared_memory.md
  compacted durable summary

task_state.json
  current facts, decisions, todos, open questions

agent_private_memory
  provider/session-specific notes
```

For production, `task_state.json` plus `shared_memory.md` is more useful than a single giant markdown transcript.

## 13. Permissions and Workspace

A production council runtime needs centralized permission policy.

Recommended:

- Each agent config defines workspace root.
- Each agent config defines provider permission mode.
- The Controller records all permission requests and resolutions.
- The UI can approve/deny when a provider supports live permissions.
- Debug/full-auto modes can be enabled per agent.

Important:

- "Maximum CLI permission" is convenient for local experiments, but production needs visible audit and containment.
- Headless mode must still surface permission requests. RAH already models this through permission events.

## 14. Resume and Continuation

Agent Council should not require the user to remember five provider session IDs.

Store:

```text
room_id
agent_id
provider
provider_session_id
last_seen_seq
model config
workspace
shared_memory_version
created_at
updated_at
```

To continue a council topic:

```text
agent-council opens room_id
loads shared memory and raw events
resumes or reconnects provider sessions where possible
marks unavailable agents as offline/stale
continues from each agent's last_seen_seq
```

If a provider session cannot resume, create a new provider session and bootstrap it from shared memory plus recent room events.

## 15. Failure Handling

Expected failures:

- CLI not installed
- not logged in
- subscription expired
- model unavailable
- provider CLI upgraded and changed protocol
- trust-folder prompt
- permission prompt
- rate limit
- context limit
- tool timeout
- stuck turn

Controller behavior:

- Mark agent status clearly.
- Do not block the whole council unless policy requires that agent.
- Continue with available agents.
- Surface failure in UI.
- Allow retry, replace provider, or disable agent.

This is another reason not to rely on manually visible TUI windows as the production mechanism.

## 16. Recommended Technical Route

### Phase 1: Harden the existing control plane

Before trusting a PM Agent, the non-AI control plane must be reliable.

Implement or verify:

- council config is roster-driven, not actor-profile hardcoded
- multiple same-CLI participants can be launched with unique actors
- each participant can declare model mode, model id, and reasoning settings
- launcher adapters either apply configured model/reasoning or fail/warn clearly
- control frames never mix with chat messages
- `channel_send_control` reaches the intended target
- tmux/TUI integration can physically interrupt a target pane
- `channel_peek_control` is checked during long tasks
- task create/handoff/review state is persisted
- file claims prevent conflicting edits
- viewer shows control frames and task state
- human can pause all agents
- human can disable or override PM Agent

### Phase 2: Add PM Agent profile

Create a constrained PM Agent persona.

The PM Agent should:

- route work through @mentions
- create tasks with acceptance criteria
- require handoff before review
- keep a compact room status
- interrupt only for clear drift, timeout, contradiction, or user command
- escalate to human instead of improvising unsafe recovery
- avoid editing code directly unless explicitly asked

The PM Agent prompt is a core product artifact, not a generic system prompt.

### Phase 3: Improve TUI/HITL console

Build the local council console around tmux/viewer:

- one pane for viewer/room
- panes for worker CLIs
- optional pane for PM Agent
- visible listening/join state
- shortcut to pause all
- shortcut to interrupt one target
- shortcut to send a message to one agent
- clear display of tasks, claims, and handoffs

This path keeps the human in full control while making AI scheduling useful.

### Phase 4: Add shared memory and compaction

Implement:

- manual compact
- PM Agent controlled compact requests
- shared memory markdown
- structured task state
- raw event archive
- per-agent memory bootstrap

The first compaction path can be manual or PM-assisted. It does not need a complex automatic policy at the start.

### Phase 5: Formalize headless adapter boundary

Headless adapters remain important, but they are the automation track, not the first requirement for a local HITL council.

Define a Agent Council adapter interface:

```python
start_agent(config) -> AgentSession
resume_agent(agent_id, provider_session_id) -> AgentSession
send_turn(agent_id, input, options) -> event stream
interrupt(agent_id) -> result
close(agent_id) -> result
health(agent_id) -> status
list_models(provider) -> model catalog
respond_permission(agent_id, request_id, response) -> result
```

Define normalized events:

```text
agent.started
agent.status_changed
turn.started
message.delta
message.completed
tool.started
tool.delta
tool.completed
permission.requested
permission.resolved
usage.updated
turn.completed
turn.failed
```

Keep these close to RAH's event model, but do not blindly copy the whole RAH workbench.

### Phase 6: Implement headless adapters

Suggested order:

1. Kimi `--wire`, because it is a clean wire protocol.
2. OpenCode `serve`/ACP, because it is structured.
3. Codex `app-server`, because RAH demonstrates this path.
4. Claude SDK, because it is headless but may require SDK-specific handling.
5. Gemini `stream-json`, initially per-turn with resume.

### Phase 7: Build automation controller

Implement:

- append-only room events
- per-agent status
- per-agent `last_seen_seq`
- delivery policy as infrastructure, not high-level intelligence
- timeout policy
- wake idle agents when PM/human requests it
- defer running agents unless PM/human interrupts
- manual route commands

### Phase 8: Add virtual listen for headless mode

Implement Controller-managed subscriptions:

```yaml
subscriptions:
  - agent_id: kimi
    interests: ["all_final_answers", "code_findings", "questions"]
    wake_policy: idle_only
    debounce_ms: 1500
```

The agent does not block on MCP; Agent Council wakes it when policy says so.

### Phase 9: WebUI

Build a council page:

- room timeline
- agent cards
- status and current turn
- model/config display
- tool/MCP cards
- permission cards
- memory panel
- scheduler controls
- debug raw event drawer

## 17. Initial MVP Recommendation

The first implementation should not try to solve every scheduling problem.

Recommended MVP:

- Roster config supports unique `actor` with explicit `provider`, `cli`, `role`, `model`, and `reasoning`.
- Same CLI can appear multiple times, e.g. `gemini-flash` and `gemini-pro`.
- Room event log with seq numbers.
- TUI/tmux council remains first-class for human intervention.
- MCP listen loop remains available for worker agents.
- Add a dedicated PM Agent profile and prompt.
- PM Agent starts as observer/cooperative, not full physical operator.
- PM Agent uses @mentions to route work.
- PM Agent uses `channel_task_create` and `channel_task_handoff` for anti-drift workflow.
- PM Agent uses `channel_send_control` for explicit interrupt/cancel decisions.
- PM Agent receives compact room/context state, not full transcript.
- Viewer shows room messages, control frames, tasks, file claims, and per-agent listening status.
- Human can pause all, interrupt one agent, or override PM Agent from the viewer/TUI.
- No token delta broadcast to other agents by default.
- Headless adapters are explored in parallel, but not required for the first HITL council MVP.

Avoid in MVP:

- Fully autonomous infinite debate.
- Broadcasting every tool event to every agent.
- Giving the PM Agent unrestricted coding authority.
- Complex relevance ranking.
- Overly broad memory automation.
- A large hardcoded scheduler that tries to replace PM Agent judgment.

## 18. Recommended Decision

The recommendation is revised:

Use a Claude Code Agent Team-style organization model on top of Agent Council's existing TUI/MCP/control-plane strengths, and use RAH-style headless sessions as a parallel long-term adapter track.

The production foundation should not be a single monolithic dead-code scheduler.

Instead, split responsibilities:

```text
Dead code:
  reliable room, roster, per-agent inboxes, control frames, task state,
  file claims, persistence, permissions, process lifecycle, audit,
  crash recovery

AI PM Agent:
  high-level scheduling, routing, waiting, interrupt decisions,
  contradiction handling, summaries, escalation to human

Human:
  final override through viewer/TUI, direct Ctrl+C, manual commands,
  ability to pause or replace PM Agent

Headless adapters:
  long-term provider integration and WebUI-only automation path
```

This preserves the useful part of listen: agents feel present in a room and the human can physically intervene. It also avoids putting too much fragile judgment into backend rules.

Claude Code's Agent Team confirms that the durable primitive should be structured shared state, not raw transcript sharing. Agent Council should make roster, task board, mailbox, status events, and shared memory first-class objects before trying to automate more complex scheduling.

The remaining caution is that MCP blocking listen should not be treated as a perfect synchronization primitive. It is a practical HITL mechanism, not a mathematically clean runtime. Agent Council still needs a hard control plane to prevent loops, stuck agents, file conflicts, and unrecoverable state.

## 19. Open Questions

- Which language should host the new runtime adapters: Python inside current Agent Council, TypeScript sidecar inspired by RAH, or a hybrid?
- Should we reuse RAH code directly where licenses and module boundaries allow, or reimplement only the adapter concepts?
- Which two providers should be implemented first for the MVP?
- Which local model/provider should serve as PM Agent by default?
- How strict should PM Agent authority be?
- Which control actions must be physically reliable before PM Agent is trusted?
- How much raw tool output should be visible to other agents by default?
- Should shared memory be markdown-first, JSON-first, or both?
- How should model-specific reasoning/thinking settings be represented without overengineering?
- When should a session graduate from TUI/HITL mode to headless automation mode?

## 20. Current Working Assumption

The current best path is:

```text
Agent Council remains the council/shared-memory product.
Claude Code Agent Team is the reference for organization semantics.
Near-term council uses TUI/MCP listen plus a constrained AI PM Agent.
Dead code focuses on reliable control plane, not high-level meeting intelligence.
RAH remains the reference for future headless provider adapters.
Headless mode is a long-term automation path, not the immediate replacement for HITL TUI.
```
