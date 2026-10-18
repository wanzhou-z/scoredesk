#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pta_fetch.py —— 从 PTA（拼题A, pintia.cn）抓取比赛数据，落盘为 contest.json

用法示例
--------
  # 0) 只读探测：确认 Cookie 有权限、接口可达（不写任何文件）
  python pta_fetch.py --check

  # 1) 抓取（Cookie 从环境变量 PTA_COOKIE 读）
  python pta_fetch.py fetch --problem-set <题目集ID>

  # 2) 抓取（Cookie 从文件读）
  python pta_fetch.py fetch --cookie-file cookie.txt

  # 3) 增量刷新（默认行为：只追加新提交，复用已缓存的题目/队伍）
  python pta_fetch.py fetch

  # 4) 全量重抓
  python pta_fetch.py fetch --full

所有 PTA 接口均为非官方接口，端点和参数复刻自以下两个现有开源方案（详见 docs/api-notes.md）：
  * https://github.com/Trouble-Maker11/PTAtoolExpand
  * https://github.com/Trouble-Maker11/pta_resovler
本工具不索取账号密码，Cookie 由用户自行从浏览器复制。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

import pta_common as C  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    print(
        "缺少依赖 requests。请先执行：\n    pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2)


def make_source(args, client=None):
    """取数通道只有一条：直连接口（requests + JSON，需要 Cookie）。

    2026-10-13：「借用浏览器」通道已整体移除 —— 它会在用户的
    浏览器里产生下载、影响观感，而且会把轮询变成“偷偷借浏览器”的行为。
    现在必须提供有效 Cookie；没有 Cookie 就直说取不到数（见 --help）。
    """
    return PTAFetcher(client, args.problem_set)


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class PTAError(Exception):
    """所有 PTA 相关错误的基类。"""


class PTAAuthError(PTAError):
    """401：Cookie 失效/未登录。"""


class PTAPermissionError(PTAError):
    """403：已登录但无权限（例如学生账号访问教师端接口）。"""


class PTAApiError(PTAError):
    """其它非预期响应（含返回 HTML 兜底页）。"""


class PTANetworkError(PTAError):
    """底层网络错误或重试耗尽。"""


# --------------------------------------------------------------------------
# Cookie 读取
# --------------------------------------------------------------------------

COOKIE_FILE_CANDIDATES = (
    "cookie.txt",
    "pta_cookie.txt",
    "cookies.txt",
    "cookie.json",
    "pta_cookie.json",
)


def _cookie_from_json_blob(text: str) -> str | None:
    """支持 {"PTASession": "...", "JSESSIONID": "..."} 形式的 JSON。"""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict):
        # 允许 {"cookies": {...}} 包裹
        if "cookies" in obj and isinstance(obj["cookies"], dict):
            obj = obj["cookies"]
        pairs = [f"{k}={v}" for k, v in obj.items() if isinstance(v, (str, int))]
        return "; ".join(pairs) if pairs else None
    if isinstance(obj, list):
        pairs = []
        for item in obj:
            if isinstance(item, dict) and "name" in item and "value" in item:
                pairs.append(f"{item['name']}={item['value']}")
        return "; ".join(pairs) if pairs else None
    return None


def _cookie_from_netscape(text: str) -> str | None:
    """支持浏览器/curl 导出的 Netscape cookies.txt。"""
    pairs: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            pairs.append(f"{parts[5]}={parts[6]}")
    return "; ".join(pairs) if pairs else None


def normalize_cookie(raw: str) -> str:
    """把各种形式的 Cookie 文本规范化为 'a=1; b=2' 请求头。"""
    text = raw.strip()
    if not text:
        return ""
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    # 去掉可能包裹的引号（PowerShell 复制时常见）
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if text.lstrip().startswith("{") or text.lstrip().startswith("["):
        parsed = _cookie_from_json_blob(text)
        if parsed:
            return parsed
    if "\t" in text:
        parsed = _cookie_from_netscape(text)
        if parsed:
            return parsed
    # 单行 a=1; b=2 —— 统一分号后的空格
    pairs = []
    for chunk in text.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if chunk and "=" in chunk:
            pairs.append(chunk)
    return "; ".join(pairs)


def load_cookie(cookie_file: str | None, explicit: str | None) -> tuple[str, str]:
    """按 显式参数 > 环境变量 > 候选文件 的顺序取 Cookie。

    返回 (cookie_header, 来源说明)。
    """
    if explicit:
        return normalize_cookie(explicit), "--cookie"

    env = os.environ.get("PTA_COOKIE") or os.environ.get("PTA_COOKIES")
    if env and env.strip():
        return normalize_cookie(env), "环境变量 PTA_COOKIE"

    if cookie_file:
        path = Path(cookie_file)
        if not path.exists():
            C.die(f"--cookie-file 指定的文件不存在：{path}")
        return normalize_cookie(path.read_text(encoding="utf-8")), f"文件 {path}"

    for name in COOKIE_FILE_CANDIDATES:
        path = Path(name)
        if path.exists():
            return normalize_cookie(path.read_text(encoding="utf-8")), f"文件 {path}"

    C.die(
        "未找到 PTA Cookie。请任选一种方式：\n"
        "  1) 设置环境变量  PowerShell: $env:PTA_COOKIE='PTASession=...; JSESSIONID=...'\n"
        "  2) 把 Cookie 粘贴到当前目录的 cookie.txt\n"
        "  3) 使用 --cookie-file 指定文件\n"
        "如何获取 Cookie 见 README.md「第一步：获取 Cookie」。"
    )
    return "", ""  # 不可达


