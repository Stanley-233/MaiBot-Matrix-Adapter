"""Matrix 媒体下载与上传，包含 E2EE 附件加解密。"""

from __future__ import annotations

import mimetypes
from typing import Any, Dict, Mapping, Optional

from nio import AsyncClient, DownloadError, MemoryDownloadResponse, UploadError
from nio.crypto.attachments import decrypt_attachment


class MediaError(Exception):
    """媒体下载、解密或上传失败。"""


def guess_mime(filename: str, data: bytes, declared: str = "") -> str:
    """按声明、文件名、文件头依次推断 MIME 类型。"""

    if declared:
        return declared
    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        return guessed
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    return "application/octet-stream"


def _mxc(content: Mapping[str, Any]) -> tuple[str, Optional[Mapping[str, Any]]]:
    """返回 mxc 地址与可选的加密信息。"""

    file_info = content.get("file")
    if isinstance(file_info, Mapping) and file_info.get("url"):
        return str(file_info["url"]), file_info
    url = content.get("url")
    if not url:
        raise MediaError("消息事件缺少文件地址")
    return str(url), None


def decrypt_info(file_info: Mapping[str, Any]) -> tuple[str, str, str]:
    key = file_info.get("key")
    key_value = key.get("k") if isinstance(key, Mapping) else key
    hashes = file_info.get("hashes")
    sha256 = hashes.get("sha256") if isinstance(hashes, Mapping) else None
    iv = file_info.get("iv")
    if not isinstance(key_value, str) or not isinstance(sha256, str) or not isinstance(iv, str):
        raise MediaError("加密文件缺少 key/hashes/iv")
    return key_value, sha256, iv


async def download(
    client: AsyncClient,
    content: Mapping[str, Any],
    max_bytes: int,
) -> bytes:
    """下载消息附件；加密附件会在校验哈希后解密。"""

    mxc, file_info = _mxc(content)
    response = await client.download(mxc)
    if isinstance(response, DownloadError):
        raise MediaError(f"下载附件失败: {response}")
    if not isinstance(response, MemoryDownloadResponse):
        raise MediaError("附件未能读入内存")

    body = response.body
    if len(body) > max_bytes:
        raise MediaError(f"附件超过下载上限 {max_bytes // (1024 * 1024)} MB")
    if file_info is not None:
        key, sha256, iv = decrypt_info(file_info)
        try:
            body = decrypt_attachment(body, key, sha256, iv)
        except Exception as exc:  # noqa: BLE001 - nio 抛出的加密异常类型不统一
            raise MediaError(f"附件解密失败: {exc}") from exc
    if not body:
        raise MediaError("附件内容为空")
    return body


async def upload(
    client: AsyncClient,
    data: bytes,
    filename: str,
    mime: str,
    encrypted: bool,
) -> Dict[str, Any]:
    """上传附件并返回可直接并入消息 content 的字段。

    加密房间使用 Matrix 标准的 EncryptedFile：``{"file": {...}}``；
    未加密房间返回 ``{"url": "mxc://..."}``。
    """

    response, file_info = await client.upload(
        data, content_type=mime, filename=filename, encrypt=encrypted
    )
    if isinstance(response, UploadError):
        raise MediaError(f"上传附件失败: {response}")
    if not response.content_uri:
        raise MediaError("上传附件未返回 mxc 地址")
    if not encrypted:
        return {"url": response.content_uri}
    if not file_info:
        raise MediaError("加密上传未返回解密信息")
    return {"file": {**file_info, "url": response.content_uri}}
