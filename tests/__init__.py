"""猫娘代码协助 —— 单元测试包。

运行方式（仓库根目录）：
    python -m unittest discover -s tests -v
或：
    python tests/run_tests.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
