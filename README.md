# Codex App tools

Manage Codex threads and server-side section membership through an existing local App Server. This standalone CLI uses the official Python SDK and a small stdio-to-Unix-WebSocket bridge. It provides `list`, `read`, `rename`, `create`, `fork`, `send`, `sections`, and `move`. It does not observe or verify the desktop sidebar.

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

Add this repository to a consuming project:

```bash
git submodule add https://github.com/windyd/codex-app-tools.git tools/codex-app-tools
git commit -m "Add Codex App tools"
uv run --script --locked tools/codex-app-tools/codex_app.py --cwd "$PWD" list
```

After cloning a consuming project, initialize the pinned revision:

```bash
git submodule update --init --recursive
```

A consuming project's Git link pins the tool commit; updating the standalone repository alone does not change consumers. To update a consumer, fetch in the submodule, check out the desired commit, then stage and commit its Git link in the parent repository.

## Thread lifecycle and approvals

New threads use `workspace-write`, `on-request`, and `auto_review`; no model override is supplied. The tool sets a name, sends the first task message, and checks the default thread list. `list --source exec` explicitly includes the otherwise filtered exec source; an `exec` session alone is not proof of App visibility.

Task commands emit JSONL metadata and public replies, and keep the SDK connection open until the turn completes. Initial `visibility` can be false while persistence catches up. The legacy `visible` event means only that the ID appears in the server's default thread list; its `scope` is `server_default_thread_list`. Neither event proves App sidebar placement. For long-running tasks, preserve the client process and its output log. Ctrl-C disconnects the client without explicitly interrupting the remote turn; check the App for its state.

The tool does not automatically accept client-side approval or user-input requests. It reports unsupported interaction requests so they can be handled in the App. The bridge disables WebSocket compression because the tested server rejects negotiation for that extension.

## Existing worktrees and App sections

```bash
uv run --script --locked codex_app.py --cwd /path/to/project sections
uv run --script --locked codex_app.py create \
  --worktree /path/to/existing-worktree \
  --section-id SECTION_ID \
  --name "B01 · GT-14327 · Search refresh" --prompt-file task.md
uv run --script --locked codex_app.py move THREAD_ID --section-id SECTION_ID
uv run --script --locked codex_app.py --cwd /path/to/project list --section-id SECTION_ID
```

`--worktree` accepts an existing, registered Git worktree root, including the main working tree. Missing paths, bare repositories, non-Git directories, and subdirectories of a worktree are rejected before connecting. Symlinks are resolved. The tool neither creates nor deletes worktrees. It sends the resolved path as the actual `thread/start.cwd` and checks the returned cwd before starting a task. Explicit `--cwd` and `--worktree` must resolve to the same directory; otherwise creation fails. When `--cwd` is omitted, `--worktree` replaces the caller-directory default. Relative prompt files still resolve from the caller's directory.

Git versions such as 2.34.1 do not support `worktree list --porcelain -z`. The validator falls back to plain `--porcelain` only for that unsupported-option error; other Git failures remain errors. On that fallback, paths containing newlines are rejected explicitly because the old format is ambiguous. Spaces, Unicode, quotes, and backslashes are supported. Regression tests exercise the fallback against real Git worktrees with the old unsupported-`-z` response simulated; the development machine uses Git 2.47.3.

Sections in the verified App Server protocol are **server-wide, independent of projects**. `sections` lists all sections on the selected server, with `id`, `name`, `scope: "server"`, and `project_id: null`; `--cwd` does not filter them. There is no owning-project field or project/section mismatch to validate in this version. The tool does not invent ownership from member threads. Only IDs are accepted, so duplicate section names are unambiguous. Unknown IDs fail, and groups are never created automatically.

Creation validates the destination section before `thread/start`, emits `thread.created` with the new ID, sets the name, calls `thread/section/move`, and verifies the section through `thread/read` **before** `turn/start`. `thread.ready` reports `thread_id`, actual `cwd`, the selected `worktree`, and `section`. `move` changes membership without resuming or sending a turn and verifies the result. Its `worktree` is resolved from the stored cwd when Git metadata is locally available, otherwise null.

After a create task completes, the tool also checks a fresh `thread/read` and the server's `thread/list` filtered by the requested `sectionId`. It retries these server checks up to three times and fails if either view does not confirm membership. `list --section-id ID` is a read-only diagnostic with both cwd and section filters; it does not inspect the UI.

| JSONL evidence | Meaning |
| --- | --- |
| `thread.ready` / `section.moved`, `server_section_assigned: true` | Server readback confirmed the assignment before a turn / after a move. |
| `visible`, `scope: "server_default_thread_list"` | ID appeared in the server's default list for the cwd; legacy event name retained for compatibility. |
| `section.verification`, `server_section_assigned` and `server_section_listed` | Results of fresh readback and a section-filtered server list after the create task. Both must be true for success. |
| `app_sidebar_verification: "not_performed"` | Desktop rendering was not checked; this is not a claim that UI placement succeeded or failed. |

Exit 0 covers the completed task and these server checks, **not** placement in the App sidebar. Do not treat an intermediate `visible` event as completion: consume the process exit status and section verification results.

Failures exit nonzero and emit a JSONL `error` with `stage`, `thread_id` when known, `cwd`, `worktree`, and `requested_section_id`. A grouping failure leaves the created thread available but sends no prompt. Do not retry `create` blindly: repair with `move THREAD_ID --section-id ...`, then use `send THREAD_ID --prompt-file ...`. If transport fails during `thread.create` before the response arrives, the ID can be unknown; inspect the App/list before retrying. A `task.run` failure may occur after the task has started.

### Verified compatibility and limits

Verified on 2026-09-24 with the existing **Codex Desktop App Server 0.156.1**, protocol schema from **codex-cli 0.156.1** (including experimental fields), **openai-codex 0.155.1**, and **websockets 17.1**. The SDK's public `request` method handles `threadSection/list` and `thread/section/move`, which lack dedicated helpers in this SDK. Missing methods produce an explicit error; the tool never edits Codex databases or restarts the server.

An integration test created a thread in a linked worktree, verified section membership before its first turn, received its reply, found it in the default thread list with matching cwd/Git metadata, and moved it to another existing section. The test thread was subsequently ungrouped and archived. Desktop clicking/rendering was not visually tested. This associates an existing checkout through the server's real cwd; it does not register the checkout in an App-managed worktree lifecycle or promise an App-specific worktree badge. The protocol exposes no separate worktree-registration parameter.

Protocol reference: [Codex App Server](https://developers.openai.com/codex/app-server). To inspect the installed runtime's exact schema without starting another server, run `codex app-server generate-json-schema --experimental --out /path/to/schema`.

### Known unresolved SSH Remote sidebar mismatch

A user of b85b3d3 reported that an externally created thread remained under its project in the App and was absent from the requested section even after re-expanding it. Independent server readbacks still showed that section. The App connected over SSH to a remote host; CLI and App Server were 0.156.1, while the desktop client version was not supplied. This is a reported server/UI mismatch, not a confirmed failure of the move RPC. Its root cause and a UI fix remain unverified.

The local 0.156.1 protocol schema has no dedicated section-change notification. In a separate two-SDK-client check on 0.156.1, the observer received a name-change notification, but no target-thread notification in the three seconds after section movement; explicit readback and a section-filtered query both succeeded. This bounded observation is a synchronization lead, not proof of how the SSH desktop client behaves. No database edits, fabricated notifications, or server restarts are used as a workaround. To investigate further, collect the actual desktop version, compare a manual App move on the same host, and verify both server filtering and the complete sidebar after reconnecting.

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
