#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vjudge_fetch.py —— 从 VJudge 抓比赛数据，落盘成**与 PTA 完全同构**的 contest.json

为什么要有它
------------
本项目（滚榜 / 实时榜）原本只认 PTA 的 ``contest.json``。VJudge 版第一步的
做法是：**不动下游任何一行**，只把 VJudge 的 rank 接口翻译成同一份
``contest.json``，下游（实时榜 poller / board_core / 滚榜生成包）照旧读。

数据源（实测，2026-10）
----------------------
匿名 GET，公开比赛无需登录、无需 Cookie：

    https://vjudge.net/contest/rank/single/<contestId>
    Header: Referer: https://vjudge.net/

返回 JSON::

    {"id": 862258, "title": "...", "begin": 1792837800000, "length": 604800000,
     "isReplay": false,
     "participants": {"<uid>": {"type": "user", "name": "handle",
                                "nickname": "", "image": "https://...", "members": []}},
     "submissions": [[uid, problemIndex(0-based), status, timeMs, gotScore, fullScore], ...]}

字段语义（已实测交叉验证，别猜）：
  * 第 1 列 = 参与者 uid（= participants 的键）；
  * 第 2 列 = 题号下标，**0 开始**；
  * 第 3 列 = 状态，**1 = 通过（满分）**，其余一律按未通过处理（不区分 WA/TLE/CE）；
  * 第 4 列 = 相对比赛开始的**毫秒数**；
  * 第 5/6 列 = 得分 / 满分（实测 status==1 时两者相等，用作交叉验证）。
  participants 里只有 handle + 头像 URL，**没有**学号 / 真实姓名 / 学校 ——
  真实姓名 / 校徽走 ``vjudge_alias.py`` 的备注库（SQLite）。

用法示例
--------
  # 1) 抓取（输出与 PTA 同构的 contest.json）
  python vjudge_fetch.py fetch --contest 862258 --out .\\vjudge-contest.json

  # 2) 直接粘浏览器地址也行（会自动抽出比赛 id）
  python vjudge_fetch.py fetch --contest https://vjudge.net/contest/862258#rank

  # 3) 只读探测：确认比赛 ID 能拉到 rank JSON（不写任何文件，供 GUI 环境预检用）
  python vjudge_fetch.py check --contest 862258

  # 4) 用另一份备注库 / 不要下载头像
  python vjudge_fetch.py fetch --contest 862258 --out out.json --db my.db --no-avatars

退出码：0 成功；2 参数错误；3 比赛不存在 / 无权限（HTTP 4xx）；
        5 返回内容不是预期 JSON；6 网络不通（重试耗尽）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

VJ_ORIGIN = "https://vjudge.net"
VJ_RANK_URL = VJ_ORIGIN + "/contest/rank/single/{contest_id}"
VJ_CONTEST_URL = VJ_ORIGIN + "/contest/{contest_id}#rank"

#: 模拟普通浏览器（少了 Referer 会被 VJudge 挡掉）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Referer": VJ_ORIGIN + "/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

DEFAULT_TIMEOUT = 60          # VJudge 偶尔要 5~10 秒才吐数据，给足
DEFAULT_MAX_RETRIES = 3       # 任务要求：3 次
RETRY_SLEEP = 2.0             # 任务要求：间隔 2 秒

#: 状态列只有 1 算通过
STATUS_ACCEPTED = 1

#: 备注库模块名（同目录那份；找不到就退化成"没有备注"）
ALIAS_MODULE = "vjudge_alias"


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class VJudgeError(Exception):
    """所有 VJudge 相关错误的基类。"""


class VJudgeNetworkError(VJudgeError):
    """底层网络错误 / HTTP 5xx，重试耗尽。"""


class VJudgeApiError(VJudgeError):
    """HTTP 4xx：比赛不存在、私有比赛未登录等。"""


class VJudgeParseError(VJudgeError):
    """200 了但返回内容不是预期的 JSON 结构。"""


