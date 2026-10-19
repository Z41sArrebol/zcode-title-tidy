#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置优先级、API Key 隐藏、数据库结构检查。全部使用临时目录。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tidy_titles as t  # noqa: E402

TASKS_SCHEMA = """
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY, workspace_key TEXT, workspace_path TEXT, title TEXT,
  title_overridden INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
  created_at INTEGER, updated_at INTEGER, task_status TEXT,
  deleted INTEGER DEFAULT 0, meta_json TEXT
);
"""

SESSIONS_SCHEMA = """
CREATE TABLE session (
  id TEXT PRIMARY KEY, title TEXT, title_source TEXT, time_created INTEGER,
  time_updated INTEGER, task_type TEXT, parent_id TEXT, directory TEXT,
  time_title_updated INTEGER
);
CREATE TABLE message (id TEXT, session_id TEXT, sequence INTEGER, data TEXT);
CREATE TABLE part (message_id TEXT, session_id TEXT, sequence INTEGER, data TEXT);
"""


def main() -> int:
    saved_env = {
        key: os.environ.get(key)
        for key in (
            "ZCODE_TITLE_API",
            "ZCODE_TITLE_MODEL",
            "ZCODE_TITLE_API_KEY",
            "ZCODE_TITLE_ZCODE_DIR",
        )
    }
    for key in saved_env:
        os.environ.pop(key, None)

    try:
        with tempfile.TemporaryDirectory(prefix="title-tidy-config-test-") as td:
            root = Path(td)
            data = root / "data"
            zcode = root / "zcode"
            data.mkdir()
            (zcode / "v2").mkdir(parents=True)
            (zcode / "cli" / "db").mkdir(parents=True)

            t.DATA_DIR = data
            t.PLANS_DIR = data / "plans"
            t.BACKUPS_DIR = data / "backups"
            t.CONFIG_PATH = data / "config.json"
            t.LATEST_PLAN = t.PLANS_DIR / "latest.json"
            t.ensure_data_dirs()
            t.FILE_CONFIG = dict(t.CONFIG_DEFAULTS)

            # 1) 没有配置时，模型未配置
            t.load_config()
            assert not t.public_settings()["model_configured"], t.public_settings()

            # 2) 保存配置；key 不能出现在公开设置里
            t.save_config({
                "api_url": "https://example.com/v1/chat/completions",
                "model": "test-model",
                "api_key": "super-secret-key",
                "zcode_dir": str(zcode),
            })
            settings = t.public_settings()
            assert settings["model_configured"] is True
            assert settings["api_key_set"] is True
            assert "super-secret-key" not in json.dumps(settings, ensure_ascii=False)
            assert t.CONFIG_PATH.exists()
            print("PASS 1: config persists and API key is not exposed")

            # 3) 环境变量覆盖配置文件
            os.environ["ZCODE_TITLE_MODEL"] = "env-model"
            t.load_config()
            assert t.MODEL == "env-model", t.MODEL
            os.environ.pop("ZCODE_TITLE_MODEL", None)
            t.load_config()
            assert t.MODEL == "test-model", t.MODEL
            print("PASS 2: environment overrides config")

            # 4) 清除已保存的 key
            t.save_config({}, clear_api_key=True)
            assert t.public_settings()["api_key_set"] is False
            print("PASS 3: clear_api_key removes the stored key")

            # 5) schema 正常
            tasks_db = zcode / "v2" / "tasks-index.sqlite"
            sessions_db = zcode / "cli" / "db" / "db.sqlite"
            con = sqlite3.connect(str(tasks_db))
            con.executescript(TASKS_SCHEMA)
            con.close()
            con = sqlite3.connect(str(sessions_db))
            con.executescript(SESSIONS_SCHEMA)
            con.close()
            assert t.check_schema(tasks_db, sessions_db)["ok"], t.check_schema(tasks_db, sessions_db)
            print("PASS 4: compatible schema is accepted")

            # 6) 缺字段时拒绝
            bad = root / "bad-tasks.sqlite"
            con = sqlite3.connect(str(bad))
            con.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT)")
            con.close()
            result = t.check_schema(bad, sessions_db)
            assert not result["ok"] and "缺少字段" in result["error"], result
            print("PASS 5: missing columns are rejected")

    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    print("ALL CONFIG TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
