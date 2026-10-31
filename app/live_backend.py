# -*- coding: utf-8 -*-
"""live_backend.py —— 「实时榜单」模式的后端（进程管理 / 运行目录 / 健康检查）。

设计约束（见任务说明）
--------------------
* ``live-board``（只读库，和本目录同级或随发行版一起打包）本模块只读它、
  只用 ``sys.path`` 引它的模块，绝不修改它下面的任何文件。所有新代码都在 ``app\\`` 下。
* 运行目录（可写）默认是 ``<work_root>\\live\\``：第一次启动时把 live-board 的
  py + web + 初始数据拷进去，poller / server 都在这个副本里跑，
  所以 poller 写的 ``state.json`` / ``contest.json`` / ``logs\\poller.log``
  都不会污染 live-board 那份（那边可能正有人在做回放）。
* 子进程一律 ``CREATE_NO_WINDOW``（不闪黑框）；停止时先 terminate，
  再用 ``taskkill /T /F`` 连子进程一起收，最后用 ``tasklist`` 按 PID 复核，
  **绝不留孤儿**。
* 本模块不 import tkinter，可以在后台线程里安全调用。
"""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:                     # M18：子进程登记/清理（同目录模块；PyInstaller 靠这行静态收集）
    import child_registry
except Exception:        # noqa: BLE001 - 缺了也不能让整个应用起不来
    child_registry = None  # type: ignore[assignment]

#: live-board 源码位置（开发机固定；打包后走 _MEIPASS / exe 同目录）
LIVE_BOARD_SOURCE = Path(__file__).resolve().parent.parent / "live-board"
#: 旧配置里可能写的端口（别人的回放进程占用中，只用于「这是别人的」提示）
OTHER_PORT = 8765
DEFAULT_PORT = 8791
DEFAULT_ALT_PORT = 8792

#: 拷贝到运行目录的文件（源码 + 网页 + 初始数据）
COPY_FILES = (
    "board_core.py", "contest_api.py", "presentation.py", "poller.py",
    "server.py", "replay.py", "launcher.py", "config.json",
    "contest.json", "state.json",
    # VJudge 版第一步：两个平台适配文件（运行目录里也会用到它们）
    "vjudge_fetch.py", "vjudge_alias.py",
)
COPY_DIRS = ("web",)

_PORT_RE = re.compile(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$")
_COOKIE_RE = re.compile(
    r"(?:^|[\s'\"-])[Cc]ookie\s*:\s*(?P<value>[^'\"\r\n]+)", re.MULTILINE)
_H_COOKIE_RE = re.compile(r"-H\s+(?P<q>['\"])(?P<value>[^'\"]*?[Cc]ookie:[^'\"]*?)(?P=q)")
#: fetch / JSON 里 ``"Cookie": "a=b"`` 这种带引号的写法（前两条都匹配不到时才兜底）。
_QUOTED_COOKIE_RE = re.compile(r"[Cc]ookie['\"]?\s*:\s*['\"](?P<value>[^'\"]{3,})['\"]")
#: 裸令牌：纯十六进制串（PTA 令牌的常见形态，实测用户那份是 **31 位**：
#: ``A83316E541D0C7FFAB56B2EA0C61465``）或 UUID —— 补成 ``PTASession=<值>``。
_BARE_TOKEN_RE = re.compile(
    r"^(?:[0-9a-fA-F]{16,128}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
#: 单个 ``名字=值``：等号两边不留空格、值里没有空白/分号/引号 —— 只粘一行 ``PTASession=xxx`` 时走这条。
_SINGLE_PAIR_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}=[^\s;\"'`]+$")
#: 「看起来是令牌、不是 Cookie」：一整串没有等号的 token（16 字符以上）。
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-+/]{16,256}$")

#: 两个会话字段的**常见形态**（只作启发式，判不出来一律让你选，不瞎猜）。
#: 实测样本（2026-10-13 / 10-16）：
#:   ``JSESSIONID`` —— 32 位十六进制（Tomcat 惯例，大小写都有），
#:                     例如 ``9d2f7c1a5b3e4f60819273a4b5c6d7e8``；
#:   ``PTASession`` —— UUID（``5d34aa39-4a37-42f3-a0e9-01f54755b7b6``）
#:                     或 31 位十六进制（``5d34c1b0f3a94e0d8b7c6a5f4e3d2c1b``）；
#:   **学号** —— 6~12 位纯数字（用户手滑把学号粘进来时要能认出来"这不是会话值"）。
_JSESSIONID_SHAPE_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_PTASESSION_HEX_RE = re.compile(r"^[0-9a-fA-F]{31}$")
_STUDENT_NUMBER_RE = re.compile(r"^\d{6,12}$")

#: 两个字段在 cookie.txt 里的标准顺序（JSESSIONID 在前）。
SESSION_FIELDS = ("JSESSIONID", "PTASession")
_SESSION_LOWER = {name.lower(): name for name in SESSION_FIELDS}

#: 整段复制的最简做法 —— 对话框、失败提示、界面提示共用同一段话（用户最容易踩的一步）。
COOKIE_COPY_AS_CURL = (
    "整段复制（最省事，**一个字符都不用改**）：DevTools → 网络/Network → 刷新页面 → "
    "右键任意 pintia.cn/api/… 请求 → Copy → Copy as cURL (bash) → "
    "粘进最上面的「整段粘贴」框 → 点「解析并保存」。")

#: PTA 实测（2026-10-13 / 10-16）：**必须同时带 JSESSIONID 和 PTASession**，只带 PTASession 会 401。
#: 下面这段话在对话框、失败提示、界面提示、README 里原样复用（用户两次踩坑都在这上面）。
COOKIE_JSESSIONID_HELP = (
    "**两个都要带**：只带 PTASession 实测会 401（两个一起才通）。\n"
    "① " + COOKIE_COPY_AS_CURL + "\n"
    "② 两个值分别粘：DevTools → 应用程序/Application → Cookie → https://pintia.cn → "
    "在 Request cookies 里（不是 Response cookies）**点一行**、在下面的 Value 列复制"
    "（别双击值单元格，双击会就地编辑/清空它）→ 分别粘进「JSESSIONID」和「PTASession」两个框。\n"
    "③ 有什么粘什么：整段 cURL、`A=1 B=2`（空格分隔）、两行分开写、甚至两个裸值分开粘都认；"
    "两个裸值分不清谁是谁时程序会让你选，**不会瞎猜**。")


def cookie_diagnosis(value: str) -> tuple[str, str]:
    """给一条 Cookie 串做「体检」：两个必需的字段在不在。

    返回 ``(level, 一句话)``：
    * ``ok``      —— JSESSIONID + PTASession 都在；
    * ``warning`` —— 缺其中一个（附 2026-10-13 的实测结论）；
    * ``unknown`` —— 两个都没有，字段不像 PTA 的 Cookie。
    """
    names = {piece.split("=", 1)[0].strip().lower()
             for piece in (value or "").split(";") if "=" in piece}
    if "jsessionid" in names and "ptasession" in names:
        return "ok", "已包含 JSESSIONID + PTASession ✅"
    if "ptasession" in names:
        return "warning", "缺少 JSESSIONID：实测会 401（2026-10-13），建议补上"
    if "jsessionid" in names:
        return "warning", "只有 JSESSIONID、没有 PTASession：一样会被拒（401），两个都要带"
    return "unknown", "字段不像 PTA 的 Cookie：既没有 JSESSIONID 也没有 PTASession"


def iter_named_pairs(text: str) -> list[tuple[str, str]]:
    """从任意文本里按顺序抽出 ``名字=值`` 对。

    分隔符宽松到「分号 / 逗号 / 换行 / 空白都算」，所以下面 4 种写法都能读出来：

        JSESSIONID=xxx; PTASession=yyy      （标准）
        JSESSIONID=xxx PTASession=yyy       （空格分隔、无分号）
        JSESSIONID=xxx\nPTASession=yyy      （两行分别写）
        JSESSIONID=xxx,PTASession=yyy       （逗号，偶尔从 JSON 里带出来）

    名字必须是类 Cookie 名的安全字符、值里不能有空白，避免把 cURL 的
    ``--data '{"a":"b"}'`` 之类误当成 Cookie。
    """
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"[;,\r\n\t ]+", text or ""):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip().strip("'\"").lstrip("-")
        value = value.strip().strip("'\"").rstrip(";,")
        if not name or not value or re.search(r"\s", value):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name):
            continue
        out.append((name, value))
    return out


