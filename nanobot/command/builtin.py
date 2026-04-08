"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import sys

from nanobot import __version__
from nanobot.agent.codex_proxy import CodexProxy, watch_until_idle
from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.utils.helpers import build_status_content
from nanobot.utils.restart import set_restart_notice_to_env


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    tasks = loop._active_tasks.pop(msg.session_key, [])
    cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    sub_cancelled = await loop.subagents.cancel_by_session(msg.session_key)
    total = cancelled + sub_cancelled
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg
    set_restart_notice_to_env(channel=msg.channel, chat_id=msg.chat_id)

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    ctx_est = 0
    try:
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(session)
    except Exception:
        pass
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)
    
    # Fetch web search provider usage (best-effort, never blocks the response)
    search_usage_text: str | None = None
    try:
        from nanobot.utils.searchusage import fetch_search_usage
        web_cfg = getattr(loop, "web_config", None)
        search_cfg = getattr(web_cfg, "search", None) if web_cfg else None
        if search_cfg is not None:
            provider = getattr(search_cfg, "provider", "duckduckgo")
            api_key = getattr(search_cfg, "api_key", "") or None
            usage = await fetch_search_usage(provider=provider, api_key=api_key)
            search_usage_text = usage.format()
    except Exception:
        pass  # Never let usage fetch break /status
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=loop.model,
            start_time=loop._start_time, last_usage=loop._last_usage,
            context_window_tokens=loop.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
            search_usage_text=search_usage_text,
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Start a fresh session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    snapshot = session.messages[session.last_consolidated:]
    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    if snapshot:
        loop._schedule_background(loop.consolidator.archive(snapshot))
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata=dict(ctx.msg.metadata or {})
    )


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    import time

    loop = ctx.loop
    msg = ctx.msg

    async def _run_dream():
        t0 = time.monotonic()
        try:
            did_work = await loop.dream.run()
            elapsed = time.monotonic() - t0
            if did_work:
                content = f"Dream completed in {elapsed:.1f}s."
            else:
                content = "Dream: nothing to process."
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"Dream failed after {elapsed:.1f}s: {e}"
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Dreaming...",
    )


