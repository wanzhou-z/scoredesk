# -*- coding: utf-8 -*-
"""标准库本地 HTTP/SSE 服务：M2 增加配置、校徽和字体静态端点。"""
from __future__ import annotations
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
BASE = Path(__file__).resolve().parent
STATE = BASE / "state.json"
HTML = BASE / "web" / "board.html"
CONFIG_PATH = BASE / "config.json"
sys.dont_write_bytecode = True
import presentation
from presentation import load_config, metadata, board_view, resolve_freeze_mode
from contest_api import ContestAPI, is_route
API = None

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def read_config():
    return load_config()

#: 降级/空态也要返回**结构完整**的一份 state（前端按字段渲染，不许缺键）
EMPTY_STATE = {"ok": False, "last_poll": None, "channel": None, "teams": 0,
               "submissions": 0, "board": [], "problems": [], "frozen": False,
               "freeze_at": None, "event": {"type": "waiting"}}

def read_state():
    """读 state.json：坏/缺都不抛，返回结构完整的空态（降级运行）。"""
    state = read_json(STATE, None)
    if not isinstance(state, dict):
        return dict(EMPTY_STATE)
    for key, value in EMPTY_STATE.items():
        state.setdefault(key, value)
    return state

def data_status():
    """数据文件健康：正常 None；有问题时给人看的一句话。"""
    try:
        return presentation.data_error()
    except Exception:                                  # noqa: BLE001
        return None

def data_error_detail():
    try:
        return list(presentation.DATA_ERRORS)
    except Exception:                                  # noqa: BLE001
        return []

#: 取数通道**恒为 http**：2026-10-13 起删除了「借用浏览器」通道，
#: 全产品只有「直连接口 + 有效 Cookie」这一条取数路径。
CHANNEL = "http"
COOKIE_CANDIDATES = (BASE / "cookie.txt", Path(r"F:\PTA-Board\cookie.txt"))
#: M14 强制全量刷新旗标（与 poller.py 里同一个文件）：界面按钮 / ``?refresh=full``
#: 写它 -> poller 下一轮做一次「不限页数 + 刷新队伍名录」的完整全量，成功后删除。
FORCE_FULL = BASE / "tmp" / "force-full.flag"


