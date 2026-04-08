"""Codex CLI proxy — relay messages to/from an interactive Codex tmux session."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from typing import Any

from loguru import logger

# Codex prompt character used to detect "ready for input".
# Codex CLI uses › (U+203A SINGLE RIGHT-POINTING ANGLE QUOTATION MARK),
# NOT ❯ (U+276F HEAVY RIGHT-POINTING ANGLE QUOTATION MARK).
_PROMPT_PATTERN = "›"

# A line that is JUST the prompt character (with optional trailing whitespace)
# means Codex is idle and waiting for input.
# History lines look like "› <message>" so they won't match this.
_IDLE_PROMPT_RE = re.compile(r"^›\s*$", re.MULTILINE)

# Patterns that indicate Codex is waiting for user input (question/selection).
# Matches lines like: "? Do you trust...", "1.", "2.", "[Y/n]", "(y/n)", "yes/no"
_QUESTION_RE = re.compile(
    r"(\?\s.+|^\s*\d+\.\s|\[Y/n\]|\[y/N\]|\(y/n\)|\(yes/no\)|yes\s*/\s*no)",
    re.IGNORECASE | re.MULTILINE,
)

# Sentinel values returned by _wait_for_input to tell callers why we stopped.
_STOPPED_PROMPT = "prompt"      # normal Codex ❯ prompt
_STOPPED_QUESTION = "question"  # Codex is asking the user something

# ANSI escape sequence regex (covers colours, cursor moves, etc.).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]")


def _get_socket_dir() -> str:
    """Return the tmux socket directory, respecting ``NANOBOT_TMUX_SOCKET_DIR``."""
    default = os.path.join(os.environ.get("TMPDIR", "/tmp"), "nanobot-tmux-sockets")
    return os.environ.get("NANOBOT_TMUX_SOCKET_DIR", default)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from *text*."""
    return _ANSI_RE.sub("", text)


