"""公开文件白名单与尽力而为的敏感信息检查，不读取运行时数据。"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

ROOT_FILES = {
    "__init__.py",
    "main.py",
    "core.py",
    "library.py",
    "backup.py",
    "voice_config.py",
    "metadata.yaml",
    "_conf_schema.json",
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-browser.txt",
    ".gitignore",
    ".gitattributes",
}
SOURCE_DIRS = {"pages", "tests", "scripts", "docs", "examples", ".github"}
EXTENSIONS = {
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".txt",
    ".html",
    ".js",
    ".css",
    ".svg",
    ".png",
    ".webp",
}
SKIP = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".ruff_cache",
    ".pytest_cache",
    "build_artifacts",
    "browser_artifacts",
}
VOICE = re.compile(r"speech:([A-Za-z0-9_-]+):[A-Za-z0-9_-]+:[A-Za-z0-9_-]+")
SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{32,}"
)
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
DOCUMENTATION_NETS = tuple(
    ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)


def public_files(root: Path):
    root = Path(root).resolve()
    result = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in SKIP for part in rel.parts):
            continue
        if path.is_symlink():
            raise ValueError("公开源码中不能包含符号链接：" + rel.as_posix())
        if not path.is_file():
            continue
        if len(rel.parts) == 1 and rel.name in ROOT_FILES:
            result.append(path)
        elif rel.parts[0] in SOURCE_DIRS and path.suffix.lower() in EXTENSIONS:
            result.append(path)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def scan_tree(root: Path):
    root = Path(root).resolve()
    problems = []
    approved = {p.relative_to(root).as_posix(): p for p in public_files(root)}
    if (root / ".git").exists():
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        )
        for name in set(result.stdout.decode("utf-8").split("\0")) - {""}:
            if name not in approved:
                problems.append({"file": name, "reason": "不在公开文件白名单中"})
    for name, path in approved.items():
        if path.stat().st_size > 8 * 1024 * 1024:
            problems.append({"file": name, "reason": "源码文件异常大"})
            continue
        if path.suffix.lower() in {".png", ".webp"}:
            if not name.startswith("docs/"):
                problems.append({"file": name, "reason": "仅允许文档目录中的图片"})
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            problems.append({"file": name, "reason": "文本不是UTF-8"})
            continue
        if SECRET.search(text):
            problems.append({"file": name, "reason": "疑似真实凭据，具体值不会回显"})
        if any(
            match.group(1) not in {"fixture", "example", "test"} for match in VOICE.finditer(text)
        ):
            problems.append({"file": name, "reason": "存在非测试用私有音色引用"})
        for match in IPV4.finditer(text):
            try:
                ip = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if not ip.is_loopback and not any(ip in network for network in DOCUMENTATION_NETS):
                problems.append({"file": name, "reason": "存在部署IP，请改为文档示例或运行时配置"})
                break
    return problems
