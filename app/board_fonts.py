# -*- coding: utf-8 -*-
"""board_fonts.py —— 中文字体扫描、排序、缺字检测与 Tk 预览。

为什么需要这个模块
------------------
ICPC Resolver 自带的 Helvetica 不含中文，**必须**用环境变量 ``ICPC_FONT``
指定一个含中文的字体文件，否则中文队名全变方框。

而且「含中文」还不够：榜单格子里用的分隔符是排版符号
``U+2212 −``、``U+2022 •``、``U+2044 ⁄``，不是 ASCII 的 ``-``。
``simhei.ttf`` 恰好缺这三个码位，会把罚次数字渲染成 ``1□□1`` 这种方框
（中文本身正常，所以很难发现）。本模块用 Pillow 逐字渲染、和
「必然缺字的参考码位」逐像素比对，把这类问题在预览阶段就暴露出来。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Any

FONT_EXTS = (".ttc", ".ttf", ".otf")

#: 实测可用的字体，按推荐优先级排列。key = 文件名（小写），value = 说明。
PREFERRED: list[tuple[str, str]] = [
    ("microsoftyahei.ttc", "推荐 · 实测无方框，最稳"),
    ("msyh.ttc", "推荐 · 微软雅黑，实测无方框"),
    ("msyhbd.ttc", "推荐 · 微软雅黑粗体"),
    ("notosanssc-vf.ttf", "推荐 · 思源黑体，覆盖完整"),
    ("notosanssc-regular.otf", "推荐 · 思源黑体"),
    ("deng.ttf", "可用 · 等线"),
    ("dengb.ttf", "可用 · 等线粗体"),
]

#: 已知有问题、但允许用户自己选的字体。
CAUTION: dict[str, str] = {
    "simhei.ttf": "数字可能变方框，不推荐",
}

#: 必须逐字检查的码位 —— 前三个是 Resolver 榜单格子里的分隔符。
CRITICAL_CODEPOINTS: list[tuple[int, str, str]] = [
    (0x2212, "\u2212", "减号（榜单格子分隔符）"),
    (0x2022, "\u2022", "圆点（榜单格子分隔符）"),
    (0x2044, "\u2044", "分数斜杠（榜单格子分隔符）"),
    (0x002D, "-", "普通连字符"),
    (0xFF0D, "\uFF0D", "全角减号"),
]

#: 预览/检测用的中文样本（示例队伍，不含任何真实姓名）。
SAMPLE_TEAM = "张三"
SAMPLE_TEXT = "2026 示例比赛 张三 8 167 1-40"


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------

def _user_font_dir() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return Path(local) / "Microsoft" / "Windows" / "Fonts"


def _iter_font_files(folder: Path) -> list[Path]:
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    out: list[Path] = []
    for item in entries:
        try:
            if item.is_file() and item.suffix.lower() in FONT_EXTS:
                out.append(item)
        except OSError:
            continue
    return out


def _rank_of(filename: str) -> tuple[int, str]:
    low = filename.lower()
    for idx, (name, note) in enumerate(PREFERRED):
        if low == name:
            return idx, note
    if low in CAUTION:
        return 800, CAUTION[low]
    return 500, ""


def _make_entry(path: Path, source: str, source_rank: int) -> tuple[int, str, dict[str, Any]]:
    rank, note = _rank_of(path.name)
    if rank >= 800:                 # 已知有问题 -> 排到最后
        sort_rank = 800
    elif note.startswith("推荐"):
        sort_rank = rank
    elif note.startswith("可用"):
        sort_rank = 100 + rank
    else:
        sort_rank = 400 + source_rank
    return sort_rank, path.name.lower(), {
        "path": str(path),
        "filename": path.name,
        "label": _label(path.name, note, source),
        "note": note,
        "recommended": note.startswith("推荐"),
        "caution": path.name.lower() in CAUTION,
        "source": source,
    }


def describe_path(path: str | Path, source: str = "已保存") -> dict[str, Any]:
    """给单个字体文件造一条下拉项（不做任何目录扫描，开销极小）。"""
    p = Path(path)
    _, _, entry = _make_entry(p, source, 0)
    return entry


def _system_fonts_dir() -> Path:
    """系统字体目录 —— 从 ``%SystemRoot%`` 推出来，**不写死盘符**。"""
    for key in ("SystemRoot", "windir"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return Path(value) / "Fonts"
    return Path(os.environ.get("SystemDrive", "C:") + "/Windows/Fonts")


def scan_dir(folder: Path, source: str, source_rank: int = 0) -> list[dict[str, Any]]:
    """只扫一个目录。启动路径上就用它，不去碰系统字体目录。"""
    return [_make_entry(p, source, source_rank)[2] for p in _iter_font_files(folder)]


def scan(resource_root: Path) -> list[dict[str, Any]]:
    """全量扫描可用的字体文件，推荐的在最前面。

    注意：这会遍历系统字体目录（本机 216 个文件），
    **不要放在启动路径上** —— 只在用户点「刷新字体列表」时调用。

    返回 [{'path', 'filename', 'label', 'note', 'recommended', 'caution', 'source'}, ...]
    """
    sees: list[tuple[int, str, dict[str, Any]]] = []
    seen_paths: set[str] = set()

    def add(folder: Path, source: str, source_rank: int) -> None:
        for path in _iter_font_files(folder):
            key = str(path).lower()
            if key in seen_paths:
                continue
            seen_paths.add(key)
            sees.append(_make_entry(path, source, source_rank))

    add(resource_root / "tools" / "fonts", "tools\\fonts", 0)
    sys_fonts = _system_fonts_dir()
    add(sys_fonts, str(sys_fonts), 1)
    user_dir = _user_font_dir()
    if user_dir is not None:
        add(user_dir, "用户字体", 2)

    sees.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in sees]


def sort_entries(choices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把若干条目按同一套规则排好（供「内置字体 + 已保存字体」这种小集合用）。"""
    ranked = []
    for entry in choices:
        rank, note = _rank_of(entry.get("filename", ""))
        if rank >= 800:
            sr = 800
        elif note.startswith("推荐"):
            sr = rank
        elif note.startswith("可用"):
            sr = 100 + rank
        else:
            sr = 400
        ranked.append((sr, str(entry.get("filename", "")).lower(), entry))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in ranked]