def cookie_field_names(value: str) -> list[str]:
    """一条 Cookie 串里有哪几个字段（保持原文顺序，同名只留第一次）。"""
    names: list[str] = []
    for name, _v in iter_named_pairs(value):
        if name not in names:
            names.append(name)
    return names


def canonical_cookie(pairs: "list[tuple[str, str]] | dict[str, str]") -> str:
    """按统一顺序拼回一行 Cookie：**JSESSIONID 在前、PTASession 在后**，其余字段按原文顺序。

    只用最后一次出现的同名值（用户可能先粘了错的、又粘了对的）。
    """
    items = list(pairs.items()) if isinstance(pairs, dict) else list(pairs)
    ordered: dict[str, str] = {}
    for name, value in items:
        if not name or not value:
            continue
        ordered[name] = value                   # 后面的覆盖前面的
    out: list[str] = []
    for field in SESSION_FIELDS:                # 标准字段固定顺序
        for name, value in ordered.items():
            if name.lower() == field.lower() and name not in out:
                out.append(f"{name}={value}")
    for name, value in ordered.items():         # 其余字段按原文顺序
        if name.lower() not in _SESSION_LOWER:
            out.append(f"{name}={value}")
    return "; ".join(out)


def _token_shape(value: str) -> str:
    """猜一个裸值像什么：``jsessionid`` / ``ptasession`` / ``student`` / ``token`` / ````。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if _STUDENT_NUMBER_RE.match(raw):
        return "student"
    if _JSESSIONID_SHAPE_RE.match(raw):
        return "jsessionid"
    if _PTASESSION_HEX_RE.match(raw) or re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw):
        return "ptasession"
    if _BARE_TOKEN_RE.match(raw):
        return "token"
    return ""


def classify_bare_pair(first: str, second: str) -> tuple[dict[str, str] | None, str]:
    """两个裸值分开粘时，判断谁是谁。

    返回 ``({"JSESSIONID": …, "PTASession": …}, 依据)``；**判不出来返回 ``(None, 说明)`` ——
    这时界面上要让用户自己选，绝不瞎猜**。判定依据是实测形态
    （见 ``_JSESSIONID_SHAPE_RE`` 上面的注释）与「像学号不像学号」：
    6~12 位纯数字更像学号，不是会话值。
    """
    a, b = (first or "").strip(), (second or "").strip()
    if not a or not b:
        return None, "只粘了一个裸值，没法判断它是 JSESSIONID 还是 PTASession。"
    if a == b:
        return None, "两个裸值一模一样，分不出哪个是 JSESSIONID、哪个是 PTASession。"
    shape_a, shape_b = _token_shape(a), _token_shape(b)
    # ① 一个是 UUID（PTASession 的常见形态）→ 另一个就是 JSESSIONID
    if shape_a == "ptasession" and shape_b != "ptasession":
        return {"JSESSIONID": b, "PTASession": a}, "UUID"
    if shape_b == "ptasession" and shape_a != "ptasession":
        return {"JSESSIONID": a, "PTASession": b}, "UUID"
    # ② 32 位十六进制（JSESSIONID）对 31 位十六进制（PTASession）
    if shape_a == "jsessionid" and shape_b == "ptasession":
        return {"JSESSIONID": a, "PTASession": b}, "32/31 位十六进制"
    if shape_b == "jsessionid" and shape_a == "ptasession":
        return {"JSESSIONID": b, "PTASession": a}, "32/31 位十六进制"
    # ③ 其中一个是学号（纯数字）——它不是会话值，但也说明不了另一个叫什么
    if "student" in {shape_a, shape_b}:
        return None, ("其中一个看起来是**学号**（6~12 位纯数字），不是会话值；"
                      "剩下的那个是 JSESSIONID 还是 PTASession 判断不了。")
    if shape_a and shape_a == shape_b:
        return None, f"两个裸值形态相同（都像 {shape_a}），分不出哪个是 JSESSIONID。"
    return None, "两个裸值都没有能识别形态，分不出哪个是 JSESSIONID、哪个是 PTASession。"


def bare_pair_parts(paste: str) -> list[str]:
    """整段粘贴是不是「两个裸值」？是就返回那两个值，否则空列表。"""
    raw = (paste or "").strip()
    if not raw:
        return []
    for pattern in (_H_COOKIE_RE, _COOKIE_RE, _QUOTED_COOKIE_RE):
        if pattern.search(raw):
            return []
    pairs = iter_named_pairs(raw)
    if pairs and any(name.lower() in _SESSION_LOWER for name, _v in pairs):
        return []
    loose = [chunk for chunk in re.split(r"[;,\s]+", raw) if chunk]
    if len(loose) == 2 and all(_token_shape(part) for part in loose):
        return loose
    return []


def _bare_pair_options(first: str, second: str) -> list[dict[str, str]]:
    """两个裸值判不出来时，给用户的两个选项（默认第一个 = 按粘贴顺序）。"""
    return [
        {"label": "按粘贴顺序：第 1 个 = JSESSIONID，第 2 个 = PTASession",
         "cookie": canonical_cookie([("JSESSIONID", first), ("PTASession", second)])},
        {"label": "反过来：第 1 个 = PTASession，第 2 个 = JSESSIONID",
         "cookie": canonical_cookie([("JSESSIONID", second), ("PTASession", first)])},
    ]


def _bare_options_with_box(loose: list[str], js: str, ps: str) -> list[dict[str, str]]:
    """两个裸值判不出来、但一个独立输入框已经填好了 → 只让用户选「哪个裸值是另一个字段」。"""
    options: list[dict[str, str]] = []
    if ps:                                   # PTASession 已确定 → 选哪个裸值是 JSESSIONID
        for idx, value in enumerate(loose, 1):
            options.append({
                "label": f"第 {idx} 个裸值是 JSESSIONID（PTASession 用你输入框里填的）",
                "cookie": canonical_cookie([("JSESSIONID", value), ("PTASession", ps)]),
            })
    elif js:                                 # JSESSIONID 已确定 → 选哪个裸值是 PTASession
        for idx, value in enumerate(loose, 1):
            options.append({
                "label": f"第 {idx} 个裸值是 PTASession（JSESSIONID 用你输入框里填的）",
                "cookie": canonical_cookie([("JSESSIONID", js), ("PTASession", value)]),
            })
    return options



def _missing_of(value: str) -> list[str]:
    """``value`` 里缺哪几个必需字段（按 JSESSIONID / PTASession 的顺序）。"""
    names = {name.lower() for name in cookie_field_names(value)}
    return [name for name in SESSION_FIELDS if name.lower() not in names]


def _cookie_ok(value: str, head: str) -> tuple[str, str]:
    """成功返回：解析摘要 + 字段体检；缺字段时把两种做法一起写进说明里。"""
    level, sentence = cookie_diagnosis(value)
    note = f"{head}；{sentence}"
    if level != "ok":
        note += "\n" + COOKIE_JSESSIONID_HELP
    return value, note


def _cookie_help(raw: str, lead: str) -> tuple[str, str]:
    """失败时的统一说明：先说你粘的是什么，再给两条能照着做的路（用户卡住时的救命文案）。"""
    snippet = " ".join((raw or "").split())[:60]
    return "", (
        f"{lead}\n"
        f"① {COOKIE_COPY_AS_CURL}\n"
        "② 两个值分别粘：DevTools → 应用程序/Application → Cookie → https://pintia.cn → "
        "在 Request cookies 里（不是 Response cookies）**点一行**、在下面的 Value 列复制"
        "（别双击值单元格，双击会就地编辑/清空它）→ 分别粘进「JSESSIONID」和「PTASession」两个框。\n"
        f"（只带 PTASession 实测会 401（亲测），两个一起才通。"
        f"你粘进来的内容：{snippet!r}）")


def parse_cookie_from_curl(text: str) -> tuple[str, str]:
    """把用户粘进来的东西变成一行能用的 Cookie，并顺手体检。

    认得这几种输入（越靠前越优先），返回 ``(cookie, 说明)``，失败时 cookie 为空串：
    1. cURL 的 ``-H 'cookie: a=b; c=d'``（单双引号都认）；
    2. 文本里任意位置的 ``Cookie: a=b; c=d``；
    3. fetch/JSON 里带引号的 ``"Cookie": "a=b"``；
    4. **名字对得上的 ``k=v`` 组合** —— 分号 / 空格 / 换行 / 逗号分隔都行：
       ``JSESSIONID=x PTASession=y``、两行分开写、``JSESSIONID=x;PTASession=y`` 都读得出来
       （只有一项也算，``PTASession=xxx`` 没有分号也收）；
    5. 裸令牌：纯十六进制串 / UUID —— 按 ``PTASession=<值>`` 处理
       （用户真实踩到：先粘 Response cookies 里的 ``JSESSIONID`` 值、又粘只带
       ``PTASession`` 的串，两次都卡住；**PTA 必须 JSESSIONID + PTASession 两个都带**）；
    6. **两个裸值分开粘**（``9BC2… 0774…``）：能按形态判定就自动配好，
       判不出来就返回一句「请自己选」的说明（界面会给两个选项，见 ``cookie_choice_options``）。

    说明文字是给人看的：成功时是「解析摘要 + 字段体检」，缺字段时连带两种补法一起写；
    失败时写「你粘的是什么」+ 两条能照做的做法。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ("整段粘贴框是空的 —— " + COOKIE_COPY_AS_CURL +
                    " 也可以把两个值分别粘进下面「JSESSIONID」「PTASession」两个框。")
    found: list[str] = []
    for pattern in (_H_COOKIE_RE, _COOKIE_RE, _QUOTED_COOKIE_RE):
        for match in pattern.finditer(raw):
            found.append(match.group("value"))
        if found:
            break
    if not found:
        # ---- 没有 Cookie 头：先试「名字对得上的 k=v」（空格/换行/分号分隔都行）----
        pairs = iter_named_pairs(raw)
        named = [(n, v) for n, v in pairs if n.lower() in _SESSION_LOWER]
        if named:
            value = canonical_cookie(pairs)
            return _cookie_ok(
                value,
                f"按名字解析出 {len(cookie_field_names(value))} 个字段"
                f"（分隔符：分号/空格/换行都认）")
        if _BARE_TOKEN_RE.match(raw):
            # 裸令牌没有 Cookie 名字，按 PTA 的会话字段补上；但裸值也可能是 Response cookies
            # 里的 JSESSIONID（用户第一次粘的正是它），所以这里必须把警告一起给出去。
            return _cookie_ok(
                "PTASession=" + raw,
                "看起来是裸令牌（没有 Cookie 名字），已按 PTASession=<值> 处理"
                " —— 裸值也可能来自 Response cookies 里的 JSESSIONID（只带 PTASession 会 401）")
        # ---- 两个裸值分开粘：按形态猜；猜不出就交给界面让用户选（不瞎猜）----
        loose = bare_pair_parts(raw)
        if loose:
            assign, reason = classify_bare_pair(loose[0], loose[1])
            if assign is not None:
                value = canonical_cookie(assign)
                first_is_js = assign.get("JSESSIONID") == loose[0]
                return _cookie_ok(value, (
                    "两个裸值分别粘：按形态判断 → 第 "
                    f"{1 if first_is_js else 2} 个是 JSESSIONID、第 "
                    f"{2 if first_is_js else 1} 个是 PTASession"
                    f"（依据：{reason}）。"
                    "判错的话请用「JSESSIONID」「PTASession」两个框分别粘"))
            return "", (
                f"{reason}\n这**不是错误**：两个值都在，只是名字对不上。"
                "点下面的选项（默认按粘贴顺序）告诉程序哪一个是 JSESSIONID 就能继续。")
        if _SINGLE_PAIR_RE.match(raw) or ("=" in raw and ";" in raw
                                          and " " not in raw.split("=", 1)[0]):
            return _cookie_ok(raw, f"解析出 {len(raw)} 字符 / {len(cookie_field_names(raw))} 个字段")
        if _OPAQUE_TOKEN_RE.match(raw):
            snippet = raw[:60]
            return _cookie_help(
                raw, f"你粘的看起来是「{snippet}」（PTA 的令牌 / 会话值），不是 Cookie，"
                     "所以没法直接用。按下面任一条做都行：")
        return _cookie_help(raw, "没找到 Cookie 头，也认不出这是 Cookie 串。按下面任一条做都行：")
    value = found[0].strip()
    head = value.lower().find("cookie:")
    if head >= 0:
        value = value[head + len("cookie:"):].strip()
    value = value.replace("\\\n", " ").replace("\r", " ").replace("\n", " ").strip()
    if value.endswith("'"):
        value = value[:-1]
    if not value or "=" not in value:
        return "", f"解析出来是空的（原文片段：{value[:60]!r}），请重新复制一次。"
    return _cookie_ok(value, f"解析出 {len(value)} 字符 / {len(cookie_field_names(value))} 个字段")


