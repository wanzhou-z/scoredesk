# -*- coding: utf-8 -*-
"""公网只读「外榜站点」：往期榜单存档的列表页 + 单场榜单页 + 只读 API。

定位
----
这是 live-board 之外的**第二个纯 Python 进程**：
* live-board 继续负责比赛中的实时 SSE；比赛结束不退出，榜单停在最后一刻；
* 管理员用 ``archive.py`` 把那一刻冻结成一份存档；
* 本进程只读 ``archives`` 目录，对外提供列表和榜单，**没有任何写接口**。

投影口径（与 live-board 保持一致）
--------------------------------
* ``meta.revealed == false``：观众视角。用存档里的 contest.json 现算
  ``build_board(..., display_time=封榜时刻, freeze_mode="audience")``，即封榜快照；
* ``meta.revealed == true``：管理员已解榜，返回最终榜（相当于 thaw / operator）；
* 所有数据都从存档目录现算，不读当前 live 的 state.json，也不 import server.py，
  因此往期站点不会因为下一场比赛覆盖 live 数据而变样。

只用标准库 + live-board 已有模块（board_core / pta_to_resolver_xml）。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.dont_write_bytecode = True

BASE = Path(__file__).resolve().parent


def _module_dirs() -> list[Path]:
    """外榜与 ``liveboard`` / ``core`` 是同级目录，直接跑本文件时补上 import 路径。"""
    repo = BASE.parent
    return [repo / "liveboard", repo / "core",
            repo / "_internal" / "liveboard", repo / "_internal" / "core"]


for _dir in _module_dirs():
    if _dir.is_dir() and str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))


def _default_web_root() -> Path:
    env = os.environ.get("OUTRANK_WEB_ROOT")
    if env:
        return Path(env)
    for cand in (BASE.parent / "liveboard" / "web", BASE / "web"):
        if (cand / "board.html").is_file():
            return cand
    return BASE.parent / "liveboard" / "web"


DEFAULT_ARCHIVES = BASE / "archives"
DEFAULT_WEB_ROOT = _default_web_root()

# live-board 的排名/题目统计函数；这里不 import presentation，避免它的导入期自愈去碰实时文件。
from board_core import (build_board, contest_over, frozen_cell_ids, missing_from_roster,
                        problem_stats, teams_expected)
from pta_to_resolver_xml import compute_first_to_solve

TZ = timezone(timedelta(hours=8))          # 比赛/存档展示统一用北京时间
META_NAME = "meta.json"
CONTEST_NAME = "contest.json"
STATE_NAME = "state.json"
VIEW_NAME = "view.json"
INDEX_NAME = "index.json"
_SLUG_RE = re.compile(r"^[0-9A-Za-z._-]+$")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    """meta 可能被人工改坏；列表页的计数不能因为一个字符串就 500。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _dt(value: Any) -> datetime | None:
    """解析 epoch / ISO 时间字符串；解析不了返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromtimestamp(float(text), timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_time(value: Any, with_date: bool = True) -> str:
    dt = _dt(value)
    if dt is None:
        return "--"
    dt = dt.astimezone(TZ)
    return dt.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M")


def _fmt_date(value: Any) -> str:
    dt = _dt(value)
    return dt.astimezone(TZ).strftime("%Y-%m-%d") if dt else "--"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


# ---------------------------------------------------------------------------
# 存档只读访问
# ---------------------------------------------------------------------------
class ArchiveStore:
    """一个 archives 目录的只读视图；所有方法都不会写盘。"""

    def __init__(self, root: str | os.PathLike[str], web_root: Path, font_path: str | None = None,
                 penalty: int | None = None):
        self.root = Path(root).resolve()
        self.web_root = Path(web_root).resolve()
        self.font_override = Path(font_path).resolve() if font_path else None
        self.penalty = penalty

    # ---- 索引 -------------------------------------------------------------
    def entries(self) -> list[dict]:
        """列表页数据：优先 index.json；坏/缺索引时扫描目录（不写回，保持只读）。"""
        data = _read_json(self.root / INDEX_NAME)
        if isinstance(data, list):
            rows = [x for x in data if isinstance(x, dict) and x.get("slug")]
        else:
            rows = []
            try:
                children = list(self.root.iterdir())
            except OSError:
                children = []
            for child in children:
                if not child.is_dir():
                    continue
                meta = _read_json(child / META_NAME)
                if isinstance(meta, dict) and meta.get("slug"):
                    rows.append(meta)
        return sorted(rows, key=_entry_sort_key)

    @staticmethod
    def _dir(slug: str) -> str:
        return str(slug)

    def archive_dir(self, slug: str) -> Path | None:
        """slug 只允许单段安全字符，挡掉 ``../`` 之类的路径穿越。"""
        slug = str(slug or "")
        if not _SLUG_RE.fullmatch(slug) or slug in (".", ".."):
            return None
        path = (self.root / slug).resolve()
        if path.parent != self.root or not path.is_dir():
            return None
        return path

    def load(self, slug: str) -> dict | None:
        """读一场存档；缺 contest/meta 视为不存在。"""
        path = self.archive_dir(slug)
        if path is None:
            return None
        meta = _read_json(path / META_NAME)
        contest = _read_json(path / CONTEST_NAME)
        if not isinstance(meta, dict) or not isinstance(contest, dict):
            return None
        state = _read_json(path / STATE_NAME)
        view = _read_json(path / VIEW_NAME)
        return {
            "slug": str(meta.get("slug") or slug),
            "dir": path,
            "meta": meta,
            "contest": self._with_penalty(contest),
            "state": state if isinstance(state, dict) else {},
            "view": view if isinstance(view, dict) else {},
        }

    def _with_penalty(self, contest: dict) -> dict:
        """``--penalty`` 是显式覆盖；不传时沿用存档里 contest.contest.penalty_minutes。"""
        if self.penalty is None:
            return contest
        out = dict(contest)
        cmeta = dict(contest.get("contest") or {})
        cmeta["penalty_minutes"] = int(self.penalty)
        out["contest"] = cmeta
        return out

    def count(self) -> int:
        return len(self.entries())

    # ---- 校徽 / 字体 ------------------------------------------------------
    def _first_file(self, candidates: list[Path]) -> Path | None:
        for path in candidates:
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
        return None

    def _default_logo(self) -> Path | None:
        candidates = [
            self.root / "default.png", self.root / "logo" / "default.png",
            self.web_root / "default.png",
            BASE / "tmp" / "logo-cache" / "default.png",
        ]
        # 视觉配置里的校徽目录（live-board 的 logos_dir）可能还有一张默认图。
        for entry in self.entries()[:5]:
            cfg = _read_json(self.root / str(entry.get("slug")) / VIEW_NAME)
            if not isinstance(cfg, dict) or not cfg.get("logos_dir"):
                continue
            folder = Path(str(cfg["logos_dir"]))
            candidates += [folder / "default.png", folder / "default.jpg", folder / "default.jpeg"]
        return self._first_file(candidates)

    def _team_logo(self, slug: str, key: str) -> Path | None:
        """先找存档自带的 logo，再找 live 的校徽目录；都没有交给调用方回 default/404。"""
        safe = re.sub(r"[^0-9A-Za-z._-]+", "", str(key or "")).removesuffix(".png")
        if not safe:
            return None
        archive = self.archive_dir(slug)
        if archive is not None:
            for folder in (archive / "logo", archive / "logos"):
                found = self._first_file([folder / (safe + ext) for ext in (".png", ".jpg", ".jpeg", ".webp")])
                if found:
                    return found
        # live-board 已经裁好圆的缓存（如果存在，通常和当前比赛一致；找不到就算了）。
        cache = BASE / "tmp" / "logo-cache"
        found = self._first_file([cache / (safe + ext) for ext in (".png", ".jpg")])
        if found:
            return found
        # 原始校徽目录：按 userId / 学号 / 姓名找同名图片。
        data = self.load(slug)
        if data:
            team = next((t for t in (data["contest"].get("teams") or [])
                         if str(t.get("userId")) == safe), None)
            cfg = data.get("view") or {}
            folder_text = cfg.get("logos_dir") if isinstance(cfg, dict) else None
            if team and folder_text:
                folder = Path(str(folder_text))
                names = [safe, str(team.get("studentNumber") or ""), str(team.get("name") or "")]
                for name in names:
                    if not name:
                        continue
                    found = self._first_file([folder / (name + ext) for ext in (".png", ".jpg", ".jpeg", ".webp")])
                    if found:
                        return found
        return None

    def logo(self, slug: str | None, key: str) -> Path | None:
        """校徽查询绝不能把 500 抛给页面：任何异常都退回默认图或 404。"""
        key = str(key or "").removesuffix(".png")
        try:
            if key != "default" and slug is not None:
                found = self._team_logo(slug, key)
                if found:
                    return found
        except Exception:                                 # noqa: BLE001 - 坏存档也不许 500
            pass
        try:
            return self._default_logo()
        except Exception:                                 # noqa: BLE001
            return None

    def font(self) -> Path | None:
        """内置微软雅黑：CLI 覆盖 → 存档视觉配置 → 仓库常见位置（都找不到就 404）。"""
        candidates: list[Path] = []
        if self.font_override:
            candidates.append(self.font_override)
        for entry in self.entries()[:5]:
            cfg = _read_json(self.root / str(entry.get("slug")) / VIEW_NAME)
            if isinstance(cfg, dict) and cfg.get("font_path"):
                candidates.append(Path(str(cfg["font_path"])))
        candidates += [
            BASE.parent / "tools" / "fonts" / "MicrosoftYaHei.ttc",
            BASE.parent / "pta-resolver" / "tools" / "fonts" / "MicrosoftYaHei.ttc",
            BASE / "tools" / "fonts" / "MicrosoftYaHei.ttc",
        ]
        return self._first_file(candidates)


def _entry_sort_key(entry: dict) -> tuple:
    started = _dt(entry.get("started_at"))
    saved = _dt(entry.get("saved_at"))
    return (-(started.timestamp() if started else 0), -(saved.timestamp() if saved else 0),
            str(entry.get("slug") or ""))


# ---------------------------------------------------------------------------
# 存档 -> 展示投影
# ---------------------------------------------------------------------------
def _contest_meta(contest: dict) -> dict:
    cmeta = contest.get("contest")
    return cmeta if isinstance(cmeta, dict) else {}


def _duration(contest: dict) -> float:
    return max(0.0, _num(_contest_meta(contest).get("duration_seconds")))


def _freeze_at(contest: dict) -> float | None:
    cmeta = _contest_meta(contest)
    duration = _num(cmeta.get("duration_seconds"))
    freeze = _num(cmeta.get("freeze_seconds"))
    if duration > 0 and freeze > 0:
        return max(0.0, duration - freeze)
    return None


def _penalty(contest: dict) -> int:
    return int(_num(_contest_meta(contest).get("penalty_minutes"), 20) or 20)


def _final_elapsed(contest: dict, state: dict) -> float:
    """存档的展示时刻：比赛已结束就用赛程时长，未结束的存档用 state 里记的实际时刻。"""
    duration = _duration(contest)
    replay = state.get("presentation") if isinstance(state.get("presentation"), dict) else {}
    recorded = state.get("display_time")
    if recorded is None:
        recorded = replay.get("elapsed")
    elapsed = _num(recorded, -1)
    if duration > 0:
        if elapsed < 0 or elapsed >= duration - 5:
            return duration
        return min(duration, elapsed)
    if elapsed >= 0:
        return elapsed
    rels = [_num(s.get("rel_time_seconds"), 0) for s in (contest.get("submissions") or [])]
    return max(rels) if rels else 0.0


def _truncate(contest: dict, at: float) -> dict:
    """把提交截断到某一刻；题目/队伍名录保持完整（与 replay/presentation 口径一致）。"""
    out = dict(contest)
    out["submissions"] = [s for s in (contest.get("submissions") or [])
                          if _num(s.get("rel_time_seconds"), 0) <= at]
    return out


def _board_at(contest: dict, at: float) -> list[dict]:
    """某一刻的「实时/最终」榜单：先截断数据，再让 board_core 按完整口径排名。"""
    return build_board(_truncate(contest, at), display_time=None, freeze_mode="operator")


def _audience_board(contest: dict, freeze_at: float) -> list[dict]:
    """封榜快照：build_board 内部会先截断再排名，并补「封榜后提交」黄格标记。"""
    return build_board(contest, display_time=freeze_at, freeze_mode="audience")


def _final_board(contest: dict) -> list[dict]:
    """最终榜：不传 display_time，包含全部已判定结果（相当于 live 的 operator/thaw）。"""
    return build_board(contest, display_time=None, freeze_mode="operator")


def _first_to_solve(contest: dict, cap: float | None = None) -> list:
    data = _truncate(contest, cap) if cap is not None else contest
    try:
        return compute_first_to_solve(data, _penalty(contest))
    except Exception:                                     # noqa: BLE001 - 展示数据坏了也不许 500
        return []


def _problems_payload(contest: dict, at: float | None = None) -> list[dict]:
    try:
        stats = problem_stats(contest, at)
    except Exception:                                     # noqa: BLE001
        stats = {}
    out = []
    for p in contest.get("problems") or []:
        pid = str(p.get("id"))
        stat = stats.get(pid) or {}
        out.append({"id": p.get("id"), "label": p.get("label") or p.get("letter"),
                    "name": p.get("name"), "ac_teams": stat.get("ac_teams", 0),
                    "subs": stat.get("subs", 0)})
    return out


def _subs_by_team(contest: dict, cutoff: float | None) -> dict:
    """与 presentation.annotate_submissions 同口径：封榜后的提交照列，但判定写「待判」。"""
    out: dict[str, list] = {}
    for sub in contest.get("submissions") or []:
        rel = _num(sub.get("rel_time_seconds"), 0)
        tid = str(sub.get("userId") or "")
        pid = str(sub.get("problemSetProblemId") or "")
        if not tid or not pid:
            continue
        status = str(sub.get("status") or "")
        if cutoff is not None and rel > cutoff:
            st = "COMPILE_ERROR" if status.upper() == "COMPILE_ERROR" else "PENDING"
        else:
            st = status
        out.setdefault(tid, []).append([pid, int(round(rel)), st, str(sub.get("compiler") or "")])
    for rows in out.values():
        rows.sort(key=lambda x: x[1])
    return out


def _problem_labels(contest: dict) -> dict:
    return {str(p.get("id")): str(p.get("label") or "") for p in (contest.get("problems") or [])}


def _star_ids(view: dict) -> set[str]:
    """打星名单只做展示标记，不影响排名/罚时（与 presentation.annotate_stars 一致）。"""
    cfg = view if isinstance(view, dict) else {}
    v = cfg.get("view") if isinstance(cfg.get("view"), dict) else {}
    raw = v.get("star_team_ids")
    if raw is None:
        raw = cfg.get("star_team_ids")
    return {str(x) for x in (raw or []) if str(x).strip()}


def _annotate_stars(board: list[dict], view: dict) -> list[dict]:
    ids = _star_ids(view)
    if ids:
        for row in board:
            row["star"] = str(row.get("team_id")) in ids
    return board


def _completeness(contest: dict, board: list[dict]) -> dict:
    try:
        expected = teams_expected(contest)
        missing = missing_from_roster(contest)
    except Exception:                                     # noqa: BLE001
        expected, missing = -1, []
    report = contest.get("fetch_report") or {}
    roster = len(contest.get("teams") or [])
    if report.get("truncated") and expected >= 0:
        expected = max(expected, roster)
    active = {str(s.get("userId")) for s in (contest.get("submissions") or [])
              if s.get("userId") is not None}
    complete = bool(expected >= 0 and len(board) >= expected and not report.get("truncated"))
    note = None
    if not complete and expected >= 0:
        note = "数据可能不全（%d / 期望 %d）" % (len(board), expected)
    return {
        "teams_expected": expected,
        "teams_active": len(active),
        "teams_roster": roster,
        "teams_missing_from_roster": len(missing),
        "data_incomplete": not complete,
        "data_note": note,
        "fetch_report": report,
    }


def build_view(data: dict, at: float | None = None) -> dict:
    """把一份存档投影成 viewer state。

    返回 ``{"state": 类 state, "info": 类 presentation.metadata}``：
    * ``at is None``：比赛最后一刻；未解榜且已过封榜时刻 -> 观众封榜快照；
    * ``at`` 有值：时间轴 seek（只读，不写任何文件）。
    """
    contest = data["contest"]
    meta = data["meta"]
    state = data["state"]
    revealed = bool(meta.get("revealed"))
    duration = _duration(contest)
    freeze_at = _freeze_at(contest)
    final_at = _final_elapsed(contest, state)

    if at is None:
        elapsed = final_at
        if revealed:
            frozen = False
            board = _final_board(contest)
            first = _first_to_solve(contest)
            problems_at = None
        elif freeze_at is not None and elapsed >= freeze_at:
            frozen = True
            board = _audience_board(contest, freeze_at)
            first = _first_to_solve(contest, freeze_at)
            problems_at = None
        else:
            frozen = False
            board = _board_at(contest, elapsed)
            first = _first_to_solve(contest, elapsed)
            problems_at = elapsed
        seek_at = None
    else:
        elapsed = max(0.0, min(duration, at) if duration > 0 else at)
        seek_at = elapsed
        if revealed:
            frozen = False
            board = _board_at(contest, elapsed)
            first = _first_to_solve(contest, elapsed)
            problems_at = elapsed
        elif freeze_at is not None and elapsed >= freeze_at:
            frozen = True
            board = _audience_board(contest, freeze_at)
            first = _first_to_solve(contest, freeze_at)
            problems_at = elapsed
        else:
            frozen = False
            board = _board_at(contest, elapsed)
            first = _first_to_solve(contest, elapsed)
            problems_at = elapsed

    board = _annotate_stars(board, data["view"])
    over = bool(duration and elapsed >= duration)
    if revealed:
        cutoff = None
    elif frozen:
        cutoff = freeze_at
    else:
        cutoff = elapsed

    frozen_cells = []
    if frozen and freeze_at is not None:
        try:
            frozen_cells = frozen_cell_ids(contest, freeze_at, elapsed)
        except Exception:                                 # noqa: BLE001
            frozen_cells = []

    # 存档页的“更新”表示这份快照的归档时间；实时接口里这个位置是 last_poll。
    updated = meta.get("saved_at") or state.get("last_poll") or _iso_now()
    mode = "operator" if revealed else "audience"
    view_state = {
        "updated": updated,
        "channel": state.get("channel") or "archive",
        "frozen": frozen,
        "freeze_at": freeze_at,
        "problems": _problems_payload(contest, problems_at),
        "board": board,
        "teams": len(board),
        "freeze_mode": mode,
        "thaw": revealed,
        "contest_over": over,
        "freeze_snapshot_at": freeze_at if frozen else None,
        "frozen_cells": list(frozen_cells) if frozen else [],
        "subs_by_team": _subs_by_team(contest, cutoff),
        "problem_labels": _problem_labels(contest),
    }
    # 封榜快照只统计到封榜时刻：期望队伍数也要用同一份截断数据，
    # 否则「封榜后才首次提交的队伍」会被误报成“数据可能不全（16 / 期望 17）”。
    complete_src = contest if revealed else _truncate(contest, cutoff if cutoff is not None else elapsed)
    view_state.update(_completeness(complete_src, board))
    info = {
        "contest": _contest_meta(contest),
        "elapsed": elapsed,
        "speed": 1,
        "replay": seek_at is not None,
        "checkpoint": None,
        "held": False,
        "first_to_solve": first,
        "locked_cells": [],
        "freeze_at": freeze_at,
        "frozen": frozen,
        "freeze_mode": mode,
        "thaw": revealed,
        "contest_over": over,
        "freeze_snapshot_at": freeze_at if frozen else None,
        "frozen_cells": list(frozen_cells) if frozen else [],
        "seek_at": seek_at,
    }
    return {"state": view_state, "info": info}


# ---------------------------------------------------------------------------
# 列表页
# ---------------------------------------------------------------------------
def _short_id(slug: str) -> str:
    """列表页 ID 列：slug 的 sha1 前 8 位；同一场存档永远得到同一串短哈希。"""
    return hashlib.sha1(str(slug).encode("utf-8")).hexdigest()[:8]


def _fmt_updated(value: Any) -> str:
    """更新列只要「月-日 时:分」，完整时间留给 title/悬浮提示。"""
    dt = _dt(value)
    return dt.astimezone(TZ).strftime("%m-%d %H:%M") if dt else "--"


def _timing_of(entry: dict, now: datetime) -> tuple[bool, bool]:
    """返回 (是否进行中, 是否即将开始)；进行中严格按 started_at <= now < ended_at。"""
    started = _dt(entry.get("started_at"))
    ended = _dt(entry.get("ended_at"))
    live = bool(started and ended and started <= now < ended)
    upcoming = bool(started and now < started)
    return live, upcoming


def _status_of(entry: dict, now: datetime) -> tuple[str, str]:
    """列表行的状态徽标：进行中（红）/ 已解榜（绿）/ 已封存（灰）。

    行上只保留用户要求的三种徽标；“即将开始/已结束”的区分体现在标题旁的统计行，
    因为往期站点里的存档绝大多数是赛后冻结的，行内再塞第四种状态会分散注意力。
    """
    if entry.get("revealed"):
        return "已解榜", "revealed"
    live, _upcoming = _timing_of(entry, now)
    if live:
        return "进行中", "live"
    return "已封存", "sealed"


def render_list_page(store: ArchiveStore) -> str:
    """外榜列表页：CCPC 外榜风格的顶部条 + 标题统计 + 搜索 + 六列表格。

    只做展示：数据仍然来自 index.json 的 meta，行链接仍然指向 /outrank/<slug>。
    主题、搜索过滤、序号重排全部是内联 JS；没有外部字体/脚本/CDN。
    """
    now = datetime.now(timezone.utc)
    rows: list[str] = []
    live_count = 0
    upcoming_count = 0
    for entry in store.entries():
        slug = str(entry.get("slug") or "")
        if not store.archive_dir(slug):
            continue
        title = str(entry.get("title") or "未命名比赛")
        label, cls = _status_of(entry, now)
        is_live, is_upcoming = _timing_of(entry, now)
        live_count += int(is_live)
        upcoming_count += int(is_upcoming)
        short = _short_id(slug)
        started = _fmt_time(entry.get("started_at"))
        ended = _fmt_time(entry.get("ended_at"))
        updated = _fmt_updated(entry.get("saved_at"))
        teams = _int(entry.get("teams"))
        subs = _int(entry.get("subs"))
        problems = _int(entry.get("problems"))
        psid = str(entry.get("psid") or "")
        counts = f"{teams} 队 · {subs} 提交 · {problems} 题"
        # 搜索匹配：标题 / 徽标文字 / 起止时间 / 短 ID / 题目集 ID / 统计数字。
        search = " ".join([title, label, started, ended, short, psid, counts,
                           str(entry.get("started_at") or ""), str(entry.get("ended_at") or "")]).lower()
        rows.append(
            f'<a class="list-row" href="/outrank/{_esc(quote(slug, safe=""))}" '
            f'data-search="{_esc(search)}" aria-label="{_esc(title)} 榜单">'
            f'<span class="col-num">{len(rows) + 1:02d}</span>'
            # M51（用户 2026-10-18）：列表不要 ID 列了
            f'<span class="col-event">'
            f'<span class="event-title">{_esc(title)}</span>'
            f'<span class="event-meta">'
            f'<span class="status {cls}"><i aria-hidden="true"></i>{_esc(label)}</span>'
            f'<span class="event-counts">{_esc(counts)}</span>'
            f'</span></span>'
            f'<span class="col-time">'
            f'<span class="time-lines">'
            f'<span class="time-line"><i>开始</i><b>{_esc(started)}</b></span>'
            f'<span class="time-line"><i>结束</i><b>{_esc(ended)}</b></span>'
            f'</span>'
            f'<span class="tz" title="北京时间 UTC+8">UTC+8</span>'
            f'</span>'
            f'<span class="col-updated" title="更新于 {_esc(updated)}">{_esc(updated)}</span>'
            f'<span class="col-arrow" aria-hidden="true">›</span>'
            f'</a>'
        )

    total = len(rows)
    rows_html = "\n      ".join(rows)
    empty_html = (
        "" if total else
        '<div class="empty-state" id="emptyStatic">还没有存档：比赛结束后在本机程序里点「存档」</div>'
    )
    page = """<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ScoreDeck 外榜 · 外榜列表</title>
