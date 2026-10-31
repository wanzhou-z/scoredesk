# -*- coding: utf-8 -*-
"""board_config.py —— 设置(config.json) 与 历史比赛(history.json) 的持久化。

设计要点
--------
* 纯标准库，不依赖第三方包；
* **任何读写失败都不抛异常**，只返回一句给人看的中文提示，GUI 直接显示；
* 落盘用「临时文件 + 原子替换」，中途断电不会留下半个 JSON；
* 源码运行时配置写在 ``app\\config.json``；PyInstaller 打包后写在 exe 同目录，
  这样整个 dist 文件夹拷给别人就能连设置一起带走；
* exe 同目录如果不可写（例如被放进 Program Files），自动退到
  ``%LOCALAPPDATA%\\PTA-Board``，并在界面上说明。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

CONFIG_NAME = "config.json"
HISTORY_NAME = "history.json"
SCHEMA_VERSION = 2

#: 奖项默认配置：一等奖 1 人、二等奖 2 人、三等奖 3 人。
#: 「人数」是从榜单第 1 名往下连着数的队数（**顺序切分，不重叠**）。
DEFAULT_AWARDS: list[dict[str, Any]] = [
    {"name": "一等奖", "count": 1, "show": "detail", "auto": "rank"},
    {"name": "二等奖", "count": 2, "show": "detail", "auto": "rank"},
    {"name": "三等奖", "count": 3, "show": "detail", "auto": "rank"},
]

#: M15 单一模型：奖项行 = 一个奖项，`team_ids` 就是**最终名单**（谁获奖谁不获奖只看它）。
#:
#:   name      奖项名（= 比赛包里的 ``<citation>``，观众看到的就是这个名字）
#:   show      detail | pause（展示方式）
#:   count     「按名次填前 N 支」用的数字，也是「已选 N 支」的预期值
#:   team_ids  该奖项的**名单**（权威；手改优先）
#:   auto      "" | "rank" | "star" —— 「重新识别 / 刷新」时按什么重算名单：
#:             ""     只用手选名单（不自动改）
#:             rank   按名次依次取前 ``count`` 支（多个 rank 行按顺序**不重叠**）
#:             star   取队名/姓名前带 ``*`` / ``＊`` 的队（``*`` 只是勾选便利，不是模式）
#:   manual    True = 这一行被手改过，刷新时**不覆盖**（界面上标「手动」）
#:   names     {"<队伍ID>": "<上次看到的队名>"} —— 只用来提示「这支队伍改名了/找不到了」
AWARD_ROW_KEYS = ("name", "show", "count", "team_ids", "auto", "manual", "names")
#: ``auto`` 只认这三种（空 = 不自动重算）
AWARD_AUTO_RULES = ("", "rank", "star")
#: 界面上每种自动来源的中文名（面板/日志/证据共用一份文案）
AWARD_AUTO_LABELS = {"": "手选", "rank": "自动（按名次）", "star": "自动（星号）"}


#: 奖项展示方式。界面**只提供这两种**：
#:   * detail -> TeamAwardPresentation：照片铺满 + 左下角「校徽 + 队名 + 奖项名」栏（目标效果，
#:     实测截图 tmp\frames\photo+bar-1/2/3.png）；
#:   * pause  -> 只停住不动，不展示奖项卡。
#: 历史备注：旧版走 events.xml 时，XMLFeedParser 把 <show> 解析成布尔值
#: （true->DETAIL / false->PAUSE），DisplayMode 的 PHOTOS / LIST 根本传不进去。
#: 现在默认 feed 是 event-feed.ndjson，`display_mode` 已经能写 photos/list 了
#: （实测 photos = TeamListPhotoPresentation：照片 + 顶部奖项名条 + 底部超大号队名，
#: 1 队时排版会溢出画面），但**故意不在界面上暴露**，免得和 detail 的目标效果混淆。
#: M13（4a）：下拉框里显示的是**短文案**。
#: 原来 detail 写成「停下显示详情（推荐：照片背景 + 校徽/队名/奖项名）」——
#: 26 个全角字，900x600 下单个下拉框物理上放不下（需要 ~590px，实际只有 ~490px），
#: 显示成「停下显示详情（推荐…」，正是用户反馈的 bug。长说明挪到
#: :data:`AWARD_SHOW_DESC`，由界面放进 ToolTip。
AWARD_SHOW_LABELS: dict[str, str] = {
    "detail": "停下显示详情（推荐）",
    "pause": "暂停（停住等手动继续）",
}

#: 每种展示方式的**长说明**（给 ToolTip / 帮助文字用，不进下拉框）。
AWARD_SHOW_DESC: dict[str, str] = {
    "detail": "停下显示详情：揭晓到这支队伍时停下 —— 照片铺满整张奖项卡，"
              "左下角显示校徽 + 队名 + 奖项名。推荐。",
    "pause": "暂停：画面停住等手动继续（在 Resolver 里点击/按空格再往下走）。",
}

#: 全部默认设置。GUI 里每个控件都对应这里的一项。
DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA_VERSION,
    # ---- 比赛页 ----
    "problem_set_id": "",
    #: 取数平台：``pta``（默认，拼题 A，需要有效 Cookie）/
    #: ``vjudge``（VJudge，公开比赛匿名接口、不需要 Cookie，默认 10 秒轮询）。
    #: 选 VJudge 时 ``problem_set_id`` 里存的就是 **VJudge 比赛 ID**（如 862258）。
    "platform": "pta",
    # 取数只有一条路：直连接口（需要 Cookie）。2026-10-13 删除「借用浏览器」通道。
    "source_mode": "http",
    # ---- 播放与显示页 ----
    # 播放模式三选一：
    #   click           全程点击（--singleStep 2）
    #   auto-then-click 先自动、从第 N 名起改成点击（--singleStep N+1）
    #   auto            全程自动（不加 --singleStep）
    "play_mode": "click",
    "click_from_place": 4,       # 「先自动后点击」从第几名开始等点击
    "first_to_solve": True,      # 自动加一血奖（最快解题奖）
    "speed": 1.0,                   # 映射到 resolver --speed
    "freeze_minutes": 60,           # 封榜时长（分钟），生成比赛包时写入 XML
    "penalty_minutes": 20,          # 罚时（分钟），生成比赛包时写入 XML
    # 奖项表：[{"name": "一等奖", "show": "detail", "count": 1, "team_ids": [...],
    #           "auto": "rank"|"star"|"", "manual": false, "names": {...}}, ...]
    # **M15 起这是唯一的奖项来源**：每一行 = 比赛包里的一个 <award>
    # （citation = 奖项名，teamId = team_ids），名单由「全队标记表」随时改。
    # 没有「奖项模式」这回事了 —— 打星（*）只是 auto="star" 的勾选便利。
    "awards": [dict(a) for a in DEFAULT_AWARDS],
    # 是否额外把前三个奖项写成 <finalized> 的金/银/铜（经典奖牌）。
    # 默认关闭：两套同时给会让同一支队既拿自定义奖又拿 Gold，重复计数。
    "award_medal_summary": False,

    # 旧的「奖牌名次 金 银 铜」写法，只在自动迁移旧配置时用到，界面不再写它。
    "medals": "1 2 3",
    # M15：上一次「拉取数据 / 重新识别」时的队伍快照 {"team_count": n, "names": {"<ID>": "<队名>"}}。
    # 下一次刷新时和当前数据比 → 界面明确提示「新增 / 消失 / 改名」（用户要求"数据变了要有提示"）。
    # **必须留在 DEFAULTS 里**，否则重启后第一轮刷新没有基线（只在同一次会话内才报变化）。
    "award_team_snapshot": {},
    # M17：旧的「实时侧奖项清单」只合并**一次**（合并过就记在这里）。
    # 不留这个标记的话，每次启动都会拿旧配置里的清单往主表里塞 ——
    # 用户删掉的奖项会被"复活"。**必须在 DEFAULTS 里**，否则 sanitize 会把它丢掉。
    "_awards_live_merged": {},
    "display_name": "{team.name}",  # M13（4b）：界面已删，固定用它；旧值读到即忽略
    "font_path": "",                # M13（4d）：界面已删；空 = 用内置字体 tools\fonts\MicrosoftYaHei.ttc
    "extra_args": "",               # 额外 Resolver 参数（高级）
    "photos_dir": "",               # 队伍照片目录（可空）：奖项卡整屏背景
    "logos_dir": "",                # 校徽目录（可空）：奖项卡左下角、队伍名前
    # ---- 界面 ----
    #: 上次用的模式（live / roll）。--mode 启动参数优先于它。
    "mode": "live",
    "window_geometry": "",
    "last_tab": 0,
    #: **每个模式各自记住上次停留的页**（页内序号，从 0 开始）：
    #: 实时榜单 0..4（数据源/外观/服务/直播/历史），赛后滚榜 0..3（数据源/外观/滚榜/历史）。
    "last_tab_live": 0,
    "last_tab_roll": 0,
    "log_open": 0,
    # ---- 路径记忆（方便下次一键滚榜）----
    "last_contest_json": "",
    "last_package_dir": "",
}


# --------------------------------------------------------------------------
# 路径判定
# --------------------------------------------------------------------------

def is_frozen() -> bool:
    """是否运行在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """本文件所在目录（源码模式 = 仓库下的 app\\）。"""
    return Path(__file__).resolve().parent


