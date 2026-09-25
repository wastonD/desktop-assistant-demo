# -*- coding: utf-8 -*-
"""桌面收纳（纯逻辑，不依赖 Qt）：扫描桌面、分类、标签、撤销；**从不移动桌面上的文件**。

设计原则：抽屉不改变桌面文件的位置，系统自带的桌面文件夹里始终看得到原有的东西。
- 抽屉里显示的就是桌面（和公共桌面）上的东西本身，文件留在原处，资源管理器的"桌面"里照样看得到；
- 「常用」和自建分类只是抽屉自己的**标签**（data/drawer_tags.json：路径 → 分类），归类/改名/删分类都只改标签；
- 从别处拖进抽屉的文件只记一个引用（"钉住"），不移动、不复制；
- 桌面想要干净：用系统自带的"显示桌面图标"开关把图标藏起来（desktop_clean），东西都在抽屉里；
- 每次改标签前存一份快照（data/drawer_tag_ops.json，最近 30 次），可以一步步撤销。
早期版本真的把文件移到了 ~/桌面收纳/<分类>/，并在 data/drawer.json 记了原位置：
legacy_items() 列出还留在那里的东西，restore_legacy() 按记录原样放回（公共桌面的一次 UAC），
原来在「常用」/自建分类文件夹里的，放回后自动变成对应的标签。这是本模块唯一还会移动文件的地方。
从不删除文件；重名一律加 " (2)"，从不覆盖。
"""
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "drawer.json"
TAGS_PATH = ROOT / "data" / "drawer_tags.json"
TAG_OPS_PATH = ROOT / "data" / "drawer_tag_ops.json"
MANIFEST_PATH = ROOT / "data" / "drawer.json"        # 旧版：收纳夹里的文件 → 原位置
OPS_PATH = ROOT / "data" / "drawer_ops.json"         # 旧版：移动历史（迁移时用不到，只保留不删）
_lock = threading.RLock()

FAVORITE = "常用"
MAX_OPS = 30

# (键, 显示名, 扩展名)；按类型自动归的分类
CATEGORIES = [
    ("app", "应用", {".lnk", ".url", ".exe", ".appref-ms", ".msi", ".bat", ".cmd"}),
    ("doc", "文档", {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".txt",
                     ".md", ".rtf", ".odt", ".ods", ".odp", ".wps", ".et", ".dps", ".epub",
                     ".mobi", ".azw3", ".caj", ".tex", ".pages", ".numbers", ".key"}),
    ("image", "图片", {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tif",
                       ".tiff", ".heic", ".psd", ".ai", ".raw", ".cr2", ".nef", ".avif"}),
    ("video", "视频", {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts",
                       ".rmvb"}),
    ("audio", "音乐", {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".ape", ".mid"}),
    ("archive", "压缩包", {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".cab"}),
    ("code", "代码", {".py", ".ipynb", ".js", ".ts", ".html", ".css", ".json", ".xml", ".yaml",
                      ".yml", ".c", ".cpp", ".h", ".java", ".go", ".rs", ".m", ".sh", ".ps1",
                      ".sql", ".ini", ".toml", ".cfg", ".log"}),
    ("folder", "文件夹", set()),
    ("other", "其他", set()),
]
LABELS = {k: label for k, label, _ in CATEGORIES}
TYPE_LABELS = [label for _, label, _ in CATEGORIES]
_EXT_TO_CAT = {ext: k for k, _, exts in CATEGORIES for ext in exts}

DEFAULTS = {
    "stash_dir": "",                 # 旧版收纳夹位置（空 = ~/桌面收纳），只用于把东西放回
    "hotkey": "Ctrl+Alt+D",
    "groups": [FAVORITE],            # 自建分类的顺序（常用永远第一）
}
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
BAD_NAME_CHARS = set('\\/:*?"<>|')


