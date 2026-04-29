import json
import shlex

import pytest

from warroom.channel.agent_profiles import (
    build_mcp_command,
    build_mcp_spec,
    format_mcp_spec_json,
    get_agent_profile,
    list_agent_profiles,
    validate_actor,
)


def test_known_agent_profiles_include_more_than_claude_codex():
    actors = {profile.actor for profile in list_agent_profiles()}
    assert {"claude", "codex", "gemini", "kimi", "opencode"}.issubset(actors)


def test_validate_actor_allows_stable_custom_names():
    assert validate_actor("qwen-72b.reviewer") == "qwen-72b.reviewer"


def test_validate_actor_rejects_shell_sensitive_names():
    with pytest.raises(ValueError):
        validate_actor("bad actor; rm -rf /")


def test_get_agent_profile_is_case_insensitive():
    profile = get_agent_profile("Gemini")
    assert profile is not None
    assert profile.actor == "gemini"


def test_build_mcp_spec_uses_shared_channel_shim():
    spec = build_mcp_spec(
        actor="gemini",
        broker="ws://127.0.0.1:9100",
        cwd="/repo",
        project_dir="/project",
    )
    assert spec["name"] == "channel"
    assert spec["command"] == "uv"
    assert spec["args"] == [
        "--directory",
        "/project",
        "run",
        "python",
        "-m",
        "warroom.channel.mcp_shim",
        "--actor",
        "gemini",
        "--broker",
        "ws://127.0.0.1:9100",
        "--cwd",
        "/repo",
    ]


def test_build_mcp_command_shell_quotes_actor_and_cwd():
    command = build_mcp_command(
        actor="opencode",
        cwd="/repo with spaces",
        project_dir="/project-root",
    )
    assert "warroom.channel.mcp_shim" in command
    argv = shlex.split(command)
    assert argv[argv.index("--directory") + 1] == "/project-root"
    assert "--actor opencode" in command
    assert "'/repo with spaces'" in command


def test_format_mcp_spec_json_round_trips():
    raw = format_mcp_spec_json(actor="kimi", cwd="/repo", project_dir="/project")
    spec = json.loads(raw)
    assert spec["args"][spec["args"].index("--directory") + 1] == "/project"
    assert spec["args"][spec["args"].index("--actor") + 1] == "kimi"
    assert spec["args"][spec["args"].index("--cwd") + 1] == "/repo"
