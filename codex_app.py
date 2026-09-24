#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["openai-codex==0.155.1", "websockets==17.1"]
# ///
"""Manage App-visible Codex threads through the official Python SDK."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from openai_codex import CodexError
from openai_codex.client import CodexClient, CodexConfig
from openai_codex.errors import JsonRpcError
from pydantic import BaseModel, ConfigDict, Field


class Section(BaseModel):
    id: str
    name: str


class SectionPage(BaseModel):
    data: list[Section]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class EmptyResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class ExplicitCwd(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        namespace.cwd = values
        namespace.cwd_explicit = True


class GitCommandError(ValueError):
    def __init__(self, cwd, result):
        self.returncode = result.returncode
        self.stderr = result.stderr
        super().__init__(f"Invalid Git worktree {cwd}: {result.stderr.strip()}")


def git_output(cwd, *args):
    # Do not let inherited Git overrides validate an unrelated repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["LC_ALL"] = "C"
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env, timeout=15
    )
    if result.returncode:
        raise GitCommandError(cwd, result)
    return result.stdout


def validate_worktree(path):
    path = path.expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("--worktree must be a directory")
    root = Path(git_output(path, "rev-parse", "--show-toplevel").removesuffix("\n")).resolve()
    if root != path:
        raise ValueError("--worktree must name the worktree root, not a subdirectory")
    try:
        entries = git_output(path, "worktree", "list", "--porcelain", "-z").split("\0")
    except GitCommandError as error:
        if error.returncode != 129 or not re.search(
            r"unknown (?:switch|option) [`'\"]z['\"]", error.stderr
        ):
            raise
        # Git 2.34 has no -z. Its worktree paths are raw, not C-quoted, so
        # newline-bearing paths cannot be safely validated by splitting lines.
        if "\n" in str(path) or "\r" in str(path):
            raise ValueError(
                "Newline-bearing worktree paths require Git with list -z support"
            ) from error
        entries = git_output(path, "worktree", "list", "--porcelain").split("\n")
    registered = [Path(e[9:]).resolve() for e in entries if e.startswith("worktree ")]
    if path not in registered:
        raise ValueError(f"Not a registered Git worktree: {path}")
    return path


def worktree_for_cwd(cwd):
    """Report local Git association when available; threads may also use non-Git cwd."""
    try:
        root = Path(git_output(cwd, "rev-parse", "--show-toplevel").removesuffix("\n"))
        return str(validate_worktree(root))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def list_sections(client):
    params = {"limit": 100}
    cursors = set()
    while True:
        try:
            page = client.request("threadSection/list", params, response_model=SectionPage)
        except JsonRpcError as error:
            if error.code == -32601:
                raise RuntimeError(
                    "This App Server does not support threadSection/list; "
                    "section operations require a compatible server. No database fallback."
                ) from error
            raise
        yield from page.data
        if not page.next_cursor:
            return
        if page.next_cursor in cursors:
            raise RuntimeError("Section pagination returned a repeated cursor")
        cursors.add(page.next_cursor)
        params = {**params, "cursor": page.next_cursor}


def section_details(section):
    # 0.156.1 sections are server-wide, with no owning project in the protocol.
    return {"id": section.id, "name": section.name, "scope": "server", "project_id": None}


def resolve_section(client, section_id):
    section = next((s for s in list_sections(client) if s.id == section_id), None)
    if section is None:
        raise ValueError(f"Section does not exist on this App Server: {section_id}")
    return section


def move_section(client, thread_id, section_id):
    try:
        client.request(
            "thread/section/move",
            {"threadId": thread_id, "sectionId": section_id},
            response_model=EmptyResponse,
        )
    except JsonRpcError as error:
        if error.code == -32601:
            raise RuntimeError("This App Server does not support thread/section/move") from error
        raise
    thread = client.thread_read(thread_id).thread
    actual = summary(thread).get("section")
    if not actual or actual.get("id") != section_id:
        raise RuntimeError(f"Section assignment was not confirmed for thread {thread_id}")
    return thread


def reject_client_request(method, params):
    """Let server-side auto review decide; never blanket-accept a client request."""
    raise RuntimeError(f"Client interaction {method} requires attention in the Codex App")


def create_client(socket_path):
    return CodexClient(
        CodexConfig(
            launch_args_override=(
                sys.executable,
                str(Path(__file__).with_name("codex_app_bridge.py")),
                str(socket_path),
            ),
            client_name="codex_app_tools",
            client_title="Codex App tools",
        ),
        approval_handler=reject_client_request,
    )


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def summary(thread):
    data = thread.model_dump(by_alias=True, mode="json")
    return {
        key: data.get(key)
        for key in ("id", "name", "source", "cwd", "status", "gitInfo", "projectId", "section")
    }


def list_threads(client, cwd, sources=None, section_id=None):
    params = {"limit": 100}
    if cwd is not None:
        params["cwd"] = str(cwd)
    if sources:
        params["sourceKinds"] = sources
    if section_id is not None:
        params["sectionId"] = section_id
    while True:
        result = client.thread_list(params)
        yield from result.data
        if not result.next_cursor:
            return
        params["cursor"] = result.next_cursor


def find_visible(client, cwd, thread_id):
    return next((row for row in list_threads(client, cwd) if row.id == thread_id), None)


def verify_section_listing(client, thread_id, section_id):
    """Check two server views, never claim the desktop sidebar was observed."""
    actual = summary(client.thread_read(thread_id).thread).get("section")
    assigned = bool(actual and actual.get("id") == section_id)
    listed = next(
        (row for row in list_threads(client, None, section_id=section_id) if row.id == thread_id),
        None,
    )
    listed_section = summary(listed).get("section") if listed is not None else None
    in_section = bool(listed_section and listed_section.get("id") == section_id)
    emit(
        "section.verification",
        thread_id=thread_id,
        section_id=section_id,
        server_section_assigned=assigned,
        server_section_listed=in_section,
        app_sidebar_verification="not_performed",
    )
    return assigned and in_section


def wait_for_turn(client, turn_id):
    try:
        while True:
            event = client.next_turn_notification(turn_id)
            if event.method not in {"item/completed", "turn/completed"}:
                continue
            params = event.payload.model_dump(by_alias=True, mode="json")
            if event.method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    emit("message", text=item.get("text", ""))
            if event.method == "turn/completed" and params["turn"]["id"] == turn_id:
                return params["turn"]
    finally:
        client.unregister_turn_notifications(turn_id)


def run_task(client, thread_id, prompt, cwd, section_id=None):
    turn = client.turn_start(thread_id, prompt).turn
    emit("turn.started", thread_id=thread_id, turn_id=turn.id)
    visible = find_visible(client, cwd, thread_id)
    emit(
        "visibility",
        thread_id=thread_id,
        listed_by_default=visible is not None,
        scope="server_default_thread_list",
        app_sidebar_verification="not_performed",
    )
    completed = wait_for_turn(client, turn.id)
    emit("turn.completed", thread_id=thread_id, status=completed["status"])
    if completed["status"] != "completed":
        raise RuntimeError(f"Turn {completed['status']}: {completed.get('error')}")
    for attempt in range(3):
        visible = find_visible(client, cwd, thread_id)
        if visible:
            emit(
                "visible",
                thread_id=thread_id,
                thread=summary(visible),
                scope="server_default_thread_list",
                listed_by_default=True,
                app_sidebar_verification="not_performed",
            )
            if section_id is None or verify_section_listing(client, thread_id, section_id):
                return
        if attempt < 2:
            time.sleep(1)
    raise RuntimeError(
        f"Turn completed, but thread {thread_id} was not confirmed in the default list"
        + (f" and requested server section {section_id}" if section_id else "")
        + "; the task has already run. App sidebar verification was not performed."
    )


def nonempty(value):
    if not value.strip():
        raise argparse.ArgumentTypeError("Value must not be empty")
    return value


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    codex_dir = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser.add_argument(
        "--socket", type=Path, default=codex_dir / "app-server-control/app-server-control.sock"
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd(),
        action=ExplicitCwd,
        help="Project filter / new thread cwd (default: current directory)",
    )
    parser.set_defaults(cwd_explicit=False)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List project threads; default App sources only")
    listing.add_argument("--source", action="append", help="Override source filter, e.g. exec")
    listing.add_argument("--section-id", type=nonempty, help="Filter the server list by section ID")
    commands.add_parser("sections", help="List server-wide App sections (not project-scoped)")
    move = commands.add_parser("move", help="Move an existing thread to an existing App section")
    move.add_argument("thread_id")
    move.add_argument("--section-id", type=nonempty, required=True)
    commands.add_parser("read", help="Read thread metadata without resuming").add_argument(
        "thread_id"
    )
    rename = commands.add_parser("rename", help="Set a user-facing name")
    rename.add_argument("thread_id")
    rename.add_argument("name", type=nonempty)
    for name in ("create", "fork", "send"):
        command = commands.add_parser(name, help="Start a task and stay connected until it ends")
        if name != "create":
            command.add_argument("thread_id")
        if name != "send":
            command.add_argument("--name", type=nonempty, required=True)
        if name == "create":
            command.add_argument("--worktree", type=Path, help="Existing registered worktree root")
            command.add_argument(
                "--section-id", type=nonempty, help="Existing server-wide section ID"
            )
        prompt = command.add_mutually_exclusive_group(required=True)
        prompt.add_argument("--prompt", type=nonempty)
        prompt.add_argument("--prompt-file", type=Path, help="UTF-8 task file; - reads stdin")
    return parser, parser.parse_args()


def main():
    parser, args = parse_args()
    client = None
    thread_id = getattr(args, "thread_id", None)
    actual_cwd = None
    worktree = None
    section = None
    stage = "validate"
    try:
        if getattr(args, "worktree", None) is not None:
            worktree = validate_worktree(args.worktree)
            if args.cwd_explicit and args.cwd.expanduser().resolve(strict=True) != worktree:
                raise ValueError("--cwd and --worktree must resolve to the same directory")
        cwd = worktree or args.cwd.expanduser().resolve(strict=True)
        if not cwd.is_dir():
            parser.error("--cwd must be a directory")
        prompt = getattr(args, "prompt", None)
        prompt_file = getattr(args, "prompt_file", None)
        if prompt_file:
            prompt = sys.stdin.read() if str(prompt_file) == "-" else prompt_file.read_text("utf-8")
            if not prompt.strip():
                parser.error("Task prompt must not be empty")
        stage = "connect"
        client = create_client(args.socket.expanduser())
        client.start()
        client.initialize()
        if getattr(args, "section_id", None):
            stage = "section.validate"
            section = resolve_section(client, args.section_id)
        if args.command == "list":
            emit(
                "threads",
                scope="server_thread_list",
                section_id=args.section_id,
                app_sidebar_verification="not_performed",
                threads=[
                    summary(t) for t in list_threads(client, cwd, args.source, args.section_id)
                ],
            )
        elif args.command == "sections":
            stage = "section.list"
            emit(
                "sections",
                scope="server",
                project_id=None,
                sections=[section_details(s) for s in list_sections(client)],
            )
        elif args.command == "move":
            thread_id = args.thread_id
            stage = "section.move"
            thread = move_section(client, thread_id, section.id)
            actual_cwd = summary(thread)["cwd"]
            emit(
                "section.moved",
                thread_id=thread_id,
                thread=summary(thread),
                cwd=summary(thread)["cwd"],
                worktree=worktree_for_cwd(actual_cwd),
                section=section_details(section),
                server_section_assigned=True,
                app_sidebar_verification="not_performed",
            )
        elif args.command == "read":
            emit("thread", thread=summary(client.thread_read(args.thread_id).thread))
        elif args.command == "rename":
            client.thread_set_name(args.thread_id, args.name)
            emit("renamed", thread_id=args.thread_id, name=args.name)
        else:
            if args.command == "create":
                stage = "thread.create"
                result = client.thread_start(
                    {
                        "cwd": str(cwd),
                        "sandbox": "workspace-write",
                        "approvalPolicy": "on-request",
                        "approvalsReviewer": "auto_review",
                        "ephemeral": False,
                    }
                )
            elif args.command == "fork":
                stage = "thread.fork"
                result = client.thread_fork(
                    args.thread_id,
                    {
                        "excludeTurns": True,
                        "deferGoalContinuation": True,
                    },
                )
            else:
                stage = "thread.resume"
                result = client.thread_resume(args.thread_id, {"excludeTurns": True})
            thread = result.thread
            thread_id = thread.id
            actual_cwd = summary(thread)["cwd"]
            emit(
                "thread.created" if args.command == "create" else "thread.loaded",
                thread_id=thread_id,
                cwd=summary(thread)["cwd"],
                worktree=str(worktree) if worktree else None,
                requested_section_id=getattr(args, "section_id", None),
                thread=summary(thread),
            )
            stage = "cwd.verify"
            if args.command == "create" and Path(summary(thread)["cwd"]).resolve() != cwd:
                raise RuntimeError("App Server returned a different cwd; task was not started")
            if args.command != "send":
                stage = "thread.rename"
                client.thread_set_name(thread.id, args.name)
                emit("renamed", thread_id=thread.id, name=args.name)
            if section:
                stage = "section.move"
                thread = move_section(client, thread_id, section.id)
                if Path(summary(thread)["cwd"]).resolve() != cwd:
                    raise RuntimeError("Thread cwd changed during section assignment")
            emit(
                "thread.ready",
                thread_id=thread_id,
                thread=summary(thread),
                cwd=summary(thread)["cwd"],
                worktree=str(worktree) if worktree else None,
                section=section_details(section) if section else summary(thread)["section"],
                server_section_assigned=True if section else None,
                app_sidebar_verification="not_performed",
            )
            stage = "task.run"
            options = {"section_id": section.id} if section else {}
            run_task(client, thread.id, prompt, Path(summary(thread)["cwd"]), **options)
        return 0
    except KeyboardInterrupt:
        print("Client disconnected. Check or stop the task in the Codex App.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, CodexError, subprocess.TimeoutExpired) as error:
        emit(
            "error",
            stage=stage,
            thread_id=thread_id,
            cwd=actual_cwd,
            requested_section_id=getattr(args, "section_id", None),
            worktree=str(worktree) if worktree else None,
            message=str(error),
        )
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
