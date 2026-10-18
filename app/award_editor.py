# -*- coding: utf-8 -*-
"""award_editor.py —— 奖项表整卡（M17 双编辑器 / M17b compact）。

一个 :class:`AwardEditor` = 一张完整的奖项表卡片内容。

**完整版（``compact=False``，滚榜侧）**::

    奖项设置 ── 第一行：奖项名 / 共 N 队 / 按名次填 / 清空 / 删除
               └─ 第二行：展示方式下拉框
               └─ 第三行：已选 N 支 · 来源
    ＋ 添加奖项 + 说明
    自动加一血奖 / 同时按金/银/铜统计（两个勾选）
    全队标记矩阵（``rank_awards_panel.RankAwardsPanel``：每个奖项一列勾选框）

**精简版（``compact=True``，实时榜单侧）** —— 用户口径：「实时榜不用改勾选了，
只用改金银铜对应的人数就行了」::

    奖项设置 ── 第一行：奖项名 / 共 N 队 / 删除
               └─ 第二行：展示方式下拉框（榜单上「停下显示详情 / 暂停」是实时侧的事）
               └─ 第三行：名次区间提示（第 1–2 名上金 / 第 3–4 名上银 / 第 5–7 名上铜）
    ＋ 添加奖项 + 说明

    **没有**：全队标记矩阵 / 勾选列 / 队伍名单 / 「已选 N 支」/「名次填」/「清空」/
    「重新识别 / 全部清空 / 按 * 自动勾选」/「自动加一血奖 / 金 银 铜统计」两个勾选。

M17 起同一套实现在界面上有**两个实例**（``BoardApp.award_editors``）：

* ``"roll"`` —— 滚榜模式「2 外观」页（**主表**：``_package_settings()`` /
  ``run_package`` 都从它取）。整卡 = 奖项表 + 全队标记矩阵；
* ``"live"`` —— 实时榜单模式「2 外观」页，``compact=True``：只有奖项名 / 人数 /
  展示方式 + 删除 + 名次区间提示。**名单（``team_ids``）只在滚榜侧勾。**

两个实例共用**同一份**数据 ``cfg["awards"]``，不存在第二份副本：

* 任一侧改动 → :meth:`AwardEditor._notify_changed`
  → ``BoardApp._on_awards_changed(key)`` → 写回 ``cfg["awards"]``
  + 把**另一侧**按同一份数据重画（``set_rows``，防递归）；
* 改名 / 改人数走 150 ms 去抖（和矩阵重算同一套节奏，避免每敲一个字符就落盘）；
  改展示方式 / 点勾选框 / 「按名次填」「清空」「删除」「＋ 添加奖项」**立即**同步；
* compact 侧**不拥有**名单：它的 record 里照样带 ``team_ids / auto / manual /
  names``（只是不显示），主表那边每算出一份新名单（勾选 / 重新识别 / 去抖重算）
  都会主动喂给 compact 侧（见 :meth:`AwardEditor._awards_rows_recomputed`），
  所以实时侧改名字 / 人数时带出去的 ``team_ids`` 就是滚榜侧那一份，一个都不会丢。

实现说明
--------
* 控件、文案、记录键（``frame/name/count/show/team_ids/auto/manual/names/status``）
  与原来 ``BoardApp._build_award_table_ui`` 完全一致 —— 这次只是把整块**搬**过来；
* 界面小工具（``_f/_t/_entry/_ghost_btn/_hint/_form_row/_indent_row``）与
  ``C / cfg / root / _log`` 仍由 ``BoardApp`` 提供，这里用 ``__getattr__`` 透传；
  于是 ``AwardEditor`` 自己就能当 :class:`RankAwardsPanel` 的 owner ——
  矩阵按**本编辑器自己的行集合**算，不再看全局的 ``award_rows``。
"""

from __future__ import annotations

from typing import Any

import tkinter as tk
from tkinter import ttk

import board_config as cfgmod
from rank_awards_panel import RankAwardsPanel

#: 与 ``pta_board_gui.UI_FONT`` 同一个值。这里单独定义是为了避开循环导入
#: （``pta_board_gui`` 在本模块导入时还没执行到 UI_FONT 那一行）。
_FONT = "Microsoft YaHei UI"

#: 改名 / 改人数的同步去抖（毫秒）—— 和 ``rank_awards_panel._EDIT_DEBOUNCE_MS`` 同节奏。
_TEXT_SYNC_MS = 150

