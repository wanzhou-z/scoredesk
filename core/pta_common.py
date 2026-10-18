# -*- coding: utf-8 -*-
"""
pta_common.py —— PTA 滚榜工具公共层

集中放置：
  * PTA 接口常量（URL / 参数）
  * 结果枚举 -> ICPC 判题缩写的映射表
  * 编译器 -> ICPC 语言 id 的映射表
  * 时间解析 / 时间戳换算
  * contest.json 规范化模型的读写与校验

本文件不含任何自创的"聪明逻辑"，所有端点和映射均来自对现有开源方案的复刻与实测：
  * https://github.com/Trouble-Maker11/PTAtoolExpand  (浏览器插件, background.js)
  * https://github.com/Trouble-Maker11/pta_resovler   (Python, src/pta_tool_class.py)
详细取证记录见 docs/api-notes.md。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------
# PTA 接口常量（全部为实测确认存在的非官方接口，详见 docs/api-notes.md）
# --------------------------------------------------------------------------

PTA_ORIGIN = "https://pintia.cn"
API_PREFIX = PTA_ORIGIN + "/api"

#: 默认目标比赛（空 = 必须由命令行 / 配置显式给出）
DEFAULT_PROBLEM_SET_ID = ""

#: 模拟真实浏览器，缺失 Referer / Accept 时 PTA 可能返回 HTML 兜底页
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Referer": PTA_ORIGIN + "/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": PTA_ORIGIN,
}

#: 登录态相关的 Cookie 名（插件只取这几个；实测 JSESSIONID + PTASession 是关键）
AUTH_COOKIE_NAMES = ("PTASession", "JSESSIONID")

#: 默认请求间隔（秒）。需求要求 200~300ms，取中值 0.25s。
DEFAULT_DELAY = 0.25
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 4
SUBMISSION_PAGE_SIZE = 100  # 已知 PTA 提交接口 limit 上限 100
TEAM_PAGE_SIZE = 20  # 沿用两个参考实现的取值


def api_problem_set(psid: str) -> str:
    """题目集基本信息（含 startAt / endAt / duration / name）。"""
    return f"{API_PREFIX}/problem-sets/{psid}"


def api_problem_set_exams(psid: str) -> str:
    """考试列表，用作权限校验探针（无权限时返回 403）。"""
    return f"{API_PREFIX}/problem-sets/{psid}/exams"


def api_preview_problems(psid: str) -> str:
    """题目列表。"""
    return f"{API_PREFIX}/problem-sets/{psid}/preview/problems"


def api_user_group_members(psid: str) -> str:
    """参赛者（队伍）列表 + 用户信息映射表。"""
    return f"{API_PREFIX}/problem-sets/{psid}/user-group-members"


def api_submissions(psid: str) -> str:
    """提交记录（游标分页）。"""
    return f"{API_PREFIX}/problem-sets/{psid}/submissions"


def api_problem_sets_admin() -> str:
    """当前账号可见的题目集列表（教师端）。"""
    return f"{API_PREFIX}/problem-sets/admin"


def preview_problems_params(page: int = 0, limit: int = 500) -> dict[str, Any]:
    return {"problem_type": "PROGRAMMING", "page": page, "limit": limit}


def user_group_members_params(page: int = 0, limit: int = TEAM_PAGE_SIZE) -> dict[str, Any]:
    # 两个参考实现使用完全相同的参数组合
    return {
        "exam_status": "UNKNOWN",
        "page": page,
        "limit": limit,
        "order_by": "startAt",
        "asc": "false",
    }


# --------------------------------------------------------------------------
# 结果枚举映射
# --------------------------------------------------------------------------
# PTA 的 submission.status -> ICPC Resolver 的 judgement acronym
# 两个参考实现给出完全一致的表，此处原样复刻。
PTA_STATUS_TO_ACRONYM: dict[str, str] = {
    "ACCEPTED": "AC",
    "WRONG_ANSWER": "WA",
    "TIME_LIMIT_EXCEEDED": "TLE",
    "COMPILE_ERROR": "CE",
    "SEGMENTATION_FAULT": "SF",
    "FLOAT_POINT_EXCEPTION": "FPE",
    "MEMORY_LIMIT_EXCEEDED": "MLE",
    "NON_ZERO_EXIT_CODE": "NZEC",
    "RUNTIME_ERROR": "RE",
    "PRESENTATION_ERROR": "PE",
    "OUTPUT_LIMIT_EXCEEDED": "OLE",
}

#: 判题结果定义，顺序与两个参考实现一致（id 1..11）
JUDGEMENTS: list[dict[str, Any]] = [
    {"id": "1", "acronym": "AC", "name": "ACCEPTED", "solved": True, "penalty": False},
    {"id": "2", "acronym": "SF", "name": "SEGMENTATION_FAULT", "solved": False, "penalty": True},
    {"id": "3", "acronym": "WA", "name": "WRONG_ANSWER", "solved": False, "penalty": True},
    {"id": "4", "acronym": "TLE", "name": "TIME_LIMIT_EXCEEDED", "solved": False, "penalty": True},
    {"id": "5", "acronym": "CE", "name": "COMPILE_ERROR", "solved": False, "penalty": False},
    {"id": "6", "acronym": "FPE", "name": "FLOAT_POINT_EXCEPTION", "solved": False, "penalty": True},
    {"id": "7", "acronym": "MLE", "name": "MEMORY_LIMIT_EXCEEDED", "solved": False, "penalty": True},
    {"id": "8", "acronym": "NZEC", "name": "NON_ZERO_EXIT_CODE", "solved": False, "penalty": True},
    {"id": "9", "acronym": "RE", "name": "RUNTIME_ERROR", "solved": False, "penalty": True},
    {"id": "10", "acronym": "PE", "name": "PRESENTATION_ERROR", "solved": False, "penalty": True},
    {"id": "11", "acronym": "OLE", "name": "OUTPUT_LIMIT_EXCEEDED", "solved": False, "penalty": True},
]

#: 若 PTA 出现参考实现未覆盖的枚举，映射到 WA 并在运行时报 warning。
#: （PTA 确实存在未在参考实现中列出的状态，例如评测中/部分正确，见 docs/api-notes.md 的"待验证"）
UNKNOWN_STATUS_FALLBACK = "WA"

#: PTA 编译器字符串 -> ICPC language id（与参考实现一致）
COMPILER_TO_LANG_ID: dict[str, str] = {
    "GCC": "1",
    "CLANG": "1",
    "MODERN_GCC": "1",
    "GXX": "2",
    "CLANGXX": "2",
    "C++": "2",
    "JAVA": "3",
    "JAVAC": "3",
    "PYTHON3": "4",
    "PYTHON2": "4",
    "PYPY3": "4",
}

LANGUAGES: list[dict[str, str]] = [
    {"id": "1", "name": "c"},
    {"id": "2", "name": "c++"},
    {"id": "3", "name": "java"},
    {"id": "4", "name": "python"},
]

LANG_ID_TO_NAME: dict[str, str] = {lang["id"]: lang["name"] for lang in LANGUAGES}

#: 无解/未知编译器时的兜底语言 id
UNKNOWN_COMPILER_FALLBACK = "1"


def compiler_to_language_name(compiler: Any) -> tuple[str, bool]:
    """PTA 编译器字符串 -> XMLFeedParser 能识别的语言**名称**。

    !! 关键坑（已实测）!!
    XMLFeedParser 解析 <run><language> 时执行的是
        for (ILanguage l : contest.getLanguages())
            if (l.getName().equals(p.value)) add(s, "language_id", l.getId());
    也就是说 <language> 里必须写语言的 **name**（c / c++ / java / python），
    写数字 id 会被静默丢弃 —— 提交的语言会变成 null。
    参考实现 PTAtoolExpand 与 pta_resovler 都写成数字 id，因此都存在这个缺陷。

    返回 (语言名称, 是否命中映射表)。
    """
    if not compiler or not isinstance(compiler, str):
        return LANGUAGES[0]["name"], False
    lang_id = COMPILER_TO_LANG_ID.get(compiler.upper())
    if lang_id is None:
        return LANGUAGES[0]["name"], False
    return LANG_ID_TO_NAME.get(lang_id, LANGUAGES[0]["name"]), True


# --------------------------------------------------------------------------
# 时间处理
# --------------------------------------------------------------------------

def parse_iso(value: Any) -> datetime | None:
    """解析 PTA 返回的 ISO-8601 时间（形如 2026-10-08T14:00:00.000Z）。

    解析失败或为空返回 None。
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # 容错：PTA 偶尔出现 6 位小数秒
        try:
            head, _, rest = text.partition(".")
            if not rest:
                return None
            frac, _, tz = rest.partition("+")
            dt = datetime.fromisoformat(f"{head}.{frac[:6]}+{tz}")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_epoch(dt: datetime | None) -> float | None:
    return None if dt is None else dt.timestamp()


