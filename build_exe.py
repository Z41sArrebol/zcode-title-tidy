#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a single-file Windows executable.

输出：dist/zcode-title-tidy.exe
运行：python build_exe.py
"""

from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent

PyInstaller.__main__.run([
    str(ROOT / "tidy_titles.py"),
    "--name", "zcode-title-tidy",
    "--onefile",
    "--noconsole",
    "--add-data", f"{ROOT / 'web'};web",
    "--clean",
    "--noconfirm",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
])
