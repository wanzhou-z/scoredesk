# -*- coding: utf-8 -*-
"""展示元数据与校徽缓存，复用原项目的匹配、裁圆和一血函数。"""
from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent
# 默认读比赛数据本体；隔离验收可用环境变量指向冻结副本，避免与正在跑的 poller 抢文件。
CONTEST_PATH = Path(os.environ.get("LIVE_BOARD_CONTEST") or (BASE / "contest.json"))
# 源码模式（公开仓库）布局是 <repo>\{app,core,liveboard,outrank}；
# 打包模式则是 <exe>\_internal\{app,core?}。这里按"谁存在用谁"排一遍，
# 不再写死 pta-resolver 这个私有目录名（用户反馈：scripts\*.bat 一跑就 ModuleNotFoundError）。
_CANDIDATE_DIRS = [
    BASE.parent,                      # 打包：_internal（app/core 与 liveboard 同级）
    BASE.parent / "app",              # 打包 / 源码：app 目录
    BASE.parent / "core",             # 源码：core 目录（pta_common / pta_fetch / vjudge_fetch）
    BASE.parent / "pta-resolver",     # 旧布局（保留兼容）
    BASE.parent / "pta-resolver" / "app",
]
for _d in _CANDIDATE_DIRS:
    try:
        if _d.is_dir() and str(_d) not in sys.path:
            sys.path.insert(0, str(_d))
    except OSError:
        pass
RESOLVER = BASE.parent  # 仅作日志/兼容用途
from board_backend import make_circular_png, match_team_logos
from pta_to_resolver_xml import compute_first_to_solve
from board_core import (build_board, data_error, make_state, repair_data_files, frozen_cell_ids,
                        problem_stats, DATA_ERRORS, REPAIRS, load_json_tolerant, note_repair)

#: 内置空态：数据文件彻底不可用时用它继续跑（进程绝不退出）。
#: contest 段留空 → make_state 算出 duration=0、freeze=0、board=[]，
#: 结构完整但榜单为空，前端和 /api/board 都不会 500。
EMPTY_CONTEST = {
    "contest": {}, "problems": [], "teams": [], "submissions": [],
}


def _data_error(exc: BaseException, path, detail: str) -> None:
    """记录数据文件问题（去重）。"""
    message = f"{Path(path).name} 无法解析：{detail}"
    if message not in DATA_ERRORS:
        DATA_ERRORS.append(message)


def _repair(message: str) -> None:
    # 去重在 board_core.note_repair 里做：repair_data_files 的 say() 已经记过一次，
    # 这里再 append 会让 /health 的 repairs 出现同一句两遍。
    note_repair(message)


def empty_contest() -> dict:
    """空态的一份全新拷贝（读侧任何一次失败都用它兜底）。"""
    import copy as _copy
    return _copy.deepcopy(EMPTY_CONTEST)


# ---------------------------------------------------------------------------
# 启动自愈：server / poller 一进来就先把数据文件修好，再谈别的
# ---------------------------------------------------------------------------
# 坏 contest.json → 用 .bak 恢复；坏/缺 state.json → 用 contest.json 重建。
# 必须在 ``LogoStore()`` 之前跑：它以前放在导入期，坏文件会让 server 直接崩。
repair_data_files(CONTEST_PATH, BASE / "state.json", log=_repair)


def _empty_contest(mtime=None):
    return empty_contest()


@lru_cache(maxsize=2)
def _read_contest_cached(path_str, mtime_ns, size):
    """按 (路径, mtime, 大小) 缓存读取；解析失败返回空态，**不抛异常**。"""
    data, status = load_json_tolerant(Path(path_str), None)
    if not status["ok"]:
        # load_json_tolerant 已经记过一条（含是否回退 .bak 的信息）；
        # 这里只在没人记过时补记，免得 /health 的 data_error_detail 里同一件事写三遍。
        if not any(status["error"] in message for message in DATA_ERRORS):
            _data_error(ValueError(status["error"]), path_str, status["error"])
        return empty_contest()
    if not isinstance(data, dict):
        _data_error(ValueError("顶层不是对象"), path_str, "顶层不是 JSON 对象")
        return empty_contest()
    return data


