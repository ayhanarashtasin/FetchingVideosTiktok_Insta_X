"""A fake Telegram Bot API HTTP server (architecture section 15: "a simulated Telegram server").

It speaks the real wire protocol - form/multipart POSTs to /bot<token>/<method>, JSON envelopes, long-
polling `getUpdates` - so the REAL aiogram client, real multipart serialisation, real polling loop and the
real `app.main.run` can be exercised end to end with no network and no token. Uploaded file bytes are
captured at the HTTP level, which is what lets tests assert byte-exact delivery.

Error injection: `server.fail(method, status, description, retry_after=None, times=1)`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

BOT_ID = 987654321


def _caption(call: Call) -> dict[str, Any]:
    cap = call.fields.get("caption")
    return {"caption": cap} if cap else {}


def _bot_info() -> dict[str, Any]:
    """A complete `getMe` reply. The required fields are read from the INSTALLED aiogram model, so the
    fake cannot silently drift from what the client library insists on."""
    from aiogram.methods import GetMe

    info: dict[str, Any] = {"id": BOT_ID, "is_bot": True, "first_name": "Test Bot", "username": "test_bot"}
    for name, field_info in GetMe.__returning__.model_fields.items():
        if field_info.is_required() and name not in info:
            info[name] = False  # every other required field is a capability flag
    return info


@dataclass
class Call:
    method: str
    fields: dict[str, str]
    files: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # form field -> (filename, bytes)
    reply: Any = None

    def json(self, name: str) -> Any:
        return json.loads(self.fields[name])

    @property
    def chat_id(self) -> int:
        return int(self.fields.get("chat_id", 0))


class FakeTelegramServer:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._updates: list[dict[str, Any]] = []
        self._mid = 1000
        self._failures: dict[str, deque[tuple[int, str, dict[str, Any]]]] = defaultdict(deque)
        self._runner: web.AppRunner | None = None
        self.base_url = ""
        self._changed = asyncio.Event()
        self.polls = 0
        self.max_upload_bytes: int | None = None  # None = unlimited (the local Bot API profile)
        self._new_update = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> str:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/bot{token}/{method}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # ------------------------------------------------------------------ test API
    def push(self, update: dict[str, Any]) -> None:
        self._updates.append(update)
        self._changed.set()
        self._new_update.set()

    def fail(
        self, method: str, status: int, description: str, *, retry_after: int | None = None, times: int = 1
    ) -> None:
        params = {"retry_after": retry_after} if retry_after is not None else {}
        for _ in range(times):
            self._failures[method].append((status, description, params))

    def calls_of(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    async def wait_for(self, predicate, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out; calls so far: {[c.method for c in self.calls]}")
            try:
                await asyncio.wait_for(self._changed.wait(), 0.1)
            except TimeoutError:
                pass
            self._changed.clear()

    # ------------------------------------------------------------------ update builders
    _uid = 0

    @classmethod
    def next_update_id(cls) -> int:
        cls._uid += 1
        return cls._uid + 10_000

    @classmethod
    def message_update(cls, user_id: int, text: str, message_id: int, *, chat_type: str = "private") -> dict[str, Any]:
        return {
            "update_id": cls.next_update_id(),
            "message": {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": user_id, "type": chat_type, "first_name": "T"},
                "from": {"id": user_id, "is_bot": False, "first_name": "T"},
                "text": text,
            },
        }

    @classmethod
    def callback_update(cls, user_id: int, data: str, cq_id: str, message_id: int = 7) -> dict[str, Any]:
        return {
            "update_id": cls.next_update_id(),
            "callback_query": {
                "id": cq_id,
                "from": {"id": user_id, "is_bot": False, "first_name": "T"},
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": int(time.time()),
                    "chat": {"id": user_id, "type": "private", "first_name": "T"},
                    "text": "x",
                },
            },
        }

    # ------------------------------------------------------------------ HTTP handling
    def _message(self, chat_id: int, **extra: Any) -> dict[str, Any]:
        self._mid += 1
        return {"message_id": self._mid, "date": int(time.time()), "chat": {"id": chat_id, "type": "private"}, **extra}

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        form = await request.post()
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        for key in form:
            value = form[key]
            if hasattr(value, "file"):  # an uploaded file part
                files[key] = (value.filename, value.file.read())
            else:
                fields[key] = str(value)
        call = Call(method, fields, files)
        if method != "getUpdates":
            self.calls.append(call)
            self._changed.set()

        if self.max_upload_bytes is not None and any(len(b) > self.max_upload_bytes for _, b in files.values()):
            return web.json_response(
                {"ok": False, "error_code": 413, "description": "Request Entity Too Large"}, status=413
            )
        queue = self._failures.get(method)
        if queue:
            status, description, params = queue.popleft()
            body = {"ok": False, "error_code": status, "description": description}
            if params:
                body["parameters"] = params
            return web.json_response(body, status=status)

        result = await self._result(method, call)
        call.reply = result
        return web.json_response({"ok": True, "result": result})

    async def _result(self, method: str, call: Call) -> Any:
        chat_id = call.chat_id
        if method == "getMe":
            return _bot_info()
        if method == "getUpdates":
            return await self._get_updates(call)
        if method in {"sendMessage"}:
            return self._message(chat_id, text=call.fields.get("text", ""))
        if method == "sendPhoto":
            # Real Telegram re-compresses native photos to <= 1280 px; model a typical result.
            sizes = [{"file_id": "a", "file_unique_id": "a", "width": 1280, "height": 720, "file_size": 90_000}]
            return self._message(chat_id, photo=sizes, **_caption(call))
        if method == "sendDocument":
            name, data = next(iter(call.files.values()), ("file", b""))
            doc = {"file_id": "d", "file_unique_id": "d", "file_name": name, "file_size": len(data)}
            return self._message(chat_id, document=doc, **_caption(call))
        if method == "sendVideo":
            return self._message(chat_id, **_caption(call))
        if method == "sendMediaGroup":
            return [self._message(chat_id) for _ in call.json("media")]
        if method in {"editMessageText", "editMessageReplyMarkup", "editMessageCaption"}:
            return self._message(chat_id, text=call.fields.get("text", ""))
        return True  # deleteMessage, answerCallbackQuery, ...

    async def _get_updates(self, call: Call) -> list[dict[str, Any]]:
        self.polls += 1
        self._changed.set()
        offset = int(call.fields.get("offset", 0))
        timeout = min(float(call.fields.get("timeout", 0)), 1.0)
        self._updates = [u for u in self._updates if u["update_id"] >= offset]  # confirmed ones are dropped
        if not self._updates and timeout > 0:
            try:
                await asyncio.wait_for(self._wait_for_update(), timeout)
            except TimeoutError:
                pass
        return list(self._updates)

    async def _wait_for_update(self) -> None:
        while not self._updates:
            self._new_update.clear()
            await self._new_update.wait()
