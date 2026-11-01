"""SQLite-backed FSM storage so seller/admin flows survive bot restarts."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

import aiosqlite
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.fsm.state import State


class SQLiteFSMStorage(BaseStorage):
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    async def setup(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path, timeout=20) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS fsm_sessions (
                    storage_key TEXT PRIMARY KEY,
                    state TEXT,
                    data TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()

    @staticmethod
    def _key(key: StorageKey) -> str:
        return json.dumps(asdict(key), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _state_value(state: str | State | None) -> str | None:
        if isinstance(state, State):
            return state.state
        return state

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        async with aiosqlite.connect(self.database_path, timeout=20) as db:
            await db.execute(
                """
                INSERT INTO fsm_sessions(storage_key, state, data)
                VALUES(?, ?, '{}')
                ON CONFLICT(storage_key) DO UPDATE SET
                    state=excluded.state,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (self._key(key), self._state_value(state)),
            )
            await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with aiosqlite.connect(self.database_path, timeout=20) as db:
            async with db.execute(
                "SELECT state FROM fsm_sessions WHERE storage_key=?",
                (self._key(key),),
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        encoded = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
        async with aiosqlite.connect(self.database_path, timeout=20) as db:
            await db.execute(
                """
                INSERT INTO fsm_sessions(storage_key, state, data)
                VALUES(?, NULL, ?)
                ON CONFLICT(storage_key) DO UPDATE SET
                    data=excluded.data,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (self._key(key), encoded),
            )
            await db.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with aiosqlite.connect(self.database_path, timeout=20) as db:
            async with db.execute(
                "SELECT data FROM fsm_sessions WHERE storage_key=?",
                (self._key(key),),
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    async def close(self) -> None:
        return None
