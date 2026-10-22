"""预置曲库：原件保护、别名/启停、版本校验与小内存音频处理。"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import uuid
from pathlib import Path

from .core import (
    ALLOWED_SONG_EXTENSIONS,
    MAX_SONG_UPLOAD_BYTES,
    VoiceError,
    _normalize_title,
    atomic_json,
    fingerprint,
    get_preset_song_path,
    list_preset_songs_detail,
    sanitize_song_name,
)

MAX_SONGS = 500
MAX_LIBRARY_BYTES = 1024 * 1024 * 1024
MAX_SONG_SECONDS = 600
PREVIEW_SECONDS = 30


class LibraryError(VoiceError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


async def disk_io(function, *args, **kwargs):
    """取消请求时仍等磁盘操作退出，避免后台写入与已释放的锁相撞。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class SongLibrary:
    def __init__(self, root: Path, *, readonly=False):
        self.root = Path(root)
        self.readonly = bool(readonly)
        self.songs = self.root / "songs"
        self.staging = self.root / ".song-staging"
        marker = self.root / ".restore-incomplete"
        if self.root.is_symlink() or marker.exists() or marker.is_symlink():
            raise LibraryError("曲库目录不安全或恢复尚未完成，拒绝读写。")
        for p in (self.songs, self.staging):
            if p.is_symlink():
                raise LibraryError("曲库目录不能是符号链接。")
        if self.readonly:
            if not self.root.is_dir() or not self.songs.is_dir():
                raise LibraryError("只读曲库不存在，不会自动创建目录。")
        else:
            for p in (self.songs, self.staging):
                p.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.index = self.root / "song-library.json"

    def _require_write(self):
        if self.readonly:
            raise LibraryError("曲库以只读方式打开，不能修改文件或设置。")

    def _read(self):
        if self.index.is_symlink():
            raise LibraryError("曲库索引不能是符号链接。")
        if not self.index.exists():
            return {"version": 1, "songs": {}}
        try:
            if self.index.is_symlink() or self.index.stat().st_size > 2 * 1024 * 1024:
                raise ValueError()
            obj = json.loads(self.index.read_text(encoding="utf-8"))
            if obj.get("version") != 1 or not isinstance(obj.get("songs"), dict):
                raise TypeError()
            for filename, value in obj["songs"].items():
                if not isinstance(filename, str) or not isinstance(value, dict):
                    raise TypeError()
                if not isinstance(value.get("enabled", False), bool) or not isinstance(
                    value.get("aliases", []), list
                ):
                    raise TypeError()
            return obj
        except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
            raise LibraryError(
                "曲库索引异常，已停止修改与点播；原音频未被删除，请管理员检查。", 500
            ) from exc

    def list(self, enabled_only=False):
        index = self._read()["songs"]
        rows = []
        for row in list_preset_songs_detail(self.songs):
            meta = index.get(row["filename"], {})
            row.update(
                aliases=meta.get("aliases", []),
                description=meta.get("description", ""),
                enabled=meta.get("enabled", True),
                duration_seconds=meta.get("duration_seconds"),
            )
            row["revision"] = fingerprint(
                row["filename"],
                row["mtime_ns"],
                row["size_bytes"],
                meta.get("revision", 0),
            )[:24]
            if not enabled_only or row["enabled"]:
                rows.append(row)
        return rows

    def get(self, filename, revision=None):
        get_preset_song_path(self.songs, filename)
        row = next((r for r in self.list() if r["filename"] == filename), None)
        if row is None:
            raise LibraryError("歌曲已变化，请刷新曲库。", 409)
        if revision is not None and (not revision or revision != row["revision"]):
            raise LibraryError("歌曲已被其他操作修改，请刷新后再操作；未覆盖任何文件。", 409)
        return row

    def _usage(self, excluding=None):
        total = 0
        seen = set()
        for directory in (self.songs, self.staging):
            for path in directory.iterdir():
                if path == excluding or path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                key = (stat.st_dev, stat.st_ino)
                if key not in seen:
                    total += stat.st_size
                    seen.add(key)
        return total

    def create_stage(self):
        self._require_write()
        return self.staging / (uuid.uuid4().hex + ".upload")

    def commit(self, staged: Path, song_name, extension, media):
        self._require_write()
        if (
            staged.is_symlink()
            or staged.parent.resolve() != self.staging.resolve()
            or not staged.is_file()
        ):
            raise LibraryError("上传暂存文件无效。")
        size = staged.stat().st_size
        ext = str(extension).lower()
        if ext not in ALLOWED_SONG_EXTENSIONS or not 0 < size <= MAX_SONG_UPLOAD_BYTES:
            raise LibraryError("音频格式或大小不符合限制，原曲库未改动。")
        name = sanitize_song_name(song_name)
        target = self.songs / (name + ext)
        if target.exists() or target.is_symlink():
            raise LibraryError("同名歌曲已存在，原文件未被覆盖；请改一个名称。", 409)
        if (
            len(self.list()) >= MAX_SONGS
            or self._usage(excluding=staged) + size > MAX_LIBRARY_BYTES
        ):
            raise LibraryError("曲库容量或歌曲数量达到上限，请先整理；没有覆盖或删除原歌曲。", 413)
        index = self._read()
        index["songs"][target.name] = {
            "enabled": False,
            "aliases": [],
            "description": "",
            "revision": 1,
            "duration_seconds": media.get("duration_seconds"),
        }
        try:
            os.link(staged, target)  # 原子、独占，不覆盖同名音频。
        except FileExistsError as exc:
            raise LibraryError("同名歌曲刚被上传，请刷新曲库。", 409) from exc
        try:
            atomic_json(self.index, index)
        except BaseException:
            target.unlink()
            raise
        staged.unlink()
        return self.get(target.name)

    def update(self, filename, revision, *, aliases, description, enabled):
        self._require_write()
        self.get(filename, revision)
        if (
            not isinstance(aliases, list)
            or len(aliases) > 12
            or not isinstance(description, str)
            or len(description) > 200
            or not isinstance(enabled, bool)
        ):
            raise LibraryError("设置无效：最多12个别名、200字说明，启停值必须是布尔值。")
        cleaned = []
        for alias in aliases:
            if (
                not isinstance(alias, str)
                or not alias.strip()
                or len(alias) > 40
                or re.search(r"[\x00-\x1f\x7f]", alias)
            ):
                raise LibraryError("每个别名应为1—40字，不能包含控制字符。")
            if alias.strip() not in cleaned:
                cleaned.append(alias.strip())
        index = self._read()
        meta = index["songs"].setdefault(filename, {})
        meta.update(
            aliases=cleaned,
            description=description.strip(),
            enabled=enabled,
            revision=int(meta.get("revision", 0)) + 1,
        )
        atomic_json(self.index, index)
        return self.get(filename)

    def rename(self, filename, revision, new_name):
        self._require_write()
        row = self.get(filename, revision)
        old = get_preset_song_path(self.songs, filename)
        name = sanitize_song_name(new_name)
        target = self.songs / (name + old.suffix)
        if target.name == old.name:
            return row
        previous = self._read()
        index = json.loads(json.dumps(previous))
        meta = index["songs"].pop(filename, {"enabled": True, "aliases": [], "revision": 0})
        meta["revision"] = int(meta.get("revision", 0)) + 1
        index["songs"][target.name] = meta
        try:
            os.link(old, target)
        except FileExistsError as exc:
            raise LibraryError("同名歌曲已存在，未覆盖任何文件。", 409) from exc
        try:
            atomic_json(self.index, index)
            old.unlink()
        except BaseException:
            atomic_json(self.index, previous)
            target.unlink(missing_ok=True)
            raise
        return self.get(target.name)

    def delete(self, filename, revision):
        self._require_write()
        row = self.get(filename, revision)
        path = get_preset_song_path(self.songs, filename)
        hidden = self.staging / (uuid.uuid4().hex + ".trash")
        index = self._read()
        index["songs"].pop(filename, None)
        path.rename(hidden)
        try:
            atomic_json(self.index, index)
        except BaseException:
            hidden.rename(path)
            raise
        try:
            hidden.unlink()
        except OSError as exc:
            raise LibraryError(
                "歌曲已移出点播列表，但暂存清理失败，请管理员检查存储。", 500
            ) from exc
        return row["name"]

    def match(self, query):
        if not isinstance(query, str) or not query.strip():
            return None
        query = query.strip()
        stripped = re.sub(
            r"^(?:播放|唱首|唱歌|点歌|我要听|我想听|放首|放一下|听歌|唱|放)\s*",
            "",
            query,
        )
        forms = {_normalize_title(query), _normalize_title(stripped)} - {""}
        rows = self.list(enabled_only=True)

        def names(row):
            return {_normalize_title(x) for x in (row["name"], *row["aliases"])} - {""}

        exact = [r for r in rows if names(r) & forms]
        matches = exact or [
            r
            for r in rows
            if any(len(q) >= 2 and (q in n or n in q) for q in forms for n in names(r))
        ]
        if len(matches) > 1:
            choices = "、".join("《" + r["name"] + "》" for r in matches[:6])
            raise LibraryError(
                "找到多个版本：" + choices + "。请指定完整歌名，不会随便选一首。", 409
            )
        return matches[0] if matches else None

    def snapshot(self, filename, revision, target, *, allow_disabled=False):
        self._require_write()
        row = self.get(filename, revision)
        if not row["enabled"] and not allow_disabled:
            raise LibraryError("这首歌已暂停点播，请换一首。")
        path = get_preset_song_path(self.songs, filename)
        if path.stat().st_size > MAX_SONG_UPLOAD_BYTES:
            raise LibraryError("歌曲过大，请管理员提供50MB以内版本。")
        shutil.copyfile(path, target)
        return row


