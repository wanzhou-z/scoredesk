# -*- coding: utf-8 -*-
"""board_backend.py —— GUI 与既有命令行后端之间的胶水层。

本模块**不重新实现任何业务逻辑**，只做三件事：

1. 把 ``pta_fetch`` / ``pta_to_resolver_xml`` / ``tools\\esc_quit.py``
   这几个已经验证可用的现有模块，按正确顺序调起来；
2. 按 ``run-resolver.bat`` 注释里记录的坑，拼出正确的 java 命令行
   与环境变量（``ICPC_FONT`` / ``--singleStep 2`` / 先 cd 进 app 目录）；
3. 把它们的 stdout/stderr 引流到 GUI 的日志区，并管理 Resolver 与
   Esc 守护这两个子进程的生命周期。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:                     # M18：子进程登记/清理（同目录模块；PyInstaller 靠这行静态收集）
    import child_registry
except Exception:        # noqa: BLE001 - 缺了也不能让应用起不来
    child_registry = None  # type: ignore[assignment]
from typing import Any

import board_config as cfgmod
import board_fonts as fontsmod
import star_awards

CREATE_NO_WINDOW = 0x08000000
RESOLVER_MAIN_CLASS = "org.icpc.tools.resolver.Resolver"

#: 奖牌名次的默认值（金 / 银 / 铜 各自到第几名）。
#: **不能全是 0** —— 全 0 时 XML 里没有 <award>，Resolver 会走
#: AwardUtil.createDefaultAwards 并抛 NPE 崩掉，详见 run_package 的 docstring。
DEFAULT_MEDALS = (1, 2, 3)


# --------------------------------------------------------------------------
# 系统自带的 Windows 位置（**不写死盘符**：从环境变量推出来，任何机器都对）
# --------------------------------------------------------------------------

def _windows_dir() -> Path:
    """Windows 安装目录：``%SystemRoot%``（没有就用 ``%windir%``）。"""
    for key in ("SystemRoot", "windir"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return Path(value)
    return Path(os.environ.get("SystemDrive", "C:") + "/Windows")


def _system_fonts_dir() -> Path:
    """系统字体目录（``%SystemRoot%\\Fonts``）。"""
    return _windows_dir() / "Fonts"


def _program_files_dirs() -> list[Path]:
    """``Program Files`` 那一批目录 —— 全从环境变量取，不写死盘符。"""
    out: list[Path] = []
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            path = Path(value)
            if path not in out:
                out.append(path)
    return out


# --------------------------------------------------------------------------
# Windows 窗口工具（按进程名找 Resolver 的窗口）
# --------------------------------------------------------------------------

WM_CLOSE = 0x0010

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as _w

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetWindowThreadProcessId.argtypes = [_w.HWND, ctypes.POINTER(_w.DWORD)]
    _user32.IsWindowVisible.argtypes = [_w.HWND]
    _user32.PostMessageW.argtypes = [_w.HWND, _w.UINT, _w.WPARAM, _w.LPARAM]
    _kernel32.OpenProcess.argtypes = [_w.DWORD, _w.BOOL, _w.DWORD]
    _kernel32.OpenProcess.restype = _w.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        _w.HANDLE, _w.DWORD, _w.LPWSTR, ctypes.POINTER(_w.DWORD)
    ]
    _kernel32.CloseHandle.argtypes = [_w.HANDLE]
    _kernel32.WaitForSingleObject.argtypes = [_w.HANDLE, _w.DWORD]
    _kernel32.WaitForSingleObject.restype = _w.DWORD
    _EnumProc = ctypes.WINFUNCTYPE(_w.BOOL, _w.HWND, _w.LPARAM)
else:  # pragma: no cover - 本项目只在 Windows 上用
    _user32 = _kernel32 = _EnumProc = None


def _exe_of_pid(pid: int) -> str:
    if sys.platform != "win32":
        return ""
    handle = _kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = _w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def find_java_windows(pid_filter: int | None = None) -> list[tuple[int, int]]:
    """找出所有「属于 java.exe 的可见窗口」，返回 [(hwnd, pid), ...]。

    判别方式与 ``tools\\esc_quit.py`` 保持一致（Resolver 是 SWT 程序，
    标题画在客户区，GetWindowText 取不到，只能按进程可执行文件名判定）。
    """
    if sys.platform != "win32":
        return []
    found: list[tuple[int, int]] = []

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = _w.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        if pid_filter is not None and pid.value != pid_filter:
            return True
        if _exe_of_pid(pid.value).lower().endswith("java.exe"):
            found.append((hwnd, pid.value))
        return True

    _user32.EnumWindows(_EnumProc(_cb), 0)
    return found


def post_close(hwnd: int) -> None:
    if sys.platform == "win32":
        _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


# --------------------------------------------------------------------------
# 日志引流
# --------------------------------------------------------------------------

class LogSink:
    """把后端模块 print 出来的文字实时送进 GUI 日志区（线程安全）。

    同时实现 ``reconfigure``，因为 ``pta_fetch.main`` 在 Windows 上会调用
    ``sys.stdout.reconfigure(encoding="utf-8")``。
    """

    def __init__(self, q: "queue.Queue[str]") -> None:
        self.q = q
        self.buffer: list[str] = []

    def write(self, text: str) -> int:
        if not text:
            return 0
        self.buffer.append(text)
        self.q.put(text)
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **_kwargs) -> None:
        return None

    def text(self) -> str:
        return "".join(self.buffer)


class LogRedirect:
    """临时把进程级 sys.stdout/sys.stderr 指向日志区。"""

    def __init__(self, sink: LogSink) -> None:
        self.sink = sink
        self._saved: tuple[Any, Any] | None = None

    def __enter__(self) -> "LogSink":
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout = self.sink          # type: ignore[assignment]
        sys.stderr = self.sink          # type: ignore[assignment]
        return self.sink

    def __exit__(self, *_exc) -> bool:
        if self._saved is not None:
            sys.stdout, sys.stderr = self._saved
        return False





# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def resolver_app_dir(root: Path) -> Path:
    return root / "tools" / "resolver" / "app"


def logs_dir() -> Path:
    folder = cfgmod.work_root() / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return folder


def tmp_dir() -> Path:
    """临时目录（**不含任何写死的本机路径**，在哪台机器上都能跑）。

    优先用 ``<工作目录>\\tmp``（程序自己目录下，发布版到哪都能用）；
    用户可用环境变量 ``PTA_BOARD_TMP`` 指定一个自己想用的位置（可选）；
    都不行就退到系统临时目录 ``tempfile.gettempdir()``。
    """
    candidates: list[Path] = []
    override = str(os.environ.get("PTA_BOARD_TMP") or "").strip()
    if override:
        candidates.append(Path(override).expanduser() / "pta-board")
    candidates.append(cfgmod.work_root() / "tmp")
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write-test"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            return cand
        except OSError:
            continue
    import tempfile
    fallback = Path(tempfile.gettempdir()) / "pta-board"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


# --------------------------------------------------------------------------
# 载入既有后端模块
# --------------------------------------------------------------------------

_BACKEND: tuple[Any, Any, Any] | None = None


def load_backend(root: Path) -> tuple[Any, Any, Any]:
    """导入 pta_common / pta_fetch / pta_to_resolver_xml。返回 (fetch, xml, common)。"""
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    for entry in (str(root), str(root / "tools")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import pta_common as common          # noqa: E402
    import pta_fetch                     # noqa: E402
    import pta_to_resolver_xml as xmlmod  # noqa: E402

    _BACKEND = (pta_fetch, xmlmod, common)
    return _BACKEND


_BACKEND_VJ: Any = None


def load_vjudge_backend(root: Path) -> Any:
    """导入 ``vjudge_fetch``（VJudge 平台用）。返回模块对象。

    与 :func:`load_backend` 分开：PTA 的 Cookie / pta_common 跟 VJudge 无关，
    VJudge 版抓取器是**自包含**的（只依赖 requests），公开赛不需要 Cookie。
    """
    global _BACKEND_VJ
    if _BACKEND_VJ is not None:
        return _BACKEND_VJ
    root = Path(root)
    for entry in (str(root), str(root / "tools")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import vjudge_fetch                     # noqa: E402
    _BACKEND_VJ = vjudge_fetch
    return _BACKEND_VJ


def normalize_vjudge_contest(value: Any) -> str:
    """把用户填的 VJudge 比赛 ID 归一成纯数字：

    接受 ``862258`` / ``https://vjudge.net/contest/862258#rank`` / ``vjudge.net/contest/862258``。
    认不出来返回空串（调用方给中文提示）。**不 import 网络模块**，纯字符串处理。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    # URL：找 /contest/<数字>
    try:
        from urllib.parse import urlsplit
        raw = urlsplit(text if "://" in text else "https://" + text)
        parts = [p for p in raw.path.split("/") if p]
        for i, part in enumerate(parts):
            if part == "contest" and i + 1 < len(parts) and parts[i + 1].isdigit():
                return parts[i + 1]
        for part in reversed(parts):
            if part.isdigit():
                return part
    except Exception:  # noqa: BLE001 - 解析不了就走下面的兜底
        pass
    digits = re.findall(r"\d+", text)
    return digits[-1] if digits else ""


def cookie_available(root: Path) -> tuple[bool, str]:
    """不加载 Cookie，只判断「有没有」。"""
    if (os.environ.get("PTA_COOKIE") or os.environ.get("PTA_COOKIES") or "").strip():
        return True, "环境变量 PTA_COOKIE"
    for name in ("cookie.txt", "pta_cookie.txt", "cookies.txt", "cookie.json", "pta_cookie.json"):
        for base in (root, Path.cwd()):
            path = base / name
            try:
                if path.is_file():
                    return True, str(path)
            except OSError:
                continue
    return False, ""


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------

def run_fetch(
    root: Path,
    psid: str,
    source_mode: str,
    out_path: Path,
    freeze_minutes: int,
    penalty_minutes: int,
    sink: LogSink,
    platform: str = "pta",
) -> dict[str, Any]:
    """抓取比赛数据，按 ``platform`` 分派：

    * ``pta``（默认）= ``python pta_fetch.py fetch ...``（需要有效 Cookie）；
    * ``vjudge``     = ``python vjudge_fetch.py fetch --contest ...``
      （匿名公开接口，拿不到封榜 / 题名，详见 vjudge_fetch.py）。

    返回 {'ok', 'message', 'via_used', 'title', 'stats'}。
    """
    platform = str(platform or "pta").strip().lower()
    if platform == "vjudge":
        return _run_fetch_vjudge(root, psid, out_path, freeze_minutes, penalty_minutes, sink)

    pta_fetch, _xmlmod, common = load_backend(root)

    psid = str(psid).strip()
    if not psid.isdigit():
        return {"ok": False, "message": f"题目集 ID 必须是纯数字，你填的是：{psid!r}", "via_used": ""}

    freeze_seconds = max(0, int(freeze_minutes)) * 60
    base_argv = [
        "fetch",
        "--problem-set", psid,
        "--out", str(out_path),
        "--freeze", str(freeze_seconds),
        "--penalty", str(max(0, int(penalty_minutes))),
        # M38（用户 2026-10-18）：每次「拉取数据」都重新读一遍队伍名录。
        #   以前不带这个开关，pta_fetch 会**复用缓存里的队伍**（第一次抓到的 17 支），
        #   后来进群的队（杨晨、郑奥）永远进不来，表现为「拉取的数据不全」。
        "--refresh-meta",
    ]

    with LogRedirect(sink):
        print("=" * 62)
        print("  开始抓取 PTA 比赛数据")
        print("=" * 62)
        print(f"  题目集 ID : {psid}")
        print(f"  输出文件  : {out_path}")
        print(f"  封榜时长  : {freeze_minutes} 分钟     罚时: {penalty_minutes} 分钟")

        # 取数通道固定：直连接口（需要 Cookie）。2026-10-13 删除「借用浏览器」回落 ——
        # 没有 Cookie 就直接说清楚，绝不偷偷去借浏览器（会在用户浏览器里产生下载）。
        via = "http"
        has_cookie, where = cookie_available(root)
        if not has_cookie:
            print("[取数] 没有找到 cookie.txt —— 直连接口需要有效 Cookie，取数不会开始。")
            result = {"ok": False, "via_used": via,
                      "message": "没有可用的 cookie.txt：实时榜单/滚榜都固定用「直连接口」，"
                                 "必须提供有效 Cookie（程序不会去借浏览器）。\n"
                                 "怎么办：① 浏览器登录 pintia.cn 并打开这场比赛 → F12 → Network → "
                                 "右键请求 → Copy as cURL (bash)；② 回到「数据源」页把整段粘进"
                                 "「粘贴 Cookie」，或手工粘 JSESSIONID=…; PTASession=… 一行"
                                 "（两个都必须带，只带 PTASession 会 401）。",
                      "title": "", "stats": {}}
            print(result["message"])
            return result
        print(f"[取数] 使用直连接口（http），Cookie 来源：{where}")

        argv = base_argv + ["--via", via]
        code = 1
        try:
            code = pta_fetch.main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
            if code == 0:
                print("[i] 抓取流程已结束。")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[错误] 抓取过程中出现未预期的问题：{type(exc).__name__}: {exc}")

    result: dict[str, Any] = {"ok": False, "via_used": via, "message": "", "title": "", "stats": {}}

    if code != 0:
        result["message"] = _fetch_failure_hint(code, via, sink.text())
        print(result["message"])
        return result

    if not out_path.is_file():
        result["message"] = f"抓取报告成功，但没有生成 {out_path}，请把上面的日志发出来看看。"
        print(result["message"])
        return result

    try:
        contest = common.load_contest(out_path)
        stats = contest.get("stats") or common.contest_stats(contest)
        meta = contest.get("contest") or {}
        result["title"] = meta.get("title") or ""
        result["stats"] = stats
    except Exception as exc:  # noqa: BLE001
        result["message"] = f"抓取完成，但读回 {out_path} 时出错：{exc}"
        print(result["message"])
        return result

    result["ok"] = True
    result["message"] = (
        f"抓取成功：《{result['title']}》 "
        f"{stats.get('problem_count', 0)} 题 / "
        f"{stats.get('team_count', 0)} 队 / "
        f"{stats.get('submission_count', 0)} 条提交"
    )
    return result


