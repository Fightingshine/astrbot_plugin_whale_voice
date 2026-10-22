from __future__ import annotations

import json
from pathlib import Path

from public_tree import scan_tree


def main():
    root = Path(__file__).resolve().parents[1]
    problems = scan_tree(root)
    print(
        json.dumps(
            {
                "ok": not problems,
                "problems": problems,
                "note": "这是自动化辅助检查，不能替代发布前人工审查。",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
