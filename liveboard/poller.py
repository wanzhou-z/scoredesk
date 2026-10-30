# -*- coding: utf-8 -*-
"""后台增量轮询器：**只走直连接口（HTTP + JSON，需要有效 Cookie）**。

2026-10-13 改动（用户要求「实时榜单就要 cookie，将借用浏览器删掉」）：

* 删掉「HTTP 失败 → 自动回落借用浏览器」这条回落逻辑，全产品不再借用浏览器；
* Cookie 缺失 / 失效时**一个请求都不发**（绝不每 20 秒撞一次 401）：
  写清楚原因到 state.json 的 ``cookie_error``，日志写「已暂停/退避轮询」，
  并做**指数退避** 20s → 60s → 300s（封顶）；恢复后自动继续（退避归零）。
"""
from __future__ import annotations
import argparse, json, logging, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from board_core import json_dump_atomic, load_json, make_state, RESOLVER
BASE = Path(__file__).resolve().parent
#: M13（4c）：不再写死本机路径 —— 由 board_core._find_resolver_dir() 按
#: 「环境变量 PTA_RESOLVER_DIR → 父目录（打包后 = _internal）→ 父目录\pta-resolver
#: → 旧绝对路径」定位，谁真的含 pta_to_resolver_xml.py 用谁。
RES = RESOLVER
FETCH = RES / "pta_fetch.py"
#: 增量基线的 contest.json：**优先运行目录自己那份**，没有再退回代码树里的。
#: 否则会拿别的比赛（例如线上 PTA 那场）当基线，日志里出现"清理僵尸提交=621"这种噪声。
DEFAULT_CONTEST = BASE / "contest.json"
#: VJudge 版抓取器（VJudge 平台用；公开赛无需 Cookie）
#: M-VJ：不能只认 RES —— 开发/测试时 RES 可能被解析到*别的*代码树（例如线上
#: 别的代码树（例如开发机上另一份 PTA 工程）），那里面没有 vjudge_fetch.py，轮询就会一直报
#: "can't open file"。所以按「运行目录 → RES → RES 的上级 → 同级的 pta-resolver」
#: 顺序找，谁真的存在用谁。
VJ_FETCH = RES / "vjudge_fetch.py"


def vj_fetch_path() -> Path:
    """定位 vjudge_fetch.py：优先跟 poller 同目录的那份。"""
    cands = [BASE / "vjudge_fetch.py",
             BASE.parent / "vjudge_fetch.py",
             BASE.parent / "_internal" / "vjudge_fetch.py",
             BASE.parent / "_internal" / "pta-resolver" / "vjudge_fetch.py",
             RES / "vjudge_fetch.py",
             RES.parent / "vjudge_fetch.py",
             BASE.parent / "pta-resolver" / "vjudge_fetch.py"]
    for cand in cands:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return VJ_FETCH

#: 取数通道（PTA 恒为 http；VJudge 轮次写 vjudge，方便页面/日志区分）。
CHANNEL = "http"
#: VJudge 轮次的 channel 值
VJ_CHANNEL = "vjudge"


def read_run_config() -> dict:
    """读运行目录的 ``config.json``（每轮都重读：GUI 改平台/间隔后不用重启服务）。"""
    try:
        cfg = load_json(BASE / "config.json")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:                                      # noqa: BLE001
        return {}


def platform_of(cfg: dict | None = None) -> str:
    """当前平台：``pta``（默认）/ ``vjudge``。"""
    cfg = cfg if isinstance(cfg, dict) else read_run_config()
    value = str(cfg.get("platform") or "pta").strip().lower()
    return "vjudge" if value == "vjudge" else "pta"


def contest_id_of(cfg: dict | None = None, fallback: str = "") -> str:
    """当前比赛 ID：VJudge 用 ``contest_id``，PTA 用 ``problem_set_id``。"""
    cfg = cfg if isinstance(cfg, dict) else read_run_config()
    for key in ("contest_id", "problem_set_id"):
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    return str(fallback or "").strip()