def _label(filename: str, note: str, source: str) -> str:
    if note:
        return f"{filename}（{note}）"
    return f"{filename}（{source} · 未验证）"


def default_choice(choices: list[dict[str, Any]]) -> str:
    """默认选中第一个可用字体：先「推荐」，再退到任意一个。"""
    for item in choices:
        if item["recommended"]:
            return item["path"]
    for item in choices:
        if not item["caution"]:
            return item["path"]
    return choices[0]["path"] if choices else ""


def find_by_name(choices: list[dict[str, Any]], filename: str) -> str:
    low = filename.lower()
    for item in choices:
        if item["filename"].lower() == low:
            return item["path"]
    return ""


#: M13：内置字体（实测无方框）。界面不再让用户挑字体，一律用它。
BUILTIN_FONT_NAME = "MicrosoftYaHei.ttc"
#: 内置字体找不到时的第二批候选（tools\fonts 里还有这些）。
BUILTIN_FONT_FALLBACKS = ("simhei.ttf", "msyh.ttc", "simsun.ttc")


def builtin_font_path(resource_root: str | Path) -> str:
    """返回**内置中文字体**的绝对路径（找不到就返回空串）。

    顺序：
      1. ``<程序目录>\\tools\\fonts\\MicrosoftYaHei.ttc``（打包进 _internal 的那个）；
      2. ``<程序目录>\\tools\\fonts`` 里的其它候选（simhei.ttf 等）；
      3. 再退到 :func:`scan` 里判为「推荐」的第一个字体。

    这是「字体板块删除后」唯一还被使用的字体入口 ——
    ``board_backend.start_resolver`` 会在 ``font_path`` 为空/文件不存在时回落到它。
    """
    root = Path(resource_root)
    folder = root / "tools" / "fonts"
    for name in (BUILTIN_FONT_NAME, *BUILTIN_FONT_FALLBACKS):
        cand = folder / name
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    choices = scan(root)
    return default_choice(choices) if choices else ""