def _fetch_failure_hint(code: int, via: str, log_text: str) -> str:
    base = f"\n[失败] 抓取没有成功（退出码 {code}）。"
    if code == 3:
        return base + ("\n  Cookie 失效了，请重新从浏览器复制一份放进 cookie.txt"
                       "（或「粘贴 Cookie」）。取数只有「直连接口」这一条路，"
                       "不会自动回落到别的通道。")
    if code == 4:
        return base + "\n  权限不足：请确认用的是【教师/管理员】账号的 Cookie。"
    if code == 6:
        return base + "\n  网络不通：检查代理/校园网，或稍后重试。"
    return base + "\n  详细原因见上面的日志；常见原因是 Cookie 过期或 PTA 接口有变动。"


def _run_fetch_vjudge(
    root: Path,
    contest_id: str,
    out_path: Path,
    freeze_minutes: int,
    penalty_minutes: int,
    sink: "LogSink",
) -> dict[str, Any]:
    """VJudge 平台的一次全量抓取（公开接口，不需要 Cookie）。

    输出与 PTA 版**完全同构**的 contest.json，下游不用知道来源是 VJudge。
    """
    result: dict[str, Any] = {"ok": False, "via_used": "vjudge", "message": "",
                              "title": "", "stats": {}}
    cid = normalize_vjudge_contest(contest_id)
    if not cid:
        result["message"] = (f"VJudge 比赛 ID 认不出来：{str(contest_id)!r}\n"
                             "  请填 862258 这种数字，或直接粘 "
                             "https://vjudge.net/contest/862258#rank")
        return result
    try:
        vj = load_vjudge_backend(root)
    except Exception as exc:  # noqa: BLE001
        result["message"] = f"找不到 vjudge_fetch.py（VJudge 版抓取器）：{exc}"
        return result

    code = 1
    with LogRedirect(sink):
        print("=" * 62)
        print("  开始抓取 VJudge 比赛数据")
        print("=" * 62)
        print(f"  比赛 ID  : {cid}")
        print(f"  输出文件 : {out_path}")
        print(f"  罚时     : {max(0, int(penalty_minutes))} 分钟")
        print("  说明     : VJudge 走匿名公开接口，不需要 Cookie；")
        print("             拿不到封榜设置 / 题目标题（题目用字母 A/B/… 代替）。")
        argv = ["fetch", "--contest", cid, "--out", str(out_path),
                "--penalty", str(max(0, int(penalty_minutes)))]
        try:
            code = int(vj.main(argv) or 0)
        except SystemExit as exc:
            code = int(exc.code or 0)
            if code == 0:
                print("[i] 抓取流程已结束。")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[错误] 抓取过程中出现未预期的问题：{type(exc).__name__}: {exc}")

    if code != 0:
        if code == 3:
            result["message"] = ("[失败] VJudge 上读不到这场比赛（HTTP 4xx）："
                                 "检查比赛 ID；私有比赛需要 Cookie（本版本未做密码赛登录）。")
        elif code == 6:
            result["message"] = "[失败] 网络不通 / VJudge 超时：检查网络或代理，稍后重试。"
        elif code == 5:
            result["message"] = "[失败] VJudge 返回内容不是预期 JSON（接口可能变更）。"
        else:
            result["message"] = f"[失败] 抓取没有成功（退出码 {code}），详情见上面的日志。"
        print(result["message"])
        return result

    if not Path(out_path).is_file():
        result["message"] = f"抓取报告成功，但没有生成 {out_path}，请看上面的日志。"
        return result

    # 用户 2026-10-27：VJudge 接口不返回封榜设置 —— 封榜时长以界面上填的为准，
    #   抓完写回 contest.json（滚榜 / 外榜都从这里读）。
    try:
        doc = json.loads(Path(out_path).read_text(encoding="utf-8"))
        cmeta = doc.get("contest") if isinstance(doc.get("contest"), dict) else {}
        freeze_sec = max(0, int(freeze_minutes)) * 60
        if freeze_sec != int(cmeta.get("freeze_seconds") or 0):
            cmeta["freeze_seconds"] = freeze_sec
            hh, mm = divmod(freeze_sec // 60, 60)
            cmeta["freeze_length"] = f"{hh}:{mm:02d}:00"
            doc["contest"] = cmeta
            warns = doc.get("warnings")
            if not isinstance(warns, list):
                warns = []
            warns.append(f"封榜时长按界面设置写入：{int(freeze_minutes)} 分钟"
                         "（VJudge 接口本身不提供该设置）。")
            doc["warnings"] = warns
            tmp = Path(str(out_path) + ".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            tmp.replace(out_path)
        print(f"  封榜时长 : {int(freeze_minutes)} 分钟（已写入 contest.json）")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 封榜时长写回失败（不影响榜单）：{type(exc).__name__}: {exc}")
    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            contest = json.load(fh)
        stats = contest.get("stats") or {}
        meta = contest.get("contest") or {}
        result["title"] = meta.get("title") or ""
        result["stats"] = stats
    except Exception as exc:  # noqa: BLE001
        result["message"] = f"抓取完成，但读回 {out_path} 时出错：{exc}"
        return result

    result["ok"] = True
    result["message"] = (
        f"抓取成功（VJudge）：《{result['title']}》 "
        f"{result['stats'].get('problem_count', 0)} 题 / "
        f"{result['stats'].get('team_count', 0)} 队 / "
        f"{result['stats'].get('submission_count', 0)} 条提交"
    )
    return result


# --------------------------------------------------------------------------
# 生成比赛包
# --------------------------------------------------------------------------

def parse_medals(text: Any) -> tuple[int, int, int]:
    """把 "1 2 3" / "1,2,3" 解析成 (金, 银, 铜) 三个名次。"""
    raw = str(text or "").replace(",", " ").replace("(", " ").replace(")", " ")
    parts = raw.split()
    if len(parts) != 3:
        return DEFAULT_MEDALS
    out: list[int] = []
    for item in parts:
        try:
            out.append(max(0, int(float(item))))
        except (TypeError, ValueError):
            return DEFAULT_MEDALS
    return out[0], out[1], out[2]


# --------------------------------------------------------------------------
# 队伍照片
# --------------------------------------------------------------------------

PHOTO_EXTS = (".jpg", ".jpeg", ".png")
#: 校徽目录里认这些扩展名；svg 也认（但 Pillow 读不了，会单独提示"请先导出成 PNG"）。
LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".svg")
#: 空文件名（``.jpg`` 这种）+ 这几个别名 = **默认校徽**（没有名字或用名字/学号匹配不到的队都用它）。
DEFAULT_LOGO_ALIASES = ("", "logo", "校徽", "default", "机构", "学校", "school",
                        "org", "organization", "institution")
#: 裁圆后的边长（正方形 PNG，圆外透明）。
LOGO_SIZE = 512
#: 超采样倍数（先按 size*ss 画圆再缩回来，边缘才不会有锯齿）。
LOGO_SUPERSAMPLE = 3

#: Resolver 的 DiskContestSource 在比赛包目录里按「类型名/对象 id」找本地资源。
#: 反编译依据（org.icpc.tools.contest.model.feed.DiskContestSource$FilePattern.<init>）：
#:     folder = IContestObject.getTypeName(type) + File.separator + id
#:     getLocalPattern(TEAM,         id, "photo") -> FilePattern(TEAM,         id, "photo", PHOTO_EXTENSIONS)
#:     getLocalPattern(ORGANIZATION, id, "logo")  -> FilePattern(ORGANIZATION, id, "logo",  LOGO_EXTENSIONS)
#:     getFilesWithPattern() -> new File(root, pattern.folder).listFiles(名字以 photo/logo 开头的图片)
#: 所以最终落盘形状必须是：
#:     <比赛包>/teams/<队伍ID>/photo.jpg          <- 奖项卡整屏背景
#:     <比赛包>/organizations/<机构ID>/logo.png    <- 奖项卡左下角校徽
#: 旧版本写成 <比赛包>/<队伍ID>/photo.jpg（少了 teams 这一层）—— 实测 getPhoto() 恒为 null，
#: 这就是"队伍照片一直不显示"的真正根因（见 docs/verification-log.txt 附录 K）。
TEAM_FOLDER = "teams"
ORG_FOLDER = "organizations"
PHOTO_TAG = "photo"
LOGO_TAG = "logo"

#: 生成 feed 时用的机构（学校）标识，必须和 pta_to_resolver_xml.py 的
#: --region-id / --organization 默认值保持一致。
ORG_ID = "1"
ORG_NAME = "PTA"


#: 「学号 + 姓名」连写时的分隔符（``26130030张三`` 没有分隔符，也照样拆）。
_COMBINED_SEP = re.compile(r"[\s_\-—–+·.,，、|/\\]+")
#: 学号最少几位才当学号用（4 位起，够短又不容易把普通词当学号）。
_NUMBER_MIN_LEN = 4


def _stem_variants(stem: str) -> list[str]:
    """文件名主干的所有「整名」写法：原样 + 去掉开头星号（全角 ＊ 也认）。

    ``*张三`` → ``["*张三", "张三"]``；``张三`` → ``["张三"]``。
    """
    out: list[str] = []
    raw = str(stem or "").strip()
    if raw:
        out.append(raw.lower())
    bare = star_awards.strip_star(stem).strip()
    if bare and bare.lower() not in out:
        out.append(bare.lower())
    return out


def _split_combined_keys(stem: str) -> list[str]:
    """把「学号 + 姓名」连写的文件名拆成可匹配的键。

    支持这些写法（星号前缀先去掉）::

        26130030张三.jpg      -> ["26130030", "张三"]
        26130030_张三.jpg     -> ["26130030", "张三"]
        26130030-张三.jpg     -> ["26130030", "张三"]
        张三26130030.jpg      -> ["张三", "26130030"]
        *26130030张三.jpg     -> ["26130030", "张三"]

    只用得到「学号段」和「姓名段」，匹配时按 **学号优先** 查（见 match_team_photos）。
    """
    keys: list[str] = []
    for variant in _stem_variants(stem):
        # 整体里没有「学号段」就不拆：纯姓名/纯文字的文件名，整名在第一遍已经进索引了。
        if not re.search(rf"\d{{{_NUMBER_MIN_LEN},}}", variant):
            continue
        for part in _COMBINED_SEP.split(variant):
            part = part.strip()
            if not part:
                continue
            if re.fullmatch(rf"\d{{{_NUMBER_MIN_LEN},}}", part):
                keys.append(part)                     # 整段就是学号
                continue
            head = re.match(rf"^(\d{{{_NUMBER_MIN_LEN},}})(.+)$", part)
            if head:                                  # 学号在前：26130030张三
                keys.append(head.group(1))
                rest = head.group(2).strip()
                if rest:
                    keys.append(rest)
                continue
            tail = re.match(rf"^(.+?)(\d{{{_NUMBER_MIN_LEN},}})$", part)
            if tail:                                  # 姓名在前：张三26130030
                keys.append(tail.group(1).strip())
                keys.append(tail.group(2))
                continue
            keys.append(part)                         # 其余段（26130030_张三 的「张三」）
    return list(dict.fromkeys(k for k in keys if k))


def build_image_index(folder: Path, exts: tuple[str, ...]) -> dict[str, Path]:
    """把图片目录建成「可匹配键（小写）-> 路径」的索引。

    两遍扫描，保证 **完整文件名永远压过拆出来的子键**：

    1. 第一遍：文件名主干（原样 + 去星号）—— 最高优先级；
    2. 第二遍：``学号+姓名`` 连写时拆出的 ``学号`` / ``姓名`` 子键 —— 只补没占用的键。

    这样 ``26130030.jpg`` 与 ``26130030张三.jpg`` 同时在目录里时，
    队伍 ``26130030`` 命中的是**精确同名**的那张，不会被合并名抢走。
    """
    index: dict[str, Path] = {}
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return index
    usable: list[Path] = []
    for item in entries:
        try:
            if not item.is_file() or item.suffix.lower() not in exts:
                continue
        except OSError:
            continue
        usable.append(item)

    for item in usable:
        for key in _stem_variants(item.stem):
            index.setdefault(key, item)
    for item in usable:
        for key in _split_combined_keys(item.stem):
            index.setdefault(key, item)
    return index


def _photo_index(photos_dir: Path) -> dict[str, Path]:
    """把图片目录建成「文件名（不含扩展名，小写）-> 路径」的索引。"""
    return _photo_index_ext(photos_dir, PHOTO_EXTS)


def _photo_index_ext(folder: Path, exts: tuple[str, ...]) -> dict[str, Path]:
    """同上，但可以指定扩展名集合（照片用 PHOTO_EXTS，校徽用 LOGO_EXTS）。"""
    return build_image_index(folder, exts)


def match_team_photos(teams: list[dict[str, Any]], photos_dir: Path) -> dict[str, Any]:
    """把图片目录里的照片按「学号 -> 姓名」匹配到队伍。

    文件名认得这些写法（都容忍开头 ``*`` / ``＊``）：``26130030.jpg``（学号）、
    ``张三.jpg``（姓名）、``26130030张三.jpg`` / ``26130030_张三.jpg``（学号+姓名）。
    **学号优先**：先按学号查，查不到再按姓名查；查到时报告里会写明命中方式。
    """
    index = _photo_index(photos_dir)
    matched: list[tuple[dict[str, Any], Path]] = []
    detail: list[dict[str, Any]] = []          # 逐队命中方式（学号/姓名），给报告用
    unmatched: list[dict[str, Any]] = []
    for team in teams:
        number = str(team.get("studentNumber") or "").strip().lower()
        name = str(team.get("name") or "").strip()
        hit: Path | None = None
        how = ""
        if number:
            hit = index.get(number)
            if hit is not None:
                how = "学号"
        if hit is None and name:
            # 打星容忍：队名 ``*张三`` 能配到 ``张三.jpg``，也能配到 ``*张三.jpg``；
            # 只影响图片文件名的匹配，**显示名不动**（画面照样是 ``*张三``）。
            for candidate in star_awards.name_keys(name):
                hit = index.get(candidate.lower())
                if hit is not None:
                    how = "姓名"
                    break
        if hit is None:
            unmatched.append(team)
        else:
            matched.append((team, hit))
            detail.append({
                "team_id": str(team.get("userId") or ""),
                "name": name,
                "student_number": number,
                "file": hit.name,
                "how": how,
            })
    return {"index": index, "matched": matched, "detail": detail, "unmatched": unmatched}


def attach_team_photos(package_dir: Path, teams: list[dict[str, Any]],
                       photos_dir: Path) -> dict[str, Any]:
    """把照片复制进比赛包：``<比赛包>/teams/<teamId>/photo.<ext>``。

    ``teams`` 这一层是 DiskContestSource 规定的（typeName + id），少了它照片挂不上。
    同时清掉旧版本留下的错误形状 ``<比赛包>/<teamId>/photo.*``，免得看起来像还在用。
    """
    result: dict[str, Any] = {
        "photos_dir": str(photos_dir),
        "total": len(teams),
        "matched_count": 0,
        "copied": 0,
        "missing": [],
        "removed": [],
        "removed_count": 0,
        "error": "",
        "target": f"{TEAM_FOLDER}\\<队伍ID>\\{PHOTO_TAG}.jpg",
    }
    if not photos_dir or not Path(photos_dir).is_dir():
        result["error"] = f"照片目录不存在：{photos_dir}"
        return result

    found = match_team_photos(teams, Path(photos_dir))
    result["matched_count"] = len(found["matched"])
    result["matched_detail"] = list(found.get("detail") or [])
    result["missing"] = [
        {
            "team_id": str(t.get("userId") or ""),
            "name": str(t.get("name") or ""),
            "student_number": str(t.get("studentNumber") or ""),
        }
        for t in found["unmatched"]
    ]

    for team, src in found["matched"]:
        team_id = str(team.get("userId") or "").strip()
        if not team_id:
            continue
        folder = Path(package_dir) / TEAM_FOLDER / team_id
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob(f"{PHOTO_TAG}.*"):
                try:
                    old.unlink()
                except OSError:
                    pass
            dest = folder / f"{PHOTO_TAG}{src.suffix.lower()}"
            shutil.copyfile(src, dest)
            result["copied"] += 1
        except OSError as exc:
            result["missing"].append({
                "team_id": team_id,
                "name": str(team.get("name") or ""),
                "student_number": str(team.get("studentNumber") or ""),
                "error": str(exc),
            })

    # 用户把某队的图删了/改名了 → 比赛包里那份必须删掉，滚榜才会回落到默认背景。
    # （以前只处理"匹配到的"队伍：删了源图，包里旧 photo.* 还在，滚榜继续显示旧图。）
    matched_ids = {str(t.get("userId") or "").strip() for t, _src in found["matched"]}
    removed: list[dict[str, Any]] = []
    for team in teams:
        team_id = str(team.get("userId") or "").strip()
        if not team_id or team_id in matched_ids:
            continue
        folder = Path(package_dir) / TEAM_FOLDER / team_id
        if not folder.is_dir():
            continue
        for old in folder.glob(f"{PHOTO_TAG}.*"):
            try:
                old.unlink()
            except OSError:
                continue
            removed.append({
                "team_id": team_id,
                "name": str(team.get("name") or ""),
                "student_number": str(team.get("studentNumber") or ""),
                "file": old.name,
            })
    result["removed"] = removed
    result["removed_count"] = len(removed)

    # 清掉旧形状 <比赛包>/<teamId>/photo.*（挂不上的那种，留着只会误导）
    for team in teams:
        team_id = str(team.get("userId") or "").strip()
        if not team_id:
            continue
        legacy = Path(package_dir) / team_id
        if legacy.is_dir() and legacy.name != TEAM_FOLDER:
            for old in legacy.glob(f"{PHOTO_TAG}.*"):
                try:
                    old.unlink()
                except OSError:
                    pass
    return result


def _logo_stem_ext(name: str) -> tuple[str, str]:
    """把 ``.jpg`` / ``26130030.png`` 拆成 (主干, 扩展名)。

    坑：``Path('.jpg').suffix`` 是空串、``.stem`` 是 ``'.jpg'`` —— 用户目录里那张
    名字为空的校徽就会既没有扩展名也没有主干。所以这里按**名字尾巴**判断，
    名字等于扩展名本身时主干算空（= 默认校徽）。
    """
    low = name.lower()
    for ext in LOGO_EXTS:
        if low == ext:
            return "", name
        if low.endswith(ext) and len(low) > len(ext):
            return name[: len(name) - len(ext)], name[len(name) - len(ext):]
    return "", ""


def scan_logo_files(logos_dir: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    """列出校徽目录里的图片。

    返回 (``[(路径, 主干)]``, ``[扩展名不认识的]``)；主干为空 = 默认校徽。
    """
    items: list[tuple[Path, str]] = []
    skipped: list[str] = []
    try:
        entries = sorted(Path(logos_dir).iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return items, skipped
    for item in entries:
        try:
            if not item.is_file():
                continue
        except OSError:
            continue
        stem, ext = _logo_stem_ext(item.name)
        if not ext:
            if not item.name.startswith("."):
                skipped.append(item.name)
            continue
        items.append((item, stem.strip()))
    return items, skipped


def match_team_logos(teams: list[dict[str, Any]], logos_dir: Path) -> dict[str, Any]:
    """按「默认 / 学号 / 姓名」把校徽目录匹配到队伍。

    规则（用户口径）：
      * **队伍自带 ``logo`` 键**（VJudge 备注库填的校徽）-> 优先用它：可以是
        校徽目录里的文件名 / 主干，也可以是已经存在的图片绝对路径；
      * 文件名主干为空（``.jpg``）或写成 logo / 校徽 / default / 机构 -> **默认校徽**；
      * 主干 = 某队**学号** -> 该队专属校徽（学号优先）；
      * 主干 = ``学号+姓名`` 连写（``26130030张三`` / ``26130030_张三``）-> 也认；
      * 否则主干 = 某队**姓名**（开头 ``*`` 自动去掉）-> 该队专属校徽；
      * 都对不上 -> 不参与匹配，报告里列出来（不再"匹配不到就用第一张"）。

    索引由 :func:`build_image_index` 统一构建（两遍扫描：完整文件名优先，
    再补 ``学号+姓名`` 拆出来的子键），照片那边走的是同一套规则。
    """
    items, skipped = scan_logo_files(Path(logos_dir))
    index: dict[str, Path] = {}
    for path, stem in items:                      # 第一遍：完整文件名（含去星号写法）
        # 主干为空（文件名就是 ``.jpg``）也是**合法键**：空串 = 默认校徽。
        index.setdefault(stem.strip().lower(), path)
        for key in _stem_variants(stem):
            index.setdefault(key, path)
    for path, stem in items:                      # 第二遍：学号+姓名 拆出的子键
        for key in _split_combined_keys(stem):
            index.setdefault(key, path)

    default: Path | None = None
    for alias in DEFAULT_LOGO_ALIASES:
        hit = index.get(alias)
        if hit is not None:
            default = hit
            break

    # VJudge 备注库里的「校徽」：URL 由抓取器先下载成本地文件；这里认
    # ① 已存在的图片路径 ② 校徽目录里的文件名 / 主干。
    def _explicit_logo(raw: str) -> Path | None:
        text = str(raw or "").strip()
        if not text or text.lower().startswith(("http://", "https://")):
            return None
        path = Path(text)
        if path.is_file():
            return path
        base = path.name.lower()
        for key in (base, base.rsplit(".", 1)[0] if "." in base else base,
                    path.stem.lower()):
            hit = index.get(key)
            if hit is not None:
                return hit
        return None

    matched: list[tuple[dict[str, Any], Path, str]] = []
    for team in teams:
        number = str(team.get("studentNumber") or "").strip().lower()
        name = str(team.get("name") or "").strip()
        hit: Path | None = None
        how = ""
        # ① 备注库指定的校徽最优先（只影响这一支队）
        explicit = _explicit_logo(str(team.get("logo") or ""))
        if explicit is not None:
            hit, how = explicit, "备注校徽"
        if hit is None and number:
            hit = index.get(number)
            if hit is not None:
                how = "学号"
        if hit is None and name:
            for candidate in star_awards.name_keys(name):
                if not candidate:
                    continue
                hit = index.get(candidate.lower())
                if hit is not None:
                    how = "姓名"
                    break
        if hit is not None:
            matched.append((team, hit, how))

    used = {path for _team, path, _how in matched}
    unused = [path.name for path, _stem in items if path not in used and path is not default]
    return {
        "items": items,
        "index": index,
        "default": default,
        "matched": matched,
        "unused": unused,
        "skipped": skipped,
    }


def vjudge_teams_with_avatar_logo(teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    r"""VJudge 专用：没有备注校徽的队，用实时榜那份头像缓存当校徽。

    用户 2026-10-27：滚榜里的校徽要和实时榜一致 —— 实时榜的优先级是
    「备注校徽 → VJudge 头像 → 默认校徽」，而滚榜原来只看校徽目录，
    于是备注过的有图、其他人全落默认校徽。这里把头像文件补进 team["logo"]
    （match_team_logos 支持绝对路径），PTA 的数据不会走到这个函数。

    头像位置：<程序根>\live\logos\<uid>.png（抓取器下的），也兼容 <程序根>\logos\<uid>.png。
    """
    try:
        root = cfgmod.work_root()
    except Exception:  # noqa: BLE001
        return teams
    cand_dirs = [Path(root) / "live" / "logos", Path(root) / "logos"]
    out: list[dict[str, Any]] = []
    for team in teams:
        if not isinstance(team, dict):
            continue
        item = team
        if not str(item.get("logo") or "").strip():
            uid = str(item.get("userId") or "")
            if uid:
                for folder in cand_dirs:
                    for ext in (".png", ".jpg", ".jpeg", ".webp"):
                        cand = folder / (uid + ext)
                        try:
                            if cand.is_file() and cand.stat().st_size > 0:
                                item = dict(item)
                                item["logo"] = str(cand)
                                break
                        except OSError:
                            continue
                    if str(item.get("logo") or "").strip():
                        break
        out.append(item)
    return out


def build_logo_plan(teams: list[dict[str, Any]], logos_dir: Path) -> dict[str, Any]:
    """把匹配结果整理成「机构（organization）计划」。

    * 默认校徽挂在基础机构（``ORG_ID`` = 1）上 —— 没专属校徽的队全都指向它；
    * 每个**不同的**专属校徽文件建一个独立机构 id（``logo1``、``logo2``…），
      引用它的队伍改指自己的机构。

    返回的 ``feed`` 就是要写进 ``--org-plan`` 的那份 JSON。
    """
    found = match_team_logos(teams, Path(logos_dir))
    default: Path | None = found["default"]

    orgs: list[dict[str, Any]] = [{
        "id": ORG_ID,
        "name": ORG_NAME,
        "logo": default,
        "how": "默认" if default is not None else "",
        "members": [],
    }]
    by_file: dict[Path, dict[str, Any]] = {}
    specific_teams: dict[str, str] = {}
    for team, path, how in found["matched"]:
        entry = by_file.get(path)
        if entry is None:
            oid = f"logo{len(by_file) + 1}"
            entry = {
                "id": oid,
                "name": str(team.get("name") or path.stem or oid),
                "logo": path,
                "how": how,
                "members": [],
            }
            by_file[path] = entry
            orgs.append(entry)
        entry["members"].append(str(team.get("name") or ""))
        team_id = str(team.get("userId") or "").strip()
        if team_id:
            specific_teams[team_id] = entry["id"]

    matched_ids = set(specific_teams)
    default_teams = [
        str(t.get("name") or "") for t in teams
        if str(t.get("userId") or "").strip() not in matched_ids
    ]
    orgs[0]["members"] = default_teams

    report = {
        "logos_dir": str(logos_dir),
        "total": len(teams),
        "default": str(default) if default is not None else "",
        "default_name": default.name if default is not None else "",
        "specific": [
            {
                "org_id": o["id"],
                "name": o["name"],
                "logo": Path(o["logo"]).name if o["logo"] is not None else "",
                "how": o["how"],
                "teams": list(o["members"]),
            }
            for o in orgs[1:]
        ],
        "default_teams": default_teams if default is not None else [],
        "no_logo_teams": [] if default is not None else default_teams,
        "unused": list(found["unused"]),
        "skipped": list(found["skipped"]),
        "orgs": orgs,
        "feed": {
            "organizations": [{"id": o["id"], "name": o["name"]} for o in orgs],
            "teams": specific_teams,
        },
        "copied": 0,
        "error": "",
        "target": f"{ORG_FOLDER}\\<机构ID>\\{LOGO_TAG}.png",
    }
    return report


def make_circular_png(src: Path, dest: Path, size: int = LOGO_SIZE,
                      supersample: int = LOGO_SUPERSAMPLE) -> tuple[bool, str]:
    """把校徽处理成**圆形 PNG**：先居中裁成正方形，再套圆形蒙版（圆外透明）。

    先用 ``size * supersample`` 的尺寸画圆再缩回来 —— 直接把 512 的圆画上去边缘会有
    明显锯齿。**只写 dest，绝不动源文件。**
    """
    try:
        from PIL import Image, ImageChops, ImageDraw
    except Exception as exc:  # noqa: BLE001 - Pillow 缺失时退回"原图直通"
        return False, f"缺少 Pillow（{exc}）"
    if src.suffix.lower() == ".svg":
        return False, "SVG 需要先导出成 PNG/JPG 才能裁圆"
    try:
        with Image.open(src) as raw:
            img = raw.convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        return False, f"打不开图片（{type(exc).__name__}: {exc}）"

    width, height = img.size
    if width < 2 or height < 2:
        return False, f"图片太小（{width}x{height}）"
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    img = img.crop((left, top, left + side, top + side))

    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    big = size * max(1, int(supersample))
    img = img.resize((big, big), resample)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    # 和原图自带的 alpha 相乘：本来就是透明的像素保持透明，圆外一律透明。
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    img = img.resize((size, size), resample)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "PNG")
    except OSError as exc:
        return False, str(exc)
    return True, ""


def copy_org_logos(package_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """按计划把校徽**裁圆后**写进 ``<比赛包>/organizations/<机构ID>/logo.png``。"""
    report = plan
    report["copied"] = 0
    report["error"] = ""
    report["notes"] = []
    for org in plan["orgs"]:
        src = org.get("logo")
        if src is None:
            continue
        folder = Path(package_dir) / ORG_FOLDER / str(org["id"])
        dest = folder / f"{LOGO_TAG}.png"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob(f"{LOGO_TAG}.*"):
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError as exc:
            report["notes"].append(f"{org['id']}：{exc}")
            continue
        ok, why = make_circular_png(Path(src), dest)
        if ok:
            report["copied"] += 1
        else:
            report["notes"].append(f"{Path(src).name}：{why}")
    if plan["orgs"] and plan["orgs"][0]["logo"] is None and not report["copied"]:
        report["error"] = "没有可用的校徽文件"
    return report


# --------------------------------------------------------------------------
# 比赛包指纹：防止「改了设置却滚了旧包」（用户实测踩过）
# --------------------------------------------------------------------------

META_NAME = "package-meta.json"


def settings_digest(settings: dict[str, Any]) -> str:
    """把影响比赛包的设置算成一个短哈希。

    ⚠️ **M14 起改用 :func:`package_digest`**（界面与 ``run_package`` 共用同一份
    规范化逻辑）。这个函数保留只为兼容老调用点 —— 它直接哈希**全量 JSON**，
    界面和生成侧只要键集/类型有一点不同就会算出两个哈希（这就是
    「设置改过了，比赛包还是旧的」永久为真的根因，见
    ``app/docs/M14-证据/awards-rootcause.json`` 的 RC1）。
    """
    payload = json.dumps(settings, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


#: 比赛包指纹**唯一**认的键（顺序即文档顺序，哈希时按 sort_keys）。
#:
#: 刻意**不包含** ``logo_plan``：校徽计划里带文件 mtime/大小，界面一侧根本拿不到，
#: 两边永远算不出同一个值。校徽信息照旧写进 ``package-meta.json`` 的 ``settings``
#: 里（只作参考、不参与指纹），所以「换了校徽」不再算新包，但 meta 里仍看得见。
DIGEST_KEYS: tuple[str, ...] = (
    "problem_set_id",
    "freeze_minutes",
    "penalty_minutes",
    "awards",
    "award_medal_summary",
    "first_to_solve",
    "photos_dir",
    "logos_dir",
    "medals",
    "contest_json",
)

#: 这几个键是路径：统一成 ``normcase(normpath(...))`` 再进指纹（``D:\a`` / ``d:/a/``
#: 必须算同一个包），避免「同一份设置、两种写法 → 两个哈希」。
DIGEST_PATH_KEYS = ("photos_dir", "logos_dir", "contest_json")


def _norm_digest_value(key: str, value: Any) -> Any:
    """把某个键的取值规范化成**两边必然一致**的 JSON 可序列化形状。"""
    if key == "awards":
        # M15：奖项行带名单 —— 名单顺序不影响包里发奖（生成侧会按名次再排），
        # 但**必须稳定**，所以这里按字符串排序，避免「同一份名单、两种顺序」算两个哈希。
        out: list[dict[str, Any]] = []
        for idx, row in enumerate(value or []):
            if not isinstance(row, dict):
                continue
            clean = cfgmod.clean_award_row(row, idx)
            out.append({"name": clean["name"], "show": clean["show"],
                        "count": int(clean["count"]),
                        "team_ids": sorted(clean["team_ids"]),
                        "auto": clean["auto"], "manual": bool(clean["manual"])})
        return out
    if key in DIGEST_PATH_KEYS:
        text = str(value or "").strip()
        return os.path.normcase(os.path.normpath(text)) if text else ""
    if key in ("freeze_minutes", "penalty_minutes"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if key in ("award_medal_summary", "first_to_solve"):
        return bool(value)
    if key == "medals":
        gold, silver, bronze = parse_medals(value)
        return f"{gold} {silver} {bronze}"
    return str(value or "")


def package_digest(settings: dict[str, Any]) -> str:
    """比赛包指纹 —— **界面（``_package_settings``）与 ``run_package`` 都用它**。

    键集固定为 :data:`DIGEST_KEYS`，取值逐个规范化（人数/封榜/罚时按整数、
    开关按布尔、路径按 ``normcase``、金银铜按 ``parse_medals`` 的整数三元组），
    所以「同一份设置 → 同一个哈希」是构造上保证的，和调用方传的是 ``"60"``
    还是 ``60``、路径是 ``D:\\x`` 还是 ``d:/x/`` 都无关。
    """
    payload = {key: _norm_digest_value(key, settings.get(key)) for key in DIGEST_KEYS}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def digest_settings_used(settings: dict[str, Any]) -> dict[str, Any]:
    """返回规范化后的指纹负载（**只用于排查/证据**，不进产品逻辑）。

    复算「界面一侧 vs 生成一侧到底差在哪」时直接看这个 dict 就行。
    """
    return {key: _norm_digest_value(key, settings.get(key)) for key in DIGEST_KEYS}


def write_package_meta(package_dir: Path, digest: str, settings: dict[str, Any],
                       awards: list[dict[str, Any]] | None = None,
                       first_to_solve: bool = False,
                       awards_mode: str = "rank",
                       award_groups: list[dict[str, Any]] | None = None) -> str:
    """生成比赛包时写一份 package-meta.json，记录生成时间 + 设置指纹 + 奖项清单。

    M12 追加两个键（老键不动）：``awards_mode``（starred / rank）、
    ``award_groups``（打星模式的组与原样成员，核对「勾了谁」直接看这里）。
    """
    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "digest": digest,
        "settings": settings,
        "first_to_solve": bool(first_to_solve),
        "awards": [{"name": a.get("name"), "count": a.get("count"), "show": a.get("show")}
                   for a in (awards or [])],
        "awards_mode": str(awards_mode or "rank"),
        "award_groups": [{"name": g.get("name"), "show": g.get("show"),
                          "team_ids": [str(t) for t in (g.get("team_ids") or [])]}
                         for g in (award_groups or [])],
    }
    path = Path(package_dir) / META_NAME
    try:
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return f"写不出 {path}：{exc}"
    return ""


def read_package_meta(package_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((Path(package_dir) / META_NAME).read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None


def package_is_current(package_dir: Path, digest: str) -> tuple[bool, str]:
    """比赛包是否与当前设置一致。返回 (是否最新, 说明)。"""
    events = Path(package_dir) / "events.xml"
    if not events.is_file():
        return False, "比赛包不存在"
    meta = read_package_meta(package_dir)
    if meta is None:
        return False, "这个比赛包没有指纹文件（是旧版本或手工生成的）"
    if str(meta.get("digest") or "") != digest:
        return False, "设置改过了，比赛包还是旧的"
    return True, f"比赛包是最新的（生成于 {meta.get('generated_at')}）"


def read_package_awards(package_dir: Path) -> dict[str, Any]:
    """读出比赛包里实际写了哪些奖项（给界面显示，一眼看出是不是旧包）。

    返回的键（**已有键一个不动**，只追加）::

        exists          包里有没有 events.xml
        awards          [奖项名…]（含一血奖，``<citation>`` 原文 —— 老调用点靠它）
        award_records   [{"id","citation","team_ids","show"}…]（M12 新增）
                        打星模式下 citation 就是**组名**，team_ids 就是勾选的队
        first_to_solve  一血奖个数（citation 以「一血」开头的）
        medals          <last-gold> 的值
        mtime / meta    包时间与包指纹
        awards_mode     包指纹里记的奖项模式（starred / rank / 旧包为 null）
    """
    events = Path(package_dir) / "events.xml"
    out: dict[str, Any] = {"exists": events.is_file(), "awards": [], "first_to_solve": 0,
                           "medals": None, "mtime": "", "meta": None,
                           "award_records": [], "awards_mode": None}
    if not out["exists"]:
        return out
    try:
        text = events.read_text(encoding="utf-8", errors="replace")
        out["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(events.stat().st_mtime))
    except OSError:
        return out
    for m in re.finditer(r"<citation>(.*?)</citation>", text):
        out["awards"].append(m.group(1))
    out["first_to_solve"] = len([c for c in out["awards"] if c.startswith("一血")])
    gold = re.search(r"<last-gold>\s*(\d+)\s*</last-gold>", text)
    out["medals"] = int(gold.group(1)) if gold else None
    out["meta"] = read_package_meta(package_dir)

    # ---- M12：把每个 <award> 块连 team_ids 一起读出来（核对「勾了谁」用）----
    for block in re.finditer(r"<award>(.*?)</award>", text, re.S):
        body = block.group(1)
        aid = re.search(r"<id>(.*?)</id>", body, re.S)
        cite = re.search(r"<citation>(.*?)</citation>", body, re.S)
        show = re.search(r"<show>(.*?)</show>", body, re.S)
        out["award_records"].append({
            "id": (aid.group(1) if aid else "").strip(),
            "citation": cite.group(1) if cite else "",
            "show": (show.group(1) if show else "true").strip(),
            "team_ids": [t.strip() for t in re.findall(r"<teamId>(.*?)</teamId>", body, re.S)],
        })
    meta = out["meta"]
    if isinstance(meta, dict):
        out["awards_mode"] = meta.get("awards_mode")
    return out


#: Resolver 的 JSON/NDJSON feed 文件名（``DiskContestSource`` 里 ndjson 优先于 xml）。
FEED_NDJSON = "event-feed.ndjson"


def read_feed_awards(package_dir: Path) -> dict[str, Any]:
    """读 ``event-feed.ndjson`` 里那行 ``{"type": "awards", "data": [...]}``。

    Resolver 实际吃的是 feed（ndjson -> json -> xml 顺序），**不是 events.xml** ——
    所以「三方一致性」验收必须单独看这里：feed 说什么，滚榜就放什么。

    返回::

        exists     feed 文件在不在
        records    [{"id","citation","display_mode","team_ids"}…]（原样，顺序即文件顺序）
        error      解析失败时的原因（存在但读不出来）
        path       feed 的绝对路径
    """
    feed = Path(package_dir) / FEED_NDJSON
    out: dict[str, Any] = {"exists": feed.is_file(), "records": [], "error": "",
                           "path": str(feed)}
    if not out["exists"]:
        return out
    try:
        text = feed.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            out["error"] = out["error"] or f"第 {lineno} 行不是合法 JSON"
            continue
        if not isinstance(item, dict) or item.get("type") != "awards":
            continue
        data = item.get("data")
        if not isinstance(data, list):
            continue
        for rec in data:
            if not isinstance(rec, dict):
                continue
            out["records"].append({
                "id": str(rec.get("id") or ""),
                "citation": str(rec.get("citation") or ""),
                "display_mode": str(rec.get("display_mode") or ""),
                "team_ids": [str(t) for t in (rec.get("team_ids") or [])],
            })
    return out


def contest_team_rows(contest_path: Path, penalty_minutes: int = 20) -> list[dict[str, Any]]:
    """从 contest.json 算出「名次 / 队名（原样含 ``*``）/ 学号 / 解题 / 罚时」的队伍行。

    给打星勾选表用（界面要按名次排、要原样显示 ``*张三``）。用和生成比赛包
    **同一套** ``compute_ranking``，所以表里的名次和滚榜画面对得上。
    """
    root = cfgmod.resource_root()
    _fetch, xmlmod, common = load_backend(root)
    contest = common.load_contest(Path(contest_path))
    try:
        penalty = int(penalty_minutes)
    except (TypeError, ValueError):
        penalty = 20
    ranked = xmlmod.compute_ranking(contest, penalty)
    return [{
        "team_id": str(t.get("team_id") or ""),
        "name": str(t.get("name") or ""),
        "student_number": str(t.get("student_number") or ""),
        "rank": int(t.get("rank") or 0),
        "solved": int(t.get("solved_count") or 0),
        "penalty": int(t.get("penalty") or 0),
    } for t in ranked]


# --------------------------------------------------------------------------
# 奖项预览（M14：界面奖项表旁的「全队标记 / 人数对照」）
# --------------------------------------------------------------------------

#: 展示方式的白名单（与 pta_to_resolver_xml 的写包口径一致）。
_AWARD_SHOW_KEYS = ("detail", "pause", "list", "photos")


def normalize_award_rows(awards: Any) -> list[dict[str, Any]]:
    """界面/配置里的奖项行 → 规范化行（M15 单一模型）。

    复用 :func:`board_config.clean_award_row`，保证「界面里看到的」「config.json 里存的」
    「生成包时用的」是同一套口径；**非法人数保留成 0**，好让预览能把它指出来。
    """
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(awards or []):
        if not isinstance(row, dict):
            continue
        out.append(cfgmod.clean_award_row(row, idx))
    return out


def _starred_ids(ranked: list[dict[str, Any]]) -> list[str]:
    """名次序里所有「队名/姓名前带 * / ＊」的队（``*`` 只是勾选便利）。"""
    return [str(t["team_id"]) for t in ranked if star_awards.has_star(t.get("name"))]


def materialize_award_rows(
    contest: dict[str, Any],
    awards: Any,
    penalty_minutes: int = 20,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把奖项行 + 当前比赛数据算成**最终名单**（预览与生成包唯一的一套口径）。

    规则（M15，**没有"奖项模式"了**）：

    * ``manual=True`` 的行 → 名单就是 ``team_ids``（本次数据里找不到的会被跳过并警告）；
    * ``auto="rank"`` 的行 → 按榜单名次**依次**取前 ``count`` 支；
      多个 rank 行之间**不重叠**（第 2 行接着第 1 行往下取）；
    * ``auto="star"`` 的行 → 取所有带 ``*`` / ＊ 的队；
    * ``auto=""`` 的行 → 只用手选名单，什么都不重算；
    * 空名单的行**不会静默消失**：由调用方把 ``rows[i]["issue"]`` 说出来。

    返回 ``(rows, warnings)``；``rows`` 里每项 = 奖项 + 最终 ``team_ids`` + 诊断信息::

        {"name","show","count","team_ids","source","auto","manual",
         "missing_ids": […],"renamed": [{"id","was","now"}],"issue": "…"}
    """
    _fetch, xmlmod, _common = load_backend(cfgmod.resource_root())
    try:
        penalty = int(penalty_minutes)
    except (TypeError, ValueError):
        penalty = 20
    warnings: list[str] = []
    ranked = xmlmod.compute_ranking(contest, penalty) if contest else []
    by_id = {str(t["team_id"]): t for t in ranked}
    star_ids = _starred_ids(ranked)

    rows: list[dict[str, Any]] = []
    cursor = 0
    for row in normalize_award_rows(awards):
        name = row["name"]
        ids = list(row["team_ids"])
        names = dict(row.get("names") or {})
        source = "手选"
        if row["manual"]:
            source = "手动"
        elif row["auto"] == "rank":
            picked = ranked[cursor:cursor + row["count"]]
            ids = [str(t["team_id"]) for t in picked]
            cursor += len(picked)
            source = "自动（按名次）"
        elif row["auto"] == "star":
            ids = list(star_ids)
            source = "自动（星号）"
        ids = list(dict.fromkeys(ids))
        missing = [tid for tid in ids if tid not in by_id]
        missing_names = [f"{tid}（上次是「{names[tid]}」）" if names.get(tid) else tid
                         for tid in missing]
        renamed = []
        for tid in ids:
            if tid in by_id and names.get(tid) and names[tid] != str(by_id[tid]["name"]):
                renamed.append({"id": tid, "was": names[tid], "now": str(by_id[tid]["name"])})
        kept = [tid for tid in ids if tid in by_id]
        issue = ""
        if not ids:
            if row["auto"] == "rank" and row["count"] <= 0:
                issue = "这一行还没填人数、也没有名单 —— 不会进包（填人数或到「全队标记」里勾队）"
            elif row["auto"] == "rank":
                issue = "按名次填不到队（榜单上已经没有人了）—— 不会进包"
            else:
                issue = "名单是空的 —— 不会进包（到「全队标记」里勾队，或用「按名次填」/「按 * 自动勾选」）"
        elif not kept:
            issue = "名单里的队在本次数据里一个都找不到 —— 不会进包"
        elif missing:
            issue = f"有 {len(missing)} 支已勾选的队在本次数据里找不到（已跳过）：" + \
                    "、".join(missing_names[:4])
        # 改名了也要说（和「找不到」可以同时出现：一队消失、另一队改名）
        if renamed:
            text = "有 " + str(len(renamed)) + " 支队改名了：" + \
                   "、".join(f"「{r['was']}」→「{r['now']}」" for r in renamed[:3])
            issue = (issue + "；" + text) if issue else text
        rows.append({
            "name": name, "show": row["show"], "count": row["count"],
            "team_ids": kept, "source": source, "auto": row["auto"],
            "manual": bool(row["manual"]),
            "missing_ids": missing, "missing_names": missing_names,
            "renamed": renamed, "issue": issue,
        })
        if issue:
            warnings.append(f"奖项「{name}」：{issue}")
    return rows, warnings


def award_rows_preview_from_contest(
    contest: dict[str, Any],
    awards: Any,
    penalty_minutes: int = 20,
    **legacy: Any,
) -> dict[str, Any]:
    """全队预览（M15 单一模型）：**调用与生成包同一个** ``materialize_award_rows``。

    返回::

        ok            有没有算出来
        mode          固定 "rows"（M15 起只有一种模型；老调用点靠它判断分支）
        rows          [{"rank","team_id","name","student_number","awards":[奖项名…],
                        "award": 第一个奖项名或 None,"solved","penalty"}…]
        awards        [{"name","count","picked","short","missing","team_ids","skipped",
                        "issue","show","source","manual","auto","missing_ids","renamed"}…]
        warnings      生成侧的全部警告（原文，含「找不到的队」「改名了」）
        total_teams   榜单队伍数
        unassigned    一个奖都没分到的队数
        missing_total 所有奖项里"在数据里找不到"的队数合计
    """
    legacy_groups = legacy.get("award_groups")
    if legacy_groups:
        # 老调用点（M12 打星组）→ 迁成行，行为等价，方便老证据脚本继续跑
        awards = list(awards or []) + cfgmod.awards_from_groups(legacy_groups)
    out: dict[str, Any] = {"ok": False, "mode": "rows", "rows": [], "awards": [],
                           "warnings": [], "total_teams": 0, "unassigned": 0,
                           "missing_total": 0}
    if not isinstance(contest, dict) or not contest:
        out["warnings"].append("没有比赛数据（contest.json 不存在或读不出来）。")
        return out
    _fetch, xmlmod, _common = load_backend(cfgmod.resource_root())
    try:
        penalty = int(penalty_minutes)
    except (TypeError, ValueError):
        penalty = 20
    ranked = xmlmod.compute_ranking(contest, penalty)
    out["total_teams"] = len(ranked)
    if not ranked:
        out["warnings"].append("比赛里没有任何队伍，无法分配奖项。")
        return out
    out["ok"] = True

    rows, warnings = materialize_award_rows(contest, awards, penalty)
    team_awards: dict[str, list[str]] = {}
    for row in rows:
        for tid in row["team_ids"]:
            team_awards.setdefault(str(tid), []).append(str(row["name"]))
    for row in rows:
        picked = len(row["team_ids"])
        want = row["count"] if row["auto"] == "rank" else len(row["missing_ids"]) + picked
        out["awards"].append({
            "name": row["name"], "count": want, "picked": picked,
            "short": bool(picked < want),
            "missing": picked == 0,
            "team_ids": list(row["team_ids"]),
            "skipped": picked == 0,
            "issue": row["issue"], "show": row["show"], "source": row["source"],
            "manual": bool(row["manual"]), "auto": row["auto"],
            "missing_ids": list(row["missing_ids"]), "renamed": list(row["renamed"]),
        })
        out["missing_total"] += len(row["missing_ids"])
    out["warnings"] = list(warnings)
    for team in ranked:
        tid = str(team["team_id"])
        hit = team_awards.get(tid) or []
        out["rows"].append({
            "rank": int(team["rank"]),
            "team_id": tid,
            "name": str(team["name"] or ""),
            "student_number": str(team["student_number"] or ""),
            "awards": hit,
            "award": hit[0] if hit else None,
            "solved": int(team["solved_count"]),
            "penalty": int(team["penalty"]),
        })
    out["unassigned"] = len([t for t in out["rows"] if not t["awards"]])
    return out


#: 兼容别名（老代码/老证据脚本叫它 rank_award_preview_from_contest）
rank_award_preview_from_contest = award_rows_preview_from_contest


def award_rows_preview(contest_path: Path, awards: Any, penalty_minutes: int = 20,
                       **legacy: Any) -> dict[str, Any]:
    """读 ``contest.json`` 后调 :func:`award_rows_preview_from_contest`（界面刷新用）。"""
    _fetch, _xmlmod, common = load_backend(cfgmod.resource_root())
    path = Path(str(contest_path or ""))
    if not path.is_file():
        return {"ok": False, "mode": "rows", "rows": [], "awards": [],
                "warnings": [f"读不到比赛数据（{path}）—— 先去「数据源」页点「拉取数据」。"],
                "total_teams": 0, "unassigned": 0, "missing_total": 0,
                "contest_json": str(path)}
    try:
        contest = common.load_contest(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "mode": "rows", "rows": [], "awards": [],
                "warnings": [f"解析 {path} 失败：{type(exc).__name__}: {exc}"],
                "total_teams": 0, "unassigned": 0, "missing_total": 0,
                "contest_json": str(path)}
    out = award_rows_preview_from_contest(contest, awards, penalty_minutes, **legacy)
    out["contest_json"] = str(path)
    return out


#: 兼容别名（M14 的面板/证据脚本叫它 rank_award_preview）
rank_award_preview = award_rows_preview






# --------------------------------------------------------------------------
# Resolver 自己的日志（用来确认 --singleStep 到底有没有生效）
# --------------------------------------------------------------------------

def newest_resolver_log(root: Path) -> Path | None:
    """找到 Resolver 自己写的那份 trace 日志（``app/logs/resolver_*.log``）。

    ⚠️ 不是我们捕获的 stdout —— 那份是有缓冲的，进程崩了会丢；
    Resolver 的 Trace 是另一路，`Option found: --singlestep 2` 只出现在它里面。
    """
    folder = resolver_app_dir(root) / "logs"
    try:
        files = [p for p in folder.glob("resolver_*.log") if p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def read_resolver_options(root: Path, tries: int = 40,
                          interval: float = 1.0) -> list[str]:
    """等 Resolver 写出它的日志，然后返回里面所有 ``Option found`` 行。

    顺手把 ``Illegal ... value ignored`` 这类告警也捞出来 —— 只要出现，
    就说明参数被 Resolver 丢掉了（`--singleStep 0/1` 就是这种情况）。
    """
    deadline = time.time() + max(1.0, tries * interval)
    best: list[str] = []
    while time.time() < deadline:
        path = newest_resolver_log(root)
        if path is not None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            best = [ln for ln in lines
                    if ("Option found" in ln or "Option not found" in ln
                        or "value ignored" in ln or "Illegal" in ln)]
            if best:
                return best
        time.sleep(interval)
    return best


def logo_plan_digest(plan: dict[str, Any] | None) -> str:
    """把校徽计划压成一段字符串，进包指纹 —— 换了校徽文件 / 换了匹配就该算新包。"""
    if not plan:
        return ""
    payload = []
    for org in plan.get("orgs", []):
        src = org.get("logo")
        info: dict[str, Any] = {"id": org.get("id"), "logo": "", "size": 0, "mtime": 0}
        if src is not None:
            path = Path(src)
            info["logo"] = path.name
            try:
                stat = path.stat()
                info["size"] = int(stat.st_size)
                info["mtime"] = int(stat.st_mtime)
            except OSError:
                pass
        payload.append(info)
    return json.dumps({"orgs": payload,
                       "teams": (plan.get("feed") or {}).get("teams") or {}},
                      ensure_ascii=False, sort_keys=True)


def contest_team_number_stats(contest_path: "Path | str") -> dict[str, Any]:
    """统计 ``contest.json`` 里有多少支队伍带**学号**（``studentNumber``）。

    M13：照片/校徽「按学号匹配」的前提是数据里真有学号。旧版 ``pta_fetch.py``
    只写 ``userId / studentUserId / name``，学号被丢掉了 —— 于是用户按学号命名的
    照片全部匹配不上，看起来就是「只认姓名」。抓数侧已修（见 ``pta_fetch.fetch_teams``），
    这里给界面提供判断依据：**一个学号都没有时要明确提示，不能静默当成"没交照片"**。

    返回 ``{"exists", "path", "total", "with_number", "missing_names", "error"}``。
    """
    path = Path(str(contest_path or ""))
    out: dict[str, Any] = {"exists": False, "path": str(path), "total": 0,
                           "with_number": 0, "missing_names": [], "error": ""}
    if not path.is_file():
        return out
    out["exists"] = True
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    teams = data.get("teams") if isinstance(data, dict) else None
    if not isinstance(teams, list):
        return out
    for team in teams:
        if not isinstance(team, dict):
            continue
        out["total"] += 1
        if str(team.get("studentNumber") or "").strip():
            out["with_number"] += 1
        elif len(out["missing_names"]) < 10:
            out["missing_names"].append(str(team.get("name") or team.get("userId") or ""))
    return out


def run_package(
    root: Path,
    contest_path: Path,
    package_dir: Path,
    freeze_minutes: int,
    penalty_minutes: int,
    medals: Any,
    sink: LogSink,
    awards: list[dict[str, Any]] | None = None,
    photos_dir: Any = "",
    logos_dir: Any = "",
    medal_summary: Any = False,
    first_to_solve: Any = True,
    awards_mode: Any = "rank",
    award_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把 contest.json 转成 Resolver 比赛包（内含 events.xml）。

    封榜时长、罚时、奖项都是「生成比赛包时」写进 XML 的，所以这里先把它们补进
    一份 contest.json 的临时副本，再调用既有的 ``pta_to_resolver_xml.main``
    —— 既不改动你的 contest.json，也不重写 XML 生成逻辑。

    **奖项（M15 单一模型）**：``awards`` 就是「奖项表」，**每一行 = 一个 ``<award>``**
    （``citation`` = 奖项名，``teamId`` = 该行名单）。名单由
    :func:`materialize_award_rows` 按行算出来（``manual`` 行照抄手选名单，
    ``auto="rank"`` 行按名次依次填、``auto="star"`` 行取带 ``*`` 的队），
    预览界面用的是**同一个函数**，所以「界面看到谁」=「包里发给谁」。
    一血奖照旧由 ``first_to_solve`` 每题一个。

    * **所有名单都空时直接拦住**（``ok=False`` + 明确提示），不静默生成一个没有奖项的包；
    * 单个奖项名单为空 / 有队在数据里找不到 → 照旧生成，但**逐条写进日志与返回值**
      （``warnings``），界面也照原样显示，绝不静默少发；
    * 向后兼容：``awards_mode`` / ``award_groups``（M12 的老参数）仍然收，
      会被折进 ``awards`` 行（老调用点/老证据脚本不用改）。

    ``photos_dir`` 非空时，会把图片按「学号 -> 姓名」匹配并复制成
    ``<比赛包>/teams/<队ID>/photo.jpg``（Resolver 的 DiskContestSource 就是按这个找图）。
    队名带 ``*`` 的照片/校徽也能配上（见 ``star_awards.name_keys``），显示名里的 ``*`` 原样保留。

    ``logos_dir`` 非空时，按「默认校徽（文件名主干为空，或 logo/校徽/default/机构）
    -> 学号 -> 姓名」匹配：默认校徽挂在基础机构（id=1）上，每个专属校徽单独建一个
    机构（id=logo1、logo2…）；所有校徽都裁成圆形 PNG 写进
    ``<比赛包>/organizations/<机构ID>/logo.png``，feed 里队伍用 organization_id 引用。
    """
    _fetch, xmlmod, common = load_backend(root)

    if not contest_path.is_file():
        return {"ok": False, "message": f"找不到 {contest_path}。请先在「比赛」页点「拉取数据」。"}

    medal_gold, medal_silver, medal_bronze = parse_medals(medals)
    award_rows = normalize_award_rows([dict(a) for a in (awards or []) if isinstance(a, dict)])
    if award_groups:                      # M12 老参数 → 折成行（行为等价）
        award_rows = award_rows + cfgmod.awards_from_groups(award_groups)
    if not award_rows:
        award_rows = normalize_award_rows(cfgmod.DEFAULT_AWARDS)
    mode = "rows"                          # meta 里记的模型名（不再是 rank/starred）
    all_zero = (medal_gold, medal_silver, medal_bronze) == (0, 0, 0) and not award_rows

    # 校徽计划要在生成 feed 之前算出来 —— organization_id 得先进 feed，
    # 之后才轮到把（裁圆的）logo.png 写进 organizations\<机构ID>\。
    logo_plan: dict[str, Any] | None = None
    logo_dir_error = ""

    with LogRedirect(sink):
        print("=" * 62)
        print("  生成 Resolver 比赛包")
        print("=" * 62)
        try:
            contest = common.load_contest(contest_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] 无法解析 {contest_path}：{exc}")
            return {"ok": False, "message": f"无法解析 {contest_path}：{exc}"}

        meta = contest.setdefault("contest", {})
        meta["freeze_seconds"] = max(0, int(freeze_minutes)) * 60
        meta["freeze_length"] = common.format_hms(meta["freeze_seconds"])
        meta["penalty_minutes"] = max(0, int(penalty_minutes))

        patched = tmp_dir() / "contest.patched.json"
        try:
            common.save_contest(patched, contest)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"无法写出临时 contest.json（{patched}）：{exc}"}

        print(f"  输入      : {contest_path}")
        print(f"  比赛包目录: {package_dir}")
        print(f"  封榜时长  : {meta['freeze_length']}     罚时: {meta['penalty_minutes']} 分钟")
        # ---- M15：奖项表 → 最终名单（与预览界面同一个函数）----------------
        rows_final, row_warnings = materialize_award_rows(contest, award_rows,
                                                          int(penalty_minutes))
        picked_total = sum(len(r["team_ids"]) for r in rows_final)
        print(f"  奖项表    : 共 {len(rows_final)} 个奖项（单一模型：每行 = 一个 <award>）")
        for idx, row in enumerate(rows_final, 1):
            print(f"     · {idx}. 「{row['name']}」{len(row['team_ids'])} 队"
                  f"（{row['source']}，展示方式 {row['show']}）"
                  + (f"  ⚠ {row['issue']}" if row["issue"] else ""))
        if picked_total == 0:
            message = (
                "奖项表里一个队都没有 —— 这次没有生成比赛包。\n"
                "每个奖项的名单要么在「全队标记」表里勾，要么用「按名次填前 N 支」/"
                "「按 * 自动勾选」填；全空的包除了没有任何奖项，滚榜也没意义，所以这里直接拦住。\n"
                "怎么办：到「滚榜 → 外观（2）」页的「奖项表」下面那张「全队标记」表里，"
                "勾上要获奖的队（队名/姓名前带 * 或 ＊ 的可以用「按 * 自动勾选」一键填），"
                "然后点「重新识别 / 刷新」。")
            print("[错误] " + message.replace("\n", "\n[错误] "))
            return {"ok": False, "message": message, "warning": True,
                    "awards_mode": mode, "package_dir": str(package_dir),
                    "warnings": list(row_warnings)}
        for line in row_warnings:
            print(f"  [警告] {line}")
        if all_zero:
            print("[警告] 既没有自定义奖项、奖牌名次又全是 0：XML 里不会有任何 <award>，"
                  "Resolver 启动时会抛 NullPointerException 崩掉。")

        argv = [
            "--contest", str(patched),
            "--out-dir", str(package_dir),
            "--medals", str(medal_gold), str(medal_silver), str(medal_bronze),
        ]
        awards_file: Path | None = None
        # 每个奖项行都是「名字 + 展示方式 + 名单」——和 M12 的「奖项组」是同一个形状，
        # 所以直接走 ``--award-groups-file``（生成侧 build_group_award_blocks 已实测可用）。
        rows_spec = [{"name": r["name"], "show": r["show"], "team_ids": list(r["team_ids"])}
                     for r in rows_final]
        groups_file = tmp_dir() / "award-rows.json"
        try:
            groups_file.write_text(
                json.dumps(rows_spec, ensure_ascii=False, indent=2), encoding="utf-8")
            argv += ["--award-rows-file", str(groups_file)]
        except OSError as exc:
            message = f"奖项名单写不出来（{groups_file}）：{exc}"
            print(f"[错误] {message}")
            return {"ok": False, "message": message}

        # ---- 校徽：先算「哪个队用哪个校徽」-> 写成一个机构计划交给 feed ----
        if logos_dir and not logo_dir_error:
            if not Path(str(logos_dir)).is_dir():
                logo_dir_error = f"校徽目录不存在：{logos_dir}"
            else:
                try:
                    logo_teams = contest.get("teams") or []
                    if str(contest.get("source") or "").lower() == "vjudge":
                        # VJudge：把「实时榜那套头像」当校徽喂进去（PTA 不动）
                        logo_teams = vjudge_teams_with_avatar_logo(logo_teams)
                    logo_plan = build_logo_plan(logo_teams, Path(str(logos_dir)))
                except Exception as exc:  # noqa: BLE001
                    logo_dir_error = f"{type(exc).__name__}: {exc}"
        if logo_plan is not None:
            plan_file = tmp_dir() / "logo-plan.json"
            try:
                plan_file.write_text(
                    json.dumps(logo_plan["feed"], ensure_ascii=False, indent=1),
                    encoding="utf-8")
                argv += ["--org-plan", str(plan_file)]
            except OSError as exc:
                print(f"[警告] 校徽映射写不出来（{exc}），本次只用一个机构（默认校徽挂在它上面）。")
                logo_plan["feed"]["teams"] = {}

        code = 1
        try:
            if medal_summary:
                argv.append("--medal-summary")
            if first_to_solve:
                argv.append("--first-to-solve")
            code = xmlmod.main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[错误] 生成 XML 时出现未预期的问题：{type(exc).__name__}: {exc}")

    events = Path(package_dir) / "events.xml"
    if code != 0 or not events.is_file():
        return {"ok": False, "message": f"生成比赛包失败（退出码 {code}），详细原因见上面的日志。"}

    # ---- 队伍照片 ----
    photo_report: dict[str, Any] | None = None
    if photos_dir:
        photo_report = attach_team_photos(package_dir, contest.get("teams") or [],
                                          Path(str(photos_dir)))
        with LogRedirect(sink):
            if photo_report.get("error"):
                print(f"[警告] 照片没处理：{photo_report['error']}")
            else:
                print(f"  队伍照片  : 匹配到 {photo_report['matched_count']} / "
                      f"{photo_report['total']} 支队伍，已复制 {photo_report['copied']} 张"
                      f"（目标：{package_dir}\\{TEAM_FOLDER}\\<队伍ID>\\{PHOTO_TAG}.jpg）")
                print("     效果：滚榜揭晓奖项时，队伍照片铺满整张奖项卡（背景）")
                mixed = photo_report["matched_count"] and photo_report["total"] > photo_report["matched_count"]
                if mixed:
                    print("     ** 有照片的队用照片铺满，没照片的队自动回退默认背景（ICPC 默认底图）**")
                missed = photo_report.get("missing") or []
                if missed:
                    names = "、".join(str(m.get("name")) for m in missed[:12])
                    more = "" if len(missed) <= 12 else f" 等 {len(missed)} 支"
                    print(f"     没配到照片：{names}{more}（这些队伍的比赛卡用默认背景）")
                    print("     （文件名用学号最稳，例如 26160358.jpg；也可以直接用姓名 汪章正.jpg）")

    # ---- 校徽（机构 logo）----
    logo_report: dict[str, Any] | None = None
    if logos_dir:
        with LogRedirect(sink):
            if logo_dir_error:
                print(f"[警告] 校徽没处理：{logo_dir_error}")
                logo_report = {
                    "logos_dir": str(logos_dir), "error": logo_dir_error,
                    "orgs": [], "copied": 0, "default": "", "specific": [],
                    "unused": [], "no_logo_teams": [], "total": 0,
                }
            elif logo_plan is not None:
                logo_report = copy_org_logos(package_dir, logo_plan)
                if logo_report["copied"] == 0:
                    if logo_report.get("error"):
                        logo_report["error"] = (
                            f"校徽目录里没有可用的校徽（支持 {'/'.join(LOGO_EXTS)}）：{logos_dir}")
                    print(f"[警告] 校徽没处理：{logo_report.get('error') or '没有可用的校徽'}")
                else:
                    print(f"  校徽      : 已挂上 {logo_report['copied']} 个（全部裁成圆形 "
                          f"{LOGO_SIZE}x{LOGO_SIZE} PNG）")
                    if logo_report["default"]:
                        print(f"     默认校徽: {logo_report['default_name']}"
                              f" -> {package_dir}\\{ORG_FOLDER}\\{ORG_ID}\\{LOGO_TAG}.png"
                              f"（{len(logo_report['default_teams'])} 支没有专属校徽的队伍都用它）")
                    else:
                        print("     默认校徽: 没有 —— 没专属校徽的队伍不显示校徽（不报错）")
                    for item in logo_report["specific"]:
                        names = "、".join(item["teams"][:6])
                        more = "" if len(item["teams"]) <= 6 else " 等"
                        print(f"     专属校徽: {item['logo']}（按{item['how']}匹配）-> "
                              f"{ORG_FOLDER}\\{item['org_id']}\\{LOGO_TAG}.png -> {names}{more}")
                    if logo_report["notes"]:
                        print(f"     ⚠ 有 {len(logo_report['notes'])} 个校徽没处理成："
                              + "；".join(logo_report["notes"][:6]))
                    print("     效果：奖项卡左下角、队伍名前，以及榜单每一行的校徽")
            if logo_report is not None:
                if logo_report.get("unused"):
                    files = "、".join(logo_report["unused"][:12])
                    more = "" if len(logo_report["unused"]) <= 12 else f" 等 {len(logo_report['unused'])} 个"
                    print(f"     ⚠ 有 {len(logo_report['unused'])} 个校徽没匹配到任何队伍：{files}{more}")
                if logo_report.get("skipped"):
                    files = "、".join(logo_report["skipped"][:8])
                    print(f"     ⚠ 有 {len(logo_report['skipped'])} 个文件不是图片，已跳过：{files}")
                if logo_report.get("no_logo_teams"):
                    print("     没有默认校徽：以下队伍不显示校徽 —— "
                          + "、".join(logo_report["no_logo_teams"][:12]))

    # ---- 写包指纹：记录生成时间 + 影响包的设置哈希 + 奖项清单 ----
    # M14（RC1）：指纹**只走 package_digest + DIGEST_KEYS**，界面一侧用同一个函数、
    # 同一份键表，所以「同一份设置 → 两边同一个哈希」。
    # logo_plan 刻意留在下面的 settings 里（参考用）但**不进指纹** —— 它带
    # 文件 mtime/大小，界面侧拿不到，是原来两边永远不相等的原因。
    digest_settings = {
        "problem_set_id": contest.get("problem_set_id"),
        "freeze_minutes": int(freeze_minutes),
        "penalty_minutes": int(penalty_minutes),
        # M15：奖品口径就是「奖项表 + 每行名单」，不再有 awards_mode / award_groups
        "awards": award_rows,
        "award_medal_summary": bool(medal_summary),
        "first_to_solve": bool(first_to_solve),
        "photos_dir": str(photos_dir or ""),
        "logos_dir": str(logos_dir or ""),
        "medals": str(medals or ""),
        "contest_json": str(contest_path),
    }
    digest = package_digest(digest_settings)
    meta_settings = dict(digest_settings)
    meta_settings["logo_plan"] = logo_plan_digest(logo_plan)     # 只进 meta，不进指纹
    meta_settings["award_rows_final"] = [
        {"name": r["name"], "show": r["show"], "team_ids": list(r["team_ids"])}
        for r in rows_final]
    meta_err = write_package_meta(package_dir, digest, meta_settings,
                                  awards=award_rows, first_to_solve=bool(first_to_solve),
                                  awards_mode=mode,
                                  award_groups=rows_spec)
    if meta_err:
        with LogRedirect(sink):
            print(f"[警告] {meta_err}（不影响滚榜，但下次会认为包是旧的）")

    size_kb = events.stat().st_size / 1024.0
    stats = {
        "title": meta.get("title") or "",
        "team_count": len(contest.get("teams") or []),
        "problem_count": len(contest.get("problems") or []),
    }
    award_lines = "\n  奖项（每行一个 <award>）：" + " / ".join(
        f"{r['name']}×{len(r['team_ids'])}" for r in rows_final)
    photo_line = ""
    if photo_report and not photo_report.get("error"):
        photo_line = (f"\n  队伍照片：匹配 {photo_report['matched_count']} / "
                      f"{photo_report['total']} 支 —— 已挂上（奖项卡整屏背景）")
    logo_line = ""
    if logo_report and not logo_report.get("error"):
        detail = ""
        if logo_report.get("specific"):
            detail = f"（专属 {len(logo_report['specific'])} 个，其余用默认）"
        elif logo_report.get("default"):
            detail = "（默认校徽，全部队伍共用）"
        logo_line = (f"\n  校徽：已挂上 {logo_report.get('copied', 0)} 个{detail}"
                     f" —— 圆形，画在奖项卡左下角与榜单行上")
        if logo_report.get("unused"):
            logo_line += (f"\n        ⚠ 有 {len(logo_report['unused'])} 个校徽没匹配到任何队伍："
                          + "、".join(logo_report["unused"][:6]))
    feed_line = "\n  feed：event-feed.ndjson（JSON/NDJSON，照片与校徽靠它才生效）"
    message = (
        f"比赛包已生成：{events}\n"
        f"  《{stats['title']}》 {stats['problem_count']} 题 / "
        f"{stats['team_count']} 队 · events.xml {size_kb:.1f} KB"
        f"{feed_line}{award_lines}{photo_line}{logo_line}"
    )
    if all_zero and mode != "starred":
        message += "\n  ⚠ 既没有自定义奖项、奖牌名次又全是 0 —— 这样滚榜会崩，请填上奖项再生成一次。"
    return {
        "ok": True,
        "events": str(events),
        "package_dir": str(package_dir),
        "stats": stats,
        "medals_all_zero": all_zero,
        "photos": photo_report,
        "logos": logo_report,
        "awards_mode": mode,
        "award_rows": rows_spec,
        "award_warnings": list(row_warnings),
        "award_groups": [],                 # M15 起没有「组」这个概念了（保留键只为兼容）
        "message": message,
    }


# --------------------------------------------------------------------------
# 环境检查
# --------------------------------------------------------------------------

class Check:
    def __init__(self, name: str, ok: bool, detail: str, fix: str = "") -> None:
        self.name = name
        self.ok = ok
        self.detail = detail
        self.fix = fix

    def line(self) -> str:
        mark = "[OK]  " if self.ok else "[缺失]"
        text = f"{mark} {self.name}：{self.detail}"
        if not self.ok and self.fix:
            text += f"\n         → {self.fix}"
        return text


def check_environment(root: Path) -> list[Check]:
    checks: list[Check] = []

    # 1. 项目根目录
    need = [root / "pta_fetch.py", root / "pta_to_resolver_xml.py", root / "pta_common.py"]
    missing = [p.name for p in need if not p.is_file()]
    if missing:
        checks.append(Check(
            "后端脚本", False, f"在 {root} 找不到 {', '.join(missing)}",
            "本程序需要和 pta_fetch.py 等脚本在一起。请从项目根目录运行，"
            "或把打包后的整个文件夹完整解压（不要只拷 exe）。",
        ))
    else:
        checks.append(Check("后端脚本", True, f"{root}（pta_fetch / pta_to_resolver_xml 已就位）"))

    # 2. Java
    java = shutil.which("java")
    if not java:
        checks.append(Check(
            "Java", False, "PATH 里找不到 java",
            "去 https://adoptium.net 装 JDK 17（或任意 JRE 17+），"
            "安装时勾选 Add to PATH，然后重启本程序。",
        ))
    else:
        version = _java_version(java)
        checks.append(Check("Java", True, f"{java}（{version}）"))

    # 3. Resolver
    app = resolver_app_dir(root)
    jars = sorted(app.glob("lib/*.jar")) if app.is_dir() else []
    if not (app / "lib").is_dir() or not jars:
        checks.append(Check(
            "ICPC Resolver", False, f"找不到 {app}\\lib\\*.jar",
            "把 ICPC Resolver 发行包解压到 tools\\resolver\\ 下，"
            "确保存在 tools\\resolver\\app\\lib\\resolver.jar。",
        ))
    else:
        checks.append(Check("ICPC Resolver", True, f"{len(jars)} 个 jar，位于 {app}"))

    # 4. 中文字体
    fonts_present = []
    for name in ("MicrosoftYaHei.ttc", "msyh.ttc", "NotoSansSC-VF.ttf", "Deng.ttf"):
        for base in (root / "tools" / "fonts", _system_fonts_dir()):
            if (base / name).is_file():
                fonts_present.append(str(base / name))
                break
    if fonts_present:
        checks.append(Check("中文字体", True, fonts_present[0]))
    else:
        checks.append(Check(
            "中文字体", False, "没找到任何含中文的字体文件",
            "把程序自带的中文字体放进程序目录的 tools\\fonts\\（发布包已内置 msyh.ttc），"
            "否则中文会显示成方框。",
        ))

    # 5. Cookie（必需）
    has_cookie, where = cookie_available(root)
    if has_cookie:
        checks.append(Check("PTA Cookie", True, where))
    else:
        checks.append(Check(
            "PTA Cookie", False,
            "没有找到 cookie.txt —— 取数固定用「直连接口」，必须有有效 Cookie",
            "浏览器登录 pintia.cn 后按 F12 → Network → 右键请求 → Copy as cURL (bash)，"
            "回到程序里点「粘贴 Cookie」粘进去（JSESSIONID 与 PTASession 两个都必须带）。",
        ))

    # 6. 配置目录可写
    ok, note = _probe_writable(cfgmod.config_home()[0])
    checks.append(Check(
        "设置保存位置", ok, note or str(cfgmod.config_home()[0]),
        "把程序移到桌面或 D 盘等可写目录再运行。",
    ))

    return checks


def _probe_writable(folder: Path) -> tuple[bool, str]:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".pta-board-check"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, f"{folder} 不可写（{exc}）"


def _java_version(java: str) -> str:
    try:
        proc = subprocess.run(
            [java, "-version"], capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        first = (proc.stderr or proc.stdout or "").splitlines()
        return first[0].strip() if first else "版本未知"
    except Exception as exc:  # noqa: BLE001
        return f"无法获取版本：{exc}"


def find_java() -> str:
    """返回 java.exe 的路径。

    注意：**故意用 java.exe 而不是 javaw.exe** —— ``tools\\esc_quit.py``
    是按进程可执行文件名 ``java.exe`` 来找 Resolver 窗口的，换成 javaw 会让
    Esc 退出守护失效。控制台窗口用 CREATE_NO_WINDOW 抑制掉。
    """
    found = shutil.which("java")
    if found:
        return found
    for env in ("JAVA_HOME", "JDK_HOME"):
        home = os.environ.get(env)
        if home:
            cand = Path(home) / "bin" / "java.exe"
            if cand.is_file():
                return str(cand)
    # Program Files / Program Files (x86) 等一律从环境变量取，不写死盘符
    java_dirs = [pf / name for pf in _program_files_dirs()
                 for name in ("Java", "Eclipse Adoptium", "Microsoft")]
    for cand in java_dirs:
        if cand.is_dir():
            for exe in sorted(cand.glob("*/bin/java.exe"), reverse=True):
                return str(exe)
    return ""


# --------------------------------------------------------------------------
# 启动滚榜
# --------------------------------------------------------------------------

def build_resolver_argv(
    root: Path,
    package_dir: Path,
    play_mode: str,
    speed: float,
    display_name: str,
    extra_args: str,
    click_from_place: int = 4,
) -> list[str]:
    """拼出 ``resolver.bat`` 内部真正执行的 java 命令行。"""
    java = find_java() or "java"
    app = resolver_app_dir(root)
    argv = [
        java,
        "-Xmx4096m",
        "-cp", str(app / "lib" / "*"),
        RESOLVER_MAIN_CLASS,
        str(package_dir),
    ]
    if display_name.strip():
        argv += ["--display_name", display_name.strip()]
    # --singleStep 的真实语义（实测）：内部 stepRow = N-1 且要求 N-1 > 0。
    #   N=2      -> stepRow=1，从第 1 名起就要点击 = 全程点击
    #   N=k+1    -> stepRow=k，前 k 名自动滚，从第 k+1 名起等点击
    #   不加参数  -> 全程自动
    if play_mode == "click":
        argv += ["--singleStep", "2"]
    elif play_mode == "auto-then-click":
        start_place = max(1, int(click_from_place or 1))
        argv += ["--singleStep", str(start_place + 1)]
    if abs(float(speed) - 1.0) > 1e-9:
        argv += ["--speed", f"{float(speed):g}"]
    if extra_args.strip():
        try:
            argv += shlex.split(extra_args, posix=False)
        except ValueError:
            argv += extra_args.split()
    return argv


def events_medals(package_dir: Path) -> int | None:
    """看比赛包里到底有没有奖项。

    返回 None 表示读不出来；返回 0 表示「一个奖都没有」——
    这种比赛包一启动 Resolver 就会 NPE 崩掉（见 run_package 的 docstring）。
    有自定义 ``<award>`` 时直接算作「有奖」（哪怕 <last-gold> 是 0）。
    """
    events = Path(package_dir) / "events.xml"
    if not events.is_file():
        return None
    try:
        text = events.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if re.search(r"<award\b", text):
        return 1
    match = re.search(r"<last-gold>\s*(\d+)\s*</last-gold>", text)
    if not match:
        return None
    return int(match.group(1))


def preflight_resolver(root: Path, package_dir: Path, font_path: str, sink: LogSink) -> str:
    """启动前的检查，返回错误说明（空字符串 = 一切就绪）。

    ``font_path`` 会被 :func:`board_fonts.resolve_font_path` 解析：为空或文件不存在时
    **自动回落到内置字体**（M13：界面已不再让用户设置字体）。
    """
    app = resolver_app_dir(root)
    if not (app / "lib").is_dir():
        return f"找不到 Resolver 程序：{app}\\lib 不存在。"
    if not list((app / "lib").glob("*.jar")):
        return f"{app}\\lib 里没有任何 jar，Resolver 发行包可能没解压完整。"
    if not find_java():
        return "PATH 里找不到 java。请安装 JDK/JRE 17 以上版本并加入 PATH。"
    events = Path(package_dir) / "events.xml"
    if not events.is_file():
        return f"比赛包里没有 events.xml：{events}。请先点「生成比赛包」。"
    if not fontsmod.resolve_font_path(root, font_path):
        return ("找不到可用的中文字体（内置字体 tools\\fonts\\MicrosoftYaHei.ttc 也不在）。\n"
                "请重装一份完整的程序 —— 字体文件被删掉时，滚榜的中文会变成方框。")
    if events_medals(package_dir) == 0:
        return (
            "这个比赛包里「一个奖都没有」，\n"
            "Resolver 启动时会抛 NullPointerException 直接崩掉。\n\n"
            "请到「播放与显示」页给「奖项设置」加至少一行（例如 一等奖 1 人），\n"
            "然后回「滚榜」页重新点一次「生成比赛包」。"
        )
    return ""


def start_resolver(
    root: Path,
    package_dir: Path,
    font_path: str,
    play_mode: str,
    speed: float,
    display_name: str,
    extra_args: str,
    sink: LogSink,
    click_from_place: int = 4,
) -> dict[str, Any]:
    """启动 Resolver，并自动设置 ICPC_FONT。

    ``font_path`` 为空/失效时**自动回落到内置字体**（M13：界面已删掉字体板块，
    用户没有任何要设置的东西）；配置里的老 ``font_path`` 只要文件还在也照样认。
    """
    problem = preflight_resolver(root, package_dir, font_path, sink)
    if problem:
        return {"ok": False, "message": problem}

    font_path = fontsmod.resolve_font_path(root, font_path)
    app = resolver_app_dir(root)
    argv = build_resolver_argv(root, package_dir, play_mode, speed, display_name, extra_args,
                               click_from_place)

    env = os.environ.copy()
    # 关键：ICPC_FONT 不设的话，Resolver 自带 Helvetica 会把中文全渲染成方框。
    env["ICPC_FONT"] = str(font_path)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = logs_dir() / f"resolver-{stamp}.log"
    try:
        handle = log_path.open("w", encoding="utf-8", errors="replace")
    except OSError:
        handle = None

    with LogRedirect(sink):
        print("-" * 62)
        print("  启动 ICPC Resolver")
        print("-" * 62)
        print(f"  工作目录  : {app}")
        print(f"  ICPC_FONT : {font_path}")
        _mode_text = {"click": "全程点击 (--singleStep 2)",
                      "auto-then-click": f"先自动后点击 (--singleStep {max(1, int(click_from_place or 1)) + 1}，"
                                         f"从第 {max(1, int(click_from_place or 1))} 名起等点击)",
                      "auto": "全程自动（不加 --singleStep）"}.get(play_mode, play_mode)
        print(f"  播放模式  : {_mode_text}")
        print(f"  速度      : {speed:g}x")
        print(f"  命令行    : {' '.join(_quote(a) for a in argv)}")
        print(f"  Resolver 日志: {log_path}")
        print("-" * 62)

    try:
        # cwd 必须是 tools\resolver\app —— resolver.bat 会把 logs\ 写到调用者
        # 当前目录，不 cd 进去日志就乱丢。
        proc = subprocess.Popen(
            argv,
            cwd=str(app),
            env=env,
            stdout=handle or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return {"ok": False, "message": f"启动 Resolver 失败：{exc}"}

    proc._pta_log = handle          # type: ignore[attr-defined]
    proc._pta_log_path = str(log_path)  # type: ignore[attr-defined]
    # M18：Resolver(java) 也登记进 <运行目录>\child-pids.json —— 应用被强杀时它会变孤儿，
    # 下次启动靠这份登记（命令行里必须出现包目录 + .jar）把它认出来收掉。
    _register_child(proc.pid, "resolver", argv, marker=[str(app)])
    return {"ok": True, "process": proc, "log": str(log_path), "argv": argv}


def _quote(text: str) -> str:
    return f'"{text}"' if " " in text else text


def wait_for_resolver_window(proc: "subprocess.Popen | None", timeout: float = 120.0,
                             sink: LogSink | None = None) -> list[tuple[int, int]]:
    """等 Resolver 的窗口出现。Esc 守护必须在窗口出现后再拉起来。

    ⚠️ 慢是正常的：Resolver 的 classpath 有二十多个 jar（含一个 23 MB 的
    sentry agent），实测本机从进程启动到「窗口出现」要 **21~23 秒**。
    所以默认超时给到 120 秒，中途会往日志里打进度，免得用户以为卡死了。
    """
    pid = proc.pid if proc is not None else None
    deadline = time.time() + max(5.0, float(timeout))
    next_note = time.time() + 5.0
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            if sink:
                sink.write(f"\n[错误] Resolver 进程已退出（退出码 {proc.returncode}）。"
                           f"请看日志 {getattr(proc, '_pta_log_path', '')}\n")
            return []
        wins = find_java_windows(pid)
        if wins:
            if sink:
                sink.write(f"[OK] Resolver 窗口已出现（{len(wins)} 个），用了 "
                           f"{time.time() - (deadline - max(5.0, float(timeout))):.0f} 秒。\n")
            return wins
        if sink and time.time() >= next_note:
            waited = time.time() - (deadline - max(5.0, float(timeout)))
            sink.write(f"[i] 已等待 {waited:.0f} 秒，Resolver 还在启动"
                       f"（本机实测要 20 秒以上，请稍等）…\n")
            next_note = time.time() + 10.0
        time.sleep(0.25)
    if sink:
        sink.write("[错误] 等 Resolver 窗口超时了。\n")
    return []


def start_esc_watchdog(script_path: Path, resolver_pid: int | None = None,
                       grace: float = 2.5) -> dict[str, Any]:
    """拉起 tools\\esc_quit.py 的 Esc 退出守护（不需要用户再去点 esc-on.bat）。

    ``resolver_pid`` 会以 ``--pid`` 传给守护：这样**即使那一刻枚举不到 Resolver
    窗口**（刚启动还没画出来、SWT 切换画面、滚完榜切最终画面 …），守护也能按
    PID 直接结束，并且 Resolver 自己退出后守护会自行收工。

    M13 起应用**在 Resolver 进程一启动就拉起守护**（不再等窗口出现），
    所以"刚启动 / 滚榜中 / 滚完"三个阶段按 Esc 都能退出。
    """
    extra: list[str] = []
    if resolver_pid:
        extra += ["--pid", str(int(resolver_pid)), "--grace", f"{max(0.0, float(grace)):g}"]
    if cfgmod.is_frozen():
        argv = [sys.executable, "--esc-watchdog", *extra]
    else:
        argv = [sys.executable, str(script_path), "--esc-watchdog", *extra]

    log_path = logs_dir() / "esc-watchdog.log"
    try:
        handle = log_path.open("w", encoding="utf-8", errors="replace")
    except OSError:
        handle = None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(script_path.parent),
            stdout=handle or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return {"ok": False, "message": f"拉起 Esc 退出守护失败：{exc}"}
    proc._pta_log = handle          # type: ignore[attr-defined]
    _register_child(proc.pid, "esc-watchdog", argv, marker=[str(script_path)])
    return {"ok": True, "process": proc, "log": str(log_path)}


def _register_child(pid: int, kind: str, argv: Any = None,
                    marker: Any = None) -> None:
    """把子进程登记进运行目录的 child-pids.json（M18；失败也不影响主流程）。"""
    if child_registry is None:
        return
    try:
        run = cfgmod.work_root() / "live"
        child_registry.record(run, pid, kind, argv, marker=[str(run), *(marker or [])])
    except Exception:  # noqa: BLE001
        pass


def _pid_alive(pid: int | None) -> bool:
    """进程是否还活着（SYNCHRONIZE 句柄 + 0 超时等待，不需要管理员）。"""
    if not pid:
        return False
    try:
        handle = _kernel32.OpenProcess(0x00100000, False, int(pid))   # SYNCHRONIZE
    except Exception:  # noqa: BLE001
        return False
    if not handle:
        return False
    try:
        return _kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
    finally:
        _kernel32.CloseHandle(handle)


def _kill_tree(pid: int) -> bool:
    """``taskkill /F /T``：强杀进程及其子进程。"""
    try:
        done = subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                              capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)
        return done.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def stop_resolver(proc: "subprocess.Popen | None", sink: LogSink, grace: float = 5.0) -> str:
    """**一定能停**：先 WM_CLOSE 温和请求，``grace`` 秒后 taskkill /F /T 强杀并复查。

    M13 起的行为（用户要求「停止滚榜按钮必须一定能停」）：

    1. 收集目标 PID：优先用 Resolver 自己的 PID（``proc.pid``），
       窗口枚举只是**补充**——枚举不到窗口照样按 PID 杀；
    2. 温和：向所有 Resolver 窗口发 ``WM_CLOSE``，等 ``grace`` 秒（默认 5 秒）；
    3. 强杀：``taskkill /F /T`` 结束每个 PID（**含子进程**），再等最多 5 秒复查；
    4. 清理：关掉日志句柄、``proc.wait()`` 回收僵尸句柄，并在日志里如实报告
       还有没有残留 PID（有就说清楚，不假装成功）。
    """
    known: set[int] = set()
    if proc is not None and proc.pid:
        known.add(int(proc.pid))
    targets = find_java_windows(proc.pid if proc is not None else None)
    if not targets and proc is None:
        targets = find_java_windows()
    known.update(pid for _hwnd, pid in targets)
    known = {p for p in known if _pid_alive(p)}

    if not known:
        _close_log(proc)
        return "没有正在运行的 Resolver。"

    pids = sorted(known)
    for hwnd, _pid in targets:
        post_close(hwnd)

    with LogRedirect(sink):
        if targets:
            print(f"[i] 已向 {len(targets)} 个 Resolver 窗口发送关闭请求，"
                  f"等它自己退出（最多 {grace:g} 秒）…")
        else:
            print(f"[i] 没枚举到 Resolver 窗口，直接按 PID 结束：{pids}")

    deadline = time.time() + max(0.0, float(grace))
    while time.time() < deadline:
        if not any(_pid_alive(p) for p in pids) and not find_java_windows(
                proc.pid if proc is not None else None):
            _close_log(proc)
            _reap(proc)
            return "滚榜已正常退出。"
        time.sleep(0.25)

    killed = sum(1 for pid in pids if _kill_tree(pid))
    if proc is not None and proc.poll() is None:
        _kill_tree(int(proc.pid))            # 兜底：可能刚 fork 出子进程
    for _ in range(25):                      # 最多再等 5 秒等它真的消失
        if not any(_pid_alive(p) for p in pids):
            break
        time.sleep(0.2)

    left = [p for p in pids if _pid_alive(p)]
    leftover_windows = find_java_windows(proc.pid if proc is not None else None)
    _close_log(proc)
    _reap(proc)
    for pid in pids:                       # M18：停掉的 Resolver 从登记表里划掉
        if not _pid_alive(pid):
            _forget_child(pid)
    with LogRedirect(sink):
        print(f"[i] 强制结束 {killed} 个进程；复查：残留 PID {left or '无'}、"
              f"残留窗口 {len(leftover_windows)} 个。")
    if left or leftover_windows:
        return (f"⚠ 还有残留没清干净（PID {left}，窗口 {len(leftover_windows)} 个）——"
                f"再点一次「停止滚榜」。")
    return f"窗口没响应，已强制结束 {killed} 个进程。"


def _forget_child(pid: int) -> None:
    """子进程正常结束后从 child-pids.json 划掉（M18）。"""
    if child_registry is None:
        return
    try:
        child_registry.forget(cfgmod.work_root() / "live", pid)
    except Exception:  # noqa: BLE001
        pass


def _reap(proc: "subprocess.Popen | None") -> None:
    """回收子进程句柄，避免留下僵尸/半死进程影响下一次启动。"""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.wait(timeout=3)
    except Exception:  # noqa: BLE001
        pass


def stop_process(proc: "subprocess.Popen | None") -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)
    except Exception:  # noqa: BLE001
        pass
    _forget_child(int(getattr(proc, "pid", 0) or 0))
    _close_log(proc)


def _close_log(proc: "subprocess.Popen | None") -> None:
    handle = getattr(proc, "_pta_log", None) if proc is not None else None
    if handle is not None:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass


def resolver_running(proc: "subprocess.Popen | None") -> bool:
    if find_java_windows(proc.pid if proc is not None else None):
        return True
    return proc is not None and proc.poll() is None
