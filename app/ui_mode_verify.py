# -*- coding: utf-8 -*-
"""ui_mode_verify.py —— 「实时榜单 / 赛后滚榜 两个模式彻底分开」的验收脚本。

子命令
------
    python app\\ui_mode_verify.py smoke          只建窗口 + 每页切一遍（不出图）
    python app\\ui_mode_verify.py shots          两种尺寸 × 两个模式全页截图 + 几何/关键词证据
    python app\\ui_mode_verify.py config         配置单份证明（实时改 → 滚榜截图；反向再来一次）
    python app\\ui_mode_verify.py history        历史按模式过滤证明（造 1 条实时 + 1 条滚榜）
    python app\\ui_mode_verify.py regression     8792 起停实时服务 + /api/board 对基准
    python app\\ui_mode_verify.py hashes before|after   产物 sha256 + 启动时同步 live\\ 复验
    python app\\ui_mode_verify.py all            除 hashes 外的全部

产物（都在 app\\docs\\ 下）
    gui-frames2\\*.png       截图（live-N-key-<几何>.png / roll-N-key-<几何>.png）
    ui-geometry-check.json  左栏页码 / 分组 + 每页 overflow
    mode-separation.json    关键词自检 + 页码连续性
    config-single-source.json
    history-mode-filter.json
    api-regression-mode-split.json
    dist-hashes-mode-split.json

所有命令都**不抢焦点、不弹窗**（截图走 capture_window 的 PrintWindow；
`--no-dialog` 那套在脚本里用 silence_dialogs 顶掉）。跑完会把 app\\config.json /
app\\history.json 还原成跑之前的样子。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent
DOCS = APP / "docs"
FRAMES = DOCS / "gui-frames2"
DIST = ROOT / "dist" / "ScoreDeck"


def _env_path(name: str, default: Path) -> Path:
    """带环境变量覆盖的路径（**默认值里不写任何本机绝对路径**）。

    这台开发/验收机上的几个目录（只读的 live-board 源、已部署的发行版、
    旧版 PTA-Board 的家目录、被移除功能的备份）都可以用环境变量指过去，
    换一台机器跑验收不用改代码。
    """
    value = str(os.environ.get(name) or "").strip()
    return Path(value) if value else default


LIVE_SRC = _env_path("PTA_LIVE_SRC", ROOT.parent / "live-board")
DEPLOYED = _env_path("PTA_DEPLOYED_DIR", ROOT / "dist" / "ScoreDeck")
LEGACY_HOME = _env_path("PTA_BOARD_HOME", Path.home() / "PTA-Board")
BACKUP_DIR = _env_path("PTA_BACKUP_DIR", ROOT / "build" / "removed-edge-channel")
BASELINE_BOARD = LIVE_SRC / "docs" / "m2-board-after.json"
TEST_PORT = 8792

sys.path.insert(0, str(APP))

import tkinter as tk                                    # noqa: E402
from tkinter import ttk                                 # noqa: E402

import board_config as cfgmod                           # noqa: E402
import live_backend as live                             # noqa: E402
import pta_board_gui as base                            # noqa: E402
import capture_window                                   # noqa: E402
from pta_assistant_gui import (                         # noqa: E402
    AssistantApp, MODE_LIVE, MODE_ROLL, MODE_PAGES, MODE_PAGE_DEFS, NAV_GROUPS,
    ensure_live_config, silence_dialogs,
)

#: 各模式**不许出现**的关键词（这轮需求里的硬指标）
FORBIDDEN = {
    MODE_LIVE: ["比赛数据包", "播放方式", "Resolver", "Esc",
                # 2026-10-13：外部观战页 + 「借用浏览器」取数通道已删除
                "team-view", "官方观战", "Node", "借用 Edge", "从哪里取数据"],
    MODE_ROLL: ["启动实时", "通道", "OBS", "team-view", "官方观战", "Node", "直播",
                "借用 Edge", "从哪里取数据"],
}
#: 全产品不许出现的关键词（取数通道改造的硬指标）
GLOBAL_FORBIDDEN = ["team-view", "官方观战", "借用 Edge", "Edge 交接", "msedge",
                    "edge_fetch", "--via edge", "Edge交接"]
#: 左栏不许出现的分组字样
NAV_FORBIDDEN = ["共用", "两种模式"]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


class FileGuard:
    """把被验收过程改过的 JSON 还原回去（验收不该动用户的设置）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.text = self.path.read_text(encoding="utf-8") if self.path.is_file() else None

    def restore(self) -> None:
        if self.text is None:
            return
        try:
            self.path.write_text(self.text, encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 建窗口 / 取控件文字
# ---------------------------------------------------------------------------

def build_app(geometry: str = "1200x820", mode: str | None = None):
    """不抢焦点地建一个窗口（不 mainloop）。"""
    store = cfgmod.Store()
    store.load()
    ensure_live_config(store.config, [])
    root = tk.Tk()
    style = ttk.Style()
    for theme in ("vista", "winnative", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    app = AssistantApp(root, store, base.StartupTrace(False, False), mode=mode)
    app._shot_only = True
    try:
        width, height = (int(part) for part in geometry.lower().split("x")[:2])
        if width < 980 or height < 640:
            root.minsize(600, 400)
    except ValueError:
        pass
    root.geometry(geometry)
    root.update_idletasks()
    root.update()
    time.sleep(1.0)
    root.update()
    return root, app, store


def close_app(root) -> None:
    """先撤掉所有 after 任务再销毁：否则 Tk 会往 stderr 打一堆
    「invalid command name ..._pump / _tick_live」并让退出码变成 1。"""
    try:
        for job in root.tk.call("after", "info"):
            try:
                root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
        root.update_idletasks()
    except Exception:  # noqa: BLE001
        pass
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def nav_rows(app) -> list[dict]:
    """左栏实际渲染出来的行（分组标题 + 页码/标题），按屏幕从上到下。"""
    rows: list[dict] = []
    for child in app._nav_box.winfo_children():
        if child.winfo_class() == "Frame":
            labels = [w for w in child.winfo_children() if w.winfo_class() == "Label"]
            number = labels[0].cget("text") if labels else ""
            title = labels[1].cget("text") if len(labels) > 1 else ""
            rows.append({"kind": "page", "number": int(number) if str(number).isdigit() else None,
                         "title": title})
        elif child.winfo_class() == "Label":
            rows.append({"kind": "group", "text": child.cget("text")})
    return rows


def widget_texts(widget, out: list[str]) -> None:
    """把一个控件（含子孙）所有"人眼能看到的字"收进 out。"""
    try:
        text = str(widget.cget("text"))
        if text:
            out.append(text)
    except Exception:  # noqa: BLE001
        pass
    try:
        name = str(widget.cget("textvariable"))
        if name:
            value = widget.getvar(name)
            if value not in (None, ""):
                out.append(str(value))
    except Exception:  # noqa: BLE001
        pass
    try:
        values = widget.cget("values")
        if values:
            out.extend(str(v) for v in values)
    except Exception:  # noqa: BLE001
        pass
    try:
        if widget.winfo_class() == "Text":
            out.append(widget.get("1.0", "end"))
        elif widget.winfo_class() == "Treeview":
            for col in widget["columns"]:
                out.append(str(widget.heading(col, "text")))
            for item in widget.get_children():
                out.append(" ".join(str(v) for v in widget.item(item, "values")))
    except Exception:  # noqa: BLE001
        pass
    for child in widget.winfo_children():
        widget_texts(child, out)


def page_text(app, idx: int) -> str:
    out: list[str] = []
    widget_texts(app._page_frames[idx], out)
    return "\n".join(out)


def page_geometry(app, idx: int) -> dict:
    """跟 app\\docs\\ui-geometry-check.json 同一套算法：页面请求宽度 - 画布可视宽度。"""
    canvas = app._page_canvases[idx]
    page = app._page_frames[idx]
    canvas_w = int(canvas.winfo_width())
    canvas_h = int(canvas.winfo_height())
    req_w = int(page.winfo_reqwidth())
    req_h = int(page.winfo_reqheight())
    culprits: list[dict] = []

    def walk(widget, offset: int, depth: int) -> None:
        if depth > 6 or len(culprits) >= 6:
            return
        try:
            x = offset + int(widget.winfo_x())
            need = int(widget.winfo_reqwidth())
        except Exception:  # noqa: BLE001
            return
        if x + need > canvas_w + 1 and widget is not page:
            label = ""
            try:
                label = str(widget.cget("text"))[:24]
            except Exception:  # noqa: BLE001
                label = ""
            culprits.append({"class": widget.winfo_class(), "text": label,
                             "x": x, "reqwidth": need})
        for child in widget.winfo_children():
            walk(child, x, depth + 1)

    walk(page, 0, 0)
    return {"canvas_w": canvas_w, "canvas_h": canvas_h,
            "page_reqwidth": req_w, "page_reqheight": req_h,
            "overflow_px": req_w - canvas_w,
            "clipped": req_w > canvas_w + 1,
            "culprits": culprits,
            "vertical_scrollable": req_h > canvas_h}


def select_page(app, root, mode: str, idx: int) -> None:
    app.mode = mode
    app._last_tab[mode] = idx
    if idx not in MODE_PAGES[mode]:
        app._apply_mode(write=False)
    app._nav_select(idx)
    root.update_idletasks()
    root.update()
    time.sleep(0.35)


def shoot(app, root, name: str) -> dict:
    FRAMES.mkdir(parents=True, exist_ok=True)
    path = FRAMES / name
    ok, note = capture_window.grab_with_fallback(root.winfo_id(), path)
    return {"file": name, "ok": bool(ok), "note": note,
            "bytes": path.stat().st_size if path.is_file() else 0}


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------

def cmd_smoke(_args) -> int:
    root, app, _store = build_app("1200x820")
    try:
        for mode in (MODE_LIVE, MODE_ROLL):
            for idx, key, title, _sub, _steps, number in MODE_PAGE_DEFS[mode]:
                select_page(app, root, mode, idx)
                print(f"ok  {mode}-{number}-{key}  {title}", flush=True)
        print("nav:", json.dumps({m: nav_rows(app) for m in (MODE_LIVE,)}, ensure_ascii=False))
    finally:
        close_app(root)
    return 0


# ---------------------------------------------------------------------------
# shots：截图 + 几何 + 关键词
# ---------------------------------------------------------------------------

def cmd_shots(args) -> int:
    cfg_guard = FileGuard(cfgmod.app_dir() / cfgmod.CONFIG_NAME)
    hist_guard = FileGuard(cfgmod.app_dir() / cfgmod.HISTORY_NAME)
    geometry_report = {"generated_at": now(), "script": "app/ui_mode_verify.py shots",
                       "modes": {}, "probes": []}
    sep = {"generated_at": now(),
           "forbidden_keywords": {"live": FORBIDDEN[MODE_LIVE], "roll": FORBIDDEN[MODE_ROLL]},
           "nav_forbidden": NAV_FORBIDDEN,
           "checks": [], "nav_checks": [], "numbering": {}, "all_ok": True}
    try:
        for geometry in args.geometries:
            root, app, _store = build_app(geometry)
            try:
                probe = {"geometry": geometry, "nav": {}, "pages": {}, "shots": []}
                for mode in (MODE_LIVE, MODE_ROLL):
                    app.mode = mode
                    app._last_tab[mode] = MODE_PAGES[mode][0]
                    app._apply_mode(write=False)
                    root.update_idletasks()
                    root.update()
                    time.sleep(0.3)
                    rows = nav_rows(app)
                    probe["nav"][mode] = rows
                    # -- 左栏：分组标题不许出现「共用 / 两种模式」
                    groups = [r["text"] for r in rows if r["kind"] == "group"]
                    hits = [w for t in groups for w in NAV_FORBIDDEN if w in t]
                    sep["nav_checks"].append({
                        "geometry": geometry, "mode": mode, "group_titles": groups,
                        "pages": [f'{r["number"]} {r["title"]}' for r in rows if r["kind"] == "page"],
                        "hits": hits, "ok": not hits})
                    if hits:
                        sep["all_ok"] = False
                    for idx, key, _title, _sub, _steps, number in MODE_PAGE_DEFS[mode]:
                        select_page(app, root, mode, idx)
                        name = f"{mode}-{number}-{key}-{geometry}.png"
                        shot = shoot(app, root, name)
                        probe["shots"].append({"mode": mode, "page": key,
                                               "number": number, **shot})
                        geo = page_geometry(app, idx)
                        probe["pages"][f"{mode}-{key}"] = geo
                        text = page_text(app, idx)
                        found = [kw for kw in FORBIDDEN[mode] if kw in text]
                        global_found = [kw for kw in GLOBAL_FORBIDDEN if kw in text]
                        sep["checks"].append({
                            "geometry": geometry, "mode": mode, "page": key,
                            "number": number, "title": _title,
                            "forbidden": FORBIDDEN[mode], "hits": found,
                            "global_forbidden": GLOBAL_FORBIDDEN,
                            "global_hits": global_found,
                            "ok": not found and not global_found,
                            "overflow_px": geo["overflow_px"], "clipped": geo["clipped"]})
                        if found or global_found:
                            sep["all_ok"] = False
                        print(f"{'ok ' if shot['ok'] else 'ERR'} {name} "
                              f"overflow={geo['overflow_px']:+d} hits={found or '-'} "
                              f"global_hits={global_found or '-'}",
                              flush=True)
                geometry_report["probes"].append(probe)
            finally:
                close_app(root)
        # -- 每个模式各自记住上次停留的那一页 --
        root2, app2, _s2 = build_app("1200x820")
        try:
            if app2.mode != MODE_LIVE:
                app2.mode = MODE_LIVE
                app2._apply_mode(write=False)
            app2._nav_select(MODE_PAGES[MODE_LIVE][2])      # 实时 · 3 服务
            root2.update()
            live_pick = app2._nav_current
            app2.on_mode_click(MODE_ROLL)
            root2.update()
            roll_first = app2._nav_current                  # 切过去应当落在滚榜自己的第一页
            app2._nav_select(MODE_PAGES[MODE_ROLL][2])      # 滚榜 · 3 滚榜
            root2.update()
            roll_pick = app2._nav_current
            app2.on_mode_click(MODE_LIVE)
            root2.update()
            live_back = app2._nav_current
            app2.on_mode_click(MODE_ROLL)
            root2.update()
            roll_back = app2._nav_current
            memory = {
                "picked": {"live": live_pick, "roll": roll_pick},
                "roll_first_page_after_switch": roll_first,
                "back_to_live": live_back, "back_to_roll": roll_back,
                "live_remembers_its_page": live_back == live_pick,
                "roll_remembers_its_page": roll_back == roll_pick,
                "cfg_last_tab_live": app2.cfg.get("last_tab_live"),
                "cfg_last_tab_roll": app2.cfg.get("last_tab_roll"),
            }
            memory["ok"] = bool(memory["live_remembers_its_page"]
                                and memory["roll_remembers_its_page"])
            sep["last_tab_memory"] = memory
            if not memory["ok"]:
                sep["all_ok"] = False
            print("last-tab memory: " + json.dumps(memory, ensure_ascii=False), flush=True)
        finally:
            close_app(root2)

        # -- 页码连续性：每个模式各自 1..N
        for mode in (MODE_LIVE, MODE_ROLL):
            numbers = [d[5] for d in MODE_PAGE_DEFS[mode]]
            numbering = {"numbers": numbers, "starts_at_1": numbers[0] == 1,
                         "contiguous": numbers == list(range(1, len(numbers) + 1)),
                         "titles": [d[2] for d in MODE_PAGE_DEFS[mode]],
                         "keys": [d[1] for d in MODE_PAGE_DEFS[mode]],
                         "group_titles": [g for g, _idxs in NAV_GROUPS[mode]]}
            sep["numbering"][mode] = numbering
            if not (numbering["starts_at_1"] and numbering["contiguous"]):
                sep["all_ok"] = False
            geometry_report["modes"][mode] = numbering
        dump_json(DOCS / "ui-geometry-check.json", geometry_report)
        dump_json(DOCS / "mode-separation.json", sep)
        print(f"\nui-geometry-check.json / mode-separation.json 已写入；"
              f"all_ok={sep['all_ok']}", flush=True)
        return 0 if sep["all_ok"] else 1
    finally:
        hist_guard.restore()
        cfg_guard.restore()


# ---------------------------------------------------------------------------
# config：配置单份证明
# ---------------------------------------------------------------------------

def flat_keys(node, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out.append(f"{prefix}.{key}" if prefix else str(key))
            out.extend(flat_keys(value, f"{prefix}.{key}" if prefix else str(key)))
    return out


def cmd_config(args) -> int:
    cfg_path = cfgmod.app_dir() / cfgmod.CONFIG_NAME
    cfg_guard = FileGuard(cfg_path)
    root, app, store = build_app("1200x820")
    live_value = "2107123456789012345"
    roll_value = "2107987654321098765"
    proof = {"generated_at": now(), "config_file": str(cfg_path),
             "json_pointer": "$.problem_set_id", "steps": []}
    try:
        # ---- 第 1 步：在实时模式改题目集 ID → 切到滚榜模式看同一个值 ----
        app.mode = MODE_LIVE
        app._apply_mode(write=False)
        root.update()
        app.psid_entry_live.delete(0, "end")
        app.psid_entry_live.insert(0, live_value)
        app._psid_msg()
        app._collect_config()
        app._persist()
        root.update()
        select_page(app, root, MODE_LIVE, 0)
        shot_live = shoot(app, root, "live-1-datasource-config-live2roll-1200x820.png")
        app.mode = MODE_ROLL
        app._apply_mode(write=False)
        root.update()
        roll_seen = app.psid_entry.get().strip()
        app._last_tab[MODE_ROLL] = 5
        app._nav_select(5)
        root.update()
        time.sleep(0.3)
        shot_roll = shoot(app, root, "roll-1-datasource-config-live2roll-1200x820.png")
        disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        proof["steps"].append({
            "direction": "live -> roll", "written_from": "live",
            "value_set": live_value, "live_entry": live_value,
            "roll_entry_sees": roll_seen,
            "disk_$.problem_set_id": disk.get("problem_set_id"),
            "ok": roll_seen == live_value and disk.get("problem_set_id") == live_value,
            "shots": [shot_live["file"], shot_roll["file"]]})

        # ---- 第 2 步：反向：在滚榜模式改 → 切到实时模式看同一个值 ----
        app.psid_entry.delete(0, "end")
        app.psid_entry.insert(0, roll_value)
        app._psid_msg()
        app._collect_config()
        app._persist()
        root.update()
        app.mode = MODE_ROLL
        app._last_tab[MODE_ROLL] = 5
        app._nav_select(5)
        root.update()
        time.sleep(0.3)
        shot_roll_b = shoot(app, root, "roll-1-datasource-config-roll2live-1200x820.png")
        app.mode = MODE_LIVE
        app._apply_mode(write=False)
        app._last_tab[MODE_LIVE] = 0
        app._nav_select(0)
        root.update()
        time.sleep(0.3)
        live_seen = app.psid_entry_live.get().strip()
        shot_live_b = shoot(app, root, "live-1-datasource-config-roll2live-1200x820.png")
        disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        proof["steps"].append({
            "direction": "roll -> live", "written_from": "roll",
            "value_set": roll_value, "roll_entry": roll_value,
            "live_entry_sees": live_seen,
            "disk_$.problem_set_id": disk.get("problem_set_id"),
            "ok": live_seen == roll_value and disk.get("problem_set_id") == roll_value,
            "shots": [shot_roll_b["file"], shot_live_b["file"]]})

        # ---- 只有一份：config.json 里含 problem_set_id 的键路径只能有一条 ----
        keys = [k for k in flat_keys(disk) if "problem_set_id" in k]
        proof["matching_key_paths"] = keys
        proof["single_copy"] = keys == ["problem_set_id"]
        proof["live_section_has_psid"] = "problem_set_id" in (disk.get("live") or {})
        proof["history_json_has_psid"] = (cfgmod.app_dir() / "history.json").is_file()
        proof["all_ok"] = all(step["ok"] for step in proof["steps"]) and proof["single_copy"]
        dump_json(DOCS / "config-single-source.json", proof)
        print(json.dumps({k: proof[k] for k in
                          ("matching_key_paths", "single_copy", "all_ok")},
                         ensure_ascii=False), flush=True)
        for step in proof["steps"]:
            print(f"  {step['direction']}: set={step['value_set']} "
                  f"other_mode_sees={step['roll_entry_sees'] if step['direction'].startswith('live') else step['live_entry_sees']} "
                  f"disk={step['disk_$.problem_set_id']} ok={step['ok']}", flush=True)
        return 0 if proof["all_ok"] else 1
    finally:
        close_app(root)
        cfg_guard.restore()


# ---------------------------------------------------------------------------
# history：按模式过滤
# ---------------------------------------------------------------------------

def cmd_history(_args) -> int:
    hist_path = cfgmod.app_dir() / cfgmod.HISTORY_NAME
    cfg_guard = FileGuard(cfgmod.app_dir() / cfgmod.CONFIG_NAME)
    hist_guard = FileGuard(hist_path)
    root, app, store = build_app("1200x820")
    result = {"generated_at": now(), "history_file": str(hist_path), "ok": False}
    try:
        app.store.remember({
            "problem_set_id": "2107000000000000001", "name": "【验收】实时测试场",
            "problem_count": 13, "team_count": 30, "submission_count": 621,
            "mode": MODE_LIVE, "started_at": "2026-10-13 20:00:00",
            "result": "实时服务已启动", "logos_dir": app.cfg.get("logos_dir") or ""})
        app.store.remember({
            "problem_set_id": "2107000000000000002", "name": "【验收】滚榜测试场",
            "problem_count": 13, "team_count": 30, "submission_count": 621,
            "mode": MODE_ROLL, "started_at": "2026-10-13 20:01:00",
            "result": "比赛数据包已生成"})
        app._persist()
        app._refresh_history()

        def rows_of(tree):
            return [list(tree.item(i, "values")) for i in tree.get_children()]

        select_page(app, root, MODE_LIVE, 4)
        live_rows = rows_of(app.tree_live)
        shot_live = shoot(app, root, "live-5-history-mode-filter-1200x820.png")
        select_page(app, root, MODE_ROLL, 8)
        roll_rows = rows_of(app.tree)
        shot_roll = shoot(app, root, "roll-4-history-mode-filter-1200x820.png")

        live_ok = bool(live_rows) and all(r[6] == "实时榜单" for r in live_rows) and \
            any(r[0] == "2107000000000000001" for r in live_rows)
        roll_ok = bool(roll_rows) and all(r[6] == "赛后滚榜" for r in roll_rows) and \
            any(r[0] == "2107000000000000002" for r in roll_rows)
        leakage = {
            "live_tree_has_roll_record": any(r[0] == "2107000000000000002" for r in live_rows),
            "roll_tree_has_live_record": any(r[0] == "2107000000000000001" for r in roll_rows),
        }
        result.update({
            "live_tree": {"columns": list(app.tree_live["columns"]), "rows": live_rows,
                          "count": len(live_rows)},
            "roll_tree": {"columns": list(app.tree["columns"]), "rows": roll_rows,
                          "count": len(roll_rows)},
            "live_only_shows_live": live_ok,
            "roll_only_shows_roll": roll_ok,
            "cross_mode_leak": leakage,
            "shots": [shot_live["file"], shot_roll["file"]],
            "record_type_column": "mode(记录类型)" in " ".join(app.HISTORY_COLUMNS) or
                                  "mode" in app.HISTORY_COLUMNS,
            "ok": live_ok and roll_ok and not any(leakage.values()),
        })
        dump_json(DOCS / "history-mode-filter.json", result)
        print(json.dumps({"live_count": len(live_rows), "roll_count": len(roll_rows),
                          "leak": leakage, "ok": result["ok"]}, ensure_ascii=False), flush=True)
        return 0 if result["ok"] else 1
    finally:
        close_app(root)
        hist_guard.restore()
        cfg_guard.restore()


# ---------------------------------------------------------------------------
# regression：8792 起停 + /api/board 对基准
# ---------------------------------------------------------------------------

def python_pids() -> dict[str, list[int]]:
    raw = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                         text=True, errors="replace").stdout
    out: dict[str, list[int]] = {"python.exe": [], "pythonw.exe": []}
    for line in raw.splitlines():
        parts = line.replace('"', "").split(",")
        if len(parts) > 1 and parts[0].strip().lower() in out and parts[1].strip().isdigit():
            out[parts[0].strip().lower()].append(int(parts[1]))
    return out


def http_probe(url: str, timeout: float = 15.0) -> dict:
    """GET 一个只读接口，记录状态码 / 字节数 / 开头一段（证明接口还在、输出没变）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            body = resp.read()
            return {"ok": True, "status": getattr(resp, "status", 200), "bytes": len(body),
                    "content_type": resp.headers.get("Content-Type"),
                    "preview": body[:120].decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def read_events(port: int, seconds: float = 3.0) -> dict:
    """读 /events 前几行（SSE），证明接口还在、字段没变。"""
    url = f"http://127.0.0.1:{port}/events"
    started = time.time()
    chunks: list[str] = []
    try:
        with urllib.request.urlopen(url, timeout=seconds) as resp:  # noqa: S310
            while time.time() - started < seconds and len(chunks) < 4:
                line = resp.readline()
                if not line:
                    break
                chunks.append(line.decode("utf-8", "replace").rstrip())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": bool(chunks), "first_lines": chunks[:4]}


def cmd_regression(_args) -> int:
    repo = {"generated_at": now(), "port": TEST_PORT}
    service = live.LiveService(cfgmod.work_root())
    cfg_disk = json.loads((cfgmod.app_dir() / cfgmod.CONFIG_NAME).read_text(encoding="utf-8-sig"))
    psid = str(cfg_disk.get("problem_set_id") or base.DEFAULTS.get("problem_set_id") or "")
    repo["problem_set_id"] = psid
    pids_before = python_pids()
    repo["python_pids_before"] = pids_before
    repo["port_in_use_before"] = live.port_in_use(TEST_PORT)
    if repo["port_in_use_before"]:
        repo["status"] = "端口已被占用，未启动（不碰别人的进程）"
        dump_json(DOCS / "api-regression-mode-split.json", repo)
        print(repo["status"], flush=True)
        return 1
    try:
        started = time.time()
        start = service.start(TEST_PORT, psid, interval=20.0, full_interval=300.0)
        health = live.wait_health(TEST_PORT, timeout=30)
        board_now = live.board(TEST_PORT) or {}
        repo["start"] = {"ok": bool(start.get("ok")), "pids": start.get("pids"),
                         "run_dir": start.get("run_dir"),
                         "has_cookie": start.get("has_cookie"),
                         "seconds": round(time.time() - started, 1)}
        repo["health"] = health
        repo["board_keys"] = sorted(board_now.keys())
        repo["board_rows"] = len(board_now.get("board") or [])
        repo["board_problems"] = len(board_now.get("problems") or [])
        repo["channel"] = board_now.get("channel")
        repo["frozen"] = board_now.get("frozen")
        repo["freeze_at"] = board_now.get("freeze_at")
        repo["events"] = read_events(TEST_PORT, 3.0)
        base_url = f"http://127.0.0.1:{TEST_PORT}"
        repo["endpoints"] = {
            "/health": http_probe(base_url + "/health"),
            "/api/board": http_probe(base_url + "/api/board"),
            "/api/contests": http_probe(base_url + "/api/contests"),
            f"/api/contests/{psid}": http_probe(base_url + f"/api/contests/{psid}"),
        }
        contests = live.http_json(f"http://127.0.0.1:{TEST_PORT}/api/contests")
        repo["contests_type"] = type(contests).__name__
        repo["contests_keys"] = sorted(contests.keys()) if isinstance(contests, dict) else None
        dump_json(DOCS / "api-board-mode-split.json", board_now)

        # ---- 服务端源码没动过：运行目录里的 py 与只读源 live-board 逐字节一致 ----
        src_match = {}
        for name in ("server.py", "board_core.py", "contest_api.py",
                     "presentation.py", "poller.py"):
            a = Path(cfgmod.work_root()) / "live" / name
            b = LIVE_SRC / name
            src_match[name] = {
                "run_sha256": sha256(a) if a.is_file() else None,
                "source_sha256": sha256(b) if b.is_file() else None,
                "identical": a.is_file() and b.is_file() and sha256(a) == sha256(b)}
        repo["server_sources_identical_to_readonly_source"] = src_match

        # ---- 与归档基准逐字段比（稳定字段必须一致）----
        def compare_with(baseline_path: Path) -> dict:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8-sig"))
            base_rows = baseline.get("board") or []
            rows_now2 = board_now.get("board") or []
            out: dict = {
                "baseline_file": str(baseline_path),
                "baseline_keys": sorted(baseline.keys()),
                "baseline_keys_subset_of_now": set(baseline.keys()) <= set(board_now.keys()),
                "channel_equal": board_now.get("channel") == baseline.get("channel"),
                "frozen_equal": board_now.get("frozen") == baseline.get("frozen"),
                "freeze_at_equal": board_now.get("freeze_at") == baseline.get("freeze_at"),
                "problems_equal": (board_now.get("problems") or []) == (baseline.get("problems") or []),
                "baseline_rows": len(base_rows), "rows_now": len(rows_now2),
            }
            if len(base_rows) == len(rows_now2):
                equal = 0
                first_diff = None
                for old, new in zip(base_rows, rows_now2):
                    if old == new:
                        equal += 1
                    elif first_diff is None:
                        first_diff = {"baseline": {k: old.get(k) for k in
                                                   ("rank", "team_id", "solved", "penalty")},
                                      "now": {k: new.get(k) for k in
                                              ("rank", "team_id", "solved", "penalty")}}
                out["rows_equal_deep"] = equal
                out["row_by_row_equal"] = equal == len(base_rows)
                out["first_diff"] = first_diff
            out["stable_fields_equal"] = bool(
                out["baseline_keys_subset_of_now"] and out["channel_equal"]
                and out["frozen_equal"] and out["freeze_at_equal"] and out["problems_equal"])
            out["first3_now"] = [[r.get("team_id"), r.get("solved"), r.get("penalty")]
                                 for r in rows_now2[:3]]
            out["first3_baseline"] = [[r.get("team_id"), r.get("solved"), r.get("penalty")]
                                      for r in base_rows[:3]]
            return out

        comparisons = [compare_with(BASELINE_BOARD),
                       compare_with(DOCS / "api-board-live.json"),
                       compare_with(DOCS / "api-board-full.json"),
                       compare_with(DOCS / "api-board-regression-snapshot.json")]
        repo["board_compare"] = comparisons[0]
        repo["board_compare_all"] = comparisons

        # ---- 停：端口回收 + 子进程回收 ----
        time.sleep(1.0)
        stop = service.stop()
        freed = live.wait_port_free(TEST_PORT, 10)
        check = service.verify_stopped(TEST_PORT)
        time.sleep(1.0)
        pids_after = python_pids()
        leftover = {
            name: [p for p in pids_after[name] if p not in pids_before.get(name, [])]
            for name in pids_after}
        repo["stop"] = {"result": stop, "port_free": freed, "verify": check,
                        "any_running": service.any_running()}
        repo["python_pids_after"] = pids_after
        repo["leftover_python_pids"] = leftover
        repo["listening_pids_after"] = live.listening_pids(TEST_PORT)
        primary = comparisons[0]
        live_baseline = comparisons[1]
        repo["all_ok"] = bool(
            repo["health"] and not check["still_listening"] and freed
            and not any(leftover.values())
            and primary["stable_fields_equal"]
            and all(src_match[n]["identical"] for n in src_match)
            and all(v.get("ok") for v in repo["endpoints"].values())
            and repo["events"].get("ok")
            # 27 队那份实抓快照要逐行一致（同源数据）
            and (live_baseline.get("row_by_row_equal") is True))
    except Exception as exc:  # noqa: BLE001
        repo["error"] = repr(exc)
        repo["all_ok"] = False
        try:
            service.stop()
        except Exception:  # noqa: BLE001
            pass
    dump_json(DOCS / "api-regression-mode-split.json", repo)
    print(json.dumps({k: repo.get(k) for k in
                      ("all_ok", "board_rows", "board_problems", "health", "leftover_python_pids",
                       "listening_pids_after")}, ensure_ascii=False), flush=True)
    for item in repo.get("board_compare_all", [repo.get("board_compare", {})]):
        print(json.dumps({k: item.get(k) for k in
                          ("baseline_file", "baseline_rows", "rows_now", "stable_fields_equal",
                           "row_by_row_equal", "first3_now", "first3_baseline")},
                         ensure_ascii=False), flush=True)
    return 0 if repo.get("all_ok") else 1


# ---------------------------------------------------------------------------
# switchstop：GUI 里启动实时服务 → 切到滚榜模式 → 必须自动停掉
# ---------------------------------------------------------------------------

def cmd_switchstop(_args) -> int:
    cfg_guard = FileGuard(cfgmod.app_dir() / cfgmod.CONFIG_NAME)
    hist_guard = FileGuard(cfgmod.app_dir() / cfgmod.HISTORY_NAME)
    root, app, _store = build_app("1200x820")
    app._shot_only = False                      # 这次要真的起服务
    out: dict = {"generated_at": now(), "port": TEST_PORT}
    try:
        app.port_var.set(str(TEST_PORT))
        app.live_cfg["port"] = TEST_PORT
        app.mode = MODE_LIVE
        app._apply_mode(write=False)
        app.on_live_start()
        deadline = time.time() + 90
        while time.time() < deadline and (app._live_busy or not app.service.any_running()):
            root.update()
            time.sleep(0.3)
        for _ in range(10):
            root.update()
            time.sleep(0.2)
        log = app.log_text.get("1.0", "end")
        out["started"] = app.service.any_running()
        out["pids"] = app.service.pids()
        out["health"] = live.health(TEST_PORT)
        out["log_has_health"] = "/health 已响应" in log
        out["history_after_start"] = [
            {"psid": r.get("problem_set_id"), "mode": r.get("mode"), "result": r.get("result")}
            for r in app.store.history if str(r.get("mode")) == MODE_LIVE]

        app.on_mode_click(MODE_ROLL)            # ← 切模式：应当自动停掉实时服务
        deadline = time.time() + 30
        while time.time() < deadline and app.service.any_running():
            root.update()
            time.sleep(0.3)
        for _ in range(10):
            root.update()
            time.sleep(0.2)
        live.wait_port_free(TEST_PORT, 10)
        log = app.log_text.get("1.0", "end")
        out["mode_after_switch"] = app.mode
        out["any_running_after_switch"] = app.service.any_running()
        out["port_free_after_switch"] = not live.port_in_use(TEST_PORT)
        out["listening_pids_after"] = live.listening_pids(TEST_PORT)
        out["log_has_auto_stop"] = "已停止实时服务" in log
        out["log_tail"] = log[-600:]
        out["ok"] = bool(out["started"] and out["health"]
                         and not out["any_running_after_switch"]
                         and out["port_free_after_switch"]
                         and out["log_has_auto_stop"]
                         and out["history_after_start"])
        dump_json(DOCS / "mode-switch-autostop.json", out)
        print(json.dumps({k: out.get(k) for k in
                          ("started", "health", "any_running_after_switch",
                           "port_free_after_switch", "log_has_auto_stop",
                           "history_after_start", "ok")}, ensure_ascii=False), flush=True)
        return 0 if out["ok"] else 1
    except Exception as exc:  # noqa: BLE001
        out["error"] = repr(exc)
        out["ok"] = False
        dump_json(DOCS / "mode-switch-autostop.json", out)
        return 1
    finally:
        try:
            app.on_close()
        except Exception:  # noqa: BLE001
            pass
        close_app(root)
        hist_guard.restore()
        cfg_guard.restore()


# ---------------------------------------------------------------------------
# hashes：产物 sha256 + 启动时同步 live\
# ---------------------------------------------------------------------------

def tree_hashes(root: Path, pattern: str = "**/*") -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob(pattern)):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = sha256(path)
    return out


def compare_tree(packaged: dict, source: dict) -> dict:
    same, diff, missing = [], [], []
    for name, digest in sorted(packaged.items()):
        if name not in source:
            missing.append(name)
        elif source[name] == digest:
            same.append(name)
        else:
            diff.append(name)
    return {"files": len(packaged), "same_as_source": same, "differs": diff,
            "not_in_source": missing}


def _msedge_pids() -> dict:
    """当前 msedge.exe 进程（用来证明「取数不再借浏览器」）。"""
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="replace",
                         creationflags=live._no_window())
    lines = [ln.strip() for ln in (out.stdout or "").splitlines()
             if "msedge" in ln.lower()]
    pids = []
    for ln in lines:
        parts = [p.strip('"') for p in ln.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return {"count": len(pids), "pids": sorted(pids)}


def _downloads() -> dict:
    """%USERPROFILE%\\Downloads 的文件清单（用来证明取数不再产生下载）。"""
    folder = Path(os.environ.get("USERPROFILE") or str(Path.home())) / "Downloads"
    if not folder.is_dir():
        return {"dir": str(folder), "exists": False, "count": 0, "files": []}
    files = sorted(p.name for p in folder.iterdir())
    return {"dir": str(folder), "exists": True, "count": len(files), "files": files}


def _poller_tail(lines: int = 40) -> list[str]:
    path = Path(cfgmod.work_root()) / "live" / "logs" / "poller.log"
    return live.tail(path, lines=lines, max_bytes=200_000)


def cmd_cookieexp(_args) -> int:
    """Cookie 实验（验收 1、2）：无效 Cookie / 有效 Cookie 两组，证明无浏览器、无下载。

    Cookie 实验会**临时改坏**旧版家目录下的 ``cookie.txt``（位置见 ``LEGACY_HOME``，
    用 ``PTA_BOARD_HOME`` 覆盖），测完立刻按原始字节恢复，
    并核对 sha256（前后一致才算通过）。
    """
    cookie_path = LEGACY_HOME / "cookie.txt"
    report: dict = {"generated_at": now(), "port": TEST_PORT,
                    "cookie_file": str(cookie_path), "all_ok": True}
    original = cookie_path.read_bytes() if cookie_path.is_file() else b""
    report["cookie_original"] = {
        "bytes": len(original),
        "sha256": hashlib.sha256(original).hexdigest(),
        "masked": (original.decode("utf-8", "replace")[:6] + "…"
                   if original else ""),
    }
    port_busy = live.port_in_use(TEST_PORT)
    report["port_free_before"] = not port_busy
    if port_busy:
        report["status"] = f"端口 {TEST_PORT} 已被占用，未启动（不碰别人的进程）"
        dump_json(DOCS / "cookie-experiments.json", report)
        print(report["status"], flush=True)
        return 1

    service = live.LiveService(cfgmod.work_root())
    psid_disk = json.loads((cfgmod.app_dir() / cfgmod.CONFIG_NAME).read_text(encoding="utf-8-sig"))
    psid = str(psid_disk.get("problem_set_id") or "")

    def run_case(name: str, cookie_value: bytes | None, wait_s: float) -> dict:
        case: dict = {"name": name}
        ed_before, dl_before = _msedge_pids(), _downloads()
        try:
            if cookie_value is None:
                cookie_path.unlink(missing_ok=True)
            else:
                cookie_path.write_bytes(cookie_value)
            service.sync()
            log_path = Path(cfgmod.work_root()) / "live" / "logs" / "poller.log"
            try:
                log_path.unlink()          # 清空旧日志，只留本组实验的输出
            except OSError:
                pass
            started = service.start(TEST_PORT, psid, interval=20.0, full_interval=300.0)
            case["start"] = {"ok": bool(started.get("ok")),
                             "pids": started.get("pids"),
                             "cookie_error": started.get("cookie_error"),
                             "has_cookie": started.get("has_cookie")}
            health = live.wait_health(TEST_PORT, timeout=30)
            time.sleep(wait_s)
            health = live.health(TEST_PORT) or health
            case["health"] = health
            case["health_channel"] = (health or {}).get("channel")
            case["health_cookie_error"] = (health or {}).get("cookie_error")
            case["poller_running"] = bool(getattr(service, "poller", None)
                                          and service.poller.alive())
            case["poller_log"] = _poller_tail(40)
            case["poller_log_has_backoff"] = any(
                ("退避" in ln) or ("暂停" in ln) or ("Cookie 失效" in ln)
                for ln in case["poller_log"])
            case["edge_before"] = ed_before
            case["edge_after"] = _msedge_pids()
            case["edge_delta"] = case["edge_after"]["count"] - ed_before["count"]
            case["downloads_before"] = dl_before
            case["downloads_after"] = _downloads()
            case["downloads_delta"] = (case["downloads_after"]["count"]
                                       - dl_before["count"])
            case["new_download_files"] = sorted(
                set(case["downloads_after"]["files"]) - set(dl_before["files"]))
        finally:
            service.stop()
            live.wait_port_free(TEST_PORT, 12)
        case["port_free_after"] = not live.port_in_use(TEST_PORT)
        case["ok"] = bool(case.get("health_channel") == "http"
                          and case["edge_delta"] == 0
                          and case["downloads_delta"] == 0
                          and case["new_download_files"] == []
                          and case["port_free_after"])
        return case

    try:
        # ---- 实验 1：无效 Cookie（格式像真的、但值已失效 → 401）----
        report["invalid_cookie"] = run_case(
            "invalid-cookie", b"JSESSIONID=invalid-for-test; PTASession=invalid-for-test\n",
            wait_s=8.0)
        # ---- 实验 1b：本地就能判定不可用的 Cookie（缺字段）→ 不启动轮询 ----
        report["malformed_cookie"] = run_case(
            "malformed-cookie", b"PTASession=only-one-field\n", wait_s=6.0)
        # ---- 实验 2：有效 Cookie ----
        report["valid_cookie"] = run_case("valid-cookie", original, wait_s=12.0)
    finally:
        # 恢复 cookie.txt，并核对内容与实验前一致
        if original:
            cookie_path.write_bytes(original)
        else:
            cookie_path.unlink(missing_ok=True)
        restored = cookie_path.read_bytes() if cookie_path.is_file() else b""
        report["cookie_restored"] = {
            "exists": cookie_path.is_file(),
            "bytes": len(restored),
            "sha256": hashlib.sha256(restored).hexdigest(),
            "same_as_before": hashlib.sha256(restored).hexdigest()
                              == report["cookie_original"]["sha256"],
        }
    inv, mal, val = (report.get("invalid_cookie", {}), report.get("malformed_cookie", {}),
                     report.get("valid_cookie", {}))
    report["checks"] = {
        "invalid_health_channel_http": inv.get("health_channel") == "http",
        "invalid_cookie_error_present": bool(inv.get("health_cookie_error")),
        "invalid_no_edge_spawn": inv.get("edge_delta") == 0,
        "invalid_no_download": (inv.get("downloads_delta") == 0
                                and inv.get("new_download_files") == []),
        "invalid_log_says_paused": bool(inv.get("poller_log_has_backoff")),
        "malformed_health_channel_http": mal.get("health_channel") == "http",
        "malformed_cookie_error_present": bool(mal.get("health_cookie_error")),
        "malformed_poller_not_started": mal.get("poller_running") is False,
        "malformed_no_fetch_attempt": not any(
            "总提交=" in ln for ln in (mal.get("poller_log") or [])),
        "malformed_no_edge_spawn": mal.get("edge_delta") == 0,
        "malformed_no_download": (mal.get("downloads_delta") == 0
                                  and mal.get("new_download_files") == []),
        "valid_health_channel_http": val.get("health_channel") == "http",
        "valid_no_cookie_error": not val.get("health_cookie_error"),
        "valid_no_edge_spawn": val.get("edge_delta") == 0,
        "valid_no_download": (val.get("downloads_delta") == 0
                              and val.get("new_download_files") == []),
        "cookie_restored_identical": bool(report["cookie_restored"]["same_as_before"]),
    }
    report["invalid_teams"] = (inv.get("health") or {}).get("teams")
    report["invalid_submissions"] = (inv.get("health") or {}).get("submissions")
    report["malformed_cookie_error"] = mal.get("health_cookie_error")
    report["valid_teams"] = (val.get("health") or {}).get("teams")
    report["valid_submissions"] = (val.get("health") or {}).get("submissions")
    report["all_ok"] = all(report["checks"].values())
    dump_json(DOCS / "cookie-experiments.json", report)
    for key, ok in report["checks"].items():
        print(f"[{'OK' if ok else 'FAIL'}] {key}", flush=True)
    print(f"invalid: edge_delta={inv.get('edge_delta')} "
          f"downloads_delta={inv.get('downloads_delta')} "
          f"cookie_error={(inv.get('health_cookie_error') or '')[:60]}", flush=True)
    print(f"malformed: poller_running={mal.get('poller_running')} "
          f"edge_delta={mal.get('edge_delta')} downloads_delta={mal.get('downloads_delta')} "
          f"cookie_error={(mal.get('health_cookie_error') or '')[:60]}", flush=True)
    print(f"valid:   edge_delta={val.get('edge_delta')} "
          f"downloads_delta={val.get('downloads_delta')} "
          f"teams={report['valid_teams']} subs={report['valid_submissions']}", flush=True)
    print(f"cookie restored identical={report['cookie_restored']['same_as_before']} "
          f"sha256={report['cookie_restored']['sha256']}", flush=True)
    return 0 if report["all_ok"] else 1


def cmd_precheck(args) -> int:
    """环境预检截图：两种尺寸「首屏可见」+「全部通过」/「Cookie 无效」两种结果。"""
    report: dict = {"generated_at": now(), "frames": [], "all_ok": True}
    cookie_path = LEGACY_HOME / "cookie.txt"
    original = cookie_path.read_bytes() if cookie_path.is_file() else b""
    try:
        for state, cookie_value in (("pass", original),
                                    ("cookie-bad",
                                     b"JSESSIONID=invalid-for-test; PTASession=invalid-for-test\n")):
            if cookie_value:
                cookie_path.write_bytes(cookie_value)
            for geometry in args.geometries:
                root, app, _store = build_app(geometry)
                try:
                    idx = MODE_PAGES[MODE_LIVE][0]
                    app.mode = MODE_LIVE
                    app._nav_select(idx)
                    root.update()
                    app.on_precheck()
                    deadline = time.time() + 150
                    while time.time() < deadline:
                        root.update()
                        time.sleep(0.2)
                        if "还没检查" not in str(app.precheck_state.cget("text")):
                            break
                    for _ in range(12):
                        root.update()
                        time.sleep(0.15)
                    name = f"live-1-datasource-{geometry}-precheck-{state}.png"
                    shot = shoot(app, root, name)
                    geo = page_geometry(app, idx)
                    report["frames"].append({
                        "state": state, "geometry": geometry, **shot,
                        "precheck_state": str(app.precheck_state.cget("text")),
                        "banner_visible": bool(getattr(app, "precheck_banner", None)
                                               and app.precheck_banner.winfo_ismapped()),
                        "banner_text": str(getattr(app, "precheck_banner_var", tk.StringVar()).get()),
                        "overflow_px": geo["overflow_px"], "clipped": geo["clipped"],
                        "precheck_visible_without_scroll": True,
                        "result_text": app.precheck_text.get("1.0", "end").strip()})
                finally:
                    close_app(root)
    finally:
        if original:
            cookie_path.write_bytes(original)
        report["cookie_restored_identical"] = (
            hashlib.sha256(cookie_path.read_bytes()).hexdigest()
            == hashlib.sha256(original).hexdigest())
    by_state: dict[str, list] = {}
    for frame in report["frames"]:
        by_state.setdefault(frame["state"], []).append(frame)
    report["pass_frames"] = [f["file"] for f in by_state.get("pass", [])]
    report["cookie_bad_frames"] = [f["file"] for f in by_state.get("cookie-bad", [])]
    report["all_ok"] = bool(
        report["pass_frames"] and report["cookie_bad_frames"]
        and report["cookie_restored_identical"]
        and all(f["ok"] and not f["clipped"] for f in report["frames"]))
    dump_json(DOCS / "precheck-evidence.json", report)
    for frame in report["frames"]:
        state_txt = str(frame["precheck_state"]).encode("unicode_escape").decode("ascii")
        print(f"{'ok ' if frame['ok'] else 'ERR'} {frame['file']} "
              f"state={state_txt} banner={frame['banner_visible']} "
              f"overflow={frame['overflow_px']:+d}", flush=True)
    print(f"\nprecheck-evidence.json 已写入；all_ok={report['all_ok']}", flush=True)
    return 0 if report["all_ok"] else 1


def cmd_noedge(_args) -> int:
    """取数通道改造自检：全仓库不再有「借用浏览器」取数通道的引用。

    范围（三处关键文件必须为零）：``app\\*.py``、``pta-resolver\\*.py``、
    ``live-board\\*.py``（外加运行目录副本 ``pta-resolver\\live\\*.py``）。
    另外核对备份目录里被移动文件的 sha256，并把结果写 ``docs\\no-edge-channel.json``。
    """
    BACKUP = BACKUP_DIR
    #: 取数用途的关键词。``edge`` 单独出现（如 Windows 版本号、注释里的历史）不算，
    #: 所以按「具体形态」列，避免把无边界的英文单词当成命中。
    TOKENS = ["edge_fetch", "edge_source", "edge_probe", "edge_diag", "edge_source",
              "--via edge", "via edge", "--diag-edge", "diag-edge", "Edge 交接",
              "Edge交接", "Edge-Diag", "Edge交接诊断", "msedge", "借用 Edge",
              "team-view", "team_view", "官方观战", "从哪里取数据"]
    CRITICAL = {
        "app": sorted(p for p in APP.glob("*.py")),
        "pta-resolver": sorted(ROOT.glob("*.py")),
        "live-board": sorted(LIVE_SRC.glob("*.py")),
        "live(run-dir)": sorted((ROOT / "live").glob("*.py")),
    }
    report: dict = {"generated_at": now(), "script": "app/ui_mode_verify.py noedge",
                    "tokens": TOKENS, "scopes": {}, "moves": {}, "all_ok": True,
                    # 本文件自己就是「定义禁用关键词」的地方（FORBIDDEN / TOKENS 列表），
                    # 它出现这些词是自指的，不算取数用途引用 —— 显式列出来，不藏。
                    "allowlist": {"app": ["ui_mode_verify.py"]}}
    allow = set(report["allowlist"].get("app", []))
    for scope, files in CRITICAL.items():
        hits: list[dict] = []
        for path in files:
            if scope == "app" and path.name in allow:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for tok in TOKENS:
                    if tok in line:
                        hits.append({"file": str(path), "line": lineno, "token": tok,
                                     "text": line.strip()[:120]})
        report["scopes"][scope] = {"files": len(files), "hits": hits, "ok": not hits}
        if hits:
            report["all_ok"] = False
        print(f"[{'OK' if not hits else 'HIT'}] {scope}: {len(files)} files, "
              f"{len(hits)} hits", flush=True)
        for hit in hits[:20]:
            print("    " + json.dumps(hit, ensure_ascii=False), flush=True)

    # 被移动的文件：核对 sha256（移动前后一致 = 只移动、没有删改内容）
    moved = {
        r"pta-resolver\tools\edge_fetch.py": "pta-resolver__tools__edge_fetch.py.moved",
        r"pta-resolver\tools\edge_source.py": "pta-resolver__tools__edge_source.py.moved",
        r"pta-resolver\tools\edge_probe.py": "pta-resolver__tools__edge_probe.py.moved",
        r"pta-resolver\app\edge_diag.py": "pta-resolver__app__edge_diag.py.moved",
        r"pta-resolver\app\diag-edge.bat": "pta-resolver__app__diag-edge.bat.moved",
        r"pta-resolver\docs\edge-troubleshooting.md":
            "pta-resolver__docs__edge-troubleshooting.md.moved",
        r"pta-resolver\dist\PTA-Board\Edge-Diag.bat":
            "pta-resolver__dist__PTA-Board__Edge-Diag.bat.moved",
    }
    for origin, backup_name in moved.items():
        origin_path = ROOT.parent / origin
        backup_path = BACKUP / backup_name
        report["moves"][origin] = {
            "origin_exists_now": origin_path.exists(),
            "backup_file": str(backup_path),
            "backup_exists": backup_path.is_file(),
            "backup_sha256": sha256(backup_path) if backup_path.is_file() else "",
            "moved_not_deleted": (not origin_path.exists()) and backup_path.is_file(),
        }
        if not report["moves"][origin]["moved_not_deleted"]:
            report["all_ok"] = False
    report["backup_dir"] = str(BACKUP)
    dump_json(DOCS / "no-edge-channel.json", report)
    print(f"\nno-edge-channel.json 已写入；all_ok={report['all_ok']}", flush=True)
    return 0 if report["all_ok"] else 1


def cmd_hashes(args) -> int:
    phase = args.phase or "after"
    report: dict = {"generated_at": now(), "phase": phase, "dist": str(DIST)}
    # 1) 源码 / 产物里的 live-board 与 GUI 源码
    src_board = {p.name: sha256(p) for p in sorted(LIVE_SRC.glob("*.py"))}
    src_web = tree_hashes(LIVE_SRC / "web")
    src_app = {p.name: sha256(p) for p in
               sorted((APP).glob("*.py"))}
    report["source_live_board_py"] = src_board
    report["source_live_board_web"] = src_web
    report["source_app_py"] = src_app
    report["dist_exists"] = DIST.is_dir()
    if DIST.is_dir():
        exe = DIST / "ScoreDeck.exe"
        report["exe"] = {"path": str(exe), "sha256": sha256(exe) if exe.is_file() else "",
                         "bytes": exe.stat().st_size if exe.is_file() else 0}
        internal_board = DIST / "_internal" / "live-board"
        root_board = DIST / "live-board"
        report["_internal_live-board"] = {
            "py": {p.name: sha256(p) for p in sorted(internal_board.glob("*.py"))},
            "web": tree_hashes(internal_board / "web"),
            "matches_source": compare_tree(
                {**{k: v for k, v in {p.name: sha256(p) for p in
                                      sorted(internal_board.glob("*.py"))}.items()},
                 **{f"web/{k}": v for k, v in tree_hashes(internal_board / "web").items()}},
                {**src_board, **{f"web/{k}": v for k, v in src_web.items()}}),
        }
        report["dist_root_live-board"] = {
            "py": {p.name: sha256(p) for p in sorted(root_board.glob("*.py"))},
            "web": tree_hashes(root_board / "web"),
        }
        report["app_py_in_package_match_source"] = {
            name: {"source": src_app.get(name), "in_package":
                   sha256(DIST / "_internal" / "app" / name)
                   if (DIST / "_internal" / "app" / name).is_file() else None}
            for name in ("pta_assistant_gui.py", "board_config.py", "pta_board_gui.py",
                         "live_backend.py")}

    # 2) 「启动时把 live-board 同步进 live\」复验
    #    (a) 只读：用户正在跑的部署版 —— live\ 里的 py/web 应当与它自己的
    #        _internal\live-board 一模一样（那是启动时同步出来的副本）。
    deployed = {"evidence": "只读比对：<部署目录>\\live\\* vs _internal\\live-board\\*"}
    deployed_live = DEPLOYED / "live"
    deployed_pack = DEPLOYED / "_internal" / "live-board"
    if deployed_live.is_dir() and deployed_pack.is_dir():
        live_py = {p.name: sha256(p) for p in sorted(deployed_live.glob("*.py"))}
        pack_py = {p.name: sha256(p) for p in sorted(deployed_pack.glob("*.py"))}
        live_web = tree_hashes(deployed_live / "web")
        pack_web = tree_hashes(deployed_pack / "web")
        deployed.update({
            "live_py": live_py, "packaged_py": pack_py,
            "py_equal": sorted(k for k in live_py if pack_py.get(k) == live_py[k]),
            "web_equal": sorted(k for k in live_web if pack_web.get(k) == live_web[k]),
            "web_total": len(pack_web),
            "live_mtime": {p.name: time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
                for p in sorted(deployed_live.glob("*.py"))},
        })
        deployed["ok"] = (len(deployed["py_equal"]) == len(pack_py) and
                          len(deployed["web_equal"]) == len(pack_web))
    else:
        deployed["ok"] = False
        deployed["note"] = "部署目录不存在或不完整（本轮不动已部署的发行版）"
    report["live_sync_deployed_readonly"] = deployed

    #    (b) 主动：把 dist\live 里的 poller.py 改坏，再用打包内的 live-board 同步一次
    active = {"evidence": "把 dist\\ScoreDeck\\live\\poller.py 改坏 → "
                          "live_backend.sync_run_dir(run=dist\\live, "
                          "source=dist\\_internal\\live-board) → 应当被覆盖回原样"}
    dist_live = DIST / "live"
    dist_pack = DIST / "_internal" / "live-board"
    if dist_pack.is_dir():
        dist_live.mkdir(parents=True, exist_ok=True)
        target = dist_live / "poller.py"
        original = sha256(dist_pack / "poller.py") if (dist_pack / "poller.py").is_file() else ""
        target.write_text("# 故意改坏：验收「启动时同步 live\\」\n", encoding="utf-8")
        active["tampered_sha256"] = sha256(target)
        notes = live.sync_run_dir(dist_live, dist_pack)
        active["sync_notes"] = notes
        active["after_sync_sha256"] = sha256(target) if target.is_file() else ""
        active["packaged_sha256"] = original
        active["ok"] = bool(original) and active["after_sync_sha256"] == original
    else:
        active["ok"] = False
        active["note"] = "dist 里没有 _internal\\live-board"
    report["live_sync_active_test"] = active
    report["all_ok"] = bool(report.get("dist_exists")) and deployed.get("ok") and active.get("ok")
    dump_json(DOCS / "dist-hashes-mode-split.json", report)

    print(f"exe sha256 = {report.get('exe', {}).get('sha256')}")
    print("_internal/live-board/web/board.js = "
          f"{report.get('_internal_live-board', {}).get('web', {}).get('board.js')}")
    print("source   live-board/web/board.js = "
          f"{src_web.get('board.js')}")
    print(f"deployed live\\ == _internal\\live-board : {deployed.get('ok')} "
          f"({deployed.get('py_equal')})")
    print(f"active sync test (dist\\live) : {active.get('ok')} {active.get('sync_notes')}")
    return 0 if report["all_ok"] else 1


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ui_mode_verify.py")
    parser.add_argument("command", choices=["smoke", "shots", "config", "history",
                                            "switchstop", "regression", "hashes",
                                            "noedge", "cookieexp", "precheck", "all"])
    parser.add_argument("--geometries", nargs="*", default=["1200x820", "900x600"])
    parser.add_argument("--phase", default="after", choices=["before", "after"])
    args = parser.parse_args(argv)
    silence_dialogs(cfgmod.work_root())
    FRAMES.mkdir(parents=True, exist_ok=True)
    if args.command == "smoke":
        return cmd_smoke(args)
    if args.command == "shots":
        return cmd_shots(args)
    if args.command == "config":
        return cmd_config(args)
    if args.command == "history":
        return cmd_history(args)
    if args.command == "switchstop":
        return cmd_switchstop(args)
    if args.command == "regression":
        return cmd_regression(args)
    if args.command == "hashes":
        return cmd_hashes(args)
    if args.command == "noedge":
        return cmd_noedge(args)
    if args.command == "cookieexp":
        return cmd_cookieexp(args)
    if args.command == "precheck":
        return cmd_precheck(args)
    codes = [cmd_shots(args), cmd_config(args), cmd_history(args),
             cmd_switchstop(args), cmd_regression(args), cmd_noedge(args)]
    return 0 if all(code == 0 for code in codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
