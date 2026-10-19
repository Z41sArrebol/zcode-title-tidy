#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可重复运行的安全边界测试。

不读取真实会话库，全部使用临时合成数据。
运行：python tests/test_safety.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tidy_titles as t  # noqa: E402

NOW = 1792300000000

TASKS_SCHEMA = """
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  workspace_key TEXT,
  workspace_path TEXT,
  title TEXT,
  title_overridden INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0,
  created_at INTEGER,
  updated_at INTEGER,
  task_status TEXT,
  deleted INTEGER DEFAULT 0,
  meta_json TEXT
);
"""

SESSION_SCHEMA = """
CREATE TABLE session (
  id TEXT PRIMARY KEY,
  title TEXT,
  title_source TEXT,
  time_created INTEGER,
  time_updated INTEGER,
  task_type TEXT,
  parent_id TEXT,
  directory TEXT,
  time_title_updated INTEGER
);
CREATE TABLE message (id TEXT, session_id TEXT, sequence INTEGER, data TEXT);
CREATE TABLE part (message_id TEXT, session_id TEXT, sequence INTEGER, data TEXT);
"""


def create_db(path: Path, schema: str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.executescript(schema)
    return con


def seed_session(tasks: sqlite3.Connection, sessions: sqlite3.Connection,
                 sid: str, title: str, keep: str = "old") -> None:
    meta = {"title": title, "titleOverridden": False, "keep": keep}
    tasks.execute(
        "INSERT INTO tasks(task_id,workspace_key,workspace_path,title,title_overridden,"
        "archived,created_at,updated_at,task_status,deleted,meta_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "test", "C:\\test", title, 0, 0, NOW, NOW, "completed", 0,
         json.dumps(meta, ensure_ascii=False)),
    )
    sessions.execute(
        "INSERT INTO session(id,title,title_source,time_created,time_updated,"
        "task_type,parent_id,directory,time_title_updated) VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, title, "first_input", NOW, NOW, "interactive", None, "C:\\test", NOW),
    )
    sessions.execute(
        "INSERT INTO message(id,session_id,sequence,data) VALUES(?,?,?,?)",
        (f"m_{sid}", sid, 0, json.dumps({
            "role": "user",
            "semantics": {"kind": "user_prompt", "uiVisibility": "visible", "origin": "real_user"},
            "metadata": {},
        }, ensure_ascii=False)),
    )
    sessions.execute(
        "INSERT INTO part(message_id,session_id,sequence,data) VALUES(?,?,?,?)",
        (f"m_{sid}", sid, 0, json.dumps({
            "type": "text",
            "text": f"测试会话 {title}",
        }, ensure_ascii=False)),
    )


def set_titles(tasks: sqlite3.Connection, sessions: sqlite3.Connection,
               sid: str, title: str) -> None:
    sessions.execute("UPDATE session SET title=? WHERE id=?", (title, sid))
    tasks.execute("UPDATE tasks SET title=? WHERE task_id=?", (title, sid))
    sessions.commit()
    tasks.commit()