#: 榜单前三个奖项上色：第 1 个奖 = 金、第 2 个 = 银、第 3 个 = 铜，
#: 第 4 个往后不上色（与 ``board_config.live_view_awards`` / 榜单页的着色规则一致）。
_MEDALS = ("金", "银", "铜")



def coerce_award_row(row: Any) -> dict[str, Any]:
    """把一份奖项数据**原样**搬成记录用的形状（**不改语义**）。

    与 :func:`board_config.clean_award_row` 的区别：这里只做类型收敛，
    不套用「有名单但没来源 → 当好手选」「只有 count 没名单 → auto=rank」这类
    老配置升级规则。``collect_awards()`` 的输出经过这里再进 ``set_rows()``，
    另一个编辑器收集出来的行就会**逐项相同**（双向同步的界面证据靠这个）。
    """
    row = row if isinstance(row, dict) else {}
    name = str(row.get("name") or "").strip()
    try:
        count = max(0, int(str(row.get("count")).strip()))
    except (TypeError, ValueError):
        count = 0
    show = str(row.get("show") or "detail").strip().lower()
    if show not in cfgmod.AWARD_SHOW_LABELS:
        show = "detail"
    ids: list[str] = []
    for value in (row.get("team_ids") or []):
        text = str(value).strip()
        if text and text not in ids:
            ids.append(text)
    auto = str(row.get("auto") or "").strip().lower()
    if auto not in cfgmod.AWARD_AUTO_RULES:
        auto = ""
    raw_names = row.get("names")
    names = ({str(k): str(v) for k, v in raw_names.items()}
             if isinstance(raw_names, dict) else {})
    return {"name": name, "show": show, "count": count, "team_ids": ids,
            "auto": auto, "manual": bool(row.get("manual")), "names": names}