def cookie_choice_options(paste: str) -> list[dict[str, str]]:
    """整段粘贴里「两个裸值分不清谁是谁」时，给界面用的两个选项（空列表 = 不需要选）。"""
    loose = bare_pair_parts(paste)
    if not loose:
        return []
    if classify_bare_pair(loose[0], loose[1])[0] is not None:
        return []
    return _bare_pair_options(loose[0], loose[1])


def parse_cookie_inputs(paste: str = "", jsessionid: str = "",
                        ptasession: str = "") -> dict[str, Any]:
    """对话框的统一入口：**两个独立输入框 + 整段粘贴框合并判断**（哪个有值用哪个）。

    返回::

        cookie          拼好的 Cookie（空串 = 还不能用）
        level           ok / warning / unknown
        note            给用户看的一句话（成功时是「已识别：JSESSIONID + PTASession ✅」）
        missing         ["JSESSIONID"] / ["PTASession"] / [] —— 缺哪个点明哪个
        source          这些值是从哪来的（界面上要显示出来）
        needs_choice    True = 两个裸值判不出谁是谁，请用户点一下选项
        choices         [{label, cookie}, …]（needs_choice 时给界面用）
    """
    def _clean_explicit(text: str, name: str) -> str:
        """独立输入框里的值：允许用户连 ``JSESSIONID=xxx`` 一起粘进来。"""
        value = (text or "").strip().strip("'\"").rstrip(";,")
        if "=" in value and value.split("=", 1)[0].strip().lower() == name.lower():
            value = value.split("=", 1)[1].strip().strip("'\"")
        return value

    js = _clean_explicit(jsessionid, "JSESSIONID")
    ps = _clean_explicit(ptasession, "PTASession")
    raw = (paste or "").strip()

    result: dict[str, Any] = {"cookie": "", "level": "unknown", "note": "",
                              "missing": [], "source": "", "needs_choice": False,
                              "choices": []}

    def _finish(value: str, source: str, extra: str = "") -> dict[str, Any]:
        """收尾：算缺字段 / 等级 / 一句让人看得懂的话。"""
        result["cookie"] = value
        result["source"] = source
        result["missing"] = _missing_of(value)
        if not result["missing"]:
            result["level"] = "ok"
            result["note"] = f"已识别：JSESSIONID + PTASession ✅（{source}）"
        else:
            result["level"] = "warning"
            has = " + ".join(name for name in SESSION_FIELDS if name not in result["missing"])
            result["note"] = (
                f"已识别：{has}；**缺 " + "、".join(result["missing"]) + "**"
                "（缺哪个就必须补哪个：实测只带 PTASession 会 401）")
        if extra:
            result["note"] += "\n" + extra
        return result

    # ① 两个框都填了 —— 最明确的一条路（整段粘贴里若还有别的字段，也一起带上）
    if js and ps:
        fields: dict[str, str] = {}
        if raw:
            paste_value, _paste_note = parse_cookie_from_curl(raw)
            for name, value in iter_named_pairs(paste_value):
                fields[name] = value
        fields.pop("JSESSIONID", None)
        fields.pop("PTASession", None)
        fields["JSESSIONID"] = js
        fields["PTASession"] = ps
        return _finish(canonical_cookie(fields), "两个输入框")

    # ② 整段粘贴：先整条解析出来（这样 cURL 里的 _uid / pintia_user 等字段一个都不丢）
    paste_value, paste_note = ("", "")
    if raw:
        paste_value, paste_note = parse_cookie_from_curl(raw)

    # ③ 两个裸值分不清谁是谁 → 请用户选（不瞎猜）。有一个输入框填好了就只用选另一个。
    if raw and not paste_value:
        loose = bare_pair_parts(raw)
        if loose:
            assign, reason = classify_bare_pair(loose[0], loose[1])
            if assign is not None:
                return _finish(canonical_cookie(assign), "整段粘贴",
                               f"两个裸值分别粘：按形态判断（依据：{reason}）——"
                               "判错的话请用「JSESSIONID」「PTASession」两个框分别粘。")
            options = _bare_options_with_box(loose, js, ps) or _bare_pair_options(*loose)
            result["needs_choice"] = True
            result["choices"] = options
            result["note"] = (reason + "\n这**不是错误**：值都在，只是名字对不上 —— "
                              "点下面的选项告诉程序哪一个是 JSESSIONID 就能继续。")
            return result
        # 整段粘贴压根认不出来，但某个框里有值 → 用框里的 + 缺的照常点明
        if js or ps:
            fields = {}
            if js:
                fields["JSESSIONID"] = js
            if ps:
                fields["PTASession"] = ps
            return _finish(canonical_cookie(fields), "输入框",
                           ("整段粘贴没认出可用字段；" + paste_note.splitlines()[0]) if paste_note else "")

    # ④ 整段粘贴解析成功 → 以它为准，两个输入框里填了的值覆盖同名字段
    if paste_value:
        fields = {}
        for name, value in iter_named_pairs(paste_value):
            fields[name] = value
        if js:
            fields = {k: v for k, v in fields.items() if k.lower() != "jsessionid"}
            fields["JSESSIONID"] = js
        if ps:
            fields = {k: v for k, v in fields.items() if k.lower() != "ptasession"}
            fields["PTASession"] = ps
        source = "整段粘贴"
        if jsessionid.strip() or ptasession.strip():
            source = "整段粘贴 + 输入框"
        return _finish(canonical_cookie(fields), source, paste_note)

    # ⑤ 只有独立输入框有值
    if js or ps:
        fields = {}
        if js:
            fields["JSESSIONID"] = js
        if ps:
            fields["PTASession"] = ps
        return _finish(canonical_cookie(fields), "输入框")

    # ⑥ 三个都空
    if not raw:
        result["note"] = ("整段粘贴框和两个输入框都是空的 —— " + COOKIE_COPY_AS_CURL +
                          " 也可以只把两个值分别粘进「JSESSIONID」「PTASession」两个框。")
        return result

    # ⑦ 整段粘贴有内容，但认不出可用字段 → 原样给「你粘的是什么 + 两条做法」
    result["note"] = paste_note or (
        "认不出这是什么 —— " + COOKIE_COPY_AS_CURL +
        " 也可以只把两个值分别粘进「JSESSIONID」「PTASession」两个框。")
    return result


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def live_board_source() -> Path:
    """只读的 live-board 源目录（开发机 / 打包产物都能找到）。

    候选顺序（第一个"看起来像 live-board"的胜出）：

    * 打包后：``_MEIPASS\\live-board``（onedir 的 ``_internal\\live-board``）、
      ``_MEIPASS\\app\\live-board``、``<exe目录>\\live-board``、``<exe目录>\\_internal\\live-board``；
    * 源码模式：``LIVE_BOARD_SOURCE``（= ``<仓库>\\live-board``）、
      **``<仓库的上一级>\\live-board``**（真实只读库与 ``pta-resolver\\`` **平级**，
      M16b 补上这一条：之前源码模式一条候选都对不上，于是 ``sync_run_dir``
      拿到一个不存在的源、运行目录永远空着）、``app\\live-board``。
    """
    here = Path(__file__).resolve()
    cands: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(Path(base) / "live-board")
        cands.append(Path(base) / "app" / "live-board")
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        cands.append(exe_dir / "live-board")
        cands.append(exe_dir / "_internal" / "live-board")
    cands.append(LIVE_BOARD_SOURCE)
    cands.append(here.parent.parent / "liveboard")            # 公开仓库布局：<repo>\liveboard
    cands.append(here.parent.parent.parent / "live-board")     # ← M16b：仓库的上一级
    cands.append(here.parent / "live-board")
    for cand in cands:
        if is_live_board(cand):
            return cand
    return cands[0]


