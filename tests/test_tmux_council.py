import json
import shlex
import subprocess

import pytest

from warroom.channel.council_config import build_council_config_from_toml, write_council_config
import warroom.channel.tmux_council as tmux_council
from warroom.channel.tmux_council import (
    build_agent_command,
    build_tmux_plan,
    join_listen_prompt,
    launch_agents_from_toml,
    prepare_cli_configs,
    render_tmux_commands,
    run_tmux_council,
)


def _write_config(tmp_path, actors=None):
    out = tmp_path / ".agent-council"
    write_council_config(
        out,
        actors=actors or ["claude", "codex", "gemini", "kimi", "opencode"],
        broker="ws://127.0.0.1:9100",
        cwd=str(tmp_path / "workspace"),
        project_dir="/project",
    )
    return out / "council.json"


def _load(path):
    return json.loads(path.read_text())


def test_build_agent_commands_use_max_access_without_model_flags(tmp_path):
    config_path = _write_config(tmp_path)
    council = _load(config_path)
    config_dir = config_path.parent
    by_actor = {agent["actor"]: agent for agent in council["agents"]}

    claude = build_agent_command(config_dir, council, by_actor["claude"])
    assert "claude" in claude
    assert "--permission-mode bypassPermissions" in claude
    assert "--dangerously-skip-permissions" in claude
    assert "--model" not in claude

    codex = build_agent_command(config_dir, council, by_actor["codex"])
    assert "--ask-for-approval never" in codex
    assert "--sandbox danger-full-access" in codex
    assert "mcp_servers.channel.command" in codex
    assert "--model" not in codex

    gemini = build_agent_command(config_dir, council, by_actor["gemini"])
    assert "gemini" in gemini
    assert "--approval-mode yolo" in gemini
    assert "--skip-trust" in gemini
    assert "--model" not in gemini

    kimi = build_agent_command(config_dir, council, by_actor["kimi"])
    assert "kimi" in kimi
    assert "--yolo" in kimi
    assert "--mcp-config-file" in kimi
    assert "--model" not in kimi

    opencode = build_agent_command(config_dir, council, by_actor["opencode"])
    assert "OPENCODE_CONFIG_CONTENT=" in opencode
    assert "opencode" in opencode
    assert '"permission":"allow"' in opencode
    assert "--model" not in opencode


def test_prepare_cli_configs_merges_gemini_project_settings(tmp_path):
    config_path = _write_config(tmp_path, actors=["gemini"])
    workspace_settings = tmp_path / "workspace" / ".gemini" / "settings.json"
    workspace_settings.parent.mkdir(parents=True)
    workspace_settings.write_text(json.dumps({"ui": {"theme": "dark"}}))

    written = prepare_cli_configs(config_path.parent, _load(config_path))

    assert written == [workspace_settings]
    merged = json.loads(workspace_settings.read_text())
    assert merged["ui"] == {"theme": "dark"}
    assert merged["mcpServers"]["channel"]["command"] == "uv"
    assert merged["mcpServers"]["channel"]["args"][
        merged["mcpServers"]["channel"]["args"].index("--actor") + 1
    ] == "gemini"


def test_prepare_cli_configs_keeps_multiple_gemini_instances_separate(tmp_path):
    config_path = _write_config(tmp_path, actors=["gemini-2.5-flash@gemini", "gemini-2.5-pro@gemini"])

    prepare_cli_configs(config_path.parent, _load(config_path))

    workspace_settings = tmp_path / "workspace" / ".gemini" / "settings.json"
    merged = json.loads(workspace_settings.read_text())
    servers = merged["mcpServers"]
    assert "channel_gemini-2_5-flash_gemini" in servers
    assert "channel_gemini-2_5-pro_gemini" in servers
    flash_args = servers["channel_gemini-2_5-flash_gemini"]["args"]
    assert flash_args[flash_args.index("--actor") + 1] == "gemini-2.5-flash@gemini"


def test_prepare_cli_configs_filters_to_launched_actors(tmp_path):
    config_path = _write_config(tmp_path, actors=["codex", "gemini"])

    written = prepare_cli_configs(
        config_path.parent,
        _load(config_path),
        actors={"codex"},
    )

    assert written == []
    assert not (tmp_path / "workspace" / ".gemini" / "settings.json").exists()