<script>
(function () {
  try {
    if (localStorage.getItem('scoredesk-outrank-theme') === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    }
  } catch (e) {}
})();
</script>
<style>
:root {
  color-scheme: dark;
  --bg: #090c12;
  --bg-glow:
    radial-gradient(900px 420px at 12% -12%, rgba(74, 132, 255, .14), transparent 64%),
    radial-gradient(760px 380px at 92% 0%, rgba(75, 209, 139, .06), transparent 60%);
  --topbar: #0c1018;
  --panel: #0f141d;
  --panel-hover: #151c28;
  --thead: #0b1018;
  --search-bg: #0a0e16;
  --line: #202836;
  --line-soft: rgba(126, 148, 180, .12);
  --text: #e9eef8;
  --text-2: #c6d1e2;
  --muted: #8995a9;
  --dim: #5d687b;
  --blue: #73a9ff;
  --red: #ff6573;
  --green: #4bd18b;
  --amber: #f1c15e;
  --sealed: #8d98ab;
  --live-bg: rgba(255, 101, 115, .09);
  --live-line: rgba(255, 101, 115, .24);
  --revealed-bg: rgba(75, 209, 139, .09);
  --revealed-line: rgba(75, 209, 139, .24);
  --sealed-bg: rgba(141, 152, 171, .08);
  --sealed-line: rgba(141, 152, 171, .18);
  --focus: rgba(115, 169, 255, .22);
  --shadow: 0 22px 62px rgba(0, 0, 0, .34);
}
html[data-theme="light"] {
  color-scheme: light;
  --bg: #f3f5f9;
  --bg-glow:
    radial-gradient(900px 440px at 12% -12%, rgba(61, 122, 255, .08), transparent 64%),
    radial-gradient(760px 380px at 92% 0%, rgba(24, 160, 88, .05), transparent 60%);
  --topbar: #ffffff;
  --panel: #ffffff;
  --panel-hover: #f6f8fc;
  --thead: #fafbfd;
  --search-bg: #f8fafc;
  --line: #e2e7ef;
  --line-soft: rgba(70, 88, 116, .10);
  --text: #1a2230;
  --text-2: #384457;
  --muted: #6e798c;
  --dim: #98a2b3;
  --blue: #2563eb;
  --red: #dc3545;
  --green: #138a55;
  --amber: #b26f00;
  --sealed: #667085;
  --live-bg: rgba(220, 53, 69, .06);
  --live-line: rgba(220, 53, 69, .22);
  --revealed-bg: rgba(19, 138, 85, .07);
  --revealed-line: rgba(19, 138, 85, .22);
  --sealed-bg: rgba(102, 112, 133, .06);
  --sealed-line: rgba(102, 112, 133, .18);
  --focus: rgba(37, 99, 235, .15);
  --shadow: 0 18px 50px rgba(31, 45, 70, .08);
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body {
  color: var(--text);
  background: var(--bg-glow), var(--bg);
  font: 14px/1.55 "Segoe UI", "Microsoft YaHei", "PingFang SC", system-ui, -apple-system, sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: inherit; text-decoration: none; }
button { font: inherit; }
[hidden] { display: none !important; }

.topbar {
  width: 100%;
  background: var(--topbar);
  border-bottom: 1px solid var(--line);
}
.topbar-inner {
  width: min(1200px, calc(100% - 48px));
  height: 58px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.brand-mark {
  width: 29px; height: 29px; border-radius: 8px;
  display: grid; place-items: center; color: #fff;
  background: linear-gradient(145deg, #3b7cff, #2450c9);
  box-shadow: 0 6px 16px rgba(36, 80, 201, .28);
}
.brand-mark svg { width: 17px; height: 17px; }
.brand-name { font-size: 16px; font-weight: 700; letter-spacing: .01em; white-space: nowrap; }
.brand-sub {
  color: var(--dim); font-size: 10px; font-weight: 700;
  letter-spacing: .18em; margin-left: 2px; transform: translateY(1px);
}
.top-actions { display: flex; align-items: center; gap: 8px; }
.icon-btn {
  width: 32px; height: 32px; border: 1px solid var(--line); border-radius: 9px;
  background: transparent; color: var(--muted); display: grid; place-items: center;
  padding: 0; transition: color .16s, background .16s, border-color .16s;
}
button.icon-btn { cursor: pointer; }
button.icon-btn:hover { color: var(--text); background: var(--panel-hover); border-color: var(--dim); }
.icon-btn.lock { cursor: default; opacity: .82; }
.icon-btn svg { width: 16px; height: 16px; }
.theme-btn .moon { display: none; }
html[data-theme="light"] .theme-btn .sun { display: none; }
html[data-theme="light"] .theme-btn .moon { display: block; }

.shell { width: min(1200px, calc(100% - 48px)); margin: 0 auto; padding: 30px 0 34px; }
.page-head {
  display: flex; align-items: flex-end; justify-content: space-between;
  gap: 18px 28px; flex-wrap: wrap; margin-bottom: 18px;
}
.page-title { margin: 0; font-size: 27px; line-height: 1.2; font-weight: 700; letter-spacing: .01em; }
.stats { display: flex; align-items: baseline; gap: 8px; color: var(--muted); font-size: 12.5px; padding-bottom: 2px; }
.stat { display: inline-flex; align-items: baseline; gap: 5px; white-space: nowrap; }
.stat b { font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat.total b { color: var(--blue); }
.stat.live b { color: var(--red); }
.stat.upcoming b { color: var(--amber); }
.stat-dot { color: var(--dim); }

.toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.search {
  width: min(470px, 100%); height: 39px; display: flex; align-items: center; gap: 9px;
  padding: 0 12px; border: 1px solid var(--line); border-radius: 9px;
  background: var(--search-bg); transition: border-color .16s, box-shadow .16s;
}
.search:focus-within { border-color: var(--blue); box-shadow: 0 0 0 3px var(--focus); }
.search svg { width: 15px; height: 15px; color: var(--dim); flex: 0 0 auto; }
.search input {
  flex: 1; min-width: 0; border: 0; outline: 0; padding: 0;
  background: transparent; color: var(--text); font: inherit; font-size: 13px;
}
.search input::placeholder { color: var(--dim); }
.count { color: var(--muted); font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; }

.table-wrap {
  border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
  background: var(--panel); box-shadow: var(--shadow);
}
.list-table { min-width: 900px; }
.list-head, .list-row {
  display: grid;
  grid-template-columns: 54px minmax(300px, 1.65fr) 246px 116px 24px;
  align-items: center; column-gap: 16px; padding: 0 18px;
}
.list-head {
  height: 42px; background: var(--thead); color: var(--dim);
  border-bottom: 1px solid var(--line);
  font-size: 11px; font-weight: 600; letter-spacing: .08em;
}
.list-row {
  min-height: 79px; background: var(--panel);
  border-bottom: 1px solid var(--line-soft);
  transition: background .15s;
}
.list-row:last-child { border-bottom: 0; }
.list-row:hover { background: var(--panel-hover); }
.col-num { color: var(--dim); font: 600 13px/1 "Cascadia Mono", "Consolas", monospace; letter-spacing: .04em; }
.col-event { min-width: 0; display: flex; flex-direction: column; gap: 7px; }
.event-title { font-size: 15px; font-weight: 700; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.event-meta { display: flex; align-items: center; gap: 9px; min-width: 0; flex-wrap: wrap; }
.status {
  display: inline-flex; align-items: center; gap: 5px; padding: 1px 7px;
  border: 1px solid transparent; border-radius: 5px;
  font-size: 11px; line-height: 1.55; white-space: nowrap;
}
.status i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; display: inline-block; }
.status.live { color: var(--red); background: var(--live-bg); border-color: var(--live-line); }
.status.revealed { color: var(--green); background: var(--revealed-bg); border-color: var(--revealed-line); }
.status.sealed { color: var(--sealed); background: var(--sealed-bg); border-color: var(--sealed-line); }
.event-counts { color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.col-time { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; }
.time-lines { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.time-line { display: flex; align-items: baseline; gap: 7px; white-space: nowrap; }
.time-line i { color: var(--dim); font-style: normal; font-size: 10px; width: 22px; flex: 0 0 auto; }
.time-line b { color: var(--text-2); font: 500 12px/1.45 "Cascadia Mono", "Consolas", monospace; font-variant-numeric: tabular-nums; }
.tz {
  flex: 0 0 auto; border: 1px solid var(--line); border-radius: 4px;
  color: var(--dim); font: 10px/1 "Cascadia Mono", "Consolas", monospace;
  padding: 4px 5px; letter-spacing: .02em;
}
.col-updated { color: var(--muted); font: 12px/1.2 "Cascadia Mono", "Consolas", monospace; font-variant-numeric: tabular-nums; white-space: nowrap; }
.col-arrow { color: var(--dim); font-size: 21px; line-height: 1; text-align: right; transition: color .15s, transform .15s; }
.list-row:hover .col-arrow { color: var(--blue); transform: translateX(2px); }

.empty-state { padding: 54px 20px; text-align: center; color: var(--muted); font-size: 13px; }
.foot {
  margin-top: 18px; display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; color: var(--dim); font-size: 11.5px;
}
.foot-brand { letter-spacing: .14em; font-weight: 600; color: var(--muted); }

@media (max-width: 720px) {
  .topbar-inner, .shell { width: calc(100% - 28px); }
  .shell { padding-top: 22px; }
  .brand-sub { display: none; }
  .page-head { align-items: flex-start; flex-direction: column; gap: 8px; }
  .page-title { font-size: 23px; }
  .toolbar { align-items: stretch; flex-direction: column; gap: 8px; }
  .search { width: 100%; }
  .foot { flex-direction: column; align-items: flex-start; }
}
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4.5 4.5h6v6h-6zM13.5 4.5h6v6h-6zM4.5 13.5h6v6h-6z"></path>
          <path d="M13.5 13.5h6v6h-6z" opacity=".55"></path>
        </svg>
      </span>
      <span class="brand-name">ScoreDeck 外榜</span>
      <span class="brand-sub">OUTRANK</span>
    </div>
    <div class="top-actions">
      <button type="button" class="icon-btn theme-btn" id="themeToggle" aria-label="切换到浅色主题" title="切换主题">
        <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
          <circle cx="12" cy="12" r="4"></circle>
          <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4"></path>
        </svg>
        <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20 14.2A8.1 8.1 0 0 1 9.8 4a8.2 8.2 0 1 0 10.2 10.2z"></path>
        </svg>
      </button>
      <span class="icon-btn lock" role="img" aria-label="只读站点" title="只读站点">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <rect x="5" y="10.5" width="14" height="9.5" rx="2"></rect>
          <path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7"></path>
        </svg>
      </span>
    </div>
  </div>
</header>

<main class="shell">
  <div class="page-head">
    <h1 class="page-title">外榜列表</h1>
    <div class="stats" aria-label="外榜统计">
      <span class="stat total"><b>__TOTAL__</b><span>场外榜</span></span>
      <span class="stat-dot">·</span>
      <span class="stat live"><b>__LIVE__</b><span>进行中</span></span>
      <span class="stat-dot">·</span>
      <span class="stat upcoming"><b>__UPCOMING__</b><span>即将开始</span></span>
    </div>
  </div>

  <div class="toolbar">
    <label class="search">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" aria-hidden="true">
        <circle cx="11" cy="11" r="6.5"></circle>
        <path d="m20 20-3.4-3.4"></path>
      </svg>
      <input id="q" type="search" placeholder="搜索比赛名 / 状态 / 日期…" autocomplete="off" aria-label="搜索往期比赛">
    </label>
    <span class="count" id="count">共 __TOTAL__ 场</span>
  </div>

  <div class="table-wrap">
    <div class="list-table">
      <div class="list-head" aria-hidden="true">
        <span>#</span><span>比赛</span><span>时间</span><span>更新</span><span></span>
      </div>
      <div id="list">
      __ROWS__
      </div>
      __EMPTY__
      <div class="empty-state" id="emptyFilter" hidden>没有匹配的比赛</div>
    </div>
  </div>

  <footer class="foot">
    <span>数据来源：live-board 存档快照</span>
    <span class="foot-brand">WANZHOU / OUTRANK</span>
  </footer>
</main>

<script>
(function () {
  "use strict";

  // ---- 主题切换：默认深色；localStorage 记住选择，太阳/月亮图标随主题互换 ----
  var root = document.documentElement;
  var themeBtn = document.getElementById("themeToggle");
  function currentTheme() { return root.getAttribute("data-theme") === "light" ? "light" : "dark"; }
  function syncThemeButton() {
    var light = currentTheme() === "light";
    themeBtn.setAttribute("aria-label", light ? "切换到深色主题" : "切换到浅色主题");
    themeBtn.title = light ? "切换到深色主题" : "切换到浅色主题";
  }
  themeBtn.addEventListener("click", function () {
    var next = currentTheme() === "light" ? "dark" : "light";
    if (next === "light") root.setAttribute("data-theme", "light");
    else root.setAttribute("data-theme", "dark");
    try { localStorage.setItem("scoredesk-outrank-theme", next); } catch (e) {}
    syncThemeButton();
  });
  syncThemeButton();

  // ---- 搜索：纯前端过滤；过滤后序号从 01 重排，「共 N 场」同步变化 ----
  var q = document.getElementById("q");
  var count = document.getElementById("count");
  var emptyFilter = document.getElementById("emptyFilter");
  var emptyStatic = document.getElementById("emptyStatic");
  var rows = Array.prototype.slice.call(document.querySelectorAll(".list-row"));
  var initialCount = rows.length;
  function applyFilter() {
    var key = (q.value || "").trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (row) {
      var hit = !key || (row.getAttribute("data-search") || "").indexOf(key) >= 0;
      row.hidden = !hit;
      if (hit) {
        shown += 1;
        var num = row.querySelector(".col-num");
        if (num) num.textContent = String(shown).padStart(2, "0");
      }
    });
    count.textContent = "共 " + shown + " 场";
    if (emptyStatic) emptyStatic.hidden = initialCount > 0;
    if (emptyFilter) emptyFilter.hidden = !(initialCount > 0 && shown === 0);
  }
  q.addEventListener("input", applyFilter);
  q.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { q.value = ""; applyFilter(); }
  });
  applyFilter();
})();
</script>
</body>
</html>
"""
    return (page.replace("__TOTAL__", str(total))
                .replace("__LIVE__", str(live_count))
                .replace("__UPCOMING__", str(upcoming_count))
                .replace("__ROWS__", rows_html)
                .replace("__EMPTY__", empty_html))


# ---------------------------------------------------------------------------
# 单场榜单页：复用 web/board.html + 一个只读路由 shim
# ---------------------------------------------------------------------------
def _board_page(web_root: Path, slug: str, meta: dict) -> str:
    """返回 board.html 原文，并在 </head> 前注入 __OUTRANK__ + API/SSE 路由 shim。

    为什么不改 board.js
    -------------------
    board.js 里的 ``/api/board``、``/events``、``/logo/`` 都是 live 的绝对路径。
    往期站点在**另一个端口**上，直接复用会打到不存在的 live 服务。这里在页面里
    先包一层 fetch / EventSource：只把 ``/api/*`` 前缀改写到 ``/outrank/<slug>/api/*``，
    并用一次性的假 EventSource 代替 SSE（存档不会更新，不需要长连接）。
    board.js 本身一个字节都不改，live-board 的行为因此完全不受影响。
    """
    try:
        source = (web_root / "board.html").read_text(encoding="utf-8")
    except OSError:
        source = "<!doctype html><html><body><h1>board.html 不存在</h1></body></html>"
    api = "/outrank/" + quote(slug, safe="")
    conf = {"slug": slug, "api": api, "title": meta.get("title") or ""}
    shim = f"""
<script>
window.__OUTRANK__ = {json.dumps(conf, ensure_ascii=False).replace("</", "<\\/")};
(function () {{
  "use strict";
  var api = window.__OUTRANK__.api;
  var nativeFetch = window.fetch ? window.fetch.bind(window) : null;
  if (nativeFetch) {{
    window.fetch = function (input, init) {{
      var url = typeof input === "string" ? input : (input && input.url);
      if (url && url.charAt(0) === "/" && url.indexOf("/api/") === 0) {{
        var mapped = api + url;
        if (typeof input === "string") input = mapped;
        else input = new Request(mapped, input);
      }}
      return nativeFetch(input, init);
    }};
  }}
  // 存档页不需要 SSE：构造时拉一次 board+presentation，然后派发一个 update 事件。
  function ArchiveEventSource(url) {{
    var self = this;
    this.url = url; this.readyState = 0; this.listeners = {{}};
    this._timer = setTimeout(function () {{
      self.readyState = 1;
      try {{ if (self.onopen) self.onopen({{type: "open"}}); }} catch (e) {{}}
      Promise.all([
        nativeFetch(api + "/api/board").then(function (r) {{ return r.json(); }}),
        nativeFetch(api + "/api/presentation").then(function (r) {{ return r.json(); }})
      ]).then(function (pair) {{
        var payload = Object.assign({{}}, pair[0], {{last_poll: pair[0].updated, presentation: pair[1]}});
        var ev = {{type: "update", data: JSON.stringify(payload)}};
        (self.listeners.update || []).forEach(function (fn) {{
          try {{ fn.call(self, ev); }} catch (e) {{}}
        }});
      }}).catch(function () {{
        self.readyState = 2;
        try {{ if (self.onerror) self.onerror({{}}); }} catch (e) {{}}
      }});
    }}, 30);
  }}
  ArchiveEventSource.prototype.addEventListener = function (type, fn) {{
    (this.listeners[type] || (this.listeners[type] = [])).push(fn);
  }};
  ArchiveEventSource.prototype.removeEventListener = function (type, fn) {{
    var arr = this.listeners[type] || [];
    var i = arr.indexOf(fn);
    if (i >= 0) arr.splice(i, 1);
  }};
  ArchiveEventSource.prototype.close = function () {{
    clearTimeout(this._timer); this.readyState = 2;
  }};
  window.EventSource = ArchiveEventSource;
}})();
</script>
</head>"""
    return source.replace("</head>", shim, 1)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class OutrankHandler(BaseHTTPRequestHandler):
    server_version = "PTAOutrank/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):                    # noqa: A003 - 标准库签名
        # 公网站点默认不刷访问日志（避免无人值守时日志无限长）；排障可看异常堆栈。
        return

    # ---- 发送 ------------------------------------------------------------
    def _send(self, data: bytes, ctype: str, status: int = 200, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, value: Any, status: int = 200) -> None:
        self._send(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _not_found(self, message: str = "not found") -> None:
        self._send((message + "\n").encode("utf-8"), "text/plain; charset=utf-8", 404)

    def _404_json(self, message: str = "archive not found") -> None:
        self._json({"error": message}, 404)

    def _file(self, path: Path | None, ctype: str | None = None) -> None:
        if path is None:
            self._not_found()
            return
        try:
            data = path.read_bytes()
        except OSError:
            self._not_found()
            return
        if ctype is None:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
        # 统一 no-cache（对应需求）；静态资源也允许浏览器协商，不需要另设缓存头。
        self._send(data, ctype)

    # ---- 入口 ------------------------------------------------------------
    def do_GET(self):                                     # noqa: N802 - 标准库接口
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:                                 # noqa: BLE001 - 单请求异常不许拖垮进程
            traceback.print_exc()
            try:
                self._json({"error": "internal error"}, 500)
            except Exception:                             # noqa: BLE001
                pass

    def do_HEAD(self):                                    # noqa: N802
        self.do_GET()

    def _reject_write(self):
        self._send(b'{"error":"read only"}\n', "application/json; charset=utf-8", 405,
                   {"Allow": "GET, HEAD, OPTIONS"})

    do_POST = do_PUT = do_PATCH = do_DELETE = _reject_write

    def do_OPTIONS(self):                                 # noqa: N802
        self._send(b"", "text/plain; charset=utf-8", 204,
                   {"Allow": "GET, HEAD, OPTIONS",
                    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS"})

    # ---- 路由 ------------------------------------------------------------
    def _route(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        store: ArchiveStore = self.server.store             # type: ignore[attr-defined]

        if path in ("/", "/outrank", "/outrank/"):
            page = render_list_page(store).encode("utf-8")
            self._send(page, "text/html; charset=utf-8")
            return

        if path.startswith("/outrank/"):
            rest = path[len("/outrank/"):]
            slug, _, tail = rest.partition("/")
            if not slug:
                self._not_found()
                return
            if tail == "" or tail == "/":
                data = store.load(slug)
                if not data:
                    self._not_found("archive not found")
                    return
                page = _board_page(store.web_root, str(data["slug"]), data["meta"]).encode("utf-8")
                self._send(page, "text/html; charset=utf-8")
                return
            if tail == "api/board":
                self._api_board(store, slug, query)
                return
            if tail == "api/presentation":
                self._api_presentation(store, slug, query)
                return
            if tail == "api/config":
                self._api_config(store, slug)
                return
            if tail == "api/seek":
                self._api_seek(store, slug, query)
                return
            if tail == "api/refresh-full":
                # 只读站点：不发旗标、不写任何文件，明确告诉前端没有写能力。
                self._json({"ok": False, "queued": False, "pending": False,
                            "readonly": True, "note": "往期榜单为只读存档，不能触发全量刷新"})
                return
            if tail.startswith("logo/"):
                key = tail[len("logo/"):]
                self._file(store.logo(slug, key))
                return
            self._not_found()
            return

        if path.startswith("/web/"):
            self._web_static(store, path[len("/web/"):])
            return

        if path.startswith("/logo/"):
            key = path[len("/logo/"):].removesuffix(".png")
            self._file(store.logo(None, key))
            return

        if path == "/font/microsoft.ttc":
            self._file(store.font(), "font/ttf")
            return

        if path == "/health":
            self._json({"ok": True, "archives": store.count(), "readonly": True})
            return

        self._not_found()

    def _web_static(self, store: ArchiveStore, rel: str) -> None:
        """静态文件只从 --web-root 里取，并挡住 ``..`` 越界。"""
        if not rel or rel.endswith("/"):
            self._not_found()
            return
        root = store.web_root
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._not_found()
            return
        if not target.is_file():
            self._not_found()
            return
        self._file(target)

    def _api_data(self, store: ArchiveStore, slug: str, query: dict, seek: bool = False):
        data = store.load(slug)
        if not data:
            self._404_json()
            return None, None
        at = None
        if seek:
            raw = (query.get("at") or ["0"])[0]
            try:
                at = float(raw)
            except (TypeError, ValueError):
                at = 0.0
            duration = _duration(data["contest"])
            at = max(0.0, min(duration, at) if duration > 0 else max(0.0, at))
        return data, build_view(data, at)

    def _api_board(self, store: ArchiveStore, slug: str, query: dict) -> None:
        data, view = self._api_data(store, slug, query)
        if data is None:
            return
        self._json(view["state"])

    def _api_presentation(self, store: ArchiveStore, slug: str, query: dict) -> None:
        data, view = self._api_data(store, slug, query)
        if data is None:
            return
        self._json(view["info"])

    def _api_config(self, store: ArchiveStore, slug: str) -> None:
        data = store.load(slug)
        if not data:
            self._404_json()
            return
        cfg = data.get("view") or {}
        view = dict(cfg.get("view") or {}) if isinstance(cfg.get("view"), dict) else {}
        # 与 presentation.load_config 相同的“顶层旧键回填 view”逻辑（只读副本，不改存档）。
        for key in ("problem_colors", "medal_colors", "awards", "rows", "scroll", "scroll_speed",
                    "scroll_pause", "anim", "follow", "follow_pause", "follow_max"):
            if key in cfg and key not in view:
                view[key] = cfg[key]
        try:
            view["awards_str"] = [
                {**(a or {}), "team_ids": [str(x) for x in ((a or {}).get("team_ids") or [])]}
                for a in (view.get("awards") or [])
            ]
        except Exception:                                 # noqa: BLE001
            view["awards_str"] = []
        title = str(view.get("title") or cfg.get("title") or data["meta"].get("title") or "")
        mode = "operator" if data["meta"].get("revealed") else "audience"
        self._json({
            "title": title,
            "font": "/font/microsoft.ttc",
            "theme": cfg.get("theme") or {},
            "view": view,
            "port": self.server.server_address[1],
            "freeze_mode": mode,
            "archive": {"slug": data["slug"], "revealed": bool(data["meta"].get("revealed"))},
        })

    def _api_seek(self, store: ArchiveStore, slug: str, query: dict) -> None:
        data, view = self._api_data(store, slug, query, seek=True)
        if data is None:
            return
        payload = dict(view["state"])
        payload["presentation"] = view["info"]
        payload["last_poll"] = payload.get("updated")
        payload["seek_at"] = view["info"].get("seek_at")
        self._json(payload)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
class OutrankServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, store: ArchiveStore):
        super().__init__(addr, handler)
        self.store = store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="外榜站点：往期榜单存档只读服务（无写接口）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument("--archives", type=Path, default=DEFAULT_ARCHIVES, help="存档根目录")
    parser.add_argument("--web-root", type=Path, default=DEFAULT_WEB_ROOT, help="board.html/board.js/board.css 所在目录")
    parser.add_argument("--penalty", type=int, default=None,
                        help="罚时分钟数覆盖（不传则用每场存档 contest.contest.penalty_minutes）")
    parser.add_argument("--font", default=None, help="微软雅黑字体路径（可选）")
    args = parser.parse_args(argv)

    archives = args.archives.resolve()
    web_root = args.web_root.resolve()
    if not archives.is_dir():
        print(f"[warn] 存档目录不存在：{archives}（列表会为空；可用 archive.py 生成）", flush=True)
    store = ArchiveStore(archives, web_root, args.font, args.penalty)
    count = store.count()
    http = OutrankServer((args.host, args.port), OutrankHandler, store)
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"outrank listening on http://{shown_host}:{args.port}/  "
          f"(bind {args.host}:{args.port}, archives={archives}, 归档 {count} 场)",
          flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
