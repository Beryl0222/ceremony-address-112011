#!/usr/bin/env python3
"""隔离模式（python3 -I）下的工作台启动器。

`-I` 会忽略 PYTHONPATH 且 -m 不补当前目录，因此用本脚本显式注入
仓库路径后再调用 CLI，保证读取资料时不会加载到工作目录中的同名模块。

    python3 -I run_workbench.py host S2
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from workbench.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