def is_live_board(path: Path) -> bool:
    try:
        return ((path / "server.py").is_file()
                and (path / "poller.py").is_file()
                and (path / "web" / "board.html").is_file()
                and (path / "board_core.py").is_file())
    except OSError:
        return False


def _probe_writable(folder: Path) -> tuple[bool, str]:
    """目录能不能写（能建目录 + 能落一个探针文件）。任何异常都不往外抛。"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"建不了目录 {folder}：{exc}"
    probe = folder / ".pta-live-write-test"
    try:
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True, str(folder)
    except OSError as exc:
        return False, f"目录不可写 {folder}：{exc}"


def _writable_dir(preferred: Path) -> Path:
    """挑一个可写的目录：先试 preferred，不行退 %LOCALAPPDATA%\\PTA-Board\\live，
    再不行退 %TEMP%\\PTA-Board\\live。

    注意：这里必须**逐个把异常吃掉**——打包后的进程如果 preferred 建不出来，
    直接抛异常会让整个实时服务起不来（实测：找 cookie 时就会炸）。
    """
    preferred = Path(preferred)
    ok, _note = _probe_writable(preferred)
    if ok:
        return preferred
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    candidates = []
    if local:
        candidates.append(Path(local) / "PTA-Board" / "live")
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if temp:
        candidates.append(Path(temp) / "PTA-Board" / "live")
    candidates.append(Path.home() / "PTA-Board" / "live")
    for cand in candidates:
        ok, _note = _probe_writable(cand)
        if ok:
            return cand
    return preferred          # 都不行就还是用 preferred，让上层报清晰的错


def run_dir(work_root: Path) -> Path:
    """实时服务的可写运行目录（live-board 的只读副本）。"""
    return _writable_dir(Path(work_root) / "live")


#: 运行目录里**绝对不许被同步覆盖/删除**的用户数据（升级也不能动）。
USER_DATA_NAMES = ("config.json", "contest.json", "state.json", "cookie.txt")
#: 运行目录里的用户数据目录（同步只保证它们存在，绝不清空）
USER_DATA_DIRS = ("logs", "snapshots", "tmp")


def _same_file(a: Path, b: Path) -> bool:
    """两个文件内容是否一致（先比大小，再逐字节比；任何异常都当"不一样"）。"""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return filecmp.cmp(str(a), str(b), shallow=False)
    except OSError:
        return False


def sync_run_dir(run: Path, source: Path | None = None, prune: bool = True) -> list[str]:
    """把 live-board 的 py + web **内容比对后**同步进运行目录；返回做过的事（给日志用）。

    M16b 修的问题（用户实测：升级后运行目录 `live\\web\\` 还是旧页面）：
    ``shutil.copytree(..., dirs_exist_ok=True)`` 本身能覆盖，但**只被
    ``LiveService.start()`` 调**，而且 copytree 的成功路径**一条日志都不打**
    —— 于是「端口被占/没点启动/只开应用」这些路径运行目录永远是旧的，
    用户重开应用看到的还是旧样式。现在：

    * **逐文件内容比对**（大小 + 逐字节），不同才覆盖，并逐条记日志
      （``同步 web\\board.css（内容变了）``）；
    * **清理源里已删的文件**：运行目录 ``web\\`` 下源里没有的、以及顶层
      源里没有的 ``*.py``（例如已经删掉的 ``star_awards_panel.py``）→ 删掉并记日志
      （``prune=False`` 可关掉）；
    * **用户数据绝不动**：``config.json / contest.json / state.json / cookie.txt``
      只在缺失时播种，``logs\\ / snapshots\\ / tmp\\`` 只保证存在；
    * 最后给一条汇总：``运行目录资源已同步（源 N 个文件：更新 M、新增 X、清理 K、未变 S）``。
    """
    source = source or live_board_source()
    notes: list[str] = []
    updated = added = pruned = same = 0
    run.mkdir(parents=True, exist_ok=True)

    # ---- 顶层文件 --------------------------------------------------------
    for name in COPY_FILES:
        src = source / name
        if not src.is_file():
            continue
        dst = run / name
        if name in USER_DATA_NAMES and dst.is_file():
            continue                     # 用户数据只在缺失时播种，绝不覆盖
        try:
            if not dst.is_file():
                shutil.copy2(src, dst)
                notes.append(f"同步 {name}（新增）")
                added += 1
            elif not _same_file(src, dst):
                shutil.copy2(src, dst)
                notes.append(f"同步 {name}（内容变了）")
                updated += 1
            else:
                same += 1
        except OSError as exc:
            notes.append(f"同步 {name} 失败：{exc}")

    # ---- 顶层「幽灵模块」：源里已删、运行目录里还留着的 *.py --------------
    if prune:
        keep = {str(n) for n in COPY_FILES if (source / n).is_file()}
        for item in sorted(run.glob("*.py")):
            if item.name in keep:
                continue
            try:
                item.unlink()
                notes.append(f"清理 {item.name}（源里已删）")
                pruned += 1
            except OSError as exc:
                notes.append(f"清理 {item.name} 失败：{exc}")

    # ---- 子目录（web\）：逐文件比对 + 清理源里已删的 ----------------------
    for name in COPY_DIRS:
        src_dir = source / name
        if not src_dir.is_dir():
            continue
        dst_dir = run / name
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            notes.append(f"同步 {name}\\ 失败：{exc}")
            continue
        for src in sorted(p for p in src_dir.rglob("*") if p.is_file()):
            rel = src.relative_to(src_dir)
            dst = dst_dir / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.is_file():
                    shutil.copy2(src, dst)
                    notes.append(f"同步 {name}\\{rel}（新增）")
                    added += 1
                elif not _same_file(src, dst):
                    shutil.copy2(src, dst)
                    notes.append(f"同步 {name}\\{rel}（内容变了）")
                    updated += 1
                else:
                    same += 1
            except OSError as exc:
                notes.append(f"同步 {name}\\{rel} 失败：{exc}")
        if prune:
            keep_names = {p.relative_to(src_dir).as_posix()
                          for p in src_dir.rglob("*") if p.is_file()}
            for dst in sorted(p for p in dst_dir.rglob("*") if p.is_file()):
                if dst.relative_to(dst_dir).as_posix() in keep_names:
                    continue
                try:
                    dst.unlink()
                    notes.append(f"清理 {name}\\{dst.relative_to(dst_dir)}（源里已删）")
                    pruned += 1
                except OSError as exc:
                    notes.append(f"清理 {name}\\{dst.relative_to(dst_dir)} 失败：{exc}")

    # ---- 用户数据目录：只保证存在，绝不清空 ------------------------------
    for name in USER_DATA_DIRS:
        try:
            (run / name).mkdir(exist_ok=True)
        except OSError:
            pass
    (run / "tmp" / "logo-cache").mkdir(parents=True, exist_ok=True)

    total = updated + added + same
    notes.append(f"运行目录资源已同步（源 {total} 个文件：更新 {updated}、新增 {added}、"
                 f"清理 {pruned}、未变 {same}）—— {run}")
    return notes


def seed_contest(run: Path, source: Path | None = None) -> bool:
    """运行目录没有 contest.json 时，用源目录那份播种（保证有榜可看）。"""
    source = source or live_board_source()
    dst, src = run / "contest.json", source / "contest.json"
    if dst.is_file() or not src.is_file():
        return False
    shutil.copy2(src, dst)
    return True


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


#: 两个平台：pta（默认，需要 Cookie）/ vjudge（公开赛匿名接口，不需要 Cookie）
PLATFORMS = ("pta", "vjudge")


def normalize_platform(value: Any) -> str:
    """把界面 / 配置里的平台值规整成 ``pta`` / ``vjudge``。"""
    text = str(value or "").strip().lower()
    return "vjudge" if text == "vjudge" else "pta"


def run_platform(run: Path) -> str:
    """读运行目录 ``config.json`` 里的 ``platform``（读不到按 pta）。"""
    cfg = read_json(Path(run) / "config.json", {})
    return normalize_platform((cfg or {}).get("platform") if isinstance(cfg, dict) else None)


def needs_cookie(platform: str) -> bool:
    """这个平台取数是否需要 Cookie（只有 PTA 需要）。"""
    return normalize_platform(platform) != "vjudge"


def write_live_config(run: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    """把 <work_root>\\live\\config.json 写成 GUI 里的设置（原子替换）。"""
    path = run / "config.json"
    tmp = path.with_name("config.json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
        return True, ""
    except OSError as exc:
        return False, str(exc)


def update_live_view(run: Path, view: dict[str, Any] | None = None,
                     extra: dict[str, Any] | None = None) -> tuple[bool, str]:
    """**只**把 ``view``（颜色 / 字体 / 奖项…）合并进运行目录的 ``config.json``。

    M17：奖项表在实时榜单模式里也能改，改完必须**立刻**反映到榜单页。
    ``server.py`` 的 ``/api/config`` 与 ``presentation.load_config()`` 都是
    **每次请求重读运行目录的 config.json**，所以只要现在把 view 写进去，
    榜单页刷新（F5）就能看到，**不需要重启服务**。

    * 运行目录里还没有 ``config.json``（服务从没起过）→ 什么都不做，返回
      ``(False, "还没有运行目录 config.json")``，等 ``seed_live_config`` 在启动时补；
    * 其它键（用户数据/端口/主题）一个都不动，只合并 ``view`` 里给的键；
    * ``extra`` 用来顺手写几个顶层键（例如 ``freeze_mode``），一般不用。
    """
    path = Path(run) / "config.json"
    current = read_json(path, None)
    if not isinstance(current, dict):
        return False, f"还没有 {path}（服务没启动过）—— 启动时会写好"
    merged = dict(current)
    if view:
        old_view = current.get("view") if isinstance(current.get("view"), dict) else {}
        merged["view"] = {**old_view, **view}
    if extra:
        merged.update(extra)
    if merged == current:
        return True, "没有变化"
    ok, note = write_live_config(Path(run), merged)
    return ok, note


def seed_live_config(run: Path, payload: dict[str, Any] | None = None,
                     port: int = DEFAULT_PORT) -> tuple[bool, str]:
    """保证运行目录里有 ``config.json``——server.py 一启动就**硬读**它。

    ``sync_run_dir`` 只在**源目录有** config.json 时才拷贝；打包产物里
    ``live-board`` 是按需生成的 py+web 子集（不含 config.json），所以
    第一次在量产机上启动时运行目录里没有它，``server.py`` 会在
    ``LogoStore()`` 里 ``FileNotFoundError`` 直接退出（2026-10-11 打包
    产物实测复现）。这里在起子进程之前补一份：优先用调用方给的
    GUI 设置（颜色/奖项/端口），缺的字段用 ``default_live_config()`` 补齐。
    已有文件则**只补缺失的键**，绝不覆盖用户改过的设置。
    """
    path = run / "config.json"
    base = default_live_config(port)
    current: dict[str, Any] = {}
    if path.is_file():
        doc = read_json(path, None)
        if isinstance(doc, dict):
            current = doc
    merged = {**base, **current}
    if payload:
        for key, value in payload.items():
            if key in ("view", "theme") and isinstance(value, dict):
                merged[key] = {**(base.get(key) or {}), **(current.get(key) or {}), **value}
            elif value is not None:
                merged[key] = value
    if merged == current and path.is_file():
        return True, "已存在"
    try:
        run.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    ok, why = write_live_config(run, merged)
    return ok, ("已写入" if ok else why)


# --------------------------------------------------------------------------
# Cookie
# --------------------------------------------------------------------------

def cookie_candidates(work_root: Path) -> list[Path]:
    """按优先级列出可能的 cookie.txt（用户在 app 里指定的排最前）。"""
    return [
        Path(work_root) / "cookie.txt",
        Path(work_root) / "cookie.txt",
        run_dir(work_root) / "cookie.txt",
    ]


def read_cookie(work_root: Path) -> tuple[str, Path | None]:
    for path in cookie_candidates(work_root):
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if text:
            return text, path
    return "", None


def cookie_status(work_root: Path) -> tuple[bool, str]:
    text, path = read_cookie(work_root)
    if not text:
        return False, ("没有找到 cookie.txt —— 实时榜单必须提供有效 Cookie"
                       "（取数方式固定为「直连接口」，没有借用浏览器这条路）")
    age = ""
    try:
        age = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    except OSError:
        pass
    names = [piece.split("=", 1)[0].strip() for piece in text.split(";") if "=" in piece]
    return True, f"{path}（{len(text)} 字符，{len(names)} 个字段，更新于 {age}）"


def cookie_problem(work_root: Path) -> str | None:
    """本地判断 Cookie 能否用于取数（不发请求）。``None`` = 看起来可用。

    真正的有效性要靠一次接口请求（「环境预检」「测试 Cookie」都会真跑一次）；
    这里只挡住「没有文件」「缺必需字段」这类必然失败的情况，用于**不启动轮询**。
    """
    text, _path = read_cookie(work_root)
    if not text:
        return ("没有找到 cookie.txt —— 实时榜单必须提供有效 Cookie（直连接口）；"
                "请点「粘贴 Cookie」更新 Cookie")
    low = text.lower()
    missing = [name for name, key in (("JSESSIONID", "jsessionid"),
                                      ("PTASession", "ptasession")) if key not in low]
    if missing:
        return ("cookie.txt 里缺少 " + " 和 ".join(missing) +
                " —— 两个都必须带（只带 PTASession 会 401）；请重新「粘贴 Cookie」")
    return None


def write_cookie(work_root: Path, value: str) -> tuple[bool, str]:
    """写进 ``work_root\\cookie.txt``（可写位置见 ``_writable_dir``）。

    返回 ``(ok, 路径或原因)``：成功时第二个值是**写成功的那个路径**（界面上要显示出来），
    失败时是一句能照着处理的原因（不只是一串异常）。
    """
    targets = [Path(work_root) / "cookie.txt", Path(work_root) / "cookie.txt"]
    error = ""
    for path in targets:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value.strip() + "\n", encoding="utf-8", newline="\n")
            # 实时运行目录也放一份，保证 poller 在本机怎么跑都能读到
            try:
                run = run_dir(work_root) / "cookie.txt"
                run.write_text(value.strip() + "\n", encoding="utf-8", newline="\n")
            except OSError:
                pass
            return True, str(path)
        except OSError as exc:
            error = f"{path}：{exc}"
    return False, (f"两个位置都写不进去（{error}）。处理办法：确认**程序目录**"
                   "没被安全软件/只读属性挡住，或把程序换到有写权限的目录"
                   "（Cookie 是取数的唯一凭据，写不进去就没法取数）。")


# --------------------------------------------------------------------------
# 端口 / 进程
# --------------------------------------------------------------------------

def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def listening_pids(port: int) -> list[int]:
    """netstat 找出正在 LISTEN 这个端口的 PID（取不到就返回空表）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=15, creationflags=_no_window()).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        match = _PORT_RE.match(line)
        if match and int(match.group(1)) == int(port):
            pid = int(match.group(2))
            if pid and pid not in pids:
                pids.append(pid)
    return pids


