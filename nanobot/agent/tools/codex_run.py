"""codex_run tool — start background Codex sessions from the agent loop."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

from nanobot.agent.codex_proxy import CodexProxy
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus

_DEFAULT_MAX_SESSIONS = 3
_CODEX_CONFIG = Path.home() / ".codex" / "config.toml"


def _ensure_codex_trust(workspace: str) -> None:
    """Add *workspace* to Codex's trusted projects if not already present."""
    ws = os.path.realpath(workspace)
    config_path = _CODEX_CONFIG

    existing = ""
    if config_path.exists():
        existing = config_path.read_text(encoding="utf-8")

    section = f'[projects."{ws}"]'
    if section in existing:
        return  # already trusted

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("a", encoding="utf-8") as f:
        f.write(f'\n{section}\ntrust_level = "trusted"\n')
    logger.info("codex_run: added trust for {}", ws)


@tool_parameters(
    tool_parameters_schema(
        prompt=StringSchema("The task prompt for Codex to execute"),
        workspace=StringSchema("Working directory for Codex (defaults to agent workspace)"),
        on_complete=StringSchema(
            "Shell command to run after Codex finishes, e.g. "
            "'glab issue note 42 -m \"Done\" -R group/project'"
        ),
        max_sessions=IntegerSchema(
            description="Max concurrent Codex sessions (default 3)",
            minimum=1,
            maximum=10,
        ),
        required=["prompt"],
    )
)
class CodexRunTool(Tool):
    """Start a background Codex session that runs a task and notifies when done."""

    def __init__(
        self,
        workspace: str,
        bus: MessageBus,
        background_task_callback: Callable[[Coroutine], None],
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
    ) -> None:
        self._workspace = workspace
        self._bus = bus
        self._schedule_background = background_task_callback
        self._max_sessions = max_sessions
        self._channel = "cli"
        self._chat_id = "direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "codex_run"

    @property
    def description(self) -> str:
        return (
            "Start a background Codex CLI session to work on a task. "
            "Returns immediately with the session name. "
            "You will be notified when Codex finishes. "
            "Use on_complete to run a shell command after completion "
            "(e.g. post a comment on GitLab)."
        )

    async def execute(
        self,
        prompt: str,
        workspace: str | None = None,
        on_complete: str | None = None,
        max_sessions: int | None = None,
        **kwargs: Any,
    ) -> str:
        limit = max_sessions or self._max_sessions

        # Check concurrent session count.
        sessions = await CodexProxy.list_sessions()
        if len(sessions) >= limit:
            names = ", ".join(f"`{s['name']}`" for s in sessions)
            return (
                f"Error: {len(sessions)} Codex sessions already running "
                f"(limit {limit}): {names}. "
                "Wait for one to finish or use /codex kill."
            )

        ws = workspace or self._workspace

        # Pre-trust the workspace so Codex won't prompt interactively.
        try:
            _ensure_codex_trust(ws)
        except Exception as e:
            logger.warning("codex_run: failed to set trust for {}: {}", ws, e)

        try:
            proxy = await CodexProxy.start(workspace=ws, codex_args="-a never")
        except Exception as e:
            return f"Error starting Codex: {e}"

        # Schedule background task: send prompt (blocking) + notify + on_complete.
        # Using proxy.send() ensures the prompt is fully submitted and we wait
        # for Codex to finish, avoiding the race condition where send_nowait +
        # watch_until_idle could think Codex is idle before it starts processing.
        self._schedule_background(
            self._send_and_complete(proxy, prompt, on_complete)
        )

        return (
            f"Codex session `{proxy.tmux_session}` started in `{ws}`.\n"
            f"Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}\n"
            "Will notify when done."
        )

    async def _send_and_complete(
        self,
        proxy: CodexProxy,
        prompt: str,
        on_complete: str | None,
    ) -> None:
        """Send prompt, wait for Codex to finish, notify, then run on_complete."""
        try:
            response = await proxy.send(prompt)

            from nanobot.bus.events import OutboundMessage

            lines = [l for l in response.splitlines() if l.strip()]
            summary = "\n".join(lines[-30:])

            await self._bus.publish_outbound(OutboundMessage(
                channel=self._channel,
                chat_id=self._chat_id,
                content=(
                    f"Codex `{proxy.tmux_session}` finished\n\n"
                    f"{summary}\n\n"
                    f"Use `/codex resume {proxy.tmux_session}` to continue."
                ),
            ))

            if on_complete:
                logger.info(
                    "codex_run: running on_complete for {}: {}",
                    proxy.tmux_session, on_complete,
                )
                proc = await asyncio.create_subprocess_shell(
                    on_complete,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=120
                )
                if proc.returncode != 0:
                    output = (stdout or b"").decode("utf-8", "replace")
                    logger.warning(
                        "codex_run: on_complete failed (rc={}): {}",
                        proc.returncode, output[:500],
                    )
        except asyncio.CancelledError:
            logger.debug(
                "codex_run: task cancelled for {}", proxy.tmux_session
            )
        except Exception as e:
            logger.error(
                "codex_run: error for {}: {}", proxy.tmux_session, e
            )
