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
DEFAULT_CONTEST = RES / "contest.json"

#: 取数通道**恒为 http**（不再有借用浏览器这条路）。
CHANNEL = "http"
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


def run_fetch(args, full=False):
    """走直连接口抓一轮；失败时抛 RuntimeError（带退出码与日志尾巴）。

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


def write_paused(reason: str, wait: float, attempt: int) -> None:
    """Cookie 不可用：写一份「暂停轮询」的 state（channel 恒为 http）。"""
    contest = load_opt(BASE / "contest.json")
    state = make_state(contest, channel=CHANNEL, last_poll=iso(),
                       event={"type": "paused", "reason": reason,
                              "retry_in": wait, "attempt": attempt})
    state["cookie_error"] = reason
    json_dump_atomic(BASE / "state.json", state)


def main():
    cfg = load_json(BASE / "config.json") if (BASE / "config.json").exists() else {}
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=float(cfg.get("poll_interval", 5)))
    ap.add_argument("--full-interval", type=float, default=float(cfg.get("full_check_interval", 120)))
    ap.add_argument("--max-pages", type=int, default=0,
                    help="增量单轮的页数上限；0=不限（M14 起默认 0，全量核对恒为 0）")
    ap.add_argument("--problem-set", default=str(cfg.get("problem_set_id") or ""),
                    help="题目集 ID；默认取 config.json 的 problem_set_id")
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
    log.info("启动：间隔 %.1fs，全量核对 %.1fs（全量=不限页数+刷新队伍名录）；增量页数上限=%s；"
             "取数通道=%s（必须有效 Cookie，不再借用浏览器）；强制全量旗标=%s",
             args.interval, args.full_interval, args.max_pages or "不限", CHANNEL, FORCE_FULL)
    while True:
        started = time.monotonic()
        reason = cookie_problem(read_cookie_text(args.cookie_file))
        if reason is None:
            forced = force_full_pending()                     # M14：界面「全量刷新」/ ?refresh=full
            want_full = bool(forced or (started - last_full) >= args.full_interval)
            fetched = None
            detail = ""
            fail_note = ""
            try:
                fetched, detail = run_fetch(args, full=want_full)
            except Exception as exc:                       # noqa: BLE001
                code = getattr(exc, "returncode", None)
                fail_note = str(exc)
                if auth_failed(code, fail_note):
                    reason = ("Cookie 已失效（接口返回 401/未授权，退出码 %s）—— "
                              "请到「1 数据源」页点「粘贴 cURL 自动生成」更新 Cookie"
                              % (code if code is not None else "?"))
                else:
                    log.error("取数失败（非 Cookie 原因）：%s", fail_note[-400:])
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
                report.update({"at": report.get("at") or iso(), "channel": CHANNEL,
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
                state = make_state(contest, channel=CHANNEL, last_poll=iso(), event=event)
                board = state["board"]
                event["rank_changed"] = old_board != board
                old_board = board
                if changed or want_full:
                    snap = {"at": iso(), "channel": CHANNEL, "submissions": len(merged),
                            "board": board, "event": event}
                    json_dump_atomic(snap_dir / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json"), snap)
                    incomplete = bool(state.get("data_incomplete"))
                    log.info("通道=%s 新增=%d 总提交=%d 队伍=%d/%s 榜单变化=%s 全量=%s 抓取页数=%s 截断=%s%s%s",
                             CHANNEL, len(new_ids), len(merged), state["teams"],
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
                             args.interval, step + 1)
                if args.once:
                    return 2
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
                continue

        wait = BACKOFF_STEPS[min(step, len(BACKOFF_STEPS) - 1)]
        step += 1
        write_paused(reason, wait, step)
        log.warning("已暂停/退避轮询：Cookie 失效 —— %s（第 %d 次退避，%.0fs 后重试，"
                    "阶梯 20→60→300s）", reason, step, wait)
        if args.once:
            return 3
        time.sleep(wait)


if __name__ == "__main__":
    raise SystemExit(main())
