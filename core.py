"""按需语音合成的输入校验、持久化限流和小文件音频处理。"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .voice_config import VOICE_URI

MODEL = "FunAudioLLM/CosyVoice2-0.5B"
ENDPOINT = "https://api.siliconflow.cn/v1/audio/speech"
VOICES = frozenset(("anna", "bella", "claire", "alex", "benjamin", "charles", "david"))
VOICE_ALIASES: dict[str, str] = {}  # 私有映射存放于插件数据目录，不提交到源码。
MAX_BYTES = 20 * 1024 * 1024
TZ = dt.timezone(dt.timedelta(hours=8))


def resolve_voice(voice: str, aliases: dict[str, str] | None = None) -> str:
    """内置音色或管理员显式配置的私有引用；不含任何部署专用默认值。"""
    if not isinstance(voice, str):
        raise VoiceError("未识别的语音音色，未发起合成。")
    voice = voice.strip()
    mapping = VOICE_ALIASES if aliases is None else aliases
    if voice in mapping:
        uri = mapping[voice]
        if not isinstance(uri, str) or not VOICE_URI.fullmatch(uri):
            raise VoiceError("私有音色映射无效，未发起合成。")
        return uri
    if VOICE_URI.fullmatch(voice):
        return voice
    if voice in VOICES:
        return f"{MODEL}:{voice}"
    if voice in {f"{MODEL}:{name}" for name in VOICES}:
        return voice
    raise VoiceError("音色尚未配置。自定义音色请填写私有引用或导入voice-aliases.json，不要改源码。")


class VoiceError(Exception):
    """可直接反馈给用户的脱敏错误。"""


def speech_text(value: str, limit: int) -> str:
    if not isinstance(value, str):
        raise VoiceError("小蓝鲸还没拿到要说的文字呀～")
    text = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]",
        "",
        value,
    ).strip()
    if not text:
        raise VoiceError("想听小蓝鲸说什么？把文字或问题也告诉我吧～")
    if len(text) > limit:
        raise VoiceError(f"这段有点长啦，先缩到 {limit} 字以内，好让大家听得清楚～")
    if re.search(
        r"sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+|-----BEGIN .*PRIVATE KEY-----",
        text,
        re.IGNORECASE,
    ):
        raise VoiceError("这段像是包含密钥，不能送去语音服务。请先去掉敏感凭据呀～")
    return text


def atomic_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def atomic_bytes(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    with temp.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(str(x) for x in parts).encode()).hexdigest()


class UsageGate:
    """不存聊天原文；发起合成前占额，失败/超时不自动重试。"""

    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            try:
                self.state = json.loads(path.read_text(encoding="utf-8"))
                assert all(isinstance(self.state[k], dict) for k in ("requests", "users", "total"))
            except (ValueError, KeyError, TypeError, AssertionError) as exc:
                raise VoiceError(
                    "语音限流记录异常，已停止生成，请管理员检查；不会自动清零额度。"
                ) from exc
        else:
            self.state = {"requests": {}, "users": {}, "total": {}}

    def reserve(
        self,
        user: str,
        message: str,
        operation: str,
        *,
        cooldown=30,
        per_user=10,
        total=100,
        now=None,
    ):
        now = time.time() if now is None else float(now)
        if operation not in ("speech", "sing") or not user or not message:
            raise VoiceError("消息标识不完整，未提交语音请求。")
        day = dt.datetime.fromtimestamp(now, TZ).date().isoformat()
        state = json.loads(json.dumps(self.state))
        state["requests"] = {k: v for k, v in state["requests"].items() if now - float(v) < 172800}
        state["users"] = {k: v for k, v in state["users"].items() if v.get("day") == day}
        state["total"] = {k: v for k, v in state["total"].items() if k == day}
        if message in state["requests"]:
            raise VoiceError("这条消息已经处理过啦，不会重复合成或扣费。想换内容请发一条新消息～")
        row = state["users"].get(user, {"day": day, "count": 0, "last": 0})
        if row["count"] and now - row["last"] < cooldown:
            raise VoiceError("让小蓝鲸喝口水嘛～稍等半分钟再喊我开口。")
        if row["count"] >= per_user or state["total"].get(day, 0) >= total:
            raise VoiceError("今天的语音小额度用完啦，明天再来听小蓝鲸吧～")
        state["requests"][message] = now
        state["users"][user] = {"day": day, "count": row["count"] + 1, "last": now}
        state["total"][day] = state["total"].get(day, 0) + 1
        atomic_json(self.path, state)
        self.state = state


def siliconflow_key(config: dict, provider_id: str) -> str:
    for row in config.get("provider", []):
        if not isinstance(row, dict) or row.get("id") != provider_id:
            continue
        if row.get("type") != "openai_embedding" or not row.get("enable", True):
            break
        parsed = urlsplit(str(row.get("embedding_api_base", "")))
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.siliconflow.cn"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") not in ("", "/v1")
        ):
            raise VoiceError("凭据来源不是已配置的硅基流动官方地址，拒绝转发密钥。")
        value = row.get("embedding_api_key", "")
        if isinstance(value, list):
            value = next((x for x in value if isinstance(x, str) and x.strip()), "")
        if isinstance(value, str) and value.strip() and not any(c.isspace() for c in value):
            return value
        break
    raise VoiceError("语音服务还没有配置好，请管理员检查硅基流动凭据来源。")


ALLOWED_SONG_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
MAX_SONG_UPLOAD_BYTES = 50 * 1024 * 1024


def _normalize_title(s: str) -> str:
    """去除歌名中的空格、标点、下划线及连接符，用于鲁棒比对。"""
    return re.sub(r"[\s\-_——·,，.。!！?？~～()（）\[\]【】]+", "", str(s or "")).lower()


def match_preset_song(songs_dir: Path | str, query: str) -> tuple[Path, str] | None:
    """在预置曲库目录检索歌曲；命中返回 (音频路径, 歌曲名)，未命中返回 None。"""
    if isinstance(songs_dir, str):
        songs_dir = Path(songs_dir)
    if (
        not isinstance(songs_dir, Path)
        or not songs_dir.exists()
        or not isinstance(query, str)
        or not query.strip()
    ):
        return None
    cleaned = re.sub(
        r"^(?:唱首|来首|播放|唱一个|唱|我要听|我想听|请唱|来个|放首|放一个|放)\s*",
        "",
        query.strip(),
    )
    cleaned = re.sub(r"\s*(?:这首歌|的歌|歌曲|歌)?$", "", cleaned).strip().lower()
    if not cleaned:
        cleaned = query.strip().lower()

    norm_cleaned = _normalize_title(cleaned)
    norm_query = _normalize_title(query)

    audio_files = []
    for file in sorted(songs_dir.iterdir()):
        if (
            file.is_file()
            and not file.is_symlink()
            and file.suffix.lower() in ALLOWED_SONG_EXTENSIONS
        ):
            stem = file.stem.lower()
            norm_stem = _normalize_title(stem)
            audio_files.append((file, stem, norm_stem))

    # 1. 第一优先级：精准匹配
    # 1.1 原名精准匹配
    for file, stem, _norm_stem in audio_files:
        if cleaned == stem or query.strip().lower() == stem:
            return (file, file.stem)

    # 1.2 归一化精准匹配（如 "人是猫完整版" 精准匹配 "人是猫_完整版.mp3"）
    for file, _stem, norm_stem in audio_files:
        if norm_cleaned == norm_stem or norm_query == norm_stem:
            return (file, file.stem)

    # 2. 第二优先级：子串模糊匹配
    candidates = []
    for file, _stem, norm_stem in audio_files:
        if norm_cleaned and norm_stem:
            if norm_cleaned in norm_stem:
                # 搜索词是歌曲名一部分（如搜索"猫"，匹配"人是猫"）
                candidates.append((0, len(norm_stem) - len(norm_cleaned), file))
            elif norm_stem in norm_cleaned:
                # 歌曲名是搜索词一部分
                candidates.append((1, len(norm_cleaned) - len(norm_stem), file))

    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1]))
        best = candidates[0][2]
        return (best, best.stem)
    return None


def format_bytes(size: int) -> str:
    """人性化格式化字节大小。"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"