def _extract_changed_files(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


def _format_dream_log_content(commit, diff: str, *, requested_sha: str | None = None) -> str:
    files_line = _format_changed_files(diff)
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {files_line}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff to display.",
        ])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.sha}` {c.timestamp} - {c.message.splitlines()[0]}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


async def cmd_dream_log(ctx: CommandContext) -> OutboundMessage:
    """Show what the last Dream changed.

    Default: diff of the latest commit (HEAD~1 vs HEAD).
    With /dream-log <sha>: diff of that specific commit.
    """
    store = ctx.loop.consolidator.store
    git = store.git

    if not git.is_initialized():
        if store.get_last_dream_cursor() == 0:
            msg = "Dream has not run yet. Run `/dream`, or wait for the next scheduled Dream cycle."
        else:
            msg = "Dream history is not available because memory versioning is not initialized."
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=msg, metadata={"render_as": "text"},
        )

    args = ctx.args.strip()

    if args:
        # Show diff of a specific commit
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        if not result:
            content = (
                f"Couldn't find Dream change `{sha}`.\n\n"
                "Use `/dream-restore` to list recent versions, "
                "or `/dream-log` to inspect the latest one."
            )
        else:
            commit, diff = result
            content = _format_dream_log_content(commit, diff, requested_sha=sha)
    else:
        # Default: show the latest commit's diff
        commits = git.log(max_entries=1)
        result = git.show_commit_diff(commits[0].sha) if commits else None
        if result:
            commit, diff = result
            content = _format_dream_log_content(commit, diff)
        else:
            content = "Dream memory has no saved versions yet."

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_dream_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore memory files from a previous dream commit.

    Usage:
        /dream-restore          — list recent commits
        /dream-restore <sha>    — revert a specific commit
    """
    store = ctx.loop.consolidator.store
    git = store.git
    if not git.is_initialized():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Dream history is not available because memory versioning is not initialized.",
        )

    args = ctx.args.strip()
    if not args:
        # Show recent commits for the user to pick
        commits = git.log(max_entries=10)
        if not commits:
            content = "Dream memory has no saved versions to restore yet."
        else:
            content = _format_dream_restore_list(commits)
    else:
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        changed_files = _format_changed_files(result[1]) if result else "the tracked memory files"
        new_sha = git.revert(sha)
        if new_sha:
            content = (
                f"Restored Dream memory to the state before `{sha}`.\n\n"
                f"- New safety commit: `{new_sha}`\n"
                f"- Restored files: {changed_files}\n\n"
                f"Use `/dream-log {new_sha}` to inspect the restore diff."
            )
        else:
            content = (
                f"Couldn't restore Dream change `{sha}`.\n\n"
                "It may not exist, or it may be the first saved version with no earlier state to restore."
            )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


# ------------------------------------------------------------------
# /codex — Codex CLI proxy mode
# ------------------------------------------------------------------

async def cmd_codex(ctx: CommandContext) -> OutboundMessage:
    """Manage Codex CLI proxy sessions.

    /codex               — start Codex mode (workspace = bot workspace)
    /codex start [path]  — start with explicit workspace
    /codex run <prompt>  — run a one-shot task in background, notify when done
    /codex exit          — detach from Codex mode (session keeps running)
    /codex kill [name]    — kill a session by name (or current if no name)
    /codex kill all      — kill all Codex sessions
    /codex list          — list alive Codex tmux sessions
    /codex peek [name]   — snapshot a session without entering it
    /codex resume <name> — resume an existing Codex tmux session
    """
    msg = ctx.msg
    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)
    args = (ctx.args or "").strip()
    meta = dict(msg.metadata or {})

    sub = args.split()[0].lower() if args else ""

    if sub == "exit":
        return await _codex_exit(msg, session, ctx.loop, meta)
    if sub == "kill":
        kill_target = args.split()[1] if len(args.split()) > 1 else ""
        return await _codex_kill(msg, session, ctx.loop, kill_target, meta)
    if sub == "list":
        return await _codex_list(msg, meta)
    if sub == "resume":
        name = args.split()[1] if len(args.split()) > 1 else ""
        return await _codex_resume(msg, session, ctx.loop, name, meta)
    if sub == "peek":
        name = args.split()[1] if len(args.split()) > 1 else ""
        return await _codex_peek(msg, session, name, meta)
    if sub == "run":
        prompt = args[len("run"):].strip()
        return await _codex_run(msg, session, ctx.loop, prompt, meta)

    # /codex or /codex start [path]
    workspace_arg = ""
    if sub == "start":
        workspace_arg = args[len("start"):].strip()
    elif sub and sub != "start":
        # /codex <something unknown> — treat as /codex start with that path
        workspace_arg = args

    return await _codex_start(msg, session, ctx.loop, workspace_arg, meta)


async def _codex_start(msg, session, loop, workspace_arg: str, meta: dict) -> OutboundMessage:
    if session.metadata.get("codex_proxy"):
        proxy = CodexProxy.from_dict(session.metadata["codex_proxy"])
        if await proxy.is_alive():
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=f"Codex mode is already active (session: `{proxy.tmux_session}`). "
                        f"Use `/codex exit` to leave first.",
                metadata=meta,
            )
        # Dead session — clean up metadata.
        session.metadata.pop("codex_proxy", None)

    workspace = workspace_arg or str(loop.workspace)

    # Notify the user that startup may take a moment.
    progress_meta = {**meta, "_progress": True}
    await loop.bus.publish_outbound(OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=f"Starting Codex in `{workspace}`...",
        metadata=progress_meta,
    ))

    try:
        proxy = await CodexProxy.start(workspace=workspace)
    except Exception as e:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=f"Failed to start Codex: {e}",
            metadata=meta,
        )

    session.metadata["codex_proxy"] = proxy.to_dict()
    loop.sessions.save(session)

    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=f"Codex mode activated.\n"
                f"- Session: `{proxy.tmux_session}`\n"
                f"- Workspace: `{workspace}`\n\n"
                f"All your messages will now be sent to Codex. "
                f"Use `/codex exit` to detach (keeps running) or `/codex kill` to terminate.",
        metadata=meta,
    )


async def _codex_run(msg, session, loop, prompt: str, meta: dict) -> OutboundMessage:
    """Start Codex, send a one-shot prompt, and detach with a background watcher."""
    if not prompt:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Usage: `/codex run <prompt>`",
            metadata=meta,
        )

    workspace = str(loop.workspace)

    progress_meta = {**meta, "_progress": True}
    await loop.bus.publish_outbound(OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=f"Starting Codex in `{workspace}`...",
        metadata=progress_meta,
    ))

    try:
        proxy = await CodexProxy.start(workspace=workspace)
    except Exception as e:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=f"Failed to start Codex: {e}",
            metadata=meta,
        )

    # Fire-and-forget: send prompt without waiting for response.
    await proxy.send_nowait(prompt)

    # Brief pause so codex starts processing before the watcher checks.
    await asyncio.sleep(1)

    # Start background watcher — will notify when codex finishes.
    task = asyncio.create_task(
        watch_until_idle(proxy, loop.bus, msg.channel, msg.chat_id)
    )
    loop._codex_watchers[proxy.tmux_session] = task
    loop._background_tasks.append(task)

    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=(
            f"Codex task started.\n"
            f"- Session: `{proxy.tmux_session}`\n"
            f"- Prompt: {prompt[:80]}{'...' if len(prompt) > 80 else ''}\n\n"
            f"Will notify when done. "
            f"Use `/codex resume {proxy.tmux_session}` to check progress."
        ),
        metadata=meta,
    )


async def cmd_codex_exit_priority(ctx: CommandContext) -> OutboundMessage:
    """Priority /codex exit — cancels any blocked codex passthrough and detaches immediately."""
    loop = ctx.loop
    msg = ctx.msg
    meta = dict(msg.metadata or {})

    # Cancel active session tasks to unblock the session lock (same as /stop).
    tasks = list(loop._active_tasks.get(msg.session_key, []))
    for t in tasks:
        if not t.done():
            t.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=2.0)

    # Access the session directly (lock is now free after cancellation).
    session = loop.sessions.get_or_create(msg.session_key)
    proxy_data = session.metadata.pop("codex_proxy", None)
    loop.sessions.save(session)

    if not proxy_data:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Not in Codex mode.", metadata=meta,
        )

    proxy = CodexProxy.from_dict(proxy_data)

    # Start background watcher if session is still alive.
    watcher_note = ""
    if await proxy.is_alive():
        if existing := loop._codex_watchers.pop(proxy.tmux_session, None):
            existing.cancel()
        task = asyncio.create_task(
            watch_until_idle(proxy, loop.bus, msg.channel, msg.chat_id)
        )
        loop._codex_watchers[proxy.tmux_session] = task
        loop._background_tasks.append(task)
        watcher_note = "\nwill be notified when it's done."

    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=(
            f"Detached from Codex session `{proxy.tmux_session}`（still running）。"
            f"{watcher_note}\n"
            f"use `/codex resume {proxy.tmux_session}` to continue。"
        ),
        metadata=meta,
    )


async def _codex_exit(msg, session, loop, meta: dict) -> OutboundMessage:
    """Detach from Codex mode — session keeps running in the background."""
    proxy_data = session.metadata.pop("codex_proxy", None)
    if not proxy_data:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Not in Codex mode.", metadata=meta,
        )

    proxy = CodexProxy.from_dict(proxy_data)
    loop.sessions.save(session)

    # If Codex is still running, start a background watcher that notifies when done.
    watcher_note = ""
    if await proxy.is_alive():
        # Cancel any existing watcher for this session (shouldn't happen, but be safe).
        if existing := loop._codex_watchers.pop(proxy.tmux_session, None):
            existing.cancel()

        import asyncio
        task = asyncio.create_task(
            watch_until_idle(proxy, loop.bus, msg.channel, msg.chat_id)
        )
        loop._codex_watchers[proxy.tmux_session] = task
        loop._background_tasks.append(task)

    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=(
            f"Detached from Codex session `{proxy.tmux_session}`（still running）。"
            f"use `/codex resume {proxy.tmux_session}` to continue。"
        ),
        metadata=meta,
    )


async def _codex_kill(msg, session, loop, target: str, meta: dict) -> OutboundMessage:
    """Kill a Codex session by name, or all sessions, or the currently attached one."""
    from nanobot.agent.codex_proxy import _get_socket_dir

    socket = os.path.join(_get_socket_dir(), "nanobot.sock")

    async def _kill_proxy(proxy: CodexProxy, sess_key: str | None = None) -> None:
        """Kill a proxy and cancel its watcher. Detaches session if attached."""
        if existing := loop._codex_watchers.pop(proxy.tmux_session, None):
            existing.cancel()
        if await proxy.is_alive():
            await proxy.kill()
        if sess_key:
            s = loop.sessions.get_or_create(sess_key)
            if s.metadata.get("codex_proxy", {}).get("tmux_session") == proxy.tmux_session:
                s.metadata.pop("codex_proxy", None)
                loop.sessions.save(s)

    if target == "all":
        sessions = await CodexProxy.list_sessions()
        if not sessions:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="No active Codex sessions.", metadata=meta,
            )
        for s in sessions:
            proxy = CodexProxy(socket=socket, tmux_session=s["name"], workspace="")
            await _kill_proxy(proxy)
        # Also clear current session's codex_proxy if any.
        session.metadata.pop("codex_proxy", None)
        loop.sessions.save(session)
        names = ", ".join(f"`{s['name']}`" for s in sessions)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=f"Terminated {len(sessions)} Codex session(s): {names}.",
            metadata=meta,
        )

    if target:
        # Kill by explicit name.
        proxy = CodexProxy(socket=socket, tmux_session=target, workspace="")
        if not await proxy.is_alive():
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=f"Session `{target}` not found. Use `/codex list` to see running sessions.",
                metadata=meta,
            )
        await _kill_proxy(proxy, sess_key=msg.session_key)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=f"Codex session `{target}` terminated.",
            metadata=meta,
        )

    # No target — kill the currently attached session.
    proxy_data = session.metadata.pop("codex_proxy", None)
    if not proxy_data:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Not in Codex mode and no session name given.\n"
                    "Usage: `/codex kill <name>` or `/codex kill all`",
            metadata=meta,
        )

    proxy = CodexProxy.from_dict(proxy_data)
    await _kill_proxy(proxy)
    loop.sessions.save(session)
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=f"Codex session `{proxy.tmux_session}` terminated.",
        metadata=meta,
    )


async def _codex_list(msg, meta: dict) -> OutboundMessage:
    sessions = await CodexProxy.list_sessions()
    if not sessions:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="No active Codex sessions.", metadata=meta,
        )
    lines = ["Active Codex sessions:"]
    for s in sessions:
        lines.append(f"- `{s['name']}` (created: {s['created']})")
    lines.append("\nUse `/codex resume <name>` to resume one.")
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content="\n".join(lines), metadata=meta,
    )


async def _codex_peek(msg, session, name: str, meta: dict) -> OutboundMessage:
    """Capture a snapshot of a Codex session without entering it."""
    from nanobot.agent.codex_proxy import _get_socket_dir, _strip_ansi

    # Resolve which session to peek at.
    if not name:
        proxy_data = session.metadata.get("codex_proxy")
        if proxy_data:
            name = proxy_data.get("tmux_session", "")
    if not name:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Usage: `/codex peek <session-name>`\n"
                    "Use `/codex list` to see available sessions.",
            metadata=meta,
        )

    socket = os.path.join(_get_socket_dir(), "nanobot.sock")
    proxy = CodexProxy(socket=socket, tmux_session=name, workspace="")

    if not await proxy.is_alive():
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=f"Session `{name}` not found. Use `/codex list` to see available sessions.",
            metadata=meta,
        )

    pane = await proxy._capture_pane(lines=200)
    lines = _strip_ansi(pane).splitlines()
    # Strip status bar (last line) and blank lines.
    content_lines = [l for l in lines[:-1] if l.strip()]
    snapshot = "\n".join(content_lines[-40:])

    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=f"📸 `{name}` snapshot:\n\n{snapshot}" if snapshot else f"`{name}` pane is empty.",
        metadata=meta,
    )


async def _codex_resume(msg, session, loop, name: str, meta: dict) -> OutboundMessage:
    if not name:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Usage: `/codex resume <session-name>`\n"
                    "Use `/codex list` to see available sessions.",
            metadata=meta,
        )

    if session.metadata.get("codex_proxy"):
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Already in Codex mode. Use `/codex exit` to detach first.",
            metadata=meta,
        )

    # Verify the session exists.
    sessions = await CodexProxy.list_sessions()
    match = next((s for s in sessions if s["name"] == name), None)
    if not match:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content=f"Session `{name}` not found. Use `/codex list` to see available sessions.",
            metadata=meta,
        )

    from nanobot.agent.codex_proxy import _get_socket_dir
    import os
    socket = os.path.join(_get_socket_dir(), "nanobot.sock")
    proxy = CodexProxy(socket=socket, tmux_session=name, workspace=str(loop.workspace))
    session.metadata["codex_proxy"] = proxy.to_dict()
    loop.sessions.save(session)

    # Cancel any background watcher for this session.
    if watcher := loop._codex_watchers.pop(name, None):
        watcher.cancel()

    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=f"Resumed Codex session `{name}`. All messages will be sent to Codex.\n"
                f"Use `/codex exit` to detach or `/codex kill` to terminate.",
        metadata=meta,
    )


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = [
        "🐈 nanobot commands:",
        "/new — Start a new conversation",
        "/stop — Stop the current task",
        "/restart — Restart the bot",
        "/status — Show bot status",
        "/codex — Enter Codex CLI proxy mode",
        "/codex run <prompt> — Run a one-shot task in background",
        "/codex exit — Detach from Codex (session keeps running)",
        "/codex kill [name] — Kill a session by name (or current if attached)",
        "/codex kill all — Kill all active Codex sessions",
        "/codex list — List active Codex sessions",
        "/codex peek [name] — Snapshot a session without entering it",
        "/codex resume <name> — Resume a Codex session",
        "/dream — Manually trigger Dream consolidation",
        "/dream-log — Show what the last Dream changed",
        "/dream-restore — Revert memory to a previous state",
        "/help — Show available commands",
    ]
    return "\n".join(lines)


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.priority("/codex exit", cmd_codex_exit_priority)
    router.exact("/new", cmd_new)
    router.exact("/status", cmd_status)
    router.exact("/dream", cmd_dream)
    router.exact("/dream-log", cmd_dream_log)
    router.prefix("/dream-log ", cmd_dream_log)
    router.exact("/dream-restore", cmd_dream_restore)
    router.prefix("/dream-restore ", cmd_dream_restore)
    router.exact("/help", cmd_help)
    router.exact("/codex", cmd_codex)
    router.prefix("/codex ", cmd_codex)