@dataclass
class Item:
    path: Path
    name: str            # 显示名（快捷方式去掉后缀）
    category: str        # 类型键：app/doc/...
    location: str        # desktop / public / pinned（别处的文件，只记引用）/ stash（旧版收纳夹里，待放回）/ system
    is_dir: bool = False
    mtime: float = 0.0
    size: int = 0
    group: str = ""      # 所在分类：有标签 = 标签名；没有 = 类型名


@dataclass
class MoveResult:
    moved: list = field(default_factory=list)     # [(from, to)]
    failed: list = field(default_factory=list)    # [(path, 原因)]
    skipped: list = field(default_factory=list)   # [path]
    cancelled: bool = False


# ---------------------------------------------------------------- 配置与路径
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _known_folder(guid: str) -> Path | None:
    """SHGetKnownFolderPath：桌面被 OneDrive 重定向时也能拿到真实路径。"""
    try:
        class GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                        ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]
        u = uuid.UUID(guid)
        g = GUID(u.fields[0], u.fields[1], u.fields[2],
                 (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
        out = ctypes.c_wchar_p()
        fn = ctypes.windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), ctypes.wintypes.DWORD, ctypes.wintypes.HANDLE,
                       ctypes.POINTER(ctypes.c_wchar_p)]
        fn.restype = ctypes.c_long
        if fn(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
            return None
        path = Path(out.value)
        ctypes.windll.ole32.CoTaskMemFree(out)
        return path
    except (AttributeError, OSError, ValueError):
        return None


def desktop_dir() -> Path:
    override = os.environ.get("ASSISTANT_DESKTOP_DIR")  # 测试/演示用，避免碰真实桌面
    if override:
        return Path(override)
    return _known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641") or Path.home() / "Desktop"


def public_desktop_dir() -> Path | None:
    override = os.environ.get("ASSISTANT_PUBLIC_DESKTOP_DIR")
    if override:
        return Path(override)
    if os.environ.get("ASSISTANT_DESKTOP_DIR"):
        return None  # 测试模式下不看真实公共桌面
    found = _known_folder("C4AA340D-F20F-4863-AFEF-D87EF2E6BA25")
    if found is None:  # 有的机器这个 KnownFolder 查不到（实测返回"找不到文件"），退回 %PUBLIC%\Desktop
        cand = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"
        found = cand if cand.is_dir() else None
    return found


def stash_dir() -> Path:
    """旧版收纳夹（只为了把以前移走的东西放回去）。"""
    override = os.environ.get("ASSISTANT_STASH_DIR")
    if override:
        return Path(override)
    custom = load_config().get("stash_dir")
    return Path(custom) if custom else Path.home() / "桌面收纳"


def _key(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _inside(p, folder) -> bool:
    if folder is None:
        return False
    a, b = _key(p), _key(folder)
    return a.startswith(b + os.sep)


def _is_public(p) -> bool:
    pub = public_desktop_dir()
    return pub is not None and _key(Path(p).parent) == _key(pub)


def on_desktop(p) -> bool:
    """直接放在桌面（或公共桌面）上的东西。"""
    parent = _key(Path(p).parent)
    pub = public_desktop_dir()
    return parent == _key(desktop_dir()) or (pub is not None and parent == _key(pub))


# ---------------------------------------------------------------- 分类与扫描
def classify(path: Path, is_dir: bool | None = None) -> str:
    if is_dir is None:
        is_dir = path.is_dir()
    if is_dir:
        return "folder"
    return _EXT_TO_CAT.get(path.suffix.lower(), "other")


def display_name(path: Path) -> str:
    if path.suffix.lower() in (".lnk", ".url", ".appref-ms"):
        return path.stem
    return path.name


def _hidden(entry: os.DirEntry) -> bool:
    if entry.name.lower() in SKIP_NAMES or entry.name.startswith(("~$", ".")):
        return True
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
        return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
    except (OSError, AttributeError):
        return False


def _item(p: Path, location: str, is_dir: bool, st, group: str | None = None) -> Item:
    kind = classify(p, is_dir)
    return Item(p, display_name(p), kind, location, is_dir, st.st_mtime,
                0 if is_dir else st.st_size, group or LABELS[kind])


def _list(folder: Path | None, location: str, group: str | None = None) -> list[Item]:
    if folder is None or not folder.is_dir():
        return []
    items = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    for entry in entries:
        if _hidden(entry):
            continue
        try:
            is_dir = entry.is_dir()
            st = entry.stat()
        except OSError:
            continue
        items.append(_item(Path(entry.path), location, is_dir, st, group))
    return items


def scan() -> list[Item]:
    """桌面 + 公共桌面 + 钉住的别处文件 + 旧收纳夹里待放回的；分类按标签，没标签按类型。"""
    tags = _load_tags()
    items = _list(desktop_dir(), "desktop") + _list(public_desktop_dir(), "public")
    seen = {_key(i.path) for i in items}
    for raw in tags["pins"]:
        p = Path(raw)
        if _key(p) in seen:
            continue
        try:
            st = p.stat()
        except OSError:
            continue                      # 被删了/移走了：不显示（引用留着，回来了还在）
        seen.add(_key(p))
        items.append(_item(p, "pinned", p.is_dir(), st))
    items += legacy_items()
    groups = tags["groups"]
    for it in items:
        g = groups.get(_key(it.path))
        if g and it.location != "stash":
            it.group = g
    items.sort(key=lambda i: (i.category != "app", i.name.lower()))
    return items


def custom_groups() -> list[str]:
    """常用 + 自建分类（按用户的顺序）。"""
    order = [g for g in load_config().get("groups", [FAVORITE]) if g != FAVORITE]
    used = set(_load_tags()["groups"].values())
    extra = sorted(g for g in used if g not in order and g != FAVORITE and g not in TYPE_LABELS)
    return [FAVORITE] + [g for g in order if g not in TYPE_LABELS] + extra


def group_names(items: list[Item] | None = None) -> list[dict]:
    """侧栏的分类列表：常用 → 自建 → 有内容的类型分类。[{name, kind, count}]"""
    items = scan() if items is None else items
    counts = {}
    for it in items:
        if it.location != "stash":
            counts[it.group] = counts.get(it.group, 0) + 1
    names = custom_groups()
    out = [{"name": FAVORITE, "kind": "fav", "count": counts.get(FAVORITE, 0)}]
    out += [{"name": g, "kind": "custom", "count": counts.get(g, 0)} for g in names[1:]]
    out += [{"name": t, "kind": "type", "count": counts.get(t, 0)} for t in TYPE_LABELS
            if counts.get(t, 0)]
    return out


def group(items: list[Item]) -> dict[str, list[Item]]:
    """按类型键分组（/收纳 概况用）。"""
    out = {k: [] for k, _, _ in CATEGORIES}
    for it in items:
        out.setdefault(it.category, []).append(it)
    return out


def desktop_items(items: list[Item] | None = None, include_public: bool = True) -> list[Item]:
    """桌面上的东西（图标没藏时就散在桌面上）。"""
    items = scan() if items is None else items
    locs = ("desktop", "public") if include_public else ("desktop",)
    return [i for i in items if i.location in locs]


# ---------------------------------------------------------------- 标签（分类）与撤销
def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _load_tags() -> dict:
    data = _read_json(TAGS_PATH, {})
    groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
    pins = data.get("pins") if isinstance(data.get("pins"), list) else []
    return {"groups": dict(groups), "pins": list(pins)}


def _change(label: str, fn, force: bool = False) -> None:
    """改标签的统一入口：先存快照（撤销用，含当时的分类列表），再改、再存。force = 标签没变也记一笔。"""
    with _lock:
        before = _load_tags()
        after = _load_tags()
        fn(after)
        if after == before and not force:
            return
        ops = _read_json(TAG_OPS_PATH, [])
        ops.append({"label": label, "at": time.strftime("%m-%d %H:%M"), "before": before,
                    "groups_cfg": load_config().get("groups", [FAVORITE])})
        _write_json(TAG_OPS_PATH, ops[-MAX_OPS:])
        _write_json(TAGS_PATH, after)


def assign(paths, group: str | None) -> int:
    """归到分类（group=None → 移出自建分类，回到按类型）。别处的文件会顺便钉进抽屉（不移动）。返回处理的个数。"""
    paths = [Path(p) for p in paths if p]
    if not paths:
        return 0
    if group in TYPE_LABELS:
        group = None

    def fn(t):
        for p in paths:
            k = _key(p)
            if not on_desktop(p) and not _inside(p, stash_dir()) and str(p) not in t["pins"]:
                t["pins"].append(str(p))
            if group:
                t["groups"][k] = group
            else:
                t["groups"].pop(k, None)
    label = f"放进「{group}」{len(paths)} 项" if group else f"移出分类 {len(paths)} 项"
    _change(label, fn)
    return len(paths)


move_to_group = assign


def unpin(paths) -> int:
    """把钉进来的别处文件从抽屉里拿掉（文件本身不动）。"""
    keys = {_key(p) for p in paths}

    def fn(t):
        t["pins"] = [p for p in t["pins"] if _key(p) not in keys]
        for k in keys:
            t["groups"].pop(k, None)
    _change(f"从抽屉拿掉 {len(keys)} 项", fn)
    return len(keys)


def list_ops() -> list[dict]:
    return _read_json(TAG_OPS_PATH, [])


def last_op() -> dict | None:
    ops = list_ops()
    return ops[-1] if ops else None


def undo_last() -> dict | None:
    """撤销最近一次标签改动：标签和分类列表都回到那之前。"""
    with _lock:
        ops = list_ops()
        if not ops:
            return None
        op = ops.pop()
        _write_json(TAGS_PATH, op["before"])
        _write_json(TAG_OPS_PATH, ops)
        if "groups_cfg" in op:
            cfg = load_config()
            cfg["groups"] = op["groups_cfg"]
            save_config(cfg)
        return op


# ---------------------------------------------------------------- 分类管理（只改标签和配置）
def _valid_name(name: str) -> str | None:
    name = (name or "").strip()
    if not name or name in (".", "..") or any(c in BAD_NAME_CHARS for c in name) or len(name) > 40:
        return None
    return name


def create_group(name: str) -> str:
    """新建分类。返回错误信息，成功返回空串。"""
    name = _valid_name(name)
    if not name:
        return "名字不合法（不能为空，不能含 \\ / : * ? \" < > |）"
    if name in custom_groups() or name in TYPE_LABELS:
        return "已经有这个分类了"
    cfg = load_config()
    cfg["groups"] = [g for g in cfg.get("groups", [FAVORITE]) if g != name] + [name]
    save_config(cfg)
    return ""


def rename_group(old: str, new: str) -> str:
    new = _valid_name(new)
    if not new:
        return "名字不合法"
    if old == FAVORITE:
        return "「常用」不能改名"
    if old not in custom_groups():
        return "分类不存在"
    if new in custom_groups() or new in TYPE_LABELS:
        return "已经有这个名字了"
    cfg = load_config()
    cfg["groups"] = [new if g == old else g for g in cfg.get("groups", [FAVORITE])]

    def fn(t):
        t["groups"] = {k: (new if g == old else g) for k, g in t["groups"].items()}
    _change(f"分类改名「{old}」→「{new}」", fn)
    save_config(cfg)
    return ""


def delete_group(name: str) -> int:
    """删除分类：里面的东西回到按类型的分类（文件不动，可撤销）。返回受影响的个数。"""
    if name == FAVORITE or name in TYPE_LABELS:
        return 0
    n = sum(1 for g in _load_tags()["groups"].values() if g == name)

    def fn(t):
        t["groups"] = {k: g for k, g in t["groups"].items() if g != name}
    _change(f"删除分类「{name}」", fn, force=True)   # 空分类也记一笔：撤销能把分类找回来
    cfg = load_config()
    cfg["groups"] = [g for g in cfg.get("groups", [FAVORITE]) if g != name]
    save_config(cfg)
    return n


# ---------------------------------------------------------------- 旧版收纳夹：原样放回
def _load_manifest() -> dict:
    return _read_json(MANIFEST_PATH, {})


def _save_manifest(data: dict) -> None:
    _write_json(MANIFEST_PATH, data)


def origin_of(path: Path) -> str | None:
    return _load_manifest().get(_key(path), {}).get("origin")


def legacy_items() -> list[Item]:
    """旧版移进 ~/桌面收纳/ 还没放回的东西（一级子文件夹 = 旧分类）。"""
    root = stash_dir()
    out = []
    for it in _list(root, "stash"):
        if it.is_dir:                     # 旧版一律移进 <收纳夹>/<分类>/，一级文件夹就是分类
            out.extend(_list(it.path, "stash", group=it.path.name))
        else:
            it.group = "其他"
            out.append(it)
    return out


def _unique(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = (target.name, "") if target.is_dir() else (target.stem, target.suffix)
    n = 2
    while True:
        cand = target.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists():
            return cand
        n += 1


def _reason(e: OSError) -> str:
    if isinstance(e, PermissionError):
        return "被占用或没有权限"
    return e.strerror or str(e)


def _run(pairs, cancel=None, progress=None) -> MoveResult:
    """执行一批 (源, 目标路径) 移动；进公共桌面的攒到最后一次性提权。"""
    res = MoveResult()
    plan = [(Path(s), Path(d)) for s, d in pairs]
    total = len(plan)
    admin = []
    with _lock:
        manifest = _load_manifest()
        for i, (src, dst) in enumerate(plan):
            if cancel and cancel():
                res.cancelled = True
                break
            if progress:
                progress(i, total, src.name)
            if not src.exists():
                manifest.pop(_key(src), None)
                res.failed.append((src, "文件不存在"))
                continue
            dst = _unique(dst)
            if _is_public(src) or _is_public(dst):
                admin.append((src, dst))
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            except OSError as e:
                res.failed.append((src, _reason(e)))
                continue
            manifest.pop(_key(src), None)
            res.moved.append((src, dst))
        if admin and not res.cancelled:
            if progress:
                progress(total, total, "等待管理员确认…")
            for src, dst, ok, err in ELEVATE(admin):
                if ok:
                    manifest.pop(_key(src), None)
                    res.moved.append((Path(src), Path(dst)))
                else:
                    res.failed.append((Path(src), err or "需要管理员权限"))
        _save_manifest(manifest)
    return res


def restore_legacy(paths=None, cancel=None, progress=None) -> MoveResult:
    """把旧版收纳夹里的东西放回原处（没记录/原处文件夹没了的放到桌面）。
    原来在「常用」或自建分类文件夹里的，放回后打上同名标签。paths=None 表示全部。"""
    items = legacy_items()
    if paths is not None:
        want = {_key(p) for p in paths}
        items = [i for i in items if _key(i.path) in want]
    desk = desktop_dir()
    manifest = _load_manifest()
    pairs, tag_of = [], {}
    for it in items:
        origin = manifest.get(_key(it.path), {}).get("origin")
        dst = Path(origin) if origin else desk / it.path.name
        if not dst.parent.is_dir():
            dst = desk / it.path.name
        pairs.append((it.path, dst))
        if it.group == FAVORITE or (it.group not in TYPE_LABELS and it.group != "其他"):
            tag_of[_key(it.path)] = it.group
    res = _run(pairs, cancel, progress)
    if tag_of:
        with _lock:
            t = _load_tags()
            for src, dst in res.moved:
                g = tag_of.get(_key(src))
                if g:
                    t["groups"][_key(dst)] = g
            _write_json(TAGS_PATH, t)
        cfg = load_config()
        known = cfg.get("groups", [FAVORITE])
        cfg["groups"] = known + sorted({g for g in tag_of.values() if g not in known})
        save_config(cfg)
    _cleanup_empty(stash_dir())
    return res


def _cleanup_empty(root: Path) -> None:
    """放回后删掉旧收纳夹里空掉的分类文件夹，全空了连收纳夹一起删（只删空目录）。"""
    try:
        for d in root.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        if root.is_dir() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------- 提权移动（公共桌面）
_PS1 = r'''param([string]$job)
$j = Get-Content -Raw -Encoding UTF8 -LiteralPath $job | ConvertFrom-Json
$out = New-Object System.Collections.ArrayList
foreach ($p in $j.pairs) {
  try {
    $d = Split-Path -Parent $p[1]
    if (!(Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    Move-Item -LiteralPath $p[0] -Destination $p[1] -ErrorAction Stop
    if (-not $j.to_public) { icacls "$($p[1])" /reset /T /Q | Out-Null }
    [void]$out.Add(@($p[0], $p[1], $true, ""))
  } catch { [void]$out.Add(@($p[0], $p[1], $false, $_.Exception.Message)) }
}
ConvertTo-Json -InputObject @($out) -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath $j.result
'''


class _SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong),
                ("hwnd", ctypes.wintypes.HWND), ("lpVerb", ctypes.c_wchar_p),
                ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.wintypes.HKEY),
                ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.wintypes.HANDLE),
                ("hProcess", ctypes.wintypes.HANDLE)]


