"""matrix-nio 会话封装：登录、同步、加密发送、媒体与房间信息。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from nio import (
    Api,
    AsyncClient,
    AsyncClientConfig,
    DiscoveryInfoResponse,
    EncryptionError,
    JoinResponse,
    LocalProtocolError,
    LoginError,
    OlmUnverifiedDeviceError,
    RoomSendError,
    RoomSendResponse,
    SendRetryError,
    SyncError,
    SyncResponse,
)
from nio.responses import ErrorResponse

try:  # 仅在安装 vodozemac（e2e extra）时可用
    from nio.store import DefaultStore
except ImportError:  # pragma: no cover - 缺少 E2EE 依赖时由 ensure_encryption_available 报错
    DefaultStore = None  # type: ignore[assignment,misc]

from . import media
from .settings import MatrixSettings
from .storage import AdapterStore

SyncHandler = Callable[[SyncResponse], Awaitable[None]]


def safe_store_name(user_id: str) -> str:
    """把 MXID 转成可以用作 Windows 文件名的片段。"""

    return user_id.replace("@", "").replace(":", "_").replace("/", "_").replace("\\", "_")


if DefaultStore is not None:

    class WindowsSafeStore(DefaultStore):
        """matrix-nio 默认用 ``{user_id}_{device_id}`` 作为数据库与信任文件名。

        MXID 含有 ``@`` 和 ``:``，在 Windows 上会被解释为 NTFS 备用数据流，
        写设备信任状态时直接报 ``WinError 123``。这里只在创建存储文件名时替换
        非法字符，随后恢复真实 user_id 供数据库查询使用。
        """

        def __post_init__(self) -> None:
            real_user_id = self.user_id
            self.user_id = safe_store_name(real_user_id)
            try:
                super().__post_init__()
            finally:
                self.user_id = real_user_id

else:  # pragma: no cover
    WindowsSafeStore = None  # type: ignore[assignment,misc]


class MatrixError(Exception):
    """Matrix 会话错误基类。"""


class MatrixLoginError(MatrixError):
    """登录失败；fatal 表示凭据错误，重试没有意义。"""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class MatrixAuthError(MatrixError):
    """访问令牌失效，需要重新登录。"""


class MatrixSendError(MatrixError):
    """发送事件失败。"""


async def resolve_homeserver(
    homeserver: str, proxy: Optional[str], logger: logging.Logger
) -> str:
    """按 Matrix 标准通过 .well-known 解析域名形式的 homeserver。

    Element 等客户端允许用户只填域名（例如 ``https://example.org``），真正的
    API 地址写在 ``/.well-known/matrix/client`` 里。matrix-nio 不会自动发现，
    直接对域名登录会得到 404 和 "unknown error"。
    """

    probe = AsyncClient(homeserver=homeserver, proxy=proxy)
    try:
        info = await probe.discovery_info()
    except Exception:  # noqa: BLE001 - 发现失败时回退到用户填写的地址
        logger.debug("Matrix .well-known 发现失败，使用配置地址: %s", homeserver)
        return homeserver
    finally:
        await probe.close()
    if isinstance(info, DiscoveryInfoResponse) and info.homeserver_url:
        resolved = info.homeserver_url.rstrip("/")
        if resolved != homeserver:
            logger.info(
                "Matrix homeserver 按 .well-known 解析: %s -> %s", homeserver, resolved
            )
        return resolved
    logger.debug("Matrix .well-known 未返回 m.homeserver.base_url: %s", homeserver)
    return homeserver


class MatrixSession:
    def __init__(
        self,
        settings: MatrixSettings,
        store: AdapterStore,
        data_dir: Path,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.store = store
        self.data_dir = data_dir
        self.logger = logger
        self.client: Optional[AsyncClient] = None
        self.user_id = ""
        self.device_id = ""
        self._sync_handler: Optional[SyncHandler] = None

    async def connect(self) -> None:
        """恢复或创建登录态，并准备好加密存储。"""

        settings = self.settings
        homeserver = settings.homeserver.strip().rstrip("/")
        user_id = settings.user_id.strip()
        if not homeserver:
            raise MatrixLoginError("未填写 Matrix homeserver", fatal=True)
        if not user_id:
            raise MatrixLoginError("未填写机器人 MXID（matrix.user_id）", fatal=True)

        stored = await self.store.load_account(homeserver, user_id)
        device_id = settings.device_id.strip() or (stored or {}).get("device_id", "")
        store_path = self.data_dir / "matrix-store"
        store_path.mkdir(parents=True, exist_ok=True)
        proxy = settings.proxy_url.strip() or None
        homeserver = await resolve_homeserver(homeserver, proxy, self.logger)

        config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
            pickle_key=settings.store_passphrase or "DEFAULT_KEY",
            store=WindowsSafeStore,
        )
        try:
            client = AsyncClient(
                homeserver=homeserver,
                user=user_id,
                device_id=device_id,
                store_path=str(store_path),
                config=config,
                proxy=proxy,
            )
        except ImportWarning as exc:
            raise MatrixLoginError(
                "matrix-nio 未安装 E2EE 依赖，请执行 pip install \"matrix-nio[e2e]\"",
                fatal=True,
            ) from exc

        token = settings.access_token.strip() or (stored or {}).get("access_token", "")
        try:
            if token and client.device_id:
                client.restore_login(user_id, client.device_id, token)
                self.logger.info("Matrix 使用已保存的访问令牌登录: %s", user_id)
            elif settings.password.strip():
                response = await client.login(
                    password=settings.password, device_name=settings.device_name
                )
                if isinstance(response, LoginError):
                    status = (
                        f"HTTP {response.status_code} " if response.status_code else ""
                    )
                    detail = response.message or "服务器未返回错误信息"
                    raise MatrixLoginError(
                        f"Matrix 登录失败 {user_id} @ {homeserver}（{status}{detail}）",
                        fatal=response.status_code in (400, 401, 403),
                    )
                token = response.access_token
                self.logger.info(
                    "Matrix 密码登录成功: %s device=%s", user_id, client.device_id
                )
            else:
                raise MatrixLoginError(
                    "缺少登录凭据：请填写 matrix.password，"
                    "或同时填写 matrix.access_token 与 matrix.device_id",
                    fatal=True,
                )
        except Exception:
            await client.close()
            raise

        if not client.device_id:
            raise MatrixLoginError("Matrix 登录未返回设备 ID", fatal=True)

        self.client = client
        self.user_id = client.user_id
        self.device_id = client.device_id
        await self.store.save_account(homeserver, self.user_id, self.device_id, token)

    async def run(self, on_sync: SyncHandler) -> None:
        """持续同步；回调抛出的异常会结束本次会话，交给上层重连。"""

        if self.client is None:
            raise MatrixError("Matrix 会话尚未连接")
        self._sync_handler = on_sync
        self.client.add_response_callback(self._on_response, SyncResponse)
        await self.client.sync_forever(
            timeout=self.settings.sync_timeout_ms,
            sync_filter={"room": {"timeline": {"limit": 20}}},
            first_sync_filter={"room": {"timeline": {"limit": 0}}},
            set_presence="online",
        )

    async def _on_response(self, response: SyncResponse) -> None:
        if isinstance(response, SyncError):
            if response.status_code in (401, 403):
                raise MatrixAuthError(f"Matrix 同步鉴权失败: {response.message}")
            raise MatrixError(f"Matrix 同步失败: {response}")
        if self._sync_handler is not None:
            await self._sync_handler(response)

    async def close(self) -> None:
        if self.client is None:
            return
        client = self.client
        self.client = None
        try:
            client.stop_sync_forever()
        except Exception:  # noqa: BLE001 - 关闭阶段的错误不影响卸载
            self.logger.debug("停止 Matrix 同步时出现异常", exc_info=True)
        await client.close()

    # ------------------------------------------------------------------ 房间信息

    def room(self, room_id: str) -> Optional[Any]:
        assert self.client is not None
        return self.client.rooms.get(room_id)

    def room_encrypted(self, room_id: str) -> bool:
        room = self.room(room_id)
        return bool(room and room.encrypted)

    def room_name(self, room_id: str) -> str:
        room = self.room(room_id)
        if room is not None and room.display_name:
            return str(room.display_name)
        return room_id

    def user_name(self, room_id: str, user_id: str) -> str:
        room = self.room(room_id)
        if room is not None:
            name = room.user_name(user_id)
            if name:
                return str(name)
        return user_id

    def is_direct(self, room_id: str, max_members: int) -> bool:
        room = self.room(room_id)
        return bool(room and room.member_count <= max_members)

    # ------------------------------------------------------------------ 收发动作

    async def join(self, room_id: str) -> None:
        """加入房间。

        matrix-nio 的 ``join()`` 不发送请求体，部分 Matrix 实现（例如 Rust 系
        服务器）会返回 ``M_BAD_JSON: EOF while parsing a value``。这里按规范补一个
        ``{}`` 请求体。
        """

        assert self.client is not None
        method, path = Api.join(self.client.access_token, room_id)
        response = await self.client._send(JoinResponse, method, path, data="{}")
        if not isinstance(response, JoinResponse):
            raise MatrixError(f"加入房间失败: {response}")

    async def send_event(
        self,
        room_id: str,
        event_type: str,
        content: Dict[str, Any],
        tx_id: Optional[str] = None,
    ) -> str:
        """发送事件；加密房间自动加密，限流时按服务端建议重试。"""

        assert self.client is not None
        for attempt in range(3):
            try:
                response = await self.client.room_send(
                    room_id,
                    event_type,
                    content,
                    tx_id=tx_id,
                    ignore_unverified_devices=self.settings.ignore_unverified_devices,
                )
            except OlmUnverifiedDeviceError:
                # 没有 cross-signing 时所有设备都未验证，必须显式忽略才能分享房间密钥。
                response = await self.client.room_send(
                    room_id,
                    event_type,
                    content,
                    tx_id=tx_id,
                    ignore_unverified_devices=True,
                )
            except (LocalProtocolError, SendRetryError, EncryptionError) as exc:
                raise MatrixSendError(f"发送到 {room_id} 失败: {exc}") from exc
            if isinstance(response, RoomSendResponse):
                return response.event_id
            if isinstance(response, RoomSendError):
                if response.retry_after_ms and attempt < 2:
                    await asyncio.sleep(response.retry_after_ms / 1000)
                    continue
                raise MatrixSendError(f"发送到 {room_id} 失败: {response}")
            raise MatrixSendError(f"发送到 {room_id} 返回未知结果: {response!r}")
        raise MatrixSendError(f"发送到 {room_id} 多次被限流")

    async def send_typing(self, room_id: str, typing: bool, timeout_ms: int = 4000) -> None:
        if self.client is None:
            return
        try:
            await self.client.room_typing(room_id, typing, timeout_ms)
        except Exception:  # noqa: BLE001 - 输入状态失败不影响消息收发
            self.logger.debug("更新 Matrix 输入状态失败", exc_info=True)

    async def send_read_receipt(self, room_id: str, event_id: str) -> None:
        assert self.client is not None
        response = await self.client.room_read_markers(room_id, event_id, event_id)
        if isinstance(response, ErrorResponse):
            self.logger.debug("发送 Matrix 已读回执失败: %s", response)

    async def download_media(self, content: Mapping[str, Any]) -> bytes:
        assert self.client is not None
        return await media.download(
            self.client, content, self.settings.max_download_mb * 1024 * 1024
        )

    async def upload_media(
        self, room_id: str, data: bytes, filename: str, mime: str
    ) -> Dict[str, Any]:
        assert self.client is not None
        return await media.upload(
            self.client, data, filename, mime, encrypted=self.room_encrypted(room_id)
        )

    async def send_media(
        self,
        room_id: str,
        kind: str,
        binary: bytes,
        filename: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """发送图片、表情或语音，并返回事件 ID。"""

        mime = media.guess_mime(filename, binary)
        attachment = await self.upload_media(room_id, binary, filename, mime)
        info = {"mimetype": mime, "size": len(binary)}
        if kind == "emoji":
            event_type = "m.sticker"
            content: Dict[str, Any] = {"body": filename, "info": info, **attachment}
        elif kind == "voice":
            event_type = "m.audio"
            content = {"msgtype": "m.audio", "body": filename, "info": info, **attachment}
        else:
            event_type = "m.image"
            content = {"msgtype": "m.image", "body": filename, "info": info, **attachment}
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        return await self.send_event(room_id, event_type, content)

    async def send_text(
        self,
        room_id: str,
        body: str,
        html: str = "",
        mentions: Optional[List[str]] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        content: Dict[str, Any] = {"msgtype": "m.text", "body": body}
        if html:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = html
        if mentions:
            content["m.mentions"] = {"user_ids": mentions}
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        return await self.send_event(room_id, "m.text", content)


def ensure_encryption_available() -> None:
    """启动前检查 matrix-nio 的 E2EE 后端是否可用。"""

    from nio.crypto import ENCRYPTION_ENABLED  # noqa: PLC0415 - 延迟到插件加载后判断

    if not ENCRYPTION_ENABLED:
        raise MatrixLoginError(
            "matrix-nio 缺少 E2EE 依赖，请安装 matrix-nio[e2e]（含 vodozemac）",
            fatal=True,
        )
