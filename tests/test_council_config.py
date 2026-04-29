import json

import pytest

from warroom.channel.council_config import (
    build_codex_config_toml,
    build_council_config,
    build_council_config_from_toml,
    build_mcp_json_config,
    build_opencode_config,
    mcp_server_name_for_actor,
    parse_actor_list,
    write_council_config,
)


def test_parse_actor_list_defaults_to_known_council():
    assert parse_actor_list(None) == ["claude", "codex", "gemini", "kimi", "opencode"]


def test_parse_actor_list_accepts_commas_spaces_and_dedupes_known_case():
    assert parse_actor_list("Claude, codex gemini codex") == ["claude", "codex", "gemini"]


def test_parse_actor_list_rejects_shell_sensitive_actor():
    with pytest.raises(ValueError):
        parse_actor_list("claude bad;actor")


def test_parse_actor_list_supports_model_cli_instances():
    assert parse_actor_list("gpt-5.4@Codex,gpt-5.5@codex") == [
        "gpt-5.4@codex",
        "gpt-5.5@codex",
    ]
    assert parse_actor_list("Codex@gpt-5.4") == ["gpt-5.4@codex"]
    assert mcp_server_name_for_actor("gpt-5.4@codex") == "channel_gpt-5_4_codex"


def test_build_council_config_records_user_surface_and_agent_files(tmp_path):
    config = build_council_config(
        actors=["claude", "codex"],
        broker="ws://127.0.0.1:9999",
        cwd=str(tmp_path / "workspace"),
        project_dir="/project",
    )

    assert config["interaction"]["userSurface"] == "web-or-terminal-viewer"
    assert config["agents"][0]["enabled"] is True
    assert config["agents"][0]["accessMode"] == "max"
    assert config["agents"][0]["model"]["selection"] == "manual-in-tui"
    assert config["agents"][0]["configFiles"]["claudeProjectMcpJson"] == "mcp/claude.mcp.json"
    assert config["agents"][1]["configFiles"]["codexConfigToml"] == "mcp/codex.codex.config.toml"
    assert config["agents"][1]["mcp"]["args"][0:2] == ["--directory", "/project"]


def test_build_council_config_infers_model_and_effort_from_instance_actor(tmp_path):
    config = build_council_config(
        actors=["gpt-5.4@codex", "gpt-5.5@codex"],
        broker="ws://127.0.0.1:9999",
        cwd=str(tmp_path / "workspace"),
        project_dir="/project",
        reasoning_effort="xhigh",
    )

    assert [agent["actor"] for agent in config["agents"]] == [
        "gpt-5.4@codex",
        "gpt-5.5@codex",
    ]
    assert [agent["cli"] for agent in config["agents"]] == ["codex", "codex"]
    assert config["agents"][0]["model"] == {
        "selection": "configured",
        "desired": "gpt-5.4",
    }
    assert config["agents"][1]["model"]["desired"] == "gpt-5.5"
    assert config["agents"][0]["reasoning"]["desired"]["reasoning_effort"] == "xhigh"
    assert config["agents"][0]["mcpServerName"] == "channel_gpt-5_4_codex"


def test_build_council_config_from_toml_uses_model_first_agent_refs(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
room = "roomx"
broker = "ws://127.0.0.1:9101"
workdir = "/tmp/work"

[[agents]]
id = "gpt-5.4@codex"
reasoning_effort = "xhigh"

[[agents]]
id = "gemini-2.5-flash@gemini"
thinking_budget = 1024
""".strip()
    )

    config = build_council_config_from_toml(config_path)

    assert config["room"] == "roomx"
    assert [agent["actor"] for agent in config["agents"]] == [
        "gpt-5.4@codex",
        "gemini-2.5-flash@gemini",
    ]
    assert config["agents"][0]["cli"] == "codex"
    assert config["agents"][0]["model"]["desired"] == "gpt-5.4"
    assert config["agents"][0]["reasoning"]["desired"]["reasoning_effort"] == "xhigh"
    assert config["agents"][1]["reasoning"]["desired"]["thinking_budget"] == 1024


def test_build_council_config_from_grouped_toml_uses_alias_as_actor(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
workdir = "/tmp/work"

[codex]
[[codex.agents]]
model_id = "gpt-5.5"
reasoning_effort = "xhigh"
alias = "codex_55"

[[codex.agents]]
model_id = "gpt-5.4"
reasoning_effort = "high"

[gemini]
[[gemini.agents]]
model_id = "gemini-2.5-pro"
thinking_budget = 32768
alias = "gemini_pro"
""".strip()
    )

    config = build_council_config_from_toml(config_path)

    assert [agent["actor"] for agent in config["agents"]] == [
        "codex_55",
        "gpt-5.4@codex",
        "gemini_pro",
    ]
    assert config["agents"][0]["label"] == "codex_55"
    assert config["agents"][0]["cli"] == "codex"
    assert config["agents"][0]["model"]["desired"] == "gpt-5.5"
    assert config["agents"][0]["mcpServerName"] == "channel_codex_55"
    assert config["agents"][1]["reasoning"]["desired"]["reasoning_effort"] == "high"
    assert config["agents"][2]["reasoning"]["desired"]["thinking_budget"] == 32768


