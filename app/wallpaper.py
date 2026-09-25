# -*- coding: utf-8 -*-
"""壁纸日程看板：把日期、今日日程、状态渲染到底图上并设为系统壁纸。

- 底图：config/wallpaper.json 的 base_image；没配置时自动把用户当前壁纸快照为底图。
- 配色：从底图自动取主色，深色底配浅字、浅色底配深字，同色系。
- 手动运行：.venv/Scripts/python -m app.wallpaper
"""
import colorsys
import ctypes
import json
import shutil
import threading
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import store

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "wallpaper.json"
BASE_DIR = ROOT / "assets" / "wallpapers"
RENDERED = ROOT / "data" / "wallpaper_rendered.png"
STATUS_PATH = ROOT / "data" / "status.json"  # 未读邮件数、资讯条数，由后续模块写入

DEFAULTS = {"enabled": True, "base_image": None, "pos_x_ratio": 0.655, "pos_y_ratio": 0.10,
            "panel_width": 480, "text_scale": 1.0, "panel_opacity": None}
WEEKDAYS = "一二三四五六日"

SPI_GETDESKWALLPAPER, SPI_SETDESKWALLPAPER = 0x0073, 0x0014
ENUM_CURRENT_SETTINGS = -1


def _load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def _save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _screen_size():
    """物理分辨率（不受进程 DPI 感知影响）。"""
    class DEVMODEW(ctypes.Structure):
        _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", ctypes.c_ushort),
                    ("dmDriverVersion", ctypes.c_ushort), ("dmSize", ctypes.c_ushort),
                    ("dmDriverExtra", ctypes.c_ushort), ("dmFields", ctypes.c_ulong),
                    ("_union", ctypes.c_byte * 16), ("dmColor", ctypes.c_short),
                    ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                    ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                    ("dmFormName", ctypes.c_wchar * 32), ("dmLogPixels", ctypes.c_ushort),
                    ("dmBitsPerPel", ctypes.c_ulong), ("dmPelsWidth", ctypes.c_ulong),
                    ("dmPelsHeight", ctypes.c_ulong), ("dmRest", ctypes.c_byte * 40)]
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    if ctypes.windll.user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return int(dm.dmPelsWidth), int(dm.dmPelsHeight)
    return 1920, 1080


def _current_wallpaper():
    buf = ctypes.create_unicode_buffer(520)
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETDESKWALLPAPER, 512, buf, 0)
    return buf.value


def _ensure_base(cfg) -> Path:
    if cfg.get("base_image"):
        p = Path(cfg["base_image"])
        if p.exists():
            return p
    current = _current_wallpaper()
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if current and Path(current).exists() and Path(current) != RENDERED:
        dst = BASE_DIR / ("base" + Path(current).suffix.lower())
        shutil.copyfile(current, dst)
    else:
        dst = BASE_DIR / "base.png"
        if not dst.exists():
            Image.new("RGB", _screen_size(), (46, 58, 74)).save(dst)
    cfg["base_image"] = str(dst)
    _save_config(cfg)
    return dst


def _dominant_color(img):
    small = img.convert("RGB").resize((48, 48))
    q = small.quantize(colors=8)
    palette = q.getpalette()
    counts = sorted(q.getcolors(), reverse=True)
    best, best_score = None, -1
    for count, idx in counts:
        r, g, b = palette[idx * 3: idx * 3 + 3]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if v < 0.06:
            continue
        score = count * (0.35 + s)  # 偏好有颜色倾向的主色，纯灰只在没得选时用
        if score > best_score:
            best, best_score = (h, s, v), score
    return best or (0.58, 0.2, 0.35)


def _palette(base_img):
    """返回 (面板底色RGBA, 主文字, 次文字, 高亮)，同色系深浅由底图明度决定。"""
    h, s, v = _dominant_color(base_img)

    def rgb(hh, ss, vv, a=255):
        r, g, b = colorsys.hsv_to_rgb(hh, min(1, ss), min(1, vv))
        return (round(r * 255), round(g * 255), round(b * 255), a)

    if v < 0.55:  # 深色底：更深的同色面板 + 浅字
        return (rgb(h, min(s, 0.55), 0.13, 165), rgb(h, 0.08, 0.97), rgb(h, 0.10, 0.80),
                rgb(h, 0.28, 1.0))
    return (rgb(h, min(s, 0.30), 0.95, 175), rgb(h, 0.45, 0.16), rgb(h, 0.30, 0.35),
            rgb(h, 0.65, 0.45))


def _font(size, bold=False):
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    try:
        return ImageFont.truetype(rf"C:\Windows\Fonts\{name}", size)
    except OSError:
        return ImageFont.truetype(r"C:\Windows\Fonts\simhei.ttf", size)