def request_force_full() -> bool:
    """落一个「请求全量」旗标；返回是否**本次新写入**（重复点按钮不算新的请求）。"""
    fresh = not FORCE_FULL.is_file()
    try:
        FORCE_FULL.parent.mkdir(parents=True, exist_ok=True)
        FORCE_FULL.write_text(json.dumps({"requested_at": __import__("time").strftime(
            "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())}, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"[warn] 写强制全量旗标失败：{exc}", flush=True)
        return False
    return fresh


def force_full_pending() -> bool:
    return FORCE_FULL.is_file()


#: M14 数据完整性：/health 与 /api/board 共用的那几个键（只增不改已有键）。
COMPLETENESS_KEYS = ("teams_expected", "teams_active", "teams_roster", "teams_missing_from_roster",
                     "data_incomplete", "data_note", "fetch_report")


def completeness(state) -> dict:
    """从 state 里取「数据全不全」的一组键；旧 state 缺键时按 board 行数兜底。"""
    board_n = len(state.get("board") or [])
    out = {key: state.get(key) for key in COMPLETENESS_KEYS}
    if out.get("teams_expected") is None:
        out["teams_expected"] = board_n
    if out.get("data_incomplete") is None:
        out["data_incomplete"] = board_n < int(out["teams_expected"])
    return out


def cookie_error():
    """凭证是否可用：``None`` = 有（看起来可用）；字符串 = 给人看的原因。

    优先用 poller 写进 state.json 的 ``cookie_error``（运行中失效那种情况），
    没有就自己看一眼 cookie.txt 在不在 —— 这样**没有启动 poller** 时
    /health 也能说清「为什么现在取不到数」。
    """
    # VJudge 平台：公开比赛匿名取数，**不需要 Cookie** —— 别报「Cookie 缺失」。
    try:
        platform = str((read_config() or {}).get("platform") or "").strip().lower()
    except Exception:                                  # noqa: BLE001
        platform = ""
    if platform == "vjudge":
        return None
    msg = read_state().get("cookie_error")
    if msg:
        return str(msg)
    for path in COOKIE_CANDIDATES:
        try:
            if path.read_text(encoding="utf-8", errors="replace").strip():
                return None
        except OSError:
            continue
    return ("没有找到可用的 cookie.txt —— 实时榜单必须提供有效 Cookie（直连接口）；"
            "请到「1 数据源」页点「粘贴 cURL 自动生成」更新 Cookie")

class _LazyLogoStore:
    """延迟到第一次用到才建校徽缓存。

    以前是导入期就 ``LogoStore()``：它要读 ``config.json`` / ``contest.json``，
    数据一坏（2026-10-11 蓝屏事故）整个 server **崩在 import**，连降级都来不及。
    现在坏文件只会让 ``/api/logos`` 报错，``/health``、``/api/board`` 照常工作。
    """
    def __init__(self):
        self._store = None
        self._failed = ""

    def _get(self):
        if self._store is None and not self._failed:
            try:
                self._store = presentation.LogoStore()
            except Exception as exc:                   # noqa: BLE001
                self._failed = f"{exc.__class__.__name__}: {exc}"
        return self._store

    @property
    def report(self):
        store = self._get()
        if store is None:
            return {"logos_dir": "", "default": "", "matched": [], "default_teams": [],
                    "unused": [], "errors": [f"校徽缓存不可用：{self._failed}"]}
        return store.report

    def path(self, key):
        store = self._get()
        return store.path(key) if store is not None else None

    def path_specific(self, key):
        """这一支队专属的校徽（匹配不到返回 None）；老版本 LogoStore 没有该方法时返回 None。"""
        store = self._get()
        getter = getattr(store, "path_specific", None) if store is not None else None
        return getter(key) if callable(getter) else None

LOGOS = _LazyLogoStore()

class Handler(BaseHTTPRequestHandler):
    server_version = "PTALiveBoard/2.0"
    def log_message(self, *args):
        return
    def send_bytes(self, data, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        # M11 裁判开关：?thaw=1（或 ?freeze=operator）才揭示真实成绩；默认永不解榜。
        thaw = str(q.get("thaw", ["0"])[0]).strip() in ("1", "true", "yes", "on")
        if is_route(u.path):
            API.handle(self, u, q); return
        try: mode=resolve_freeze_mode(q.get("freeze", [None])[0], thaw)
        except ValueError as exc: self.send_bytes(json.dumps({"error":str(exc)}, ensure_ascii=False).encode(), "application/json; charset=utf-8", 400); return
        if u.path == "/health":
            s = read_state(); out = {k: s.get(k) for k in ("ok", "last_poll", "channel", "teams", "submissions")}
            # 取数通道：PTA 恒为 http；VJudge 轮次 poller 写的是 vjudge（没有 Cookie 这条）。
            out["channel"] = s.get("channel") or CHANNEL
            out["cookie_error"] = cookie_error()
            # 数据文件有问题时把原因摊开给人看；正常时为 null（GUI 和人都能一眼看到降级运行）。
            out["data_error"] = data_status(); out["data_error_detail"] = data_error_detail()
            out["repairs"] = list(getattr(presentation, "REPAIRS", []))
            # M14：拉取全不全（teams 与 /api/board 的行数同源，teams_expected 是"应有"的行数）。
            out.update(completeness(s))
            out["force_full_pending"] = force_full_pending()
            self.send_bytes(json.dumps(out, ensure_ascii=False).encode(), "application/json; charset=utf-8"); return
        if u.path == "/api/config":
            cfg = read_config(); view = dict(cfg.get("view", {}) or {})
            # M17 追加（只增不改）：awards_str —— 与 view.awards 同结构，但 team_ids 一律转成**字符串**。
            #   奖项名单里的队伍 id 是 19 位整数，浏览器把 JSON 数字解析成 Number 会丢精度
            #   （1900000000000007919 -> 1900000000000008000），于是名单匹配不上 —— 主控复核 Bug A。
            #   前端优先用 awards_str；老字段 view.awards 原样保留（GUI 的配置页仍照旧读它）。
            try:
                view["awards_str"] = [
                    {**(a or {}), "team_ids": [str(x) for x in ((a or {}).get("team_ids") or [])]}
                    for a in (view.get("awards") or [])
                ]
            except Exception:                                   # noqa: BLE001 - 配置坏了也不能让 /api/config 崩
                view["awards_str"] = []
            # M26：标题优先级 = 用户自己填的 view.title（非空）→ 旧的顶层 title。
            #   GUI「直播」页填的标题会写进 live.view.title；以前这里只回顶层 title，
            #   前端又优先用 PTA 题目集名字，于是那个输入框等于没用（用户反馈 2026-10-17）。
            view_title = view.get("title")
            view_title = view_title.strip() if isinstance(view_title, str) else ""
            out = {"title": (view_title or cfg.get("title") or ""), "font": "/font/microsoft.ttc", "theme": cfg.get("theme") or {}, "view": view, "port": cfg.get("port", 8765), "freeze_mode":cfg.get("freeze_mode", "operator")}; self.send_bytes(json.dumps(out, ensure_ascii=False).encode(), "application/json; charset=utf-8"); return
        if u.path == "/api/presentation":
            self.send_bytes(json.dumps(metadata(read_state(), mode, thaw), ensure_ascii=False).encode(), "application/json; charset=utf-8"); return
        if u.path == "/api/logos":
            self.send_bytes(json.dumps(LOGOS.report, ensure_ascii=False).encode(), "application/json; charset=utf-8"); return
        if u.path == "/api/seek":
            # M23 时间轴：按任意比赛时刻（秒）重建只读展示快照，供 ?timebar=1 拖动 seek。
            # 不写 state.json、不改 poller / 排名 / 封榜逻辑；口径与 replay.py 相同。
            try: at = float(q.get("at", ["0"])[0] or 0)
            except ValueError: at = 0.0
            s, info = presentation.seek_view(at, mode, thaw)
            payload = {"last_poll": s.get("last_poll"), "channel": s.get("channel"), "frozen": s.get("frozen"),
                       "freeze_at": s.get("freeze_at"), "event": s.get("event"), "problems": s.get("problems", []),
                       "board": s.get("board", []), "presentation": info,
                       # M41：回放到某一刻时，悬停卡片也只看到那一刻之前的提交
                       "subs_by_team": s.get("subs_by_team") or {},
                       "problem_labels": s.get("problem_labels") or {}}
            payload.update(completeness(s))
            payload["seek_at"] = at
            self.send_bytes(json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8"); return
        if u.path == "/api/board":
            s, _ = board_view(read_state(), mode, thaw); top = int(q.get("top", ["0"])[0] or 0); board = s.get("board", [])[:top] if top else s.get("board", [])
            # 降级运行时也要是**结构完整**的 JSON（空榜单），不要 500、不要崩。
            # M11 新增键（只增不改）：freeze_mode/thaw/contest_over/freeze_snapshot_at/frozen_cells。
            out = {"updated": s.get("last_poll"), "channel": s.get("channel"), "frozen": s.get("frozen", False), "freeze_at": s.get("freeze_at"), "problems": s.get("problems", []), "board": board, "teams": s.get("teams", len(board)), "data_error": data_status(),
                   "freeze_mode": mode, "thaw": bool(thaw), "contest_over": s.get("contest_over", False),
                   "freeze_snapshot_at": s.get("freeze_snapshot_at") if s.get("frozen") else None,
                   "frozen_cells": list(s.get("frozen_cells") or []) if s.get("frozen") else [],
                   # M41（用户 2026-10-18）：悬停信息卡用的逐题提交记录（只读，缺了就是空）
                   "subs_by_team": s.get("subs_by_team") or {},
                   "problem_labels": s.get("problem_labels") or {}}
            # M14：数据完整性（只增不改已有键）。`?refresh=full` 顺便落一个强制全量旗标。
            out.update(completeness(s))
            if str(q.get("refresh", [""])[0]).lower() == "full":
                out["full_refresh_requested"] = request_force_full()
            self.send_bytes(json.dumps(out, ensure_ascii=False).encode(), "application/json; charset=utf-8"); return
        if u.path == "/api/refresh-full":
            # 界面按钮 / ?refresh=full 的后端：只落旗标，真正的全量由 poller 下一轮执行。
            queued = request_force_full()
            out = {"ok": True, "queued": queued, "pending": force_full_pending(),
                   "flag": str(FORCE_FULL), "note": "poller 下一轮做完整全量（不限页数 + 刷新队伍名录）"}
            self.send_bytes(json.dumps(out, ensure_ascii=False).encode(), "application/json; charset=utf-8"); return
        if u.path in ("/web/board.css", "/web/board.js"):
            file = BASE / u.path.lstrip("/")
            ctype = "text/css" if file.suffix == ".css" else "text/javascript"
            self.send_bytes(file.read_bytes(), ctype + "; charset=utf-8"); return
        if u.path.startswith("/web/fonts/") and u.path.endswith((".woff2", ".woff")):
            # M17 追加：内置的参照榜字体（web/fonts/live_display|csg_rank/*.woff2，SIL OFL 1.1）
            # 离线可用：只从 BASE/web/fonts 下读，挡掉 .. 之类的越界路径。
            rel = Path(unquote(u.path).lstrip("/")).relative_to("web/fonts")
            root = (BASE / "web" / "fonts").resolve()
            path = (root / rel).resolve()
            if path.is_file() and str(path).startswith(str(root)):
                self.send_bytes(path.read_bytes(), "font/woff2", extra={"Cache-Control": "public, max-age=86400"})
            else:
                self.send_error(404)
            return
        if u.path in ("/board", "/"):
            try: self.send_bytes(HTML.read_bytes(), "text/html; charset=utf-8")
            except OSError: self.send_error(404)
            return
        if u.path.startswith("/logo/"):
            key = unquote(u.path.rsplit("/", 1)[-1]).removesuffix(".png")
            # 校徽优先级（VJudge 版）：
            #   ① 备注库指定的校徽 / ② 备注姓名或学号匹配到校徽目录（都在 LogoStore 里）
            #   → ③ 抓取器缓存的 VJudge 头像 live\logos\<uid>.png（原图直接给，前端 .logo 已裁圆）
            #   → ④ 默认校徽
            path = LOGOS.path_specific(key)
            if path and path.is_file():
                self.send_bytes(path.read_bytes(), "image/png", extra={"Cache-Control": "public, max-age=300"})
                return
            #   → ③ VJudge 头像缓存 live\logos\<uid>.png（用多域名回退下载，见 vjudge_fetch）
            safe = bool(key) and key not in (".", "..") and "/" not in key and "\\" not in key
            if safe:
                try:
                    raw = BASE / "logos" / (key + ".png")
                    if raw.is_file() and raw.stat().st_size > 0:
                        self.send_bytes(raw.read_bytes(), "image/png",
                                        extra={"Cache-Control": "public, max-age=120"})
                        return
                except OSError:
                    pass
            path = LOGOS.path(key)
            if path and path.is_file(): self.send_bytes(path.read_bytes(), "image/png", extra={"Cache-Control": "public, max-age=300"})
            else: self.send_error(404)
            return
        if u.path == "/font/microsoft.ttc":
            # M13（4c）：不再写死本机路径。按顺序找内置微软雅黑：
            #   ① 配置里的 font_path（用户从界面上选过）
            #   ② live-board 同级 / 上级的 tools\fonts（打包后 = _internal\tools\fonts）
            #   ③ 仓库形状（..\pta-resolver\tools\fonts）
            #   ④ 环境变量 PTA_FONT_PATH
            cfg_font = read_config().get("font_path")
            cands = [Path(cfg_font)] if cfg_font else []
            env_font = os.environ.get("PTA_FONT_PATH")
            if env_font:
                cands.append(Path(env_font))
            cands += [BASE.parent / "tools" / "fonts" / "MicrosoftYaHei.ttc",
                      BASE.parent / "pta-resolver" / "tools" / "fonts" / "MicrosoftYaHei.ttc"]
            path = next((p for p in cands if p.is_file()), cands[0])
            if path.is_file(): self.send_bytes(path.read_bytes(), "font/ttf", extra={"Cache-Control": "public, max-age=86400"})
            else: self.send_error(404)
            return
        if u.path == "/events": self.sse(mode); return
        self.send_error(404)
    def do_OPTIONS(self):
        # 预检无需认证；只读 API 不允许浏览器写入数据。
        self.send_bytes(b"", "text/plain", 204, {"Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "Authorization, Accept, Content-Type", "Access-Control-Max-Age": "600"})
    def sse(self, mode):
        self.send_response(200); self.send_header("Content-Type", "text/event-stream; charset=utf-8"); self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "keep-alive"); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
        # SSE 断线后本次连接结束，不再尝试从已中止的套接字读取下一条请求。
        self.close_connection=True
        last_mtime = 0
        heartbeat = time.monotonic()
        try:
            self.wfile.write(b": connected\n\n"); self.wfile.flush()
            while True:
                try: mt = STATE.stat().st_mtime_ns
                except OSError: mt = 0
                if mt > last_mtime:
                    s, info = board_view(read_state(), mode); payload = {"last_poll": s.get("last_poll"), "channel": s.get("channel"), "frozen": s.get("frozen"), "freeze_at": s.get("freeze_at"), "event": s.get("event"), "problems": s.get("problems", []), "board": s.get("board", []), "presentation": info,
                               # M41：悬停信息卡的数据也走 SSE（与 /api/board 同源）
                               "subs_by_team": s.get("subs_by_team") or {},
                               "problem_labels": s.get("problem_labels") or {}}
                    # M14：SSE 也带数据完整性（页面上的"数据可能不全（N / 期望 M）"用它）。
                    payload.update(completeness(s))
                    self.wfile.write(("event: update\ndata: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")); self.wfile.flush(); last_mtime = mt
                elif time.monotonic() - heartbeat >= 15:
                    self.wfile.write(b": ping\n\n"); self.wfile.flush()
                    heartbeat = time.monotonic()
                time.sleep(0.1)
        # Windows 客户端主动关闭 SSE 时也可能报告连接中止，属于正常断线。
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

def main():
    global STATE, API
    cfg = read_config(); ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=int(cfg.get("port", 8765))); ap.add_argument("--host", default=cfg.get("host", "127.0.0.1")); ap.add_argument("--state", type=Path, default=STATE); ap.add_argument("--contest", type=Path, default=BASE / "contest.json"); ap.add_argument("--api-config", type=Path, help="仅 Contest API 的隔离配置，供认证验收"); a = ap.parse_args(); STATE=a.state; API=ContestAPI(a.contest, STATE, a.api_config)
    # 让读侧（presentation.current_contest）跟着 --contest 走，并对这个文件做启动自愈：
    # 坏 contest.json 用 .bak 恢复；坏/缺 state.json 从 contest.json 重建。
    os.environ["LIVE_BOARD_CONTEST"] = str(a.contest)
    from board_core import repair_data_files
    for line in repair_data_files(a.contest, a.state) or []:
        print(f"[自愈] {line}", flush=True)
    print(f"PTA live board listening on http://{a.host}:{a.port}", flush=True); http = ThreadingHTTPServer((a.host, a.port), Handler); http.serve_forever()
if __name__ == "__main__": main()
