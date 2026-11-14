"""Bounded raster icons from mod archives and the official Modrinth CDN."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import struct
import tempfile
import threading
import time
from urllib.parse import urlsplit
import zipfile
import zlib

import requests

from . import __version__
from .mods import ModManager, ModError, inspect_jar
from .modrinth import ModrinthClient

MAX_ICON_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4 * 1024 * 1024
PROJECT_TTL = 24 * 60 * 60
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class IconError(ValueError):
    pass


@dataclass(frozen=True)
class Icon:
    content: bytes
    content_type: str
    etag: str


def raster(content: bytes) -> Icon:
    """Check raster signatures and dimensions without executing image/mod code."""
    if not 0 < len(content) <= MAX_ICON_BYTES:
        raise IconError("Недопустимый размер иконки.")
    width = height = 0
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 33 and content[8:12] == b"\0\0\0\r" and content[12:16] == b"IHDR":
        width, height = struct.unpack_from(">II", content, 16)
        kind = "image/png"
        offset, image_data, ended = 8, False, False
        while offset + 12 <= len(content):
            size = int.from_bytes(content[offset:offset+4], "big")
            end = offset + 12 + size
            if end > len(content): raise IconError("Повреждённая PNG-иконка.")
            chunk = content[offset+4:offset+8]
            if zlib.crc32(content[offset+4:end-4]) & 0xFFFFFFFF != int.from_bytes(content[end-4:end], "big"):
                raise IconError("Повреждённая PNG-иконка.")
            if chunk == b"acTL": raise IconError("Анимированные иконки не поддерживаются.")
            if chunk == b"IDAT": image_data = True
            if chunk == b"IEND":
                ended = size == 0 and end == len(content)
                break
            offset = end
        if not image_data or not ended: raise IconError("Повреждённая PNG-иконка.")
    elif content.startswith(b"\xff\xd8\xff"):
        if not content.endswith(b"\xff\xd9"): raise IconError("Повреждённая JPEG-иконка.")
        kind = "image/jpeg"
        offset = 2
        while offset + 4 <= len(content):
            if content[offset] != 255: raise IconError("Повреждённая JPEG-иконка.")
            while offset < len(content) and content[offset] == 255: offset += 1
            if offset >= len(content): break
            marker = content[offset]; offset += 1
            if marker in {0xD9, 0xDA}: break
            if marker == 0x01 or 0xD0 <= marker <= 0xD8: continue
            if offset + 2 > len(content): break
            size = int.from_bytes(content[offset:offset+2], "big")
            if size < 2 or offset + size > len(content): raise IconError("Повреждённая JPEG-иконка.")
            if marker in {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF} and size >= 7:
                height, width = struct.unpack_from(">HH", content, offset+3)
                break
            offset += size
    elif len(content) >= 26 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        if int.from_bytes(content[4:8], "little") + 8 != len(content): raise IconError("Повреждённая WebP-иконка.")
        kind = "image/webp"
        chunk, size = content[12:16], int.from_bytes(content[16:20], "little")
        if size > len(content)-20: raise IconError("Повреждённая WebP-иконка.")
        if chunk == b"VP8X" and size >= 10:
            if content[20] & 2: raise IconError("Анимированные иконки не поддерживаются.")
            width = 1 + int.from_bytes(content[24:27], "little")
            height = 1 + int.from_bytes(content[27:30], "little")
        elif chunk == b"VP8 " and size >= 10 and content[23:26] == b"\x9d\x01\x2a":
            width, height = (value & 0x3FFF for value in struct.unpack_from("<HH", content, 26))
        elif chunk == b"VP8L" and size >= 5 and content[20] == 0x2F:
            bits = int.from_bytes(content[21:25], "little")
            width, height = 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    else:
        raise IconError("Иконка должна быть PNG, JPEG или статическим WebP.")
    if not 0 < width <= 2048 or not 0 < height <= 2048 or width * height > MAX_PIXELS:
        raise IconError("Недопустимые размеры изображения иконки.")
    return Icon(content, kind, hashlib.sha256(content).hexdigest())


class ModIcons:
    def __init__(self, cache_directory: Path | str, timeout: int = 15):
        if Path(cache_directory).expanduser().is_symlink(): raise IconError("Небезопасный каталог кэша иконок.")
        self.cache = Path(cache_directory).expanduser().resolve()
        self.timeout = min(max(timeout, 5), 30)
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": f"ConsoleCraft/{__version__}", "Accept": "image/png,image/jpeg,image/webp"})
        self.catalog = ModrinthClient(timeout=self.timeout)

    def _paths(self, key):
        digest = hashlib.sha256(key.encode()).hexdigest()
        image, absent = self.cache / (digest + ".img"), self.cache / (digest + ".missing")
        if self.cache.is_symlink() or image.is_symlink() or absent.is_symlink():
            raise IconError("Небезопасный каталог кэша иконок.")
        return image, absent

    def _cached(self, path):
        try:
            if path.is_file() and path.stat().st_size <= MAX_ICON_BYTES: return raster(path.read_bytes())
        except (IconError, OSError): pass
        return None

    def _save(self, path, content):
        self.cache.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache, prefix=".icon-", delete=False) as stream:
                temporary = Path(stream.name); stream.write(content)
            os.replace(temporary, path)
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)
        with _LOCKS_GUARD:
            owned = list(self.cache.glob("*.img")) + list(self.cache.glob("*.missing"))
            if len(owned) > 256:
                for old in sorted(owned, key=lambda p:p.stat().st_mtime)[:len(owned)-256]:
                    if old != path and not old.is_symlink(): old.unlink(missing_ok=True)

    def _lock(self, key):
        with _LOCKS_GUARD:
            return _LOCKS.setdefault(str(self.cache) + key, threading.Lock())

    def local(self, game_directory: Path | str, filename: str) -> Icon:
        if not isinstance(filename, str) or not filename.endswith((".jar", ".jar.disabled")):
            raise IconError("Мод не найден.")
        archive_path = ModManager(game_directory)._path(filename)
        if not archive_path.is_file(): raise IconError("Мод не найден.")
        details = archive_path.stat()
        key = f"local:{archive_path.resolve()}:{details.st_size}:{details.st_mtime_ns}:{details.st_ctime_ns}"
        image, absent = self._paths(key)
        with self._lock(key):
            cached = self._cached(image)
            if cached: return cached
            if absent.is_file(): raise IconError("У мода нет подходящей иконки.")
            try:
                metadata = inspect_jar(archive_path)
                name = metadata.icon_path
                parts = PurePosixPath(name).parts
                if not name or len(name)>4096 or not parts or "\\" in name or name.startswith("/") or any(p in {".",".."} or ":" in p or any(ord(c)<32 for c in p) for p in parts):
                    raise IconError("У мода нет безопасного пути к иконке.")
                with zipfile.ZipFile(archive_path) as archive:
                    entry = archive.getinfo(name)
                    if not 0 < entry.file_size <= MAX_ICON_BYTES or entry.file_size > max(entry.compress_size,1)*1000:
                        raise IconError("Иконка превышает допустимый размер.")
                    icon = raster(archive.read(entry))
                self._save(image, icon.content)
                return icon
            except (IconError, ModError, KeyError, zipfile.BadZipFile, OSError):
                self._save(absent, b"")
                raise IconError("У мода нет подходящей иконки.") from None

    def project(self, project: str) -> Icon:
        if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project):
            raise IconError("Некорректный ID проекта.")
        key = "project:" + project
        image, absent = self._paths(key)
        with self._lock(key):
            cached = self._cached(image)
            if cached and time.time()-image.stat().st_mtime < PROJECT_TTL: return cached
            if absent.is_file() and time.time()-absent.stat().st_mtime < 600: raise IconError("Иконка не найдена.")
            try:
                metadata = self.catalog.project(project)
                url = metadata.get("icon_url")
                parsed = urlsplit(url) if isinstance(url,str) else None
                if not parsed or parsed.scheme != "https" or parsed.hostname not in {"cdn.modrinth.com", "cdn-raw.modrinth.com"} or parsed.username or parsed.password or parsed.port not in {None,443}:
                    raise IconError("Иконка не размещена в официальном CDN Modrinth.")
                response = self.http.get(url, timeout=(5,self.timeout), stream=True, allow_redirects=False)
                try:
                    if response.status_code != 200: raise IconError("Иконка временно недоступна.")
                    length = response.headers.get("Content-Length")
                    if length and (not length.isdecimal() or int(length)>MAX_ICON_BYTES): raise IconError("Иконка слишком велика.")
                    chunks, total = [], 0
                    for chunk in response.iter_content(65536):
                        total += len(chunk)
                        if total > MAX_ICON_BYTES: raise IconError("Иконка слишком велика.")
                        chunks.append(chunk)
                    icon = raster(b"".join(chunks))
                finally: response.close()
                self._save(image,icon.content)
                absent.unlink(missing_ok=True)
                return icon
            except Exception:
                # Icons are optional; never fail catalogue/mod operations or expose HTTP details.
                if cached: return cached
                self._save(absent,b"")
                raise IconError("Иконка проекта временно недоступна.") from None
