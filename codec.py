"""Matrix 事件与宿主标准消息段之间的转换；不依赖主程序内部模块。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html as html_lib
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Mapping, Optional, Tuple

PLATFORM = "matrix"

_MATRIX_TO = re.compile(r"^https?://matrix\.to/#/(@[^/?#\s]+)$")
_TEXT_MSGTYPES = {"m.text", "m.notice", "m.emote"}
_MEDIA_SEGMENTS = {"image", "emoji", "voice"}
_DEFAULT_FILENAMES = {"image": "image", "emoji": "sticker", "voice": "voice"}


def binary_segment(kind: str, binary: bytes, label: str = "") -> Dict[str, Any]:
    """把二进制媒体包装成宿主标准的媒体消息段。"""

    if not binary:
        raise ValueError("媒体内容为空")
    return {
        "type": kind,
        "data": label,
        "hash": hashlib.sha256(binary).hexdigest(),
        "binary_data_base64": base64.b64encode(binary).decode("ascii"),
    }


def decode_binary(segment: Mapping[str, Any]) -> bytes:
    value = segment.get("binary_data_base64")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{segment.get('type')} 消息段缺少二进制内容")
    try:
        binary = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("媒体 Base64 无效") from exc
    if not binary:
        raise ValueError("媒体内容为空")
    return binary


class _MessageHTMLParser(HTMLParser):
    """把 Matrix formatted_body 拆成文本与 matrix.to 提及。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: List[Tuple[str, Any]] = []
        self._anchor: Optional[str] = None
        self._anchor_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "a" and self._anchor is None:
            href = dict(attrs).get("href") or ""
            match = _MATRIX_TO.match(href)
            if match is not None:
                self._anchor = match.group(1)
                self._anchor_parts = []
                return
        if tag == "br":
            self.tokens.append(("text", "\n"))
        elif tag == "p" and self.tokens:
            self.tokens.append(("text", "\n"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            label = "".join(self._anchor_parts).strip().lstrip("@") or self._anchor
            self.tokens.append(
                ("at", {"target_user_id": self._anchor, "target_user_nickname": label})
            )
            self._anchor = None
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor_parts.append(data)
        elif data:
            self.tokens.append(("text", data))


def _segments_from_html(formatted: str) -> List[Dict[str, Any]]:
    parser = _MessageHTMLParser()
    parser.feed(formatted)
    parser.close()

    segments: List[Dict[str, Any]] = []
    for kind, value in parser.tokens:
        if kind == "text":
            text = value
            if not text:
                continue
            if segments and segments[-1]["type"] == "text":
                segments[-1]["data"] += text
            else:
                segments.append({"type": "text", "data": text})
        else:
            segments.append({"type": "at", "data": dict(value)})

    if segments and segments[0]["type"] == "text":
        segments[0]["data"] = segments[0]["data"].lstrip("\n")
    if segments and segments[-1]["type"] == "text":
        segments[-1]["data"] = segments[-1]["data"].rstrip()
    return [segment for segment in segments if segment["type"] != "text" or segment["data"]]


def decode_text(
    content: Mapping[str, Any], bot_user_id: str
) -> Tuple[List[Dict[str, Any]], bool]:
    """解析 m.text/m.notice/m.emote，返回消息段与是否提及机器人。"""

    body = str(content.get("body", ""))
    formatted = content.get("formatted_body")
    segments: List[Dict[str, Any]] = []
    if isinstance(formatted, str) and content.get("format") == "org.matrix.custom.html":
        segments = _segments_from_html(formatted)
    if not segments:
        segments = [{"type": "text", "data": body}] if body else []
    if content.get("msgtype") == "m.emote" and segments:
        if segments[0]["type"] == "text":
            segments[0]["data"] = "* " + segments[0]["data"]
        else:
            segments.insert(0, {"type": "text", "data": "* "})

    mentions: set[str] = set()
    raw_mentions = content.get("m.mentions")
    if isinstance(raw_mentions, Mapping):
        mentions = {str(user) for user in raw_mentions.get("user_ids") or []}
    for segment in segments:
        if segment["type"] == "at":
            mentions.add(str(segment["data"]["target_user_id"]))

    return segments, bot_user_id in mentions


def decode_message(
    *,
    event_id: str,
    room_id: str,
    sender: str,
    timestamp_ms: int,
    event_type: str,
    content: Mapping[str, Any],
    bot_user_id: str,
    is_direct: bool,
    room_name: str,
    sender_name: str,
    media: List[Dict[str, Any]],
    encrypted: bool = False,
    reply_to: Optional[str] = None,
    reply_context: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """把一条 Matrix 聊天事件转换成宿主标准消息字典。"""

    msgtype = str(content.get("msgtype", ""))
    if event_type == "m.sticker":
        segments: List[Dict[str, Any]] = []
        mentioned = False
    elif msgtype in _TEXT_MSGTYPES:
        segments, mentioned = decode_text(content, bot_user_id)
    else:
        segments, mentioned = [], False
    segments = list(segments) + list(media)
    if not segments:
        return None

    if reply_to is not None:
        context = reply_context or {}
        segments.insert(
            0,
            {
                "type": "reply",
                "data": {
                    "target_message_id": reply_to,
                    "target_message_content": str(context.get("body", ""))[:200],
                    "target_message_sender_id": context.get("sender"),
                    "target_message_sender_nickname": context.get("sender_name"),
                },
            },
        )
        if context.get("sender") == bot_user_id:
            mentioned = True

    info: Dict[str, Any] = {
        "user_info": {"user_id": sender, "user_nickname": sender_name},
        "additional_config": {
            "matrix_room_id": room_id,
            "matrix_event_id": event_id,
            "matrix_sender_id": sender,
            "matrix_event_type": event_type,
            "matrix_encrypted": encrypted,
        },
    }
    if not is_direct:
        info["group_info"] = {"group_id": room_id, "group_name": room_name or room_id}

    body = str(content.get("body", ""))
    result: Dict[str, Any] = {
        "platform": PLATFORM,
        "account_id": bot_user_id,
        "scope": None,
        "message_id": event_id,
        "timestamp": str(int(timestamp_ms // 1000)),
        "message_info": info,
        "raw_message": segments,
        "is_at": mentioned,
        "is_mentioned": mentioned,
        "is_picture": any(segment["type"] == "image" for segment in media),
        "is_emoji": any(segment["type"] == "emoji" for segment in media),
        "is_command": msgtype == "m.text" and body.startswith(("!", "/")),
    }
    if reply_to is not None:
        result["reply_to"] = reply_to
    return result


def decode_reaction(
    *,
    event_id: str,
    room_id: str,
    sender: str,
    sender_name: str,
    timestamp_ms: int,
    key: str,
    reacts_to: str,
    bot_user_id: str,
    is_direct: bool,
    room_name: str,
    target_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """把 m.reaction 转成 is_notify 事件通知，引用原消息。"""

    text = f"[事件-消息表态] {sender_name} 对消息 {reacts_to} 添加了 {key} 表态"
    info: Dict[str, Any] = {
        "user_info": {"user_id": sender, "user_nickname": sender_name},
        "additional_config": {
            "matrix_room_id": room_id,
            "matrix_event_id": event_id,
            "matrix_sender_id": sender,
            "matrix_event_type": "m.reaction",
            "matrix_reaction_key": key,
            "matrix_reaction_target": reacts_to,
        },
    }
    if not is_direct:
        info["group_info"] = {"group_id": room_id, "group_name": room_name or room_id}
    if target_context is not None and target_context.get("sender"):
        info["additional_config"]["matrix_reaction_target_sender"] = target_context["sender"]

    return {
        "platform": PLATFORM,
        "account_id": bot_user_id,
        "scope": None,
        "message_id": f"reaction:{event_id}",
        "timestamp": str(int(timestamp_ms // 1000)),
        "message_info": info,
        "raw_message": [{"type": "text", "data": text}],
        "reply_to": reacts_to,
        "is_notify": True,
        "is_at": False,
        "is_mentioned": False,
        "is_picture": False,
        "is_emoji": False,
        "is_command": False,
    }


def outbound_room_id(message: Mapping[str, Any]) -> str:
    """出站目标：群信息优先，其次宿主给出的平台目标字段。"""

    info = message.get("message_info") or {}
    group = info.get("group_info")
    config = info.get("additional_config") or {}
    target = group.get("group_id") if isinstance(group, Mapping) else None
    target = target or config.get("platform_io_target_group_id") or config.get(
        "platform_io_target_user_id"
    )
    if not target:
        raise ValueError("出站消息缺少目标房间 ID")
    return str(target)


def reply_target(message: Mapping[str, Any]) -> Optional[str]:
    """只有显式 reply 段才表示 Matrix 引用；宿主的 reply_to 只是上下文锚点。"""

    for segment in message.get("raw_message") or []:
        if segment.get("type") != "reply":
            continue
        data = segment.get("data")
        target = data.get("target_message_id") if isinstance(data, Mapping) else data
        return str(target) if target else None
    return None


def _split_plain(text: str, limit: int) -> List[str]:
    if limit <= 0:
        raise ValueError("文本分段上限必须为正数")
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit)
        if boundary <= 0:
            boundary = limit
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _render_text(tokens: List[Tuple[str, Any]], limit: int) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    current: List[Tuple[str, Any]] = []
    length = [0]

    def flush() -> None:
        if not current:
            return
        body_parts: List[str] = []
        html_parts: List[str] = []
        mentions: List[str] = []
        for kind, value in current:
            if kind == "text":
                body_parts.append(value)
                html_parts.append(html_lib.escape(value).replace("\n", "<br/>"))
            else:
                user_id, label = value
                body_parts.append(label)
                html_parts.append(
                    f'<a href="https://matrix.to/#/{html_lib.escape(user_id)}">'
                    f"{html_lib.escape(label)}</a>"
                )
                if user_id not in mentions:
                    mentions.append(user_id)
        chunks.append(
            {
                "kind": "text",
                "body": "".join(body_parts),
                "html": "".join(html_parts),
                "mentions": mentions,
            }
        )
        current.clear()
        length[0] = 0

    for kind, value in tokens:
        if kind == "text":
            for piece in _split_plain(value, limit):
                if current and length[0] + len(piece) > limit:
                    flush()
                current.append(("text", piece))
                length[0] += len(piece)
        else:
            user_id, label = value
            if current and length[0] + len(label) > limit:
                flush()
            current.append(("at", (user_id, label)))
            length[0] += len(label)
    flush()
    return chunks


def encode_message(segments: List[Dict[str, Any]], limit: int = 8000) -> List[Dict[str, Any]]:
    """把宿主消息段转换成 Matrix 出站分片；文本按长度拆成多条事件。"""

    parts: List[Dict[str, Any]] = []
    tokens: List[Tuple[str, Any]] = []

    def flush_text() -> None:
        if tokens:
            parts.extend(_render_text(tokens, limit))
            tokens.clear()

    for segment in segments:
        kind = segment.get("type")
        data = segment.get("data")
        if kind == "text":
            if not isinstance(data, str):
                raise ValueError("文本段必须是字符串")
            tokens.append(("text", data))
        elif kind == "at":
            if not isinstance(data, Mapping):
                raise ValueError("@ 段必须包含目标用户信息")
            user_id = str(data["target_user_id"])
            label = str(
                data.get("target_user_cardname")
                or data.get("target_user_nickname")
                or user_id
            )
            tokens.append(("at", (user_id, label)))
        elif kind == "reply":
            continue  # 回复关系由整条出站消息统一处理。
        elif kind in _MEDIA_SEGMENTS:
            flush_text()
            filename = str(data) if isinstance(data, str) and data else _DEFAULT_FILENAMES[kind]
            parts.append(
                {
                    "kind": kind,
                    "binary": decode_binary(segment),
                    "filename": filename,
                }
            )
        elif kind == "forward":
            raise ValueError("Matrix 适配器暂不支持合并转发消息段")
        else:
            raise ValueError(f"不支持的出站消息段: {kind}")
    flush_text()
    return parts
