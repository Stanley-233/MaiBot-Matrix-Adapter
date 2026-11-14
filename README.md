# MaiBot Matrix 适配器

需要 MaiBot >= 1.3.18（插件 SDK >= 2.19.0）。

基于 MaiBot SDK `MessageGateway` 的独立适配器插件，使用 [matrix-nio](https://github.com/matrix-nio/matrix-nio) 0.26 与 vodozemac 连接 Matrix。一个插件实例登录一个机器人账号；群/用户白名单与回复策略由 MaiBot 统一控制。

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

## 安装与配置

1. 在 MaiBot 根目录把插件克隆到 `plugins/matrix_adapter`：

   ```powershell
   git clone https://github.com/Mai-with-u/MaiBot-Matrix-Adapter.git plugins/matrix_adapter
   ```

2. 安装依赖。`matrix-nio[e2e]` 会拉取 vodozemac 的预编译 wheel，**不需要安装 libolm**：

   ```powershell
   uv pip install --python .venv/Scripts/python.exe -r plugins/matrix_adapter/requirements.txt
   ```

   也可以由 MaiBot 按 `_manifest.json` 自动安装；若自动安装漏掉 `e2e` extra，请手动执行上面的命令。Python 3.12 可直接使用 vodozemac 的预编译 wheel（官方覆盖 cp310–cp314）。

3. 准备机器人账号。建议单独注册一个 Matrix 账号，不要复用个人账号。

4. 在 WebUI 插件配置中填写 `matrix.homeserver`、`matrix.user_id`、`matrix.password`，设置 `plugin.enabled = true`。SDK 会生成实际 `config.toml`；也可以参考 `config.example.toml` 手动配置。

   `config.example.toml` 只是模板，Runner 不读取；真正的配置文件是插件目录下的 `config.toml`（不入库、含令牌）。`tests/test_config_template.py` 会检查模板与 `settings.py` 的字段是否同步。

5. 首次启动会创建设备并保存访问令牌。日志出现 `Matrix 网关已就绪` 后即可在房间里 @ 机器人。

6. 在 MaiBot 的适配器策略中配置 `matrix` 平台、机器人 MXID 与房间/用户放行规则。

### 配置项

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

## 失败行为

- 单条事件的处理异常会记录完整堆栈并写入 `matrix.sqlite3` 的 `failed_events` 表，随后继续同步。
- 同步令牌保存在 matrix-nio 的加密存储中，重启不会重放历史消息；首次登录时时间线限制为 0 条。
- 发送前会完成全部分片校验与媒体上传；若部分发送成功，返回失败并附带已发送的事件 ID，不能整体重发。
- 访问令牌失效会自动清除本地会话并用密码重新登录；密码错误等凭据问题会停止连接并在配置页显示原因。

## 开发与验证

```powershell
uv sync --group dev
uv run ruff check .
uv run python -m unittest discover -s tests
```

真实联调需要一个 Matrix 账号与可访问的 homeserver，覆盖：私聊/群聊收发、加密房间、加密媒体、@ 与回复、表态通知、断线重连。

参考：[MaiBot SDK 开发指南](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md)、[Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/)、[matrix-nio 文档](https://matrix-nio.readthedocs.io/)。