def port_in_use(port: int) -> bool:
    return bool(listening_pids(port))


def _alive_ctypes(pid: int) -> bool | None:
    """用 OpenProcess + GetExitCodeProcess 判断进程是否存在（权威且快）。

    返回 None 表示这条路不可用（非 Windows / 权限问题），调用方再退回 tasklist。
    """
    if not pid or os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                      int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def pid_alive(pid: int) -> bool:
    """进程还在吗？先用 OpenProcess（权威），不行再退回 tasklist 文本匹配。"""
    if not pid:
        return False
    direct = _alive_ctypes(pid)
    if direct is not None:
        return direct
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15,
                             creationflags=_no_window()).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return str(int(pid)) in out


def process_name(pid: int) -> str:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15,
                             creationflags=_no_window()).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.split()[0] if out else ""


def kill_pid(pid: int, timeout: float = 5.0) -> tuple[bool, str]:
    """终止一个 PID（连同它的子进程）。返回 (是否已确认消失, 说明)。"""
    if not pid:
        return True, "没有 PID"
    if not pid_alive(pid):
        return True, "进程已经不在"
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=20, creationflags=_no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"taskkill 失败：{exc}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True, "已终止"
        time.sleep(0.2)
    return False, "任务管理器里还在（可能需要管理员权限）"


