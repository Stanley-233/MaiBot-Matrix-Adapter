"""Matrix 适配器配置模型，由 SDK 据此外生成默认配置与 WebUI Schema。"""

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase


class PluginSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(default=False, description="启用 Matrix 连接")
    config_version: str = Field(default="0.1.0", description="配置版本")


class MatrixSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "Matrix"
    __ui_order__: ClassVar[int] = 1

    homeserver: str = Field(
        default="https://matrix.org",
        description="Matrix 服务器地址，例如 https://matrix.example.org",
    )
    user_id: str = Field(
        default="",
        description="机器人完整 MXID，例如 @maibot:example.org",
    )
    password: str = Field(
        default="",
        repr=False,
        description="登录密码；首次登录创建设备，之后优先复用已保存的访问令牌",
        json_schema_extra={"x-widget": "password"},
    )
    access_token: str = Field(
        default="",
        repr=False,
        description="可选的访问令牌；填写后优先于密码，需同时填写 device_id",
        json_schema_extra={"x-widget": "password"},
    )
    device_id: str = Field(
        default="",
        description="设备 ID；留空时首次登录自动创建并持久化，请勿在运行中修改",
    )
    device_name: str = Field(default="MaiBot", description="新设备在 Matrix 客户端中显示的名称")
    store_passphrase: str = Field(
        default="",
        repr=False,
        description="加密存储口令；修改后旧的加密存储将无法读取",
        json_schema_extra={"x-widget": "password"},
    )
    proxy_url: str = Field(
        default="",
        description="HTTP/SOCKS 代理地址，例如 http://127.0.0.1:7890；留空直连",
    )
    sync_timeout_ms: int = Field(
        default=30000, ge=5000, le=300000, description="增量同步长轮询等待时间（毫秒）"
    )
    reconnect_delay_sec: float = Field(
        default=5, ge=1, le=300, description="网络故障后的重连间隔（秒）"
    )
    auto_join: bool = Field(default=True, description="收到房间邀请时自动加入")
    dm_max_members: int = Field(
        default=2,
        ge=2,
        le=50,
        description="成员数不超过该值的房间视为私聊；设为 2 表示仅双人房间",
    )
    send_read_receipt: bool = Field(default=True, description="处理消息后发送已读回执")
    ignore_unverified_devices: bool = Field(
        default=True,
        description="向未验证设备分享房间密钥；matrix-nio 不支持 cross-signing，关闭后加密房间将无法发送",
    )
    max_download_mb: int = Field(
        default=50, ge=1, le=200, description="单个入站附件下载上限（MB）"
    )
    text_chunk_chars: int = Field(
        default=8000, ge=500, le=30000, description="单条文本消息的最大字符数，超出自动拆分"
    )


class AdapterSettings(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    matrix: MatrixSettings = Field(default_factory=MatrixSettings)
