#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pta_to_resolver_xml.py —— 把 contest.json 转成 ICPC Resolver 可读的 XML

输出两种形态：
  1) `--out contest.xml`           单个 XML 文件（<contest> 根，含 info/problem/team/run/...）
  2) `--out-dir resolver-package`  一个"比赛包"目录，内含 events.xml
                                   —— 直接把它喂给 Resolver 即可：resolver.bat resolver-package

关于文件名为什么叫 events.xml（重要，已实测）
---------------------------------------------
ICPC Resolver 2.6 用 org.icpc.tools.contest.model.feed.DiskContestSource 加载本地比赛包，
它对"事件流文件"的查找顺序是：

    event-feed.ndjson  ->  event-feed.json  ->  events.xml

只有文件名以 `xml` 结尾时才会走 XMLFeedParser 解析 <contest> 结构。
也就是说：本工具生成的 XML 必须以 `events.xml` 命名放进一个目录里，
Resolver 才会识别（详见 docs/api-notes.md 的「Resolver 侧取证」）。

XML 结构严格对齐 XMLFeedParser 实际读取的标签（由反编译字符串常量 + 上游源码核实）：

  info      : contest-id, title(->formal_name), short-title(->name),
              starttime(epoch 秒), length, scoreboard-freeze-length, penalty
              （`started` / `duration` / `penalty_time` 等别名不被读取）
  region    : external-id(->group id), name
  judgement : acronym(->判题类型 id), name, solved, penalty
              （`<id>` 会被忽略）
  language  : id, name
  problem   : id(按 1-based 序号解析为 ordinal), letter/label, name
              （`<ordinal>` 不被读取）
  team      : id, external-id, name, university(->organization),
              university-short-name, nationality
              （`<region>` 会产生 "teams/group_id" 警告，故不输出）
  run       : id, judged, language(必须写语言**名称**), problem(1-based 序号),
              team, time(相对秒), timestamp, result(判题缩写), solved, penalty
  finalized : last-gold / last-silver / last-bronze（解析器按累计名次换算成奖牌数）

三个已实测确认、且两个参考实现都写错的点（详见 docs/api-notes.md）：
  1. <run><language> 必须写名称 "c++"，写数字 "2" 会被静默丢弃 -> 语言变 null
  2. <judgement><id> 会被忽略，判题类型 id 取自 <acronym>
  3. <finalized> 的 last-* 是"累计名次"，解析器会做 bronze-=silver; silver-=gold
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pta_common as C  # noqa: E402

#: XML 文本节点里 & 和 < > 是必须转义的；引号在文本里其实不必转义，
#: 但一起转义更稳妥（与两个参考实现的行为一致），也避免将来被塞进属性时报错。
_XML_ENTITIES = {'"': "&quot;", "'": "&apos;"}


# --------------------------------------------------------------------------
# 元素输出小工具
# --------------------------------------------------------------------------

def _text(value: Any) -> str:
    return "" if value is None else str(value)


def el(indent: int, tag: str, value: Any) -> str:
    pad = "  " * indent
    return f"{pad}<{tag}>{escape(_text(value), _XML_ENTITIES)}</{tag}>\n"


# --------------------------------------------------------------------------
# XML 构建
# --------------------------------------------------------------------------

