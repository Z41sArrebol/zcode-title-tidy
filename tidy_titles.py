#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZCode 会话标题整理器

扫描 ZCode 的两个 SQLite 库，按会话真实内容重新生成统一风格的中文标题，
在本地网页里预览/修改后，备份并写回。

用法：
    python tidy_titles.py serve            # 启动本地网页
    python tidy_titles.py scan             # 只扫描并打印
    python tidy_titles.py generate         # 命令行生成全部候选
    python tidy_titles.py apply --latest   # 应用最近一次方案（需先退出 ZCode）
    python tidy_titles.py revert <备份目录> # 只撤销某次写回的标题
    python tidy_titles.py restore <备份目录> # 整库恢复（灾难恢复）
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


APP_DIR = _bundle_dir()
HOME = Path.home()
VERSION = "0.2.0"
DEFAULT_ZCODE_DIR = HOME / ".zcode"
DEFAULT_DATA_DIR = HOME / ".zcode-title-tidy"
DATA_DIR = Path(
    os.environ.get("ZCODE_TITLE_DATA_DIR") or DEFAULT_DATA_DIR
).expanduser()
PLANS_DIR = DATA_DIR / "plans"
BACKUPS_DIR = DATA_DIR / "backups"
CONFIG_PATH = DATA_DIR / "config.json"
WEB_DIR = APP_DIR / "web"
LOG_PATH = DATA_DIR / "tool.log"
LATEST_PLAN = PLANS_DIR / "latest.json"

DEFAULT_API_URL = ""
DEFAULT_MODEL = ""
DEFAULT_PORT = int(os.environ.get("ZCODE_TITLE_PORT", "47821"))

CONFIG_DEFAULTS = {
    "api_url": DEFAULT_API_URL,
    "model": DEFAULT_MODEL,
    "api_key": "",
    "zcode_dir": str(DEFAULT_ZCODE_DIR),
}

FILE_CONFIG = dict(CONFIG_DEFAULTS)
CONFIG = dict(CONFIG_DEFAULTS)

API_URL = DEFAULT_API_URL
MODEL = DEFAULT_MODEL
API_KEY = ""
ZCODE_DIR = DEFAULT_ZCODE_DIR
TASKS_DB = ZCODE_DIR / "v2" / "tasks-index.sqlite"
SESSIONS_DB = ZCODE_DIR / "cli" / "db" / "db.sqlite"

log = logging.getLogger("title-tidy")

CSRF_TOKEN = secrets.token_urlsafe(24)
APPLY_LOCK = threading.Lock()
GEN_LOCK = threading.Lock()


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)


def migrate_legacy_data() -> None:
    """把旧版放在程序目录里的 plans/backups/tool.log 迁到用户数据目录。"""
    if APP_DIR.resolve() == DATA_DIR.resolve():
        return
    ensure_data_dirs()

    old_plans = APP_DIR / "plans"
    if old_plans.is_dir():
        for child in old_plans.iterdir():
            dest = PLANS_DIR / child.name
            if not dest.exists():
                try:
                    shutil.move(str(child), str(dest))
                except Exception as exc:
                    log.warning("迁移旧方案失败 %s：%s", child, exc)

    old_backups = APP_DIR / "backups"
    if old_backups.is_dir():
        for child in old_backups.iterdir():
            dest = BACKUPS_DIR / child.name
            if not dest.exists():
                try:
                    shutil.move(str(child), str(dest))
                except Exception as exc:
                    log.warning("迁移旧备份失败 %s：%s", child, exc)

    old_log = APP_DIR / "tool.log"
    if old_log.exists() and not LOG_PATH.exists():
        try:
            shutil.move(str(old_log), str(LOG_PATH))
        except Exception as exc:
            log.warning("迁移旧日志失败：%s", exc)


def env_override_flags() -> dict:
    return {
        "api_url": "ZCODE_TITLE_API" in os.environ,
        "model": "ZCODE_TITLE_MODEL" in os.environ,
        "api_key": "ZCODE_TITLE_API_KEY" in os.environ,
        "zcode_dir": "ZCODE_TITLE_ZCODE_DIR" in os.environ,
    }


