"""Matrix 适配器插件：matrix-nio 双向消息网关。"""

from __future__ import annotations

import asyncio
import traceback
from contextlib import suppress
from typing import Any, ClassVar, Dict, List, Optional, Type

import msgpack
from maibot_sdk import (
    ConfigWidget,
    ConfigWidgetState,
    HookHandler,
    MaiBotPlugin,
    MessageGateway,
    PluginConfigBase,
)

from nio import MegolmEvent, ReactionEvent, RoomMessage, StickerEvent

from .client import (
    MatrixAuthError,
    MatrixLoginError,
    MatrixSession,
    ensure_encryption_available,
)
from .codec import (
    PLATFORM,
    binary_segment,
    decode_message,
    decode_reaction,
    encode_message,
    outbound_room_id,
    reply_target,
)
from .settings import AdapterSettings
from .storage import AdapterStore

GATEWAY_NAME = "matrix_gateway"
MAX_GATEWAY_MESSAGE_BYTES = 16 * 1024 * 1024 - 64 * 1024
TYPING_INTERVAL_SEC = 4.0
TYPING_MAX_SEC = 180.0


def validate_message_size(message: Dict[str, Any]) -> None:
    """宿主 IPC 每帧上限 16 MB，媒体转 Base64 后要预留信封空间。"""

    size = len(msgpack.packb(message, use_bin_type=True))
    if size > MAX_GATEWAY_MESSAGE_BYTES:
        raise ValueError(
            f"消息 {message.get('message_id', '<未知>')} 转换后超过 MaiBot 网关的 16 MB 帧限制: {size} bytes"
        )