# --------------------------------------------------------------------------
# 小工具：时间 / 格式 / 字母
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def format_hms(seconds: float) -> str:
    """秒 -> ``H:MM:SS``（小时不补零、可以超过 24，7 天 = ``168:00:00``）。"""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    total = max(0, total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def problem_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA, 38 -> AM …（题单型比赛 39 题用得到）。"""
    n = int(index)
    if n < 0:
        return "?"
    if n < 26:
        return chr(ord("A") + n)
    n -= 26
    return chr(ord("A") + n // 26) + chr(ord("A") + n % 26)


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> "None":
    print(f"[error] {msg}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------
# 比赛 ID 归一化
# --------------------------------------------------------------------------

def normalize_contest_id(value: Any) -> str:
    """把用户填的东西统一成比赛 id（纯数字）。

    接受：``862258`` / ``https://vjudge.net/contest/862258#rank`` /
    ``https://vjudge.net/contest/862258`` / ``vjudge.net/contest/862258`` /
    带尾部空格的 `` 862258 ``。认不出来返回空串（由调用方报中文错误）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    # 去掉 URL 里的 query / fragment 再按路径段找
    raw = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
    parts = [p for p in raw.path.split("/") if p]
    for i, part in enumerate(parts):
        if part == "contest" and i + 1 < len(parts) and parts[i + 1].isdigit():
            return parts[i + 1]
    for part in reversed(parts):
        if part.isdigit():
            return part
    # 兜底：整串里最后一串连续数字（例如 "contest862258"）
    digits = "".join(" " if not ch.isdigit() else ch for ch in text).split()
    return digits[-1] if digits else ""


# --------------------------------------------------------------------------
# Cookie（公开赛可留空；私有赛带 Cookie 时原样透传）
# --------------------------------------------------------------------------

def load_cookie(cookie_file: str | None = None) -> str:
    """读 Cookie 文件：支持纯文本、``Cookie: a=b; c=d``、JSON、Netscape 行。

    读不到就返回空串 —— VJudge 公开比赛不需要 Cookie。
    """
    if not cookie_file:
        return ""
    path = Path(str(cookie_file))
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    low = text.lower()
    if low.startswith("{") or low.startswith("["):
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict):
            if isinstance(obj.get("cookies"), dict):
                obj = obj["cookies"]
            pairs = [f"{k}={v}" for k, v in obj.items() if isinstance(v, (str, int))]
            if pairs:
                return "; ".join(pairs)
        if isinstance(obj, list):
            pairs = [f"{item['name']}={item['value']}" for item in obj
                     if isinstance(item, dict) and "name" in item and "value" in item]
            if pairs:
                return "; ".join(pairs)
    if "cookie:" in low:
        idx = low.index("cookie:")
        text = text[idx + len("cookie:"):].strip()
    # Netscape cookie.txt：取最后一列的名字=值
    if "\t" in text:
        pairs = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 7:
                pairs.append(f"{cols[5]}={cols[6]}")
        if pairs:
            return "; ".join(pairs)
    return text


# --------------------------------------------------------------------------
# HTTP 客户端
# --------------------------------------------------------------------------