def format_hms(seconds: float) -> str:
    """秒 -> H:MM:SS（Resolver 的 length / freeze 格式）。"""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def parse_hms(text: str) -> int:
    """H:MM:SS 或 MM:SS 或纯秒数 -> 秒。"""
    text = str(text).strip()
    if not text:
        raise ValueError("空的时间长度")
    if ":" not in text:
        return int(float(text))
    parts = [p for p in text.split(":")]
    total = 0
    for part in parts:
        total = total * 60 + int(float(part))
    return total


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def now_iso() -> str:
    return iso(datetime.now(timezone.utc)) or ""


# --------------------------------------------------------------------------
# 规范化模型
# --------------------------------------------------------------------------

def empty_contest(psid: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": "pta-resolver",
        "problem_set_id": psid,
        "fetched_at": None,
        "source": {
            "origin": PTA_ORIGIN,
            "rankings_url": f"{PTA_ORIGIN}/problem-sets/{psid}/rankings",
        },
        "problem_set": {},
        "contest": {
            "title": None,
            "start_epoch": None,
            "end_epoch": None,
            "duration_seconds": None,
            "freeze_seconds": 3600,
            "penalty_minutes": 20,
            "start_source": None,
        },
        "problems": [],
        "teams": [],
        "submissions": [],
        "warnings": [],
        "stats": {},
    }


