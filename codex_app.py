#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["openai-codex==0.155.1", "websockets==17.1"]
# ///
"""Manage App-visible Codex threads through the official Python SDK."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from openai_codex import CodexError
from openai_codex.client import CodexClient, CodexConfig


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
    return {key: data.get(key) for key in ("id", "name", "source", "cwd", "status")}


def list_threads(client, cwd, sources=None):
    params = {"cwd": str(cwd), "limit": 100}
    if sources:
        params["sourceKinds"] = sources
    while True:
        result = client.thread_list(params)
        yield from result.data
        if not result.next_cursor:
            return
        params["cursor"] = result.next_cursor


def find_visible(client, cwd, thread_id):
    return next((row for row in list_threads(client, cwd) if row.id == thread_id), None)


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


def run_task(client, thread_id, prompt, cwd):
    turn = client.turn_start(thread_id, prompt).turn
    emit("turn.started", thread_id=thread_id, turn_id=turn.id)
    visible = find_visible(client, cwd, thread_id)
    emit("visibility", thread_id=thread_id, listed_by_default=visible is not None)
    completed = wait_for_turn(client, turn.id)
    emit("turn.completed", thread_id=thread_id, status=completed["status"])
    if completed["status"] != "completed":
        raise RuntimeError(f"Turn {completed['status']}: {completed.get('error')}")
    for attempt in range(3):
        visible = find_visible(client, cwd, thread_id)
        if visible:
            emit("visible", thread=summary(visible))
            return
        if attempt < 2:
            time.sleep(1)
    raise RuntimeError(f"Turn completed, but thread {thread_id} is absent from the default list")


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
        help="Project filter / new thread cwd (default: current directory)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List project threads; default App sources only")
    listing.add_argument("--source", action="append", help="Override source filter, e.g. exec")
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
        prompt = command.add_mutually_exclusive_group(required=True)
        prompt.add_argument("--prompt", type=nonempty)
        prompt.add_argument("--prompt-file", type=Path, help="UTF-8 task file; - reads stdin")
    return parser, parser.parse_args()


def main():
    parser, args = parse_args()
    client = None
    try:
        cwd = args.cwd.expanduser().resolve(strict=True)
        if not cwd.is_dir():
            parser.error("--cwd must be a directory")
        prompt = getattr(args, "prompt", None)
        prompt_file = getattr(args, "prompt_file", None)
        if prompt_file:
            prompt = sys.stdin.read() if str(prompt_file) == "-" else prompt_file.read_text("utf-8")
            if not prompt.strip():
                parser.error("Task prompt must not be empty")
        client = create_client(args.socket.expanduser())
        client.start()
        client.initialize()
        if args.command == "list":
            emit("threads", threads=[summary(t) for t in list_threads(client, cwd, args.source)])
        elif args.command == "read":
            emit("thread", thread=summary(client.thread_read(args.thread_id).thread))
        elif args.command == "rename":
            client.thread_set_name(args.thread_id, args.name)
            emit("renamed", thread_id=args.thread_id, name=args.name)
        else:
            if args.command == "create":
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
                result = client.thread_fork(
                    args.thread_id,
                    {
                        "excludeTurns": True,
                        "deferGoalContinuation": True,
                    },
                )
            else:
                result = client.thread_resume(args.thread_id, {"excludeTurns": True})
            thread = result.thread
            emit("thread.ready", thread=summary(thread))
            if args.command != "send":
                client.thread_set_name(thread.id, args.name)
                emit("renamed", thread_id=thread.id, name=args.name)
            run_task(client, thread.id, prompt, Path(summary(thread)["cwd"]))
        return 0
    except KeyboardInterrupt:
        print("Client disconnected. Check or stop the task in the Codex App.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, CodexError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
