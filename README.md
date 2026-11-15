# MaiBot Matrix 适配器

需要 MaiBot >= 1.3.18（插件 SDK >= 2.19.0）。

基于 MaiBot SDK 的独立适配器插件，使用 [matrix-nio](https://github.com/matrix-nio/matrix-nio) 0.26 与 vodozemac 连接 Matrix。

## 使用方法

将本仓库克隆到 MaiBot 的 plugins/ 下即可。

cd /path/to/MaiBot/plugins
git clone https://github.com/Stanley-233/MaiBot-Matrix-Adapter.git

## 支持范围

✅ 已接入 · 🚧 适配器待实现 · ❌ 上游尚未支持

- ✅ 密码和 Token 登录，自动保存登录态
- ✅ 私聊与群聊
- ✅ 收到邀请后自动加入房间
- ✅ 文本收发，支持 HTML 正文解析和长文本分段
- ✅ 图片收发，也能接收以文件形式发送的图片
- ✅ 贴纸与语音收发
- ✅ @ 提及与回复引用
- ✅ 接收表态（`m.reaction`）
- ✅ 已读回执与输入状态
- ✅ 【实验性】加密房间收发
- ✅ 【实验性】加密附件上传与下载
- 🚧 更换头像与修改昵称
- 🚧 发送表态
- 🚧 线程（`m.thread`）
- 🚧 消息编辑与撤回
- 🚧 视频与通用文件收发
- 🚧 手动设备验证与 SAS emoji 验证
- ❌ 合并转发
- ❌ Cross-signing
- ❌ 服务端密钥备份（Secure Backup）
- ❌ 房间内 emoji 验证

本适配器使用的 Matrix Python SDK 的上游支持情况见[功能列表](https://github.com/matrix-nio/matrix-nio#features)。

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
| `plaintext_outbound` | **测试开关**：加密房间也直接发明文消息，存在泄露风险，默认关 |
| `max_download_mb` | 单个入站附件上限 |
| `text_chunk_chars` | 单条文本最大字符数，超出拆分 |

## 端对端加密说明

加密消息和附件收发目前还是实验性功能，不需要额外安装 libolm。设备验证尚未接入，也不支持 cross-signing，因此机器人在 Element 中可能显示为“未验证”。

`ignore_unverified_devices` 默认开启，允许机器人向未验证设备分享房间密钥。对方客户端是否向机器人分享密钥，则取决于对方的设置；如果只允许已验证设备，机器人可能无法解密对方的消息。

重装或迁移前，请备份插件的数据目录，并保留原来的 `device_id` 和 `store_passphrase`。密钥保存在 `data/plugins/<plugin_id>/matrix-store`，目前没有服务端密钥备份，丢失后可能无法再解密历史消息。

## 开发与验证

```powershell
uv sync --group dev
uv run ruff check .
uv run python -m unittest discover -s tests
```

已在 Continuwuity 26.9.3 下测试通过。

参考：
- [MaiBot SDK 开发指南](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md)
- [Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/)
- [matrix-nio 文档](https://matrix-nio.readthedocs.io/)