class MatrixAdapterPlugin(MaiBotPlugin):
    config_model: ClassVar[Optional[Type[PluginConfigBase]]] = AdapterSettings

    config_widgets: ClassVar[List[ConfigWidget]] = [
        ConfigWidget(id="connection", kind="status", label="Matrix 连接状态"),
        ConfigWidget(id="reconnect", kind="action", label="重新连接"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._worker: Optional[asyncio.Task[None]] = None
        self._session: Optional[MatrixSession] = None
        self._store: Optional[AdapterStore] = None
        self._account_id = ""
        self._ready = False
        self._last_error = ""
        self._lifecycle_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._typing_tasks: Dict[str, asyncio.Task[None]] = {}
        self._join_tasks: Dict[str, asyncio.Task[None]] = {}

    # ---------------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        self._store = AdapterStore(self.ctx.paths.data_dir / "matrix.sqlite3")
        await self._store.initialize()
        if self.config.plugin.enabled:
            self._start()

    async def on_unload(self) -> None:
        async with self._lifecycle_lock:
            await self._stop()

    async def on_config_update(
        self, scope: str, config_data: Dict[str, Any], version: str
    ) -> None:
        del version
        if scope != "self":
            return
        async with self._lifecycle_lock:
            await self._stop()
            self.set_plugin_config(config_data)
            if self.config.plugin.enabled:
                self._start()

    def _start(self) -> None:
        if not self.config.matrix.user_id.strip():
            self.ctx.logger.error("Matrix 适配器缺少 matrix.user_id，请在插件配置中填写")
            return
        self._worker = asyncio.create_task(self._run(), name="matrix-adapter")

    async def _stop(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        for room_id in list(self._typing_tasks):
            await self._stop_typing(room_id)
        for task in list(self._join_tasks.values()):
            task.cancel()
        self._join_tasks.clear()
        session = self._session
        self._session = None
        if session is not None:
            await session.close()
        self._account_id = ""
        self._ready = False

    def _require_store(self) -> AdapterStore:
        if self._store is None:
            raise RuntimeError("Matrix 存储尚未初始化")
        return self._store

    async def get_config_widget_state(self) -> Dict[str, ConfigWidgetState]:
        configured = bool(
            self.config.matrix.user_id.strip()
            and (
                self.config.matrix.password.strip()
                or self.config.matrix.access_token.strip()
            )
        )
        if self._ready and self._session is not None:
            text = f"已连接：{self._session.user_id} · device {self._session.device_id}"
            tone = "success"
        elif self._last_error:
            text = f"连接失败：{self._last_error}"
            tone = "error"
        elif self._worker is not None and not self._worker.done():
            text = "连接中 / 等待重连"
            tone = "neutral"
        elif configured:
            text = "尚未连接"
            tone = "neutral"
        else:
            text = "请填写 matrix.user_id 与密码或访问令牌"
            tone = "neutral"
        return {
            "connection": ConfigWidgetState(text=text, tone=tone),
            "reconnect": ConfigWidgetState(disabled=not configured),
        }

    async def on_config_widget_action(self, action_id: str) -> None:
        if action_id != "reconnect":
            raise ValueError(f"未知配置页操作: {action_id}")
        async with self._lifecycle_lock:
            await self._stop()
            self._last_error = ""
            if self.config.plugin.enabled:
                self._start()

    # ---------------------------------------------------------------- 连接任务

    async def _set_ready(self, ready: bool) -> None:
        accepted = await self.ctx.gateway.update_state(
            gateway_name=GATEWAY_NAME,
            ready=ready,
            platform=PLATFORM,
            account_id=self._account_id,
            metadata={
                "protocol": "matrix_client_server",
                "device_id": self._session.device_id if self._session else "",
                "e2ee": True,
            },
        )
        self._ready = ready and accepted
        if ready and not accepted:
            raise RuntimeError("MaiBot 未接受 Matrix 网关路由")

    async def _run(self) -> None:
        settings = self.config.matrix
        if settings.plaintext_outbound:
            self.ctx.logger.warning(
                "已启用 matrix.plaintext_outbound：加密房间也会直接发明文消息，仅用于联调"
            )
        while True:
            session: Optional[MatrixSession] = None
            try:
                ensure_encryption_available()
                session = MatrixSession(
                    settings,
                    self._require_store(),
                    self.ctx.paths.data_dir,
                    self.ctx.logger,
                )
                await session.connect()
                self._session = session
                self._account_id = session.user_id
                await self._set_ready(True)
                self._last_error = ""
                self.ctx.logger.info(
                    "Matrix 网关已就绪: %s device=%s", session.user_id, session.device_id
                )
                await session.run(self._handle_sync)
                break
            except asyncio.CancelledError:
                raise
            except MatrixLoginError as exc:
                self._last_error = str(exc)
                if exc.fatal:
                    self.ctx.logger.error("Matrix 连接终止: %s", exc)
                    return
                self.ctx.logger.warning("Matrix 登录暂时失败: %s；稍后重试", exc)
            except MatrixAuthError as exc:
                self._last_error = str(exc)
                self.ctx.logger.warning("Matrix 访问令牌失效，清理会话后重新登录: %s", exc)
                with suppress(Exception):
                    await self._require_store().clear_account(
                        settings.homeserver.strip().rstrip("/"), settings.user_id.strip()
                    )
            except Exception as exc:  # noqa: BLE001 - 任务级错误需要记录并重连
                self._last_error = str(exc)
                self.ctx.logger.error(
                    "Matrix 接收任务中断，将重新连接: %s", exc, exc_info=True
                )
            finally:
                self._session = None
                self._account_id = ""
                self._ready = False
                for room_id in list(self._typing_tasks):
                    await self._stop_typing(room_id)
                for task in list(self._join_tasks.values()):
                    task.cancel()
                self._join_tasks.clear()
                with suppress(Exception):
                    await self._set_ready(False)
                if session is not None:
                    await session.close()
            await asyncio.sleep(settings.reconnect_delay_sec)

    # ---------------------------------------------------------------- 入站同步

    async def _handle_sync(self, response: Any) -> None:
        session = self._session
        if session is None:
            return

        for room_id in response.rooms.invite:
            await self._handle_invite(room_id, session)

        for room_id, joined in response.rooms.join.items():
            for event in joined.timeline.events:
                await self._handle_event(room_id, event, session)

    async def _handle_invite(self, room_id: str, session: MatrixSession) -> None:
        if not self.config.matrix.auto_join:
            self.ctx.logger.info("收到 Matrix 房间邀请但未开启自动加入: %s", room_id)
            return
        existing = self._join_tasks.get(room_id)
        if existing is not None and not existing.done():
            return
        try:
            await session.join(room_id)
            self.ctx.logger.info("已加入 Matrix 房间: %s", room_id)
        except Exception as exc:  # noqa: BLE001 - 单个邀请失败不影响后续事件
            self.ctx.logger.warning(
                "加入 Matrix 房间失败 %s: %s；将自动重试", room_id, exc
            )
            self._join_tasks[room_id] = asyncio.create_task(
                self._retry_join(room_id, session), name=f"matrix-join-{room_id}"
            )

    async def _retry_join(self, room_id: str, session: MatrixSession) -> None:
        try:
            for attempt, delay in enumerate((5, 15, 45), start=1):
                await asyncio.sleep(delay)
                try:
                    await session.join(room_id)
                    self.ctx.logger.info(
                        "第 %s 次重试后已加入 Matrix 房间: %s", attempt, room_id
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - 重试继续
                    self.ctx.logger.warning(
                        "第 %s 次重试加入 Matrix 房间 %s 失败: %s",
                        attempt,
                        room_id,
                        exc,
                    )
        finally:
            self._join_tasks.pop(room_id, None)

    async def _handle_event(self, room_id: str, event: Any, session: MatrixSession) -> None:
        try:
            if isinstance(event, MegolmEvent):
                self.ctx.logger.debug(
                    "Matrix 事件无法解密，已自动请求房间密钥: %s", event.event_id
                )
                return
            if isinstance(event, ReactionEvent):
                await self._handle_reaction(room_id, event, session)
                return
            if isinstance(event, (RoomMessage, StickerEvent)):
                await self._handle_message(room_id, event, session)
        except Exception as exc:  # noqa: BLE001 - 单条事件失败不能中断同步
            self.ctx.logger.error(
                "Matrix 事件处理失败: room=%s event=%s error=%s",
                room_id,
                getattr(event, "event_id", "?"),
                exc,
                exc_info=True,
            )
            with suppress(Exception):
                await self._require_store().save_failed_event(
                    str(getattr(event, "event_id", "")),
                    room_id,
                    str(exc),
                    traceback.format_exc(),
                )

    async def _handle_message(
        self, room_id: str, event: Any, session: MatrixSession
    ) -> None:
        sender = str(event.sender)
        if not sender or sender == session.user_id:
            return
        content = event.source.get("content") or {}
        if not isinstance(content, dict):
            return
        if content.get("m.new_content") is not None:
            return  # 编辑事件不重复进入聊天流。

        store = self._require_store()
        body = str(content.get("body", ""))
        if await store.message_meta(event.event_id) is not None:
            return

        encrypted = bool(getattr(event, "decrypted", False))
        is_direct = session.is_direct(room_id, self.config.matrix.dm_max_members)
        if is_direct:
            await store.save_dm_room(sender, room_id)

        media_segments = await self._extract_media(event, content, session)
        reply_to, reply_context = await self._reply_context(content, room_id, session)

        message = decode_message(
            event_id=event.event_id,
            room_id=room_id,
            sender=sender,
            timestamp_ms=int(event.server_timestamp or 0),
            event_type=str(event.source.get("type", "")),
            content=content,
            bot_user_id=session.user_id,
            is_direct=is_direct,
            room_name=session.room_name(room_id),
            sender_name=session.user_name(room_id, sender),
            media=media_segments,
            encrypted=encrypted,
            reply_to=reply_to,
            reply_context=reply_context,
        )
        if message is None:
            return
        await store.claim_event(event.event_id, room_id, sender, body)
        await self._route_inbound(message)
        if self.config.matrix.send_read_receipt:
            with suppress(Exception):
                await session.send_read_receipt(room_id, event.event_id)

    async def _handle_reaction(
        self, room_id: str, event: ReactionEvent, session: MatrixSession
    ) -> None:
        sender = str(event.sender)
        if not sender or sender == session.user_id:
            return
        store = self._require_store()
        body = f"reaction:{event.key}:{event.reacts_to}"
        if await store.message_meta(event.event_id) is not None:
            return
        target = await store.message_meta(event.reacts_to)
        if target is not None and target.get("sender"):
            target["sender_name"] = session.user_name(room_id, target["sender"])
        is_direct = session.is_direct(room_id, self.config.matrix.dm_max_members)
        message = decode_reaction(
            event_id=event.event_id,
            room_id=room_id,
            sender=sender,
            sender_name=session.user_name(room_id, sender),
            timestamp_ms=int(event.server_timestamp or 0),
            key=str(event.key),
            reacts_to=str(event.reacts_to),
            bot_user_id=session.user_id,
            is_direct=is_direct,
            room_name=session.room_name(room_id),
            target_context=target,
        )
        await store.claim_event(event.event_id, room_id, sender, body)
        await self._route_inbound(message)

    async def _extract_media(
        self, event: Any, content: Dict[str, Any], session: MatrixSession
    ) -> List[Dict[str, Any]]:
        if isinstance(event, StickerEvent):
            binary = await session.download_media(content)
            segment = await asyncio.to_thread(
                binary_segment, "emoji", binary, str(content.get("body", ""))
            )
            return [segment]
        msgtype = str(content.get("msgtype", ""))
        info = content.get("info") if isinstance(content.get("info"), dict) else {}
        mime = str(info.get("mimetype", ""))
        if msgtype == "m.image" or (msgtype == "m.file" and mime.startswith("image/")):
            binary = await session.download_media(content)
            segment = await asyncio.to_thread(
                binary_segment, "image", binary, str(content.get("body", ""))
            )
            return [segment]
        if msgtype == "m.audio":
            binary = await session.download_media(content)
            segment = await asyncio.to_thread(
                binary_segment, "voice", binary, str(content.get("body", ""))
            )
            return [segment]
        if msgtype in {"m.video", "m.file"}:
            self.ctx.logger.info(
                "Matrix 暂不支持该消息类型: event=%s msgtype=%s mime=%s",
                getattr(event, "event_id", "?"),
                msgtype,
                mime,
            )
        return []

    async def _reply_context(
        self, content: Dict[str, Any], room_id: str, session: MatrixSession
    ) -> tuple[Optional[str], Optional[Dict[str, str]]]:
        relates = content.get("m.relates_to")
        if not isinstance(relates, dict):
            return None, None
        reply = relates.get("m.in_reply_to")
        if not isinstance(reply, dict) or not reply.get("event_id"):
            return None, None
        target = str(reply["event_id"])
        context = await self._require_store().message_meta(target)
        if context is not None:
            context["sender_name"] = session.user_name(
                context.get("room_id") or room_id, context.get("sender", "")
            )
        return target, context

    async def _route_inbound(self, message: Dict[str, Any]) -> None:
        await asyncio.to_thread(validate_message_size, message)
        accepted = await self.ctx.gateway.route_message(
            gateway_name=GATEWAY_NAME,
            message=message,
            external_message_id=message["message_id"],
            dedupe_key=message["message_id"],
        )
        if not accepted:
            self.ctx.logger.info("MaiBot 未接收 Matrix 消息: %s", message["message_id"])

    # ---------------------------------------------------------------- 输入状态

    @HookHandler(
        "maisaka.replyer.before_request",
        name="matrix_reply_typing",
        description="选中 reply 并开始生成回复时显示 Matrix 输入状态",
        timeout_ms=3000,
    )
    async def on_reply_generation(self, session_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if not session_id or not self._ready or self._session is None:
            return {"action": "continue"}
        try:
            streams = await self._get_streams()
        except Exception as exc:  # noqa: BLE001 - 输入状态失败不影响回复生成
            self.ctx.logger.debug("查询 Matrix 聊天流失败: %s", exc)
            return {"action": "continue"}
        matches = [stream for stream in streams if stream["session_id"] == session_id]
        if len(matches) != 1:
            return {"action": "continue"}
        stream = matches[0]
        if (
            stream.get("platform") != PLATFORM
            or stream.get("account_id") != self._account_id
            or stream.get("scope")
        ):
            return {"action": "continue"}
        target = stream.get("group_id") if stream.get("is_group_session") else stream.get("user_id")
        if not target:
            return {"action": "continue"}
        room_id = str(target)
        if room_id.startswith("@"):
            mapped = await self._require_store().room_for_user(room_id)
            if mapped is None:
                return {"action": "continue"}
            room_id = mapped
        self._start_typing(room_id)
        return {"action": "continue"}

    async def _get_streams(self) -> List[Dict[str, Any]]:
        streams = await self.ctx.chat.get_all_streams(platform=PLATFORM)
        if isinstance(streams, dict) and streams.get("success") is False:
            raise RuntimeError(f"查询聊天流失败: {streams.get('error')}")
        if not isinstance(streams, list):
            raise TypeError("SDK chat.get_all_streams 必须返回聊天流列表")
        return streams

    def _start_typing(self, room_id: str) -> None:
        task = self._typing_tasks.get(room_id)
        if task is not None and not task.done():
            return
        self._typing_tasks[room_id] = asyncio.create_task(
            self._typing_loop(room_id), name=f"matrix-typing-{room_id}"
        )

    async def _typing_loop(self, room_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TYPING_MAX_SEC
        try:
            while loop.time() < deadline:
                session = self._session
                if session is None:
                    return
                await session.send_typing(room_id, True)
                await asyncio.sleep(TYPING_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        finally:
            session = self._session
            if session is not None:
                with suppress(Exception):
                    await session.send_typing(room_id, False)
            self._typing_tasks.pop(room_id, None)

    async def _stop_typing(self, room_id: str) -> None:
        task = self._typing_tasks.pop(room_id, None)
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # ---------------------------------------------------------------- 出站发送

    @MessageGateway(
        name=GATEWAY_NAME,
        route_type="duplex",
        platform=PLATFORM,
        protocol="matrix_client_server",
        description="Matrix 文本、图片、表情、语音、@ 与回复引用双向消息网关",
        timeout_ms=180000,
    )
    async def send_to_matrix(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del route, metadata, kwargs
        async with self._send_lock:
            session = self._session
            if not self._ready or session is None:
                return {"success": False, "error": "Matrix 网关尚未就绪"}
            sent: List[str] = []
            try:
                if (
                    message.get("platform") != PLATFORM
                    or message.get("account_id") != self._account_id
                ):
                    raise ValueError("出站消息的路由身份与 Matrix 网关不一致")
                room_id = outbound_room_id(message)
                if not room_id.startswith("!"):
                    if not room_id.startswith("@"):
                        raise ValueError(f"Matrix 房间 ID 必须以 ! 开头: {room_id}")
                    mapped = await self._require_store().room_for_user(room_id)
                    if mapped is None:
                        raise ValueError(
                            f"尚未与该用户建立私聊房间: {room_id}；请先让对方发送一条消息"
                        )
                    room_id = mapped
                reply_to = reply_target(message)
                parts = await asyncio.to_thread(
                    encode_message,
                    message.get("raw_message") or [],
                    self.config.matrix.text_chunk_chars,
                )
                if not parts:
                    raise ValueError("出站消息为空")
                await self._stop_typing(room_id)
                for index, part in enumerate(parts):
                    prefix = reply_to if index == 0 else None
                    if part["kind"] == "text":
                        event_id = await session.send_text(
                            room_id,
                            part["body"],
                            part["html"],
                            part["mentions"],
                            prefix,
                        )
                    else:
                        event_id = await session.send_media(
                            room_id,
                            part["kind"],
                            part["binary"],
                            part["filename"],
                            prefix,
                        )
                    sent.append(event_id)
                return {
                    "success": True,
                    "external_message_id": sent[0],
                    "metadata": {"matrix_event_ids": sent},
                }
            except Exception as exc:  # noqa: BLE001 - 出站失败需要返回给宿主
                error = str(exc)
                if sent:
                    error = f"已发送 {len(sent)} 条后失败，不能整体重发: {error}"
                self.ctx.logger.warning("Matrix 出站失败: %s", error)
                return {
                    "success": False,
                    "error": error,
                    "metadata": {"matrix_event_ids": sent},
                }


def create_plugin() -> MatrixAdapterPlugin:
    return MatrixAdapterPlugin()