def resource_root() -> Path:
    """只读资源根目录：里面有 ``pta_fetch.py`` 和 ``tools\\``。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            cand = Path(base)
            if (cand / "pta_fetch.py").is_file() or (cand / "tools").is_dir():
                return cand
        return Path(sys.executable).resolve().parent
    return app_dir().parent


def work_root() -> Path:
    """可写工作根目录：contest.json / resolver-package\\ / logs\\ 都落在这里。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return app_dir().parent


# --------------------------------------------------------------------------
# 队伍照片 / 校徽 的默认目录
# --------------------------------------------------------------------------
#: 「队伍照片目录」没配置时用它 —— 就是**应用所在目录**下的 ``滚榜图片``。
#: 例如程序解压在 ``D:\PTA-比赛助手\``，这里就是 ``D:\PTA-比赛助手\滚榜图片``。
PHOTOS_DIR_NAME = "滚榜图片"
#: 「校徽目录」没配置时用它 —— 应用所在目录下的 ``校徽``。
LOGOS_DIR_NAME = "校徽"


def default_photos_dir() -> Path:
    """队伍照片目录的默认位置（应用所在目录下的 ``滚榜图片``）。"""
    return work_root() / PHOTOS_DIR_NAME


def default_logos_dir() -> Path:
    """校徽目录的默认位置（应用所在目录下的 ``校徽``）。"""
    return work_root() / LOGOS_DIR_NAME


