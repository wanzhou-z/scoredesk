# -*- coding: utf-8 -*-
"""逐条核对 HTTP Contest API 与本地数据，支持隔离回放状态。"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True
from board_core import build_board
import pta_common as C

BASE = Path(__file__).resolve().parent
SCHEMAS = {
    "": {"id": str, "name": str, "duration": str, "scoreboard_type": str, "penalty_time": int},
    "teams": {"id": str, "name": str, "label": str, "organization_id": str, "group_ids": list},
    "problems": {"id": str, "label": str, "name": str, "ordinal": int},
    "organizations": {"id": str, "name": str},
    "groups": {"id": str, "name": str},
    "languages": {"id": str, "name": str},
    "judgement-types": {"id": str, "name": str, "solved": bool, "penalty": bool},
    "submissions": {"id": str, "team_id": str, "problem_id": str, "language_id": str, "time": str, "contest_time": str},
    "judgements": {"id": str, "submission_id": str, "judgement_type_id": str, "start_time": str, "start_contest_time": str, "end_time": str, "end_contest_time": str},
    "awards": {"id": str, "citation": str, "team_ids": list},
    "scoreboard": {"time": str, "contest_time": str, "state": dict, "rows": list},
}


def check_fields(obj, schema):
    # 精确类型检查，避免 Python 把布尔值当作整数接受。
    for key, kind in schema.items():
        assert key in obj and type(obj[key]) is kind, f"字段 {key} 缺失或类型不正确"


def seconds(value):
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def verify(base, source, state, mode="operator", user=None, password=None):
    root = json.loads(Path(source).read_text(encoding="utf-8-sig"))
    local = json.loads(Path(state).read_text(encoding="utf-8-sig"))
    cid = str(root.get("problem_set_id") or "pta")
    prefix = base.rstrip("/") + "/contests/" + cid
    def get(resource):
        req = urllib.request.Request(prefix + ("/" + resource if resource else "") + "?freeze=" + mode)
        if user:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
        with urllib.request.urlopen(req, timeout=10) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "*", "缺少 CORS"
            data = json.load(response)
            if resource in SCHEMAS:
                if resource not in ("", "scoreboard"):
                    assert isinstance(data, list)
                for obj in data if isinstance(data, list) else [data]:
                    check_fields(obj, SCHEMAS[resource])
            return data
    meta = root["contest"]
    duration = float(meta["duration_seconds"])
    elapsed = float(local.get("presentation", {}).get("elapsed", max(0, time.time() - float(meta["start_epoch"]))))
    freeze_at = duration - float(meta.get("freeze_seconds") or 0)
    frozen = bool(meta.get("freeze_seconds") and freeze_at <= elapsed < duration)
    cutoff = freeze_at if frozen and mode == "audience" else min(elapsed, duration)
    submissions = [s for s in root["submissions"] if float(s.get("rel_time_seconds") or 0) <= cutoff]
    expected_subs = {str(s["id"]): s for s in submissions}
    teams = get("teams")
    assert len(teams) == len(root["teams"])
    expected_teams = {str(t["userId"]): t for t in root["teams"]}
    assert {t["id"] for t in teams} == set(expected_teams)
    orgs, groups, languages, verdicts = get("organizations"), get("groups"), get("languages"), get("judgement-types")
    for team in teams:
        raw = expected_teams[team["id"]]
        assert team["name"] == (raw.get("name") or str(raw["userId"]))
        assert team["label"] == str(raw.get("studentNumber") or raw["userId"])
        assert team["organization_id"] in {o["id"] for o in orgs}
        assert set(team["group_ids"]) <= {g["id"] for g in groups}
    problems = get("problems")
    assert [(p["id"], p["label"], p["name"], p["ordinal"]) for p in problems] == [(str(p["id"]), p.get("label") or p.get("letter"), p["name"], int(p["ordinal"])) for p in root["problems"]]
    actual_subs = get("submissions")
    assert len(actual_subs) == len(expected_subs)
    assert {s["id"] for s in actual_subs} == set(expected_subs)
    for sub in actual_subs:
        raw = expected_subs[sub["id"]]
        assert sub["team_id"] == str(raw["userId"])
        assert sub["problem_id"] == str(raw["problemSetProblemId"])
        compiler = str(raw.get("compiler") or "UNKNOWN").upper()
        lang = C.COMPILER_TO_LANG_ID.get(compiler, "unknown-" + hashlib.sha256(compiler.encode()).hexdigest()[:12])
        assert sub["language_id"] == lang
        assert seconds(sub["contest_time"]) == float(raw.get("rel_time_seconds") or 0)
        assert datetime.fromisoformat(sub["time"].replace("Z", "+00:00")) == datetime.fromisoformat(raw["submitAt"].replace("Z", "+00:00"))
        assert sub["team_id"] in expected_teams and sub["problem_id"] in {p["id"] for p in problems}
        assert sub["language_id"] in {lang["id"] for lang in languages}
    judgements = get("judgements")
    expected_judged = {str(s["id"]): s for s in submissions if s.get("status", "").upper() in C.PTA_STATUS_TO_ACRONYM}
    assert len(judgements) == len(expected_judged)
    assert {j["submission_id"] for j in judgements} == set(expected_judged)
    for judgement in judgements:
        raw = expected_judged[judgement["submission_id"]]
        assert judgement["id"] == "j-" + str(raw["id"])
        verdict = C.PTA_STATUS_TO_ACRONYM[raw["status"].upper()]
        expected_verdict = "RTE" if verdict in ("SF", "FPE", "NZEC", "RE") else verdict
        assert judgement["judgement_type_id"] == expected_verdict
        assert judgement["judgement_type_id"] in {v["id"] for v in verdicts}
        assert seconds(judgement["start_contest_time"]) == float(raw.get("rel_time_seconds") or 0)
        end = max(float(raw["submit_epoch"]), datetime.fromisoformat(raw.get("raw", {}).get("judgedAt", raw["submitAt"]).replace("Z", "+00:00")).timestamp())
        assert abs(seconds(judgement["end_contest_time"]) - (end - float(meta["start_epoch"]))) < .0011
    api_state = get("state")
    standard = "/standard/api" in base
    if standard:
        assert isinstance(api_state, dict)
        assert bool(api_state["started"]) == (elapsed >= 0)
        assert bool(api_state["ended"]) == (elapsed >= duration)
        assert bool(api_state["frozen"]) == bool(meta.get("freeze_seconds") and elapsed >= freeze_at)
        assert bool(api_state["thawed"]) == bool(meta.get("freeze_seconds") and elapsed >= duration)
        assert all(v is None or isinstance(v, str) for v in api_state.values())
    else:
        assert isinstance(api_state, list) and len(api_state) == 1
        assert api_state[0]["started"] == (elapsed >= 0)
        assert api_state[0]["ended"] == (elapsed >= duration)
        assert api_state[0]["frozen"] == frozen
        assert api_state[0]["thawed"] == bool(meta.get("freeze_seconds") and elapsed >= duration)
        assert api_state[0]["freeze_mode"] == mode
    assert (api_state if standard else api_state[0])["finalized"] in (False, None)
    visible = dict(root); visible["submissions"] = submissions
    board = build_board(visible, display_time=elapsed, freeze_mode=mode)
    scoreboard = get("scoreboard")
    assert scoreboard["state"] == (api_state if standard else api_state[0])
    assert [(r["rank"], r["team_id"], r["score"]["num_solved"], r["score"]["total_time"]) for r in scoreboard["rows"]] == [(r["rank"], r["team_id"], r["solved"], r["penalty"]) for r in board]
    for row, expected in zip(scoreboard["rows"], board):
        check_fields(row, {"rank": int, "team_id": str, "score": dict, "problems": list})
        check_fields(row["score"], {"num_solved": int, "total_time": int})
        for cell, original in zip(row["problems"], expected["problems"]):
            check_fields(cell, {"problem_id": str, "num_judged": int, "num_pending": int, "solved": bool})
            assert cell["problem_id"] == original["problem_id"]
            assert cell["solved"] == (original["state"] in ("ac", "locked"))
            if cell["solved"]:
                check_fields(cell, {"time": int})
                assert cell["time"] == int(original["time"]) // 60
    contest = get("")
    assert contest["id"] == cid and contest["name"] == meta["title"]
    assert seconds(contest["duration"]) == duration and contest["penalty_time"] == int(meta["penalty_minutes"])
    assert seconds(contest["scoreboard_freeze_duration"]) == float(meta["freeze_seconds"])
    awards = get("awards"); assert awards == []
    result = {"result": "PASS", "profile": "standard" if standard else "task", "freeze": mode, "elapsed": elapsed, "frozen": frozen, "teams": len(teams), "problems": len(problems), "submissions": len(actual_subs), "judgements": len(judgements), "scoreboard_rows": len(board), "organizations": len(orgs), "languages": len(languages), "judgement_types": len(verdicts), "awards": len(awards)}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8765/api")
    parser.add_argument("--from", dest="source", type=Path, default=BASE / "contest.json")
    parser.add_argument("--state", type=Path, default=BASE / "state.json")
    parser.add_argument("--freeze", choices=("audience", "operator"), default="operator")
    parser.add_argument("--user"); parser.add_argument("--password")
    args = parser.parse_args()
    try:
        verify(args.base, args.source, args.state, args.freeze, args.user, args.password)
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

if __name__ == "__main__":
    main()