def resolve_contest_start(
    problem_set: dict[str, Any],
    submissions: list[dict[str, Any]],
    warnings: list[str],
) -> tuple[datetime, str, float | None]:
    """确定比赛开始时间，返回 (start_dt, 来源说明, end_dt)。

    PTA 的 startAt 在题目集未发布/未开始时可能是占位值（参考实现提到会显示"元年"），
    直接使用会导致所有 run 的相对时间为天文数字。因此按以下优先级回退：

      1. problemSet.startAt（年份 >= 1970 时可信）
      2. endAt - duration
      3. 最早的一条提交时间（推断值，会在 warnings 中标注）
      4. 当前时间 - duration
    """
    start_dt = parse_iso(problem_set.get("startAt"))
    end_dt = parse_iso(problem_set.get("endAt"))
    duration = problem_set.get("duration") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0

    if start_dt is not None and start_dt.year >= 1970:
        return start_dt, "problemSet.startAt", end_dt

    if start_dt is not None:
        warnings.append(
            f"problemSet.startAt={problem_set.get('startAt')!r} 是占位值（年份 < 1970），已忽略。"
        )

    if end_dt is not None and duration > 0:
        warnings.append("使用 endAt - duration 推断比赛开始时间。")
        return end_dt - timedelta(seconds=duration), "endAt-duration", end_dt

    if end_dt is not None and end_dt.year >= 1970:
        warnings.append("使用 endAt 作为比赛结束时间，开始时间取最早提交（推断值）。")
        inferred = _earliest_submission(submissions)
        if inferred is not None:
            return inferred, "earliest-submission(inferred)", end_dt

    inferred = _earliest_submission(submissions)
    if inferred is not None:
        warnings.append(
            "PTA 未提供可用的 startAt/endAt，比赛开始时间取最早一条提交，"
            "滚榜时长可能与真实比赛不符，请用 --start 手动指定。"
        )
        return inferred, "earliest-submission(inferred)", end_dt

    warnings.append("完全无法确定比赛开始时间，回落到当前时间。请用 --start 手动指定。")
    return datetime.now(timezone.utc), "now(fallback)", end_dt


def _earliest_submission(submissions: list[dict[str, Any]]) -> datetime | None:
    best: datetime | None = None
    for sub in submissions:
        dt = parse_iso(sub.get("submitAt"))
        if dt is not None and (best is None or dt < best):
            best = dt
    return best


