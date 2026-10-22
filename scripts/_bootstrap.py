"""让维护脚本在任意克隆目录名下使用插件的规范包名。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "astrbot_plugin_whale_voice"


def bootstrap_package():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("无法初始化插件包。")
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = module
        spec.loader.exec_module(module)
    return ROOT
