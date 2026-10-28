"""Drives the real dispatcher with a fake Telegram session — no token, no network."""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageReplyMarkup, EditMessageText, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import Chat, Message, PhotoSize, Update

from two_fit.app import create_dispatcher
from two_fit.config import Config
from two_fit.i18n import I18n
from two_fit.repo import Repo

_ids = itertools.count(1000)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendPhoto)):
            photo = [PhotoSize(file_id=f"ph{next(_ids)}", file_unique_id="u", width=640, height=360)] \
                if isinstance(method, SendPhoto) else None
            return Message(
                message_id=next(_ids), date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None), caption=getattr(method, "caption", None), photo=photo,
            )
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        if False:
            yield b""

    async def close(self) -> None:
        pass


def text_of(m: TelegramMethod) -> str:
    return getattr(m, "text", None) or getattr(m, "caption", None) or ""


def buttons_of(m: TelegramMethod) -> list:
    markup = getattr(m, "reply_markup", None)
    if markup is None:
        return []
    if hasattr(markup, "inline_keyboard"):
        return [b for row in markup.inline_keyboard for b in row]
    if hasattr(markup, "keyboard"):
        return [b for row in markup.keyboard for b in row]
    return []


class Harness:
    def __init__(self, db_path, admin_ids=frozenset({900})):
        self.cfg = Config(
            bot_token="42:TEST", admin_ids=frozenset(admin_ids), support_username="s_sarvar",
            db_path=db_path, commission_percent=10, page_size=5,
            welcome_image=db_path.parent / "missing.png", logo=db_path.parent / "missing-logo.png",
        )
        self.update_id = itertools.count(1)

    async def start(self):
        self.repo = Repo(self.cfg.db_path, self.cfg.commission_percent)
        await self.repo.connect()
        self.session = FakeSession()
        self.bot = Bot(self.cfg.bot_token, session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = create_dispatcher(self.repo, I18n(), self.cfg, self.bot)
        return self

    async def stop(self):
        await self.repo.close()

    # ------------------------------------------------------------ sending updates
    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": f"Name{uid}", "username": f"user{uid}"}

    async def _feed(self, update: dict) -> list[TelegramMethod]:
        before = len(self.session.calls)
        upd = Update.model_validate({"update_id": next(self.update_id), **update}, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, upd)
        return self.session.calls[before:]

    async def send(self, uid, text=None, *, contact=None, photo=False):
        msg = {"message_id": next(_ids), "date": int(datetime.now().timestamp()),
               "chat": {"id": uid, "type": "private"}, "from": self._user(uid)}
        if text is not None:
            msg["text"] = text
            if text.startswith("/"):
                msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if contact:
            msg["contact"] = {"phone_number": contact, "first_name": "X", "user_id": uid}
        if photo:
            msg["photo"] = [{"file_id": f"shot-{uid}", "file_unique_id": "s", "width": 100, "height": 100}]
        return await self._feed({"message": msg})

    async def click(self, uid, data: str):
        cb = {"id": str(next(_ids)), "from": self._user(uid), "chat_instance": "ci", "data": data,
              "message": {"message_id": next(_ids), "date": int(datetime.now().timestamp()),
                          "chat": {"id": uid, "type": "private"}, "text": "old"}}
        return await self._feed({"callback_query": cb})

    # ------------------------------------------------------------ inspecting replies
    def to(self, uid) -> list[TelegramMethod]:
        return [c for c in self.session.calls if getattr(c, "chat_id", None) == uid]

    def texts_to(self, uid) -> str:
        return "\n".join(text_of(c) for c in self.to(uid))

    def button(self, uid, contains: str) -> str:
        """callback_data of the most recent inline button sent to uid whose text contains ``contains``."""
        for call in reversed(self.to(uid)):
            for b in buttons_of(call):
                if contains in b.text and getattr(b, "callback_data", None):
                    return b.callback_data
        raise AssertionError(f"button {contains!r} not found for {uid}")

    def url_buttons(self, uid) -> list[str]:
        return [b.url for c in self.to(uid) for b in buttons_of(c) if getattr(b, "url", None)]


def alerts(calls) -> list[str]:
    return [c.text for c in calls if type(c).__name__ == "AnswerCallbackQuery" and c.text]


def texts(calls) -> str:
    return "\n".join(text_of(c) for c in calls if isinstance(c, (SendMessage, SendPhoto, EditMessageText)))


__all__ = ["Harness", "alerts", "texts", "EditMessageReplyMarkup"]