def test_run_tmux_council_prepares_only_selected_runnable_agents(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, actors=["codex", "gemini"])
    captured = []

    monkeypatch.setattr(tmux_council.shutil, "which", lambda _: "/usr/bin/fake")
    monkeypatch.setattr(tmux_council, "_tmux_session_exists", lambda _session: False)
    monkeypatch.setattr(tmux_council, "_run_tmux_command", lambda _command: None)

    def fake_prepare(config_dir, council, *, actors=None):
        captured.append(actors)
        return []

    monkeypatch.setattr(tmux_council, "prepare_cli_configs", fake_prepare)

    run_tmux_council(
        config_path,
        actors="codex",
        session_name="selected-council",
        attach=False,
    )

    assert captured == [{"codex"}]


def test_build_codex_instance_command_applies_model_and_effort(tmp_path):
    config_path = tmp_path / ".agent-council" / "council.json"
    write_council_config(
        tmp_path / ".agent-council",
        actors=["gpt-5.4@codex", "gpt-5.5@codex"],
        broker="ws://127.0.0.1:9100",
        cwd=str(tmp_path / "workspace"),
        project_dir="/project",
        reasoning_effort="xhigh",
    )
    council = _load(config_path)
    config_dir = config_path.parent
    by_actor = {agent["actor"]: agent for agent in council["agents"]}

    codex_54 = build_agent_command(config_dir, council, by_actor["gpt-5.4@codex"])
    codex_55 = build_agent_command(config_dir, council, by_actor["gpt-5.5@codex"])

    assert "--model gpt-5.4" in codex_54
    assert "'model_reasoning_effort=\"xhigh\"'" in codex_54
    assert "mcp_servers.channel_gpt-5_4_codex.command" in codex_54
    assert '"--actor", "gpt-5.4@codex"' in codex_54
    assert "--model gpt-5.5" in codex_55
    assert "mcp_servers.channel_gpt-5_5_codex.command" in codex_55


def test_kimi_instance_omits_unknown_model_flag(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_path = tmp_path / ".agent-council" / "council.json"
    write_council_config(
        tmp_path / ".agent-council",
        actors=["kimi-k2.6@kimi"],
        broker="ws://127.0.0.1:9100",
        cwd=str(tmp_path / "workspace"),
        project_dir="/project",
    )
    council = _load(config_path)
    agent = council["agents"][0]

    command = build_agent_command(config_path.parent, council, agent)
    args = shlex.split(command)

    assert "--model" not in args
    assert args[args.index("--mcp-config-file") + 1] == str(
        (config_path.parent / "mcp/kimi-k2.6@kimi.mcp.json").resolve()
    )


def test_kimi_instance_passes_configured_model_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    kimi_config = home / ".kimi" / "config.toml"
    kimi_config.parent.mkdir(parents=True)
    kimi_config.write_text(
        """
default_model = "kimi-code/kimi-for-coding"

[models."kimi-code/kimi-for-coding"]
provider = "managed:kimi-code"
model = "kimi-for-coding"
""".strip()
    )
    monkeypatch.setenv("HOME", str(home))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
workdir = "{tmp_path / "workspace"}"

[kimi]
[[kimi.agents]]
model_id = "kimi-code/kimi-for-coding"
alias = "kimi_coder"
thinking = true
""".strip()
    )
    council = build_council_config_from_toml(config_path)
    agent = council["agents"][0]

    command = build_agent_command(tmp_path / ".agent-council", council, agent)
    args = shlex.split(command)

    assert args[args.index("--model") + 1] == "kimi-code/kimi-for-coding"
    assert "--thinking" in args


def test_launch_agents_from_toml_dry_run_uses_second_window(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
room = "room1"

[[agents]]
id = "gpt-5.4@codex"
reasoning_effort = "xhigh"

[[agents]]
id = "gpt-5.5@codex"
reasoning_effort = "xhigh"
""".strip()
    )
    monkeypatch.setattr(tmux_council.shutil, "which", lambda _: "/usr/bin/fake")

    skipped, written, commands = launch_agents_from_toml(
        config_path,
        workdir=str(tmp_path / "workspace"),
        session_name="stage-council",
        window_name="agents",
        dry_run=True,
        listen_delay_s=1,
    )

    rendered = render_tmux_commands(commands)
    assert skipped == []
    assert written == []
    assert "tmux new-window -d -t stage-council -n agents" in rendered
    assert "codex --cd" in rendered
    assert "--model gpt-5.4" in rendered
    assert "--model gpt-5.5" in rendered
    assert "model_reasoning_effort" in rendered
    assert "tmux select-window -t stage-council:agents" in rendered


