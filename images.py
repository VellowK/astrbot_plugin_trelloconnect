"""Bounded image staging, isolated from Trello credentials and user text paths."""

import asyncio
import base64
import ipaddress
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

import aiohttp

from .models import Picture


class ImageError(ValueError):
    pass


def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return ip.is_global
    except ValueError:
        return False


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.inner = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        addresses = await self.inner.resolve(host, port, family)
        if not addresses or any(not _public(item["host"]) for item in addresses):
            raise OSError("Private image host blocked")
        return addresses

    async def close(self):
        await self.inner.close()


def validate_url(value: str, allow_private: bool):
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ImageError("图片地址协议无效或包含账号信息。")
    if not allow_private:
        if parts.hostname.lower() in {"localhost", "localhost.localdomain"}:
            raise ImageError("默认禁止内网图片地址。")
        try:
            ipaddress.ip_address(parts.hostname)
        except ValueError:
            pass
        else:
            if not _public(parts.hostname):
                raise ImageError("默认禁止内网图片地址。")


def image_kind(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ImageError("只支持实际内容为 PNG、JPEG、GIF 或 WebP 的图片。")


class ImageStager:
    def __init__(self, root: Path, max_bytes: int, timeout: int = 40, allow_private: bool = False):
        self.root, self.max_bytes = root, max_bytes
        self.timeout, self.allow_private = timeout, allow_private
        self.session: aiohttp.ClientSession | None = None
        self.root.mkdir(parents=True, exist_ok=True)

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def _download(self, url: str) -> bytes:
        if not self.session or self.session.closed:
            connector = aiohttp.TCPConnector(
                resolver=None if self.allow_private else PublicResolver(), use_dns_cache=False
            )
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trust_env=False,
            )
        for _ in range(4):
            validate_url(url, self.allow_private)
            async with self.session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        raise ImageError("图片重定向缺少地址。")
                    next_url = urljoin(url, location)
                    if urlsplit(url).scheme == "https" and urlsplit(next_url).scheme != "https":
                        raise ImageError("禁止图片 HTTPS 降级重定向。")
                    url = next_url
                    continue
                if response.status != 200:
                    raise ImageError(f"图片下载失败（HTTP {response.status}）。")
                if response.content_length and response.content_length > self.max_bytes:
                    raise ImageError("图片超过大小上限。")
                result = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    result.extend(chunk)
                    if len(result) > self.max_bytes:
                        raise ImageError("图片超过大小上限。")
                return bytes(result)
        raise ImageError("图片重定向次数过多。")

    def _local_bytes(self, source: str) -> bytes:
        if source.startswith("file:"):
            parts = urlsplit(source)
            if parts.netloc and parts.netloc.lower() != "localhost":
                raise ImageError("不支持远程文件共享图片。")
            source = url2pathname(parts.path)
        if source.startswith(("\\\\", "//")):
            raise ImageError("不支持远程文件共享图片。")
        path = Path(source)
        if not path.is_file() or path.stat().st_size > self.max_bytes:
            raise ImageError("本地图片不存在或超过大小上限。")
        with path.open("rb") as stream:
            return stream.read(self.max_bytes + 1)

    async def stage(self, component, number: int) -> Picture:
        # These fields must come from an adapter Image component, never plain text.
        source = (
            getattr(component, "url", "")
            or getattr(component, "file", "")
            or getattr(component, "path", "")
        )
        if not isinstance(source, str) or not source:
            raise ImageError("图片消息缺少可用来源。")
        try:
            if source.startswith(("http://", "https://")):
                data = await self._download(source)
            elif source.startswith("base64://") or source.startswith("data:image/"):
                payload = source[9:] if source.startswith("base64://") else source.split(",", 1)[-1]
                if len(payload) > (self.max_bytes + 2) // 3 * 4 + 4:
                    raise ImageError("图片超过大小上限。")
                data = base64.b64decode(payload, validate=True)
            else:
                data = await asyncio.to_thread(self._local_bytes, source)
            if not data or len(data) > self.max_bytes:
                raise ImageError("图片为空或超过大小上限。")
            mime, suffix = image_kind(data)
            path = self.root / ("image_" + uuid.uuid4().hex + suffix)
            await asyncio.to_thread(path.write_bytes, data)
            return Picture(number, path, mime, f"image-{number}{suffix}")
        except ImageError:
            raise
        except (OSError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
            raise ImageError("无法读取图片，可能地址过期、内网受限、编码错误或下载超时。") from None

    def remove(self, pictures: list[Picture]):
        for picture in pictures:
            if picture.path.parent == self.root and picture.path.name.startswith("image_"):
                picture.path.unlink(missing_ok=True)