class VJudgeClient:
    """最小 HTTP 客户端：requests 可用就用 requests，否则退回 urllib。"""

    def __init__(self, cookie: str = "", timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES, verbose: bool = True) -> None:
        self.cookie = (cookie or "").strip()
        self.timeout = max(5.0, float(timeout))
        self.max_retries = max(1, int(max_retries))
        self.verbose = verbose
        self.request_count = 0
        try:
            import requests  # noqa: F401
            self._requests = requests
        except ImportError:  # pragma: no cover - 正常装了 requests
            self._requests = None

    def _headers(self) -> dict[str, str]:
        headers = dict(DEFAULT_HEADERS)
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _get_once(self, url: str) -> bytes:
        self.request_count += 1
        if self._requests is not None:
            resp = self._requests.get(url, headers=self._headers(),
                                      timeout=(15, self.timeout))
            if resp.status_code >= 500:
                raise VJudgeNetworkError(f"HTTP {resp.status_code}（VJudge 服务端错误）")
            if resp.status_code in (401, 403):
                raise VJudgeApiError(
                    f"HTTP {resp.status_code}：比赛不存在、未公开或需要登录（可带 --cookie-file）")
            if resp.status_code == 404:
                raise VJudgeApiError(f"HTTP 404：VJudge 上没有这场比赛（{url}）")
            if resp.status_code >= 400:
                raise VJudgeApiError(f"HTTP {resp.status_code}：请求被拒绝")
            return resp.content
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as fh:
                return fh.read()
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise VJudgeNetworkError(f"HTTP {exc.code}（VJudge 服务端错误）") from exc
            raise VJudgeApiError(f"HTTP {exc.code}：比赛不存在 / 未公开 / 需要登录") from exc

    def get_json(self, url: str) -> Any:
        """带重试（3 次、间隔 2 秒）地取一份 JSON。"""
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                raw = self._get_once(url)
                try:
                    return json.loads(raw.decode("utf-8", errors="replace"))
                except (ValueError, UnicodeDecodeError) as exc:
                    head = raw[:120].decode("utf-8", errors="replace")
                    raise VJudgeParseError(
                        f"返回内容不是 JSON（可能被打到验证页）：{head!r}") from exc
            except VJudgeApiError:
                raise                     # 4xx 不重试，重试也没用
            except VJudgeParseError:
                raise
            except VJudgeNetworkError as exc:
                last = exc
            except Exception as exc:      # noqa: BLE001 - requests 的各种网络异常
                last = VJudgeNetworkError(f"{type(exc).__name__}: {exc}")
            if attempt < self.max_retries:
                if self.verbose:
                    print(f"[i] 第 {attempt} 次请求失败（{last}），{RETRY_SLEEP:.0f} 秒后重试 …",
                          file=sys.stderr)
                time.sleep(RETRY_SLEEP)
        raise VJudgeNetworkError(
            f"网络请求失败（已重试 {self.max_retries} 次，每次间隔 {RETRY_SLEEP:.0f} 秒）：{last}")


# --------------------------------------------------------------------------
# 备注库
# --------------------------------------------------------------------------

def load_aliases(db_path: str | None = None) -> tuple[dict[str, dict[str, str]], str]:
    """读备注库。返回 ``(map, 说明)``；备注库坏了**不影响抓数**。"""
    here = Path(__file__).resolve().parent
    for cand in (here, here.parent / "pta-resolver", here.parent):
        if (cand / (ALIAS_MODULE + ".py")).is_file() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
    try:
        import vjudge_alias  # noqa: E402
    except Exception as exc:  # noqa: BLE001
        return {}, f"备注库不可用（{type(exc).__name__}: {exc}），本次全部用 handle 显示"
    try:
        return vjudge_alias.load_map(db_path or None), f"备注库：{db_path or vjudge_alias.default_db_path()}"
    except Exception as exc:  # noqa: BLE001
        return {}, f"备注库读取失败（{type(exc).__name__}: {exc}），本次全部用 handle 显示"


# --------------------------------------------------------------------------
# 头像 / 备注校徽 → live\\logos\\<uid>.png
# --------------------------------------------------------------------------

def _looks_like_url(text: str) -> bool:
    low = str(text or "").strip().lower()
    return low.startswith("http://") or low.startswith("https://")


#: VJudge 给的头像域名（cn.cravatar.com）在国内经常连不上；
#: 同一个 hash 在下面几个站点是通的 —— 按顺序试，谁先成功用谁。
AVATAR_HOSTS = ("cn.cravatar.com", "cravatar.com", "secure.gravatar.com", "www.gravatar.com")
#: 本次运行里已经确认连不上的域名（连一次要好几秒，记住它别每张图都重试）
_DEAD_HOSTS: set[str] = set()


def avatar_url_candidates(url: str) -> list[str]:
    """把 cn.cravatar.com 的头像地址展开成「多域名候选」。"""
    text = str(url or "").strip()
    if not _looks_like_url(text):
        return []
    def _host(u):
        try:
            return u.split("//", 1)[1].split("/", 1)[0].lower()
        except IndexError:
            return ""
    out = [] if _host(text) in _DEAD_HOSTS else [text]
    for host in AVATAR_HOSTS:
        if host in _DEAD_HOSTS:
            continue
        marker = "/avatar/"
        if marker in text:
            tail = text.split(marker, 1)[1]
            cand = f"https://{host}{marker}{tail}"
            if cand not in out:
                out.append(cand)
    return out


class _DownloadBudget:
    """一轮抓取里只允许花这么多秒下头像 —— 轮询器每 10 秒跑一轮，不能卡住。"""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + max(0.0, float(seconds))

    def left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def ok(self) -> bool:
        return self.left() > 1.0


