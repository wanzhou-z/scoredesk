# -*- coding: utf-8 -*-
from __future__ import annotations
import copy, json, os, sys, tempfile
from pathlib import Path
from typing import Any
def _find_resolver_dir() -> Path:
    """定位 ``pta_to_resolver_xml.py`` 所在的目录（发布版不能写死本机路径）。

    按顺序找，谁真的含 ``pta_to_resolver_xml.py`` 就用谁：

      1. 环境变量 ``PTA_RESOLVER_DIR``（部署时可以覆盖）；
      2. 本文件所在目录（几个模块被拷到一起时）；
      3. 仓库形状：父目录下的 ``core\\``（本仓库）或 ``pta-resolver\\``（旧布局）；
      4. 打包形状：父目录下的 ``_internal\\core\\``。
    """
    here = Path(__file__).resolve().parent
    cands = []
    env = os.environ.get("PTA_RESOLVER_DIR")
    if env:
        cands.append(Path(env))
    cands += [here, here.parent, here.parent / "core", here.parent / "pta-resolver",
              here.parent / "_internal" / "core"]
    for cand in cands:
        try:
            if (cand / "pta_to_resolver_xml.py").is_file():
                return cand
        except OSError:
            continue
    return cands[0]


RESOLVER = _find_resolver_dir()
if str(RESOLVER) not in sys.path: sys.path.insert(0,str(RESOLVER))
from pta_to_resolver_xml import compute_ranking

def load_json(path: Path)->dict[str,Any]: return json.loads(path.read_text(encoding="utf-8"))

# --------------------------------------------------------------------------
# 数据文件健康：降级运行 + 自愈（2026-10-11 蓝屏事故后加）
# --------------------------------------------------------------------------
#: 进程内记录的数据文件问题，给 /health 的 data_error 用（正常时为空）。
DATA_ERRORS: list[str] = []
#: 启动自愈做过的事（写日志/证据用）。
REPAIRS: list[str] = []


def note_data_error(message: str) -> str:
    """记一条数据文件问题；同一条只记一次，返回记下的字符串。"""
    if message not in DATA_ERRORS:
        DATA_ERRORS.append(message)
    return message


def data_error() -> str | None:
    return DATA_ERRORS[0] if DATA_ERRORS else None


def note_repair(message: str) -> str:
    """记一条自愈动作；**同一条只记一次**（/health 的 repairs 不允许重复）。"""
    if message not in REPAIRS:
        REPAIRS.append(message)
    return message


def backup_path(path: Path) -> Path:
    """上一份好数据的路径：``contest.json`` -> ``contest.json.bak``（只保留 1 份）。"""
    path = Path(path)
    return path.with_name(path.name + ".bak")


def valid_json_file(path: Path) -> bool:
    """文件存在且能解析成 JSON 才算「好数据」。"""
    try:
        json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return True
    except (OSError, ValueError):
        return False


def load_json_tolerant(path: Path, default: dict[str,Any]|None=None) -> tuple[Any,dict[str,Any]]:
    """容错读 JSON：坏读 → 试 ``.bak`` → 再不行用 default，**绝不抛异常**。

    返回 ``(数据, 状态)``，状态里带 ``ok``/``source``/``error``，方便上层写日志和 /health。
    """
    path = Path(path)
    empty: dict[str,Any] = {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), {"ok": True, "source": path.name, "error": None}
    except FileNotFoundError:
        return empty, {"ok": False, "source": None, "error": f"{path.name} 不存在"}
    except (OSError, ValueError) as exc:
        bak = backup_path(path)
        detail = f"{path.name} 无法解析（{exc.__class__.__name__}: {exc}）"
        if bak.is_file():
            try:
                data = json.loads(bak.read_text(encoding="utf-8-sig"))
                note_data_error(f"{detail}，已回退备份 {bak.name}")
                return data, {"ok": False, "source": bak.name, "error": detail}
            except (OSError, ValueError) as exc2:
                detail += f"；备份 {bak.name} 也读不了（{exc2.__class__.__name__}: {exc2}）"
        note_data_error(detail + "，已用空态降级运行")
        return empty, {"ok": False, "source": None, "error": detail}


