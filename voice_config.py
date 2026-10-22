"""私有音色映射与旧版迁移；只解析AST，不执行旧插件源码。"""

from __future__ import annotations

import ast
import json
import os
import re
import tempfile
from pathlib import Path

VOICE_URI = re.compile(r"speech:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+")
MAX_CONFIG_BYTES = 64 * 1024


class VoiceConfigError(ValueError):
    """不得在错误中回显私有音色引用或凭据。"""


def validate_aliases(obj):
    if not isinstance(obj, dict) or len(obj) > 64:
        raise VoiceConfigError("音色映射必须是最多64项的JSON对象。")
    for name, uri in obj.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 64
            or re.search(r"[\x00-\x1f\x7f]", name)
        ):
            raise VoiceConfigError("音色别名应为1—64字，不包含控制字符。")
        if not isinstance(uri, str) or not VOICE_URI.fullmatch(uri):
            raise VoiceConfigError("音色映射包含无效引用，请管理员检查私有配置。")
    return dict(obj)


def load_voice_aliases(path: Path):
    path = Path(path)
    if path.is_symlink():
        raise VoiceConfigError("私有音色配置不能是符号链接。")
    if not path.exists():
        return {}
    try:
        if path.is_symlink() or path.stat().st_size > MAX_CONFIG_BYTES:
            raise VoiceConfigError("私有音色配置不是安全的普通小文件。")
        return validate_aliases(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, json.JSONDecodeError) as exc:
        raise VoiceConfigError("私有音色配置读取失败，请管理员检查；未自动清空。") from exc


def extract_legacy_aliases(source: str):
    try:
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "VOICE_ALIASES" for t in node.targets
            ):
                return validate_aliases(ast.literal_eval(node.value))
    except (SyntaxError, ValueError, TypeError) as exc:
        raise VoiceConfigError("旧版音色映射无法安全解析，未执行旧源码、未写入配置。") from exc
    raise VoiceConfigError("旧源码中没有找到可迁移的VOICE_ALIASES。")


def migrate_voice_aliases(legacy_source: Path, destination: Path, *, apply=False):
    source = Path(legacy_source)
    destination = Path(destination)
    if source.is_symlink() or source.stat().st_size > 2 * 1024 * 1024:
        raise VoiceConfigError("旧源码必须是普通小文件。")
    old = extract_legacy_aliases(source.read_text(encoding="utf-8-sig"))
    current = load_voice_aliases(destination)
    merged = dict(current)
    for name, uri in old.items():
        if name in current and current[name] != uri:
            raise VoiceConfigError("已有同名音色映射与旧版冲突，已停止，不覆盖管理员配置。")
        merged[name] = uri
    added = len(merged) - len(current)
    result = {
        "legacy_count": len(old),
        "existing_count": len(current),
        "added_count": added,
        "written": False,
        "dry_run": not apply,
        "private_values_printed": False,
    }
    if not apply or not added:
        return result
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".voice-config-", suffix=".tmp", dir=destination.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, destination)
        result["written"] = True
    finally:
        tmp.unlink(missing_ok=True)
    return result
