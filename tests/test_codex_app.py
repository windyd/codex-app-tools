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


@pytest.fixture
def worktree(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    root = tmp_path / "linked worktree"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
            "--allow-empty",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(root)], check=True
    )
    return root


def fake_thread(cwd, section=None):
    thread = Mock(id="new-thread")
    thread.model_dump.return_value = {
        "id": "new-thread",
        "cwd": str(cwd),
        "section": section,
    }
    return thread


def setup_create(monkeypatch, cwd, extra=()):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "codex_app.py",
            "create",
            "--name",
            "Task",
            "--prompt",
            "hello",
            *extra,
        ],
    )
    client = Mock()
    client.thread_start.return_value = SimpleNamespace(thread=fake_thread(cwd))
    monkeypatch.setattr(app, "create_client", lambda _: client)
    run = Mock()
    monkeypatch.setattr(app, "run_task", run)
    return client, run


def test_worktree_root_validation_and_inherited_git_override(worktree, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/does/not/exist")
    assert app.validate_worktree(worktree) == worktree
    nested = worktree / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="root"):
        app.validate_worktree(nested)


def test_invalid_worktree_never_connects(monkeypatch, tmp_path):
    client, run = setup_create(monkeypatch, tmp_path, ["--worktree", str(tmp_path)])
    assert app.main() == 1
    client.start.assert_not_called()
    run.assert_not_called()


def test_explicit_cwd_conflict_never_connects(monkeypatch, tmp_path, worktree):
    client, run = setup_create(monkeypatch, worktree, ["--worktree", str(worktree)])
    monkeypatch.setattr(sys, "argv", [sys.argv[0], "--cwd", str(tmp_path), *sys.argv[1:]])
    assert app.main() == 1
    client.start.assert_not_called()
    run.assert_not_called()


def test_sections_pagination_preserves_duplicate_names():
    client = Mock()
    client.request.side_effect = [
        app.SectionPage.model_validate({"data": [{"id": "a", "name": "Same"}], "nextCursor": "2"}),
        app.SectionPage.model_validate({"data": [{"id": "b", "name": "Same"}]}),
    ]
    assert [s.id for s in app.list_sections(client)] == ["a", "b"]
    assert client.request.call_args.args[1]["cursor"] == "2"


def test_unknown_section_fails_before_thread_creation(monkeypatch, tmp_path, capsys):
    client, run = setup_create(monkeypatch, tmp_path, ["--section-id", "missing"])
    client.request.return_value = app.SectionPage(data=[])
    assert app.main() == 1
    client.thread_start.assert_not_called()
    run.assert_not_called()
    error = json.loads(capsys.readouterr().out)
    assert error["stage"] == "section.validate"
    assert error["thread_id"] is None


def test_unsupported_sections_report_limit_without_creating(monkeypatch, tmp_path, capsys):
    client, _ = setup_create(monkeypatch, tmp_path, ["--section-id", "section"])
    client.request.side_effect = app.JsonRpcError(-32601, "Method not found")
    assert app.main() == 1
    client.thread_start.assert_not_called()
    assert "does not support threadSection/list" in capsys.readouterr().out


def test_grouping_failure_returns_created_id_without_starting_task(monkeypatch, tmp_path, capsys):
    client, run = setup_create(monkeypatch, tmp_path, ["--section-id", "section"])
    monkeypatch.chdir(tmp_path)
    client.request.side_effect = [
        app.SectionPage(data=[app.Section(id="section", name="Batch")]),
        app.JsonRpcError(-32602, "Section was deleted"),
    ]
    assert app.main() == 1
    run.assert_not_called()
    error = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert error["stage"] == "section.move"
    assert error["thread_id"] == "new-thread"
    assert error["requested_section_id"] == "section"


def test_create_sets_worktree_and_section_before_task(monkeypatch, worktree, capsys):
    client, run = setup_create(
        monkeypatch,
        worktree,
        [
            "--worktree",
            str(worktree),
            "--section-id",
            "section",
        ],
    )
    section = {"id": "section", "name": "Batch"}
    client.request.side_effect = [
        app.SectionPage(data=[app.Section(**section)]),
        app.EmptyResponse(),
    ]
    client.thread_read.return_value = SimpleNamespace(thread=fake_thread(worktree, section))
    ordering = Mock()
    ordering.attach_mock(client, "client")
    ordering.attach_mock(run, "run")
    assert app.main() == 0
    assert client.thread_start.call_args.args[0]["cwd"] == str(worktree)
    calls = [c[0] for c in ordering.mock_calls]
    assert (
        calls.index("client.thread_set_name")
        < calls.index("client.thread_read")
        < calls.index("run")
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    ready = next(e for e in events if e["event"] == "thread.ready")
    assert ready["cwd"] == ready["worktree"] == str(worktree)
    assert ready["section"]["id"] == "section"
    assert ready["section"]["project_id"] is None


def test_wrong_returned_cwd_prevents_task(monkeypatch, tmp_path, worktree):
    client, run = setup_create(monkeypatch, tmp_path, ["--worktree", str(worktree)])
    assert app.main() == 1
    run.assert_not_called()
    client.thread_set_name.assert_not_called()


def test_move_verifies_assignment_and_does_not_resume(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["codex_app.py", "move", "new-thread", "--section-id", "s"])
    client = Mock()
    client.request.side_effect = [
        app.SectionPage(data=[app.Section(id="s", name="Batch")]),
        app.EmptyResponse(),
    ]
    client.thread_read.return_value = SimpleNamespace(
        thread=fake_thread(tmp_path, {"id": "s", "name": "Batch"})
    )
    monkeypatch.setattr(app, "create_client", lambda _: client)
    assert app.main() == 0
    client.thread_resume.assert_not_called()
    client.turn_start.assert_not_called()
    assert json.loads(capsys.readouterr().out)["event"] == "section.moved"


def test_move_does_not_claim_success_if_readback_differs():
    client = Mock()
    client.thread_read.return_value = SimpleNamespace(thread=fake_thread(Path("/repo")))
    with pytest.raises(RuntimeError, match="not confirmed"):
        app.move_section(client, "thread", "s")
