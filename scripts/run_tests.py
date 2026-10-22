"""统一离线回归入口；不会请求模型或真实QQ。"""

from __future__ import annotations

import sys
import unittest

from _bootstrap import bootstrap_package


def main():
    root = bootstrap_package()
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
