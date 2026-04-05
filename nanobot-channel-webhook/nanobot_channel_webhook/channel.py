

import asyncio
import hashlib
import json
import time
from typing import Any

import aiohttp
from aiohttp import web
from loguru import logger

from nanobot.channels.base import BaseChannel
from nanobot.bus.events import OutboundMessage


class WebhookChannel(BaseChannel):
    name = "webhook"
    display_name = "Webhook"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {
            "enabled": False,
            "port": 9000,
            "callbackPath": "/callback",
            "allowFrom": ["*"],
            "verifySignature": True,
            "appId": "",
            "appSecret": "",
            "signingSecret": "",
        }

    def __init__(self, config: Any, bus):
        super().__init__(config, bus)
        self._token = ""
        self._token_expire_at = 0
        self._http: aiohttp.ClientSession | None = None
        self._last_employee_code: str | None = None

    def _cfg(self, key: str, default: Any = None) -> Any:
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def is_allowed(self, sender_id: str) -> bool:
        """Support allowFrom/allow_from across dict/object config shapes."""
        if isinstance(self.config, dict):
            allow_list = self.config.get("allowFrom", self.config.get("allow_from", []))
        else:
            allow_list = getattr(self.config, "allow_from", [])

        if not allow_list:
            logger.warning("{}: allow_from is empty — all access denied", self.name)
            return False
        if "*" in allow_list:
            return True
        return str(sender_id) in {str(x) for x in allow_list}

    async def start(self) -> None:
        """Start an HTTP server that listens for incoming messages.

        IMPORTANT: start() must block forever (or until stop() is called).
        If it returns, the channel is considered dead.
        """
        self._running = True
        port = int(self._cfg("port", 9000))
        callback_path = str(self._cfg("callbackPath", "/callback"))
        if not callback_path.startswith("/"):
            callback_path = f"/{callback_path}"
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

        app = web.Application()
        app.router.add_post(callback_path, self._on_request)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Webhook listening on :{}{}", port, callback_path)

        # Block until stopped
        while self._running:
            await asyncio.sleep(1)

        await runner.cleanup()

    async def stop(self) -> None:
        self._running = False
        if self._http:
            await self._http.close()
        self._http = None

    async def send(self, msg: OutboundMessage) -> None:
        """Deliver an outbound message.

        msg.content  — markdown text (convert to platform format as needed)
        msg.media    — list of local file paths to attach
        msg.chat_id  — the recipient (same chat_id you passed to _handle_message)
        msg.metadata — may contain "_progress": True for streaming chunks
        """
        app_id = str(self._cfg("appId", "")).strip()
        app_secret = str(self._cfg("appSecret", "")).strip()
        if not app_id or not app_secret:
            logger.warning("[webhook] missing appId/appSecret; skip outbound")
            return
        if not self._http:
            logger.warning("[webhook] http client not ready; skip outbound")
            return

        

        employee_code = self._resolve_employee_code(str(msg.chat_id))
        if not employee_code:
            logger.warning("[webhook] no routable employee_code for chat_id={}", msg.chat_id)
            return

        token = await self._get_access_token(app_id, app_secret)
        if not token:
            logger.warning("[webhook] failed to get access token; skip outbound")
            return

        payload = {
            "employee_code": employee_code,
            "message": {"tag": "text", "text": {"content": msg.content}},
        }
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with self._http.post(
                "https://openapi.seatalk.io/messaging/v2/single_chat",
                json=payload,
                headers=headers,
                timeout=20,
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning("[webhook] send failed status={} body={}", resp.status, text)
                    return
                try:
                    data = json.loads(text)
                except Exception:
                    logger.warning("[webhook] send failed non-json body={}", text[:200])
                    return
                if data.get("code") != 0:
                    logger.warning("[webhook] send failed code={} body={}", data.get("code"), text)
                    return
                logger.info("[webhook] -> {}: {}", employee_code, msg.content[:80])
        except asyncio.TimeoutError:
            logger.warning("[webhook] send timeout for employee_code={}", employee_code)
        except Exception as e:
            logger.warning("[webhook] send exception: {}", e)

    async def _on_request(self, request: web.Request) -> web.Response:
        """Handle an incoming HTTP POST."""
        raw_body = await request.read()
        body: dict[str, Any]
        try:
            body = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
        except Exception:
            # Some callback verifications may use form-encoded payloads.
            try:
                form = await request.post()
                body = dict(form)
            except Exception:
                body = {}

        if not await self._verify_signature(request, raw_body):
            return web.json_response({"ok": False, "error": "invalid signature"}, status=401)

        # SeaTalk URL verification: echo seatalk_challenge within 5 seconds.
        challenge = body.get("seatalk_challenge") or request.query.get("seatalk_challenge")
        if isinstance(body.get("event"), dict):
            challenge = challenge or body["event"].get("seatalk_challenge")
        if challenge is not None:
            return web.json_response({"seatalk_challenge": str(challenge)})

        event_type = body.get("event_type", "")
        event = body.get("event") if isinstance(body.get("event"), dict) else {}
        if event_type != "message_from_bot_subscriber":
            return web.json_response({"ok": True})

        sender = str(event.get("employee_code", "unknown"))
        if sender and sender not in {"unknown", "user", "direct"}:
            self._last_employee_code = sender
        chat_id = sender
        text = ""
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if isinstance(message.get("text"), dict):
            text = str(message["text"].get("content", ""))
        if not text:
            return web.json_response({"ok": True})

        await self._handle_message(
            sender_id=sender,
            chat_id=chat_id,
            content=text,
            media=[],
            metadata={"event_id": body.get("event_id", "")},
        )

        return web.json_response({"ok": True})

    async def _verify_signature(self, request: web.Request, raw_body: bytes) -> bool:
        if not bool(self._cfg("verifySignature", True)):
            return True
        signing_secret = str(self._cfg("signingSecret", ""))
        if not signing_secret:
            logger.warning("[webhook] verifySignature enabled but signingSecret is empty")
            return False

        incoming = request.headers.get("Signature", "")
        calculated = hashlib.sha256(raw_body + signing_secret.encode("utf-8")).hexdigest()
        if incoming != calculated:
            logger.warning("[webhook] signature mismatch")
            return False
        return True

    async def _get_access_token(self, app_id: str, app_secret: str) -> str:
        now = int(time.time())
        if self._token and now < self._token_expire_at - 10:
            return self._token

        payload = {"app_id": app_id, "app_secret": app_secret}
        if not self._http:
            return ""
        
        async with self._http.post(
            "https://openapi.seatalk.io/auth/app_access_token",
            json=payload,
            timeout=10,
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                logger.warning("[webhook] token request failed status={} body={}", resp.status, text)
                return ""
            data = json.loads(text)
            if data.get("code") != 0:
                logger.warning("[webhook] token request failed code={} body={}", data.get("code"), text)
                return ""
            self._token = str(data.get("app_access_token", ""))
            expire = int(data.get("expire", 0))
            self._token_expire_at = int(time.time()) + expire if expire else int(time.time()) + 300
            return self._token


    def _resolve_employee_code(self, chat_id: str) -> str:
        raw = (chat_id or "").strip()
        if raw and raw not in {"user", "direct", "unknown", "cli"}:
            return raw
        return self._last_employee_code or ""