#: 指数退避阶梯（秒）：20 → 60 → 300 封顶；成功一轮后回到第一档。
BACKOFF_STEPS = (20.0, 60.0, 300.0)
#: M14 强制全量刷新旗标：server.py 的 ``/api/refresh-full``（界面按钮 / ``?refresh=full``）
#: 写它，poller 在下一轮读到就做一次**完整**全量（不限页数 + 刷新名录），成功后删掉。
#: 抓取失败/退避时**不删** —— 请求不会丢，等能抓了再执行。
FORCE_FULL = BASE / "tmp" / "force-full.flag"


def force_full_pending() -> bool:
    return FORCE_FULL.is_file()


def iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def setup_log(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("poller")
    log.setLevel(logging.INFO)
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    return log


# --------------------------------------------------------------------------
# Cookie：本地判据（不发请求）
# --------------------------------------------------------------------------

def read_cookie_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def cookie_problem(text: str) -> str | None:
    """本地判断 Cookie 看起来是否可用。

    返回 ``None`` = 看起来可用（真正的有效性由一次接口请求决定）；
    返回字符串 = 给人看的「为什么现在取不到数」。**这里绝不发请求**。
    """
    if not text:
        return ("没有找到可用的 cookie.txt —— 实时榜单必须提供有效 Cookie（直连接口）；"
                "请到「1 数据源」页点「粘贴 cURL 自动生成」更新 Cookie")
    value = text.strip()
    if value.startswith("{") or value.startswith("["):
        try:
            obj = json.loads(value)
            if isinstance(obj, dict):
                obj = obj.get("cookies", obj)
                value = "; ".join(f"{k}={v}" for k, v in obj.items())
        except (ValueError, TypeError):
            pass
    low = value.lower()
    has_js = "jsessionid" in low
    has_pt = "ptasession" in low
    if not (has_js and has_pt):
        missing = " 和 ".join(
            name for name, ok in (("JSESSIONID", has_js), ("PTASession", has_pt)) if not ok)
        return (f"cookie.txt 里缺少 {missing} —— 两个都必须带（只带 PTASession 会 401）；"
                "请到「1 数据源」页重新「粘贴 cURL 自动生成」")
    return None


def auth_failed(returncode: int | None, output: str) -> bool:
    """这次抓取失败是不是「Cookie 失效/无权限」这类凭证问题。"""
    if returncode in (3, 4):
        return True
    text = (output or "")
    return ("401" in text) or ("未授权" in text) or ("Cookie 失效" in text)


def run_fetch(args, full=False, platform="pta", contest_id=""):
    """抓一轮；失败时抛 RuntimeError（带退出码与日志尾巴）。

    ``platform == "vjudge"`` 时改调 ``vjudge_fetch.py fetch --contest <id>``：
    公开接口、匿名、不需要 Cookie，每轮都是**全量**（这个接口本来就一次给全）。
    其余逻辑（合并 / 落盘 / make_state）两边完全一样 —— 因为 contest.json 同构。

    M14「拉取不全」修复（三处，缺一不可）：
      ① **全量核对不带页数上限**：以前无论增量还是全量都原样转发 ``--max-pages``，
         而它的默认值是 1 —— 冷启动（缓存里没有提交）时只拿**第 1 页 100 条**，
         100 条只覆盖得到十几支队伍，页面上就"才拉到 17 个"；更糟的是增量游标
         永远停在第 1 页，后面的老提交**再也回不来**。现在 ``full=True`` 恒用 0（不限）。
      ② **全量核对强制刷新队伍名录**（``--refresh-meta``）：名录是"队名/学号"的来源，
         也是 ``compute_ranking`` 的遍历集合 —— 过期名录会让"有提交的队伍"直接掉榜。
      ③ 提交重抓用 ``--full``（忽略增量游标），配合 ``pta_fetch`` 的
         "新数据覆盖旧数据"（fresh-wins）把上游改过的 ``userId``/状态真正纠正过来。
    """
    out = BASE / "contest.json"
    if platform == "vjudge":
        cid = str(contest_id or contest_id_of() or args.problem_set).strip()
        # 备注库和 GUI 的「队伍备注」页共用同一份：<程序根>\vjudge-aliases.db
        db_path = BASE.parent / "vjudge-aliases.db"
        # 轮询轮次：不下载头像（头像是装饰，GUI 拉取时再下）；
        # 超时收紧到 12 秒 / 最多重试 2 次，避免一轮卡住把 10 秒的节奏拖垮。
        # 头像：**要抓，但给个小预算**。之前这里用 --no-avatars，导致比赛中新进队的
        #   选手永远没有头像（服务端找不到就退回默认校徽）。已缓存的不会再下载，
        #   所以只有新队会花时间；预算 10 秒封顶，不拖垮 10 秒一轮的节奏。
        cmd = [sys.executable, str(vj_fetch_path()), "fetch", "--contest", cid,
               "--out", str(out), "--quiet", "--db", str(db_path),
               "--avatar-budget", "10", "--timeout", "12", "--max-retries", "2"]
        cookie = Path(str(args.cookie_file))
        try:
            if cookie.is_file() and cookie.read_text(encoding="utf-8",
                                                     errors="replace").strip():
                cmd += ["--cookie-file", str(cookie)]
        except OSError:
            pass
        p = subprocess.run(cmd, cwd=str(RES), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=max(float(args.timeout), 180.0))
        detail = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            exc = RuntimeError(detail[-1200:])
            exc.returncode = p.returncode      # type: ignore[attr-defined]
            raise exc
        return load_json(out), detail

    psid = args.problem_set
    max_pages = 0 if full else max(0, int(args.max_pages))
    cmd = [sys.executable, str(FETCH), "fetch", "--problem-set", psid,
           "--via", "http", "--max-pages", str(max_pages),
           "--out", str(out), "--quiet",
           "--cookie-file", str(Path(args.cookie_file))]
    if full:
        cmd += ["--full", "--refresh-meta"]
    p = subprocess.run(cmd, cwd=str(RES), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=args.timeout)
    detail = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        exc = RuntimeError(detail[-1200:])
        exc.returncode = p.returncode          # type: ignore[attr-defined]
        raise exc
    # 返回**完整**输出：调用方要用它解析"请求 N 页"（落盘/日志里只存尾巴）。
    return load_json(out), detail


def load_opt(path):
    """读 JSON；缺文件/坏 JSON 一律返回 {}（轮询器不许因为数据文件挂掉）。"""
    try:
        return load_json(Path(path)) or {}
    except Exception:                                      # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# 实时榜单的「封榜时长」覆盖（用户 2026-10-24：滚榜那份封榜时间也放到实时榜单里改）
#   live/config.json 里的 ``freeze_minutes``：0 = 不封榜，缺省/空 = 用比赛自带的
#   freeze_seconds。按 mtime 缓存，改完下一轮轮询就生效，不用重启服务。
_FREEZE_CACHE = {"mtime": None, "value": None}


def freeze_override():
    import os
    path = BASE / "config.json"
    try:
        mt = path.stat().st_mtime
    except OSError:
        return None
    if _FREEZE_CACHE["mtime"] == mt:
        return _FREEZE_CACHE["value"]
    cfg = load_json(path) if path.exists() else {}
    raw = (cfg or {}).get("freeze_minutes")
    val = None
    if raw is not None and str(raw).strip() != "":
        try:
            mins = float(raw)
            if mins >= 0:
                val = mins * 60.0
        except (TypeError, ValueError):
            val = None
    _FREEZE_CACHE.update(mtime=mt, value=val)
    return val

def write_paused(reason: str, wait: float, attempt: int, channel: str = CHANNEL) -> None:
    """本轮取不到数：写一份「暂停轮询」的 state。

    ``channel`` 为 ``http``（PTA）或 ``vjudge``；``cookie_error`` 只在凭证类问题时
    才有意义（VJudge 公开赛不需要 Cookie，这里只写普通失败原因）。
    """
    contest = load_opt(BASE / "contest.json")
    state = make_state(contest, channel=channel, last_poll=iso(),
                       freeze_seconds=freeze_override(),
                       event={"type": "paused", "reason": reason,
                              "retry_in": wait, "attempt": attempt})
    if channel != VJ_CHANNEL:
        state["cookie_error"] = reason
    else:
        state["poll_error"] = reason
    json_dump_atomic(BASE / "state.json", state)


def main():
    cfg = load_json(BASE / "config.json") if (BASE / "config.json").exists() else {}
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=float(cfg.get("poll_interval", 5)))
    ap.add_argument("--full-interval", type=float, default=float(cfg.get("full_check_interval", 120)))
    ap.add_argument("--max-pages", type=int, default=0,
                    help="增量单轮的页数上限；0=不限（M14 起默认 0，全量核对恒为 0）")
    ap.add_argument("--problem-set", default="",
                    help="比赛 ID（PTA=题目集 ID / VJudge=比赛 ID）；留空则从 config.json 读")
    ap.add_argument("--cookie-file", default=str(RES / "cookie.txt"))
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    log = setup_log(BASE / "logs" / "poller.log")
    state_path = BASE / "state.json"
    snap_dir = BASE / "snapshots"
    snap_dir.mkdir(exist_ok=True)
    contest = load_opt(DEFAULT_CONTEST)
    last_full = 0.0
    old_ids = {str(s.get("id")): s for s in contest.get("submissions", []) if s.get("id") is not None}
    old_board = []
    step = 0                                  # 退避档位：0 → 20s，1 → 60s，2 → 300s
    platform = platform_of(cfg)
    log.info("启动：平台=%s；间隔 %.1fs，全量核对 %.1fs；增量页数上限=%s；"
             "取数通道=%s；强制全量旗标=%s",
             platform, args.interval, args.full_interval, args.max_pages or "不限",
             VJ_CHANNEL if platform == "vjudge" else CHANNEL, FORCE_FULL)
    while True:
        started = time.monotonic()
        # 每轮重读 config.json：GUI 改平台 / 比赛 ID 后，poller 不用重启就能跟上。
        cfg_now = read_run_config()
        platform = platform_of(cfg_now)
        channel = VJ_CHANNEL if platform == "vjudge" else CHANNEL
        contest_id = contest_id_of(cfg_now, args.problem_set)
        # VJudge 公开赛匿名取数，不需要 Cookie；PTA 仍然先做本地判据（绝不每轮撞 401）
        reason = None if platform == "vjudge" else cookie_problem(
            read_cookie_text(args.cookie_file))
        if reason is None:
            forced = force_full_pending()                     # M14：界面「全量刷新」/ ?refresh=full
            want_full = bool(forced or (started - last_full) >= args.full_interval)
            if platform == "vjudge":
                # VJudge 这个接口一次给全，每轮都是全量；旗标直接兑现。
                want_full = True
            fetched = None
            detail = ""
            fail_note = ""
            try:
                fetched, detail = run_fetch(args, full=want_full, platform=platform,
                                            contest_id=contest_id)
            except Exception as exc:                       # noqa: BLE001
                code = getattr(exc, "returncode", None)
                fail_note = str(exc)
                if platform != "vjudge" and auth_failed(code, fail_note):
                    reason = ("Cookie 已失效（接口返回 401/未授权，退出码 %s）—— "
                              "请到「1 数据源」页点「粘贴 cURL 自动生成」更新 Cookie"
                              % (code if code is not None else "?"))
                else:
                    log.error("取数失败（平台=%s，非 Cookie 原因）：%s",
                              platform, fail_note[-400:])
            if fetched is not None:
                step = 0
                if want_full:
                    last_full = started
                    # 请求已兑现才撤旗标（上面抛异常时不会走到这里）。
                    if forced:
                        try:
                            FORCE_FULL.unlink()
                        except OSError:
                            pass
                # M14 抓取报告（随 contest.json/state.json 落盘，/health 与页面据此判断「全不全」）。
                # 主体由 pta_fetch 写（只有它知道"是不是撞上了 --max-pages 上限"），
                # 这里补上取数通道与"本轮是不是被强制全量"。页数从收尾输出兜底解析。
                pages = None
                match = re.search(r"请求\s*(\d+)\s*页", detail or "")
                if match:
                    pages = int(match.group(1))
                report = dict(fetched.get("fetch_report") or {})
                report.setdefault("pages", pages)
                report.update({"at": report.get("at") or iso(), "channel": channel,
                               "platform": platform,
                               "full": bool(report.get("full", want_full)),
                               "forced_full": bool(forced),
                               "max_pages": 0 if want_full else max(0, int(args.max_pages)),
                               # 全量核对恒为不限页数 => 不可能被截断；增量轮若显式带 --max-pages 则如实标出。
                               "truncated": bool(report.get("truncated",
                                                            (not want_full) and int(args.max_pages) > 0))})
                new_ids = []
                newmap = {str(s.get("id")): s for s in fetched.get("submissions", [])
                          if s.get("id") is not None}
                merged = dict(old_ids)
                changed = False
                for sid, sub in newmap.items():
                    if sid not in merged or merged[sid] != sub:
                        changed = True
                    if sid not in old_ids:
                        new_ids.append(sid)
                    merged[sid] = sub
                # M14：**全量核对且没被截断**时以本轮为准（替换而不是并集）——
                # 否则上游已删除/改了 userId 的僵尸提交会永远留在 contest.json 里，
                # 让"有提交的队伍"掉出榜单（用户看到的 17/25 就是这么来的）。
                dropped = 0
                if want_full and not report.get("truncated"):
                    dropped = len([sid for sid in merged if sid not in newmap])
                    if dropped:
                        changed = True
                    merged = dict(newmap)
                contest = dict(fetched)
                contest["submissions"] = list(merged.values())
                contest["fetch_report"] = report
                old_ids = merged
                event = {"type": "poll", "new_submissions": len(new_ids),
                         "ids": new_ids[:20], "full_check": want_full, "forced_full": bool(forced),
                         "dropped_stale": dropped, "detail": (detail or "")[-500:], "fetch_report": report}
                state = make_state(contest, channel=channel, last_poll=iso(), event=event,
                                   freeze_seconds=freeze_override())
                board = state["board"]
                event["rank_changed"] = old_board != board
                old_board = board
                # VJudge 每轮都是「全量」（接口一次给全），所以不能按 want_full 每轮都写快照
                # （10 秒一轮 = 每小时 360 个文件）；只有数据真的变了才落快照。
                if changed or (want_full and platform != "vjudge"):
                    snap = {"at": iso(), "channel": channel, "submissions": len(merged),
                            "board": board, "event": event}
                    json_dump_atomic(snap_dir / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json"), snap)
                    incomplete = bool(state.get("data_incomplete"))
                    log.info("平台=%s 通道=%s 新增=%d 总提交=%d 队伍=%d/%s 榜单变化=%s 全量=%s 抓取页数=%s 截断=%s%s%s",
                             platform, channel, len(new_ids), len(merged), state["teams"],
                             state.get("teams_expected"), event["rank_changed"], want_full,
                             report.get("pages"), report.get("truncated"),
                             (" 清理僵尸提交=%d" % dropped) if dropped else "",
                             ("  ⚠ " + str(state.get("data_note"))) if incomplete else "")
                json_dump_atomic(BASE / "contest.json", contest)
                json_dump_atomic(state_path, state)
                if args.once:
                    return 0
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
                continue
            if reason is None:
                # 非凭证类失败（网络抖动等）：按正常间隔重试，不进 Cookie 退避。
                write_paused(fail_note[-400:] or "本次取数失败（非 Cookie 原因）",
                             args.interval, step + 1, channel)
                if args.once:
                    return 2
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
                continue

        wait = BACKOFF_STEPS[min(step, len(BACKOFF_STEPS) - 1)]
        step += 1
        write_paused(reason, wait, step, channel)
        log.warning("已暂停/退避轮询（平台=%s）：%s（第 %d 次退避，%.0fs 后重试，"
                    "阶梯 20→60→300s）", platform, reason, step, wait)
        if args.once:
            return 3
        time.sleep(wait)


if __name__ == "__main__":
    raise SystemExit(main())
