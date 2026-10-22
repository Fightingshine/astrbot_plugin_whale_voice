"""构建可导入AstrBot的确定性源码ZIP；只打包公开白名单。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

from public_tree import public_files, scan_tree

PACKAGE = "astrbot_plugin_whale_voice"


def release_version(root):
    tree = ast.parse((root / "main.py").read_text(encoding="utf-8-sig"))
    version = next(
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in n.targets)
    )
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8-sig")
    found = re.search(r"(?m)^version:\s*[\"']?([^\s\"']+)", metadata)
    if (
        not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version)
        or not found
        or found.group(1).removeprefix("v") != version
    ):
        raise ValueError("运行版本和metadata.yaml不一致，拒绝构建。")
    project = (root / "pyproject.toml").read_text(encoding="utf-8-sig")
    if not re.search(r'^version\s*=\s*"' + re.escape(version) + r'"', project, re.M):
        raise ValueError("pyproject.toml版本不一致，拒绝构建。")
    return version


def build(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    problems = scan_tree(root)
    if problems:
        raise ValueError(
            "公开源码检查失败：" + "; ".join(x["file"] + "：" + x["reason"] for x in problems)
        )
    version = release_version(root)
    manifest = {"plugin": PACKAGE, "version": version, "files": {}}
    contents = {}
    for path in public_files(root):
        name = path.relative_to(root).as_posix()
        blob = path.read_bytes()
        if path.suffix.lower() not in {".png", ".webp"}:
            blob = blob.decode("utf-8-sig").replace("\r\n", "\n").encode("utf-8")
        contents[name] = blob
        manifest["files"][name] = {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
    required = {
        "main.py",
        "core.py",
        "library.py",
        "backup.py",
        "fingerprints.py",
        "voice_config.py",
        "metadata.yaml",
        "_conf_schema.json",
        "pages/songs/index.html",
        "LICENSE",
    }
    if not required.issubset(contents):
        raise ValueError("源码归档缺少必需运行文件或页面，拒绝发布。")
    contents["PUBLIC_FILES.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, blob in sorted(contents.items()):
            item = zipfile.ZipInfo(PACKAGE + "/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, blob)
    output.mkdir(parents=True, exist_ok=True)
    path = output / (PACKAGE + "-v" + version + ".zip")
    blob = target.getvalue()
    if path.exists() and path.read_bytes() != blob:
        raise ValueError("同版本归档已存在且内容不同；请升级版本或使用新的输出目录，不盲目覆盖。")
    path.write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        sha + "  " + path.name + "\n", encoding="utf-8"
    )
    return {
        "version": version,
        "archive": str(path),
        "sha256": sha,
        "source_file_count": len(manifest["files"]),
    }


def main():
    parser = argparse.ArgumentParser(description="构建不含运行数据或音色引用的源码归档。")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "build_artifacts"
    )
    args = parser.parse_args()
    try:
        result = build(Path(__file__).resolve().parents[1], args.output_dir)
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
