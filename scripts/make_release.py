#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create clean source / Windows release zips without personal data.

运行：python scripts/make_release.py
输出：release/zcode-title-tidy-<version>-source.zip
      release/zcode-title-tidy-<version>-win64.zip
"""

from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tidy_titles as t  # noqa: E402

VERSION = t.VERSION
RELEASE_DIR = ROOT / "release"

EXCLUDE_DIRS = {
    ".git", ".venv", "__pycache__", "node_modules", "dist", "build",
    "release", "plans", "backups", "test-artifacts", "test-dbs",
}
EXCLUDE_FILES = {"tool.log", "config.json", "state-test.json", "token.txt"}
EXCLUDE_RE = re.compile(r"^(state-.*\.json|r\d+\.json)$", re.IGNORECASE)

SOURCE_ROOT_FILES = [
    "tidy_titles.py",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "build_exe.py",
    "启动预览.bat",
    "应用标题.bat",
    ".gitignore",
]
SOURCE_TREES = ["web", "tests", "scripts", "web-src"]


def should_skip(path: Path, rel: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if path.name in EXCLUDE_FILES or EXCLUDE_RE.match(path.name):
        return True
    return False


def add_tree(zf: zipfile.ZipFile, base: Path, prefix: str) -> int:
    count = 0
    for path in base.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(base)
        if should_skip(path, rel):
            continue
        zf.write(path, f"{prefix}/{rel.as_posix()}")
        count += 1
    return count


def main() -> int:
    RELEASE_DIR.mkdir(exist_ok=True)
    source_zip = RELEASE_DIR / f"zcode-title-tidy-{VERSION}-source.zip"
    binary_zip = RELEASE_DIR / f"zcode-title-tidy-{VERSION}-win64.zip"
    prefix = f"zcode-title-tidy-{VERSION}"

    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in SOURCE_ROOT_FILES:
            path = ROOT / name
            if path.exists():
                zf.write(path, f"{prefix}/{name}")
        for tree in SOURCE_TREES:
            base = ROOT / tree
            if base.is_dir():
                add_tree(zf, base, f"{prefix}/{tree}")

    exe = ROOT / "dist" / "zcode-title-tidy.exe"
    if exe.exists():
        with zipfile.ZipFile(binary_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(exe, f"{prefix}/zcode-title-tidy.exe")
            for name in ("README.md", "LICENSE", "CHANGELOG.md"):
                path = ROOT / name
                if path.exists():
                    zf.write(path, f"{prefix}/{name}")
        print(f"binary: {binary_zip} ({binary_zip.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print("binary: skipped (dist/zcode-title-tidy.exe not found; run build_exe.py)")

    print(f"source: {source_zip} ({source_zip.stat().st_size / 1024 / 1024:.1f} MB)")

    sums = []
    for path in (source_zip, binary_zip):
        if path.exists():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            sums.append(f"{digest}  {path.name}")
    (RELEASE_DIR / "SHA256SUMS.txt").write_text(
        chr(10).join(sums) + chr(10), encoding="utf-8"
    )
    print(f"checksums: {RELEASE_DIR / 'SHA256SUMS.txt'}")
    with zipfile.ZipFile(source_zip) as zf:
        names = zf.namelist()
    bad = [n for n in names if any(x in n for x in ("/plans/", "/backups/", "tool.log", "node_modules"))]
    if bad:
        print("ERROR: source zip contains private/build data:", bad[:10])
        return 1
    print(f"source files: {len(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