def make_plan(path: Path, items: list[dict]) -> Path:
    path.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def base_item(sid: str, old: str, new: str) -> dict:
    return {
        "id": sid,
        "workspace": "test",
        "workspace_path": "C:\\test",
        "old_title": old,
        "old_source": "first_input",
        "old_overridden": False,
        "new_title": new,
        "gen_status": "ok",
        "gen_error": "",
        "updated_at": NOW,
        "selected": True,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="title-tidy-test-") as td:
        root = Path(td)
        tasks_db = root / "tasks-index.sqlite"
        sessions_db = root / "db.sqlite"
        backups = root / "backups"
        plans = root / "plans"
        backups.mkdir()
        plans.mkdir()
        plan_path = make_plan(root / "plan.json", [
            base_item("s1", "A1", "N1"),
            base_item("s2", "A2", "N2"),
        ])

        tasks = create_db(tasks_db, TASKS_SCHEMA)
        sessions = create_db(sessions_db, SESSION_SCHEMA)
        seed_session(tasks, sessions, "s1", "A1")
        seed_session(tasks, sessions, "s2", "A2")
        tasks.commit()
        sessions.commit()

        # 指向临时库，不碰真实数据
        t.TASKS_DB = tasks_db
        t.SESSIONS_DB = sessions_db
        t.BACKUPS_DIR = backups
        t.PLANS_DIR = plans
        t.LATEST_PLAN = plans / "latest.json"
        t.STATE["progress"]["running"] = False

        # ---- 1. 应用 + 撤销不影响之后新增的聊天 ----
        res = t.apply_plan(plan_path=plan_path, force_zcode=True)
        assert res.get("ok") and res.get("applied") == 2, res
        backup1 = Path(res["backup"])

        seed_session(tasks, sessions, "s3", "A3")
        tasks.commit()
        sessions.commit()

        rev = t.revert_backup(backup1, force_zcode=True)
        assert rev.get("ok") and len(rev.get("restored") or []) == 2, rev
        assert sessions.execute("SELECT title FROM session WHERE id='s1'").fetchone()[0] == "A1"
        assert sessions.execute("SELECT title FROM session WHERE id='s2'").fetchone()[0] == "A2"
        assert sessions.execute("SELECT title FROM session WHERE id='s3'").fetchone()[0] == "A3"
        print("PASS 1: revert does not touch a later chat")

        # ---- 2. 撤销不影响写回后修改的其他 meta_json 键 ----
        res = t.apply_plan(plan_path=plan_path, force_zcode=True)
        assert res.get("ok") and res.get("applied") == 2, res
        backup2 = Path(res["backup"])

        raw = tasks.execute("SELECT meta_json FROM tasks WHERE task_id='s1'").fetchone()[0]
        meta = json.loads(raw or "{}")
        meta["other"] = "keep-me"
        tasks.execute("UPDATE tasks SET meta_json=? WHERE task_id='s1'",
                      (json.dumps(meta, ensure_ascii=False),))
        tasks.commit()

        rev = t.revert_backup(backup2, force_zcode=True)
        assert rev.get("ok") and len(rev.get("restored") or []) == 2, rev
        meta = json.loads(tasks.execute(
            "SELECT meta_json FROM tasks WHERE task_id='s1'").fetchone()[0])
        assert meta.get("title") == "A1", meta
        assert meta.get("titleOverridden") is False, meta
        assert meta.get("keep") == "old", meta
        assert meta.get("other") == "keep-me", meta
        assert tasks.execute("SELECT title FROM tasks WHERE task_id='s1'").fetchone()[0] == "A1"
        print("PASS 2: revert preserves unrelated meta_json keys")

        # ---- 3. 旧方案跨重启：当前标题变了，必须识别为过期并跳过 ----
        set_titles(tasks, sessions, "s1", "B1")
        # 3a. 直接指定方案文件
        res = t.apply_plan(plan_path=plan_path, force_zcode=True)
        assert res.get("applied") == 1, res
        assert any("方案已过期" in (s.get("reason") or "") for s in res.get("skipped") or []), res
        assert sessions.execute("SELECT title FROM session WHERE id='s1'").fetchone()[0] == "B1"
        # 3b. 网页重启后加载旧方案的路径
        items = t.scan_sessions(tasks_db, sessions_db)
        t.merge_plan(items, plan_path)
        res = t.apply_plan(ids=["s1"], force_zcode=True)
        assert res.get("applied") == 0, res
        assert any("方案已过期" in (s.get("reason") or "") for s in res.get("skipped") or []), res
        assert sessions.execute("SELECT title FROM session WHERE id='s1'").fetchone()[0] == "B1"
        print("PASS 3: stale plan is detected and skipped on both load paths")

        # ---- 4. 两个库任一写入失败：事务回滚，不留半写状态 ----
        set_titles(tasks, sessions, "s1", "A1")
        set_titles(tasks, sessions, "s2", "A2")
        orig_rw = t.rw_connect

        class FailConn:
            def __init__(self, con):
                self._con = con

            def execute(self, sql, *args):
                if "UPDATE idx.tasks" in sql:
                    raise sqlite3.OperationalError("simulated tasks write failure")
                return self._con.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._con, name)

        def fake_rw(path):
            return FailConn(orig_rw(path))

        t.rw_connect = fake_rw
        failed = False
        try:
            t.apply_plan(plan_path=plan_path, force_zcode=True)
        except Exception:
            failed = True
        finally:
            t.rw_connect = orig_rw

        assert failed, "expected simulated write failure"
        assert sessions.execute("SELECT title FROM session WHERE id='s1'").fetchone()[0] == "A1"
        assert tasks.execute("SELECT title FROM tasks WHERE task_id='s1'").fetchone()[0] == "A1"
        print("PASS 4: failed second-db write rolls back the whole transaction")

        # ---- 5. 部分跳过：一条写入，一条因标题变化跳过 ----
        set_titles(tasks, sessions, "s1", "B1")
        res = t.apply_plan(plan_path=plan_path, force_zcode=True)
        assert res.get("ok") and res.get("applied") == 1, res
        assert len(res.get("skipped") or []) == 1, res
        assert sessions.execute("SELECT title FROM session WHERE id='s2'").fetchone()[0] == "N2"
        assert sessions.execute("SELECT title FROM session WHERE id='s1'").fetchone()[0] == "B1"
        print("PASS 5: partial skip reports one applied and one skipped")

        # ---- 6. 旧格式备份（只有 applied 数量）也能字段级撤销 ----
        set_titles(tasks, sessions, "s1", "A1")
        set_titles(tasks, sessions, "s2", "A2")
        res = t.apply_plan(plan_path=plan_path, force_zcode=True)
        assert res.get("ok") and res.get("applied") == 2, res
        backup6 = Path(res["backup"])
        result_file = backup6 / "apply-result.json"
        data = json.loads(result_file.read_text(encoding="utf-8"))
        data.pop("applied_items", None)
        data["applied"] = res["applied"]
        result_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        rev = t.revert_backup(backup6, force_zcode=True)
        assert rev.get("ok") and len(rev.get("restored") or []) == 2, rev
        assert sessions.execute("SELECT title FROM session WHERE id='s1'").fetchone()[0] == "A1"
        assert sessions.execute("SELECT title FROM session WHERE id='s2'").fetchone()[0] == "A2"
        print("PASS 6: legacy apply backup can still be reverted")

        tasks.close()
        sessions.close()

    print("ALL SAFETY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
