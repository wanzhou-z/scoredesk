# -*- coding: utf-8 -*-
"""star_awards.py —— 「星号（``*`` / ``＊``）」的识别与姓名匹配键（纯逻辑，无 tkinter）。

## M16 起这个模块只剩两件事

1. **认星号**：``has_star`` / ``strip_star`` —— 「队名/姓名前带 ``*``」只用来做
   **一键勾选便利**（奖项表里 ``auto="star"`` 那一行的「按 * 自动勾选」按钮）
   和**照片/校徽文件名匹配**，**不再是一种"模式"**；
2. **匹配键**：``name_keys`` —— ``*张三.jpg`` 能配到 ``*张三``，也能配到 ``张三``
   （M13 的学号/姓名匹配都靠它）。

## 删掉了什么（M15/M16）

M12 的「打星模式」整套逻辑（``awards_mode`` / ``award_groups`` / ``star_marks``、
分组、组内解析、按组发奖）**已经全部删除**：

* 奖项来源现在只有**一张奖项表**（`board_config.AWARD_ROW_KEYS`），每行自带名单；
* 「分组」这个概念不存在了 —— 用户口径：「就不要这个板块了直接删掉，如果有打星的选手，
  我直接一个一个标注不用分组了，将全队标记留下就行了」；
* 老配置里的 ``award_groups`` 由 :func:`board_config.awards_from_groups` 迁成
  **普通奖项行**（名字与名单原样保留），迁移后这三个键从配置里消失。
"""

from __future__ import annotations

from typing import Any

#: 半角 + 全角星号。用户可能在任何输入法/表里打出全角 ＊。
STAR_CHARS = "*＊"

#: 「星号在显示名里」这条规则的唯一说明处。
STAR_KEEP_NOTE = "星号保留在队名/姓名里（画面显示 *张三），只用于识别与勾选便利，不做删改"


def has_star(name: Any) -> bool:
    """名字（队名或队员姓名）是否以 ``*`` / ``＊`` 开头（允许前面有空白）。"""
    return str(name or "").lstrip().startswith(tuple(STAR_CHARS))


def strip_star(name: Any) -> str:
    """去掉开头的星号和空白 —— **只给文件名匹配用**，不要拿它当显示名。

    ⚠ 死循环坑（M13 修）：早期写成 ``while text[:1] in STAR_CHARS`` ——
    Python 里 ``"" in "*＊"`` 是 **True**（空串是任何串的子串），所以
    ``strip_star("")`` 会永远循环下去。默认校徽的文件名恰为扩展名
    （``.jpg``）时主干就是空串，整个「生成比赛包」会卡死在这里。
    现在显式判空 + 用 ``startswith``。
    """
    text = str(name or "").strip()
    while text and text[0] in STAR_CHARS:
        text = text[1:].lstrip()
    return text.strip()


def name_keys(name: Any) -> list[str]:
    """给照片/校徽匹配用的候选键：原名 + 去星名（都带星/不带星都能配上）。

    例：``"*张三"`` → ``["*张三", "张三"]``；``"张三"`` → ``["张三"]``。
    这样 ``*张三.jpg`` 能配到 ``*张三``（也能配到 ``张三``），反之亦然。
    """
    raw = str(name or "").strip()
    if not raw:
        return []
    keys = [raw]
    bare = strip_star(raw)
    if bare and bare != raw:
        keys.append(bare)
    return keys


def starred_names(names: Any) -> list[str]:
    """一串名字里带星号的（给日志/自检用，顺序原样保留）。"""
    return [str(n) for n in (names or []) if has_star(n)]