def _contest_file():
    """当前 contest 文件路径（支持 ``LIVE_BOARD_CONTEST`` 环境变量覆盖）。"""
    return Path(os.environ.get("LIVE_BOARD_CONTEST") or (BASE / "contest.json"))


def current_contest():
    """稳健读取当前比赛数据。文件缺失/损坏 → 空态（不抛）。"""
    path = _contest_file()
    try:
        stat = path.stat()
        return _read_contest_cached(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        _data_error(exc,
                    path,
                    f"读不到文件（{exc.__class__.__name__}: {exc}）")
        return empty_contest()


def _contest(mtime):
    """兼容旧调用（``presentation._contest(mtime)``）；实现见 current_contest。"""
    return current_contest()


def load_config():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8-sig"))
    # 视觉配置通过现有 /api/config 的 view 字段下发，避免改动榜单数据端点。
    view = cfg.setdefault("view", {})
    for key in ("problem_colors", "medal_colors", "awards", "rows", "scroll", "scroll_speed", "scroll_pause", "anim",
                "follow", "follow_pause", "follow_max"):
        if key in cfg and key not in view:
            view[key] = cfg[key]
    if not view.get("awards") and view.get("awards_mode") != "starred":
        # M12：打星模式下 view.awards 就是「前三组」（可能暂时为空 = 还没勾选），
        # 这时**不能**回落到 F:\PTA-Board\config.json 的老奖项 —— 否则会按名次着色。
        # 只读用户正在使用的配置；本项目配置有奖项时优先使用本项目配置。
        external = Path(r"F:\PTA-Board\config.json")
        try:
            other = json.loads(external.read_text(encoding="utf-8-sig"))
            if isinstance(other.get("awards"), list):
                view["awards"] = other["awards"]
        except (OSError, ValueError, TypeError):
            pass
    return cfg


def resolve_freeze_mode(override=None, thaw=False):
    """观众/裁判视角。``thaw=True`` 或 ``freeze=operator`` 时**揭示**真实成绩。

    M11：默认（audience）在封榜后**永不解榜** —— 比赛结束也保持封榜快照；
    裁判/自己人要看真实结果，必须显式给 ``?thaw=1``（:func:`resolve_freeze_mode`
    把它当作 operator）或 ``?freeze=operator``。
    """
    if thaw:
        return "operator"
    mode=override if override is not None else load_config().get("freeze_mode", "operator")
    if mode not in ("operator", "audience"):
        raise ValueError("freeze 必须为 operator 或 audience")
    return mode


@lru_cache(maxsize=2)
def _audience_snapshot(mtime, freeze_at):
    # 复用既有计算，只调整数据截止时刻；没有复制排名规则。
    source=current_contest()
    board=build_board(source, display_time=freeze_at, freeze_mode="audience")
    prefix=dict(source)
    prefix["submissions"]=[s for s in source.get("submissions",[]) if float(s.get("rel_time_seconds") or 0)<=freeze_at]
    first=compute_first_to_solve(prefix, int((source.get("contest") or {}).get("penalty_minutes") or 20))
    return board, first


def _contest_stamp() -> int:
    """contest 文件的 mtime_ns；拿不到就 0（缓存键仍可用，只是不刷新）。"""
    try:
        return _contest_file().stat().st_mtime_ns
    except OSError:
        return 0


def metadata(state, freeze_mode=None, thaw=False):
    """只补充展示信息，不改变 M1 排名、解题数、罚时或 API 榜单行。

    M11：封榜**永不解榜**（默认观众视角）—— ``frozen`` 一旦为真就一直为真，
    直到 ``?thaw=1`` / ``freeze=operator``。新增展示键：``thaw``、``contest_over``、
    ``frozen_cells``（封榜后有提交的格子）、``freeze_snapshot_at``；已有键含义不变。
    """
    contest = current_contest()
    replay = state.get("presentation", {})
    elapsed = replay.get("elapsed")
    meta = state.get("contest") or contest.get("contest") or {}
    if elapsed is None:
        import time
        elapsed = max(0, time.time() - float(meta.get("start_epoch") or time.time()))
    freeze_at = state.get("freeze_at")
    first = replay.get("first_to_solve")
    if first is None:
        first = compute_first_to_solve(contest, int(meta.get("penalty_minutes") or 20))
        first = [item for item in first if item["rel_seconds"] <= elapsed]
    locked = replay.get("locked_cells")
    if locked is None:
        locked = frozen_cell_ids(contest, freeze_at, elapsed) if freeze_at is not None else []
    mode=resolve_freeze_mode(freeze_mode, thaw)
    duration=float(meta.get("duration_seconds") or 0)
    # M11：封榜起点一过就一直封着（不再要求 elapsed < duration）。
    frozen=bool(freeze_at is not None and elapsed >= float(freeze_at) and mode=="audience")
    if frozen:
        _, first=_audience_snapshot(_contest_stamp(), freeze_at)
        first=replay.get("audience_first_to_solve", first)
    if frozen and locked:
        # 观众快照下这些格子在榜单里就是隐藏状态，locked_cells 保持空（与 M1 一致）。
        locked=[]
    return {
        "contest": meta, "elapsed": elapsed,
        "speed": replay.get("speed", 1),
        "replay": bool(replay.get("replay")),
        "checkpoint": replay.get("checkpoint"),
        "held": replay.get("held", False),
        "first_to_solve": first, "locked_cells": locked,
        # 实时封榜由比赛时钟决定，保持 /api/board 的 M1 字段原值。
        "freeze_at": freeze_at,
        "frozen": frozen, "freeze_mode": mode,
        # ---- M11 新增（只增不改） ------------------------------------------
        "thaw": bool(thaw) or mode == "operator",
        "contest_over": bool(duration and elapsed >= duration),
        "freeze_snapshot_at": freeze_at if frozen else None,
        "frozen_cells": list(locked) if frozen else [],
    }


def annotate_problem_stats(view, display_time=None):
    """M23：给 problems[] 补 ``ac_teams`` / ``subs``（**只增不改**，旧 state 也能补）。

    state.json 里已经有这两个键时原样返回（由 ``board_core.make_state`` 写入）；
    旧 state（2026-10-17 之前生成的）没有，就按当前 contest.json 现算一份。
    口径见 :func:`board_core.problem_stats`；封榜后仍按完整提交统计（用户指定）。
    """
    problems = view.get("problems")
    if not isinstance(problems, list) or not problems:
        return view
    if all(isinstance(p, dict) and "ac_teams" in p and "subs" in p for p in problems):
        return view
    try:
        stats = problem_stats(current_contest(), display_time)
    except Exception as exc:                           # noqa: BLE001 - 统计挂了也不许影响 API
        _data_error(exc, "contest", f"题目统计失败（{exc.__class__.__name__}: {exc}）")
        return view
    view["problems"] = [dict(p or {}, **stats.get(str((p or {}).get("id")), {"ac_teams": 0, "subs": 0}))
                        for p in problems]
    return view


def annotate_stars(view):
    """M39（用户 2026-10-18）：给榜单行打「打星」标记 ``row["star"]``。

    名单来自配置 ``view.star_team_ids``（界面上「全队标记」勾选写入）。
    **只做展示标记**：排名、罚时、奖项、一血一律照旧 —— 打星队照常参与排名。
    """
    try:
        cfg = load_config() or {}
        view_cfg = cfg.get("view") or {}
        raw = view_cfg.get("star_team_ids")
        if raw is None:
            raw = cfg.get("star_team_ids")
        ids = {str(x) for x in (raw or []) if str(x).strip()}
        for row in (view.get("board") or []):
            if isinstance(row, dict):
                row["star"] = str(row.get("team_id")) in ids
    except Exception as exc:  # noqa: BLE001
        _data_error(exc, "config", f"打星名单读取失败（{exc.__class__.__name__}: {exc}）")
    return view


def annotate_submissions(view, info):
    """M41（用户 2026-10-18）：给榜单附上「谁在哪题提交过什么」，供悬停信息卡用。

    只做只读展示：`subs_by_team[team_id] = [[problem_id, 秒, 判定, 语言], ...]`（按时间升序）。
    封榜观众视角只给封榜前的提交；时间轴（seek）再按那一刻截断。
    """
    try:
        contest = current_contest()
        subs = contest.get("submissions") or []
        labels = {str(p.get("id")): str(p.get("label") or "") for p in (contest.get("problems") or [])}
        cutoff = None
        if info.get("frozen") and info.get("freeze_at") is not None:
            cutoff = float(info["freeze_at"])
        if str(view.get("channel") or "") == "seek" and info.get("elapsed") is not None:
            cap = float(info["elapsed"])
            cutoff = cap if cutoff is None else min(cutoff, cap)
        out = {}
        for sub in subs:
            try:
                rel = float(sub.get("rel_time_seconds") or 0)
            except (TypeError, ValueError):
                rel = 0.0
            tid = str(sub.get("userId") or "")
            pid = str(sub.get("problemSetProblemId") or "")
            if not tid or not pid:
                continue
            if cutoff is not None and rel > cutoff:
                # M43（用户 2026-10-18）：封榜后的提交**照常列出来**（时间/语言都给），
                #   但判定一律写 PENDING —— 页面上显示「待判」，不泄露封榜后的结果。
                # 编译错误不是"判定结果"，封榜后也照实说（CE 本来就不算次数/罚时），
                # 其他一律「待判」，避免把封榜后的结果泄出去。
                st = str(sub.get("status") or "").upper()
                label_st = "COMPILE_ERROR" if st == "COMPILE_ERROR" else "PENDING"
                out.setdefault(tid, []).append([pid, int(round(rel)), label_st,
                                                str(sub.get("compiler") or "")])
                continue
            out.setdefault(tid, []).append([pid, int(round(rel)),
                                            str(sub.get("status") or ""),
                                            str(sub.get("compiler") or "")])
        for rows in out.values():
            rows.sort(key=lambda x: x[1])
        view["subs_by_team"] = out
        view["problem_labels"] = labels
    except Exception as exc:  # noqa: BLE001
        _data_error(exc, "contest", f"提交记录附加失败（{exc.__class__.__name__}: {exc}）")
    return view


def board_view(state, freeze_mode=None, thaw=False):
    """按每个 HTTP 客户端的视角投影，绝不覆盖轮询器的实时原状态。

    M11：观众视角在封榜后**永远**使用封榜快照（比赛结束也一样）；``thaw=True``
    或 ``freeze=operator`` 时不做任何投影，直接给真实榜单。
    """
    info=metadata(state, freeze_mode, thaw)
    view=dict(state)
    if info["frozen"]:
        frozen_board=state.get("audience_board")
        if frozen_board is None:
            frozen_board, _=_audience_snapshot(_contest_stamp(), info["freeze_at"])
        view["board"]=frozen_board
        view["frozen"]=True
        # 观众 SSE 不携带封榜后的提交/名次/一血 diff。
        view["event"]={"type":"freeze", "new_submissions":0, "rank_changed":False}
    annotate_problem_stats(view, info.get("elapsed"))
    annotate_stars(view)
    annotate_submissions(view, info)
    return view, info


# ---------------------------------------------------------------------------
# M23 时间轴（?timebar=1）：按任意比赛时刻重建一份**只读展示快照**
#   背景：页面上的数据来自 state.json（SSE），只有"最新一帧"，没法拖到任意时刻。
#   这里复用 replay.py 的同一口径（submissions 截断到 at -> 重算榜单），**不落盘、不写 state.json、
#   不改 poller / 排名算法 / 封榜判定**；封榜时刻之后且观众视角时，榜单换成封榜快照（与 board_view 一致）。
# ---------------------------------------------------------------------------
def seek_view(at, freeze_mode=None, thaw=False):
    """把比赛时刻 ``at``（秒）渲染成与 SSE 更新同形状的一份 payload。"""
    contest = current_contest()
    meta = contest.get("contest") or {}
    duration = float(meta.get("duration_seconds") or 0)
    try:
        at = float(at)
    except (TypeError, ValueError):
        at = 0.0
    at = max(0.0, min(duration, at)) if duration else max(0.0, at)
    freeze = float(meta.get("freeze_seconds") or 0)
    freeze_at = max(0.0, duration - freeze) if freeze else None
    penalty = int(meta.get("penalty_minutes") or 20)
    mode = resolve_freeze_mode(freeze_mode, thaw)

    def prefix(upto):
        out = dict(contest)
        out["submissions"] = [s for s in (contest.get("submissions") or [])
                              if float(s.get("rel_time_seconds") or 0) <= upto]
        return out

    current = prefix(at)
    state = make_state(current, channel="seek", last_poll=None, display_time=None)
    first = [item for item in compute_first_to_solve(current, penalty) if item["rel_seconds"] <= at]
    frozen = bool(freeze_at is not None and at >= freeze_at and mode == "audience")
    audience_board = audience_first = None
    if frozen:
        snap = prefix(freeze_at)
        # 封榜快照：传入**完整** contest（build_board 内部按 freeze_at 截断排名），这样
        # 「封榜后提交」才标得出黄格（与 presentation._audience_snapshot / replay 的生产路径一致）。
        audience_board = build_board(contest, display_time=freeze_at, freeze_mode="audience")
        audience_first = compute_first_to_solve(snap, penalty)
        state["audience_board"] = audience_board
    state["frozen"] = frozen
    state["freeze_at"] = freeze_at
    state["contest_over"] = bool(duration and at >= duration)
    state["presentation"] = {
        "replay": True, "elapsed": at, "speed": 1, "checkpoint": None, "held": False,
        "first_to_solve": first, "audience_first_to_solve": audience_first,
        # 观众封榜快照里的格子不再额外锁（与 replay.py 的观众快照一致）。
        "locked_cells": [],
    }
    view, info = board_view(state, mode, thaw)
    return view, info


class LogoStore:
    def __init__(self):
        self.cache = BASE / "tmp" / "logo-cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.files = {}
        self.report = {}
        self.refresh()

    def refresh(self):
        cfg = load_config()
        folder = Path(cfg["logos_dir"])
        contest = current_contest()
        try:
            teams = contest.get("teams") or []
            found = match_team_logos(teams, folder)
        except Exception as exc:                     # noqa: BLE001 - 校徽挂了也要能让榜单起来
            _data_error(exc, "logos", f"校徽/队伍匹配失败（{exc.__class__.__name__}: {exc}）")
            self.files = {}
            self.report = {"logos_dir": str(folder), "default": "", "matched": [],
                           "default_teams": [], "unused": [], "errors": [str(exc)]}
            return
        self.files = {}
        self.report = {
            "logos_dir": str(folder), "default": "", "matched": [],
            "default_teams": [], "unused": found["unused"], "errors": [],
        }
        default = found["default"]
        if default is not None:
            dest = self.cache / "default.png"
            ok, note = make_circular_png(default, dest)
            if ok:
                self.files["default"] = dest
                self.report["default"] = default.name
            else:
                self.report["errors"].append(note)
        for team, src, how in found["matched"]:
            tid = str(team["userId"])
            dest = self.cache / (tid + ".png")
            ok, note = make_circular_png(src, dest)
            if ok:
                self.files[tid] = dest
                self.report["matched"].append({
                    "team_id": tid, "student_number": team.get("studentNumber"),
                    "name": team.get("name"), "file": src.name, "how": how,
                })
            else:
                self.report["errors"].append(note)
        specific = set(self.files) - {"default"}
        if "default" in self.files:
            self.report["default_teams"] = [
                str(t.get("studentNumber") or t["userId"]) for t in teams
                if str(t["userId"]) not in specific
            ]

    def path(self, key):
        return self.files.get(key) or self.files.get("default")

    def path_specific(self, key):
        """只返回**这一支队专属**的校徽（匹配不到返回 None，不回落到默认）。

        VJudge 版用：``/logo/<uid>`` 先问这里（备注校徽 / 备注姓名 / 学号），
        没有就用默认校徽。（VJudge 头像那条回落 2026-10-27 已按用户要求删除：
        头像经常抓不到，宁可显示默认校徽，也不要一堆半坏的头像。）
        """
        return self.files.get(key)