# --------------------------------------------------------------------------
# HTTP 客户端
# --------------------------------------------------------------------------

class PTAClient:
    """带限流与退避重试的 PTA API 客户端（单线程，绝不并发）。"""

    def __init__(
        self,
        cookie: str,
        delay: float = C.DEFAULT_DELAY,
        timeout: int = C.DEFAULT_TIMEOUT,
        max_retries: int = C.DEFAULT_MAX_RETRIES,
        verbose: bool = True,
    ) -> None:
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.max_retries = max_retries
        self.verbose = verbose
        self.request_count = 0
        self._last_request = 0.0

        self.session = requests.Session()
        self.session.headers.update(C.DEFAULT_HEADERS)
        if cookie:
            self.session.headers["Cookie"] = cookie

    # -- 内部 --------------------------------------------------------------

    def _throttle(self) -> None:
        """保证相邻请求间隔 >= delay，避免触发 PTA 反爬。"""
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr)

    # -- 对外 --------------------------------------------------------------

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self.max_retries:
            attempt += 1
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                self._backoff(attempt, f"网络错误：{exc}")
                continue
            finally:
                self._last_request = time.monotonic()
                self.request_count += 1

            code = resp.status_code

            if code in (429, 500, 502, 503, 504):
                last_error = PTAApiError(f"HTTP {code} @ {url}")
                self._backoff(attempt, f"HTTP {code}，退避重试")
                continue

            if code == 401:
                raise PTAAuthError(
                    f"HTTP 401 未授权（{url}）。\n"
                    "  -> Cookie 已过期或未提供。请重新从浏览器复制 Cookie（见 README）。"
                )
            if code == 403:
                raise PTAPermissionError(
                    f"HTTP 403 无权限（{url}）。\n"
                    "  -> 接口存在但当前账号无权访问。请确认使用【教师/管理员】账号的 Cookie；\n"
                    "     学生账号访问教师端接口会稳定返回 403。"
                )
            if code != 200:
                raise PTAApiError(f"HTTP {code} @ {url}")

            ctype = resp.headers.get("Content-Type", "")
            if "json" not in ctype.lower():
                snippet = (resp.text or "")[:160].replace("\n", " ")
                raise PTAApiError(
                    f"期望 JSON，实际得到 {ctype!r}（HTTP 200）@ {url}\n"
                    f"  -> 这通常是 PTA 返回的 SPA HTML 兜底页，说明接口路径不存在或未登录。\n"
                    f"     响应片段：{snippet!r}"
                )
            try:
                return resp.json()
            except ValueError as exc:
                raise PTAApiError(f"响应不是合法 JSON @ {url}：{exc}") from exc

        raise PTANetworkError(f"重试 {self.max_retries} 次后仍失败：{last_error}")

    def _backoff(self, attempt: int, reason: str) -> None:
        if attempt > self.max_retries:
            return
        wait = min(30.0, (2 ** (attempt - 1)) * 0.5)
        self._log(f"  [retry {attempt}/{self.max_retries}] {reason}，{wait:.1f}s 后重试")
        time.sleep(wait)


# --------------------------------------------------------------------------
# 抓取逻辑
# --------------------------------------------------------------------------

