#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vjudge_alias.py —— VJudge 版「备注 / 别名库」（SQLite）

VJudge 的 rank 接口只给 handle（`participants[uid].name`）和头像 URL，
**没有**学号 / 真实姓名 / 学校。现场想在大屏上看到真实姓名、给个别队伍
换专属校徽，就得自己维护一张备注表 —— 这就是本模块。

* 存储：SQLite，默认 ``<程序根>\\vjudge-aliases.db``
  （默认放在程序根目录），可用 ``--db`` 覆盖；
* 表结构：``alias(vjudge_uid TEXT PRIMARY KEY, handle TEXT NOT NULL,
  display TEXT DEFAULT '', logo TEXT DEFAULT '', note TEXT DEFAULT '',
  updated_at TEXT NOT NULL)``；
* 读侧（抓取器 / 实时榜）只需要 ``load_map()``；写侧（GUI 的「队伍备注」页）
  用 ``set_alias()``；调试 / 导出用 ``list_rows()``。

这个文件在 ``pta-resolver\\`` 和 ``live-board\\`` 各放一份（内容相同），
两份都**不依赖**任何第三方库，只用标准库 sqlite3。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 数据库文件名（放在程序根目录，跟 cookie.txt / contest.json 同级）
DB_NAME = "vjudge-aliases.db"