def download_image(url: str, dest: Path, timeout: float, verbose: bool = True) -> bool:
    """把一张图片下到 ``dest``（已存在就不下）。失败返回 False，不抛。"""
    dest = Path(dest)
    if dest.is_file() and dest.stat().st_size > 0:
        return True
    budget = max(2.0, min(12.0, float(timeout)))
    try:
        import requests
        resp = requests.get(url, headers={
            "User-Agent": DEFAULT_UA, "Referer": VJ_ORIGIN + "/"},
            timeout=(min(5.0, budget), budget))
        if resp.status_code != 200 or not resp.content:
            return False
        data = resp.content
    except Exception:  # noqa: BLE001
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": DEFAULT_UA, "Referer": VJ_ORIGIN + "/"})
            with urllib.request.urlopen(req, timeout=budget) as fh:
                data = fh.read()
        except Exception:  # noqa: BLE001
            return False
    if not data:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except OSError:
        return False
    if verbose:
        print(f"      头像缓存：{dest.name}（{len(data)} 字节）")
    return True


# --------------------------------------------------------------------------
# 规范化：VJudge rank JSON -> contest.json
# --------------------------------------------------------------------------

def build_contest(data: dict[str, Any], contest_id: str, *,
                  penalty_minutes: int = 20,
                  alias_map: dict[str, dict[str, str]] | None = None,
                  out_dir: Path | None = None,
                  avatars: bool = True,
                  avatar_budget: float = 15.0,
                  timeout: float = DEFAULT_TIMEOUT,
                  verbose: bool = True) -> tuple[dict[str, Any], list[str]]:
    """把 rank JSON 翻译成 contest.json（字段与 PTA 版一一对应）。"""
    alias_map = alias_map or {}
    warnings: list[str] = []
    out_dir = Path(out_dir) if out_dir is not None else Path.cwd()
    logo_dir = out_dir / "logos"

    try:
        begin_ms = float(data.get("begin") or 0)
        length_ms = float(data.get("length") or 0)
    except (TypeError, ValueError):
        raise VJudgeParseError("begin / length 不是数字，返回结构可能变了")
    if begin_ms <= 0:
        raise VJudgeParseError("返回里没有有效的 begin（比赛开始时间）")
    if length_ms <= 0:
        raise VJudgeParseError("返回里没有有效的 length（比赛时长）")

    start_epoch = begin_ms / 1000.0
    end_epoch = (begin_ms + length_ms) / 1000.0
    duration_seconds = length_ms / 1000.0
    start_dt = datetime.fromtimestamp(start_epoch, timezone.utc)
    end_dt = datetime.fromtimestamp(end_epoch, timezone.utc)
    title = str(data.get("title") or "").strip() or f"VJudge Contest {contest_id}"

    participants = data.get("participants")
    if not isinstance(participants, dict):
        raise VJudgeParseError("返回里没有 participants 字典")
    raw_subs = data.get("submissions")
    if not isinstance(raw_subs, list):
        raise VJudgeParseError("返回里没有 submissions 数组")

    # ---- problems：题号下标 -> 字母（拿不到题名，就用字母当名字）----
    max_index = -1
    for row in raw_subs:
        try:
            max_index = max(max_index, int(row[1]))
        except (TypeError, ValueError, IndexError):
            continue
    problem_total = max_index + 1
    problems: list[dict[str, Any]] = []
    for idx in range(problem_total):
        letter = problem_letter(idx)
        problems.append({
            "id": f"vj-{contest_id}-{idx}",
            "ordinal": idx + 1,
            "letter": letter,
            "label": letter,
            "name": letter,
        })
    if not problems:
        warnings.append("这场比赛一条提交都没有，题目列表为空（problems=0）。")

    # ---- teams：备注库优先，否则 handle；原始 handle 永远写进 vjudge_handle ----
    teams: list[dict[str, Any]] = []
    logo_notes = 0
    avatar_ok = 0
    avatar_fail = 0
    avatar_skipped = 0
    budget = _DownloadBudget(avatar_budget)
    for uid, info in participants.items():
        uid = str(uid)
        info = info if isinstance(info, dict) else {}
        handle = str(info.get("name") or "").strip() or uid
        alias = alias_map.get(uid) or {}
        display = str(alias.get("display") or "").strip()
        name = display or handle
        logo = ""

        explicit = str(alias.get("logo") or "").strip()
        if explicit:
            if _looks_like_url(explicit):
                # 备注里给了图片 URL：下载缓存到 live\logos\<uid>.png（第 1 优先）
                dest = logo_dir / f"{uid}.png"
                if dest.is_file():
                    logo = str(dest)
                    logo_notes += 1
                elif avatars and budget.ok() and any(
                        download_image(cand, dest, min(8.0, budget.left()), verbose)
                        for cand in avatar_url_candidates(explicit) if budget.ok()):
                    logo = str(dest)
                    logo_notes += 1
                elif not avatars:
                    logo = explicit
                else:
                    logo = explicit        # 下不下来就留 URL（下游会忽略，回落后面的规则）
                    warnings.append(f"备注校徽下载失败（或超时）：uid={uid} {explicit}")
            else:
                logo = explicit            # 校徽目录里的文件名，交给展示层按名字匹配
        # 用户 2026-10-27（有的选手头像查不到）：**不管有没有备注校徽，都把头像顺手缓存一份**。
        #   之前是 elif avatars —— 只要备注里填了校徽就跳过下载，于是那个队永远没有
        #   live\logos\<uid>.png，一旦把备注校徽清空就看不到头像。现在两个都抓：
        #   展示仍然「备注校徽优先」，但头像文件始终备好。
        if avatars:
            image = str(info.get("image") or "").strip()
            if _looks_like_url(image):
                dest = logo_dir / f"{uid}.png"
                if dest.is_file():
                    avatar_ok += 1
                elif budget.ok():
                    # cn.cravatar.com 常常连不上 -> 同一个 hash 换域名再试
                    got = False
                    for cand in avatar_url_candidates(image):
                        if not budget.ok():
                            break
                        if download_image(cand, dest, min(6.0, budget.left()), verbose):
                            got = True
                            break
                        try:
                            _DEAD_HOSTS.add(cand.split("//", 1)[1].split("/", 1)[0].lower())
                        except IndexError:
                            pass
                    if got:
                        avatar_ok += 1
                    elif budget.ok():
                        avatar_fail += 1
                    else:
                        avatar_skipped += 1
                else:
                    avatar_skipped += 1

        team: dict[str, Any] = {
            "userId": uid,
            "name": name,
            "studentNumber": "",
            # 新增键（下游不认识就会忽略）：原始 handle，备注改回 / 排查用
            "vjudge_handle": handle,
            "name_source": "alias.display" if display else "vjudge.participants.name",
        }
        if logo:
            team["logo"] = logo
        teams.append(team)

    if logo_notes:
        warnings.append(f"{logo_notes} 支队伍用了备注里指定的校徽（已缓存到 {logo_dir}）。")
    if False:
        warnings.append(f"{avatar_ok} 支队伍的 VJudge 头像已缓存到 {logo_dir}"
                        "（榜单 /logo/<uid> 会优先命中）。")
    if avatar_fail:
        warnings.append(f"{avatar_fail} 支队伍的头像下载失败（不影响榜单，会回落默认校徽）。")
    if avatar_skipped:
        warnings.append(f"{avatar_skipped} 支队伍的头像因本轮时间预算用尽而跳过"
                        "（下一轮会继续缓存，不影响榜单显示）。")

    # ---- submissions ----
    pid_of = {idx: probs["id"] for idx, probs in enumerate(problems)}
    submissions: list[dict[str, Any]] = []
    for row in raw_subs:
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        try:
            uid = str(row[0])
            idx = int(row[1])
            status = int(row[2])
            # 用户 2026-10-27：VJudge 的 submissions[3] 单位是**秒**（不是毫秒），
            #   之前按毫秒 /1000，整张榜时间小了一千倍、罚时全错（49897 被算成 282）。
            rel_sec = float(row[3])
        except (TypeError, ValueError):
            continue
        got_score = row[4] if len(row) > 4 else None
        full_score = row[5] if len(row) > 5 else None
        accepted = (status == STATUS_ACCEPTED)
        # 交叉验证：status==1 时分数应当满分；不一致只提示（不改判定，按任务口径 status==1 才算 AC）
        if accepted and got_score is not None and full_score is not None:
            try:
                if float(got_score) != float(full_score):
                    warnings.append(
                        f"uid={uid} 题 {idx} 的 status=1 但得分 {got_score}/{full_score}，"
                        "仍按「通过」处理（以 status 为准）")
            except (TypeError, ValueError):
                pass
        rel_seconds = int(round(rel_sec))
        submit_epoch = start_epoch + rel_sec
        submissions.append({
            "id": str(row[0]) + "-" + str(idx) + "-" + str(int(rel_sec)),
            "userId": uid,
            "problemSetProblemId": pid_of.get(idx, f"vj-{contest_id}-{idx}"),
            "status": "ACCEPTED" if accepted else "WRONG_ANSWER",
            "compiler": "",
            "submit_epoch": submit_epoch,
            "rel_time_seconds": rel_seconds,
            "score": got_score,
            "raw": list(row),
        })
    # 稳定排序：先相对时间，再 userId / 题号（VJudge 返回本身不是按时间排的）
    submissions.sort(key=lambda s: (s.get("rel_time_seconds") or 0,
                                    str(s.get("userId") or ""),
                                    str(s.get("problemSetProblemId") or "")))

    # ---- contest / problem_set / stats / fetch_report ----
    contest = {
        "schema_version": 1,
        "generator": "vjudge-resolver",
        "problem_set_id": str(contest_id),
        "fetched_at": now_iso(),
        # 任务口径：source 写成 "vjudge"（PTA 版这里是 dict；下游没有任何代码读它）
        "source": "vjudge",
        "problem_set": {
            "id": str(contest_id),
            "name": title,
            "description": "",
            "type": "VJUDGE_CONTEST",
            "timeType": "FIXED_TIME",
            "startAt": iso(start_dt),
            "endAt": iso(end_dt),
            "duration": int(duration_seconds),
            "isReplay": bool(data.get("isReplay")),
        },
        "contest": {
            "title": title,
            "short_title": title,
            "start_epoch": start_epoch,
            "start_iso": iso(start_dt),
            "end_epoch": end_epoch,
            "end_iso": iso(end_dt),
            "duration_seconds": duration_seconds,
            "length": format_hms(duration_seconds),
            # 这个接口拿不到封榜设置：先写 0（= 不封榜），并在 warnings 里说明。
            "freeze_seconds": 0,
            "freeze_length": format_hms(0),
            "penalty_minutes": int(penalty_minutes),
            "start_source": "vjudge.begin",
        },
        "problems": problems,
        "teams": teams,
        "submissions": submissions,
        "warnings": warnings,
    }
    contest["stats"] = contest_stats(contest)
    contest["fetch_report"] = {
        "pages": 1,
        "truncated": False,
        "duration_seconds": None,       # 由 cmd_fetch 填真实抓取耗时
        "at": contest["fetched_at"],
        "api": VJ_RANK_URL.format(contest_id=contest_id),
        "submissions_fetched": len(submissions),
        "submissions_total": len(submissions),
        "teams": len(teams),
        "problems": len(problems),
        "used_cache": False,
    }
    # 一定带上的说明（任务要求：freeze 拿不到 → warnings 里说清楚）
    warnings.insert(0, "VJudge 的 rank 接口不提供封榜设置，freeze_seconds 固定写 0（不封榜）；"
                       "要封榜请在实时榜单页单独设「封榜时长」。")
    if not any("没有题名" in w for w in warnings):
        warnings.append("VJudge rank 接口不提供题目标题，题目名用字母（A/B/…/AA…）代替。")
    return contest, warnings