def load_config() -> dict:
    global FILE_CONFIG, CONFIG, API_URL, MODEL, API_KEY, ZCODE_DIR, TASKS_DB, SESSIONS_DB
    ensure_data_dirs()

    cfg = dict(CONFIG_DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in CONFIG_DEFAULTS:
                    if key in raw:
                        cfg[key] = raw[key] if raw[key] is not None else ""
        except Exception as exc:
            log.warning("读取配置失败：%s", exc)
    else:
        # 旧版用户：保留原来的本地 Magpie 默认值，避免升级后突然不可用
        legacy = LATEST_PLAN.exists() or (APP_DIR / "plans" / "latest.json").exists()
        if legacy:
            cfg["api_url"] = "http://127.0.0.1:3425/v1/chat/completions"
            cfg["model"] = "group/auto-deepseek-v4-1-flash"
            try:
                CONFIG_PATH.write_text(
                    json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                try:
                    os.chmod(CONFIG_PATH, 0o600)
                except Exception:
                    pass
            except Exception as exc:
                log.warning("写入初始配置失败：%s", exc)

    FILE_CONFIG = dict(cfg)

    # 环境变量优先级最高，方便 CI / 高级用户；不会把 key 写进配置文件
    if os.environ.get("ZCODE_TITLE_API") is not None:
        cfg["api_url"] = os.environ["ZCODE_TITLE_API"].strip()
    if os.environ.get("ZCODE_TITLE_MODEL") is not None:
        cfg["model"] = os.environ["ZCODE_TITLE_MODEL"].strip()
    if os.environ.get("ZCODE_TITLE_API_KEY") is not None:
        cfg["api_key"] = os.environ["ZCODE_TITLE_API_KEY"]
    if os.environ.get("ZCODE_TITLE_ZCODE_DIR") is not None:
        cfg["zcode_dir"] = os.environ["ZCODE_TITLE_ZCODE_DIR"].strip()

    CONFIG = dict(cfg)
    API_URL = str(cfg.get("api_url") or "").strip()
    MODEL = str(cfg.get("model") or "").strip()
    API_KEY = str(cfg.get("api_key") or "")
    ZCODE_DIR = Path(str(cfg.get("zcode_dir") or DEFAULT_ZCODE_DIR)).expanduser()
    TASKS_DB = ZCODE_DIR / "v2" / "tasks-index.sqlite"
    SESSIONS_DB = ZCODE_DIR / "cli" / "db" / "db.sqlite"
    return dict(CONFIG)


def save_config(updates: dict, clear_api_key: bool = False) -> dict:
    global FILE_CONFIG
    ensure_data_dirs()
    cfg = dict(FILE_CONFIG)

    if "api_url" in updates:
        url = str(updates.get("api_url") or "").strip()
        if url:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("API 地址必须是 http/https URL")
        cfg["api_url"] = url

    if "model" in updates:
        cfg["model"] = str(updates.get("model") or "").strip()

    if "zcode_dir" in updates:
        zdir = str(updates.get("zcode_dir") or "").strip()
        if not zdir:
            raise ValueError("ZCode 数据目录不能为空")
        cfg["zcode_dir"] = zdir

    if clear_api_key:
        cfg["api_key"] = ""
    elif str(updates.get("api_key") or "").strip():
        cfg["api_key"] = str(updates["api_key"]).strip()

    FILE_CONFIG = dict(cfg)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass
    return load_config()


def public_settings() -> dict:
    state = globals().get("STATE") or {}
    compat = state.get("compatibility") or {}
    return {
        "api_url": API_URL,
        "model": MODEL,
        "api_key_set": bool(API_KEY),
        "zcode_dir": str(ZCODE_DIR),
        "tasks_db": str(TASKS_DB),
        "sessions_db": str(SESSIONS_DB),
        "data_dir": str(DATA_DIR),
        "config_path": str(CONFIG_PATH),
        "model_configured": bool(API_URL and MODEL),
        "env_overrides": env_override_flags(),
        "compatible": bool(compat.get("ok", True)),
        "compatibility_error": compat.get("error", ""),
    }


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def setup_logging(console: bool = True) -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    if sys.platform == "win32" and console:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_time(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return dt.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def month_day(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        d = dt.datetime.fromtimestamp(ms / 1000)
        return f"{d.month}月{d.day}日"
    except Exception:
        return ""


def ro_connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def rw_connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def query_in(con: sqlite3.Connection, sql_template: str, ids: list[str], chunk: int = 400):
    rows: list[Any] = []
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        if not part:
            continue
        sql = sql_template.format(ph=",".join("?" for _ in part))
        rows.extend(con.execute(sql, part).fetchall())
    return rows


def workspace_label(path: str) -> str:
    p = (path or "").rstrip("\\/")
    base = os.path.basename(p)
    return base or p or "未知工作区"


def clean_source_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\x00", " ")
    text = re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", "[图片]", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

GENERIC_TITLES = {
    "你好", "你好？", "hi", "hello", "嗨", "在吗", "1", "11",
    "untitled session", "new session", "新会话",
}

GREETING_TEXTS = {
    "", "你好", "您好", "你好你好", "hi", "hello", "嗨", "哈喽", "在吗",
    "在么", "1", "11", "111", "1111", "?", "？", "额", "嗯",
}


def is_greeting(text: str) -> bool:
    t = re.sub(r"[\s\W_]+", "", (text or "").lower())
    return t in GREETING_TEXTS


def is_real_user_message(obj: dict) -> bool:
    if obj.get("role") != "user":
        return False
    if obj.get("synthetic") is True:
        return False
    meta = obj.get("metadata") or {}
    if meta.get("visibility") == "model-only":
        return False
    sem = obj.get("semantics") or {}
    if sem:
        if sem.get("kind") and sem.get("kind") != "user_prompt":
            return False
        if sem.get("uiVisibility") and sem.get("uiVisibility") != "visible":
            return False
        if sem.get("origin") and sem.get("origin") not in ("real_user",):
            return False
    return True


def parts_text(parts: list[tuple[int, str]]) -> str:
    out: list[str] = []
    for _seq, raw in sorted(parts):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            t = obj["text"].strip()
            if t:
                out.append(t)
    return "\n".join(out)


GREETING_TITLE_MARKERS = ("问候", "寒暄", "打招呼", "初次", "闲聊", "见面")


def _old_title_note(old_title: str) -> str:
    t = (old_title or "").strip().lower()
    if not t or t in GENERIC_TITLES:
        return "（旧标题无意义，已忽略）"
    return old_title


def build_digest(workspace: str, old_title: str, prompts: list[str], assistant: str) -> str:
    total = len(prompts)
    substantive = list(prompts)
    skipped = 0
    while substantive and is_greeting(substantive[0]):
        substantive.pop(0)
        skipped += 1
    if not substantive:
        substantive = list(prompts)
        skipped = 0

    lines = [
        f"工作区：{workspace}",
        f"旧标题（通常概括最初意图，可参考但不要照抄）：{_old_title_note(old_title)}",
        f"用户消息共 {total} 条"
        + (f"（开头 {skipped} 条是纯问候，已跳过）" if skipped else "")
        + "。下面按时间抽取，省略了中间部分。",
    ]
    if not substantive:
        lines.append("[没有可用的用户消息]")
        return "\n".join(lines)

    n = len(substantive)
    if n <= 6:
        picks = [(i + 1, t) for i, t in enumerate(substantive)]
    else:
        picks = [
            (1, substantive[0]),
            (2, substantive[1]),
            (3, substantive[2]),
            (n - 1, substantive[-2]),
            (n, substantive[-1]),
        ]

    for idx, text in picks:
        limit = 760 if idx == 1 else 460
        t = clean_source_text(text)[:limit]
        tag = "首条实质消息" if idx == 1 else ("最后" if idx == n else "")
        suffix = f" · {tag}" if tag else ""
        lines.append(f"[用户 {idx}{suffix}] {t}")
        if len("\n".join(lines)) > 3300:
            break

    if assistant:
        a = clean_source_text(assistant)
        if not (substantive and a.lstrip().startswith(("你好", "您好"))):
            lines.append("[助手首条回复] " + a[:380])
    return "\n".join(lines)[:3800]


class SchemaError(RuntimeError):
    pass


def check_schema(tasks_db: Path | None = None, sessions_db: Path | None = None) -> dict:
    tasks_db = tasks_db or TASKS_DB
    sessions_db = sessions_db or SESSIONS_DB
    requirements = {
        Path(tasks_db): {
            "tasks": {
                "task_id", "workspace_key", "workspace_path", "title",
                "title_overridden", "archived", "created_at", "updated_at",
                "task_status", "deleted", "meta_json",
            },
        },
        Path(sessions_db): {
            "session": {
                "id", "title", "title_source", "time_created", "time_updated",
                "task_type", "parent_id", "directory", "time_title_updated",
            },
            "message": {"id", "session_id", "sequence", "data"},
            "part": {"message_id", "session_id", "sequence", "data"},
        },
    }
    for path, tables in requirements.items():
        if not path.exists():
            return {"ok": False, "error": f"找不到数据库：{path}"}
        con = ro_connect(path)
        try:
            for table, required in tables.items():
                rows = con.execute(f"PRAGMA table_info('{table}')").fetchall()
                got = {row[1] for row in rows}
                if not got:
                    return {"ok": False, "error": f"{path.name} 缺少表 {table}"}
                missing = required - got
                if missing:
                    return {
                        "ok": False,
                        "error": f"{path.name} 的 {table} 缺少字段：{', '.join(sorted(missing))}",
                    }
        finally:
            con.close()
    return {"ok": True, "error": ""}


def scan_sessions(tasks_db: Path = TASKS_DB, sessions_db: Path = SESSIONS_DB) -> list[dict]:
    if not tasks_db.exists():
        raise FileNotFoundError(f"找不到 tasks-index 数据库：{tasks_db}")
    if not sessions_db.exists():
        raise FileNotFoundError(f"找不到 session 数据库：{sessions_db}")
    schema = check_schema(tasks_db, sessions_db)
    if not schema["ok"]:
        raise SchemaError(schema["error"])

    tcon = ro_connect(tasks_db)
    scon = ro_connect(sessions_db)
    try:
        task_rows = tcon.execute(
            """
            SELECT task_id, workspace_key, workspace_path, title, title_overridden,
                   archived, created_at, updated_at, task_status
            FROM tasks
            WHERE deleted = 0
            """
        ).fetchall()
        if not task_rows:
            return []

        ids = [r[0] for r in task_rows]
        sess_rows = query_in(
            scon,
            "SELECT id, title, title_source, time_created, time_updated, task_type, parent_id, directory "
            "FROM session WHERE id IN ({ph})",
            ids,
        )
        sess = {r[0]: r for r in sess_rows}

        msg_rows = query_in(
            scon,
            "SELECT id, session_id, sequence, data FROM message WHERE session_id IN ({ph})",
            ids,
        )
        part_rows = query_in(
            scon,
            "SELECT message_id, sequence, data FROM part WHERE session_id IN ({ph})",
            ids,
        )
    finally:
        tcon.close()
        scon.close()

    parts_by_msg: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    for mid, seq, raw in part_rows:
        parts_by_msg[mid].append((seq, raw))

    msgs_by_session: dict[str, list[tuple[int, str, str]]] = collections.defaultdict(list)
    for mid, sid, seq, raw in msg_rows:
        msgs_by_session[sid].append((seq, mid, raw))

    items: list[dict] = []
    for (task_id, wkey, wpath, title, overridden, archived,
         created_at, updated_at, task_status) in task_rows:
        srow = sess.get(task_id)
        title_source = (srow[2] if srow and srow[2] else ("custom" if overridden else "first_input"))
        prompts: list[str] = []
        assistant_text = ""

        for seq, mid, raw in sorted(msgs_by_session.get(task_id, []), key=lambda x: x[0]):
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if is_real_user_message(obj):
                text = parts_text(parts_by_msg.get(mid, []))
                if text.strip():
                    prompts.append(text)
            elif (
                obj.get("role") == "assistant"
                and obj.get("synthetic") is not True
                and not assistant_text
            ):
                sem = obj.get("semantics") or {}
                if sem.get("uiVisibility") == "hidden":
                    continue
                text = parts_text(parts_by_msg.get(mid, []))
                if text.strip():
                    assistant_text = text

        workspace = workspace_label(wpath)
        digest = build_digest(workspace, title, prompts, assistant_text)
        first_prompt = next((p for p in prompts if not is_greeting(p)), prompts[0] if prompts else "")
        has_substantive = any(not is_greeting(p) for p in prompts)
        item = {
            "id": task_id,
            "workspace": workspace,
            "workspace_path": wpath,
            "workspace_key": wkey,
            "old_title": title or "",
            "old_source": title_source,
            "old_overridden": bool(overridden),
            "archived": bool(archived),
            "task_status": task_status or "",
            "created_at": created_at,
            "updated_at": updated_at,
            "title_len": len(title or ""),
            "prompts_count": len(prompts),
            "first_prompt": first_prompt,
            "has_substantive": has_substantive,
            "digest": digest,
            "flags": [],
            "new_title": "",
            "gen_status": "pending",
            "gen_error": "",
            "selected": True,
        }
        items.append(item)

    # 旧标题问题标记
    dup_counter = collections.Counter((it["workspace"], it["old_title"]) for it in items)
    for it in items:
        flags: list[str] = []
        t = it["old_title"]
        tl = t.strip().lower()
        if it["title_len"] >= 30:
            flags.append("长标题")
        if it["title_len"] >= 59:
            flags.append("疑似截断")
        if it["old_source"] == "first_input":
            flags.append("自动生成失败")
        if tl in GENERIC_TITLES:
            flags.append("空标题")
        if dup_counter[(it["workspace"], t)] > 1:
            flags.append("重复")
        if re.search(r"[\r\n]|^\[|```|`", t):
            flags.append("含格式")
        if not it["prompts_count"]:
            flags.append("无用户消息")
        it["flags"] = flags

    ws_counter = collections.Counter(it["workspace"] for it in items)
    ws_order = {ws: i for i, (ws, _n) in enumerate(ws_counter.most_common())}
    items.sort(key=lambda x: (ws_order.get(x["workspace"], 999), -(x["updated_at"] or 0)))
    return items


# ---------------------------------------------------------------------------
# 模型起名
# ---------------------------------------------------------------------------

TITLE_SYSTEM_PROMPT = """你是 ZCode 会话标题整理器。根据会话材料，生成一个中文短标题。

判断主线的方法：
- 先看第一条实质消息，它通常定义了这次会话最初要做什么。
- 后面的消息只用来补充细节、区分轮次，或者判断是否换题。
- 如果第一条只是“你好”“在吗”这类问候，再看后面的消息确定真实主题。
- 不要因为会话末尾临时插入的排错、提问，就丢掉最初的主线。
- 如果会话确实跨了两个同等重要的主题，可以用“A 与 B”的形式，但总长仍要控制在 16 个汉字左右。

硬性要求：
1. 只输出一个 JSON 对象：{"title":"..."}，不要解释、不要 Markdown、不要代码块。
2. 标题 6–16 个汉字；英文专有名词按一个词计。总长度不超过 30 个字符。
3. 用名词短语或动宾短语，直接概括“在做什么 / 在查什么 / 在修什么”，不要用“会话、任务、请求、关于、相关、帮助、问题”等空词。
4. 保留项目名、仓库名、文件名、技术名（例如 ZCode、Magpie、quiz-trainer、weapp-tailwindcss、南邮、盲测、常州公益）。
5. 不要标点、引号、书名号、编号、emoji、句号；不要以“的”结尾。
6. 旧标题通常概括了最初意图，可以参考它的含义，但不要照抄；表达要更短、更整齐。
7. 同一工作区里尽量不与已有标题重复；主题相近时加上能区分的细节（如“第一轮 / 第二轮”“修复 / 验证”）。
8. 标题要像人给文件夹起的名字，不要像一句完整的话。

只输出 JSON。"""


def _model_headers(api_key: str | None = None) -> dict:
    key = API_KEY if api_key is None else api_key
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def call_model(digest: str, hint: str = "", model: str | None = None,
               timeout: int = 90) -> str:
    model = model or MODEL
    if not API_URL or not model:
        raise RuntimeError("模型服务未配置，请先在设置里填写 API 地址和模型")
    user = digest
    if hint:
        user += "\n\n额外要求：" + hint
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": TITLE_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "max_tokens": 700,
        "temperature": 0.2,
        "stream": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers=_model_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"模型请求失败 HTTP {exc.code}: {body}") from exc
    choices = obj.get("choices") or []
    if not choices:
        raise RuntimeError(f"模型无返回：{str(obj)[:300]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return content or ""


def test_model(api_url: str | None = None, model: str | None = None,
               api_key: str | None = None, timeout: int = 20) -> dict:
    url = (api_url or API_URL).strip()
    mdl = (model or MODEL).strip()
    key = API_KEY if api_key is None else api_key.strip()
    if not url or not mdl:
        return {"ok": False, "message": "请先填写 API 地址和模型"}
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"ok": False, "message": "API 地址必须是 http/https URL"}

    payload = {
        "model": mdl,
        "messages": [{"role": "user", "content": "回复 OK"}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_model_headers(key), method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        return {"ok": False, "message": f"HTTP {exc.code}: {body}"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    latency = int((time.time() - started) * 1000)
    try:
        obj = json.loads(raw)
    except Exception:
        return {"ok": False, "message": f"返回不是 JSON：{raw[:200]}"}
    if not obj.get("choices"):
        return {"ok": False, "message": f"返回里没有 choices：{str(obj)[:240]}"}
    return {
        "ok": True,
        "message": "连接成功",
        "latency_ms": latency,
        "model": mdl,
        "status": status,
    }


def clean_title(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    s = re.sub(r"^[-*#>\s]+", "", s)
    s = re.sub(r"^(?:标题|Title)\s*[:：]\s*", "", s, flags=re.IGNORECASE)
    s = s.strip(" \t\r\n\"'“”‘’`《》「」【】[]()（）")
    s = re.sub(r"[。！？!?；;：:，,、]+$", "", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^关于", "", s)
    return s.strip()


def parse_title(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                for key in ("title", "标题", "name"):
                    v = obj.get(key)
                    if isinstance(v, str) and v.strip():
                        return clean_title(v)
        except Exception:
            pass
    for line in s.splitlines():
        line = line.strip()
        if line:
            return clean_title(line)
    return ""


def title_ok(s: str) -> tuple[bool, str]:
    if not s:
        return False, "空标题"
    if len(s) > 32:
        return False, "太长"
    if "\n" in s or "\r" in s:
        return False, "多行"
    if "`" in s or s.startswith("["):
        return False, "格式"
    if len(s) < 3:
        return False, "太短"
    return True, ""


def fallback_title(item: dict) -> str:
    t = item.get("first_prompt") or item.get("old_title") or ""
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    prefixes = [
        "你是本次项目实验的选手", "你是本次", "请读取", "请先", "请你", "请",
        "帮我", "我需要", "现在", "首先", "先", "看一下", "看看", "检查",
        "排查", "修复", "开发", "写一个", "写个",
    ]
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            if t.startswith(p):
                t = t[len(p):].lstrip("，,。:：、 ")
                changed = True
    t = re.split(r"[。！？!?\n]", t)[0].strip(" ，,、:：")
    if len(t) > 18:
        t = t[:18].rstrip("的")
    return t or "未命名会话"


def generate_one(item: dict, hint: str = "", model: str | None = None) -> tuple[str, str]:
    """返回 (标题, 错误信息)。失败时返回兜底标题。"""
    model = model or MODEL
    digest = item.get("digest") or f"工作区：{item.get('workspace', '')}\n旧标题：{item.get('old_title', '')}"
    last_err = ""
    cur_hint = hint
    for attempt in range(3):
        try:
            raw = call_model(digest, hint=cur_hint, model=model)
            title = parse_title(raw)
            ok, why = title_ok(title)
            if ok and item.get("has_substantive") and any(m in title for m in GREETING_TITLE_MARKERS):
                ok, why = False, "只写了问候"
                cur_hint = (
                    (cur_hint + " " if cur_hint else "")
                    + "这个会话有实质任务，禁止使用“问候/寒暄/打招呼/初次/闲聊/见面”等词，"
                      "请直接概括后面的实质任务。"
                )
                last_err = why
                continue
            if ok:
                return title, ""
            last_err = why
            cur_hint = (cur_hint + " " if cur_hint else "") + (
                f"上一次结果不合格（{why}），请严格输出 6–16 个汉字，只返回 JSON。"
            )
        except Exception as exc:
            last_err = str(exc)
            log.warning("生成失败 %s：%s", item.get("id"), exc)
            time.sleep(1.2 * (attempt + 1))
    return fallback_title(item), last_err


STATUS_MARKERS = ("已废弃", "废弃", "已停用", "停用", "已归档", "归档", "旧版", "存档")


def normalize_blind_test_title(it: dict) -> None:
    """把同一批盲测会话统一成：盲测·选手X·第N轮动作。"""
    text = f"{it.get('old_title', '')} {it.get('new_title', '')}"
    if "盲测" not in text:
        return
    m = re.search(r"选手\s*[【\[(（]?\s*([A-Da-d])", text)
    if not m:
        m = re.search(r"盲测\s*([A-Da-d])\s*第", text)
    if not m:
        return
    r = re.search(r"第\s*([一二三123])\s*轮", text)
    if not r:
        return
    letter = m.group(1).upper()
    num = {"1": "一", "2": "二", "3": "三"}.get(r.group(1), r.group(1))
    if "修复" in text and "验证" in text:
        action = "修复验证"
    elif "开发" in text and "验证" in text:
        action = "开发验证"
    elif "修复" in text:
        action = "修复"
    elif "验证" in text:
        action = "验证"
    elif any(k in text for k in ("开发", "交付", "任务", "实现")):
        action = "开发"
    else:
        action = ""
    it["new_title"] = f"盲测·选手{letter}·第{num}轮{action}"


def preserve_status_marker(it: dict) -> None:
    """保留用户在原标题里写的“（已废弃）”这类状态。"""
    old = it.get("old_title") or ""
    new = (it.get("new_title") or "").strip()
    if not new:
        return
    for marker in STATUS_MARKERS:
        if marker not in old or marker in new:
            continue
        m = re.search(r"[（(]([^）)]*" + re.escape(marker) + r"[^）)]*)[）)]", old)
        suffix = f"（{m.group(1)}）" if m else f"（{marker}）"
        it["new_title"] = new + suffix
        return


def postprocess_titles(items: list[dict]) -> None:
    # 盲测组的命名规则只对特定实验数据有意义，默认关闭；需要时设
    # ZCODE_TITLE_NORMALIZE_BLIND=1 打开。
    if os.environ.get("ZCODE_TITLE_NORMALIZE_BLIND") == "1":
        for it in items:
            normalize_blind_test_title(it)
    for it in items:
        preserve_status_marker(it)


def resolve_duplicates(items: list[dict], model: str | None = None) -> int:
    model = model or MODEL
    groups: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for it in items:
        t = (it.get("new_title") or "").strip()
        if t:
            groups[(it.get("workspace", ""), t)].append(it)
    changed = 0
    for (_ws, title), group in groups.items():
        if len(group) < 2:
            continue
        for it in group[1:]:
            try:
                hint = f"同一工作区已有标题「{title}」。请换一个更具体、更有区分度的标题，不要重复。"
                new_title, err = generate_one(it, hint=hint, model=model)
                if new_title == title or not new_title:
                    new_title = f"{title}·{month_day(it.get('updated_at'))}"
                it["new_title"] = new_title
                it["gen_status"] = "ok"
                it["gen_error"] = err
                changed += 1
            except Exception:
                it["new_title"] = f"{title}·{month_day(it.get('updated_at'))}"
                changed += 1
    return changed


# ---------------------------------------------------------------------------
# 全局状态 + 方案
# ---------------------------------------------------------------------------

STATE_LOCK = threading.RLock()
SAVE_LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "sessions": [],
    "by_id": {},
    "loaded_at": None,
    "plan_path": str(LATEST_PLAN),
    "model": MODEL,
    "progress": {
        "running": False, "done": 0, "total": 0, "ok": 0, "failed": 0,
        "current": "", "started_at": None, "finished_at": None,
    },
    "last_apply": None,
    "compatibility": {"ok": True, "error": ""},
}


def set_sessions(items: list[dict], preserve: bool = True) -> None:
    with STATE_LOCK:
        old_by_id = STATE.get("by_id", {}) if preserve else {}
        for it in items:
            old = old_by_id.get(it["id"])
            if not old:
                continue
            for key in ("new_title", "gen_status", "gen_error", "selected", "applied_at"):
                if key in old:
                    it[key] = old[key]
            if old.get("new_title"):
                it["gen_status"] = old.get("gen_status", "ok")
        STATE["sessions"] = items
        STATE["by_id"] = {it["id"]: it for it in items}
        STATE["loaded_at"] = now_ms()


def rescan() -> None:
    try:
        items = scan_sessions(TASKS_DB, SESSIONS_DB)
    except Exception as exc:
        with STATE_LOCK:
            STATE["compatibility"] = {"ok": False, "error": str(exc)}
            STATE["sessions"] = []
            STATE["by_id"] = {}
            STATE["loaded_at"] = now_ms()
        log.warning("扫描数据库失败：%s", exc)
        return
    with STATE_LOCK:
        STATE["compatibility"] = {"ok": True, "error": ""}
    set_sessions(items, preserve=True)


def plan_items() -> list[dict]:
    with STATE_LOCK:
        out = []
        for it in STATE["sessions"]:
            out.append({
                "id": it["id"],
                "workspace": it["workspace"],
                "workspace_path": it["workspace_path"],
                "old_title": it.get("plan_old_title") or it["old_title"],
                "old_source": it["old_source"],
                "old_overridden": it["old_overridden"],
                "new_title": it.get("new_title", ""),
                "gen_status": it.get("gen_status", "pending"),
                "gen_error": it.get("gen_error", ""),
                "updated_at": it["updated_at"],
                "selected": bool(it.get("selected", True)),
                "stale": bool(it.get("stale")),
            })
        return out


def plan_payload() -> dict:
    return {
        "version": 1,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        "api": API_URL,
        "tasks_db": str(TASKS_DB),
        "sessions_db": str(SESSIONS_DB),
        "items": plan_items(),
    }


def save_plan(explicit: bool = False) -> Path:
    with SAVE_LOCK:
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        payload = plan_payload()
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = LATEST_PLAN.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(LATEST_PLAN)
        if explicit:
            path = PLANS_DIR / f"plan-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            path.write_text(text, encoding="utf-8")
            with STATE_LOCK:
                STATE["plan_path"] = str(path)
            return path
        return LATEST_PLAN


def load_plan(path: Path = LATEST_PLAN) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("读取方案失败：%s", exc)
        return []
    return payload.get("items") or []


def merge_plan(items: list[dict], path: Path = LATEST_PLAN) -> None:
    plan = {p["id"]: p for p in load_plan(path) if p.get("id")}
    for it in items:
        p = plan.get(it["id"])
        if not p:
            continue
        plan_old = (p.get("old_title") or "").strip()
        if plan_old:
            it["plan_old_title"] = plan_old
            it["stale"] = bool(p.get("stale")) or plan_old != it["old_title"]
        else:
            it["plan_old_title"] = it["old_title"]
            it["stale"] = bool(p.get("stale"))

        new_title = (p.get("new_title") or "").strip()
        if new_title:
            it["new_title"] = new_title
            it["gen_status"] = p.get("gen_status") or "ok"
            it["gen_error"] = p.get("gen_error") or ""
        if "selected" in p:
            it["selected"] = bool(p["selected"])
    set_sessions(items, preserve=False)


def generate_items(ids: list[str] | None = None, force: bool = False,
                   workers: int = 4, model: str | None = None) -> dict:
    if not GEN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有生成任务在运行，请稍候。"}
    try:
        return _generate_items_locked(ids=ids, force=force, workers=workers, model=model)
    finally:
        GEN_LOCK.release()


def _generate_items_locked(ids: list[str] | None = None, force: bool = False,
                           workers: int = 4, model: str | None = None) -> dict:
    model = model or MODEL
    if not API_URL or not model:
        return {
            "ok": False,
            "error": "model_not_configured",
            "message": "模型服务未配置，请先在设置里填写 API 地址和模型。",
        }
    if not STATE.get("compatibility", {}).get("ok", True):
        return {
            "ok": False,
            "error": "incompatible",
            "message": "数据库结构不兼容，已禁止生成。",
        }
    with STATE_LOCK:
        if STATE["progress"]["running"]:
            return {"ok": False, "error": "已有生成任务在运行"}
        sessions = list(STATE["sessions"])
        if ids is None:
            targets = [it for it in sessions if force or not it.get("new_title")]
        else:
            id_set = set(ids)
            targets = [it for it in sessions if it["id"] in id_set and (force or not it.get("new_title"))]
        if not targets:
            return {"ok": True, "generated": 0, "message": "没有需要生成的条目"}
        STATE["progress"] = {
            "running": True, "done": 0, "total": len(targets), "ok": 0, "failed": 0,
            "current": "", "started_at": now_ms(), "finished_at": None,
        }

    started = time.time()
    dup = 0

    def work(it: dict):
        title, err = generate_one(it, model=model)
        return it, title, err

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futures = {ex.submit(work, it): it for it in targets}
            for fut in concurrent.futures.as_completed(futures):
                it = futures[fut]
                try:
                    _it, title, err = fut.result()
                except Exception as exc:
                    it, title, err = it, fallback_title(it), str(exc)
                with STATE_LOCK:
                    it["new_title"] = title
                    it["gen_status"] = "ok" if not err else "fallback"
                    it["gen_error"] = err
                    it["plan_old_title"] = it["old_title"]
                    it["stale"] = False
                    p = STATE["progress"]
                    p["done"] += 1
                    if err:
                        p["failed"] += 1
                    else:
                        p["ok"] += 1
                    p["current"] = it["old_title"][:40]
                if STATE["progress"]["done"] % 5 == 0:
                    save_plan(False)
        dup = resolve_duplicates(targets, model=model)
        postprocess_titles(targets)
        # 后处理可能产生同名（例如同一选手同一轮），再消一次
        dup += resolve_duplicates(targets, model=model)
    finally:
        with STATE_LOCK:
            STATE["progress"]["running"] = False
            STATE["progress"]["finished_at"] = now_ms()
        save_plan(False)

    with STATE_LOCK:
        p = STATE["progress"]
        return {
            "ok": True,
            "generated": p["done"],
            "ok_count": p["ok"],
            "failed": p["failed"],
            "duplicates_fixed": dup,
            "seconds": round(time.time() - started, 1),
        }


# ---------------------------------------------------------------------------
# 写回
# ---------------------------------------------------------------------------

def zcode_state() -> str:
    """返回 running / stopped / unknown。unknown 必须按不可写处理。"""
    if sys.platform != "win32":
        return "unknown"
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ZCode.exe", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=flags,
        )
        if proc.returncode != 0:
            return "unknown"
        out = proc.stdout or ""
        return "running" if "zcode.exe" in out.lower() else "stopped"
    except Exception:
        return "unknown"


def zcode_running() -> bool:
    """兼容旧调用：无法确认时也视为运行（fail closed）。"""
    return zcode_state() != "stopped"


def backup_databases(backup_dir: Path, items: list[dict], plan_file: Path | None = None) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name, path in (("tasks-index.sqlite", TASKS_DB), ("db.sqlite", SESSIONS_DB)):
        src = ro_connect(path)
        dst = sqlite3.connect(str(backup_dir / name))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    original = []
    scon = ro_connect(SESSIONS_DB)
    tcon = ro_connect(TASKS_DB)
    try:
        for it in items:
            srow = scon.execute(
                "SELECT title, title_source, time_title_updated FROM session WHERE id = ?",
                (it["id"],),
            ).fetchone()
            trow = tcon.execute(
                "SELECT title, title_overridden, meta_json, updated_at FROM tasks WHERE task_id = ?",
                (it["id"],),
            ).fetchone()
            original.append({
                "id": it["id"],
                "session": {
                    "title": srow[0] if srow else None,
                    "title_source": srow[1] if srow else None,
                    "time_title_updated": srow[2] if srow else None,
                },
                "task": {
                    "title": trow[0] if trow else None,
                    "title_overridden": trow[1] if trow else None,
                    "meta_json": trow[2] if trow else None,
                    "updated_at": trow[3] if trow else None,
                },
            })
    finally:
        scon.close()
        tcon.close()

    (backup_dir / "original.json").write_text(
        json.dumps({"created_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "items": original}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    src_plan = plan_file or LATEST_PLAN
    if src_plan.exists():
        shutil.copy2(src_plan, backup_dir / "plan.json")


def apply_plan(ids: list[str] | None = None, force_zcode: bool = False,
               plan_path: Path | None = None) -> dict:
    if not APPLY_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "busy", "message": "已有写回任务在运行，请稍候。"}
    try:
        return _apply_plan_locked(ids=ids, force_zcode=force_zcode, plan_path=plan_path)
    finally:
        APPLY_LOCK.release()


def _apply_plan_locked(ids: list[str] | None = None, force_zcode: bool = False,
                       plan_path: Path | None = None) -> dict:
    state = zcode_state()
    if state != "stopped" and not force_zcode:
        return {
            "ok": False,
            "error": "zcode_running",
            "zcode_state": state,
            "message": "检测到 ZCode 正在运行，或无法确认其状态。请完全退出 ZCode（包括托盘图标）后再应用。",
        }
    if not STATE.get("compatibility", {}).get("ok", True):
        return {
            "ok": False,
            "error": "incompatible",
            "message": "数据库结构不兼容，已禁止写入。请查看设置里的兼容性提示。",
        }
    if STATE["progress"]["running"]:
        return {"ok": False, "error": "busy", "message": "正在生成标题，请等生成完成后再应用。"}

    if plan_path is None:
        save_plan(False)

    if plan_path and plan_path.exists():
        source_items = load_plan(plan_path)
        current = {it["id"]: it for it in scan_sessions(TASKS_DB, SESSIONS_DB)}
        targets = []
        id_set = set(ids) if ids is not None else None
        for p in source_items:
            it = current.get(p["id"])
            if not it:
                continue
            new_title = (p.get("new_title") or "").strip()
            if not new_title:
                continue
            if id_set is not None:
                if it["id"] not in id_set:
                    continue
            elif not p.get("selected", True):
                continue
            # 分开保存：old_title 是当前库里的值，plan_old_title 是方案生成时的值
            plan_old = (p.get("old_title") or it["old_title"]).strip()
            it["plan_old_title"] = plan_old
            it["stale"] = bool(p.get("stale")) or (plan_old != it["old_title"])
            it["new_title"] = new_title
            targets.append(it)
    else:
        with STATE_LOCK:
            sessions = list(STATE["sessions"])
        id_set = set(ids) if ids is not None else None
        targets = [
            it for it in sessions
            if (it.get("new_title") or "").strip()
            and (id_set is not None and it["id"] in id_set
                 or id_set is None and it.get("selected", True))
        ]

    if not targets:
        return {"ok": False, "error": "empty", "message": "没有可应用的标题。"}

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUPS_DIR / ts
    n = 1
    while backup_dir.exists():
        n += 1
        backup_dir = BACKUPS_DIR / f"{ts}-{n}"
    backup_databases(backup_dir, targets, plan_path or LATEST_PLAN)

    skipped: list[dict] = []
    applied: list[dict] = []
    t0 = time.time()

    con = rw_connect(SESSIONS_DB)
    try:
        con.execute("ATTACH DATABASE ? AS idx", (str(TASKS_DB),))
        con.execute("BEGIN IMMEDIATE")

        for it in targets:
            sid = it["id"]
            new_title = (it.get("new_title") or "").strip().replace("\r", " ").replace("\n", " ")
            if not new_title:
                continue
            srow = con.execute(
                "SELECT title, title_source FROM session WHERE id = ?", (sid,)
            ).fetchone()
            if not srow:
                skipped.append({"id": sid, "reason": "会话不存在"})
                continue

            trow = con.execute(
                "SELECT title, title_overridden, meta_json FROM idx.tasks WHERE task_id = ?",
                (sid,),
            ).fetchone()
            if not trow:
                skipped.append({"id": sid, "reason": "侧栏任务不存在"})
                continue

            cur_title, cur_source = srow
            task_title = trow[0]
            expected = (it.get("plan_old_title") or it["old_title"] or "").strip()

            if it.get("stale"):
                skipped.append({
                    "id": sid,
                    "reason": "方案已过期，原标题已变化，请重新生成",
                    "session_title": cur_title,
                    "task_title": task_title,
                    "expected": expected,
                })
                continue

            # 两个库都必须仍然是方案里的原标题，否则视为冲突，不覆盖
            if cur_title != expected or task_title != expected:
                skipped.append({
                    "id": sid,
                    "reason": "原标题已变化，跳过",
                    "session_title": cur_title,
                    "task_title": task_title,
                    "expected": expected,
                })
                continue

            con.execute(
                "UPDATE session SET title = ?, title_source = 'custom', time_title_updated = ? "
                "WHERE id = ?",
                (new_title, now_ms(), sid),
            )
            try:
                meta = json.loads(trow[2] or "{}")
            except Exception:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            meta["title"] = new_title
            meta["titleOverridden"] = True
            con.execute(
                "UPDATE idx.tasks SET title = ?, title_overridden = 1, meta_json = ? "
                "WHERE task_id = ?",
                (new_title, json.dumps(meta, ensure_ascii=False, separators=(",", ":")), sid),
            )
            applied.append({
                "id": sid,
                "workspace": it.get("workspace", ""),
                "old_title": cur_title,
                "new_title": new_title,
            })

        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            con.execute("PRAGMA main.wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        try:
            con.execute("PRAGMA idx.wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        con.close()

    # 校验
    verify_errors: list[dict] = []
    scon = ro_connect(SESSIONS_DB)
    tcon = ro_connect(TASKS_DB)
    try:
        for a in applied:
            s = scon.execute("SELECT title, title_source FROM session WHERE id = ?", (a["id"],)).fetchone()
            t = tcon.execute("SELECT title, title_overridden FROM tasks WHERE task_id = ?", (a["id"],)).fetchone()
            if not s or s[0] != a["new_title"]:
                verify_errors.append({"id": a["id"], "where": "session", "got": s})
            if not t or t[0] != a["new_title"] or t[1] != 1:
                verify_errors.append({"id": a["id"], "where": "tasks", "got": t})
    finally:
        scon.close()
        tcon.close()

    ok = not verify_errors
    result = {
        "ok": ok,
        "applied": len(applied),
        "applied_items": applied,
        "skipped": skipped,
        "verify_errors": verify_errors,
        "backup": str(backup_dir),
        "seconds": round(time.time() - t0, 2),
        "at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if verify_errors:
        result["ok"] = False
        result["error"] = "verify_failed"
        result["message"] = (
            f"写入完成，但有 {len(verify_errors)} 条校验不一致。请查看 "
            f"{backup_dir / 'apply-result.json'}，必要时执行 revert。"
        )
    elif not applied and skipped:
        result["ok"] = False
        result["error"] = "conflict"
        result["message"] = (
            f"没有写入任何标题：{len(skipped)} 条原标题已变化。请重新扫描后再应用。"
        )
    (backup_dir / "apply-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with STATE_LOCK:
        STATE["last_apply"] = result
        for a in applied:
            it = STATE["by_id"].get(a["id"])
            if it:
                it["old_title"] = a["new_title"]
                it["title_len"] = len(a["new_title"])
                it["old_source"] = "custom"
                it["old_overridden"] = True
                it["applied_at"] = now_ms()
                it["gen_status"] = "applied"
    return result


def revert_backup(backup_dir: Path, force_zcode: bool = False) -> dict:
    """只撤销某次 apply 写入的标题字段，保留之后的新会话和消息。"""
    state = zcode_state()
    if state != "stopped" and not force_zcode:
        return {
            "ok": False,
            "error": "zcode_running",
            "zcode_state": state,
            "message": "检测到 ZCode 正在运行，或无法确认其状态。请完全退出后再撤销。",
        }
    if not APPLY_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "busy", "message": "已有写回/撤销任务在运行。"}

    try:
        backup_dir = backup_dir.resolve()
        if not str(backup_dir).startswith(str(BACKUPS_DIR.resolve())):
            return {"ok": False, "error": "bad_path", "message": "备份目录不在 backups 下。"}

        result_path = backup_dir / "apply-result.json"
        original_path = backup_dir / "original.json"
        if not result_path.exists() or not original_path.exists():
            return {
                "ok": False,
                "error": "missing",
                "message": "备份缺少 apply-result.json 或 original.json，无法字段级撤销；请用 restore 做整库恢复。",
            }

        try:
            apply_result = json.loads(result_path.read_text(encoding="utf-8"))
            original = {
                x["id"]: x
                for x in json.loads(original_path.read_text(encoding="utf-8")).get("items", [])
            }
        except Exception as exc:
            return {"ok": False, "error": "bad_backup", "message": f"备份文件损坏：{exc}"}

        applied = apply_result.get("applied_items")
        if not isinstance(applied, list):
            legacy = apply_result.get("applied")
            if isinstance(legacy, list):
                applied = legacy
            else:
                # 旧格式备份只有 applied 数量；从 plan.json 重建本次应用的条目
                plan_file = backup_dir / "plan.json"
                applied = []
                if plan_file.exists():
                    try:
                        plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
                        applied = [
                            {
                                "id": p["id"],
                                "old_title": p.get("old_title", ""),
                                "new_title": p.get("new_title", ""),
                            }
                            for p in plan_data.get("items", [])
                            if p.get("new_title") and p.get("selected", True)
                        ]
                    except Exception:
                        applied = []
        if not applied:
            return {"ok": False, "error": "empty", "message": "这次应用没有成功写入记录，无法撤销。"}

        restored: list[dict] = []
        skipped: list[dict] = []
        con = rw_connect(SESSIONS_DB)
        try:
            con.execute("ATTACH DATABASE ? AS idx", (str(TASKS_DB),))
            con.execute("BEGIN IMMEDIATE")
            for a in applied:
                sid = a.get("id")
                new_title = a.get("new_title") or ""
                o = original.get(sid)
                if not sid or not new_title or not o:
                    skipped.append({"id": sid, "reason": "缺少原始记录"})
                    continue
                srow = con.execute(
                    "SELECT title, title_source FROM session WHERE id = ?", (sid,)
                ).fetchone()
                trow = con.execute(
                    "SELECT title, title_overridden, meta_json FROM idx.tasks WHERE task_id = ?", (sid,)
                ).fetchone()
                if not srow or not trow:
                    skipped.append({"id": sid, "reason": "会话或侧栏任务不存在"})
                    continue
                if srow[0] != new_title or trow[0] != new_title:
                    skipped.append({
                        "id": sid,
                        "reason": "标题已被再次修改，跳过",
                        "current": srow[0],
                    })
                    continue

                con.execute(
                    "UPDATE session SET title = ?, title_source = ?, time_title_updated = ? WHERE id = ?",
                    (
                        o["session"]["title"],
                        o["session"]["title_source"],
                        o["session"]["time_title_updated"],
                        sid,
                    ),
                )
                try:
                    cur_meta = json.loads(trow[2] or "{}")
                    orig_meta = json.loads(o["task"]["meta_json"] or "{}")
                except Exception:
                    skipped.append({"id": sid, "reason": "meta_json 无法解析，跳过"})
                    continue
                if not isinstance(cur_meta, dict) or not isinstance(orig_meta, dict):
                    skipped.append({"id": sid, "reason": "meta_json 不是对象，跳过"})
                    continue

                # 只恢复标题相关键，保留写回之后修改的其他元数据
                for key in ("title", "titleOverridden"):
                    if key in orig_meta:
                        cur_meta[key] = orig_meta[key]
                    else:
                        cur_meta.pop(key, None)

                con.execute(
                    "UPDATE idx.tasks SET title = ?, title_overridden = ?, meta_json = ? WHERE task_id = ?",
                    (
                        o["task"]["title"],
                        o["task"]["title_overridden"],
                        json.dumps(cur_meta, ensure_ascii=False, separators=(",", ":")),
                        sid,
                    ),
                )
                restored.append({
                    "id": sid,
                    "title": o["session"]["title"],
                    "previous": new_title,
                    "source": o["session"]["title_source"],
                    "overridden": o["task"]["title_overridden"],
                })
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                con.execute("PRAGMA main.wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                con.execute("PRAGMA idx.wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            con.close()

        verify_errors: list[dict] = []
        scon = ro_connect(SESSIONS_DB)
        tcon = ro_connect(TASKS_DB)
        try:
            for r in restored:
                s = scon.execute("SELECT title FROM session WHERE id = ?", (r["id"],)).fetchone()
                t = tcon.execute("SELECT title FROM tasks WHERE task_id = ?", (r["id"],)).fetchone()
                if not s or s[0] != r["title"]:
                    verify_errors.append({"id": r["id"], "where": "session", "got": s})
                if not t or t[0] != r["title"]:
                    verify_errors.append({"id": r["id"], "where": "tasks", "got": t})
        finally:
            scon.close()
            tcon.close()

        result = {
            "ok": not verify_errors,
            "restored": restored,
            "skipped": skipped,
            "verify_errors": verify_errors,
            "backup": str(backup_dir),
            "at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        if verify_errors:
            result["ok"] = False
            result["error"] = "verify_failed"
            result["message"] = f"撤销完成，但有 {len(verify_errors)} 条校验不一致。"
        elif not restored and skipped:
            result["ok"] = False
            result["error"] = "conflict"
            result["message"] = "没有撤销任何标题：它们在这之后又被修改过。"

        (backup_dir / "revert-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with STATE_LOCK:
            for r in restored:
                it = STATE["by_id"].get(r["id"])
                if it:
                    it["old_title"] = r["title"]
                    it["title_len"] = len(r["title"] or "")
                    it["old_source"] = r.get("source") or it["old_source"]
                    it["old_overridden"] = bool(r.get("overridden"))
                    it["gen_status"] = "ok" if it.get("new_title") else "pending"
        return result
    finally:
        APPLY_LOCK.release()


def restore_backup(backup_dir: Path, force_zcode: bool = False) -> dict:
    """整库灾难恢复：会覆盖备份时刻之后的会话/消息，日常撤销请用 revert。"""
    state = zcode_state()
    if state != "stopped" and not force_zcode:
        return {
            "ok": False,
            "error": "zcode_running",
            "zcode_state": state,
            "message": "请先完全退出 ZCode。",
        }
    if not APPLY_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "busy", "message": "已有写回/撤销任务在运行。"}

    try:
        backup_dir = backup_dir.resolve()
        missing = [
            name for name in ("tasks-index.sqlite", "db.sqlite")
            if not (backup_dir / name).exists()
        ]
        if missing:
            return {"ok": False, "error": "missing", "message": f"备份缺少：{', '.join(missing)}"}

        pre_dir = BACKUPS_DIR / f"pre-restore-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        n = 1
        while pre_dir.exists():
            n += 1
            pre_dir = BACKUPS_DIR / f"pre-restore-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{n}"
        backup_databases(pre_dir, [])

        for name, path in (("tasks-index.sqlite", TASKS_DB), ("db.sqlite", SESSIONS_DB)):
            shutil.copy2(backup_dir / name, path)
            for suffix in ("-wal", "-shm"):
                p = Path(str(path) + suffix)
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass

        return {
            "ok": True,
            "restored": str(backup_dir),
            "pre_restore_backup": str(pre_dir),
            "warning": "整库恢复已把两个数据库恢复到备份时刻，备份之后的新会话/消息会被覆盖；日常撤销标题请用 revert。",
        }
    finally:
        APPLY_LOCK.release()


def launch_zcode() -> dict:
    candidates = [
        os.environ.get("ZCODE_EXE", ""),
        r"D:\ZCode\ZCode.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ZCode" / "ZCode.exe"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            subprocess.Popen([c], cwd=str(Path(c).parent))
            return {"ok": True, "path": c}
    return {"ok": False, "error": "not_found", "message": "没找到 ZCode.exe，请手动打开。"}


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json; charset=utf-8",
}


def stats_payload() -> dict:
    with STATE_LOCK:
        sessions = STATE["sessions"]
        total = len(sessions)
        generated = sum(1 for it in sessions if it.get("new_title"))
        selected = sum(1 for it in sessions if it.get("selected") and it.get("new_title"))
        neutral = {"手动改过", "无用户消息"}
        problems = sum(
            1 for it in sessions
            if any(f not in neutral for f in (it.get("flags") or []))
        )
        by_ws = collections.Counter(it["workspace"] for it in sessions)
        return {
            "total": total,
            "generated": generated,
            "selected": selected,
            "problems": problems,
            "workspaces": by_ws.most_common(),
        }


def state_payload() -> dict:
    with STATE_LOCK:
        sessions = list(STATE["sessions"])
        progress = dict(STATE["progress"])
        settings = public_settings()
        return {
            "ok": True,
            "version": VERSION,
            "sessions": sessions,
            "stats": stats_payload(),
            "progress": progress,
            "zcode_state": zcode_state(),
            "zcode_running": zcode_running(),
            "csrf_token": CSRF_TOKEN,
            "model": MODEL,
            "model_configured": settings["model_configured"],
            "api_url": settings["api_url"],
            "api_key_set": settings["api_key_set"],
            "zcode_dir": settings["zcode_dir"],
            "data_dir": settings["data_dir"],
            "tasks_db": settings["tasks_db"],
            "sessions_db": settings["sessions_db"],
            "compatible": settings["compatible"],
            "compatibility_error": settings["compatibility_error"],
            "plan_path": STATE["plan_path"],
            "loaded_at": STATE["loaded_at"],
            "last_apply": STATE["last_apply"],
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "ZCodeTitleTidy/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _json(self, code: int, obj: Any):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception as exc:
            raise ValueError("invalid content-length") from exc
        if length < 0 or length > 2 * 1024 * 1024:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            raise ValueError("empty body")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid json") from exc
        if not isinstance(obj, dict):
            raise ValueError("body must be an object")
        return obj

    def _check_write(self) -> tuple[bool, str]:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host not in allowed_hosts:
            return False, "bad_host"

        origin = (self.headers.get("Origin") or "").strip().lower()
        allowed_origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        if origin and origin not in allowed_origins:
            return False, "bad_origin"

        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return False, "bad_content_type"

        token = self.headers.get("X-CSRF-Token") or ""
        if not secrets.compare_digest(token, CSRF_TOKEN):
            return False, "bad_token"
        return True, ""

    def _static(self, rel: str):
        path = (WEB_DIR / rel).resolve()
        if not str(path).startswith(str(WEB_DIR.resolve())) or not path.is_file():
            self._json(404, {"ok": False, "error": "not_found"})
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path.startswith("/assets/") or Path(path).suffix in STATIC_TYPES:
                self._static(path.lstrip("/"))
            elif path == "/api/state":
                self._json(200, state_payload())
            elif path == "/api/settings":
                self._json(200, {"ok": True, "settings": public_settings()})
            elif path == "/api/export":
                payload = plan_payload()
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="zcode-title-plan.json"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/ping":
                self._json(200, {"ok": True, "app": "zcode-title-tidy"})
            else:
                self._json(404, {"ok": False, "error": "not_found"})
        except Exception as exc:
            log.exception("GET 失败")
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]

            allowed, reason = self._check_write()
            if not allowed:
                self._json(403, {
                    "ok": False,
                    "error": "forbidden",
                    "message": f"拒绝写入请求（{reason}）",
                })
                return

            try:
                body = self._body()
            except ValueError as exc:
                self._json(400, {
                    "ok": False,
                    "error": "bad_request",
                    "message": str(exc),
                })
                return

            if path == "/api/rescan":
                rescan()
                self._json(200, state_payload())
                return

            if path == "/api/generate":
                ids = body.get("ids")
                if not isinstance(ids, list) or not ids or not all(
                    isinstance(x, str) and x for x in ids
                ):
                    self._json(400, {
                        "ok": False,
                        "error": "bad_request",
                        "message": "ids 必须是非空字符串数组",
                    })
                    return
                force = body.get("force") is True
                if not API_URL or not MODEL:
                    self._json(400, {
                        "ok": False,
                        "error": "model_not_configured",
                        "message": "模型服务未配置，请先在设置里填写 API 地址和模型。",
                    })
                    return
                if not STATE.get("compatibility", {}).get("ok", True):
                    self._json(400, {
                        "ok": False,
                        "error": "incompatible",
                        "message": "数据库结构不兼容，已禁止生成。",
                    })
                    return
                with STATE_LOCK:
                    if STATE["progress"]["running"]:
                        self._json(200, {"ok": False, "error": "已有生成任务在运行"})
                        return

                def run_generation():
                    try:
                        generate_items(ids=ids, force=force)
                    except Exception:
                        log.exception("批量生成失败")
                        with STATE_LOCK:
                            STATE["progress"]["running"] = False

                threading.Thread(target=run_generation, daemon=True).start()
                self._json(200, {"ok": True, "started": True, "count": len(ids)})
                return

            if path == "/api/update":
                sid = body.get("id")
                raw_title = body.get("new_title")
                if not isinstance(sid, str) or not sid or not isinstance(raw_title, str):
                    self._json(400, {
                        "ok": False,
                        "error": "bad_request",
                        "message": "id 和 new_title 必须是字符串",
                    })
                    return
                new_title = raw_title.strip().replace("\r", " ").replace("\n", " ")
                if len(new_title) > 200:
                    self._json(400, {
                        "ok": False,
                        "error": "bad_request",
                        "message": "new_title 过长",
                    })
                    return
                selected = body.get("selected")
                if selected is not None and not isinstance(selected, bool):
                    self._json(400, {
                        "ok": False,
                        "error": "bad_request",
                        "message": "selected 必须是布尔值",
                    })
                    return
                with STATE_LOCK:
                    it = STATE["by_id"].get(sid)
                    if not it:
                        self._json(404, {"ok": False, "error": "not_found"})
                        return
                    it["new_title"] = new_title
                    it["gen_status"] = "edited" if new_title else "pending"
                    if selected is not None:
                        it["selected"] = selected
                save_plan(False)
                self._json(200, {"ok": True})
                return

            if path == "/api/select":
                ids = body.get("ids")
                selected = body.get("selected")
                if (
                    not isinstance(ids, list)
                    or not all(isinstance(x, str) for x in ids)
                    or not isinstance(selected, bool)
                ):
                    self._json(400, {
                        "ok": False,
                        "error": "bad_request",
                        "message": "ids 必须是字符串数组，selected 必须是布尔值",
                    })
                    return
                wanted = set(ids)
                with STATE_LOCK:
                    for it in STATE["sessions"]:
                        if it["id"] in wanted:
                            it["selected"] = selected
                save_plan(False)
                self._json(200, {"ok": True})
                return

            if path == "/api/regenerate":
                sid = body.get("id")
                if not isinstance(sid, str) or not sid:
                    self._json(400, {
                        "ok": False,
                        "error": "bad_request",
                        "message": "id 必须是非空字符串",
                    })
                    return
                if not API_URL or not MODEL:
                    self._json(400, {
                        "ok": False,
                        "error": "model_not_configured",
                        "message": "模型服务未配置，请先在设置里填写 API 地址和模型。",
                    })
                    return
                if not STATE.get("compatibility", {}).get("ok", True):
                    self._json(400, {
                        "ok": False,
                        "error": "incompatible",
                        "message": "数据库结构不兼容，已禁止生成。",
                    })
                    return
                with STATE_LOCK:
                    it = STATE["by_id"].get(sid)
                    if not it:
                        self._json(404, {"ok": False, "error": "not_found"})
                        return
                    if STATE["progress"]["running"]:
                        self._json(409, {
                            "ok": False,
                            "error": "busy",
                            "message": "正在批量生成，请稍候",
                        })
                        return
                    snapshot = dict(it)
                title, err = generate_one(snapshot)
                with STATE_LOCK:
                    it["new_title"] = title
                    it["gen_status"] = "ok" if not err else "fallback"
                    it["gen_error"] = err
                    it["plan_old_title"] = it["old_title"]
                    it["stale"] = False
                postprocess_titles([it])
                save_plan(False)
                self._json(200, {"ok": True, "item": it})
                return

            if path == "/api/settings":
                updates = {
                    key: body[key]
                    for key in ("api_url", "model", "zcode_dir", "api_key")
                    if key in body
                }
                try:
                    save_config(updates, clear_api_key=body.get("clear_api_key") is True)
                except ValueError as exc:
                    self._json(400, {
                        "ok": False,
                        "error": "bad_settings",
                        "message": str(exc),
                    })
                    return
                rescan()
                if LATEST_PLAN.exists():
                    try:
                        merge_plan(STATE["sessions"])
                    except Exception as exc:
                        log.warning("合并旧方案失败：%s", exc)
                self._json(200, {
                    "ok": True,
                    "settings": public_settings(),
                    "state": state_payload(),
                })
                return

            if path == "/api/settings/test":
                key = body.get("api_key")
                if not isinstance(key, str) or not key.strip():
                    key = None
                result = test_model(
                    api_url=(body.get("api_url") or None),
                    model=(body.get("model") or None),
                    api_key=key,
                )
                self._json(200 if result.get("ok") else 400, result)
                return

            if path == "/api/save":
                saved = save_plan(explicit=True)
                self._json(200, {"ok": True, "path": str(saved)})
                return

            if path == "/api/apply":
                ids = body.get("ids")
                if not isinstance(ids, list) or not ids or not all(
                    isinstance(x, str) and x for x in ids
                ):
                    self._json(400, {
                        "ok": False,
                        "error": "bad_request",
                        "message": "ids 必须是非空字符串数组",
                    })
                    return
                if body.get("confirm") is not True:
                    self._json(400, {
                        "ok": False,
                        "error": "confirm_required",
                        "message": "写回需要明确确认",
                    })
                    return
                result = apply_plan(ids=ids)
                if result.get("ok"):
                    code = 200
                elif result.get("error") in ("zcode_running", "busy"):
                    code = 409
                else:
                    code = 400
                self._json(code, result)
                return

            if path == "/api/revert":
                backup = body.get("backup")
                if not isinstance(backup, str) or not backup:
                    self._json(400, {
                        "ok": False,
                        "error": "bad_request",
                        "message": "backup 必须是非空字符串",
                    })
                    return
                if body.get("confirm") is not True:
                    self._json(400, {
                        "ok": False,
                        "error": "confirm_required",
                        "message": "撤销需要明确确认",
                    })
                    return
                backup_path = Path(backup).resolve()
                if not str(backup_path).startswith(str(BACKUPS_DIR.resolve())):
                    self._json(400, {
                        "ok": False,
                        "error": "bad_path",
                        "message": "备份目录不在 backups 下",
                    })
                    return
                result = revert_backup(backup_path)
                if result.get("ok"):
                    code = 200
                elif result.get("error") in ("zcode_running", "busy"):
                    code = 409
                else:
                    code = 400
                self._json(code, result)
                return

            if path == "/api/open-zcode":
                self._json(200, launch_zcode())
                return

            self._json(404, {"ok": False, "error": "not_found"})
        except Exception as exc:
            log.exception("POST %s 失败", self.path)
            self._json(500, {"ok": False, "error": str(exc)})


def port_available(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def existing_instance(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            obj = json.loads(r.read().decode("utf-8"))
            return obj.get("app") == "zcode-title-tidy"
    except Exception:
        return False


def serve(port: int, open_browser: bool = True) -> None:
    rescan()
    if LATEST_PLAN.exists():
        try:
            merge_plan(STATE["sessions"])
        except Exception as exc:
            log.warning("合并旧方案失败：%s", exc)

    chosen = port
    for candidate in range(port, port + 20):
        if existing_instance(candidate):
            url = f"http://127.0.0.1:{candidate}/"
            print(f"已在运行：{url}")
            if open_browser:
                webbrowser.open(url)
            return
        if port_available(candidate):
            chosen = candidate
            break
    else:
        raise RuntimeError("找不到可用端口")

    server = ThreadingHTTPServer(("127.0.0.1", chosen), Handler)
    url = f"http://127.0.0.1:{chosen}/"
    log.info("服务已启动：%s", url)
    print(f"ZCode 标题整理器：{url}")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_scan(items: list[dict]) -> None:
    print(f"共 {len(items)} 个会话\n")
    cur_ws = None
    for it in items:
        if it["workspace"] != cur_ws:
            cur_ws = it["workspace"]
            print(f"\n== {cur_ws} ==")
        flags = "/".join(it["flags"]) or "-"
        print(
            f"{fmt_time(it['updated_at']):12} {it['old_source']:11} "
            f"{it['title_len']:>2}字 {flags:18} {it['old_title'][:60]}"
        )


def cli_generate(args) -> int:
    rescan()
    if LATEST_PLAN.exists():
        merge_plan(STATE["sessions"])
    ids = args.ids
    if args.limit:
        sessions = sorted(STATE["sessions"], key=lambda x: -(x["updated_at"] or 0))[:args.limit]
        ids = [it["id"] for it in sessions]
    result = generate_items(ids=ids, force=args.force, workers=args.workers, model=args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        return 1
    print("\n生成结果：")
    for it in STATE["sessions"]:
        if it.get("new_title"):
            print(f"  {it['old_title'][:34]:36} -> {it['new_title']}")
    print(f"\n方案已保存：{LATEST_PLAN}")
    return 0


def cli_apply(args) -> int:
    plan_path = Path(args.plan) if args.plan else LATEST_PLAN
    if not plan_path.exists():
        print(f"找不到方案：{plan_path}", file=sys.stderr)
        return 1
    result = apply_plan(ids=args.ids, force_zcode=args.force, plan_path=plan_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        if result.get("error") == "zcode_running":
            print("\n请完全退出 ZCode（包括托盘）后重试。", file=sys.stderr)
        return 2
    print(f"\n完成：写入 {result['applied']} 条，备份在 {result['backup']}")
    return 0


def main() -> int:
    global TASKS_DB, SESSIONS_DB, MODEL
    parser = argparse.ArgumentParser(description="ZCode 会话标题整理器")
    parser.add_argument("--tasks-db", default=None)
    parser.add_argument("--sessions-db", default=None)
    parser.add_argument("--model", default=None)
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动本地网页")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--no-open", action="store_true")

    sub.add_parser("scan", help="扫描并打印会话")

    p_gen = sub.add_parser("generate", help="生成候选标题")
    p_gen.add_argument("--ids", nargs="*")
    p_gen.add_argument("--force", action="store_true")
    p_gen.add_argument("--workers", type=int, default=4)
    p_gen.add_argument("--limit", type=int)

    p_apply = sub.add_parser("apply", help="应用方案（需退出 ZCode）")
    p_apply.add_argument("--latest", action="store_true")
    p_apply.add_argument("--plan")
    p_apply.add_argument("--ids", nargs="*")
    p_apply.add_argument("--force", action="store_true")

    p_revert = sub.add_parser("revert", help="只撤销某次写回的标题")
    p_revert.add_argument("backup_dir")
    p_revert.add_argument("--force", action="store_true")

    p_restore = sub.add_parser("restore", help="整库恢复（会覆盖备份后的新会话）")
    p_restore.add_argument("backup_dir")
    p_restore.add_argument("--force", action="store_true")

    sub.add_parser("open-zcode", help="打开 ZCode")
    sub.add_parser("export", help="导出方案 JSON")

    args = parser.parse_args()

    ensure_data_dirs()
    migrate_legacy_data()
    load_config()
    if args.tasks_db:
        TASKS_DB = Path(args.tasks_db)
    if args.sessions_db:
        SESSIONS_DB = Path(args.sessions_db)
    if args.model:
        MODEL = args.model

    setup_logging(console=True)

    if args.cmd in (None, "serve"):
        port = getattr(args, "port", DEFAULT_PORT)
        no_open = getattr(args, "no_open", False)
        serve(port, open_browser=not no_open)
        return 0

    if args.cmd == "scan":
        print_scan(scan_sessions(TASKS_DB, SESSIONS_DB))
        return 0

    if args.cmd == "generate":
        return cli_generate(args)

    if args.cmd == "apply":
        return cli_apply(args)

    if args.cmd == "revert":
        result = revert_backup(Path(args.backup_dir), force_zcode=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2

    if args.cmd == "restore":
        result = restore_backup(Path(args.backup_dir), force_zcode=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2

    if args.cmd == "open-zcode":
        print(json.dumps(launch_zcode(), ensure_ascii=False))
        return 0

    if args.cmd == "export":
        payload = plan_payload()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