#: 表结构：与任务说明**逐字一致**（uid = participants 的键）
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS alias(
  vjudge_uid TEXT PRIMARY KEY,
  handle     TEXT NOT NULL,
  display    TEXT DEFAULT '',
  logo       TEXT DEFAULT '',
  note       TEXT DEFAULT '',
  updated_at TEXT NOT NULL
);
"""

#: 兼容老库：以后若加字段，缺哪列补哪列（只加不删）。
_EXTRA_COLUMNS = (
    ("display", "TEXT DEFAULT ''"),
    ("logo", "TEXT DEFAULT ''"),
    ("note", "TEXT DEFAULT ''"),
    ("handle", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
)


def default_db_path() -> Path:
    """默认数据库路径：``<程序根>\\vjudge-aliases.db``。

    * 开发时 = 本文件所在目录（``pta-resolver\\vjudge-aliases.db``）；
    * 打包后 = exe 所在目录（**不能用 _MEIPASS**：那是每次启动都会变的临时目录，
      备注这样写进去下次就找不到了）；
    * 环境变量 ``VJUDGE_ALIAS_DB`` 可强制覆盖（多份程序共用一份备注时用）。

    注意：GUI 的「队伍备注」页与 poller 调用抓取器时**传的是同一个路径**
    （``<work_root>\\vjudge-aliases.db``），所以两边看到的是同一份备注。
    """
    env = str(os.environ.get("VJUDGE_ALIAS_DB") or "").strip()
    if env:
        return Path(env).expanduser()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / DB_NAME
    return Path(__file__).resolve().parent / DB_NAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve(db_path: Any = None) -> Path:
    if db_path in (None, ""):
        return default_db_path()
    return Path(str(db_path)).expanduser()


def connect(db_path: Any = None) -> sqlite3.Connection:
    """打开（并按需建表）数据库；调用方负责关闭。

    ``timeout=5`` 是给「GUI 正在写 + poller 正在读」这种并发准备的：
    SQLite 默认忙等 5 秒才报 database is locked。
    """
    path = _resolve(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA_SQL)
    # 老库缺列时补齐（只加不删；失败就算了，不影响现有列）
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(alias)")}
        for name, ddl in _EXTRA_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE alias ADD COLUMN {name} {ddl}")
    except sqlite3.Error:
        pass
    conn.commit()
    return conn


def load_map(db_path: Any = None) -> dict[str, dict[str, str]]:
    """读出 ``{uid: {"display": ..., "logo": ...}}``。

    抓取器只需要这两个字段：display 非空就当队名，logo 非空就优先当校徽。
    数据库不存在 / 表为空 / 打不开都返回 ``{}``（**绝不因为备注库坏了抓不到数**）。
    """
    out: dict[str, dict[str, str]] = {}
    try:
        conn = connect(db_path)
    except (OSError, sqlite3.Error):
        return out
    try:
        for row in conn.execute("SELECT vjudge_uid, display, logo FROM alias"):
            uid = str(row["vjudge_uid"] or "").strip()
            if not uid:
                continue
            out[uid] = {
                "display": str(row["display"] or "").strip(),
                "logo": str(row["logo"] or "").strip(),
            }
    except sqlite3.Error:
        return {}
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return out


def get_row(db_path: Any = None, uid: str = "") -> dict[str, str] | None:
    """读一条完整记录（GUI 双击编辑前回填用）；没有就返回 None。"""
    uid = str(uid or "").strip()
    if not uid:
        return None
    try:
        conn = connect(db_path)
    except (OSError, sqlite3.Error):
        return None
    try:
        row = conn.execute(
            "SELECT vjudge_uid, handle, display, logo, note, updated_at "
            "FROM alias WHERE vjudge_uid = ?", (uid,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if row is None:
        return None
    return {key: str(row[key] or "") for key in
            ("vjudge_uid", "handle", "display", "logo", "note", "updated_at")}


def set_alias(db_path: Any = None, uid: str = "", handle: str = "",
              display: str = "", logo: str = "", note: str = "") -> None:
    """写入 / 覆盖一条备注（``uid`` 主键，存在就更新）。

    * ``display`` 空串 = 不备注（榜单回落显示 handle）；
    * ``logo`` 空串 = 走默认规则（备注姓名 → 校徽目录 → VJudge 头像 → 默认校徽），
      也可以填**校徽目录里的文件名**或**图片 URL**；
    * ``handle`` 每次抓取都会被最新 handle 覆盖，这里写进来方便人看 / 排查。
    """
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("uid 不能为空")
    handle = str(handle or "").strip()
    display = str(display or "").strip()
    logo = str(logo or "").strip()
    note = str(note or "").strip()
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO alias(vjudge_uid, handle, display, logo, note, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(vjudge_uid) DO UPDATE SET "
            "handle=excluded.handle, display=excluded.display, "
            "logo=excluded.logo, note=excluded.note, updated_at=excluded.updated_at",
            (uid, handle, display, logo, note, _now_iso()))
        conn.commit()
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def delete_alias(db_path: Any = None, uid: str = "") -> bool:
    """删掉一条备注（GUI 里清空整行时用）；返回是否真的删了。"""
    uid = str(uid or "").strip()
    if not uid:
        return False
    try:
        conn = connect(db_path)
    except (OSError, sqlite3.Error):
        return False
    try:
        cur = conn.execute("DELETE FROM alias WHERE vjudge_uid = ?", (uid,))
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def list_rows(db_path: Any = None) -> list[dict[str, str]]:
    """列出全部备注，按 handle 排序（GUI 的表格 / 人工排查用）。"""
    rows: list[dict[str, str]] = []
    try:
        conn = connect(db_path)
    except (OSError, sqlite3.Error):
        return rows
    try:
        for row in conn.execute(
                "SELECT vjudge_uid, handle, display, logo, note, updated_at "
                "FROM alias ORDER BY handle COLLATE NOCASE"):
            rows.append({key: str(row[key] or "") for key in
                         ("vjudge_uid", "handle", "display", "logo", "note", "updated_at")})
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return rows


def main(argv: list[str] | None = None) -> int:
    """命令行小工具：``python vjudge_alias.py list / set / del``（方便手改、导出）。"""
    import argparse
    import json
    parser = argparse.ArgumentParser(prog="vjudge_alias.py",
                                     description="VJudge 备注 / 别名库（SQLite）小工具")
    parser.add_argument("--db", default="", help=f"数据库路径（默认 {DB_NAME}）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出全部备注")
    p_list.add_argument("--json", action="store_true", help="以 JSON 输出")

    p_set = sub.add_parser("set", help="写一条备注")
    p_set.add_argument("uid")
    p_set.add_argument("--handle", default="")
    p_set.add_argument("--display", default="")
    p_set.add_argument("--logo", default="")
    p_set.add_argument("--note", default="")

    p_del = sub.add_parser("del", help="删一条备注")
    p_del.add_argument("uid")

    args = parser.parse_args(argv)
    if args.command == "list":
        rows = list_rows(args.db)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print(f"数据库：{_resolve(args.db)}")
            print(f"共 {len(rows)} 条")
            for row in rows:
                print(f"  {row['vjudge_uid']:<10} {row['handle']:<20} "
                      f"display={row['display']!r} logo={row['logo']!r} "
                      f"note={row['note']!r}")
        return 0
    if args.command == "set":
        set_alias(args.db, args.uid, args.handle, args.display, args.logo, args.note)
        print(f"已写入：{args.uid} -> display={args.display!r} logo={args.logo!r}")
        return 0
    if args.command == "del":
        ok = delete_alias(args.db, args.uid)
        print("已删除" if ok else "没有这条记录")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
    raise SystemExit(main())
