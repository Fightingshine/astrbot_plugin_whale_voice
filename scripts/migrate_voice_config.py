"""将旧版源码中的音色映射迁到私有数据目录；默认只检查。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import bootstrap_package

bootstrap_package()
from astrbot_plugin_whale_voice.voice_config import VoiceConfigError, migrate_voice_aliases


def main():
    parser = argparse.ArgumentParser(
        description="迁移旧版克隆音色引用，不执行旧源码、不输出音色值。"
    )
    parser.add_argument("--legacy-source", type=Path, required=True, help="已备份的旧版core.py")
    parser.add_argument(
        "--destination", type=Path, required=True, help="服务器插件数据目录中的voice-aliases.json"
    )
    parser.add_argument("--apply", action="store_true", help="确认写入；省略时只检查")
    args = parser.parse_args()
    try:
        result = migrate_voice_aliases(args.legacy_source, args.destination, apply=args.apply)
    except (VoiceConfigError, OSError, UnicodeError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc)
                    if isinstance(exc, VoiceConfigError)
                    else "文件读取或写入失败，请检查路径和权限。",
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
