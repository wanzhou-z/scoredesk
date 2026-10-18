# -*- coding: utf-8 -*-
"""从已有本地状态投影 Contest API；不抓取、不复制判题或排名算法。"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from board_core import build_board
from presentation import load_config, resolve_freeze_mode
import pta_common as C

BASE = Path(__file__).resolve().parent
STANDARD = "/api/standard/api"
COLLECTIONS = ("judgement-types", "languages", "problems", "groups", "organizations", "teams", "submissions", "judgements", "awards")


def verdict_id(acronym):
    # 原项目的崩溃细分类归入标准 RTE；只转换 API 标签，不影响原判罚。
    return "RTE" if acronym in ("SF", "FPE", "NZEC", "RE") else acronym


def iso(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def relative(seconds):
    milliseconds = round(float(seconds) * 1000)
    sign = "-" if milliseconds < 0 else ""
    hours, remainder = divmod(abs(milliseconds), 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, ms = divmod(remainder, 1000)
    return f"{sign}{hours}:{minutes:02d}:{secs:02d}.{ms:03d}"


def epoch(value, fallback):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return fallback


def is_route(path):
    return path.rstrip("/") in ("/api", STANDARD) or path.startswith(("/api/contests", STANDARD + "/"))


class ContestAPI:
    def __init__(self, source, state, config=None):
        self.source, self.state, self.config = Path(source), Path(state), config
        self.lock = threading.RLock()
        self.cache_key = None
        self.cache = None

    def settings(self):
        if self.config:
            return json.loads(Path(self.config).read_text(encoding="utf-8-sig"))
        return load_config()

    def snapshot(self, mode, standard):
        # 原子替换期间保留上一份有效快照，不因暂时读失败发送删除事件。
        with self.lock:
            key = (self.source.stat().st_mtime_ns, self.state.stat().st_mtime_ns, mode, standard, int(time.time()))
            if key == self.cache_key:
                return self.cache
            source = json.loads(self.source.read_text(encoding="utf-8-sig"))
            state = json.loads(self.state.read_text(encoding="utf-8-sig"))
            meta = source["contest"]
            start = float(meta["start_epoch"])
            duration = float(meta["duration_seconds"])
            freeze_seconds = float(meta.get("freeze_seconds") or 0)
            freeze_at = duration - freeze_seconds
            elapsed = float(state.get("presentation", {}).get("elapsed", max(0, time.time() - start)))
            frozen = bool(freeze_seconds and freeze_at <= elapsed < duration)
            cutoff = freeze_at if frozen and mode == "audience" else min(elapsed, duration)
            cid = str(source.get("problem_set_id") or "pta")
            contest = {"id": cid, "name": meta["title"], "formal_name": meta["title"], "start_time": iso(start), "duration": relative(duration), "scoreboard_type": "pass-fail", "penalty_time": int(meta.get("penalty_minutes") or 20)}
            if freeze_seconds:
                contest["scoreboard_freeze_duration"] = relative(freeze_seconds)
            timestamp_state = {"started": iso(start) if elapsed >= 0 else None, "frozen": iso(start + freeze_at) if freeze_seconds and elapsed >= freeze_at else None, "ended": iso(start + duration) if elapsed >= duration else None, "thawed": iso(start + duration) if freeze_seconds and elapsed >= duration else None, "finalized": None, "end_of_updates": None}
            api_state = timestamp_state if standard else {"id": "state", "started": elapsed >= 0, "ended": elapsed >= duration, "frozen": frozen, "thawed": bool(freeze_seconds and elapsed >= duration), "finalized": False, "freeze_mode": mode}
            result = {"contest": contest, "state": api_state, "awards": [], "groups": [{"id": "pta", "name": "PTA", "hidden": False}]}
            result["problems"] = [{"id": str(p["id"]), "label": p.get("label") or p.get("letter"), "name": p["name"], "ordinal": int(p["ordinal"])} for p in source["problems"]]
            # 不伪造题目限制、测试点数量、源码压缩包；规范缺口在文档公开。
            result["teams"] = [{"id": str(t["userId"]), "label": str(t.get("studentNumber") or t["userId"]), "name": t.get("name") or str(t["userId"]), "organization_id": "pta", "group_ids": ["pta"]} for t in source["teams"]]
            result["organizations"] = [{"id": "pta", "name": "PTA", "formal_name": "PTA 比赛队伍（源数据未提供学校）"}]
            result["languages"] = copy.deepcopy(C.LANGUAGES)
            # 标准采用 verdict acronym 作为 ID，映射表仍复用原项目。
            definitions = {verdict_id(j["acronym"]): {"id": verdict_id(j["acronym"]), "name": j["name"], "solved": j["solved"], "penalty": j["penalty"]} for j in C.JUDGEMENTS}
            result["judgement-types"] = list(definitions.values())
            result["submissions"], result["judgements"] = [], []
            visible = copy.copy(source)
            visible["submissions"] = sorted([s for s in source["submissions"] if float(s.get("rel_time_seconds") or 0) <= cutoff], key=lambda s: (float(s.get("rel_time_seconds") or 0), str(s["id"])))
            language_ids = {lang["id"] for lang in result["languages"]}
            for sub in visible["submissions"]:
                sid = str(sub["id"])
                compiler = str(sub.get("compiler") or "UNKNOWN").upper()
                lang = C.COMPILER_TO_LANG_ID.get(compiler)
                if lang is None:
                    lang = "unknown-" + hashlib.sha256(compiler.encode()).hexdigest()[:12]
                    if lang not in language_ids:
                        result["languages"].append({"id": lang, "name": compiler})
                        language_ids.add(lang)
                rel = float(sub.get("rel_time_seconds") or 0)
                submitted = epoch(sub.get("submitAt"), start + rel)
                result["submissions"].append({"id": sid, "team_id": str(sub["userId"]), "problem_id": str(sub["problemSetProblemId"]), "language_id": lang, "time": iso(submitted), "contest_time": relative(rel)})
                verdict = C.PTA_STATUS_TO_ACRONYM.get(str(sub.get("status") or "").upper())
                # 未知/排队不伪装为 WA；未提供真实开始时间时使用提交时间近似。
                if verdict:
                    judged = max(submitted, epoch((sub.get("raw") or {}).get("judgedAt"), submitted))
                    result["judgements"].append({"id": "j-" + sid, "submission_id": sid, "judgement_type_id": verdict_id(verdict), "start_time": iso(submitted), "start_contest_time": relative(rel), "end_time": iso(judged), "end_contest_time": relative(judged - start)})
            # 排名、得分、罚时仅调用既有算法；裁判榜的 locked AC 映射为实时 AC。
            board = build_board(visible, display_time=elapsed, freeze_mode=mode)
            attempts = {}
            for sub in result["submissions"]:
                attempts.setdefault((sub["team_id"], sub["problem_id"]), []).append(sub)
            judged_by_id = {j["submission_id"]: j for j in result["judgements"]}
            rows = []
            for row in board:
                cells = []
                for cell in row["problems"]:
                    num_judged = num_pending = 0
                    for sub in attempts.get((row["team_id"], cell["problem_id"]), []):
                        judgement = judged_by_id.get(sub["id"])
                        if judgement:
                            num_judged += 1
                            if judgement["judgement_type_id"] == "AC":
                                break
                        else:
                            num_pending += 1
                    solved = cell["state"] in ("ac", "locked")
                    item = {"problem_id": cell["problem_id"], "num_judged": num_judged, "num_pending": num_pending, "solved": solved}
                    if solved:
                        item["time"] = int(cell["time"]) // 60
                    cells.append(item)
                rows.append({"rank": row["rank"], "team_id": row["team_id"], "score": {"num_solved": row["solved"], "total_time": row["penalty"]}, "problems": cells})
            result["scoreboard"] = {"time": iso(start + max(0, min(cutoff, duration))), "contest_time": relative(max(0, min(cutoff, duration))), "state": api_state, "rows": rows}
            self.cache_key, self.cache = key, result
            return result

    def authorized(self, handler):
        auth = self.settings().get("api_auth")
        if not auth:
            return True
        if not isinstance(auth, dict) or not auth.get("user") or not auth.get("password"):
            raise ValueError("api_auth 启用时必须同时设置非空 user 和 password")
        expected = "Basic " + base64.b64encode((auth["user"] + ":" + auth["password"]).encode("utf-8")).decode("ascii")
        return hmac.compare_digest(handler.headers.get("Authorization", "").encode(), expected.encode())

    def handle(self, handler, url, query):
        def send(obj, status=200, extra=None):
            handler.send_bytes(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8", status, extra)
        try:
            if not self.authorized(handler):
                send({"error": "Basic authentication required"}, 401, {"WWW-Authenticate": 'Basic realm="Contest API", charset="UTF-8"'})
                return
            mode = query.get("freeze", [self.settings().get("freeze_mode", "operator")])[0]
            if mode not in ("audience", "operator"):
                raise ValueError("freeze 必须为 audience 或 operator")
            standard = url.path.startswith(STANDARD)
            prefix = STANDARD if standard else "/api"
            path = url.path[len(prefix):].strip("/")
            snap = self.snapshot(mode, standard)
            parts = [unquote(p) for p in path.split("/") if p]
            if not parts:
                send({"version": "2023-06", "version_url": "https://ccs-specs.icpc.io/2023-06/contest_api", "provider": {"name": "PTA Live Board M3 (read-only subset)", "version": "3.0"}})
                return
            if parts == ["contests"]:
                send([snap["contest"]]); return
            if len(parts) < 2 or parts[0] != "contests" or parts[1] != snap["contest"]["id"]:
                send({"error": "Unknown contest"}, 404); return
            if len(parts) == 2:
                send(snap["contest"]); return
            resource = parts[2]
            if resource == "event-feed" and len(parts) == 3:
                if any(k in query for k in ("since_token", "since_id", "types")):
                    send({"error": "Resume/filter not implemented; reconnect without cursor for a full snapshot"}, 400); return
                self.feed(handler, mode, standard); return
            if resource == "access" and len(parts) == 3:
                endpoints = [{"type": typ, "properties": sorted({k for obj in (value if isinstance(value, list) else [value]) for k in obj})} for typ, value in snap.items()]
                send({"capabilities": [], "endpoints": endpoints}); return
            if resource not in snap:
                send({"error": "Unsupported resource"}, 404); return
            value = snap[resource]
            if len(parts) == 4 and isinstance(value, list):
                obj = next((o for o in value if o["id"] == parts[3]), None)
                send(obj if obj is not None else {"error": "Unknown resource id"}, 200 if obj is not None else 404); return
            if len(parts) != 3:
                send({"error": "Unknown path"}, 404); return
            if resource == "state" and not standard:
                value = [value]
            send(value)
        except ValueError as exc:
            send({"error": str(exc)}, 400)
        except (OSError, KeyError, TypeError) as exc:
            send({"error": "Local source unavailable", "detail": str(exc)}, 503)

    @staticmethod
    def events(snap, standard):
        items = {}
        for typ in ("contest",) + COLLECTIONS + ("state",):
            value = snap[typ]
            for obj in value if isinstance(value, list) else [value]:
                ident = obj.get("id", "state")
                item = {"type": typ, "data": obj}
                # 标准 singleton 不含资源 id；兼容入口满足任务的 type/id/data 形状。
                if typ != "state" or not standard:
                    item["id"] = ident
                items[(typ, ident)] = item
        return items

    def feed(self, handler, mode, standard):
        handler.send_response(200)
        for key, value in (("Content-Type", "application/x-ndjson; charset=utf-8"), ("Cache-Control", "no-cache"), ("Access-Control-Allow-Origin", "*"), ("Connection", "close"), ("X-Accel-Buffering", "no")):
            handler.send_header(key, value)
        handler.end_headers()
        handler.close_connection = True
        previous = {}
        heartbeat = time.monotonic()
        try:
            while True:
                try:
                    current = self.events(self.snapshot(mode, standard), standard)
                except (OSError, ValueError):
                    time.sleep(.1)
                    continue
                lines = []
                for key, event in current.items():
                    if previous.get(key) != event:
                        lines.append(event)
                for key, event in previous.items():
                    if key not in current:
                        lines.append({**event, "data": None})
                if lines:
                    handler.wfile.write(("".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in lines)).encode("utf-8"))
                    handler.wfile.flush()
                    previous = current
                    heartbeat = time.monotonic()
                elif time.monotonic() - heartbeat >= 15:
                    handler.wfile.write(b"\n"); handler.wfile.flush(); heartbeat = time.monotonic()
                time.sleep(.1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
