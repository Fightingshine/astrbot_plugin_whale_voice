"""提取内联脚本交给Node检查语法，不执行页面或网络请求。"""

from __future__ import annotations

import re
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if any(name.lower().startswith("on") for name, _ in attrs):
            raise ValueError("页面不能使用内联事件属性，请使用安全的事件监听器。")
        if tag == "script" and not dict(attrs).get("src"):
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, text):
        if self.active:
            self.parts.append(text)


def main():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "pages").rglob("*.html"):
        parser = Scripts()
        parser.feed(path.read_text(encoding="utf-8-sig"))
        if re.search(r"\b(?:window\.)?(?:prompt|confirm|alert)\s*\(", "\n".join(parser.parts)):
            raise ValueError("原生沙箱不允许系统对话框，请使用HTML dialog。")
        with tempfile.TemporaryDirectory(prefix="whale-js-check-") as directory:
            script = Path(directory) / "page.js"
            script.write_text("\n".join(parser.parts), encoding="utf-8")
            subprocess.run(["node", "--check", str(script)], check=True)
    print("页面脚本语法与内联事件检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
