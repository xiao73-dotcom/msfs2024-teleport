"""FS2024 自由飞行器（本地 EXE + 内嵌 Web 界面）。

运行方式：
    python app.py                       # 语言随系统（中文系统 -> 中文界面）
    MSFS_TELEPORT_LANG=en python app.py # 强制英文界面（开发调试）
    python build.py                     # -> dist/MSFSTeleport.exe（中英双语单包）

界面文案集中在 strings.py，本文件只管逻辑。
地图为在线瓦片（街道 / 卫星 / 地形三个图层，全球覆盖）：
- 中国境内街道用高德（GCJ-02，前端做 WGS-84 <-> GCJ-02 换算）；
- 境外及卫星 / 地形统一用 Esri server.arcgisonline.com（WGS-84，全球覆盖）。
  注意 services.arcgisonline.com 与 tile.openstreetmap.org 在国内网络普遍不可达，
  server.arcgisonline.com 与 tile.openstreetmap.jp 实测可达，勿改回。

依赖：SimConnect, webview(pywebview), requests
"""

import base64
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import sys
import threading
import time
import traceback
import urllib.request
import zipfile

import webview

from geocode import parse_latlon, search
from sim_bridge import (
    SimBridge,
    _APP_DIR,
    diagnose as sim_diagnose,
    focus_msfs,
    logger,
)
import strings
from strings import LANG, js_strings, render, t

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_T = 0x54
VK_E = 0x45
VK_G = 0x47

# 机型预设巡航速度（节）
AIRCRAFT_SPEEDS = {
    "airliner": 450,
    "ga": 120,
    "heli": 90,
    "glider": 60,
    "bush": 110,
    "warplane": 350,
}

# 高度预设（米）：地面 / 起落航线 / 进近 / 标准 / 低巡航 / 中巡航 / 高巡航
ALT_PRESETS_M = [0, 90, 300, 600, 1500, 3000, 9000]
M_PER_FT = 0.3048

# 窗口：标准 Windows 可缩放窗口。初始尺寸与用户上次拖拽后的尺寸都持久化。
WIN_MIN = (860, 560)          # 常规窗口允许保存的最小尺寸下限（启动/记忆校验用）
WIN_MIN_RESIZE = (360, 240)  # create_window 的实际最小尺寸：必须足够小，
                             # 否则“仅地图”小窗会被 pywebview 的 min_size 卡住无法缩小
MAPONLY_SIZE = (850, 560)    # "仅地图"模式默认窗口尺寸（用户可手动调，之后会被记住）
WIN_DEFAULT = (1440, 900)         # 默认窗口尺寸（首次启动或无记忆时使用）

# pywebview 窗口实例（供置顶、定位等使用）
WIN = None


def _ui_path():
    return os.path.join(_APP_DIR, "ui.json")


def _read_ui():
    try:
        with open(_ui_path(), "r", encoding="utf-8") as f:
            v = json.load(f)
            if isinstance(v, dict):
                return v
    except Exception:  # noqa: BLE001
        pass
    return {}


def _write_ui(d):
    try:
        with open(_ui_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("save ui.json failed: %s", e)


def _load_win_size():
    v = _read_ui()
    w, h = int(v.get("w", 0)), int(v.get("h", 0))
    if w >= WIN_MIN[0] and h >= WIN_MIN[1]:
        return (w, h)
    return WIN_DEFAULT


def _save_win_size(w, h):
    d = _read_ui()
    d["w"] = int(w)
    d["h"] = int(h)
    _write_ui(d)


# 左侧面板宽度：可拖拽调节，并持久化到 ui.json
SIDE_MIN, SIDE_MAX, SIDE_DEFAULT = 260, 560, 330


def _load_side_width():
    w = int(_read_ui().get("side", SIDE_DEFAULT) or SIDE_DEFAULT)
    return max(SIDE_MIN, min(SIDE_MAX, w))


def _save_side_width(w):
    d = _read_ui()
    d["side"] = max(SIDE_MIN, min(SIDE_MAX, int(w)))
    _write_ui(d)


def _load_ontop():
    return bool(_read_ui().get("ontop", False))


def _save_ontop(on):
    d = _read_ui()
    d["ontop"] = bool(on)
    _write_ui(d)


def _load_follow():
    return bool(_read_ui().get("follow", False))


def _load_opacity():
    """窗口不透明度偏好（百分比，100 = 不透明）。"""
    try:
        return max(20, min(100, int(_read_ui().get("opacity", 100))))
    except (TypeError, ValueError):
        return 100


def _save_opacity(pct):
    try:
        pct = max(20, min(100, int(pct)))
    except (TypeError, ValueError):
        return
    d = _read_ui()
    if d.get("opacity") != pct:
        d["opacity"] = pct
        _write_ui(d)


def _save_follow(on):
    d = _read_ui()
    d["follow"] = bool(on)
    _write_ui(d)


def _load_autozoom():
    return bool(_read_ui().get("autozoom", False))


def _save_autozoom(on):
    d = _read_ui()
    if d.get("autozoom") != bool(on):
        d["autozoom"] = bool(on)
        _write_ui(d)


# 地图朝向：'north'（北向上，默认）| 'heading'（机头朝上，地图随航向旋转）
def _load_upmode():
    m = str(_read_ui().get("upmode", "north") or "north").lower()
    return "heading" if m == "heading" else "north"


def _save_upmode(mode):
    m = "heading" if str(mode).lower() == "heading" else "north"
    d = _read_ui()
    if d.get("upmode") != m:
        d["upmode"] = m
        _write_ui(d)
    return m


class _WinRect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _WinRect),
        ("rcWork", _WinRect),
        ("dwFlags", wintypes.DWORD),
    ]


def _monitor_work_area(hwnd=None):
    """窗口所在显示器的工作区（任务栏以外的可用区域）。

    返回 dict：
      work  —— 逻辑像素 (left, top, right, bottom)，pywebview 的 move/resize 用这个坐标系
      praw  —— 物理像素 (left, top, right, bottom)，用于 Win32 校正
      scale —— 该显示器的 DPI 缩放系数（1.0 = 100%）

    关键点：pywebview 的 move/resize 用的是**逻辑像素**，而 MonitorFromWindow /
    GetMonitorInfo 返回的是**物理像素**，二者混用会把窗口挪到屏幕外。所以这里统一取
    该窗口所在显示器的真实 DPI，把物理工作区换算成逻辑坐标；多显示器、不同缩放、
    换一台不同分辨率的电脑都能自适应。
    """
    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
    except Exception:  # noqa: BLE001
        pass

    MONITOR_DEFAULTTONEAREST = 0x00000002
    hmon = user32.MonitorFromWindow(hwnd or 0, MONITOR_DEFAULTTONEAREST)
    mi = _MonitorInfo()
    mi.cbSize = ctypes.sizeof(_MonitorInfo)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        raise OSError("GetMonitorInfoW failed")

    dpi = 0
    if hwnd:
        try:
            dpi = user32.GetDpiForWindow(hwnd) or 0
        except Exception:  # noqa: BLE001
            dpi = 0
    if not dpi:
        try:
            dpi = user32.GetDpiForSystem() or 0
        except Exception:  # noqa: BLE001
            dpi = 0
    if not dpi:
        dpi = 96
    scale = max(1.0, float(dpi) / 96.0)

    rc = mi.rcWork
    praw = (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))
    work = (
        rc.left / scale,
        rc.top / scale,
        rc.right / scale,
        rc.bottom / scale,
    )
    return {"work": work, "praw": praw, "scale": scale}


