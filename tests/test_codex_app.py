"""Verify local adapter behavior; RPC and WebSocket framing belong to dependencies."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("openai_codex", reason="Run with the isolated Codex script dependencies")
from openai_codex.models import (  # noqa: E402
    ItemCompletedNotification,
    Notification,
    TurnCompletedNotification,
    UnknownNotification,
)

spec = importlib.util.spec_from_file_location(
    "codex_app", Path(__file__).resolve().parents[1] / "codex_app.py"
)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def completed(status="completed"):
    return Notification(
        method="turn/completed",
        payload=TurnCompletedNotification.model_validate(
            {
                "threadId": "thread",
                "turn": {"id": "turn", "status": status, "items": [], "itemsView": "full"},
            }
        ),
    )


def test_visibility_checks_all_default_pages():
    calls = []

    def thread_list(params):
        calls.append(dict(params))
        assert "sourceKinds" not in params
        if not params.get("cursor"):
            return SimpleNamespace(data=[SimpleNamespace(id="old")], next_cursor="page2")
        assert params["cursor"] == "page2"
        return SimpleNamespace(data=[SimpleNamespace(id="new")], next_cursor=None)

    assert app.find_visible(SimpleNamespace(thread_list=thread_list), Path("/project"), "new")
    assert len(calls) == 2


def test_sdk_approval_default_is_overridden(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(app, "CodexClient", constructor)
    app.create_client(Path("/app.sock"))
    callback = constructor.call_args.kwargs["approval_handler"]
    for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        with pytest.raises(RuntimeError, match="requires attention"):
            callback(method, {})


def test_typed_events_emit_public_reply_and_release_subscription(capsys):
    reply = Notification(
        method="item/completed",
        payload=ItemCompletedNotification.model_validate(
            {
                "threadId": "thread",
                "turnId": "turn",
                "completedAtMs": 0,
                "item": {"id": "reply", "type": "agentMessage", "text": "验证通过"},
            }
        ),
    )
    client = Mock()
    client.next_turn_notification.side_effect = [
        Notification(method="future/event", payload=UnknownNotification(params={})),
        reply,
        completed(),
    ]
    assert app.wait_for_turn(client, "turn")["status"] == "completed"
    assert json.loads(capsys.readouterr().out) == {"event": "message", "text": "验证通过"}
    client.unregister_turn_notifications.assert_called_once_with("turn")


def test_failed_turn_is_not_reported_as_success(monkeypatch, capsys):
    client = Mock()
    client.turn_start.return_value = SimpleNamespace(turn=SimpleNamespace(id="turn"))
    client.next_turn_notification.return_value = completed("failed")
    monkeypatch.setattr(app, "find_visible", lambda *_: None)
    with pytest.raises(RuntimeError, match="Turn failed"):
        app.run_task(client, "thread", "prompt", Path("/project"))
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert "visible" not in events
    client.unregister_turn_notifications.assert_called_once_with("turn")


def test_default_project_is_callers_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["codex_app.py", "list"])
    _, args = app.parse_args()
    assert args.cwd == tmp_path


def test_explicit_project_and_socket_override_defaults(monkeypatch, tmp_path):
    project = tmp_path / "project"
    socket = tmp_path / "server.sock"
    monkeypatch.setattr(
        sys, "argv", ["codex_app.py", "--cwd", str(project), "--socket", str(socket), "list"]
    )
    _, args = app.parse_args()
    assert args.cwd == project
    assert args.socket == socket


def test_create_uses_selected_project_and_preserves_prompt_path(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "task.md").write_text("Run the task", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "codex_app.py",
            "--cwd",
            str(project),
            "create",
            "--name",
            "Task",
            "--prompt-file",
            "task.md",
        ],
    )
    thread = Mock(id="new-thread")
    thread.model_dump.return_value = {"id": "new-thread", "cwd": str(project)}
    client = Mock()
    client.thread_start.return_value = SimpleNamespace(thread=thread)
    monkeypatch.setattr(app, "create_client", lambda _: client)
    run = Mock()
    monkeypatch.setattr(app, "run_task", run)
    assert app.main() == 0
    params = client.thread_start.call_args.args[0]
    assert params["cwd"] == str(project)
    assert params["sandbox"] == "workspace-write"
    assert params["approvalPolicy"] == "on-request"
    assert params["approvalsReviewer"] == "auto_review"
    client.thread_set_name.assert_called_once_with("new-thread", "Task")
    run.assert_called_once_with(client, "new-thread", "Run the task", project)
    client.close.assert_called_once()