def test_build_tmux_plan_filters_actors_and_skips_missing(tmp_path):
    config_path = _write_config(tmp_path)

    plan = build_tmux_plan(
        config_path,
        actors="codex,missing-custom",
        session_name="test-council",
        skip_missing=True,
    )

    names = [pane.name for pane in plan.panes]
    assert names[0] == "viewer"
    assert "broker" not in names
    assert "claude" not in names
    assert "gemini" not in names
    assert any(item.startswith("missing-custom:") for item in plan.skipped)


def test_run_tmux_council_dry_run_prints_commands_without_writing_gemini(tmp_path):
    config_path = _write_config(tmp_path, actors=["gemini"])

    plan, written, commands = run_tmux_council(
        config_path,
        actors="gemini",
        session_name="dry-council",
        attach=False,
        dry_run=True,
    )

    rendered = render_tmux_commands(commands)
    assert written == []
    assert "tmux new-session" in rendered
    assert "dry-council" in rendered
    assert "gemini" in rendered
    assert "select-pane -t dry-council:0 -T viewer" in rendered
    assert "main-pane-width 50%" in rendered
    assert "split-window -t dry-council:0 -h -p 50" in rendered
    assert "select-layout -t dry-council:0 main-vertical" in rendered
    assert "[agent-council-pane] viewer" in rendered
    assert "AGENT_COUNCIL_AGENT_PANES=" in rendered
    assert "AGENT_COUNCIL_AGENT_PANE_ACTORS=" in rendered
    assert "AGENT_COUNCIL_SESSION=dry-council" in rendered
    assert "--no-viewer" not in rendered
    assert plan.session_name == "dry-council"
    assert not (tmp_path / "workspace" / ".gemini" / "settings.json").exists()


def test_run_tmux_council_auto_listen_sends_prompt_to_agent_panes(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, actors=["codex", "gemini"])
    monkeypatch.setattr(tmux_council.shutil, "which", lambda _: "/usr/bin/fake")

    _plan, _written, commands = run_tmux_council(
        config_path,
        actors="codex,gemini",
        session_name="auto-council",
        attach=False,
        dry_run=True,
        auto_listen=True,
        listen_delay_s=3.5,
    )

    rendered = render_tmux_commands(commands)
    assert "tmux run-shell -b" in rendered
    assert "sleep 3.5" in rendered
    assert "tmux send-keys -t auto-council:0.1 -l" in rendered
    assert "tmux send-keys -t auto-council:0.2 -l" in rendered
    assert "tmux send-keys -t auto-council:0.1 Enter" in rendered
    assert "tmux send-keys -t auto-council:0.2 Enter" in rendered
    assert 'channel_join(room="room1")' in rendered
    assert "codex" in rendered
    assert "gemini" in rendered
    assert "你的 actor id 是 'codex'" in join_listen_prompt("room1", actor="codex")
    assert "recent_messages" in join_listen_prompt("room1", actor="codex")
    assert "用户消息优先级最高" in join_listen_prompt("room1", actor="codex")
    assert "其他 agent 的 @ 点名" in join_listen_prompt("room1", actor="codex")
    assert "timeout 不是结束" in join_listen_prompt("room1")


def test_tmux_viewer_banner_includes_skipped_agents(tmp_path):
    config_path = _write_config(tmp_path)

    plan = build_tmux_plan(
        config_path,
        actors="codex",
        session_name="banner-council",
        skip_missing=True,
    )
    rendered = render_tmux_commands(
        run_tmux_council(
            config_path,
            actors="codex",
            session_name="banner-council",
            dry_run=True,
        )[2]
    )

    assert "claude: not selected" in plan.skipped
    assert "[agent-council] skipped:" in rendered
    assert "claude: not selected" in rendered


def test_run_tmux_council_existing_session_requires_replace(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, actors=["codex"])

    monkeypatch.setattr(tmux_council.shutil, "which", lambda _: "/usr/bin/fake")

    def fake_run(command, **_kwargs):
        if command[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(tmux_council.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="already exists"):
        run_tmux_council(
            config_path,
            actors="codex",
            session_name="existing-council",
            attach=False,
        )


def test_run_tmux_council_replace_kills_existing_session(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, actors=["codex"])
    calls = []

    monkeypatch.setattr(tmux_council.shutil, "which", lambda _: "/usr/bin/fake")

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(tmux_council.subprocess, "run", fake_run)

    run_tmux_council(
        config_path,
        actors="codex",
        session_name="existing-council",
        attach=False,
        replace_existing=True,
    )

    assert ["tmux", "kill-session", "-t", "existing-council"] in calls
    assert any(command[:2] == ["tmux", "new-session"] for command in calls)
