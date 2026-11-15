"""通过真实 Olm 密钥分发和 Megolm 解密检查出站事件，不访问 homeserver。"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    import vodozemac
    from nio import (
        AsyncClient,
        AsyncClientConfig,
        Event,
        MatrixRoom,
        RoomEncryptedAudio,
        RoomEncryptedImage,
        RoomMessageText,
        RoomSendResponse,
        ShareGroupSessionResponse,
        StickerEvent,
    )
    from nio.crypto import OlmDevice, OutboundSession
    from nio.crypto.attachments import encrypt_attachment
except ImportError:  # pragma: no cover - 纯 codec 测试不要求 E2EE 依赖
    raise unittest.SkipTest("matrix-nio[e2e] 未安装") from None

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "matrix_adapter", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
assert _spec is not None and _spec.loader is not None
_package = importlib.util.module_from_spec(_spec)
sys.modules["matrix_adapter"] = _package
_spec.loader.exec_module(_package)

from matrix_adapter.client import MatrixSession, WindowsSafeStore  # noqa: E402
from matrix_adapter.settings import MatrixSettings  # noqa: E402
from matrix_adapter.storage import AdapterStore  # noqa: E402

BOT = "@maibot:example.org"
ALICE = "@alice:example.org"
ROOM = "!room:example.org"


class OutboundEventTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.client = AsyncClient(
            "https://example.org",
            user=BOT,
            device_id="BOT",
            store_path=str(root),
            config=AsyncClientConfig(store=WindowsSafeStore),
        )
        self.client.restore_login(BOT, "BOT", "test-token")
        self.addCleanup(self.client.store.database.close)
        self.addAsyncCleanup(self.client.close)
        room = MatrixRoom(ROOM, BOT, encrypted=True)
        room.members_synced = True
        room.add_member(BOT, "MaiBot", None)
        room.add_member(ALICE, "Alice", None)
        self.client.rooms[ROOM] = room

        # 接收端直接使用 vodozemac API，避免只验证 nio 自己的封装。
        self.receiver = vodozemac.Account()
        self.receiver.generate_one_time_keys(1)
        device = OlmDevice(
            ALICE,
            "RECEIVER",
            {
                "curve25519": self.receiver.curve25519_key.to_base64(),
                "ed25519": self.receiver.ed25519_key.to_base64(),
            },
        )
        self.client.olm.device_store.add(device)
        olm_session = OutboundSession(
            self.client.olm.account,
            device.curve25519,
            next(iter(self.receiver.one_time_keys.values())).to_base64(),
        )
        self.client.olm.session_store.add(device.curve25519, olm_session)
        self.group_session = None
        self.sent = []
        self.transport = patch.object(self.client, "_send", side_effect=self._receive)
        self.transport.start()
        self.addCleanup(self.transport.stop)
        self.session = MatrixSession(
            MatrixSettings(), AdapterStore(root / "adapter.sqlite3"), root, logging.getLogger(__name__)
        )
        self.session.client = self.client

    async def _receive(self, response_class, method, path, data, response_data):
        payload = json.loads(data)
        if response_class is ShareGroupSessionResponse:
            encrypted = payload["messages"][ALICE]["RECEIVER"]
            ciphertext = encrypted["ciphertext"][self.receiver.curve25519_key.to_base64()]
            message = vodozemac.AnyOlmMessage.from_parts(
                ciphertext["type"], base64.b64decode(ciphertext["body"] + "==")
            )
            _, plaintext = self.receiver.create_inbound_session(
                vodozemac.Curve25519PublicKey.from_base64(encrypted["sender_key"]), message.to_pre_key()
            )
            key_event = json.loads(plaintext)
            self.assertEqual(key_event["type"], "m.room_key")
            self.assertEqual(key_event["sender"], BOT)
            self.assertEqual(key_event["sender_device"], "BOT")
            self.assertEqual(key_event["keys"]["ed25519"], self.client.olm.account.identity_keys["ed25519"])
            self.assertEqual(key_event["recipient"], ALICE)
            self.assertEqual(key_event["recipient_keys"]["ed25519"], self.receiver.ed25519_key.to_base64())
            self.assertEqual(key_event["content"]["room_id"], ROOM)
            self.group_session = vodozemac.InboundGroupSession(
                vodozemac.SessionKey(key_event["content"]["session_key"])
            )
            self.assertEqual(key_event["content"]["session_id"], self.group_session.session_id)
            response = ShareGroupSessionResponse(*response_data)
            await self.client.receive_response(response)
            return response
        self.assertIs(response_class, RoomSendResponse)
        if not self.session.settings.plaintext_outbound:
            self.assertIn("/m.room.encrypted/", path)
            self.assertIsNotNone(self.group_session)
            self.assertEqual(payload["session_id"], self.group_session.session_id)
            decrypted = self.group_session.decrypt(vodozemac.MegolmMessage.from_base64(payload["ciphertext"]))
            event = json.loads(decrypted.plaintext)
            self.assertEqual(event["room_id"], ROOM)
        else:
            event = {"type": "m.room.message", "content": payload}
            self.assertIn("/m.room.message/", path)
        event.update(event_id=f"$test{len(self.sent)}", sender=BOT, origin_server_ts=1)
        self.sent.append(event)
        return RoomSendResponse(event["event_id"], ROOM)

    async def test_encrypted_text_is_a_room_message_after_decryption(self) -> None:
        await self.session.send_text(ROOM, "hello", "<b>hello</b>", [ALICE], "$reply")
        event = self.sent[-1]
        self.assertIsInstance(Event.parse_decrypted_event(event), RoomMessageText)
        self.assertEqual(event["content"]["body"], "hello")
        self.assertEqual(event["content"]["m.mentions"], {"user_ids": [ALICE]})
        self.assertEqual(event["content"]["m.relates_to"], {"m.in_reply_to": {"event_id": "$reply"}})

    async def test_plaintext_probe_uses_room_message(self) -> None:
        self.session.settings.plaintext_outbound = True
        await self.session.send_text(ROOM, "PLAINTEXT PROBE")
        self.assertIsInstance(Event.parse_event(self.sent[-1]), RoomMessageText)

    async def test_encrypted_media_types_after_decryption(self) -> None:
        for kind, filename, event_class in (
            ("image", "image.png", RoomEncryptedImage),
            ("voice", "voice.ogg", RoomEncryptedAudio),
        ):
            _, file_info = encrypt_attachment(b"media")
            attachment = {"file": {**file_info, "url": "mxc://example.org/media"}}
            with self.subTest(kind=kind), patch.object(
                self.session, "upload_media", new=AsyncMock(return_value=attachment)
            ):
                await self.session.send_media(ROOM, kind, b"media", filename)
                self.assertIsInstance(Event.parse_decrypted_event(self.sent[-1]), event_class)

    async def test_sticker_keeps_its_own_event_type(self) -> None:
        with patch.object(self.session, "upload_media", new=AsyncMock(return_value={"url": "mxc://example.org/media"})):
            await self.session.send_media(ROOM, "emoji", b"media", "sticker.png")
        self.assertIsInstance(Event.parse_decrypted_event(self.sent[-1]), StickerEvent)


if __name__ == "__main__":
    unittest.main()