class PTAFetcher:
    def __init__(self, client: PTAClient, psid: str) -> None:
        self.client = client
        self.psid = psid

    # -- 元信息 ------------------------------------------------------------

    def fetch_problem_set(self) -> dict[str, Any]:
        data = self.client.get_json(C.api_problem_set(self.psid))
        return (data or {}).get("problemSet") or {}

    def verify_permission(self) -> str:
        """用 /exams 探针验证教师端权限。返回 'ok' 或抛异常。"""
        self.client.get_json(C.api_problem_set_exams(self.psid))
        return "ok"

    def fetch_problems(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 0
        while True:
            data = self.client.get_json(
                C.api_preview_problems(self.psid),
                C.preview_problems_params(page=page, limit=500),
            )
            items = (data or {}).get("problemSetProblems") or []
            if not items:
                break
            out.extend(items)
            if len(items) < 500:
                break
            page += 1
        return out

    def fetch_teams(self) -> list[dict[str, Any]]:
        """拉取参赛者，并用 examByUserId / studentUserById 解析队伍名**和学号**。

        M13：旧版本只写 ``userId / studentUserId / name``，把学号丢了 —— 结果就是
        用户反馈的「滚榜图片只认姓名、不认学号」（按学号命名的照片全部匹配不上）。
        学号在 ``studentUser`` 里（见 ``docs\\api-notes.md`` C.5 的 wire-format 布局：
        ``studentUser { #1 学号, #2 姓名, #3 studentUserId }``），拿不到就写 ``None``，
        **不瞎造**。
        """
        exam_by_user: dict[str, Any] = {}
        student_user_by_id: dict[str, Any] = {}
        members_all: list[dict[str, Any]] = []
        page = 0
        total: int | None = None

        while True:
            params = C.user_group_members_params(page=page, limit=C.TEAM_PAGE_SIZE)
            data = self.client.get_json(C.api_user_group_members(self.psid), params) or {}
            if page == 0:
                total = data.get("total")
            exam_by_user.update(data.get("examByUserId") or {})
            student_user_by_id.update(data.get("studentUserById") or {})
            members = data.get("userGroupMembers") or []
            if not members:
                break
            members_all.extend(members)
            if total is not None and len(members_all) >= int(total):
                break
            if len(members) < C.TEAM_PAGE_SIZE:
                break
            page += 1

        teams: list[dict[str, Any]] = []
        for member in members_all:
            user_id = member.get("userId")
            student_user_id = member.get("studentUserId")
            name, source = self._resolve_team_name(user_id, student_user_id, exam_by_user, student_user_by_id)
            number, number_source = self._resolve_student_number(
                user_id, student_user_id, exam_by_user, student_user_by_id, member)
            teams.append(
                {
                    "userId": str(user_id) if user_id is not None else None,
                    "studentUserId": (
                        str(student_user_id) if student_user_id is not None else None
                    ),
                    "name": name,
                    "name_source": source,
                    # M13：学号（照片/校徽按学号匹配就靠它）。拿不到就是 None。
                    "studentNumber": number or None,
                    "student_number_source": number_source,
                    "raw": member,
                }
            )
        with_number = sum(1 for t in teams if t.get("studentNumber"))
        print(f"      学号：{with_number} / {len(teams)} 支拿到了 studentNumber")
        return teams

    #: ``studentUser`` 里学号可能叫这些名字（主用 studentNumber，其余是兼容写法）。
    STUDENT_NUMBER_KEYS = ("studentNumber", "studentNo", "student_number", "number",
                           "studentNum", "examNumber")

    @classmethod
    def _student_number_of(cls, obj: Any) -> str:
        """从对象里取学号；取不到返回空串（**不造值**）。"""
        if not isinstance(obj, dict):
            return ""
        for key in cls.STUDENT_NUMBER_KEYS:
            value = obj.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @classmethod
    def _resolve_student_number(
        cls,
        user_id: Any,
        student_user_id: Any,
        exam_by_user: dict[str, Any],
        student_user_by_id: dict[str, Any],
        member: dict[str, Any],
    ) -> tuple[str, str]:
        """按优先级找学号，返回 ``(学号, 来源)``；找不到返回 ``("", "")``。

        顺序（与取队伍名走同一批对象，保证「名字从哪来、学号也从哪来」）：
          1. ``examByUserId[userId].studentUser``（当前接口的名字来源，学号就在同一个对象里）
          2. ``studentUserById[studentUserId]``
          3. ``userGroupMembers[].exam.studentUser``（旧接口把 exam 内嵌在成员里）
          4. ``userGroupMembers[].studentUser`` / 成员本身
        """
        candidates: list[tuple[str, Any]] = []
        entry = exam_by_user.get(str(user_id)) if user_id is not None else None
        if isinstance(entry, dict):
            candidates.append(("examByUserId.studentUser", entry.get("studentUser")))
        entry2 = student_user_by_id.get(str(student_user_id)) if student_user_id is not None else None
        if isinstance(entry2, dict):
            candidates.append(("studentUserById", entry2))
        exam = member.get("exam")
        if isinstance(exam, dict):
            candidates.append(("userGroupMembers[].exam.studentUser", exam.get("studentUser")))
        candidates.append(("userGroupMembers[].studentUser", member.get("studentUser")))
        candidates.append(("userGroupMembers[]", member))
        if isinstance(entry, dict):
            candidates.append(("examByUserId", entry))
        for label, obj in candidates:
            number = cls._student_number_of(obj)
            if number:
                return number, label
        return "", ""

    @staticmethod
    def _resolve_team_name(
        user_id: Any,
        student_user_id: Any,
        exam_by_user: dict[str, Any],
        student_user_by_id: dict[str, Any],
    ) -> tuple[str, str]:
        """队伍名来源优先级与参考实现一致：
        1) examByUserId[userId].studentUser.name
        2) studentUserById[studentUserId].name
        3) Team_<userId> 兜底
        """
        entry = exam_by_user.get(str(user_id)) if user_id is not None else None
        if isinstance(entry, dict):
            student_user = entry.get("studentUser")
            if isinstance(student_user, dict):
                name = (student_user.get("name") or "").strip()
                if name:
                    return name, "examByUserId.studentUser.name"

        entry = student_user_by_id.get(str(student_user_id)) if student_user_id is not None else None
        if isinstance(entry, dict):
            name = (entry.get("name") or "").strip()
            if name:
                return name, "studentUserById.name"

        return f"Team_{user_id}", "fallback"

    # -- 提交 --------------------------------------------------------------

    def fetch_submissions(
        self,
        known_ids: set[str] | None = None,
        max_pages: int = 0,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """游标分页拉取提交记录。

        PTA 按 id 倒序返回（最新在前），分页参数 `before` 取上一页最后一条的 id，
        响应中的 `hasBefore` 指示是否还有更早的数据。

        增量：若提供 known_ids，遇到已缓存的 id 立即停止（后面的必然也都已缓存）。

        返回 (新增提交列表, 实际请求页数, 是否因增量而提前停止)。
        """
        known_ids = known_ids or set()
        collected: list[dict[str, Any]] = []
        before: str | None = None
        pages = 0
        incremental_stop = False

        while True:
            params: dict[str, Any] = {"limit": C.SUBMISSION_PAGE_SIZE}
            if before:
                params["before"] = before

            data = self.client.get_json(C.api_submissions(self.psid), params) or {}
            subs = data.get("submissions") or []
            pages += 1

            if not subs:
                break

            for sub in subs:
                sid = sub.get("id")
                sid_str = str(sid) if sid is not None else None
                if sid_str is not None and sid_str in known_ids:
                    incremental_stop = True
                    break
                collected.append(sub)

            if incremental_stop:
                break

            has_before = data.get("hasBefore")
            if has_before is None:
                has_before = len(subs) >= C.SUBMISSION_PAGE_SIZE
            if not has_before:
                break

            if max_pages and pages >= max_pages:
                break

            cursor = str(subs[-1].get("id")) if subs[-1].get("id") is not None else None
            if cursor is None or cursor == before:
                C.warn("游标未推进，为防止死循环提前停止抓取提交记录。")
                break
            before = cursor

        return collected, pages, incremental_stop


# --------------------------------------------------------------------------
# 命令：fetch
# --------------------------------------------------------------------------

def needs_meta_refresh(cached_teams: list[dict[str, Any]], refresh_meta: bool,
                       quiet: bool, full: bool) -> bool:
    """M13：要不要**强制刷新队伍元信息**（把 ``studentNumber`` 补回来）。

    只有「缓存里有队伍、却一个学号都没有」时才需要刷；并且**不能在实时轮询里每轮都刷**
    （``live-board\\poller.py`` 每轮都带 ``--quiet`` 调本命令），
    所以只在交互式运行（GUI / 命令行手跑）或 ``--full`` 周期里才刷。
    """
    cached = [t for t in (cached_teams or []) if isinstance(t, dict)]
    if not cached or refresh_meta:
        return False
    if any(str(t.get("studentNumber") or "").strip() for t in cached):
        return False
    return bool((not quiet) or full)


def cmd_fetch(args: argparse.Namespace) -> int:
    psid = args.problem_set
    out_path = Path(args.out)

    cookie, cookie_src = load_cookie(args.cookie_file, args.cookie)
    client = PTAClient(
        cookie,
        delay=args.delay,
        timeout=args.timeout,
        max_retries=args.max_retries,
        verbose=not args.quiet,
    )
    fetcher = make_source(args, client)

    print(f"[i] 题目集 ID : {psid}")
    print("[i] 取数通道  : 直连接口（HTTP + JSON，需要 Cookie）")
    print(f"[i] Cookie 来源: {cookie_src}")
    print(f"[i] Cookie 来源: {cookie_src}")
    print(f"[i] 输出文件  : {out_path.resolve()}")

    # ---- 载入已有缓存 ----
    cached: dict[str, Any] = {}
    if out_path.exists() and not args.full:
        try:
            cached = C.load_contest(out_path)
            print(
                f"[i] 已加载缓存：题目 {len(cached.get('problems') or [])} / "
                f"队伍 {len(cached.get('teams') or [])} / "
                f"提交 {len(cached.get('submissions') or [])}"
            )
            if cached.get("problem_set_id") not in (None, psid):
                C.warn(
                    f"缓存文件属于题目集 {cached.get('problem_set_id')}，与当前 {psid} 不一致，将全量重抓。"
                )
                cached = {}
        except (ValueError, json.JSONDecodeError) as exc:
            C.warn(f"缓存文件无法解析（{exc}），将全量重抓。")
            cached = {}
    elif args.full and out_path.exists():
        print("[i] --full：忽略已有缓存，全量重抓。")

    contest = C.empty_contest(psid)

    # ---- 1. 比赛元信息 ----
    print("[1/3] 获取比赛信息 ...")
    problem_set = fetcher.fetch_problem_set()
    contest["problem_set"] = problem_set
    if not problem_set:
        C.warn("problemSet 为空，接口可能返回了非预期结构。")
    title = problem_set.get("name") or f"PTA Problem Set {psid}"

    # ---- 2. 题目 ----
    if cached.get("problems") and not args.refresh_meta:
        problems = cached["problems"]
        print(f"[2/3] 题目：复用缓存 {len(problems)} 题（--refresh-meta 可强制刷新）")
    else:
        print("[2/3] 获取题目列表 ...")
        raw_problems = fetcher.fetch_problems()
        problems = []
        for idx, item in enumerate(raw_problems):
            letter = chr(ord("A") + idx) if idx < 26 else f"P{idx + 1}"
            problems.append(
                {
                    "id": str(item.get("id")) if item.get("id") is not None else None,
                    "ordinal": idx + 1,
                    "letter": letter,
                    "label": letter,
                    "name": item.get("title") or item.get("name") or f"Problem {letter}",
                    "raw": item,
                }
            )
        print(f"      题目数：{len(problems)}")

    # ---- 3. 队伍 ----
    # M13：缓存里的队伍如果是**旧版本抓的**（一个学号都没有），自动强制刷新一次 ——
    # 否则用户升级后照片/校徽还是只能按姓名匹配，得自己去猜「要 --refresh-meta」。
    #
    # ⚠ 但**实时轮询器**（`live-board\poller.py`）每轮都会带着 ``--quiet`` 调本命令：
    # 要是它每次都强制刷新队伍元信息，就变成每轮多打一次接口。
    # 所以只在「交互式运行（GUI / 命令行手跑）」或 ``--full`` 周期里才自动刷新。
    cached_teams = cached.get("teams") or []
    cached_with_number = sum(1 for t in cached_teams if str(t.get("studentNumber") or "").strip())
    force_meta = needs_meta_refresh(cached_teams, bool(args.refresh_meta),
                                    bool(getattr(args, "quiet", False)),
                                    bool(getattr(args, "full", False)))
    # M38（用户 2026-10-18）：缓存名录缺队 —— 提交里出现过的 userId 在名录里找不到时，
    #   说明队伍在第一次抓取之后才进群（或第一次没拉全），必须强制刷新名录，
    #   否则这些队的提交没法归属，榜单上直接看不到人。
    try:
        _cached_ids = {str(t.get("userId")) for t in cached_teams if isinstance(t, dict)}
        _sub_ids = {str(s0.get("userId")) for s0 in (cached.get("submissions") or [])
                    if isinstance(s0, dict) and s0.get("userId") is not None}
        _orphans = _sub_ids - _cached_ids
        if _orphans and not args.refresh_meta:
            C.warn("缓存名录里缺 %d 支「有提交但不在名录里」的队（如 %s）—— 本次自动重抓队伍名录。"
                   % (len(_orphans), "、".join(sorted(_orphans)[:2])))
            force_meta = True
    except Exception:  # noqa: BLE001
        pass
    if cached_teams and not args.refresh_meta and not force_meta:
        teams = cached_teams
        print(f"[3/3] 队伍：复用缓存 {len(teams)} 支（--refresh-meta 可强制刷新）")
        if not cached_with_number:
            C.warn("缓存里的队伍一个学号都没有（旧版本抓的）——照片/校徽只能按姓名匹配；"
                   "到界面上重新点一次「拉取数据」就会自动补学号。")
    else:
        if force_meta:
            C.warn("缓存里的队伍一个学号都没有（旧版本抓的），本次自动刷新队伍信息（补学号）。")
        print("[3/3] 获取队伍信息 ...")
        teams = fetcher.fetch_teams()
        print(f"      队伍数：{len(teams)}")
        if teams and not any(str(t.get("studentNumber") or "").strip() for t in teams):
            C.warn(
                "本次抓取一个学号都没拿到（studentNumber 全空）。\n"
                "        按学号命名的队伍照片 / 校徽会匹配不上，只能按姓名匹配；\n"
                "        请确认账号有权限读 user-group-members 的 exams 信息，"
                "或用 --refresh-meta 重抓一次。"
            )

    # ---- 4. 提交（增量） ----
    known_ids = {s["id"] for s in (cached.get("submissions") or []) if s.get("id")}
    print(
        f"[4/4] 获取提交记录 ...（增量模式，已知 {len(known_ids)} 条）"
        if known_ids
        else "[4/4] 获取提交记录 ...（全量）"
    )
    new_raw, pages, stopped = fetcher.fetch_submissions(
        known_ids=set() if args.full else known_ids,
        max_pages=args.max_pages,
    )
    suffix = "，命中缓存后提前停止" if stopped else ""
    print(f"      新增 {len(new_raw)} 条（请求 {pages} 页{suffix}）")

    # ---- 5. 规范化 ----
    warnings: list[str] = []
    all_raw = new_raw + [
        s.get("raw", {}) for s in (cached.get("submissions") or []) if s.get("raw")
    ]

    start_dt, start_source, end_dt = C.resolve_contest_start(problem_set, all_raw, warnings)

    if args.start:
        manual = C.parse_iso(args.start) or C.parse_iso(args.start + "T00:00:00Z")
        if manual is None:
            C.die(f"--start 无法解析：{args.start!r}（建议 ISO 8601，例如 2026-10-08T14:00:00Z）")
        start_dt, start_source = manual, "--start"
        warnings.append(f"比赛开始时间由 --start 手动指定：{C.iso(start_dt)}")

    start_epoch = start_dt.timestamp()

    by_id: dict[str, dict[str, Any]] = {}
    for raw in all_raw:
        norm = C.normalize_submission(raw, start_epoch)
        if norm["id"]:
            by_id[norm["id"]] = norm

    submissions = sorted(
        by_id.values(),
        key=lambda s: (s.get("submit_epoch") or 0, s.get("id") or ""),
    )

    duration = problem_set.get("duration") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0 and end_dt is not None:
        duration = max(0.0, (end_dt - start_dt).total_seconds())
    if duration <= 0 and submissions:
        last = submissions[-1].get("submit_epoch")
        if last:
            duration = max(0.0, last - start_epoch)
            warnings.append(f"PTA 未提供 duration，按最后一条提交推算为 {C.format_hms(duration)}。")
    if duration <= 0:
        duration = 5 * 3600
        warnings.append("无法确定比赛时长，回落到默认 5:00:00，请用 --length 手动指定。")

    if args.length:
        duration = C.parse_hms(args.length)
        warnings.append(f"比赛时长由 --length 手动指定为 {C.format_hms(duration)}。")

    freeze = args.freeze if args.freeze is not None else contest["contest"]["freeze_seconds"]

    contest["problems"] = problems
    contest["teams"] = teams
    contest["submissions"] = submissions
    contest["fetched_at"] = C.now_iso()
    contest["warnings"] = warnings
    contest["contest"] = {
        "title": title,
        "short_title": title,
        "start_epoch": start_epoch,
        "start_iso": C.iso(start_dt),
        "end_epoch": end_dt.timestamp() if end_dt else None,
        "end_iso": C.iso(end_dt),
        "duration_seconds": duration,
        "length": C.format_hms(duration),
        "freeze_seconds": freeze,
        "freeze_length": C.format_hms(freeze),
        "penalty_minutes": args.penalty,
        "start_source": start_source,
    }
    contest["stats"] = C.contest_stats(contest)

    # 未知状态/编译器提示
    unknown_status = sorted(
        {s["status"] for s in submissions if s["status"] not in C.PTA_STATUS_TO_ACRONYM}
    )
    if unknown_status:
        C.warn(
            "出现参考实现未覆盖的判题状态，已按 "
            f"{C.UNKNOWN_STATUS_FALLBACK} 处理：{unknown_status}"
        )
        warnings.append(f"未知判题状态（按 WA 处理）：{unknown_status}")

    unknown_compiler = sorted(
        {
            s["compiler"]
            for s in submissions
            if s.get("compiler") and s["compiler"] not in C.COMPILER_TO_LANG_ID
        }
    )
    if unknown_compiler:
        C.warn(f"出现未映射的编译器，已按 c 处理：{unknown_compiler}")
        warnings.append(f"未映射的编译器（按 c 处理）：{unknown_compiler}")

    for msg in warnings:
        C.warn(msg)

    # M14（实时榜单「拉取不全」取证）：把**这一轮到底拉全了没有**写进 contest.json。
    # 只有取数方自己知道"是不是撞上了 --max-pages 上限"；实时榜单（live-board/poller.py）
    # 据此在 /health 与页面上显示「数据可能不全（N / 期望 M）」，并提示点「全量刷新」。
    # 纯新增键，不改任何已有键的含义。
    contest["fetch_report"] = {
        "at": C.now_iso(),
        "full": bool(args.full),
        "max_pages": int(args.max_pages or 0),
        "pages": int(pages),
        "stopped_early": bool(stopped),
        "truncated": bool(args.max_pages and pages >= args.max_pages),
        "submissions_fetched": len(new_raw),
        "submissions_total": len(submissions),
        "teams": len(teams),
        "problems": len(problems),
        "used_cache": bool(cached),
    }

    C.save_contest(out_path, contest)

    stats = contest["stats"]
    print("")
    print("=" * 62)
    print(f"  题目集    : {title}")
    print(f"  题目数    : {stats['problem_count']}")
    print(f"  队伍数    : {stats['team_count']}")
    print(f"  提交数    : {stats['submission_count']}")
    print(f"  开始时间  : {C.iso(start_dt)}  (来源: {start_source})")
    print(f"  比赛时长  : {C.format_hms(duration)}")
    print(f"  封榜时长  : {C.format_hms(freeze)}")
    print(f"  提交区间  : {stats['submit_min']} .. {stats['submit_max']}")
    print(f"  结果分布  : {stats['status_counts']}")
    print(f"  已写入    : {out_path.resolve()}")
    print(f"  请求次数  : {client.request_count if client else fetcher.request_count}")
    print("=" * 62)
    print("")
    print("下一步：python pta_to_resolver_xml.py --contest "
          f"{out_path} --out-dir resolver-package")
    return 0


# --------------------------------------------------------------------------
# 命令：--check / doctor（只读，不写任何文件）
# --------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    """只读体检：直连接口（HTTP + JSON，需要 Cookie）。不写任何文件。"""
    psid = args.problem_set
    cookie, cookie_src = load_cookie(args.cookie_file, args.cookie)
    client = PTAClient(
        cookie,
        delay=args.delay,
        timeout=args.timeout,
        max_retries=args.max_retries,
        verbose=not args.quiet,
    )
    fetcher = PTAFetcher(client, psid)

    print("=" * 62)
    print("  PTA 接口连通性 / 权限探测（只读，不写任何文件）")
    print("=" * 62)
    print(f"  题目集 ID  : {psid}")
    print(f"  Cookie 来源: {cookie_src}")
    has_auth = "PTASession" in cookie or "JSESSIONID" in cookie
    print(f"  含登录 Cookie(PTASession/JSESSIONID): {'是' if has_auth else '否'}")
    print("-" * 62)

    probe_plan = [
        ("题目集信息", C.api_problem_set(psid), None),
        ("权限校验(/exams)", C.api_problem_set_exams(psid), None),
        ("题目列表", C.api_preview_problems(psid), C.preview_problems_params(0, 500)),
        ("队伍列表", C.api_user_group_members(psid), C.user_group_members_params(0, 1)),
        ("提交记录(第1页)", C.api_submissions(psid), {"limit": 1}),
    ]

    failures: list[str] = []
    problem_set: dict[str, Any] = {}

    for label, url, params in probe_plan:
        try:
            data = client.get_json(url, params)
            note = ""
            if label == "题目集信息":
                problem_set = (data or {}).get("problemSet") or {}
                note = problem_set.get("name") or "(无 name 字段)"
            elif label == "题目列表":
                note = f"{len((data or {}).get('problemSetProblems') or [])} 题/首页"
            elif label == "队伍列表":
                note = f"total={data.get('total')}"
            elif label.startswith("提交"):
                note = f"{len((data or {}).get('submissions') or [])} 条/首页, hasBefore={data.get('hasBefore')}"
            print(f"  [OK]   HTTP 200  {label:<18} {note}")
        except PTAAuthError as exc:
            failures.append(label)
            print(f"  [401]  {label:<18} Cookie 无效或已过期")
            if args.verbose_errors:
                print(f"         {exc}")
        except PTAPermissionError as exc:
            failures.append(label)
            print(f"  [403]  {label:<18} 已登录但无权限（需教师/管理员账号）")
            if args.verbose_errors:
                print(f"         {exc}")
        except PTAApiError as exc:
            failures.append(label)
            print(f"  [ERR]  {label:<18} {exc}")
        except PTANetworkError as exc:
            failures.append(label)
            print(f"  [NET]  {label:<18} {exc}")

    print("-" * 62)

    if failures:
        print(f"  ✗ 探测未全部通过，失败项：{failures}")
        print("    401 -> 重新复制 Cookie；403 -> 换教师/管理员账号；")
        print("    参考 docs/api-notes.md 的『未登录基线响应』一节。")
        print(f"  请求次数: {client.request_count}")
        print("=" * 62)
        return 1

    # ---- 统计信息（只读） ----
    try:
        problems = fetcher.fetch_problems()
    except PTAError as exc:
        C.warn(f"题目列表拉取失败：{exc}")
        problems = []
    try:
        teams = fetcher.fetch_teams()
    except PTAError as exc:
        C.warn(f"队伍列表拉取失败：{exc}")
        teams = []

    subs: list[dict[str, Any]] = []
    pages = 0
    truncated = False
    try:
        subs, pages, _ = fetcher.fetch_submissions(max_pages=args.max_pages)
        if args.max_pages and pages >= args.max_pages:
            truncated = True
    except PTAError as exc:
        C.warn(f"提交记录拉取失败：{exc}")

    epochs = [C.to_epoch(C.parse_iso(s.get("submitAt"))) for s in subs]
    epochs = [e for e in epochs if e is not None]
    start_dt = C.parse_iso(problem_set.get("startAt"))
    duration = problem_set.get("duration") or 0

    statuses: dict[str, int] = {}
    for s in subs:
        key = (s.get("status") or "UNKNOWN").upper()
        statuses[key] = statuses.get(key, 0) + 1

    print("  探测结果")
    print(f"    比赛名称  : {problem_set.get('name')}")
    print(f"    开始时间  : {problem_set.get('startAt')}")
    print(f"    结束时间  : {problem_set.get('endAt')}")
    print(f"    时长(秒)  : {duration}")
    print(f"    题目数    : {len(problems)}")
    print(f"    队伍数    : {len(teams)}")
    if truncated:
        print(f"    提交数    : >= {len(subs)}  （已扫描 {pages} 页，受 --max-pages 限制）")
    else:
        print(f"    提交数    : {len(subs)}  （扫描 {pages} 页）")
    if epochs:
        lo, hi = min(epochs), max(epochs)
        lo_iso = C.iso(datetime.fromtimestamp(lo, timezone.utc))
        hi_iso = C.iso(datetime.fromtimestamp(hi, timezone.utc))
        print(f"    提交时间范围: {lo_iso} .. {hi_iso}")
        if start_dt is not None and start_dt.year >= 1970:
            print(f"    相对比赛时间: {C.format_hms(lo - start_dt.timestamp())}"
                  f" .. {C.format_hms(hi - start_dt.timestamp())}")
    print(f"    结果分布  : {dict(sorted(statuses.items(), key=lambda kv: -kv[1]))}")

    unknown = sorted({k for k in statuses if k not in C.PTA_STATUS_TO_ACRONYM})
    if unknown:
        print(f"    ⚠ 未映射状态: {unknown}（将按 WA 处理）")

    print("-" * 62)
    print(f"  ✓ Cookie 有效，接口可访问。请求次数: {client.request_count}")
    print("    未写入任何文件。确认无误后执行：")
    print(f"      python pta_fetch.py fetch --problem-set {psid}")
    print("=" * 62)
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pta_fetch.py",
        description="从 PTA(拼题A) 抓取比赛数据并落盘为 contest.json（复刻现有开源方案的非官方接口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="fetch",
        choices=["fetch", "check"],
        help="fetch=抓取并写盘（默认）; check=只读探测（等同 --check）",
    )
    parser.add_argument(
        "-c", "--check",
        action="store_true",
        help="只读探测接口连通性与 Cookie 权限，打印题目数/队伍数/提交数/时间范围，不写文件",
    )
    parser.add_argument(
        "-p", "--problem-set",
        default=C.DEFAULT_PROBLEM_SET_ID,
        help="PTA 题目集 ID（problemSetId），必须填写",
    )
    parser.add_argument(
        "--via",
        choices=["http"],
        default="http",
        help="取数通道：只有 http=直连接口（requests + JSON，需要有效 Cookie）。"
             "「借用浏览器」通道已于 2026-10-13 移除。",
    )
    parser.add_argument("--cookie-file", help="存放 Cookie 的文件（默认自动查找 cookie.txt 等）")
    parser.add_argument("--cookie", help="直接传入 Cookie 字符串（不推荐，会留在命令历史里）")
    parser.add_argument("-o", "--out", default="contest.json", help="contest.json 输出路径")
    parser.add_argument("--delay", type=float, default=C.DEFAULT_DELAY,
                        help=f"请求间隔秒数，默认 {C.DEFAULT_DELAY}（建议 0.2~0.3）")
    parser.add_argument("--timeout", type=int, default=C.DEFAULT_TIMEOUT, help="单请求超时秒数")
    parser.add_argument("--max-retries", type=int, default=C.DEFAULT_MAX_RETRIES,
                        help="失败重试次数（指数退避）")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="提交记录最多抓取的页数，0=不限（doctor 场景可用于快速探测）")
    parser.add_argument("--full", action="store_true",
                        help="忽略缓存，全量重抓题目/队伍/提交")
    parser.add_argument("--refresh-meta", action="store_true",
                        help="强制刷新题目与队伍列表（提交仍按增量）")
    parser.add_argument("--start", help="手动指定比赛开始时间（ISO 8601），覆盖 PTA 的 startAt")
    parser.add_argument("--length", help="手动指定比赛时长，格式 H:MM:SS")
    parser.add_argument("--freeze", type=int, default=None,
                        help="封榜时长（秒），默认 3600（1:00:00）")
    parser.add_argument("--penalty", type=int, default=20,
                        help="每次错误提交的罚时（分钟），默认 20")
    parser.add_argument("-q", "--quiet", action="store_true", help="不打印进度到 stderr")
    parser.add_argument("--verbose-errors", action="store_true", help="check 模式打印完整错误信息")
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            except Exception:
                pass

    parser = build_parser()
    args = parser.parse_args(argv)

    if not str(args.problem_set or "").strip():
        print("[错误] 请用 --problem-set 指定题目集 ID（PTA 比赛网址 /problem-sets/ 后面那串数字）。",
              file=sys.stderr)
        return 2

    if args.check:
        args.command = "check"

    # 统一把已知异常转成可读的中文提示，避免用户看到 Python traceback
    try:
        if args.command == "check":
            return cmd_check(args)
        return cmd_fetch(args)
    except PTAAuthError as exc:
        print(f"\n[认证失败] {exc}", file=sys.stderr)
        print("  -> 详见 README.md「第一步：获取 Cookie」", file=sys.stderr)
        return 3
    except PTAPermissionError as exc:
        print(f"\n[权限不足] {exc}", file=sys.stderr)
        print("  -> 请确认使用的是【教师/管理员】账号，学生账号无法访问这些接口。", file=sys.stderr)
        return 4
    except PTAApiError as exc:
        print(f"\n[接口错误] {exc}", file=sys.stderr)
        print("  -> 可尝试加大间隔重试（--delay 0.5）；若持续失败则 PTA 可能已变更接口。",
              file=sys.stderr)
        return 5
    except PTANetworkError as exc:
        print(f"\n[网络错误] {exc}", file=sys.stderr)
        print("  -> 请检查网络/代理，稍后重试。", file=sys.stderr)
        return 6
    except KeyboardInterrupt:
        print("\n[已中断] 未写入 contest.json，已有缓存保持不变。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
