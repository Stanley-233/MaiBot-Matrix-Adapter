"""持久化登录凭据、私聊房间映射与消息归属；所有 SQLite 操作在线程中完成。"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional


class AdapterStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS accounts ("
                "homeserver TEXT, user_id TEXT, device_id TEXT, access_token TEXT, updated_at REAL NOT NULL, "
                "PRIMARY KEY(homeserver, user_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS dm_rooms (user_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS message_rooms ("
                "event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, sender TEXT NOT NULL, "
                "body TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS failed_events ("
                "event_id TEXT PRIMARY KEY, room_id TEXT, error TEXT NOT NULL, "
                "traceback TEXT NOT NULL, failed_at REAL NOT NULL)"
            )

    async def load_account(self, homeserver: str, user_id: str) -> Optional[Dict[str, str]]:
        return await asyncio.to_thread(self._load_account, homeserver, user_id)

    def _load_account(self, homeserver: str, user_id: str) -> Optional[Dict[str, str]]:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT device_id, access_token FROM accounts WHERE homeserver = ? AND user_id = ?",
                (homeserver, user_id),
            ).fetchone()
        return {"device_id": row[0], "access_token": row[1]} if row else None

    async def save_account(
        self, homeserver: str, user_id: str, device_id: str, access_token: str
    ) -> None:
        await asyncio.to_thread(
            self._save_account, homeserver, user_id, device_id, access_token
        )

    def _save_account(
        self, homeserver: str, user_id: str, device_id: str, access_token: str
    ) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO accounts VALUES (?, ?, ?, ?, ?)",
                (homeserver, user_id, device_id, access_token, time.time()),
            )

    async def clear_account(self, homeserver: str, user_id: str) -> None:
        await asyncio.to_thread(self._clear_account, homeserver, user_id)

    def _clear_account(self, homeserver: str, user_id: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "DELETE FROM accounts WHERE homeserver = ? AND user_id = ?",
                (homeserver, user_id),
            )

    async def save_dm_room(self, user_id: str, room_id: str) -> None:
        await asyncio.to_thread(self._save_dm_room, user_id, room_id)

    def _save_dm_room(self, user_id: str, room_id: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO dm_rooms VALUES (?, ?, ?)",
                (user_id, room_id, time.time()),
            )

    async def room_for_user(self, user_id: str) -> Optional[str]:
        return await asyncio.to_thread(self._room_for_user, user_id)

    def _room_for_user(self, user_id: str) -> Optional[str]:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT room_id FROM dm_rooms WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row[0] if row else None

    async def claim_event(self, event_id: str, room_id: str, sender: str, body: str) -> bool:
        """首次见到该事件返回 True；同步重叠或重放返回 False。"""

        return await asyncio.to_thread(
            self._claim_event, event_id, room_id, sender, body
        )

    def _claim_event(self, event_id: str, room_id: str, sender: str, body: str) -> bool:
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO message_rooms VALUES (?, ?, ?, ?, ?)",
                (event_id, room_id, sender, body[:500], time.time()),
            )
            return cursor.rowcount > 0

    async def message_meta(self, event_id: str) -> Optional[Dict[str, str]]:
        return await asyncio.to_thread(self._message_meta, event_id)

    def _message_meta(self, event_id: str) -> Optional[Dict[str, str]]:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT room_id, sender, body FROM message_rooms WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return {"room_id": row[0], "sender": row[1], "body": row[2]} if row else None

    async def save_failed_event(
        self, event_id: str, room_id: str, error: str, traceback_text: str
    ) -> None:
        await asyncio.to_thread(
            self._save_failed_event, event_id, room_id, error, traceback_text
        )

    def _save_failed_event(
        self, event_id: str, room_id: str, error: str, traceback_text: str
    ) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO failed_events VALUES (?, ?, ?, ?, ?)",
                (event_id, room_id, error, traceback_text, time.time()),
            )

    async def failed_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._failed_event, event_id)

    def _failed_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT room_id, error, traceback, failed_at FROM failed_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "room_id": row[0],
            "error": row[1],
            "traceback": row[2],
            "failed_at": row[3],
        }
