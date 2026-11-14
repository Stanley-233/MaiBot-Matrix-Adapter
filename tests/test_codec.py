"""codec 纯函数测试；不依赖 matrix-nio 与宿主。"""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codec  # noqa: E402

BOT = "@maibot:example.org"
ALICE = "@alice:example.org"
ROOM = "!abc:example.org"


class BinarySegmentTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        segment = codec.binary_segment("image", b"hello", "a.png")
        self.assertEqual(segment["type"], "image")
        self.assertEqual(segment["data"], "a.png")
        self.assertEqual(codec.decode_binary(segment), b"hello")

    def test_invalid_base64(self) -> None:
        with self.assertRaises(ValueError):
            codec.decode_binary({"type": "image", "binary_data_base64": "!!!"})


class DecodeTextTest(unittest.TestCase):
    def test_plain_body(self) -> None:
        segments, mentioned = codec.decode_text({"msgtype": "m.text", "body": "hi"}, BOT)
        self.assertEqual(segments, [{"type": "text", "data": "hi"}])
        self.assertFalse(mentioned)

    def test_formatted_mention(self) -> None:
        content = {
            "msgtype": "m.text",
            "body": f"{ALICE}: hi",
            "format": "org.matrix.custom.html",
            "formatted_body": (
                f'<a href="https://matrix.to/#/{ALICE}">Alice</a>: hi'
            ),
        }
        segments, mentioned = codec.decode_text(content, BOT)
        self.assertEqual(segments[0]["type"], "at")
        self.assertEqual(segments[0]["data"]["target_user_id"], ALICE)
        self.assertEqual(segments[0]["data"]["target_user_nickname"], "Alice")
        self.assertFalse(mentioned)

    def test_m_mentions_marks_bot(self) -> None:
        content = {
            "msgtype": "m.text",
            "body": "hey bot",
            "m.mentions": {"user_ids": [BOT]},
        }
        _, mentioned = codec.decode_text(content, BOT)
        self.assertTrue(mentioned)

    def test_emote_prefix(self) -> None:
        segments, _ = codec.decode_text({"msgtype": "m.emote", "body": "waves"}, BOT)
        self.assertEqual(segments[0]["data"], "* waves")


class DecodeMessageTest(unittest.TestCase):
    def _decode(self, **overrides):
        payload = {
            "event_id": "$event1",
            "room_id": ROOM,
            "sender": ALICE,
            "timestamp_ms": 1_700_000_000_000,
            "event_type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello", "m.mentions": {"user_ids": [BOT]}},
            "bot_user_id": BOT,
            "is_direct": False,
            "room_name": "Test Room",
            "sender_name": "Alice",
            "media": [],
        }
        payload.update(overrides)
        return codec.decode_message(**payload)

    def test_group_message(self) -> None:
        message = self._decode()
        assert message is not None
        self.assertEqual(message["platform"], "matrix")
        self.assertEqual(message["message_id"], "$event1")
        self.assertEqual(message["timestamp"], "1700000000")
        self.assertEqual(message["message_info"]["group_info"]["group_id"], ROOM)
        self.assertEqual(message["message_info"]["group_info"]["group_name"], "Test Room")
        self.assertTrue(message["is_at"])
        self.assertFalse(message["is_command"])

    def test_direct_message_has_no_group(self) -> None:
        message = self._decode(is_direct=True)
        assert message is not None
        self.assertNotIn("group_info", message["message_info"])

    def test_command_detection(self) -> None:
        message = self._decode(content={"msgtype": "m.text", "body": "/help"})
        assert message is not None
        self.assertTrue(message["is_command"])

    def test_reply_segment(self) -> None:
        message = self._decode(
            content={
                "msgtype": "m.text",
                "body": "re",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$prev"}},
            },
            reply_to="$prev",
            reply_context={"sender": BOT, "sender_name": "MaiBot", "body": "old"},
        )
        assert message is not None
        self.assertEqual(message["reply_to"], "$prev")
        self.assertEqual(message["raw_message"][0]["type"], "reply")
        self.assertTrue(message["is_at"])

    def test_empty_message_returns_none(self) -> None:
        self.assertIsNone(self._decode(content={"msgtype": "m.text", "body": ""}))

    def test_image_flag(self) -> None:
        media = [codec.binary_segment("image", b"\x89PNG", "a.png")]
        message = self._decode(content={"msgtype": "m.image", "body": "a.png"}, media=media)
        assert message is not None
        self.assertTrue(message["is_picture"])
        self.assertFalse(message["is_emoji"])


class DecodeReactionTest(unittest.TestCase):
    def test_notify(self) -> None:
        message = codec.decode_reaction(
            event_id="$r1",
            room_id=ROOM,
            sender=ALICE,
            sender_name="Alice",
            timestamp_ms=1_700_000_000_000,
            key="👍",
            reacts_to="$m1",
            bot_user_id=BOT,
            is_direct=False,
            room_name="Test Room",
            target_context={"sender": BOT, "sender_name": "MaiBot"},
        )
        self.assertTrue(message["is_notify"])
        self.assertEqual(message["reply_to"], "$m1")
        self.assertEqual(message["message_id"], "reaction:$r1")
        self.assertIn("👍", message["raw_message"][0]["data"])


class EncodeMessageTest(unittest.TestCase):
    def test_text_with_mention_and_reply(self) -> None:
        segments = [
            {"type": "reply", "data": {"target_message_id": "$prev"}},
            {"type": "text", "data": "hi "},
            {
                "type": "at",
                "data": {"target_user_id": ALICE, "target_user_nickname": "Alice"},
            },
            {"type": "text", "data": "!"},
        ]
        parts = codec.encode_message(segments)
        self.assertEqual(len(parts), 1)
        part = parts[0]
        self.assertEqual(part["body"], "hi Alice!")
        self.assertIn(f"https://matrix.to/#/{ALICE}", part["html"])
        self.assertEqual(part["mentions"], [ALICE])
        self.assertEqual(codec.reply_target({"raw_message": segments}), "$prev")

    def test_long_text_split(self) -> None:
        parts = codec.encode_message([{"type": "text", "data": "x" * 25}], limit=10)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(part["kind"] == "text" for part in parts))

    def test_media(self) -> None:
        parts = codec.encode_message(
            [codec.binary_segment("voice", b"RIFF", "voice.wav")]
        )
        self.assertEqual(parts[0]["kind"], "voice")
        self.assertEqual(parts[0]["binary"], b"RIFF")
        self.assertEqual(parts[0]["filename"], "voice.wav")

    def test_forward_unsupported(self) -> None:
        with self.assertRaises(ValueError):
            codec.encode_message([{"type": "forward", "data": []}])

    def test_outbound_room_priority(self) -> None:
        message = {
            "message_info": {
                "group_info": {"group_id": ROOM},
                "additional_config": {"platform_io_target_user_id": ALICE},
            }
        }
        self.assertEqual(codec.outbound_room_id(message), ROOM)
        with self.assertRaises(ValueError):
            codec.outbound_room_id({"message_info": {}})


class BinaryEncodeTest(unittest.TestCase):
    def test_base64_payload_is_ascii(self) -> None:
        segment = codec.binary_segment("image", b"\x00\xff")
        base64.b64decode(segment["binary_data_base64"], validate=True)


if __name__ == "__main__":
    unittest.main()
