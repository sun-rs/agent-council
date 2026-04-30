import json
import subprocess

from prompt_toolkit.completion import CompleteEvent, PathCompleter
from prompt_toolkit.document import Document

from warroom.channel import viewer


def _completion_texts(text: str, completer=None) -> list[str]:
    active_completer = completer or viewer.CouncilCompleter()
    return [
        completion.text
        for completion in active_completer.get_completions(
            Document(text, cursor_position=len(text)),
            CompleteEvent(completion_requested=True),
        )
    ]


def test_council_completer_completes_slash_commands():
    completions = _completion_texts("/in")

    assert "/init" in completions
    assert "/inject" in completions
    assert "/inject-missing" in completions


def test_council_completer_ignores_normal_chat_text():
    assert _completion_texts("hello") == []


def test_council_completer_completes_inject_targets(monkeypatch):
    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANES_ENV,
        json.dumps(["agent-council:agents.0", "agent-council:agents.1"]),
    )
    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANE_ACTORS_ENV,
        json.dumps(
            {
                "agent-council:agents.0": "kimi_reader",
                "agent-council:agents.1": "codex_55",
            }
        ),
    )

    assert _completion_texts("/inject c") == ["codex_55"]
    assert "all" in _completion_texts("/inject ")


def test_council_completer_completes_init_workdir(tmp_path):
    (tmp_path / "project-alpha").mkdir()
    (tmp_path / "project-file").write_text("not a directory")
    completer = viewer.CouncilCompleter(
        path_completer=PathCompleter(
            only_directories=True,
            get_paths=lambda: [str(tmp_path)],
            expanduser=True,
        )
    )

    completions = _completion_texts("/init proj", completer=completer)

    assert "ect-alpha" in completions


def test_council_agent_panes_reads_json_env(monkeypatch):
    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANES_ENV,
        json.dumps(["agent-council:0.1", "agent-council:0.2"]),
    )

    assert viewer._council_agent_panes() == [
        "agent-council:0.1",
        "agent-council:0.2",
    ]


def test_council_agent_pane_actors_reads_json_env(monkeypatch):
    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANE_ACTORS_ENV,
        json.dumps({"agent-council:0.1": "codex@gpt-5.4"}),
    )

    assert viewer._council_agent_pane_actors() == {
        "agent-council:0.1": "codex@gpt-5.4",
    }


def test_send_init_prompt_to_agent_panes_uses_tmux_send_keys(monkeypatch):
    calls = []
    sleeps = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(viewer.subprocess, "run", fake_run)
    monkeypatch.setattr(viewer.time, "sleep", lambda seconds: sleeps.append(seconds))

    count = viewer.send_init_prompt_to_agent_panes(
        "room1",
        panes=["agent-council:0.1", "agent-council:0.2"],
    )

    assert count == 2
    assert calls[0][0][:5] == ["tmux", "send-keys", "-t", "agent-council:0.1", "-l"]
    assert 'channel_join(room="room1")' in calls[0][0][-1]
    assert calls[1][0] == ["tmux", "send-keys", "-t", "agent-council:0.1", "Enter"]
    assert calls[2][0][:5] == ["tmux", "send-keys", "-t", "agent-council:0.2", "-l"]
    assert calls[3][0] == ["tmux", "send-keys", "-t", "agent-council:0.2", "Enter"]
    assert sleeps == [viewer.DEFAULT_SEND_ENTER_DELAY_S, viewer.DEFAULT_SEND_ENTER_DELAY_S]


def test_send_init_prompt_includes_actor_identity_from_env(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANES_ENV,
        json.dumps(["agent-council:0.1"]),
    )
    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANE_ACTORS_ENV,
        json.dumps({"agent-council:0.1": "codex@gpt-5.4"}),
    )
    monkeypatch.setattr(viewer.subprocess, "run", fake_run)
    monkeypatch.setattr(viewer.time, "sleep", lambda _seconds: None)

    count = viewer.send_init_prompt_to_agent_panes("room1")

    assert count == 1
    assert "codex@gpt-5.4" in calls[0][0][-1]