def _read_status():
    if STATUS_PATH.exists():
        try:
            return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def render() -> Image.Image:
    cfg = _load_config()
    base_path = _ensure_base(cfg)
    W, H = _screen_size()
    base = Image.open(base_path).convert("RGB")
    # 等比铺满裁切
    ratio = max(W / base.width, H / base.height)
    base = base.resize((round(base.width * ratio), round(base.height * ratio)), Image.LANCZOS)
    base = base.crop(((base.width - W) // 2, (base.height - H) // 2,
                      (base.width - W) // 2 + W, (base.height - H) // 2 + H))
    panel_bg, fg, fg_dim, accent = _palette(base)
    if cfg.get("panel_opacity"):
        panel_bg = panel_bg[:3] + (max(40, min(255, int(cfg["panel_opacity"]))),)

    img = base.convert("RGBA")
    draw = ImageDraw.Draw(img)
    # 布局按 2560 宽设计，其他分辨率等比缩放；text_scale 是用户的字号缩放
    sc = W / 2560 * float(cfg.get("text_scale", 1.0))
    x0 = round(W * cfg["pos_x_ratio"])
    y = round(H * cfg["pos_y_ratio"])

    today = date.today()
    events = store.today_events()
    pending = [e for e in events if not e["done"]]
    status = _read_status()

    # 日期标题（带一层淡阴影保证任何底图上可读）
    date_str = f"{today.month}月{today.day}日 星期{WEEKDAYS[today.weekday()]}"
    f_date = _font(round(66 * sc), bold=True)
    draw.text((x0 + 2, y + 2), date_str, font=f_date, fill=(0, 0, 0, 110))
    draw.text((x0, y), date_str, font=f_date, fill=fg)
    y += round(92 * sc)

    subtitle = f"{len(pending)} 件待办" if pending else "今天没有安排"
    if isinstance(status.get("unread_mail"), int):
        subtitle += f" · {status['unread_mail']} 封未读"
    f_sub = _font(round(28 * sc))
    draw.text((x0 + 1, y + 1), subtitle, font=f_sub, fill=(0, 0, 0, 110))
    draw.text((x0, y), subtitle, font=f_sub, fill=fg_dim)
    y += round(64 * sc)

    # 日程面板
    pw = round(cfg["panel_width"] * sc)
    pad = round(26 * sc)
    line_h = round(46 * sc)
    f_item = _font(round(26 * sc))
    f_title = _font(round(27 * sc), bold=True)
    rows = max(len(events), 1)
    ph = pad * 2 + round(44 * sc) + rows * line_h
    if status.get("news_ready"):
        ph += round(52 * sc)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(
        (x0, y, x0 + pw, y + ph), radius=round(16 * sc), fill=panel_bg)
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    ty = y + pad
    draw.text((x0 + pad, ty), "今日日程", font=f_title, fill=accent)
    ty += round(44 * sc)
    if not events:
        draw.text((x0 + pad, ty), "—  空  —", font=f_item, fill=fg_dim)
        ty += line_h
    for e in events:
        hm = e["at"][11:16]
        if e["done"]:
            draw.text((x0 + pad, ty), f"√ {hm}  {e['title']}", font=f_item, fill=fg_dim)
        else:
            draw.text((x0 + pad, ty), f"· {hm}  {e['title']}", font=f_item, fill=fg)
        ty += line_h
    if status.get("news_ready"):
        ty += round(6 * sc)
        draw.line((x0 + pad, ty, x0 + pw - pad, ty), fill=fg_dim[:3] + (90,), width=1)
        ty += round(14 * sc)
        draw.text((x0 + pad, ty), f"今日资讯 {status['news_ready']} 条已备好",
                  font=f_item, fill=fg_dim)
    return img.convert("RGB")


def apply(force=False) -> bool:
    """渲染并设为壁纸。内容没变化时跳过（避免无谓的壁纸刷新闪动）。返回是否更新了。"""
    cfg = _load_config()
    if not cfg.get("enabled", True):
        return False
    img = render()
    RENDERED.parent.mkdir(parents=True, exist_ok=True)
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=2)  # 低压缩档，编码快好几倍，文件稍大无所谓
    data = buf.getvalue()
    if not force and RENDERED.exists() and RENDERED.read_bytes() == data:
        return False
    RENDERED.write_bytes(data)
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, str(RENDERED), 3)  # 3 = 写入配置并广播
    return bool(ok)


_apply_lock = threading.Lock()


def apply_async(force=False):
    """后台线程渲染并设置壁纸。渲染要 2~4 秒，绝不能在界面线程上跑。"""

    def _run():
        with _apply_lock:  # 串行化，避免并发重复渲染
            try:
                apply(force=force)
            except Exception as e:
                print("壁纸渲染失败：", e)

    threading.Thread(target=_run, daemon=True).start()


def set_raw(path) -> bool:
    """直接把某个图片设为壁纸（用于关闭看板时还原底图）。"""
    return bool(ctypes.windll.user32.SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, str(path), 3))


if __name__ == "__main__":
    print("已刷新壁纸" if apply(force=True) else "刷新失败或已禁用")
