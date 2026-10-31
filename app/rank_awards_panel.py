# -*- coding: utf-8 -*-
"""M15：「奖项表」下面的**全队标记矩阵**（每个奖项一列勾选框）。

用户口径（M15）::

    怎么只有打星的有奖了，奖项表怎么没用了
    将两个结合到一起算了，不用专门做打星的了，
    改为只有奖项表有识别，将全队标记做好我能随时更改就行了

所以 M15 把「打星奖项」整张卡删掉，奖项只剩**一张「奖项表」**（每行 = 一个奖项，
自带名单），下面接**这一张全队标记矩阵**：

* 表头 = ``名次 ｜ 队名（原样含 *） ｜ 学号 ｜ <每个奖项一列：奖项名 (已选 N)> ｜ 获奖来源``；
* 每行每列一个勾选框 —— **勾一下 = 该队获这个奖**，立刻把这一行标成「手动」并写回
  ``cfg["awards"]``（手改优先，「重新识别 / 刷新」**不会**覆盖手改过的行）；
* 工具条：``重新识别 / 刷新``、``按 * 自动勾选到 [奖项]``、``全部清空``；
* 状态行实时给出 ``已勾选 N 队 · 未获奖 M 队 / 共 T 队``，并把
  「找不到的队 / 改名了」和「数据变了（新增 / 消失）」明确写出来。

算法只有一套：``board_backend.award_rows_preview_from_contest`` —— 它直接调用生成
比赛包用的 ``materialize_award_rows``，所以「界面里打勾的」=「包里发出去的」。
这个面板**不做任何切分/排序/发奖**，只负责显示与把勾选写回奖项行。

性能与布局纪律：

* ``refresh()`` 会重读 565 KB 的 ``contest.json``；**每敲一下键盘只重算**
  （用缓存的 contest 字典 + 去抖 150ms），不会反复读文件；
* 矩阵画在**自带 Canvas** 里，列宽是**像素固定**的（``pack_propagate(False)``），
  Canvas 的请求尺寸只由 ``height``/``width`` 决定、与内容无关 —— 奖项再多也不会
  把整页请求宽度顶爆（900x600 不截断），多了只出横向滚动条；
* 只有当「队伍列表」或「奖项个数」真的变了才重建控件，其余情况原地更新
  （32 队 × 若干奖项 = 上百个勾选框，每敲一次键盘全量重建会卡）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

#: 中文字体（和主界面一致；面板不 import pta_board_gui，避免循环 import）
_FONT = "Microsoft YaHei UI"
#: 敲一次奖项名/人数后，多久重算一次（毫秒，去抖）
_EDIT_DEBOUNCE_MS = 150

# ---- 矩阵列宽（**像素**；单元格固定尺寸 + pack_propagate(False)）------------------------
# M15 收尾（Lead 实测 900x600）：4 个奖项时整行内容 ~615px，比画布可视宽度宽 ~40px，
# 「获奖来源」列会被切掉一截、横向滚动条被顶到窗口外。这里把三列各收一点，
# 让 900x600 下 xview 直接是 (0.0, 1.0)（一行全看得见，不用横向滚）。
_COL_RANK = 42
_COL_TEAM = 146
_COL_NUMBER = 84
_COL_STAR = 52        # M39：打星勾选列（蓝/黄六边形标记的来源）
_COL_AWARD_MIN = 60
_COL_AWARD_MAX = 104
_COL_SOURCE = 118
_ROW_H = 24
_HEAD_H = 34
#: 矩阵可视高度（其余靠自带滚动条）
_CANVAS_H = 236
#: 提示文字的初始换行宽度（会交给 owner._auto_wrap 跟着真实宽度走）
_HINT_WRAP = 520

#: 奖项名在列头里最多显示几个「显示宽度」（中文算 2）
_HEAD_NAME_UNITS = 10


def text_units(text: Any) -> int:
    """按「显示宽度」数：中文/全角算 2，其它算 1（用来算列宽和省略号）。"""
    total = 0
    for ch in str(text or ""):
        total += 2 if ord(ch) > 0x2E7F else 1
    return total


def elide(text: Any, limit: int) -> str:
    """把文字截到 ``limit`` 个显示宽度以内，超出补 ``…``。"""
    raw = str(text or "")
    if limit <= 0 or text_units(raw) <= limit:
        return raw
    out = ""
    used = 0
    for ch in raw:
        width = 2 if ord(ch) > 0x2E7F else 1
        if used + width > limit - 1:
            break
        out += ch
        used += width
    return out + "…"


def has_star(name: Any) -> bool:
    """队名是否以 ``*`` / ``＊`` 开头（和 ``star_awards.has_star`` 同一口径）。"""
    return str(name or "").lstrip().startswith(("*", "＊"))


class RankAwardsPanel:
    """全队标记矩阵。``owner`` 必须提供
    ``_f/_t/_ghost_btn/C/cfg/_log/root/collect_awards/refresh_award_state``。"""

    def __init__(self, owner: Any, parent: Any, editor: Any = None) -> None:
        self.owner = owner
        self.parent = parent
        #: M17：本矩阵属于哪一张奖项表（``award_editor.AwardEditor``；老调用方不传 = None，
        #: 这时退回「看 owner 的 award_rows / collect_awards」的老口径）。
        self.editor = editor
        #: 榜单队伍行（preview["rows"]：rank/team_id/name/student_number/awards…）
        self.rows: list[dict[str, Any]] = []
        #: 逐奖项对照（preview["awards"]：name/count/picked/team_ids/missing_ids/renamed…）
        self.awards: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.mode = "rows"
        self.total_teams = 0
        self.unassigned = 0
        self.picked_total = 0
        self._preview: dict[str, Any] = {}
        self._contest: dict[str, Any] | None = None    # 缓存的 contest.json 内容
        self._contest_path = ""
        self._load_error = ""
        self._diff: dict[str, Any] = {}                # 与上次快照的差异（新增/消失/改名）
        self._tracked: set[int] = set()
        self._job: Any = None
        self._built = False
        self._hb_shown = False
        self._win_w: int | None = None
        #: 渲染缓存：队伍签名（队伍 ID 元组 + 奖项个数）不变就原地更新
        self._row_sig: tuple | None = None
        self._cells: dict[tuple[int, str], tk.Checkbutton] = {}
        self._cell_vars: dict[tuple[int, str], tk.BooleanVar] = {}
        self._row_refs: list[dict[str, Any]] = []
        self._head_refs: list[dict[str, Any]] = []
        self._build()

    # ---------------------------------------------------------------- 小工具
    def _c(self, key: str, fallback: str) -> str:
        return str((getattr(self.owner, "C", {}) or {}).get(key, fallback))

    def _tip(self, widget, text: str) -> None:
        """复用 owner 的 ToolTip（没有就静默跳过，不为了提示把界面搞崩）。"""
        maker = getattr(self.owner, "_tooltip", None)
        if callable(maker):
            try:
                maker(widget, text)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            import pta_board_gui  # noqa: PLC0415
            pta_board_gui.ToolTip(widget, text)
        except Exception:  # noqa: BLE001
            pass

    def _label(self, parent, text: str = "", fg: str = "", bg: str = "",
               size: int = 9, anchor: str = "w", **kw) -> tk.Label:
        return tk.Label(parent, text=text, anchor=anchor,
                        font=(_FONT, size), fg=fg or self._c("sub", "#4b5b70"),
                        bg=bg or self._c("card", "#ffffff"), justify="left", **kw)

    def _autowrap(self, label: tk.Label, min_width: int = 200) -> tk.Label:
        """把提示标签登记给 owner 的统一换行调度（没有就退回固定宽度）。"""
        hook = getattr(self.owner, "_auto_wrap", None)
        if callable(hook):
            try:
                hook(label, source=label, min_width=min_width)
                schedule = getattr(self.owner, "_schedule_fit", None)
                if callable(schedule):
                    schedule()
                return label
            except Exception:  # noqa: BLE001
                pass
        return label

    # ---------------------------------------------------------------- 界面
    def _build(self) -> None:
        o = self.owner
        card = self._c("card", "#ffffff")

        # ---- ① 标题（单独一行、跟着宽度换行，绝不用长标题顶宽整页）----
        title = self._label(
            self.parent,
            "全队标记：每个奖项一列，勾上 = 该队获这个奖（手改优先，随时可改）",
            fg=self._c("sub", "#4b5b70"), wraplength=_HINT_WRAP)
        title.pack(fill="x", pady=(10, 0))
        self._autowrap(title)

        # ---- ② 工具条：刷新 / 全部清空 / 按 * 自动勾选到 <奖项> ----
        bar = o._f(self.parent)
        bar.pack(fill="x", pady=(4, 0))
        self.refresh_btn = o._ghost_btn(bar, "重新识别 / 刷新", self.on_refresh)
        self.refresh_btn.pack(side="left")
        self._tip(self.refresh_btn,
                  "重读 contest.json，再用奖项表里现在填的奖项名 / 人数 / 自动规则\n"
                  "把名单与人数据实算一遍（和生成比赛包用的是同一个函数）。\n"
                  "手改过的行（界面上标「手动」）原样保留，不会被覆盖。")
        self.clear_btn = o._ghost_btn(bar, "全部清空", self.on_clear_all)
        self.clear_btn.pack(side="left", padx=(6, 0))
        self._tip(self.clear_btn,
                  "把所有奖项的名单清空（每个奖项变成「手选」空名单）——\n"
                  "清空后的行不会被「重新识别」自动填回去，方便你从头一个个勾。")
        self._label(bar, "按 * 自动勾选到", fg=self._c("hint", "#64748b")
                    ).pack(side="left", padx=(14, 4))
        self.star_combo = ttk.Combobox(bar, state="readonly", width=12, values=[])
        self.star_combo.pack(side="left")
        self._tip(self.star_combo,
                  "选一个奖项，然后点右边按钮：把队名/姓名前带 * 或 ＊ 的队\n"
                  "全部勾到这个奖项上（自动规则 = 星号）。")
        self.star_btn = o._ghost_btn(bar, "填星号", self.on_fill_star)
        self.star_btn.pack(side="left", padx=(4, 0))

        # ---- ③ 状态行（实时）----
        self.status_label = self._label(self.parent, "还没有读取比赛数据。", anchor="w",
                                        wraplength=_HINT_WRAP)
        self.status_label.pack(fill="x", pady=(6, 0), padx=(0, 4))
        self._autowrap(self.status_label)

        # ---- ④ 数据变化 / 警告行（有内容才出现）----
        self.warn_label = tk.Label(
            self.parent, text="", bg=self._c("warn_bg", "#fffaeb"),
            fg=self._c("warn", "#b45309"), font=(_FONT, 9),
            anchor="w", justify="left", wraplength=_HINT_WRAP, padx=8, pady=4)
        self._autowrap(self.warn_label)
        self._warn_packed = False

        # ---- ⑤ 矩阵（自带 Canvas + 滚动条）----
        wrap = o._f(self.parent)
        wrap.pack(fill="x", pady=(6, 0))
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(wrap, height=_CANVAS_H, width=560, bg=card,
                                highlightthickness=1, highlightbackground=card, bd=0)
        self.vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(wrap, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.table = tk.Frame(self.canvas, bg=card)
        self._table_window = self.canvas.create_window((0, 0), window=self.table, anchor="nw")
        self.table.bind("<Configure>", lambda _e: self._sync_scrollregion())
        self.canvas.bind("<Configure>", lambda _e: self._sync_scrollregion())
        self._built = True
        self.render_rows()

    # ------------------------------------------------------------ 滚动
    def _on_wheel(self, event):
        """滚轮只滚矩阵，**返回 "break"** 阻止整页跟着滚（页面用了 bind_all）。"""
        steps = 0
        try:
            steps = int(-int(getattr(event, "delta", 0)) / 120)
        except Exception:  # noqa: BLE001
            steps = 0
        if not steps:
            steps = -1 if int(getattr(event, "delta", 0)) > 0 else 1
        try:
            self.canvas.yview_scroll(steps, "units")
        except Exception:  # noqa: BLE001
            pass
        return "break"

    def _bind_wheel_tree(self, widget) -> None:
        try:
            widget.bind("<MouseWheel>", self._on_wheel)
            for child in widget.winfo_children():
                self._bind_wheel_tree(child)
        except Exception:  # noqa: BLE001
            pass

    def _sync_scrollregion(self) -> None:
        """矩阵内容宽 → 内层窗口宽；比画布宽才出横向滚动条（绝不撑爆页面宽度）。"""
        try:
            canvas_w = int(self.canvas.winfo_width())
            table_w = int(self.table.winfo_reqwidth())
            table_h = int(self.table.winfo_reqheight())
            canvas_h = int(self.canvas.winfo_height())
            target = max(table_w, canvas_w if canvas_w > 1 else 0)
            if target != self._win_w:
                self._win_w = target
                self.canvas.itemconfigure(self._table_window, width=target)
            self.canvas.configure(scrollregion=(0, 0, target, max(table_h, canvas_h)))
            need_hbar = canvas_w > 1 and table_w - canvas_w > 2
            if need_hbar and not self._hb_shown:
                self.hbar.grid(row=1, column=0, sticky="ew")
                self._hb_shown = True
            elif not need_hbar:
                if self._hb_shown:
                    self.hbar.grid_remove()
                    self._hb_shown = False
                try:
                    self.canvas.xview_moveto(0)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 编辑联动
    def track(self, record: dict[str, Any]) -> None:
        """给一个奖项行挂上「改名/改人数 → 立刻重算」的监听（只挂一次）。"""
        if id(record) in self._tracked:
            return
        self._tracked.add(id(record))
        for key in ("name", "count"):
            var = record.get(key)
            if var is None or not hasattr(var, "trace_add"):
                continue
            try:
                var.trace_add("write", lambda *_a: self._schedule_edit())
            except Exception:  # noqa: BLE001
                continue

    def track_all(self) -> None:
        for record in self._records():
            if isinstance(record, dict):
                self.track(record)

    def _schedule_edit(self, delay: int = _EDIT_DEBOUNCE_MS) -> None:
        """敲键盘 → 去抖后**只重算**（用缓存，不读文件）。"""
        if self._job is not None:
            try:
                self.owner.root.after_cancel(self._job)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._job = self.owner.root.after(delay, self.recompute_from_cache)
        except Exception:  # noqa: BLE001
            self._job = None
            self.recompute_from_cache()

    def notify_changed(self, delay: int = 0) -> None:
        """外部改了奖项表（加/删/批量设值）后调一下：重算人数与勾选状态。"""
        self._schedule_edit(delay)

    # ------------------------------------------------------------ 数据
    def contest_path(self):
        """当前要读的比赛数据：配置里的 ``last_contest_json``；它不存在时回落到
        工作根目录下的 ``contest.json``（那正是「拉取数据」的输出位置）。"""
        from pathlib import Path
        import board_config as cfgmod
        text = str(self.owner.cfg.get("last_contest_json") or "").strip()
        # 优先配置里那份（当前这场比赛）；它失效时再由 freshest 在两个候选里挑一个。
        if text:
            path = Path(text)
            if path.is_file():
                return path
        return cfgmod.freshest_contest(None)

    def _records(self) -> list[dict[str, Any]]:
        """奖项表里现在的行（M17：优先取本编辑器自己的行，剔除异常项）。"""
        source = None
        editor = getattr(self, "editor", None)
        if editor is not None:
            source = getattr(editor, "award_rows", None)
        if source is None:
            source = getattr(self.owner, "award_rows", None)
        out: list[dict[str, Any]] = []
        for record in (source or []):
            if isinstance(record, dict):
                out.append(record)
        return out

    def _table_awards(self) -> list[dict[str, Any]]:
        """当前表里填的奖项（M17：优先用本编辑器的 collect_awards，语义与生成包一致）。"""
        editor = getattr(self, "editor", None)
        getter = getattr(editor, "collect_awards", None) if editor is not None else None
        if not callable(getter):
            getter = getattr(self.owner, "collect_awards", None)
        try:
            if callable(getter):
                rows = list(getter() or [])
                if rows:
                    return rows
        except Exception:  # noqa: BLE001
            pass
        return list(self.owner.cfg.get("awards") or [])

    def _penalty(self) -> int:
        try:
            return int(self.owner.cfg.get("penalty_minutes") or 20)
        except (TypeError, ValueError):
            return 20

    def has_data(self) -> bool:
        """有没有缓存住比赛数据（有 → 重算不必再读 565 KB 的 contest.json）。"""
        return self._contest is not None

    # ------------------------------------------------------------ 刷新
    def refresh(self, quiet: bool = False) -> None:
        """重读 ``contest.json`` + 当前表值 → 重算矩阵（「重新识别 / 刷新」按钮）。"""
        import board_backend as backend
        import board_config as cfgmod
        path = self.contest_path()
        self._contest_path = str(path)
        self._load_error = ""
        self._diff = {}
        if not path.is_file():
            self._contest = None
            self._load_error = (f"读不到比赛数据（{path}）—— 先去「数据源」页点「拉取数据」，"
                                "再回来点「重新识别 / 刷新」。")
            self.rows, self.awards, self.warnings = [], [], [self._load_error]
            self.total_teams, self.unassigned, self.picked_total = 0, 0, 0
            self._render()
            return
        try:
            _fetch, _xmlmod, common = backend.load_backend(cfgmod.resource_root())
            self._contest = common.load_contest(path)
        except Exception as exc:  # noqa: BLE001
            self._contest = None
            self._load_error = f"读取比赛数据失败：{type(exc).__name__}: {exc}"
            self.rows, self.awards, self.warnings = [], [], [self._load_error]
            self._render()
            return
        self._recompute(quiet=quiet, source="file")

    def recompute_from_cache(self) -> None:
        """不读文件，用缓存里的 contest 重算（敲键盘走这条路）。"""
        self._job = None
        if self._contest is None:
            return
        self._recompute(quiet=True, source="cache")

    def _recompute(self, quiet: bool = False, source: str = "cache") -> None:
        import board_backend as backend
        import board_config as cfgmod

        awards_in = self._table_awards()
        preview = backend.award_rows_preview_from_contest(
            self._contest, awards_in, self._penalty())
        self._preview = preview
        self.rows = list(preview.get("rows") or [])
        self.awards = list(preview.get("awards") or [])
        self.warnings = list(preview.get("warnings") or [])
        self.mode = str(preview.get("mode") or "rows")
        self.total_teams = int(preview.get("total_teams") or 0)
        self.unassigned = int(preview.get("unassigned") or 0)
        self.picked_total = sum(len(a.get("team_ids") or []) for a in self.awards)

        # ① 自动行：把算出来的名单**写回**奖项行（界面「已选 N」与包内一致）。
        #    手动行一个字都不动 —— 找不到的 id 要一直留在名单里，警告才不会消失。
        self._write_back()

        # ② 数据变化（新增/消失/改名）：先比较上一次的快照，再写新快照。
        teams = (self._contest or {}).get("teams") or []
        try:
            self._diff = cfgmod.diff_team_snapshot(self.owner.cfg.get("award_team_snapshot"), teams)
            self.owner.cfg["award_team_snapshot"] = cfgmod.snapshot_teams(teams)
        except Exception:  # noqa: BLE001
            self._diff = {}

        # ③ 先渲染，再记下这次看到的队名。
        #    ⚠ 顺序很重要：`change_lines()` 里的「改名了」是拿**上一轮记下的**队名
        #    跟这次数据比出来的；如果先把 names 更新掉，就永远比不出改名了。
        self._render()
        self._sync_names()

        if not quiet:
            extra = len([w for w in self.warnings])
            self._log(f"[奖项] 重新识别：读到 {self.total_teams} 支队，"
                      f"{len(self.awards)} 个奖项，已勾选 {self.picked_total} 队 / "
                      f"未获奖 {self.unassigned} 队。"
                      + (f" 警告 {extra} 条（见上面的橙条）。" if extra else "")
                      + ("（用缓存重算，没重读文件）" if source == "cache" else "") + "\n")

    def _write_back(self) -> None:
        """把预览里算出来的名单同步到奖项行（只动非手动的行）。"""
        records = self._records()
        for idx, record in enumerate(records):
            if idx >= len(self.awards):
                break
            row = self.awards[idx]
            if bool(record.get("manual")):
                continue                     # 手改优先：原样保留
            record["team_ids"] = [str(x) for x in (row.get("team_ids") or [])]
        try:
            hook = getattr(self.owner, "_sync_award_row_status", None)
            if callable(hook):
                hook()
        except Exception:  # noqa: BLE001
            pass
        # M17：重算会把算出来的名单写回行 → 让 owner（= 本矩阵所属的奖项表编辑器）
        # 顺手把 cfg["awards"] 也刷成一样（纯内存），「一份数据」不会慢半拍。
        try:
            hook = getattr(self.owner, "_awards_rows_recomputed", None)
            if callable(hook):
                hook()
        except Exception:  # noqa: BLE001
            pass

    def _sync_names(self) -> None:
        """把「这次看到的队名」记进每一行的 ``names``（下次刷新用来报改名）。"""
        by_id = {str(t.get("team_id")): str(t.get("name") or "") for t in self.rows}
        for record in self._records():
            names = record.get("names")
            if not isinstance(names, dict):
                names = {}
                record["names"] = names
            for tid in (record.get("team_ids") or []):
                tid = str(tid)
                if tid in by_id:
                    names[tid] = by_id[tid]

    # ------------------------------------------------------------ 渲染
    def _render(self) -> None:
        if not self._built:
            return
        self._set_status_text()
        self._set_warn_text()
        self._refresh_star_choices()
        self.render_rows()

    # ---- 状态行 -------------------------------------------------------
    def covered_ids(self) -> list[str]:
        """现在被任何奖项勾上的队（按数据里真有的队算）。"""
        known = {str(t.get("team_id")) for t in self.rows}
        seen: list[str] = []
        for record in self._records():
            for tid in (record.get("team_ids") or []):
                tid = str(tid)
                if tid in known and tid not in seen:
                    seen.append(tid)
        return seen

    def status_text(self) -> str:
        if self._load_error:
            return self._load_error
        if not self.rows:
            return "还没有读取比赛数据。"
        covered = len(self.covered_ids())
        total = self.total_teams or len(self.rows)
        return (f"已勾选 {covered} 队 · 未获奖 {max(0, total - covered)} 队 / 共 {total} 队")

    def _set_status_text(self) -> None:
        try:
            self.status_label.configure(text=self.status_text())
        except Exception:  # noqa: BLE001
            pass

    # ---- 数据变化 / 警告行 ---------------------------------------------
    def _live_names(self) -> dict[str, str]:
        """当前数据里 ``队伍ID → 队名``。"""
        return {str(t.get("team_id")): str(t.get("name") or "") for t in self.rows}

    def _missing_pairs(self) -> list[str]:
        """「本次数据里找不到」的队，**按奖项行现在的名单实时算**。

        条目形如 ``<队伍ID>（上次是「<队名>」）``（没记过队名就只给 ID）；同一支队在
        多个奖项里只算一次。实时算的好处：用户到「全队标记」里把找不到的队取消勾选，
        这条提示**当场**消失，不用再点一次刷新。
        """
        by_id = self._live_names()
        if not by_id:
            return []
        out: list[str] = []
        for record in self._records():
            names = record.get("names") if isinstance(record.get("names"), dict) else {}
            for raw in (record.get("team_ids") or []):
                tid = str(raw)
                if tid in by_id or any(str(item).split("（")[0] == tid for item in out):
                    continue
                old = str(names.get(tid) or "")
                out.append(f"{tid}（上次是「{old}」）" if old else tid)
        return out

    def _renamed_pairs(self) -> list[tuple[str, str, str]]:
        """改名了的队（``names`` 里上次看到的队名 ≠ 本次数据里的队名），同样实时算。"""
        by_id = self._live_names()
        if not by_id:
            return []
        out: list[tuple[str, str, str]] = []
        for record in self._records():
            names = record.get("names") if isinstance(record.get("names"), dict) else {}
            for raw in (record.get("team_ids") or []):
                tid = str(raw)
                was = str(names.get(tid) or "")
                if tid in by_id and was and was != by_id[tid]:
                    key = (tid, was, by_id[tid])
                    if key not in out:
                        out.append(key)
        return out

    def change_lines(self) -> list[str]:
        """状态行下面的提示（数据变化 / 找不到 / 改名 / 其它警告），没有就空列表。"""
        lines: list[str] = []
        if self._load_error:
            return ["⚠ " + self._load_error]

        import board_config as cfgmod
        hint = ""
        try:
            hint = cfgmod.snapshot_hint_text(self._diff)
        except Exception:  # noqa: BLE001
            hint = ""
        if hint:
            lines.append(hint)

        missing = self._missing_pairs()
        if missing:
            shown = "、".join(missing[:4])
            more = "" if len(missing) <= 4 else f" 等 {len(missing)} 支"
            lines.append(f"⚠ 有 {len(missing)} 支已勾选的队在本次数据里找不到：{shown}{more}")

        renamed = self._renamed_pairs()
        if renamed:
            # 快照提示里已经报过同一批改名（数据层面）就不重复说一遍；
            # 没有上一次快照时（has_prev=False）这里就是唯一会说「改名了」的地方。
            known = {str(item.get("id")) for item in (self._diff.get("renamed") or [])
                     if isinstance(item, dict)}
            shown_pairs = [item for item in renamed if item[0] not in known]
            if shown_pairs:
                shown = "、".join(f"「{was}」→「{now}」" for _tid, was, now in shown_pairs[:3])
                more = "" if len(shown_pairs) <= 3 else f" 等 {len(shown_pairs)} 支"
                lines.append(f"⚠ 有 {len(shown_pairs)} 支队改名了：{shown}{more}")

        # 其余警告（空名单 / 人数为 0 / 榜单没人了…）—— 已经说过的（找不到/改名）不重复
        others = [str(w) for w in self.warnings
                  if "找不到" not in str(w) and "改名" not in str(w)
                  and "不影响滚榜" not in str(w)]
        if others:
            lines.append("⚠ " + "；".join(others[:3])
                         + (f"（还有 {len(others) - 3} 条，见日志）" if len(others) > 3 else ""))
        return lines

    def warning_text(self) -> str:
        """兼容旧证据脚本：取警告行的文字。"""
        try:
            return str(self.warn_label.cget("text") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _set_warn_text(self) -> None:
        message = "\n".join(self.change_lines())
        try:
            self.warn_label.configure(text=message)
        except Exception:  # noqa: BLE001
            return
        try:
            if message and not self._warn_packed:
                self.warn_label.pack(fill="x", pady=(4, 0), padx=(0, 4))
                self._warn_packed = True
            elif not message and self._warn_packed:
                self.warn_label.pack_forget()
                self._warn_packed = False
        except Exception:  # noqa: BLE001
            pass

    # ---- 兼容 M14 证据脚本（award_check.py 用 __new__ 造对象后调它）----
    def counts_text(self) -> str:
        """「一等奖 1/1 ✓ ｜ 三等奖 3/5 ⚠ 榜单只剩 3 队」这样的一行对照。"""
        awards = getattr(self, "awards", None) or []
        load_error = getattr(self, "_load_error", "")
        rows = getattr(self, "rows", None) or []
        unassigned = int(getattr(self, "unassigned", 0) or 0)
        total_teams = int(getattr(self, "total_teams", 0) or 0)
        if not awards:
            if load_error:
                return load_error
            if not rows:
                return "还没有读取比赛数据。"
            return "奖项表是空的：不会写出任何奖项。"
        parts: list[str] = []
        for award in awards:
            name = award.get("name") or "（无名）"
            count = int(award.get("count") or 0)
            picked = int(award.get("picked") or 0)
            if count <= 0:
                parts.append(f"{name} 0/0 ⚠ {award.get('issue') or '人数不是正整数'}")
            elif award.get("missing") and award.get("skipped"):
                parts.append(f"{name} {picked}/{count} ⚠ {award.get('issue') or '一队都没分到'}")
            elif award.get("short"):
                parts.append(f"{name} {picked}/{count} ⚠ 榜单只剩 {picked} 队")
            elif award.get("issue"):
                parts.append(f"{name} {picked}/{count} ⚠ {award['issue']}")
            else:
                parts.append(f"{name} {picked}/{count} ✓")
        parts.append(f"未获奖 {unassigned} 队 / 共 {total_teams} 队")
        return " ｜ ".join(parts)

    # ---- 矩阵 ---------------------------------------------------------
    def award_columns(self) -> list[dict[str, Any]]:
        """每个奖项一列：{index, name, picked, source, width}。"""
        out: list[dict[str, Any]] = []
        records = self._records()
        for idx, record in enumerate(records):
            name = ""
            var = record.get("name")
            try:
                name = str(var.get()).strip() if var is not None else ""
            except Exception:  # noqa: BLE001
                name = ""
            name = name or f"奖项 {idx + 1}"
            ids = [str(x) for x in (record.get("team_ids") or [])]
            units = max(text_units(elide(name, _HEAD_NAME_UNITS)), 7)
            out.append({
                "index": idx,
                "name": name,
                "picked": len(ids),
                "source": self.row_source_text(record),
                "width": max(_COL_AWARD_MIN, min(_COL_AWARD_MAX, 14 + 7 * units)),
            })
        return out

    @staticmethod
    def row_source_text(record: dict[str, Any]) -> str:
        import board_config as cfgmod
        if record.get("manual"):
            return "手动"
        return cfgmod.AWARD_AUTO_LABELS.get(str(record.get("auto") or ""), "手选")

    def _refresh_star_choices(self) -> None:
        names = [col["name"] for col in self.award_columns()]
        try:
            values = list(self.star_combo.cget("values"))
        except Exception:  # noqa: BLE001
            values = []
        if values != names:
            current = ""
            try:
                current = str(self.star_combo.get() or "")
            except Exception:  # noqa: BLE001
                current = ""
            try:
                self.star_combo.configure(values=names)
                if current in names:
                    self.star_combo.set(current)
                elif names:
                    self.star_combo.current(0 if len(names) > 1 else 0)
            except Exception:  # noqa: BLE001
                pass

    def star_target_index(self) -> int:
        """工具条下拉框选中的奖项（序号）。没得选时返回 -1。"""
        names = [col["name"] for col in self.award_columns()]
        if not names:
            return -1
        text = ""
        try:
            text = str(self.star_combo.get() or "")
        except Exception:  # noqa: BLE001
            text = ""
        if text in names:
            return names.index(text)
        try:
            idx = int(self.star_combo.current())
        except Exception:  # noqa: BLE001
            idx = -1
        return idx if 0 <= idx < len(names) else 0

    def render_rows(self) -> None:
        if not self._built:
            return
        records = self._records()
        cols = self.award_columns()
        sig = (tuple(str(t.get("team_id") or "") for t in self.rows), len(cols))
        if sig != self._row_sig:
            self._rebuild_table(cols)
            self._row_sig = sig
        self._update_table(cols)
        self._safe(self._bind_wheel_tree, self.table)
        self._sync_scrollregion()

    @staticmethod
    def _safe(fn, *args, **kw) -> None:
        try:
            fn(*args, **kw)
        except Exception:  # noqa: BLE001
            pass

    def _fixed_cell(self, parent, width: int, bg: str, height: int = _ROW_H) -> tk.Frame:
        """固定像素宽的单元格（``pack_propagate(False)`` → 内容绝不撑宽整页）。"""
        cell = tk.Frame(parent, width=width, height=height, bg=bg)
        cell.pack(side="left")
        cell.pack_propagate(False)
        return cell

    def _rebuild_table(self, cols: list[dict[str, Any]]) -> None:
        card = self._c("card", "#ffffff")
        head_bg = self._c("head", "#f4f7fb")
        border = self._c("border", "#d9e2ec")
        for child in self.table.winfo_children():
            child.destroy()
        self._cells = {}
        self._cell_vars = {}
        self._row_refs = []
        self._head_refs = []
        self._win_w = None

        if not self.rows:
            hint = self._load_error or (
                "还没有队伍数据：点上面的「重新识别 / 刷新」读一次 contest.json"
                "（还没拉过数据就去「数据源」页点「拉取数据」）。")
            self._label(self.table, hint, fg=self._c("hint", "#64748b"),
                        wraplength=_HINT_WRAP).pack(anchor="w", padx=8, pady=8)
            return

        # ---- 表头 ----
        head = tk.Frame(self.table, bg=head_bg)
        head.pack(fill="x")
        hint_fg = self._c("hint", "#64748b")
        for text, width in (("名次", _COL_RANK), ("队名", _COL_TEAM),
                            ("学号", _COL_NUMBER), ("打星", _COL_STAR)):
            cell = self._fixed_cell(head, width, head_bg, _HEAD_H)
            self._label(cell, text, fg=hint_fg, bg=head_bg).pack(anchor="w", padx=6, pady=8)
        for col in cols:
            cell = self._fixed_cell(head, col["width"], head_bg, _HEAD_H)
            top = self._label(cell, elide(col["name"], _HEAD_NAME_UNITS),
                              fg=self._c("ink", "#16202e"), bg=head_bg)
            top.pack(anchor="center", pady=(2, 0))
            bottom = self._label(cell, f"已选 {col['picked']}", fg=hint_fg, bg=head_bg)
            bottom.pack(anchor="center")
            self._head_refs.append({"name": top, "count": bottom,
                                    "source": col.get("source") or ""})
        cell = self._fixed_cell(head, _COL_SOURCE, head_bg, _HEAD_H)
        self._label(cell, "获奖来源", fg=hint_fg, bg=head_bg).pack(anchor="w", padx=6, pady=8)
        tk.Frame(self.table, height=1, bg=border).pack(fill="x")

        # ---- 每一支队 ----
        accent = self._c("accent", "#2563eb")
        ink = self._c("ink", "#16202e")
        sub = self._c("sub", "#4b5b70")
        for team in self.rows:
            tid = str(team.get("team_id") or "")
            line = tk.Frame(self.table, bg=card)
            line.pack(fill="x")
            ref: dict[str, Any] = {"team_id": tid, "cells": []}
            cell = self._fixed_cell(line, _COL_RANK, card)
            ref["rank"] = self._label(cell, str(team.get("rank") or ""), fg=sub, bg=card)
            ref["rank"].pack(anchor="w", padx=6)
            cell = self._fixed_cell(line, _COL_TEAM, card)
            star = has_star(team.get("name"))
            ref["name"] = self._label(cell, elide(team.get("name") or "（无队名）", 24),
                                      fg=accent if star else ink, bg=card)
            ref["name"].pack(anchor="w", padx=6)
            ref["star"] = star
            cell = self._fixed_cell(line, _COL_NUMBER, card)
            ref["number"] = self._label(cell, str(team.get("student_number") or ""),
                                        fg=self._c("hint", "#64748b"), bg=card)
            ref["number"].pack(anchor="w", padx=6)
            # M39（用户 2026-10-18）：打星勾选（手动，不做任何自动判断）。
            #   勾上 = 榜单上显示黄色六边形+星；不影响排名、罚时、奖项。
            cell = self._fixed_cell(line, _COL_STAR, card)
            svar = tk.BooleanVar(value=tid in self._star_set())
            scb = tk.Checkbutton(
                cell, variable=svar, command=lambda t=tid: self._on_star_toggle(t),
                bg=card, activebackground=card, selectcolor="white",
                bd=0, highlightthickness=0, cursor="hand2", takefocus=0)
            scb.pack(expand=True)
            ref["star_cb"] = scb
            ref["star_var"] = svar
            for col in cols:
                cell = self._fixed_cell(line, col["width"], card)
                var = tk.BooleanVar(value=self._checked(col["index"], tid))
                cb = tk.Checkbutton(
                    cell, variable=var, command=lambda a=col["index"], t=tid: self._on_toggle(a, t),
                    bg=card, activebackground=card, selectcolor="white",
                    bd=0, highlightthickness=0, cursor="hand2", takefocus=0)
                cb.pack(expand=True)
                self._cells[(col["index"], tid)] = cb
                self._cell_vars[(col["index"], tid)] = var
                ref["cells"].append(cb)
            cell = self._fixed_cell(line, _COL_SOURCE, card)
            ref["source"] = self._label(cell, "", fg=sub, bg=card)
            ref["source"].pack(anchor="w", padx=6)
            self._row_refs.append(ref)

    def _update_table(self, cols: list[dict[str, Any]]) -> None:
        accent = self._c("accent", "#2563eb")
        ink = self._c("ink", "#16202e")
        hint_fg = self._c("hint", "#64748b")
        # 表头：奖项名 + 已选 N
        for idx, col in enumerate(cols):
            if idx >= len(self._head_refs):
                continue
            ref = self._head_refs[idx]
            self._safe(ref["name"].configure, text=elide(col["name"], _HEAD_NAME_UNITS))
            self._safe(ref["count"].configure, text=f"已选 {col['picked']}")
        # 勾选框状态 + 队名/学号（数据可能变了）
        team_by_id = {str(t.get("team_id") or ""): t for t in self.rows}
        for ref in self._row_refs:
            tid = ref["team_id"]
            team = team_by_id.get(tid) or {}
            try:
                ref["rank"].configure(text=str(team.get("rank") or ""))
                name = str(team.get("name") or "")
                star = has_star(name)
                ref["name"].configure(text=elide(name or "（无队名）", 24),
                                      fg=accent if star else ink)
                ref["star"] = star
                ref["number"].configure(text=str(team.get("student_number") or ""))
                if "star_var" in ref:
                    want_star = tid in self._star_set()
                    if bool(ref["star_var"].get()) != want_star:
                        ref["star_var"].set(want_star)
            except Exception:  # noqa: BLE001
                pass
            hits: list[str] = []
            for idx, col in enumerate(cols):
                if idx >= len(ref["cells"]):
                    continue
                var = self._cell_vars.get((col["index"], tid))
                want = self._checked(col["index"], tid)
                if var is not None:
                    try:
                        if bool(var.get()) != want:
                            var.set(want)
                    except Exception:  # noqa: BLE001
                        pass
                if want and col["name"] not in hits:
                    hits.append(col["name"])
            try:
                text = "、".join(hits[:2]) + ("…" if len(hits) > 2 else "")
                ref["source"].configure(text=elide(text, 18) or "—",
                                        fg=hint_fg if not hits else accent)
            except Exception:  # noqa: BLE001
                pass

    # ---- M39：打星名单（手动勾选） --------------------------------------
    def _star_ids(self) -> list[str]:
        """现在被标成「打星」的 team_id（配置里 view.star_team_ids / star_team_ids）。"""
        cfg = getattr(self.owner, "cfg", None) or {}
        raw = cfg.get("star_team_ids")
        if raw is None:
            raw = (cfg.get("view") or {}).get("star_team_ids")
        return [str(x) for x in (raw or []) if str(x).strip()]

    def _star_set(self) -> set:
        return set(self._star_ids())

    def _on_star_toggle(self, team_id: str) -> None:
        """勾/取消「打星」：写配置 → 保存 → 推给实时运行目录（榜单下次请求即生效）。"""
        ids = self._star_ids()
        if team_id in ids:
            ids = [x for x in ids if x != team_id]
        else:
            ids.append(team_id)
        cfg = getattr(self.owner, "cfg", None)
        if isinstance(cfg, dict):
            cfg["star_team_ids"] = ids
            view = cfg.get("view")
            if not isinstance(view, dict):
                view = {}
                cfg["view"] = view
            view["star_team_ids"] = ids
        persist = getattr(self.owner, "_persist", None)
        if callable(persist):
            try:
                persist()
            except Exception:  # noqa: BLE001
                pass
        try:
            import live_backend as live
            import board_config as cfgmod
            run = live.run_dir(cfgmod.work_root())
            live.update_live_view(run, {"star_team_ids": ids})
        except Exception:  # noqa: BLE001
            pass
        self._set_status_text()

    def _checked(self, award_index: int, team_id: str) -> bool:
        records = self._records()
        if not (0 <= award_index < len(records)):
            return False
        return str(team_id) in {str(x) for x in (records[award_index].get("team_ids") or [])}

    # ---- 勾选 ---------------------------------------------------------
    def _on_toggle(self, award_index: int, team_id: str) -> None:
        """勾选框被点：这一行立刻变「手动」并写回 ``cfg["awards"]``（手改优先）。"""
        records = self._records()
        if not (0 <= award_index < len(records)):
            return
        record = records[award_index]
        var = self._cell_vars.get((award_index, team_id))
        want = bool(var.get()) if var is not None else False
        ids = [str(x) for x in (record.get("team_ids") or [])]
        if want:
            if str(team_id) not in ids:
                ids.append(str(team_id))
        else:
            ids = [x for x in ids if x != str(team_id)]
        record["team_ids"] = ids
        record["manual"] = True                 # 手改优先：重新识别不会覆盖
        record["auto"] = ""                     # 纯手选
        names = record.get("names")
        if not isinstance(names, dict):
            names = {}
            record["names"] = names
        current = self._team_name(team_id)
        if current:
            names[str(team_id)] = current
        self._push_state()
        # 勾一下不用重读 contest.json：有缓存的比赛数据就**用缓存重算一遍**
        # （实测 32 队 / 621 提交 ≈ 1.2 ms），这样表头、状态行、「找不到 / 改名」
        # 提示全都是最新的；连数据都还没读到时，只按勾选刷一遍界面。
        if self._contest is None:
            self._local_render()
        else:
            self.recompute_from_cache()

    def _team_name(self, team_id: str) -> str:
        for team in self.rows:
            if str(team.get("team_id")) == str(team_id):
                return str(team.get("name") or "")
        return ""

    def _push_state(self) -> None:
        """把奖项行的最新状态写回 ``cfg["awards"]``（生成包/指纹都用它）。

        M17：属于某个编辑器时改走 ``editor.push_state()`` —— 除了写 cfg，
        还要把**另一个编辑器**（滚榜 / 实时）的界面刷成同一份数据。
        """
        editor = getattr(self, "editor", None)
        if editor is not None:
            try:
                editor.push_state()
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            collect = getattr(self.owner, "collect_awards", None)
            if callable(collect):
                self.owner.cfg["awards"] = collect()
        except Exception:  # noqa: BLE001
            pass
        try:
            hook = getattr(self.owner, "_sync_award_row_status", None)
            if callable(hook):
                hook()
        except Exception:  # noqa: BLE001
            pass

    def _local_render(self) -> None:
        """不重算名单，只按当前勾选把界面刷新一遍（点勾选框走这条路）。"""
        self._set_status_text()
        self._set_warn_text()
        cols = self.award_columns()
        self._update_table(cols)
        self._sync_scrollregion()

    # ------------------------------------------------------------ 事件
    def on_refresh(self) -> None:
        """按钮：走 owner 的 ``refresh_award_state``（读表值 → 重算 → 写回 cfg）。"""
        hook = getattr(self.owner, "refresh_award_state", None)
        if callable(hook):
            hook(quiet=False)
        else:  # 兜底：owner 没有那个方法时也要能用
            self.refresh(quiet=False)

    def on_fill_star(self) -> None:
        """「按 * 自动勾选到 <奖项>」：把这一行设成 ``auto="star"`` 并重算。"""
        idx = self.star_target_index()
        if idx < 0:
            return
        self.set_star_target(idx)

    def set_star_target(self, award_index: int) -> bool:
        """把第 ``award_index`` 个奖项设成「自动（星号）」并立刻重算。"""
        records = self._records()
        if not (0 <= award_index < len(records)):
            return False
        record = records[award_index]
        record["auto"] = "star"
        record["manual"] = False
        record["team_ids"] = []          # 交给「重新识别」按 * 重算
        self._push_state()
        hook = getattr(self.owner, "refresh_award_state", None)
        if callable(hook):
            hook(quiet=True)
        else:
            self.refresh(quiet=True)
        return True

    def on_clear_all(self) -> None:
        """「全部清空」：所有奖项 → 手选空名单（不会被重新识别填回去）。"""
        for record in self._records():
            record["team_ids"] = []
            record["manual"] = True
            record["auto"] = ""
        self._push_state()
        hook = getattr(self.owner, "refresh_award_state", None)
        if callable(hook):
            hook(quiet=False)
        else:
            self.refresh(quiet=False)

    # ------------------------------------------------------------ 自测用
    def checkbox_count(self) -> int:
        return len(self._cells)

    def cells(self) -> list[tuple[int, str, tk.Checkbutton]]:
        return [(idx, tid, cb) for (idx, tid), cb in self._cells.items()]

    def award_header_texts(self) -> list[str]:
        out: list[str] = []
        try:
            for ref in self._head_refs:
                out.append(str(ref["name"].cget("text") or ""))
        except Exception:  # noqa: BLE001
            pass
        return out

    def toggle_cell(self, award_index: int, team_id: str, value: bool | None = None) -> bool:
        """自测用：模拟点一下某个勾选框（``value=None`` = 反过来）。"""
        var = self._cell_vars.get((award_index, str(team_id)))
        if var is None:
            return False
        var.set(not bool(var.get()) if value is None else bool(value))
        self._on_toggle(award_index, str(team_id))
        return True

    def _log(self, text: str) -> None:
        try:
            self.owner._log(text)
        except Exception:  # noqa: BLE001
            pass
