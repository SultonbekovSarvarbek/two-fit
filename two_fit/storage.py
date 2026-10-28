"""aiogram FSM storage on top of the same SQLite database.

Wizard progress (half-filled listing, purchase form) survives a bot restart.
"""
from __future__ import annotations

from typing import Any, Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from .repo import Repo


def _key(key: StorageKey) -> str:
    return ":".join(
        str(x)
        for x in (key.bot_id, key.chat_id, key.user_id, key.thread_id or 0, key.business_connection_id or "",
                  key.destiny)
    )


class SQLiteStorage(BaseStorage):
    def __init__(self, repo: Repo):
        self.repo = repo

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self.repo.fsm_set_state(_key(key), value)

    async def get_state(self, key: StorageKey) -> str | None:
        state, _ = await self.repo.fsm_get(_key(key))
        return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self.repo.fsm_set_data(_key(key), dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        _, data = await self.repo.fsm_get(_key(key))
        return data

    async def close(self) -> None:  # the repo owns the connection
        pass