async def _run(cmd: str, *, timeout: float = 30) -> tuple[int, str]:
    """Run a shell command and return ``(returncode, stdout+stderr)``."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "timeout"
    return proc.returncode, (stdout or b"").decode("utf-8", "replace")


class CodexProxy:
    """Manage a single interactive Codex CLI session inside tmux."""

    def __init__(self, socket: str, tmux_session: str, workspace: str) -> None:
        self.socket = socket
        self.tmux_session = tmux_session
        self.workspace = workspace

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def start(
        cls,
        workspace: str,
        initial_prompt: str | None = None,
    ) -> CodexProxy:
        """Spawn a tmux session running ``codex`` in interactive mode."""
        socket_dir = _get_socket_dir()
        os.makedirs(socket_dir, exist_ok=True)
        socket = os.path.join(socket_dir, "nanobot.sock")
        name = f"codex-{uuid.uuid4().hex[:8]}"

        logger.info("Starting Codex proxy: session={}, workspace={}", name, workspace)

        # Create detached tmux session.
        rc, out = await _run(
            f'tmux -S {_q(socket)} new-session -d -s {_q(name)} -c {_q(workspace)}'
        )
        if rc != 0:
            raise RuntimeError(f"Failed to create tmux session: {out}")

        # Launch codex inside the session.
        rc, out = await _run(
            f'tmux -S {_q(socket)} send-keys -t {_q(name)} "codex" Enter'
        )
        if rc != 0:
            raise RuntimeError(f"Failed to start codex: {out}")

        proxy = cls(socket=socket, tmux_session=name, workspace=workspace)

        # Wait for Codex to become ready.
        await proxy._wait_for_prompt(timeout=60)

        logger.info("Codex proxy ready: {}", name)

        if initial_prompt:
            await proxy.send(initial_prompt)

        return proxy

    async def send(self, message: str) -> str:
        """Send *message* to Codex and return its response text."""
        # Send the message via send-keys -l (literal) + Enter.
        await _run(
            f'tmux -S {_q(self.socket)} send-keys -t {_q(self.tmux_session)} -l -- '
            f'{_q(message)}'
        )
        await _run(
            f'tmux -S {_q(self.socket)} send-keys -t {_q(self.tmux_session)} Enter'
        )

        # Wait for Codex to finish (prompt reappears) or ask a question.
        stop_reason = await self._wait_for_input(timeout=120)

        # Capture full pane and strip ANSI codes.
        full = await self._capture_pane()
        lines = [_strip_ansi(l) for l in full.splitlines()]

        # Strip the status bar — tmux pane's last line is always the codex status/info bar.
        # _wait_for_input already excludes it from stability checks.
        if lines:
            lines = lines[:-1]

        # Anchor on the LAST occurrence of the user's message echo ("› <message>").
        # Codex echoes input as "› <message>" regardless of TUI redraws, so this
        # works even when codex repaints the screen rather than appending new lines.
        msg_stripped = message.strip()
        last_echo = -1
        for i, line in enumerate(lines):
            clean = line.strip()
            if clean.startswith(_PROMPT_PATTERN):
                after_prompt = clean[len(_PROMPT_PATTERN):].strip()
                if after_prompt == msg_stripped:
                    last_echo = i

        if last_echo >= 0:
            response_lines = lines[last_echo + 1:]
        else:
            # Echo not found — fall back to last portion of visible pane.
            response_lines = lines[-30:]

        if stop_reason == _STOPPED_PROMPT:
            # Strip trailing idle › prompt line(s) — not part of the response.
            while response_lines and _IDLE_PROMPT_RE.match(response_lines[-1].strip()):
                response_lines.pop()

        # For questions (_STOPPED_QUESTION), keep all lines so the user sees what's asked.

        cleaned = "\n".join(response_lines).strip()
        return cleaned or "(no output)"

    async def is_alive(self) -> bool:
        """Check whether the tmux session is still running."""
        rc, _ = await _run(
            f'tmux -S {_q(self.socket)} has-session -t {_q(self.tmux_session)}',
            timeout=5,
        )
        return rc == 0

    async def kill(self) -> None:
        """Kill the tmux session."""
        logger.info("Killing Codex proxy: {}", self.tmux_session)
        await _run(
            f'tmux -S {_q(self.socket)} kill-session -t {_q(self.tmux_session)}',
            timeout=10,
        )

    # ------------------------------------------------------------------
    # Serialization (for session metadata persistence)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "socket": self.socket,
            "tmux_session": self.tmux_session,
            "workspace": self.workspace,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CodexProxy:
        return cls(
            socket=data["socket"],
            tmux_session=data["tmux_session"],
            workspace=data["workspace"],
        )

    # ------------------------------------------------------------------
    # List / discover running sessions
    # ------------------------------------------------------------------

    @staticmethod
    async def list_sessions() -> list[dict[str, str]]:
        """Return info about all alive ``codex-*`` tmux sessions on the nanobot socket."""
        socket = os.path.join(_get_socket_dir(), "nanobot.sock")
        rc, out = await _run(
            f"tmux -S {_q(socket)} list-sessions -F '#{{session_name}} #{{session_created}}' 2>/dev/null",
            timeout=5,
        )
        if rc != 0:
            return []
        sessions = []
        for line in out.strip().splitlines():
            parts = line.split(None, 1)
            if not parts or not parts[0].startswith("codex-"):
                continue
            sessions.append({
                "name": parts[0],
                "created": parts[1] if len(parts) > 1 else "",
            })
        return sessions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _capture_pane(self, lines: int = 2000) -> str:
        """Capture the current pane content."""
        rc, out = await _run(
            f'tmux -S {_q(self.socket)} capture-pane -p -J -t {_q(self.tmux_session)} -S -{lines}',
            timeout=10,
        )
        return out if rc == 0 else ""

    async def _wait_for_prompt(self, timeout: int = 120) -> None:
        """Poll until the Codex ❯ prompt or a question appears (used during startup)."""
        await self._wait_for_input(timeout=timeout)

    async def _wait_for_input(self, timeout: int = 120) -> str:
        """Poll the pane until Codex is ready for input.

        Strategy: stability-based detection.
        - While Codex is processing, the pane content changes continuously.
        - When Codex is done (idle), the content area stops changing.
        - We exclude the last line (status bar, which may tick) from comparison.
        - Two consecutive identical snapshots (~0.6s apart) → Codex is done.

        Returns ``_STOPPED_PROMPT`` when stable, or ``_STOPPED_QUESTION`` when
        the stable content contains a question/selection pattern.
        """
        import time

        POLL_INTERVAL = 1
        STABLE_REQUIRED = 2  # consecutive identical snapshots needed

        deadline = time.monotonic() + timeout
        prev_content: str | None = None
        stable_count = 0

        while time.monotonic() < deadline:
            pane = await self._capture_pane(lines=200)
            lines = pane.splitlines()
            # Exclude last line (Codex status bar) from stability comparison.
            content = _strip_ansi("\n".join(lines[:-1] if lines else []))

            if content == prev_content and content.strip():
                stable_count += 1
                if stable_count >= STABLE_REQUIRED:
                    if _QUESTION_RE.search(content):
                        await asyncio.sleep(0.5)
                        return _STOPPED_QUESTION
                    return _STOPPED_PROMPT
            else:
                stable_count = 0
                prev_content = content

            await asyncio.sleep(POLL_INTERVAL)

        logger.warning("Waiting for Codex input timed out after {}s", timeout)
        return _STOPPED_PROMPT  # best-effort fallback


def _q(s: str) -> str:
    """Shell-quote a string."""
    return "'" + s.replace("'", "'\\''") + "'"


async def watch_until_idle(
    proxy: CodexProxy,
    bus: Any,
    channel: str,
    chat_id: str,
    *,
    timeout: int = 3600,
) -> None:
    """Background task: notify the user when a Codex session finishes working.

    Starts watching immediately. Compares two snapshots taken ~2 seconds apart:
    if identical, the session was already idle when the user detached — exits silently.
    Otherwise, waits for the session to become stable and sends a completion notification.
    """
    from nanobot.bus.events import OutboundMessage

    POLL_INTERVAL = 1.0
    STABLE_REQUIRED = 3  # slightly stricter than interactive (3s stable = done)
    ACTIVITY_DETECT_SECS = 2  # window to determine if session is actively working

    try:
        import time

        # --- Phase 1: determine if Codex is actively working.
        # Take two snapshots ACTIVITY_DETECT_SECS apart.  If the pane content is
        # unchanged, the session was already idle when the user detached — no
        # notification needed.  If it changed, Codex is still computing.
        pane = await proxy._capture_pane(lines=200)
        snapshot_before = _strip_ansi("\n".join(pane.splitlines()[:-1]))

        await asyncio.sleep(ACTIVITY_DETECT_SECS)

        if not await proxy.is_alive():
            return  # session died quietly — skip notification

        pane = await proxy._capture_pane(lines=200)
        lines_after = pane.splitlines()
        snapshot_after = _strip_ansi("\n".join(lines_after[:-1] if lines_after else []))

        if snapshot_before == snapshot_after:
            # Pane unchanged — session was idle when user detached.
            return

        # --- Phase 2: pane changed, Codex is working. Wait for it to settle.
        prev_content: str | None = snapshot_after
        stable_count = 0
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)

            if not await proxy.is_alive():
                await bus.publish_outbound(OutboundMessage(
                    channel=channel, chat_id=chat_id,
                    content=f"⚠️ Codex session `{proxy.tmux_session}` ended unexpectedly.",
                ))
                return

            pane = await proxy._capture_pane(lines=200)
            lines = pane.splitlines()
            content = _strip_ansi("\n".join(lines[:-1] if lines else []))

            if content == prev_content and content.strip():
                stable_count += 1
                if stable_count >= STABLE_REQUIRED:
                    break
            else:
                stable_count = 0
                prev_content = content
        else:
            # Timed out — notify anyway with whatever's on screen.
            logger.warning("watch_until_idle: timed out after {}s for {}", timeout, proxy.tmux_session)

        # --- Phase 3: capture output and notify.
        pane = await proxy._capture_pane(lines=200)
        lines = _strip_ansi(pane).splitlines()
        # Strip status bar (last line) and blank lines, take last 30 meaningful lines.
        content_lines = [l for l in lines[:-1] if l.strip()]
        summary = "\n".join(content_lines[-30:])

        await bus.publish_outbound(OutboundMessage(
            channel=channel, chat_id=chat_id,
            content=(
                f"✅ Codex `{proxy.tmux_session}` 完成了\n\n"
                f"{summary}\n\n"
                f"用 `/codex resume {proxy.tmux_session}` 繼續。"
            ),
        ))

    except asyncio.CancelledError:
        # User resumed the session — watcher cancelled, no notification needed.
        logger.debug("watch_until_idle cancelled for {}", proxy.tmux_session)
    except Exception as e:
        logger.error("watch_until_idle error for {}: {}", proxy.tmux_session, e)