def normalize_submission(sub: dict[str, Any], start_epoch: float) -> dict[str, Any]:
    """把一条 PTA 原始提交规范化为 contest.json 中的条目。"""
    submit_dt = parse_iso(sub.get("submitAt"))
    submit_epoch = to_epoch(submit_dt)
    rel = None
    if submit_epoch is not None:
        rel = int(round(submit_epoch - start_epoch))
    status = (sub.get("status") or "UNKNOWN").upper()
    return {
        "id": str(sub.get("id")) if sub.get("id") is not None else None,
        "userId": str(sub.get("userId")) if sub.get("userId") is not None else None,
        "problemSetProblemId": (
            str(sub.get("problemSetProblemId"))
            if sub.get("problemSetProblemId") is not None
            else None
        ),
        "status": status,
        "compiler": (sub.get("compiler") or "").upper() or None,
        "submitAt": iso(submit_dt),
        "submit_epoch": submit_epoch,
        "rel_time_seconds": rel,
        "score": sub.get("score"),
        "time": sub.get("time"),
        "memory": sub.get("memory"),
        "raw": sub,
    }


def load_contest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "submissions" not in data:
        raise ValueError(f"{path} 不是本工具生成的 contest.json")
    return data


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """临时文件 → flush + fsync → ``os.replace``（备份文件也走同一套，别被蓝屏写坏）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_contest(path: Path, contest: dict[str, Any]) -> None:
    """原子写 ``contest.json``：临时文件 → flush + **fsync** → 留 1 份 ``.bak`` → ``os.replace``。

    这里是**实时榜单真正的那次落盘**：``live-board\\poller.py`` 通过
    ``pta_fetch.py fetch --out <live-board>\\contest.json`` 调到的就是本函数。

    旧实现只有 ``tmp.replace(path)``、**没有 fsync**：重命名（目录项）先落盘，数据还在页
    缓存里。2026-10-11 18:48 的蓝屏（bugcheck 0x0000000A）之后 ``contest.json`` / ``state.json``
    尾部成片 NUL，``json.loads`` 报 ``Expecting value: line 1 column 1 (char 0)``，
    ``server.py`` 直接崩在导入期。先 fsync 再 replace，磁盘上只会是「旧内容」或「完整新内容」。
    覆盖前把上一份好数据留成 ``contest.json.bak``（只保留 1 份，硬链接 O(1)，
    且 ``contest.json`` 在两次重命名之间**不会消失**），供读侧容错与启动自愈使用。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 保持与旧实现**逐字节一致**的输出（缩进 2、结尾换行、Windows 文本模式的 CRLF），
    # 这样升级前后同内容的 contest.json 不会因为换行风格产生无意义的 diff。
    text = json.dumps(contest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    payload = text.replace("\n", "\r\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if path.is_file():
            bak = path.with_name(path.name + ".bak")
            try:
                if bak.exists():
                    os.unlink(bak)
                os.link(path, bak)          # O(1)，且 path 始终在位
            except OSError:
                try:
                    _atomic_write_bytes(bak, path.read_bytes())
                except OSError:
                    pass
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def contest_stats(contest: dict[str, Any]) -> dict[str, Any]:
    """汇总统计，doctor 与 fetch 共用。"""
    subs = contest.get("submissions") or []
    statuses: dict[str, int] = {}
    for sub in subs:
        statuses[sub.get("status") or "UNKNOWN"] = (
            statuses.get(sub.get("status") or "UNKNOWN", 0) + 1
        )
    epochs = [s["submit_epoch"] for s in subs if s.get("submit_epoch") is not None]
    rels = [s["rel_time_seconds"] for s in subs if s.get("rel_time_seconds") is not None]
    return {
        "problem_count": len(contest.get("problems") or []),
        "team_count": len(contest.get("teams") or []),
        "submission_count": len(subs),
        "status_counts": dict(sorted(statuses.items(), key=lambda kv: -kv[1])),
        "submit_min": iso(datetime.fromtimestamp(min(epochs), timezone.utc)) if epochs else None,
        "submit_max": iso(datetime.fromtimestamp(max(epochs), timezone.utc)) if epochs else None,
        "rel_min": min(rels) if rels else None,
        "rel_max": max(rels) if rels else None,
    }


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> "None":
    print(f"[error] {msg}", file=sys.stderr)
    raise SystemExit(code)
