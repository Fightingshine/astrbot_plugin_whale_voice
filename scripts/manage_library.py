"""私有曲库离线维护入口，不调用模型、QQ或云端音色服务。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import bootstrap_package

bootstrap_package()
from astrbot_plugin_whale_voice.backup import (
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from astrbot_plugin_whale_voice.library import LibraryError


def main():
    parser = argparse.ArgumentParser(
        description="备份原音与元数据，校验归档，只恢复到新目录且默认暂停点播。"
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    backup = commands.add_parser("backup", help="只读导出私有曲库，不覆盖已有ZIP")
    backup.add_argument("--data-dir", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="有界校验清单、文件路径、CRC与SHA256")
    verify.add_argument("--archive", type=Path, required=True)
    restore = commands.add_parser("restore", help="完整校验后恢复到尚不存在的新目录")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "backup":
            result = create_backup(args.data_dir, args.output)
        elif args.operation == "verify":
            result = verify_backup(args.archive)
        else:
            result = restore_backup(args.archive, args.destination)
    except (BackupError, LibraryError, OSError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, (BackupError, LibraryError))
            else "文件操作失败，请检查路径、权限和磁盘空间；原曲库未被覆盖。"
        )
        print(
            json.dumps(
                {"ok": False, "error": message, "cloud_calls": 0, "qq_sends": 0}, ensure_ascii=False
            )
        )
        return 1
    print(
        json.dumps(
            {"ok": True, **result, "cloud_calls": 0, "qq_sends": 0}, ensure_ascii=False, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