def repair_data_files(contest_path: Path, state_path: Path, *, log=None) -> list[str]:
    """启动自愈：坏 ``contest.json`` 用 ``.bak`` 恢复；坏/缺 ``state.json`` 从 contest 重建。

    返回做过的事（空列表 = 一切正常）。不抛异常：自愈失败也要让服务能降级起来。
    """
    def say(message: str) -> None:
        # note_repair 自带去重：以前 REPAIRS.append() 之后又回调 log()（=presentation._repair）
        # 再 append 一次，/health 的 repairs 里同一句出现两遍。
        note_repair(message)
        if log is not None:
            log(message)

    contest_path, state_path = Path(contest_path), Path(state_path)
    # ① contest.json：坏了就用备份顶上去
    if not valid_json_file(contest_path):
        bak = backup_path(contest_path)
        if valid_json_file(bak):
            try:
                data = bak.read_bytes()
                write_bytes_atomic(contest_path, data)
                say(f"已自动恢复 {contest_path.name}（来自 {bak.name}，{len(data)} 字节）")
            except OSError as exc:
                say(f"自动恢复 {contest_path.name} 失败：{exc}")
                note_data_error(f"{contest_path.name} 已损坏且自动恢复失败：{exc}")
        elif contest_path.is_file():
            say(f"{contest_path.name} 已损坏且没有可用备份 —— 服务将降级运行")
            # 无备份可回退时必须让 /health 的 data_error 非空：只写 repairs 的话，
            # state.json 还好、没人读过 contest.json 时 data_error 会是 null（等于瞒报）。
            note_data_error(f"{contest_path.name} 无法解析且没有可用备份，已用空态降级运行")
        else:
            note_data_error(f"{contest_path.name} 不存在，已用空态降级运行")
    # ② state.json：缺了或坏了，就用 contest.json 现成函数重建（没 poller 也能出终局榜）
    if not valid_json_file(state_path) and valid_json_file(contest_path):
        try:
            contest, _ = load_json_tolerant(contest_path, {})
            stamp = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
            json_dump_atomic(state_path, make_state(contest, channel="http", last_poll=stamp,
                                                    event={"type": "rebuild", "new_submissions": 0}))
            say(f"已从 {contest_path.name} 重建 {state_path.name}"
                f"（{len(contest.get('submissions') or [])} 提交，榜单由 board_core 重算）")
        except Exception as exc:                       # noqa: BLE001 - 自愈不许拖垮启动
            say(f"重建 {state_path.name} 失败：{exc.__class__.__name__}: {exc}")
    return list(REPAIRS)

def cutoff_seconds(contest) -> float:
    """封榜时刻（秒）：``duration_seconds - freeze_seconds``；没有封榜时返回 0。"""
    cmeta=contest.get("contest") or {}
    duration=float(cmeta.get("duration_seconds") or 0); freeze=float(cmeta.get("freeze_seconds") or 0)
    return max(0.0,duration-freeze) if freeze else 0.0


def _post_freeze_counts(contest,team_id,pid,freeze_at):
    """封榜时刻**之后**的提交计数（观众黄格用）。

    M11 新增，只读；返回 ``(wrong_after, accepted_after, pending_after)``：
      * ``pending`` = 有提交但还没有判定结果（PTA 判题中，status 仍是 PENDING/JUDGING）；
      * 编译错误（COMPILE_ERROR）按既有口径**不算罚时**，这里也不计入 ``wrong_after``。
    不改变任何已有字段的含义。
    """
    wrong_after=0; accepted_after=0; pending_after=0
    for sub in contest.get("submissions",[]):
        if str(sub.get("userId"))!=team_id: continue
        if str(sub.get("problemSetProblemId") or "")!=pid: continue
        rel=float(sub.get("rel_time_seconds") or 0)
        if rel<=freeze_at: continue
        status=str(sub.get("status") or "UNKNOWN").upper()
        if status in ("PENDING","JUDGING"):
            pending_after+=1
        elif status=="ACCEPTED":
            accepted_after+=1
        elif status!="COMPILE_ERROR":
            wrong_after+=1
    return wrong_after,accepted_after,pending_after