def find_python() -> str:
    """找一个**真的 Python 解释器**来跑 poller / server。

    打包后的 ``sys.executable`` 是 PTA-Assistant.exe（PyInstaller 引导程序），
    直接拿它去跑 ``xxx.py`` 只会把 exe 自己再启动一遍 —— 实测就是
    「Exception in Tkinter callback」。所以这里按顺序找：
      1) 环境变量 PTA_PYTHON（现场可以自己指定）
      2) PATH 里的 pythonw / python（先用 --version 验一下是真货）
      3) %LOCALAPPDATA%\\Programs\\Python\\Python3xx\\
      4) 注册表里登记的 Python
      5) 都没找到就退回 sys.executable（源码模式下它本来就是 python.exe）
    """
    env = os.environ.get("PTA_PYTHON")
    if env and Path(env).is_file():
        return env

    def usable(path: str) -> bool:
        try:
            proc = subprocess.run([path, "--version"], capture_output=True, text=True,
                                  timeout=15, creationflags=_no_window())
        except (OSError, subprocess.SubprocessError):
            return False
        return "python" in ((proc.stdout or "") + (proc.stderr or "")).lower()

    candidates: list[str] = []
    for name in ("pythonw.exe", "python.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        for version in ("313", "312", "311", "310", "39"):
            for name in ("pythonw.exe", "python.exe"):
                candidates.append(str(Path(local) / "Programs" / "Python" /
                                      f"Python{version}" / name))
    # 各盘根目录 + Program Files 下的 Python3x —— 盘符/目录全从环境变量与盘符列表推出来，
    # 不写死任何本机路径（注册表那一段才是主路径，这里只是兜底）。
    py_roots: list[str] = [os.environ.get("SystemDrive", "C:") + "\\"]
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            py_roots.append(value + "\\")
    for drive in "CDEFG":
        py_roots.append(drive + ":\\")
    for root in dict.fromkeys(py_roots):
        try:
            for entry in sorted(Path(root).glob("Python3*")):
                for name in ("pythonw.exe", "python.exe"):
                    candidates.append(str(entry / name))
        except OSError:
            pass
    if os.name == "nt":
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, r"SOFTWARE\Python\PythonCore"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Python\PythonCore")):
                try:
                    with winreg.OpenKey(hive, key) as root_key:
                        for i in range(winreg.QueryInfoKey(root_key)[0]):
                            version = winreg.EnumKey(root_key, i)
                            with winreg.OpenKey(root_key, version + r"\InstallPath") as ip:
                                base = winreg.QueryValue(ip, None)
                            for name in ("pythonw.exe", "python.exe"):
                                candidates.append(str(Path(base) / name))
                except OSError:
                    continue
        except ImportError:
            pass

    for cand in candidates:
        if Path(cand).is_file() and usable(cand):
            return cand
    return sys.executable


def is_real_python() -> tuple[bool, str]:
    """实时服务能不能在这个环境里跑（需要真的 Python 解释器）。"""
    exe = find_python()
    same = str(Path(exe).resolve()).lower() == str(Path(sys.executable).resolve()).lower()
    if is_frozen() and same:
        return False, (f"找不到 Python 解释器（sys.executable 是打包后的 exe：{exe}）。"
                       "实时榜单模式需要装 Python 3.9+，或设置环境变量 PTA_PYTHON "
                       "指向 pythonw.exe。赛后滚榜模式不受影响。")
    return True, exe


def pillow_available() -> tuple[bool, str]:
    try:
        import PIL
        from PIL import Image  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"Pillow 不可用：{exc}"
    return True, f"Pillow {getattr(PIL, '__version__', '?')}（校徽裁圆可用）"


# --------------------------------------------------------------------------
# 子进程管理
# --------------------------------------------------------------------------

class Child:
    """一个被 GUI 管着的子进程（poller 或 server）。"""

    def __init__(self, name: str, argv: list[str], cwd: Path, log_path: Path,
                 env: dict | None = None) -> None:
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.log_path = log_path
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self._log = None

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "a", encoding="utf-8", buffering=1)
        self._log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"启动 {self.name}：{' '.join(self.argv)}\n")
        self._log.flush()
        self.proc = subprocess.Popen(
            self.argv, cwd=str(self.cwd), env=self.env,
            stdout=self._log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=_no_window(), close_fds=True)
        self.started_at = time.time()
        # M18：登记到 <运行目录>\child-pids.json —— 应用被强杀时子进程会变孤儿，
        # 下次启动靠这份登记把它们认出来收掉（命令行判定，绝不动别人的进程）。
        if child_registry is not None:
            try:
                child_registry.record(self.cwd, self.proc.pid, self.name, self.argv,
                                      marker=[str(self.cwd)])
            except Exception:  # noqa: BLE001
                pass

    @property
    def pid(self) -> int:
        return int(self.proc.pid) if self.proc else 0

    def alive(self) -> bool:
        """还活着吗？

        以 ``poll() is None`` 为准（这是权威的）；``tasklist`` 只当作交叉验证，
        它万一抽风（权限/语言/被安全软件拦）不能把「正在运行」判成「已退出」。
        """
        if not self.proc or self.proc.poll() is not None:
            return False
        return True

    def _close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.flush()
                self._log.close()
            except OSError:
                pass
            self._log = None

    def stop(self, timeout: float = 6.0) -> tuple[bool, str]:
        """先温和 terminate，再强制回收，最后按 PID 复核。"""
        if not self.proc:
            self._close_log()
            return True, f"{self.name} 没在运行"
        pid = self.pid
        was_running = self.proc.poll() is None
        note_extra = ""
        try:
            if was_running:
                self.proc.terminate()
        except OSError:
            pass
        deadline = time.time() + timeout
        while was_running and time.time() < deadline:
            if self.proc.poll() is not None:
                break
            time.sleep(0.15)
        still = pid_alive(pid)
        forced = False
        if still:
            kill_pid(pid)
            forced = True
        gone = not pid_alive(pid)
        self._close_log()
        if gone:
            # M18：正常收掉了，从登记表里划掉（启动时的残留清理就不必再管它）
            if child_registry is not None:
                try:
                    child_registry.forget(self.cwd, pid)
                except Exception:  # noqa: BLE001
                    pass
        if gone:
            how = "已终止" if (was_running or forced) else "本来就没在运行"
        else:
            how = "⚠ 仍在运行"
            note_extra = "（可能需要管理员权限）"
        return gone, f"{self.name}(PID {pid}) {how}{note_extra}"