def test_refresh_agent_panes_from_tmux_records_titles(monkeypatch):
    def fake_run(command, **kwargs):
        assert command[:3] == ["tmux", "list-panes", "-t"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="0\tkimi_reader\n1\tcodex_55\n",
        )

    monkeypatch.setattr(viewer.subprocess, "run", fake_run)

    assert viewer.refresh_agent_panes_from_tmux("agent-council", "agents") == 2
    assert viewer._council_agent_panes() == [
        "agent-council:agents.0",
        "agent-council:agents.1",
    ]
    assert viewer._council_agent_pane_actors()["agent-council:agents.0"] == "kimi_reader"


def test_handle_viewer_inject_command_targets_one_actor(monkeypatch):
    sent = []
    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANES_ENV,
        json.dumps(["agent-council:agents.0", "agent-council:agents.1"]),
    )
    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANE_ACTORS_ENV,
        json.dumps(
            {
                "agent-council:agents.0": "kimi_reader",
                "agent-council:agents.1": "codex_55",
            }
        ),
    )

    def fake_send(room, panes=None):
        sent.append((room, panes))
        return len(panes or [])

    monkeypatch.setattr(viewer, "send_init_prompt_to_agent_panes", fake_send)

    assert viewer.handle_viewer_command("/inject kimi_reader", "room1") == "handled"
    assert sent == [("room1", ["agent-council:agents.0"])]


def test_handle_viewer_help_explains_local_commands(capsys):
    assert viewer.handle_viewer_command("/help", "room1") == "handled"

    output = capsys.readouterr().out
    assert "/init [workdir]" in output
    assert "/inject <agent|all>" in output
    assert "/inject-missing" in output
    assert "not broadcast" in output


async def test_send_bootstrap_prompt_to_missing_agent_panes_only_targets_not_joined(monkeypatch):
    sent = []
    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANES_ENV,
        json.dumps(["agent-council:agents.0", "agent-council:agents.1"]),
    )
    monkeypatch.setenv(
        viewer.COUNCIL_AGENT_PANE_ACTORS_ENV,
        json.dumps(
            {
                "agent-council:agents.0": "kimi_reader",
                "agent-council:agents.1": "codex_55",
            }
        ),
    )

    class FakeClient:
        async def room_state(self, room):
            assert room == "room1"
            return {"active_agents": [{"actor": "codex_55"}]}

    def fake_send(room, panes=None):
        sent.append((room, panes))
        return len(panes or [])

    monkeypatch.setattr(viewer, "send_init_prompt_to_agent_panes", fake_send)

    count, actors = await viewer.send_bootstrap_prompt_to_missing_agent_panes(
        FakeClient(),
        "room1",
    )

    assert count == 1
    assert actors == ["kimi_reader"]
    assert sent == [("room1", ["agent-council:agents.0"])]


def test_handle_viewer_init_command_is_local(monkeypatch):
    sent = []

    def fake_send(room):
        sent.append(room)
        return 3

    monkeypatch.setattr(viewer, "send_init_prompt_to_agent_panes", fake_send)

    assert viewer.handle_viewer_command("/init", "room1") == "handled"
    assert sent == ["room1"]
    assert viewer.handle_viewer_command("/unknown", "room1") == "handled"
    assert viewer.handle_viewer_command("normal message", "room1") is None


def test_exit_council_session_kills_configured_tmux_session(monkeypatch):
    calls = []
    monkeypatch.setenv(viewer.COUNCIL_SESSION_ENV, "agent-council")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(viewer.subprocess, "run", fake_run)

    assert viewer.exit_council_session() is True
    assert calls == [
        (["tmux", "kill-session", "-t", "agent-council"], {"check": True})
    ]


def test_handle_viewer_exit_returns_exit_signal(monkeypatch):
    killed = []

    def fake_exit():
        killed.append(True)
        return True

    monkeypatch.setattr(viewer, "exit_council_session", fake_exit)

    assert viewer.handle_viewer_command("/exit", "room1") == "exit"
    assert killed == [True]


def test_exit_without_council_session_exits_viewer_only(monkeypatch):
    monkeypatch.delenv(viewer.COUNCIL_SESSION_ENV, raising=False)

    assert viewer.exit_council_session() is False
