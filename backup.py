"""私有曲库备份v1：有界流式校验，只恢复到新目录且默认全部暂停。"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import struct
import tempfile
import unicodedata
import zipfile
import zlib
from pathlib import Path

from .core import ALLOWED_SONG_EXTENSIONS, MAX_SONG_UPLOAD_BYTES, atomic_json, get_preset_song_path
from .library import MAX_LIBRARY_BYTES, MAX_SONG_SECONDS, MAX_SONGS, SongLibrary

FORMAT = "blue-whale-song-backup"
MANIFEST = "BACKUP_MANIFEST.json"
CHUNK = 64 * 1024
MAX_MANIFEST = 2 * 1024 * 1024
MAX_CENTRAL = 2 * 1024 * 1024
MAX_FILE = MAX_SONG_UPLOAD_BYTES
MAX_TOTAL = MAX_LIBRARY_BYTES
MAX_COUNT = MAX_SONGS
MAX_ARCHIVE = MAX_TOTAL + MAX_MANIFEST + MAX_CENTRAL
PRIVATE = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|speech:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+|Bearer\s+\S{16,}",
    re.I,
)
RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class BackupError(ValueError):
    """可反馈的脱敏错误，不包含私有音色或凭据。"""


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _opened_signature(value):
    # Windows 3.12的stat仍可能以ctime表示创建时间，fstat则表示状态变化时间。
    # 仅在两种API的打开边界比较创建时间；同API快照继续保留ctime变化检测。
    return (*_signature(value)[:4], getattr(value, "st_birthtime_ns", value.st_ctime_ns))


def _filename(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 240
        or value in (".", "..")
        or value != value.strip(" .")
        or re.search(r'[\\/:*?"<>|\x00-\x1f\x7f]', value)
        or Path(value).suffix.lower() not in ALLOWED_SONG_EXTENSIONS
        or unicodedata.normalize("NFKC", value.split(".", 1)[0].rstrip(" .")).casefold() in RESERVED
    ):
        raise BackupError("备份包含不安全或不能跨平台恢复的音频文件名。")
    if PRIVATE.search(value):
        raise BackupError("曲目文件名疑似包含凭据或私有音色引用，已停止。")
    return value


def _canonical(value):
    return unicodedata.normalize("NFC", value).casefold()


def _metadata(value, *, strict=False):
    if not isinstance(value, dict):
        raise BackupError("曲目元数据需要是对象。")
    if strict and set(value) != {"aliases", "description", "enabled", "duration_seconds"}:
        raise BackupError("曲目元数据包含未知字段，拒绝恢复。")
    aliases = value.get("aliases", [])
    description = value.get("description", "")
    enabled = value.get("enabled", False)
    duration = value.get("duration_seconds")
    if (
        not isinstance(aliases, list)
        or len(aliases) > 12
        or any(
            not isinstance(a, str)
            or not a.strip()
            or len(a) > 40
            or re.search(r"[\x00-\x1f\x7f]", a)
            for a in aliases
        )
        or not isinstance(description, str)
        or len(description) > 200
        or not isinstance(enabled, bool)
        or (
            duration is not None
            and (
                type(duration) not in (int, float)
                or not math.isfinite(duration)
                or not 0 < duration <= MAX_SONG_SECONDS
            )
        )
    ):
        raise BackupError("曲目元数据不符合当前曲库格式。")
    if PRIVATE.search(" ".join([description, *aliases])):
        raise BackupError("曲目说明或别名疑似包含凭据或私有音色引用，已停止。")
    return {
        "aliases": list(aliases),
        "description": description,
        "enabled": enabled,
        "duration_seconds": duration,
    }


@contextlib.contextmanager
def _regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise BackupError("需要普通文件，不能读取符号链接或特殊文件。")
    before = path.stat()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or _opened_signature(opened) != _opened_signature(
            before
        ):
            raise BackupError("文件在打开时发生变化，请稍后重试。")
        yield handle


def _snapshot(library):
    for path in library.songs.iterdir():
        if path.is_symlink():
            raise BackupError("曲库包含符号链接，不能保证完整备份，已停止。")
    index = library.index
    if index.is_symlink():
        raise BackupError("索引不能是符号链接。")
    index_state = None
    if index.exists():
        if index.stat().st_size > MAX_MANIFEST:
            raise BackupError("源曲库索引过大。")
        with _regular(index) as handle:
            before = _signature(os.fstat(handle.fileno()))
            raw = handle.read(MAX_MANIFEST + 1)
            if len(raw) > MAX_MANIFEST or _signature(os.fstat(handle.fileno())) != before:
                raise BackupError("源曲库索引在读取时变化或过大。")
        index_state = (before, hashlib.sha256(raw).hexdigest())
    rows = library.list()
    names = set()
    for row in rows:
        name = _filename(row["filename"])
        key = _canonical(name)
        if key in names:
            raise BackupError("曲库存在大小写或Unicode等价重名，请先在管理页整理。")
        names.add(key)
        _metadata(row)
    if (
        len(rows) > MAX_COUNT
        or sum(r["size_bytes"] for r in rows) > MAX_TOTAL
        or any(not 0 < r["size_bytes"] <= MAX_FILE for r in rows)
    ):
        raise BackupError("曲库大小或数量超过备份限制。")
    state = [
        (
            r["filename"],
            _signature(get_preset_song_path(library.songs, r["filename"]).stat()),
            r["revision"],
        )
        for r in rows
    ]
    return rows, (index_state, sorted(state))


def _info(name, compression=zipfile.ZIP_STORED):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    info.compress_type = compression
    return info


def _copy(source, target, expected, *, total_limit=MAX_FILE):
    digest = hashlib.sha256()
    count = 0
    while True:
        block = source.read(CHUNK)
        if not block:
            break
        count += len(block)
        if count > expected or count > total_limit:
            raise BackupError("文件实际体积超过声明或安全限制。")
        digest.update(block)
        if target is not None:
            target.write(block)
    if count != expected:
        raise BackupError("文件体积与清单不一致。")
    return digest.hexdigest()


def create_backup(data_root: Path, output: Path):
    """只读原库；临时归档完整校验后才独占发布，不覆盖现有备份。"""
    library = SongLibrary(Path(data_root), readonly=True)
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or output.suffix.lower() != ".zip":
        raise BackupError("备份目标必须是尚不存在的ZIP文件，不覆盖现有文件。")
    if output.resolve().is_relative_to(library.root.resolve()):
        raise BackupError("备份请保存到曲库数据目录之外，原库保持只读。")
    rows, snapshot = _snapshot(library)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=".whale-backup-", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temporary = Path(raw)
    manifest = {
        "format": FORMAT,
        "format_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "encrypted": False,
        "contains_private_audio": True,
        "songs": [],
        "total_bytes": 0,
    }
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=False) as archive:
            for row in rows:
                path = get_preset_song_path(library.songs, row["filename"])
                with (
                    _regular(path) as source,
                    archive.open(_info("songs/" + row["filename"]), "w") as target,
                ):
                    opened = os.fstat(source.fileno())
                    before = _signature(opened)
                    checksum = _copy(source, target, row["size_bytes"])
                    if (
                        _signature(os.fstat(source.fileno())) != before
                        or path.is_symlink()
                        or _opened_signature(path.stat()) != _opened_signature(opened)
                    ):
                        raise BackupError("原音在备份时发生变化，未发布不一致归档。")
                manifest["songs"].append(
                    {
                        "filename": row["filename"],
                        "size_bytes": row["size_bytes"],
                        "sha256": checksum,
                        "metadata": _metadata(row),
                    }
                )
                manifest["total_bytes"] += row["size_bytes"]
            data = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False).encode(
                "utf-8"
            )
            if len(data) > MAX_MANIFEST:
                raise BackupError("备份清单过大。")
            archive.writestr(_info(MANIFEST), data)
        if _snapshot(library)[1] != snapshot:
            raise BackupError("曲库在备份期间发生变化，未发布归档，请稍后重试。")
        with temporary.open("r+b") as durable:
            durable.flush()
            os.fsync(durable.fileno())
        verified = verify_backup(temporary)
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise BackupError("目标备份刚被其他操作创建，不会覆盖。") from exc
        return {
            "backup_created": True,
            "archive_sha256": verified["archive_sha256"],
            "song_count": len(rows),
            "audio_bytes": manifest["total_bytes"],
            "encrypted": False,
            "contains_private_audio": True,
            "warning": "备份未加密且包含音频和元数据，请私下保存，不要上传GitHub。",
        }
    finally:
        temporary.unlink(missing_ok=True)


def _central_directory(handle):
    size = os.fstat(handle.fileno()).st_size
    if not 22 <= size <= MAX_ARCHIVE:
        raise BackupError("归档大小无效或超过安全上限。")
    handle.seek(max(0, size - 65557))
    tail = handle.read(65557)
    marker = tail.rfind(b"PK\x05\x06")
    if marker < 0 or len(tail) - marker < 22:
        raise BackupError("归档尾部损坏。")
    _, disk, central_disk, disk_count, count, central_size, offset, comment_size = (
        struct.unpack_from("<4s4H2LH", tail, marker)
    )
    absolute = size - len(tail) + marker
    if (
        disk
        or central_disk
        or count != disk_count
        or count > MAX_COUNT + 1
        or count == 0xFFFF
        or central_size > MAX_CENTRAL
        or offset + central_size != absolute
        or absolute + 22 + comment_size != size
    ):
        raise BackupError("拒绝分卷、ZIP64、异常目录或过多条目的归档。")
    handle.seek(0)


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("清单包含重复JSON字段。")
        result[key] = value
    return result


def _inspect(archive):
    if archive.comment:
        raise BackupError("备份格式不允许ZIP注释。")
    entries = archive.infolist()
    if len(entries) > MAX_COUNT + 1:
        raise BackupError("归档条目数量超过上限。")
    if not entries or min(info.header_offset for info in entries) != 0:
        raise BackupError("备份包含未知前缀内容。")
    mapping, canonical = {}, set()
    total = 0
    for info in entries:
        name = info.filename
        key = _canonical(name)
        mode = info.external_attr >> 16
        if (
            info.orig_filename != name
            or bool(info.extra)
            or bool(info.comment)
            or key in canonical
            or info.is_dir()
            or info.flag_bits & 1
            or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
            or info.external_attr & 0x10
        ):
            raise BackupError("归档含重复条目、链接、特殊文件、加密或不支持的压缩格式。")
        canonical.add(key)
        if name == MANIFEST:
            if info.file_size > MAX_MANIFEST:
                raise BackupError("清单体积过大。")
        elif name.startswith("songs/") and name.count("/") == 1:
            _filename(name[6:])
            if not 0 < info.file_size <= MAX_FILE:
                raise BackupError("曲目体积超限。")
            total += info.file_size
        else:
            raise BackupError("归档包含不属于曲库的路径或文件。")
        if total > MAX_TOTAL:
            raise BackupError("归档解压体积超过曲库上限。")
        mapping[name] = info
    if MANIFEST not in mapping:
        raise BackupError("缺少备份清单。")
    try:
        with archive.open(mapping[MANIFEST]) as handle:
            raw = handle.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            raise BackupError("清单实际体积超限。")
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BackupError("备份清单不是有效UTF-8 JSON。") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != FORMAT
        or type(manifest.get("format_version")) is not int
        or manifest["format_version"] != 1
        or manifest.get("encrypted") is not False
        or manifest.get("contains_private_audio") is not True
        or not isinstance(manifest.get("songs"), list)
        or len(manifest["songs"]) > MAX_COUNT
        or type(manifest.get("total_bytes")) is not int
        or manifest["total_bytes"] != total
    ):
        raise BackupError("备份格式或总量不符合清单。")
    if set(manifest) != {
        "format",
        "format_version",
        "created_at",
        "encrypted",
        "contains_private_audio",
        "songs",
        "total_bytes",
    }:
        raise BackupError("备份清单包含未知字段。")
    try:
        created = manifest["created_at"]
        if (
            not isinstance(created, str)
            or len(created) > 80
            or dt.datetime.fromisoformat(created.replace("Z", "+00:00")).tzinfo is None
        ):
            raise ValueError()
    except ValueError as exc:
        raise BackupError("备份时间格式无效。") from exc
    files = set()
    for row in manifest["songs"]:
        if not isinstance(row, dict) or set(row) != {
            "filename",
            "size_bytes",
            "sha256",
            "metadata",
        }:
            raise BackupError("曲目清单格式无效或包含未知字段。")
        name = "songs/" + _filename(row.get("filename"))
        if (
            name in files
            or name not in mapping
            or type(row.get("size_bytes")) is not int
            or row["size_bytes"] != mapping[name].file_size
            or not isinstance(row.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
        ):
            raise BackupError("曲目清单与归档文件不一致。")
        _metadata(row.get("metadata"), strict=True)
        files.add(name)
    if files | {MANIFEST} != set(mapping):
        raise BackupError("归档存在未列入清单的文件。")
    return manifest, mapping


def _verify_members(archive, manifest, mapping):
    for row in manifest["songs"]:
        with archive.open(mapping["songs/" + row["filename"]]) as source:
            checksum = _copy(source, None, row["size_bytes"])
        if checksum != row["sha256"]:
            raise BackupError("音频SHA256校验失败，未恢复文件。")


def verify_backup(path: Path):
    try:
        with _regular(path) as handle:
            before = _signature(os.fstat(handle.fileno()))
            _central_directory(handle)
            with zipfile.ZipFile(handle) as archive:
                manifest, mapping = _inspect(archive)
                _verify_members(archive, manifest, mapping)
            handle.seek(0)
            archive_sha256 = _copy(handle, None, before[2], total_limit=MAX_ARCHIVE)
            if _signature(os.fstat(handle.fileno())) != before:
                raise BackupError("归档在校验时发生变化。")
        return {
            "valid": True,
            "archive_sha256": archive_sha256,
            "song_count": len(manifest["songs"]),
            "audio_bytes": manifest["total_bytes"],
            "encrypted": False,
            "contains_private_audio": True,
        }
    except BackupError:
        raise
    except (
        UnicodeError,
        ValueError,
        zlib.error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        RuntimeError,
        NotImplementedError,
    ) as exc:
        raise BackupError("归档损坏、CRC错误或压缩格式不支持。") from exc


def restore_backup(path: Path, destination: Path):
    """完整校验后才独占创建目标；失败时保留带标记的暂停隔离目录。"""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError("恢复只能使用尚不存在的新目录，不能覆盖或清空现有目录。")
    reserved = False
    try:
        with _regular(path) as handle:
            before = _signature(os.fstat(handle.fileno()))
            _central_directory(handle)
            with zipfile.ZipFile(handle) as archive:
                manifest, mapping = _inspect(archive)
                _verify_members(archive, manifest, mapping)
                if _signature(os.fstat(handle.fileno())) != before:
                    raise BackupError("归档在预检时发生变化。")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.mkdir(mode=0o700, exist_ok=False)
                reserved = True
                marker = destination / ".restore-incomplete"
                marker.write_text("恢复尚未完成，不能用于运行。", encoding="utf-8")
                songs = destination / "songs"
                songs.mkdir(mode=0o700)
                index = {
                    "version": 1,
                    "songs": {
                        row["filename"]: {
                            **_metadata(row["metadata"]),
                            "enabled": False,
                            "revision": 1,
                        }
                        for row in manifest["songs"]
                    },
                }
                atomic_json(destination / "song-library.json", index)
                for row in manifest["songs"]:
                    target = songs / row["filename"]
                    fd = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                        0o600,
                    )
                    with (
                        os.fdopen(fd, "wb") as output,
                        archive.open(mapping["songs/" + row["filename"]]) as source,
                    ):
                        checksum = _copy(source, output, row["size_bytes"])
                        output.flush()
                        os.fsync(output.fileno())
                    if checksum != row["sha256"]:
                        raise BackupError("恢复过程中校验不一致。")
                if _signature(os.fstat(handle.fileno())) != before:
                    raise BackupError("归档在恢复时发生变化。")
                marker.unlink()
        return {
            "restored": True,
            "song_count": len(manifest["songs"]),
            "enabled_count": 0,
            "requires_manual_enable": True,
            "credentials_restored": False,
        }
    except (
        UnicodeError,
        zlib.error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        RuntimeError,
        NotImplementedError,
    ) as exc:
        if reserved:
            raise BackupError("恢复未完成，新目录不应投入运行；现有曲库未改动。") from exc
        raise BackupError("归档损坏，未创建恢复目录。") from exc
    except (OSError, BackupError) as exc:
        if reserved:
            raise BackupError(
                "恢复未完成，保留新隔离目录供检查，请勿投入运行；不会覆盖现有曲库。"
            ) from exc
        raise