class AwardEditor:
    """一张奖项表（``compact=False`` 时还带全队标记矩阵）。``app`` = ``pta_board_gui.BoardApp``。"""

    #: 「展示方式」文案 → 键（与 ``board_config.AWARD_SHOW_LABELS`` 反向）
    _SHOW_LABEL_TO_KEY = {v: k for k, v in cfgmod.AWARD_SHOW_LABELS.items()}

    def __init__(self, app: Any, key: str, body: Any, hint_wrap: int = 0,
                 compact: bool = False) -> None:
        self.app = app
        self.key = str(key)
        self.body = body
        self.hint_wrap = int(hint_wrap or 0)
        #: M17b：``True`` = 精简版（实时榜单侧）：只有奖项名 / 人数 / 展示方式 + 删除，
        #: 一行「名次区间提示」，**不建**矩阵、没有勾选与名单相关按钮。
        self.compact = bool(compact)

        # ---- 界面控件（名字与 M15 完全一致，外面靠 property 兼容）----
        self.awards_box: Any = None
        self.award_rows: list[dict] = []
        self.award_rows_holder: Any = None
        self.fts_var: Any = None
        self.award_medal_var: Any = None
        #: 矩阵面板；compact 下**恒为 None**（属性访问不抛异常）
        self.panel: RankAwardsPanel | None = None

        # ---- 同步状态 ----
        self._bulk = False          # 整表重画（set_rows）期间不广播「我改了」
        self._sync_job: Any = None  # 名字/人数去抖用的 after id
        self._destroyed = False

        self._build_ui()

        # 初始内容：直接吃 ``cfg["awards"]`` 这一份（滚榜页随后会被
        # ``_apply_config`` 再用同一个值设一遍，结果一致）。
        was_bulk = self._bulk
        self._bulk = True
        try:
            self.set_rows(list(self.app.cfg.get("awards") or []))
        finally:
            self._bulk = was_bulk

    # ---------------------------------------------------------------- 转发
    def __getattr__(self, item: str) -> Any:
        """未定义的属性透传给 ``BoardApp``（``_f/_t/_entry/_ghost_btn/_hint/
        _form_row/_indent_row/C/cfg/root/_log/_auto_wrap/_schedule_fit`` …）。

        这样 :class:`RankAwardsPanel` 可以拿本编辑器当 owner，而它需要的
        界面小工具与配置仍然指向真正的窗口对象。
        """
        if item.startswith("__"):
            raise AttributeError(item)
        app = self.__dict__.get("app")
        if app is None:
            raise AttributeError(item)
        return getattr(app, item)

    @property
    def award_panel(self) -> RankAwardsPanel | None:
        """矩阵面板（老名字 ``award_panel``；内部用 ``panel``）。"""
        return self.panel

    @property
    def rows(self) -> list[dict]:
        """奖项行记录（``award_rows`` 的别名）。

        证据脚本/外壳按 ``editor.rows[0]["name"]`` 这样取控件，这里保证拿到的是
        **同一个 list 对象**（不是副本），改它就等于改这张表。
        """
        return self.award_rows

    # ---------------------------------------------------------------- 界面
    def _build_ui(self) -> None:
        """建出整卡内容（原 ``BoardApp._build_award_table_ui`` 的正文）。"""
        body2 = self.body
        # -- 奖项表：可增删的行（奖项名 + 人数 + 展示方式 + 已选/来源 + 操作）----
        # 生成比赛包时写成 <award> 块：<citation> 就是这里填的名字。
        row5 = self._form_row(body2, "奖项设置", pady=(6, 2))
        self.awards_box = self._f(row5)
        self.awards_box.pack(side="left", fill="x", expand=True)
        self.award_rows = []
        self.award_rows_holder = self._f(self.awards_box)
        self.award_rows_holder.pack(fill="x")
        awards_bar = self._f(self.awards_box)
        awards_bar.pack(fill="x", pady=(5, 0))
        add_btn = self._ghost_btn(awards_bar, "＋ 添加奖项", self.add_award_row)
        add_btn.pack(side="left")
        self._tip(add_btn, "每行 = 一个奖项：名字随便写（一等奖 / 优秀奖 / 最佳女队…），\n"
                           "人数是「从当前名次往下连着给几队」。")
        if self.compact:
            # M17d：compact（实时榜单侧）**这段说明不写了** —— 用户口径「整理一下，太乱了」：
            # 每一行自带「第 N–M 名上金/银/铜」，下面还有一张「金银铜名额」表（列对齐），
            # 这里再说一遍「第 1 个奖金、第 2 个银…」就是第三遍重复。
            pass
        else:
            self._hint(awards_bar, "按榜单名次从第 1 名往下依次发；每行都会变成一个独立的奖项"
                                   "（奖项名就是 Resolver 里显示的名字）",
                       size=9, wrap=(self.hint_wrap or None)).pack(side="left", fill="x",
                                                                   expand=True, padx=10)

        if self.compact:
            # -- M17b：compact 到「展示方式」为止 ---------------------------------
            # 一血奖 / 金 银 铜统计是**生成比赛包**的选项，属于滚榜侧；
            # 全队标记矩阵（勾选 / 重新识别 / 全部清空 / 按 * 自动勾选 / 填星号）
            # 也整块不建 —— 那些只属于滚榜侧。fts_var / award_medal_var 保持 None，
            # 外壳读的 self.fts_var / self.award_medal_var 仍然指向 "roll" 编辑器。
            self.fts_var = None
            self.award_medal_var = None
            self.panel = None
        else:
            self.fts_var = tk.BooleanVar(value=True)
            fts_chk = tk.Checkbutton(
                self.awards_box, text="自动加一血奖（第一个通过这道题的队伍，每题一个，不用手填）",
                variable=self.fts_var, bg=self.C["card"], fg=self.C["sub"],
                activebackground=self.C["card"], selectcolor="white",
                font=(_FONT, 9), anchor="w", bd=0, highlightthickness=0, cursor="hand2")
            fts_chk.pack(anchor="w", pady=(6, 0))
            self._tip(fts_chk,
                      "一血 = 第一个通过这道题（first to solve）。\n"
                      "勾上后逐题自动找出「第一个通过」的队伍，写成 first-to-solve-<题目id> 奖项，\n"
                      "名字形如「一血 · A 题（0:00:35）」。默认开启。")

            self.award_medal_var = tk.BooleanVar(value=False)
            medal_chk = tk.Checkbutton(
                self.awards_box, text="同时按金/银/铜统计（前三个奖项的人数当作金/银/铜块数）",
                variable=self.award_medal_var, bg=self.C["card"], fg=self.C["sub"],
                activebackground=self.C["card"], selectcolor="white",
                font=(_FONT, 9), anchor="w", bd=0, highlightthickness=0, cursor="hand2")
            medal_chk.pack(anchor="w", pady=(6, 0))
            self._tip(medal_chk,
                      "默认不勾：只发你写的这些奖项。\n"
                      "勾上会额外把前三个奖项的人数写成 <finalized> 的金/银/铜 ——\n"
                      "两套同时给的话，同一支队伍会既拿「一等奖」又拿 Gold，重复计数，所以默认关闭。")

            # -- M15：全队标记矩阵（每个奖项一列勾选框；算法与生成包完全同一套）----
            panel_holder = self._f(body2)
            panel_holder.pack(fill="x", pady=(6, 0))
            # owner = 本编辑器：矩阵只认**这张表**的行（editor= 显式传，语义更清楚）
            self.panel = RankAwardsPanel(self, panel_holder, editor=self)

        # 启动路径上不读 contest.json（565 KB）—— 等窗口画出来再补一次。
        if str(self.app.cfg.get("last_contest_json") or "").strip():
            self.app._schedule_award_refresh(700)

    def _tip(self, widget: Any, text: str) -> None:
        """ToolTip（延迟导入 ``pta_board_gui``，避免模块级循环依赖）。"""
        maker = getattr(self.app, "_tooltip", None)
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

    # ------------------------------------------------------------ 增删行
    def add_award_row(self, name: str = "", count: Any = 1, show: str = "detail",
                      **state: Any) -> dict:
        """往奖项表里加一行：奖项名 + 人数 + 展示方式 + 已选/来源 + 操作按钮。

        ``record`` 里的 ``frame / name / count / show`` 四个键**必须保留**
        （``pta_assistant_gui.add_award_row`` 覆写与 ``_fit_award_show_combos`` 直接取）。
        M15 追加 ``team_ids / auto / manual / names / status``：这一行的**名单与来源**，
        ``collect_awards()`` 与「全队标记」矩阵都用它们。

        新行（点「＋ 添加奖项」）默认 ``count=1, auto="rank"``；``state`` 可以覆盖
        （``set_rows`` 载入历史/配置时用）。
        """
        frame = self._f(self.award_rows_holder)
        frame.pack(fill="x", pady=2)
        # M16：这一行**显式分两行**（第一行 = 编辑控件 + 三个按钮；第二行 = 「已选 N 支 · 来源」）。
        # 原来把第二行直接 pack 进同一个 frame，Tk 的 packer 会把它塞到左边那串控件的**右侧**
        # （占用剩余空腔），900x600 下就被挤出卡片看不见了 —— 实测 status 标签 x=934、卡片右边界 878。
        line1 = self._f(frame)
        line1.pack(fill="x")
        first_line = line1

        name_var = tk.StringVar(value=str(name or ""))
        count_var = tk.StringVar(value=str(count))
        show_var = tk.StringVar(
            value=cfgmod.AWARD_SHOW_LABELS.get(show, cfgmod.AWARD_SHOW_LABELS["detail"]))

        name_entry = self._entry(first_line, name_var, 10)
        name_entry.pack(side="left")
        self._t(first_line, "共", size=9, fg=self.C["hint"]).pack(side="left", padx=(4, 3))
        count_entry = self._entry(first_line, count_var, 3)
        count_entry.pack(side="left")
        self._t(first_line, "队", size=9, fg=self.C["hint"]).pack(side="left", padx=(3, 6))

        record = {
            "frame": frame, "name": name_var, "count": count_var, "show": show_var,
            "team_ids": [str(x) for x in (state.get("team_ids") or [])],
            "auto": str(state.get("auto", "rank") or ""),
            "manual": bool(state.get("manual", False)),
            "names": dict(state.get("names") or {}),
        }

        # M15：三个短按钮（「按名次填 / 清空 / 删除」）—— 都放第一行
        # M17b：compact（实时榜单侧）**没有**「按名次填 / 清空」（名单相关），只留「删除」。
        if not self.compact:
            rank_btn = self._ghost_btn(first_line, "按名次填",
                                       lambda: self.fill_award_by_rank(record))
            rank_btn.pack(side="left", padx=(6, 0))
            self._tip(rank_btn,
                      "按名次填前 N 支：把这一行改成「自动（按名次）」，从榜单第 1 名往下\n"
                      "依次取「共 N 队」里填的 N 支队（多个这样的行之间不重叠），然后立刻重算。")
            clear_btn = self._ghost_btn(first_line, "清空", lambda: self.clear_award_row(record))
            clear_btn.pack(side="left", padx=(4, 0))
            self._tip(clear_btn,
                      "把这一行的名单清空并改成「手选」——清空过的行不会被「重新识别」\n"
                      "自动填回去，方便你到下面的「全队标记」表里一个个勾。")
        del_btn = self._ghost_btn(first_line, "删除", lambda: self.remove_award_row(record))
        del_btn.pack(side="left", padx=(4, 0))

        # ---- 第二行：展示方式 + 「已选 N 支 · 来源」 --------------------------
        # M16：展示方式下拉框放到**第二行** —— 900x600 下第一行已经要塞「奖项名/共 N 队/
        # 三个按钮」，再挤一个 10 全角字的下拉框就会把文案截成「停下显示详情（推…」
        # （M13 报过的老 bug）。放第二行后它拿得到完整宽度，`_fit_award_show_combos`
        # 也只看这一行的剩余空间（它用 combo.master 算"这一行别的控件占了多少"）。
        # M17c：**compact（实时榜单侧）不建这一行** —— 用户口径「实时榜里展示方式也不用，
        # 可以删了」：`show` 是**滚榜生成包**才用的字段（Stopped/Pause 的 Resolver 行为），
        # 实时榜单只按当前名次上色。数据里的 `show` 字段照旧保留、滚榜侧照旧能改，
        # compact 只是不显示、不修改它（`collect_awards()` 仍把原值带出去）。
        if not self.compact:
            second_line = self._f(frame)
            second_line.pack(fill="x", pady=(0, 2))
            self._t(second_line, "展示方式", size=9, fg=self.C["hint"]).pack(side="left")
            # M13（4a）：短文案见 board_config.AWARD_SHOW_LABELS，长说明放 ToolTip。
            # ⚠ 这个 Combobox 是 ``record["frame"]`` 的**子孙控件**（在 second_line 里）：
            #    ``pta_assistant_gui._fit_award_show_combos`` 递归找每行的第一个 ttk.Combobox。
            show_combo = ttk.Combobox(second_line, textvariable=show_var, state="readonly",
                                      width=24,
                                      values=list(cfgmod.AWARD_SHOW_LABELS.values()))
            show_combo.pack(side="left", padx=(6, 10))
            self._tip(show_combo,
                      "停下显示详情（推荐）：揭晓到这支队伍时停下 —— 照片铺满整张奖项卡，\n"
                      "　　　　　　　　　　　　　左下角显示校徽 + 队名 + 奖项名。\n"
                      "暂停：画面停住等手动继续（在 Resolver 里点击/按空格再往下走）。")

        if self.compact:
            # M17b：第三行换成「名次区间提示」（实时榜单按名次上金/银/铜），
            # **没有**「已选 N 支 · 来源」——名单只在滚榜侧勾，这里不显示任何名单信息。
            hint_row = self._f(frame)
            hint_row.pack(fill="x", pady=(0, 2))
            record["status"] = None
            record["rank_hint"] = self._t(hint_row, "", size=9, fg=self.C["hint"], anchor="w")
            record["rank_hint"].pack(side="left", padx=(2, 0))
        else:
            # 第二行：已选 N 支 · 来源 —— 独占一行（不在第一行里抢宽度，900x600 也看得见）
            status_row = self._f(frame)
            status_row.pack(fill="x", pady=(0, 2))
            record["status"] = self._t(status_row, "", size=9, fg=self.C["hint"], anchor="w")
            record["status"].pack(side="left", padx=(2, 0))

        self.award_rows.append(record)
        self._update_award_row_status(record)
        # 这一行以后每改一次名/人数，「全队标记」矩阵就跟着重算（用缓存，不读文件）
        panel = self.panel
        if panel is not None:
            try:
                panel.track(record)
                panel.notify_changed(0)
            except Exception:  # noqa: BLE001
                pass
        # M17：改名/改人数/改展示方式 → 同步到另一个编辑器（真正的双向同步）
        self._watch_record(record)
        # 滚榜页那套「展示方式下拉框按最长文案量宽」的收尾（合并外壳才有这段）
        if self.key == "roll":
            fit = getattr(self.app, "_fit_award_show_combos", None)
            if callable(fit):
                try:
                    fit()
                except Exception:  # noqa: BLE001
                    pass
        if self.compact:
            self._refresh_rank_hints()
        if not self._bulk:
            self._notify_changed()
        return record

    def _watch_record(self, record: dict) -> None:
        """盯住这一行的输入控件：名字/人数去抖同步，展示方式立即同步。"""
        for key in ("name", "count"):
            var = record.get(key)
            if var is None or not hasattr(var, "trace_add"):
                continue
            handler = (self._on_count_changed if key == "count"
                       else (lambda *_a: self._notify_changed(_TEXT_SYNC_MS)))
            try:
                var.trace_add("write", handler)
            except Exception:  # noqa: BLE001
                continue
        var = record.get("show")
        if var is not None and hasattr(var, "trace_add"):
            try:
                var.trace_add("write", lambda *_a: self._notify_changed(0))
            except Exception:  # noqa: BLE001
                pass

    def _on_count_changed(self, *_args: Any) -> None:
        """改人数：名次区间提示立刻重算 + 去抖同步到另一侧。

        （``StringVar.trace_add`` 的回调会带 ``(name, index, op)`` 三个参数，
        所以这里必须收 ``*_args``。）
        """
        self._refresh_rank_hints()
        self._notify_changed(_TEXT_SYNC_MS)

    def _refresh_rank_hints(self) -> None:
        """compact 侧每行的「名次区间提示」（第 1–2 名上金 / 第 4 个往后不上色）。

        算法 = 榜单页的着色规则（``board_config.live_view_awards`` / ``live_awards_from_rows``）：
        第 1 个奖 = 金、第 2 个 = 银、第 3 个 = 铜、第 4 个往后不上色；
        名次区间按**行顺序把人数累加**出来（第 1 行从第 1 名起，往后接着数）。
        """
        if not self.compact:
            return
        taken = 0
        for index, record in enumerate(list(self.award_rows)):
            label = record.get("rank_hint")
            if label is None:
                continue
            try:
                count = max(0, int(str(record["count"].get()).strip()))
            except (KeyError, TypeError, ValueError):
                count = 0
            if index >= len(_MEDALS):
                text = f"第 {index + 1} 个往后不上色"
            elif count <= 0:
                text = "人数是 0：这一档不上色"
            else:
                start = taken + 1
                end = taken + count
                span = f"第 {start} 名" if end <= start else f"第 {start}–{end} 名"
                text = f"{span}上{_MEDALS[index]}"
            taken += count
            try:
                label.configure(text=text)
            except Exception:  # noqa: BLE001
                pass

    def remove_award_row(self, record: dict) -> None:
        if record in self.award_rows:
            self.award_rows.remove(record)
        try:
            record["frame"].destroy()
        except Exception:  # noqa: BLE001
            pass
        panel = self.panel
        if panel is not None:
            try:
                panel.notify_changed(0)
            except Exception:  # noqa: BLE001
                pass
        self._refresh_rank_hints()
        if not self._bulk:
            self._notify_changed()

    # ------------------------------------------------------------ 整表载入
    def set_awards(self, rows: list[dict]) -> None:
        """把奖项表整表换成 ``rows``（历史/配置载入走这条路）。

        M15：每一行会**整行**载入 —— 连名单（``team_ids``）、来源（``auto/manual``）、
        上次看到的队名（``names``）一起，所以载入历史后名单不会被「重新识别」改掉。
        空表回落到 ``DEFAULT_AWARDS``（老语义，不动）。
        """
        rows = [r for r in (rows or []) if isinstance(r, dict)] or list(cfgmod.DEFAULT_AWARDS)
        clean: list[dict] = []
        for index, row in enumerate(rows):
            try:
                clean.append(cfgmod.clean_award_row(dict(row), index))
            except Exception:  # noqa: BLE001
                clean.append({"name": row.get("name") or "奖项",
                              "count": row.get("count") or 1,
                              "show": row.get("show") or "detail", "team_ids": [],
                              "auto": "rank", "manual": False, "names": {}})
        self.set_rows(clean)

    def set_rows(self, rows: list[dict]) -> None:
        """按 ``rows`` **原样**重画整张表（M17 双向同步用）。

        与 :meth:`set_awards` 的区别：不套 ``clean_award_row`` 的升级规则、空表也不做
        ``DEFAULT_AWARDS`` 回落 —— 另一侧 ``collect_awards()`` 收出来的行经过这里
        再收一次必须**逐项不变**，两边的数据才算真的一致。
        """
        self._cancel_sync_job()
        was_bulk = self._bulk
        self._bulk = True
        try:
            for record in list(self.award_rows):
                self.remove_award_row(record)
            for row in (rows or []):
                if not isinstance(row, dict):
                    continue
                data = coerce_award_row(row)
                # ⚠ 只能用**三个位置参数**调 add_award_row（子类覆写没有 **kwargs 的兼容口径）；
                #   名单/来源随后单独写进 record。
                record = self.add_award_row(data["name"], data["count"], data["show"])
                if isinstance(record, dict):
                    record["team_ids"] = [str(x) for x in data["team_ids"]]
                    record["auto"] = str(data["auto"] or "")
                    record["manual"] = bool(data["manual"])
                    record["names"] = dict(data["names"])
                    self._update_award_row_status(record)
            self._sync_award_row_status()
        finally:
            self._bulk = was_bulk
        panel = self.panel
        if panel is not None:
            try:
                panel.notify_changed(0)
            except Exception:  # noqa: BLE001
                pass
        self._refresh_rank_hints()

    # ------------------------------------------------------------ 读值
    def _read_record(self, record: dict) -> tuple[str, dict]:
        """一行控件 → ``(原始名字, {name,count,show,team_ids,auto,manual,names})``。"""
        try:
            raw_name = str(record["name"].get()).strip()
        except Exception:  # noqa: BLE001
            raw_name = ""
        try:
            count = int(str(record["count"].get()).strip())
        except (TypeError, ValueError):
            count = 0
        try:
            show = self._SHOW_LABEL_TO_KEY.get(record["show"].get(), "detail")
        except Exception:  # noqa: BLE001
            show = "detail"
        ids = [str(x) for x in (record.get("team_ids") or [])]
        auto = str(record.get("auto") or "")
        if auto not in cfgmod.AWARD_AUTO_RULES:
            auto = ""
        manual = bool(record.get("manual"))
        names = record.get("names") if isinstance(record.get("names"), dict) else {}
        return raw_name, {"name": raw_name, "count": max(0, count), "show": show,
                          "team_ids": ids, "auto": auto, "manual": manual,
                          "names": dict(names)}

    def collect_awards(self) -> list[dict]:
        """读出奖项表 —— M15 的**唯一奖项来源**（生成包/预览/指纹都用它）。

        每一行返回 :data:`board_config.AWARD_ROW_KEYS` 的完整形状::

            {"name", "show", "count", "team_ids", "auto", "manual", "names"}

        ``team_ids`` 就是「全队标记」矩阵里现在勾着的队；``names`` 至少覆盖
        ``team_ids``（用来提示「这支队伍改名了 / 找不到了」）。
        """
        out: list[dict] = []
        for record in list(self.award_rows):
            raw_name, row = self._read_record(record)
            if not raw_name and row["count"] <= 0 and not row["team_ids"]:
                continue
            out.append({"name": raw_name or "奖项", "count": row["count"], "show": row["show"],
                        "team_ids": row["team_ids"], "auto": row["auto"],
                        "manual": row["manual"], "names": row["names"]})
        return out

    def rows_snapshot(self) -> list[dict]:
        """这张表的**全部**行（不过滤空行）—— 同步给另一侧时用它。

        和 :meth:`collect_awards` 的区别：空行（名字/人数/名单全空，收集时会被丢掉）
        也照原样带上，这样两边的**行数**也一致；两边各自 ``collect_awards()`` 过滤后
        自然还是同一份数据。
        """
        out: list[dict] = []
        for record in list(self.award_rows):
            raw_name, row = self._read_record(record)
            row["name"] = raw_name or "奖项"
            out.append(row)
        return out

    # -- 单行操作 ----------------------------------------------------------

    def fill_award_by_rank(self, record: dict) -> None:
        """「名次填」：这一行 = 自动（按名次），人数取输入框里的值，然后重算。"""
        record["auto"] = "rank"
        record["manual"] = False
        record["team_ids"] = []
        self._after_award_row_change(quiet=True)

    def clear_award_row(self, record: dict) -> None:
        """「清空」：这一行 = 手选空名单（**显式**写三个键，不会被重新识别填回去）。"""
        record["team_ids"] = []
        record["auto"] = ""
        record["manual"] = True
        self._after_award_row_change(quiet=True)

    def _after_award_row_change(self, quiet: bool = False) -> None:
        """奖项行被按钮改过之后：写回 cfg + 同步另一侧 + 重算矩阵（读缓存，不重读文件）。"""
        self._notify_changed()                      # cfg + 另一侧界面（立即）
        if not self.app.recompute_award_panels():   # 有缓存 → 只重算，不读文件
            try:
                self.app.refresh_award_state(quiet=quiet)
            except Exception:  # noqa: BLE001
                pass
        self._sync_award_row_status()

    def _update_award_row_status(self, record: dict) -> None:
        """刷新一行右下角的「已选 N 支 · 来源」。"""
        label = record.get("status")
        if label is None:
            return
        panel = self.panel
        source = "手选"
        if panel is not None:
            try:
                source = panel.row_source_text(record)
            except Exception:  # noqa: BLE001
                source = "手选"
        else:
            source = "手动" if record.get("manual") else "手选"
        picked = len([x for x in (record.get("team_ids") or [])])
        try:
            label.configure(text=f"已选 {picked} 支 · {source}")
        except Exception:  # noqa: BLE001
            pass

    def _sync_award_row_status(self) -> None:
        """刷新**所有**奖项行的「已选 N 支 · 来源」（勾选框一改就调）。"""
        for record in list(getattr(self, "award_rows", []) or []):
            if isinstance(record, dict):
                self._update_award_row_status(record)

    def _awards_rows_recomputed(self) -> None:
        """矩阵重算（按名次 / 星号算名单）把名单写回了行 → 顺手把 cfg 刷成一样。

        纯内存，不触发同步广播；这样「界面上看到的 = cfg['awards'] 里存的」
        在**任何时候**都成立，不只是用户点按钮的那一刻。

        M17b：compact（实时侧）**不拥有名单** —— 主表这边每算出一份新名单就主动
        喂过去（只做「主表 → compact」**一个方向**：compact 侧没有矩阵、不会反向
        触发，天然无环）。否则实时侧手里那份是旧的，下一次它改人数时就会把旧名单
        带回去。用户正在实时侧打字（有去抖任务）时先不喂，免得把人家正编辑的行重画掉。
        """
        try:
            self.app.cfg["awards"] = self.collect_awards()
        except Exception:  # noqa: BLE001
            pass
        if self.compact or self.key != "roll":
            return
        if self.app._awards_syncing or self.app._award_edit_pending():
            return
        try:
            rows = self.rows_snapshot()
        except Exception:  # noqa: BLE001
            return
        for key, editor in list(self.app.award_editors.items()):
            if key == self.key or not getattr(editor, "compact", False):
                continue
            try:
                if editor.rows_snapshot() == rows:
                    continue
                editor.set_rows([dict(row) for row in rows])
            except Exception:  # noqa: BLE001
                continue

    # ------------------------------------------------------------ 同步
    def push_state(self) -> None:
        """矩阵里勾了框 / 「全部清空」/「按 * 自动勾选」之后：写回 cfg + 同步另一侧。

        （``RankAwardsPanel._push_state`` 走这条路；原来的实现只写 cfg，
        现在多了一步「把另一侧界面也刷成同一份」。）
        """
        try:
            self.app.cfg["awards"] = self.collect_awards()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.app._on_awards_changed(self.key)
        except Exception:  # noqa: BLE001
            pass

    def refresh_award_state(self, quiet: bool = False) -> None:
        """矩阵工具条上的「重新识别 / 刷新」：走 BoardApp 的全局刷新（两个编辑器都刷）。"""
        self.app.refresh_award_state(quiet=quiet)

    def _notify_changed(self, delay: int = 0) -> None:
        """告诉 ``BoardApp``：「这张表改了」，让它写 cfg 并重画另一侧。

        ``delay > 0``（改名 / 改人数这类逐字符输入）时：**先**把最新值写进
        ``cfg["awards"]``（纯内存，很便宜 —— 切模式时 ``_sync_award_editors``
        读的就是它，去抖窗口里也不会读到旧值），去抖到点后再做重活
        （同步另一侧 + 外壳的写运行目录 / 提示）。
        """
        if self._bulk or self._destroyed:
            return
        if self.app.award_editors.get(self.key) is not self:
            return                      # 这个编辑器已被同 key 的新实例替换（自测会这样）
        if delay and delay > 0:
            try:
                self.app.cfg["awards"] = self.collect_awards()
            except Exception:  # noqa: BLE001
                pass
            self._cancel_sync_job()
            try:
                self._sync_job = self.app.root.after(int(delay), self._run_notify)
                return
            except Exception:  # noqa: BLE001
                self._sync_job = None
        self._run_notify()

    def _run_notify(self) -> None:
        self._sync_job = None
        if self._destroyed:
            return
        if self.app.award_editors.get(self.key) is not self:
            return
        try:
            self.app._on_awards_changed(self.key)
        except Exception:  # noqa: BLE001
            pass

    def _cancel_sync_job(self) -> None:
        job = self._sync_job
        self._sync_job = None
        if job:
            try:
                self.app.root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
