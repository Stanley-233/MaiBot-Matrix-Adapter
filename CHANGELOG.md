# Changelog

## [0.1.0] - 2026-11-14

首个版本。

- 基于 MaiBot SDK `MessageGateway` 的 Matrix 双向网关，平台名 `matrix`。
- 使用 matrix-nio 0.26 与 vodozemac 后端，支持加密房间收发，无需系统安装 libolm。
- 支持文本、图片、表情（`m.sticker`）、语音（`m.audio`）、@ 提及、回复引用。
- 支持加密媒体上传与下载（Matrix `EncryptedFile`）。
- 支持表态事件（`m.reaction`）作为 `is_notify` 通知进入聊天流。
- 支持私聊/群聊识别、私聊房间映射、自动接受邀请、已读回执与输入状态。
- 登录凭据、设备 ID 与消息归属持久化在插件数据目录的 SQLite 中。
- 新增联调开关 `matrix.plaintext_outbound`：加密房间也直接发明文消息（默认关闭）。
