# -*- coding: utf-8 -*-
"""pta_board_gui.py —— PTA 滚榜助手（图形界面）。

用法
----
    python app\\pta_board_gui.py              # 打开窗口
    python app\\pta_board_gui.py --esc-watchdog   # 内部用：Esc 退出守护
    python app\\pta_board_gui.py --smoke-test     # 内部用：打包后的自检
    python app\\pta_board_gui.py --startup-trace  # 内部用：量首屏耗时

业务逻辑全部复用仓库里已有的 ``pta_fetch.py`` / ``pta_to_resolver_xml.py``
``tools\\esc_quit.py``，本文件只负责界面与流程编排。

界面设计
--------
参照成熟桌面应用（VS Code / 现代设置中心）的布局：

* 左侧深色导航栏（四个页面 + 底部环境状态），右侧浅灰底内容区；
* 内容区全部是白色卡片：卡片标题加粗 + 一行灰色描述，控件按表单网格对齐；
* 语义化配色：主按钮蓝、开始滚榜绿、成功绿 / 警告橙 / 错误红，提示统一浅灰小字，
  长说明收进悬停 tooltip（ToolTip），不再挤在控件右侧；
* 日志区是「终端」风格的可折叠面板：标题栏常驻（开关 / 最新一行 / 进度条 / 按钮），
  默认折叠，任务运行时自动展开，状态写进 config.json 的 log_open；
* 底部深色状态栏横贯全窗：左边一句话状态，右边 Resolver / Esc 守护两个带灯状态。

实现上：布局容器、卡片、文字、按钮全部用 tk 自绘（ttk 在 vista 主题下画不出
彩色背景），ttk 只保留 Combobox / Scale / Scrollbar / Treeview / Progressbar
这几个自绘不划算的控件。启动性能纪律见 README 第 9 节 —— __init__ 里不许做重活。
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

# ---- 启动计时：这行必须是本模块最早的语句，用来量「解释器就绪 -> 导入完成」----
_T_MODULE = time.perf_counter()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_backend as backend      # noqa: E402
import board_config as cfgmod        # noqa: E402
import board_fonts as fontsmod       # noqa: E402
from award_editor import AwardEditor  # noqa: E402
from rank_awards_panel import RankAwardsPanel  # noqa: E402

_T_IMPORTS = time.perf_counter()

APP_TITLE = "PTA 滚榜助手 (PTA Rolling Scoreboard)"
GEOMETRY_RE = re.compile(r"^\d+x\d+([+-]\d+[+-]\d+)?$")

GEOM_SIZE_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?$")


def usable_geometry(geom: str, screen_w: int, screen_h: int) -> "str | None":
    """判断保存下来的窗口几何还能不能用，不能用就返回 None。

    两种情况会存下「看着合法、恢复后却看不见」的值：
      1. 关窗时窗口正处于最小化状态 —— Tk 可能报出 1x1+-32000+-32000 这类坐标；
      2. 上次把窗口放在了副屏上，这次副屏没接 —— 坐标落在当前屏幕之外。
    这类几何一律拒绝，让调用方回退到默认尺寸。
    坐标允许为负（副屏可以摆在工作区左侧/上方），但不允许远超屏幕范围。
    """
    m = GEOM_SIZE_RE.match(geom or "")
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w < 600 or h < 400:          # 过小：多半是最小化残留，或用户误拖成一条缝
        return None
    if m.group(3) is None:
        return f"{w}x{h}"
    x, y = int(m.group(3)), int(m.group(4))
    if not (-screen_w <= x <= screen_w) or not (-screen_h <= y <= screen_h):
        return None
    return geom

UI_FONT = "Microsoft YaHei UI"


# ==========================================================================
# 启动计时（--startup-trace）
# ==========================================================================

def _process_start_epoch() -> float | None:
    """取本进程的创建时间（Windows GetProcessTimes）。

    有了它才能算出「双击 -> 窗口可见」的完整时间，把 PyInstaller 引导和
    Python 解释器启动也算进去，而不是只量我们自己代码的那一段。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as w

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", w.DWORD), ("dwHighDateTime", w.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = w.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            w.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ]
        creation, exit_t, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(kernel32.GetCurrentProcess(),
                                        ctypes.byref(creation), ctypes.byref(exit_t),
                                        ctypes.byref(kernel_t), ctypes.byref(user_t)):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        if not ticks:
            return None
        return ticks / 10_000_000.0 - 11644473600.0   # FILETIME -> Unix epoch
    except Exception:  # noqa: BLE001
        return None