def resolve_dir(value: Any, fallback: Path) -> Path:
    """把用户填的目录（可能带 ``%VAR%`` / ``~``）展开成绝对路径；空值用默认目录。"""
    text = str(value or "").strip()
    if not text:
        return Path(fallback)
    return Path(os.path.expandvars(os.path.expanduser(text)))


def ensure_dir(path: Path) -> tuple[bool, str]:
    """目录不存在就创建（含父目录）。返回 (是否可用, 说明)。"""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return True, str(Path(path).resolve())
    except OSError as exc:
        return False, str(exc)


def _probe_writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".pta-board-write-test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True
    except OSError:
        return False


def config_home() -> tuple[Path, str]:
    """返回 (配置文件所在目录, 需要提示给用户的话)。"""
    if not is_frozen():
        return app_dir(), ""

    exe_dir = Path(sys.executable).resolve().parent
    if _probe_writable(exe_dir):
        return exe_dir, ""

    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if local:
        fallback = Path(local) / "PTA-Board"
        if _probe_writable(fallback):
            return fallback, (
                f"程序所在目录不可写（{exe_dir}），设置已改存到 {fallback}。\n"
                "想让设置跟着程序走，请把整个文件夹解压到桌面或 D 盘等可写位置。"
            )
    return exe_dir, f"程序所在目录不可写（{exe_dir}），设置可能保存失败。"


# --------------------------------------------------------------------------
# 低层读写
# --------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: Any) -> tuple[bool, str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建目录 {path.parent}：{exc}"

    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
        return True, ""
    except OSError as exc:
        return False, (
            f"无法写入 {path.name}：{exc}\n"
            f"位置：{path}\n"
            "如果程序放在 Program Files 之类**系统保护目录**里（没有写权限），"
            "请把它移到桌面或 D 盘再试。"
        )
    except (TypeError, ValueError) as exc:
        return False, f"内容无法保存成 JSON（这是程序内部错误）：{exc}"


def _read_json(path: Path) -> tuple[Any, str]:
    """读取 JSON。文件不存在返回 (None, "")；损坏时把坏文件改名备份再返回 (None, 提示)。

    用 ``utf-8-sig`` 而不是 ``utf-8``：用户很可能会用记事本打开 config.json
    改一改再保存，而记事本默认就写成「带 BOM 的 UTF-8」，标准 utf-8 解码会
    直接抛错。多容忍一个 BOM 能省掉一堆莫名其妙的「设置读不出来」。
    """
    if not path.exists():
        return None, ""
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh), ""
    except (ValueError, UnicodeDecodeError, OSError) as exc:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        broken = path.with_name(f"{path.stem}.broken-{stamp}{path.suffix}")
        moved = ""
        try:
            os.replace(path, broken)
            moved = f"\n原文件已备份为 {broken.name}。"
        except OSError:
            moved = "\n（旧文件备份失败，已忽略它的内容。）"
        return None, f"{path.name} 内容损坏，已改用默认值：{exc}{moved}"


# --------------------------------------------------------------------------
# 数值清洗
# --------------------------------------------------------------------------

def _as_int(value: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        got = int(round(float(value)))
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, got))


def _as_float(value: Any, fallback: float, lo: float, hi: float) -> float:
    try:
        got = float(value)
    except (TypeError, ValueError):
        return fallback
    if got != got:  # NaN
        return fallback
    return max(lo, min(hi, got))


