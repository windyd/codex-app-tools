# Codex App tools

Manage App-visible Codex threads through an existing local App Server. This standalone CLI uses the official Python SDK and a small stdio-to-Unix-WebSocket bridge. It provides `list`, `read`, `rename`, `create`, `fork`, and `send`.

## Requirements and execution

- Linux or another environment supporting Unix sockets, Python 3.12, and `uv`.
- An existing Codex App Server and access to its control socket.
- Dependencies are pinned by PEP 723 metadata and `codex_app.py.lock`: `openai-codex==0.155.1`, `websockets==17.1`. They are installed in uv's script environment, separate from any consuming project's environment.

Run from the project you want to operate on, or provide an explicit project directory:

```bash
uv run --script --locked /path/to/codex-app-tools/codex_app.py list
uv run --script --locked /path/to/codex-app-tools/codex_app.py --cwd /path/to/project list
uv run --script --locked /path/to/codex-app-tools/codex_app.py --cwd /path/to/project read THREAD_ID
uv run --script --locked /path/to/codex-app-tools/codex_app.py --cwd /path/to/project rename THREAD_ID "Task name"
uv run --script --locked /path/to/codex-app-tools/codex_app.py --cwd /path/to/project create --name "New task" --prompt-file /path/to/task.md
uv run --script --locked /path/to/codex-app-tools/codex_app.py fork THREAD_ID --name "Follow-up" --prompt "Task instructions"
uv run --script --locked /path/to/codex-app-tools/codex_app.py send THREAD_ID --prompt-file /path/to/followup.md
```

`--cwd` defaults to the caller's current directory, never this tool repository. For `list`, it filters project threads; for `create`, it sets the new thread directory. `fork` and `send` preserve the existing thread's project. `read` and `rename` target the supplied ID directly. Global options go before the subcommand. Relative prompt-file paths are resolved from the caller's directory; `--prompt-file -` reads stdin.

`--socket` overrides the default `$CODEX_HOME/app-server-control/app-server-control.sock`, or `~/.codex/app-server-control/app-server-control.sock` when `CODEX_HOME` is unset. The tool connects to that server; it does not start a replacement server.

## Use as a submodule

For a shared Git remote:

```bash
git submodule add <repository-url> tools/codex-app-tools
git commit -m "Add Codex App tools"
uv run --script --locked tools/codex-app-tools/codex_app.py --cwd "$PWD" list
```

For same-host use before a remote is configured, the initial local source is `/home/kevin/Project/codex-app-tools`:

```bash
git -c protocol.file.allow=always submodule add /home/kevin/Project/codex-app-tools tools/codex-app-tools
```

Other machines need a reachable remote URL; the local path is not portable across hosts. Set a remote and publish the standalone repository before switching consuming projects with `git submodule set-url tools/codex-app-tools <repository-url>`.

After cloning a consuming project, initialize the pinned revision:

```bash
git submodule update --init --recursive
```

For a trusted local-path source, add `-c protocol.file.allow=always` to that Git command. This is a per-command setting. A consuming project's Git link pins the tool commit; updating the standalone repository alone does not change consumers. To update a consumer, fetch in the submodule, check out the desired commit, then stage and commit its Git link in the parent repository.

## Thread lifecycle and approvals

New threads use `workspace-write`, `on-request`, and `auto_review`; no model override is supplied. The tool sets a name, sends the first task message, and checks the default thread list. `list --source exec` explicitly includes the otherwise filtered exec source; an `exec` session alone is not proof of App visibility.

Task commands emit JSONL metadata and public replies, and keep the SDK connection open until the turn completes. Initial `visibility` can be false while persistence catches up; successful completion includes a `visible` event. For long-running tasks, preserve the client process and its output log. Ctrl-C disconnects the client without explicitly interrupting the remote turn; check the App for its state.

The tool does not automatically accept client-side approval or user-input requests. It reports unsupported interaction requests so they can be handled in the App. The bridge disables WebSocket compression because the tested server rejects negotiation for that extension.

## Development and validation

From this repository:

```bash
uv run --no-project --with openai-codex==0.155.1 --with websockets==17.1 --with pytest --default-index https://pypi.org/simple python -m pytest tests -q
uv run --script --locked codex_app.py --help
uv run --script --locked codex_app.py --cwd /path/to/project list
```

Unit tests use a fake SDK client and never create real threads. The final command is a read-only smoke check against the configured local server. Test dependencies are separate from the locked runtime environment.

## Origin

Extracted from JevRepro's `scripts/codex_app.py`, bridge, script lock, and adapter tests, originally added in commit `b23f988`. Project-specific experiment policy and Obsidian notes remain in the consuming repository. SDK and transport behavior were retained; the default working directory and client identity were made project-independent.