async def process(*args, timeout=30):
    p = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        if p.returncode:
            raise LibraryError("音频无法解析或转换失败，请检查文件是否损坏；原曲库未改动。")
        return out
    except BaseException:
        if p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
            await p.communicate()
        raise


async def probe_audio(path: Path, max_seconds=MAX_SONG_SECONDS):
    if (
        path.is_symlink()
        or not path.is_file()
        or not 16 <= path.stat().st_size <= MAX_SONG_UPLOAD_BYTES
    ):
        raise LibraryError("音频文件为空、过大或不是允许的普通文件。")
    data = await process(
        "ffprobe",
        "-protocol_whitelist",
        "file,pipe",
        "-format_whitelist",
        "mp3,wav,mov,ogg,flac",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type:stream_disposition=attached_pic",
        "-of",
        "json",
        str(path),
        timeout=20,
    )
    try:
        obj = json.loads(data)
        duration = float(obj["format"]["duration"])
        streams = obj.get("streams", [])
        if (
            not math.isfinite(duration)
            or not 0 < duration <= max_seconds
            or not any(x.get("codec_type") == "audio" for x in streams)
            or any(
                x.get("codec_type") == "video" and not x.get("disposition", {}).get("attached_pic")
                for x in streams
            )
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LibraryError(
            f"文件需要是可解码的纯音频，时长不超过{max_seconds}秒；没有保存到曲库。"
        ) from exc
    return {"duration_seconds": round(duration, 3)}


async def render_song(source: Path, output: Path, *, preview=False):
    media = await probe_audio(source, max_seconds=3600 if preview else MAX_SONG_SECONDS)
    args = [
        "ffmpeg",
        "-protocol_whitelist",
        "file,pipe",
        "-format_whitelist",
        "mp3,wav,mov,ogg,flac",
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-threads",
        "1",
        "-i",
        str(source),
    ]
    if preview:
        args += ["-t", str(PREVIEW_SECONDS)]
    args += [
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "24000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(output),
    ]
    try:
        await process(*args, timeout=30 if preview else 90)
        if not output.is_file() or not 16 <= output.stat().st_size <= 6 * 1024 * 1024:
            raise LibraryError("播放副本大小异常，未发送到QQ；原音频未改动。")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return min(media["duration_seconds"], PREVIEW_SECONDS) if preview else media["duration_seconds"]