def kill_tree(pid: int) -> tuple[bool, str]:
    return kill_pid(pid)


def child_env(run: Path | None = None) -> dict:
    """子进程环境变量。

    live-board 的 ``presentation.py`` 会 ``from board_backend import ...``，
    而它只把 ``pta-resolver`` 和 ``pta-resolver\\app`` 塞进 sys.path ——
    源码模式下能命中，打包后（board_backend 变成 exe 里的模块）就找不到了。
    这里用 PYTHONPATH 把两边都兜住：运行目录、pta-resolver、pta-resolver\\app
    （打包后是 exe 目录和 _internal）。
    """
    env = os.environ.copy()
    paths = [str(run)] if run else []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        paths.append(str(Path(base)))
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        # app\ 与 tools\ 都在 _internal 下：board_backend.py 在 _internal\app，
        # pta_common.py / pta_to_resolver_xml.py 在 _internal 根。
        paths += [str(exe_dir), str(exe_dir / "_internal"),
                  str(exe_dir / "_internal" / "app"),
                  str(exe_dir / "_internal" / "tools")]
    else:
        resolver = Path(__file__).resolve().parent.parent
        # core\ 是公开仓库里放 pta_common / pta_fetch / pta_to_resolver_xml /
        # vjudge_fetch 的地方；漏了它，源码模式点「启动实时榜」时 poller 直接崩。
        paths += [str(resolver), str(resolver / "app"), str(resolver / "tools"),
                  str(resolver / "core")]
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class LiveService:
    """实时服务：同时管 poller + server 两个子进程。"""

    def __init__(self, work_root: Path, python: str | None = None) -> None:
        self.work_root = Path(work_root)
        self.run = run_dir(self.work_root)
        self.python = python or find_python()
        self.poller: Child | None = None
        self.server: Child | None = None
        self.events: list[str] = []

    # -- 运行目录 ----------------------------------------------------------

    def sync(self) -> list[str]:
        return sync_run_dir(self.run)

    @property
    def state_path(self) -> Path:
        return self.run / "state.json"

    @property
    def contest_path(self) -> Path:
        return self.run / "contest.json"

    @property
    def poller_log(self) -> Path:
        return self.run / "logs" / "poller.log"

    @property
    def server_log(self) -> Path:
        return self.run / "logs" / "server.out.log"

    # -- 状态 --------------------------------------------------------------

    def running(self) -> bool:
        return bool(self.poller and self.poller.alive()) and bool(
            self.server and self.server.alive())

    def any_running(self) -> bool:
        return bool((self.poller and self.poller.alive())
                    or (self.server and self.server.alive()))

    def pids(self) -> dict[str, int]:
        return {
            "poller": self.poller.pid if self.poller and self.poller.alive() else 0,
            "server": self.server.pid if self.server and self.server.alive() else 0,
        }

    # -- 启停 --------------------------------------------------------------

    def start(self, port: int, psid: str, host: str = "127.0.0.1",
              interval: float = 5.0, full_interval: float = 120.0,
              live_config: dict[str, Any] | None = None) -> dict[str, Any]:
        """启动 poller + server。调用方负责先处理端口占用。"""
        sync_notes = self.sync()
        if not self.contest_path.is_file():
            seed_contest(self.run)
        # server.py 启动即读 live\config.json，缺了会直接崩（打包首次运行必现）
        cfg_ok, cfg_note = seed_live_config(
            self.run, live_config, port=int(port))
        platform = run_platform(self.run)
        cookie, cookie_path = read_cookie(self.work_root)
        # VJudge 公开赛不需要 Cookie —— 别拿 PTA 的判据拦住它。
        problem = cookie_problem(self.work_root) if needs_cookie(platform) else ""
        env = child_env(self.run)
        # Cookie 缺失/明显不可用：**不启动轮询**（绝不每 20 秒撞一次 401）。
        # server 照常起来给界面/日志，/health 会带 cookie_error 说明原因。
        if problem:
            self.poller = None
        else:
            self.poller = Child(
                "poller",
                [self.python, "-u", str(self.run / "poller.py"),
                 "--problem-set", str(psid), "--interval", str(float(interval)),
                 "--full-interval", str(float(full_interval)),
                 "--cookie-file", str(cookie_path or (self.work_root / "cookie.txt"))],
                self.run, self.poller_log, env)
            self.poller.start()
        # server.py 还需要 live\contest.json 存在才能起来（presentation.py 会 stat 它）。
        # 全新量产的机器上运行目录里没有它，poller 要抓完一轮才写出来，所以这里
        # **等一下**（最多 45 s）：等它出现再拉 server，而不是让 server 崩掉。
        # 没等到也照拉 server —— 让用户看到界面和日志，而不是静默失败。
        waited = 0.0
        while not self.contest_path.is_file() and waited < 45.0:
            time.sleep(0.5)
            waited += 0.5
        self.server = Child(
            "server",
            [self.python, "-u", str(self.run / "server.py"),
             "--port", str(int(port)), "--host", host,
             "--state", str(self.state_path), "--contest", str(self.contest_path)],
            self.run, self.server_log, env)
        self.server.start()
        return {"ok": True, "pids": self.pids(), "run_dir": str(self.run),
                "live_config": cfg_note, "live_config_ok": cfg_ok,
                "contest_ready": self.contest_path.is_file(),
                "contest_wait": round(waited, 1),
                "poller_log": str(self.poller_log), "server_log": str(self.server_log),
                "has_cookie": bool(cookie), "cookie_error": problem or "",
                "platform": platform,
                # M16b：把「运行目录资源同步」的逐条日志交回界面（以前这里被丢掉了，
                # 用户升级后运行目录还是旧的却看不到任何提示）
                "sync_notes": sync_notes}

    def repoll_once(self, port: int, psid: str, interval: float = 5.0,
                    full_interval: float = 120.0) -> dict[str, Any]:
        """「拉取/刷新数据」：重启 poller 并让它立刻跑一轮（server 不动）。"""
        self.sync()
        if self.poller:
            self.poller.stop()
        platform = run_platform(self.run)
        problem = cookie_problem(self.work_root) if needs_cookie(platform) else ""
        if problem:
            self.poller = None
            return {"ok": False, "pids": self.pids(), "cookie_error": problem}
        cookie, cookie_path = read_cookie(self.work_root)
        self.poller = Child(
            "poller",
            [self.python, "-u", str(self.run / "poller.py"),
             "--problem-set", str(psid), "--interval", str(float(interval)),
             "--full-interval", str(float(full_interval)),
             "--cookie-file", str(cookie_path or (self.work_root / "cookie.txt"))],
            self.run, self.poller_log, child_env(self.run))
        self.poller.start()
        return {"ok": True, "pids": self.pids()}

    def stop(self) -> dict[str, Any]:
        """停止两个子进程 + 回收端口，返回给人看的证据。"""
        before = self.pids()
        notes: list[str] = []
        for child in (self.poller, self.server):
            if child is None:
                continue
            ok, note = child.stop()
            notes.append(note + ("" if ok else "  ⚠ 没能确认退出"))
        self.poller = None
        self.server = None
        return {"ok": True, "before": before, "notes": notes}

    def verify_stopped(self, port: int) -> dict[str, Any]:
        """复核：端口没人监听、我们起过的 PID 都不在了。"""
        pids = listening_pids(port)
        return {"port": port, "still_listening": pids,
                "clean": not pids}