def sanitize_song_name(name: str) -> str:
    """清理歌曲名，移除路径字符与非法字符，保证安全。"""
    cleaned = re.sub(r'[\/\\:\*\?"<>\|\x00-\x1f]', "", str(name or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # 移除开头和结尾的句点，避免隐藏文件或相对路径
    cleaned = cleaned.strip(". ")
    if not cleaned:
        raise VoiceError("歌曲名不能为空或包含非法字符。")
    if len(cleaned) > 64:
        cleaned = cleaned[:64].strip()
    return cleaned


def list_preset_songs_detail(songs_dir: Path | str) -> list[dict]:
    """先过滤符号链接再取stat；单个文件被移除不影响整个曲库。"""
    root = Path(songs_dir)
    if not root.is_dir() or root.is_symlink():
        return []
    result = []
    for file in root.iterdir():
        try:
            if (
                file.is_symlink()
                or not file.is_file()
                or file.suffix.lower() not in ALLOWED_SONG_EXTENSIONS
            ):
                continue
            stat = file.stat()
            result.append(
                {
                    "name": file.stem,
                    "filename": file.name,
                    "extension": file.suffix.lower(),
                    "size_bytes": stat.st_size,
                    "size_str": format_bytes(stat.st_size),
                    "mtime": int(stat.st_mtime),
                    "mtime_ns": stat.st_mtime_ns,
                    "mtime_str": dt.datetime.fromtimestamp(stat.st_mtime, TZ).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                }
            )
        except OSError:
            continue
    return sorted(result, key=lambda item: (-item["mtime_ns"], item["filename"]))


def get_preset_song_path(songs_dir: Path | str, filename: str) -> Path:
    """只接受曲库内单个音频文件名，拒绝路径折叠与符号链接。"""
    root = Path(songs_dir)
    if not root.is_dir() or root.is_symlink():
        raise VoiceError("曲库目录不存在或不安全。")
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 240
        or filename in (".", "..")
        or any(c in filename for c in ("/", "\\", "\x00"))
        or Path(filename).suffix.lower() not in ALLOWED_SONG_EXTENSIONS
    ):
        raise VoiceError("歌曲文件名或格式非法。")
    target = root / filename
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise VoiceError("拒绝访问链接或非法文件路径。")
    if not target.is_file():
        raise VoiceError("未找到歌曲文件，请刷新曲库。")
    return target


def save_preset_song(
    songs_dir: Path | str,
    song_name: str,
    ext: str,
    content: bytes,
) -> tuple[Path, str]:
    """安全保存上传的音频文件到预置曲库。"""
    if isinstance(songs_dir, str):
        songs_dir = Path(songs_dir)
    if not isinstance(songs_dir, Path) or not songs_dir.exists():
        raise VoiceError("曲库目录不存在。")
    ext = ext.lower().strip()
    if not ext.startswith("."):
        ext = "." + ext
    if ext not in ALLOWED_SONG_EXTENSIONS:
        raise VoiceError(
            f"不支持的音频格式：{ext}，支持的格式为 {', '.join(sorted(ALLOWED_SONG_EXTENSIONS))}"
        )
    if not content or len(content) == 0:
        raise VoiceError("上传的音频内容为空。")
    if len(content) > MAX_SONG_UPLOAD_BYTES:
        raise VoiceError(
            f"上传文件大小超过限制（最大支持 {format_bytes(MAX_SONG_UPLOAD_BYTES)}）。"
        )

    safe_name = sanitize_song_name(song_name)
    target_filename = f"{safe_name}{ext}"
    target_path = songs_dir / target_filename

    # 独占发布，避免同名上传静默覆盖；先完整写入临时文件再提交。
    temp = songs_dir / ("." + uuid.uuid4().hex + ".upload")
    try:
        with temp.open("xb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temp, target_path)
        except FileExistsError as exc:
            raise VoiceError("已存在同名歌曲，原文件未被覆盖，请使用不同名称。") from exc
    finally:
        temp.unlink(missing_ok=True)
    return target_path, safe_name


def delete_preset_song(songs_dir: Path | str, filename: str) -> str:
    """从曲库删除指定的音频文件。"""
    target = get_preset_song_path(songs_dir, filename)
    song_name = target.stem
    target.unlink(missing_ok=True)
    return song_name


def rename_preset_song(
    songs_dir: Path | str,
    filename: str,
    new_name: str,
) -> tuple[Path, str]:
    """安全重命名曲库中的歌曲。"""
    old_target = get_preset_song_path(songs_dir, filename)
    safe_new_name = sanitize_song_name(new_name)
    new_filename = f"{safe_new_name}{old_target.suffix}"
    new_target = old_target.parent / new_filename
    if new_target.exists() and new_target.resolve() != old_target.resolve():
        raise VoiceError(f"歌名《{safe_new_name}》已存在，请换一个名称。")
    old_target.rename(new_target)
    return new_target, safe_new_name


def list_preset_songs(songs_dir: Path | str) -> list[str]:
    return [
        row["name"]
        for row in sorted(list_preset_songs_detail(songs_dir), key=lambda row: row["filename"])
    ]


def singing_ready(provider):
    if provider is None:
        raise VoiceError(
            "小蓝鲸的歌唱声线还没接好呀～请管理员先配置 MiMo 歌唱提供商，再把提供商 ID 填进本插件；不会把朗读歌词冒充唱歌。"
        )
    if provider.__class__.__name__ != "ProviderMiMoTTSAPI" or not hasattr(
        provider, "_build_style_prefix"
    ):
        raise VoiceError(
            "歌唱提供商需要选择支持歌唱模式的 MiMo TTS，普通朗读音色不能用来冒充唱歌。"
        )
    if provider._build_style_prefix() != "<style>唱歌</style>":
        raise VoiceError("MiMo 还没有启用歌唱模式，请把该提供商的风格提示设置为“唱歌”。")
    if not str(getattr(provider, "chosen_api_key", "")).strip():
        raise VoiceError(
            "MiMo 歌唱提供商还缺 API Key，请管理员在 AstrBot 模型提供商页面填写，不要发到群里。"
        )
    return provider


async def synthesize(session, key: str, text: str, voice: str, output: Path, *, voice_aliases=None):
    voice_param = resolve_voice(voice, voice_aliases)
    payload = {
        "model": MODEL,
        "input": text,
        "voice": voice_param,
        "response_format": "mp3",
        "speed": 1.0,
    }
    try:
        async with session.post(
            ENDPOINT,
            headers={"Authorization": "Bearer " + key},
            json=payload,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise VoiceError(
                    f"语音服务返回 HTTP {response.status}，这次没有拿到语音；不会自动重复请求。"
                )
            kind = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if kind not in (
                "audio/mpeg",
                "audio/mp3",
                "application/octet-stream",
                "audio/wav",
                "audio/x-wav",
            ):
                raise VoiceError("语音服务未返回音频，已停止处理，不会自动重试。")
            size = 0
            with output.open("xb") as f:
                async for block in response.content.iter_chunked(32768):
                    size += len(block)
                    if size > MAX_BYTES:
                        raise VoiceError("语音文件过大，已停止接收，不会重复生成。")
                    f.write(block)
            if size < 16:
                raise VoiceError("语音服务返回了空音频，不会自动重试。")
    except BaseException:
        output.unlink(missing_ok=True)
        raise


async def normalize_audio(source: Path, output: Path, max_seconds: int):
    """小文件转单声道 WAV；所有子进程都有超时并在取消时回收。"""
    if source.is_symlink() or not source.is_file() or not 16 <= source.stat().st_size <= MAX_BYTES:
        raise VoiceError("语音文件不存在或大小异常，未向 QQ 发送。")

    async def run(*args):
        p = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout=20)
            if p.returncode:
                raise VoiceError("音频格式处理失败，未向 QQ 发送；不会自动重新生成。")
            return out
        except BaseException:
            if p.returncode is None:
                p.kill()
                await p.communicate()
            raise

    data = await run(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(source),
    )
    try:
        duration = float(json.loads(data)["format"]["duration"])
        if not 0 < duration <= max_seconds:
            raise ValueError()
    except (ValueError, TypeError, KeyError) as exc:
        raise VoiceError("音频时长异常或过长，未向 QQ 发送；请下次缩短文字。") from exc
    try:
        await run(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-threads",
            "1",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(output),
        )
        if not output.is_file() or not 16 <= output.stat().st_size <= MAX_BYTES:
            raise VoiceError("转换后的语音大小异常，未向 QQ 发送。")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return duration


def clean_cache(directory: Path, *, now=None):
    now = time.time() if now is None else now
    for p in directory.iterdir():
        if p.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}\.(?:mp3|wav)", p.name):
            continue
        if p.is_file() and now - p.stat().st_mtime > 86400:
            p.unlink()