def resolve_font_path(resource_root: str | Path, font_path: Any = "") -> str:
    """把配置里的 ``font_path`` 解析成**一定能用**的字体路径。

    非空且文件存在就直接用（向后兼容 / 高级用户手工替换内置字体也行）；
    否则回落到 :func:`builtin_font_path`。
    """
    text = str(font_path or "").strip()
    if text:
        try:
            if Path(text).is_file():
                return text
        except OSError:
            pass
    return builtin_font_path(resource_root)


# --------------------------------------------------------------------------
# 缺字检测（Pillow 逐像素比对）
# --------------------------------------------------------------------------

def _open_font(path: Path, size: int):
    """打开字体文件。.ttc 是字体集合，逐个 index 试到成功为止。"""
    from PIL import ImageFont

    last: Exception | None = None
    for index in range(0, 6):
        try:
            return ImageFont.truetype(str(path), size, index=index)
        except Exception as exc:  # noqa: BLE001 - Pillow 抛的异常类型很杂
            last = exc
    try:
        return ImageFont.truetype(str(path), size)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(last or exc)) from exc


def _render(font, text: str, box: int = 128) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("L", (box, box), 0)
    ImageDraw.Draw(img).text((10, 10), text, font=font, fill=255)
    return img.tobytes()


def probe_glyphs(path: str | Path, size: int = 48) -> dict[str, Any]:
    """检查字体文件是否缺字。

    判定方法：把字符渲染结果与「必然缺字」的参考码位（U+E000 私用区 /
    U+FFFF 非字符）的渲染结果比对，完全相同即为缺字（方框）。
    这与 ``tools\\FontCheck.java`` 的思路一致，只是改在 Pillow 里做。
    """
    p = Path(path)
    result: dict[str, Any] = {
        "ok": False,
        "exists": p.is_file(),
        "missing": [],     # [{'code', 'char', 'label'}]
        "checked": 0,
        "family": "",
        "style": "",
        "error": "",
    }
    if not p.is_file():
        result["error"] = f"字体文件不存在：{p}"
        return result

    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except ImportError as exc:
        result["error"] = f"缺少 Pillow，无法检测缺字：{exc}"
        return result

    try:
        font = _open_font(p, size)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"无法读取这个字体文件：{exc}"
        return result

    try:
        family, style = font.getname()
        result["family"] = str(family)
        result["style"] = str(style)
    except Exception:  # noqa: BLE001
        pass

    try:
        blanks = {_render(font, "\ue000"), _render(font, "\uffff")}
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"字体渲染失败：{exc}"
        return result

    checks: list[tuple[str, str]] = [(ch, label) for _, ch, label in CRITICAL_CODEPOINTS]
    for ch in "示例比赛张三滚榜0123456789":
        checks.append((ch, f"常用字符 {ch}"))

    for ch, label in checks:
        if ch.isspace():
            continue
        result["checked"] += 1
        try:
            if _render(font, ch) in blanks:
                result["missing"].append({
                    "code": f"U+{ord(ch):04X}",
                    "char": ch,
                    "label": label,
                })
        except Exception:  # noqa: BLE001
            continue

    result["ok"] = bool(result["checked"]) and not result["missing"]
    return result


def describe_probe(probe: dict[str, Any], filename: str = "") -> str:
    """把 probe_glyphs 的结果变成一句中文结论。"""
    name = filename or Path(str(probe.get("family") or "?")).name
    if probe.get("error"):
        return f"{name}：无法检测 —— {probe['error']}"
    missing = probe.get("missing") or []
    if not missing:
        fam = probe.get("family") or name
        return f"{fam}：{probe.get('checked', 0)} 个关键字符全部有字形，可以放心用。"
    codes = " ".join(m["code"] for m in missing[:6])
    more = "" if len(missing) <= 6 else f" 等 {len(missing)} 个"
    return (
        f"{name}：缺少 {len(missing)} 个关键字符（{codes}{more}）—— "
        "滚榜时这些字符会显示成方框，建议换一个字体。"
    )


# --------------------------------------------------------------------------
# 交给 Tk 预览：把字体文件临时加载进本进程
# --------------------------------------------------------------------------