def test_build_council_config_from_grouped_toml_rejects_duplicate_alias(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
workdir = "/tmp/work"

[codex]
[[codex.agents]]
model_id = "gpt-5.5"
alias = "deepseek_v4"

[gemini]
[[gemini.agents]]
model_id = "gemini-2.5-pro"
alias = "deepseek_v4"
""".strip()
    )

    with pytest.raises(ValueError, match="duplicate agent alias/actor"):
        build_council_config_from_toml(config_path)


def test_build_common_mcp_json_config_is_compatible_shape(tmp_path):
    config = build_mcp_json_config(
        "kimi",
        broker="ws://127.0.0.1:9100",
        cwd=str(tmp_path),
        project_dir="/project",
    )

    server = config["mcpServers"]["channel"]
    assert server["type"] == "stdio"
    assert server["command"] == "uv"
    assert "--actor" in server["args"]
    assert server["args"][server["args"].index("--actor") + 1] == "kimi"


def test_build_codex_config_toml_uses_mcp_servers_table(tmp_path):
    raw = build_codex_config_toml(
        "codex",
        broker="ws://127.0.0.1:9100",
        cwd=str(tmp_path),
        project_dir="/project-root",
    )

    assert "[mcp_servers.channel]" in raw
    assert 'command = "uv"' in raw
    assert '"--directory"' in raw
    assert '"/project-root"' in raw


def test_build_opencode_config_uses_local_command_vector(tmp_path):
    config = build_opencode_config(
        "opencode",
        broker="ws://127.0.0.1:9100",
        cwd=str(tmp_path),
        project_dir="/project",
    )

    server = config["mcp"]["channel"]
    assert server["type"] == "local"
    assert server["command"][0] == "uv"
    assert "--actor" in server["command"]
    assert server["enabled"] is True


def test_write_council_config_writes_canonical_and_cli_artifacts(tmp_path):
    paths = write_council_config(
        tmp_path / ".agent-council",
        actors=["claude", "codex", "gemini", "opencode"],
        broker="ws://127.0.0.1:9100",
        cwd=str(tmp_path / "workspace"),
        project_dir="/project",
    )

    assert tmp_path / ".agent-council" / "council.json" in paths
    assert (tmp_path / ".agent-council" / "mcp" / "claude.mcp.json").exists()
    assert (tmp_path / ".agent-council" / "mcp" / "codex.codex.config.toml").exists()
    assert (tmp_path / ".agent-council" / "mcp" / "gemini.gemini.settings.json").exists()
    assert (tmp_path / ".agent-council" / "mcp" / "opencode.opencode.json").exists()

    council = json.loads((tmp_path / ".agent-council" / "council.json").read_text())
    assert [agent["actor"] for agent in council["agents"]] == [
        "claude",
        "codex",
        "gemini",
        "opencode",
    ]


def test_write_council_config_writes_multi_codex_artifacts(tmp_path):
    paths = write_council_config(
        tmp_path / ".agent-council",
        actors=["gpt-5.4@codex", "gpt-5.5@codex"],
        broker="ws://127.0.0.1:9100",
        cwd=str(tmp_path / "workspace"),
        project_dir="/project",
        reasoning_effort="xhigh",
    )

    assert tmp_path / ".agent-council" / "mcp" / "gpt-5.4@codex.codex.config.toml" in paths
    assert (tmp_path / ".agent-council" / "mcp" / "gpt-5.5@codex.codex.config.toml").exists()
    council = json.loads((tmp_path / ".agent-council" / "council.json").read_text())
    assert council["agents"][0]["model"]["desired"] == "gpt-5.4"
    assert council["agents"][1]["reasoning"]["desired"]["reasoning_effort"] == "xhigh"


def test_write_council_config_refuses_overwrite_without_force(tmp_path):
    out = tmp_path / ".agent-council"
    write_council_config(out, actors=["claude"], cwd=str(tmp_path), project_dir="/project")

    with pytest.raises(FileExistsError):
        write_council_config(out, actors=["claude"], cwd=str(tmp_path), project_dir="/project")

    write_council_config(
        out,
        actors=["claude"],
        cwd=str(tmp_path),
        project_dir="/project",
        force=True,
    )