def _geo_fits(g, left, top, right, bottom, tol=8):
    """记住的窗口几何是否还落在当前显示器的工作区内。"""
    try:
        w, h = int(g["w"]), int(g["h"])
        x, y = int(g["x"]), int(g["y"])
    except (KeyError, TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    if w > (right - left) or h > (bottom - top):
        return False
    return (x >= left - tol and y >= top - tol
            and x + w <= right + tol and y + h <= bottom + tol)


def _ensure_visible(hwnd, area):
    """最后一道保险：窗口若仍在工作区外，用物理像素把它拉回可见区域。"""
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    r = _WinRect()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return
    wl, wt, wr, wb = area["praw"]
    w = r.right - r.left
    h = r.bottom - r.top
    x = min(max(r.left, wl), max(wl, wr - min(w, wr - wl)))
    y = min(max(r.top, wt), max(wt, wb - min(h, wb - wt)))
    if (x, y) != (r.left, r.top):
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        user32.SetWindowPos(
            hwnd, 0, x, y, 0, 0,
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        logger.info("window pulled back into work area: (%s,%s) -> (%s,%s)",
                    r.left, r.top, x, y)


def _set_win_rect(hwnd, g, scale):
    """用 Win32 直接设置窗口位置与大小（逻辑像素 × scale = 物理像素）。

    比 pywebview 的 win.resize/win.move 可靠：后者在非 UI 线程调用时会被静默丢弃，
    正是"点了'仅地图'窗口纹丝不动"的根因。SetWindowPos 走 Win32，任意线程都生效，
    且与 set_window_opacity / _ensure_visible 走同一套机制。
    """
    if not hwnd or not g:
        return False
    try:
        user32 = ctypes.windll.user32
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        SWP_FRAMECHANGED = 0x0020
        px = int(round(float(g["x"]) * scale))
        py = int(round(float(g["y"]) * scale))
        pw = int(round(float(g["w"]) * scale))
        ph = int(round(float(g["h"]) * scale))

        # 先读当前窗口尺寸，确认确实需要变更（避免"看起来没反应"的误判）
        r = _WinRect()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        cur_w = r.right - r.left
        cur_h = r.bottom - r.top
        logger.info(
            "SetWindowPos: current %dx%d @(%d,%d) -> target %dx%d @(%d,%d) scale=%.2f",
            cur_w, cur_h, r.left, r.top, pw, ph, px, py, scale,
        )

        ret = user32.SetWindowPos(
            hwnd, 0, px, py, pw, ph,
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED,
        )
        # 用 SendMessage 通知窗口重绘，确保 WebView2 内部布局同步更新
        WM_SIZE = 0x0005
        user32.SendMessageW(hwnd, WM_SIZE, 0, (ph << 16) | pw)
        logger.info(
            "SetWindowPos returned %s -> phys %dx%d @(%d,%d)  (logical %dx%d scale=%.2f)",
            ret, pw, ph, px, py, int(g["w"]), int(g["h"]), scale,
        )
        return bool(ret)
    except Exception as e:  # noqa: BLE001
        logger.debug("SetWindowPos failed: %s", e)
        return False


def _load_map_geo():
    """“仅地图”模式的窗口位置与大小（用户手动调过就以用户的为准）。"""
    g = _read_ui().get("map_geo")
    if not isinstance(g, dict):
        return None
    try:
        return {
            "x": int(g["x"]),
            "y": int(g["y"]),
            "w": int(g["w"]),
            "h": int(g["h"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _save_map_geo(x, y, w, h):
    d = _read_ui()
    d["map_geo"] = {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
    _write_ui(d)


def _fav_path():
    return os.path.join(_APP_DIR, "favorites.json")


def _load_favorites():
    try:
        p = _fav_path()
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:  # noqa: BLE001
        logger.warning("load favorites failed: %s", e)
    return []


def _save_favorites(items):
    try:
        with open(_fav_path(), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("save favorites failed: %s", e)
        return False


# 在线瓦片源。坐标系统一说明：
# - 高德底图是 GCJ-02（国家加密坐标系），与 MSFS 的 WGS-84 差 300~600 米，
#   前端渲染时会做换算；中国境外 Esri 全家桶是标准 WGS-84，无需换算。
# - 中国境内 Esri 街道图 z14+ 是空白瓦片（无数据），所以境内街道走高德更细。
# - 实测可用性（2026-09，本机网络）：server.arcgisonline.com / autonavi 直连 OK；
#   services.arcgisonline.com、tile.openstreetmap.org、Carto 无论直连还是走代理
#   都不通，不要用。
TILE_SOURCES = {
    # 注记标准图（含地名）在 wprd 主机；webrd 只有 style=8 无注记路网（style=7 会 404）
    "amap_street": "https://wprd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&style=7&x={x}&y={y}&z={z}",
    "esri_street": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    "esri_sat": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
}

# Esri 的 {s} 占位不存在，format 时也要求提供，给个固定值
_TILE_SUB = {"amap_street": None, "esri_street": "1", "esri_sat": "1"}


def _tile_cache_dir():
    d = os.path.join(_APP_DIR, "tilecache")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


_TILE_LOCK = threading.Lock()
_TILE_MEM = {}
_TILE_PENDING = {}


def _fetch_tile(url):
    """直连下载瓦片。

    刻意不读系统代理：WebView2 默认走系统代理，而 OSM/Carto 等源在代理上会被
    拦掉（实测 502）；国内源直连反而更快。由 Python 代抓再塞给前端，可控性最好。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"),
        ("Referer", "https://www.amap.com/"),
    ]
    req = urllib.request.Request(url)
    with opener.open(req, timeout=12) as resp:
        return resp.read()


def _tile_uri(raw):
    """瓦片字节 -> data URI。Esri 卫星是 JPEG，高德是 PNG，按魔数判断。"""
    mime = "image/jpeg" if raw[:2] == b"\xff\xd8" else "image/png"
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


# ---------------------------------------------------------------------------
# 内置离线底图（world_z5_sat.zip）
# 把 z0~z5 的全球卫星瓦片全部随 EXE 分发：启动那一屏（z5 总览）永远秒开，
# 平移/突进过程中也总有本地底图兜底 —— 彻底消除"网络慢就出现黑块"。
# zip 内条目：sat/{z}/{x}_{y}.jpg
# ---------------------------------------------------------------------------
_BUNDLE_ZIP_NAME = "world_z5_sat.zip"
_BUNDLE_MAXZ = 5          # 内置底图覆盖的最大级别（含）
_BZ_LOCK = threading.Lock()
_BZ = None                # zipfile.ZipFile | False（不可用）
_BZ_MEM = {}              # (z,x,y) -> data URI


def _pkg_path(name):
    """数据文件：优先 PyInstaller 解压目录（_MEIPASS），其次 EXE/脚本同目录。"""
    base = getattr(sys, "_MEIPASS", "") or ""
    if base:
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return os.path.join(_APP_DIR, name)


def _bundle_zip():
    global _BZ
    if _BZ is not None:
        return _BZ
    with _BZ_LOCK:
        if _BZ is not None:
            return _BZ
        p = _pkg_path(_BUNDLE_ZIP_NAME)
        try:
            if os.path.isfile(p):
                _BZ = zipfile.ZipFile(p)
                logger.info("offline basemap loaded: %s (%d entries)", p, len(_BZ.namelist()))
            else:
                logger.warning("offline basemap not found: %s", p)
                _BZ = False
        except Exception as e:  # noqa: BLE001
            logger.warning("offline basemap unreadable (%s): %s", p, e)
            _BZ = False
    return _BZ


def _bundled_tile_uri(z, x, y):
    """返回内置底图瓦片的 data URI；没有返回 None。"""
    if z < 0 or z > _BUNDLE_MAXZ:
        return None
    key = (z, x, y)
    hit = _BZ_MEM.get(key)
    if hit:
        return hit
    zf = _bundle_zip()
    if not zf:
        return None
    try:
        raw = zf.read("sat/%d/%d_%d.jpg" % (z, x, y))
    except Exception:  # noqa: BLE001  KeyError / BadZipFile
        return None
    uri = _tile_uri(raw)
    if len(_BZ_MEM) < 900:      # 简单上限，避免长时间运行无界增长
        _BZ_MEM[key] = uri
    return uri


# ---------------------------------------------------------------------------
# 自有窗口定位与标题（标题会随界面语言变化，所以匹配不能只认当前语言）
# ---------------------------------------------------------------------------
_SELF_HWND_CACHE = {"hwnd": None}


def _self_title_keys():
    """本程序窗口标题可能出现的全部关键字（中/英两种语言 + 历史名）。

    窗口标题会随界面语言切换而改变，所以**不能**只用 `t("app_title")` 去匹配，
    否则切换语言后"按标题找自己"就会失败（最小化/置顶/热键都会跟着失效）。
    """
    keys = set()
    for name in ("ZH", "EN"):
        d = getattr(strings, name, None)
        if isinstance(d, dict) and d.get("app_title"):
            keys.add(str(d["app_title"]).strip().lower())
    keys.update({"fs relocator", "fs瞬移者", "fs2024", "msfs-2024", "msfsteleport"})
    return {k for k in keys if k}


def _find_self_hwnd(force=False):
    """找到本程序自己的主窗口句柄（按 pid 过滤，排除隐藏的 GDI+ 辅助窗口）。

    优先按标题命中；万一标题被改得不认识了，退回"本进程可见且有标题的最大窗口"，
    并缓存句柄避免每次全量枚举。
    """
    try:
        user32 = ctypes.windll.user32
    except Exception:  # noqa: BLE001
        return None
    cached = _SELF_HWND_CACHE.get("hwnd")
    if cached and not force:
        try:
            if user32.IsWindow(cached):
                return cached
        except Exception:  # noqa: BLE001
            pass
        _SELF_HWND_CACHE["hwnd"] = None

    try:
        self_pid = os.getpid()
        keys = _self_title_keys()
        best = {"hwnd": None, "area": -1}
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        buf = ctypes.create_unicode_buffer(512)
        rect = wintypes.RECT()

        def _each(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != self_pid:
                return True
            user32.GetWindowTextW(hwnd, buf, 512)
            low = (buf.value or "").strip().lower()
            if not low or "gdi+" in low:
                return True
            if any(k in low for k in keys):
                best["hwnd"] = hwnd
                return False                       # 标题命中即终局
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area > best["area"]:
                best["area"] = area
                best["hwnd"] = hwnd
            return True

        user32.EnumWindows(WNDENUMPROC(_each), 0)
        hwnd = best["hwnd"]
        if hwnd:
            _SELF_HWND_CACHE["hwnd"] = hwnd
        return hwnd
    except Exception as e:  # noqa: BLE001
        logger.debug("_find_self_hwnd failed: %s", e)
        return None


def set_window_title(title):
    """把原生窗口标题改成 title（标题栏 / 任务栏 / Alt+Tab 全都跟着变）。

    pywebview 的窗口标题是**创建时固定**的，页面里的 `<title>` 只影响文档标题，
    不会动原生标题栏——所以切换语言后必须显式改一次。
    """
    if not title:
        return False
    ok = False
    try:
        if WIN is not None:
            # 优先 set_title()：它只等 "shown" 事件；而 .title 属性 setter 会等
            # "loaded" 事件，偏偏 load_html() 会 clear 该事件——重载页面时用属性
            # setter 有阻塞风险（最多 15 秒）。
            setter = getattr(WIN, "set_title", None)
            if callable(setter):
                setter(title)
            else:
                WIN.title = title                  # 老版本 pywebview 的退路
            ok = True
    except Exception as e:  # noqa: BLE001
        logger.debug("set window title via pywebview failed: %s", e)
    if not ok:                                     # 兜底：直接改原生标题
        hwnd = _find_self_hwnd()
        if hwnd:
            try:
                ctypes.windll.user32.SetWindowTextW(hwnd, title)
                ok = True
            except Exception as e:  # noqa: BLE001
                logger.debug("SetWindowTextW failed: %s", e)
    return ok


class Api:
    """暴露给前端 JS 的后端接口（pywebview js_api）。"""

    def __init__(self):
        self.bridge = SimBridge()
        self._armed = None
        self._hotkey_started = False
        self._pos = None
        self._pos_lock = threading.Lock()
        self._follow_on = _load_follow()
        self._auto_zoom = _load_autozoom()
        self._up_mode = _load_upmode()
        self._map_only = False
        self._pre_map_geo = None
        self._start_tracker()

    # ---- 地图瓦片代理（街道 / 卫星 / 地形底图） ----
    def tile(self, arg):
        """下载一张瓦片并以 data URI 返回。

        arg: {z, x, y, src} —— src 默认 amap_street；base=true 表示要"内置离线底图"
        （z<=5 的卫星瓦片，随 EXE 分发，任何图层都能拿它当低倍率兜底）。
        返回 {"ok":true,"data":"data:image/...;base64,..."} 或 {"ok":false,"err":"..."}
        """
        try:
            z = int(arg["z"])
            x = int(arg["x"])
            y = int(arg["y"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "err": "bad tile coord"}
        if z < 0 or z > 19:
            return {"ok": False, "err": "zoom out of range"}
        src = str(arg.get("src") or "amap_street")
        if src not in TILE_SOURCES:
            src = "amap_street"

        n = 1 << z
        if x < 0 or y < 0 or x >= n or y >= n:
            return {"ok": False, "err": "tile out of range"}

        # 内置离线底图（z<=5 卫星）优先：秒出、不依赖网络
        if z <= _BUNDLE_MAXZ and (src == "esri_sat" or bool(arg.get("base"))):
            b = _bundled_tile_uri(z, x, y)
            if b:
                return {"ok": True, "data": b, "cached": True, "base": True}

        key = "%s/%d/%d/%d" % (src, z, x, y)
        with _TILE_LOCK:
            hit = _TILE_MEM.get(key)
        if hit:
            return {"ok": True, "data": hit, "cached": True}

        # 磁盘缓存
        try:
            fp = os.path.join(_tile_cache_dir(), key.replace("/", os.sep))
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    raw = f.read()
                uri = _tile_uri(raw)
                with _TILE_LOCK:
                    _TILE_MEM[key] = uri
                    if len(_TILE_MEM) > 600:
                        _TILE_MEM.clear()
                return {"ok": True, "data": uri, "cached": True}
        except Exception:  # noqa: BLE001
            fp = None

        if src.startswith("amap"):
            sub = str((x + y) % 4 + 1)
        else:
            sub = "1"
        url = TILE_SOURCES[src].format(z=z, x=x, y=y, s=sub)
        try:
            raw = _fetch_tile(url)
        except Exception as e:  # noqa: BLE001
            logger.debug("tile fetch failed %s: %s", url, e)
            return {"ok": False, "err": str(e)[:120]}

        # 空瓦片（如 Esri 中国境内 z14+ 街道）按失败处理，让前端走降级
        if len(raw) <= 300:
            return {"ok": False, "err": "empty tile"}

        try:
            if isinstance(fp, str):
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "wb") as f:
                    f.write(raw)
        except Exception:  # noqa: BLE001
            pass

        uri = _tile_uri(raw)
        with _TILE_LOCK:
            _TILE_MEM[key] = uri
            if len(_TILE_MEM) > 600:
                _TILE_MEM.clear()
        return {"ok": True, "data": uri}

    # ---- 飞机位置追踪（后台线程缓存，避免阻塞界面） ----
    def _start_tracker(self):
        def _loop():
            fails = 0
            while True:
                try:
                    if self.bridge.sm is None:
                        # 尚未连接（或游戏已退出）：不主动发起连接，等用户点“连接”。
                        fails = 0
                    else:
                        p = self.bridge.get_position(timeout=2.5)
                        if p:
                            with self._pos_lock:
                                self._pos = p
                            fails = 0
                        else:
                            fails += 1
                            # 连续多次读不到位置，说明连接已失效（如 0xC00000B0）。
                            # connect() 内部会先探活，确认已死才重建，避免误重连。
                            if fails >= 3:
                                logger.info("position unavailable %s times; reconnecting", fails)
                                self.bridge.connect()
                                fails = 0
                except Exception as e:  # noqa: BLE001
                    logger.debug("tracker error: %s", e)
                time.sleep(2.0)

        threading.Thread(target=_loop, daemon=True).start()

    def position(self):
        with self._pos_lock:
            return self._pos

    # ---- 快捷瞬移：绕开“点按钮会抢走游戏焦点”这个根本问题 ----
    def arm(self, payload):
        try:
            lat = float(payload["lat"])
            lon = float(payload["lon"])
            alt = float(payload.get("alt", 1500))
            heading = float(payload.get("heading", 90))
            speed = float(payload.get("speed", 130))
        except (KeyError, ValueError, TypeError):
            return {"ok": False, "msg": t("invalid_params")}
        self._armed = {
            "lat": lat, "lon": lon, "alt": alt,
            "heading": heading, "speed": speed,
        }
        self._start_hotkey()
        logger.info("armed hotkey target: %s", self._armed)
        return {"ok": True, "msg": t("arm_ok", lat=f"{lat:.4f}", lon=f"{lon:.4f}")}

    def arm_status(self):
        return {"ok": self._armed is not None, "target": self._armed}

    def _start_hotkey(self):
        if self._hotkey_started:
            return
        self._hotkey_started = True
        threading.Thread(target=self._hotkey_loop, daemon=True).start()
        logger.info("hotkey watcher started (Ctrl+Alt+T teleport / Ctrl+Alt+E resume control)")

    def _hotkey_loop(self):
        user32 = ctypes.windll.user32
        down_t = down_e = False
        while True:
            try:
                ctrl = user32.GetAsyncKeyState(VK_CONTROL) & 0x8000
                alt = user32.GetAsyncKeyState(VK_MENU) & 0x8000
                p_t = bool(ctrl and alt and (user32.GetAsyncKeyState(VK_T) & 0x8000))
                p_e = bool(ctrl and alt and (user32.GetAsyncKeyState(VK_E) & 0x8000))
                if p_t and not down_t:
                    down_t = True
                    self._fire_hotkey()
                elif not p_t:
                    down_t = False
                if p_e and not down_e:
                    down_e = True
                    self._fire_unpause()
                elif not p_e:
                    down_e = False
            except Exception as e:  # noqa: BLE001
                logger.warning("hotkey loop error: %s", e)
            time.sleep(0.08)

    def _fire_hotkey(self):
        target = self._armed
        if not target:
            logger.info("hotkey pressed but no target armed")
            return
        logger.info("hotkey triggered -> %s", target)

        def _run():
            try:
                if not self.bridge.is_connected():
                    self.bridge.connect()
                self.bridge.teleport(
                    lat=target["lat"], lon=target["lon"],
                    alt=target["alt"], heading=target["heading"],
                    airspeed=target.get("speed", 130),
                )
                logger.info("hotkey teleport done")
            except Exception as e:  # noqa: BLE001
                logger.error("hotkey teleport failed: %s", e, exc_info=True)

        threading.Thread(target=_run, daemon=True).start()

    def _fire_unpause(self):
        logger.info("hotkey resume control")

        def _run():
            try:
                self.bridge.unpause()
                focus_msfs()
                logger.info("hotkey resume control done")
            except Exception as e:  # noqa: BLE001
                logger.error("hotkey resume control failed: %s", e, exc_info=True)

        threading.Thread(target=_run, daemon=True).start()

    # ---- 连接 ----
    def connect(self):
        try:
            self.bridge.connect()
            return {"ok": True, "msg": t("connect_ok")}
        except Exception as e:  # noqa: BLE001
            logger.error("Api.connect failed: %s", e, exc_info=True)
            return {
                "ok": False,
                "msg": t("connect_fail", err=e),
                "detail": traceback.format_exc(),
                "diag": sim_diagnose(),
            }

    def status(self):
        return {"connected": self.bridge.is_connected()}

    def diagnose(self):
        try:
            return sim_diagnose()
        except Exception as e:  # noqa: BLE001
            logger.error("Api.diagnose failed: %s", e, exc_info=True)
            return {"error": str(e)}

    # ---- 地名解析 ----
    def geocode(self, query):
        """完整解析：全部源并行 + 合并排序（最准，约 1~3 秒）。"""
        return self._geocode(query, fast=False)

    def geocode_fast(self, query):
        """快速通道：离线词典 + 最快的一个源。通常 1 秒内返回，
        用来先把候选列表画出来，然后由完整解析结果覆盖。"""
        return self._geocode(query, fast=True)

    def _geocode(self, query, fast=False):
        q = (query or "").strip()
        if not q:
            return {"ok": False, "msg": t("geocode_empty")}
        try:
            # 区域自适应：中文界面/中文地名 -> 国内（高德优先）；英文界面 -> 国外（全球源）
            region = "cn" if strings.LANG == "zh" else "intl"
            results = search(q, region=region, fast=fast)
            if not results:
                return {"ok": False, "msg": t("geocode_none")}
            return {"ok": True, "results": results}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": t("geocode_fail", err=e)}

    def parse(self, text):
        res = parse_latlon(text)
        if res is None:
            return {"ok": False, "msg": t("parse_fail")}
        return {"ok": True, "lat": res[0], "lon": res[1]}

    # ---- 解除暂停（手柄只能控摄像机时的救急） ----
    def unpause(self):
        try:
            if not self.bridge.is_connected():
                self.bridge.connect()
            # 同样先让出焦点，再解除暂停
            user32 = ctypes.windll.user32
            self_hwnd = user32.GetForegroundWindow()
            try:
                if self_hwnd:
                    user32.ShowWindow(self_hwnd, 6)  # SW_MINIMIZE
                    time.sleep(0.25)
            except Exception as e:  # noqa: BLE001
                logger.debug("minimize self before unpause failed: %s", e)

            ok = self.bridge.unpause()

            try:
                if self_hwnd:
                    user32.ShowWindow(self_hwnd, 9)  # SW_RESTORE
                    time.sleep(0.10)
            except Exception as e:  # noqa: BLE001
                logger.debug("restore self after unpause failed: %s", e)

            if ok:
                focus_msfs()
                return {"ok": True, "msg": t("unpause_ok")}
            return {"ok": False, "msg": t("unpause_manual")}
        except Exception as e:  # noqa: BLE001
            logger.error("Api.unpause failed: %s", e, exc_info=True)
            return {"ok": False, "msg": t("unpause_fail", err=e)}

    def _self_hwnd(self):
        """找到本程序自己的主窗口句柄（排除隐藏的 GDI+ 辅助窗口）。

        标题会随界面语言切换而变，故匹配逻辑集中在 `_find_self_hwnd()`，
        内部同时认中/英两种标题并带兜底，避免"换语言后认不出自己"。
        """
        return _find_self_hwnd()

    def _minimize_self(self):
        """瞬移前最小化本窗口，确保 MSFS 真正拿到焦点（不会进入切出暂停）。"""
        hwnd = self._self_hwnd()
        if not hwnd:
            return
        try:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            time.sleep(0.25)
        except Exception as e:  # noqa: BLE001
            logger.debug("minimize self failed: %s", e)

    def _restore_self(self):
        """瞬移后重新显示本窗口，但**不抢焦点**——MSFS 必须保持输入焦点，手柄才可用。"""
        hwnd = self._self_hwnd()
        if not hwnd:
            return
        try:
            user32 = ctypes.windll.user32
            # SW_SHOWNOACTIVATE: 还原为上次的大小位置，但不激活窗口
            user32.ShowWindow(hwnd, 4)
            if user32.IsIconic(hwnd):
                SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
                SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
                user32.SetWindowPos(
                    hwnd, 0, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("restore self failed: %s", e)

    # ---- 瞬移 ----
    def teleport(self, payload):
        try:
            lat = float(payload["lat"])
            lon = float(payload["lon"])
            alt = float(payload.get("alt", 1500))
            heading = float(payload.get("heading", 90))
            speed = float(payload.get("speed", 130))
        except (KeyError, ValueError, TypeError):
            return {"ok": False, "msg": t("invalid_params")}
        try:
            # 点按钮会让本窗口抢占焦点，从而触发 MSFS 的“切出暂停”。
            # 先最小化本窗口，让 MSFS 彻底获得焦点并解除暂停，再执行 set_pos。
            self._minimize_self()
            self.bridge.teleport(
                lat=lat, lon=lon, alt=alt, heading=heading, airspeed=speed
            )
            return {
                "ok": True,
                "msg": t("teleport_ok", lat=f"{lat:.5f}", lon=f"{lon:.5f}", alt=f"{alt:.0f}"),
            }
        except Exception as e:  # noqa: BLE001
            logger.error("Api.teleport failed: %s", e, exc_info=True)
            return {"ok": False, "msg": t("teleport_fail", err=e)}
        finally:
            self._restore_self()

    # ---- 视图控制：置顶 / 跟随飞机 ----
    def set_on_top(self, payload):
        on = bool(payload.get("on")) if isinstance(payload, dict) else bool(payload)
        _save_ontop(on)
        try:
            if WIN is not None:
                WIN.on_top = on
        except Exception as e:  # noqa: BLE001
            logger.warning("set_on_top failed: %s", e)
        return {"ok": True, "on": on}

    def get_on_top(self):
        return {"ok": True, "on": _load_ontop()}

    def set_follow(self, payload):
        """只记录开关状态；真正的“每 10 秒居中”由前端 JS 轮询完成。

        之前用 Python 线程 + evaluate_js 推动画布，会跨线程操作 WebView，
        并且在 SimConnect 上产生并发调用（导致连接 0xC00000B0 失效），故改到 JS。
        """
        on = bool(payload.get("on")) if isinstance(payload, dict) else bool(payload)
        self._follow_on = on
        _save_follow(on)
        logger.info("follow mode -> %s", on)
        return {"ok": True, "on": on}

    def get_follow(self):
        return {"ok": True, "on": self._follow_on}

    # ---- 缩放随高度 ----
    def set_auto_zoom(self, payload):
        on = bool(payload.get("on")) if isinstance(payload, dict) else bool(payload)
        self._auto_zoom = on
        _save_autozoom(on)
        return {"ok": True, "on": on}

    def get_auto_zoom(self):
        return {"ok": True, "on": _load_autozoom()}

    # ---- 地图朝向：北向上 / 机头朝上（前端负责旋转渲染，这里只做持久化） ----
    def set_up_mode(self, payload):
        mode = payload.get("mode") if isinstance(payload, dict) else payload
        self._up_mode = _save_upmode(mode)
        logger.info("map up-mode -> %s", self._up_mode)
        return {"ok": True, "mode": self._up_mode}

    def get_up_mode(self):
        return {"ok": True, "mode": _load_upmode()}

    # ---- 收藏点 ----
    def favorites(self):
        return {"ok": True, "items": _load_favorites()}

    # ---- 界面设置（左侧栏宽度等） ----
    def save_side_width(self, payload):
        try:
            w = max(SIDE_MIN, min(SIDE_MAX, int(payload["width"])))
        except (KeyError, TypeError, ValueError):
            return {"ok": False}
        _save_side_width(w)
        return {"ok": True}

    def add_favorite(self, payload):
        try:
            lat = float(payload["lat"])
            lon = float(payload["lon"])
        except (KeyError, ValueError, TypeError):
            return {"ok": False, "msg": t("fav_need_coord")}
        items = _load_favorites()
        name = (payload.get("name") or "").strip() or f"{lat:.4f}, {lon:.4f}"
        items.append({
            "id": str(int(time.time() * 1000)),
            "name": name,
            "lat": lat,
            "lon": lon,
            "color": payload.get("color") or "red",
        })
        if _save_favorites(items):
            return {"ok": True, "items": items}
        return {"ok": False, "msg": "save failed"}

    def del_favorite(self, payload):
        fid = str(payload.get("id", ""))
        items = [x for x in _load_favorites() if str(x.get("id")) != fid]
        _save_favorites(items)
        return {"ok": True, "items": items}

    # ---- 窗口外观：深色标题栏 / 仅地图模式半透明 ----
    def apply_dark_titlebar(self):
        """把系统标题栏染成与 UI 一致的深藏青色（DWM，Win10 1809+ / Win11）。"""
        hwnd = self._self_hwnd()
        if not hwnd:
            return {"ok": False}
        try:
            dwm = ctypes.windll.dwmapi
            one = ctypes.c_int(1)
            # DWMWA_USE_IMMERSIVE_DARK_MODE（Win10 兼容属性 20）
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(one), ctypes.sizeof(one))
            # DWMWA_CAPTION_COLOR / DWMWA_CAPTION_TEXT_COLOR（Win11 生效，旧系统自动忽略）
            caption = ctypes.c_uint(0x261B14)  # COLORREF(BGR) of #141B26
            text = ctypes.c_uint(0xF4ECE6)     # COLORREF(BGR) of #E6ECF4
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4)
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), 4)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            logger.debug("dark titlebar failed: %s", e)
            return {"ok": False}

    def set_window_opacity(self, payload):
        """调整整窗不透明度（百分比）。拖动滑杆或仅地图模式调用；同时记住偏好。"""
        try:
            pct = int(payload.get("pct", 100))
        except (TypeError, ValueError, AttributeError):
            pct = 100
        pct = max(20, min(100, pct))
        hwnd = self._self_hwnd()
        if not hwnd:
            return {"ok": False}
        try:
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            LWA_ALPHA = 0x02
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if pct >= 100:
                if ex & WS_EX_LAYERED:
                    user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
                    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
            else:
                if not (ex & WS_EX_LAYERED):
                    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
                user32.SetLayeredWindowAttributes(hwnd, 0, int(255 * pct / 100), LWA_ALPHA)
            _save_opacity(pct)
            logger.info("window opacity -> %s%%", pct)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            logger.debug("set opacity failed: %s", e)
            return {"ok": False}

    def get_opacity(self):
        return {"ok": True, "pct": _load_opacity()}

    def map_only_geometry(self, payload=None):
        """进入/退出“仅地图”时调整窗口位置与大小。

        进入：缩到最小尺寸并放到屏幕右上角；用户之后手动拖动/缩放过就以记住的那套为准。
        退出：恢复到进入“仅地图”之前的尺寸与位置。
        """
        try:
            on = bool((payload or {}).get("on", True))
        except (AttributeError, TypeError):
            on = True

        def _apply():
            win = WIN
            if win is None:
                return
            try:
                if on:
                    try:
                        self._pre_map_geo = {
                            "x": int(win.x),
                            "y": int(win.y),
                            "w": int(win.width),
                            "h": int(win.height),
                        }
                    except Exception:  # noqa: BLE001
                        self._pre_map_geo = None

                    hwnd = self._self_hwnd()
                    area = _monitor_work_area(hwnd)
                    left, top, right, bottom = area["work"]
                    avail_w, avail_h = right - left, bottom - top
                    margin = 24

                    g = _load_map_geo()
                    if g and not _geo_fits(g, left, top, right, bottom):
                        # 之前记住的几何属于别的显示器/分辨率，作废重算
                        logger.info("stored map_geo does not fit current display; recompute")
                        g = None
                    if not g:
                        # 仅地图默认一个小巧的角落窗口（用户仍可手动调，且会被记住）
                        m_w, m_h = MAPONLY_SIZE
                        w = int(min(m_w, max(360, avail_w - margin * 2)))
                        h = int(min(m_h, max(260, avail_h - margin * 2)))
                        g = {
                            "x": int(max(left, right - w - margin)),
                            "y": int(top + margin),
                            "w": w,
                            "h": h,
                        }

                    self._map_only = True
                    # 用 pywebview 自己的 move/resize（逻辑像素）：
                    # 它会同步更新 pywebview 内部的窗口状态，任何线程调用都生效；
                    # 而直接 SetWindowPos 会被 pywebview 的 UI 线程用缓存尺寸复位，
                    # 表现就是“点了仅地图窗口纹丝不动”。（3~6 轮实测有效）
                    # 顺序：先 resize 再 move —— pywebview 的 resize 保持左上角不动，
                    # 先缩到目标尺寸、再整体挪到右上角，窗口才不会向右超出屏幕。
                    win.resize(int(g["w"]), int(g["h"]))
                    win.move(int(g["x"]), int(g["y"]))
                    logger.info(
                        "map-only geometry -> %sx%s @(%s,%s) logical work=(%.0f,%.0f,%.0f,%.0f)",
                        g["w"], g["h"], g["x"], g["y"], left, top, right, bottom,
                    )
                else:
                    self._map_only = False
                    g = self._pre_map_geo
                    if g:
                        win.move(int(g["x"]), int(g["y"]))
                        win.resize(int(g["w"]), int(g["h"]))
                        logger.info(
                            "restore geometry -> %sx%s @(%s,%s)",
                            g["w"], g["h"], g["x"], g["y"],
                        )
                    else:
                        # 没记住进入前的几何（极少），恢复上次记住的常规窗口尺寸并居中
                        try:
                            w, h = _load_win_size()
                            hwnd = self._self_hwnd()
                            area = _monitor_work_area(hwnd) if hwnd else None
                            if area:
                                left, top, right, bottom = area["work"]
                                avail_w, avail_h = right - left, bottom - top
                                x = int(left + max(0, (avail_w - w) // 2))
                                y = int(top + max(0, (avail_h - h) // 2))
                            else:
                                x, y = 0, 0
                            win.move(x, y)
                            win.resize(w, h)
                        except Exception:  # noqa: BLE001
                            logger.debug("restore fallback failed")
            except Exception as e:  # noqa: BLE001
                logger.debug("map_only_geometry failed: %s", e)

        # 必须在 JS 调用所在的线程（桥线程）同步执行：
        # pywebview 的 win.move/win.resize 在任何线程调用都会更新内部窗口状态并生效
        # （3~6 轮实测有效）；而直接 SetWindowPos 会被 pywebview 的 UI 线程用缓存尺寸
        # 复位，导致“点了仅地图窗口纹丝不动”。这里同步执行即可生效。
        _apply()
        return {"ok": True}

    # ---- 彩蛋（点击页脚署名触发） ----
    def set_lang(self, payload=None):
        """语言切换：点击页脚“中/En”按钮 -> 中文 / 英文界面互相切换。

        原地重载界面实现全局语言切换，不重启进程——避开 PyInstaller onefile
        重启导致的临时目录（_MEI）冲突及 SimConnect.dll / .NET 加载失败。
        语言选择持久化到 ui.json，下次启动沿用。
        """
        try:
            lang = (payload or {}).get("lang", "") if isinstance(payload, dict) else ""
        except Exception:  # noqa: BLE001
            lang = ""
        lang = "en" if str(lang).strip().lower().startswith("en") else "zh"
        try:
            strings.set_lang(lang)
            d = _read_ui()
            d["lang"] = lang
            _write_ui(d)
            logger.info("language switched to %s (persisted to ui.json)", lang)
        except Exception as e:  # noqa: BLE001
            logger.warning("set_lang persist failed: %s", e)
        # 原生窗口标题必须显式改：pywebview 的窗口标题是创建时固定的，
        # 重载页面只会更新文档 <title>，标题栏 / 任务栏不会跟着变。
        try:
            set_window_title(t("app_title"))
        except Exception as e:  # noqa: BLE001
            logger.warning("sync window title failed: %s", e)
        # 用新语言重新渲染整页并就地重载：窗口与 SimConnect 连接均保持不变
        try:
            win = WIN
            if win is not None:
                win.load_html(build_html())
        except Exception as e:  # noqa: BLE001
            logger.warning("reload for language failed: %s", e)
        # 换标题会重建非客户区，个别系统上会重置深色标题栏属性，补染一次
        try:
            self.apply_dark_titlebar()
        except Exception as e:  # noqa: BLE001
            logger.debug("re-apply dark titlebar failed: %s", e)
        return {"ok": True, "lang": lang}

    def open_external(self, payload=None):
        """彩蛋 2：点击页脚“WorkBuddy 制作” -> 用系统默认浏览器打开邀请链接。

        视觉上保持普通灰字（非超链接样式），只在点击时触发外部打开。
        """
        try:
            url = (payload or {}).get("url", "") if isinstance(payload, dict) else ""
        except Exception:  # noqa: BLE001
            url = ""
        if not url:
            return {"ok": False}
        try:
            os.startfile(url)  # Windows：调用系统关联程序（默认浏览器）打开
            logger.info("easter egg: opened %s", url)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            logger.warning("open_external failed: %s", e)
            return {"ok": False}


HTML_TEMPLATE = r"""
<!doctype html>
<html lang="{{html_lang}}">
<head>
<meta charset="utf-8">
<title>{{app_title}}</title>
<style>
  :root{
    /* MSFS in-game style: dark navy panels, blue accent */
    --bg:#0b0f16; --card:#141b26; --line:#26303f; --ink:#e6ecf4;
    --sub:#8fa0b5; --accent:#2f8fff; --ok:#27c46d; --err:#ff5b66;
    --field:#0e141d; --hover:#1c2736;
    --side-w:{{side_w}}px;
  }
  *{box-sizing:border-box}
  html,body{height:100%;}
  body{margin:0;font:15px/1.55 "Segoe UI","Microsoft YaHei",-apple-system,Roboto,sans-serif;
       background:var(--bg);color:var(--ink);overflow:hidden;}
  ::-webkit-scrollbar{width:9px;height:9px;}
  ::-webkit-scrollbar-track{background:transparent;}
  ::-webkit-scrollbar-thumb{background:#2c3949;border-radius:5px;}
  ::-webkit-scrollbar-thumb:hover{background:#3b4c61;}
  /* Outlook-style two-column layout: controls on the left, map on the right */
  .app{display:flex;height:100vh;}
  .side{width:var(--side-w);flex:0 0 var(--side-w);background:var(--card);
        border-right:1px solid var(--line);overflow-y:auto;padding:14px 14px 20px;
        display:flex;flex-direction:column;}
  /* 子项不压缩；页脚用 margin-top:auto 沉到面板底部（内容超高时随内容滚动） */
  .side>*{flex:0 0 auto;}
  /* 页脚四项居中紧排（不用 space-evenly：窗口拉宽后会被撑散） */
  .credit{margin-top:auto;padding-top:12px;font-size:10px;line-height:1.5;color:var(--sub);
          display:flex;align-items:center;justify-content:center;gap:6px;flex-wrap:wrap;text-align:center;}
  .credit .sep{opacity:.4;}
  .langbtn{cursor:pointer;border:1px solid var(--line);border-radius:4px;padding:1px 7px;
           color:var(--ink);background:var(--field);transition:background .12s,border-color .12s;}
  .langbtn:hover{background:var(--hover);border-color:var(--accent);}
  .main{flex:1;min-width:0;display:flex;flex-direction:column;}
  /* draggable splitter between the side panel and the map */
  .splitter{flex:0 0 8px;cursor:col-resize;background:#10151d;transition:background .12s;
           display:flex;align-items:center;justify-content:center;
           border-left:1px solid var(--line);border-right:1px solid var(--line);}
  .splitter:hover,.splitter.on{background:var(--accent);}
  .splitter::after{content:'';width:2px;height:24px;border-radius:2px;background:#3b4c61;}
  .splitter:hover::after,.splitter.on::after{background:#fff;}
  /* map-only mode */
  .app.maponly .side, .app.maponly .splitter{display:none;}
  .app.maponly .main{flex:1 1 100%;min-width:100%;}
  #btnShowPanel{display:none;}
  .app.maponly #btnShowPanel{display:inline-flex !important;}
  .view-opts{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:8px 0 4px;}
  input[type=range]{flex:1 1 90px;min-width:70px;max-width:150px;height:22px;padding:0;
          background:transparent;border:0;accent-color:var(--accent);cursor:pointer;}
  .chk{display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none;font-size:13px;color:var(--ink);}
  .chk input{width:15px;height:15px;accent-color:var(--accent);}
  .hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;gap:8px;}
  .hd h1{font-size:18px;margin:0;font-weight:600;}
  .stat{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--sub);
        cursor:pointer;user-select:none;}
  .stat:hover{color:var(--accent);}
  .dot{width:9px;height:9px;border-radius:50%;background:#3b4c61;}
  .dot.on{background:var(--ok);}
  .row{display:flex;gap:8px;margin-bottom:10px;align-items:center;}
  input,select{flex:1;min-width:0;padding:9px 10px;border:1px solid var(--line);border-radius:8px;
        font-size:14px;outline:none;background:var(--field);color:var(--ink);}
  /* 目标位置区的窄输入框（朝向 / 自定义空速） */
  input.narrow{flex:0 0 66px;width:66px;min-width:0;}
  /* 经度 / 纬度：窄框的 2 倍宽 */
  input.w2{flex:0 0 132px;width:132px;min-width:0;text-align:center;}
  /* 海拔：窄框的 1.5 倍宽 */
  input.w15{flex:0 0 99px;width:99px;min-width:0;text-align:center;}
  input::placeholder{color:#5d6f85;}
  input:focus,select:focus{border-color:var(--accent);}
  select option{background:var(--field);color:var(--ink);}
  button{padding:9px 14px;border:0;border-radius:8px;background:var(--accent);color:#fff;
         font-size:14px;cursor:pointer;white-space:nowrap;}
  button.ghost{background:var(--hover);color:var(--ink);}
  button.tiny{padding:5px 9px;font-size:13px;}
  button:active{transform:translateY(1px);}
  .list{margin-top:6px;display:flex;flex-direction:column;gap:6px;}
  /* 收藏列表：超过约 5 项出现滚动条 */
  /* 收藏列表已改为下拉框 */
  .item{border:1px solid var(--line);border-radius:9px;padding:7px 9px;cursor:pointer;
        display:flex;align-items:center;gap:8px;font-size:12px;}
  .item:hover{border-color:var(--accent);}
  .item .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .item .co{font-size:12px;color:var(--sub);}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px;}
  .lab{font-size:13px;color:var(--sub);margin:0 0 3px 2px;}
  .msg{margin-top:10px;font-size:13px;min-height:18px;}
  .msg.ok{color:var(--ok);} .msg.err{color:var(--err);}
  .hint{font-size:11px;color:var(--sub);}
  .row-btns{display:flex;gap:6px;align-items:center;}
  .sec{margin-top:12px;padding:12px 0 6px;border-top:2px solid var(--line);}
  .sec.first{margin-top:0;padding-top:0;border-top:0;}
  .sec .cap{font-size:12px;font-weight:700;color:var(--ink);margin-bottom:10px;display:flex;align-items:center;gap:6px;}
  .sec .cap::before{content:'';display:inline-block;width:3px;height:14px;background:var(--accent);border-radius:2px;}
  .stline{font-size:11px;color:var(--sub);margin-top:4px;min-height:14px;word-break:break-all;}
  /* ---- right: map ---- */
  .mapbar{display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--card);
          border-bottom:1px solid var(--line);flex-wrap:wrap;}
  .segbar{display:flex;gap:4px;align-items:center;}
  .segbar .lbl{font-size:12px;color:var(--sub);margin-right:2px;}
  .seg{padding:5px 12px;font-size:13px;background:var(--hover);color:var(--ink);border-radius:6px;cursor:pointer;
       border:1px solid var(--line);user-select:none;}
  .seg.on{background:var(--accent);color:#fff;border-color:var(--accent);}
  /* 地图朝向切换按钮（北向上 / 机头朝上） */
  .upbtn{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;font-size:13px;cursor:pointer;
         user-select:none;border-radius:6px;border:1px solid var(--line);background:var(--hover);color:var(--ink);}
  .upbtn:hover{border-color:var(--accent);}
  .upbtn .upico{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:16px;
         padding:0 3px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.3px;
         background:rgba(255,255,255,.13);color:var(--ink);line-height:1;}
  .upbtn.on{background:var(--accent);border-color:var(--accent);color:#fff;}
  .upbtn.on .upico{background:rgba(255,255,255,.24);color:#fff;}
  .mapwrap{position:relative;flex:1;min-height:0;background:#07090d;}
  canvas{position:absolute;inset:0;width:100%;height:100%;display:block;
         cursor:crosshair;touch-action:none;}
  .legend{position:absolute;top:8px;right:10px;font-size:11px;color:var(--ink);z-index:5;
          background:rgba(13,18,26,.82);border:1px solid var(--line);border-radius:6px;padding:2px 7px;
          pointer-events:none;}
  /* 地图正上方的缩放/定位控件（顶部居中） */
  .mapctrltop{position:absolute;top:8px;left:50%;transform:translateX(-50%);z-index:6;
          display:flex;gap:6px;background:rgba(13,18,26,.82);border:1px solid var(--line);
          border-radius:8px;padding:4px 6px;pointer-events:auto;}
  .mapcoord{position:absolute;left:10px;bottom:8px;font-size:11px;color:var(--ink);z-index:5;
          background:rgba(13,18,26,.82);border:1px solid var(--line);border-radius:6px;padding:2px 7px;
          pointer-events:none;}
  /* 地图底部居中的窗口透明度拖拽条（样式与左下角经纬度条一致） */
  .opbar{position:absolute;left:50%;transform:translateX(-50%);bottom:8px;z-index:6;
         display:flex;align-items:center;gap:8px;font-size:11px;color:var(--ink);
         background:rgba(13,18,26,.82);border:1px solid var(--line);border-radius:6px;padding:3px 10px;
         user-select:none;white-space:nowrap;}
  .opbar-lbl{letter-spacing:.5px;}
  .opbar-val{min-width:36px;text-align:right;color:var(--sub);}
  .opbar input[type=range]{flex:none;width:108px;height:4px;padding:0;border:0;border-radius:2px;
         background:#3a3f45;accent-color:#9aa3ad;cursor:pointer;}
  .opbar input[type=range]:focus{outline:none;border-color:transparent;}
  .tileinfo{position:absolute;right:10px;bottom:8px;font-size:11px;color:var(--sub);z-index:5;
  /* 收藏点确认弹窗 */
  .favask{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:20;
          background:rgba(5,8,12,.35);}
  .favask-box{background:rgba(17,24,34,.96);border:1px solid var(--line);border-radius:12px;
          padding:16px 18px;min-width:260px;max-width:70%;box-shadow:0 8px 30px rgba(0,0,0,.5);}
  .favask-t{font-size:15px;color:var(--ink);margin-bottom:12px;word-break:break-all;}
  .favask-bar{height:3px;background:#243040;border-radius:2px;overflow:hidden;margin-bottom:12px;}
  .favask-bar div{height:100%;background:var(--accent);width:100%;}
  .favask-btns{display:flex;gap:8px;justify-content:flex-end;}
  /* .presets（预设按钮）已移除 */
  .hd h1{font-size:18px;margin:0;font-weight:600;color:#fff;}
  .hd h1::before{content:'✈';margin-right:7px;color:var(--accent);}
</style>
</head>
<body>
<div class="app">
  <!-- left: search / target / favorites -->
  <div class="side">
    <div class="hd">
      <h1>{{h1}}</h1>
      <div class="row-btns">
        <div class="stat" onclick="connect()" title="{{reconnect_title}}"><span class="dot" id="dot"></span><span id="statTxt">{{not_connected}}</span></div>
      </div>
    </div>

    <!-- section: search -->
    <div class="sec first">
      <div class="cap">{{cap_search}}</div>
      <div class="row">
        <input id="q" placeholder="{{ph_place}}" onkeydown="if(event.key==='Enter')geocode()" oninput="onQueryInput()">
        <button onclick="geocode()">{{btn_search}}</button>
      </div>
      <div class="list" id="cands"></div>
    </div>

    <!-- section: target & aircraft -->
    <div class="sec">
      <div class="cap">{{cap_target}}</div>
      <!-- 经度 + 纬度（横排平分） -->
      <div class="row">
        <div style="flex:1"><div class="lab">{{lab_lon}}</div><input id="lon" class="w2" type="number" step="0.00001" placeholder="{{ph_lon}}"></div>
        <div style="flex:1"><div class="lab">{{lab_lat}}</div><input id="lat" class="w2" type="number" step="0.00001" placeholder="{{ph_lat}}"></div>
      </div>
      <!-- 海拔（加宽 1.5×）+ 单位选择（尺寸不变，随海拔框右移）+ 朝向 -->
      <div class="row">
        <div style="flex:0 0 99px"><div class="lab">{{lab_alt}}</div><input id="alt" class="w15" type="number" value="1500"></div>
        <div style="flex:0 0 104px;margin-left:10px"><div class="lab">&nbsp;</div><select id="altUnit" onchange="onUnit()"><option value="m">{{unit_m}}</option><option value="ft">{{unit_ft}}</option></select></div>
        <div style="flex:1"><div class="lab">{{lab_hdg}}</div><input id="hdg" class="narrow" type="number" value="90"></div>
      </div>
      <!-- 预设空速（下拉）+ 自定义空速（数字） -->
      <div class="row">
        <div style="flex:1">
          <div class="lab">{{lab_preset_speed}}</div>
          <select id="acType" onchange="onAcType()">
            <option value="ga">{{ac_ga}}</option>
            <option value="airliner">{{ac_airliner}}</option>
            <option value="heli">{{ac_heli}}</option>
            <option value="glider">{{ac_glider}}</option>
            <option value="bush">{{ac_bush}}</option>
            <option value="warplane">{{ac_warplane}}</option>
            <option value="custom">{{ac_custom}}</option>
          </select>
        </div>
        <div style="flex:1">
          <div class="lab">{{lab_speed}}</div>
          <input id="spd" class="narrow" type="number" value="120">
        </div>
      </div>
      <div class="row">
        <button onclick="teleportCurrent()">{{btn_teleport}}</button>
        <button class="ghost" onclick="armHotkey()">{{btn_arm}}</button>
      </div>
    </div>

    <!-- section: favorites -->
    <div class="sec">
      <div class="cap">{{lab_fav}}</div>
      <div class="row">
        <select id="favSelect" onchange="onFavSelect(this.value)" title="{{lab_fav}}">
          <option value="">{{ph_fav_choose}}</option>
        </select>
        <button class="ghost tiny" onclick="delSelFav()" title="{{btn_fav_del}}">{{btn_fav_del}}</button>
      </div>
      <div class="row">
        <input id="favName" placeholder="{{ph_fav_name}}">
        <button class="ghost" onclick="addFav()">{{btn_fav_add}}</button>
      </div>
    </div>

    <!-- section: messages（只保留一行文字提示；故障时显示红色可操作建议） -->
    <div class="sec">
      <div class="msg" id="msg"></div>
    </div>

    <!-- footer: language toggle / credits / version -->
    <div class="credit">
      <span class="langbtn" id="langBtn" title="{{hint_lang}}" onclick="toggleLang()">{{lang_label}}</span>
      <span class="sep">·</span>
      <span class="cd" id="cdDesign" style="cursor:pointer" onclick="openMail()">{{credit_design}}</span>
      <span class="sep">·</span>
      <span id="cdMade" style="cursor:pointer" onclick="eggWb()">{{credit_made}}</span>
      <span class="sep">·</span>
      <span class="ver">{{credit_ver}}</span>
    </div>
  </div>

  <!-- draggable splitter -->
  <div class="splitter" id="splitter" title=""></div>

  <!-- right: map -->
  <div class="main">
    <div class="mapbar">
      <span class="segbar">
        <span class="lbl">{{layer_lbl}}</span>
        <span class="seg" id="segStreet" onclick="setLayer('street')">{{layer_street}}</span>
        <span class="seg on" id="segSat" onclick="setLayer('sat')">{{layer_sat}}</span>
        <span class="upbtn" id="btnUp" onclick="toggleUpMode()" title="{{hint_upmode}}"><span class="upico" id="upIco">N</span><span id="upTxt">{{opt_northup}}</span></span>
      </span>
      <span class="row-btns" style="margin-left:auto">
        <label class="chk" style="font-size:12px" title="{{hint_view}}"><input type="checkbox" id="chkTop" onchange="setOnTop(this.checked)"> <span>{{opt_ontop}}</span></label>
        <label class="chk" style="font-size:12px" title="{{hint_view}}"><input type="checkbox" id="chkFollow" onchange="setFollow(this.checked)"> <span>{{opt_follow}}</span></label>
        <label class="chk" style="font-size:12px" title="{{opt_autozoom}}"><input type="checkbox" id="chkAutoZoom" onchange="setAutoZoom(this.checked)"> <span>{{opt_autozoom}}</span></label>
        <button class="ghost tiny" id="btnMapOnly" onclick="toggleMapOnly(true)">{{btn_maponly}}</button>
        <button class="ghost tiny" id="btnShowPanel" onclick="toggleMapOnly(false)" style="display:none">{{btn_showpanel}}</button>
      </span>
    </div>
    <div class="mapwrap" id="mapwrap">
      <canvas id="map" title="{{map_hint}}"></canvas>
      <!-- 地图正上方的缩放与定位控件（置于地图顶部中央） -->
      <div class="mapctrltop">
        <button class="ghost tiny" onclick="zoomBy(-1)" title="{{btn_zoom_out}}">{{btn_zoom_out}}</button>
        <button class="ghost tiny" onclick="zoomBy(1)" title="{{btn_zoom_in}}">{{btn_zoom_in}}</button>
        <button class="ghost tiny" onclick="locateAircraft()" title="{{btn_locate}}">{{btn_locate}}</button>
      </div>
      <div class="legend" id="legend"></div>
      <div class="mapcoord" id="mapCoord">—</div>
      <div class="opbar" id="opBar">
        <span class="opbar-lbl">{{lbl_opacity}}</span>
        <input type="range" id="opSlider" min="30" max="100" step="5" value="100" title="{{hint_view}}">
        <span class="opbar-val" id="opVal">100%</span>
      </div>
      <div class="tileinfo" id="tileInfo"></div>
      <!-- 收藏点确认弹窗：居中预览 5 秒 -->
      <div class="favask" id="favAsk" style="display:none">
        <div class="favask-box">
          <div class="favask-t" id="favAskText"></div>
          <div class="favask-bar"><div id="favAskBar"></div></div>
          <div class="favask-btns">
            <button onclick="favAskAnswer(true)">{{ask_yes}}</button>
            <button class="ghost" onclick="favAskAnswer(false)">{{ask_no}}</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const TXT = __JS_TXT__;
const FAV_COLORS = ['#d9434e','#2f6fed','#e8b425','#1f9d55'];
function tr(k,kv){let s=TXT[k]||k; if(kv){for(const a in kv){s=s.split('{'+a+'}').join(kv[a]);}} return s;}

function api(){return (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;}
function isApiReady(){const a=api(); return !!(a && (a.connect || a.geocode));}

// Readiness probe: the pywebviewready event is unreliable in some environments,
// so poll until the api methods are actually usable.
let ready=false;
const readyTimer=setInterval(()=>{
  if(isApiReady()){ready=true;clearInterval(readyTimer);connect();loadFav();refreshTiles(true);initViewOpts();}
},150);
setTimeout(()=>{clearInterval(readyTimer); if(!ready) setMsg(tr('js_bridge_dead'),'err');},20000);

// Unified call wrapper: every exception is surfaced in the message area.
// IMPORTANT: never pass undefined as the argument - pywebview would serialize it
// as an extra positional argument ("takes 1 positional argument but 2 were given").
function call(fn,arg){
  const a=api();
  if(arg===undefined) return a[fn]();
  return a[fn](arg);
}
function toggleLang(){
  // “中/En”按钮：点击切换中英文界面（原位重载，不重启进程）
  const cur=(document.documentElement.lang||'').toLowerCase().startsWith('en')?'en':'zh';
  if(isApiReady()) safe('set_lang',{lang: cur==='en' ? 'zh' : 'en'});
}
function openMail(){
  // 点击页脚署名（“JerryXiao设计”） -> 用系统默认邮件程序给 jerryxiao@msn.com 写信（主题=当前语言软件名）
  const subj = encodeURIComponent(TXT.app_title || 'FS Relocator');
  const url = 'mailto:jerryxiao@msn.com?subject=' + subj;
  if(isApiReady()) safe('open_external',{url:url});
}
function eggWb(){
  // 彩蛋 2：点击“WorkBuddy 制作” -> 系统默认浏览器打开邀请链接（视觉保持普通文字）
  if(isApiReady()) safe('open_external',{url:'https://www.workbuddy.cn/events/invite?inviteCode=2e01i4hpyk71'});
}
function safe(fn,arg){
  return call(fn,arg).catch(e=>{
    setMsg(tr('js_call_error')+(e && e.message?e.message:e),"err");
    return {ok:false};
  });
}

// 消息位：只显示错误/警告（红色），成功提示一律不展示（保持界面干净）。
// ⚠ 关键：**非错误调用必须清空旧提示**——否则上一次的红字会一直留在面板上
// （旧实现遇到 kind!=='err' 直接 return，连 setMsg('','') 都清不掉；用户 2026-09-16 反馈的残留问题）。
let _msgTimer=null;
function clearMsg(){
  if(_msgTimer){ clearTimeout(_msgTimer); _msgTimer=null; }
  const m=document.getElementById('msg');
  if(!m) return;
  if(m.textContent) m.textContent='';
  if(m.className!=='msg') m.className='msg';
}
function setMsg(t,kind,ttl){
  // ttl(ms)：提示性文字自动消失的时长；真正的故障不传 ttl，保留到状态变化为止
  if(_msgTimer){ clearTimeout(_msgTimer); _msgTimer=null; }
  if(kind==='err' && t){
    const m=document.getElementById('msg'); m.textContent=t; m.className='msg err';
    if(ttl>0) _msgTimer=setTimeout(clearMsg, ttl);
    return;
  }
  clearMsg();
}
function setStat(on,txt){
  const dot=document.getElementById('dot');
  const was=dot.className.indexOf('on')>=0;
  dot.className='dot'+(on?' on':'');
  document.getElementById('statTxt').textContent=txt||(on?TXT.connected:TXT.not_connected);
  if(on && !was) clearMsg();   // 刚连上：把“未连接”阶段留下的红色提示清掉
}

// ---------------- sidebar splitter ----------------
const SIDE_MIN=260, SIDE_MAX=560;
const sideEl=document.querySelector('.side');
let sdrag=null;
document.getElementById('splitter').addEventListener('mousedown',e=>{
  sdrag={x:e.clientX,w:sideEl.offsetWidth};
  document.getElementById('splitter').classList.add('on');
  e.preventDefault();
});
window.addEventListener('mousemove',e=>{
  if(!sdrag) return;
  const w=Math.max(SIDE_MIN,Math.min(SIDE_MAX,sdrag.w+(e.clientX-sdrag.x)));
  document.documentElement.style.setProperty('--side-w',w+'px');
});
window.addEventListener('mouseup',()=>{
  if(!sdrag) return;
  sdrag=null;
  document.getElementById('splitter').classList.remove('on');
  const w=parseInt(getComputedStyle(document.documentElement).getPropertyValue('--side-w'))||330;
  if(isApiReady()) safe('save_side_width',{width:w});
  refreshTiles(true);
});

// ---------------- map engine ----------------
const cv=document.getElementById('map'), ctx=cv.getContext('2d');
// 启动即显示 z5 全球卫星总览：z5 卫星瓦片全球仅 1024 张，任何网速都能秒开；
// 玩家选点后再从这一级"突进"到目标细节级（见 selectLocation）。
let view={lon:105,lat:32,z:5};
let target=null, acft=null, favs=[];
let followOn=false;                 // “跟随飞机”开关
let _bootLocate=true;               // 启动后拿到第一个飞机位置时，强制把镜头定位到飞机（只做一次）
let autoZoom=false;                 // “缩放随高度”开关（手动缩放会自动退出）
let favAsk=null;                    // 收藏点预览确认状态（5 秒倒计时）
let lastAcCat=null;                 // 上次识别到的机型，机型变化时才自动切换空速档
let layer='sat';                    // 'street' | 'sat' - 启动默认卫星图（z5 总览）
let curSrc='amap_street';           // resolved from layer + view position
let curGcj=false;                   // true when curSrc is GCJ-02 (Amap)
let upMode='north';                 // 'north'（北向上，默认）| 'heading'（机头朝上）
let mapRot=0;                       // 目标旋转角（弧度，顺时针为正）；北向上恒为 0
let mapRotDisp=0;                   // 实际渲染用旋转角（切换朝向时由动画插值）
let mapTiltDisp=0;                  // 实际渲染用倾斜角（弧度，0=俯视；选点后 45°）
const TILT_MAX=45*Math.PI/180;      // 倾斜上限：地图平面绕屏幕水平轴转 45°
const ORBIT_SPEED=4.5*Math.PI/180;  // 环绕速度（弧度/秒，逆时针），约 80 秒一圈
let _animRot=null;                  // 朝向/倾斜过渡动画状态
let _orbit=null;                    // 环绕状态（选点定位结束后自动开始）
let _uiRaf=0;                       // 共用动画循环句柄
let _panAnim=null;                  // 镜头平滑平移状态
let _tiltGoal=0;                    // 倾斜目标：0 或 TILT_MAX
let DPR=1, tileToken=0;
let CV_W=1, CV_H=1;                 // logical canvas size (matches buffer/DPR)
const tileStore=new Map();          // key -> {img|null, busy:bool}
let tileStat={load:0, fail:0};
const MAXZ={street:18, sat:19};
const BASE_MAXZ=5;                  // 内置离线底图（随 EXE 分发的 z0~z5 卫星瓦片）

// ---- WGS-84 <-> GCJ-02 (Amap tiles are offset by a few hundred metres) ----
const _PI=Math.PI, _A=6378245.0, _EE=0.00669342162296594323;
function outOfChina(lon,lat){ return (lon<72.004||lon>137.8347||lat<0.8293||lat>55.8271); }
function _tlat(x,y){
  let r=-100.0+2.0*x+3.0*y+0.2*y*y+0.1*x*y+0.2*Math.sqrt(Math.abs(x));
  r+=(20.0*Math.sin(6.0*x*_PI)+20.0*Math.sin(2.0*x*_PI))*2.0/3.0;
  r+=(20.0*Math.sin(y*_PI)+40.0*Math.sin(y/3.0*_PI))*2.0/3.0;
  r+=(160.0*Math.sin(y/12.0*_PI)+320*Math.sin(y*_PI/30.0))*2.0/3.0; return r;
}
function _tlon(x,y){
  let r=300.0+x+2.0*y+0.1*x*x+0.1*x*y+0.1*Math.sqrt(Math.abs(x));
  r+=(20.0*Math.sin(6.0*x*_PI)+20.0*Math.sin(2.0*x*_PI))*2.0/3.0;
  r+=(20.0*Math.sin(x*_PI)+40.0*Math.sin(x/3.0*_PI))*2.0/3.0;
  r+=(150.0*Math.sin(x/12.0*_PI)+300.0*Math.sin(x/30.0*_PI))*2.0/3.0; return r;
}
function wgs2gcj(lon,lat){
  if(outOfChina(lon,lat)) return [lon,lat];
  let dLat=_tlat(lon-105.0,lat-35.0), dLon=_tlon(lon-105.0,lat-35.0);
  const rad=lat/180.0*_PI, mg=Math.sin(rad);
  const m2=1-_EE*mg*mg, sq=Math.sqrt(m2);
  dLat=(dLat*180.0)/((_A*(1-_EE))/(m2*sq)*_PI);
  dLon=(dLon*180.0)/(_A/sq*Math.cos(rad)*_PI);
  return [lon+dLon,lat+dLat];
}
function gcj2wgs(lon,lat){
  if(outOfChina(lon,lat)) return [lon,lat];
  let gl=lon, ga=lat;
  for(let i=0;i<3;i++){ const t=wgs2gcj(gl,ga); gl+=lon-t[0]; ga+=lat-t[1]; }
  return [gl,ga];
}
// Which tile source to use: street layer picks Amap (GCJ) inside China for
// detail, Esri (WGS) elsewhere for global coverage; sat is always Esri.
function pickSrc(){
  if(layer==='sat') return ['esri_sat',false];
  return outOfChina(view.lon,view.lat) ? ['esri_street',false] : ['amap_street',true];
}
function updateSrc(){
  const p=pickSrc();
  if(p[0]!==curSrc){ tileToken++; tileStat={load:0,fail:0}; }
  curSrc=p[0]; curGcj=p[1];
}
function needsShift(){ return curGcj; }
function toDisp(lon,lat){ return needsShift()? wgs2gcj(lon,lat) : [lon,lat]; }
function fromDisp(lon,lat){ return needsShift()? gcj2wgs(lon,lat) : [lon,lat]; }

// ---- Web Mercator ----
function mx(lon){ return (lon+180.0)/360.0; }
function my(lat){ const s=Math.sin(lat*_PI/180.0); return 0.5-Math.log((1+s)/(1-s))/(4*_PI); }
function mxInv(v){ return v*360.0-180.0; }
function myInv(v){ const n=_PI-2*_PI*v; return 180.0/_PI*Math.atan(0.5*(Math.exp(n)-Math.exp(-n))); }
function W(){ return CV_W||cv.clientWidth||1; }
function H(){ return CV_H||cv.clientHeight||1; }
function worldPx(){ return 256*Math.pow(2,view.z); }
function centerDisp(){ return toDisp(view.lon,view.lat); }

// ---- 视图变换：地图内容 -> 屏幕 ----
// 角度 mapRotDisp：地图内容绕屏幕中心顺时针旋转（北向上 0；机头朝上 -heading）
// 倾斜 mapTiltDisp：地图平面绕屏幕水平轴倾斜，纵向压缩 cos(φ)，模拟斜视视角
// 合成矩阵 M = S(1,cosφ)·R(θ) 是正交（仿射）变换，严格可逆 ——
// 因此鼠标取经纬度、瓦片网格对齐、图标定位全部精确，不存在透视带来的非线性偏移。
function curM(){                       // ctx.transform 参数 (a,b,c,d)
  const c=Math.cos(mapRotDisp), s=Math.sin(mapRotDisp), k=Math.cos(mapTiltDisp);
  return [c, s*k, -s, c*k];
}
function applyM(dx,dy){                // 施加 M
  if(!mapRotDisp && !mapTiltDisp) return [dx,dy];
  const c=Math.cos(mapRotDisp), s=Math.sin(mapRotDisp), k=Math.cos(mapTiltDisp);
  return [dx*c-dy*s, (dx*s+dy*c)*k];
}
function unapplyM(x,y){                // 撤销 M
  if(!mapRotDisp && !mapTiltDisp) return [x,y];
  const c=Math.cos(mapRotDisp), s=Math.sin(mapRotDisp);
  const k=Math.cos(mapTiltDisp)||1;
  return [x*c+y*s/k, -x*s+y*c/k];
}
function projRaw(lon,lat){                   // 相对屏幕中心、未变换的像素偏移
  const d=toDisp(lon,lat), c=centerDisp(), wpx=worldPx();
  return [ (mx(d[0])-mx(c[0]))*wpx, (my(d[1])-my(c[1]))*wpx ];
}
function proj(lon,lat){
  const r=projRaw(lon,lat), p=applyM(r[0],r[1]);
  return [p[0]+W()/2, p[1]+H()/2];
}
function unproj(x,y){
  const a=unapplyM(x-W()/2, y-H()/2);
  const c=centerDisp(), wpx=worldPx();
  const lo=mxInv( mx(c[0]) + a[0]/wpx );
  const la=myInv( my(c[1]) + a[1]/wpx );
  return fromDisp(lo,la);   // -> [lon, lat] in WGS-84
}
// 视口覆盖半径（世界像素）：旋转后要用半对角线，否则四角会露白；
// 倾斜后纵向可见范围放大 1/cosφ，半径须按 R=√((W/2)²+(H/2)²/cos²φ) 取，才能保证四角全覆盖。
// 未旋转未倾斜时仍用矩形半宽/半高，避免多请求瓦片。
function sweepRad(){
  const k=Math.cos(mapTiltDisp);
  const kk=k>0.25?k:0.25;
  const hw=W()/2, hh=H()/2;
  return Math.sqrt(hw*hw + hh*hh/(kk*kk));
}
function sweepRX(){
  if(!mapRotDisp && !mapTiltDisp) return W()/2;
  return sweepRad();
}
function sweepRY(){
  if(!mapRotDisp && !mapTiltDisp) return H()/2;
  return sweepRad();
}
function syncCanvasSize(){
  const w=cv.clientWidth, h=cv.clientHeight;
  if(!w||!h) return false;
  const dpr=Math.min(window.devicePixelRatio||1, 2);
  if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(h*dpr)){
    cv.width=Math.round(w*dpr); cv.height=Math.round(h*dpr);
  }
  DPR=dpr;
  CV_W=Math.max(1, Math.round(cv.width/dpr));
  CV_H=Math.max(1, Math.round(cv.height/dpr));
  return true;
}

// ---- raster tiles (street / satellite) ----
function tileKey(z,x,y){ return curSrc+'/'+z+'/'+x+'/'+y; }
let inflight=0, _tileTimer=null;
// When a tile finishes (success or failure) there may still be uncovered rows in
// the viewport, because each sweep requests at most MAXINFLIGHT tiles. Chain
// another sweep shortly after; it becomes a no-op once the view is fully covered.
let _fillTimer=null;
function kickFill(){
  if(_fillTimer) return;
  _fillTimer=setTimeout(()=>{ _fillTimer=null; refreshTilesNow(); },100);
}
function refreshTilesNow(){
  updateSrc();
  const tok=tileToken;
  const c=toDisp(view.lon,view.lat), wpx=worldPx();
  const cx=mx(c[0])*wpx, cy=my(c[1])*wpx;
  const rx=sweepRX(), ry=sweepRY();
  const X0=cx-rx, Y0=cy-ry;
  const tx0=Math.floor(X0/256), tx1=Math.ceil((X0+2*rx)/256)-1;
  const ty0=Math.floor(Y0/256), ty1=Math.ceil((Y0+2*ry)/256)-1;
  const n=1<<view.z;
  let requested=0;
  for(let ty=Math.max(0,ty0);ty<=Math.min(n-1,ty1);ty++){
    for(let tx=Math.max(0,tx0);tx<=Math.min(n-1,tx1);tx++){
      const key=tileKey(view.z,tx,ty);
      if(tileStore.has(key)){
        const rec=tileStore.get(key);
        if(rec.busy || rec.img) continue;
        // previous failure: remove and retry
        tileStore.delete(key);
      }
      if(inflight>=10){ kickFill(); continue; }
      requested++;
      inflight++;
      tileStore.set(key,{img:null,busy:true});
      Promise.resolve(call('tile',{z:view.z,x:tx,y:ty,src:curSrc})).then(r=>{
        inflight--;
        if(r&&r.ok&&r.data){
          const im=new Image();
          im.onload=()=>{ tileStore.set(key,{img:im,busy:false}); tileStat.load++; if(tileToken===tok) drawMap(); kickFill(); };
          im.onerror=()=>{ tileStore.delete(key); tileStat.fail++; kickFill(); };
          im.src=r.data;
        } else {
          tileStore.delete(key); tileStat.fail++;
        }
        kickFill();
      }).catch(()=>{ inflight--; tileStore.delete(key); tileStat.fail++; kickFill(); });
    }
  }
  updateTileInfo();
}
// ---- 周边预取：视口外圈一圈 + 下一级中心 3x3，用独立小并发在后台拉取， ----
// 让跟随/平移/放大时瓦片已经在缓存里，消除卡顿感。
let pfInflight=0, _pfTimer=null;
function schedulePrefetch(){
  if(_pfTimer) clearTimeout(_pfTimer);
  _pfTimer=setTimeout(()=>{ _pfTimer=null; prefetchAround(); },600);
}
function prefetchAround(){
  updateSrc();
  const c=toDisp(view.lon,view.lat), wpx=worldPx();
  const cx=mx(c[0])*wpx, cy=my(c[1])*wpx;
  const rx=sweepRX(), ry=sweepRY();
  const X0=cx-rx, Y0=cy-ry;
  const tx0=Math.floor(X0/256), tx1=Math.ceil((X0+2*rx)/256)-1;
  const ty0=Math.floor(Y0/256), ty1=Math.ceil((Y0+2*ry)/256)-1;
  const n=1<<view.z;
  const want=[];
  for(let ty=ty0-1;ty<=ty1+1;ty++){
    for(let tx=tx0-1;tx<=tx1+1;tx++){
      if(tx>=tx0&&tx<=tx1&&ty>=ty0&&ty<=ty1) continue;      // 视口内的不算预取
      if(tx<0||tx>=n||ty<0||ty>=n) continue;
      want.push([view.z,tx,ty]);
    }
  }
  const z2=view.z+1, n2=1<<z2;                               // 放大一级的中心 3x3
  if(z2<=19){
    const cx2=Math.floor(mx(c[0])*n2), cy2=Math.floor(my(c[1])*n2);
    for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++){
      const tx=cx2+dx, ty=cy2+dy;
      if(tx>=0&&tx<n2&&ty>=0&&ty<n2) want.push([z2,tx,ty]);
    }
  }
  for(const t of want){
    if(pfInflight>=3) break;
    const key=curSrc+'/'+t[0]+'/'+t[1]+'/'+t[2];
    if(tileStore.has(key)) continue;
    pfInflight++;
    const tok=tileToken;
    Promise.resolve(call('tile',{z:t[0],x:t[1],y:t[2],src:curSrc})).then(r=>{
      pfInflight--;
      if(r&&r.ok&&r.data){
        const im=new Image();
        im.onload=()=>{ tileStore.set(key,{img:im,busy:false}); };
        im.src=r.data;
      }
    }).catch(()=>{ pfInflight--; });
  }
}
// Coalesce bursts: dragging fires mousemove dozens of times per second and we
// don't want a network round-trip (or a stale tile sweep) for each one.
function refreshTiles(immediate){
  if(_tileTimer){ clearTimeout(_tileTimer); _tileTimer=null; }
  if(immediate) refreshTilesNow();
  else _tileTimer=setTimeout(()=>{ _tileTimer=null; refreshTilesNow(); }, 160);
}
// ---- 内置离线底图（z0~z5 卫星，随 EXE 分发） ----
// 低倍率下永远有一张本地卫星图可铺底：网络再慢也不会出现黑块。
// 存进 tileStore 的 'base/...' 键，与图层无关（街道图也拿它当底）。
let _baseBusy=0;
const _BASE_MAX_INFLIGHT=6;
function _baseEnsure(bk,z,tx,ty){
  if(tileStore.has(bk)) return;                  // 已请求过（含失败占位），不重复轰炸
  if(_baseBusy>=_BASE_MAX_INFLIGHT) return;
  _baseBusy++;
  tileStore.set(bk,{img:null,busy:true});
  Promise.resolve(call('tile',{z:z,x:tx,y:ty,base:true})).then(r=>{
    _baseBusy--;
    if(r&&r.ok&&r.data){
      const im=new Image();
      im.onload=()=>{ tileStore.set(bk,{img:im,busy:false}); scheduleDraw(); };
      im.onerror=()=>{ tileStore.set(bk,{img:null,busy:false}); };
      im.src=r.data;
    } else { tileStore.set(bk,{img:null,busy:false}); }
  }).catch(()=>{ _baseBusy--; tileStore.set(bk,{img:null,busy:false}); });
}
// 取某一级祖先瓦片的图像：z<=BASE_MAXZ 时优先用本地底图，其次才用同源缓存
function ancestorImg(z,tx,ty){
  if(z<=BASE_MAXZ){
    const bk='base/'+z+'/'+tx+'/'+ty;
    const b=tileStore.get(bk);
    if(b&&b.img) return b.img;
    if(!b) _baseEnsure(bk,z,tx,ty);
  }
  const rec=tileStore.get(tileKey(z,tx,ty));
  return (rec&&rec.img)?rec.img:null;
}
function drawTiles(){
  const c=toDisp(view.lon,view.lat), wpx=worldPx();
  const cx=mx(c[0])*wpx, cy=my(c[1])*wpx;
  const rx=sweepRX(), ry=sweepRY();   // 取瓦片范围用半对角线（旋转时四角也要覆盖）
  const W_=W(), H_=H();
  // ★绘制原点必须用画布半宽/半高：ctx 的旋转是绕 (W/2,H/2) 做的。
  //   若这里用 rx/ry（旋转时=半对角线），整幅影像会被推走 → 地图与飞机图标错位
  //   （692×430 画布、z14 下约 1.4 km）。
  const X0=cx-W_/2, Y0=cy-H_/2;
  const tx0=Math.floor((cx-rx)/256), tx1=Math.ceil((cx+rx)/256)-1;
  const ty0=Math.floor((cy-ry)/256), ty1=Math.ceil((cy+ry)/256)-1;
  const n=1<<view.z;
  // 旋转（机头朝上）/ 倾斜（选点后 45°）：整体绕屏幕中心施加正交变换。
  // 瓦片仍按未变换的网格计算，坐标变换交给 canvas，避免逐瓦片做投影运算。
  ctx.save();
  if(mapRotDisp||mapTiltDisp){
    const m=curM();
    ctx.translate(W_/2,H_/2);
    ctx.transform(m[0],m[1],m[2],m[3],0,0);
    ctx.translate(-W_/2,-H_/2);
  }
  // 由粗到细铺底：先从最浅的祖先层（低级别、覆盖整屏）开始，逐级向下叠到当前级别，
  // 细节层最后画、盖在最上面。这样即使当前级别瓦片还在下载（网速慢），也总有一张
  // 已缓存的浅层图放大兜底 —— 这是"选点突进"不黑屏的关键。
  const vx0=W_/2-rx, vy0=H_/2-ry, vx1=W_/2+rx, vy1=H_/2+ry;   // 视口（旋转时用半对角线）
  for(let dz=view.z-1; dz>=0; dz--){
    const z=view.z-dz;
    if(z<1) continue;
    const nn=1<<z, sc=Math.pow(2,dz);
    const nx0=Math.max(0,Math.floor(tx0/sc)), nx1=Math.min(nn-1,Math.floor(tx1/sc));
    const ny0=Math.max(0,Math.floor(ty0/sc)), ny1=Math.min(nn-1,Math.floor(ty1/sc));
    const size=256*sc;
    for(let ty=ny0;ty<=ny1;ty++){
      for(let tx=nx0;tx<=nx1;tx++){
        const img=ancestorImg(z,tx,ty);
        if(!img) continue;
        const px=(tx*size)-X0, py=(ty*size)-Y0;
        // 只画落在视口内的部分：极浅层（如 z1）放大到 z16 时整张贴图会达上千万像素，
        // 裁剪后开销与其他层级一致，避免拖慢动画。
        const sx=Math.max(0, vx0-px), sy=Math.max(0, vy0-py);
        const ex=Math.min(size, vx1-px), ey=Math.min(size, vy1-py);
        const sw=ex-sx, sh=ey-sy;
        if(sw<=0||sh<=0) continue;
        const k=256/size;
        ctx.drawImage(img, sx*k, sy*k, sw*k, sh*k, px+sx, py+sy, sw, sh);
      }
    }
  }
  ctx.restore();
}
function updateTileInfo(){
  const el=document.getElementById('tileInfo');
  el.textContent = TXT.tile_stat
    .replace('{load}',tileStat.load).replace('{fail}',tileStat.fail)
    .replace('{z}',view.z);
}

// ---- main draw ----
function drawMap(){
  if(!syncCanvasSize()) return;
  updateSrc();
  const w=W(), h=H();
  ctx.setTransform(DPR,0,0,DPR,0,0);
  ctx.clearRect(0,0,w,h);
  ctx.fillStyle='#07090d'; ctx.fillRect(0,0,w,h);
  drawTiles();

  // favorites
  for(let i=0;i<favs.length;i++){
    const f=favs[i], p=proj(f.lon,f.lat);
    if(p[0]<-40||p[0]>w+40||p[1]<-20||p[1]>h+20) continue;
    ctx.fillStyle=f.color||'#d9434e';
    ctx.beginPath(); ctx.arc(p[0],p[1],4,0,6.283); ctx.fill();
    ctx.strokeStyle='#fff'; ctx.lineWidth=1; ctx.stroke();
    if(f.name){
      ctx.font='10px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif';
      // 无背景色：白字 + 深色描边（光晕），在任何图层上都清晰且不突兀
      ctx.lineWidth=3; ctx.strokeStyle='rgba(10,12,16,.65)'; ctx.lineJoin='round';
      ctx.strokeText(f.name,p[0]+7,p[1]+1);
      ctx.fillStyle='#ffffff'; ctx.fillText(f.name,p[0]+7,p[1]+1);
    }
  }
  // target crosshair
  if(target){
    const p=proj(target[0],target[1]);
    ctx.strokeStyle='#d9434e'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.moveTo(p[0]-7,p[1]); ctx.lineTo(p[0]+7,p[1]);
    ctx.moveTo(p[0],p[1]-7); ctx.lineTo(p[0],p[1]+7); ctx.stroke();
    ctx.beginPath(); ctx.arc(p[0],p[1],9,0,6.283); ctx.stroke();
  }
  // aircraft — MSFS 雷达同款：白色剪影、后掠机翼、深色描边（机头朝上绘制）
  if(acft){
    const p=proj(acft.lon,acft.lat);
    ctx.save(); ctx.translate(p[0],p[1]);
    // 机头指向「地图空间中航向方向」在当前视图变换下的屏幕方向 —— 图标与地图严格联动：
    //   · 不用 upMode 判断（切朝向的 1.2~1.5s 里地图还在缓转，图标会瞬跳、看着与地图脱节）
    //   · 不能忽略倾斜（45° 时纵向压缩 0.71，屏幕角度会比地图上的实际指向偏出最多约 10°）
    //   · 环绕/倾斜动画期间 mapRotDisp/mapTiltDisp 每帧在变，这里跟着算，图标自然随地图转
    // 北向上·无倾斜·航向 h 时等价于 rotate(h)；机头朝上稳态等价于 rotate(0) —— 与旧行为一致。
    {
      const hr=(acft.heading||0)*Math.PI/180;
      const d=applyM(Math.sin(hr), -Math.cos(hr));
      ctx.rotate(Math.atan2(d[0], -d[1]));
    }
    ctx.beginPath();
    ctx.moveTo(0,-14);                              // 机头
    ctx.bezierCurveTo(1.9,-12.8, 2.2,-10, 2.2,-6);  // 前机身右缘
    ctx.lineTo(2.6,-4);
    ctx.lineTo(14,3);                               // 右翼前缘 → 翼尖
    ctx.lineTo(14,6.2);                             // 右翼尖弦长
    ctx.lineTo(2.6,2.5);                            // 翼后缘回到机身
    ctx.lineTo(2.0,8.5);                            // 后机身
    ctx.lineTo(7,11);                               // 右平尾
    ctx.lineTo(7,13.2);
    ctx.lineTo(1.2,11.5);
    ctx.lineTo(0,12.2);                             // 尾锥
    ctx.lineTo(-1.2,11.5);
    ctx.lineTo(-7,13.2);
    ctx.lineTo(-7,11);
    ctx.lineTo(-2.0,8.5);
    ctx.lineTo(-2.6,2.5);
    ctx.lineTo(-14,6.2);
    ctx.lineTo(-14,3);
    ctx.lineTo(-2.6,-4);
    ctx.lineTo(-2.2,-6);
    ctx.bezierCurveTo(-2.2,-10, -1.9,-12.8, 0,-14);
    ctx.closePath();
    ctx.fillStyle='#ffffff';
    ctx.strokeStyle='rgba(8,12,18,.8)';
    ctx.lineWidth=1.6; ctx.lineJoin='round';
    ctx.fill(); ctx.stroke();
    ctx.restore();
  }
  drawCompass();
  const lg=document.getElementById('legend');
  lg.textContent = 'z'+view.z+' · '+TXT['layer_'+layer]+(_legendNote ? ' · '+_legendNote : '');
}

// ---- 方位罗盘（地图左上角，半透明；机头朝上时随地图同步转动） ----
function drawCompass(){
  const cx=54, cy=54, R=34;
  // 指北向量 = 地图中的北(0,-1) 经当前视图变换后的方向（倾斜会让它略微偏转）
  const c0=Math.cos(mapRotDisp), s0=Math.sin(mapRotDisp);
  let ux=s0, uy=-c0*Math.cos(mapTiltDisp);
  const n0=Math.hypot(ux,uy)||1; ux/=n0; uy/=n0;
  const px_=-uy, py_=ux;                 // 垂直方向单位向量
  const a=Math.atan2(ux,-uy);            // 北方向在屏幕上的角度（顺时针）
  ctx.save();
  // 半透明底盘 + 外圈
  ctx.beginPath(); ctx.arc(cx,cy,R,0,6.283);
  ctx.fillStyle='rgba(13,18,26,.42)'; ctx.fill();
  ctx.lineWidth=1.4; ctx.strokeStyle='rgba(196,210,230,.45)'; ctx.stroke();
  ctx.beginPath(); ctx.arc(cx,cy,R-7,0,6.283);
  ctx.lineWidth=1; ctx.strokeStyle='rgba(196,210,230,.18)'; ctx.stroke();
  // 指北针（红）/ 指南针（浅）
  ctx.beginPath();
  ctx.moveTo(cx+ux*(R-13), cy+uy*(R-13));
  ctx.lineTo(cx+px_*4.6, cy+py_*4.6);
  ctx.lineTo(cx-px_*4.6, cy-py_*4.6);
  ctx.closePath();
  ctx.fillStyle='rgba(226,86,86,.92)'; ctx.fill();
  ctx.beginPath();
  ctx.moveTo(cx-ux*(R-13), cy-uy*(R-13));
  ctx.lineTo(cx+px_*4.6, cy+py_*4.6);
  ctx.lineTo(cx-px_*4.6, cy-py_*4.6);
  ctx.closePath();
  ctx.fillStyle='rgba(198,212,230,.42)'; ctx.fill();
  // NESW：位置随 mapRot 转动，字面保持正向以便阅读
  ctx.font='bold 11px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif';
  ctx.textAlign='center'; ctx.textBaseline='middle';
  const dirs=[['N',0],['E',90],['S',180],['W',270]];
  for(let i=0;i<dirs.length;i++){
    const ang=a+dirs[i][1]*Math.PI/180;
    const x=cx+Math.sin(ang)*(R-11), y=cy-Math.cos(ang)*(R-11);
    ctx.lineWidth=2.6; ctx.lineJoin='round'; ctx.strokeStyle='rgba(6,9,14,.85)';
    ctx.strokeText(dirs[i][0],x,y);
    ctx.fillStyle = dirs[i][0]==='N' ? '#ff8080' : 'rgba(228,238,250,.94)';
    ctx.fillText(dirs[i][0],x,y);
  }
  // 中心点
  ctx.beginPath(); ctx.arc(cx,cy,1.8,0,6.283);
  ctx.fillStyle='rgba(228,238,250,.75)'; ctx.fill();
  ctx.restore();
}

// ---- interaction ----
let drag=null;
let pendingDraw=false;
function scheduleDraw(){
  if(pendingDraw) return;
  pendingDraw=true;
  requestAnimationFrame(()=>{ pendingDraw=false; drawMap(); });
}
cv.addEventListener('mousedown',e=>{_stopAnim(); orbitStop(true); drag={x:e.offsetX,y:e.offsetY,lon:view.lon,lat:view.lat,moved:0,tok:tileToken};});
cv.addEventListener('mousemove',e=>{
  if(!drag) return;
  const wpx=worldPx(), dx=e.offsetX-drag.x, dy=e.offsetY-drag.y;
  drag.moved=Math.max(drag.moved, Math.abs(dx)+Math.abs(dy));
  if(drag.moved<3) return;
  const c=unproj(W()/2-dx, H()/2-dy);
  view.lon=c[0]; view.lat=c[1];
  drag.x=e.offsetX; drag.y=e.offsetY;
  scheduleDraw(); refreshTiles();
});
window.addEventListener('mouseup',e=>{
  if(!drag) return;
  if(drag.moved<4){
    const r=cv.getBoundingClientRect();
    const ll=unproj(e.clientX-r.left, e.clientY-r.top);
    setTarget(ll[1],ll[0]);
  } else {
    refreshTiles(true);   // drag finished: sweep the whole viewport at once
    schedulePrefetch();
  }
  drag=null;
});
cv.addEventListener('wheel',e=>{
  e.preventDefault();
  const r=cv.getBoundingClientRect();
  const mx_=e.clientX-r.left, my_=e.clientY-r.top;
  const before=unproj(mx_,my_);
  // Integer steps only: tiles are indexed by integer zoom levels, fractional
  // zoom breaks the tile math (this is exactly why the wheel seemed "dead").
  zoomAt(e.deltaY<0?1:-1, before, [mx_,my_]);
},{passive:false});
cv.addEventListener('dblclick',e=>{
  const r=cv.getBoundingClientRect();
  const mx_=e.clientX-r.left, my_=e.clientY-r.top;
  zoomAt(1, unproj(mx_,my_), [mx_,my_]);
});
function clampView(){
  view.lat=Math.max(-85,Math.min(85,view.lat));
  while(view.lon>180) view.lon-=360;
  while(view.lon<-180) view.lon+=360;
}
function zoomBy(d){ zoomAt(d,null,null); }
// 高度（英尺）→ 地图缩放级别：飞得低看得细，飞得高看得广
function zoomForAlt(altFt){
  // 输入为离地高度 AGL（英尺）。低空尽量放大（≤600ft → z18 最清晰），
  // 随离地高度升高逐级缩小。
  if(altFt==null||isNaN(altFt)) return null;
  if(altFt<=600)  return 18;
  if(altFt<=1200) return 17;
  if(altFt<=2400) return 16;
  if(altFt<=4000) return 15;
  if(altFt<=7000) return 14;
  if(altFt<=11000) return 13;
  if(altFt<=16000) return 12;
  if(altFt<=24000) return 11;
  if(altFt<=34000) return 10;
  if(altFt<=44000) return 9;
  return 8;
}
async function setAutoZoom(on){
  autoZoom=!!on;
  const el=document.getElementById('chkAutoZoom'); if(el) el.checked=autoZoom;
  clearMsg();   // 开关一拨就清掉上一条提示（例如“已手动缩放，退出高度联动”）
  if(!isApiReady()) return;
  const r=await safe('set_auto_zoom',{on:autoZoom});
  if(r && !r.ok) setMsg(TXT.js_call_error + ' set_auto_zoom','err');
}
function zoomAt(dz, anchorLL, anchorPx){
  _stopAnim();
  orbitStop(true);
  const nz=Math.max(2,Math.min(MAXZ[layer]||18, view.z+dz));
  if(nz===view.z) return;
  view.z=nz;
  // 手动缩放（+/−/滚轮/双击）即退出高度联动，避免两个“意志”打架
  if(autoZoom){ setAutoZoom(false); setMsg(tr('autozoom_off'),'err',8000); }
  if(anchorLL&&anchorPx){
    // keep the geo point under the cursor stationary
    for(let i=0;i<2;i++){
      const now=unproj(anchorPx[0],anchorPx[1]);
      const dl=now[0]-anchorLL[0], da=now[1]-anchorLL[1];
      view.lon+= (dl>180?dl-360:(dl<-180?dl+360:dl));
      view.lat+= da;
    }
  }
  clampView();
  tileToken++;
  drawMap(); refreshTiles();
  schedulePrefetch();
}
function setTarget(lat,lon){
  target=[lon,lat];
  document.getElementById('lat').value=lat.toFixed(6);
  document.getElementById('lon').value=lon.toFixed(6);
  document.getElementById('mapCoord').textContent=lat.toFixed(5)+', '+lon.toFixed(5);
  // if the picked point is off-screen (e.g. a search result), pan it into view
  const p=proj(lon,lat);
  if(p[0]<0||p[0]>W()||p[1]<0||p[1]>H()){ view.lon=lon; view.lat=lat; tileToken++; }
  drawMap(); refreshTiles();
}

// ---------- 选点定位：卫星图层 + 北向上 + "先总览平移，再突进" ----------
// 为什么这样做：z16 卫星瓦片体积大，网速慢时直接飞过去会一路黑屏。
// 改为三阶段，全程都有已缓存的浅层图放大兜底，不会黑屏：
//   阶段1 pan  ：在 z5 全球总览上把镜头平移到目标点（z5 全球仅 1024 张瓦片，秒开）
//   阶段2 wait ：等目标区域的 z16 瓦片下好（阶段1 已在并行预热），最多等 SEL_WAIT_MS
//   阶段3 zoom ：从 z5 平滑突进到 z16（此时瓦片已在缓存，放大过程连续）
// 全程约 4~6 秒 —— 与 MSFS 自由飞行选点后加载地景的节奏相当。
const SEL_LEVEL=16;                        // 目标细节级别
const SEL_OVERVIEW=5;                      // 总览级别
const SEL_MID=10;                          // 中间过渡级别（提前预热，突进时更清晰）
const SEL_PAN_MIN=900, SEL_PAN_MAX=2100;   // 平移动画时长（按距离自适应，毫秒）
const SEL_WAIT_MS=2600;                    // 等瓦片的上限
const SEL_ZOOM_MS=2600;                    // 突进时长
let _selLat=null, _selLon=null;            // 上一次选中的地点
let _anim=null;                            // 进行中的相机动画状态
let _legendNote='';                        // 地图左下角附加提示（移动中/载入目标区域…）
let _warmQ=[], _warmBusy=0;
const _WARM_MAX=4;                         // 预热并发上限（别把有限带宽全占死）

function _stopAnim(){
  if(_anim){ if(_anim.raf) cancelAnimationFrame(_anim.raf); _anim=null; }
  if(_legendNote){ _legendNote=''; scheduleDraw(); }
}
function _setViewZ(z){
  const nz=Math.max(2, Math.min(MAXZ[layer]||18, Math.round(z)));
  if(nz!==view.z){ view.z=nz; tileToken++; return true; }
  return false;
}
// 目标点周围 z 级瓦片就绪度 -> [已就绪, 总数]（决定"可以突进了吗"）
function _readyAt(z,lon,lat,rad){
  const d=toDisp(lon,lat), n=1<<z;
  const cx=Math.floor(mx(d[0])*n), cy=Math.floor(my(d[1])*n);
  let ok=0, total=0;
  for(let dy=-rad;dy<=rad;dy++)for(let dx=-rad;dx<=rad;dx++){
    const tx=cx+dx, ty=cy+dy;
    if(tx<0||ty<0||tx>=n||ty>=n) continue;
    total++;
    const rec=tileStore.get(tileKey(z,tx,ty));
    if(rec && rec.img) ok++;
  }
  return [ok,total];
}
// 预热队列：按"中心 -> 外圈"顺序后台下载，与平移动画并行跑
function _warmPump(){
  while(_warmBusy<_WARM_MAX && _warmQ.length){
    const t=_warmQ.shift(), key=t[0], z=t[1], tx=t[2], ty=t[3];
    if(tileStore.has(key)) continue;
    _warmBusy++;
    tileStore.set(key,{img:null,busy:true});
    Promise.resolve(call('tile',{z:z,x:tx,y:ty,src:curSrc})).then(r=>{
      _warmBusy--;
      if(r&&r.ok&&r.data){
        const im=new Image();
        im.onload=()=>{ tileStore.set(key,{img:im,busy:false}); scheduleDraw(); };
        im.onerror=()=>{ tileStore.delete(key); };
        im.src=r.data;
      } else { tileStore.delete(key); }
      _warmPump();
    }).catch(()=>{ _warmBusy--; tileStore.delete(key); _warmPump(); });
  }
}
function _warmArea(z,lon,lat,rad){
  const d=toDisp(lon,lat), n=1<<z;
  const cx=Math.floor(mx(d[0])*n), cy=Math.floor(my(d[1])*n);
  for(let r=0;r<=rad;r++){
    for(let dy=-r;dy<=r;dy++)for(let dx=-r;dx<=r;dx++){
      if(r>0 && Math.max(Math.abs(dx),Math.abs(dy))!==r) continue;   // 只取当前这一圈
      const tx=cx+dx, ty=cy+dy;
      if(tx<0||ty<0||tx>=n||ty>=n) continue;
      const key=curSrc+'/'+z+'/'+tx+'/'+ty;
      if(!tileStore.has(key)) _warmQ.push([key,z,tx,ty]);
    }
  }
  _warmPump();
}
function _animStep(){
  if(!_anim) return;
  const a=_anim, now=performance.now();
  if(a.phase==='pan'){
    let t=(now-a.start)/a.panMs; if(t>1) t=1;
    const e = t<0.5 ? 2*t*t : 1-Math.pow(-2*t+2,2)/2;                 // easeInOutQuad
    let dl=a.eLon-a.sLon; if(dl>180) dl-=360; if(dl<-180) dl+=360;    // 跨 180 度走最短路径
    view.lon=a.sLon+dl*e; view.lat=a.sLat+(a.eLat-a.sLat)*e;
    clampView();
    _legendNote=tr('sel_moving');
    scheduleDraw();
    if(!a._last || now-a._last>120){ a._last=now; refreshTiles(true); }  // 平移途中也要补瓦片
    if(t<1){ a.raf=requestAnimationFrame(_animStep); return; }
    a.phase='wait'; a.start=now;
    _animStep(); return;
  }
  if(a.phase==='wait'){
    // 等目标区域 z16 下好；中心 3x3 够用就走，最多等 SEL_WAIT_MS（慢网也不会卡死）
    const rd=_readyAt(SEL_LEVEL,a.eLon,a.eLat,1);
    if(rd[0]>=Math.min(rd[1],7) || now-a.start>=SEL_WAIT_MS){
      a.phase='zoom'; a.start=now; a.sZ=view.z;
      _animStep(); return;
    }
    _legendNote=tr('sel_loading')+' '+rd[0]+'/'+rd[1];
    scheduleDraw();
    a.raf=requestAnimationFrame(_animStep); return;
  }
  let t=(now-a.start)/SEL_ZOOM_MS; if(t>1) t=1;
  const e = t<0.5 ? 2*t*t : 1-Math.pow(-2*t+2,2)/2;
  view.lon=a.eLon; view.lat=a.eLat;
  clampView();
  _setViewZ(a.sZ + (SEL_LEVEL-a.sZ)*e);
  _legendNote='';
  scheduleDraw();
  if(!a._last || now-a._last>110){ a._last=now; refreshTiles(true); }
  if(t<1){ a.raf=requestAnimationFrame(_animStep); }
  else { _anim=null; refreshTiles(true); schedulePrefetch(); orbitStart(); }   // 落位后进入倾斜环绕展示
}
// 玩家点选某个地点后的统一入口：search / 收藏 选择都走这里
function selectLocation(lat,lon,name){
  if(typeof lat!=='number'||typeof lon!=='number'||isNaN(lat)||isNaN(lon)) return;
  // 1) 强制卫星图层 + 北向上（无论当前是何种图层/朝向）
  setLayer('sat'); setNorthUp();
  // 2) 取消"跟随飞机"，否则位置轮询会把镜头抢回飞机
  if(followOn){
    const el=document.getElementById('chkFollow');
    if(el) el.checked=false;
    setFollow(false);
  }
  // 3) 目标十字线 + 经纬度框
  target=[lon,lat];
  const la=document.getElementById('lat'), lo=document.getElementById('lon');
  if(la) la.value=lat.toFixed(6);
  if(lo) lo.value=lon.toFixed(6);
  const mc=document.getElementById('mapCoord');
  if(mc) mc.textContent=lat.toFixed(5)+', '+lon.toFixed(5);
  // 4) 相机飞行：先退到 z5 总览（在高倍率上平移必然黑屏），平移途中并行预热目标瓦片，
  //    下好后再突进到位。这一步彻底解决"慢网下选点一片黑"的问题。
  _selLat=lat; _selLon=lon;
  _stopAnim();
  orbitStop(true);                            // 上一次的环绕立即收掉，飞行期间保持正视角
  const sLon=view.lon, sLat=view.lat;
  if(view.z>SEL_OVERVIEW) _setViewZ(SEL_OVERVIEW);
  clampView(); drawMap(); refreshTiles(true);
  _warmArea(SEL_LEVEL,lon,lat,2);            // 目标中心 5x5=25 张，中心优先
  _warmArea(SEL_MID,  lon,lat,1);            // 过渡层 3x3，突进过程更清晰
  const dd=Math.hypot(lon-sLon, lat-sLat);   // 越远平移越久，但有上下限
  const panMs=Math.round(SEL_PAN_MIN+(SEL_PAN_MAX-SEL_PAN_MIN)*Math.min(1,dd/150));
  _anim={ raf:0, phase:'pan', start:performance.now(), panMs:panMs,
          sLon:sLon, sLat:sLat, sZ:view.z, eLon:lon, eLat:lat, _last:0 };
  _anim.raf=requestAnimationFrame(_animStep);
}
function setLayer(l){
  orbitStop(false);
  layer=l; tileToken++; tileStat={load:0,fail:0};
  ['Street','Sat'].forEach(k=>{
    const el=document.getElementById('seg'+k);
    if(el) el.className='seg'+(l===k.toLowerCase()?' on':'');
  });
  if(view.z>(MAXZ[layer]||18)) view.z=MAXZ[layer]||18;
  // 切换到某图层时，若“缩放随高度”开启，立即按当前飞行高度把缩放级别同步到位，
  // 避免“切到卫星图后高度联动看起来没生效”的错觉。
  if(autoZoom && acft){
    let tz=zoomForAlt(acft.agl_ft ?? acft.alt_ft);   // 优先用离地高度(AGL)，保证高原/平原缩放一致
    if(tz!=null){ const mx=MAXZ[layer]||18; if(tz>mx)tz=mx; if(tz<2)tz=2; if(tz!==view.z) view.z=tz; }
  }
  clampView(); drawMap(); refreshTiles();
}
function locateAircraft(){
  if(!acft){setMsg(TXT.map_no_fix,'err');return;}
  clearMsg();          // 有定位了就清掉上一次“暂无飞机位置”
  _stopAnim();
  orbitStop(false);
  view.lon=acft.lon; view.lat=acft.lat;
  view.z=Math.max(view.z, layer==='street'?12:10);
  tileToken++; drawMap(); refreshTiles();
}
// 平滑（dur>0）或瞬时（dur 省略）把镜头移到指定位置
function flyTo(lat,lon,hdg,dur){
  acft={lat:lat,lon:lon,heading:hdg||0};
  if(dur>0){
    _panAnim={start:performance.now(), dur:dur, sLon:view.lon, sLat:view.lat, eLon:lon, eLat:lat};
    uiKick();                                   // 交给共用动画循环（uiTick）驱动
  } else {
    _panAnim=null;
    view.lon=lon; view.lat=lat;
  }
  tileToken++; refreshTiles();
  schedulePrefetch();
  scheduleDraw();
  const el=document.getElementById('acLine');
  if(el) el.textContent='✈ '+lat.toFixed(4)+', '+lon.toFixed(4)+' · '+Math.round(hdg||0)+'°';
}
async function setOnTop(on){
  clearMsg();
  if(!isApiReady()) return;
  const r=await safe('set_on_top',{on:on});
  if(r && !r.ok) setMsg(TXT.js_call_error + ' set_on_top','err');
}
async function setFollow(on){
  clearMsg();
  followOn=!!on;
  if(on){
    orbitStop(false);                                     // 跟随飞机时退出环绕展示
    if(acft) flyTo(acft.lat, acft.lon, acft.heading||0, 800);
  }
  if(!isApiReady()) return;
  const r=await safe('set_follow',{on:on});
  if(r && !r.ok) setMsg(TXT.js_call_error + ' set_follow','err');
}
// ---- 地图朝向：北向上 / 机头朝上 ----
// 逻辑目标角：机头朝上时随航向实时变化，北向上恒为 0
function rotTargetNow(){
  const h=(acft && typeof acft.heading==='number') ? acft.heading : 0;
  return (upMode==='heading') ? (-h*Math.PI/180) : 0;
}
function updateRotation(){
  mapRot=rotTargetNow();
  // 无动画时实时跟随（机头朝上转弯时地图要连续跟手）；动画期间由动画驱动赋值
  if(!_animRot && !_orbit){ mapRotDisp=mapRot; mapTiltDisp=_tiltGoal; }
}
// ---- 朝向过渡 / 倾斜 / 环绕：共用一条 rAF 循环 ----
function angDiff(from,to){                     // 最短路径角度差
  let d=(to-from)%(2*Math.PI);
  if(d>Math.PI) d-=2*Math.PI;
  if(d<-Math.PI) d+=2*Math.PI;
  return d;
}
function uiTick(){
  _uiRaf=0;
  const now=performance.now();
  let need=false;
  if(_animRot){
    const a=_animRot;
    let t=(now-a.start)/(a.dur||1); if(t>1) t=1;
    const e = t<0.5 ? 2*t*t : 1-Math.pow(-2*t+2,2)/2;          // easeInOutQuad
    const tr=rotTargetNow();
    mapRotDisp=a.fromRot+angDiff(a.fromRot,tr)*e;
    mapTiltDisp=a.fromTilt+(_tiltGoal-a.fromTilt)*e;
    need=true;
    if(t>=1){
      mapRotDisp=tr; mapTiltDisp=_tiltGoal; _animRot=null;
      if(a.done) a.done();
    }
  } else if(_orbit){
    const dt=_orbit.last ? Math.min(0.1,(now-_orbit.last)/1000) : 0;
    _orbit.last=now;
    mapRotDisp-=ORBIT_SPEED*dt;                // 逆时针缓慢环绕
    mapTiltDisp=_tiltGoal;
    need=true;
  }
  if(_panAnim){
    const p=_panAnim;
    let t=(now-p.start)/p.dur; if(t>1) t=1;
    const e = t<0.5 ? 2*t*t : 1-Math.pow(-2*t+2,2)/2;
    let dl=p.eLon-p.sLon; if(dl>180) dl-=360; if(dl<-180) dl+=360;
    view.lon=p.sLon+dl*e; view.lat=p.sLat+(p.eLat-p.sLat)*e;
    clampView(); need=true;
    if(t>=1){ _panAnim=null; refreshTiles(true); schedulePrefetch(); }
  }
  if(need){
    scheduleDraw();
    if(_animRot||_orbit||_panAnim) _uiRaf=requestAnimationFrame(uiTick);
  }
}
function uiKick(){ if(!_uiRaf) _uiRaf=requestAnimationFrame(uiTick); }
// 切换朝向：从当前显示角走最短路径到新目标角
function switchRot(dur){
  _animRot={start:performance.now(), dur:dur||1500, fromRot:mapRotDisp, fromTilt:mapTiltDisp, done:null};
  uiKick();
}
// 选点定位结束后：缓升 45° 倾斜，随后持续逆时针环绕（直到用户任何操作）
function orbitStart(){
  if(upMode!=='north') return;
  _orbit=null;
  _tiltGoal=TILT_MAX;
  _animRot={start:performance.now(), dur:1400, fromRot:mapRotDisp, fromTilt:mapTiltDisp,
            done:()=>{ _orbit={last:0}; refreshTiles(true); }};
  uiKick();
}
// 退出环绕：instant=true 立即复位（拖拽/缩放要跟手），否则 0.5 秒平滑复位
function orbitStop(instant){
  if(!_orbit && !_tiltGoal) return;
  _orbit=null; _tiltGoal=0;
  if(instant){
    _animRot=null;
    mapTiltDisp=0; mapRotDisp=rotTargetNow();
    scheduleDraw();
  } else {
    _animRot={start:performance.now(), dur:520, fromRot:mapRotDisp, fromTilt:mapTiltDisp, done:null};
  }
  uiKick();
}
function refreshUpBtn(){
  const btn=document.getElementById('btnUp');
  const ico=document.getElementById('upIco');
  const txt=document.getElementById('upTxt');
  const on=(upMode==='heading');
  if(btn) btn.className='upbtn'+(on?' on':'');
  if(ico) ico.textContent = on?'HDG':'N';
  if(txt) txt.textContent = on?(TXT.opt_headup||'机头朝上'):(TXT.opt_northup||'北向上');
}
async function toggleUpMode(){
  upMode = (upMode==='heading') ? 'north' : 'heading';
  applyUpMode();
  if(isApiReady()) safe('set_up_mode',{mode:upMode});
}
function applyUpMode(){
  refreshUpBtn();
  orbitStop(false);
  // 机头朝上：地图随航向旋转，飞机必须居中才有意义，自动打开“跟随飞机”
  if(upMode==='heading' && !followOn){
    const el=document.getElementById('chkFollow');
    if(el) el.checked=true;
    setFollow(true);
  }
  switchRot(1500);          // 先记录当前显示角作为动画起点，再更新目标角
  updateRotation();
  tileToken++;
  refreshTiles(); schedulePrefetch();
}
// 强制北向上（选点定位时调用）：不触发"机头朝上自动跟随"那套逻辑
function setNorthUp(){
  if(upMode==='north') return;
  upMode='north';
  orbitStop(false);
  switchRot(1200);
  updateRotation();
  tileToken++;
  refreshUpBtn();
  if(isApiReady()) safe('set_up_mode',{mode:'north'});
  refreshTiles(); schedulePrefetch();
}
function toggleMapOnly(maponly){
  orbitStop(true);                     // 画布尺寸要变，先收起倾斜环绕
  document.querySelector('.app').classList.toggle('maponly', maponly);
  // 切换地图栏内按钮显隐：仅地图时显示"显示面板"，否则显示"仅地图"
  const btnMO=document.getElementById('btnMapOnly');
  const btnSP=document.getElementById('btnShowPanel');
  if(btnMO) btnMO.style.display=maponly?'none':'';
  if(btnSP) btnSP.style.display=maponly?'':'none';
  // 仅地图默认 75%；若用户拖过滑杆则以滑杆为准
  const os=document.getElementById('opSlider');
  if(maponly && !opTouched) os.value=75;
  else if(!maponly) os.value=100;   // 显示面板时强制 100%，便于看清操作
  applyOpacity(+os.value);
  // 仅地图：窗口缩到最小尺寸并挪到屏幕右上角（用户之后仍可手动拖动/缩放，尺寸会被记住）
  if(isApiReady()) safe('map_only_geometry',{on:!!maponly});
  setTimeout(()=>{ syncCanvasSize(); drawMap(); refreshTiles(); schedulePrefetch(); }, 60);
}
// 窗口不透明度滑杆（30–100%），位于地图底部居中
const os=document.getElementById('opSlider');
let opTouched=false;
os.addEventListener('input',()=>{ opTouched=true; applyOpacity(+os.value); });
function applyOpacity(pct){
  const v=document.getElementById('opVal'); if(v) v.textContent=pct+'%';
  return (isApiReady() ? safe('set_window_opacity',{pct:pct}) : null);
}
async function initViewOpts(){
  if(!isApiReady()) return;
  try{
    const top=await safe('get_on_top');
    if(top && typeof top.on==='boolean') document.getElementById('chkTop').checked=top.on;
    const flw=await safe('get_follow');
    if(flw && typeof flw.on==='boolean'){
      document.getElementById('chkFollow').checked=flw.on;
      followOn=flw.on;
    }
    const az=await safe('get_auto_zoom');
    if(az && typeof az.on==='boolean'){
      autoZoom=az.on;
      document.getElementById('chkAutoZoom').checked=autoZoom;
    }
    const op=await safe('get_opacity');
    if(op && op.ok && typeof op.pct==='number'){
      os.value=op.pct;
      const v=document.getElementById('opVal'); if(v) v.textContent=op.pct+'%';
      if(op.pct<100) opTouched=true;
    }
    const um=await safe('get_up_mode');
    if(um && (um.mode==='north'||um.mode==='heading')){
      upMode=um.mode;
      refreshUpBtn();
      // 机头朝上必须保持飞机居中，否则飞机会飞出视野
      if(upMode==='heading' && !followOn){
        document.getElementById('chkFollow').checked=true;
        followOn=true;
        safe('set_follow',{on:true});
      }
      updateRotation();
      drawMap();
    }
  }catch(e){}
}
refreshUpBtn();
window.addEventListener('resize',()=>{ syncCanvasSize(); drawMap(); refreshTiles(); });
if(window.ResizeObserver){
  new ResizeObserver(()=>{ syncCanvasSize(); drawMap(); refreshTiles(); })
    .observe(document.getElementById('mapwrap'));
}
setInterval(()=>{ syncTargetFromInputs(); drawMap(); },1500);
// 飞机位置轮询：图标每 1.2 秒更新；跟随模式下飞机始终锁定在地图中央；
// “缩放随高度”开启时，按飞行高度自动调整缩放级别（拖动地图时暂停让位）。
// （正在拖动地图时暂停让位，松手后下一轮自动恢复居中）。
setInterval(async ()=>{
  if(!isApiReady()) return;
  const p=await safe('position');
  if(p && typeof p.lat==='number'){
    acft={lat:p.lat,lon:p.lon,heading:p.heading||0};
    let changed=false;
    // 自愈：平移动画若因异常没跑完，别让 _panAnim 永久卡住下面的居中判断（否则"跟随飞机"失效）
    if(_panAnim && performance.now()>_panAnim.start+_panAnim.dur+600) _panAnim=null;
    // 启动后第一次拿到有效位置：无论“跟随飞机”是否勾选，都先把镜头定位到飞机（居中 +
    // 至少到定位级缩放），保证一打开程序就能看到飞机。只做一次；不改变用户的勾选状态
    // （跟随关掉时，之后仍可自由拖动地图）。
    // 若首个位置到达时用户正在拖动/选点飞行，则让位给用户操作。
    if(_bootLocate){
      _bootLocate=false;
      if(!drag && !favAsk && !_anim && !_panAnim){
        view.lat=p.lat; view.lon=p.lon;
        view.z=Math.max(view.z, layer==='street'?12:10);   // 与「定位飞机」按钮的后备缩放一致
        changed=true;
      }
    }
    if(!drag && !favAsk && !_anim && !_panAnim){   // 相机飞行/平滑平移中让位，别抢镜头
      if(followOn){
        const moved=Math.abs(view.lat-p.lat)>1e-9||Math.abs(view.lon-p.lon)>1e-9;
        view.lat=p.lat; view.lon=p.lon;
        changed=changed||moved;
      }
      if(autoZoom){
        let tz=zoomForAlt(p.agl_ft ?? p.alt_ft);   // 优先用离地高度(AGL)，保证高原/平原缩放一致
        if(tz){
          // 不同图层的最大缩放级别不同（地形仅到 z13），超出会导致瓦片请求失败/抖动，
          // 必须把高度联动结果限制在当前图层允许范围内。
          const mx=MAXZ[layer]||18;
          if(tz>mx) tz=mx;
          if(tz<2) tz=2;
          if(tz!==view.z){ view.z=tz; changed=true; }
        }
      }
      // 根据玩家当前飞行器类型自动设定空速档位：仅在机型变化时更新，
      // 避免频繁覆盖用户手动设定（战斗机→军机档，滑翔机→滑翔机档，以此类推）。
      if(p.acCat && AIR_SPEEDS[p.acCat]!=null && p.acCat!==lastAcCat){
        lastAcCat=p.acCat;
        try{
          const sel=document.getElementById('acType');
          if(sel && sel.value!==p.acCat) sel.value=p.acCat;
          const spd=document.getElementById('spd');
          if(spd) spd.value=AIR_SPEEDS[p.acCat];
        }catch(e){}
      }
    }
    if(changed){ tileToken++; refreshTiles(); schedulePrefetch(); }
    // 机头朝上模式下航向变化只改旋转角 —— 只需重绘，无需重新拉瓦片
    updateRotation();
    drawMap();
  }
},1200);
function syncTargetFromInputs(){
  const la=parseFloat(document.getElementById('lat').value);
  const lo=parseFloat(document.getElementById('lon').value);
  if(!isNaN(la)&&!isNaN(lo)){
    if(!target||Math.abs(target[0]-lo)>1e-6||Math.abs(target[1]-la)>1e-6){ target=[lo,la]; }
    document.getElementById('mapCoord').textContent=la.toFixed(5)+', '+lo.toFixed(5);
  }
}

// ---------------- presets（已按需求移除预设按钮） ----------------
let altUnit='m';
function onUnit(){
  const v=parseFloat(document.getElementById('alt').value)||0;
  const u=document.getElementById('altUnit').value;
  if(u!==altUnit){ document.getElementById('alt').value = u==='ft'? Math.round(v/0.3048) : Math.round(v*0.3048); }
  altUnit=u;
}
function onAcType(){
  const v=document.getElementById('acType').value;
  if(v!=='custom'){ document.getElementById('spd').value=AIR_SPEEDS[v]||120; }
}
const AIR_SPEEDS={airliner:450,ga:120,heli:90,glider:60,bush:110,warplane:350};

// ---------------- favorites ----------------
async function loadFav(){
  if(!isApiReady()) return;
  const r=await safe('favorites');
  favs=(r&&r.items)||[]; renderFav(); drawMap();
}
function renderFav(){
  // 收藏改为下拉框：只刷新占位项之后的选项，尽量保持当前选中项
  const sel=document.getElementById('favSelect');
  if(!sel) return;
  const cur=sel.value;
  while(sel.options.length>1) sel.remove(1);
  favs.forEach(f=>{
    const o=document.createElement('option');
    o.value=f.id;
    o.textContent=f.name;
    sel.appendChild(o);
  });
  sel.value=favs.some(f=>String(f.id)===String(cur))?cur:'';
}
function onFavSelect(id){
  const f=favs.find(x=>String(x.id)===String(id));
  if(f) peekFav(f);
}
async function delSelFav(){
  const sel=document.getElementById('favSelect');
  if(!sel||!sel.value) return;
  const r=await safe('del_favorite',{id:sel.value});
  if(r&&r.items){ favs=r.items; renderFav(); drawMap(); }
}
// 点击收藏点：地图居中预览 5 秒并弹确认；「是」瞬移，「否」/超时回到飞机当前位置。
function peekFav(f){
  if(favAsk) favAskAnswer(false);
  favAsk={fav:f, t0:Date.now()};
  // 选点定位：卫星图层 + 北向上 + 三阶段飞行（z5 平移 -> 预热 -> z16 突进，与搜索选点一致）
  selectLocation(f.lat,f.lon,f.name);
  const nm=f.name||(''+f.lat.toFixed(4)+', '+f.lon.toFixed(4));
  document.getElementById('favAskText').textContent=
    (TXT.fav_ask||'是否前往「{name}」上空？').split('{name}').join(nm);
  document.getElementById('favAskBar').style.width='100%';
  document.getElementById('favAsk').style.display='flex';
  favAsk.timer=setInterval(()=>{
    if(!favAsk){ clearInterval(favAsk.timer); return; }
    const left=Math.max(0,5000-(Date.now()-favAsk.t0));
    document.getElementById('favAskBar').style.width=(left/50)+'%';
    if(left<=0) favAskAnswer(false);
  },100);
}
function favAskAnswer(yes){
  if(!favAsk) return;
  if(favAsk.timer) clearInterval(favAsk.timer);
  document.getElementById('favAsk').style.display='none';
  const fa=favAsk; favAsk=null;
  if(yes){
    document.getElementById('lat').value=fa.fav.lat.toFixed(6);
    document.getElementById('lon').value=fa.fav.lon.toFixed(6);
    syncTargetFromInputs();
    teleportCurrent();
  }
  // 「否」/超时：不做任何视图跳转 —— 收藏点保持居中，直到用户手动拖动地图
}
async function addFav(){
  if(!isApiReady()){setMsg(tr('js_not_ready_wait'),'err');return;}
  const la=parseFloat(document.getElementById('lat').value);
  const lo=parseFloat(document.getElementById('lon').value);
  if(isNaN(la)||isNaN(lo)){setMsg(tr('fav_need_coord'),'err');return;}
  const r=await safe('add_favorite',{lat:la,lon:lo,name:document.getElementById('favName').value});
  if(r&&r.items){ favs=r.items; renderFav(); drawMap(); document.getElementById('favName').value=''; setMsg('',''); }
}

// ---------------- core actions ----------------
function buildPayload(){
  const lat=parseFloat(document.getElementById('lat').value);
  const lon=parseFloat(document.getElementById('lon').value);
  if(isNaN(lat)||isNaN(lon)) return null;
  let alt=parseFloat(document.getElementById('alt').value)||0;
  if(altUnit==='ft') alt=alt*0.3048;
  return {
    lat:lat, lon:lon, alt:alt,
    heading:parseFloat(document.getElementById('hdg').value)||90,
    speed:parseFloat(document.getElementById('spd').value)||120
  };
}

async function armHotkey(){
  if(!isApiReady()){setMsg(tr('js_not_ready_wait'),'err');return;}
  const payload=buildPayload();
  if(!payload){setMsg(tr('js_need_latlon'),'err');return;}
  const r=await safe('arm',payload);
  if(r.ok) clearMsg(); else setMsg(r.msg||'', 'err');
}

function unpause(){
  if(!isApiReady()){setMsg(tr('js_not_ready_wait'),'err');return;}
  setMsg(tr('js_unpausing'),'ok');
  setTimeout(async ()=>{
    const r=await safe('unpause');
    setMsg(r.msg||'', r.ok?'ok':'err');
  }, 50);
}

function connect(){
  if(!isApiReady()){setMsg(tr('js_not_ready_wait'),'err');return false;}
  setStat(false,tr('connecting'));
  setMsg(tr('js_connecting'),'ok');
  setTimeout(async ()=>{
    const r=await safe('connect');
    setStat(!!r.ok, r.ok?TXT.connected:TXT.not_connected);
    if(r.ok){ setMsg(r.msg||'', 'ok'); }
    else    { setMsg(errWithHint(r.msg, diagHint(r.diag)), 'err'); }
  }, 50);
  return false;
}

let _searchSeq=0, _qTimer=null;
function renderCands(list){
  const box=document.getElementById('cands'); box.innerHTML='';
  (list||[]).forEach(it=>{
    const d=document.createElement('div'); d.className='item';
    d.innerHTML='<span class="nm">'+it.name+'</span>';
    d.onclick=()=>{
      // 搜索选点：卫星图层 + 北向上 + 三阶段飞行（z5 平移 -> 预热 -> z16 突进）
      selectLocation(it.lat,it.lon,it.name);
      setMsg(tr('js_chosen',{name:it.name}),'ok');
    };
    box.appendChild(d);
  });
  return box.children.length;
}
// 输入时防抖预热：先把"词典 + 最快源"的结果灌进后端缓存，回车时几乎瞬间出候选
function onQueryInput(){
  const q=(document.getElementById('q').value||'').trim();
  if(_qTimer) clearTimeout(_qTimer);
  if(q.length<2) return;
  _qTimer=setTimeout(()=>{ if(isApiReady()) safe('geocode_fast', q); },500);
}
async function geocode(){
  const q=document.getElementById('q').value;
  const seq=++_searchSeq;
  // 第一阶段：离线词典 + 最快源（通常 1 秒内）先把候选画出来
  const rf=await safe('geocode_fast', q);
  if(seq!==_searchSeq) return;
  if(rf&&rf.ok&&rf.results&&rf.results.length){
    renderCands(rf.results);
    setMsg(tr('js_candidates',{n:rf.results.length}),'ok');
  } else {
    document.getElementById('cands').innerHTML='';
    setMsg(tr('js_searching'),'ok');
  }
  // 第二阶段：全部源并行合并排序（更准），完成后直接覆盖
  const r=await safe('geocode', q);
  if(seq!==_searchSeq) return;
  if(!r||!r.ok){
    if(!document.getElementById('cands').children.length)
      setMsg((r&&r.msg)||tr('js_search_fail'),'err');
    return;
  }
  renderCands(r.results);
  setMsg(tr('js_candidates',{n:r.results.length}),'ok');
}

function teleportCurrent(){
  if(!isApiReady()){setMsg(tr('js_not_ready_wait'),'err');return;}
  const payload=buildPayload();
  if(!payload){setMsg(tr('js_need_latlon'),'err');return;}
  setStat(false,tr('connecting'));
  setMsg(tr('js_prepare'),'ok');
  setTimeout(async ()=>{
    let st=await safe('status');
    if(!(st&&st.connected)){
      const c=await safe('connect');
      setStat(!!c.ok, c.ok?TXT.connected:TXT.not_connected);
      if(!c.ok){ setMsg(errWithHint(c.msg||tr('js_connect_failed'), diagHint(c.diag)),'err'); return; }
    } else { setStat(true); }
    setMsg(tr('js_teleporting'),'ok');
    const r=await safe('teleport',payload);
    setMsg(r.msg, r.ok?'ok':'err');
  }, 50);
}

// 连接失败时的红色文字提示：从诊断数据里挑出**最可操作的一条**，不再显示诊断面板。
function diagHint(d){
  const g=d||{};
  if(!g.dll_found) return TXT.diag_hint_no_dll;
  if(!g.pkg_dll_ok) return TXT.diag_hint_copy;
  if(g.msfs_running===false) return TXT.hint_fix_msfs;
  if(g.diag_error) return TXT.lbl_diag_error+g.diag_error;
  return '';
}
// 把基础消息与建议合成一条红色提示（建议可能为空）
function errWithHint(base,hint){ return hint ? (base ? base+' — '+hint : hint) : (base||''); }

drawMap();
// The first connection is triggered by the readiness poll (see readyTimer)
</script>
</body>
</html>
"""


def build_html():
    """渲染当前语言的 HTML：占位符 + JS 文案表。

    世界地图数据不再内嵌（WebView2 NavigateToString 2MB 上限会白屏），
    改为前端就绪后调用 Api.world() 异步拉取。
    """
    html = render(HTML_TEMPLATE)
    html = html.replace("__JS_TXT__", js_strings())
    html = html.replace("{{side_w}}", str(_load_side_width()))
    return html


# 启动语言：优先跟随 MSFS2024 当前界面语言（zh-* -> 中文，其他 -> 英文）。
# 读不到 MSFS 语言（未安装/找不到配置文件）时，才沿用上次在界面里手动选择的语言（ui.json）。
# 界面里的语言彩蛋仍可在本次会话内切换，并写入 ui.json。
try:
    _msfs_lang = strings._msfs_lang()
    if _msfs_lang:
        strings.set_lang(_msfs_lang)
        logger.info("startup language from MSFS UI language: %s", _msfs_lang)
    else:
        _saved_lang = _read_ui().get("lang")
        if _saved_lang in ("zh", "en"):
            strings.set_lang(_saved_lang)
            logger.info("startup language from ui.json (MSFS lang undetected): %s",
                        _saved_lang)
except Exception:  # noqa: BLE001
    pass

HTML = build_html()


def _bring_self_front():
    """把本程序窗口调到最前台；最小化时先还原。"""
    if WIN is None:
        return False
    try:
        # 先走 pywebview 公开 API 还原（非最小化时调用无副作用）
        WIN.restore()
    except Exception as e:  # noqa: BLE001
        logger.debug("bring_self_front pywebview call failed: %s", e)

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 统一走 _find_self_hwnd()：它同时认中/英标题并带兜底，
        # 不会因界面语言切换后标题变了而找不到自己的窗口。
        hwnd = _find_self_hwnd()
        if not hwnd:
            logger.warning("bring_self_front: own window not found")
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        if fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        logger.info("bring_self_front: done")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("bring_self_front failed: %s", e)
        return False


def _start_bringfront_hotkey():
    """注册系统全局热键 Ctrl+Alt+G，飞行中按一下把本窗口调到前台。"""
    def _loop():
        user32 = ctypes.windll.user32
        MOD_CONTROL = 0x0002
        MOD_ALT = 0x0001
        WM_HOTKEY = 0x0312
        HOTKEY_ID = 1
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, VK_G):
            logger.warning("RegisterHotKey failed; Ctrl+Alt+G bring-to-front disabled")
            return
        logger.info("registered global hotkey Ctrl+Alt+G (bring-to-front)")

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hWnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", wintypes.POINT),
            ]

        msg = MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                logger.info("hotkey Ctrl+Alt+G pressed -> bring to front")
                _bring_self_front()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=_loop, daemon=True).start()


def _screen_limit(size):
    """把窗口限制在屏幕可用区域内，避免大尺寸档位超出屏幕。"""
    try:
        user32 = ctypes.windll.user32
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
        sw = user32.GetSystemMetrics(0)   # SM_CXSCREEN
        sh = user32.GetSystemMetrics(1)
        avail_h = max(420, int(sh * 0.94))  # leave room for the taskbar
        return (
            max(360, min(size[0], int(sw * 0.92))),
            max(420, min(size[1], avail_h)),
        )
    except Exception:  # noqa: BLE001
        return size


def main():
    global WIN
    logger.info("app starting; lang=%s", LANG)
    api = Api()
    w, h = _screen_limit(_load_win_size())
    logger.info("window size: %sx%s", w, h)

    # 启动位置：主显示器工作区正中间（按当前显示器的真实分辨率与 DPI 缩放计算，
    # 换一台不同分辨率/缩放比例的电脑同样适用）
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass
    try:
        area = _monitor_work_area(None)
        left, top, right, bottom = area["work"]
    except Exception:  # noqa: BLE001
        user32 = ctypes.windll.user32
        left, top = 0, 0
        right, bottom = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    avail_w, avail_h = right - left, bottom - top
    w = int(min(w, avail_w))
    h = int(min(h, avail_h))
    x = int(left + max(0, (avail_w - w) // 2))
    y = int(top + max(0, (avail_h - h) // 2))
    logger.info("screen work area %.0fx%.0f -> window %sx%s @(%s,%s)",
                avail_w, avail_h, w, h, x, y)
    ontop = _load_ontop()

    win = webview.create_window(
        t("app_title"),
        html=HTML,
        js_api=api,
        width=w,
        height=h,
        x=x,
        y=y,
        resizable=True,
        min_size=WIN_MIN_RESIZE,
        on_top=ontop,
    )
    WIN = win
    _attach_resize_persist(win, api)
    _attach_map_geo_track(win, api)
    _start_bringfront_hotkey()

    # 等原生窗口句柄就绪后把系统标题栏染成深色，并恢复上次的窗口不透明度
    def _style_when_ready():
        for _ in range(80):  # 最多等 16 秒
            if api._self_hwnd():
                api.apply_dark_titlebar()
                # 每次启动强制恢复 100% 不透明（运行中可随意调，但不跨启动记忆）
                api.set_window_opacity({"pct": 100})
                return
            time.sleep(0.2)

    threading.Thread(target=_style_when_ready, daemon=True).start()

    # MSFS_TELEPORT_DEBUG=1 时开启 WebView2 开发者工具，便于排查前端问题
    webview.start(debug=bool(os.environ.get("MSFS_TELEPORT_DEBUG")))


def _attach_resize_persist(win, api=None):
    """用户拖拽缩放窗口后把尺寸写盘，下次启动按该尺寸打开。

    处于“仅地图”模式时改为记录该模式的专属尺寸，避免把小窗口尺寸写回常规尺寸。
    """
    pending = {"size": None, "timer": None}

    def on_resized(w, h):
        pending["size"] = (w, h)
        if pending["timer"] is not None:
            return

        def flush():
            pending["timer"] = None
            s = pending["size"]
            if not s:
                return
            if api is not None and getattr(api, "_map_only", False):
                # 仅地图模式：记为该模式的尺寸，不覆盖常规窗口尺寸
                _save_map_geo(win.x or 0, win.y or 0, s[0], s[1])
                logger.info("map-only resized to %sx%s (saved)", *s)
            else:
                _save_win_size(*s)
                logger.info("window resized to %sx%s (saved)", *s)

        pending["timer"] = threading.Timer(1.0, flush)
        pending["timer"].daemon = True
        pending["timer"].start()

    try:
        win.events.resized += on_resized
    except Exception as e:  # noqa: BLE001
        logger.warning("attach resized event failed: %s", e)


def _attach_map_geo_track(win, api):
    """在“仅地图”模式下用户手动拖动窗口时，记住他调好的位置与大小。"""
    pending = {"timer": None}

    def schedule():
        if pending["timer"] is not None:
            pending["timer"].cancel()
        pending["timer"] = threading.Timer(1.2, flush)
        pending["timer"].daemon = True
        pending["timer"].start()

    def flush():
        pending["timer"] = None
        if not getattr(api, "_map_only", False):
            return
        try:
            _save_map_geo(win.x or 0, win.y or 0, win.width, win.height)
            logger.info(
                "map-only geometry saved: %sx%s @(%s,%s)",
                win.width, win.height, win.x, win.y,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("save map geo failed: %s", e)

    try:
        win.events.moved += lambda x, y: schedule()
    except Exception as e:  # noqa: BLE001
        logger.debug("attach moved event failed: %s", e)


if __name__ == "__main__":
    main()
