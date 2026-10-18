# -*- coding: utf-8 -*-
"""child_registry.py —— 应用自己起的子进程**登记 + 清理**（M18）。

主控实测到的真问题：一个 01:36 启动的 `poller.py` 一直活到 03:36（后台持续请求 PTA）。
原因是应用退出时没把自己的子进程带走（应用被强杀/异常退出时子进程还会变孤儿）。

这个模块做两件事：

* :func:`record` / :func:`forget` —— 每启动一个子进程（`server.py` / `poller.py` /
  Resolver 的 `java` / Esc 守护），把 ``PID + 启动时间 + 命令行摘要 + 判定标记``
  写进 ``<运行目录>\\child-pids.json``；
* :func:`cleanup`（退出时）/ :func:`reap`（启动时）—— 按记录**逐个核对是不是我们的进程**
  （命令行里必须出现运行目录 / 包目录 + 对应脚本名），是就先温和（``taskkill`` 不带 ``/F``，
  等于 WM_CLOSE）、5 秒不退再 ``taskkill /F /T /PID``，最后按 PID 复核；
  **对不上的一律不动**（PID 可能被系统复用成别的程序 —— 绝不能误杀）。

本模块**不 import 同目录的其它模块**（只依赖标准库），避免和 `live_backend` 形成循环导入。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CHILD_FILE = "child-pids.json"
#: 我们自己会起的子进程种类 → 该种类命令行里必须出现的关键词 / 可执行名
KIND_HINTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # kind: (命令行关键词（任一命中即可）, 可执行名关键词（任一命中即可）)
    "server": (("server.py",), ("python",)),
    "poller": (("poller.py",), ("python",)),
    "resolver": ((".jar", "resolver"), ("java",)),
    "esc-watchdog": (("esc_quit.py", "--esc-watchdog"), ("python",)),
    "mock": (("mock_source.py",), ("python",)),
}
#: 命令行里出现这些就一定是"我们这套东西"（双保险用；正常靠 marker）
OUR_MARKERS = ("pta-resolver", "pta-assistant", "live-board", "实时榜")


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def registry_path(run: Path) -> Path:
    return Path(run) / CHILD_FILE


def _run(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              creationflags=_no_window())
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- 记录

def load(run: Path) -> list[dict[str, Any]]:
    """读登记表（读不到/坏了都当空表，绝不抛）。"""
    path = registry_path(run)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(doc, dict):
        doc = doc.get("children")
    if not isinstance(doc, list):
        return []
    out: list[dict[str, Any]] = []
    for item in doc:
        if isinstance(item, dict) and int(item.get("pid") or 0) > 0:
            out.append(item)
    return out


def save(run: Path, entries: list[dict[str, Any]]) -> None:
    path = registry_path(run)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(CHILD_FILE + ".tmp")
        tmp.write_text(json.dumps({"children": entries}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def record(run: Path, pid: int, kind: str, argv: Any = None, marker: Any = None) -> None:
    """登记一个子进程（同一个 PID 覆盖旧记录）。"""
    pid = int(pid or 0)
    if pid <= 0:
        return
    argv_list = [str(a) for a in (argv or [])]
    summary = " ".join(argv_list)[:400]
    markers = [str(m) for m in (marker or []) if str(m).strip()]
    if not markers:
        markers = [str(Path(run))]           # 默认用运行目录当标记
    entries = [e for e in load(run) if int(e.get("pid") or 0) != pid]
    entries.append({
        "pid": pid, "kind": str(kind or "child"),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "started_ts": time.time(),
        "markers": markers, "cmdline": summary,
    })
    save(run, entries)


def forget(run: Path, pid: int) -> None:
    """子进程正常结束时把它从登记表里划掉。"""
    pid = int(pid or 0)
    if pid <= 0:
        return
    entries = load(run)
    left = [e for e in entries if int(e.get("pid") or 0) != pid]
    if len(left) != len(entries):
        save(run, left)


# ---------------------------------------------------------------- 判定 / 结束

def pid_alive(pid: int) -> bool:
    """进程还在吗（OpenProcess 权威判断；非 Windows 退回 tasklist）。"""
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259                        # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        out = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        return str(pid) in out


def process_exe(pid: int) -> str:
    """进程可执行文件名（取不到返回空串）。"""
    out = _run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"]).strip()
    first = out.split()[0] if out else ""
    return first if first and not first.startswith("信息") else first


def command_line(pid: int) -> str:
    """进程完整命令行。

    优先 ``wmic``（Win10 自带、快）；没有就退 ``PowerShell`` CIM；都拿不到返回空串
    （**空串 = 判定不了 → 调用方必须"不动它"**）。
    """
    pid = int(pid or 0)
    if pid <= 0:
        return ""
    out = _run(["wmic", "process", "where", f"processid={pid}", "get", "commandline",
                "/value"], timeout=12)
    for line in out.splitlines():
        line = line.strip()
        if line.lower().startswith("commandline="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    # 退回 PowerShell CIM（wmic 在新系统上可能已被移除）
    ps = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
               f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
              timeout=20)
    value = ps.strip()
    return "" if value.lower().startswith("get-ciminstance") else value


def _norm(text: str) -> str:
    return str(text or "").replace("/", "\\").lower()


def is_ours(entry: dict[str, Any], marker_hint: str = "") -> tuple[bool, str]:
    """这条记录对应的进程**现在**还是不是我们的？返回 (是/否, 说明)。

    判据（全部满足才算"是"）：

    * PID 还活着（否则返回 True：已经没了，什么都不用做）；
    * 命令行读得到；
    * 命令行里出现记录里的 ``markers``（运行目录 / 包目录 / jar 路径）**或**兜底关键词；
    * 命令行里出现该 kind 的关键词（``server.py`` / ``poller.py`` / ``.jar`` …），
      且可执行名匹配（python / java）。
    """
    pid = int(entry.get("pid") or 0)
    if not pid_alive(pid):
        return True, "已经不在"
    kind = str(entry.get("kind") or "")
    cmd = _norm(command_line(pid))
    if not cmd:
        return False, "读不到命令行 → 不确认，绝不动它"
    need_cmds, need_exes = KIND_HINTS.get(kind, ((), ()))
    markers = [_norm(m) for m in (entry.get("markers") or []) if str(m).strip()]
    if marker_hint:
        markers.append(_norm(marker_hint))
    markers = [m for m in markers if len(m) > 3]
    hit_marker = [m for m in markers if m in cmd]
    hit_any = [m for m in OUR_MARKERS if _norm(m) in cmd]
    if not hit_marker and not hit_any:
        return False, f"命令行里没有我们的路径（markers={markers[:2]}）→ 不动"
    if need_cmds and not any(_norm(c) in cmd for c in need_cmds):
        return False, f"命令行里没有 {kind} 的特征（{need_cmds}）→ 不动"
    exe = _norm(process_exe(pid))
    if need_exes and exe and not any(_norm(e) in exe for e in need_exes):
        return False, f"可执行名 {exe} 与 {kind} 不符 → 不动"
    how = hit_marker[0] if hit_marker else hit_any[0]
    return True, f"确认是我们的 {kind}（命中 {how}）"


def terminate(pid: int, gentle_timeout: float = 5.0) -> tuple[bool, str]:
    """先温和、再强杀，最后复核。返回 (是否已消失, 说明)。

    * 温和：``taskkill /PID <pid> /T``（**不带 /F** = WM_CLOSE，让它自己收尾）；
    * ``gentle_timeout`` 秒还不退 → ``taskkill /PID <pid> /T /F``；
    * 全程按 PID 复核（``pid_alive``）。
    """
    pid = int(pid or 0)
    if pid <= 0:
        return True, "没有 PID"
    if not pid_alive(pid):
        return True, "本来就不在"
    gentle = _run(["taskkill", "/PID", str(pid), "/T"], timeout=15)
    deadline = time.time() + max(0.0, float(gentle_timeout))
    while time.time() < deadline:
        if not pid_alive(pid):
            return True, "温和结束时已退出"
        time.sleep(0.2)
    forced = _run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not pid_alive(pid):
            return True, "强制结束时已退出"
        time.sleep(0.2)
    note = (forced or gentle).strip().splitlines()
    return False, f"仍在运行（{note[-1][:80] if note else '需要管理员权限'}）"


def _sweep(run: Path, phase: str, log=None, gentle_timeout: float = 5.0) -> dict[str, Any]:
    """按登记表扫一遍：核对我们自己的进程 → 结束 → 清记录。"""
    run = Path(run)
    entries = load(run)
    killed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    notes: list[str] = []
    for entry in entries:
        pid, kind = int(entry.get("pid") or 0), str(entry.get("kind") or "child")
        ours, why = is_ours(entry, marker_hint=str(run))
        if not ours:
            skipped.append({"pid": pid, "kind": kind, "why": why})
            notes.append(f"跳过 PID {pid}（{kind}）：{why}")
            continue
        if not pid_alive(pid):
            skipped.append({"pid": pid, "kind": kind, "why": "已经不在"})
            continue
        ok, how = terminate(pid, gentle_timeout=gentle_timeout)
        killed.append({"pid": pid, "kind": kind, "gone": bool(ok), "how": how})
        notes.append(f"{'结束' if ok else '⚠ 未能结束'} PID {pid}（{kind}）：{how}")
    left = [e for e in load(run)
            if not any(int(e.get("pid") or 0) == int(k["pid"]) for k in killed)]
    save(run, left)
    result = {"phase": phase, "run": str(run), "killed": killed, "skipped": skipped,
              "notes": notes, "count": len([k for k in killed if k["gone"]]),
              "left": [e.get("pid") for e in left]}
    if log is not None:
        for note in notes:
            try:
                log(note)
            except Exception:  # noqa: BLE001
                pass
    return result


def cleanup(run: Path, log=None, gentle_timeout: float = 5.0) -> dict[str, Any]:
    """**退出时**用：把我们起的子进程全部收掉（温和 → 5 秒 → 强杀）。"""
    result = _sweep(run, "exit", log=log, gentle_timeout=gentle_timeout)
    if log is not None:
        try:
            log(f"已清理 {result['count']} 个子进程"
                + (f"，跳过 {len(result['skipped'])} 个（不是我们的）"
                   if result["skipped"] else ""))
        except Exception:  # noqa: BLE001
            pass
    return result


def reap(run: Path, log=None, gentle_timeout: float = 5.0) -> dict[str, Any]:
    """**启动时**用：清掉上一轮留下的残留（应用被强杀时子进程会变孤儿）。

    只动"确认是我们自己的"（命令行里带着我们的运行目录 + 脚本名）；对不上的一律不碰。
    """
    result = _sweep(run, "startup", log=log, gentle_timeout=gentle_timeout)
    if log is not None:
        kinds = "、".join(sorted({str(k["kind"]) for k in result["killed"] if k["gone"]})) or "无"
        try:
            log(f"启动清理：结束 {result['count']} 个上一轮残留的子进程（{kinds}）"
                + (f"；跳过 {len(result['skipped'])} 个（不是我们的）"
                   if result["skipped"] else ""))
        except Exception:  # noqa: BLE001
            pass
    return result


def orphan_hint(run: Path) -> str:
    """给日志/界面用的一句话：还有几个登记在册的活着的子进程。"""
    alive = [e for e in load(run) if pid_alive(int(e.get("pid") or 0))]
    return f"登记在册且仍在运行：{len(alive)} 个" if alive else "没有登记在册的子进程"


if __name__ == "__main__":                      # 手动排查：python child_registry.py <run目录>
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    print(json.dumps({"registry": registry_path(target), "entries": load(target),
                      "hint": orphan_hint(target)}, ensure_ascii=False, indent=2))
