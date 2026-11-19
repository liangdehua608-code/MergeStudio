#!/usr/bin/env python3
"""便捷入口：python run.py 等价于 python -m mergestudio。"""
import sys

from mergestudio.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