def _load_star_ids(extra: list[Any] | None = None) -> set[str]:
    """读 config.json 里的 ``star_team_ids``（打星队伍）。

    为什么在生成侧读、而不是让 GUI 传参：滚榜是冻结在 exe 里的 GUI 调
    ``pta_to_resolver_xml.main``，加参数要重新打包；而 config.json 本来就是
    运行期文件，就地读取既不用改 GUI，也能跟着用户勾选实时生效。
    读不到就返回空集合（照旧不加星）。
    """
    import json as _json

    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / "config.json",
        here.with_name("config.json"),
        here.parent / "config.json",
        here.parent.parent / "config.json",
    ]
    # 命令行从别的目录调用时，contest.json 所在目录/它的上一级往往才是程序根
    for folder in extra or []:
        try:
            candidates.append(Path(folder) / "config.json")
        except TypeError:  # pragma: no cover - 防御性
            continue
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            data = _json.loads(cand.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001 - 配置读坏了不该影响出榜
            continue
        raw = data.get("star_team_ids") if isinstance(data, dict) else None
        if isinstance(raw, list):
            return {str(x) for x in raw if x is not None}
    return set()


def _with_star(name: str, team_id: str, star_ids: set[str]) -> str:
    """打星队伍在滚榜里显示成「队名 ★」，和题目格上的星标一个意思。"""
    if star_ids and str(team_id) in star_ids and "★" not in name:
        return f"{name} ★"
    return name


def compute_ranking(contest: dict[str, Any], penalty_minutes: int) -> list[dict[str, Any]]:
    """按 ICPC 规则算出榜单顺序：解题数降序，罚时升序。

    罚时规则与 docs/resolver-howto.md 第 9 节一致：
      * 通过的题：第一次 AC 的相对分钟数 + 该题 AC 之前每次「算罚时」的提交 × 罚时分钟；
      * 编译错误（CE）算罚时 = false，不计；
      * AC 之后的错误提交不再计入。

    Resolver 自己也是按这套规则排的，所以这里算出来的名次和滚榜画面一致。
    """
    teams = contest.get("teams") or []
    submissions = contest.get("submissions") or []
    problems = contest.get("problems") or []

    valid_problems = {str(p["id"]) for p in problems if p.get("id")}
    by_acronym = {j["acronym"]: j for j in C.JUDGEMENTS}

    # 用户 2026-10-27：VJudge 的罚时是「每道通过的题按整分钟向上取整」再相加
    #   （实测 13 题差 6 分钟 = 每题的小数部分被抹掉）。PTA 那边保持原来的精确相加，
    #   不要因为改这个把已经对齐的 PTA 榜弄偏。
    ceil_ac_minutes = str(contest.get("source") or "").lower() == "vjudge"

    state: dict[str, dict[str, Any]] = {}
    for team in teams:
        tid = team.get("userId")
        if tid is None:
            continue
        state[str(tid)] = {
            "team_id": str(tid),
            "name": team.get("name") or "",
            "student_number": str(team.get("studentNumber") or ""),
            "solved": set(),
            "wrong": {},
            "penalty": 0.0,
        }

    ordered = sorted(
        submissions,
        key=lambda s: (s.get("rel_time_seconds") or 0, str(s.get("id") or "")),
    )
    for sub in ordered:
        tid = str(sub.get("userId") or "")
        pid = str(sub.get("problemSetProblemId") or "")
        entry = state.get(tid)
        if entry is None or pid not in valid_problems:
            continue
        rel = sub.get("rel_time_seconds")
        if rel is None:
            continue
        if pid in entry["solved"]:
            continue
        status = (sub.get("status") or "UNKNOWN").upper()
        acronym = C.PTA_STATUS_TO_ACRONYM.get(status, C.UNKNOWN_STATUS_FALLBACK)
        judgement = by_acronym.get(acronym)
        if judgement is None:
            continue
        if judgement["solved"]:
            # 取整口径统一放在 board_core（按来源 ceil/floor），这里只做精确相加，
            # 否则 VJudge 会被"取整两次"，罚时反而比网页多几分钟。
            entry["penalty"] += max(0.0, float(rel)) / 60.0
            entry["penalty"] += penalty_minutes * entry["wrong"].get(pid, 0)
            entry["solved"].add(pid)
        elif judgement["penalty"]:
            entry["wrong"][pid] = entry["wrong"].get(pid, 0) + 1

    ranked = sorted(
        state.values(),
        key=lambda e: (-len(e["solved"]), e["penalty"], e["name"], e["team_id"]),
    )
    for idx, entry in enumerate(ranked):
        entry["rank"] = idx + 1
        entry["solved_count"] = len(entry["solved"])
        # 用户 2026-10-27：VJudge 网页的罚时是「秒数总和 / 60 后**向下取整**」
        #   （6 支队逐一核对过）；PTA 仍用四舍五入，保持原有对齐。
        if ceil_ac_minutes:
            entry["penalty"] = int(entry["penalty"])
        else:
            entry["penalty"] = int(round(entry["penalty"]))
    return ranked


#: 奖项的 id 前缀。id 只决定奖项"类型"（ICPC 用它挑图标/排序），
#: 比如 'gold-medal' 命中 Medal 正则、'honors-N' 命中 Honors 正则。
#: 真正显示给观众看的是 <citation>，所以统一用 honors-N 就够了。
_AWARD_ID_PREFIX = "honors-"
#: 实测（附录 H）：XMLFeedParser 把 <show> 当**布尔**解析 ——
#:   parseBoolean(value) ? DisplayMode.DETAIL : DisplayMode.PAUSE
#: 所以 events.xml 只能表达 DETAIL / PAUSE 两种展示方式；
#: DisplayMode.PHOTOS / LIST **无法从 XML 传入**（这也是队伍照片不显示的原因）。
AWARD_SHOW_TO_XML = {"detail": "true", "pause": "false"}
_AWARD_SHOW_CHOICES = tuple(AWARD_SHOW_TO_XML)

#: 经典"金/银/铜"摘要占的位数（只在 --medal-summary 打开时用）。
_MEDAL_SLOTS = 3


def build_award_blocks(
    contest: dict[str, Any],
    awards_spec: list[dict[str, Any]],
    penalty_minutes: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把「奖项名 + 人数」换算成 Resolver 认识的 `<award>` 块。

    从榜单第 1 名开始依次切分：第 1 个奖项取前 N1 名，第 2 个取接下来 N2 名……
    返回 (奖项列表, 警告列表)。

    每个奖项写进 events.xml 的形状（字段来自 ref/XMLFeedParser.java 第 586-601 行）::

        <award>
          <id>gold-medal</id>          <- id 决定奖项"类型"（正则匹配，见上）
          <citation>一等奖</citation>   <- 显示给观众看的名字，随便写
          <show>photos</show>          <- 展示方式：photos/list/detail/pause
          <teamId>2107387926386905096</teamId>
          ...
        </award>
    """
    warnings: list[str] = []
    if not awards_spec:
        return [], warnings

    ranked = compute_ranking(contest, penalty_minutes)
    if not ranked:
        return [], ["比赛里没有任何队伍，无法分配奖项。"]

    blocks: list[dict[str, Any]] = []
    cursor = 0
    for idx, row in enumerate(awards_spec):
        name = str(row.get("name") or "").strip() or f"奖项 {idx + 1}"
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            warnings.append(f"奖项「{name}」的人数不是正整数，已跳过。")
            continue
        # 只认 detail / pause；老配置里的 photos / list 一律归到 detail，
        # 因为 PAUSE 只会停住不动，DETAIL 才会把奖项画面显示出来。
        show = str(row.get("show") or "detail").strip().lower()
        if show not in _AWARD_SHOW_CHOICES:
            show = "detail"

        picked = ranked[cursor:cursor + count]
        if not picked:
            warnings.append(f"奖项「{name}」还想给 {count} 队，但榜单上已经没有人了，已跳过。")
            continue
        if len(picked) < count:
            warnings.append(
                f"奖项「{name}」设置 {count} 队，但只剩 {len(picked)} 队，实际只发 {len(picked)} 队。"
            )
        cursor += len(picked)

        # 每个奖项都用 honors-N 作 id：id 只决定"类型"（图标/排序），
        # 真正显示给观众看的是 <citation>，所以不用纠结用哪种 id。
        award_id = f"{_AWARD_ID_PREFIX}{idx + 1}"
        blocks.append({
            "id": award_id,
            "citation": name,
            "show": show,
            "count": len(picked),
            "team_ids": [t["team_id"] for t in picked],
            "teams": [{"rank": t["rank"], "name": t["name"],
                       "student_number": t["student_number"],
                       "solved": t["solved_count"], "penalty": t["penalty"]} for t in picked],
        })

    if cursor < len(ranked):
        warnings.append(f"还有 {len(ranked) - cursor} 支队伍没有分到任何奖项（不影响滚榜）。")
    return blocks, warnings


def build_group_award_blocks(
    contest: dict[str, Any],
    groups_spec: list[dict[str, Any]],
    penalty_minutes: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把「奖项组 + 组里的队伍 ID」换算成 Resolver 的 ``<award>`` 块（打星模式）。

    与 :func:`build_award_blocks` 的区别（**这是打星模式的核心**）：

    * 按名次的奖项**完全不参与** —— 每组的获奖人就是 ``team_ids`` 里那批队，
      不按名次切区间；
    * 一血奖照旧由 :func:`build_first_to_solve_blocks` 每题一个（本函数不管）；
    * ``citation`` = **组名**（新手组 / 老登组 / 最佳女队…），所以观众看到的就是组名。

    输入形状::

        [{"name": "新手组", "show": "detail", "team_ids": ["1001", "1002"]}, …]

    组内队伍按**名次**排序（滚榜里同组也是从高到低揭晓）；``team_ids`` 里查不到的队
    （数据更新过、ID 对不上）会被跳过并给警告 —— 不静默吞掉。
    """
    warnings: list[str] = []
    if not groups_spec:
        return [], warnings

    ranked = compute_ranking(contest, penalty_minutes)
    by_id = {t["team_id"]: t for t in ranked}
    if not ranked:
        return [], ["比赛里没有任何队伍，无法分配奖项。"]

    blocks: list[dict[str, Any]] = []
    for idx, row in enumerate(groups_spec):
        name = str(row.get("name") or "").strip() or f"第 {idx + 1} 组"
        show = str(row.get("show") or "detail").strip().lower()
        if show not in _AWARD_SHOW_CHOICES:
            show = "detail"
        raw_ids = row.get("team_ids")
        team_ids = [str(t).strip() for t in raw_ids] if isinstance(raw_ids, (list, tuple)) else []
        team_ids = [t for t in team_ids if t]

        missing = [t for t in team_ids if t not in by_id]
        picked = sorted((by_id[t] for t in team_ids if t in by_id), key=lambda t: t["rank"])
        if not picked:
            warnings.append(f"奖项组「{name}」一个队都没有（勾 0 队），已跳过。")
            continue
        if missing:
            warnings.append(
                f"奖项组「{name}」里有 {len(missing)} 个队伍 ID 在本次数据里找不到，已跳过："
                + "、".join(missing[:6]) + ("…" if len(missing) > 6 else ""))

        blocks.append({
            "id": f"{_AWARD_ID_PREFIX}{len(blocks) + 1}",
            "citation": name,
            "show": show,
            "count": len(picked),
            "team_ids": [t["team_id"] for t in picked],
            "teams": [{"rank": t["rank"], "name": t["name"],
                       "student_number": t["student_number"],
                       "solved": t["solved_count"], "penalty": t["penalty"]} for t in picked],
            "group_index": idx,
            "missing_team_ids": missing,
        })
    return blocks, warnings


def compute_first_to_solve(contest: dict[str, Any], penalty_minutes: int) -> list[dict[str, Any]]:
    """逐题找出「第一个通过」的队伍（一血 / 最快解题奖）。

    判定用的是和榜单同一套判题规则（见 compute_ranking）：按相对时间排序，
    某题**第一条 AC** 的队伍就是该题一血。封榜不影响这里 —— 一血是终榜口径，
    封榜只影响滚榜时揭晓的顺序。
    """
    problems = contest.get("problems") or []
    submissions = contest.get("submissions") or []
    teams_by_id = {str(t.get("userId")): t for t in (contest.get("teams") or []) if t.get("userId")}
    valid_problems = {str(pp["id"]) for pp in problems if pp.get("id")}
    by_acronym = {j["acronym"]: j for j in C.JUDGEMENTS}

    letter_of = {}
    name_of = {}
    for idx, item in enumerate(problems):
        if not item.get("id"):
            continue
        pid = str(item["id"])
        letter_of[pid] = item.get("letter") or (chr(ord("A") + idx) if idx < 26 else f"P{idx + 1}")
        name_of[pid] = item.get("name") or letter_of[pid]

    first: dict[str, dict[str, Any]] = {}
    ordered = sorted(submissions,
                     key=lambda s: (s.get("rel_time_seconds") or 0, str(s.get("id") or "")))
    for sub in ordered:
        pid = str(sub.get("problemSetProblemId") or "")
        tid = str(sub.get("userId") or "")
        if pid in first or pid not in valid_problems or tid not in teams_by_id:
            continue
        rel = sub.get("rel_time_seconds")
        if rel is None:
            continue
        status = (sub.get("status") or "UNKNOWN").upper()
        judgement = by_acronym.get(C.PTA_STATUS_TO_ACRONYM.get(status, C.UNKNOWN_STATUS_FALLBACK))
        if judgement is None or not judgement["solved"]:
            continue
        first[pid] = {
            "problem_id": pid,
            "letter": letter_of.get(pid, "?"),
            "problem_name": name_of.get(pid, ""),
            "team_id": tid,
            "team_name": str(teams_by_id[tid].get("name") or ""),
            "rel_seconds": int(rel),
            "rel": C.format_hms(rel),
        }
    return [first[pp] for pp in sorted(first, key=lambda k: letter_of.get(k, k))]


def build_first_to_solve_blocks(contest: dict[str, Any], penalty_minutes: int) -> list[dict[str, Any]]:
    """把一血奖写成 <award> 块。

    id 必须是 ``first-to-solve-<题目id>``（AwardUtil 里的模式就是 ``first-to-solve-*``，
    见 IAward.AwardType 的正则 ``first-to-solve-.*``），这样 Resolver 才会把它
    当成 First-to-Solve 类型的奖。citation 显示给观众，所以写中文。
    """
    blocks: list[dict[str, Any]] = []
    for item in compute_first_to_solve(contest, penalty_minutes):
        blocks.append({
            "id": f"first-to-solve-{item['problem_id']}",
            "citation": f"一血 · {item['letter']} 题（{item['rel']}）",
            "show": "detail",
            "count": 1,
            "team_ids": [item["team_id"]],
            "teams": [{"rank": 0, "name": item["team_name"], "student_number": "",
                       "solved": 0, "penalty": 0}],
            "first_to_solve": item,
        })
    return blocks

def build_xml(
    contest: dict[str, Any],
    organization: str,
    region_id: str,
    medals: tuple[int, int, int],
    include_finalized: bool = True,
    awards: list[dict[str, Any]] | None = None,
    medal_summary: bool = False,
    first_to_solve: bool = False,
    award_groups: list[dict[str, Any]] | None = None,
    star_ids: set[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """生成 XML 文本 + 一份统计/映射报告。

    ``award_groups`` 非空 = **打星模式**：按组（``team_ids``）发奖，``awards``
    （按名次的一等奖/二等奖…）**不参与**；两者同时给以 ``award_groups`` 为准。
    """
    if star_ids is None:
        star_ids = _load_star_ids()
    else:
        star_ids = {str(x) for x in star_ids}

    meta = contest.get("contest") or {}
    problems = contest.get("problems") or []
    teams = contest.get("teams") or []
    submissions = contest.get("submissions") or []

    warnings: list[str] = []
    parts: list[str] = []
    add = parts.append

    title = meta.get("title") or contest.get("problem_set", {}).get("name") or "PTA Contest"
    start_epoch = meta.get("start_epoch")
    if start_epoch is None:
        raise SystemExit(
            "[error] contest.json 中缺少 contest.start_epoch，无法生成 XML。"
            "请先重新运行 pta_fetch.py fetch。"
        )
    duration_seconds = meta.get("duration_seconds")
    if not duration_seconds or duration_seconds <= 0:
        raise SystemExit("[error] contest.json 中缺少有效的比赛时长（duration_seconds）。")
    freeze_seconds = meta.get("freeze_seconds")
    if freeze_seconds is None:
        freeze_seconds = 3600
    penalty_minutes = meta.get("penalty_minutes")
    if penalty_minutes is None:
        penalty_minutes = 20

    add('<?xml version="1.0" encoding="UTF-8"?>\n')
    add("<contest>\n")

    # ---- info ----
    add("  <info>\n")
    add(el(2, "title", title))
    add(el(2, "short-title", meta.get("short_title") or title))
    add(el(2, "contest-id", contest.get("problem_set_id") or "0"))
    add(el(2, "starttime", f"{float(start_epoch):.1f}"))
    add(el(2, "length", C.format_hms(duration_seconds)))
    add(el(2, "scoreboard-freeze-length", C.format_hms(freeze_seconds)))
    add(el(2, "penalty", int(penalty_minutes)))
    # 注意：不要在 <info> 里写 <started>。
    # XMLFeedParser 的 INFO 分支只认 contest-id/title/short-title/starttime/length/
    # scoreboard-freeze-length/penalty，<started> 会被直接忽略（两个参考实现都输出了它，
    # 属于无害但无效的字段）。比赛状态由 starttime + length 推导。
    add("  </info>\n")

    # ---- region ----
    add("  <region>\n")
    add(el(2, "external-id", region_id))
    add(el(2, "name", organization))
    add("  </region>\n")

    # ---- judgement ----
    # 注意：<judgement> 里不要写 <id>。
    # XMLFeedParser 用 <acronym> 作为判题类型的 id（`if ("acronym".equals(p.name)) add(type, ID, p.value)`），
    # <id> 会被忽略。判题类型 id 必须是 AC / WA / TLE 这类缩写，
    # <run><result> 才能通过 getJudgementTypeById 解析成功。
    for j in C.JUDGEMENTS:
        add("  <judgement>\n")
        add(el(2, "acronym", j["acronym"]))
        add(el(2, "name", j["name"]))
        add(el(2, "solved", "true" if j["solved"] else "false"))
        add(el(2, "penalty", "true" if j["penalty"] else "false"))
        add("  </judgement>\n")

    # ---- language ----
    for lang in C.LANGUAGES:
        add("  <language>\n")
        add(el(2, "id", lang["id"]))
        add(el(2, "name", lang["name"]))
        add("  </language>\n")

    # ---- problem ----
    # 注意：<problem><id> 会被 XMLFeedParser 当作 "序号+1"（ordinal = id-1），
    # 题目最终的 id 会退化成字母 A/B/C...。<ordinal> 元素不被读取，不要输出。
    for idx, problem in enumerate(problems):
        letter = problem.get("letter") or (
            chr(ord("A") + idx) if idx < 26 else f"P{idx + 1}"
        )
        add("  <problem>\n")
        add(el(2, "id", idx + 1))
        add(el(2, "letter", letter))
        add(el(2, "name", problem.get("name") or f"Problem {letter}"))
        add("  </problem>\n")

    # ---- team ----
    # 注意：不要输出 <region>。XMLFeedParser 会用 <region> 的值去匹配一个 Group 并写入
    # team.group_id；而 Team 模型没有 group_id 这个已知属性，会产生
    # "Unknown property ignored: teams/group_id" 警告。学校信息用 <university> 表达即可
    # （解析器会据此建立 Organization 并自动挂上 organization_id）。
    seen_team_ids: set[str] = set()
    for team in teams:
        team_id = team.get("userId")
        if team_id is None:
            continue
        if team_id in seen_team_ids:
            warnings.append(f"重复的 team userId={team_id}，已跳过后续同名条目。")
            continue
        seen_team_ids.add(team_id)
        add("  <team>\n")
        add(el(2, "id", team_id))
        add(el(2, "external-id", region_id))
        # 打星队伍在滚榜计分板/奖项页的队名后面加一个 ★（用户 2026-10-23 指定）
        add(el(2, "name", _with_star(str(team.get("name") or f"Team_{team_id}"), team_id, star_ids)))
        add(el(2, "university", organization))
        add("  </team>\n")

    # ---- run ----
    problem_index: dict[str, int] = {}
    for idx, problem in enumerate(problems):
        if problem.get("id"):
            problem_index[str(problem["id"])] = idx + 1

    run_id = 0
    skipped_unknown_problem = 0
    skipped_unknown_team = 0
    skipped_no_time = 0
    unknown_status: dict[str, int] = {}
    unknown_compiler: dict[str, int] = {}

    for sub in submissions:
        pid = sub.get("problemSetProblemId")
        pid_str = str(pid) if pid is not None else None
        if pid_str is None or pid_str not in problem_index:
            skipped_unknown_problem += 1
            continue

        tid = sub.get("userId")
        tid_str = str(tid) if tid is not None else None
        if tid_str is None or tid_str not in seen_team_ids:
            skipped_unknown_team += 1
            continue

        rel = sub.get("rel_time_seconds")
        if rel is None:
            skipped_no_time += 1
            continue
        rel = max(0, int(rel))

        status = (sub.get("status") or "UNKNOWN").upper()
        acronym = C.PTA_STATUS_TO_ACRONYM.get(status)
        if acronym is None:
            unknown_status[status] = unknown_status.get(status, 0) + 1
            acronym = C.UNKNOWN_STATUS_FALLBACK
        judgement = next((j for j in C.JUDGEMENTS if j["acronym"] == acronym), None)
        if judgement is None:  # pragma: no cover
            judgement = C.JUDGEMENTS[2]

        compiler = (sub.get("compiler") or "").upper()
        lang_name, lang_known = C.compiler_to_language_name(compiler)
        if not lang_known and compiler:
            unknown_compiler[compiler] = unknown_compiler.get(compiler, 0) + 1

        timestamp = sub.get("submit_epoch")
        if timestamp is None:
            timestamp = float(start_epoch) + rel

        run_id += 1
        add("  <run>\n")
        add(el(2, "id", run_id))
        add(el(2, "judged", "true"))
        # <language> 必须写语言名称（c/c++/java/python），写数字 id 会被解析器静默忽略。
        add(el(2, "language", lang_name))
        # <problem> 写 1-based 序号：解析器做 (value-1) 后再按 ordinal 匹配。
        add(el(2, "problem", problem_index[pid_str]))
        # <status> 当前版本的 XMLFeedParser 并不读取（RUN 分支只认 id/language/problem/
        # team/time/timestamp 以及判题相关的 judged/result/solved/penalty）。
        # 保留它是为了与两个参考实现保持一致，并对未来版本前向兼容。
        add(el(2, "status", "done"))
        add(el(2, "team", tid_str))
        add(el(2, "time", rel))
        add(el(2, "timestamp", f"{float(timestamp):.2f}"))
        add(el(2, "solved", "true" if judgement["solved"] else "false"))
        add(el(2, "penalty", "true" if judgement["penalty"] else "false"))
        # <result> 写判题缩写（也是 judgement 的 id），解析器据此建立 Judgement。
        add(el(2, "result", acronym))
        add("  </run>\n")

    # ---- finalized ----
    # XMLFeedParser 会把 last-* 换算成"奖牌数量"：
    #     bronze -= silver;  silver -= gold;
    # 即 last-gold = 金牌数，last-silver = 金+银，last-bronze = 金+银+铜。
    #
    # 实测结论（2026-10-09，真实 Resolver 2.6.1331，见 docs/verification-log.txt 附录 G）：
    #   * `<award><citation>自定义名字</citation><teamId>…</teamId></award>` **可以放任意多个**，
    #     每个都会变成一个独立的奖项。主控实测 4 个自定义奖项（一等奖/二等奖/三等奖/优秀奖）
    #     时 Resolver 日志给出 `4 awards assigned`，并逐个 `Award at row: N`。
    #   * `<finalized>` 的金/银/铜是另一套（经典奖牌）。两套同时给会**重复计数**
    #     （同一队既拿「一等奖」又拿 Gold），所以默认只写零值，把 <finalized> 当作
    #     "比赛已结束"的标记用；想要经典金银铜就用 --medal-summary 打开。
    award_blocks: list[dict[str, Any]] = []
    if award_groups:
        # 打星模式：唯一奖项来源是「组」，按名次的 awards 一律忽略。
        award_blocks, award_warnings = build_group_award_blocks(
            contest, award_groups, int(penalty_minutes))
        warnings.extend(award_warnings)
    elif awards:
        award_blocks, award_warnings = build_award_blocks(contest, awards, int(penalty_minutes))
        warnings.extend(award_warnings)
    fts_blocks: list[dict[str, Any]] = []
    if first_to_solve:
        # 一血奖自动生成，用户不用管；id 必须是 first-to-solve-<题目id>
        fts_blocks = build_first_to_solve_blocks(contest, int(penalty_minutes))
        if fts_blocks:
            warnings.append(
                '已自动加入 ' + str(len(fts_blocks)) + ' 个一血奖（最快解题）：'
                + '、'.join(b['first_to_solve']['letter'] + ' ' + b['first_to_solve']['team_name']
                            for b in fts_blocks))

    if include_finalized:
        gold, silver, bronze = medals
        if medal_summary and award_blocks:
            # 经典金银铜：前三个奖项的人数当作金/银/铜的块数
            counts = [b["count"] for b in award_blocks[:_MEDAL_SLOTS]]
            while len(counts) < _MEDAL_SLOTS:
                counts.append(0)
            gold, silver, bronze = counts[0], counts[1], counts[2]
        elif award_blocks or fts_blocks:
            # 有自定义奖项：<finalized> 只留"比赛已结束"的语义，不产生奖牌
            gold = silver = bronze = 0
        last_gold = int(gold)
        last_silver = int(gold) + int(silver)
        last_bronze = int(gold) + int(silver) + int(bronze)
        add("  <finalized>\n")
        add(el(2, "last-gold", last_gold))
        add(el(2, "last-silver", last_silver))
        add(el(2, "last-bronze", last_bronze))
        add("  </finalized>\n")

    # ---- award ----（一血奖排在最前，先颁小奖再颁大奖）
    # 字段见 ref/XMLFeedParser.java 第 586-601 行：只认 teamId(可重复) / id / citation / show。
    # 每一个奖项一行 <award>，<citation> 就是界面上填的奖项名。
    for block in fts_blocks + award_blocks:
        add("  <award>\n")
        add(el(2, "id", block["id"]))
        add(el(2, "citation", block["citation"]))
        add(el(2, "show", AWARD_SHOW_TO_XML.get(block["show"], "true")))
        for team_id in block["team_ids"]:
            add(el(2, "teamId", team_id))
        add("  </award>\n")

    add("</contest>\n")

    report = {
        "title": title,
        "problem_count": len(problems),
        "team_count": len(seen_team_ids),
        "run_count": run_id,
        "submission_count": len(submissions),
        "skipped_unknown_problem": skipped_unknown_problem,
        "skipped_unknown_team": skipped_unknown_team,
        "skipped_no_time": skipped_no_time,
        "unknown_status": unknown_status,
        "unknown_compiler": unknown_compiler,
        "length": C.format_hms(duration_seconds),
        "freeze": C.format_hms(freeze_seconds),
        "penalty": int(penalty_minutes),
        "start_epoch": float(start_epoch),
        "warnings": warnings,
        "awards": award_blocks,
        "first_to_solve": fts_blocks,
        "medal_summary": bool(medal_summary and award_blocks),
        "awards_mode": "starred" if award_groups is not None else "rank",
    }
    return "".join(parts), report


# --------------------------------------------------------------------------
# CDS / Contest API 风格的 NDJSON feed（默认输出，用它才能带照片与校徽）
# --------------------------------------------------------------------------
#
# 为什么需要它（反编译 org.icpc.tools.contest.model.feed.DiskContestSource 得到）：
#   * feed 的查找顺序是 event-feed.ndjson -> event-feed.json -> events.xml，
#     文件名**不以 xml 结尾**就交给 NDJSONFeedParser 逐行解析（每行一个 JSON 对象）；
#   * XMLFeedParser 只把 award 的 teamId/id/citation/show 转发给 Award，
#     而 `show` 在 XML 里被当**布尔**解析 —— 于是 DisplayMode.PHOTOS 传不进去；
#     在 JSON 里 Award.addImpl 认的是 display_mode/show + detail|pause|list|photos 四个字符串，
#     所以 JSON feed 能表达的展示方式比 XML 多。
#   * 队伍照片 / 机构校徽其实不靠 feed 里的字段，而是靠
#     DiskContestSource.attachLocalResources -> getLocalPattern：
#         FilePattern.folder = getTypeName(type) + File.separator + id
#     也就是本地图片必须放在  <比赛包>\teams\<队伍ID>\photo.jpg
#     与                    <比赛包>\organizations\<机构ID>\logo.png
#     （**不是** <比赛包>\<队伍ID>\photo.jpg —— 这是旧版一直没挂上照片的根因）。
#
# 本函数只负责 feed 文本；图片由 app/board_backend.py 复制到上面那两个目录。

FEED_NDJSON = "event-feed.ndjson"
FEED_XML = "events.xml"


def _iso_utc(epoch: float) -> str:
    """把 unix 秒转成 Contest API 用的 ISO-8601（带 +00:00 偏移）。"""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(float(epoch), _dt.timezone.utc).isoformat()


def _ndjson_lines(objects: list[tuple[str, Any]]) -> str:
    """每行一个 ``{"type":..., "data":...}`` —— NDJSONFeedParser 的"新格式"。"""
    out = []
    for type_name, data in objects:
        out.append(json.dumps({"type": type_name, "data": data}, ensure_ascii=False))
    return "\n".join(out) + "\n"


def build_ndjson(
    contest: dict[str, Any],
    organization: str,
    region_id: str,
    medals: tuple[int, int, int],
    include_finalized: bool = True,
    awards: list[dict[str, Any]] | None = None,
    medal_summary: bool = False,
    first_to_solve: bool = False,
    org_plan: dict[str, Any] | None = None,
    award_groups: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """生成 Contest API（CDS）风格的 NDJSON feed 文本 + 统计报告。

    ``org_plan``（可选）用来做「一个校徽一个 organization」的映射，形状：
        {"organizations": [{"id": "1", "name": "PTA"},
                           {"id": "logo1", "name": "汪章正"}],
         "teams": {"<队伍ID>": "logo1"}}
    没在 ``teams`` 里的队伍一律用 ``region_id`` 那个机构（默认校徽挂它身上）。
    见 app/board_backend.py 的 match_team_logos / copy_org_logos。

    字段名全部由反编译 contestModel.jar 的 ``addImpl`` 字符串常量核实：
      info         : name / formal_name / start_time / duration /
                     scoreboard_freeze_duration / penalty_time
      judgement-type: id / name / solved / penalty
      language     : id / name
      problem      : id / label / name / ordinal / test_data_count
      team         : id / name / organization_id / group_ids
      organization : id / name / formal_name（logo 走本地文件，见上）
      submission   : id / team_id / problem_id / language_id / contest_time / time
      judgement    : id / submission_id / judgement_type_id / start|end_contest_time
      award        : id / citation / team_ids / display_mode
      state        : started / ended / frozen / finalized
    """
    meta = contest.get("contest") or {}
    problems = contest.get("problems") or []
    teams = contest.get("teams") or []
    submissions = contest.get("submissions") or []

    title = meta.get("title") or contest.get("problem_set", {}).get("name") or "PTA Contest"
    start_epoch = meta.get("start_epoch")
    if start_epoch is None:
        raise SystemExit(
            "[error] contest.json 中缺少 contest.start_epoch，无法生成 feed。"
            "请先重新运行 pta_fetch.py fetch。"
        )
    duration_seconds = meta.get("duration_seconds")
    if not duration_seconds or duration_seconds <= 0:
        raise SystemExit("[error] contest.json 中缺少有效的比赛时长（duration_seconds）。")
    freeze_seconds = meta.get("freeze_seconds")
    if freeze_seconds is None:
        freeze_seconds = 3600
    penalty_minutes = meta.get("penalty_minutes")
    if penalty_minutes is None:
        penalty_minutes = 20

    org_id = str(region_id or "1")
    warnings: list[str] = []
    objects: list[tuple[str, Any]] = []

    # ---- contest ----
    objects.append(("contest", {
        "id": "main",
        "name": title,
        "formal_name": title,
        "start_time": _iso_utc(start_epoch),
        "duration": C.format_hms(duration_seconds),
        "scoreboard_freeze_duration": C.format_hms(freeze_seconds),
        "penalty_time": int(penalty_minutes),
    }))

    # ---- judgement-types ----
    objects.append(("judgement-types", [
        {"id": j["acronym"], "name": j["name"],
         "solved": bool(j["solved"]), "penalty": bool(j["penalty"])}
        for j in C.JUDGEMENTS
    ]))

    # ---- languages ----
    objects.append(("languages", [
        {"id": lang["id"], "name": lang["name"]} for lang in C.LANGUAGES
    ]))

    # ---- problems ----
    letter_of: dict[str, str] = {}
    problem_objs = []
    for idx, problem in enumerate(problems):
        letter = problem.get("letter") or (
            chr(ord("A") + idx) if idx < 26 else f"P{idx + 1}"
        )
        pid = str(problem.get("id") or (idx + 1))
        letter_of[pid] = letter
        problem_objs.append({
            "id": pid,
            "label": letter,
            "name": problem.get("name") or f"Problem {letter}",
            "ordinal": idx,
            # Problem.addImpl 缺 test_data_count 会打 "Test data count missing" 警告
            "test_data_count": 1,
        })
    objects.append(("problems", problem_objs))

    # ---- groups + organizations ----
    # 一个机构（学校）一个 group：这样 Resolver 的 AwardUtil.assignGroup 有东西可用，
    # 彻底避开"没有 <group> 就 NPE 崩掉"的那条分支。
    # 注意：group_ids 仍然全部指向这个唯一的 group（评奖按学校分组），
    # 只有 organization_id 会按校徽分成多个 —— 一个校徽一个 organization。
    objects.append(("groups", [{"id": org_id, "name": organization, "type": "institution"}]))

    org_objs: list[dict[str, Any]] = [
        {"id": org_id, "name": organization, "formal_name": organization}
    ]
    seen_org_ids = {org_id}
    for extra in ((org_plan or {}).get("organizations") or []):
        if not isinstance(extra, dict):
            continue
        oid = str(extra.get("id") or "").strip()
        if not oid or oid in seen_org_ids:
            continue
        oname = str(extra.get("name") or oid).strip() or oid
        seen_org_ids.add(oid)
        org_objs.append({"id": oid, "name": oname, "formal_name": oname})
    objects.append(("organizations", org_objs))

    team_orgs = {
        str(k): str(v)
        for k, v in ((org_plan or {}).get("teams") or {}).items()
        if str(v or "").strip()
    }

    # ---- teams ----
    seen_team_ids: set[str] = set()
    team_objs = []
    for team in teams:
        team_id = team.get("userId")
        if team_id is None:
            continue
        team_id = str(team_id)
        if team_id in seen_team_ids:
            warnings.append(f"重复的 team userId={team_id}，已跳过后续同名条目。")
            continue
        seen_team_ids.add(team_id)
        team_objs.append({
            "id": team_id,
            "name": team.get("name") or f"Team_{team_id}",
            "organization_id": team_orgs.get(team_id, org_id),
            "group_ids": [org_id],
        })
    objects.append(("teams", team_objs))

    # ---- submissions + judgements ----
    problem_index = {str(p["id"]): idx for idx, p in enumerate(problems) if p.get("id")}
    sub_objs = []
    judge_objs = []
    skipped_unknown_problem = 0
    skipped_unknown_team = 0
    skipped_no_time = 0
    unknown_status: dict[str, int] = {}
    unknown_compiler: dict[str, int] = {}
    run_id = 0

    for sub in submissions:
        pid = sub.get("problemSetProblemId")
        pid_str = str(pid) if pid is not None else None
        if pid_str is None or pid_str not in problem_index:
            skipped_unknown_problem += 1
            continue
        tid = sub.get("userId")
        tid_str = str(tid) if tid is not None else None
        if tid_str is None or tid_str not in seen_team_ids:
            skipped_unknown_team += 1
            continue
        rel = sub.get("rel_time_seconds")
        if rel is None:
            skipped_no_time += 1
            continue
        rel = max(0, int(rel))

        status = (sub.get("status") or "UNKNOWN").upper()
        acronym = C.PTA_STATUS_TO_ACRONYM.get(status)
        if acronym is None:
            unknown_status[status] = unknown_status.get(status, 0) + 1
            acronym = C.UNKNOWN_STATUS_FALLBACK

        compiler = (sub.get("compiler") or "").upper()
        lang_name, lang_known = C.compiler_to_language_name(compiler)
        if not lang_known and compiler:
            unknown_compiler[compiler] = unknown_compiler.get(compiler, 0) + 1
        lang_id = next((str(l["id"]) for l in C.LANGUAGES if l["name"] == lang_name), "1")

        timestamp = sub.get("submit_epoch")
        if timestamp is None:
            timestamp = float(start_epoch) + rel

        run_id += 1
        sid = f"s{run_id}"
        rel_hms = C.format_hms(rel)
        sub_objs.append({
            "id": sid,
            "team_id": tid_str,
            "problem_id": pid_str,
            "language_id": lang_id,
            "contest_time": rel_hms,
            "time": _iso_utc(timestamp),
        })
        judge_objs.append({
            "id": f"j{run_id}",
            "submission_id": sid,
            "judgement_type_id": acronym,
            "start_contest_time": rel_hms,
            "end_contest_time": rel_hms,
            "start_time": _iso_utc(timestamp),
            "end_time": _iso_utc(timestamp),
        })
    objects.append(("submissions", sub_objs))
    objects.append(("judgements", judge_objs))

    # ---- awards（与 XML 完全同一套计算结果）----
    award_blocks: list[dict[str, Any]] = []
    if award_groups:
        # 打星模式：唯一奖项来源是「组」，按名次的 awards 一律忽略。
        award_blocks, award_warnings = build_group_award_blocks(
            contest, award_groups, int(penalty_minutes))
        warnings.extend(award_warnings)
    elif awards:
        award_blocks, award_warnings = build_award_blocks(contest, awards, int(penalty_minutes))
        warnings.extend(award_warnings)
    fts_blocks: list[dict[str, Any]] = []
    if first_to_solve:
        fts_blocks = build_first_to_solve_blocks(contest, int(penalty_minutes))

    award_objs = []
    for block in fts_blocks + award_blocks:
        award_objs.append({
            "id": block["id"],
            "citation": block["citation"],
            # JSON feed 里展示方式可以写字符串：detail / pause / list / photos
            "display_mode": block["show"] if block["show"] in ("detail", "pause", "list", "photos")
            else "detail",
            "team_ids": list(block["team_ids"]),
        })
    objects.append(("awards", award_objs))

    # ---- state ----
    state: dict[str, Any] = {
        "started": _iso_utc(start_epoch),
        "ended": _iso_utc(float(start_epoch) + float(duration_seconds)),
        "frozen": _iso_utc(float(start_epoch) + float(duration_seconds) - float(freeze_seconds)),
    }
    if include_finalized:
        state["finalized"] = _iso_utc(float(start_epoch) + float(duration_seconds))
    objects.append(("state", state))

    report = {
        "title": title,
        "problem_count": len(problems),
        "team_count": len(seen_team_ids),
        "run_count": run_id,
        "submission_count": len(submissions),
        "skipped_unknown_problem": skipped_unknown_problem,
        "skipped_unknown_team": skipped_unknown_team,
        "skipped_no_time": skipped_no_time,
        "unknown_status": unknown_status,
        "unknown_compiler": unknown_compiler,
        "length": C.format_hms(duration_seconds),
        "freeze": C.format_hms(freeze_seconds),
        "penalty": int(penalty_minutes),
        "start_epoch": float(start_epoch),
        "warnings": warnings,
        "awards": award_blocks,
        "first_to_solve": fts_blocks,
        "medal_summary": bool(medal_summary and award_blocks),
        "organization_id": org_id,
        "organization": organization,
        "organizations": len(org_objs),
        "teams_with_own_org": len(team_orgs),
        "awards_mode": "starred" if award_groups is not None else "rank",
    }
    return _ndjson_lines(objects), report


# --------------------------------------------------------------------------
# 校验（不依赖 Resolver，做结构自检）
# --------------------------------------------------------------------------

REQUIRED_INFO = (
    "title",
    "short-title",
    "contest-id",
    "starttime",
    "length",
    "scoreboard-freeze-length",
    "penalty",
)


def self_check(xml_text: str, report: dict[str, Any]) -> list[str]:
    """用标准库 XML 解析器做结构自检，返回问题列表（空 = 通过）。"""
    import xml.etree.ElementTree as ET

    issues: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return [f"XML 解析失败：{exc}"]

    if root.tag != "contest":
        issues.append(f"根元素应为 <contest>，实际为 <{root.tag}>")

    info = root.find("info")
    if info is None:
        issues.append("缺少 <info>")
    else:
        for tag in REQUIRED_INFO:
            if info.find(tag) is None:
                issues.append(f"<info> 缺少必需子元素 <{tag}>")

    # 索引
    problem_ids = {p.findtext("id") for p in root.findall("problem")}
    team_ids = {t.findtext("id") for t in root.findall("team")}
    lang_names = {l.findtext("name") for l in root.findall("language")}
    acronyms = {j.findtext("acronym") for j in root.findall("judgement")}

    if not problem_ids:
        issues.append("没有任何 <problem>")
    if not team_ids:
        issues.append("没有任何 <team>")
    if not root.findall("judgement"):
        issues.append("没有任何 <judgement>")
    if not lang_names:
        issues.append("没有任何 <language>")

    run_ids: set[str] = set()
    for run in root.findall("run"):
        rid = run.findtext("id")
        if rid in run_ids:
            issues.append(f"<run> id 重复：{rid}")
        run_ids.add(rid)
        for child in ("team", "problem", "language", "time", "result"):
            if run.find(child) is None:
                issues.append(f"<run id={rid}> 缺少 <{child}>")
        if run.findtext("problem") not in problem_ids:
            issues.append(f"<run id={rid}> 引用了不存在的 problem={run.findtext('problem')}")
        if run.findtext("team") not in team_ids:
            issues.append(f"<run id={rid}> 引用了不存在的 team={run.findtext('team')}")
        # 关键：<run><language> 里放的是语言名称，不是数字 id
        if run.findtext("language") not in lang_names:
            issues.append(
                f"<run id={rid}> 的 language={run.findtext('language')!r} "
                f"不是已定义的 <language><name>（必须是名称，不能是数字 id）"
            )
        if run.findtext("result") not in acronyms:
            issues.append(
                f"<run id={rid}> 的 result={run.findtext('result')!r} "
                "未在 <judgement><acronym> 中定义"
            )
        try:
            if int(run.findtext("time") or "-1") < 0:
                issues.append(f"<run id={rid}> 的 time 为负数")
        except ValueError:
            issues.append(f"<run id={rid}> 的 time 不是整数")

    # 截断输出，避免刷屏
    if len(issues) > 40:
        issues = issues[:40] + [f"... 另有 {len(issues) - 40} 条未显示"]

    if report.get("problem_count") != len(problem_ids):
        issues.append("report.problem_count 与实际 <problem> 数量不一致")
    if report.get("team_count") != len(team_ids):
        issues.append("report.team_count 与实际 <team> 数量不一致")
    if report.get("run_count") != len(run_ids):
        issues.append("report.run_count 与实际 <run> 数量不一致")

    return issues


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pta_to_resolver_xml.py",
        description="把 contest.json 转成 ICPC Resolver 可读的 XML / 比赛包",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--contest", default="contest.json", help="输入的 contest.json 路径")
    p.add_argument("--out", default=None,
                   help="输出单个 XML 文件的路径（例如 contest.xml）")
    p.add_argument("--out-dir", default=None,
                   help="输出比赛包目录（内含 events.xml），可直接喂给 Resolver")
    p.add_argument("--organization", default="PTA",
                   help="学校/组织名称，写入 region 与 team.university，默认 PTA")
    p.add_argument("--region-id", default="1", help="region 的外部 ID，默认 1")
    p.add_argument("--medals", nargs=3, type=int, metavar=("GOLD", "SILVER", "BRONZE"),
                   default=(0, 0, 0),
                   help="金银铜奖的最后一名次（last-gold/silver/bronze），默认 0 0 0 表示不颁奖")
    p.add_argument("--no-finalized", action="store_true",
                   help="不输出 <finalized>（比赛未结束时有用，配合 resolver --test）")
    p.add_argument("--awards-file", default=None,
                   help="奖项配置 JSON 文件：[{\"name\":\"一等奖\",\"count\":1,\"show\":\"photos\"}, …]。"
                        "给了它就会按榜单名次依次发奖，每个奖项写一个独立的 <award> 块"
                        "（实测 Resolver 支持任意多个）")
    p.add_argument("--award-rows-file", "--award-groups-file", dest="award_rows_file",
                   default=None,
                   help="【M15 唯一奖项来源】**奖项表** JSON 文件："
                        '[{"name":"一等奖","show":"detail","team_ids":["1001","1002"]}, …]。'
                        "每一行 = 比赛包里的一个 <award>（id=honors-N，citation=奖项名，"
                        "teamId=该行名单）—— 谁获奖只看名单，不再按名次自动算。"
                        "别名 --award-groups-file（M12 的老名字，行为完全一样）")
    p.add_argument("--first-to-solve", action="store_true",
                   help="自动逐题生成一血奖（最快解题奖），id 用 first-to-solve-<题目id>")
    p.add_argument("--medal-summary", action="store_true",
                   help="有自定义奖项时，额外把前三个奖项的人数写成 <finalized> 的金/银/铜。"
                        "默认关闭 —— 两套同时给会让同一支队伍既拿自定义奖又拿 Gold，重复计数")
    p.add_argument("--feed", choices=("json", "xml"), default="json",
                   help="比赛包用哪种 feed：json（默认）= 另外写一份 event-feed.ndjson，"
                        "Resolver 会优先用它（能带队伍照片背景 + 机构校徽）；"
                        "xml = 只写 events.xml（旧行为，回退开关）")
    p.add_argument("--org-plan", default=None,
                   help="校徽 -> 机构映射的 JSON 文件（由 app/board_backend.py 生成）："
                        '{"organizations":[{"id","name"}...],"teams":{"<队伍ID>":"<机构ID>"}}。'
                        "给了它，feed 里每个不同校徽就是一个 organization。")
    p.add_argument("--quiet", action="store_true", help="不打印报告")
    return p


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            except Exception:
                pass

    args = build_parser().parse_args(argv)

    contest_path = Path(args.contest)
    if not contest_path.exists():
        C.die(f"找不到 {contest_path}。请先运行：python pta_fetch.py fetch")
    contest = C.load_contest(contest_path)

    if not args.out and not args.out_dir:
        args.out = "contest.xml"

    awards_spec: list[dict[str, Any]] | None = None
    if args.awards_file:
        try:
            awards_spec = json.loads(Path(args.awards_file).read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            C.die(f"--awards-file 读不出来或不是合法 JSON：{args.awards_file}\n  {exc}")
        if not isinstance(awards_spec, list):
            C.die("--awards-file 的内容必须是一个 JSON 数组。")

    award_groups: list[dict[str, Any]] | None = None
    rows_source = getattr(args, "award_rows_file", None)
    if rows_source:
        try:
            loaded_groups = json.loads(
                Path(rows_source).read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            C.die(f"--award-rows-file 读不出来或不是合法 JSON：{rows_source}\n  {exc}")
        if not isinstance(loaded_groups, list):
            C.die("--award-rows-file 的内容必须是一个 JSON 数组。")
        award_groups = loaded_groups
        awards_spec = None                 # 单一模型：按名次的 --awards-file 不再参与
        if getattr(args, "awards_file", None):
            print("[提示] 同时给了 --awards-file 与 --award-rows-file："
                  "按 M15 单一模型走，按名次自动切分的奖项已忽略。")

    org_plan: dict[str, Any] | None = None
    if args.org_plan:
        try:
            loaded = json.loads(Path(args.org_plan).read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            C.die(f"--org-plan 读不出来或不是合法 JSON：{args.org_plan}\n  {exc}")
        if not isinstance(loaded, dict):
            C.die("--org-plan 的内容必须是一个 JSON 对象。")
        org_plan = loaded

    xml_text, report = build_xml(
        contest,
        star_ids=_load_star_ids([contest_path.parent, contest_path.parent.parent]),
        organization=args.organization,
        region_id=args.region_id,
        medals=tuple(args.medals),  # type: ignore[arg-type]
        include_finalized=not args.no_finalized,
        awards=awards_spec,
        medal_summary=bool(args.medal_summary),
        first_to_solve=bool(args.first_to_solve),
        award_groups=award_groups,
    )

    issues = self_check(xml_text, report)
    if issues:
        print("[error] 生成的 XML 未通过结构自检：", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1

    written: list[Path] = []
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(xml_text, encoding="utf-8")
        written.append(out)

    if args.out_dir:
        pkg = Path(args.out_dir)
        pkg.mkdir(parents=True, exist_ok=True)
        # 关键：文件名必须是 events.xml，见模块 docstring。
        # 即使默认走 JSON feed，这份 XML 也一起写着 —— 它是回退开关，
        # 而且 app 的「包指纹 / 奖项清单 / 崩溃预检」都还在读它。
        events = pkg / "events.xml"
        events.write_text(xml_text, encoding="utf-8")
        written.append(events)

        # 清理掉另一种 feed，避免"想回退到 XML 却还在跑旧的 ndjson"
        # （DiskContestSource 的顺序是 ndjson -> json -> xml，ndjson 会一直赢）
        for stale in (pkg / "event-feed.ndjson", pkg / "event-feed.json"):
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass

        if args.feed == "json":
            ndjson_text, ndjson_report = build_ndjson(
                contest,
                organization=args.organization,
                region_id=args.region_id,
                medals=tuple(args.medals),  # type: ignore[arg-type]
                include_finalized=not args.no_finalized,
                awards=awards_spec,
                medal_summary=bool(args.medal_summary),
                first_to_solve=bool(args.first_to_solve),
                org_plan=org_plan,
                award_groups=award_groups,
            )
            feed = pkg / FEED_NDJSON
            feed.write_text(ndjson_text, encoding="utf-8")
            written.append(feed)
            report["ndjson"] = ndjson_report

    if not args.quiet:
        print("=" * 62)
        print("  已生成 ICPC Resolver XML")
        print("=" * 62)
        print(f"  比赛标题  : {report['title']}")
        print(f"  题目数    : {report['problem_count']}")
        print(f"  队伍数    : {report['team_count']}")
        print(f"  提交总数  : {report['submission_count']}")
        print(f"  写入 run  : {report['run_count']}")
        print(f"  时长      : {report['length']}   封榜: {report['freeze']}   罚时: {report['penalty']} 分钟")
        if report.get("first_to_solve"):
            print(f"  一血奖    : {len(report['first_to_solve'])} 项（自动生成）")
        mode = report.get("awards_mode") or "rank"
        print(f"  奖项模式  : {'打星（按组发奖）' if mode == 'starred' else '按名次（一等奖/二等奖…）'}")
        if report.get("awards"):
            print(f"  奖项      : {len(report['awards'])} 项")
            for block in report["awards"]:
                names = "、".join(t["name"] for t in block["teams"])
                print(f"     · {block['citation']}（{block['count']} 队，"
                      f"id={block['id']}，show={block['show']}）：{names}")
        elif mode == "starred":
            print("  ⚠ 打星模式但一个队都没勾选：包里除一血奖外不会有任何奖项。")
        if report["skipped_unknown_problem"]:
            print(f"  ⚠ 跳过（题目不在题目列表内）: {report['skipped_unknown_problem']}")
        if report["skipped_unknown_team"]:
            print(f"  ⚠ 跳过（队伍不在队伍列表内）: {report['skipped_unknown_team']}")
        if report["skipped_no_time"]:
            print(f"  ⚠ 跳过（缺少提交时间）: {report['skipped_no_time']}")
        if report["unknown_status"]:
            print(f"  ⚠ 未映射判题状态（按 WA）: {report['unknown_status']}")
        if report["unknown_compiler"]:
            print(f"  ⚠ 未映射编译器（按 c）: {report['unknown_compiler']}")
        print("  结构自检  : 通过 ✓")
        nd = report.get("ndjson") or {}
        if nd.get("organizations"):
            print(f"  机构(校徽): {nd['organizations']} 个（其中 {nd.get('teams_with_own_org', 0)} "
                  f"支队伍用专属校徽）")
        if args.out_dir:
            kind = "JSON feed (event-feed.ndjson，Resolver 优先用它)" if args.feed == "json" \
                else "仅 XML (events.xml)"
            print(f"  feed 形态 : {kind}")
        for path in written:
            print(f"  已写入    : {path.resolve()}")
        if args.out_dir:
            print("")
            print("  下一步（滚榜）：")
            print(f"    resolver.bat {Path(args.out_dir).resolve()} --display_name \"{{team.name}}\"")
        print("=" * 62)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
