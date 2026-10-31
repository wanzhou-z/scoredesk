# -*- coding: utf-8 -*-
"""pta_assistant_gui.py —— PTA 比赛助手（合并版外壳：**两个模式各自独立**）。

一个窗口、两种模式，但**页签完全分开**
--------------------------------------
    实时榜单（比赛中）  ⇄  赛后滚榜

* 点「实时榜单（比赛中）」→ 左栏只有实时榜单要用的 5 页：
  ``1 数据源 / 2 外观 / 3 服务 / 4 直播 / 5 历史``；
* 点「赛后滚榜」→ 左栏整栏换成滚榜要用的 4 页：
  ``1 数据源 / 2 外观 / 3 滚榜 / 4 历史``；
* 左栏**没有**「共用」这种分组，也不会两个模式的页签混在一起；页码各自从 1 开始，
  每个模式各自记住上次停留的那一页。

两个模式仍然**只有一份设置**（``app\\config.json``）：题目集 ID / Cookie / 端口 /
照片与校徽目录 / 配色都只存一份，两边都能改，改哪边都即时生效。
（M13：滚榜模式不再让用户挑中文字体 —— 后端在 ``font_path`` 为空/失效时自动回落到
内置字体；实时榜单「2 外观」页的**榜单页字体**是另一回事，仍然保留。）

* **实时榜单**：复用外部 live-board 源目录的 poller / server（**只读**，
  不改那边任何文件），在可写运行目录里跑一份副本，20 秒增量轮询，
  给现场投屏用的榜单页（``/board`` 及其查询参数）。
* **赛后滚榜**：完全沿用 ``pta_board_gui.BoardApp`` 已有逻辑
  （生成比赛包 / 奖项表 / 三种播放模式 / 开始滚榜 / Esc 守护）。

用法
----
    python app\\pta_assistant_gui.py                  # 打开上次用的模式
    python app\\pta_assistant_gui.py --mode live      # 直接进实时榜单
    python app\\pta_assistant_gui.py --mode roll      # 直接进赛后滚榜
    python app\\pta_assistant_gui.py --headless-shot  # 无头截图（验收用）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_backend as backend        # noqa: E402
import board_config as cfgmod          # noqa: E402
import live_backend as live            # noqa: E402
import pta_board_gui as base           # noqa: E402
import star_awards                     # noqa: E402
from pta_board_gui import combo_width_chars  # noqa: E402  （M16：从已删的 star_awards_panel 搬来的）

UI_FONT = base.UI_FONT
BoardApp = base.BoardApp
ToolTip = base.ToolTip
_NavAdapter = base._NavAdapter

APP_TITLE = "PTA 比赛助手（实时榜单 + 赛后滚榜）"

# ---------------------------------------------------------------------------
# 非交互模式的「一律不弹窗」开关
# ---------------------------------------------------------------------------
#: True 时所有 messagebox 调用都只写 logs\gui-errors.log，绝不弹模态框。
#: 由 main() 在 --headless-shot / --no-dialog 生效时统一置位，用的不是
#: 实例属性而是**模块级**开关：这样连 assistant 之外（base 复用进来的
#: 代码路径）的弹窗调用也一起被压住。模态框会把无人值守的截图/自检进程
#: 永久卡死，所以这个开关必须是「进流程就生效」，不能靠各调用点自己判断。
_SILENT_DIALOGS = False


def silence_dialogs(root_dir: Path) -> None:
    """把 tkinter.messagebox 全部换成只写日志的实现（就地生效）。

    **必须同时盖两层**（2026-10-16 修）：
    1. 本模块的 ``messagebox`` 名字 → 换成 stub（原来只有这一层）；
    2. **真正的 ``tkinter.messagebox`` 模块对象上的函数** → 换成同一批 stub。
       其它模块（例如 ``pta_board_gui._install_excepthook`` 里的 ``messagebox.showerror``）
       在 import 时就拿到了那个模块对象，只改本模块的 globals / ``sys.modules``
       **盖不住它们** —— 这就是 ``--headless-shot`` 截图写失败时还会弹
       「程序出错了」模态框的原因（无人值守进程被卡死）。
    """
    global _SILENT_DIALOGS
    _SILENT_DIALOGS = True

    def _write(title: str, message: object, kind: str) -> None:
        line = (f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [dialog/{kind}] "
                f"{title}: {message}\n")
        try:
            (Path(root_dir) / "logs").mkdir(parents=True, exist_ok=True)
            with (Path(root_dir) / "logs" / "gui-errors.log").open(
                    "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:  # noqa: BLE001 - 日志写不进去也不能反过来弹窗
            pass
        try:
            sys.stderr.write(line)
        except Exception:  # noqa: BLE001
            pass

    def _mk(kind: str):
        def _fn(title: object = "", message: object = "", **_kw: object) -> str:
            _write(str(title), message, kind)
            # askyesno/askokcancel 在非交互模式下按「否」处理：
            # 不做带副作用的动作（例如强杀别人的服务）才是安全默认。
            return "no" if kind.startswith("ask") else "ok"
        return _fn

    # 直接替换整个模块对象：tkinter 内部会取 tkinter.messagebox.commondialog，
    # 逐属性打补丁盖不住那条路径。
    import types
    stub = types.ModuleType("tkinter.messagebox")
    _names = ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel",
              "askquestion", "askretrycancel", "askyesnocancel")
    for _name in _names:
        setattr(stub, _name, _mk(_name.replace("show", "") or "info"))
    stub.showinfo = _mk("info")
    stub.showwarning = _mk("warning")
    stub.showerror = _mk("error")
    stub.ABORT = stub.RETRY = stub.IGNORE = stub.OK = stub.CANCEL = stub.YES = stub.NO = "x"

    # ② 真正的模块对象上的函数一起换掉（其它模块持有的是这个对象）
    try:
        import tkinter.messagebox as _real_mb
        for _name in _names:
            setattr(_real_mb, _name, getattr(stub, _name))
        for _name in ("ABORT", "RETRY", "IGNORE", "OK", "CANCEL", "YES", "NO"):
            setattr(_real_mb, _name, "x")
        # 留个标记：别的模块可以据此判断「已经被静默了」
        _real_mb._pta_silenced = True          # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    # ① 本模块的名字也换掉（保持老行为：连 tkinter 内部那条路径都盖住）
    globals()["messagebox"] = stub
    try:
        sys.modules["tkinter.messagebox"] = stub
    except Exception:  # noqa: BLE001
        pass


MODE_LIVE = "live"
MODE_ROLL = "roll"
MODE_LABELS = {MODE_LIVE: "实时榜单（比赛中）", MODE_ROLL: "赛后滚榜"}
MODE_SHORT = {MODE_LIVE: "实时榜单", MODE_ROLL: "赛后滚榜"}

#: 页面表。``idx`` 是 **建设顺序**（= _page_frames 的下标，绝不能动）。
#: 第 4 项「一句话说明」和第 5 项「这页怎么用」都**按模式各写一份**：
#: 实时榜单只讲实时要用的事，赛后滚榜只讲滚榜要用的事，两边互不出现对方的词。
#: 第 6 项是该页在**本模式内**的显示序号（每个模式各自从 1 开始）。
LIVE_PAGE_DEFS = (
    # (idx, key, 标题, 一句话说明, 这页怎么用, 本模式内的显示序号)
    (0, "datasource", "数据源",
     "实时榜单的全部输入：平台、环境预检、题目集 / 比赛 ID、Cookie、端口",
     "① 最上面选平台（PTA / VJudge）并点「开始检查」，确认这台机器能跑"
     "　② 填 ID（PTA = 题目集网址里 /problem-sets/ 后那串数字；VJudge = 862258 这种比赛 ID）"
     "　③ PTA 必须更新 Cookie；VJudge 公开赛可留空"
     "　④ 点「4 服务」页的「启动」",
     1),
    # ── VJudge 版第一步新增：队伍备注（建设顺序 idx=9，侧栏显示在「数据源」后面）──
    (9, "teammemo", "队伍备注",
     "给 VJudge 选手填真实姓名 / 校徽 / 备注，榜单上就显示备注名",
     "① 先到「1 数据源」拉一次数据"
     "　② 双击表格里的单元格改「备注姓名 / 校徽 / 备注」"
     "　③ 改完点「重新拉取数据」或等下一轮轮询，榜单上就显示备注名"
     "　（只有平台 = VJudge 时这一页可用）",
     2),
    (1, "display", "外观",
     "榜单页长什么样：榜单页字体、13 题配色、奖项区块金/银/铜、校徽目录",
     "① 选榜单页字体（中文推荐 yahei）"
     "　② 13 个色块点一下就能改颜色"
     "　③ 奖项区块金/银/铜不用改也能用；奖项清单来自滚榜那边的奖项表"
     "　④ 校徽目录留空就用默认目录",
     3),
    (2, "service", "服务",
     "比赛中用：启动 / 停止实时榜单服务（就是给投屏看的那个网页）",
     "① 点「启动」　② 状态变成「运行中」后，去「直播」页复制网址"
     "　③ 比赛结束点「停止」",
     4),
    (3, "broadcast", "直播",
     "比赛中用：调榜单显示效果，把网址交给 OBS 或浏览器",
     "① 调榜单参数（一屏几行、要不要来回滚动）"
     "　② 点「复制 URL」　③ 粘到 OBS 的「浏览器」源里，或直接开浏览器",
     5),
    (4, "history", "历史",
     "只列「实时榜单」模式的运行记录（滚榜的记录在滚榜模式的历史页）",
     "双击某一行 = 载入那场比赛的题目集 ID",
     6),
)

ROLL_PAGE_DEFS = (
    (5, "datasource", "数据源",
     "赛后滚榜的全部输入：题目集 ID、Cookie、拉取数据、抓取结果",
     "① 填题目集 ID　② 点「拉取数据」把这场比赛抓到本地"
     "　③ 抓完到「滚榜」页生成比赛包",
     1),
    (6, "display", "外观",
     "滚榜画面长什么样：奖项表 + 全队标记、队伍照片目录、校徽目录",
     "① 奖项表：一行 = 一个奖项（名字随便写），名单在下面「全队标记」里勾"
     "　② 队伍照片 / 校徽目录留空就用默认目录"
     "　③ 中文用内置字体，不用设置",
     2),
    (7, "roll", "滚榜",
     "赛后用：比赛包放到哪、三种播放方式、生成并开始 / 停止滚榜",
     "① 确认「比赛包放到哪」　② 选「播放方式」"
     "　③ 点「生成比赛数据包」　④ 点绿色大按钮「▶ 开始滚榜」"
     "　⑤ 播放中用 p 暂停、2 快进、+/- 调速、Esc 退出",
     3),
    (8, "history", "历史",
     "只列「赛后滚榜」模式的运行记录（实时榜单的记录在实时模式的历史页）",
     "双击某一行 = 载入那场比赛的设置（题目集 ID 一起帮你填好）",
     4),
)

#: 平表：**下标 = 建设顺序 = 页面 idx**。实时榜单 6 页（0..4 + 9 队伍备注），
#: 赛后滚榜 4 页（5..8）。这里按 idx 排序，保证 ``PAGE_DEFS[idx]`` 取到的是
#: 第 idx 页（侧栏顺序另由 ``MODE_PAGES`` 决定，见 LIVE_PAGE_DEFS 的排列顺序）。
PAGE_DEFS = tuple(sorted(LIVE_PAGE_DEFS + ROLL_PAGE_DEFS, key=lambda item: item[0]))

PAGES = tuple((key, title, sub) for _i, key, title, sub, _steps, _no in PAGE_DEFS)

MODE_PAGE_DEFS = {MODE_LIVE: LIVE_PAGE_DEFS, MODE_ROLL: ROLL_PAGE_DEFS}

#: 模式 → 该模式的页面 idx（顺序 = 侧栏顺序）。
MODE_PAGES = {
    MODE_LIVE: [item[0] for item in LIVE_PAGE_DEFS],
    MODE_ROLL: [item[0] for item in ROLL_PAGE_DEFS],
}

#: 侧栏分组：**每个模式只有自己的那一组**，组标题就是模式名。
#: 左栏里不会再出现「共用」「两种模式」这类字样；显示序号各自从 1 开始
#: （由 ``_rebuild_nav`` 按组内顺序重新编号）。
NAV_GROUPS = {
    MODE_LIVE: ((MODE_LABELS[MODE_LIVE], tuple(MODE_PAGES[MODE_LIVE])),),
    MODE_ROLL: ((MODE_LABELS[MODE_ROLL], tuple(MODE_PAGES[MODE_ROLL])),),
}

DEFAULT_PROBLEM_COLORS = [
    "#FFFFFF", "#000000", "#7FE3E8", "#7BE84A", "#2B3CE8", "#FFF045",
    "#B9BFC6", "#B32438", "#FF9A2E", "#7A7A00", "#E94BD8", "#A9742B", "#00A5A5",
]
DEFAULT_MEDAL_COLORS = ["#D4AF37", "#A8B0B8", "#CD7F32"]

#: live 段默认值（写进 app\\config.json，用户能直接看/改）
LIVE_DEFAULTS = {
    "port": live.DEFAULT_PORT,
    "channel": "http",
    "poll_interval": 5,
    "full_check_interval": 120,
    "freeze_mode": "audience",
    "request_count": 0,
    "last_fetch": "",
    "last_poll": "",
    "last_board_mtime": 0.0,
    "view": {
        "overlay": 0, "top": 0, "rows": 13, "scale": 1, "font": "yahei",
        "anim": 1, "scroll": 1, "scroll_speed": 24, "scroll_pause": 2,
        "title": "", "freeze": "audience",
    },
    "problem_colors": list(DEFAULT_PROBLEM_COLORS),
    "medal_colors": list(DEFAULT_MEDAL_COLORS),
    "awards_cache": [],
    "awards_source": "",
}

#: 取数方式**只有一条**：直连接口（需要 Cookie）。2026-10-13 起删除「借用浏览器」
#: 通道 —— 它会借用户已登录的浏览器产生下载、影响观感。
#: M17e：「模拟数据（离线）」的 GUI 入口已删除（用户用不上）。命令行工具没删：
#: `live-board\start-mock.bat` 与 `tools\mock_source.py` 仍可单独跑，见 docs 说明。

CHANNEL_LINE = "取数方式：直连接口（需要 Cookie）"
CHANNEL_HELP = ("本程序只有这一条取数路径：用 cookie.txt 里的登录态直接请求 PTA 接口。"
                "Cookie 失效时会**停止取数并提示更新**，不会去动浏览器、也不会产生下载。")
VIEW_PRESETS = {
    # 「全榜静态」：一屏放完、不滚动、不要动效（队伍少时最好看）。
    "static": {"rows": 0, "scroll": 0, "anim": 0},
    # 「前 12 名来回滚动」：一屏 12 行 + 来回滚动 + 动效（队伍多时用）。
    "pingpong": {"rows": 12, "scroll": 1, "anim": 1},
}
#: 预设按钮 / 状态栏 / 日志共用的中文名字（界面上不再重复拼字符串）。
PRESET_LABELS = {
    "static": "全榜静态（不滚动，适合队伍少）",
    "pingpong": "前 12 名来回滚动（队伍多时用）",
}


# ==========================================================================
# 配置迁移：把两份旧配置合并进 app\config.json
# ==========================================================================

def _legacy_paths() -> list[Path]:
    """旧版单文件滚榜助手（PTA-Board）可能留下的 config.json。

    **不写死任何本机路径**：按候选位置自动探测 ——
    ``%PTA_BOARD_HOME%``（用户可自己指）、用户主目录下的 ``PTA-Board``、
    旧版写设置的 ``%LOCALAPPDATA%\\PTA-Board``，最后才是 live-board 自带的 config.json。
    哪个都不存在，合并逻辑就什么都不做（返回空列表/读不到就跳过）。
    """
    out: list[Path] = []
    override = str(os.environ.get("PTA_BOARD_HOME") or "").strip()
    if override:
        out.append(Path(override).expanduser() / "config.json")
    try:
        out.append(Path.home() / "PTA-Board" / "config.json")
    except (RuntimeError, OSError):
        pass
    for key in ("LOCALAPPDATA", "APPDATA"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            out.append(Path(value) / "PTA-Board" / "config.json")
            break
    out.append(live.live_board_source() / "config.json")
    return out


def merge_live_awards_into_table(cfg: dict, live_cfg: dict) -> dict:
    """把**旧的、独立的实时侧奖项配置**并进主表（``cfg["awards"]``），一份数据。

    M17：奖项表在实时榜单模式里也能改，所以**只许有一份**：``cfg["awards"]``。
    历史遗留的实时侧清单有两个来源：

    * ``live.awards_cache``（app 配置里那份"榜单上会怎么着色"的缓存）；
    * 旧配置文件（``_legacy_paths()``）里的 ``awards`` / ``view.awards``。

    合并口径（**主表为准**）：

    * 主表里**已有同名奖项** → 保留主表那一行（名字/展示方式/人数都不动）；
      只有当主表那一行**名单是空的**、而旧条目**带 team_ids** 时，才把旧名单接过来
      （``manual=True``：这是用户当年勾的，不能被"重新识别"冲掉）；
    * 主表里**没有的奖项名** → 追加成一行（带 team_ids → 手动行；只有人数 → ``auto="rank"``）；
    * 合并完把主表的 ``view.awards`` **写回运行目录** ``live\\config.json``，
      把旧的那份独立副本覆盖掉（服务没起过就跳过，启动时会写）。

    返回一份可复算的报告（`m17-awards-shared.json` 用它）。
    """
    import board_config as _cfgmod
    report: dict = {"changed": False, "added": [], "adopted_ids": [], "kept": [],
                    "legacy_sources": [], "run_config_written": "", "skipped": ""}
    rows = _cfgmod.clean_awards(cfg.get("awards"))
    already = bool((cfg.get("_awards_live_merged") or {}).get("at"))
    legacy: list[dict] = []

    if already:
        # 只合并一次：用户后来删掉的奖项不能因为旧配置还在就"复活"
        report["skipped"] = "旧的实时侧清单已经合并过（_awards_live_merged 有记录）"
    else:
        cache = live_cfg.get("awards_cache")
        if isinstance(cache, list) and cache:
            legacy.append({"source": "app 配置 live.awards_cache", "items": cache})

        for path in _legacy_paths():
            try:
                if not path.is_file():
                    continue
                doc = live.read_json(path, None)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict):
                continue
            items: list = []
            if isinstance(doc.get("awards"), list):
                items = list(doc["awards"])
            view = doc.get("view") if isinstance(doc.get("view"), dict) else {}
            if isinstance(view.get("awards"), list) and view["awards"]:
                items = list(view["awards"]) or items
            if items:
                legacy.append({"source": str(path), "items": items})

    by_name = {str(r.get("name") or ""): r for r in rows}
    for group in legacy:
        report["legacy_sources"].append(
            {"source": group["source"], "count": len(group["items"]),
             "names": [str(i.get("name") or "") for i in group["items"] if isinstance(i, dict)]})
        for item in group["items"]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            ids = [str(t).strip() for t in (item.get("team_ids") or []) if str(t).strip()]
            target = by_name.get(name)
            if target is not None:
                if ids and not (target.get("team_ids") or []):
                    target["team_ids"] = ids
                    target["manual"] = True
                    target["auto"] = ""
                    target["names"] = {**{t: "" for t in ids},
                                       **(target.get("names") or {})}
                    report["adopted_ids"].append({"name": name, "team_ids": ids,
                                                  "source": group["source"]})
                    report["changed"] = True
                else:
                    report["kept"].append(name)
                continue
            row = _cfgmod.clean_award_row(
                {"name": name, "show": item.get("show") or "detail",
                 "count": len(ids) or int(item.get("count") or 0),
                 "team_ids": ids, "manual": True if ids else False,
                 "auto": "" if ids else "rank"}, len(rows))
            rows.append(row)
            by_name[name] = row
            report["added"].append({"name": name, "count": row["count"],
                                    "team_ids": list(row["team_ids"]),
                                    "auto": row["auto"], "manual": row["manual"],
                                    "source": group["source"]})
            report["changed"] = True

    if report["changed"]:
        cfg["awards"] = rows
        cfg["_awards_live_merged"] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sources": [str(g["source"]) for g in legacy],
        }

    # 实时侧那份"清单缓存"改成**由主表派生**（不再是一份独立的副本）——
    # 这样「奖项清单」卡就是主表本身，也不会有"删了又回来"的旧数据。
    try:
        live_cfg["awards_cache"] = _cfgmod.live_awards_from_rows(cfg.get("awards") or [])
        live_cfg["awards_source"] = "奖项表（滚榜 / 实时共用同一份 config.json 的 awards）"
    except Exception:  # noqa: BLE001
        pass

    # 把主表的载荷写回运行目录，清掉那边可能与主表不一致的旧副本
    try:
        run = live.run_dir(cfgmod.work_root())
        ok, note = live.update_live_view(run, {
            "awards": _cfgmod.live_view_awards(cfg.get("awards") or []),
            "awards_mode": "rows"})
        report["run_config_written"] = note or ("已更新" if ok else "未更新")
    except Exception as exc:  # noqa: BLE001
        report["run_config_written"] = f"{type(exc).__name__}: {exc}"

    report["rows_after"] = [{"name": r.get("name"), "count": r.get("count"),
                             "team_ids": list(r.get("team_ids") or []),
                             "auto": r.get("auto"), "manual": bool(r.get("manual"))}
                            for r in (cfg.get("awards") or [])]
    return report


def ensure_live_config(cfg: dict, notes: list[str]) -> bool:
    """补齐 ``cfg['live']``；首次启动时从两份旧配置合并。返回是否有改动。"""
    changed = False
    raw = cfg.get("live")
    live_cfg = dict(LIVE_DEFAULTS)
    live_cfg["view"] = dict(LIVE_DEFAULTS["view"])
    live_cfg["problem_colors"] = list(DEFAULT_PROBLEM_COLORS)
    live_cfg["medal_colors"] = list(DEFAULT_MEDAL_COLORS)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in ("view", "problem_colors", "medal_colors", "awards_cache"):
                continue
            live_cfg[key] = value
        if isinstance(raw.get("view"), dict):
            live_cfg["view"].update(raw["view"])
        for key in ("problem_colors", "medal_colors"):
            if isinstance(raw.get(key), list) and len(raw[key]) >= 3:
                live_cfg[key] = list(raw[key])
        if isinstance(raw.get("awards_cache"), list):
            live_cfg["awards_cache"] = raw["awards_cache"]
    else:
        changed = True

    cfg["live"] = live_cfg

    # ---- 从旧版滚榜助手的 config.json（自动探测到的位置）合并 ------------
    old_roll = live.read_json(_legacy_paths()[0], None)
    if isinstance(old_roll, dict):
        if not cfg.get("awards") and isinstance(old_roll.get("awards"), list):
            cfg["awards"] = old_roll["awards"]
            changed = True
        for key in ("photos_dir", "logos_dir", "font_path"):
            if not str(cfg.get(key) or "").strip() and str(old_roll.get(key) or "").strip():
                cfg[key] = old_roll[key]
                changed = True
            if not str(live_cfg.get(key) or "").strip() and str(old_roll.get(key) or "").strip():
                live_cfg[key] = old_roll[key]
                changed = True
        if not cfg.get("problem_set_id") and old_roll.get("problem_set_id"):
            cfg["problem_set_id"] = old_roll["problem_set_id"]
            changed = True
        notes.append(f"已合并旧滚榜配置：{_legacy_paths()[0]}")

    # ---- 从 live-board\config.json（实时榜单旧配置）合并 ----------------
    old_live = live.read_json(_legacy_paths()[1], None)
    if isinstance(old_live, dict):
        for key in ("port", "poll_interval", "full_check_interval", "freeze_mode",
                    "logos_dir", "font_path", "title"):
            if raw is None or key not in raw:
                if old_live.get(key) not in (None, ""):
                    live_cfg[key] = old_live[key]
                    changed = True
        view_src = old_live.get("view") if isinstance(old_live.get("view"), dict) else {}
        for key, value in view_src.items():
            if key in live_cfg["view"] and key not in (raw or {}).get("view", {}):
                live_cfg["view"][key] = value
        for key in ("problem_colors", "medal_colors"):
            if isinstance(view_src.get(key), list) and len(view_src[key]) >= 3:
                live_cfg[key] = list(view_src[key])
        notes.append(f"已合并旧实时配置：{_legacy_paths()[1]}")

    # ---- 奖项：滚榜配置优先，其次实时配置；都没有就只读 PTA-Board -------
    awards = cfg.get("awards") or []
    source = "app\\config.json 的奖项表" if awards else ""
    if not awards:
        for path in _legacy_paths():
            other = live.read_json(path, None)
            if isinstance(other, dict) and isinstance(other.get("awards"), list) \
                    and other["awards"]:
                awards = other["awards"]
                source = str(path)
                break
            if isinstance(other, dict):
                view = other.get("view") if isinstance(other.get("view"), dict) else {}
                if isinstance(view.get("awards"), list) and view["awards"]:
                    awards = view["awards"]
                    source = f"{path} 的 view.awards"
                    break
    live_cfg["awards_cache"] = awards or []
    live_cfg["awards_source"] = source
    cfg["live"] = live_cfg
    return changed


# ==========================================================================
# 主窗口
# ==========================================================================

class AssistantApp(BoardApp):
    """在滚榜助手的外壳上加「模式切换」和实时榜单相关页面。"""

    def __init__(self, root, store, trace=None, mode: str | None = None) -> None:
        cfg = store.config

        # ---- 运行期状态（必须在 super().__init__ 之前就存在）-----------
        saved_mode = str(cfg.get("mode") or "")
        if mode in (MODE_LIVE, MODE_ROLL):
            self.mode = mode
        elif saved_mode in (MODE_LIVE, MODE_ROLL):
            self.mode = saved_mode
        else:
            self.mode = MODE_LIVE
        #: 每个模式各自记住上次停留的页（全局 idx：实时 0..4，滚榜 5..8）
        self._last_tab = {
            MODE_LIVE: self._clamp_tab(MODE_LIVE, cfg.get("last_tab_live")),
            MODE_ROLL: self._clamp_tab(MODE_ROLL, cfg.get("last_tab_roll")),
        }
        self._live_ready = False
        self._mode_widgets = []
        self._mode_hint = None
        self._nav_items = []
        self._nav_gap = tk.Frame(root)
        self.live_status: dict = {}
        self._live_log_pos = 0
        self._live_last_tick = 0.0
        self._live_busy = ""
        self._live_trace_lines: list[str] = []
        self._shot_only = False
        self._op_started = ""
        #: 榜单页 URL / 显示的参数（「2 外观」页的字体也登记在这里）
        self.view_vars: dict[str, tk.Variable] = {}
        #: Cookie 提示（两个模式各有一张卡，同一套状态一起刷新）
        self._cookie_widgets: dict[str, dict] = {}

        # ---- 配置迁移（首次启动自动合并两份旧配置）--------------------
        self._config_notes: list[str] = []
        changed = ensure_live_config(cfg, self._config_notes)
        if changed:
            try:
                store.save()
            except Exception:  # noqa: BLE001
                pass

        self.live_cfg = cfg.setdefault("live", dict(LIVE_DEFAULTS))
        self.live_cfg.setdefault("view", dict(LIVE_DEFAULTS["view"]))

        # ---- M17：实时侧那份"旧奖项配置"合并进主表（奖项表只有一份）------
        self._awards_merge_report: dict = {}
        try:
            self._awards_merge_report = merge_live_awards_into_table(cfg, self.live_cfg)
        except Exception as exc:  # noqa: BLE001 - 迁移失败不能挡住启动
            self._awards_merge_report = {"error": f"{type(exc).__name__}: {exc}"}
        if self._awards_merge_report.get("changed"):
            try:
                store.save()
            except Exception:  # noqa: BLE001
                pass

        # ---- 页面表（在 BoardApp 建页面之前就换掉）--------------------
        self.PAGES = PAGES

        self.service = live.LiveService(cfgmod.work_root())

        super().__init__(root, store, trace)
        self._live_ready = True

        # ---- 9 个模式页面已建好：装模式切换、按当前模式重排导航 --------
        self._apply_mode(write=False)
        self.cfg["mode"] = self.mode
        self._decorate_pages()
        self.root.title(APP_TITLE)
        self.root.after(400, self._post_boot)

    @staticmethod
    def _clamp_tab(mode: str, value) -> int:
        """把「某模式内第几页」夹成该模式的合法全局 idx。"""
        pages = MODE_PAGES[mode]
        try:
            offset = int(value)
        except (TypeError, ValueError):
            offset = 0
        return pages[max(0, min(len(pages) - 1, offset))]

    # ---------------------------------------------------------------- 页面

    def _page_header(self, page: tk.Frame, idx: int) -> None:
        """页头 = 标题 + 一句话说明 + 「这页怎么用」步骤条。

        合并版是给助教/同学直接拿去用的，所以每页开头都用大白话说清
        「这页干什么、按什么顺序点」。步骤条是独立的一条浅蓝底带，
        一眼能和正文的灰字提示区分开。
        """
        BoardApp._page_header(self, page, idx)
        # 页头那句「一句话说明」基类是不换行的：句子稍长就会把**整页**的请求宽度
        # 顶出 Canvas（900x600 实测 +2px）。这里也登记成自动换行。
        try:
            head = page.winfo_children()[0]
            for child in head.winfo_children():
                if child.winfo_class() == "Label" and len(str(child.cget("text"))) > 18:
                    self._auto_wrap(child, source=child, min_width=180)
        except Exception:  # noqa: BLE001
            pass
        steps = PAGE_DEFS[idx][4]
        band = self._f(page, bg="#e8eefc", highlightbackground="#c3d4f7",
                       highlightthickness=1, bd=0)
        band.pack(fill="x", padx=24, pady=(8, 0))
        label = tk.Label(band, text="这页怎么用：" + steps, bg="#e8eefc",
                         fg="#1e3a8a", font=(UI_FONT, 9), anchor="w", justify="left",
                         wraplength=300)
        label.pack(fill="x", padx=12, pady=7)
        self._auto_wrap(label, source=band, min_width=220)
        #: 记住每条步骤带：滚榜页的横幅要插在它**下面**、第一张卡片上面。
        if not hasattr(self, "_step_bands"):
            self._step_bands: dict[int, tk.Frame] = {}
        self._step_bands[idx] = band

    def _page_frame(self, idx: int):
        """按 PAGE_DEFS 的建设顺序建页，并把 page holder 一并记下来。

        这里**刻意不 pack** holder：合并版的页面容器统一由 ``_show_page_only``
        用 place 控制。同一个控件上混用 pack/place 会让 Tk 的几何管理卡死
        （实测：先 pack 再 place，第一次 update 就永久挂住）。
        """
        holder = self._f(self.content, bg=self.C["bg"])
        self._page_holders.append(holder)

        canvas = tk.Canvas(holder, bg=self.C["bg"], highlightthickness=0, bd=0)
        vbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._page_canvases.append(canvas)

        page = self._f(canvas, bg=self.C["bg"])
        window_id = canvas.create_window((0, 0), window=page, anchor="nw")

        def _clamp():
            try:
                w = canvas.winfo_width()
                h = max(page.winfo_reqheight(), canvas.winfo_height())
                canvas.configure(scrollregion=(0, 0, w, h))
                canvas.xview_moveto(0)
            except Exception:  # noqa: BLE001
                pass

        def _on_page_config(_event=None):
            _clamp()
            self._fit_all_hints()

        def _on_canvas_config(event):
            canvas.itemconfigure(window_id, width=event.width)
            _clamp()

        page.bind("<Configure>", _on_page_config)
        canvas.bind("<Configure>", _on_canvas_config)
        canvas.bind("<FocusIn>", lambda _e: canvas.xview_moveto(0))
        page.bind("<FocusIn>", lambda _e: canvas.xview_moveto(0), add="+")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        self._page_frames.append(page)
        self._page_header(page, idx)
        return page

    # 基类的 _build_window 固定调 4 个方法 + 建 4 个页面；合并版按 PAGE_DEFS
    # 建 **9 个**页面：实时榜单 5 页（0..4）+ 赛后滚榜 4 页（5..8），两套互不共用。
    def _build_window(self) -> None:
        """自己搭窗口骨架（**不调** BoardApp._build_window）。

        基类在建页面时会对页面容器调 ``pack()``；而合并版要在一屏里按模式
        切换可见页，只能用 place，且同一个控件上混用 pack/place 会让 Tk
        的几何管理卡死（实测：pack 之后再 place，第一次 update 永久挂住）。
        所以这里照抄基类的骨架，去掉它对页面容器的 pack。
        """
        self.root.title(APP_TITLE)
        self.root.minsize(980, 640)
        saved = base.usable_geometry(
            str(self.cfg.get("window_geometry") or ""),
            self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        self.root.geometry(saved or "1080x780")
        self.root.configure(bg=self.C["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_styles()
        self._build_status_bar()

        main_row = self._f(self.root, bg=self.C["bg"])
        main_row.pack(fill="both", expand=True)
        self._build_sidebar(main_row)

        right = self._f(main_row, bg=self.C["bg"])
        right.pack(side="left", fill="both", expand=True)
        self._build_log_pane(right)

        self.content = self._f(right, bg=self.C["bg"])
        self.content.pack(fill="both", expand=True)
        # 内容区宽度定了以后要把**所有**页面按这个宽度重新摆一遍：隐藏页也要
        # 有真实宽度，页面里的提示才换行换对（见 _show_page_only 的说明）。
        self.content.bind("<Configure>",
                          lambda _e: self._show_page_only(self._nav_current), add="+")

        # 10 个页面：建设顺序 = PAGE_DEFS 的 idx；容器只由 _show_page_only 摆
        for idx in range(len(PAGE_DEFS)):
            self._page_frame(idx)
        self._page_frames = [f for f in self._page_frames if f is not None]
        assert len(self._page_frames) == len(PAGE_DEFS), len(self._page_frames)

        # 页面容器 → 语义属性名。
        # 前 6 页是实时榜单那一套（数据源 / 队伍备注 / 外观 / 服务 / 直播 / 历史），
        # 后 4 页是赛后滚榜那一套 —— 基类的 _build_contest_tab / _build_play_tab /
        # _build_roll_tab / _build_history_tab 只认 tab_contest / tab_play /
        # tab_roll / tab_history，所以把后四个名字指给滚榜的页面：
        # 基类那些方法（历史载入 → tab_roll 等）就落在本模式的页上。
        self.tab_datasource_live = self._page_frames[0]  # 实时 · 1 数据源
        self.tab_memo_live = self._page_frames[9]        # 实时 · 2 队伍备注（VJudge）
        self.tab_display_live = self._page_frames[1]     # 实时 · 3 外观
        self.tab_service = self._page_frames[2]          # 实时 · 4 服务
        self.tab_live = self._page_frames[3]             # 实时 · 5 直播
        self.tab_history_live = self._page_frames[4]     # 实时 · 6 历史
        # 注意：``tab_contest`` 是**基类** ``_build_contest_tab`` 认的名字，
        # 它会被画到「滚榜 · 1 数据源」上，所以这里必须指第 5 页 ——
        # 指错了就会把滚榜的第一张卡画进实时模式的页里（实测踩过）。
        self.tab_contest = self._page_frames[5]          # 滚榜 · 1 数据源
        self.tab_datasource = self._page_frames[5]       # 同上（别名，读代码更好懂）
        self.tab_play = self._page_frames[6]             # 滚榜 · 2 外观
        self.tab_roll = self._page_frames[7]             # 滚榜 · 3 滚榜
        self.tab_history = self._page_frames[8]          # 滚榜 · 4 历史

        # **先建滚榜那一套**：题目集 ID / 照片目录 / 校徽目录这些
        # StringVar 由基类方法创建，实时榜单的页面是"第二个入口"，共用同一批
        # 变量 —— 这样两边显示同一个值、改哪边都即时生效、配置也只存一份。
        self._builders = {
            5: self._build_roll_datasource_tab,
            6: self._build_roll_display_tab,
            7: self._build_roll_roll_tab,
            8: self._build_roll_history_tab,
            0: self._build_live_datasource_tab,
            9: self._build_live_memo_tab,
            1: self._build_live_display_tab,
            2: self._build_service_tab,
            3: self._build_live_tab,
            4: self._build_live_history_tab,
        }
        for idx in (5, 6, 7, 8, 0, 1, 2, 3, 4, 9):
            self._builders[idx]()
        self._build_roll_banner()

        self._show_page_only(0)
        self.nb = _NavAdapter(self)
        self._nav_select(0)

    # ------------------------------------------------------------ 侧栏导航

    def _build_sidebar(self, parent) -> None:
        BoardApp._build_sidebar(self, parent)
        # 模式切换放在品牌下面、导航上面：pack(before=...) 精确插位
        self._mode_box = self._f(self._nav_box.master, bg=self.C["side"])
        self._mode_box.pack(fill="x", padx=14, pady=(0, 8),
                            before=self._nav_box)
        self._t(self._mode_box, "模式", size=8, fg=self.C["side_dim"],
                bg=self.C["side"], anchor="w").pack(anchor="w", pady=(0, 4))
        self._mode_buttons_box = self._f(self._mode_box, bg=self.C["side"])
        self._mode_buttons_box.pack(fill="x")
        for key in (MODE_LIVE, MODE_ROLL):
            item = self._f(self._mode_buttons_box, bg="#2b3a52", cursor="hand2")
            item.pack(fill="x", pady=1)
            bar = tk.Frame(item, bg="#2b3a52", width=3)
            bar.pack(side="left", fill="y")
            lbl = self._t(item, MODE_LABELS[key], size=9, bold=True,
                          fg=self.C["side_text"], bg="#2b3a52", anchor="w")
            lbl.pack(side="left", fill="x", expand=True, padx=(8, 6), pady=6)
            for widget in (item, lbl):
                widget.bind("<Button-1>", lambda _e, k=key: self.on_mode_click(k))
            self._mode_widgets.append((key, item, bar, lbl))
        self._mode_hint = self._t(self._mode_box, "", size=8, fg=self.C["side_dim"],
                                  bg=self.C["side"], anchor="w", justify="left")
        self._mode_hint.pack(anchor="w", pady=(5, 0))
        self._auto_wrap(self._mode_hint, source=self._mode_box, min_width=120)

    def _rebuild_nav(self) -> None:
        """按**当前模式**重建侧栏：分组标题 + 从 1 开始的显示序号。

        每个模式只有自己那一组，组标题就是模式名 —— 左栏里不会再出现
        「共用」这类字样，也不会两个模式的页签混在一起。序号是「本模式内的读
        的顺序」，所以两个模式各自从 1 开始。

        注意：基类 ``_build_sidebar`` 已经按它自己的 4 页建过一批导航项，
        这里必须先把 ``self._nav_box`` 里的旧控件全部销毁，否则会看到两批
        导航项同时出现。
        """
        for child in list(self._nav_box.winfo_children()):
            child.destroy()
        self._nav_items = []
        self._nav_numbers: dict[int, int] = {}
        nav = self._nav_box
        number = 0
        for group_title, idxs in NAV_GROUPS.get(self.mode, ()):
            head = self._t(nav, group_title, size=9, bold=True,
                           fg=self.C["side_text"], bg=self.C["side"], anchor="w")
            head.pack(fill="x", padx=(13, 0), pady=(7, 2))
            for idx in idxs:
                number += 1
                page_title = PAGE_DEFS[idx][2]
                self._nav_numbers[idx] = number

                item = self._f(nav, bg=self.C["side"], cursor="hand2")
                item.pack(fill="x", pady=1)
                bar = tk.Frame(item, bg=self.C["side"], width=3)
                bar.pack(side="left", fill="y")
                chip = tk.Label(item, text=str(number), width=2,
                                font=(UI_FONT, 8, "bold"),
                                bg="#31405a", fg=self.C["side_text"])
                chip.pack(side="left", padx=(13, 8), pady=4)
                lbl = self._t(item, page_title, size=10, fg=self.C["side_text"],
                              bg=self.C["side"], anchor="w")
                lbl.pack(side="left", fill="x", expand=True)
                for widget in (item, chip, lbl):
                    widget.bind("<Button-1>", lambda _e, i=idx: self._nav_select(i))
                    widget.bind("<Enter>", lambda _e, i=idx: self._nav_hover(i, True))
                    widget.bind("<Leave>", lambda _e, i=idx: self._nav_hover(i, False))
                self._nav_items.append([idx, item, bar, chip, lbl])

    def _nav_hover(self, idx: int, on: bool) -> None:
        if idx == self._nav_current:
            return
        for item_idx, item, bar, chip, lbl in self._nav_items:
            if item_idx != idx:
                continue
            bg = self.C["side_hover"] if on else self.C["side"]
            item.configure(bg=bg)
            bar.configure(bg=bg)
            chip.configure(bg="#31405a" if not on else "#3a4c6b")
            lbl.configure(bg=bg)

    def _nav_paint(self) -> None:
        for item_idx, item, bar, chip, lbl in self._nav_items:
            if item_idx == self._nav_current:
                item.configure(bg=self.C["side_hover"])
                bar.configure(bg=self.C["accent"])
                chip.configure(bg=self.C["accent"], fg="white")
                lbl.configure(bg=self.C["side_hover"], fg="white")
            else:
                item.configure(bg=self.C["side"])
                bar.configure(bg=self.C["side"])
                chip.configure(bg="#31405a", fg=self.C["side_text"])
                lbl.configure(bg=self.C["side"], fg=self.C["side_text"])

    def _nav_select(self, tab=None):
        """切页 = 只换高亮 + 把这一页滚到顶。

        注意：这里**故意不**复用基类的「隐藏其它页」逻辑 —— 合并版里
        可见页集合是由「模式」决定的（_apply_mode 已经排好 place 顺序），
        再让基类去 pack_forget 会把同模式的其它页也藏掉，
        结果几页的 Canvas 叠在一起（截图里就是「左边是数据源、右边是外观」）。
        """
        if tab is None:
            return self._page_frames[self._nav_current]
        idx = tab if isinstance(tab, int) else self._page_frames.index(tab)
        if self._live_ready and idx not in MODE_PAGES.get(self.mode, ()):
            # 侧栏只列本模式的页，正常点不到别的模式；这里只是给代码调用兜底：
            # 要跳到别的模式的页，就连模式一起切过去。
            for other, pages in MODE_PAGES.items():
                if idx in pages:
                    self._last_tab[self.mode] = self._nav_current
                    self.mode = other
                    self._last_tab[other] = idx
                    self._apply_mode(write=True)
                    break
            else:
                return self._page_frames[self._nav_current]
        self._nav_current = idx
        self._last_tab[self.mode] = idx
        self._nav_paint()
        if self._live_ready:
            self._show_page_only(idx)

        def _reset_scroll():
            try:
                self._page_canvases[idx].yview_moveto(0)
            except Exception:  # noqa: BLE001
                pass

        self.root.after(10, _reset_scroll)
        return self._page_frames[idx]

    def _nav_index(self, what) -> int:
        return len(self._page_frames) if what == "end" else self._page_frames.index(what)

    # ------------------------------------------------------------ 模式切换

    def on_mode_click(self, target: str) -> None:
        if target == self.mode:
            return
        previous = self.mode
        self._collect_live_config()
        self._last_tab[previous] = self._nav_current   # 记住旧模式停在哪一页
        self.mode = target
        self._apply_mode(write=True)
        # M17：奖项表两侧共用同一份 cfg["awards"] —— 切模式时把两边界面都按它重画一遍，
        # 保证"切过去立刻是同一份值"（不依赖谁先刷新）。
        self._sync_award_editors()
        if target == MODE_LIVE and previous != MODE_LIVE and not self._shot_only:
            self._roll_to_live()

    def _sync_award_editors(self) -> None:
        """把两个奖项表编辑器都按 ``cfg["awards"]`` 重画（切模式 / 改完之后的收尾）。"""
        editors = getattr(self, "award_editors", None)
        if not isinstance(editors, dict):
            return
        rows = self.cfg.get("awards") or []
        for key, editor in editors.items():
            if key == "roll":
                continue          # 滚榜那份是主表本身，不用回写（回写会打断正在编辑的输入）
            try:
                editor.set_rows(rows)
            except Exception:  # noqa: BLE001
                pass

    def _show_page_only(self, idx: int) -> None:
        """只显示第 idx 个页面容器；其余页挪到可视区外面（x=-20000）。

        所有容器统一用 place，且**从不 place_forget** —— 原因：Tk 在「控件还没有
        几何管理器」时对 place_forget 的处理会跟 <Configure> → scrollregion 重算
        形成抖动，实测直接在 update 里挂死；一直保持 place 就没有这种状态翻转。

        隐藏页**也给真实宽度**（只是高度留 1px）：这些页面里的提示文字是按
        「可用宽度」自动换行的（_hint / _auto_wrap + _run_fit），如果隐藏页只有
        1px 宽，_run_fit 会按「内容区宽度」给它算出过宽的 wraplength 并且**锁死**
        （变化 <16px 就不再调整），切过去就横向溢出。所以这里统一按内容区宽度摆。
        """
        width = self.content.winfo_width() or 1
        height = self.content.winfo_height() or 1
        for i, holder in enumerate(self._page_holders):
            if i == idx:
                holder.place(x=0, y=0, width=width, height=height)
            else:
                holder.place(x=-20000, y=0, width=width, height=1)

    def _run_fit(self) -> None:
        """基类的换行预算之后再按「标签所在行的实际可用宽度」收一次。

        基类 budget = 内容区宽 − 132，只减了页面左右内边距（24×2）和卡片正文
        内边距（18×2）；可表单行里还有一个 15 字符的左栏标签（≈123px），提示按
        基类宽度换行会把**整页**的请求宽度顶出 Canvas（900x600 实测 +2 ~ +38px，
        页面因此多出一点横向滚动余量，焦点进输入框时整页会横移）。

        这里只做「只收不放」的后处理：算出比基类更小的上限时才改，所以每轮只会
        让行更窄，不会来回抖成循环。
        """
        BoardApp._run_fit(self)
        for item in list(getattr(self, "_wrap_labels", [])):
            try:
                label, _base, min_width = item
            except (TypeError, ValueError):
                continue
            try:
                row = label.master
                row_w = int(row.winfo_width())
                if row_w <= 1:
                    continue
                used = 0
                for sib in row.winfo_children():
                    if sib is label or sib.winfo_manager() != "pack":
                        continue
                    info = sib.pack_info()
                    if str(info.get("side", "left")) != "left":
                        continue
                    if str(info.get("fill", "")) == "x":
                        continue
                    used += int(sib.winfo_width()) + 2 * int(info.get("padx") or 0)
                cap = row_w - used - 10
                if cap >= int(min_width) and int(label.cget("wraplength")) > cap:
                    label.configure(wraplength=cap)
            except Exception:  # noqa: BLE001
                continue
        self._fit_award_show_combos()

    #: 奖项表每行「展示方式」下拉框的最小字符宽（再窄就没法读了）
    _AWARD_SHOW_MIN_CHARS = 10

    def _fit_award_show_combos(self) -> None:
        """M13（4a 续）：奖项表每行的「展示方式」下拉框按最长文案量宽，并夹进本行可用宽度。

        基类 ``pta_board_gui.BoardApp.add_award_row`` 里把宽度写死成 ``width=20``，而
        ``board_config.AWARD_SHOW_LABELS`` 的文案长到
        「停下显示详情（推荐：照片背景 + 校徽/队名/奖项名）」—— 界面上被截成
        「停下显示详情（推荐：照片…」，和打星奖项面板那个下拉框是同一类问题。

        上限取**这一行的真实可用宽度**（页面 Canvas 不给横向滚动余量）：宁可窄窗口下
        截断文案，也不能把「删除」按钮挤出可视区。Lead 若在基类里把这一行改成自适应，
        这段（连同 ``_run_fit`` 末尾那一行调用）可以直接删。
        """
        # M17：奖项表现在有**两个**编辑器（滚榜 + 实时），两边都要按各自那一行的
        # 实际可用宽度收窄 —— 只遍历 self.award_rows 会让实时那张表保持 width=24。
        editors = getattr(self, "award_editors", None)
        targets: list[tuple[object, list]] = []
        if isinstance(editors, dict) and editors:
            for editor in editors.values():
                targets.append((getattr(editor, "award_rows_holder", None),
                                list(getattr(editor, "award_rows", []) or [])))
        else:
            targets.append((holder, list(getattr(self, "award_rows", []) or [])))
        for row_holder, records in targets:
            self._fit_one_editor_combos(row_holder, records)

    def _fit_one_editor_combos(self, holder, records: list) -> None:
        """把一个编辑器里所有行的「展示方式」下拉框调到合适宽度（见上）。"""
        if holder is None or not holder.winfo_exists():
            return
        holder_w = int(holder.winfo_width())
        if holder_w <= 1:                       # 页面还没摆到可视区，量不到可用宽度
            return
        for record in list(records):
            frame = record.get("frame")
            if frame is None or not frame.winfo_exists():
                continue
            # 递归找（M16：基类把每行拆成「第一行控件 + 第二行已选 N 支」两层，
            # 下拉框不再是 frame 的直接子控件了）
            combo = None
            stack = list(frame.winfo_children())
            while stack and combo is None:
                widget = stack.pop(0)
                if isinstance(widget, ttk.Combobox):
                    combo = widget
                    break
                try:
                    stack.extend(widget.winfo_children())
                except Exception:  # noqa: BLE001
                    continue
            if combo is None:
                continue
            try:
                font = tkfont.nametofont("TkDefaultFont")
                unit = max(1, int(font.measure("0")))
                values = [str(v) for v in combo.cget("values")] or [str(combo.get() or "")]
                # 除下拉框以外、**它所在那一行**还占掉多少宽度。
                # M16：下拉框搬到了「第二行」（展示方式 + 已选 N 支·来源），
                # 所以基准要用 combo.master（那一行）而不是整个 record["frame"]，
                # 否则会把第一行（奖项名/共 N 队/三个按钮）的宽度也算进来 → 被过度压窄。
                line = combo.master if combo.master is not None else frame
                other = max(0, int(line.winfo_reqwidth()) - int(combo.winfo_reqwidth()))
                # 下拉框自身除文字外还要的宽度（箭头 + 内边距），实测
                overhead = max(0, int(combo.winfo_reqwidth())
                               - int(combo.cget("width")) * unit)
                room_widget = holder_w - other - 6          # 整个下拉框能占多宽
                room_text = room_widget - overhead          # 其中能画字的部分
                ideal = combo_width_chars(values, minimum=self._AWARD_SHOW_MIN_CHARS)
                fit = max(self._AWARD_SHOW_MIN_CHARS, int(room_text // unit))
                chars = max(self._AWARD_SHOW_MIN_CHARS, min(ideal, fit))
                if int(combo.cget("width")) != chars:
                    combo.configure(width=chars)
            except Exception:  # noqa: BLE001
                continue

    def add_award_row(self, name: str = "", count=1, show: str = "detail"):
        """基类建完这一行之后，立刻把「展示方式」下拉框调到合适宽度（见上）。"""
        record = BoardApp.add_award_row(self, name, count, show)
        self._fit_award_show_combos()
        return record

    def _apply_mode(self, write: bool = True) -> None:
        """把界面切成当前模式：**整栏页签换掉** + 显示该模式上次停留的页。"""
        pages = MODE_PAGES[self.mode]
        current = self._last_tab.get(self.mode)
        if current not in pages:
            current = pages[0]
        self._last_tab[self.mode] = current
        self._nav_current = current
        self._rebuild_nav()                 # ← 整栏页签按模式重建（序号各自从 1 开始）
        if self._live_ready:
            self._show_page_only(current)
        self._nav_paint()
        self._paint_mode_buttons()
        if write:
            self._log(f"\n[模式] 已切到「{MODE_LABELS[self.mode]}」\n")
            self.cfg["mode"] = self.mode
            self._persist()
            self._on_mode_entered()
        try:
            self._page_canvases[current].yview_moveto(0)
        except Exception:  # noqa: BLE001
            pass

    def _paint_mode_buttons(self) -> None:
        for key, item, bar, lbl in self._mode_widgets:
            active = key == self.mode
            item.configure(bg=self.C["side_hover"] if active else "#2b3a52")
            bar.configure(bg=self.C["accent"] if active else "#2b3a52")
            lbl.configure(bg=self.C["side_hover"] if active else "#2b3a52",
                          fg="white" if active else self.C["side_text"])
        if self._mode_hint is not None:
            # 侧栏只有 190px 宽，这行提示必须短 —— 太长会挤掉最下面的页签。
            if self.mode == MODE_LIVE:
                text = "比赛中：先「4 服务」启动，再「5 直播」复制网址"
            else:
                text = "赛后：先「1 数据源」拉数据，再「3 滚榜」开始"
            self._mode_hint.configure(text=text)

    def _on_mode_entered(self) -> None:
        """切模式时的专属动作（服务停/起、提示语）。

        ``--headless-shot`` 截图模式下一律不做服务动作：截图进程不能启动
        子进程、更不能弹模态框（会把无人值守的截图卡死）。
        """
        if self._shot_only:
            return
        if self.mode == MODE_ROLL:
            # 用户 2026-10-24：切到「赛后滚榜」**不能再停掉实时服务** ——
            #   直播中切过去一次，榜单页就直接断线（浏览器不停重连、画面停住）。
            #   滚榜是本地生成比赛包 + 播放 Resolver，用不到榜单页那个端口。
            if self.service.any_running():
                port = self.live_cfg.get("port") or live.DEFAULT_PORT
                self._log(f"[模式] 实时服务保持运行（端口 {port}）—— 滚榜不影响榜单页\n")
            self._refresh_roll_banner()
        else:
            self._refresh_live_page()

    # ------------------------------------------------------------ 模式联动

    def _roll_to_live(self) -> None:
        """「切到实时榜单」：端口被占就先换个空闲端口，再启动服务。"""
        self._collect_live_config()
        port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
        if live.port_in_use(port) and not self.service.any_running():
            spare = next((p for p in (live.DEFAULT_PORT, live.DEFAULT_ALT_PORT, port + 2)
                          if p != port and not live.port_in_use(p)), None)
            if spare is None:
                self._set_status(f"端口 {port} 被占用，且找不到空闲端口")
                self._log(f"[模式] 端口 {port} 被占用，没启动服务（也没去杀别人）。\n")
                self._check_port_detail()
                return
            self._log(f"[模式] 端口 {port} 被别的服务占用 —— 自动改用 {spare}，"
                      f"不碰那个进程。\n")
            self.port_var.set(str(spare))
            self.live_cfg["port"] = spare
            self._persist()
            port = spare
        if not self.service.any_running():
            self.on_live_start()

    # ------------------------------------------------------------ 页面收尾

    def _decorate_pages(self) -> None:
        """两个模式共用的收尾：Cookie / 端口药丸、按钮可用性、迁移日志。"""
        if hasattr(self, "channel_hint"):
            self.channel_hint.configure(text=CHANNEL_HELP, fg=self.C["hint"])
        self._sync_fetch_buttons()
        self._sync_service_buttons()
        self._refresh_cookie_pill()
        self._refresh_port_pill()
        if self._config_notes:
            for note in self._config_notes:
                self._log(f"[配置迁移] {note}\n")
        self._log(f"[配置] 设置文件：{self.store.config_path}\n")

    # == 实时榜单 · 1 数据源 ============================================

    def _build_live_datasource_tab(self) -> None:
        """实时榜单模式第 1 页：平台 / 题目集（比赛）ID / Cookie / 端口 / 环境预检。

        这一页**只放实时榜单要用的东西** —— 比赛数据包、播放方式、Resolver、
        Esc 那些滚榜的事都在滚榜模式自己的页里，不在这出现。
        """
        page = self.tab_datasource_live

        # 「平台」放在**这一页最上面**：先选 PTA / VJudge，下面所有东西才谈得上。
        self._build_platform_card(page)

        # ✅ 环境预检卡**放在平台下面**（不许藏在折叠/二级入口里）：
        #    有 ❌ 时还会在步骤带下面弹出醒目红横幅。
        self._build_precheck_card(page)

        body = self._card(page, "实时榜单取数（比赛中用）",
                          "实时榜单每隔一会儿就去取一次新提交；这里决定「取哪一场」「多久取一次」")

        row = self._form_row(body, "题目集 ID")
        # 平台选 VJudge 时这一行的标签 / 提示会换成「VJudge 比赛 ID」（见 _refresh_platform_ui）
        self.psid_label_live = row.winfo_children()[0]
        self.psid_entry_live = self._entry(row, self.psid_var, 28)
        self.psid_entry_live.pack(side="left")
        self.psid_entry_live.bind("<KeyRelease>", lambda _e: self._psid_msg())
        self.psid_tip_live = ToolTip(self.psid_entry_live,
                                     "要比哪一场比赛，就填哪一场的题目集 ID。\n"
                                     "在浏览器打开这场比赛，地址栏里 /problem-sets/ 后面那串数字就是它，\n"
                                     "例如 2107050785530253312。\n"
                                     "这个输入框和「赛后滚榜」那边的题目集 ID 是同一个值：\n"
                                     "改这边那边也跟着变，只保存一份到设置文件里。")
        self.live_fetch_btn = self._ghost_btn(row, "重新拉取数据", self.on_fetch)
        self.live_fetch_btn.pack(side="left", padx=(10, 0))
        ToolTip(self.live_fetch_btn,
                "让取数程序立刻再取一轮（服务在跑时用）。\n"
                "服务没在跑的话，它会先帮你把实时榜单服务启动起来。")
        row = self._indent_row(body)
        self.psid_hint_live = self._hint(row, "")
        self.psid_hint_live.pack(side="left", fill="x", expand=True)

        # 旧的那三个取数选项已删除：这一行的文字固定，右边只有 Cookie 状态。
        # 2026-10-13：不再有「回落去借浏览器」这条路。
        row = self._form_row(body, "取数方式")
        self._t(row, CHANNEL_LINE, size=10, bold=True, fg=self.C["ink"]).pack(side="left")
        self.channel_state = self._pill(row, "未启动", "hint")
        self.channel_state.pack(side="left", padx=10)

        row = self._indent_row(body)
        self.channel_hint = self._hint(row, CHANNEL_HELP)
        self.channel_hint.pack(side="left", fill="x", expand=True)

        self._build_cookie_card(body, MODE_LIVE)


        row = self._form_row(body, "多久取一次")
        self.interval_var = tk.StringVar(value=str(self.live_cfg.get("poll_interval", 5)))
        self._entry(row, self.interval_var, 6).pack(side="left")
        self._hint(row, "秒（默认 5 秒；每轮约 2 次请求 ≈ 1440 次/小时，遇到限流就调大）",
                   size=9).pack(side="left", fill="x", expand=True, padx=6)

        row = self._form_row(body, "多久全量核对一次")
        self.full_var = tk.StringVar(value=str(self.live_cfg.get("full_check_interval", 120)))
        self._entry(row, self.full_var, 7).pack(side="left")
        self._hint(row, "秒（默认 120 秒；防漏掉改判、删提交）",
                   size=9).pack(side="left", fill="x", expand=True, padx=6)

        row = self._form_row(body, "榜单页端口")
        self.port_var = tk.StringVar(value=str(self.live_cfg.get("port", live.DEFAULT_PORT)))
        self._entry(row, self.port_var, 7).pack(side="left")
        self._hint(row, "（榜单页网址里的端口号；默认 8791，8765 上通常是别人在跑的服务）",
                   size=9).pack(side="left", fill="x", expand=True, padx=6)
        self.port_state = self._pill(row, "", "hint")
        self.port_state.pack(side="left", padx=8)

        # 用户 2026-10-24：滚榜页的「封榜时长」也在实时榜单里放一份，方便比赛中改。
        row = self._form_row(body, "封榜时长（分钟）")
        freeze_now = self.live_cfg.get("freeze_minutes")
        if freeze_now in (None, ""):
            freeze_now = self.cfg.get("freeze_minutes", 60)
        self.freeze_live_var = tk.StringVar(value=str(freeze_now))
        freeze_live_entry = self._entry(row, self.freeze_live_var, 7)
        freeze_live_entry.pack(side="left")
        ToolTip(freeze_live_entry,
                "比赛最后这么久不公开榜单（封榜）。和「滚榜」页共用同一份值，"
                "改完下一轮轮询就生效，不用重启服务；0 = 不封榜。")
        self._hint(row, "和滚榜页共用一份值；留空 = 用比赛自带的封榜时长；0 = 不封榜",
                   size=9).pack(side="left", fill="x", expand=True, padx=6)
        for ev in ("<FocusOut>", "<Return>"):
            try:
                freeze_live_entry.bind(ev, lambda _e=None: self._push_live_freeze())
            except Exception:  # noqa: BLE001
                pass

        row = self._indent_row(body)
        self.live_action_hint = self._hint(row, "")
        self.live_action_hint.pack(side="left", fill="x", expand=True)

        self._refresh_cookie_pill()
        self._refresh_port_pill()
        self._refresh_platform_ui()

    # == 实时榜单 · 1 数据源 · 平台（PTA / VJudge） ======================

    #: 下拉框显示的文字 -> 配置里的值
    PLATFORM_LABELS = {"PTA（拼题 A）": "pta", "VJudge": "vjudge"}
    #: VJudge 默认 10 秒轮询（任务要求）；PTA 保持原来的 5 秒
    VJUDGE_DEFAULT_INTERVAL = 10

    def _build_platform_card(self, page) -> None:
        """「平台」下拉：放在数据源页**最上面**（先选平台，下面才谈得上填什么）。"""
        body = self._card(page, "平台",
                          "这场比赛的取数来源：PTA（拼题 A）或 VJudge；"
                          "选完下面的 ID / Cookie / 轮询都会跟着变")

        current = str(self.cfg.get("platform") or "pta").strip().lower()
        label = "VJudge" if current == "vjudge" else "PTA（拼题 A）"
        self.platform_var = tk.StringVar(value=label)
        row = self._form_row(body, "平台")
        self.platform_combo = ttk.Combobox(row, textvariable=self.platform_var,
                                           state="readonly", width=16,
                                           values=list(self.PLATFORM_LABELS.keys()))
        self.platform_combo.pack(side="left")
        self.platform_combo.bind("<<ComboboxSelected>>",
                                 lambda _e: self._on_platform_change())
        ToolTip(self.platform_combo,
                "PTA（拼题 A）：原来的取数方式，需要有效 Cookie（JSESSIONID + PTASession）。\n"
                "VJudge：公开比赛匿名取数，不需要 Cookie；拿不到封榜/题名，"
                "真实姓名用「队伍备注」页维护。")
        self.platform_state = self._pill(row, "", "hint")
        self.platform_state.pack(side="left", padx=10)
        row = self._indent_row(body)
        self.platform_hint = self._hint(row, "")
        self.platform_hint.pack(side="left", fill="x", expand=True)

    def _platform(self) -> str:
        """当前平台（``pta`` / ``vjudge``）。"""
        try:
            label = str(self.platform_var.get())
        except Exception:  # noqa: BLE001
            label = ""
        return self.PLATFORM_LABELS.get(label, "pta")

    def _on_platform_change(self) -> None:
        """切平台：存配置、换标签/提示、VJudge 时给 10 秒轮询、刷新备注页可用性。"""
        platform = self._platform()
        self.cfg["platform"] = platform
        self.live_cfg["platform"] = platform
        # VJudge 默认 10 秒（任务要求）；切回 PTA 不强行改用户已填的间隔
        if platform == "vjudge":
            try:
                current = int(float(str(self.interval_var.get()).strip()))
            except (AttributeError, ValueError):
                current = 0
            if current <= 5:
                try:
                    self.interval_var.set(str(self.VJUDGE_DEFAULT_INTERVAL))
                except Exception:  # noqa: BLE001
                    pass
                self.live_cfg["poll_interval"] = self.VJUDGE_DEFAULT_INTERVAL
        self._refresh_platform_ui()
        self._refresh_cookie_pill()
        self._sync_memo_page()
        self._push_live_platform(platform)
        try:
            self._persist()
        except Exception:  # noqa: BLE001
            pass
        self._set_status("平台已切到 " + ("VJudge" if platform == "vjudge" else "PTA")
                         + "（改完点「重新拉取数据」或「启动」生效）")

    def _refresh_platform_ui(self) -> None:
        """按平台更新：ID 那一行的标签 / 提示、取数方式说明、平台药丸。"""
        if not hasattr(self, "platform_var"):
            return
        platform = self._platform()
        vj = platform == "vjudge"
        if hasattr(self, "platform_state"):
            self._set_pill(self.platform_state, "VJudge（公开榜）" if vj else "PTA",
                           "ok" if vj else "hint")
        if hasattr(self, "platform_hint"):
            self.platform_hint.configure(
                text=("VJudge：公开比赛匿名取数，不需要 Cookie；轮询默认 10 秒。"
                      "拿不到封榜设置与题目标题（题目用 A/B/… 代替），"
                      "真实姓名 / 校徽到「队伍备注」页维护 —— 榜单会显示备注名。"
                      if vj else
                      "PTA（拼题 A）：用题目集 ID + 有效 Cookie 取数，轮询默认 5 秒。"),
                fg=self.C["hint"])
        if hasattr(self, "psid_label_live"):
            try:
                self.psid_label_live.configure(text="VJudge 比赛 ID" if vj else "题目集 ID")
            except Exception:  # noqa: BLE001
                pass
        if hasattr(self, "channel_hint"):
            self.channel_hint.configure(
                text=("VJudge：直连 https://vjudge.net/contest/rank/single/<比赛ID>，"
                      "匿名 GET、不需要 Cookie。"
                      if vj else CHANNEL_HELP),
                fg=self.C["hint"])
        if hasattr(self, "psid_hint_live"):
            self.psid_hint_live.configure(
                text=("VJudge 比赛 ID：填 862258 这种数字，或直接粘 "
                      "https://vjudge.net/contest/862258#rank （粘网址也行，会自动抽出 ID）"
                      if vj else "题目集 ID：PTA 比赛网址里 /problem-sets/ 后面那串数字，"
                                "例如 2107050785530253312"),
                fg=self.C["hint"])

    def _push_live_platform(self, platform: str | None = None) -> None:
        """把 platform / contest_id 立刻写进实时运行目录，poller 下一轮就能读到。"""
        platform = platform or self._platform()
        try:
            run = live.run_dir(cfgmod.work_root())
            live.update_live_view(run, None, {
                "platform": platform,
                "contest_id": str(self.cfg.get("problem_set_id") or "").strip(),
            })
        except Exception:  # noqa: BLE001 - 写不进去也不影响界面（启动服务时会写）
            pass

    def _push_live_freeze(self) -> None:
        """把「封榜时长」写进主配置 + 实时运行目录（下一轮轮询生效，不用重启）。

        用户 2026-10-24：这份值滚榜页和实时榜单页**共用**（同一份 config.json），
        所以这里顺手把滚榜页那个输入框也改成同一个数，避免两边显示不一致。
        """
        raw = ""
        try:
            raw = str(self.freeze_live_var.get()).strip()
        except Exception:  # noqa: BLE001
            return
        value = None
        if raw != "":
            try:
                value = max(0, int(float(raw)))
            except ValueError:
                value = None
        self.live_cfg["freeze_minutes"] = value
        if value is not None:
            self.cfg["freeze_minutes"] = value
            try:
                self.freeze_var.set(str(value))        # 滚榜页显示同一份值
            except Exception:  # noqa: BLE001
                pass
        try:
            self._persist()
        except Exception:  # noqa: BLE001
            pass
        try:
            run = live.run_dir(cfgmod.work_root())
            live.update_live_view(run, None, {"freeze_minutes": value})
            self._set_status("封榜时长：%s（下一轮轮询生效）"
                             % ("留空 = 用比赛自带" if value is None else str(value) + " 分钟"))
        except Exception:  # noqa: BLE001
            self._set_status("封榜时长已保存（实时运行目录暂时没写进去，起服务时会补）")

    # == 实时榜单 · 2 队伍备注（VJudge 专用） ============================

    #: 表格列：(键, 表头, 宽度, 是否可编辑)
    MEMO_COLUMNS = (
        ("handle", "handle（原始账号）", 18, False),
        ("display", "备注姓名", 18, True),
        # 用户 2026-10-27：第三列「备注」多余，去掉；校徽列的表头写清楚怎么填
        ("logo", "校徽（留空=按备注姓名自动匹配）", 26, True),
    )

    def _alias_module(self):
        """导入备注库模块（同目录 / 程序根那份 vjudge_alias.py）。"""
        root = cfgmod.resource_root()
        for entry in (str(root), str(Path(root).parent / "pta-resolver"), str(Path(root) / "app")):
            try:
                if Path(entry).is_dir() and entry not in sys.path:
                    sys.path.insert(0, entry)
            except OSError:
                continue
        import vjudge_alias  # noqa: E402
        return vjudge_alias

    def _alias_db(self) -> Path:
        """备注库路径：``<程序根>\\vjudge-aliases.db``（和 poller 抓取时用的同一份）。"""
        return cfgmod.work_root() / "vjudge-aliases.db"

    def _build_live_memo_tab(self) -> None:
        """「队伍备注」页：从最近一次抓到的 contest.json 读 VJudge 选手，双击改备注。"""
        page = self.tab_memo_live
        self._memo_rows = []
        self._memo_row_map = {}
        self._memo_widgets = []
        self._memo_loaded = False

        body = self._card(
            page, "队伍备注（VJudge 专用）",
            "VJudge 只给 handle，没有学号和真实姓名。这里给每支队填「备注姓名 / 校徽 / 备注」，"
            "抓取时自动用备注名当队名 —— 榜单上就显示真实姓名")

        row = self._form_row(body, "数据来源")
        self.memo_source = self._hint(row, "还没有读取比赛数据")
        self.memo_source.pack(side="left", fill="x", expand=True)

        row = self._form_row(body, "操作")
        self.memo_reload_btn = self._ghost_btn(row, "重新读取选手列表", self._memo_reload)
        self.memo_reload_btn.pack(side="left")
        ToolTip(self.memo_reload_btn,
                "从最近一次抓到的 contest.json 重新读一遍 participants。\n"
                "刚拉完数据、或者比赛里又进了新选手时点一下。")
        self.memo_logo_btn = self._ghost_btn(row, "选择校徽图片…", self._memo_pick_logo)
        self.memo_logo_btn.pack(side="left", padx=(8, 0))
        ToolTip(self.memo_logo_btn,
                "先双击某行的「校徽」格，再点这里选一张图。\n"
                "图片会被复制到 <程序根>\\校徽\\<备注姓名>.png，并把文件名填进那一格。\n"
                "（留空 = 按备注姓名自动匹配 校徽\\ 目录里的同名图片）")
        self.memo_state = self._pill(row, "", "hint")
        self.memo_state.pack(side="left", padx=10)
        self._memo_widgets += [self.memo_reload_btn, self.memo_logo_btn]

        row = self._indent_row(body)
        hint = self._hint(
            row, "双击「备注姓名 / 校徽 / 备注」单元格即可编辑，改完按回车（或点别处）自动写入 "
                 f"{self._alias_db()}。榜单上显示备注名要等**下一次抓取**："
                 "点「1 数据源」页的「重新拉取数据」，或等下一轮轮询。")
        hint.pack(side="left", fill="x", expand=True)
        self._memo_hint_row = row

        # ---- 表格（放在一个固定高度的可滚动区域里，选手多了也不会把页面撑爆）----
        wrap = self._f(body)
        wrap.pack(fill="both", expand=True, padx=12, pady=(4, 6))
        canvas = tk.Canvas(wrap, height=260, bg=self.C["card"], highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.memo_table = self._f(canvas, bg=self.C["card"])
        window_id = canvas.create_window((0, 0), window=self.memo_table, anchor="nw")

        def _on_cfg(_e=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfigure(window_id, width=max(canvas.winfo_width(), 640))
            except Exception:  # noqa: BLE001
                pass

        self.memo_table.bind("<Configure>", _on_cfg)
        canvas.bind("<Configure>", _on_cfg)
        self._memo_canvas = canvas

        # 表头
        for col, (key, title, width, _edit) in enumerate(self.MEMO_COLUMNS):
            lbl = tk.Label(self.memo_table, text=title, bg="#eef2f7",
                           fg=self.C["sub"], font=(UI_FONT, 9, "bold"),
                           anchor="w", padx=6, pady=4, width=width)
            lbl.grid(row=0, column=col, sticky="nsew", padx=(0, 1), pady=(0, 2))
        self._memo_header = 1

        for col in range(len(self.MEMO_COLUMNS)):
            self.memo_table.grid_columnconfigure(col, weight=1)

        footer = self._hint(body, "改完点「重新拉取数据」或等下一轮轮询，榜单上就显示备注名。")
        footer.pack(anchor="w", fill="x", padx=14, pady=(0, 8))

        self._sync_memo_page()

    # ---- 队伍备注：读 / 写 / 可用性 ------------------------------------

    def _memo_contest_candidates(self) -> list[Path]:
        """按优先级列出「最近一次抓到的 contest.json」候选路径。"""
        root = cfgmod.work_root()
        cands = []
        try:
            cands.append(live.run_dir(root) / "contest.json")   # 实时运行目录（poller 写的）
        except Exception:  # noqa: BLE001
            pass
        cands.append(root / "contest.json")                     # 滚榜 / 手动拉的那份
        last = str(self.cfg.get("last_contest_json") or "").strip()
        if last:
            cands.append(Path(last))
        out: list[Path] = []
        for path in cands:
            try:
                if path.is_file() and path not in out:
                    out.append(path)
            except OSError:
                continue
        return out

    def _memo_read_teams(self) -> tuple[list[dict], Path | None]:
        """读出一份带 teams 的 contest.json；没有就返回 ([], None)。"""
        for path in self._memo_contest_candidates():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            teams = data.get("teams") if isinstance(data, dict) else None
            if isinstance(teams, list) and teams:
                return teams, path
        return [], None

    def _memo_clear_rows(self) -> None:
        for widget in getattr(self, "_memo_rows", []):
            try:
                widget.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._memo_rows = []
        self._memo_row_map = {}

    def _memo_reload(self) -> None:
        """重新从 contest.json 读选手列表并重建表格。"""
        if not hasattr(self, "memo_table"):
            return
        self._memo_clear_rows()
        teams, path = self._memo_read_teams()
        if not teams:
            self.memo_source.configure(
                text="还没找到带队伍的比赛数据 —— 先到「1 数据源」页点「拉取数据」（或「启动」让轮询跑一轮）。",
                fg=self.C["warn"])
            self._set_pill(self.memo_state, "没有数据", "warn")
            self._memo_loaded = False
            return
        try:
            alias_mod = self._alias_module()
            db = self._alias_db()
            aliases = {str(r.get("vjudge_uid")): r for r in alias_mod.list_rows(db)}
        except Exception as exc:  # noqa: BLE001 - 备注库坏了也要能看到选手列表
            alias_mod, aliases, db = None, {}, self._alias_db()
            self._log(f"[备注] 备注库读取失败（{type(exc).__name__}: {exc}），先只显示 handle\n")
        editable = self._platform() == "vjudge"
        for idx, team in enumerate(teams, start=1):
            uid = str(team.get("userId") or "").strip()
            if not uid:
                continue
            handle = str(team.get("vjudge_handle") or team.get("name") or uid)
            saved = aliases.get(uid) or {}
            values = {"handle": handle,
                      "display": str(saved.get("display") or ""),
                      "logo": str(saved.get("logo") or ""),
                      "note": str(saved.get("note") or "")}
            row_entries: dict[str, tk.Widget] = {}
            for col, (key, _title, width, is_edit) in enumerate(self.MEMO_COLUMNS):
                if key == "handle" or not is_edit:
                    widget = tk.Label(self.memo_table, text=values[key], bg=self.C["card"],
                                      fg=self.C["ink"], font=(UI_FONT, 9),
                                      anchor="w", padx=6, pady=2)
                    widget.grid(row=idx, column=col, sticky="nsew", padx=(0, 1), pady=1)
                    row_entries[key] = widget
                    continue
                entry = tk.Entry(self.memo_table, width=width, font=(UI_FONT, 9),
                                 relief="flat", bd=0, bg="#f7f9fc", fg=self.C["ink"],
                                 highlightthickness=1, highlightbackground="#d7dee8",
                                 state="normal", readonlybackground="#f7f9fc")
                entry.insert(0, values[key])
                # 先 normal 再 readonly：readonly 的 Entry 上 insert/delete 是**静默失效**的
                # （实测：直接建 readonly 会一个字都填不进去）
                entry.configure(state="readonly")
                entry.grid(row=idx, column=col, sticky="nsew", padx=(0, 1), pady=1)
                entry.bind("<Double-1>", lambda _e, u=uid, k=key: self._memo_edit_cell(u, k))
                entry.bind("<Return>", lambda _e, u=uid: self._memo_commit(u))
                entry.bind("<FocusOut>", lambda _e, u=uid: self._memo_commit(u))
                # 用户 2026-10-27：只读单元格里 Ctrl+V 会被 Tk 直接吃掉（静默失败）。
                # 这里显式接管：先切换到可编辑、再执行粘贴，并且给一个右键菜单。
                entry.bind("<Control-v>", lambda _e, u=uid, k=key: self._memo_paste(u, k))
                entry.bind("<Control-V>", lambda _e, u=uid, k=key: self._memo_paste(u, k))
                entry.bind("<Shift-Insert>", lambda _e, u=uid, k=key: self._memo_paste(u, k))
                entry.bind("<Button-3>", lambda e, u=uid, k=key: self._memo_menu(e, u, k))
                row_entries[key] = entry
            self._memo_rows.append(self.memo_table.grid_slaves(row=idx))
            self._memo_row_map[uid] = {"handle": handle, "entries": row_entries}
        self._memo_loaded = True
        self.memo_source.configure(
            text=f"读取到 {len(self._memo_row_map)} 名选手：{path}（双击单元格编辑）",
            fg=self.C["hint"])
        self._set_pill(self.memo_state, "可用" if editable else "只在 VJudge 平台可用",
                       "ok" if editable else "warn")
        self._sync_memo_page(rebuild=False)

    def _memo_edit_cell(self, uid: str, key: str) -> None:
        """双击单元格：变成可编辑并聚焦。"""
        if self._platform() != "vjudge":
            self._set_pill(self.memo_state, "只在 VJudge 平台可用", "warn")
            return
        row = getattr(self, "_memo_row_map", {}).get(uid)
        if not row:
            return
        entry = row["entries"].get(key)
        if entry is None:
            return
        try:
            entry.configure(state="normal", bg="white")
            entry.focus_set()
            entry.select_range(0, "end")
        except Exception:  # noqa: BLE001
            pass

    def _memo_paste(self, uid: str, key: str) -> str:
        """Ctrl+V / Shift+Insert：先把单元格切到可编辑，再真正粘贴。"""
        if self._platform() != "vjudge":
            return "break"
        self._memo_edit_cell(uid, key)
        row = getattr(self, "_memo_row_map", {}).get(uid) or {}
        entry = (row.get("entries") or {}).get(key)
        if entry is None:
            return "break"
        try:
            entry.event_generate("<<Paste>>")
        except Exception:  # noqa: BLE001
            try:
                entry.insert("insert", self.clipboard_get())
            except Exception:  # noqa: BLE001
                pass
        return "break"

    def _memo_menu(self, event, uid: str, key: str) -> None:
        """右键菜单：粘贴 / 复制 / 剪切 / 清空（都会先把格子切成可编辑）。"""
        row = getattr(self, "_memo_row_map", {}).get(uid) or {}
        entry = (row.get("entries") or {}).get(key)
        if entry is None:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="粘贴（Ctrl+V）", command=lambda: self._memo_paste(uid, key))
        menu.add_separator()
        menu.add_command(label="复制", command=lambda: self._memo_clip(entry, copy=True))
        menu.add_command(label="剪切", command=lambda: self._memo_clip(entry, copy=False))
        menu.add_command(label="清空这一格", command=lambda: self._memo_clear_cell(uid, key))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _memo_clip(self, entry, copy: bool) -> None:
        try:
            text = entry.selection_get()
        except Exception:  # noqa: BLE001
            text = entry.get()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception:  # noqa: BLE001
            pass
        if not copy:
            try:
                entry.configure(state="normal", bg="white")
                entry.focus_set()
                entry.delete("sel.first", "sel.last")
            except Exception:  # noqa: BLE001
                pass

    def _memo_clear_cell(self, uid: str, key: str) -> None:
        row = getattr(self, "_memo_row_map", {}).get(uid) or {}
        entry = (row.get("entries") or {}).get(key)
        if entry is None:
            return
        try:
            entry.configure(state="normal", bg="white")
            entry.delete(0, "end")
        except Exception:  # noqa: BLE001
            pass
        self._memo_commit(uid)

    def _memo_pick_logo(self) -> None:
        """选中某行的「校徽」格 → 选一张图 → 自动复制进 校徽\\ 目录并填好文件名。"""
        if self._platform() != "vjudge":
            self._set_pill(self.memo_state, "只在 VJudge 平台可用", "warn")
            return
        target = None
        for uid, row in (getattr(self, "_memo_row_map", {}) or {}).items():
            entry = (row.get("entries") or {}).get("logo")
            try:
                if entry is not None and entry.focus_get() is entry:
                    target = (uid, row, entry)
                    break
            except Exception:  # noqa: BLE001
                continue
        if target is None:
            self._set_pill(self.memo_state, "先双击某行的校徽格", "warn")
            return
        uid, row, entry = target
        path = filedialog.askopenfilename(
            title="选一张校徽图片（建议 128x128 PNG）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("所有文件", "*.*")])
        if not path:
            return
        src = Path(path)
        display = ""
        try:
            display = str((row.get("entries") or {}).get("display").get()).strip()
        except Exception:  # noqa: BLE001
            display = ""
        name_src = display or row.get("handle") or uid
        safe = "".join(ch for ch in name_src if ch not in '\\/:*?"<>|').strip() or uid
        logo_dir = cfgmod.work_root() / "校徽"
        try:
            logo_dir.mkdir(parents=True, exist_ok=True)
            dest = logo_dir / (safe + src.suffix.lower())
            dest.write_bytes(src.read_bytes())
        except Exception as exc:  # noqa: BLE001
            self._set_pill(self.memo_state, f"复制图片失败：{type(exc).__name__}", "err")
            return
        try:
            entry.configure(state="normal", bg="white")
            entry.delete(0, "end")
            entry.insert(0, dest.name)
        except Exception:  # noqa: BLE001
            pass
        self._memo_commit(uid)
        self._set_status(f"校徽已设置：{dest.name}（已复制到 {logo_dir}）")
    def _memo_commit(self, uid: str) -> None:
        """把这一行当前的值写进 SQLite 备注库（回车 / 失焦触发）。"""
        row = getattr(self, "_memo_row_map", {}).get(uid)
        if not row:
            return
        entries = row["entries"]
        try:
            display = str(entries["display"].get()).strip()
            logo = str(entries["logo"].get()).strip()
            note_entry = entries.get("note")
            note = str(note_entry.get()).strip() if note_entry is not None else ""
        except Exception:  # noqa: BLE001
            return
        for key in ("display", "logo", "note"):
            try:
                entries[key].configure(state="readonly", bg="#f7f9fc")
            except Exception:  # noqa: BLE001
                pass
        try:
            alias_mod = self._alias_module()
            alias_mod.set_alias(self._alias_db(), uid, row["handle"], display, logo, note)
        except Exception as exc:  # noqa: BLE001
            self._set_pill(self.memo_state, "保存失败", "err")
            self._log(f"[备注] 写入失败（uid={uid}）：{type(exc).__name__}: {exc}\n")
            return
        self._set_pill(self.memo_state, "已保存", "ok")
        self._set_status(f"备注已保存：{row['handle']}"
                         + (f" → {display}" if display else "（未填备注名 = 显示 handle）"))
        self._log(f"[备注] 已保存 uid={uid} handle={row['handle']} "
                  f"display={display!r} logo={logo!r}\n")

    def _sync_memo_page(self, rebuild: bool = True) -> None:
        """按平台决定这一页可用 / 置灰，并在 VJudge 时自动读一次列表。"""
        if not hasattr(self, "memo_table"):
            return
        vjudge = (self._platform() == "vjudge")
        for widget in getattr(self, "_memo_widgets", []):
            try:
                widget.configure(state="normal" if vjudge else "disabled")
            except Exception:  # noqa: BLE001
                pass
        if not vjudge:
            self.memo_source.configure(
                text=("当前平台是 PTA（拼题 A）：PTA 有学号和真实姓名，不需要备注页。"
                      "把「平台」切到 VJudge 后这一页才可用。"),
                fg=self.C["hint"])
            self._set_pill(self.memo_state, "只在 VJudge 平台可用", "warn")
            for row in getattr(self, "_memo_row_map", {}).values():
                for key in ("display", "logo", "note"):
                    try:
                        row["entries"][key].configure(state="disabled")
                    except Exception:  # noqa: BLE001
                        pass
            return
        for row in getattr(self, "_memo_row_map", {}).values():
            for key in ("display", "logo", "note"):
                try:
                    row["entries"][key].configure(state="readonly", bg="#f7f9fc")
                except Exception:  # noqa: BLE001
                    pass
        # 每次进这一页都重读一次：比赛里随时会进来新选手，
        # 原来只在第一次打开时读（用户 2026-10-31 反馈：新选手在备注页看不到）。
        if rebuild:
            self._memo_reload()

    # == 实时榜单 · 1 数据源 · 环境预检 ==================================

    #: 预检项的显示前缀：✅ 通过 / ⚠️ 提醒 / ❌ 不行（Text 里按颜色分标签）。
    PRECHECK_MARKS = {"ok": "✅", "warn": "⚠️", "err": "❌"}

    def _build_precheck_card(self, page) -> None:
        """「环境预检」卡：页面最上面的醒目卡片（按钮 + 结果区 + 红横幅）。

        用户反馈「没看到环境预检在哪点」—— 所以卡片和「▶ 开始检查」按钮都在
        首屏最上面，且**启动后自动跑一次**（静默，不弹窗）。
        """
        # 失败横幅：先建好、不 pack；有 ❌ 时才插到「这页怎么用」步骤带下面。
        self.precheck_banner = self._f(page, bg="#fdecec",
                                       highlightbackground="#e8877f", highlightthickness=1, bd=0)
        self.precheck_banner_var = tk.StringVar(value="")
        banner_label = tk.Label(self.precheck_banner, textvariable=self.precheck_banner_var,
                                bg="#fdecec", fg="#b3261e", font=(UI_FONT, 10, "bold"),
                                anchor="w", justify="left", wraplength=420)
        banner_label.pack(fill="x", padx=12, pady=8)
        self._auto_wrap(banner_label, source=self.precheck_banner, min_width=220)

        body = self._card(page, "环境预检（一键检查这台机器能不能跑）",
                          "比赛前先跑一次：每项都说明「是干什么的」，结果是 ✅ 通过 / ⚠️ 提醒 / "
                          "❌ 不行，并给出「怎么办」")

        row = self._form_row(body, "开始检查")
        self.precheck_btn = self._solid_btn(row, "▶ 开始检查", self.on_precheck,
                                            self.C["accent"], self.C["accent_hover"])
        self.precheck_btn.pack(side="left")
        ToolTip(self.precheck_btn,
                "逐项检查（每项都会说清是干什么的）：\n"
                "· Cookie 是否有效 —— 会真的请求一次 PTA 接口（不是只看文件在不在）\n"
                "· 服务端口是否空闲 —— 端口被占就起不来榜单服务\n"
                "· Python 解释器是否可用 —— 实时服务要用它跑 poller / server\n"
                "· Pillow、字体等依赖是否就绪 —— 校徽裁圆、队名不出方框\n"
                "· 历史数据目录可写 —— 运行记录、快照要写得进去\n"
                "检查只读：不写任何比赛数据、不弹窗。")
        self.precheck_state = self._pill(row, "还没检查", "hint")
        self.precheck_state.pack(side="left", padx=10)

        self.precheck_text = tk.Text(body, height=9, width=1, wrap="word", undo=False,
                                     font=("Microsoft YaHei UI", 9), bg="#f7f9fc",
                                     fg=self.C["sub"], relief="flat", bd=0,
                                     padx=8, pady=6)
        self.precheck_text.tag_configure("ok", foreground="#166534")
        self.precheck_text.tag_configure("warn", foreground="#92400e")
        self.precheck_text.tag_configure("err", foreground="#b3261e")
        self.precheck_text.tag_configure("fix", foreground="#475569")
        bar = ttk.Scrollbar(body, orient="vertical", command=self.precheck_text.yview)
        self.precheck_text.configure(yscrollcommand=bar.set)
        self.precheck_text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.precheck_text.insert("1.0", "还没跑过环境预检。点上面的「▶ 开始检查」，"
                                        "每一项都会写成「是干什么的 + 可以 / 不行 + 怎么办」。")
        self.precheck_text.configure(state="disabled")

    def _set_precheck_banner(self, text: str) -> None:
        """页面顶部红横幅：有 ❌ 时显示，全通过时收起。"""
        banner = getattr(self, "precheck_banner", None)
        if banner is None:
            return
        if text:
            self.precheck_banner_var.set(text)
            if not banner.winfo_ismapped():
                anchor = getattr(self, "_step_bands", {}).get(0)
                if anchor is not None:
                    banner.pack(fill="x", padx=24, pady=(8, 0), after=anchor)
                else:
                    banner.pack(fill="x", padx=24, pady=(8, 0))
        elif banner.winfo_ismapped():
            banner.pack_forget()
        self._fit_all_hints()

    def _build_cookie_card(self, parent, key: str) -> None:
        """Cookie 那一行。

        实时榜单和赛后滚榜**各有一张卡**（各自能改、各自有用），但读写的都是
        同一份 ``cookie.txt``、同一份设置 —— 不是两套值。
        """
        row = self._form_row(parent, "Cookie")
        pill = self._pill(row, "检查中 …", "hint")
        pill.pack(side="left")
        self._ghost_btn(row, "测试 Cookie", self.on_test_cookie).pack(side="left", padx=(10, 6))
        self._ghost_btn(row, "粘贴 Cookie", self.on_paste_curl).pack(side="left")
        self._ghost_btn(row, "打开工作目录",
                        lambda: self._open_folder(cfgmod.work_root())).pack(side="left", padx=6)
        row2 = self._indent_row(parent)
        hint = self._hint(row2, "")
        hint.pack(side="left", fill="x", expand=True)
        self._cookie_widgets[key] = {"pill": pill, "hint": hint}

    def _refresh_cookie_pill(self) -> None:
        """Cookie 状态药丸 + 就地提示。

        2026-10-13：PTA 下 Cookie 是**必需**的（没有「借用浏览器」这条路了）。
        VJudge 公开赛**不需要 Cookie** —— 这里显示「可留空」，不再报红。
        """
        if not self._cookie_widgets:
            return
        if getattr(self, "platform_var", None) is not None and self._platform() == "vjudge":
            for entry in self._cookie_widgets.values():
                self._set_pill(entry["pill"], "VJudge 公开赛可留空", "ok")
                entry["hint"].configure(
                    text=("VJudge 公开比赛匿名取数，**不需要 Cookie**（可以留空）。"
                          "只有私有 / 需要登录的比赛才要填：浏览器登录 vjudge.net 后"
                          "F12 → Network 复制 Cookie 粘到这里（本版本没有做密码赛登录）。"),
                    fg=self.C["hint"])
            self._fit_all_hints()
            return
        ok, note = live.cookie_status(cfgmod.work_root())
        problem = live.cookie_problem(cfgmod.work_root())
        for entry in self._cookie_widgets.values():
            if ok and not problem:
                self._set_pill(entry["pill"], "Cookie 有效", "ok")
                entry["hint"].configure(
                    text=(note + "。取数方式固定为「直连接口」，它就靠这份 Cookie；"
                          "失效时程序会停止取数并提示更新（不会去动浏览器、不会产生下载）。"),
                    fg=self.C["hint"])
                continue
            self._set_pill(entry["pill"], "Cookie 缺失/缺字段", "err")
            entry["hint"].configure(
                text=("⚠️ " + (problem or note) + "。**实时榜单必须有有效 Cookie，"
                      "否则取不到任何数据**（程序不会去借浏览器）。"
                      "怎么更新 Cookie（两条做法任选一条，都不用记分号）："
                      "① 浏览器里登录 pintia.cn 并打开这场比赛 → F12 → Network → 刷新页面 → "
                      "右键任意 pintia.cn/api/… 请求 → Copy → Copy as cURL (bash) → "
                      "回到这里点「粘贴 Cookie」，整段粘进第一个框（一个字符都不用改）；"
                      "② 只粘两个值：F12 → Application → Cookies → https://pintia.cn → 在 "
                      "Request cookies 里分别复制 JSESSIONID 与 PTASession 两个值"
                      "（点一行后在 Value 列复制，别双击值单元格），"
                      "分别粘进对话框里的「JSESSIONID」「PTASession」两个框即可。**两个都必须带**："
                      "实测只带 PTASession 会 401。"),
                fg=self.C["err"])
        self._fit_all_hints()

    def _refresh_port_pill(self) -> None:
        if not hasattr(self, "port_state"):
            return
        try:
            port = int(self.port_var.get())
        except (TypeError, ValueError):
            port = int(self.live_cfg.get("port", live.DEFAULT_PORT))
        in_use = live.port_in_use(port)
        mine = self.service.any_running() and port == int(self.live_cfg.get("port") or 0)
        if in_use and not mine:
            pids = live.listening_pids(port)
            self._set_pill(self.port_state, f"{port} 被占用", "err")
            self._msg_hint_live(
                f"端口 {port} 被别的程序占用了（PID {pids}）。怎么办，二选一："
                f"① 到「4 服务」页点「终止旧服务」把上次没关干净的服务收掉；"
                f"② 或者把上面这个端口号改成一个没被占用的（点「4 服务」页的「换一个空闲端口」）。"
                f"本程序不会偷偷帮你杀进程。")
        elif in_use:
            self._set_pill(self.port_state, f"{port} 运行中", "ok")
            self._msg_hint_live("")
        else:
            self._set_pill(self.port_state, f"{port} 空闲", "hint")
            self._msg_hint_live("")

    def _msg_hint_live(self, text: str, kind: str = "hint") -> None:
        if not text:
            text = ("实时榜单服务还没启动。点「4 服务」页的「启动」以后，榜单页就在 "
                    f"http://127.0.0.1:{self.port_var.get()}/board"
                    "（投屏/OBS 用「5 直播」页那个带参数的网址）。")
        self.live_action_hint.configure(text=text, fg=self.KIND[kind])
        self._fit_all_hints()

    def _psid_msg(self, text: str | None = None, error: bool = False) -> None:
        """题目集 / VJudge 比赛 ID 的就地反馈（两个模式各有一个输入框，同一套提示）。

        ``BoardApp._psid_msg`` 用 psid_hint 的文案，这里再同步到实时页的
        ``psid_hint_live``；平台 = VJudge 时换成 VJudge 口径（粘网址也行）。
        """
        BoardApp._psid_msg(self, text, error)
        hint = getattr(self, "psid_hint_live", None)
        if hint is not None:
            src = getattr(self, "psid_hint", None)
            if src is not None:
                hint.configure(text=src.cget("text"), fg=src.cget("fg"))
        if text is None and self._platform() == "vjudge" and hint is not None:
            hint.configure(
                text=("VJudge 比赛 ID：填 862258 这种数字，或直接粘 "
                      "https://vjudge.net/contest/862258#rank（粘网址也行，会自动抽出 ID）"),
                fg=self.C["hint"])
        self._sync_fetch_buttons()

    # == 实时榜单 · 2 外观：榜单页字体 / 配色 / 奖项着色 / 校徽 ==========

    def _build_live_display_tab(self) -> None:
        """实时榜单模式第 2 页：榜单页外观。

        只有榜单页自己的东西：字体、13 题配色、奖项区块金/银/铜、校徽目录。
        """
        page = self.tab_display_live

        body_font = self._card(page, "榜单页字体",
                               "榜单页投到大屏上用的字体；中文推荐 yahei（微软雅黑）")
        row = self._form_row(body_font, "字体")
        var = tk.StringVar(value=str((self.live_cfg.get("view") or {}).get("font", "yahei")))
        self.view_vars["font"] = var
        combo = ttk.Combobox(row, textvariable=var, state="readonly", width=12,
                             values=["yahei", "helvetica", "default"])
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_live_page())
        ToolTip(combo, "榜单页用的字体：yahei=微软雅黑（中文推荐）、"
                       "helvetica=欧文字体、default=浏览器默认。")
        row = self._indent_row(body_font)
        self._hint(row, "这一项只影响榜单页怎么显示，不改任何榜单数据；"
                        "改完「5 直播」页那条网址里的参数会跟着更新。"
                  ).pack(side="left", fill="x", expand=True)


        medal_body = self._card(page, "奖项区块颜色（金 / 银 / 铜）",
                                "榜单上按当前名次分块上色：第 1 个奖的人数 = 前面几名上金，"
                                "第 2 个接着上银、第 3 个上铜；名次一变颜色跟着变")
        row = self._form_row(medal_body, "金 / 银 / 铜")
        self._medal_rows = []
        for i, name in enumerate(("金", "银", "铜")):
            swatch = tk.Label(row, text=name, width=3, height=1,
                              font=(UI_FONT, 9, "bold"),
                              bg=str((self.live_cfg.get("medal_colors")
                                      or DEFAULT_MEDAL_COLORS)[i]),
                              fg="#16202e", relief="flat", bd=0, cursor="hand2")
            swatch.pack(side="left", padx=(0, 6))
            swatch.bind("<Button-1>",
                        lambda _e, n=i, w=swatch: self._pick_medal(n, w))
            self._medal_rows.append(swatch)
        self._ghost_btn(row, "恢复默认配色", self.on_reset_medal_colors).pack(side="left", padx=10)
        ToolTip(row, "点色块改颜色。金/银/铜分别对应下面「奖项清单」里的第 1、2、3 个奖项；"
                     "第 4 个奖项往后不上色。")
        medal_hint = self._indent_row(medal_body)
        self._hint(medal_hint, "实时榜单按当前名次上色：第 1 个奖的「共 N 队」= 当前前 N 名上金、"
                               "第 2 个接着上银、第 3 个上铜，第 4 个往后不上色 —— 名次移动，颜色跟着移动。"
                               "（滚榜生成包那边是另一套规则：按「全队标记」里勾的名单发奖。）"
                  ).pack(side="left", fill="x", expand=True)

        # ---- M17c：实时榜单这边**只改奖项名 + 人数**（金/银/铜各几个名额）----
        # 用户口径：「实时榜不用改勾选了，只用改金银铜对应的人数就行了」——
        # 所以这里用**同一个 AwardEditor** 的 compact 模式（同一套实现、只是少了几块）：
        # 没有全队标记矩阵 / 勾选列 / 已选支数 / 按*自动勾选 / 名次填 / 清空，
        # 每行只有「奖项名 + 共 N 队 + 展示方式 + 删除」和一行名次区间提示。
        # 数据仍然是 config.json 里那一份 cfg["awards"]：这里改名字/人数，滚榜侧立刻同步；
        # **名单(team_ids)只能在滚榜侧勾**，compact 编辑器会把它们原样带出来（不显示、不丢）。
        award_card = self._card(
            page, "奖项表（与滚榜模式共用的同一份）",
            "这里只改奖项名 + 人数：第 1 个奖的人数 = 当前前 N 名上金，第 2 个接着银、"
            "第 3 个铜（按当前名次，名次动颜色跟着动）。勾选队伍在滚榜模式那边")
        builder = getattr(self, "build_award_editor", None)
        if callable(builder):
            try:
                builder("live", award_card, hint_wrap=300, compact=True)
            except TypeError:                     # 老基类没有 compact 参数时的兜底
                builder("live", award_card, hint_wrap=300)
        else:                                     # 极端情况（老基类）也别崩
            self._hint(award_card, "当前版本不支持在实时榜单这边编辑奖项表，"
                                   "请到「滚榜 → 2 外观」改。").pack(anchor="w", padx=12, pady=6)
        self._award_sync_hint = self._hint(award_card, "", size=9)
        self._award_sync_hint.pack(anchor="w", fill="x", padx=12, pady=(2, 8))
        self._set_award_sync_hint()


        # ---- M40（用户 2026-10-18）：实时榜单这边**只**加「打星」勾选 ----
        #   不搬全队标记矩阵、不碰奖项表：奖照样看「滚榜 → 2 外观」的全队标记；
        #   这里勾上的队，实时榜单显示「黄色六边形 + 星」，没勾的是「蓝色六边形 + 点」。
        star_card = self._card(
            page, "打星队伍（手动勾选，只影响标识）",
            "勾上 = 实时榜单显示黄色六边形 + 星；不勾 = 蓝色六边形 + 点。"
            "打星不影响排名、罚时，也不决定奖项（奖按「滚榜 → 2 外观」的全队标记）。")
        self._star_card_body = star_card
        self._build_star_picker()

        # 校徽：和「赛后滚榜」模式的外观页共用同一个目录设置（同一份 config）
        logos_body = self._card(page, "校徽目录",
                                "榜单每一行前面的校徽图；留空就用默认目录（不存在会自动创建）")
        row = self._form_row(logos_body, "校徽目录")
        logos_entry = self._entry(row, self.logos_var, 34)
        logos_entry.pack(side="left")
        self._ghost_btn(row, "选择…", self._pick_logos_dir).pack(side="left", padx=(8, 0))
        self._ghost_btn(row, "默认目录", self._reset_logos_dir).pack(side="left", padx=(6, 0))
        ToolTip(logos_entry,
                "放校徽的文件夹。文件名用学号或姓名就是那支队的专属校徽；\n"
                "空名字（.jpg）或 logo / 校徽 / default / 机构 当作默认校徽。\n"
                "实时榜单服务启动时会把校徽裁成圆形放进运行目录，榜单页用 /logo/<队伍ID> 取图。")
        row = self._indent_row(logos_body)
        self._hint(row, "这个目录和「赛后滚榜」模式里的校徽目录是同一份设置："
                        "改哪边都会保存到同一个设置文件，切过去看到的是同一个值。"
                  ).pack(side="left", fill="x", expand=True)

    def _set_award_sync_hint(self) -> None:
        """实时侧奖项表下面那行小字（M17d：压成一句，别和「金银铜名额」卡重复）。"""
        hint = getattr(self, "_award_sync_hint", None)
        if hint is None:
            return
        try:
            hint.configure(text="与滚榜共用同一份奖项表：这里改名字 / 名额，那边立刻同步",
                           fg=self.C["hint"])
        except Exception:  # noqa: BLE001
            pass

    def _after_awards_changed(self) -> None:
        """奖项表**任何一侧**被改动后的收尾（M17）：写运行目录 + 刷新榜单页清单 + 给提示。

        基类 `AwardEditor` 已经在改动时把 ``cfg["awards"]`` 更新好、并把另一侧界面同步了；
        这里负责"让实时榜单那边也跟上"：

        1. 重建 ``live.view.awards``（``board_config.live_view_awards``：每行都带 count，
           有名单的行另带 team_ids —— 实时榜读 count、滚榜读 team_ids）；
        2. **立刻**把 ``view`` 写进运行目录 ``live\\config.json`` —— ``server.py`` 的
           ``/api/config`` 与 ``presentation.load_config()`` 都是**每次请求重读**这个文件，
           所以**不用重启服务**，榜单页刷新（F5）就能看到新名单/新配色；
        3. 刷新「奖项清单」那张只读摘要卡与提示行。
        """
        try:
            self.cfg["awards"] = self.collect_awards()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._collect_live_config()          # 重建 live.view.awards（含 count=0 口径）
        except Exception:  # noqa: BLE001
            pass
        view = dict((self.live_cfg.get("view") or {}))
        try:
            run = live.run_dir(cfgmod.work_root())
            live.update_live_view(run, {"awards": view.get("awards") or [],
                                        "awards_mode": view.get("awards_mode") or "rows"})
        except Exception:  # noqa: BLE001 - 写不进去也不影响界面（启动服务时会写）
            pass
        self.live_cfg["awards_source"] = "奖项表（滚榜 / 实时共用同一份 config.json 的 awards）"
        # M17d：提示只留一句（"F5 即生效 / 不用重启"那句在「金银铜名额」卡底部）
        self._set_award_sync_hint()
        try:
            self._set_award_sync_hint()          # 那行小字跟着更新
        except Exception:  # noqa: BLE001
            pass
        try:
            self._persist()
        except Exception:  # noqa: BLE001
            pass
        self._set_status("奖项表已更新（滚榜 / 实时共用同一份）")

    def _pick_medal(self, index: int, swatch: tk.Label) -> None:
        from tkinter import colorchooser
        current = (self.live_cfg.get("medal_colors") or DEFAULT_MEDAL_COLORS)[index]
        rgb, hexv = colorchooser.askcolor(color=current, title="奖项区块着色", parent=self.root)
        if not hexv:
            return
        colors = list(self.live_cfg.get("medal_colors") or DEFAULT_MEDAL_COLORS)
        colors[index] = hexv.upper()
        self.live_cfg["medal_colors"] = colors
        swatch.configure(bg=hexv)
        self._persist()
        self._refresh_live_page()

    def on_reset_medal_colors(self) -> None:
        """奖项区块着色恢复默认（M17e：每题配色那张卡已删，这里只管金/银/铜）。"""
        self.live_cfg["medal_colors"] = list(DEFAULT_MEDAL_COLORS)
        for i, swatch in enumerate(self._medal_rows):
            swatch.configure(bg=DEFAULT_MEDAL_COLORS[i])
        self._persist()
        self._refresh_live_page()
        self._set_status("奖项区块着色已恢复默认")

    # == 历史：加模式/起止时间/结果列 ==================================

    # == 历史：两个模式各一张表，各自只列自己的记录 ====================

    HISTORY_COLUMNS = ("psid", "name", "problems", "teams", "subs", "last",
                       "mode", "started_at", "result")
    HISTORY_HEADS = {"psid": "题目集 ID", "name": "比赛名称", "problems": "题", "teams": "队",
                     "subs": "提交", "last": "最后使用", "mode": "记录类型",
                     "started_at": "开始时间", "result": "结果"}

    def _build_history_page(self, page, attr: str, card_title: str, card_desc: str,
                            empty_title: str, empty_hint: str):
        """建一张「历史」表。两个模式各建一张，互不共用。

        ``attr`` 是树控件属性名（实时 = ``tree_live``，滚榜 = ``tree``），
        因为基类方法认的是 ``self.tree`` / ``self.history_empty``。
        """
        body = self._card(page, card_title, card_desc, expand=True)
        holder = self._f(body)
        holder.pack(fill="both", expand=True)

        tree = ttk.Treeview(holder, columns=self.HISTORY_COLUMNS, show="headings",
                            selectmode="extended")
        for col in self.HISTORY_COLUMNS:
            tree.heading(col, text=self.HISTORY_HEADS[col])
        tree.tag_configure("odd", background="#f6f8fb")
        tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.bind("<Double-1>", self.on_history_load)

        empty = self._f(holder)
        self._t(empty, empty_title, size=12, bold=True,
                fg=self.C["hint"]).pack(pady=(0, 6))
        self._t(empty, empty_hint, size=9, fg=self.C["hint"],
                justify="center").pack()

        bar = self._f(body)
        bar.pack(fill="x", pady=(10, 0))
        self._ghost_btn(bar, "载入选中", self.on_history_load).pack(side="left")
        self._ghost_btn(bar, "删除选中", self.on_history_delete).pack(side="left", padx=6)
        self._ghost_btn(bar, "清空本模式记录", self.on_history_clear).pack(side="left")
        self._ghost_btn(bar, "打开工作目录",
                        lambda: self._open_folder(cfgmod.work_root())).pack(side="right")

        setattr(self, attr, tree)
        setattr(self, attr + "_empty", empty)
        self._apply_widths(tree, budget=1000)
        return tree

    # ---- M40：实时榜单的「打星」勾选（手动，无自动判断） -----------------
    def _star_ids(self) -> list[str]:
        raw = self.cfg.get("star_team_ids")
        if raw is None:
            raw = (self.cfg.get("view") or {}).get("star_team_ids")
        return [str(x) for x in (raw or []) if str(x).strip()]

    def _star_picker_teams(self) -> list[dict]:
        """读当前比赛数据里的队伍（按名次排），给打星勾选用。"""
        try:
            text = str(self.cfg.get("last_contest_json") or "").strip()
            # 取最新的一份（程序根的 contest.json 可能比 live\contest.json 旧，
            # 那会导致备注改了名字、这里还显示 handle）
            path = cfgmod.freshest_contest(text or None)
            penalty = int(self.cfg.get("penalty_minutes") or 20)
        except Exception:  # noqa: BLE001
            return []
        try:
            # 注意签名是 contest_team_rows(contest_path, penalty_minutes)，位置传参最稳。
            rows = backend.contest_team_rows(path, penalty) or []
        except Exception:  # noqa: BLE001
            return []
        rows = [r for r in rows if str(r.get("team_id") or "").strip()]
        rows.sort(key=lambda r: (int(r.get("rank") or 0) or 999, str(r.get("name") or "")))
        return rows

    def _build_star_picker(self) -> None:
        """把队伍列成勾选框（3 列），点一下立刻写配置并推给实时运行目录。"""
        body = getattr(self, "_star_card_body", None)
        if body is None:
            return
        for child in list(body.winfo_children()):
            if getattr(child, "_star_picker", False):
                child.destroy()
        host = tk.Frame(body, bg=body.cget("bg"))
        host._star_picker = True  # type: ignore[attr-defined]
        host.pack(fill="x", padx=12, pady=(2, 8))
        teams = self._star_picker_teams()
        if not teams:
            self._hint(host, "还没有队伍数据：先到「1 数据源」点「拉取数据」，再回来点下面的"
                             "「刷新队伍列表」（拉取成功后这里会自动刷新）。"
                       ).pack(anchor="w")
            row0 = tk.Frame(host, bg=host.cget("bg"))
            row0.pack(anchor="w", pady=(6, 0))
            self._ghost_btn(row0, "刷新队伍列表", self._build_star_picker).pack(side="left")
            return
        marked = set(self._star_ids())
        grid = tk.Frame(host, bg=host.cget("bg"))
        grid.pack(anchor="w", fill="x")
        for i, team in enumerate(teams):
            tid = str(team.get("team_id") or "")
            name = str(team.get("name") or "（无队名）").replace("*", "").replace("＊", "").strip()
            number = str(team.get("student_number") or "").strip()
            rank = team.get("rank") or ""
            label = "%s. %s%s" % (rank, name, ("（%s）" % number) if number else "")
            var = tk.BooleanVar(value=tid in marked)
            cb = tk.Checkbutton(
                grid, text=label, variable=var, bg=host.cget("bg"),
                activebackground=host.cget("bg"), selectcolor="white", anchor="w",
                bd=0, highlightthickness=0, cursor="hand2", takefocus=0,
                command=lambda t=tid, v=var: self._on_star_toggle(t, bool(v.get())))
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 14), pady=1)
        row = tk.Frame(host, bg=host.cget("bg"))
        row.pack(anchor="w", pady=(4, 0))
        self._ghost_btn(row, "刷新队伍列表", self._build_star_picker).pack(side="left")
        self._hint(row, "勾完回榜单按 F5 就生效；勾选保存在 config.json 的 star_team_ids。"
                  ).pack(side="left", padx=(10, 0))

    def _on_star_toggle(self, team_id: str, on: bool) -> None:
        ids = [x for x in self._star_ids() if x != team_id]
        if on:
            ids.append(team_id)
        self.cfg["star_team_ids"] = ids
        view = self.cfg.get("view")
        if not isinstance(view, dict):
            view = {}
            self.cfg["view"] = view
        view["star_team_ids"] = ids
        try:
            self._persist()
        except Exception:  # noqa: BLE001
            pass
        try:
            run = live.run_dir(cfgmod.work_root())
            live.update_live_view(run, {"star_team_ids": ids})
            self._set_status("打星已更新：%d 支（榜单 F5 生效）" % len(ids))
        except Exception:  # noqa: BLE001
            self._set_status("打星已保存（实时运行目录暂时没写进去，起服务时会补）")

    def _build_live_history_tab(self) -> None:
        self._build_history_page(
            self.tab_history_live, "tree_live",
            "实时榜单用过的比赛",
            "只显示「实时榜单」模式的记录：这一栏看不到赛后滚榜的记录",
            "还没有实时榜单的记录",
            "怎么才会有记录：在「1 数据源」页填题目集 ID 并启动实时榜单服务，\n"
            "或者比赛结束前一直开着服务；每次运行都会在这里留一条。")

    def _build_roll_history_tab(self) -> None:
        tree = self._build_history_page(
            self.tab_history, "tree",
            "滚榜用过的比赛",
            "只显示「赛后滚榜」模式的记录：这一栏看不到实时榜单的记录",
            "还没有滚榜的记录",
            "怎么才会有记录：到「1 数据源」页填题目集 ID、点「拉取数据」，\n"
            "抓过的比赛会自动出现在这里；下次双击那一行就能一键载入。")
        self.history_empty = getattr(self, "tree_empty")
        return tree

    def _hist_tree(self):
        """当前模式该用哪张历史表。"""
        return getattr(self, "tree_live" if self.mode == MODE_LIVE else "tree", None)

    def _hist_pairs(self):
        return ((MODE_LIVE, getattr(self, "tree_live", None),
                 getattr(self, "tree_live_empty", None)),
                (MODE_ROLL, getattr(self, "tree", None),
                 getattr(self, "tree_empty", None)))

    @staticmethod
    def _record_mode(rec: dict) -> str:
        mode = str(rec.get("mode") or "").strip().lower()
        return mode if mode in (MODE_LIVE, MODE_ROLL) else MODE_ROLL

    def _apply_widths(self, tree, budget: int) -> None:
        """历史表列宽：9 列全显示要 700px 左右，窄窗口收起「题 / 队 / 提交」三列。"""
        if tree is None:
            return
        cols = list(tree["columns"])
        small = [c for c in ("problems", "teams", "subs") if c in cols]
        show_small = budget >= 700
        # 116(psid) + 88(last) + 78(mode) + 82(started_at) + 84(result) = 448
        fixed = 116 + 88 + 78 + 82 + 84 + (sum((30, 30, 44)) if show_small else 0)
        name = max(110, min(320, int(budget) - fixed))
        widths = {"psid": 116, "name": name, "problems": 30, "teams": 30, "subs": 44,
                  "last": 88, "mode": 78, "started_at": 82, "result": 84}
        try:
            for key, width in widths.items():
                if key in cols:
                    tree.column(key, width=width, minwidth=min(width, 60),
                                stretch=False, anchor="w")
            tree.configure(displaycolumns=[c for c in cols if show_small or c not in small])
        except Exception:  # noqa: BLE001
            pass

    def _on_budget_changed(self, budget: int) -> None:
        """内容区宽度变了（_run_fit 回调）：让两张历史表的列宽跟着走。

        ``budget`` 是「内容区宽度 − 132（页面/卡片内边距 + 表单左栏）」的估算；
        历史表右边还有一个竖向滚动条没算进去，所以要再按表格所在容器实际宽度
        收一次 —— 否则列宽合计会超出可视宽度（900x600 实测 +32px）。
        """
        for _mode, tree, _empty in self._hist_pairs():
            if tree is None:
                continue
            cap = int(budget)
            try:
                holder_w = int(tree.master.winfo_width())
                if holder_w > 1:
                    cap = min(cap, holder_w - 20)
            except Exception:  # noqa: BLE001
                pass
            self._apply_widths(tree, cap)

    def _refresh_history(self) -> None:
        """按模式分别装填两张历史表：每张只放自己模式的记录，并显示记录类型。"""
        self.history_rows = []
        for mode, tree, empty in self._hist_pairs():
            if tree is None:
                continue
            for item in tree.get_children():
                tree.delete(item)
            rows = [r for r in self.store.history if self._record_mode(r) == mode]
            for i, rec in enumerate(rows):
                tree.insert("", "end", values=(
                    rec.get("problem_set_id", ""),
                    rec.get("name", ""),
                    rec.get("problem_count", 0),
                    rec.get("team_count", 0),
                    rec.get("submission_count", 0),
                    rec.get("last_used", ""),
                    MODE_SHORT.get(mode, "—"),
                    str(rec.get("started_at") or rec.get("last_used") or ""),
                    str(rec.get("result") or "")[:60],
                ), tags=("odd",) if i % 2 else ())
            if empty is not None:
                if rows:
                    empty.place_forget()
                else:
                    empty.place(relx=0.5, rely=0.42, anchor="center")

    def on_history_load(self, _event=None) -> None:
        """双击某一行 = 载入那场比赛的设置（只认当前模式的那张表）。"""
        tree = self._hist_tree()
        if tree is None:
            return
        selection = tree.selection()
        if not selection:
            return
        values = tree.item(selection[0], "values")
        if not values:
            return
        psid = str(values[0])
        rec = self.store.find(psid)
        if not rec:
            return
        if self.mode == MODE_LIVE:
            self.psid_var.set(psid)
            if rec.get("logos_dir"):
                self.logos_var.set(str(rec["logos_dir"]))
            self._psid_msg()
            self._persist()
            self._log(f"[历史] 已载入《{rec.get('name')}》（题目集 {psid}）的题目集 ID。\n")
            self._set_status(f"已载入历史比赛：{rec.get('name')}")
            self._nav_select(self.tab_datasource_live)   # 回到「1 数据源」，接着点启动
            return
        self.tree = tree                                # 基类方法认的就是这个名字
        BoardApp.on_history_load(self, _event)

    def on_history_delete(self) -> None:
        tree = self._hist_tree()
        if tree is None:
            return
        if not tree.selection():
            messagebox.showinfo("没选东西", "请先在列表里选中要删除的比赛（可以按住 Ctrl 多选）。")
            return
        self.tree = tree
        BoardApp.on_history_delete(self)

    def on_history_clear(self) -> None:
        """只清空**当前模式**的记录，不动另一个模式的历史。"""
        mode = self.mode
        rows = [r for r in self.store.history if self._record_mode(r) == mode]
        if not rows:
            messagebox.showinfo("这个模式还没有记录",
                                f"「{MODE_LABELS[mode]}」现在没有任何历史记录。")
            return
        if not messagebox.askyesno(
                "确认清空",
                f"清空「{MODE_LABELS[mode]}」的 {len(rows)} 条历史记录？\n"
                f"（另一个模式的历史不受影响）"):
            return
        self.store.history = [r for r in self.store.history if self._record_mode(r) != mode]
        self._persist()
        self._refresh_history()
        self._log(f"[历史] 已清空「{MODE_LABELS[mode]}」的记录。\n")

    def _stamp_history(self, **fields) -> None:
        """给历史里刚写的那条补字段（不 import 那 3 个私有函数，直接改 JSON）。"""
        psid = str(self.cfg.get("problem_set_id") or "")
        if not psid:
            return
        path = self.store.history_path
        data = live.read_json(path, None)
        if not isinstance(data, list):
            return
        want_mode = str(fields.get("mode") or "")
        hit = False
        for rec in data:
            if not isinstance(rec, dict) or str(rec.get("problem_set_id")) != psid:
                continue
            # 同一个题目集 ID 在两种模式下各有一条记录：只补当前模式那条
            if want_mode and self._record_mode(rec) != want_mode:
                continue
            for key, value in fields.items():
                if value not in (None, ""):
                    rec[key] = value
            hit = True
        if not hit:
            return
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
        self.store.history = cfgmod.sanitize_history(data)

    def _remember_live_run(self, result_text: str = "实时服务运行中") -> None:
        """实时榜单跑起来/刷新后，往历史里记一条**属于实时模式**的记录。"""
        psid = str(self.cfg.get("problem_set_id") or "")
        if not psid:
            return
        try:
            stats = live.poller_stats(self.service.poller_log)
            self.store.remember({
                "problem_set_id": psid,
                "name": str(self.live_cfg.get("view", {}).get("title") or "")
                        or f"题目集 {psid}",
                "problem_count": int(stats.get("problems") or 0),
                "team_count": int(stats.get("teams") or 0),
                "submission_count": int(stats.get("submissions") or 0),
                "contest_json": str(self.service.contest_path),
                "logos_dir": self.cfg.get("logos_dir") or "",
                "font_path": self.cfg.get("font_path") or "",
                "mode": MODE_LIVE,
                "started_at": self._op_started or time.strftime("%Y-%m-%d %H:%M:%S"),
                "result": result_text,
            })
            self._persist()
            self._refresh_history()
        except Exception:  # noqa: BLE001 - 记历史失败不能反过来影响服务
            pass

    # ================================================================
    # 赛后滚榜：1 数据源 / 2 外观 / 3 滚榜
    # ================================================================
    # 说明：这三页的属性名（psid_var / font_var / photos_var / pkg_var /
    # play_var / award_rows …）**和基类一模一样** —— 基类的 _apply_config、
    # _collect_config、_load_fonts、on_fetch、on_package、on_start_roll、
    # _update_mode_line 等几十个方法都直接按这些名字取控件。所以这里是「把
    # 基类那两页的内容搬到模式自己的页上」，不是另写一套逻辑。

    def _build_roll_datasource_tab(self) -> None:
        """滚榜模式第 1 页：题目集 ID / Cookie / 拉取数据 / 抓取结果。"""
        BoardApp._build_contest_tab(self)
        body = self._card(self.tab_datasource, "Cookie（必需）",
                          "取数方式固定为「直连接口」：滚榜和实时榜单用的是同一条链路、同一份 Cookie")
        self._build_cookie_card(body, MODE_ROLL)
        row = self._indent_row(body)
        self._hint(row, "只有这一份 cookie.txt：实时榜单模式那边的 Cookie 卡改的也是它，"
                        "两边看到的是同一个值，不存在第二份。没有有效 Cookie 就取不到数据"
                        "（2026-10-13 起删除了「借用浏览器」这条路，不再有任何回落）。"
                  ).pack(side="left", fill="x", expand=True)

    def _build_roll_display_tab(self) -> None:
        """滚榜模式第 2 页：**奖项表 + 全队标记** / 队伍照片 / 校徽。

        M15：原来那张「打星奖项（* 作为唯一奖项来源）」整卡已删除 ——
        奖项只有一张表（每行 = 一个奖项 + 自己的名单），打星（``*``）退化成
        「按 * 自动勾选」这个**便利按钮**，不再是模式。

        M13（4d）：原来排在最前面的「中文字体（必做）」整张卡片也已删除 ——
        字体改由后端负责：``font_path`` 为空或文件不存在时自动回落到内置字体
        （程序目录下 ``tools\\fonts\\MicrosoftYaHei.ttc``），界面不再需要让用户挑字体，
        也不再需要「字体未设置」的橙色警示条。
        """
        page = self.tab_play

        body2 = self._card(page, "奖项表（每行 = 一个奖项）",
                           "每一行 = 比赛包里的一个奖项：名字 + 名单。"
                           "名单在下面的「全队标记」里勾，随时能改")
        # 这一整块（奖项表 + 全队标记矩阵 + 「重新识别 / 刷新」）在基类里实现，
        # 合并外壳直接复用 —— 不再各维护一份，免得两边界面不一致。
        self._build_award_table_ui(body2, hint_wrap=300)

        # M13（4b）：「队伍名怎么显示」（绑定 cfg["display_name"] 的输入框）整行已删除 ——
        # 用户明确说这个设置用不上，队名一律按默认的 {team.name} 显示
        # （``board_config.sanitize`` 会把旧值忽略掉并存进 ``_display_name_ignored``）。

        # -- 队伍照片：复制成 <比赛包>\teams\<队伍ID>\photo.jpg，铺满奖项卡 ----
        body1 = self._card(page, "队伍照片目录",
                           "滚榜揭晓奖项时，照片铺满整张奖项卡当背景")
        row3 = self._form_row(body1, "队伍照片目录", pady=(6, 2))
        self.photos_var = tk.StringVar()
        photos_entry = self._entry(row3, self.photos_var, 34)
        photos_entry.pack(side="left")
        self._ghost_btn(row3, "选择…", self._pick_photos_dir).pack(side="left", padx=(8, 0))
        self._ghost_btn(row3, "默认目录",
                        self._reset_photos_dir).pack(side="left", padx=(6, 0))
        # M26：只把照片/校徽重新灌进已经生成好的比赛包（不重建 XML、不动奖项）
        self.refresh_images_btn = self._ghost_btn(
            row3, "刷新", self.on_refresh_package_images)
        self.refresh_images_btn.pack(side="left", padx=(6, 0))
        ToolTip(self.refresh_images_btn,
                "把当前「队伍照片目录」和「校徽目录」里的图重新灌进已经生成好的比赛包：\n"
                "  <比赛包>\\teams\\<队伍ID>\\photo.<扩展名>\n"
                "  <比赛包>\\organizations\\<机构ID>\\logo.png\n"
                "只换图，不重建 events.xml、不动奖项、不重新生成整个包，几秒就好。\n"
                "改完照片/校徽目录后点它，不用再整包重生成一遍。")
        ToolTip(photos_entry,
                "放队伍照片的文件夹（滚榜揭晓奖项时，照片铺满整张奖项卡当背景）。\n"
                "不填就用应用所在目录下的「滚榜图片」（不存在会自动创建）。\n"
                "文件名用学号最稳（26160358.jpg），也可以直接用姓名（汪章正.jpg）；\n"
                "支持 .jpg / .jpeg / .png。\n"
                "生成比赛包时会复制进 <比赛包>\\teams\\<队伍ID>\\photo.jpg（teams 这一层必须有）。\n"
                "建议 16:9 或更宽、1920x1080 以上 —— 照片会铺满整张奖项卡。\n"
                "留空、或者某支队伍没配到照片时，该队的奖项卡自动用默认背景（ICPC 默认底图），\n"
                "左下角的「校徽 + 队名 + 奖项名」栏照常显示。")
        row3b = self._indent_row(body1, pady=(0, 4))
        self.photos_hint = self._hint(
            row3b,
            f"现在用：{cfgmod.default_photos_dir()}（应用所在目录，不存在会自动创建）。"
            "文件名优先按学号匹配（如 26160358.jpg），找不到再按姓名（如 汪章正.jpg）；支持 jpg/jpeg/png。"
            "生成后会复制进 <比赛包>\\teams\\<队伍ID>\\photo.jpg。",
            size=9)
        self.photos_hint.pack(side="left", fill="x", expand=True)

        # -- 校徽：复制成 <比赛包>\organizations\<机构ID>\logo.png ----
        row3c = self._form_row(body1, "校徽目录", pady=(6, 2))
        self.logos_var = tk.StringVar()
        logos_entry = self._entry(row3c, self.logos_var, 34)
        logos_entry.pack(side="left")
        self._ghost_btn(row3c, "选择…", self._pick_logos_dir).pack(side="left", padx=(8, 0))
        self._ghost_btn(row3c, "默认目录",
                        self._reset_logos_dir).pack(side="left", padx=(6, 0))
        ToolTip(logos_entry,
                "放校徽的文件夹。\n"
                "不填就用应用所在目录下的「校徽」（不存在会自动创建）。\n"
                "规则：\n"
                "  · 文件名没有名字（.jpg 这种），或叫 logo / 校徽 / default / 机构\n"
                "    → 默认校徽，所有队都用它；\n"
                "  · 文件名 = 学号（26130030.png）或姓名（汪章正.png）→ 那支队的专属校徽；\n"
                "  · 名字对不上任何队伍 → 忽略，生成日志里会列出来。\n"
                "生成时会把校徽裁成圆形（居中裁成正方形 + 圆外透明）再放进\n"
                "<比赛包>\\organizations\\<机构ID>\\logo.png；你的原文件不会被改动。")
        row3d = self._indent_row(body1, pady=(0, 4))
        self.logos_hint = self._hint(
            row3d,
            f"现在用：{cfgmod.default_logos_dir()}（应用所在目录，不存在会自动创建）。"
            "空名字（.jpg）或 logo/校徽/default/机构 = 默认校徽；学号或姓名（汪章正.png）= 该队专属；"
            "对不上的会被忽略并在生成日志里列出。校徽会裁成圆形放进 "
            "<比赛包>\\organizations\\<机构ID>\\logo.png（你的原文件不动），"
            "显示在奖项卡左下角。",
            size=9)
        self.logos_hint.pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------
    # 「刷新」：只把照片 / 校徽重新灌进已经生成好的比赛包
    # ------------------------------------------------------------

    def on_refresh_package_images(self) -> None:
        """把当前「队伍照片目录」「校徽目录」里的图重新灌进已有的比赛包。

        只换图（``teams\\<队伍ID>\\photo.<ext>`` 与 ``organizations\\<机构ID>\\logo.png``），
        不重建 events.xml、不动奖项、不重新生成整个包，所以几秒就能完成。
        照片可能有几 MB，复制放后台线程，界面用 ``_post`` 回来更新。
        """
        if self.busy:
            return
        self._collect_config()
        package_dir = Path(self.pkg_var.get().strip() or self._default_package_dir())
        meta = package_dir / backend.META_NAME
        teams_dir = package_dir / backend.TEAM_FOLDER
        if not meta.is_file() or not teams_dir.is_dir():
            messagebox.showinfo(
                "还没有比赛包",
                f"{package_dir} 里还没有生成好的比赛包。\n\n"
                "先到「3 滚榜」页点一次「生成比赛数据包」，生成好之后再来这里刷新。")
            return
        contest = Path(self.cfg.get("last_contest_json")
                       or (cfgmod.work_root() / "contest.json"))
        if not contest.is_file():
            messagebox.showwarning(
                "还没有数据",
                f"找不到比赛数据：{contest}\n\n请先在「1 数据源」页点「拉取数据」。")
            return
        photos = self._prepare_image_dir(self.photos_var.get(), cfgmod.default_photos_dir(),
                                         "队伍照片")
        logos = self._prepare_image_dir(self.logos_var.get(), cfgmod.default_logos_dir(),
                                        "校徽")
        if photos is None or logos is None:
            return
        self._persist()
        self._busy_on("正在刷新比赛包里的照片 / 校徽 …")
        self._log(f"\n[刷新] 比赛包 = {package_dir}\n")

        def job() -> None:
            result: dict = {"photos": None, "logos": None, "teams": 0, "error": ""}
            try:
                data = json.loads(contest.read_text(encoding="utf-8"))
                teams = data.get("teams") or []
                result["teams"] = len(teams)
                result["photos"] = backend.attach_team_photos(
                    package_dir, teams, Path(photos))
                plan = backend.build_logo_plan(teams, Path(logos))
                result["logos"] = backend.copy_org_logos(package_dir, plan)
            except Exception as exc:  # noqa: BLE001 - 失败要回到界面说清楚，不能静默
                result["error"] = f"{type(exc).__name__}: {exc}"
            self._post(lambda: self._refresh_images_done(result))

        threading.Thread(target=job, daemon=True).start()

    def _refresh_images_done(self, result: dict) -> None:
        """后台刷新结束：状态栏 + 日志写清照片/校徽数量与缺照片的队伍。"""
        self._busy_off()
        if result.get("error"):
            text = f"刷新失败：{result['error']}"
            self._set_status(text)
            self._log(f"[刷新] 失败：{result['error']}\n")
            messagebox.showwarning("刷新失败", text)
            return
        photos = result.get("photos") or {}
        logos = result.get("logos") or {}
        copied = int(photos.get("copied") or 0)
        matched = int(photos.get("matched_count") or 0)
        total = int(photos.get("total") or result.get("teams") or 0)
        missing = list(photos.get("missing") or [])
        names = "、".join(str(m.get("name") or m.get("team_id") or "?") for m in missing)
        logo_copied = int(logos.get("copied") or 0)
        removed = list(photos.get("removed") or [])

        bits = [f"照片 {copied} 张", f"校徽 {logo_copied} 个"]
        if removed:
            bits.append(f"清掉旧图 {len(removed)} 张")
        if photos.get("error"):
            bits[0] += f"（{photos['error']}）"
        elif matched == 0 and total:
            bits[0] += "（一张都没匹配上）"
        if logos.get("error"):
            bits[1] += f"（{logos['error']}）"
        elif logo_copied == 0:
            bits[1] += "（没有可用的校徽）"
        status = "已刷新比赛包图片：" + "、".join(bits)
        if missing:
            status += f"；缺照片 {len(missing)} 支：{names}"
        self._set_status(status)

        self._log(f"[刷新] 已刷新：照片 {copied} 张（匹配 {matched} / {total} 支），"
                  f"校徽 {logo_copied} 个（只换图，events.xml 和奖项都没动）\n")
        if removed:
            self._log("[刷新] 清掉旧图 %d 张：%s（源图已删或改名，这些队改用默认背景）\n"
                      % (len(removed), "、".join(str(x.get("name") or x.get("team_id") or "?") for x in removed)))
        if photos.get("error"):
            self._log(f"[刷新] 照片没处理：{photos['error']}\n")
        if missing:
            self._log(f"[刷新] 缺照片 {len(missing)} 支：{names}"
                      "（这些队伍的奖项卡用默认背景）\n")
        elif total:
            self._log("[刷新] 队伍照片全部就位。\n")
        if logos.get("error"):
            self._log(f"[刷新] 校徽没处理：{logos['error']}\n")
        notes = list(logos.get("notes") or [])
        if notes:
            self._log(f"[刷新] 校徽有 {len(notes)} 个没处理成："
                      + "；".join(notes[:8]) + "\n")
        self._log("[刷新] 完成，可以接着点「开始滚榜」。\n")

    def _build_roll_roll_tab(self) -> None:
        """滚榜模式第 3 页：比赛包位置 / 播放方式 / 生成并开始滚榜。"""
        page = self.tab_roll

        body1 = self._card(page, "比赛包（生成 / 放在哪）",
                           "把抓到的比赛数据转成滚榜程序（Resolver）认的数据包")
        row = self._form_row(body1, "比赛包放到哪", pady=(4, 4))
        self.pkg_var = tk.StringVar()
        self._entry(row, self.pkg_var, 44).pack(side="left")
        self._ghost_btn(row, "选择…", self._pick_package_dir).pack(side="left", padx=(8, 0))
        row2 = self._indent_row(body1, pady=(2, 2))
        self.package_btn = self._ghost_btn(row2, "生成比赛数据包", self.on_package)
        self.package_btn.pack(side="left")
        ToolTip(self.package_btn,
                "把 contest.json 转成 Resolver 用的比赛包（events.xml + 队伍照片 + 校徽）。\n"
                "改过奖项表、封榜时长、队伍照片之后要重新生成一次，\n"
                "否则「开始滚榜」会提示比赛包和当前设置不一致。")
        # M13（4e）：提示原来跟按钮挤在同一行（side="left" + padx=12），窄窗口下被截断。
        # 现在单独占一行、吃掉整行剩余宽度，换行宽度跟着可用宽度走（_auto_wrap + _run_fit），
        # 提示再长（比赛包路径 + 队数 / 照片数）也能整句看到。
        info_row = self._indent_row(body1, pady=(0, 4))
        self.package_info = self._t(info_row, "还没有生成比赛数据包。", size=9,
                                    fg=self.C["hint"], justify="left", anchor="w")
        self.package_info.pack(side="left", fill="x", expand=True)
        self._auto_wrap(self.package_info, min_width=200)

        # M13：这批数据有没有学号（照片/校徽按学号匹配的前提）。没有就红字提示 ——
        # 抓数侧已修（pta_fetch.fetch_teams 会写 studentNumber），但老数据/旧缓存
        # 仍然没有，必须让用户看见，而不是静默把按学号命名的照片当成"没交照片"。
        self.number_warn = self._t(body1, "", size=9, fg=self.KIND["warn"],
                                   anchor="w", justify="left", wraplength=520)
        self._auto_wrap(self.number_warn, source=body1, min_width=260)

        row3 = self._form_row(body1, "封榜时长（分钟）", pady=(4, 4))
        self.freeze_var = tk.StringVar(value="60")
        freeze_entry = self._entry(row3, self.freeze_var, 8)
        freeze_entry.pack(side="left")
        ToolTip(freeze_entry, "比赛最后这么久不公开榜单（封榜），滚榜从封榜这一刻开始揭晓。")
        self._hint(row3, "比赛最后这么久不公开榜单（封榜），滚榜从这一刻开始揭晓",
                   size=9).pack(side="left", fill="x", expand=True, padx=10)

        row4 = self._form_row(body1, "罚时（分钟）", pady=(4, 4))
        self.penalty_var = tk.StringVar(value="20")
        self._entry(row4, self.penalty_var, 8).pack(side="left")
        self._hint(row4, "每道最终通过的题，之前每错一次加这么多分钟（ICPC 默认 20 分钟）",
                   size=9).pack(side="left", fill="x", expand=True, padx=10)

        body2 = self._card(page, "滚榜怎么揭晓（播放方式）",
                           "比赛结束后放滚榜时的节奏：三种方式对应三种现场场合")
        row = self._form_row(body2, "播放方式", pady=(4, 4))
        self.play_var = tk.StringVar()
        play_combo = ttk.Combobox(row, textvariable=self.play_var, state="readonly",
                                  width=34, values=list(base.PLAY_LABELS.keys()))
        play_combo.pack(side="left")
        play_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_play_mode())
        ToolTip(play_combo,
                "三种方式怎么选（现场用哪句顺口就用哪句）：\n"
                "  · 全程点击播放 —— 你要一边讲一边揭晓，每一步都由你点；\n"
                "  · 先自动、后点击 —— 前面名次自己滚，到你要讲的名次停下来等你点；\n"
                "  · 全程自动播放 —— 没人守着，放完为止（也可用「揭晓速度」控制快慢）。\n"
                "对应 Resolver 的参数：前两种用 --singleStep，最后一种不加。")
        play_hint = self._indent_row(body2, pady=(0, 4))
        self._t(play_hint,
                "现场通常选「先自动、后点击」：前面自动滚过去，到关键名次停下等你讲解。",
                size=9, fg=self.C["hint"]).pack(side="left")

        row_p = self._form_row(body2, "从第几名开始点击", pady=(2, 4))
        self.click_from_var = tk.StringVar(value="4")
        self.click_from_entry = self._entry(row_p, self.click_from_var, 6)
        self.click_from_entry.pack(side="left")
        self._hint(row_p, "名（只在「先自动、后点击」下有用：前面的名次自动滚，到这个名次停住等你点）",
                   size=9).pack(side="left", fill="x", expand=True)
        ToolTip(self.click_from_entry,
                "填 4 = 前 3 名自动揭晓，从第 4 名开始等你点。\n"
                "（Resolver 的 --singleStep N 实际从第 N-1 名开始等点击，所以这里填 4 会传 --singleStep 5。）")

        row2s = self._form_row(body2, "揭晓速度", pady=(4, 4))
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_scale = ttk.Scale(row2s, from_=0.25, to=4.0, length=240,
                                     variable=self.speed_var, command=self._on_speed)
        self.speed_scale.pack(side="left")
        self.speed_label = self._t(row2s, "1.00x", fg=self.C["ink"], width=7)
        self.speed_label.pack(side="left", padx=(6, 0))
        self._hint(row2s, "2x 快一倍、0.5x 慢一倍；播放中还能按 +/- 调速、2 快进、p 暂停",
                   size=9).pack(side="left", fill="x", expand=True, padx=4)

        row2b = self._form_row(body2, "额外参数", pady=(4, 4))
        self.extra_var = tk.StringVar()
        extra_entry = self._entry(row2b, self.extra_var, 40)
        extra_entry.pack(side="left")
        ToolTip(extra_entry, "给 Resolver 的额外命令行参数，一般留空。\n"
                             "例如 --display 1 投到第二块屏、--light 浅色主题、--info 显示更多信息。")
        self._hint(row2b, "高级项，一般留空：如 --display 1 投第二块屏、--light 浅色、--info 更多信息",
                   size=9).pack(side="left", fill="x", expand=True, padx=10)

        body3 = self._card(page, "开始滚榜",
                           "点一下自动做完：用内置中文字体 → 加播放参数 → 拉起 Esc 退出守护",
                           expand=True)
        hero = self._f(body3)
        hero.pack(expand=True)

        self.start_btn = self._solid_btn(
            hero, "▶  开始滚榜", self.on_start_roll,
            bg=self.C["go"], hover=self.C["go_hover"],
            font=(UI_FONT, 17, "bold"), padx=46, pady=15)
        self.start_btn.pack(pady=(14, 10))
        ToolTip(self.start_btn,
                "点一下自动做完三件事：\n"
                "  ① 中文字体用程序自带的（不用设置）\n"
                "  ② 按上面选的播放方式加参数\n"
                "  ③ 拉起 Esc 退出守护\n"
                "比赛包不存在或和当前设置不一致时，它会先提示/自动重新生成。")

        three_things = self._t(hero, "点一下自动做完三件事：用内置中文字体（不用设置） · "
                                     "按播放方式加参数 · 拉起 Esc 退出守护",
                               size=9, fg=self.C["hint"], justify="center")
        three_things.pack()
        self._auto_wrap(three_things)

        self.roll_info = self._auto_wrap(
            self._t(hero, "", fg=self.C["sub"], justify="center"))
        self.roll_info.pack(pady=10)

        self.mode_line = self._auto_wrap(
            self._t(hero, "", size=11, bold=True, fg=self.C["ink"], justify="center"))
        self.mode_line.pack(pady=(0, 4))
        self.hint_line = self._auto_wrap(
            self._t(hero, "", size=9, fg=self.C["hint"], justify="center"))
        self.hint_line.pack(pady=(0, 6))

        self.stop_btn = self._ghost_btn(hero, "■  停止滚榜", self.on_stop_roll)
        self.stop_btn.pack(pady=(2, 8))
        ToolTip(self.stop_btn, "先礼后兵：先请 Resolver 自己关（WM_CLOSE），\n超时才 taskkill /F 强杀。")
        half_hint = self._t(hero, "按不动、窗口关不掉时：先点「停止滚榜」，"
                                  "还不行就到「日志」区看最后几行、或按 Ctrl+Q。",
                            size=9, fg=self.C["hint"], justify="center")
        half_hint.pack(pady=(0, 4))
        self._auto_wrap(half_hint)

        shortcuts = self._t(hero, "Resolver 快捷键：p 暂停/继续 · 2 快进一步 · 0 从头开始 · "
                                  "+/- 调速 · Esc 退出 · Ctrl-Q 退出",
                            size=9, fg=self.C["hint"], justify="center")
        shortcuts.pack(pady=(6, 4))
        self._auto_wrap(shortcuts)

    # ================================================================
    # 实时榜单：服务页
    # ================================================================

    def _build_service_tab(self) -> None:
        page = self.tab_service

        body = self._card(page, "实时榜单服务（启动 / 停止）",
                          "两个后台小程序：一个取数、一个出榜单网页")
        row = self._form_row(body, "状态")
        self.svc_state = self._pill(row, "未启动", "hint")
        self.svc_state.pack(side="left")
        self._svc_start_btn = self._ghost_btn(row, "启动", self.on_live_start)
        self._svc_start_btn.pack(side="left", padx=8)
        self._svc_stop_btn = self._ghost_btn(row, "停止", self.on_live_stop)
        self._svc_stop_btn.pack(side="left")
        self._ghost_btn(row, "打开榜单页", self.on_open_board).pack(side="left", padx=8)
        self._ghost_btn(row, "打开日志文件夹",
                        lambda: self._open_folder(self.service.run / "logs")).pack(side="left")
        ToolTip(self._svc_start_btn, "启动取数 + 榜单网页两个后台程序。\n"
                                     "启动后点「打开榜单页」就能看到榜；给 OBS 用的网址在「5 直播」页。")
        ToolTip(self._svc_stop_btn, "停止这两个后台程序（比赛结束、换比赛时点一下）。\n"
                                    "如果之前是别的程序占着端口，用下面的「终止旧服务」。")

        row = self._indent_row(body)
        self.svc_hint = self._hint(row, "")
        self.svc_hint.pack(side="left", fill="x", expand=True)

        body2 = self._card(page, "运行状态", "每 3 秒自动刷新；数字来自服务自己的健康检查与取数日志")
        self.svc_vars = {}
        for key, label in (("pids", "后台进程编号"), ("port", "榜单页端口"),
                           ("channel", "取数通道（恒为直连接口 http）"), ("last_poll", "最近一次取数时间"),
                           ("teams", "队伍数"), ("submissions", "提交数"),
                           ("latency", "最近一次取数耗时"), ("requests", "累计请求数"),
                           ("run_dir", "数据放在哪")):
            row = self._form_row(body2, label)
            var = tk.StringVar(value="—")
            widget = tk.Label(row, textvariable=var, font=(UI_FONT, 10),
                              fg=self.C["ink"], bg=self.C["card"], anchor="w")
            widget.pack(side="left", fill="x", expand=True)
            self.svc_vars[key] = var

        body3 = self._card(page, "端口被占用怎么办",
                           "本程序绝不偷偷杀别人的进程：先看清是谁在用，再决定")
        row = self._form_row(body3, "占用检查")
        self.port_detail = self._hint(row, "")
        self.port_detail.pack(side="left", fill="x", expand=True)
        row = self._indent_row(body3)
        self._ghost_btn(row, "重新检查", self._check_port_detail).pack(side="left")
        self._kill_old_btn = self._ghost_btn(row, "终止旧服务", self.on_kill_old)
        self._kill_old_btn.pack(side="left", padx=8)
        self._ghost_btn(row, "换一个空闲端口", self.on_pick_free_port).pack(side="left")
        ToolTip(self._kill_old_btn,
                "只杀「上次没关干净的本程序服务」（名称/启动方式对得上的那种）。\n"
                "确认不了来源的进程，它会让你自己决定，不会动手。")

    # 实时榜单：直播（榜单参数 / URL）
    # ================================================================

    def _build_live_tab(self) -> None:
        page = self.tab_live

        body = self._card(page, "榜单怎么显示（排行榜参数）",
                          "这里每一项都会拼进下面的网址；榜单数据本身不受影响")
        # 注意：不能重置 —— 「2 外观」页已经把 view.font 登记进来了
        self.view_vars = getattr(self, "view_vars", {}) or {}
        view = self.live_cfg.get("view") or {}

        def _pair(parent, key, label, width, note, tip):
            """一个参数 = 名字 + 输入框，紧跟一句直白说明（说明在输入框下面一行）。

            说明写清「0 是什么、填别的值是什么」，用户不用去猜网址参数。
            """
            col = self._f(parent)
            col.pack(side="left", fill="x", expand=True, padx=(0, 18))
            line = self._f(col)
            line.pack(fill="x", anchor="w")
            self._t(line, label, size=9, fg=self.C["sub"]).pack(side="left", padx=(0, 3))
            var = tk.StringVar(value=str(view.get(key, LIVE_DEFAULTS["view"][key])))
            self.view_vars[key] = var
            entry = self._entry(line, var, width)
            entry.pack(side="left")
            var.trace_add("write", lambda *_a: self._refresh_live_page())
            ToolTip(entry, tip + f"\n对应网址参数 ?{key}=…；改完下面「榜单页 URL」立刻更新。")
            hint = self._hint(col, note)
            hint.pack_configure(pady=(2, 0))
            return entry

        row = self._form_row(body, "画面大小")
        _pair(row, "overlay", "透明底色（OBS 用）", 4,
              "0 = 正常白底页面；1 = 去掉白底，叠到 OBS / 直播画面上（网址带 ?overlay=1）。",
              "透明底色（OBS 用）：0 = 正常白底页面；1 = 去掉白底，叠到 OBS/直播画面上。")
        _pair(row, "top", "只显示前 N 名", 4,
              "0 = 全部队伍都显示；填 12 = 只显示前 12 名，第 13 名往后不出现。",
              "只显示前 N 名：0 = 全部显示；12 = 只显示前 12 名（后面的名次不显示）。")
        row = self._indent_row(body, pady=(6, 4))
        _pair(row, "rows", "一屏显示几行", 4,
              "0 = 整榜一次性排完、不滚动（人少时最好看）；12 = 一屏只放 12 行，多的靠滚动看。",
              "一屏显示几行：0 = 整榜一次性排完、不滚动；12 = 一屏 12 行，多的靠滚动。")
        _pair(row, "scale", "整体缩放", 4,
              "整页字号/间距一起缩放，1 = 原始大小；投大屏看不清时调到 1.2~1.5。",
              "整体缩放：1 = 原始大小；投大屏看不清时调到 1.2~1.5。")

        row = self._form_row(body, "滚动与动效")
        _pair(row, "scroll", "来回滚动", 4,
              "1 = 开启滚动榜（队伍多、一屏放不下时来回滚动）；0 = 静止不动。",
              "来回滚动：1 = 开启滚动榜；0 = 静止不动。")
        _pair(row, "scroll_speed", "滚动速度", 4,
              "数字越大滚得越快（像素/秒），默认 24；太晃眼就调小。",
              "滚动速度：数字越大越快（像素/秒），默认 24；太晃眼就调小。")
        row = self._indent_row(body, pady=(6, 4))
        _pair(row, "scroll_pause", "每屏停几秒", 4,
              "滚到一屏边界后停住的时间（秒），默认 2；方便观众看清。",
              "每屏停几秒：滚到一屏边界后停住的时间（秒），默认 2。")
        _pair(row, "anim", "排名变化动效", 4,
              "1 = 名次变化时有移动动画；0 = 直接跳到新位置（机器慢时关掉更流畅）。",
              "排名变化动效：1 = 有移动动画；0 = 直接跳到新位置。")

        row = self._form_row(body, "榜单页标题")
        var = tk.StringVar(value=str(view.get("title", "")))
        self.view_vars["title"] = var
        entry = self._entry(row, var, 30)
        entry.pack(side="left")
        var.trace_add("write", lambda *_a: self._refresh_live_page())
        ToolTip(entry, "留空 = 用 PTA 比赛名；填了就用你写的（会带进网址 ?title=…）。")
        title_hint = self._hint(row, "留空 = 用 PTA 比赛名；填了就用你写的（会带进网址 ?title=…）")
        title_hint.pack_configure(padx=(8, 0))
        row = self._indent_row(body)
        self._hint(row, "字体在「2 外观」页设；这里只管标题文字与榜单参数。"
                  ).pack(side="left", fill="x", expand=True)

        row = self._form_row(body, "封榜后怎么显示")
        self._hint(row, "固定观众视角：封榜后永不解榜，榜单停在封榜那一刻，"
                        "封榜后的提交只在对应格子上标黄，不揭晓结果。没有其它视角可切换。"
                  ).pack(side="left", fill="x", expand=True)

        body2 = self._card(page, "榜单页网址（复制给 OBS 或浏览器）",
                           "先复制这条网址，再照下面的步骤贴到 OBS / 浏览器")
        row = self._form_row(body2, "榜单页 URL")
        self.url_var = tk.StringVar(value="")
        entry = self._entry(row, self.url_var, 58)
        entry.pack(side="left", fill="x", expand=True)
        entry.configure(state="readonly")
        row = self._indent_row(body2)
        self._ghost_btn(row, "复制 URL", self.on_copy_url).pack(side="left")
        self._ghost_btn(row, "在浏览器打开", self.on_open_board).pack(side="left", padx=6)
        row = self._indent_row(body2, pady=(4, 0))
        self._preset_btn = self._ghost_btn(
            row, "预设：全榜静态（不滚动，适合队伍少）",
            lambda: self.on_preset("static"))
        self._preset_btn.pack(side="left")
        self._preset_pingpong_btn = self._ghost_btn(
            row, "预设：前 12 名来回滚动（队伍多时用）",
            lambda: self.on_preset("pingpong"))
        self._preset_pingpong_btn.pack(side="left", padx=6)
        ToolTip(self._preset_btn,
                "队伍少（一屏能放完）就用这个：一屏显示全部、不滚动、不要动效。\n"
                "按下去上面「一屏显示几行 / 来回滚动 / 排名变化动效」和下面的网址会一起改。")
        ToolTip(self._preset_pingpong_btn,
                "队伍多（一屏放不下）就用这个：一屏 12 行、自动来回滚动、开动效。\n"
                "按下去上面「一屏显示几行 / 来回滚动 / 排名变化动效」和下面的网址会一起改。")
        row = self._indent_row(body2, pady=(6, 0))
        self._hint(row, "贴到 OBS 的步骤：① 点「复制 URL」　② OBS 里加一个「浏览器」来源"
                        "　③ 宽 1920 高 1080，把网址粘进 URL 栏　④ 想要透明叠画面，"
                        "把上面「透明底色（OBS 用）」设成 1。"
                  ).pack(side="left", fill="x", expand=True)
        self.preset_note_row = self._indent_row(body2, pady=(4, 0))
        self.preset_note = self._hint(
            self.preset_note_row,
            "点上面的预设，上面几个参数和这条网址会一起改；已打开的榜单页要按 F5 才会应用新参数。")
        self.preset_note.pack(side="left", fill="x", expand=True)
        self.url_hint_row = self._indent_row(body2)
        self.url_hint = self._hint(self.url_hint_row, "")
        self.url_hint.pack(side="left", fill="x", expand=True)

        # 2026-10-13：外部观战页整块界面已删除（用户：用不上）。
        # 后端 /api/contests/... 端点保留（零维护、不影响界面），只是界面上不再出现。
        self._refresh_live_page()

    def _view_int(self, key: str, fallback: int = 0) -> int:
        var = self.view_vars.get(key)
        try:
            return int(float(str(var.get()).strip() or fallback))
        except (AttributeError, TypeError, ValueError):
            return fallback

    def _view_str(self, key: str, fallback: str = "") -> str:
        var = self.view_vars.get(key)
        try:
            return str(var.get()).strip()
        except AttributeError:
            return fallback

    def _collect_live_config(self) -> None:
        """把界面上的值收进 self.live_cfg（并同步 view / 端口 / 间隔 / 平台）。"""
        live_cfg = self.live_cfg
        # 取数通道恒为 http（直连接口）：界面上已经没有可选的取数通道了。
        live_cfg["channel"] = "http"
        # VJudge 版：平台 + 比赛 ID 必须写进运行目录 config.json —— poller 每轮重读，
        # 这样切平台 / 改 ID 不用重启服务（服务启动时也会整份写一遍）。
        platform = self._platform() if hasattr(self, "platform_var") else "pta"
        live_cfg["platform"] = platform
        try:
            psid_now = str(self.cfg.get("problem_set_id") or self.psid_var.get() or "").strip()
        except Exception:  # noqa: BLE001
            psid_now = str(self.cfg.get("problem_set_id") or "").strip()
        live_cfg["contest_id"] = psid_now
        for key in ("overlay", "top", "rows", "scroll", "scroll_speed",
                    "scroll_pause", "anim"):
            live_cfg["view"][key] = self._view_int(key, LIVE_DEFAULTS["view"][key])
        live_cfg["view"]["scale"] = self._view_int("scale", 1) or 1
        live_cfg["view"]["font"] = self._view_str("font", "yahei")
        live_cfg["view"]["title"] = self._view_str("title", "")
        # 封榜只有观众视角（用户 2026-10-17：封榜是只给观众视角的，不用给别的视角）：
        # 封榜后永不解榜、榜单停在封榜那一刻，封榜后的提交只标黄格。
        # 界面上已删掉视角下拉框，这里恒定为 audience —— 榜单页 URL 带 ?freeze=audience。
        live_cfg["freeze_mode"] = "audience"
        live_cfg["view"]["freeze"] = "audience"
        try:
            raw_freeze = str(self.freeze_live_var.get()).strip()
        except Exception:  # noqa: BLE001
            raw_freeze = ""
        if raw_freeze == "":
            live_cfg["freeze_minutes"] = None          # 空 = 用比赛自带的封榜时长
        else:
            try:
                live_cfg["freeze_minutes"] = max(0, int(float(raw_freeze)))
            except ValueError:
                live_cfg["freeze_minutes"] = None
        try:
            live_cfg["port"] = int(str(self.port_var.get()).strip())
        except (AttributeError, ValueError):
            live_cfg["port"] = int(LIVE_DEFAULTS["port"])
        try:
            live_cfg["poll_interval"] = max(5, int(float(str(self.interval_var.get()).strip())))
        except (AttributeError, ValueError):
            live_cfg["poll_interval"] = int(LIVE_DEFAULTS["poll_interval"])
        try:
            live_cfg["full_check_interval"] = max(30, int(float(str(self.full_var.get()).strip())))
        except (AttributeError, ValueError):
            live_cfg["full_check_interval"] = int(LIVE_DEFAULTS["full_check_interval"])
        live_cfg["view"]["problem_colors"] = list(
            live_cfg.get("problem_colors") or DEFAULT_PROBLEM_COLORS)
        live_cfg["view"]["medal_colors"] = list(
            live_cfg.get("medal_colors") or DEFAULT_MEDAL_COLORS)
        # ---- M15/M17 单一模型 + **两边用法不同**（M17 口径纠正）------------
        # 结构上写在 ``live.view.awards``：每项 {name, show, count, team_ids?}。
        # **同一行同时有 count 与 team_ids 是正常的**：
        #   * 滚榜（最终颁奖）读 ``team_ids``（勾选名单）→ 每个奖一个 <award>；
        #   * 实时榜（过程中，名次随时在动）读 ``count``（名次区间）→ 第 1 个奖金、
        #     第 2 个银、第 3 个铜，名次一变颜色跟着变（前端 board.js 的 medalFor 只读 count）。
        # 所以这里**永远写 count**（不因为勾了名单就归零），并用 board_config 的统一口径。
        try:
            self.cfg["awards"] = self.collect_awards()
        except Exception:  # noqa: BLE001 - 界面正在销毁时不要炸
            pass
        live_cfg["view"]["awards"] = cfgmod.live_view_awards(
            self.cfg.get("awards") or []) or cfgmod.normalize_live_view_awards(
            live_cfg.get("awards_cache") or [])
        live_cfg["view"]["awards_mode"] = "rows"

    def _live_query(self) -> dict:
        """榜单页查询参数（按榜单页的取值域收敛：0/1 开关 + 非负整数）。

        标题只在用户填了非空值时才带（用 urlencode 正确编码中文/空格），
        空则不带 —— 榜单页会回落到 PTA 比赛名，保持旧行为。
        """
        def flag(key):
            return 1 if self._view_int(key, LIVE_DEFAULTS["view"][key]) else 0

        def count(key):
            return max(0, self._view_int(key, int(LIVE_DEFAULTS["view"][key])))

        query = {
            "overlay": flag("overlay"),
            "top": count("top"),
            "rows": count("rows"),
            "scroll": flag("scroll"),
            "scroll_speed": max(1, count("scroll_speed")),
            "scroll_pause": count("scroll_pause"),
            "anim": flag("anim"),
            "freeze": "audience",
        }
        title = self._view_str("title", "")
        if title:
            query["title"] = title
        return query

    def _refresh_live_page(self) -> None:
        if not self._live_ready or not hasattr(self, "url_var"):
            return
        try:
            port = int(str(self.port_var.get()).strip())
        except (AttributeError, ValueError):
            port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
        url = live.board_url(port, self._live_query())
        self.url_var.set(url)
        self.url_hint.configure(
            text=(f"这条网址只影响「榜单页怎么显示」，不改任何榜单数据（/api/board 的输出完全一样）。"
                  f"复制后：浏览器直接打开就能看；给 OBS 就加「浏览器」来源。"
                  f"（端口 {port}：实时服务没启动时打开会打不开，先去「服务」页点「启动」。）"))
        self._fit_all_hints()

    def on_preset(self, key: str) -> None:
        """套用「一屏几行 / 来回滚动 / 动效」组合。

        用户反馈「两个预设按钮按了没反应」：参数和网址其实都改了，但只有 URL 尾部
        几个字符变化、状态栏又在窗口最下面，看起来就像没动。所以这里**当场给出
        看得见的反馈**：字段值变、网址变、按钮下面一行说明变、状态栏一句话、日志一行；
        另外说清「已打开的榜单页要按 F5 才会应用新参数」。
        """
        preset = VIEW_PRESETS[key]
        label = PRESET_LABELS.get(key, key)
        for name, value in preset.items():
            var = self.view_vars.get(name)
            if var is not None:
                var.set(str(value))
        self._collect_live_config()
        self._persist()
        self._refresh_live_page()
        rows = self._view_int("rows", 0)
        scroll = self._view_int("scroll", 0)
        anim = self._view_int("anim", 0)
        try:
            running = bool(self.service.any_running())
        except Exception:  # noqa: BLE001 - 服务对象异常时按钮也要有反馈
            running = False
        note = (f"已套用预设：{label}。参数已改成 rows={rows}、scroll={scroll}、anim={anim}"
                "（0 是默认值，网址里就不写这一项），下面的网址已跟着更新。")
        if running:
            note += "服务正在跑：已打开的榜单页要按 F5 或重新点「在浏览器打开」才会应用新参数。"
        else:
            note += "实时服务还没启动：启动后复制这条网址即可。"
        try:
            self.preset_note.configure(text=note)
        except Exception:  # noqa: BLE001
            pass
        self._fit_all_hints()
        status = "已套用预设：" + label
        if running:
            status += "（榜单页按 F5 生效）"
        self._set_status(status)
        self._log(f"[榜单参数] 已套用预设：{label}"
                  f"（rows={rows} scroll={scroll} anim={anim}）→ {self.url_var.get()}\n")

    def on_copy_url(self) -> None:
        url = self.url_var.get()
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._set_status("榜单页 URL 已复制到剪贴板")
        self._log(f"[直播] 已复制 URL：{url}\n")

    # 2026-10-13：外部观战页界面与运行时检测 / 启动逻辑已整体删除。

    # ================================================================
    # 实时服务：启停
    # ================================================================

    def on_live_primary(self) -> None:
        if self.service.any_running():
            self.on_open_board()
        else:
            self.on_live_start()

    def on_live_start(self) -> None:
        if self._live_busy:
            return
        okpy, note = live.is_real_python()
        if not okpy:
            self._log(f"\n[服务] 起不了实时服务：{note}\n")
            self._msg_hint_live(f"⚠ {note}", "err")
            self._set_status("实时服务需要 Python 解释器（详见环境预检）")
            if not self._shot_only:
                messagebox.showwarning("需要 Python 解释器", note)
            return
        debug_mark(f"on_live_start 进入 python={note}")
        self._collect_live_config()
        self._collect_config()
        self._persist()
        port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
        debug_mark(f"端口={port} 被占用={live.port_in_use(port)}")
        if live.port_in_use(port) and not self.service.any_running():
            pids = live.listening_pids(port)
            self._log(f"\n[服务] 端口 {port} 已被 PID {pids} 占用 —— 没有启动，也**没有**去杀它。\n")
            self._check_port_detail()
            self._set_status(f"端口 {port} 被占用：可点「终止旧服务」或改端口")
            messagebox.showwarning(
                "端口被占用",
                f"端口 {port} 已被 PID {pids} 占用。\n\n"
                "如果这是上次没退干净的实时服务，可以点「终止旧服务」；\n"
                "如果是别人在用的服务（例如 8765 上的回放），请改成别的端口。")
            return

        def job() -> None:
            debug_mark("后台线程：service.start 开始")
            # 把界面上的实时设置一起交给 service：它会写进运行目录的
            # config.json（server.py 启动就硬读这个文件，打包首次运行缺了会崩）
            result = self.service.start(port, str(self.cfg.get("problem_set_id") or ""),
                                        interval=float(self.live_cfg.get("poll_interval", 5)),
                                        full_interval=float(
                                            self.live_cfg.get("full_check_interval", 120)),
                                        live_config=dict(self.live_cfg))
            debug_mark(f"后台线程：service.start 返回 {result.get('pids')} "
                       f"config={result.get('live_config')}")
            health = live.wait_health(port, timeout=25)
            debug_mark(f"后台线程：health={health}")
            self._post(lambda: self._live_started(result, health, port))
            debug_mark("后台线程：已投递 UI 回调")

        self._live_busy = "start"
        self._run_job(job, "正在启动实时服务 …")

    def _live_started(self, result: dict, health: dict | None, port: int) -> None:
        self._live_busy = ""
        self._busy_off()
        self._live_log_pos = 0
        for kind in ("poller", "server"):
            child = getattr(self.service, kind)
            if child:
                self._log(f"[服务] {kind} 已启动：PID {child.pid}\n")
        self._log(f"[服务] 运行目录：{result.get('run_dir')}\n")
        cookie_error = str(result.get("cookie_error") or "")
        if cookie_error:
            # 没有可用凭证：**不启动轮询**，界面上红字显著提示、日志说清原因。
            self._log("[服务] ❌ Cookie 失效，已停止轮询：" + cookie_error + "\n")
            self._log("[服务] 没有取数进程；服务本身在跑，/health 会带 cookie_error。\n")
            self._refresh_cookie_pill()
            self._msg_hint_live(
                "⚠ Cookie 失效，已停止取数：不会去借浏览器、也不会每 20 秒撞一次 401。"
                "请到本页「Cookie」那一行点「粘贴 Cookie」更新 Cookie，"
                "然后点「▶ 检查 Cookie」或「重新拉取数据」恢复。", "err")
            self._set_precheck_banner(
                "❌ Cookie 失效：实时榜单已停止取数（不会去借浏览器，也不会反复撞 401）。"
                "请到本页「Cookie」那一行点「粘贴 Cookie」更新 Cookie 后重新启动。")
        else:
            self._log("[服务] 取数通道=直连接口（http），Cookie 已就位。\n")
        if health:
            self._log(f"[服务] /health 已响应：{json.dumps(health, ensure_ascii=False)}\n")
            self._set_status(f"实时服务运行中：http://127.0.0.1:{port}/board")
        else:
            self._log("[服务] ⚠ /health 25 秒内没有响应，看日志区的 server 输出。\n")
            self._set_status("实时服务起来了，但 /health 没响应 —— 看日志")
        self.live_cfg["last_fetch"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._persist()
        self._refresh_live_page()
        self._check_port_detail()
        self._remember_live_run("实时服务已启动" if health else "实时服务已启动（/health 未响应）")
        self._refresh_roll_banner()

    def on_live_stop(self) -> None:
        if self._live_busy:
            return
        if not self.service.any_running():
            self._live_action_hint.configure(
                text="实时服务本来就没在跑。", fg=self.C["hint"])
            return

        def job() -> None:
            info = self.service.stop()
            port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
            live.wait_port_free(port, timeout=8)
            check = self.service.verify_stopped(port)
            self._post(lambda: self._live_stopped(info, check))

        self._live_busy = "stop"
        self._run_job(job, "正在停止实时服务 …")

    def _live_stopped(self, info: dict, check: dict) -> None:
        self._live_busy = ""
        self._busy_off()
        for note in info.get("notes") or []:
            self._log(f"[服务] {note}\n")
        if check.get("clean"):
            self._log(f"[服务] 端口 {check['port']} 已无监听（回收完成）。\n")
            self._set_status(f"实时服务已停止，端口 {check['port']} 已回收")
        else:
            self._log(f"[服务] ⚠ 端口 {check['port']} 仍有监听：PID {check['still_listening']}\n")
            self._set_status("已停止，但端口还被占着 —— 看日志")
        self._check_port_detail()

    def _check_port_detail(self) -> None:
        if not hasattr(self, "port_detail"):
            return
        port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
        pids = live.listening_pids(port)
        if not pids:
            text = f"端口 {port} 空闲；本程序起的服务：{'运行中' if self.service.any_running() else '未运行'}。"
        else:
            names = ", ".join(f"PID {pid}（{live.process_name(pid) or '未知'}）" for pid in pids)
            text = (f"端口 {port} 正在被监听：{names}。"
                    "如果是上次残留的实时服务，点「终止旧服务」；"
                    f"如果是别人在用的（例如 {live.OTHER_PORT} 上的回放），请改端口。")
        self.port_detail.configure(text=text)
        self._fit_all_hints()
        self._refresh_port_pill()
        self._refresh_service_panel()

    def on_kill_old(self) -> None:
        port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
        pids = [p for p in live.listening_pids(port) if p not in self.service.pids().values()]
        if not pids:
            self._set_status("没有需要终止的旧服务")
            self._check_port_detail()
            return
        names = "、".join(f"PID {pid}（{live.process_name(pid)}）" for pid in pids)
        if not messagebox.askyesno(
                "终止旧服务",
                f"即将强杀占用端口 {port} 的进程：\n{names}\n\n"
                "只会杀这一个端口的监听进程，不会碰其它服务。要继续吗？"):
            return
        notes = []
        for pid in pids:
            ok, note = live.kill_pid(pid)
            notes.append(f"PID {pid}：{note}")
        live.wait_port_free(port, timeout=6)
        self._log("[服务] 终止旧服务 —— " + "；".join(notes) + "\n")
        self._check_port_detail()

    def on_pick_free_port(self) -> None:
        port = live.free_port(int(self.live_cfg.get("port") or live.DEFAULT_PORT))
        self.port_var.set(str(port))
        self.live_cfg["port"] = port
        self._persist()
        self._check_port_detail()
        self._refresh_live_page()
        self._set_status(f"端口已改成 {port}")

    # ------------------------------------------------------------ 状态刷新

    def _post_boot(self) -> None:
        """启动后的收尾：环境预检（自动跑一次）、端口检查、榜单页刷新。"""
        self._refresh_cookie_pill()
        self._check_port_detail()
        self._refresh_live_page()
        self._refresh_roll_banner()
        if self.service.any_running():
            self._log("[服务] 检测到本程序记录的实时服务还在运行。\n")
        # M16b：**一开应用就把运行目录的 py + web 同步一遍**（内容比对，没变就什么都不做）。
        # 以前只在「点启动服务」时同步，于是「端口被占 / 没点启动 / 只开应用看一眼」
        # 这些路径运行目录永远停在旧版本 —— 用户升级后重开应用看到的还是旧样式。
        self.root.after(300, self._bg_sync_run_dir)
        # M18：**启动时清上一轮残留**（应用被强杀时 poller/server/java 会变孤儿）。
        # 只动"命令行里确认是我们自己的"（运行目录 + server.py/poller.py/.jar），
        # 对不上的一律不碰（PID 可能被系统复用成别的程序）。
        self.root.after(200, self._bg_reap_children)
        # 启动后**自动跑一次环境预检**（静默：不弹窗、不抢焦点），有 ❌ 会亮顶部红横幅。
        if self.mode == MODE_LIVE:
            self.root.after(600, lambda: self.on_precheck(silent=True))
        self.root.after(1200, self._tick_live)

    def _bg_reap_children(self) -> None:
        """后台清上一轮残留子进程（M18；不阻塞首屏、不弹窗）。"""
        def job() -> None:
            try:
                import child_registry
                report = child_registry.reap(live.run_dir(cfgmod.work_root()))
            except Exception as exc:  # noqa: BLE001
                self._post(lambda: self._log(f"[子进程] 启动清理失败：{exc}\n"))
                return
            killed = report.get("killed") or []
            skipped = report.get("skipped") or []

            def show() -> None:
                if killed:
                    self._log("[子进程] 启动清理：" + "；".join(
                        f"结束 {k['kind']} PID {k['pid']}（{k['how']}）" for k in killed)
                        + "\n")
                    self._set_status(f"启动清理：结束了 {len(killed)} 个上一轮残留的子进程")
                for item in skipped:
                    if item.get("why") != "已经不在":
                        self._log(f"[子进程] 跳过 PID {item['pid']}（{item['why']}）\n")

            self._post(show)

        threading.Thread(target=job, daemon=True).start()

    def _cleanup_children(self) -> dict:
        """退出时收掉自己起的子进程（M18；幂等，多次调用没事）。"""
        try:
            import child_registry
            report = child_registry.cleanup(live.run_dir(cfgmod.work_root()))
        except Exception as exc:  # noqa: BLE001
            try:
                self._log(f"[子进程] 退出清理失败：{exc}\n")
            except Exception:  # noqa: BLE001
                pass
            return {"count": 0, "notes": [str(exc)], "killed": [], "skipped": []}
        if report.get("killed"):
            try:
                self._log("[子进程] 退出清理：" + "；".join(
                    f"{k['kind']} PID {k['pid']}（{k['how']}）" for k in report["killed"])
                    + f" —— 已清理 {report['count']} 个子进程\n")
            except Exception:  # noqa: BLE001
                pass
        return report

    def _bg_sync_run_dir(self) -> None:
        """后台把运行目录资源同步到当前版本（不阻塞首屏，不弹窗）。"""
        def job() -> None:
            try:
                notes = live.sync_run_dir(live.run_dir(cfgmod.work_root()))
            except Exception as exc:  # noqa: BLE001 - 同步失败也不能影响主流程
                # 注意：**不能**在 lambda 里直接引用 except 的 exc —— except 结束后
                # exc 已被删除，延迟执行会抛 NameError（实测踩过）。先取出字符串。
                message = f"{type(exc).__name__}: {exc}"
                self._post(lambda m=message: self._log(f"[资源] 运行目录同步失败：{m}\n"))
                return
            summary = next((n for n in reversed(notes) if "运行目录资源已同步" in n), "")
            changed = [n for n in notes if "已同步（源" not in n]
            # 只有"真的有变化"或"有问题"才刷屏；没变化只留一行摘要（便于以后排查）
            def report() -> None:
                if changed:
                    self._log("[资源] " + "；".join(changed[:12])
                              + ("…\n" if len(changed) > 12 else "\n"))
                if summary:
                    self._log(f"[资源] {summary}\n")
                    self._set_status("运行目录资源已同步")
            self._post(report)

        threading.Thread(target=job, daemon=True).start()

    def _tick_live(self) -> None:
        try:
            interval = 1.0
            now = time.time()
            if now - self._live_last_tick >= interval:
                self._live_last_tick = now
                self._drain_live_logs()
                self._refresh_service_panel()
                # 卡片上写着「每 3 秒自动刷新」——之前没有任何调用点，
                # 于是「最近一次取数时间 / 耗时 / 累计请求数」永远是 — 和 0。
                if now - getattr(self, "_live_status_at", 0.0) >= 3.0:
                    self._live_status_at = now
                    try:
                        if self.service.running() or live.port_in_use(
                                int(self.live_cfg.get("port") or live.DEFAULT_PORT)):
                            self._refresh_live_status(quiet=True)
                    except Exception:  # noqa: BLE001
                        pass
            self.root.after(1000, self._tick_live)
        except Exception:  # noqa: BLE001
            self.root.after(2000, self._tick_live)

    def _drain_live_logs(self) -> None:
        """把 poller / server 的日志增量喂进底部日志面板。"""
        for child in (self.service.poller, self.service.server):
            if child is None:
                continue
            path = child.log_path
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= self._live_log_pos:
                if size < self._live_log_pos:
                    self._live_log_pos = 0
                continue
            try:
                with path.open("rb") as fh:
                    fh.seek(self._live_log_pos)
                    data = fh.read(65536)
                    self._live_log_pos = fh.tell()
            except OSError:
                continue
            text = data.decode("utf-8", errors="replace")
            if text:
                self._append_log(text)

    def _append_log(self, text: str) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def _refresh_service_panel(self) -> None:
        if not self._live_ready or not hasattr(self, "svc_vars"):
            return
        pids = self.service.pids()
        running = self.service.any_running()
        port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
        self._set_pill(self.svc_state, "运行中" if running else "未运行",
                       "ok" if running else "hint")
        self._set_pill(self.channel_state,
                       {"live": "运行中"}.get("live") if False else
                       ("运行中" if running else "未启动"), "ok" if running else "hint")
        self.svc_vars["pids"].set(
            f"poller {pids.get('poller') or '—'} / server {pids.get('server') or '—'}")
        self.svc_vars["port"].set(f"{port}（{'监听中' if live.port_in_use(port) else '空闲'}）")
        self.svc_vars["run_dir"].set(str(self.service.run))
        stats = live.poller_stats(self.service.poller_log)
        self.svc_vars["channel"].set(stats.get("channel") or "—")
        self.svc_vars["last_poll"].set(self.live_cfg.get("last_poll") or "—")
        self.svc_vars["teams"].set(str(stats.get("teams") or "—"))
        self.svc_vars["submissions"].set(str(stats.get("submissions") or "—"))
        self.svc_vars["latency"].set(
            f"{self.live_cfg.get('last_latency_ms')} ms（/health 往返）"
            if self.live_cfg.get("last_latency_ms") else "—")
        self.svc_vars["requests"].set(str(self.live_cfg.get("request_count") or 0))
        self._sync_service_buttons(running)

    def _sync_service_buttons(self, running: bool | None = None) -> None:
        """「启动 / 停止」只让能点的那个可点，并说明另一个为什么灰着。

        以前两个按钮永远可点：已经在跑时点「启动」、没跑时点「停止」都没有反应，
        第一次用的人会以为是程序卡了。
        """
        if not hasattr(self, "svc_hint"):
            return
        if running is None:
            try:
                running = self.service.any_running()
            except Exception:  # noqa: BLE001
                running = False
        for btn, enabled in ((getattr(self, "_svc_start_btn", None), not running),
                             (getattr(self, "_svc_stop_btn", None), running)):
            if btn is None:
                continue
            try:
                btn.configure(state="normal" if enabled else "disabled")
            except Exception:  # noqa: BLE001
                pass
        if running:
            self._msg(self.svc_hint,
                      "正在运行。去「直播」页复制网址给 OBS；比赛结束再点「停止」。", "ok")
        else:
            self._msg(self.svc_hint,
                      "还没启动。点「启动」即可（比赛数据要先在「比赛」页拉过一次，"
                      "否则榜单会是空的）。", "hint")

    def _refresh_live_status(self, force: bool = False, quiet: bool = False) -> None:
        """后台拉 /health（不阻塞界面、不抢焦点）。

        quiet=True 是「每 3 秒自动刷新」用的：不动进度条、不禁用按钮、
        也不额外拉 /api/board（那张表用不到），失败就静默跳过。
        """
        if quiet:
            if getattr(self, "_live_status_quiet", False):
                return                      # 上一轮还没回来，别叠
            self._live_status_quiet = True
        elif self._live_busy:
            return
        port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)

        def job() -> None:
            started = time.perf_counter()
            try:
                health = live.health(port)
            except Exception:  # noqa: BLE001
                health = None
            latency = (time.perf_counter() - started) * 1000
            if quiet:
                state, data = {}, {}
            else:
                state = live.read_json(self.service.state_path, {}) or {}
                data = live.board(port) or {}
            self._post(lambda: self._live_status_done(health, latency, state, data,
                                                      quiet=quiet))

        if quiet:
            threading.Thread(target=job, daemon=True).start()
        else:
            self._run_job(job, "刷新实时状态 …")

    def _poller_cycles(self) -> int | None:
        """poller.log 里累计跑过多少轮（读不到就 None，界面保留原值）。"""
        try:
            child = getattr(self.service, "poller", None)
            path = getattr(child, "log_path", None)
            if path is None:
                # 服务是别处起的（或本实例没托管）：退回运行目录里的 poller.log
                state = getattr(self.service, "state_path", None)
                if state is None:
                    return None
                path = Path(state).parent / "logs" / "poller.log"
            if not Path(path).is_file():
                return None
            return int(live.poller_stats(Path(path)).get("cycles") or 0)
        except Exception:  # noqa: BLE001
            return None

    def _live_status_done(self, health, latency, state, data, quiet: bool = False) -> None:
        if quiet:
            self._live_status_quiet = False
        else:
            self._busy_off()
        if health:
            self.live_status = health
            self.live_cfg["last_latency_ms"] = round(latency, 1)
            # 「累计请求数」= poller 真正跑过的轮数（从 poller.log 数），
            # 不是界面刷新次数 —— 否则每 3 秒 +1，一会儿就上千，看不出真实取数量。
            cycles = self._poller_cycles()
            if cycles is not None:
                self.live_cfg["request_count"] = cycles
            self.live_cfg["last_poll"] = health.get("last_poll") or ""
            self.live_cfg["last_health_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._persist()
        self._refresh_service_panel()
        self._refresh_roll_banner()

    # ------------------------------------------------------------ 通用操作

    def on_open_board(self) -> None:
        port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
        if not live.port_in_use(port):
            self._set_status(f"端口 {port} 上没有服务 —— 先到「服务」页点「启动」")
            self._log(f"[直播] 端口 {port} 没有监听，先在「服务」页点「启动」。\n")
            return
        url = live.board_url(port, self._live_query())
        webbrowser.open(url)
        self._log(f"[直播] 已在浏览器打开：{url}\n")

    def on_precheck(self, silent: bool = False) -> None:
        """环境预检：逐项检查并写「是干什么的 + ✅/⚠️/❌ + 怎么办」。

        ``silent=True`` 是启动后自动跑的那一次（同样不弹窗，只是状态栏不抢眼）。
        """
        if self._live_busy:
            return

        def job() -> None:
            items: list[dict] = []
            port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
            platform = self._platform()
            psid = str(self.cfg.get("problem_set_id") or "")

            # ① VJudge：比赛 ID 能不能拉到 rank JSON（只读，不写文件）
            #    PTA ：Cookie 是否有效 —— 真的请求一次 PTA（不是只看文件在不在）
            if platform == "vjudge":
                items.append(self._vjudge_precheck_item(psid))
            else:
                problem = live.cookie_problem(cfgmod.work_root())
                if problem:
                    items.append({
                        "level": "err", "name": "Cookie 是否有效",
                        "what": "实时榜单取数就靠它（真实请求一次 PTA 接口验证）",
                        "detail": problem,
                        "fix": "去更新 Cookie：① 浏览器登录 pintia.cn 并打开这场比赛 → F12 → "
                               "Network → 右键请求 → Copy as cURL (bash) → 回本页「粘贴 Cookie」；"
                               "② 或 F12 → Application → Cookies → https://pintia.cn，在 Request "
                               "cookies 里分别复制 JSESSIONID 与 PTASession 两个值粘成一行"
                               "（两个都必须带，只带 PTASession 会 401）。"})
                else:
                    try:
                        code, out = self._run_cookie_check()
                    except Exception as exc:  # noqa: BLE001
                        code, out = 1, f"自检异常：{exc}"
                    if code == 0:
                        items.append({
                            "level": "ok", "name": "Cookie 是否有效",
                            "what": "实时榜单取数就靠它（真实请求一次 PTA 接口验证）",
                            "detail": f"真实请求了一次 PTA（题目集 {psid or '未填'}），Cookie 可用",
                            "fix": ""})
                    elif code == 2:
                        items.append({
                            "level": "warn", "name": "Cookie 是否有效",
                            "what": "实时榜单取数就靠它（真实请求一次 PTA 接口验证）",
                            "detail": out.strip()[:180],
                            "fix": "这台机器上没有可用的 Python 解释器，所以只能看 cookie.txt 在不在、"
                                   "没法真的请求一次；实时服务本身也需要 Python，请先装 Python 3.9+。"})
                    else:
                        tail = " / ".join(line.strip() for line in out.splitlines()[-2:] if line.strip())
                        items.append({
                            "level": "err", "name": "Cookie 是否有效",
                            "what": "实时榜单取数就靠它（真实请求一次 PTA 接口验证）",
                            "detail": f"真实请求失败（退出码 {code}）：{tail[:180]}",
                            "fix": "Cookie 多半已过期。去更新 Cookie（两条做法同上）；"
                                   "更新后重新点「▶ 开始检查」。"})

            # ② 服务端口是否空闲
            pids = live.listening_pids(port)
            if pids:
                names = "、".join(f"PID {pid}（{live.process_name(pid)}）" for pid in pids)
                level = "warn" if self.service.any_running() and port == int(
                    self.live_cfg.get("port") or 0) else "err"
                items.append({
                    "level": level, "name": "服务端口是否空闲",
                    "what": "榜单页要在本机这个端口上开服务，被占就起不来",
                    "detail": f"端口 {port} 被占用：{names}"
                              + ("（看起来是本程序自己在跑）" if level == "warn" else ""),
                    "fix": "① 到「4 服务」页点「终止旧服务」收掉上次残留的服务；"
                           "② 或者点「换一个空闲端口」。"})
            else:
                items.append({
                    "level": "ok", "name": "服务端口是否空闲",
                    "what": "榜单页要在本机这个端口上开服务，被占就起不来",
                    "detail": f"端口 {port} 空闲", "fix": ""})
            if live.port_in_use(live.OTHER_PORT):
                items.append({
                    "level": "warn", "name": f"端口 {live.OTHER_PORT}（别人的服务）",
                    "what": "通常有别人的回放在跑",
                    "detail": f"{live.OTHER_PORT} 上有服务在监听 —— 本程序不会去动它",
                    "fix": "只要上面选的端口不是它就行，不用处理。"})

            # ③ Python 解释器是否可用
            okpy, pynote = live.is_real_python()
            items.append({
                "level": "ok" if okpy else "err", "name": "Python 解释器是否可用",
                "what": "实时服务的取数进程和网页服务都要用它来跑",
                "detail": pynote,
                "fix": "" if okpy else "装 Python 3.9+（安装时勾选 Add to PATH），"
                                       "或设置环境变量 PTA_PYTHON 指向 pythonw.exe，然后重启本程序。"})

            # ④ Pillow / 字体等依赖是否就绪
            pok, pnote = live.pillow_available()
            font_ok, font_note = True, ""
            try:
                from PIL import ImageFont  # noqa: F401
            except Exception as exc:  # noqa: BLE001
                font_ok, font_note = False, f"Pillow 字体模块不可用：{exc}"
            level = "ok" if (pok and font_ok) else ("warn" if pok else "err")
            items.append({
                "level": level, "name": "Pillow / 字体等依赖是否就绪",
                "what": "校徽裁圆、榜单页字体渲染要用（缺了中文会变成方框）",
                "detail": pnote + ("；字体渲染可用" if font_ok else f"；{font_note}"),
                "fix": "" if level == "ok" else
                       "pip install Pillow；中文字体不用设置，程序会自动用内置字体"
                       "（内置字体缺失时才会出现 □ 方框，重装一份完整程序即可）。"})

            # ⑤ 历史数据目录可写
            try:
                writable, wnote = live._probe_writable(cfgmod.work_root())
            except Exception as exc:  # noqa: BLE001
                writable, wnote = False, str(exc)
            items.append({
                "level": "ok" if writable else "err", "name": "历史数据目录可写",
                "what": "运行记录、榜单快照、配置要写在这里",
                "detail": (str(cfgmod.work_root()) if writable else wnote),
                "fix": "" if writable else "把程序/work 目录换成可写目录（例如桌面或 D 盘）再运行。"})

            self._post(lambda: self._precheck_done(items, silent))

        self._run_job(job, "正在跑环境预检 …" if not silent else "启动自检 …")

    def _run_cookie_check(self) -> tuple[int, str]:
        """调用 pta_fetch.py check --via http（只读，不写任何文件）。

        这就是「Cookie 是否有效」的真实判据 —— 真的请求一次 PTA，不是只看文件在不在。

        注意：**必须用一个真的 Python 解释器**（``live.find_python()``）。
        打包版里 ``sys.executable`` 是 exe 自己，直接拿它跑 pta_fetch.py 会变成
        「再启动一个本程序」—— 启动后自动预检会因此拉起第二个窗口，绝不能这么干。
        没有可用解释器时返回退出码 2（调用方按「⚠️ 无法验证」显示，不误报 Cookie 失效）。
        """
        ok, note = live.is_real_python()
        if not ok:
            return 2, "环境里没有可用的 Python 解释器，无法真实请求一次 PTA 验证 Cookie：" + note
        python = live.find_python()
        root = cfgmod.resource_root()
        script = Path(root) / "pta_fetch.py"
        _text, path = live.read_cookie(cfgmod.work_root())
        argv = [python, str(script), "check",
                "--problem-set", str(self.cfg.get("problem_set_id") or ""),
                "--via", "http", "--max-pages", "1", "--max-retries", "1"]
        if path:
            argv += ["--cookie-file", str(path)]
        proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120,
                              creationflags=live._no_window())
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def _run_vjudge_check(self, contest_id: str = "") -> tuple[int, str]:
        """调用 vjudge_fetch.py check --contest <id>（只读，不写任何文件）。

        VJudge 的环境预检就查这一件事：比赛 ID 能不能拉到 rank JSON。
        其余检查项（端口 / Python / Pillow / 历史目录）跟平台无关，照旧检查。
        """
        psid = str(contest_id or self.cfg.get("problem_set_id") or "").strip()
        if not psid:
            return 2, "还没填 VJudge 比赛 ID"
        ok, note = live.is_real_python()
        if not ok:
            return 2, "环境里没有可用的 Python 解释器，无法真实请求一次 VJudge：" + note
        python = live.find_python()
        root = cfgmod.resource_root()
        script = Path(root) / "vjudge_fetch.py"
        if not script.is_file():
            return 1, f"找不到 {script}（VJudge 版抓取器没跟程序放在一起）"
        argv = [python, str(script), "check", "--contest", psid, "--max-retries", "1"]
        proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120,
                              creationflags=live._no_window())
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def _vjudge_precheck_item(self, contest_id: str = "") -> dict:
        """VJudge 平台的环境预检项：比赛 ID 能否拉到 rank JSON。"""
        psid = backend.normalize_vjudge_contest(contest_id or self.cfg.get("problem_set_id") or "")
        if not psid:
            return {"level": "err", "name": "VJudge 比赛 ID 能否拉到 rank JSON",
                    "what": "实时榜单取数就靠这个接口（匿名 GET，公开比赛无需登录）",
                    "detail": f"还没填 / 填的认不出来：{contest_id!r}",
                    "fix": "到本页「VJudge 比赛 ID」里填 862258 这种数字，"
                           "或直接粘 https://vjudge.net/contest/862258#rank。"}
        try:
            code, out = self._run_vjudge_check(psid)
        except Exception as exc:  # noqa: BLE001
            code, out = 1, f"自检异常：{exc}"
        tail = " / ".join(line.strip() for line in out.splitlines()[-2:] if line.strip())
        if code == 0:
            return {"level": "ok", "name": "VJudge 比赛 ID 能否拉到 rank JSON",
                    "what": "实时榜单取数就靠这个接口（匿名 GET，公开比赛无需登录）",
                    "detail": f"比赛 {psid} 的 rank JSON 可读（不需要 Cookie）", "fix": ""}
        if code == 2:
            return {"level": "warn", "name": "VJudge 比赛 ID 能否拉到 rank JSON",
                    "what": "实时榜单取数就靠这个接口（匿名 GET，公开比赛无需登录）",
                    "detail": tail[:180],
                    "fix": "没有可用的 Python 解释器就没法真的请求一次；"
                           "实时服务本身也要 Python，请先装 Python 3.9+。"}
        return {"level": "err", "name": "VJudge 比赛 ID 能否拉到 rank JSON",
                "what": "实时榜单取数就靠这个接口（匿名 GET，公开比赛无需登录）",
                "detail": f"请求没通过（退出码 {code}）：{tail[:180]}",
                "fix": "检查比赛 ID 是否正确、网络是否通；VJudge 偶发很慢，"
                       "过一会儿重试；私有比赛本版本不支持（没有密码赛登录）。"}

    def _precheck_done(self, items: list[dict], silent: bool = False) -> None:
        self._busy_off()
        text = self.precheck_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        for item in items:
            mark = self.PRECHECK_MARKS.get(item["level"], "•")
            line = f"{mark} {item['name']}：{item['detail']}\n"
            text.insert("end", line, item["level"])
            text.insert("end", f"     这是干什么的：{item.get('what') or '—'}\n")
            if item.get("fix"):
                text.insert("end", f"     怎么办：{item['fix']}\n", "fix")
        text.configure(state="disabled")
        bad = [i for i in items if i["level"] == "err"]
        warn = [i for i in items if i["level"] == "warn"]
        self._set_pill(self.precheck_state,
                       "全部通过 ✅" if not bad and not warn else
                       (f"{len(bad)} 项不行 ❌" if bad else f"{len(warn)} 项提醒 ⚠️"),
                       "ok" if not bad and not warn else ("err" if bad else "warn"))
        if bad:
            names = "、".join(i["name"] for i in bad)
            self._set_precheck_banner(
                f"❌ 环境预检有 {len(bad)} 项不通过：{names} —— "
                "请看下面每一项的「怎么办」，修好后再点「▶ 开始检查」。")
        else:
            self._set_precheck_banner("")
        plain = "\n".join(
            f"[{'OK' if i['level'] == 'ok' else ('提醒' if i['level'] == 'warn' else '不行')}] "
            f"{i['name']}：{i['detail']}" for i in items)
        self._log("\n" + "=" * 62 + "\n  环境预检\n" + "=" * 62 + "\n" + plain + "\n")
        self._set_status(f"环境预检完成：{len(items)} 项，"
                         f"{'全部通过' if not bad and not warn else f'不行 {len(bad)} 项 / 提醒 {len(warn)} 项'}")

    def on_test_cookie(self) -> None:
        if self._live_busy:
            return
        ok, note = live.cookie_status(cfgmod.work_root())
        if not ok:
            self._refresh_cookie_pill()
            self._log(f"[Cookie] {note}\n")
            self._set_status("没有 cookie.txt —— 可以用「粘贴 Cookie」生成一份")
            return

        def job() -> None:
            code, out = self._run_cookie_check()
            self._post(lambda: self._cookie_tested(code, out))

        self._run_job(job, "正在测试 Cookie（pta_fetch.py check --via http）…")

    def _cookie_tested(self, code: int, out: str) -> None:
        self._busy_off()
        self._log("\n" + out.strip() + "\n")
        if code == 0:
            for entry in self._cookie_widgets.values():
                self._set_pill(entry["pill"], "Cookie 有效", "ok")
                entry["hint"].configure(
                    text="Cookie 自检通过：可以用「仅 http」或「自动」通道直连接口取数。",
                    fg=self.C["ok"])
            self._set_status("Cookie 有效")
        else:
            for entry in self._cookie_widgets.values():
                self._set_pill(entry["pill"], "Cookie 失效", "err")
                entry["hint"].configure(
                    text=("Cookie 自检失败（退出码 %d）。实时榜单必须有有效 Cookie，"
                          "否则取不到任何数据（不会去借浏览器）。处理办法：\n"
                          "1) 打开 https://pintia.cn 并确认是登录状态；\n"
                          "2) F12 → Network → 刷新页面 → 右键任意 pintia.cn/api/… 请求 → "
                          "Copy → Copy as cURL (bash)；\n"
                          "3) 回到这里点「粘贴 Cookie」，把整段粘进第一个框保存"
                          "（也可以把两个值分别粘进「JSESSIONID」「PTASession」两个框，不用记分号）；\n"
                          "4) **必须两个都带**：实测只带 PTASession 会 401，"
                          "在 Application → Cookies → https://pintia.cn 的 Request cookies 里"
                          "（不是 Response cookies）分别复制两个值，别双击值单元格；\n"
                          "5) 更新完再点一次「测试 Cookie」。" % code),
                    fg=self.C["err"])
        self._fit_all_hints()

    #: M14：对话框里两个独立输入框的宽度（GUI 截图 900x600 下也不截断）
    _COOKIE_FIELD_W = 46

    def _cookie_dialog_geometry(self, width: int = 860, height: int = 600) -> str:
        """算一个「不超出屏幕」的对话框尺寸（小屏 900x600 也要放得下）。"""
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        except Exception:  # noqa: BLE001
            sw, sh = width, height
        return f"{min(width, max(560, sw - 60))}x{min(height, max(420, sh - 80))}"

    def on_paste_curl(self) -> None:
        """M14：Cookie 粘贴对话框。

        三种输入**都能用**，而且可以混着用（哪个有值用哪个）：
        ① 最上面的「整段粘贴」框 —— 浏览器 Copy → Copy as cURL (bash)，一个字符都不用改；
        ② 「JSESSIONID」与「PTASession」两个独立输入框 —— 只会手工复制的用户直接分开粘，
           **再也不用记分号**；
        ③ 整段粘贴里的名字对不上时（两个裸值），程序会让你在下拉里选谁是谁，不瞎猜。
        """
        dialog = tk.Toplevel(self.root)
        dialog.title("粘贴 Cookie（整段 / 两个值分开粘都行）")
        dialog.configure(bg=self.C["card"])
        dialog.transient(self.root)
        dialog.resizable(True, True)
        dialog.geometry(self._cookie_dialog_geometry())
        try:
            dialog.minsize(720, 520)
        except Exception:  # noqa: BLE001
            pass

        head = self._t(dialog, "把浏览器整段复制的 cURL 粘进①最省事；"
                               "也可以把两个值分别粘进②的两个框（不用记分号）",
                       size=10, bold=True, fg=self.C["ink"], justify="left", anchor="w",
                       wraplength=820)
        head.pack(anchor="w", fill="x", padx=14, pady=(12, 2))
        hint = self._hint(
            dialog,
            "⚠️ JSESSIONID 和 PTASession 必须两个都带：实测只带 PTASession 会 401。\n"
            "· ① 整段复制（推荐，一个字符都不用改）：DevTools → Network → 刷新页面 → "
            "右键任意 pintia.cn/api/… 请求 → Copy → Copy as cURL (bash) → 粘进下面第一个框。\n"
            "· ② 两个值分开粘：DevTools → Application → Cookies → https://pintia.cn → "
            "在 Request cookies 里（不是 Response cookies）点一行、在下面的 Value 列复制"
            "（别双击值单元格，双击会就地编辑/清空它）→ 分别粘进「JSESSIONID」/「PTASession」。\n"
            "· 空格分隔、两行分开写、只粘一个裸值都认；两个裸值分不清谁是谁时会让你选。")
        # 注意：``_hint`` 内部已经把它 ``pack(side="left")`` 了（卡片里是「一行」的用法），
        # 对话框里要单独占一行，所以这里必须显式把它改回 ``side="top"``。
        hint.pack(side="top", anchor="w", padx=14, pady=(0, 6), fill="x")

        self._t(dialog, "① 整段粘贴（Copy as cURL (bash) / 一行 Cookie 都行）",
                size=9, fg=self.C["sub"], anchor="w").pack(anchor="w", padx=14)
        text = tk.Text(dialog, height=7, wrap="word", font=("Consolas", 9),
                       bg="#f7f9fc", fg=self.C["ink"], relief="flat", bd=0,
                       padx=8, pady=6, highlightthickness=1,
                       highlightbackground="#c8d2de")
        text.pack(fill="both", expand=True, padx=14, pady=(2, 6))

        # ---- ② 两个独立输入框（M14 新增：不用记分号、不用拼串）--------------
        fields = self._f(dialog)
        fields.pack(fill="x", padx=14)
        self._t(fields, "② 或者只粘两个值（哪个有值用哪个；跟①混着用也行）",
                size=9, fg=self.C["sub"], anchor="w").pack(anchor="w")
        js_row = self._f(fields)
        js_row.pack(fill="x", pady=(4, 0))
        self._t(js_row, "JSESSIONID", size=10, fg=self.C["sub"], width=12,
                anchor="w").pack(side="left")
        js_var = tk.StringVar()
        js_entry = self._entry(js_row, js_var, self._COOKIE_FIELD_W)
        js_entry.pack(side="left", fill="x", expand=True)
        ps_row = self._f(fields)
        ps_row.pack(fill="x", pady=(4, 0))
        self._t(ps_row, "PTASession", size=10, fg=self.C["sub"], width=12,
                anchor="w").pack(side="left")
        ps_var = tk.StringVar()
        ps_entry = self._entry(ps_row, ps_var, self._COOKIE_FIELD_W)
        ps_entry.pack(side="left", fill="x", expand=True)

        # ---- ③ 两个裸值判不出谁是谁时的选项（默认按粘贴顺序，不瞎猜）------
        choice_box = self._f(dialog)
        choice_var = tk.StringVar(value="")
        choice_btns: list = []

        def _hide_choices() -> None:
            for widget in choice_btns:
                widget.destroy()
            choice_btns.clear()
            choice_box.pack_forget()

        def _show_choices(options: list[dict]) -> None:
            _hide_choices()
            self._t(choice_box, "⚠ 这两个裸值分不清谁是 JSESSIONID（点一下选好再保存）：",
                    size=9, fg=self.C["warn"], anchor="w").pack(anchor="w")
            choice_var.set(options[0]["cookie"])
            for idx, option in enumerate(options):
                btn = tk.Radiobutton(
                    choice_box, text=option["label"], variable=choice_var,
                    value=option["cookie"], bg=self.C["warn_bg"], fg=self.C["sub"],
                    activebackground=self.C["warn_bg"], selectcolor="white",
                    font=(UI_FONT, 9), anchor="w", bd=0, highlightthickness=0,
                    cursor="hand2")
                btn.pack(anchor="w")
                choice_btns.append(btn)
            choice_box.pack(fill="x", padx=14, pady=(6, 0), before=state)
            dialog.update_idletasks()

        choice_box.pack_forget()

        # 成功/失败提示都可能有 3-4 行长句：必须让它自己换行，否则会把对话框顶宽（右边看不见）。
        state = self._t(dialog, "", size=9, fg=self.C["hint"], justify="left", anchor="w",
                        wraplength=820)
        state.pack(anchor="w", fill="x", padx=14, pady=(8, 0))

        def _collect() -> dict:
            return live.parse_cookie_inputs(text.get("1.0", "end"),
                                            js_var.get(), ps_var.get())

        def do_parse(save: bool) -> None:
            result = _collect()
            if result["needs_choice"] and not choice_var.get():
                _show_choices(result["choices"])
                state.configure(text=result["note"], fg=self.C["warn"])
                return
            value = choice_var.get() or result["cookie"]
            if not value:
                # note 里已经写清「你粘的是什么」+ 两条做法，这里原样显示（红字，对话框不关，方便重粘）。
                _hide_choices()
                state.configure(text=result["note"], fg=self.C["err"])
                return
            note = result["note"]
            level, _sentence = live.cookie_diagnosis(value)
            if not save:
                _hide_choices()
                state.configure(text=f"{note}：{value[:60]}…",
                                fg=self.C["ok"] if level == "ok" else self.C["warn"])
                return
            ok, where = live.write_cookie(cfgmod.work_root(), value)
            if ok:
                _hide_choices()
                state.configure(text=f"已保存到 {where} —— {note}",
                                fg=self.C["ok"] if level == "ok" else self.C["warn"])
                self._log(f"[Cookie] 已解析并保存：{note} -> {where}\n")
                self._refresh_cookie_pill()
                if level == "ok":
                    self._set_status("Cookie 已保存（JSESSIONID + PTASession 都在），可以点「测试 Cookie」验证")
                    dialog.after(1200, dialog.destroy)
                else:
                    # 缺字段时把警告留在屏幕上（不自动关），用户照着补完再粘一次。
                    self._set_status("Cookie 已保存，但缺少必需字段 —— 请看红字提醒后再「测试 Cookie」")
                    dialog.after(15000, dialog.destroy)
            else:
                state.configure(text=f"保存失败：{where}", fg=self.C["err"])
                self._log(f"[Cookie] 保存失败：{where}\n")

        bar = self._f(dialog)
        bar.pack(fill="x", padx=14, pady=10)
        self._solid_btn(bar, "解析并保存", lambda: do_parse(True),
                        self.C["accent"], self.C["accent_hover"]).pack(side="left")
        self._ghost_btn(bar, "只解析看看", lambda: do_parse(False)).pack(side="left", padx=8)
        self._ghost_btn(bar, "取消", dialog.destroy).pack(side="right")
        # Enter = 解析并保存（两个输入框里都生效），Esc = 关掉
        for widget in (js_entry, ps_entry):
            widget.bind("<Return>", lambda _e: do_parse(True))
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        text.focus_set()

    # ------------------------------------------------------------ 滚榜提示

    def _build_roll_banner(self) -> None:
        """在滚榜模式的「3 滚榜」页最上面插一条「比赛数据已就绪」的横幅。

        必须在这一页的卡片都建好之后调用：那时页头已经 pack 好，
        这里用 ``after=`` 把横幅插到「这页怎么用」步骤带**后面**
        （不能 pack 到末尾，否则会跑到页面最下面）。
        """
        children = self.tab_roll.winfo_children()
        if len(children) < 2:
            return
        banner = self._f(self.tab_roll, bg="#eef7f0",
                         highlightbackground="#bfe3cb", highlightthickness=1, bd=0)
        anchor = getattr(self, "_step_bands", {}).get(7)
        if anchor is not None:
            banner.pack(fill="x", padx=24, pady=(10, 0), after=anchor)
        else:
            banner.pack(fill="x", padx=24, pady=(10, 0), before=children[1])
        inner = self._f(banner, bg="#eef7f0")
        inner.pack(fill="x", padx=14, pady=8)
        self._t(inner, "✓", size=11, bold=True, fg=self.C["ok"],
                bg="#eef7f0").pack(side="left", padx=(0, 8))
        self.roll_banner_var = tk.StringVar(value="")
        label = tk.Label(inner, textvariable=self.roll_banner_var, bg="#eef7f0",
                         fg="#166534", font=(UI_FONT, 9), anchor="w", justify="left",
                         wraplength=420)
        label.pack(side="left", fill="x", expand=True)
        self._auto_wrap(label, source=inner, min_width=200)

    def _refresh_roll_banner(self) -> None:
        """「3 滚榜」页顶部：比赛数据已就绪（来自 <时间> 的拉取）。"""
        var = getattr(self, "roll_banner_var", None)
        if var is None:
            return
        when = self.live_cfg.get("last_fetch") or ""
        health = self.live_status or {}
        board = self.live_cfg.get("last_board") or {}
        teams = health.get("teams") or board.get("teams")
        subs = health.get("submissions")
        bits = []
        if when:
            bits.append(f"比赛数据已就绪（最近一次取数：{when}）")
        else:
            bits.append("还没有取过比赛数据：先去「1 数据源」页点「拉取数据」")
        if teams:
            bits.append(f"队伍 {teams} 支")
        if subs:
            bits.append(f"提交 {subs} 条")
        pkg = self.cfg.get("last_package_dir") or ""
        if pkg and Path(pkg).is_dir():
            bits.append(f"比赛数据包：{pkg}")
        bits.append("下一步：先在下面点「生成比赛数据包」，再点绿色大按钮「▶ 开始滚榜」")
        self.roll_banner_var.set("；".join(bits))

    # ================================================================
    # M13（4b / 4d）：字体与队名显示
    # ================================================================
    # 「中文字体（必做）」整张卡片和「队伍名怎么显示」那一行已经从本壳与基类里
    # **删掉**：字体由后端在 font_path 为空/失效时自动回落到内置字体
    # （``board_fonts.resolve_font_path`` → 程序目录 tools\fonts\MicrosoftYaHei.ttc），
    # 队名一律 ``{team.name}``。基类也已清干净（不再引用 font_var / font_combo /
    # font_note / font_state / font_banner / display_var），所以这里不需要任何替身。

    # ------------------------------------------------------------ 配置收发

    def _collect_config(self) -> None:
        BoardApp._collect_config(self)
        self._collect_live_config()
        # 每个模式各自记住上次停在那一页（页内序号，不含另一个模式的偏移）
        try:
            pages = MODE_PAGES[self.mode]
            offset = pages.index(self._nav_current) if self._nav_current in pages else 0
            self.cfg["last_tab_live" if self.mode == MODE_LIVE else "last_tab_roll"] = offset
        except Exception:  # noqa: BLE001
            pass

    def _apply_config(self) -> None:
        """基类只认一个 last_tab；这里改成「每个模式各自一份」。"""
        BoardApp._apply_config(self)
        self.mode = str(self.cfg.get("mode") or self.mode)
        if self.mode not in (MODE_LIVE, MODE_ROLL):
            self.mode = MODE_LIVE
        self._last_tab = {
            MODE_LIVE: self._clamp_tab(MODE_LIVE, self.cfg.get("last_tab_live")),
            MODE_ROLL: self._clamp_tab(MODE_ROLL, self.cfg.get("last_tab_roll")),
        }
        if self._live_ready:
            self._nav_select(self._last_tab[self.mode])

    # ------------------------------------------------------------ 取数 / 生成包

    def on_fetch(self, check_only: bool = False) -> None:
        """「拉取/刷新数据」。

        实时模式 = 增量：重启 poller 并让它立刻跑一轮（不重拉全量、不影响 server）。
        滚榜模式 = 原样走基类（全量抓取，供生成比赛包）。
        """
        self._op_started = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.mode != MODE_LIVE or check_only:
            return BoardApp.on_fetch(self, check_only)

        self._collect_config()
        self._persist()
        if not str(self.cfg.get("problem_set_id") or "").isdigit():
            BoardApp.on_fetch(self, check_only)
            return
        port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
        self._log(f"\n[实时] 增量刷新数据（题目集 {self.cfg.get('problem_set_id')}）…\n")
        if not self.service.any_running():
            self._log("[实时] 服务没在跑：先到「4 服务」页点「启动」，再刷新。\n")
            self.on_live_start()
            return

        def job() -> None:
            self.service.repoll_once(port, str(self.cfg.get("problem_set_id") or ""),
                                     interval=float(self.live_cfg.get("poll_interval", 5)),
                                     full_interval=float(
                                         self.live_cfg.get("full_check_interval", 120)))
            self._post(self._live_repolled)

        self._run_job(job, "正在实时增量刷新 …")

    def _live_repolled(self) -> None:
        self._busy_off()
        self.live_cfg["last_fetch"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._persist()
        self._log("[实时] poller 已重启并开始新一轮增量拉取（5 秒内会有新数据）。\n")
        self._set_status("实时数据正在增量刷新，看「4 服务」页的状态")
        self._remember_live_run("实时数据增量刷新")
        self._refresh_roll_banner()
        for _fn in (self._build_star_picker, self._memo_reload):
            try:
                self._post(_fn)      # 新队伍 / 新备注名立刻反映到打星列表和备注表
            except Exception:  # noqa: BLE001
                pass

    def _fetch_done(self, result: dict) -> None:
        BoardApp._fetch_done(self, result)
        if result.get("ok"):
            self.live_cfg["last_fetch"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                self._stamp_history(mode=self.mode, started_at=self._op_started,
                                    result=result.get("message") or "抓取成功")
            except Exception:  # noqa: BLE001
                pass
            self._persist()
            self._refresh_history()
            self._refresh_roll_banner()
            for _fn in (self._build_star_picker, self._memo_reload):
                try:
                    self._post(_fn)      # 拉取成功后打星列表 / 备注表自动跟上
                except Exception:  # noqa: BLE001
                    pass

    def on_package(self) -> None:
        self._op_started = time.strftime("%Y-%m-%d %H:%M:%S")
        return BoardApp.on_package(self)

    def _package_done(self, result: dict) -> None:
        BoardApp._package_done(self, result)
        if result.get("ok"):
            self.live_cfg["last_fetch"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                self._stamp_history(mode=self.mode, started_at=self._op_started,
                                    result=result.get("message", "").splitlines()[0])
            except Exception:  # noqa: BLE001
                pass
            self._persist()
            self._refresh_history()
            self._refresh_roll_banner()

    def on_close(self) -> None:
        """退出时自动停掉自己起的 poller / server / java（M18：一个都不留）。"""
        try:
            if self.service.any_running():
                info = self.service.stop()
                self._log("[退出] 已停止实时服务：" + "；".join(info.get("notes") or []) + "\n")
                port = int(self.live_cfg.get("port") or live.DEFAULT_PORT)
                live.wait_port_free(port, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        # M18：按登记表兜底清一遍（含上一轮孤儿、Resolver 的 java、Esc 守护）
        self._cleanup_children()
        # 退出前把界面上刚改的值（尤其是「榜单页标题」这种只刷新网址、不触发持久化的输入框）
        # 收进配置再存盘 —— 否则改完标题直接关窗口，下次启动又回到旧值，看起来就像「改了不生效」。
        try:
            self._collect_config()
        except Exception:  # noqa: BLE001 - 关窗口这一步不能炸
            pass
        BoardApp.on_close(self)


# ==========================================================================
# 无头截图（验收用；不抢焦点、不闪屏）
# ==========================================================================

def debug_mark(message: str) -> None:
    """把启动关键点写到 logs\\assistant-debug.log（排查打包后卡住用）。

    GUI 是 windowed（console=False）的，打包后 stderr 看不见，
    Tk 回调里的异常也不会走 sys.excepthook，所以留一个落盘的痕迹。
    """
    try:
        folder = cfgmod.work_root() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "assistant-debug.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except Exception:  # noqa: BLE001
        pass


def run_headless_shot(out_root: Path, geometry: str = "1200x820",
                      only_mode: str | None = None) -> int:
    """建窗口 → update → PrintWindow 截图；不 SetForegroundWindow、不抢焦点。"""
    import json as _json
    import tkinter as tk

    store = cfgmod.Store()
    store.load()
    notes: list[str] = []
    ensure_live_config(store.config, notes)
    root = tk.Tk()
    style = ttk.Style()
    for theme in ("vista", "winnative", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    app = AssistantApp(root, store, base.StartupTrace(False, False))
    app._shot_only = True
    # 900x600 这一类小尺寸要能建出来：临时放宽 minsize（截图后进程就退了）
    try:
        width, height = (int(part) for part in geometry.lower().split("x")[:2])
        if width < 980 or height < 640:
            root.minsize(600, 400)
    except ValueError:
        width, height = 1200, 820
    root.geometry(geometry)
    root.update_idletasks()
    root.update()
    time.sleep(1.2)
    root.update()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import capture_window  # 延迟导入，只有截图时才需要

    # 输出目录：**写不进去就只写一行日志 + 退出码 3，绝不弹框**
    # （无人值守下弹窗会把进程卡死；实测本机有些目录 —— 例如工具目录与 %TEMP% ——
    #   对应用进程反而不可写，所以默认目录必须是"程序自己目录"。）
    try:
        out_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[错误] 截图目录写不进去：{out_root}（{type(exc).__name__}: {exc}）"
              " —— 请用 --shot-out 指一个可写目录（默认是程序目录下的 shots\\）。", flush=True)
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        return 3
    # M18：`--headless-shot` 跑完也不许留子进程（万一这轮起了 poller/server）
    import atexit
    atexit.register(app._cleanup_children)
    report = {"geometry": geometry, "size": f"{width}x{height}", "frames": [], "notes": notes,
              "out_root": str(out_root), "errors": []}
    modes = [only_mode] if only_mode in (MODE_LIVE, MODE_ROLL) else [MODE_LIVE, MODE_ROLL]
    for mode in modes:
        app.mode = mode
        app._last_tab[mode] = MODE_PAGES[mode][0]
        app._apply_mode(write=False)
        root.update_idletasks()
        root.update()
        time.sleep(0.4)
        for idx, key, _title, _sub, _steps, number in MODE_PAGE_DEFS[mode]:
            app._last_tab[mode] = idx
            app._nav_select(idx)
            root.update_idletasks()
            root.update()
            time.sleep(0.4)
            # 文件名带「本模式内的页码」，例如 live-1-datasource-1200x820.png
            name = f"{mode}-{number}-{key}-{geometry}.png"
            path = out_root / name
            # 单个页面抓图失败也不能中断/抛异常（抛出去会走 excepthook）：
            # 记一行日志、写进 report，继续下一页。
            try:
                ok, note = capture_window.grab_with_fallback(root.winfo_id(), path)
            except Exception as exc:  # noqa: BLE001
                ok, note = False, f"{type(exc).__name__}: {exc}"
            report["frames"].append({"mode": mode, "page": key, "number": number,
                                     "file": name, "ok": bool(ok), "note": note,
                                     "hwnd": capture_window.find_hwnd(root.winfo_id())})
            if not ok:
                report["errors"].append({"file": name, "note": note})
            print(f"{'ok ' if ok else 'ERR'} {name} {note}", flush=True)
    root.destroy()
    try:
        (out_root / f"shot-report-{geometry}.json").write_text(
            _json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[错误] 截图报告写不进去：{out_root}（{exc}）", flush=True)
        return 3
    if report["errors"]:
        print(f"[错误] {len(report['errors'])} 页截图失败（见上面的 ERR 行）—— 退出码 2", flush=True)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pta_assistant_gui.py", add_help=True)
    parser.add_argument("--mode", choices=[MODE_LIVE, MODE_ROLL], default=None,
                        help="直接进哪个模式（默认用上次的选择）")
    parser.add_argument("--autostart-live", action="store_true",
                        help="进「实时榜单」模式并自动启动实时服务（投屏机无人值守开机自启用）")
    parser.add_argument("--headless-shot", action="store_true",
                        help="内部用：无头截图各页面后退出")
    parser.add_argument("--shot-out", default=None,
                        help="--headless-shot 的输出目录；默认 = 程序目录下的 shots\\"
                             "（程序自己目录永远可写；本机 %%TEMP%% / tools\\ 对应用进程反而不可写）")
    parser.add_argument("--geometry", default="1200x820", help="--headless-shot 的窗口尺寸")
    parser.add_argument("--shot-mode", default=None, choices=[MODE_LIVE, MODE_ROLL],
                        help="--headless-shot 只截某个模式的页面")
    parser.add_argument("--startup-trace", action="store_true", help="内部用：量首屏耗时")
    parser.add_argument("--no-dialog", action="store_true",
                        help="自动化用：所有 messagebox 只写 logs\\gui-errors.log，不弹窗")
    parser.add_argument("--keep-open", action="store_true")
    # 内部用：Esc 退出守护。合并版以前没声明这个开关，parse_known_args 会把它当
    # 「未知参数」丢掉 → 打包后 start_esc_watchdog 用 sys.executable + --esc-watchdog
    # 拉起子进程时，子进程会照常开一个**完整界面**（用户看到「又弹出一个 PTA 比赛助手」）。
    parser.add_argument("--esc-watchdog", action="store_true", help=argparse.SUPPRESS)

    args, _unknown = parser.parse_known_args(argv)
    if args.esc_watchdog:
        return base.run_esc_watchdog()

    # 自动化入口一律静默：headless-shot（含 smoke/截图）与 --no-dialog 都压住弹窗。
    # 必须在建窗口之前生效 —— 首屏的任何告警弹窗都会卡死无人值守进程。
    if args.headless_shot or args.no_dialog:
        base._install_excepthook(cfgmod.resource_root())
        silence_dialogs(cfgmod.work_root())

    if args.headless_shot:
        # 默认输出目录 = **程序自己目录**下的 shots\（源码模式 = app\shots\，
        # 打包后 = exe 同目录）。以前默认写 app\docs\gui-frames，本机上被 ACL 挡住时
        # 会抛异常 → excepthook 弹「程序出错了」；现在既能写、失败也只落日志。
        out = (Path(args.shot_out) if args.shot_out
               else cfgmod.config_home()[0] / "shots")
        return run_headless_shot(out, args.geometry, args.shot_mode)

    trace = base.StartupTrace(bool(args.startup_trace), not args.keep_open)
    store = cfgmod.Store()
    store.load()
    trace.mark("设置读取完成")

    root = tk.Tk()
    trace.mark("Tk 初始化完成")
    try:
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        from tkinter import font as tkfont
        for font_name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            try:
                tkfont.nametofont(font_name).configure(family=UI_FONT, size=10)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    base._install_excepthook(cfgmod.resource_root())
    debug_mark(f"开始建窗口 argv={sys.argv[1:]} frozen={cfgmod.is_frozen()}")
    app = AssistantApp(root, store, trace, mode=args.mode)
    debug_mark(f"窗口建好 mode={app.mode} port={app.live_cfg.get('port')}")
    if args.autostart_live:
        app.mode = MODE_LIVE
        app._apply_mode(write=False)
        app._log("\n[启动] --autostart-live：自动启动实时服务。\n")
        debug_mark("安排自动启动实时服务")
        root.after(1200, lambda: (debug_mark("autostart 触发"), app.on_live_start()))
    if trace.enabled:
        base._arm_visibility_trace(root, trace)
    # M18：主循环异常退出（含 KeyboardInterrupt / 未捕获异常）也要把自己的子进程带走
    import atexit
    atexit.register(app._cleanup_children)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    finally:
        app._cleanup_children()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
