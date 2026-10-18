# -*- coding: utf-8 -*-
"""往期榜单存档（纯标准库）。

为什么单独做一个模块
--------------------
live-board 的实时运行状态在 ``state.json``，比赛结束后进程不退出、榜单停在最后一刻。
往期站点只需要**只读快照**：把某一刻的 contest / state / 视觉配置冻结下来，
以后无论 live-board 是否还在跑、是否被下一场比赛覆盖，都能按原样重新投影榜单。

设计约定
--------
* 一场比赛一个目录：``<archives>/<slug>/``，slug = ``题目集ID-开始时间戳``。
* 目录里固定四份文件：``contest.json`` / ``state.json`` / ``view.json`` / ``meta.json``。
* ``view.json`` 存 live-board 的 ``config.json`` 原文（视觉主题、奖项、打星名单都在里面）。
* ``index.json`` 是列表页唯一要读的索引，按开始时间倒序；它只是派生缓存，
  丢了可以用 :func:`rebuild_index` 从各目录的 meta.json 重建。
* 所有写盘都是「同目录临时文件 + fsync + os.replace」：读侧（公网站点）可能正在
  读旧文件，必须保证它看到的要么是完整旧内容、要么是完整新内容，不能读到半个 JSON。
* 本模块**不 import** live-board 的 presentation / server，也不碰实时状态：
  存档脚本可以在比赛结束后单独运行，不会触发 live-board 的启动自愈或改写正在跑的文件。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 只读站点由同一台机器上的另一个进程跑；不要往 live-board 目录里丢 __pycache__。
sys.dont_write_bytecode = True

INDEX_NAME = "index.json"
META_NAME = "meta.json"
CONTEST_NAME = "contest.json"
STATE_NAME = "state.json"
VIEW_NAME = "view.json"

# 同一进程里管理员脚本可能连续操作多个场次，锁只保护「读-改-写 index」这段临界区。
_INDEX_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------
def write_json_atomic(path: str | os.PathLike[str], value: Any) -> None:
    """把 JSON 原子地写到 ``path``（UTF-8、缩进 2、末尾换行）。

    为什么要 fsync 再 replace
    -------------------------
    只 write 就 replace 的话，断电/蓝屏后目录项已经指向新文件、内容却可能还在页缓存，
    结果是读侧拿到半截 JSON。这里沿用 live-board 在 2026-10-11 事故后采用的同一套路：
    临时文件 → flush + fsync → os.replace。临时文件必须和目标文件同目录，
    否则跨卷 replace 会退化成拷贝，原子性就没了。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # 失败时清掉临时文件；**不要**碰原文件，旧内容仍然是一份完整数据。
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: str | os.PathLike[str]) -> Any:
    """容错读 JSON：不存在 / 坏文件都返回 None，由调用方决定降级方式。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 时间 / 标题 / slug
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    """当前 UTC 时间，形如 ``2026-10-18T05:20:00Z``（存档统一用 UTC，展示时再转本地）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_epoch(value: Any) -> float | None:
    """把 epoch 数字或 ISO 时间字符串解析成 epoch 秒；解析不了返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        # Python 3.11 的 fromisoformat 已支持结尾 Z；低版本先把 Z 换成 +00:00。
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso_from(value: Any, fallback: str | None = None) -> str | None:
    """统一成 UTC ISO 字符串；原始值已经是 ISO 就原样保留（保留毫秒精度）。"""
    if value is None or value == "":
        return fallback
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return fallback
        if not text.replace(".", "", 1).replace("-", "", 1).isdigit():
            return text
        value = text
    epoch = _parse_epoch(value)
    if epoch is None:
        return fallback
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _contest_meta(contest: dict) -> dict:
    meta = contest.get("contest")
    return meta if isinstance(meta, dict) else {}


def _title_of(contest: dict) -> str:
    """比赛名取值链：contest.title → 顶层 title → problem_set.title。"""
    cmeta = _contest_meta(contest)
    problem_set = contest.get("problem_set") if isinstance(contest.get("problem_set"), dict) else {}
    for value in (cmeta.get("title"), contest.get("title"), problem_set.get("title")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "未命名比赛"


def _psid_of(contest: dict) -> str:
    """题目集 ID：存档 slug 的第一段；缺了就写 unknown，保证目录仍然可建。"""
    cmeta = _contest_meta(contest)
    for value in (contest.get("problem_set_id"), cmeta.get("problem_set_id"), cmeta.get("problemSetId")):
        if value is not None and str(value).strip():
            return str(value).strip()
    return "unknown"


def _start_epoch(contest: dict) -> float | None:
    cmeta = _contest_meta(contest)
    for value in (cmeta.get("start_epoch"), cmeta.get("start_iso"), contest.get("start_epoch"),
                  contest.get("start_iso")):
        epoch = _parse_epoch(value)
        if epoch is not None:
            return epoch
    return None


def _end_epoch(contest: dict) -> float | None:
    cmeta = _contest_meta(contest)
    for value in (cmeta.get("end_epoch"), cmeta.get("end_iso"), contest.get("end_epoch"),
                  contest.get("end_iso")):
        epoch = _parse_epoch(value)
        if epoch is not None:
            return epoch
    return None


def _safe_slug_part(value: str) -> str:
    """只允许字母数字和 ``._-``：slug 会直接当目录名，不能带路径分隔符或控制字符。"""
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value)).strip("._-")
    return text or "unknown"


def make_slug(contest: dict, now_epoch: float | None = None) -> str:
    """``题目集ID-开始时间戳``；没有开始时间就用当前时间（用户指定）。"""
    stamp = _start_epoch(contest)
    if stamp is None:
        stamp = time.time() if now_epoch is None else now_epoch
    return f"{_safe_slug_part(_psid_of(contest))}-{int(stamp)}"


# ---------------------------------------------------------------------------
# meta / index
# ---------------------------------------------------------------------------
def _meta_from_contest(contest: dict, slug: str, saved_at: str,
                       previous: dict | None = None) -> dict:
    """按用户给的字段表生成 meta.json。

    ``revealed`` 在新存档里默认 false；如果同一个 slug 被重新冻结（例如比赛结束后
    又补抓了一次数据），沿用旧 meta 的揭示状态 —— 管理员已经解过的榜不应被重抓重置。
    """
    previous = previous if isinstance(previous, dict) else {}
    started = _start_epoch(contest)
    ended = _end_epoch(contest)
    cmeta = _contest_meta(contest)
    return {
        "psid": _psid_of(contest),
        "title": _title_of(contest),
        "started_at": _iso_from(cmeta.get("start_iso"), _iso_from(started)),
        "ended_at": _iso_from(cmeta.get("end_iso"), _iso_from(ended)),
        "saved_at": saved_at,
        "teams": len(contest.get("teams") or []),
        "subs": len(contest.get("submissions") or []),
        "problems": len(contest.get("problems") or []),
        "slug": slug,
        "revealed": bool(previous.get("revealed", False)),
        "revealed_at": previous.get("revealed_at") if previous.get("revealed") else None,
    }


def _sort_key(entry: dict) -> tuple:
    """列表排序键：开始时间倒序；没有开始时间的按保存时间垫底。"""
    started = _parse_epoch((entry or {}).get("started_at")) or 0.0
    saved = _parse_epoch((entry or {}).get("saved_at")) or 0.0
    return (-started, -saved, str((entry or {}).get("slug") or ""))


def _scan_entries(root: Path) -> list[dict]:
    """扫描 archives_root 下所有含 meta.json 的目录（不信任 index.json 的兜底路径）。"""
    entries: list[dict] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return entries
    for child in children:
        if not child.is_dir():
            continue
        meta = _read_json(child / META_NAME)
        if isinstance(meta, dict) and meta.get("slug"):
            entries.append(meta)
    entries.sort(key=_sort_key)
    return entries


def _load_index(root: Path) -> list[dict]:
    """读 index.json；不是数组或为空时退回扫描目录，避免列表页因为坏索引变成空白。"""
    data = _read_json(root / INDEX_NAME)
    if isinstance(data, list):
        entries = [item for item in data if isinstance(item, dict) and item.get("slug")]
        if entries:
            return entries
    return _scan_entries(root)


def rebuild_index(archives_root: str | os.PathLike[str]) -> list[dict]:
    """扫描所有存档目录并重写 index.json；返回按开始时间倒序的新索引。"""
    root = Path(archives_root)
    root.mkdir(parents=True, exist_ok=True)
    with _INDEX_LOCK:
        entries = _scan_entries(root)
        write_json_atomic(root / INDEX_NAME, entries)
    return entries


def _update_index(root: Path, meta: dict) -> list[dict]:
    """把一场比赛的 meta 合并进索引（同 slug 覆盖），原子写回。"""
    with _INDEX_LOCK:
        entries = _load_index(root)
        entries = [item for item in entries if item.get("slug") != meta.get("slug")]
        entries.append(meta)
        entries.sort(key=_sort_key)
        write_json_atomic(root / INDEX_NAME, entries)
    return entries


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def snapshot_archive(live_dir: str | os.PathLike[str],
                     archives_root: str | os.PathLike[str]) -> str:
    """把一场比赛冻结到 ``archives_root/<slug>/``，返回 slug。

    ``live_dir`` 里读 ``contest.json`` / ``state.json`` / ``config.json``；
    contest 是必需的主数据，state / config 缺失时写空对象而不是失败（站点仍能打开）。
    """
    live = Path(live_dir)
    root = Path(archives_root)
    contest = _read_json(live / CONTEST_NAME)
    if not isinstance(contest, dict):
        raise ValueError(f"{live / CONTEST_NAME} 不存在或不是 JSON 对象，无法存档")

    state = _read_json(live / STATE_NAME)
    if not isinstance(state, dict):
        state = {}
    config = _read_json(live / "config.json")
    if not isinstance(config, dict):
        # 兼容视觉配置本身就叫 view.json 的部署。
        config = _read_json(live / VIEW_NAME)
    if not isinstance(config, dict):
        config = {}

    now = _now_iso()
    slug = make_slug(contest)
    dest = root / slug
    previous = _read_json(dest / META_NAME)
    meta = _meta_from_contest(contest, slug, now, previous if isinstance(previous, dict) else None)

    # 先写内容文件，最后写 meta：目录里出现 meta.json 才算一份完整存档。
    write_json_atomic(dest / CONTEST_NAME, contest)
    write_json_atomic(dest / STATE_NAME, state)
    write_json_atomic(dest / VIEW_NAME, config)
    write_json_atomic(dest / META_NAME, meta)
    _update_index(root, meta)
    return slug


def set_revealed(archives_root: str | os.PathLike[str], slug: str, on: bool = True) -> dict:
    """管理员解榜/重新封存：只改 meta.json 与 index.json 的 revealed / revealed_at。

    ``on=True`` 时写入当前时间；``on=False`` 时清空 revealed_at。
    返回更新后的 meta；slug 不存在时抛 FileNotFoundError（调用方可以据此给 404）。
    """
    root = Path(archives_root)
    slug = str(slug)
    if not re.fullmatch(r"[0-9A-Za-z._-]+", slug) or slug in (".", ".."):
        raise ValueError(f"非法 slug：{slug!r}")
    meta_path = root / slug / META_NAME
    meta = _read_json(meta_path)
    if not isinstance(meta, dict):
        raise FileNotFoundError(f"找不到存档 {slug}（{meta_path}）")

    meta["slug"] = slug
    meta["revealed"] = bool(on)
    meta["revealed_at"] = _now_iso() if on else None
    write_json_atomic(meta_path, meta)
    _update_index(root, meta)
    return meta


def load_meta(archives_root: str | os.PathLike[str], slug: str) -> dict | None:
    """读一份 meta（给管理脚本做巡检用；读不到返回 None，不抛异常）。"""
    if not re.fullmatch(r"[0-9A-Za-z._-]+", str(slug)) or slug in (".", ".."):
        return None
    meta = _read_json(Path(archives_root) / str(slug) / META_NAME)
    return meta if isinstance(meta, dict) else None


__all__ = [
    "snapshot_archive", "set_revealed", "rebuild_index", "load_meta", "make_slug",
    "write_json_atomic", "INDEX_NAME", "META_NAME",
]
