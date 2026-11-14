# MaiBot Matrix 适配器

需要 MaiBot >= 1.3.18（插件 SDK >= 2.19.0）。

基于 MaiBot SDK `MessageGateway` 的独立适配器插件，使用 [matrix-nio](https://github.com/matrix-nio/matrix-nio) 0.26 与 vodozemac 连接 Matrix。

## 支持范围

| 内容 | 接收 | 发送 |
| --- | --- | --- |
| 文本 | `m.text`、`m.notice`、`m.emote`，HTML 正文转纯文本 | 保留正文，长文本自动分段 |
| 图片 | `m.image`，`m.file` 中的图片也接收 | 上传图片，支持加密房间 |
| 表情 | `m.sticker`、图片类消息 | 以 `m.sticker` 发送 |
| 语音 | `m.audio` | 以 `m.audio` 发送 |
| @ | `m.mentions` 与 matrix.to 提及 | 生成 matrix.to 链接与 `m.mentions` |
| 回复引用 | `m.in_reply_to`，保留目标事件与发送者 | `m.in_reply_to` |
| 表态 | `m.reaction` 作为 `is_notify` 通知 | 尚未发送 |
| 加密房间 | Megolm 自动解密、按需请求房间密钥 | 自动加密，忽略未验证设备 |
| 加密媒体 | 自动校验 SHA-256 并解密 | 自动加密上传 |

暂不支持：线程（`m.thread`）、消息编辑、消息撤回、合并转发、视频与任意文件、Cross-signing、密钥备份（Secure Backup）、房间内 emoji 验证。matrix-nio 本身也不支持后三项，详见下文。

## 配置项

| 键 | 说明 |
| --- | --- |
| `homeserver` | Matrix 服务器地址，含协议，例如 `https://matrix.example.org` |
| `user_id` | 机器人完整 MXID |
| `password` | 登录密码；首次登录后改用保存的访问令牌 |
| `access_token` | 可选；填写后优先于密码，需同时填写 `device_id` |
| `device_id` | 设备 ID；留空自动生成并持久化，**运行中不要修改** |
| `device_name` | 新设备显示名称 |
| `store_passphrase` | 加密存储口令；修改后旧存储无法读取 |
| `proxy_url` | HTTP/SOCKS 代理；留空直连，不读环境变量 |
| `sync_timeout_ms` | 长轮询等待时间 |
| `reconnect_delay_sec` | 断线重连间隔 |
| `auto_join` | 收到邀请自动加入 |
| `dm_max_members` | 成员数不超过该值视为私聊，默认 2 |
| `send_read_receipt` | 处理消息后发已读回执 |
| `ignore_unverified_devices` | 向未验证设备分享房间密钥，默认开；关闭后加密房间将无法发送 |
| `plaintext_outbound` | **联调开关**：加密房间也直接发明文消息，存在泄露风险，默认关 |
| `max_download_mb` | 单个入站附件上限 |
| `text_chunk_chars` | 单条文本最大字符数，超出拆分 |

## 路由与消息 ID

- 平台固定为 `matrix`；`account_id` 为机器人 MXID；`scope` 为空。
- 群聊的 `group_id` 是房间 ID（`!opaque:server`），`group_name` 取房间名称。
- 私聊不填 `group_info`；成员数不超过 `dm_max_members` 的房间按私聊处理。
- 消息 ID 直接使用 Matrix 事件 ID（`$opaque`），回复引用与表态通知也使用同一套 ID。
- 私聊出站需要房间映射：宿主给出目标用户 MXID 时，适配器从入站记录里找到对应房间。**机器人无法凭空私聊一个没说过话的用户**，请先让对方发一条消息。
- 宿主负责聊天流创建与 `session_id` 解析，插件不做备用会话 ID 计算。

## E2EE 说明

- 0.26.0 起 matrix-nio 用 vodozemac 取代 python-olm/libolm，安装不再依赖系统 C 库。
- 加密存储与 `device_id` 必须持久化（插件默认放在 `data/plugins/<plugin_id>/matrix-store`），丢失后历史房间密钥无法恢复。
- matrix-nio 不支持 cross-signing。机器人在客户端里会一直显示为未验证设备：
  - 用户可以在 Element 中用 emoji/SAS 验证该设备，验证后显示正常；
  - 若用户开启「仅向已验证设备发送加密消息」，机器人将收不到房间密钥、无法解密。适配器已默认 `ignore_unverified_devices = true`，保证己方加密发送不被阻塞。
- 没有服务端密钥备份：不要删除或迁移加密存储，否则旧消息永久不可读。
- Windows 下 matrix-nio 默认用 `MXID_设备ID.db` 命名存储，MXID 里的 `:` 会被 NTFS 当成备用数据流，写设备信任状态时报 `WinError 123`。适配器使用自定义 Store 把文件名中的 `@`/`:` 替换掉，存储内容与账号 ID 不受影响。

### TODO: Element 无法解密适配器发出的加密消息

服务器侧已确认消息已发送（`m.room.encrypted` 存在，`m.room_key` 分发返回成功，目标设备的 Olm 会话齐全），但 Element 端看不到内容。排查方向：

- to-device `m.room_key` 是否真正到达 Element 的设备（需要接收端日志）；
- nio 0.26 + vodozemac 的 Olm 会话在跨客户端场景是否兼容（参考 matrix-nio #567 / #570 的迁移回归）；
- Olm 会话过期或被对端轮换后，`share_group_session_parallel` 会**静默跳过**没有会话的设备。

联调期间可在配置里打开 `matrix.plaintext_outbound = true`，加密房间也会直接发明文（不加密，注意内容泄露），等上述问题定位后应移除该开关。

## 开发与验证

```powershell
uv sync --group dev
uv run ruff check .
uv run python -m unittest discover -s tests
```

真实联调需要一个 Matrix 账号与可访问的 homeserver，覆盖：私聊/群聊收发、加密房间、加密媒体、@ 与回复、表态通知、断线重连。

参考：[MaiBot SDK 开发指南](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md)、[Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/)、[matrix-nio 文档](https://matrix-nio.readthedocs.io/)。