def _elevated_move(pairs, runas=True, timeout_ms=180000):
    """一次 UAC 确认，批量移动。返回 [(src, dst, ok, err)]。runas=False 仅供测试（不提权直接跑脚本）。"""
    work = ROOT / "data" / "elevate"
    work.mkdir(parents=True, exist_ok=True)
    ps1 = work / "move.ps1"
    ps1.write_text(_PS1, encoding="utf-8-sig")
    job = work / "job.json"
    result = work / "result.json"
    if result.exists():
        result.unlink()
    to_public = any(_is_public(d) for _, d in pairs)
    job.write_text(json.dumps({"pairs": [[str(s), str(d)] for s, d in pairs],
                               "result": str(result), "to_public": to_public},
                              ensure_ascii=False), encoding="utf-8-sig")
    params = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{ps1}" "{job}"'
    fail = lambda msg: [(str(s), str(d), False, msg) for s, d in pairs]
    try:
        if runas:
            info = _SHELLEXECUTEINFO()
            info.cbSize = ctypes.sizeof(info)
            info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
            info.lpVerb = "runas"
            info.lpFile = "powershell.exe"
            info.lpParameters = params
            info.nShow = 0
            if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
                return fail("没有获得管理员授权")
            ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, timeout_ms)
            ctypes.windll.kernel32.CloseHandle(info.hProcess)
        else:
            import subprocess
            subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(ps1), str(job)], timeout=timeout_ms / 1000,
                           creationflags=0x08000000)
        rows = json.loads(result.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return fail(f"提权移动失败：{e}")
    if rows and not isinstance(rows[0], list):
        rows = [rows]
    return [(r[0], r[1], bool(r[2]), r[3]) for r in rows]


ELEVATE = _elevated_move  # 测试里替换成普通移动


def summary(res: MoveResult, verb: str) -> str:
    parts = [f"{verb} {len(res.moved)} 项"]
    if res.cancelled:
        parts.append("已取消剩下的")
    if res.failed:
        names = "、".join(Path(p).name for p, _ in res.failed[:3])
        more = " 等" if len(res.failed) > 3 else ""
        parts.append(f"{len(res.failed)} 项没动成（{names}{more}：{res.failed[0][1]}）")
    return "，".join(parts) + "。"
