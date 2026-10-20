# -*- coding: utf-8 -*-
"""文件工具箱入口（功能已按模块拆分至 toolbox/ 包，打包仍为单个 exe）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from toolbox.main import main

if __name__ == "__main__":
    main()