def _as_choice(value: Any, choices: tuple[str, ...], fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in choices else fallback


def _clean_medals(value: Any) -> str:
    """奖牌名次规范成 ``"金 银 铜"`` 三个非负整数。

    允许用户写成 ``1,2,3`` / ``1 2 3`` / ``(1, 2, 3)`` 这类形式。
    """
    raw = str(value or "").replace(",", " ").replace("(", " ").replace(")", " ")
    parts = raw.split()
    if len(parts) != 3:
        return str(DEFAULTS["medals"])
    out: list[int] = []
    for item in parts:
        try:
            out.append(max(0, int(float(item))))
        except (TypeError, ValueError):
            return str(DEFAULTS["medals"])
    return " ".join(str(n) for n in out)


def clean_award_row(item: Any, index: int = 0) -> dict[str, Any]:
    """把一行奖项规整成 :data:`AWARD_ROW_KEYS` 的形状（**永不抛异常**）。

    * ``team_ids`` 一律是去重后的字符串列表（顺序保留 = 观众看到的揭晓顺序，
      不过生成包时 ``build_group_award_blocks`` 还会按名次再排一遍）；
    * ``auto`` 只认 ``""`` / ``"rank"`` / ``"star"``；
    * **有名单、又没标来源** → 当成手选（``manual=True``）：老配置升级上来时
      名单原样保留、不会被"重新识别"改掉，这是最安全的默认；
    * **只有 count、没有名单** → ``auto="rank"``：升级前"按名次依次发奖"的行为不变。
    """
    item = item if isinstance(item, dict) else {}
    name = str(item.get("name") or "").strip()
    if not name:
        name = f"奖项 {index + 1}"
    show = str(item.get("show") or "detail").strip().lower()
    if show not in AWARD_SHOW_LABELS:
        show = "detail"
    raw_count = item.get("count")
    try:
        count = int(str(raw_count).strip()) if isinstance(raw_count, str) else int(raw_count)
    except (TypeError, ValueError):
        count = 0
    count = max(0, min(100000, count))

    ids: list[str] = []
    for value in (item.get("team_ids") or []):
        text = str(value).strip()
        if text and text not in ids:
            ids.append(text)
    auto = str(item.get("auto") or "").strip().lower()
    if auto not in AWARD_AUTO_RULES:
        auto = ""
    manual = bool(item.get("manual"))
    # 这一行是不是"新模型"写出来的（显式带了 team_ids / auto / manual 任何一个键）
    explicit = any(key in item for key in ("team_ids", "auto", "manual"))
    if ids and not manual and not auto:
        manual = True                       # 有名单但没来源 → 当手选，别乱改
    if not ids and not auto and count > 0 and not explicit:
        # **只有老配置才会**（一/二/三等奖 + 人数，从来没有名单）：按名次依次填，
        # 升级前的行为完全不变。新模型里"清空"过的行（显式 team_ids=[] / manual=True）
        # 不会再被这里悄悄填回去。
        auto = "rank"
    names_raw = item.get("names")
    names = ({str(k): str(v) for k, v in names_raw.items()}
             if isinstance(names_raw, dict) else {})
    if count <= 0 and ids:
        count = len(ids)
    return {"name": name, "show": show, "count": count, "team_ids": ids,
            "auto": auto, "manual": manual, "names": names}


def clean_awards(value: Any) -> list[dict[str, Any]]:
    """把奖项列表规整成「奖项行」列表（M15 单一模型）。

    非法项直接丢掉；空列表时回落到 :data:`DEFAULT_AWARDS`
    （「一个奖都没有」会让 Resolver 启动时 NPE 崩掉）。
    """
    out: list[dict[str, Any]] = []
    if isinstance(value, list):
        for idx, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            row = clean_award_row(item, idx)
            if not row["team_ids"] and row["count"] <= 0 and not row["auto"]:
                # 既没名单、又没人数、也没自动规则 → 这一行没有任何用，丢掉
                continue
            out.append(row)
    return out or [clean_award_row(dict(a), i) for i, a in enumerate(DEFAULT_AWARDS)]


def snapshot_teams(teams: Any) -> dict[str, Any]:
    """把当前队伍列表拍一张快照：``{"team_count": n, "names": {"<队伍ID>": "<队名>"}}``。

    界面每次成功刷新后把它写进 ``cfg["award_team_snapshot"]``，下次刷新时用
    :func:`diff_team_snapshot` 比较 —— 用户要求「数据变了（改名/消失/新增）要有明确提示」。
    """
    names: dict[str, str] = {}
    for team in teams or []:
        if not isinstance(team, dict):
            continue
        tid = str(team.get("team_id") or team.get("userId") or "").strip()
        if tid:
            names[tid] = str(team.get("name") or "")
    return {"team_count": len(names), "names": names}


def diff_team_snapshot(snapshot: Any, teams: Any) -> dict[str, Any]:
    """比较上一次的快照与当前队伍 → 新增 / 消失 / 改名（都给出可读的名字）。"""
    now = snapshot_teams(teams)
    old_names = snapshot.get("names") if isinstance(snapshot, dict) else {}
    old_names = old_names if isinstance(old_names, dict) else {}
    added = [{"id": tid, "name": now["names"][tid]}
             for tid in now["names"] if tid not in old_names]
    gone = [{"id": tid, "name": old_names.get(tid, "")}
            for tid in old_names if tid not in now["names"]]
    renamed = [{"id": tid, "was": str(old_names[tid]), "now": now["names"][tid]}
               for tid in now["names"]
               if tid in old_names and str(old_names[tid]) and str(old_names[tid]) != now["names"][tid]]
    return {"added": added, "added_count": len(added),
            "gone": gone, "gone_count": len(gone),
            "renamed": renamed, "renamed_count": len(renamed),
            "team_count": now["team_count"],
            "prev_team_count": int((snapshot or {}).get("team_count") or len(old_names))
            if isinstance(snapshot, dict) else len(old_names),
            "has_prev": bool(old_names)}


def snapshot_hint_text(diff: Any, limit: int = 4) -> str:
    """把 :func:`diff_team_snapshot` 的结果拼成一句界面提示（没有变化就返回空串）。"""
    if not isinstance(diff, dict) or not diff.get("has_prev"):
        return ""
    parts: list[str] = []
    if diff.get("added_count"):
        names = "、".join(str(item.get("name") or item.get("id")) for item in diff["added"][:limit])
        parts.append(f"新增 {diff['added_count']} 支队（{names}）")
    if diff.get("gone_count"):
        names = "、".join(str(item.get("name") or item.get("id")) for item in diff["gone"][:limit])
        parts.append(f"有 {diff['gone_count']} 支队从数据里消失了（{names}）")
    if diff.get("renamed_count"):
        names = "、".join(f"「{item['was']}」→「{item['now']}」"
                          for item in diff["renamed"][:limit])
        parts.append(f"有 {diff['renamed_count']} 支队改名了（{names}）")
    return "⚠ 数据变了：" + "；".join(parts) if parts else ""


def live_view_awards(rows: Any, limit: int = 3) -> list[dict[str, Any]]:
    """奖项行 → **写进实时榜单配置** ``live.view.awards`` 的载荷（M17 口径纠正）。

    **实时榜单按"当前名次区间"着色**（用户原话：「实时榜单是随时动的，所以要看排名来决定
    金银铜」）—— 所以这里**永远写 ``count``**，不因为这一行勾了名单就把 ``count`` 归零：

    * 第 1 个奖 ``count=1`` → 当前第 1 名上金；第 2 个 ``count=2`` → 第 2..3 名上银；
      第 3 个 → 接着一段上铜；第 4 个往后不参与着色。名次一变，颜色跟着变。
    * ``team_ids`` 一起带上（如果这一行有）—— **那是滚榜的规则**：滚榜生成包按名单发奖。
      同一行同时有 ``count`` 与 ``team_ids`` 是**正常**的：滚榜读名单、实时榜读人数。
      前端 ``board.js`` 的 ``medalFor(rank)`` 只读 ``count``（M17 口径纠正，见那里的注释）。
    """
    out: list[dict[str, Any]] = []
    for row in (rows or [])[:max(0, int(limit))]:
        if not isinstance(row, dict):
            continue
        clean = clean_award_row(row)
        item: dict[str, Any] = {"name": str(clean["name"] or "奖项"),
                                "show": str(clean["show"] or "detail"),
                                "count": int(clean["count"])}
        ids = [str(t) for t in clean["team_ids"]]
        if ids:
            item["team_ids"] = ids
        out.append(item)
    return out


def normalize_live_view_awards(items: Any, limit: int = 3) -> list[dict[str, Any]]:
    """把**已经读进来的** ``view.awards``（可能是老配置）按同一口径归一化。

    * ``count`` **原样保留**（实时榜按它算名次区间；"名单 + count 同时非 0"不是错误）；
    * ``team_ids`` 有就带上（滚榜那份规则要它）；
    * 顺手修一份历史遗留：之前一版把"有名单的行"的 ``count`` 写成 0，
      那种配置在实时榜上**一个名次都覆盖不到**，这里用名单长度补回一个可用的区间。
    """
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        if len(out) >= max(0, int(limit)):
            break
        ids = [str(t).strip() for t in (item.get("team_ids") or []) if str(t).strip()]
        name = str(item.get("name") or f"奖项 {idx + 1}")
        show = str(item.get("show") or "detail")
        try:
            count = max(0, int(item.get("count") or 0))
        except (TypeError, ValueError):
            count = 0
        if count == 0 and ids:            # 修复"名次区间被写成 0"的旧配置
            count = len(ids)
        entry: dict[str, Any] = {"name": name, "show": show, "count": count}
        if ids:
            entry["team_ids"] = ids
        out.append(entry)
    return out


def live_awards_from_rows(rows: Any, limit: int = 3) -> list[dict[str, Any]]:
    """奖项行 → 实时榜单着色用的「前 ``limit`` 个奖项」（金 / 银 / 铜）。

    人数取**名单实际长度**（不是 ``count`` 输入框里的期望值）—— 榜单上着色分块按
    真实名单来，和滚榜画面一致。第 4 个往后不上色（既有设计，不变）。

    例外：**还没刷新过**的「按名次」行名单是空的，这时显示它配置的人数 ``count``
    （否则清单里会一片 0，看着像坏了）；手动/星号行没勾就是真的 0 队。
    """
    out: list[dict[str, Any]] = []
    for row in (rows or [])[:max(0, int(limit))]:
        if not isinstance(row, dict):
            continue
        clean = clean_award_row(row)
        ids = list(clean["team_ids"])
        shown = len(ids) if ids else (int(clean["count"]) if clean["auto"] == "rank" else 0)
        out.append({"name": str(clean["name"] or "奖项"),
                    "count": shown,
                    "show": str(clean["show"] or "detail")})
    return out


def awards_from_groups(groups: Any) -> list[dict[str, Any]]:
    """把 M12 的「奖项组」迁成奖项行（``略``组名 → 奖项名，队名单原样带过来）。

    迁移出来的行一律 ``manual=True``：名单是用户当年实际发过的，**一个都不改**。
    """
    out: list[dict[str, Any]] = []
    if isinstance(groups, list):
        for idx, item in enumerate(groups):
            if not isinstance(item, dict):
                continue
            row = clean_award_row({
                # 老配置里的组没有名字时给个中性名（**不要**再出现"打星"字样：
                # 这个字符串会变成界面上一个奖项行的名字，是面向用户的）
                "name": str(item.get("name") or f"奖项 {idx + 1}").strip(),
                "show": item.get("show") or "detail",
                "team_ids": item.get("team_ids") or [],
                "manual": True,
            }, idx)
            if row["team_ids"]:
                out.append(row)
    return out


def _awards_from_legacy_medals(text: Any) -> list[dict[str, Any]]:
    """把旧的 ``medals: "1 2 3"``（金/银/铜各多少块）迁成三行奖项。"""
    raw = str(text or "").replace(",", " ").replace("(", " ").replace(")", " ")
    parts = raw.split()
    if len(parts) != 3:
        return [clean_award_row(dict(a), i) for i, a in enumerate(DEFAULT_AWARDS)]
    names = ("一等奖", "二等奖", "三等奖")
    out: list[dict[str, Any]] = []
    for name, part in zip(names, parts):
        try:
            count = max(0, int(float(part)))
        except (TypeError, ValueError):
            count = 0
        if count:
            out.append(clean_award_row({"name": name, "count": count, "show": "detail"}))
    return out or [clean_award_row(dict(a), i) for i, a in enumerate(DEFAULT_AWARDS)]


def sanitize(raw: Any) -> dict[str, Any]:
    """把外部读进来的 dict 规整成一份一定合法的设置。"""
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})

    cfg["problem_set_id"] = str(cfg.get("problem_set_id") or "").strip() or DEFAULTS["problem_set_id"]
    # 平台：pta（默认）/ vjudge；旧配置没有这个键 → pta，行为不变。
    cfg["platform"] = _as_choice(str(cfg.get("platform") or "").strip().lower(),
                                 ("pta", "vjudge"), "pta")
    # 取数方式固定：直连接口（需要 Cookie）；旧配置里的其它值一律归到 http。
    cfg["source_mode"] = "http"
    # 旧配置迁移：step -> click（全程点击），auto -> auto（全程自动）
    _pm = str(cfg.get("play_mode") or "").strip().lower()
    if _pm in ("step", "click", ""):
        _pm = "click"
    elif _pm in ("auto",):
        _pm = "auto"
    cfg["play_mode"] = _as_choice(_pm, ("click", "auto-then-click", "auto"), "click")
    cfg["click_from_place"] = _as_int(cfg.get("click_from_place"), 4, 1, 10000)
    cfg["first_to_solve"] = bool(cfg.get("first_to_solve", True))
    cfg["speed"] = _as_float(cfg.get("speed"), 1.0, 0.1, 16.0)
    cfg["freeze_minutes"] = _as_int(cfg.get("freeze_minutes"), 60, 0, 24 * 60)
    cfg["penalty_minutes"] = _as_int(cfg.get("penalty_minutes"), 20, 0, 600)
    cfg["medals"] = _clean_medals(cfg.get("medals"))
    # ---- M15：奖项 = 一张表（每行一个奖项 + 自己的名单）---------------------
    # 迁移口径（旧配置 → 新配置，**一步到位、可复算**）：
    #   1) 老 awards（一/二/三等奖 + count，没有名单）→ 变成带名单的行：
    #      auto="rank"（升级前就是"按名次依次发"，行为完全不变），
    #      界面/生成包第一次刷新时按当前榜单把 team_ids 填上；
    #   2) 老 award_groups（打星模式真正发过的组）→ 追加成行，**名单原样**、
    #      manual=True（当年发过的一个都不改）；
    #   3) 老的 awards_mode / star_marks 只作迁移输入，迁移完不再参与任何逻辑
    #      （旧值原样记进 _awards_migrated_from，方便复算与排查）。
    raw_awards = raw.get("awards") if isinstance(raw, dict) else None
    raw_groups = raw.get("award_groups") if isinstance(raw, dict) else None
    raw_marks = raw.get("star_marks") if isinstance(raw, dict) else None
    old_mode = str(raw.get("awards_mode") or "").strip().lower() if isinstance(raw, dict) else ""
    had_awards = isinstance(raw_awards, list) and bool(raw_awards)
    if had_awards:
        cfg["awards"] = clean_awards(raw_awards)
        cfg["_awards_migrated"] = False
    else:
        migrated = _awards_from_legacy_medals(cfg.get("medals"))
        cfg["awards"] = migrated
        cfg["_awards_migrated"] = isinstance(raw, dict) and bool(raw)
    group_rows = awards_from_groups(raw_groups)
    if group_rows:
        existing = {str(row.get("name") or "") for row in cfg["awards"]}
        for row in group_rows:
            name = str(row.get("name") or "")
            if name in existing:                 # 同名奖项：合并名单，不建重复行
                for target in cfg["awards"]:
                    if str(target.get("name") or "") == name:
                        ids = list(target.get("team_ids") or [])
                        for tid in row["team_ids"]:
                            if tid not in ids:
                                ids.append(tid)
                        target["team_ids"] = ids
                        target["manual"] = True
                        target["count"] = max(int(target.get("count") or 0), len(ids))
                        break
            else:
                cfg["awards"].append(row)
                existing.add(name)
    # 打星队的"手动勾选记录"（star_marks）在旧版里可能指向某个组；
    # 组已经原样搬成行，所以这里只留一份计数说明，不再参与逻辑。
    if group_rows or old_mode or isinstance(raw_marks, dict) and raw_marks:
        cfg["_awards_migrated_from"] = {
            "awards_mode": old_mode or None,
            "award_groups": [{"name": str(g.get("name") or ""),
                              "show": str(g.get("show") or "detail"),
                              "team_count": len(g.get("team_ids") or [])}
                             for g in (raw_groups or []) if isinstance(g, dict)],
            "star_marks": len(raw_marks or {}) if isinstance(raw_marks, dict) else 0,
            "rows_before": [{"name": str(a.get("name") or ""),
                             "count": a.get("count"),
                             "team_count": len(a.get("team_ids") or [])}
                            for a in (raw_awards or []) if isinstance(a, dict)],
            "rows_after": [{"name": str(r.get("name") or ""),
                            "count": r.get("count"),
                            "team_count": len(r.get("team_ids") or []),
                            "auto": r.get("auto"), "manual": bool(r.get("manual"))}
                           for r in cfg["awards"]],
        }
    cfg["award_medal_summary"] = bool(cfg.get("award_medal_summary"))
    # M15：队伍快照（"数据变了"提示的基线）—— 规整成 {team_count:int, names:{str:str}}
    snap = cfg.get("award_team_snapshot")
    names = snap.get("names") if isinstance(snap, dict) else None
    cfg["award_team_snapshot"] = {
        "team_count": _as_int((snap or {}).get("team_count") if isinstance(snap, dict) else 0,
                              0, 0, 1_000_000),
        "names": ({str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {}),
    }
    # M17：旧的「实时侧奖项清单」只合并一次（合并过就记标记，别让删掉的奖项复活）
    merged = cfg.get("_awards_live_merged")
    cfg["_awards_live_merged"] = {
        "at": str((merged or {}).get("at") or "") if isinstance(merged, dict) else "",
        "sources": [str(s) for s in ((merged or {}).get("sources") or [])]
        if isinstance(merged, dict) else [],
    }
    # ---- M13（4b）：队伍名显示模板已固定 -----------------------------------
    # 用户口径：「队伍名怎么显示这个还用设置吗，用不上的话在应用端删了」。
    # 界面已删除该输入框；这里**读到旧值也忽略**（向后兼容：不报错、不迁移成
    # 别的键），一律写成 Resolver 的默认模板 {team.name}。
    _old_display = str(cfg.get("display_name") or "").strip()
    cfg["display_name"] = DEFAULTS["display_name"]
    if _old_display and _old_display != DEFAULTS["display_name"]:
        cfg["_display_name_ignored"] = _old_display
    # ---- M13（4d）：字体已内置 ---------------------------------------------
    # 界面不再让用户设置字体；`font_path` 键**保留**（老配置读进来不报错），
    # 真正的回落发生在 board_fonts.resolve_font_path / board_backend.start_resolver：
    # 只要这个路径文件不存在（或为空），就用程序目录 tools\fonts\MicrosoftYaHei.ttc。
    _old_font = str(cfg.get("font_path") or "").strip()
    _font_ok = False
    if _old_font:
        try:
            _font_ok = Path(_old_font).is_file()
        except OSError:            # 非法路径字符等
            _font_ok = False
    if _old_font and not _font_ok:
        # 指向已经不在的字体（例如别人机器上的绝对路径）——记一笔，运行时回落到内置字体。
        cfg["_font_path_ignored"] = _old_font
    cfg["font_path"] = _old_font if _font_ok else ""
    cfg["extra_args"] = str(cfg.get("extra_args") or "")
    # 照片 / 校徽目录：**没配置**（空）时用应用所在目录下的 滚榜图片 / 校徽，
    # 已经配置过的一律不动（用户手填的路径永远是第一优先）。
    cfg["photos_dir"] = (str(cfg.get("photos_dir") or "").strip()
                         or str(default_photos_dir()))
    cfg["logos_dir"] = (str(cfg.get("logos_dir") or "").strip()
                        or str(default_logos_dir()))
    cfg["window_geometry"] = str(cfg.get("window_geometry") or "")
    cfg["mode"] = _as_choice(cfg.get("mode"), ("live", "roll"), "live")
    cfg["last_tab"] = _as_int(cfg.get("last_tab"), 0, 0, 8)
    cfg["last_tab_live"] = _as_int(cfg.get("last_tab_live"), 0, 0, 4)
    cfg["last_tab_roll"] = _as_int(cfg.get("last_tab_roll"), 0, 0, 3)
    cfg["log_open"] = _as_int(cfg.get("log_open"), 0, 0, 1)
    cfg["last_contest_json"] = str(cfg.get("last_contest_json") or "")
    cfg["last_package_dir"] = str(cfg.get("last_package_dir") or "")
    cfg["schema"] = SCHEMA_VERSION
    return cfg


# --------------------------------------------------------------------------
# 历史记录
# --------------------------------------------------------------------------

HISTORY_FIELDS = (
    "problem_set_id", "name", "problem_count", "team_count", "submission_count",
    "last_used", "contest_json", "package_dir",
    "freeze_minutes", "penalty_minutes", "medals", "awards", "award_medal_summary", "photos_dir",
    "logos_dir",
    "font_path", "play_mode", "click_from_place", "first_to_solve", "speed", "source_mode",
    # M12 的三件（awards_mode / award_groups / star_marks）在 M15 合并进 awards 行的名单；
    # 这里**保留字段名**只为了老历史记录读进来不报错，值由 sanitize_history 迁移到 awards。
    "awards_mode", "award_groups", "star_marks",
    # 模式分离后新增：这条记录属于哪个模式（历史页按它过滤）+ 运行起止与结果
    "mode", "started_at", "result",
)


def sanitize_history(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        psid = str(item.get("problem_set_id") or "").strip()
        if not psid:
            continue
        rec: dict[str, Any] = {}
        for key in HISTORY_FIELDS:
            rec[key] = item.get(key)
        rec["problem_set_id"] = psid
        rec["name"] = str(item.get("name") or f"题目集 {psid}")
        rec["problem_count"] = _as_int(item.get("problem_count"), 0, 0, 10000)
        rec["team_count"] = _as_int(item.get("team_count"), 0, 0, 100000)
        rec["submission_count"] = _as_int(item.get("submission_count"), 0, 0, 10_000_000)
        rec["last_used"] = str(item.get("last_used") or "")
        rec["contest_json"] = str(item.get("contest_json") or "")
        rec["package_dir"] = str(item.get("package_dir") or "")
        rec["font_path"] = str(item.get("font_path") or "")
        _pm2 = str(item.get("play_mode") or "click").strip().lower()
        rec["play_mode"] = {"step": "click", "auto": "auto"}.get(_pm2, _pm2)
        rec["play_mode"] = _as_choice(rec["play_mode"], ("click", "auto-then-click", "auto"), "click")
        rec["click_from_place"] = _as_int(item.get("click_from_place"), 4, 1, 10000)
        rec["first_to_solve"] = bool(item.get("first_to_solve", True))
        rec["source_mode"] = "http"
        rec["speed"] = _as_float(item.get("speed"), 1.0, 0.1, 16.0)
        rec["freeze_minutes"] = _as_int(item.get("freeze_minutes"), 60, 0, 24 * 60)
        rec["penalty_minutes"] = _as_int(item.get("penalty_minutes"), 20, 0, 600)
        rec["medals"] = _clean_medals(item.get("medals"))
        # M15：历史记录里的奖项也走同一套迁移（老记录的 award_groups 追加成行）。
        rec["awards"] = clean_awards(item.get("awards"))
        for row in awards_from_groups(item.get("award_groups")):
            if not any(str(r.get("name") or "") == row["name"] for r in rec["awards"]):
                rec["awards"].append(row)
        rec["award_medal_summary"] = bool(item.get("award_medal_summary"))
        # 老字段只留一份原始值（只读参考），不再参与任何逻辑
        rec["awards_mode"] = str(item.get("awards_mode") or "")
        rec["award_groups"] = []
        rec["star_marks"] = {}
        rec["photos_dir"] = str(item.get("photos_dir") or "")
        rec["logos_dir"] = str(item.get("logos_dir") or "")
        # 旧记录没有 mode：这个程序最早只做滚榜，所以缺省按「赛后滚榜」算，
        # 免得老记录跑到实时榜单的历史页里去。
        rec["mode"] = _as_choice(item.get("mode"), ("live", "roll"), "roll")
        rec["started_at"] = str(item.get("started_at") or "")
        rec["result"] = str(item.get("result") or "")
        out.append(rec)
    return out


# --------------------------------------------------------------------------
# 门面
# --------------------------------------------------------------------------

class Store:
    """设置 + 历史的读写门面。所有方法都不抛异常。"""

    def __init__(self) -> None:
        self.dir, self.dir_note = config_home()
        self.config: dict[str, Any] = dict(DEFAULTS)
        self.history: list[dict[str, Any]] = []
        self.notes: list[str] = []

    # -- 路径 --------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self.dir / CONFIG_NAME

    @property
    def history_path(self) -> Path:
        return self.dir / HISTORY_NAME

    # -- 载入 --------------------------------------------------------------

    def load(self) -> list[str]:
        """载入配置与历史，返回需要展示给用户的提示（可能为空）。"""
        notes: list[str] = []
        if self.dir_note:
            notes.append(self.dir_note)

        raw, err = _read_json(self.config_path)
        if err:
            notes.append(err)
        self.config = sanitize(raw)

        raw_h, err_h = _read_json(self.history_path)
        if err_h:
            notes.append(err_h)
        self.history = sanitize_history(raw_h)

        self.notes = notes
        return notes

    # -- 保存 --------------------------------------------------------------

    def save(self) -> list[str]:
        notes: list[str] = []
        self.config["schema"] = SCHEMA_VERSION
        ok, err = _atomic_write_json(self.config_path, self.config)
        if not ok:
            notes.append(err)
        ok_h, err_h = _atomic_write_json(self.history_path, self.history)
        if not ok_h:
            notes.append(err_h)
        return notes

    # -- 历史 --------------------------------------------------------------

    def remember(self, record: dict[str, Any]) -> None:
        """按 problem_set_id 合并一条历史（新的覆盖旧的，并排到最前面）。"""
        psid = str(record.get("problem_set_id") or "").strip()
        if not psid:
            return
        merged = {k: record.get(k) for k in HISTORY_FIELDS if k in record}
        for old in self.history:
            if old.get("problem_set_id") == psid:
                combined = dict(old)
                combined.update({k: v for k, v in merged.items() if v not in (None, "")})
                merged = combined
                break
        merged["problem_set_id"] = psid
        merged["name"] = str(merged.get("name") or f"题目集 {psid}")
        merged["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
        cleaned = sanitize_history([merged])
        if not cleaned:
            return
        self.history = [h for h in self.history if h.get("problem_set_id") != psid]
        self.history.insert(0, cleaned[0])
        del self.history[200:]  # 别无限涨

    def forget(self, psids: list[str]) -> int:
        drop = {str(p) for p in psids}
        before = len(self.history)
        self.history = [h for h in self.history if str(h.get("problem_set_id")) not in drop]
        return before - len(self.history)

    def find(self, psid: str) -> dict[str, Any] | None:
        for item in self.history:
            if str(item.get("problem_set_id")) == str(psid):
                return item
        return None

def freshest_contest(preferred: str | "Path" | None = None) -> "Path":
    """挑一个**最新的** contest.json 给界面用（打星列表 / 全队标记都要）。

    用户反馈（2026-10-31）：备注改了名字，但「打星队伍」和滚榜「全队标记」两处
    还是旧名字 —— 因为配置里的 ``last_contest_json`` 指着程序根目录那份旧数据，
    而实时榜单用的是 ``live\contest.json``（每次轮询后更新）。这里改成按修改时间
    取最新的一份，两份谁新用谁。
    """
    from pathlib import Path as _P
    root = work_root()
    cands: list[_P] = []
    if preferred:
        cands.append(_P(str(preferred)))
    cands += [root / "contest.json", root / "live" / "contest.json"]
    best: tuple[float, _P] | None = None
    for cand in cands:
        try:
            if cand.is_file():
                stamp = cand.stat().st_mtime
                if best is None or stamp > best[0]:
                    best = (stamp, cand)
        except OSError:
            continue
    if best is not None:
        return best[1]
    return cands[0] if cands else root / "contest.json"