def _active_team_ids(contest): return {str(s.get("userId")) for s in contest.get("submissions",[]) if s.get("userId") is not None}
def _status_cells(contest,team_id,freeze_at=None,post_contest=None):
    """逐题格状态：``pending``（有一次提交但还没判定）/ ``wrong`` / ``ac`` / ``locked``。

    ``contest`` 决定**已判定的结果**（观众视角传的就是截断到封榜时刻的那份）；
    ``post_contest``（可选）是**完整**数据，只用来标「这格有封榜后提交」（黄格）。
    两个参数分开是必须的：观众快照的榜单本体必须先截断，否则封榜后的名次/积分会漏出去。

    M11 追加字段（**不改已有键的含义**）：``wrong_before``/``wrong_after`` 把
    ``wrong`` 拆成「封榜前 / 封榜后」两段，另有布尔 ``post`` 表示这格有封榜后提交、
    ``pending`` 表示这格有一次还没判定的提交（前端绿黑闪烁）、
    ``seen`` 表示这格有提交（哪怕是封榜后被藏起来的那次）。
    """
    def _mark_last(cell, rel):
        # M17 新增键（只增不改）：这一格**最后一次提交**的相对分钟数（整数、向下取整），
        # 不管那次是 AC/WA/判题中 —— 前端 WA 格也要显示时间（用户逐条整改 2）。
        m=int(rel//60)
        prev=cell.get("last_submit_minutes")
        cell["last_submit_minutes"]=m if prev is None else max(int(prev),m)

    def _mark_post(cell, rel):
        # M17 新增键：封榜后**最后一次**提交的相对分钟数（整数、向下取整）—— 前端琥珀格显示 `299'` 用。
        # 只新增键；同一格多次封榜后提交取最大分钟数。
        m=int(rel//60)
        prev=cell.get("post_last_minutes")
        cell["post_last_minutes"]=m if prev is None else max(int(prev),m)

    cells={}; ordered=sorted(contest.get("submissions",[]),key=lambda s:(s.get("rel_time_seconds") or 0,str(s.get("id") or "")))
    for sub in ordered:
        if str(sub.get("userId"))!=team_id: continue
        pid=str(sub.get("problemSetProblemId") or ""); rel=int(sub.get("rel_time_seconds") or 0)
        if not pid: continue
        status=str(sub.get("status") or "UNKNOWN").upper(); cell=cells.setdefault(pid,{"state":"pending","wrong":0,"time":None})
        # M45（用户 2026-10-18）：**已经通过（或封榜后已通过）之后的一切提交都作废** ——
        #   不计数、不标黄格、不改「最后一次提交」时间，彻底当没发生过。
        #   M48：判据必须是「真的 AC 过」（time 有值），占位/未判定的 locked 不算。
        if cell.get("state") in ("ac", "locked") and cell.get("time") is not None:
            continue
        cell["seen"]=True; _mark_last(cell,rel)
        cell.setdefault("pending",False)
        after=freeze_at is not None and rel>freeze_at
        if status in ("PENDING","JUDGING"):
            cell["pending"]=True
            if after: cell["post"]=True; _mark_post(cell,rel)
            continue
        if cell["state"]=="ac":
            if after: cell["post"]=True; _mark_post(cell,rel)
            continue
        if status=="ACCEPTED":
            cell["state"]="locked" if after else "ac"; cell["time"]=rel
            if after: cell["post"]=True; _mark_post(cell,rel)
        elif status!="COMPILE_ERROR":
            cell["wrong"]=int(cell.get("wrong",0))+1
            cell["wrong_after" if after else "wrong_before"]=int(cell.get("wrong_after" if after else "wrong_before",0))+1
            if after: cell["post"]=True; _mark_post(cell,rel)
            if cell["state"]=="pending": cell["state"]="wrong"
    if post_contest is not None and freeze_at is not None:
        # 只补「封榜后」的标记；已判定结果仍以 ``contest``（截断后）为准。
        ordered=sorted(post_contest.get("submissions",[]),
                       key=lambda s:(s.get("rel_time_seconds") or 0,str(s.get("id") or "")))
        for sub in ordered:
            if str(sub.get("userId"))!=team_id: continue
            pid=str(sub.get("problemSetProblemId") or "")
            if not pid: continue
            rel=float(sub.get("rel_time_seconds") or 0)
            if rel<=freeze_at:
                continue
            status=str(sub.get("status") or "UNKNOWN").upper()
            cell=cells.get(pid)
            if cell is None:
                # 封榜前这格没有任何提交：黄格 +（若还在判题）闪烁；state 仍是封榜语义。
                cell=cells.setdefault(pid,{"state":"locked","wrong":0,"time":None,"seen":True,
                                           "wrong_before":0,"wrong_after":0,"pending":False,"post":False})
            if cell.get("state") in ("ac", "locked") and cell.get("time") is not None:
                continue        # M48：真的 AC 过才算作废（占位格要继续标黄）
            cell["seen"]=True; _mark_last(cell,rel)
            cell.setdefault("wrong_before",0); cell.setdefault("wrong_after",0)
            cell["post"]=True; _mark_post(cell,rel)
            if status=="COMPILE_ERROR":
                continue                               # CE 不算次数、不算罚时（与封榜前口径一致）
            if status in ("PENDING","JUDGING"):
                cell["pending"]=True                   # 封榜后还在判：黄格呼吸（不揭晓）
            elif status=="ACCEPTED":
                pass                                   # 封榜后的 AC：保持封榜语义（黄格，不揭晓）
            # M48：封榜后的提交同样计入「次数」（除 CE 外，含判题中），
            #   这样格子上的数字和悬停卡里列出的记录条数才对得上。
            cell["wrong_after"]=int(cell.get("wrong_after",0))+1
            cell["wrong"]=int(cell.get("wrong",0))+1
    return cells

def build_board(contest,*,display_time=None,freeze_mode="operator"):
    """榜单行（名次/队伍/题格）。

    M11 起 ``freeze_at`` 的判定改为**只看是否已过封榜时刻**（不再要求 ``display_time < duration``）：
    封榜一旦开始就**保持快照**，比赛结束也不自动揭示 —— 是否揭示由展示层的
    ``thaw`` / ``freeze=operator`` 决定（见 :func:`frozen_cell_ids` 与 ``presentation.metadata``）。
    """
    if freeze_mode not in ("operator", "audience"):
        raise ValueError("freeze_mode 必须为 operator 或 audience")
    cmeta=contest.get("contest") or {}
    duration=float(cmeta.get("duration_seconds") or 0); freeze=float(cmeta.get("freeze_seconds") or 0)
    cutoff=max(0.0,duration-freeze)
    audience_view=bool(freeze and display_time is not None and display_time>=cutoff)
    full_contest=contest                                # 完整数据：只用来标「封榜后提交」（黄格）
    if audience_view:
        # 必须在排名计算前截断；名次、积分、队伍集合和题格使用同一时刻的数据。
        contest=copy.deepcopy(contest)
        contest["submissions"]=[s for s in contest.get("submissions",[]) if float(s.get("rel_time_seconds") or 0)<=cutoff]
    penalty=int((contest.get("contest") or {}).get("penalty_minutes") or 20)
    # PTA 榜单按整分钟显示；仍调用已验证的 compute_ranking。
    rank_input=copy.deepcopy(contest)
    for sub in rank_input.get("submissions",[]):
        if sub.get("rel_time_seconds") is not None: sub["rel_time_seconds"]=(int(sub["rel_time_seconds"])//60)*60
    ranked=compute_ranking(rank_input,penalty)
    active=_active_team_ids(contest); probs=contest.get("problems") or []
    # 封榜判定（M11）：只看"是否已过封榜时刻"，比赛结束也一样。
    #   * 观众视角（audience_view）：榜单本体已经截断在封榜时刻，题格也必须用同一个 freeze_at
    #     来打「封榜后提交」标记 —— 否则前端算不出黄色段（曾经的 bug：观众快照传 None）。
    #   * 实时视角：freeze_at = 封榜时刻，用于把封榜后的格子标成 locked/黄。
    freeze_at=cutoff if freeze else None
    out=[]
    for e in ranked:
        tid=str(e["team_id"])
        if tid not in active: continue
        # 观众快照只统计到封榜时刻；实时视图统计到当前 display_time。post_contest 用于黄格标记。
        cell_freeze = cutoff if audience_view else (freeze_at if (display_time is not None and freeze_at is not None and display_time >= freeze_at) else None)
        post_src = full_contest if (cell_freeze is not None and full_contest is not contest) else None
        team=next((t for t in contest.get("teams",[]) if str(t.get("userId"))==tid),{}); cells=_status_cells(contest,tid,cell_freeze,post_src); plist=[]
        for p in probs:
            pid=str(p.get("id")); cell=copy.deepcopy(cells.get(pid,{"state":"pending","wrong":0,"time":None})); cell.setdefault("wrong_before",0); cell.setdefault("wrong_after",0); cell.setdefault("post",False); cell.setdefault("pending",False); cell.update({"problem_id":pid,"label":p.get("label") or p.get("letter") or "?"}); plist.append(cell)
        out.append({"rank":len(out)+1,"team_id":tid,"team_name":team.get("name") or e.get("name") or tid,"student_number":str(team.get("studentNumber") or e.get("student_number") or ""),"solved":int(e.get("solved_count",0)),"penalty":int(e.get("penalty",0)),"problems":plist})
    return out

def contest_over(contest,display_time) -> bool:
    """比赛时间是否已走完（``display_time >= duration``）；duration 缺失时为 False。"""
    duration=float((contest.get("contest") or {}).get("duration_seconds") or 0)
    return bool(duration and display_time is not None and display_time>=duration)


def frozen_cell_ids(contest,freeze_at,display_time=None):
    """封榜后发生提交的格子 ``userId:problemId``（观众视角下这些格子藏真实结果）。

    ``display_time`` 传 None 表示「封榜后的全部提交」；传数值则只算到那一刻。
    """
    out=set()
    for sub in contest.get("submissions",[]):
        rel=float(sub.get("rel_time_seconds") or 0)
        if rel<=freeze_at: continue
        if display_time is not None and rel>display_time: continue
        out.add(str(sub.get("userId"))+":"+str(sub.get("problemSetProblemId")))
    return sorted(out)


def teams_expected(contest) -> int:
    """**期望**能进榜的队伍数（M14「拉取不全」的判据）。

    口径：在**已知题目**上有过提交的队伍数（``userId`` 去重）。
    只要数据是完整的，榜单行数就必须等于这个数：
      * 榜单只收「有提交的队伍」，所以"名录里有、但一次没交"的队不算缺；
      * 反过来，**有提交却没进榜**就是真的缺 —— 上游改过 userId、名录过期
        （``teams`` 里没有这个 userId）时就会出现，正是用户反馈的"才拉到 17 个"。
    返回 ``-1`` 表示 contest 不含题目信息（空态/降级），此时不做判定。
    """
    pids = {str(p.get("id")) for p in (contest.get("problems") or []) if p.get("id")}
    if not pids:
        return -1
    seen = set()
    for sub in contest.get("submissions") or []:
        uid = sub.get("userId")
        if uid is None:
            continue
        if str(sub.get("problemSetProblemId") or "") in pids:
            seen.add(str(uid))
    return len(seen)


def missing_from_roster(contest) -> list[str]:
    """有提交、却不在 ``contest["teams"]`` 名录里的队伍 id（诊断用，已排序）。

    这些队**进不了榜**（``compute_ranking`` 只遍历名录），是"拉取不全"的直接原因；
    /health 把它列出来，界面上「数据可能不全」的告警就有据可查。
    """
    roster = {str(t.get("userId")) for t in (contest.get("teams") or []) if t.get("userId") is not None}
    pids = {str(p.get("id")) for p in (contest.get("problems") or []) if p.get("id")}
    out = set()
    for sub in contest.get("submissions") or []:
        uid = sub.get("userId")
        if uid is None or str(sub.get("problemSetProblemId") or "") not in pids:
            continue
        if str(uid) not in roster:
            out.add(str(uid))
    return sorted(out)


def problem_stats(contest,display_time=None):
    """每题统计（M23 只增不改的派生数据）：``{problem_id: {"ac_teams": n, "subs": m}}``。

    口径（用户 2026-10-17 指定）：
      * ``ac_teams`` = 该题 **AC 的不同队伍数**（``userId`` 去重；**封榜后仍算**，
        即不看 ``freeze_at``，只看 ``display_time`` —— 回放到某一刻时按那一刻截断）；
      * ``subs`` = 该题 **提交总次数**（任何判题结果都算，含编译错误/判题中）。
    只做计数，不参与排名、不改变任何已有键；题目集合与 ``contest["problems"]`` 一致。
    """
    pids=[str(p.get("id")) for p in contest.get("problems") or [] if p.get("id") is not None]
    ac_teams={pid:set() for pid in pids}; subs={pid:0 for pid in pids}
    # M45（用户 2026-10-18）：一队一题通过之后，它后面再交的都不作废但也不计数 ——
    #   所以先把提交按时间排好，再跳过「该队该题已通过」之后的那些。
    ordered=sorted(contest.get("submissions") or [],
                   key=lambda s:(float(s.get("rel_time_seconds") or 0),str(s.get("id") or "")))
    solved=set()
    for sub in ordered:
        pid=str(sub.get("problemSetProblemId"))
        if pid not in ac_teams: continue
        if display_time is not None and sub.get("rel_time_seconds") is not None:
            try:
                if float(sub["rel_time_seconds"])>float(display_time): continue
            except (TypeError,ValueError): pass
        uid=str(sub.get("userId"))
        if (uid,pid) in solved: continue          # 通过之后的提交作废：不计次数
        subs[pid]+=1
        if str(sub.get("status") or "").upper()=="ACCEPTED" and sub.get("userId") is not None:
            ac_teams[pid].add(uid); solved.add((uid,pid))
    return {pid:{"ac_teams":len(ac_teams[pid]),"subs":subs[pid]} for pid in pids}


def make_state(contest,*,channel,last_poll,display_time=None,event=None):
    """轮询器落盘的 state：**封榜后永久保持封榜**，比赛结束也不自动揭示。

    ``frozen`` 语义（M11 收敛为一条）：``display_time`` 已过封榜时刻 → True，此后一直 True，
    直到展示层用 ``?thaw=1``（或 ``freeze=operator`` 裁判视角）显式揭示。旧行为里
    「``now>=duration`` 就 frozen=False」让比赛一结束就自动解封，等于白封 —— 已删除。

    M14 追加键（**只增不改**）：「数据拉取全不全」的证据链 ——
    ``teams_expected`` / ``teams_active`` / ``teams_roster`` / ``teams_missing_from_roster`` /
    ``data_incomplete`` / ``data_note`` / ``fetch_report``（``pta_fetch`` 写进 contest.json 的
    那份抓取报告：页数、是否被 ``--max-pages`` 截断、用时）。
    """
    board=build_board(contest,display_time=display_time); cmeta=contest.get("contest") or {}; duration=float(cmeta.get("duration_seconds") or 0); freeze=float(cmeta.get("freeze_seconds") or 0)
    now=display_time if display_time is not None else duration
    freeze_at=max(0.0,duration-freeze) if freeze else None
    frozen=bool(freeze and now>=freeze_at)
    over=contest_over(contest,now)
    expected=teams_expected(contest); missing=missing_from_roster(contest)
    report=contest.get("fetch_report") or {}
    truncated=bool(report.get("truncated"))
    roster_n=len(contest.get("teams") or [])
    if truncated and expected>=0:
        # 被 --max-pages 截断时，"有提交的队数"和"榜单行数"会**一起变小**，
        # 光比它们相等是判不出问题的。这时拿**队伍名录**当上界（名录分页有 total 校验，
        # 不受 --max-pages 影响）——于是"少拉了几页"照样能显示成「N / 期望 M」。
        expected=max(expected, roster_n)
    incomplete=bool(expected>=0 and (len(board)<expected or truncated))
    note=None
    if incomplete:
        note="数据可能不全（%d / 期望 %d）" % (len(board),expected)
        if missing:
            note+="：有 %d 支交了题的队伍不在队伍名录里（上游 userId 变过或名录过期）" % len(missing)
        if truncated:
            note+="：本轮提交抓取被 --max-pages 截断（只扫了 %s 页），请点「全量刷新」" % report.get("pages")
    # M23：每题「AC 队伍数 / 提交总次数」写进 problems[]（只新增 ac_teams/subs 两个键）。
    _pstats=problem_stats(contest,now if display_time is not None else None)
    return {"ok":True,"last_poll":last_poll,"channel":channel,"teams":len(board),"submissions":len(contest.get("submissions") or []),"frozen":frozen,"freeze_at":freeze_at,"contest":cmeta,"problems":[{"id":p.get("id"),"label":p.get("label") or p.get("letter"),"name":p.get("name"),"ac_teams":_pstats.get(str(p.get("id")),{}).get("ac_teams",0),"subs":_pstats.get(str(p.get("id")),{}).get("subs",0)} for p in contest.get("problems") or []],"board":board,"event":event,
            # M11 新增键（不改已有键含义）：封榜快照与「比赛是否走完」。
            "freeze_snapshot_at":freeze_at if frozen else None,
            "contest_over":over,
            "frozen_cells":frozen_cell_ids(contest,freeze_at,now) if frozen else [],
            "display_time":now,
            # M14 数据完整性（只增不改）。
            "teams_expected":expected,"teams_active":len(_active_team_ids(contest)),
            "teams_roster":len(contest.get("teams") or []),
            "teams_missing_from_roster":len(missing),
            "data_incomplete":incomplete,"data_note":note,"fetch_report":report}

def write_bytes_atomic(path: Path, data: bytes) -> None:
    """把**已有字节**原子地落盘：临时文件 → flush + fsync → ``os.replace``。

    与 :func:`json_dump_atomic` 同一套路（含 fsync），给「自愈时把 ``.bak`` 拷回
    ``contest.json``」这类不做 JSON 序列化的写用：恢复动作本身也不许被蓝屏写坏。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def keep_backup(path: Path) -> None:
    """把 ``path`` 当前内容留成 ``xxx.bak``（**只保留 1 份**），且**不动 path 本身**。

    为什么用硬链接而不是 ``os.replace(path, bak)``
    ---------------------------------------------
    旧实现先把 ``contest.json`` **改名**成 ``.bak``、再 ``os.replace(tmp, path)``：
    两次重命名之间有窗口 —— 此刻 ``contest.json`` 在磁盘上**根本不存在**
    （实验 2 实测：在最后一次 replace 前崩掉，文件消失，只剩 ``.bak``）。
    硬链接是 O(1)：``.bak`` 与 ``path`` 指向同一份旧数据，``path`` 始终在位；
    随后 ``os.replace(tmp, path)`` 让 ``path`` 换到新 inode，``.bak`` 继续留住旧内容。
    文件系统不支持硬链接（FAT/exFAT/跨卷）时退回「读出来 + 原子写」。
    """
    path = Path(path)
    bak = backup_path(path)
    try:
        if bak.exists():
            os.unlink(bak)
        os.link(path, bak)
        return
    except OSError:
        pass
    try:
        write_bytes_atomic(bak, path.read_bytes())
    except OSError:
        pass


def json_dump_atomic(path:Path,value):
    """原子写：临时文件 → flush + **fsync** → 保留上一份好数据为 ``.bak`` → ``os.replace``。

    为什么必须 fsync
    ----------------
    旧实现在 ``tmp.replace(path)`` 之前**只 write_text、不 fsync**：重命名先落盘、
    数据还在页缓存里，一旦蓝屏/断电（2026-10-11 18:48 的 0x0000000A），
    目录项指向的是「新文件」而内容没写完 —— 实测 ``contest.json`` / ``state.json``
    尾部变成大段 NUL，``json.loads`` 报 ``Expecting value: line 1 column 1 (char 0)``，
    ``server.py`` 直接崩在导入期，实时榜单再也起不来。
    先 fsync 临时文件再 replace，保证「要么旧内容、要么完整新内容」。

    ``.bak`` 只保留 1 份（``xxx.bak``，见 :func:`keep_backup`），给读侧容错和启动自愈用。
    目标文件已是同样内容时直接返回：不 fsync、不换备份、不动 mtime（少蹭磁盘 + 少留窗口）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    new_bytes = payload.encode("utf-8")
    try:
        if path.is_file() and path.read_bytes() == new_bytes:
            return
    except OSError:
        pass
    # 临时文件放在同目录（跨设备 replace 会退化成拷贝，失去原子性）
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(new_bytes)
            handle.flush()
            os.fsync(handle.fileno())          # 关键：数据先真正落盘
        # 只有当前文件是好的才留备份，避免把坏数据覆盖掉上一份好数据
        if path.is_file() and valid_json_file(path):
            keep_backup(path)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