# --------------------------------------------------------------------------
# HTTP 探针（只读，不抢焦点）
# --------------------------------------------------------------------------

def http_json(url: str, timeout: float = 3.0) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def health(port: int, host: str = "127.0.0.1", timeout: float = 3.0) -> dict[str, Any] | None:
    data = http_json(f"http://{host}:{int(port)}/health", timeout)
    return data if isinstance(data, dict) else None


def board(port: int, host: str = "127.0.0.1", timeout: float = 8.0,
          freeze: str | None = None) -> dict[str, Any] | None:
    url = f"http://{host}:{int(port)}/api/board"
    if freeze:
        url += f"?freeze={freeze}"
    data = http_json(url, timeout)
    return data if isinstance(data, dict) else None


def wait_health(port: int, timeout: float = 25.0,
                host: str = "127.0.0.1") -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = health(port, host)
        if data:
            return data
        time.sleep(0.4)
    return None


def wait_port_free(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_in_use(port):
            return True
        time.sleep(0.25)
    return not port_in_use(port)


# --------------------------------------------------------------------------
# 轮询日志解析（状态面板要用：通道 / 队伍数 / 提交数 / 耗时 / 次数）
# --------------------------------------------------------------------------

_POLL_RE = re.compile(
    r"通道=(?P<channel>\w+)\s+新增=(?P<new>\d+)\s+总提交=(?P<subs>\d+)\s+"
    r"队伍=(?P<teams>\d+)\s+榜单变化=(?P<changed>\w+)")
_START_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+INFO\s+启动：")


def tail(path: Path, lines: int = 40, max_bytes: int = 200_000) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def poller_stats(path: Path) -> dict[str, Any]:
    """从 poller.log 里读出最近一轮的关键数字 + 累计轮次。"""
    out: dict[str, Any] = {"cycles": 0, "channel": "", "teams": 0, "submissions": 0,
                           "new": 0, "changed": "", "last_line": "", "started_at": ""}
    for line in tail(path, lines=4000):
        match = _POLL_RE.search(line)
        if match:
            out["cycles"] += 1
            out["channel"] = match.group("channel")
            out["new"] = int(match.group("new"))
            out["teams"] = int(match.group("teams"))
            out["submissions"] = int(match.group("subs"))
            out["changed"] = match.group("changed")
            out["last_line"] = line.strip()
        match_start = _START_RE.search(line)
        if match_start:
            out["started_at"] = match_start.group("ts")
            out["cycles"] = 0
    return out


def newest_submission_iso(state: dict[str, Any]) -> str:
    """从 contest.json 里取最近一次提交的时间（用于算「已过去多久」）。"""
    latest = 0.0
    for sub in (state or {}).get("submissions") or []:
        try:
            value = float(sub.get("rel_time_seconds") or 0)
        except (TypeError, ValueError):
            continue
        latest = max(latest, value)
    if latest <= 0:
        return ""
    return f"{int(latest // 3600)}:{int(latest % 3600 // 60):02d}:{int(latest % 60):02d}"


def human_age(seconds: float) -> str:
    if seconds < 0:
        return "刚刚"
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{int(seconds // 3600)} 小时前"


# --------------------------------------------------------------------------
# 直播 URL
# --------------------------------------------------------------------------

VIEW_KEYS = (("overlay", int), ("top", int), ("rows", int), ("scroll", int),
             ("scroll_speed", int), ("scroll_pause", int), ("anim", int),
             ("scale", float), ("font", str), ("title", str), ("freeze", str))


def board_url(port: int, qs: dict[str, Any], host: str = "127.0.0.1") -> str:
    """拼榜单页 URL（只带非默认值，方便肉眼核对）。"""
    from urllib.parse import urlencode
    parts = {k: v for k, v in (qs or {}).items() if v not in (None, "", 0, 0.0, "0")}
    query = urlencode(parts)
    base = f"http://{host}:{int(port)}/board"
    return f"{base}?{query}" if query else base


def _app_root() -> Path:
    """程序所在目录（打包后是 exe 同目录，源码模式是项目根）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_font() -> str:
    """随程序附带的中文字体（tools\\fonts）；找不到返回空串，由调用方回退。"""
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(_app_root())
    roots.append(Path(__file__).resolve().parent.parent)
    for root in roots:
        for name in ("MicrosoftYaHei.ttc", "simhei.ttf"):
            cand = root / "tools" / "fonts" / name
            if cand.is_file():
                return str(cand)
    return ""


def default_live_config(port: int = DEFAULT_PORT) -> dict[str, Any]:
    return {
        "host": "127.0.0.1",
        "port": int(port),
        "poll_interval": 5,
        "full_check_interval": 120,
        "channel": "auto",
        #: 取数平台（poller 每轮重读本文件）：pta / vjudge
        "platform": "pta",
        #: 当前比赛 ID（VJudge = 比赛 ID；PTA = 题目集 ID）
        "contest_id": "",
        "title": "",
        "freeze_mode": "operator",
        "logos_dir": str(_app_root() / "校徽"),
        "font_path": bundled_font(),
        "view": {
            "overlay": 0, "top": 0, "rows": 13, "scale": 1, "font": "yahei",
            "anim": 1, "scroll": 1, "scroll_speed": 24, "scroll_pause": 2,
            "problem_colors": [
                "#FFFFFF", "#000000", "#7FE3E8", "#7BE84A", "#2B3CE8", "#FFF045",
                "#B9BFC6", "#B32438", "#FF9A2E", "#7A7A00", "#E94BD8", "#A9742B",
                "#00A5A5"],
            "medal_colors": ["#D4AF37", "#A8B0B8", "#CD7F32"],
            "awards": [],
        },
        "theme": {
            "accent": "#43d17a", "accent2": "#52a7ff", "danger": "#f06b73",
            "panel": "#172133", "panel_alt": "#1d2a40", "muted": "#9aaaca",
            "ac": "#126747", "wrong": "#662d39", "pending": "#253449",
            "locked": "#3a4153", "first_blood": "#0B5B2E",
        },
    }


def free_port(preferred: int = DEFAULT_PORT) -> int:
    """挑一个没被占用的端口（别人占着 8765 也绝不去动）。"""
    for port in (preferred, DEFAULT_ALT_PORT, 8793, 8794):
        if not port_in_use(port):
            return port
    return preferred