def contest_stats(contest: dict[str, Any]) -> dict[str, Any]:
    """与 PTA 版 ``pta_common.contest_stats`` **同名同义**的统计块。"""
    subs = contest.get("submissions") or []
    statuses: dict[str, int] = {}
    for sub in subs:
        key = str(sub.get("status") or "UNKNOWN")
        statuses[key] = statuses.get(key, 0) + 1
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


# --------------------------------------------------------------------------
# 原子落盘（与 pta_common.save_contest 同一套路：临时文件 + fsync + .bak）
# --------------------------------------------------------------------------

def save_contest(path: Path, contest: dict[str, Any]) -> None:
    """原子写 contest.json —— **绝不留半截 JSON**。

    临时文件 → flush + fsync → 上一份留成 ``.bak`` → ``os.replace``。
    与 PTA 版逐字节一致（缩进 2、CRLF、结尾换行），这样两种来源的
    contest.json 在 diff 里不会因为换行风格不同而整份变红。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
                os.link(path, bak)
            except OSError:
                try:
                    bak.write_bytes(path.read_bytes())
                except OSError:
                    pass
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# 命令：fetch
# --------------------------------------------------------------------------

def cmd_fetch(args: argparse.Namespace) -> int:
    started = time.monotonic()
    contest_id = normalize_contest_id(args.contest)
    if not contest_id:
        die(f"--contest 认不出来：{args.contest!r}。\n"
            "  填 862258 这种数字，或直接粘 https://vjudge.net/contest/862258#rank", 2)
    out_path = Path(args.out)
    if not args.quiet:
        print("[i] 平台      : VJudge")
        print(f"[i] 比赛 ID   : {contest_id}")
        print(f"[i] 数据源    : {VJ_RANK_URL.format(contest_id=contest_id)}")
        print(f"[i] 输出文件  : {out_path.resolve()}")

    cookie = load_cookie(args.cookie_file)
    client = VJudgeClient(cookie, timeout=args.timeout,
                          max_retries=args.max_retries, verbose=not args.quiet)
    print("[1/3] 获取 rank JSON ...")
    url = VJ_RANK_URL.format(contest_id=contest_id)
    data = client.get_json(url)
    if not isinstance(data, dict):
        raise VJudgeParseError("rank 接口返回的不是 JSON 对象")
    if not args.quiet:
        print(f"      participants={len(data.get('participants') or {})} "
              f"submissions={len(data.get('submissions') or [])}")

    alias_map, alias_note = load_aliases(args.db)
    if not args.quiet:
        print(f"[2/3] 备注库：{alias_note}（{len(alias_map)} 条）")

    print("[3/3] 规范化 + 落盘 ...")
    contest, warnings = build_contest(
        data, contest_id,
        penalty_minutes=max(0, int(args.penalty)),
        alias_map=alias_map,
        out_dir=out_path.resolve().parent,
        avatars=not getattr(args, "no_avatars", False),
        avatar_budget=float(args.avatar_budget),
        timeout=args.timeout,
        verbose=not args.quiet,
    )
    duration = time.monotonic() - started
    contest["fetch_report"]["duration_seconds"] = round(duration, 3)

    if args.refresh_meta:
        # VJudge 每次都是全量重抓，"强制刷新元信息" 本来就是默认行为；保留参数只为 CLI 习惯一致。
        warnings.append("--refresh-meta：VJudge 抓取本来就是每轮全量重抓，该参数不影响结果。")

    for msg in warnings:
        warn(msg)
    save_contest(out_path, contest)

    stats = contest["stats"]
    if not args.quiet:
        print("")
        print("=" * 62)
        print(f"  比赛      : {contest['contest']['title']}")
        print(f"  开始时间  : {contest['contest']['start_iso']}")
        print(f"  比赛时长  : {contest['contest']['length']}（{int(contest['contest']['duration_seconds'])} 秒）")
        print(f"  题目数    : {stats['problem_count']}")
        print(f"  队伍数    : {stats['team_count']}")
        print(f"  提交数    : {stats['submission_count']}")
        print(f"  结果分布  : {stats['status_counts']}")
        print(f"  已写入    : {out_path.resolve()}")
        print(f"  抓取耗时  : {duration:.2f} 秒")
        print("=" * 62)
    return 0


# --------------------------------------------------------------------------
# 命令：check（只读，不写任何文件；GUI 环境预检用）
# --------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    contest_id = normalize_contest_id(args.contest)
    if not contest_id:
        print(f"[error] --contest 认不出来：{args.contest!r}", file=sys.stderr)
        return 2
    cookie = load_cookie(args.cookie_file)
    client = VJudgeClient(cookie, timeout=args.timeout,
                          max_retries=args.max_retries, verbose=False)
    print("=" * 62)
    print("  VJudge 接口连通性探测（只读，不写任何文件）")
    print("=" * 62)
    print(f"  比赛 ID : {contest_id}")
    print(f"  Cookie  : {'已提供（私有赛用）' if cookie else '无（公开赛可留空）'}")
    url = VJ_RANK_URL.format(contest_id=contest_id)
    try:
        data = client.get_json(url)
    except VJudgeApiError as exc:
        print(f"  ✗ {exc}")
        print("    比赛不存在 / 未公开时就是这条；私有赛请带 --cookie-file。")
        print("=" * 62)
        return 3
    except VJudgeNetworkError as exc:
        print(f"  ✗ 网络不通：{exc}")
        print("=" * 62)
        return 6
    except VJudgeParseError as exc:
        print(f"  ✗ {exc}")
        print("=" * 62)
        return 5
    if not isinstance(data, dict) or "participants" not in data:
        print("  ✗ 返回结构不是 rank JSON（缺少 participants）")
        print("=" * 62)
        return 5
    subs = data.get("submissions") or []
    max_idx = max((int(r[1]) for r in subs if isinstance(r, (list, tuple)) and len(r) > 1), default=-1)
    print(f"  [OK] 比赛标题 : {data.get('title')}")
    print(f"  [OK] 队伍数   : {len(data.get('participants') or {})}")
    print(f"  [OK] 提交数   : {len(subs)}")
    print(f"  [OK] 题目数   : {max_idx + 1}（题号下标 0..{max_idx}）")
    begin, length = data.get("begin"), data.get("length")
    if begin and length:
        start = datetime.fromtimestamp(float(begin) / 1000.0, timezone.utc)
        print(f"  [OK] 比赛时间 : {iso(start)} 起，时长 {format_hms(float(length) / 1000.0)}")
    print("-" * 62)
    print("  ✓ 比赛 ID 可读，可以点「拉取数据」。未写入任何文件。")
    print("=" * 62)
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vjudge_fetch.py",
        description="从 VJudge 抓比赛数据并落盘为与 PTA 同构的 contest.json（匿名公开接口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("command", nargs="?", default="fetch",
                        choices=["fetch", "check"],
                        help="fetch=抓取并写盘（默认）; check=只读探测（不写文件）")
    parser.add_argument("-c", "--contest", default="",
                        help="VJudge 比赛 ID（862258），也可以直接粘 "
                             "https://vjudge.net/contest/862258#rank")
    parser.add_argument("--cookie-file", default="",
                        help="可选：私有赛需要登录时用（公开赛留空即可）")
    parser.add_argument("-o", "--out", default="contest.json",
                        help="contest.json 输出路径（默认当前目录 contest.json）")
    parser.add_argument("--db", default="",
                        help="备注 / 别名库路径（默认 <程序根>\\vjudge-aliases.db）")
    parser.add_argument("--penalty", type=int, default=20,
                        help="每次错误提交的罚时（分钟），默认 20")
    parser.add_argument("--refresh-meta", action="store_true",
                        help="兼容 PTA 版习惯（VJudge 每轮本来就是全量重抓）")
    parser.add_argument("--no-avatars", action="store_true",
                        help="不下载 VJudge 头像（离线 / 省流量）")
    parser.add_argument("--avatar-budget", type=float, default=15.0,
                        help="本轮最多花多少秒缓存头像（默认 15；轮询器每 10 秒跑一轮，别调大）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"单请求超时秒数，默认 {DEFAULT_TIMEOUT}")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
                        help=f"失败重试次数，默认 {DEFAULT_MAX_RETRIES}（间隔 {RETRY_SLEEP:.0f} 秒）")
    parser.add_argument("-q", "--quiet", action="store_true", help="不打印进度（给轮询器用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return cmd_check(args)
        return cmd_fetch(args)
    except VJudgeApiError as exc:
        print(f"\n[比赛不可读] {exc}", file=sys.stderr)
        print("  -> 检查比赛 ID；公开比赛不用登录，私有比赛请用 --cookie-file 带上 Cookie。",
              file=sys.stderr)
        return 3
    except VJudgeParseError as exc:
        print(f"\n[返回内容异常] {exc}", file=sys.stderr)
        print("  -> VJudge 接口可能变更，或请求被验证页拦截。", file=sys.stderr)
        return 5
    except VJudgeNetworkError as exc:
        print(f"\n[网络错误] {exc}", file=sys.stderr)
        print("  -> 检查网络 / 代理，稍后重试（未写入任何文件，旧 contest.json 保持不变）。",
              file=sys.stderr)
        return 6
    except KeyboardInterrupt:
        print("\n[已中断] 未写入 contest.json，已有数据保持不变。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