FR_PRIVATE = 0x10

_loaded_private: list[str] = []


def register_private(path: str | Path) -> tuple[bool, str]:
    """用 AddFontResourceExW 把字体文件临时加载进本进程（不改系统、不需管理员）。

    返回 (是否成功, 说明)。成功后 Tk 就能按 family 名使用这个字体。
    """
    if os.name != "nt":
        return False, "当前系统不是 Windows，无法临时加载字体文件。"
    p = Path(path)
    if not p.is_file():
        return False, f"字体文件不存在：{p}"
    try:
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi32.AddFontResourceExW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
        gdi32.AddFontResourceExW.restype = ctypes.c_int
    except Exception as exc:  # noqa: BLE001
        return False, f"无法调用系统字体接口：{exc}"
    try:
        added = gdi32.AddFontResourceExW(str(p), FR_PRIVATE, None)
    except Exception as exc:  # noqa: BLE001
        return False, f"加载字体失败：{exc}"
    if not added:
        return False, "系统拒绝加载这个字体文件（格式不受支持或文件损坏）。"
    if str(p) not in _loaded_private:
        _loaded_private.append(str(p))
    return True, ""


def unregister_all() -> None:
    try:
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi32.RemoveFontResourceExW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
        gdi32.RemoveFontResourceExW.restype = ctypes.c_int
    except Exception:  # noqa: BLE001
        return
    for item in list(_loaded_private):
        try:
            gdi32.RemoveFontResourceExW(item, FR_PRIVATE, None)
        except Exception:  # noqa: BLE001
            pass
        if item in _loaded_private:
            _loaded_private.remove(item)


def family_name(path: str | Path) -> tuple[str, str]:
    """用 Pillow 读出字体文件里的 family / style 名。"""
    try:
        from PIL import ImageFont
    except ImportError:
        return "", ""
    try:
        f = _open_font(Path(path), 24)
        family, style = f.getname()
        return str(family), str(style)
    except Exception:  # noqa: BLE001
        return "", ""


#: 一个几乎不可能存在的字体名，用来问 Tk「找不到时你会退到哪个字体」。
_BOGUS_FAMILY = "\u0001__pta_board_no_such_font__"


def tk_family_for(path: str | Path) -> tuple[str, str]:
    """把字体文件交给 Tk，返回 (Tk 认识的 family 名, 出错说明)。

    坑：字体文件里的 family 名（Pillow 读出来的，例如 ``Microsoft YaHei``、
    ``SimHei``）在中文 Windows 上**不等于** Tk 用的名字 —— Tk 用的是本地化名
    （``微软雅黑``、``黑体``）。所以不能拿 ``tkfont.families()`` 去比对，
    只能建一个 Font 再问 ``actual('family')``：如果它和「随便乱写的字体名」
    退回到的默认字体不同，就说明 Tk 真的找到了这个字体。
    """
    try:
        import tkinter.font as tkfont
    except ImportError:
        return "", "当前环境没有 tkinter"

    ok, why = register_private(path)
    if not ok:
        return "", why

    family, _style = family_name(path)
    if not family:
        return "", "读不出字体文件内部的字体名"

    try:
        actual = tkfont.Font(family=family).actual("family")
        fallback = tkfont.Font(family=_BOGUS_FAMILY).actual("family")
    except Exception as exc:  # noqa: BLE001
        return "", f"Tk 无法创建字体对象：{exc}"

    if actual and actual != fallback:
        return str(actual), ""
    return "", f"Tk 里找不到字体名 {family!r}（可能已经被系统占用或文件损坏）"


if __name__ == "__main__":  # 手工排查用：python board_fonts.py [字体路径]
    root = Path(__file__).resolve().parent.parent
    items = scan(root)
    print(f"共找到 {len(items)} 个字体文件")
    for entry in items[:25]:
        mark = "★" if entry["recommended"] else ("!" if entry["caution"] else " ")
        print(f" {mark} {entry['label']}")
    if len(sys.argv) > 1:
        info = probe_glyphs(sys.argv[1])
        print()
        print(describe_probe(info, Path(sys.argv[1]).name))