class StartupTrace:
    """把启动各阶段耗时写进 logs\\startup-trace.txt。

    ``t0`` 用 ``_T_MODULE``（本模块第一条语句的时刻），阶段间隔用单调时钟
    ``perf_counter``；而「进程创建」是 epoch 时间，所以另存一个 epoch 起点
    ``t0_epoch`` 来算引导耗时 —— 两套时钟不能直接相减。
    """

    def __init__(self, enabled: bool, exit_after: bool) -> None:
        self.enabled = enabled
        self.exit_after = exit_after
        self.t0 = _T_MODULE
        self.t0_epoch = time.time() - (time.perf_counter() - _T_MODULE)
        self.proc_start = _process_start_epoch() if enabled else None
        self.marks: list[tuple[str, float]] = []

    def mark(self, label: str) -> None:
        if self.enabled:
            self.marks.append((label, time.perf_counter()))

    # -- 报告 --------------------------------------------------------------

    def write(self) -> str:
        if not self.enabled:
            return ""
        lines: list[str] = []
        lines.append("=" * 70)
        lines.append(f"启动计时  {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("-" * 70)
        lines.append(f"  frozen            : {cfgmod.is_frozen()}")
        lines.append(f"  executable        : {sys.executable}")
        lines.append(f"  argv              : {' '.join(sys.argv[1:])}")
        lines.append(f"  Python            : {sys.version.split()[0]} / Tk {tk.TkVersion}")
        if self.proc_start:
            lines.append(f"  进程创建时刻      : {time.strftime('%H:%M:%S', time.localtime(self.proc_start))}"
                         f".{int((self.proc_start % 1) * 1000):03d}")
        lines.append("-" * 70)

        prev = self.t0
        total = 0.0
        for label, stamp in self.marks:
            delta = (stamp - prev) * 1000.0
            total = (stamp - self.t0) * 1000.0
            lines.append(f"  {label:<28s} +{delta:8.1f} ms   (自模块开始 {total:8.1f} ms)")
            prev = stamp

        if self.marks:
            lines.append("-" * 70)
            boot = (self.t0_epoch - self.proc_start) * 1000.0 if self.proc_start else None
            if boot is not None:
                lines.append(f"  PyInstaller 引导 + 解释器启动 : {boot:8.1f} ms")
                lines.append(f"  【首屏总耗时】进程创建 -> 窗口可见 : "
                             f"{boot + total:8.1f} ms  = {(boot + total) / 1000.0:.2f} 秒")
            else:
                lines.append(f"  【首屏总耗时】模块开始 -> 窗口可见 : "
                             f"{total:8.1f} ms  = {total / 1000.0:.2f} 秒")
        # 机器可读的一行（纯 ASCII），方便脚本/CI 直接解析，不必处理中文编码
        if self.marks:
            boot = (self.t0_epoch - self.proc_start) * 1000.0 if self.proc_start else 0.0
            lines.append("")
            lines.append(f"STARTUP_BOOT_MS={boot:.1f}")
            for label, stamp in self.marks:
                lines.append(f"STARTUP_MARK_MS={label}={(stamp - self.t0) * 1000.0:.1f}")
            lines.append(f"STARTUP_TOTAL_MS={boot + total:.1f}")
        lines.append("")

        text = "\n".join(lines)
        try:
            path = backend.logs_dir() / "startup-trace.txt"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 - 打包成窗口程序后没有 stdout
            pass
        return text

#: 取数方式**只有一条**：直连接口（需要 Cookie）。2026-10-13 删除「借用浏览器」通道 ——
#: 它会在用户的浏览器里产生下载、影响观感；现在没有有效 Cookie 就直说取不到数。
CHANNEL_LINE = "取数方式：直连接口（需要 Cookie）"
CHANNEL_DESC = ("用 cookie.txt 里的登录态直接请求 PTA 接口；没有有效 Cookie 就取不到数据，"
                "程序不会去动浏览器、也不会自动回落到别的做法。")

PLAY_LABELS = {
    "先自动、后点击（前面自动滚，到设定名次改成点击）": "auto-then-click",
    "全程点击播放（每揭晓一步都等你点）": "click",
    "全程自动播放（连续揭晓，不用点）": "auto",
}
PLAY_LABEL_BY_KEY = {v: k for k, v in PLAY_LABELS.items()}


# ==========================================================================
# 内部模式：Esc 退出守护 / 打包自检
# ==========================================================================

def run_esc_watchdog() -> int:
    """--esc-watchdog：等价于直接运行 tools\\esc_quit.py。"""
    root = cfgmod.resource_root()
    sys.path.insert(0, str(root / "tools"))
    import esc_quit  # noqa: E402
    return int(esc_quit.main())


def _smoke_report(lines: list[str]) -> None:
    folder = backend.logs_dir()
    path = folder / "smoke-test.txt"
    text = "\n".join(lines) + "\n"
    try:
        path.write_text(text, encoding="utf-8")
        lines.append(f"[report] 自检报告已写入 {path}")
    except OSError as exc:
        lines.append(f"[report] 写报告失败：{exc}")
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - 打包成窗口程序后没有 stdout
        pass


def run_smoke_test(package_dir: Path | None, font_path: str | None) -> int:
    """--smoke-test：不点界面，直接走「生成比赛包 → 开始滚榜 → Esc 守护」的代码路径。

    这是为了能对打包后的 exe 做真实验证：按钮调用的就是同一批函数。
    """
    lines: list[str] = []
    ok = True

    def say(text: str = "") -> None:
        lines.append(text)

    def fail(text: str) -> None:
        nonlocal ok
        ok = False
        say(f"[FAIL] {text}")

    say("=" * 62)
    say("  PTA 滚榜助手 —— 打包产物自检 (--smoke-test)")
    say("=" * 62)
    say(f"  frozen      : {cfgmod.is_frozen()}")
    say(f"  executable  : {sys.executable}")
    say(f"  资源根目录  : {cfgmod.resource_root()}")
    say(f"  可写工作目录: {cfgmod.work_root()}")
    say(f"  设置目录    : {cfgmod.config_home()[0]}")
    say(f"  Python      : {sys.version.split()[0]}")

    # --- 1. Tk 能否起来 ---
    try:
        root = tk.Tk()
        root.withdraw()
        say(f"[OK] Tk 可用，TkVersion={tk.TkVersion}")
    except Exception as exc:  # noqa: BLE001
        fail(f"Tk 无法启动：{exc}")
        _smoke_report(lines)
        return 1

    # --- 2. 配置读写 ---
    try:
        store = cfgmod.Store()
        notes = store.load()
        say(f"[OK] 读到 config.json：{store.config_path}"
            f"（题目集 {store.config['problem_set_id']}，字体 {store.config['font_path'] or '未设置'}）")
        for note in notes:
            say(f"     note: {note.splitlines()[0]}")
        store.config["last_tab"] = store.config.get("last_tab", 0)
        errs = store.save()
        if errs:
            fail(f"config.json 写入失败：{errs[0]}")
        else:
            say(f"[OK] 写回 config.json 成功")
    except Exception as exc:  # noqa: BLE001
        fail(f"配置读写异常：{exc}")
        store = cfgmod.Store()

    # --- 3. 字体 ---
    choices = fontsmod.scan(cfgmod.resource_root())
    say(f"[OK] 扫描到 {len(choices)} 个字体文件")
    chosen = font_path or store.config.get("font_path") or fontsmod.default_choice(choices)
    if not chosen or not Path(chosen).is_file():
        fail(f"没有可用的中文字体（当前：{chosen!r}）")
    else:
        probe = fontsmod.probe_glyphs(chosen)
        say(f"[i] 字体探测：{fontsmod.describe_probe(probe, Path(chosen).name)}")
        if probe.get("missing"):
            fail("所选字体缺字，滚榜时会出现方框")
        else:
            ok_reg, why = fontsmod.register_private(chosen)
            say(f"[{'OK' if ok_reg else 'i'}] 临时加载字体到本进程：{why or '成功'}")
        store.config["font_path"] = str(chosen or "")
        errs = store.save()
        if errs:
            say(f"[i] 把选中的字体写回 config.json 失败：{errs[0]}")

    # --- 4. 环境检查 ---
    for check in backend.check_environment(cfgmod.resource_root()):
        prefix = "[OK]  " if check.ok else "[缺失]"
        say(f"{prefix} {check.name}：{check.detail.splitlines()[0]}")
        if not check.ok and check.fix:
            say(f"       -> {check.fix}")
            if check.name in ("Java", "ICPC Resolver", "后端脚本", "中文字体"):
                ok = False

    root.destroy()

    # --- 5. 生成比赛包 + 启动 Resolver ---
    if package_dir is None:
        say("[i] 没有传 --package，跳过滚榜启动测试。")
        _smoke_report(lines)
        return 0 if ok else 1

    sink = backend.LogSink(queue.Queue())
    package_dir = Path(package_dir)
    if not (package_dir / "events.xml").is_file():
        contest = cfgmod.work_root() / "contest.json"
        if not contest.is_file():
            fail(f"既没有 {package_dir / 'events.xml'} 也没有 {contest}，无法测试滚榜。")
            _smoke_report(lines)
            return 1
        res = backend.run_package(cfgmod.resource_root(), contest, package_dir, 60, 20, "1 2 3", sink)
        say(f"[{'OK' if res['ok'] else 'FAIL'}] 生成比赛包：{res['message'].splitlines()[0]}")
        if not res["ok"]:
            ok = False
    else:
        say(f"[OK] 复用已有比赛包 {package_dir / 'events.xml'}")

    res = backend.start_resolver(
        cfgmod.resource_root(), package_dir, chosen,
        "step", 1.0, store.config.get("display_name") or "{team.name}", "", sink,
    )
    if not res["ok"]:
        fail(f"启动 Resolver 失败：{res['message']}")
        _smoke_report(lines)
        return 1
    proc = res["process"]
    say(f"[OK] Resolver 已启动 PID={proc.pid}")
    say(f"     ICPC_FONT={chosen}")
    say(f"     日志={res['log']}")

    wins = backend.wait_for_resolver_window(proc, timeout=45.0, sink=sink)
    if not wins:
        fail("45 秒内没有看到 Resolver 窗口")
        backend.stop_resolver(proc, sink, grace=3.0)
        _smoke_report(lines)
        return 1
    say(f"[OK] Resolver 窗口已出现：{wins}")

    wd = backend.start_esc_watchdog(cfgmod.resource_root() / "tools" / "esc_quit.py",
                                    proc.pid if proc is not None else None)
    if wd["ok"]:
        say(f"[OK] Esc 退出守护已拉起 PID={wd['process'].pid}（锁定 Resolver PID="
            f"{getattr(proc, 'pid', '?')}）")
    else:
        fail(f"Esc 退出守护失败：{wd['message']}")

    say("[i] 保持 12 秒，供外部截图验证中文显示 …")
    time.sleep(12)

    stopped = backend.stop_resolver(proc, sink, grace=5.0)
    say(f"[OK] 停止滚榜：{stopped}")
    if wd["ok"]:
        backend.stop_process(wd["process"])

    say("")
    say("=" * 62)
    say(f"  自检结论：{'全部通过' if ok else '有失败项，见上面的 [FAIL]'}")
    say("=" * 62)
    _smoke_report(lines)
    return 0 if ok else 1


# ==========================================================================
# 悬停提示
# ==========================================================================

class ToolTip:
    """轻量悬停提示：悬停约 0.5 秒后在控件下方弹出，移开或点击即消失。

    用途是把原先挤在控件右侧的长括号注释收起来：行内只留一句话的小字，
    细节放进 tooltip。Windows 经典黄底样式，不抢焦点。
    """

    def __init__(self, widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: "tk.Toplevel | None" = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._job = self.widget.after(500, self._show)

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:  # noqa: BLE001
                pass
            self._job = None

    def _show(self) -> None:
        self._job = None
        if self.tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:  # noqa: BLE001
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        frame = tk.Frame(tip, background="#ffffe1",
                         highlightbackground="#9c9c9c", highlightthickness=1)
        frame.pack()
        tk.Label(frame, text=self.text, background="#ffffe1", foreground="#222222",
                 font=(UI_FONT, 9), justify="left", wraplength=340,
                 padx=8, pady=5).pack()
        self.tip = tip

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.tip = None


# ==========================================================================
# 主界面
# ==========================================================================

class _NavAdapter:
    """给 BoardApp 的侧栏导航提供 ttk.Notebook 的 select/index 最小接口。

    这样各处理函数里的 ``self.nb.select(self.tab_roll)`` /
    ``self.nb.index("end")`` 这些调用不用跟着布局改动。
    """

    def __init__(self, app: "BoardApp") -> None:
        self.app = app

    def select(self, tab=None):
        return self.app._nav_select(tab)

    def index(self, what) -> int:
        return self.app._nav_index(what)


class BoardApp:
    """主窗口。布局：

    +--------+---------------------------+
    | 侧栏   | 页面头部（标题 + 一句说明）|
    | 导航   | 白色卡片群                 |
    |        | 可折叠终端日志（标题栏常驻）|
    +--------+---------------------------+
    | 深色状态栏（左：状态；右：进程灯）   |
    +-------------------------------------+
    """

    # -- 配色（一套温和的 slate 系，参照成熟桌面应用） ----------------------
    C = {
        "bg": "#f1f4f8",          # 内容区底色
        "card": "#ffffff",
        "border": "#e3e8ef",
        "side": "#1f2a3d",        # 侧栏底
        "side_hover": "#2b3a52",
        "side_text": "#9fb0c7",
        "side_dim": "#8fa3bd",
        "accent": "#2563eb",      # 主按钮
        "accent_hover": "#1d4ed8",
        "go": "#16a34a",          # 开始滚榜（保留绿色大按钮的思路）
        "go_hover": "#15803d",
        "ink": "#16202e",         # 主文字
        "sub": "#4b5b70",         # 次文字
        "hint": "#64748b",        # 提示灰（原来是 #8595a9，白底对比度只有 2.6:1，看不清）
        "ok": "#16a34a",
        "err": "#dc2626",
        "warn": "#b45309",
        "warn_bg": "#fffaeb",
        "warn_border": "#f3dfa0",
        "term": "#101b2c",        # 日志终端底
        "term_fg": "#c9d6e4",
        "status": "#1f2a3d",      # 状态栏底（同侧栏）
        "lamp_on": "#4ade80",
        "lamp_off": "#5b6b82",
    }

    # 就地反馈文字颜色
    KIND = {"hint": C["hint"], "sub": C["sub"], "ok": C["ok"],
            "err": C["err"], "warn": C["warn"]}

    # 状态药丸（字体状态用）：kind -> (底色, 字色)
    PILL = {"ok": ("#dcf2e3", "#166534"),
            "warn": ("#fdf0d3", "#92400e"),
            "err": ("#fbdedd", "#b3261e"),
            "hint": ("#edf1f6", "#4b5b70")}

    LABEL_W = 15          # 表单行左侧标签统一宽度（字符）

    PAGES = (
        ("contest", "比赛", "第一步：填题目集 ID，把比赛数据拉到本地"),
        ("display", "播放与显示", "播放方式、奖项表与全队标记（每队勾选）、中文字体"),
        ("roll", "滚榜", "第二步：生成比赛包  →  第三步：开始滚榜"),
        ("history", "历史", "抓过的比赛都在这，双击一行一键载入"),
    )

    def __init__(self, root, store: cfgmod.Store, trace: "StartupTrace | None" = None) -> None:
        self.root = root
        self.store = store
        self.cfg = store.config
        self.trace = trace or StartupTrace(False, False)
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.sink = backend.LogSink(self.log_queue)

        #: M17：奖项表编辑器（``"roll"`` = 滚榜那份主表，``"live"`` = 实时榜单那份）。
        #: 两个实例共用 ``cfg["awards"]`` 这一份数据：任一侧改动都会写回它并把另一侧重画。
        self.award_editors: "dict[str, AwardEditor]" = {}
        #: 双向同步的重入闸（另一侧重画时不要再触发一次同步）
        self._awards_syncing = False

        self.resolver_proc = None
        self.watchdog_proc = None
        self.busy = False
        #: 「停止滚榜」必须在任何阶段都能生效（含正在启动的那几秒）。
        self._stop_requested = False
        self._stopping = False
        self.font_choices: list[dict] = []
        self.font_label_to_path: dict[str, str] = {}
        self.history_rows: list[str] = []
        self.log_open = bool(int(self.cfg.get("log_open") or 0))
        self._banner_shown = False
        self._nav_current = -1
        self._nav_widgets: list[tuple] = []
        self._page_frames: list[tk.Frame] = []
        #: 需要按可用宽度自适应换行的提示标签（见 _hint）
        self._wrap_labels: list[tk.Label] = []
        self._page_holders: list[tk.Frame] = []
        self._page_canvases: list[tk.Canvas] = []

        self._build_window()
        self.trace.mark("界面控件构建完成")
        self._load_fonts(initial=True)
        self.trace.mark("字体下拉框装填完成")
        self._apply_config()
        self.trace.mark("设置回填完成")
        self._refresh_history()
        self.trace.mark("历史列表装填完成")
        self._pump()
        self._tick_process_state()
        if not self.store.config_path.exists():
            # 第一次运行就把 config.json 落盘，用户能直接看到、也能手工改
            self._persist()
        self.trace.mark("首次落盘/收尾完成")
        self.root.after(300, self._startup_checks)

    # ------------------------------------------------------------ 基础件

    def _f(self, parent, bg=None, **kw) -> tk.Frame:
        return tk.Frame(parent, bg=bg or self.C["card"], **kw)

    def _t(self, parent, text="", size=10, bold=False, fg=None, bg=None, **kw) -> tk.Label:
        """文字标签：默认白底次文字色。"""
        font = (UI_FONT, size, "bold") if bold else (UI_FONT, size)
        return tk.Label(parent, text=text, font=font,
                        fg=fg or self.C["sub"], bg=bg or self.C["card"], **kw)

    def _msg(self, label: tk.Label, text: str, kind: str = "hint") -> None:
        """就地反馈：换文字 + 换语义色（hint/ok/err/warn/sub）。"""
        label.configure(text=text, fg=self.KIND[kind])

    def _set_pill(self, label: tk.Label, text: str, kind: str) -> None:
        bg, fg = self.PILL[kind]
        label.configure(text=text, bg=bg, fg=fg)

    def _pill(self, parent, text="", kind="hint") -> tk.Label:
        bg, fg = self.PILL[kind]
        return tk.Label(parent, text=text, bg=bg, fg=fg,
                        font=(UI_FONT, 9, "bold"), padx=10, pady=2)

    @staticmethod
    def _bind_hover(btn: tk.Button, normal_bg: str, hover_bg: str) -> None:
        """tk 按钮没有原生悬停态，用 Enter/Leave 模拟；禁用时不换色。"""
        def on_enter(_e):
            if str(btn["state"]) != "disabled":
                btn.configure(background=hover_bg)

        def on_leave(_e):
            btn.configure(background=normal_bg)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)

    def _solid_btn(self, parent, text: str, command, bg: str, hover: str,
                   fg: str = "white", font=None, padx: int = 18,
                   pady: int = 6) -> tk.Button:
        """实心彩色按钮：用于页面主操作（拉取数据 / 开始滚榜）。"""
        btn = tk.Button(parent, text=text, command=command,
                        font=font or (UI_FONT, 10, "bold"),
                        bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                        relief="flat", bd=0, padx=padx, pady=pady,
                        cursor="hand2", disabledforeground="#d5dae1")
        self._bind_hover(btn, bg, hover)
        return btn

    def _ghost_btn(self, parent, text: str, command, bg: str = "") -> tk.Button:
        """描边浅底按钮：用于次要操作（预览 / 清空 / 选择…）。"""
        bg = bg or self.C["card"]
        hover = "#eef2f7"
        btn = tk.Button(parent, text=text, command=command, font=(UI_FONT, 9),
                        bg=bg, fg=self.C["sub"],
                        activebackground=hover, activeforeground=self.C["ink"],
                        relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
                        highlightthickness=1, highlightbackground=self.C["border"],
                        highlightcolor=self.C["accent"], disabledforeground="#c3cad4")
        self._bind_hover(btn, bg, hover)
        return btn

    def _entry(self, parent, var, width: int) -> tk.Entry:
        return tk.Entry(parent, textvariable=var, width=width, font=(UI_FONT, 10),
                        relief="flat", bd=0, bg="white", fg=self.C["ink"],
                        insertbackground=self.C["ink"],
                        highlightthickness=1, highlightbackground="#c8d2de",
                        highlightcolor=self.C["accent"])

    def _form_row(self, parent, label: str, pady=(4, 4)) -> tk.Frame:
        """表单行：左侧固定宽度标签 + 返回放控件的行容器（保证各卡片对齐）。"""
        row = self._f(parent)
        row.pack(fill="x", pady=pady)
        self._t(row, label, fg=self.C["sub"], width=self.LABEL_W, anchor="w").pack(side="left")
        return row

    def _indent_row(self, parent, pady=(0, 4)) -> tk.Frame:
        """与 _form_row 的控件列对齐的次级行（放提示 / 错误小字）。"""
        row = self._f(parent)
        row.pack(fill="x", pady=pady)
        self._t(row, "", width=self.LABEL_W).pack(side="left")
        return row

    def _hint(self, parent, text: str = "", size: int = 9, fg=None,
              wrap: int | None = None) -> tk.Label:
        """跟着可用宽度自动换行的提示文字（替代写死的 wraplength）。

        写死 `wraplength=720` 的坑（见 docs/verification-log.txt 附录 K.11）：
        卡片里的提示是「15 字符缩进 + 正文」，提示本身再要 720px，
        窄窗口下整页的**请求宽度**就超过了 Canvas 可视宽度；
        Canvas 于是有了横向滚动余量，某个 Entry 拿到焦点时 Tk 会横向平移去露出它，
        结果整页左移 —— 提示文字开头被推出可视区（用户截图里的「空。文件名优先…」）。

        所以这里不再写死宽度：标签用 fill="x" 吃掉缩进后的剩余宽度，
        每次它自己 <Configure> 就把 wraplength 调成实际宽度 − 6。

        ``wrap`` 给定时改用**固定换行宽度**（不参与自动收敛）：用于那些
        「跟着内容一起变宽、自我维持」的提示（奖项表那一行的说明就是这样，
        它会把整页请求宽度顶到 900x600 的极限）。固定值只会让布局变窄。
        """
        if wrap:
            label = self._t(parent, text, size=size, fg=fg or self.C["hint"],
                            justify="left", anchor="w", wraplength=int(wrap))
            label.pack(side="left", fill="x", expand=False)
            return label
        label = self._t(parent, text, size=size, fg=fg or self.C["hint"],
                        justify="left", anchor="w", wraplength=320)
        label.pack(side="left", fill="x", expand=True)
        # fill="x" 的标签宽度来自布局，拿它自己当基准是稳定的
        return self._auto_wrap(label, source=label)

    def _auto_wrap(self, label: tk.Label, source=None, min_width: int = 160) -> tk.Label:
        """登记一个「跟着可用宽度换行」的标签（不改动它的布局）。

        窄窗口下「写死宽度」和「完全不设 wraplength」是同一个坑的两面：
        前者让整页请求宽度超过 Canvas（→ 横向平移、左边被推出可视区），
        后者让长句直接冲出卡片右边缘。

        **实现要点（踩过坑）**：这里**只登记**，不在标签自己的 <Configure> 里
        回调去改 wraplength。早期版本是每个标签各自绑定回调、一有 <Configure>
        就 configure(wraplength=…)；configure 又会引发新的 <Configure>，
        几百个标签互相触发，900x600 下 `update_idletasks()` **永不返回**
        （进程 100% CPU，实测卡死）。
        现在统一由 ``_schedule_fit()`` 去抖（180ms）后跑一趟（见 `_run_fit`）。
        """
        base = source if source is not None else label.master
        self._wrap_labels.append((label, base, min_width))
        return label

    def _schedule_fit(self, delay: int = 180) -> None:
        """安排一次「重算所有提示换行宽度」；多次调用会被合并成一次。"""
        if getattr(self, "_fit_job", None):
            return
        try:
            self._fit_job = self.root.after(delay, self._run_fit)
        except Exception:  # noqa: BLE001 - 窗口正在销毁
            self._fit_job = None

    def _run_fit(self) -> None:
        """真正重算换行宽度：去抖之后跑，且**变化小于 16px 就不动**。

        换行宽度的基准是**内容区宽度**（``self.content``），不是标签自己父容器的
        实际宽度 —— 后者会形成环：标签请求宽度 → 父容器请求宽度 → 换行宽度 →
        标签请求宽度，窄窗口下永远收敛不到「放得下」（实测卡死过一次）。
        内容区宽度只由窗口大小决定，与标签内容无关，天然没有环。

        每个标签再取 ``min(父容器宽度 - 6, 预算)``：侧栏里的提示（父容器只有
        190px）不会被按内容区宽度撑破。
        """
        self._fit_job = None
        if getattr(self, "_wrap_busy", False):
            return
        self._wrap_busy = True
        try:
            try:
                avail = int(self.content.winfo_width())
            except Exception:  # noqa: BLE001
                avail = 0
            if avail <= 1:
                try:
                    avail = int(self.root.winfo_width()) - 220
                except Exception:  # noqa: BLE001
                    avail = 700
            # 132 = 卡片左右内边距(24*2) + 卡片正文内边距(18*2) + 表单左栏(15 字宽)
            budget = max(200, avail - 132)
            for item in list(getattr(self, "_wrap_labels", [])):
                try:
                    lbl, base, min_width = item
                except (TypeError, ValueError):
                    continue
                cap = budget
                try:
                    base_width = int(base.winfo_width())
                    if base_width > 1:
                        cap = min(cap, base_width - 6)
                except Exception:  # noqa: BLE001
                    pass
                new = max(int(min_width), cap)
                try:
                    if abs(new - int(lbl.cget("wraplength"))) < 16:
                        continue
                    lbl.configure(wraplength=new)
                except Exception:  # noqa: BLE001
                    pass
            hook = getattr(self, "_on_budget_changed", None)
            if hook is not None:
                try:
                    hook(budget)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._wrap_busy = False

    def _fit_all_hints(self) -> None:
        """兼容旧调用点：现在只是安排一次去抖后的重算（不再同步 configure）。"""
        self._schedule_fit()

    def _card(self, parent, title: str, desc: str = "", expand: bool = False) -> tk.Frame:
        """白色卡片：标题 + 可选描述，返回放正文的 body 框架。

        描述文字**必须**给个 wraplength 初值（这里 300）：Tk 里并排 pack 的两个标签
        请求宽度是**相加**的，描述不给换行宽度的话，卡片请求宽度就会等于
        「标题 + 整句描述」那么长，窗口一窄整页就被撑出可视区、右边被裁掉
        （900x600 实测过）。给了初值再交给 _auto_wrap 跟着实际宽度走。
        """
        card = self._f(parent, highlightbackground=self.C["border"], highlightthickness=1, bd=0)
        card.pack(fill="both" if expand else "x", expand=expand, padx=24, pady=(10, 0))
        head = self._f(card)
        # 内边距 14（原来是 18）：900x600 下整页请求宽度全都要压进画布可视宽度
        # （overflow ≤ 0 且留 ≥4px 余量，见 docs/ui-geometry-check.json）。
        head.pack(fill="x", padx=14, pady=(10, 0))
        self._t(head, title, size=11, bold=True, fg=self.C["ink"], anchor="w").pack(side="left")
        if desc:
            # 描述文字用**固定**换行宽度：不能用「跟着卡片实际宽度走」的动态方案 ——
            # 卡片宽度依赖标题+描述的请求宽度，描述又依赖卡片宽度，是个环，
            # 窄窗口下会稳定在「比可视宽度宽几十像素」的状态（右边被裁掉一截）。
            # 258px 是按最窄支持窗口（900x600）算的安全值：标题+描述+内边距 < 可用宽度。
            desc_label = self._t(head, desc, size=9, fg=self.C["hint"], anchor="w",
                                 justify="left", wraplength=258)
            desc_label.pack(side="left", fill="x", expand=True, padx=(12, 0), pady=(2, 0))
        body = self._f(card)
        body.pack(fill="both", expand=True, padx=14, pady=(5, 12))
        return body

    def _page_header(self, page: tk.Frame, idx: int) -> None:
        _key, title, sub = self.PAGES[idx]
        head = self._f(page, bg=self.C["bg"])
        head.pack(fill="x", padx=24, pady=(12, 0))
        self._t(head, title, size=15, bold=True, fg=self.C["ink"],
                bg=self.C["bg"], anchor="w").pack(anchor="w")
        self._t(head, sub, size=9, fg=self.C["hint"], bg=self.C["bg"],
                anchor="w").pack(anchor="w", pady=(3, 0))

    # ---------------------------------------------------------------- 窗口

    def _build_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.minsize(980, 640)
        saved = usable_geometry(str(self.cfg.get("window_geometry") or ""),
                                self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        # 存下来的几何不可用（副屏拔掉 / 最小化残留）时回退默认尺寸，
        # 否则窗口会出现在屏幕外，看起来像「程序打不开」。
        self.root.geometry(saved or "1080x780")
        self.root.configure(bg=self.C["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_styles()
        self._build_status_bar()          # 先 pack 底部状态栏（横贯全窗）

        main_row = self._f(self.root, bg=self.C["bg"])
        main_row.pack(fill="both", expand=True)

        self._build_sidebar(main_row)

        right = self._f(main_row, bg=self.C["bg"])
        right.pack(side="left", fill="both", expand=True)

        self._build_log_pane(right)       # 先 pack 底部日志区

        self.content = self._f(right, bg=self.C["bg"])
        self.content.pack(fill="both", expand=True)

        # 四个页面
        self.tab_contest = self._page_frame(0)
        self.tab_play = self._page_frame(1)
        self.tab_roll = self._page_frame(2)
        self.tab_history = self._page_frame(3)
        self._build_contest_tab()
        self._build_play_tab()
        self._build_roll_tab()
        self._build_history_tab()

        self.nb = _NavAdapter(self)
        self._nav_select(0)

    def _build_styles(self) -> None:
        """ttk 只保留 Combobox / Scale / Scrollbar / Treeview / Progressbar。"""
        style = ttk.Style(self.root)
        style.configure("Treeview", background="white", fieldbackground="white",
                        rowheight=28, font=(UI_FONT, 9), borderwidth=0)
        style.configure("Treeview.Heading", font=(UI_FONT, 9, "bold"))
        style.configure("Horizontal.TProgressbar", thickness=8)
        style.configure("Horizontal.TScale", background="white")

    # ------------------------------------------------------------ 侧栏导航

    def _build_sidebar(self, parent) -> None:
        side = self._f(parent, bg=self.C["side"], width=190)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        brand = self._f(side, bg=self.C["side"])
        brand.pack(fill="x", padx=18, pady=(20, 14))
        self._t(brand, "PTA 滚榜助手", size=13, bold=True, fg="white",
                bg=self.C["side"], anchor="w").pack(anchor="w")
        self._t(brand, "Rolling Scoreboard", size=8, fg=self.C["side_dim"],
                bg=self.C["side"], anchor="w").pack(anchor="w", pady=(2, 0))

        nav = self._f(side, bg=self.C["side"])
        nav.pack(fill="x", pady=(4, 0))
        self._nav_box = nav
        self._build_nav_items()

        # 底部：环境状态（_startup_checks 跑完后刷新）
        bottom = self._f(side, bg=self.C["side"])
        bottom.pack(side="bottom", fill="x", padx=16, pady=14)
        row = self._f(bottom, bg=self.C["side"])
        row.pack(anchor="w")
        self.env_dot = self._t(row, "●", size=9, fg="#d9a72e", bg=self.C["side"])
        self.env_dot.pack(side="left")
        self.env_text = self._t(row, " 正在检查环境 …", size=9, fg=self.C["side_text"],
                                bg=self.C["side"], anchor="w")
        self.env_text.pack(side="left", padx=(2, 0))

    def _build_nav_items(self) -> None:
        for i, (_key, title, _sub) in enumerate(self.PAGES):
            item = self._f(self._nav_box, bg=self.C["side"], cursor="hand2")
            item.pack(fill="x", pady=1)
            bar = tk.Frame(item, bg=self.C["side"], width=3)
            bar.pack(side="left", fill="y")
            chip = tk.Label(item, text=str(i + 1), width=2,
                            font=(UI_FONT, 9, "bold"),
                            bg="#31405a", fg=self.C["side_text"])
            chip.pack(side="left", padx=(13, 10), pady=9)
            lbl = self._t(item, title, size=11, fg=self.C["side_text"],
                          bg=self.C["side"], anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            widgets = (item, bar, chip, lbl)
            for w in (item, chip, lbl):
                w.bind("<Button-1>", lambda _e, idx=i: self._nav_select(idx))
                w.bind("<Enter>", lambda _e, idx=i: self._nav_hover(idx, True))
                w.bind("<Leave>", lambda _e, idx=i: self._nav_hover(idx, False))
            self._nav_widgets.append(widgets)

    def _nav_hover(self, idx: int, on: bool) -> None:
        if idx == self._nav_current:
            return
        item, bar, chip, lbl = self._nav_widgets[idx]
        bg = self.C["side_hover"] if on else self.C["side"]
        item.configure(bg=bg)
        bar.configure(bg=bg)
        chip.configure(bg="#31405a" if not on else "#3a4c6b")
        lbl.configure(bg=bg)

    def _nav_paint(self) -> None:
        for i, (item, bar, chip, lbl) in enumerate(self._nav_widgets):
            if i == self._nav_current:
                item.configure(bg=self.C["side_hover"])
                bar.configure(bg=self.C["accent"])
                chip.configure(bg=self.C["accent"], fg="white")
                lbl.configure(bg=self.C["side_hover"], fg="white")
            else:
                item.configure(bg=self.C["side"])
                bar.configure(bg=self.C["side"])
                chip.configure(bg="#31405a", fg=self.C["side_text"])
                lbl.configure(bg=self.C["side"], fg=self.C["side_text"])

    def _page_frame(self, idx: int) -> tk.Frame:
        """每个页面套一层 Canvas + 纵向滚动条。

        窗口不够高时（用户实测 1080x780 或更小就看不全），可以滚轮/拖滚动条
        一直滚到底 —— 之前「奖项设置」最下面那行说明会被下沿截掉。
        底部「终端日志」是独立的 Text，有自己的滚动，不受影响。
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
            """scrollregion 只用「canvas 可视宽度 + 内容高度」——绝不给横向滚动留余量。

            以前是 canvas.bbox("all")：内层窗口还没被夹到 canvas 宽度时，
            bbox 会带上页面的**请求宽度**（实测 900x600 下 page.reqwidth=921 > canvas 873），
            scrollregion 就宽了 48px，Tk 一有机会就横向平移整页（见 _hint 的注释）。
            """
            try:
                w = canvas.winfo_width()
                h = max(page.winfo_reqheight(), canvas.winfo_height())
                canvas.configure(scrollregion=(0, 0, w, h))
                canvas.xview_moveto(0)
            except Exception:  # noqa: BLE001
                pass

        def _on_page_config(_e=None):
            _clamp()
            self._fit_all_hints()

        def _on_canvas_config(e):
            canvas.itemconfigure(window_id, width=e.width)
            _clamp()

        page.bind("<Configure>", _on_page_config)
        canvas.bind("<Configure>", _on_canvas_config)
        # 焦点落在页面里的 Entry 上时，Tk 可能试图滚动祖先 Canvas 去露出它；
        # 上面已经把横向余量清成 0，这里再兜一次底，确保 xview 回到最左。
        canvas.bind("<FocusIn>", lambda _e: canvas.xview_moveto(0))
        page.bind("<FocusIn>", lambda _e: canvas.xview_moveto(0), add="+")
        # 鼠标进入本页才接管滚轮，离开就还回去（否则会抢走日志区的滚轮）
        canvas.bind("<Enter>", lambda _e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        self._page_frames.append(page)
        self._page_header(page, idx)
        return page

    def _nav_select(self, tab=None):
        """tab 可以是页面序号或页面框架；无参时返回当前页面框架。"""
        if tab is None:
            return self._page_frames[self._nav_current]
        if isinstance(tab, int):
            idx = tab
        else:
            idx = self._page_frames.index(tab)
        if idx != self._nav_current:
            for holder in self._page_holders:
                holder.pack_forget()
            self._page_holders[idx].pack(fill="both", expand=True)
            self._nav_current = idx
            self._nav_paint()

        def _reset_scroll():
            try:
                self._page_canvases[idx].yview_moveto(0)
            except Exception:  # noqa: BLE001
                pass

        self.root.after(10, _reset_scroll)
        return self._page_frames[idx]

    def _nav_index(self, what) -> int:
        if what == "end":
            return len(self._page_frames)
        return self._page_frames.index(what)

    # ------------------------------------------------------------ 日志区（可折叠）

    def _build_log_pane(self, parent) -> None:
        card = self._f(parent, highlightbackground=self.C["border"], highlightthickness=1, bd=0)
        card.pack(fill="x", side="bottom", padx=24, pady=(10, 10))

        header = self._f(card)
        header.pack(fill="x", padx=10, pady=7)

        self.log_toggle_btn = self._ghost_btn(header, "", self._toggle_log)
        self.log_toggle_btn.pack(side="left")
        self.log_preview_var = tk.StringVar(value="")
        tk.Label(header, textvariable=self.log_preview_var, anchor="w",
                 font=(UI_FONT, 9), fg=self.C["hint"], bg=self.C["card"]).pack(
                     side="left", fill="x", expand=True, padx=(10, 10))

        self._ghost_btn(header, "清空", self.clear_log).pack(side="right", padx=(6, 0))
        self._ghost_btn(header, "日志文件夹",
                        lambda: self._open_folder(backend.logs_dir())).pack(side="right")
        self.progress = ttk.Progressbar(header, mode="indeterminate", length=140)
        self.progress.pack(side="right", padx=(0, 10))
        self.progress.pack_forget()       # 空闲时不占位，_busy_on 时才出现

        self.log_body = self._f(card)
        self.log_text = tk.Text(self.log_body, wrap="word", height=7, undo=False,
                                font=("Consolas", 9), bg=self.C["term"],
                                fg=self.C["term_fg"], insertbackground=self.C["term_fg"],
                                relief="flat", bd=0, padx=10, pady=6,
                                selectbackground="#31405a")
        scroll = ttk.Scrollbar(self.log_body, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(state="disabled")

        self._sync_log_pane()

    def _sync_log_pane(self) -> None:
        if self.log_open:
            self.log_body.pack(fill="x", padx=10, pady=(0, 10))
            self.log_toggle_btn.configure(text="▾  终端日志")
        else:
            self.log_body.pack_forget()
            self.log_toggle_btn.configure(text="▸  终端日志")

    def _toggle_log(self) -> None:
        self.log_open = not self.log_open
        self._sync_log_pane()

    # ------------------------------------------------------------ 状态栏

    def _build_status_bar(self) -> None:
        bar = self._f(self.root, bg=self.C["status"], height=30)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self.status_var = tk.StringVar(value="就绪")
        tk.Label(bar, textvariable=self.status_var, anchor="w",
                 font=(UI_FONT, 9), fg="#dbe4ef", bg=self.C["status"]).pack(
                     side="left", padx=12, pady=5)

        right = self._f(bar, bg=self.C["status"])
        right.pack(side="right", padx=12)
        self.proc_resolver_var = tk.StringVar(value="Resolver：未运行")
        self.proc_watchdog_var = tk.StringVar(value="Esc 守护：未运行")
        self.resolver_dot = self._t(right, "●", size=9, fg=self.C["lamp_off"],
                                    bg=self.C["status"])
        self.resolver_dot.pack(side="left")
        tk.Label(right, textvariable=self.proc_resolver_var, font=(UI_FONT, 9),
                 fg=self.C["side_text"], bg=self.C["status"]).pack(side="left", padx=(3, 12))
        self.watchdog_dot = self._t(right, "●", size=9, fg=self.C["lamp_off"],
                                    bg=self.C["status"])
        self.watchdog_dot.pack(side="left")
        tk.Label(right, textvariable=self.proc_watchdog_var, font=(UI_FONT, 9),
                 fg=self.C["side_text"], bg=self.C["status"]).pack(side="left", padx=(3, 0))

    # ------------------------------------------------------------ 比赛页

    def _build_contest_tab(self) -> None:
        page = self.tab_contest

        # M13（4d）：原来这里有一条「还没有设置中文字体」的橙色警示条（带
        # 「去设置字体 →」按钮）。字体已改为程序内置、后端自动回落，用户没有
        # 任何要设置的东西，所以整条警示条已删除。

        # -- 卡片：拉取比赛数据 ---------------------------------------------
        body = self._card(page, "第一步：把这场比赛的数据拉到本地",
                          "题目集 ID = PTA 比赛网址里 /problem-sets/ 后面那串数字")
        self._contest_first_card = body.master

        row = self._form_row(body, "题目集 ID", pady=(4, 2))
        self.psid_var = tk.StringVar()
        self.psid_entry = self._entry(row, self.psid_var, 24)
        self.psid_entry.pack(side="left")
        self.psid_entry.bind("<KeyRelease>", lambda _e: self._psid_msg())
        ToolTip(self.psid_entry,
                "要比哪一场比赛，就填哪一场的题目集 ID。\n"
                "在浏览器打开这场比赛，地址栏里 /problem-sets/ 后面那串数字就是它，\n"
                "例如 2107050785530253312。\n"
                "不知道 ID？到「历史」页看看以前抓过的比赛，双击一行就能载入。")
        self.fetch_btn = self._solid_btn(row, "拉取数据", self.on_fetch,
                                         bg=self.C["accent"], hover=self.C["accent_hover"])
        self.fetch_btn.pack(side="left", padx=(10, 8))
        ToolTip(self.fetch_btn,
                "把这场比赛的题目、队伍、提交记录下载到本机。\n"
                "第一次会久一点（几分钟），之后是增量更新，很快。\n"
                "下载完「抓取结果」里会写清楚抓到多少题、多少队、多少提交。")
        self.check_btn = self._ghost_btn(row, "只检查能不能读",
                                         lambda: self.on_fetch(check_only=True))
        self.check_btn.pack(side="left")
        ToolTip(self.check_btn,
                "不确定 Cookie 还有效吗，先点这个：\n"
                "它不下载、不写任何文件，只确认接口能读、账号有权限。")
        self.fetch_state = self._t(row, "", size=9, fg=self.C["hint"])
        self.fetch_state.pack(side="left", padx=12)

        hint_row = self._indent_row(body)
        self.psid_hint = self._t(hint_row,
                                 "还没填。点上面输入框填题目集 ID，再点右边的「拉取数据」。",
                                 size=9, fg=self.C["hint"])
        self.psid_hint.pack(side="left")

        row2 = self._form_row(body, "取数方式", pady=(6, 2))
        self._t(row2, CHANNEL_LINE, size=10, bold=True, fg=self.C["ink"]).pack(side="left")
        hint2 = self._indent_row(body)
        self._hint(hint2, CHANNEL_DESC, size=9).pack(side="left", fill="x", expand=True)

        # -- 卡片：抓取结果 --------------------------------------------------
        result_body = self._card(page, "抓取结果", expand=True)
        self.summary_var = tk.StringVar(
            value="还没有抓取过数据。\n\n怎么开始：① 在上面填题目集 ID　② 点「拉取数据」\n"
                  "拉完这里会显示抓到多少题、多少队伍、多少提交记录。")
        self.summary_label = tk.Label(result_body, textvariable=self.summary_var, justify="left",
                                      anchor="nw", bg="white", fg=self.C["sub"],
                                      font=(UI_FONT, 10), wraplength=320)
        self.summary_label.pack(fill="both", expand=True)
        self._auto_wrap(self.summary_label, source=self.summary_label, min_width=200)

    def _psid_msg(self, text: str | None = None, error: bool = False) -> None:
        """题目集 ID 输入框下方的就地反馈：默认灰字提示，出错时换成红字。"""
        if text is None:
            if str(self.psid_var.get()).strip():
                self._msg(self.psid_hint,
                          "ID 看起来没问题。点右边的「拉取数据」开始下载。", "hint")
            else:
                self._msg(self.psid_hint,
                          "还没填。点上面输入框填题目集 ID，再点右边的「拉取数据」。", "hint")
        else:
            self._msg(self.psid_hint, text, "err" if error else "hint")
        self._sync_fetch_buttons()

    def _sync_fetch_buttons(self) -> None:
        """题目集 ID 没填就禁用「拉取数据 / 只检查能不能读」，并说明原因。

        以前点了没反应（或弹一句看不懂的话），第一次用的人不知道是漏填了 ID。
        """
        ready = bool(str(self.psid_var.get()).strip())
        for btn in (getattr(self, "fetch_btn", None), getattr(self, "check_btn", None)):
            if btn is None:
                continue
            try:
                btn.configure(state="normal" if ready else "disabled")
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------- 播放与显示页

    def _build_play_tab(self) -> None:
        page = self.tab_play

        body1 = self._card(page, "滚榜怎么揭晓（播放方式）",
                           "比赛结束后放滚榜时的节奏：三种方式对应三种现场场合")
        row = self._form_row(body1, "播放方式", pady=(4, 4))
        self.play_var = tk.StringVar()
        play_combo = ttk.Combobox(row, textvariable=self.play_var, state="readonly",
                                  width=34, values=list(PLAY_LABELS.keys()))
        play_combo.pack(side="left")
        play_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_play_mode())
        ToolTip(play_combo,
                "三种方式怎么选（现场用哪句顺口就用哪句）：\n"
                "  · 全程点击播放 —— 你要一边讲一边揭晓，每一步都由你点；\n"
                "  · 先自动、后点击 —— 前面名次自己滚，到你要讲的名次停下来等你点；\n"
                "  · 全程自动播放 —— 没人守着，放完为止（也可用「揭晓速度」控制快慢）。\n"
                "对应 Resolver 的参数：前两种用 --singleStep，最后一种不加。")
        play_hint = self._indent_row(body1, pady=(0, 4))
        self._t(play_hint,
                "现场通常选「先自动、后点击」：前面自动滚过去，到关键名次停下等你讲解。",
                size=9, fg=self.C["hint"]).pack(side="left")

        row_p = self._form_row(body1, "从第几名开始点击", pady=(2, 4))
        self.click_from_var = tk.StringVar(value="4")
        self.click_from_entry = self._entry(row_p, self.click_from_var, 6)
        self.click_from_entry.pack(side="left")
        self._hint(row_p, "名（只在「先自动、后点击」下有用：前面的名次自动滚，到这个名次停住等你点）",
                   size=9).pack(side="left", fill="x", expand=True)
        ToolTip(self.click_from_entry,
                "填 4 = 前 3 名自动揭晓，从第 4 名开始等你点。\n"
                "（Resolver 的 --singleStep N 实际从第 N-1 名开始等点击，所以这里填 4 会传 --singleStep 5。）")

        row2 = self._form_row(body1, "揭晓速度", pady=(4, 4))
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_scale = ttk.Scale(row2, from_=0.25, to=4.0, length=240,
                                     variable=self.speed_var, command=self._on_speed)
        self.speed_scale.pack(side="left")
        self.speed_label = self._t(row2, "1.00x", fg=self.C["ink"], width=7)
        self.speed_label.pack(side="left", padx=(6, 0))
        self._hint(row2, "2x 快一倍、0.5x 慢一倍；播放中还能按 +/- 调速、2 快进、p 暂停",
                   size=9).pack(side="left", fill="x", expand=True, padx=4)

        row2b = self._form_row(body1, "额外参数", pady=(4, 4))
        self.extra_var = tk.StringVar()
        extra_entry = self._entry(row2b, self.extra_var, 40)
        extra_entry.pack(side="left")
        ToolTip(extra_entry, "给 Resolver 的额外命令行参数，一般留空。\n"
                             "例如 --display 1 投到第二块屏、--light 浅色主题、--info 显示更多信息。")
        self._hint(row2b, "高级项，一般留空：如 --display 1 投第二块屏、--light 浅色、--info 更多信息",
                   size=9).pack(side="left", fill="x", expand=True, padx=10)

        # -- M15：打星奖项那张卡**整卡已删除** ---------------------------------
        # 用户口径：「怎么只有打星的有奖了，奖项表怎么没用了 / 将两个结合到一起算了，
        # 不用专门做打星的了，改为只有奖项表有识别，将全队标记做好我能随时更改就行了」。
        # 所以 M15 起奖项**只有一个来源 = 下面的「奖项表」**：每一行 = 一个奖项，
        # 自带名单（team_ids），名单就在同一张卡下面的「全队标记」矩阵里勾。
        # 打星（*）退化成一种勾选便利：`auto="star"`（工具条「按 * 自动勾选到 <奖项>」），
        # 不再是「模式」。原 StarAwardsPanel 整卡（含「奖项模式」单选 / 「奖项组」表 /
        # 打星全队勾选表）已删除，`self.star_panel` 也不再存在。

        body2 = self._card(page, "写进比赛数据包的数据",
                           "生成比赛数据包时写进 XML，滚榜就按这套规则算名次和奖项")
        row3 = self._form_row(body2, "封榜时长（分钟）", pady=(4, 4))
        self.freeze_var = tk.StringVar(value="60")
        freeze_entry = self._entry(row3, self.freeze_var, 8)
        freeze_entry.pack(side="left")
        ToolTip(freeze_entry, "比赛最后这么久不公开榜单（封榜），滚榜从封榜这一刻开始揭晓。")
        self._hint(row3, "比赛最后这么久不公开榜单（封榜），滚榜从这一刻开始揭晓",
                   size=9).pack(side="left", fill="x", expand=True, padx=10)

        row4 = self._form_row(body2, "罚时（分钟）", pady=(4, 4))
        self.penalty_var = tk.StringVar(value="20")
        self._entry(row4, self.penalty_var, 8).pack(side="left")
        self._hint(row4, "每道最终通过的题，之前每错一次加这么多分钟（ICPC 默认 20 分钟）",
                   size=9).pack(side="left", fill="x", expand=True, padx=10)

        # -- 奖项设置 + M14 全队标记 / 人数对照表 ------------------------------
        # 整块抽成 _build_award_table_ui（AssistantApp 复用同一套控件与同一个面板对象）。
        self._build_award_table_ui(body2)

        # ---------------------------------------------------------------
        # M13（4b / 4d）：这里原本有「队伍名怎么显示」输入框和「中文字体（必做）」
        # 整张卡片，用户反馈两者都用不上，已**删除**：
        #   · 队名一律按 {team.name} 显示（等价于 Resolver 的默认行为）；
        #   · 中文字体改用程序自带、实测没问题的内置字体
        #     （程序目录 tools\fonts\MicrosoftYaHei.ttc）—— 后端在 font_path
        #     为空/失效时自动回落，界面不再需要让用户挑字体，也不会再出现
        #     「还没有设置中文字体」的橙色警示条。
        # 只留一行说明，用户知道中文不会被渲染成方框即可。
        # ---------------------------------------------------------------
        font_note_row = self._indent_row(body2, pady=(6, 0))
        self._hint(font_note_row,
                   "中文字体：程序自带（无需设置）。滚榜画面用内置的微软雅黑，"
                   "队名不会变成方框；想换字体请替换程序目录下 tools\\fonts\\MicrosoftYaHei.ttc。",
                   size=9).pack(side="left", fill="x", expand=True)

    # -- 奖项表 ------------------------------------------------------------

    def _build_award_table_ui(self, body2, hint_wrap: int = 0) -> None:
        """滚榜页的「奖项表」整卡（M17：实现已搬到 :class:`award_editor.AwardEditor`）。

        M15 起**奖项只有一个来源**：这张表。每一行 = 比赛包里的一个 ``<award>``
        （``citation`` = 奖项名，``teamId`` = 这一行的名单），名单就在同一张卡下面的
        「全队标记」矩阵里**勾**（每个奖项一列）——「随时能改」是用户的硬要求。

        M17：实时榜单那页建的是**同一套实现**的第二个实例（``"live"``），两个实例共用
        ``cfg["awards"]`` 这一份数据、任一侧改动另一侧立刻同步。这里保留老入口与老属性名
        （``awards_box / award_rows / award_rows_holder / award_panel / fts_var /
        award_medal_var``，见下面的 property），老代码一个字都不用改。
        """
        self.build_award_editor("roll", body2, hint_wrap=hint_wrap)

    def build_award_editor(self, key: str, body, hint_wrap: int = 0,
                           compact: bool = False) -> "AwardEditor":
        """在卡片 ``body`` 里建出**整卡内容**的奖项表编辑器，注册进 ``award_editors[key]``。

        ``key`` 至少有两个：``"roll"``（滚榜页，主表，``compact=False``）与 ``"live"``
        （实时榜单页，``compact=True``）。两个编辑器共用同一份 ``cfg["awards"]`` ——
        改动经 :meth:`_on_awards_changed` 写回并把另一侧重画，所以切模式/切页立刻看到同样的值。

        ``compact=True``（M17b，用户口径「实时榜不用改勾选了，只用改金银铜对应的人数
        就行了」）：只建「奖项名 + 共 N 队 + 展示方式 + 删除」和一行「名次区间提示」，
        **不建**全队标记矩阵，也没有勾选 / 名单相关的按钮；``team_ids`` 照旧跟着 record
        走（不显示），主表那边算出的新名单会主动喂过来（名单只在滚榜侧勾）。

        重复建同一个 ``key`` 时，字典里换成新实例（旧实例的改动广播会自动失效，
        见 ``AwardEditor._notify_changed``），不销毁旧控件（同 key 建两次只是证据脚本的用法）。
        """
        editor = AwardEditor(self, key, body, hint_wrap=hint_wrap, compact=compact)
        self.award_editors[str(key)] = editor
        return editor

    # -- 奖项表（M17：老属性名指向 "roll" 那个编辑器）------------------------

    _SHOW_LABEL_TO_KEY = {v: k for k, v in cfgmod.AWARD_SHOW_LABELS.items()}

    def _roll_award_editor(self) -> "AwardEditor":
        """``"roll"`` 编辑器（滚榜那份 = 主表；``_package_settings()`` / 生成包都取它）。"""
        editor = self.award_editors.get("roll")
        if editor is None:
            raise AttributeError("奖项表编辑器还没建出来（award_editors['roll']）")
        return editor

    @property
    def awards_box(self):
        return self._roll_award_editor().awards_box

    @property
    def award_rows(self) -> list[dict]:
        return self._roll_award_editor().award_rows

    @property
    def award_rows_holder(self):
        return self._roll_award_editor().award_rows_holder

    @property
    def award_panel(self):
        return self._roll_award_editor().panel

    @property
    def fts_var(self):
        return self._roll_award_editor().fts_var

    @property
    def award_medal_var(self):
        return self._roll_award_editor().award_medal_var

    # -- 奖项表：对外方法（老签名/老语义，全部落到 "roll" 编辑器）------------

    def add_award_row(self, name: str = "", count: Any = 1, show: str = "detail",
                      **state: Any) -> dict:
        """往**滚榜那份**奖项表里加一行（``record`` 的键见 ``AwardEditor.add_award_row``）。"""
        return self._roll_award_editor().add_award_row(name, count, show, **state)

    def remove_award_row(self, record: dict) -> None:
        return self._roll_award_editor().remove_award_row(record)

    def set_awards(self, rows: list[dict]) -> None:
        """把奖项表整表换成 ``rows``（历史/配置载入走这条路；作用于 "roll" 编辑器）。"""
        return self._roll_award_editor().set_awards(rows)

    def collect_awards(self) -> list[dict]:
        """读出奖项表 —— M15 的**唯一奖项来源**（作用在 "roll" 那份上）。"""
        return self._roll_award_editor().collect_awards()

    def fill_award_by_rank(self, record: dict) -> None:
        return self._roll_award_editor().fill_award_by_rank(record)

    def clear_award_row(self, record: dict) -> None:
        return self._roll_award_editor().clear_award_row(record)

    def _after_award_row_change(self, quiet: bool = False) -> None:
        return self._roll_award_editor()._after_award_row_change(quiet=quiet)

    def _update_award_row_status(self, record: dict) -> None:
        return self._roll_award_editor()._update_award_row_status(record)

    def _sync_award_row_status(self) -> None:
        return self._roll_award_editor()._sync_award_row_status()

    # -- M17：双向同步 ------------------------------------------------------

    def _on_awards_changed(self, source_key: str) -> None:
        """**任一**编辑器改动后的统一收口：写 ``cfg["awards"]`` + 重画另一个编辑器。

        调用点：改名/改人数（去抖）、改展示方式、勾选/取消、名次填、清空、增删行。
        最后调 :meth:`_after_awards_changed`（基类空实现；合并外壳覆写它写运行目录 + 提示）。
        """
        if self._awards_syncing:
            return
        self._awards_syncing = True
        try:
            source = self.award_editors.get(source_key)
            if source is None:
                return
            try:
                self.cfg["awards"] = source.collect_awards()
            except Exception:  # noqa: BLE001
                pass
            try:
                rows = source.rows_snapshot()
            except Exception:  # noqa: BLE001
                rows = list(self.cfg.get("awards") or [])
            self._mirror_award_editors(source_key, rows)
        finally:
            self._awards_syncing = False
        try:
            self._after_awards_changed()
        except Exception:  # noqa: BLE001 - 收尾（写文件/提示）失败不能反过来影响编辑
            pass

    def _mirror_award_editors(self, source_key: str, rows: list[dict]) -> None:
        """把 ``rows`` 铺到**除 source 之外**的编辑器上（内容相同就跳过，不白重建控件）。"""
        for key, editor in list(self.award_editors.items()):
            if key == source_key:
                continue
            try:
                if editor.rows_snapshot() == rows:
                    continue
                editor.set_rows([dict(row) for row in rows])
            except Exception:  # noqa: BLE001
                continue

    def _after_awards_changed(self) -> None:
        """改动收尾钩子。基类什么都不做；``pta_assistant_gui`` 覆写它写运行目录 + 提示。"""
        return None

    def _award_edit_pending(self) -> bool:
        """有没有编辑器正等着去抖同步（有 → 别用 cfg 反向覆盖它）。"""
        for editor in list(self.award_editors.values()):
            if getattr(editor, "_sync_job", None):
                return True
        return False

    def recompute_award_panels(self) -> bool:
        """用**缓存**里的比赛数据把所有编辑器的矩阵重算一遍（不读文件）。

        返回是否有任一面板真的重算了（都没有缓存 → 调用方去走 ``refresh_award_state``）。
        """
        done = False
        for editor in list(self.award_editors.values()):
            panel = getattr(editor, "panel", None)
            if panel is None:
                continue
            try:
                if panel.has_data():
                    panel.recompute_from_cache()
                    done = True
            except Exception:  # noqa: BLE001
                continue
        return done

    def _on_play_mode(self) -> None:
        """切模式时开关「从第几名开始点击」并刷新模式说明。"""
        mode = PLAY_LABELS.get(self.play_var.get(), "click")
        try:
            self.click_from_entry.configure(
                state=("normal" if mode == "auto-then-click" else "disabled"))
        except Exception:  # noqa: BLE001
            pass
        self._collect_config()
        self._update_mode_line()
        # M14：模式/参数动了，奖项人数对照也跟着重算（去抖，不刷屏）
        self._schedule_award_refresh(250)

    def _on_speed(self, _value=None) -> None:
        try:
            speed = round(float(self.speed_var.get()) * 4) / 4.0
        except (TypeError, ValueError):
            return
        self.speed_label.configure(text=f"{speed:.2f}x")

    def _load_fonts(self, initial: bool = False) -> None:
        """M13（4d）：界面不再让用户挑字体 —— 这里只把 ``font_path`` 指向**内置字体**。

        用户口径：「字体文件大概率用不到，可以删了这个板块，直接使用这个实测没问题的
        字体就行了」。内置字体 = 程序目录 ``tools\\fonts\\MicrosoftYaHei.ttc``
        （实测无方框）；``font_path`` 存在的旧配置会被**兼容迁移**到这里
        （读到时忽略原值，见 ``board_config.sanitize``）。
        """
        path = fontsmod.builtin_font_path(cfgmod.resource_root())
        if path:
            self.cfg["font_path"] = path
            self._log(f"[字体] 使用内置字体：{Path(path).name}（无需设置）。\n")
        else:
            self._log("[字体] ⚠ 没找到内置字体 tools\\fonts\\MicrosoftYaHei.ttc，"
                      "中文可能显示成方框；请重装一份完整程序。\n")

    def _label_for_path(self, path: str) -> str | None:
        """M13：字体下拉框已删除，保留这个方法只为历史配置载入时不炸。"""
        return None

    def _on_font_pick(self, quiet: bool = False) -> None:
        """M13：没有字体控件了，确保 ``font_path`` 始终是内置字体。"""
        self._load_fonts(initial=True)

    def _update_font_banner(self) -> None:
        """M13（4d）：字体警示条已删除（字体内置，不会缺字），永远收起。"""
        return

    def on_preview_font(self) -> None:
        """M13（4d）：字体预览窗口已随板块删除，这里改成一句说明。"""
        path = fontsmod.builtin_font_path(cfgmod.resource_root())
        messagebox.showinfo(
            "字体已内置",
            "程序自带实测可用的中文字体，不用再选也不用预览：\n"
            f"{path or '（未找到内置字体，请重装一份完整程序）'}\n\n"
            "滚榜画面与榜单页都用它，中文队名不会变成方框。")

    # ------------------------------------------------------------ 滚榜页

    def _build_roll_tab(self) -> None:
        page = self.tab_roll

        body1 = self._card(page, "第二步：生成比赛数据包",
                           "把抓到的比赛数据转成滚榜程序（Resolver）认的数据包")
        row = self._form_row(body1, "比赛包放到哪", pady=(4, 4))
        self.pkg_var = tk.StringVar()
        # 宽度 44（原 52）：900x600 下 52 字符的输入框会把整行顶宽 10px，
        # 路径过长时输入框自己会横向滚动，不影响使用。
        self._entry(row, self.pkg_var, 44).pack(side="left")
        self._ghost_btn(row, "选择…", self._pick_package_dir).pack(side="left", padx=(8, 0))
        row2 = self._indent_row(body1, pady=(2, 2))
        self.package_btn = self._ghost_btn(row2, "生成比赛数据包", self.on_package)
        self.package_btn.pack(side="left")
        ToolTip(self.package_btn,
                "把 contest.json 转成 Resolver 用的比赛包（events.xml + 队伍照片 + 校徽）。\n"
                "改过奖项表、封榜时长、队伍照片之后要重新生成一次，\n"
                "否则「开始滚榜」会提示比赛包和当前设置不一致。")
        # M13（4e）：提示文字原来跟按钮挤在同一行（side=left），窄窗口下会被按钮
        # 挤掉一半（用户反馈"右边的提示文字显示不完全"）。现在**另起一行、占满这行
        # 剩余宽度**，换行宽度按这一行的实际可用宽度算（见 _fit_package_info）。
        row2b = self._indent_row(body1, pady=(0, 4))
        self.package_info = self._t(row2b, "还没有生成比赛数据包。", size=9,
                                    fg=self.C["hint"], anchor="w", justify="left")
        self.package_info.pack(side="left", fill="x", expand=True)

        def _fit_package_info(_event=None) -> None:
            """按「这一行 − 左侧缩进列」的真实宽度设 wraplength。

            不交给 ``_auto_wrap``：它按内容区宽度估，估出来比这一行实际可用宽度
            大 89px（900x600 实测），最后一行还是会被裁掉。这里只改 wraplength、
            不改宽度，没有「请求宽度 ↔ 换行宽度」的死循环。
            """
            try:
                row_w = int(row2b.winfo_width())
                kids = row2b.winfo_children()
                indent_w = int(kids[0].winfo_width()) if kids else 0
                if row_w <= 1:
                    return
                new = max(200, row_w - indent_w - 12)
                if abs(new - int(self.package_info.cget("wraplength"))) >= 8:
                    self.package_info.configure(wraplength=new)
            except Exception:  # noqa: BLE001
                pass

        self._fit_package_info = _fit_package_info
        row2b.bind("<Configure>", _fit_package_info)
        self.root.after_idle(_fit_package_info)

        # M13：这批数据有没有学号（照片/校徽按学号匹配的前提）。没有就红字提示，
        # 不把"5 张按学号命名的照片"静默当成"这些队没交照片"。见 _refresh_number_warn。
        self.number_warn = self._t(body1, "", size=9, fg=self.KIND["warn"],
                                   anchor="w", justify="left", wraplength=520)
        self._auto_wrap(self.number_warn, source=body1, min_width=260)

        # -- 队伍照片：复制成 <比赛包>\teams\<队伍ID>\photo.jpg，铺满奖项卡 ----
        row3 = self._form_row(body1, "队伍照片目录", pady=(6, 2))
        self.photos_var = tk.StringVar()
        photos_entry = self._entry(row3, self.photos_var, 40)
        photos_entry.pack(side="left")
        self._ghost_btn(row3, "选择…", self._pick_photos_dir).pack(side="left", padx=(8, 0))
        self._ghost_btn(row3, "默认目录",
                        self._reset_photos_dir).pack(side="left", padx=(6, 0))
        ToolTip(photos_entry,
                "放队伍照片的文件夹（滚榜揭晓奖项时，照片铺满整张奖项卡当背景）。\n"
                "不填就用应用所在目录下的「滚榜图片」子目录（本机自动定位，\n"
                "与 exe / 启动脚本同一个文件夹）；目录不存在会自动创建。\n"
                "文件名认这些写法（都可以带打星的 * / ＊ 前缀）：\n"
                "  · 学号（26160358.jpg）—— 最稳，推荐；\n"
                "  · 姓名（汪章正.jpg）；\n"
                "  · 学号+姓名（26160358汪章正.jpg / 26160358_汪章正.jpg）。\n"
                "支持 .jpg / .jpeg / .png。\n"
                "生成比赛包时会复制进 <比赛包>\\teams\\<队伍ID>\\photo.jpg（teams 这一层必须有）。\n"
                "建议 16:9 或更宽、1920x1080 以上 —— 照片会铺满整张奖项卡。\n"
                "留空、或者某支队伍没配到照片时，该队的奖项卡自动用默认背景（ICPC 默认底图），\n"
                "左下角的「校徽 + 队名 + 奖项名」栏照常显示。")
        row3b = self._indent_row(body1, pady=(0, 4))
        self.photos_hint = self._hint(
            row3b,
            f"现在用：{cfgmod.default_photos_dir()}（应用所在目录，不存在会自动创建）。"
            "文件名按**学号优先**匹配（26160358.jpg），也认姓名（汪章正.jpg）；"
            "学号+姓名连写（26160358汪章正.jpg / 26160358_汪章正.jpg）同样认；"
            "打星的 * / ＊ 前缀自动容忍；支持 jpg/jpeg/png。"
            "生成后会复制进 <比赛包>\\teams\\<队伍ID>\\photo.jpg —— "
            "滚榜揭晓奖项时队伍照片会铺满整张奖项卡（背景）。"
            "某队没照片时自动用默认背景（ICPC 默认底图），栏目照常。",
            size=9)
        self.photos_hint.pack(side="left", fill="x", expand=True)

        # -- 校徽：复制成 <比赛包>\organizations\<机构ID>\logo.png，画在奖项卡姓名前 ----
        row3c = self._form_row(body1, "校徽目录", pady=(6, 2))
        self.logos_var = tk.StringVar()
        logos_entry = self._entry(row3c, self.logos_var, 40)
        logos_entry.pack(side="left")
        self._ghost_btn(row3c, "选择…", self._pick_logos_dir).pack(side="left", padx=(8, 0))
        self._ghost_btn(row3c, "默认目录",
                        self._reset_logos_dir).pack(side="left", padx=(6, 0))
        ToolTip(logos_entry,
                "放校徽的文件夹。\n"
                "不填就用应用所在目录下的「校徽」子目录（本机自动定位，与 exe / 启动脚本同目录）；\n"
                "目录不存在会自动创建。\n"
                "规则：\n"
                "  · 文件名没有名字（.jpg 这种），或叫 logo / 校徽 / default / 机构\n"
                "    → 默认校徽，所有队都用它；\n"
                "  · 文件名 = 学号（26130030.png）→ 那支队的专属校徽（学号优先）；\n"
                "  · 学号+姓名连写（26130030张三.png / 26130030_张三.png）也认；\n"
                "  · 否则按姓名（汪章正.png）；打星的 * / ＊ 前缀自动容忍；\n"
                "  · 名字对不上任何队伍 → 忽略，生成日志里会列出来。\n"
                "生成时会把校徽裁成圆形（居中裁成正方形 + 圆外透明）再放进\n"
                "<比赛包>\\organizations\\<机构ID>\\logo.png；你的原文件不会被改动。\n"
                "没有默认校徽就是没有校徽（不会报错，也不显示）。")
        row3d = self._indent_row(body1, pady=(0, 4))
        self.logos_hint = self._hint(
            row3d,
            f"现在用：{cfgmod.default_logos_dir()}（应用所在目录，不存在会自动创建）。"
            "空名字（.jpg）或 logo/校徽/default/机构 = 默认校徽；学号优先（26130030.png），"
            "也认姓名（汪章正.png）与学号+姓名连写（26130030汪章正.png）；打星 * / ＊ 前缀自动容忍；"
            "对不上的会被忽略并在生成日志里列出。校徽会**裁成圆形**放进 "
            "<比赛包>\\organizations\\<机构ID>\\logo.png（你的原文件不动），"
            "显示在奖项卡左下角与榜单每一行上。",
            size=9)
        self.logos_hint.pack(side="left", fill="x", expand=True)

        body2 = self._card(page, "第三步：开始滚榜",
                           "点一下自动做完：设中文字体 → 加播放参数 → 拉起 Esc 退出守护",
                           expand=True)
        hero = self._f(body2)
        hero.pack(expand=True)

        self.start_btn = self._solid_btn(
            hero, "▶  开始滚榜", self.on_start_roll,
            bg=self.C["go"], hover=self.C["go_hover"],
            font=(UI_FONT, 17, "bold"), padx=46, pady=15)
        self.start_btn.pack(pady=(14, 10))
        ToolTip(self.start_btn,
                "点一下自动做完三件事：\n"
                "  ① 把中文字体告诉 Resolver（不设，队名全是方框）\n"
                "  ② 按上面选的播放方式加参数\n"
                "  ③ 拉起 Esc 退出守护\n"
                "比赛包不存在或和当前设置不一致时，它会先提示/自动重新生成。")

        three_things = self._t(hero, "点一下自动做完三件事：设置中文字体（不设队名全是方框） · "
                                     "按播放方式加参数 · 拉起 Esc 退出守护",
                               size=9, fg=self.C["hint"], justify="center")
        three_things.pack()
        self._auto_wrap(three_things)

        self.roll_info = self._auto_wrap(
            self._t(hero, "", fg=self.C["sub"], justify="center"))
        self.roll_info.pack(pady=10)

        # 本次模式用一行醒目大字显示 —— 用户最容易搞混的就是「我明明选了点击逐步」。
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

        shortcuts = self._t(hero, "Resolver 快捷键：p 暂停/继续 · 2 快进一步 · 0 从头开始 · +/- 调速 · Esc 退出 · Ctrl-Q 退出",
                            size=9, fg=self.C["hint"], justify="center")
        shortcuts.pack(pady=(6, 4))
        self._auto_wrap(shortcuts)

    def _pick_package_dir(self) -> None:
        folder = filedialog.askdirectory(
            title="选择比赛包目录（里面应该有 events.xml）",
            initialdir=str(self._default_package_dir()),
        )
        if folder:
            self.pkg_var.set(folder)

    def _pick_photos_dir(self) -> None:
        current = self.photos_var.get().strip()
        folder = filedialog.askdirectory(
            title="选择队伍照片目录（文件名用学号或姓名）",
            initialdir=current if current and Path(current).is_dir() else str(cfgmod.work_root()),
        )
        if folder:
            self.photos_var.set(folder)

    def _reset_photos_dir(self) -> None:
        """「默认目录」按钮：回到应用所在目录下的 滚榜图片（不存在就创建）。"""
        self.photos_var.set(self._use_default_dir(cfgmod.default_photos_dir(), "队伍照片"))

    def _reset_logos_dir(self) -> None:
        """「默认目录」按钮：回到应用所在目录下的 校徽（不存在就创建）。"""
        self.logos_var.set(self._use_default_dir(cfgmod.default_logos_dir(), "校徽"))

    def _use_default_dir(self, path: Path, label: str) -> str:
        ok, detail = cfgmod.ensure_dir(Path(path))
        if not ok:
            self._log(f"[目录] 默认{label}目录创建失败：{detail}\n")
            return str(path)
        self._log(f"[目录] {label}目录 = {detail}\n")
        return detail

    def _prepare_image_dir(self, raw: str, fallback: Path, label: str) -> str | None:
        """照片/校徽目录：空 -> 默认目录（不存在就创建）；手填的目录不存在则拦下来。"""
        text = str(raw or "").strip()
        default = Path(fallback)
        path = cfgmod.resolve_dir(text, default)
        if path.is_dir():
            return str(path)
        if not text or path == default:
            # 默认目录一律自动创建（需求：不存在就创建一个空目录）
            ok, detail = cfgmod.ensure_dir(path)
            if ok:
                self._log(f"[目录] 已创建默认{label}目录：{detail}\n")
                return detail
            messagebox.showwarning(f"{label}目录不可用", f"{path}\n\n{detail}")
            return None
        messagebox.showwarning(f"{label}目录不存在",
                               f"{path}\n\n这个目录不存在，本次不会处理{label}。"
                               f"\n（想用默认目录就点「默认目录」按钮）")
        return None

    def _pick_logos_dir(self) -> None:
        current = self.logos_var.get().strip()
        folder = filedialog.askdirectory(
            title="选择校徽目录（空名字 = 默认校徽；学号/姓名 = 专属校徽）",
            initialdir=current if current and Path(current).is_dir() else str(cfgmod.work_root()),
        )
        if folder:
            self.logos_var.set(folder)

    def _default_package_dir(self) -> Path:
        saved = str(self.cfg.get("last_package_dir") or "")
        if saved:
            return Path(saved)
        return cfgmod.work_root() / "resolver-package"

    # ------------------------------------------------------------ 历史页

    def _build_history_tab(self) -> None:
        page = self.tab_history

        body = self._card(page, "用过的比赛", "双击一行 = 载入那场比赛的设置（题目集 ID 一起填好）",
                          expand=True)

        holder = self._f(body)
        holder.pack(fill="both", expand=True)

        columns = ("psid", "name", "problems", "teams", "subs", "last")
        heads = ("题目集 ID", "比赛名称", "题", "队", "提交", "最后使用")
        self.tree = ttk.Treeview(holder, columns=columns, show="headings", selectmode="extended")
        for col, head in zip(columns, heads):
            self.tree.heading(col, text=head)
        self._apply_history_widths()
        self.tree.tag_configure("odd", background="#f6f8fb")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.on_history_load)

        # 空态引导：没有历史时盖在表格中间，有数据就藏起来
        self.history_empty = self._f(holder)
        self._t(self.history_empty, "还没有历史记录", size=12, bold=True,
                fg=self.C["hint"]).pack(pady=(0, 6))
        self._t(self.history_empty,
                "怎么才会有记录：到「比赛」页填题目集 ID、点「拉取数据」，\n"
                "抓过的比赛会自动出现在这里；下次双击那一行就能一键载入。",
                size=9, fg=self.C["hint"], justify="center").pack()

        bar = self._f(body)
        bar.pack(fill="x", pady=(10, 0))
        self._ghost_btn(bar, "载入选中", self.on_history_load).pack(side="left")
        self._ghost_btn(bar, "删除选中", self.on_history_delete).pack(side="left", padx=6)
        self._ghost_btn(bar, "清空历史", self.on_history_clear).pack(side="left")
        self._ghost_btn(bar, "打开比赛包目录",
                        lambda: self._open_folder(Path(self.pkg_var.get() or cfgmod.work_root()))
                        ).pack(side="right")

    def _apply_history_widths(self) -> None:
        """给历史表所有列定死宽度（含合并版补的模式/开始时间/结果三列）。

        为什么必须单独一个方法：``tree.configure(columns=…)`` 会**重置所有列**的
        width/minwidth/stretch 回 ttk 默认值（200/20/拉伸）—— 合并版补列时正好踩到，
        结果 9 列默认 200 宽 = 1414px，900x600 下右边的列全被裁掉。
        所以「补列之后再调一次本方法」。
        合计 ≈ 658px < 900x600 的内容区宽度，窄窗口也不会裁列。
        stretch=False：窗口再宽也不拉伸，列位置稳定，好认。
        """
        tree = getattr(self, "tree", None)
        if tree is None:
            return
        existing = set(tree["columns"])
        for key, width in (("psid", 116), ("name", 138), ("problems", 30), ("teams", 30),
                           ("subs", 44), ("last", 88), ("mode", 46),
                           ("started_at", 82), ("result", 84)):
            if key in existing:
                tree.column(key, width=width, minwidth=width, stretch=False, anchor="w")

    def _refresh_history(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.history_rows = []
        for idx, rec in enumerate(self.store.history):
            self.tree.insert("", "end", values=(
                rec.get("problem_set_id", ""),
                rec.get("name", ""),
                rec.get("problem_count", 0),
                rec.get("team_count", 0),
                rec.get("submission_count", 0),
                rec.get("last_used", ""),
            ), tags=("odd",) if idx % 2 else ())
            self.history_rows.append(str(rec.get("problem_set_id")))
        if self.store.history:
            self.history_empty.place_forget()
        else:
            self.history_empty.place(relx=0.5, rely=0.42, anchor="center")

    def on_history_load(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        values = self.tree.item(selection[0], "values")
        if not values:
            return
        psid = str(values[0])
        rec = self.store.find(psid)
        if not rec:
            return
        self.psid_var.set(psid)
        # M13（4d）：字体板块已删 —— 历史记录里的 font_path 不再生效，
        # 一律用内置字体（后端 start_resolver 也会再兜一层回落）。
        self._load_fonts(initial=True)
        self.play_var.set(PLAY_LABEL_BY_KEY.get(rec.get("play_mode", "click"), self.play_var.get()))
        self.click_from_var.set(str(rec.get("click_from_place") or 4))
        self.fts_var.set(bool(rec.get("first_to_solve", True)))
        if rec.get("speed"):
            self.speed_var.set(float(rec["speed"]))
            self._on_speed()
        self.freeze_var.set(str(rec.get("freeze_minutes", 60)))
        self.penalty_var.set(str(rec.get("penalty_minutes", 20)))
        if rec.get("awards"):
            self.set_awards(rec["awards"])
        self.award_medal_var.set(bool(rec.get("award_medal_summary")))
        # M15：历史里的 awards_mode / award_groups / star_marks 已废弃 ——
        # 奖项与名单只走 `rec["awards"]`（上面 set_awards 整行载入，含名单与来源）。
        # 载入后立刻按当前数据重算一次（M14 RC2：手改过的行不会被覆盖）。
        self.refresh_award_state(quiet=True)
        if rec.get("photos_dir"):
            self.photos_var.set(str(rec["photos_dir"]))
        if rec.get("logos_dir"):
            self.logos_var.set(str(rec["logos_dir"]))
        if rec.get("package_dir"):
            self.pkg_var.set(rec["package_dir"])
        self._update_mode_line()
        self.nb.select(self.tab_roll)
        self._log(f"[历史] 已载入《{rec.get('name')}》（题目集 {psid}）的设置。\n")
        self._set_status(f"已载入历史比赛：{rec.get('name')}")

    def on_history_delete(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("没选东西", "请先在列表里选中要删除的比赛（可以按住 Ctrl 多选）。")
            return
        psids = [str(self.tree.item(i, "values")[0]) for i in selection]
        if not messagebox.askyesno("确认删除", f"从历史里删除这 {len(psids)} 场比赛的记录？\n\n"
                                              "（只会删记录，不会删比赛包文件）"):
            return
        removed = self.store.forget(psids)
        self._persist()
        self._refresh_history()
        self._log(f"[历史] 删除了 {removed} 条记录。\n")

    def on_history_clear(self) -> None:
        if not self.store.history:
            messagebox.showinfo("历史是空的", "现在没有任何历史记录。")
            return
        if not messagebox.askyesno("确认清空", f"清空全部 {len(self.store.history)} 条历史记录？"):
            return
        self.store.history = []
        self._persist()
        self._refresh_history()
        self._log("[历史] 已清空。\n")

    # ------------------------------------------------------------- 行为

    def _apply_config(self) -> None:
        self.psid_var.set(str(self.cfg.get("problem_set_id") or ""))
        self.play_var.set(PLAY_LABEL_BY_KEY.get(self.cfg.get("play_mode", "click"),
                                                list(PLAY_LABELS.keys())[0]))
        self.click_from_var.set(str(self.cfg.get("click_from_place") or 4))
        self.fts_var.set(bool(self.cfg.get("first_to_solve", True)))
        self.speed_var.set(float(self.cfg.get("speed") or 1.0))
        self._on_speed()
        self.freeze_var.set(str(self.cfg.get("freeze_minutes", 60)))
        self.penalty_var.set(str(self.cfg.get("penalty_minutes", 20)))
        self.set_awards(self.cfg.get("awards") or [])
        self.award_medal_var.set(bool(self.cfg.get("award_medal_summary")))
        self.extra_var.set(str(self.cfg.get("extra_args") or ""))
        self.photos_var.set(self._dir_from_config(
            self.cfg.get("photos_dir"), cfgmod.default_photos_dir(), "队伍照片"))
        self.logos_var.set(self._dir_from_config(
            self.cfg.get("logos_dir"), cfgmod.default_logos_dir(), "校徽"))
        self.pkg_var.set(str(self.cfg.get("last_package_dir") or self._default_package_dir()))
        tab = int(self.cfg.get("last_tab") or 0)
        if 0 <= tab < self.nb.index("end"):
            self.nb.select(tab)
        # 读 contest.json 是 565 KB 的 JSON + 621 条提交的统计，**不放在启动路径上**；
        # 交给 _startup_checks 那个后台线程去补，窗口先出来。
        saved_contest = str(self.cfg.get("last_contest_json") or "")
        if saved_contest and Path(saved_contest).is_file():
            self.summary_var.set(f"正在读取上次抓到的数据：{saved_contest} …")
        # M14：奖项行的值已经在上面回填过了 —— 等窗口画出来再补一次全队预览
        # （读 contest.json 是 565 KB 的重活，不放启动路径）。
        self._schedule_award_refresh(500)

    def _dir_from_config(self, value, fallback: Path, label: str) -> str:
        """启动时把配置里的目录解析成绝对路径；没配过就用默认目录并创建它。"""
        text = str(value or "").strip()
        path = cfgmod.resolve_dir(text, Path(fallback))
        if not path.is_dir():
            ok, detail = cfgmod.ensure_dir(path)
            if ok:
                if not text:
                    self._log(f"[目录] {label}目录（默认位置已创建）：{detail}\n")
                return detail
            self._log(f"[目录] {label}目录不可用（{detail}）：{path}\n")
            return str(path)
        try:
            return str(path.resolve())
        except OSError:
            return str(path)

    def _collect_config(self) -> None:
        self.cfg["problem_set_id"] = self.psid_var.get().strip()
        # 取数只有一条路：直连接口（需要 Cookie）。
        self.cfg["source_mode"] = "http"
        self.cfg["play_mode"] = PLAY_LABELS.get(self.play_var.get(), "click")
        self.cfg["click_from_place"] = self._int_field(self.click_from_var, 4, 1, 10000)
        self.cfg["first_to_solve"] = bool(self.fts_var.get())
        self.cfg["speed"] = round(float(self.speed_var.get()) * 4) / 4.0
        self.cfg["freeze_minutes"] = self._int_field(self.freeze_var, 60, 0, 1440)
        self.cfg["penalty_minutes"] = self._int_field(self.penalty_var, 20, 0, 600)
        self.cfg["awards"] = self.collect_awards()
        self.cfg["award_medal_summary"] = bool(self.award_medal_var.get())
        self.cfg["display_name"] = "{team.name}"      # M13（4b）：已固定，界面不再可改
        self.cfg["extra_args"] = self.extra_var.get().strip()
        self.cfg["photos_dir"] = self.photos_var.get().strip()
        self.cfg["logos_dir"] = self.logos_var.get().strip()
        self.cfg["last_package_dir"] = self.pkg_var.get().strip()
        self.cfg["log_open"] = 1 if self.log_open else 0
        try:
            self.cfg["last_tab"] = self.nb.index(self.nb.select())
        except Exception:  # noqa: BLE001
            pass

    def _int_field(self, var, fallback: int, lo: int, hi: int) -> int:
        try:
            value = int(str(var.get()).strip())
        except (TypeError, ValueError):
            value = fallback
        return max(lo, min(hi, value))

    def _persist(self) -> None:
        self._collect_config()
        notes = self.store.save()
        for note in notes:
            self._log(f"[设置] {note}\n")
            self._set_status(note.splitlines()[0])

    # -- M14：奖项状态刷新（「重新识别 / 刷新」按钮 + 各处自动刷新）---------

    def _schedule_award_refresh(self, delay: int = 250, quiet: bool = True) -> None:
        """去抖地安排一次 :meth:`refresh_award_state`（切模式 / 载入历史等路径）。"""
        try:
            job = getattr(self, "_award_refresh_job", None)
            if job:
                self.root.after_cancel(job)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._award_refresh_job = self.root.after(
                max(0, int(delay)), lambda: self.refresh_award_state(quiet=quiet))
        except Exception:  # noqa: BLE001
            self._award_refresh_job = None

    def refresh_award_state(self, quiet: bool = False) -> None:
        """从奖项表读值 → 重算全队表/人数对照 → 写回 ``cfg["awards"]``。

        调用点（M14 第 4 条「填上去没刷新成功」的根治）：
        「重新识别 / 刷新」按钮、每个奖项行的改名/改人数（去抖，用缓存重算）、
        切「打星 / 按名次」模式、载入历史、拉数据完成、生成比赛包、开始滚榜。

        M17：**两个编辑器（滚榜 + 实时）的矩阵都刷**（各按自己那份行集合算，结果一致），
        ``cfg["awards"]`` 仍然取 **"roll"** 那一份（保持「滚榜那份 = 主表」的既有口径：
        ``_package_settings()`` / ``run_package`` 都从它取）。外部载入（配置/历史）后，
        顺手把「实时」那份也对齐过来 —— 有编辑器正等着去抖同步时不动它，免得盖掉用户刚敲的字。

        M17b：compact（实时侧）没有矩阵，它的行只靠「主表 → 它」的镜像保持最新；
        所以矩阵重算之后**再镜像一次**（否则它手里那份名单是重算之前的旧值）。
        同样避开正在去抖编辑的编辑器。

        ``quiet=True`` 不写日志（自动刷新路径不刷屏）。
        """
        self._award_refresh_job = None
        roll = self.award_editors.get("roll")
        if roll is not None:
            try:
                self.cfg["awards"] = roll.collect_awards()
            except Exception:  # noqa: BLE001 - 界面正在销毁时不要炸
                pass
            if not self._award_edit_pending():
                try:
                    self._mirror_award_editors("roll", roll.rows_snapshot())
                except Exception:  # noqa: BLE001
                    pass
        for editor in list(self.award_editors.values()):
            panel = getattr(editor, "panel", None)
            if panel is None:
                continue
            try:
                panel.refresh(quiet=quiet)
            except Exception as exc:  # noqa: BLE001
                try:
                    self._log(f"[奖项] 刷新失败：{type(exc).__name__}: {exc}\n")
                except Exception:  # noqa: BLE001
                    pass
        # 重算会把「按名次 / 星号」算出来的名单写回各行 —— 再收一次，让
        # ``cfg["awards"]``（以及生成包读的那份）与界面上看到的完全一致；
        # 顺手把这份「刚算出来的」名单喂给没有矩阵的那一侧（compact，实时侧），
        # 否则它手里那份是重算之前的旧名单。
        if roll is not None:
            try:
                self.cfg["awards"] = roll.collect_awards()
            except Exception:  # noqa: BLE001
                pass
            if not self._award_edit_pending():
                try:
                    self._mirror_award_editors("roll", roll.rows_snapshot())
                except Exception:  # noqa: BLE001
                    pass

    def on_fetch(self, check_only: bool = False) -> None:
        if self.busy:
            return
        self._collect_config()
        psid = str(self.cfg.get("problem_set_id") or "").strip()
        # VJudge 版：题目集 ID 那一栏填的是 VJudge 比赛 ID，允许直接粘网址。
        platform = str(self.cfg.get("platform") or "pta").strip().lower()
        if platform == "vjudge":
            normalized = backend.normalize_vjudge_contest(psid)
            if not normalized:
                self._psid_msg("VJudge 比赛 ID 应该是 862258 这种数字，"
                               "也可以直接粘 https://vjudge.net/contest/862258#rank —— 请检查后再试",
                               error=True)
                self._set_status("VJudge 比赛 ID 格式不对，没有开始抓取")
                self.psid_entry.focus_set()
                return
            if normalized != psid:
                self.cfg["problem_set_id"] = normalized
                try:
                    self.psid_var.set(normalized)
                except Exception:  # noqa: BLE001
                    pass
            psid = normalized
        elif not psid.isdigit():
            # 就地反馈：红字 + 状态栏 + 聚焦输入框，不弹窗打断
            self._psid_msg("题目集 ID 应该是一串数字，例如 2107050785530253312 —— 请检查后再试",
                           error=True)
            self._set_status("题目集 ID 格式不对，没有开始抓取")
            self.psid_entry.focus_set()
            return
        self._psid_msg()

        root = cfgmod.resource_root()
        out_path = cfgmod.work_root() / "contest.json"
        source_mode = self.cfg["source_mode"]
        freeze = self.cfg["freeze_minutes"]
        penalty = self.cfg["penalty_minutes"]
        self._persist()

        if check_only:
            # 取数方式固定：直连接口（需要 Cookie）。没有 Cookie 就直说，不做任何回落。
            source_mode = "http"

            def job() -> None:
                if platform == "vjudge":
                    vj = backend.load_vjudge_backend(root)
                    with backend.LogRedirect(self.sink):
                        print("=" * 62)
                        print("  只读体检：确认 VJudge 比赛 ID 能拉到 rank JSON（不写任何文件）")
                        print("=" * 62)
                    try:
                        code = vj.main(["check", "--contest", psid])
                    except SystemExit as exc:
                        code = int(exc.code or 1)
                    except Exception as exc:  # noqa: BLE001
                        code = 1
                        self._log(f"\n[错误] 体检异常：{exc}\n")
                    self._post(lambda: self._fetch_done(
                        {"ok": code == 0,
                         "message": "体检通过，比赛 ID 可读，可以点「拉取数据」了。" if code == 0
                         else f"体检没通过（退出码 {code}），原因见上面的日志。"}))
                    return
                pta_fetch, _x, _c = backend.load_backend(root)
                via = "http"
                with backend.LogRedirect(self.sink):
                    print("=" * 62)
                    print("  只读体检：确认 Cookie 有效、接口能读（不写任何文件）")
                    print("=" * 62)
                try:
                    code = pta_fetch.main(["check", "--problem-set", psid, "--via", via,
                                           "--max-pages", "1"])
                except SystemExit as exc:
                    code = int(exc.code or 1)
                except Exception as exc:  # noqa: BLE001
                    code = 1
                    self._log(f"\n[错误] 体检异常：{exc}\n")
                self._post(lambda: self._fetch_done(
                    {"ok": code == 0, "message": "体检通过，数据可读，可以点「拉取数据」了。" if code == 0
                     else f"体检没通过（退出码 {code}），原因见上面的日志。"}))
        else:
            def job() -> None:
                result = backend.run_fetch(root, psid, source_mode, out_path,
                                           freeze, penalty, self.sink,
                                           platform=platform)
                self._post(lambda: self._fetch_done(result))

        self._msg(self.fetch_state,
                  "正在只读体检 …" if check_only else "正在获取比赛数据 …", "hint")
        self._run_job(job, "正在获取比赛数据 …")

    def _fetch_done(self, result: dict) -> None:
        self._busy_off()
        if result.get("ok"):
            self._msg(self.fetch_state, "✓ 完成", "ok")
            stats = result.get("stats") or {}
            self.summary_var.set(
                f"《{result.get('title') or '（没有标题）'}》\n"
                f"题目数：{stats.get('problem_count', 0)}    "
                f"队伍数：{stats.get('team_count', 0)}    "
                f"提交数：{stats.get('submission_count', 0)}\n"
                f"提交时间范围：{self._short(stats.get('submit_min'))} .. {self._short(stats.get('submit_max'))}\n"
                f"结果分布：{stats.get('status_counts') or {}}\n"
                f"contest.json：{cfgmod.work_root() / 'contest.json'}"
            )
            self.cfg["last_contest_json"] = str(cfgmod.work_root() / "contest.json")
            self.store.remember({
                "problem_set_id": self.cfg["problem_set_id"],
                "name": result.get("title") or "",
                "problem_count": stats.get("problem_count", 0),
                "team_count": stats.get("team_count", 0),
                "submission_count": stats.get("submission_count", 0),
                "contest_json": self.cfg["last_contest_json"],
                "package_dir": self.cfg.get("last_package_dir") or "",
                "freeze_minutes": self.cfg["freeze_minutes"],
                "penalty_minutes": self.cfg["penalty_minutes"],
                "medals": self.cfg["medals"],
                "awards": self.cfg.get("awards") or [],
                "award_medal_summary": bool(self.cfg.get("award_medal_summary")),
                "photos_dir": self.cfg.get("photos_dir") or "",
                "logos_dir": self.cfg.get("logos_dir") or "",
                "font_path": self.cfg.get("font_path") or "",
                "play_mode": self.cfg["play_mode"],
                "speed": self.cfg["speed"],
                "source_mode": self.cfg["source_mode"],
            })
            self._persist()
            self._refresh_history()
            self._set_status(result.get("message") or "抓取成功")
            # M14/M15（RC2）：数据换了 —— 奖项表（含全队标记矩阵）必须重新识别一次，
            # 否则生成包时会拿着上一份 contest.json 的队伍/名次算奖。
            self.refresh_award_state(quiet=True)
        else:
            self._msg(self.fetch_state, "✗ 失败，详见日志", "err")
            self.summary_var.set(result.get("message") or "抓取失败，请看下面的日志。")
            self._set_status("抓取失败 —— 看日志区的最后几行")
            messagebox.showerror("抓取失败", result.get("message") or "抓取失败，请看日志。")

    def _short(self, value) -> str:
        text = str(value or "")
        return text.replace("T", " ").replace("+00:00", " UTC") if text else "（未知）"

    # -- 生成比赛包 --------------------------------------------------------

    def on_package(self) -> None:
        if self.busy:
            return
        self._collect_config()
        contest = Path(self.cfg.get("last_contest_json") or (cfgmod.work_root() / "contest.json"))
        if not contest.is_file():
            messagebox.showwarning("还没有数据",
                                   "还没有 contest.json。请先在「比赛」页点「拉取数据」。")
            return
        # M14/M15：生成前先把奖项表「读值 → 重算全队标记」跑一遍，让界面上的名单
        # 与即将写进包的结果一致（也顺手把 cfg["awards"] 对齐到表里现在的值）。
        self.refresh_award_state(quiet=True)
        package_dir = Path(self.pkg_var.get().strip() or self._default_package_dir())
        freeze = self.cfg["freeze_minutes"]
        penalty = self.cfg["penalty_minutes"]
        # M15：只有一个奖项来源 —— 奖项表（每行自带名单）。不再传 awards_mode/award_groups。
        awards = self.collect_awards()
        photos = self._prepare_image_dir(self.photos_var.get(), cfgmod.default_photos_dir(),
                                         "队伍照片")
        if photos is None:
            return
        logos = self._prepare_image_dir(self.logos_var.get(), cfgmod.default_logos_dir(),
                                        "校徽")
        if logos is None:
            return
        self._persist()

        def job() -> None:
            result = backend.run_package(cfgmod.resource_root(), contest, package_dir,
                                         freeze, penalty, self.cfg.get("medals") or "1 2 3",
                                         self.sink, awards=awards, photos_dir=photos,
                                         logos_dir=logos,
                                         medal_summary=self.cfg.get("award_medal_summary"),
                                         first_to_solve=self.cfg.get("first_to_solve"))
            self._post(lambda: self._package_done(result))

        self._run_job(job, "正在生成比赛包 …")

    def _package_done(self, result: dict) -> None:
        self._busy_off()
        if result.get("ok"):
            self._msg(self.package_info, result["message"].splitlines()[0], "ok")
            self._set_status("比赛包已生成，可以点「开始滚榜」了")
            photos = result.get("photos") or None
            if photos and not photos.get("error"):
                missing = photos.get("missing") or []
                if missing:
                    names = "、".join(str(m.get("name")) for m in missing[:8])
                    more = "" if len(missing) <= 8 else f" 等 {len(missing)} 支"
                    self._msg(self.photos_hint,
                              f"照片已挂上：{photos['matched_count']} / {photos['total']} 支"
                              f"（滚榜时铺满奖项卡背景）；没配到的有 {names}{more}"
                              " —— 未匹配的队伍将使用默认背景（ICPC 默认底图）", "warn")
                else:
                    self._msg(self.photos_hint,
                              f"照片已挂上：{photos['total']} 支全部就位 ✓"
                              "（滚榜时铺满奖项卡背景）", "ok")
            elif photos and photos.get("error"):
                self._msg(self.photos_hint, f"照片没处理：{photos['error']}", "err")
            logos = result.get("logos") or None
            if logos and not logos.get("error"):
                specific = logos.get("specific") or []
                if specific:
                    detail = "、".join(
                        f"{item['logo']}({item['how']})→{'、'.join(item['teams'][:3])}"
                        for item in specific[:3])
                    more = "" if len(specific) <= 3 else f" 等 {len(specific)} 个"
                    text = (f"校徽已挂上 {logos.get('copied', 0)} 个（已裁成圆形）："
                            f"默认 {logos.get('default_name') or '（无）'}；专属 {detail}{more}")
                else:
                    text = (f"校徽已挂上：{logos.get('default_name') or ''}"
                            f"（默认校徽，{len(logos.get('default_teams') or [])} 支队伍共用，已裁成圆形）")
                if logos.get("unused"):
                    text += f"；⚠ {len(logos['unused'])} 个没匹配到队伍：" + \
                            "、".join(logos["unused"][:4])
                if logos.get("no_logo_teams"):
                    text += f"；⚠ 没有默认校徽，{len(logos['no_logo_teams'])} 支队伍不显示校徽"
                if logos.get("notes"):
                    text += f"；⚠ 有 {len(logos['notes'])} 个没能处理成"
                self._msg(self.logos_hint, text, "warn" if (
                    logos.get("unused") or logos.get("no_logo_teams") or logos.get("notes")) else "ok")
            elif logos and logos.get("error"):
                self._msg(self.logos_hint, f"校徽没处理：{logos['error']}", "err")
            self.store.remember({
                "problem_set_id": self.psid_var.get().strip(),
                "name": (result.get("stats") or {}).get("title") or "",
                "problem_count": (result.get("stats") or {}).get("problem_count", 0),
                "team_count": (result.get("stats") or {}).get("team_count", 0),
                "package_dir": result.get("package_dir", ""),
            })
            self._persist()
            self._refresh_history()
            self._msg(self.roll_info, result["message"], "ok")
            self._show_package_state()
        else:
            self._msg(self.package_info, "生成失败", "err")
            self._set_status("生成比赛包失败")
            # M15：名单全空会被 run_package 直接拦住（单一模型下的硬保护）——
            # 把完整文案原样贴在界面上，不只靠弹窗（Popup 在 --no-dialog 下看不见）。
            self._msg(self.roll_info, result.get("message", "生成失败"), "err")
            messagebox.showerror("生成比赛包失败", result.get("message", ""))

    # -- 开始 / 停止滚榜 ---------------------------------------------------

    def _package_settings(self) -> dict:
        """所有会影响比赛包内容的设置 —— 用来算指纹。

        M14（RC1）：键集与 :data:`board_backend.DIGEST_KEYS` **完全一致**，值也先按
        整数/布尔规范化，再交给 ``backend.package_digest`` —— ``run_package`` 一侧
        用的是同一个函数，所以「同一份设置 → 同一个哈希」。

        M15：``awards`` 就是 :meth:`collect_awards` 的**唯一奖项来源**（每行自带名单），
        ``awards_mode`` / ``award_groups`` 两个键**已删除**（``DIGEST_KEYS`` 里也没有）。
        ``logo_plan`` 不在里面（它带文件 mtime，界面侧拿不到；只写进
        ``package-meta.json`` 的 settings 里做参考）。
        """
        try:
            freeze = int(self.cfg.get("freeze_minutes") or 0)
        except (TypeError, ValueError):
            freeze = 0
        try:
            penalty = int(self.cfg.get("penalty_minutes") or 0)
        except (TypeError, ValueError):
            penalty = 0
        try:
            awards = self.collect_awards()
        except Exception:  # noqa: BLE001 - 控件还没建好时退回配置里的值
            awards = list(self.cfg.get("awards") or [])
        return {
            "problem_set_id": self.cfg.get("problem_set_id"),
            "freeze_minutes": freeze,
            "penalty_minutes": penalty,
            "awards": awards,
            "award_medal_summary": bool(self.cfg.get("award_medal_summary")),
            "first_to_solve": bool(self.cfg.get("first_to_solve")),
            "photos_dir": self.cfg.get("photos_dir") or "",
            "logos_dir": self.cfg.get("logos_dir") or "",
            "medals": self.cfg.get("medals") or "1 2 3",
            "contest_json": str(self.cfg.get("last_contest_json") or ""),
        }

    def _award_digest(self) -> str:
        """当前设置（**含奖项表里现在填的值**）的比赛包指纹。"""
        try:
            self.cfg["awards"] = self.collect_awards()
        except Exception:  # noqa: BLE001
            pass
        return backend.package_digest(self._package_settings())

    def _refresh_number_warn(self) -> None:
        """M13：数据里**一个学号都没有**时，在滚榜页明确提示。

        用户报的「照片只认姓名不认学号」根因就是抓数这一环把 ``studentNumber`` 丢了
        （见 ``pta_fetch.fetch_teams`` 的 M13 修复）。老数据重新打开时也要能看见提示，
        免得 5 张按学号命名的照片被静默当成"这些队没交照片"。
        """
        label = getattr(self, "number_warn", None)
        if label is None:
            return
        path = Path(str(self.cfg.get("last_contest_json") or "").strip()
                    or (cfgmod.work_root() / "contest.json"))
        stats = backend.contest_team_number_stats(path)
        show = bool(stats.get("exists") and stats.get("total") and not stats.get("with_number"))
        if show:
            names = "、".join(stats["missing_names"][:5])
            self._msg(
                label,
                f"⚠ 这批数据没有学号（{stats['total']} 支队全都没有 studentNumber，"
                f"例如 {names}）——"
                "按学号命名的队伍照片 / 校徽【匹配不上】，只会按姓名匹配。\n"
                "   怎么办：到「1 数据源」页重新点「拉取数据」（旧的队伍缓存会被自动刷新），"
                "抓完这里会自动消失。", "warn")
        try:
            if show and not getattr(self, "_number_warn_shown", False):
                label.pack(fill="x", padx=(0, 4), pady=(2, 4))
                self._number_warn_shown = True
            elif not show and getattr(self, "_number_warn_shown", False):
                label.pack_forget()
                self._msg(label, "", "warn")      # 藏起来时顺手清掉文字，别留陈旧内容
                self._number_warn_shown = False
        except Exception:  # noqa: BLE001 - 窗口正在销毁
            pass

    def _show_package_state(self) -> None:
        """在滚榜页显示比赛包的生成时间 + 包内实际含哪些奖项（一眼看出新旧）。"""
        self._refresh_number_warn()
        pkg = Path(self.pkg_var.get().strip() or self._default_package_dir())
        info = backend.read_package_awards(pkg)
        if not info["exists"]:
            self._msg(self.package_info, "还没有生成比赛包。", "hint")
            return
        cur, why = backend.package_is_current(pkg, self._award_digest())
        awards = info["awards"]
        shown = "、".join(awards[:4]) + ("…" if len(awards) > 4 else "")
        text = (f"包时间 {info['mtime']} · 奖项 {len(awards)} 个"
                + (f"（{shown}）" if shown else "（无）"))
        if info["first_to_solve"]:
            text += f" · 含一血 {info['first_to_solve']}"
        if info["medals"]:
            text += f" · 金银铜 last-gold={info['medals']}"
        self._msg(self.package_info, text if cur else f"{text} —— ⚠ {why}", "ok" if cur else "warn")

    def on_start_roll(self) -> None:
        if self.busy:
            return
        if backend.resolver_running(self.resolver_proc):
            messagebox.showinfo("已经在滚了", "Resolver 已经在运行。要重开请先点「停止滚榜」。")
            return
        self._stop_requested = False          # 新的一次滚榜：清掉上一轮的停止请求
        self.resolver_proc = None             # 上一轮可能已自然退出，清干净再起
        self._collect_config()
        # M14：开滚前也刷新一次奖项状态（与即将用于自动重生成包的 cfg["awards"] 对齐）
        self.refresh_award_state(quiet=True)
        package_dir = Path(self.pkg_var.get().strip() or self._default_package_dir())
        if not (package_dir / "events.xml").is_file():
            if messagebox.askyesno("还没有比赛包",
                                   f"{package_dir} 里没有 events.xml。\n\n"
                                   "要现在先生成比赛包吗？"):
                self.on_package()
            return

        root = cfgmod.resource_root()
        font_path = self.cfg.get("font_path") or ""
        play_mode = self.cfg["play_mode"]
        speed = self.cfg["speed"]
        display = self.cfg.get("display_name") or "{team.name}"   # M13：固定模板
        extra = self.cfg["extra_args"]
        click_from = int(self.cfg.get("click_from_place") or 4)
        self._persist()

        # —— 关键：先确认比赛包和当前设置一致，旧包不硬滚 ——
        digest = self._award_digest()
        current, why = backend.package_is_current(package_dir, digest)
        if not current:
            self._log(f"\n[比赛包] {why} —— 已自动重新生成比赛包再滚。\n")
            self._set_status("设置已变，正在自动重新生成比赛包 …")

        problem = backend.preflight_resolver(root, package_dir, font_path, self.sink)
        if problem:
            messagebox.showerror("还不能开滚榜", problem)
            self._log(f"[错误] {problem}\n")
            return

        # M15：只有一个奖项来源 —— 奖项表（每行自带名单），不再传 mode/groups。
        awards = self.collect_awards()
        photos = self._prepare_image_dir(self.cfg.get("photos_dir"),
                                         cfgmod.default_photos_dir(), "队伍照片") or ""
        logos = self._prepare_image_dir(self.cfg.get("logos_dir"),
                                        cfgmod.default_logos_dir(), "校徽") or ""
        freeze = int(self.cfg.get("freeze_minutes") or 60)
        penalty = int(self.cfg.get("penalty_minutes") or 20)

        def job() -> None:
            self.sink.write("\n")
            if not current:
                regen = backend.run_package(
                    root,
                    Path(self.cfg.get("last_contest_json") or (cfgmod.work_root() / "contest.json")),
                    package_dir, freeze, penalty, self.cfg.get("medals") or "1 2 3",
                    self.sink, awards=awards, photos_dir=photos, logos_dir=logos,
                    medal_summary=self.cfg.get("award_medal_summary"),
                    first_to_solve=self.cfg.get("first_to_solve"))
                if not regen.get("ok"):
                    self._post(lambda: self._roll_failed(
                        "设置改了但比赛包没重新生成成功，已拦住不滚旧包：\n" + regen.get("message", "")))
                    return
                self._post(self._refresh_package_state)
            result = backend.start_resolver(root, package_dir, font_path, play_mode,
                                            speed, display, extra, self.sink, click_from)
            if not result.get("ok"):
                self._post(lambda: self._roll_failed(result.get("message", "")))
                return
            self.resolver_proc = result["process"]
            self._post(lambda: self._roll_started(result))

            # ✱ Esc 守护**立刻**拉起（带 Resolver 的 PID），不再等窗口出现 ——
            #   这样"刚启动 / 滚榜中 / 滚完"三个阶段按 Esc 都能退出；
            #   窗口还没画出来时守护按 PID 一样能结束（见 tools\esc_quit.py）。
            wd = backend.start_esc_watchdog(root / "tools" / "esc_quit.py",
                                            result["process"].pid)

            if self._stop_requested:
                # 用户在启动过程中就点了「停止滚榜」：收掉刚起来的 Resolver，不再往下走
                self.sink.write("[i] 启动过程中收到「停止滚榜」，已直接收掉 Resolver。\n")
                if wd.get("ok"):
                    backend.stop_process(wd["process"])
                backend.stop_resolver(self.resolver_proc, self.sink, grace=1.0)
                self.resolver_proc = None
                self._post(lambda: self._stop_done("已按「停止滚榜」结束。"))
                return

            wins = backend.wait_for_resolver_window(self.resolver_proc, timeout=45.0, sink=self.sink)
            if not wins:
                self.sink.write("[错误] 45 秒内没有等到 Resolver 窗口，可能已经崩了。\n")
                if wd.get("ok"):
                    backend.stop_process(wd["process"])
                self._post(lambda: self._roll_failed("Resolver 窗口没有出现，请看日志。"))
                return

            self._post(lambda: self._watchdog_done(wd, wins))
            self._tail_resolver_options(root)

        self._update_mode_line()
        self._run_job(job, "正在启动滚榜 …")

    def _refresh_package_state(self) -> None:
        self._show_package_state()

    def _roll_started(self, result: dict) -> None:
        self._msg(self.roll_info,
                  f"Resolver 已启动（PID {result['process'].pid}）。\n"
                  f"Esc 退出守护也已锁定这个 PID —— **现在就可以按 Esc 退出**，"
                  f"不用等窗口画出来（首次启动要 20 秒以上）。\n"
                  f"正在等窗口出现 …", "ok")

    def _update_mode_line(self, running: bool = False) -> None:
        """把「本次模式」和对应操作提示写死在滚榜页上。

        用户反馈过「设了点击逐步却像在自动播放」——绝大多数情况是把鼠标点在
        别处了：Resolver 只有拿到焦点才收得到按键/点击，所以这里必须明说。
        """
        mode = self.cfg.get("play_mode") or "click"
        if mode == "click":
            self._msg(self.mode_line, "本次模式：全程点击播放", "ok")
            self._msg(self.hint_line,
                      f"⚠ 点击要落在 Resolver 窗口上才算数 —— 先用鼠标点一下那个窗口再点/按键。"
                      f"（参数：--singleStep 2）", "warn")
        elif mode == "auto-then-click":
            place = int(self.cfg.get("click_from_place") or 4)
            self._msg(self.mode_line,
                      f"本次模式：先自动、后点击（前 {place - 1} 名自动滚，从第 {place} 名起等你点）", "ok")
            self._msg(self.hint_line,
                      f"⚠ 到第 {place} 名之后，点击要落在 Resolver 窗口上才算数。"
                      f"（参数：--singleStep {place + 1}）", "warn")
        else:
            self._msg(self.mode_line, "本次模式：全程自动播放（不用点）", "sub")
            self._msg(self.hint_line,
                      "中途可用 +/- 调速、2 快进一步、p 暂停；想改成一步一步来，"
                      "请到「播放与显示」页换播放模式", "hint")

    def _tail_resolver_options(self, root: Path) -> None:
        """后台读 Resolver 自己的日志，把参数解析结果原样贴到 GUI 日志里。

        `Option found: --singlestep 2` 表示参数被认了；
        一旦出现 `Illegal --singleStep value ignored` 就是被丢掉了（--singleStep 0/1 会这样）。
        """
        lines = backend.read_resolver_options(root, tries=40, interval=1.0)

        def report() -> None:
            if not lines:
                self._log("[提示] 没能读到 Resolver 的启动日志（不影响滚榜）。\n")
                return
            for line in lines:
                bad = ("ignored" in line) or ("Illegal" in line)
                self._log(f"[Resolver 参数] {line}\n")
                if bad:
                    self._msg(self.mode_line,
                              "⚠ Resolver 忽略了播放参数，本次可能不是点击逐步！", "err")
                    self._set_status("警告：Resolver 忽略了播放参数，请看日志")
            if not any("ignored" in ln or "Illegal" in ln for ln in lines):
                self._log("[OK] Resolver 已接受本次播放参数。\n")

        self._post(report)

    def _watchdog_done(self, wd: dict, wins: list) -> None:
        self._busy_off()
        if wd.get("ok"):
            self.watchdog_proc = wd["process"]
            self._log(f"[OK] Esc 退出守护已就绪（PID {wd['process'].pid}）：滚榜时按 Esc 就能退出。\n")
            self._msg(self.roll_info,
                      f"滚榜进行中 —— 窗口 {len(wins)} 个。\n"
                      "随时按 Esc 退出；也可以在 Resolver 里按 Ctrl-Q。\n"
                      "要在这里关掉，点下面的「停止滚榜」。", "ok")
            self._set_status("滚榜中 —— 按 Esc 退出")
        else:
            self._log(f"[警告] {wd.get('message')}\n")
            self._log("      Esc 键可能不管用，请改用 Resolver 里的 Ctrl-Q，或点「停止滚榜」。\n")
            self._msg(self.roll_info,
                      "滚榜进行中，但 Esc 守护没起来 —— 请用 Ctrl-Q 或「停止滚榜」退出。", "warn")
            self._set_status("滚榜中（Esc 守护失败）")

    def _roll_failed(self, message: str) -> None:
        self._busy_off()
        self._msg(self.roll_info, message, "err")
        self._set_status("启动滚榜失败")
        messagebox.showerror("启动滚榜失败", message)

    def on_stop_roll(self) -> None:
        """「停止滚榜」——**一定能停**（M13）。

        刻意**不看 ``self.busy``**：正在启动/正在生成比赛包时用户照样能点这个按钮，
        所以这里直接开一个独立线程去杀，不参与 ``_run_job`` 的忙门闩。
        杀法见 :func:`board_backend.stop_resolver`：先 WM_CLOSE 温和请求，
        ``grace`` 秒后 ``taskkill /F /T`` 连子进程一起强杀并复查残留。
        重启：``_stop_done`` 会把 ``resolver_proc`` 清空，之后再点「开始滚榜」是干净的。
        """
        if getattr(self, "_stopping", False):
            return
        self._stopping = True
        self._stop_requested = True
        self._set_status("正在停止滚榜 …")

        def job() -> None:
            message = backend.stop_resolver(self.resolver_proc, self.sink, grace=5.0)
            if self.watchdog_proc is not None:
                backend.stop_process(self.watchdog_proc)
                self.watchdog_proc = None
            self._post(lambda: self._stop_done(message))

        def runner() -> None:
            try:
                job()
            except Exception:  # noqa: BLE001
                self._log(f"[内部错误] 停止滚榜异常：{traceback.format_exc()}\n")
                self._post(lambda: self._stop_done("停止时出了点问题，请看日志。"))
            finally:
                self._stopping = False

        threading.Thread(target=runner, daemon=True).start()

    def _stop_done(self, message: str) -> None:
        self._busy_off()
        self.resolver_proc = None
        self._stop_requested = False
        self._stopping = False
        self._msg(self.roll_info, message + "　现在可以再点「开始滚榜」。", "sub")
        self._set_status(message)
        self._log(f"[i] {message}\n")

    # -- 日志 / 状态 -------------------------------------------------------

    def _log(self, text: str) -> None:
        self.log_queue.put(text)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.log_preview_var.set("")

    def _pump(self) -> None:
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    self._log(f"[内部错误] 界面回调异常：{traceback.format_exc()}\n")
        except queue.Empty:
            pass

        chunks: list[str] = []
        try:
            while True:
                chunks.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "".join(chunks))
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 4000:
                self.log_text.delete("1.0", f"{lines - 3000}.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
            # 标题栏上的「最新一行」预览：日志折叠着也能看到进展
            last = ""
            for chunk in reversed(chunks):
                for line in reversed(chunk.splitlines()):
                    if line.strip():
                        last = line.strip()
                        break
                if last:
                    break
            if last:
                self.log_preview_var.set(last[:110])

        self.root.after(120, self._pump)

    def _tick_process_state(self) -> None:
        running = backend.resolver_running(self.resolver_proc)
        watching = self.watchdog_proc is not None and self.watchdog_proc.poll() is None
        if self.watchdog_proc is not None and not watching:
            self.watchdog_proc = None
        self.proc_resolver_var.set(f"Resolver：{'运行中' if running else '未运行'}")
        self.proc_watchdog_var.set(f"Esc 守护：{'运行中' if watching else '未运行'}")
        self.resolver_dot.configure(fg=self.C["lamp_on"] if running else self.C["lamp_off"])
        self.watchdog_dot.configure(fg=self.C["lamp_on"] if watching else self.C["lamp_off"])
        # Resolver 自己退出了（比如用户按了 Esc），顺手收掉守护进程
        if self.watchdog_proc is not None and not running:
            backend.stop_process(self.watchdog_proc)
            self.watchdog_proc = None
            self.resolver_proc = None            # 清干净，方便直接再点「开始滚榜」
            self._stop_requested = False
            self._log("[i] Resolver 已退出，Esc 守护也一并收掉了。\n")
            self._set_status("滚榜已结束")
        self.root.after(1500, self._tick_process_state)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _post(self, fn) -> None:
        self.ui_queue.put(fn)

    def _run_job(self, job, status: str) -> None:
        self._busy_on(status)
        thread = threading.Thread(target=self._guarded, args=(job,), daemon=True)
        thread.start()

    def _guarded(self, job) -> None:
        try:
            job()
        except Exception:  # noqa: BLE001
            detail = traceback.format_exc()
            self._log(f"\n[内部错误] 任务异常：\n{detail}\n")
            self._post(lambda: self._roll_failed(f"程序内部出错：{detail.splitlines()[-1]}"))

    def _busy_on(self, status: str) -> None:
        self.busy = True
        self._set_status(status)
        self.progress.pack(side="right", padx=(0, 10))
        self.progress.start(12)
        # 有任务在跑就把日志区展开，让进度和输出直接可见（用户可再手动折叠）
        if not self.log_open:
            self.log_open = True
            self._sync_log_pane()
        # ✱ 「停止滚榜」**故意不禁用**：正在启动/生成包的时候用户照样要能停下来。
        for button in (self.fetch_btn, self.check_btn, self.package_btn, self.start_btn):
            try:
                button.configure(state="disabled")
            except Exception:  # noqa: BLE001
                pass
        try:
            self.stop_btn.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass

    def _busy_off(self) -> None:
        self.busy = False
        self.progress.stop()
        self.progress.pack_forget()
        for button in (self.fetch_btn, self.check_btn, self.package_btn,
                       self.start_btn, self.stop_btn):
            try:
                button.configure(state="normal")
            except Exception:  # noqa: BLE001
                pass

    def _open_folder(self, folder: Path) -> None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # noqa: S606 - Windows 上就是「打开资源管理器」
        except Exception as exc:  # noqa: BLE001
            messagebox.showinfo("打不开", f"无法打开 {folder}：{exc}")

    # -- 启动自检 ----------------------------------------------------------

    def _startup_checks(self) -> None:
        root = cfgmod.resource_root()
        self._log("=" * 62 + "\n")
        self._log(f"  {APP_TITLE}\n")
        self._log("=" * 62 + "\n")
        self._log(f"  资源目录  : {root}\n")
        self._log(f"  可写目录  : {cfgmod.work_root()}\n")
        self._log(f"  设置文件  : {self.store.config_path}\n")
        self._log("-" * 62 + "\n")
        for note in self.store.notes:
            self._log(f"[提示] {note}\n")

        def job() -> None:
            checks = backend.check_environment(root)
            for check in checks:
                self._log(check.line() + "\n")
            self._post(lambda: self._startup_summary(checks))
            saved = str(self.cfg.get("last_contest_json") or "")
            if saved and Path(saved).is_file():
                _f, _x, common = backend.load_backend(root)
                data = common.load_contest(Path(saved))
                stats = data.get("stats") or common.contest_stats(data)
                meta = data.get("contest") or {}
                self._post(lambda: self._show_existing_contest(Path(saved), meta, stats))

        threading.Thread(target=self._guarded, args=(job,), daemon=True).start()

    def _show_existing_contest(self, contest: Path, meta: dict, stats: dict) -> None:
        self.summary_var.set(
            f"上次抓到的数据：《{meta.get('title') or ''}》\n"
            f"题目数：{stats.get('problem_count', 0)}    "
            f"队伍数：{stats.get('team_count', 0)}    "
            f"提交数：{stats.get('submission_count', 0)}\n"
            f"文件：{contest}\n"
            "（想更新就再点一次「拉取数据」，会做增量刷新。）"
        )

    def _startup_summary(self, checks: list) -> None:
        bad = [c for c in checks if not c.ok]
        blocking = [c for c in bad if c.name in ("后端脚本", "Java", "ICPC Resolver", "中文字体")]
        if not bad:
            self._set_status("环境检查通过，可以开始用了")
            self.env_dot.configure(fg=self.C["lamp_on"])
            self.env_text.configure(text=" 环境正常")
        elif not blocking:
            self._set_status(f"环境检查基本通过（{len(bad)} 条提示，见日志）")
            self.env_dot.configure(fg="#d9a72e")
            self.env_text.configure(text=f" {len(bad)} 条提示，见日志")
        else:
            names = "、".join(c.name for c in blocking)
            self._set_status(f"缺少：{names} —— 见日志里的「→」怎么补")
            self.env_dot.configure(fg="#e06a6a")
            self.env_text.configure(text=f" 缺少：{names}")
            self.nb.select(self.tab_contest)
            self._log("\n" + "!" * 62 + "\n")
            self._log("  下面这些还没准备好，先按提示补齐再滚榜：\n")
            for check in blocking:
                self._log(f"   · {check.name}：{check.detail}\n")
                if check.fix:
                    self._log(f"     → {check.fix}\n")
            self._log("!" * 62 + "\n")

    # -- 退出 --------------------------------------------------------------

    def on_close(self) -> None:
        if backend.resolver_running(self.resolver_proc):
            if not messagebox.askyesno("滚榜还在跑", "Resolver 还在运行。\n\n关闭本窗口会一并停止滚榜，确定吗？"):
                return
        # 只在正常可见状态下记录几何：最小化时 Tk 可能报出 -32000 之类的坐标，
        # 记下来会让下次启动的窗口跑到屏幕外。取不到可用值时保留上一次的记录，不要清空。
        try:
            if self.root.state() != "iconic":
                geometry = usable_geometry(self.root.geometry(),
                                           self.root.winfo_screenwidth(),
                                           self.root.winfo_screenheight())
                if geometry:
                    self.cfg["window_geometry"] = geometry
        except Exception:  # noqa: BLE001
            pass
        self.cfg["log_open"] = 1 if self.log_open else 0

        notes = self.store.save()
        for note in notes:
            print(note, file=sys.stderr)

        try:
            if backend.resolver_running(self.resolver_proc):
                backend.stop_resolver(self.resolver_proc, self.sink, grace=3.0)
        except Exception:  # noqa: BLE001
            pass
        backend.stop_process(self.watchdog_proc)
        fontsmod.unregister_all()
        self.root.destroy()


# ==========================================================================
# 字体预览窗口
# ==========================================================================

class FontPreview:
    """把字体文件临时加载进本进程，再用 Tk 渲染一行中英文样本。"""

    def __init__(self, parent, path: str) -> None:
        self.path = path
        self.top = tk.Toplevel(parent)
        self.top.title(f"字体预览 —— {Path(path).name}")
        self.top.transient(parent)
        self.top.geometry("780x480")
        self._images: list = []

        info = fontsmod.probe_glyphs(path)
        family_in_file, style = fontsmod.family_name(path)
        tk_family, tk_why = fontsmod.tk_family_for(path)

        outer = ttk.Frame(self.top, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=str(path), foreground="#555", wraplength=740,
                  justify="left").pack(anchor="w")
        ttk.Label(outer, text=f"字体内部名称：{family_in_file or '（读不出来）'}"
                             f"{'  /  ' + style if style else ''}"
                             + (f"    Tk 里叫：{tk_family}" if tk_family else "")).pack(anchor="w", pady=(2, 0))

        verdict = fontsmod.describe_probe(info, Path(path).name)
        color = "#1a6b34" if info.get("ok") else "#a11"
        ttk.Label(outer, text=verdict, foreground=color, wraplength=740,
                  justify="left", font=(UI_FONT, 10, "bold")).pack(anchor="w", pady=(8, 4))

        box = ttk.LabelFrame(outer, text="下面这行就是 Resolver 里队名牌的样子 —— 出现方框就是不能用")
        box.pack(fill="x", pady=8)

        sample = fontsmod.SAMPLE_TEXT
        rendered_with_tk = bool(tk_family)
        if rendered_with_tk:
            tk.Label(box, text=sample, font=(tk_family, 20), anchor="w").pack(
                anchor="w", padx=10, pady=10)
        else:
            ttk.Label(box, text=f"（Tk 用不了这个字体：{tk_why}；下面改用图片方式渲染同一行，"
                                "字形数据是一样的）",
                      foreground="#a60", wraplength=720, justify="left").pack(
                          anchor="w", padx=10, pady=(8, 0))
            img = self._pillow_image(sample, 26)
            if img is not None:
                label = ttk.Label(box, image=img)
                label.pack(anchor="w", padx=10, pady=10)

        sep = ttk.LabelFrame(outer, text="榜单格子里真正用到的符号（这几个最容易缺）")
        sep.pack(fill="x", pady=6)
        marks = "−  •  ⁄  -  －  0 1 8  ( )  :"
        if rendered_with_tk:
            tk.Label(sep, text=marks, font=(tk_family, 20), anchor="w").pack(anchor="w", padx=10, pady=8)
        else:
            img2 = self._pillow_image(marks, 26)
            if img2 is not None:
                ttk.Label(sep, image=img2).pack(anchor="w", padx=10, pady=8)

        if info.get("missing"):
            names = "、".join(f"{m['char']}({m['code']})" for m in info["missing"])
            ttk.Label(outer, text=f"缺字清单：{names}", foreground="#a11",
                      wraplength=740, justify="left").pack(anchor="w", pady=(4, 0))

        ttk.Label(outer, text=(
            "提示：只要上面两行里没有出现「□」，这个字体就能放心用于滚榜。"
        ), foreground="#555", wraplength=740, justify="left").pack(anchor="w", pady=(10, 0))

        ttk.Button(outer, text="关闭", command=self.top.destroy).pack(anchor="e", pady=(10, 0))
        self.top.bind("<Escape>", lambda _e: self.top.destroy())

    def _pillow_image(self, text: str, size: int):
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
        except ImportError:
            return None
        try:
            font = fontsmod._open_font(Path(self.path), size)  # noqa: SLF001
            tmp = Image.new("L", (10, 10))
            box = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
            width = max(40, box[2] - box[0] + 24)
            height = max(30, box[3] - box[1] + 24)
            img = Image.new("RGB", (width, height), "white")
            ImageDraw.Draw(img).text((12, 12), text, font=font, fill="black")
            photo = ImageTk.PhotoImage(img)
            self._images.append(photo)
            return photo
        except Exception:  # noqa: BLE001
            return None


# ==========================================================================
# 入口
# ==========================================================================

def _install_excepthook(root_dir: Path) -> None:
    log_path = backend.logs_dir() / "gui-errors.log"

    def hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n----- {time.strftime('%Y-%m-%d %H:%M:%S')} -----\n{text}")
        except OSError:
            pass
        try:
            messagebox.showerror(
                "程序出错了",
                f"遇到一个没预料到的问题：\n\n{exc_value}\n\n"
                f"详细信息已记录到：\n{log_path}\n\n"
                "可以把上面那段话截图发出来，方便定位。",
            )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook
    threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pta_board_gui.py", add_help=True)
    parser.add_argument("--esc-watchdog", action="store_true",
                        help="内部使用：运行 Esc 退出守护后退出")
    parser.add_argument("--smoke-test", action="store_true",
                        help="内部使用：自检（生成比赛包 + 启动 Resolver + Esc 守护）")
    parser.add_argument("--package", default=None, help="--smoke-test 用的比赛包目录")
    parser.add_argument("--font", default=None, help="--smoke-test 用的字体文件")
    parser.add_argument("--no-dialog", action="store_true",
                        help="自动化用：所有 messagebox 只写 logs\\gui-errors.log，不弹窗")
    parser.add_argument("--startup-trace", action="store_true",
                        help="量首屏耗时：记录「进程创建 -> 窗口可见」各阶段并写入 "
                             "logs\\startup-trace.txt，写完自动退出（供脚本计时用）")
    parser.add_argument("--keep-open", action="store_true",
                        help="配合 --startup-trace：写完计时后不退出，继续正常使用")
    args, _unknown = parser.parse_known_args(argv)

    if args.esc_watchdog:
        return run_esc_watchdog()
    if args.smoke_test:
        pkg = Path(args.package) if args.package else None
        return run_smoke_test(pkg, args.font)

    trace = StartupTrace(bool(args.startup_trace), not args.keep_open)
    trace.mark("模块导入完成（tkinter + 本项目模块）")

    store = cfgmod.Store()
    store.load()
    trace.mark("设置读取完成")

    root = tk.Tk()
    trace.mark("Tk 初始化完成")
    try:
        # 让 ttk 控件也用上中文友好的字体
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        for font_name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            try:
                tkfont.nametofont(font_name).configure(family=UI_FONT, size=10)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    trace.mark("ttk 主题与默认字体设置完成")

    _install_excepthook(cfgmod.resource_root())
    app = BoardApp(root, store, trace)

    if trace.enabled:
        _arm_visibility_trace(root, trace)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    return 0


def _arm_visibility_trace(root, trace: StartupTrace) -> None:
    """在 mainloop 的第一轮里判定「窗口真的可见了」，然后写报告。

    绑定 <Map> 事件比轮询更准：Tk 把顶层窗口映射到屏幕时就会触发。
    """
    state = {"done": False}
    deadline = time.time() + 60.0

    def check(_event=None) -> None:
        if state["done"]:
            return
        try:
            visible = bool(root.winfo_viewable())
        except Exception:  # noqa: BLE001
            visible = False
        if visible:
            state["done"] = True
            trace.mark("窗口可见（已映射到屏幕）")
            text = trace.write()
            if not text:
                pass
            if trace.exit_after:
                root.after(400, root.destroy)
            return
        if time.time() > deadline:
            state["done"] = True
            trace.mark("窗口可见（超时未判定，按 update 后计）")
            trace.write()
            if trace.exit_after:
                root.after(200, root.destroy)
            return
        root.after(5, check)

    root.bind("<Map>", check)
    root.after(0, check)


def combo_width_chars(values, minimum: int = 26, extra_px: int = 40) -> int:
    """把「最长文案的实测像素宽」换算成 ttk.Combobox 的 ``-width``（字符数）。

    Tk 的 ``-width`` 是按字体里 ``0`` 的宽度算的，而中文是**全角**：一个汉字 ≈ 2 个
    平均字宽。所以「看着够宽」的 22 装不下 10 个全角字 + 下拉箭头，会被截成
    ``停下显示详情（推…``（M13 4a 报的就是这个）。这里不写死数字，按实测像素反推，
    换字体 / 改 DPI / 加文案都跟着走。

    ``extra_px`` 放 ``下拉箭头 + 左右内边距`` 的余量；``minimum`` 是最小字符数。
    量不到（没有 Tk 根窗口 / 字体缺失）时退回 ``max(minimum, 30)``。

    （M16 从已废弃的 ``star_awards_panel.py`` 搬到这里 —— 那是 UI 布局工具，
      和「打星/分组」没关系，留在会删掉的文件里会被一起带走。）
    """
    try:
        font = tkfont.nametofont("TkDefaultFont")
        longest = max((str(v) for v in values), key=len, default="")
        unit = max(1, int(font.measure("0")))       # Tk 的「平均字符宽」= '0' 的宽度
        need = int(font.measure(longest)) + int(extra_px)
        return max(int(minimum), -(-need // unit))  # 向上取整
    except Exception:  # noqa: BLE001 - 没有 Tk 根窗口 / 字体缺失
        return max(int(minimum), 30)


if __name__ == "__main__":
    raise SystemExit(main())
