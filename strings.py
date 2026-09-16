"""界面文案（中文 / 英文）。

用法：
- 逻辑代码里统一用 `t('键名')` 取文案，不要写死语言。
- 构建时由 build.py 生成 `_build_lang.py` 指定默认语言（用于产出纯英文 EXE）。
- 开发调试可用环境变量切换：`MSFS_TELEPORT_LANG=en python app.py`

新增文案时请同时补 ZH 和 EN，缺失会自动回退到中文，不会崩。
"""

import json
import os

# ---------------------------------------------------------------- 版本
# 累计迭代轮次：2026-09-08 首版 → 2026-09-11 共 16 轮（详见 .workbuddy/memory/）。
# 升版本只改这一个数字。
APP_VERSION = "0.41"

# ---------------------------------------------------------------- 中文
ZH = {
    # ---- 通用 / 窗口 ----
    "app_title": "FS瞬移者",
    "html_lang": "zh-CN",
    "h1": "FS瞬移者",

    # ---- 顶部状态与按钮 ----
    "reconnect_title": "点击重连",
    "not_connected": "未连接",
    "connected": "已连接",
    "connecting": "连接中…",
    "btn_unpause": "恢复操控",
    "unpause_title": "手柄摇杆/十字键失灵时点这里（也可按 Ctrl+Alt+E）",
    "btn_diag": "诊断",
    "opt_ontop": "置顶显示",
    "opt_follow": "跟随飞机",
    "opt_autozoom": "缩放随高度",
    "autozoom_off": "已手动缩放，退出高度联动",
    "btn_maponly": "仅地图",
    "lbl_opacity": "透明度",
    "btn_showpanel": "显示面板",
    "opt_northup": "北向上",
    "opt_headup": "机头朝上",
    "hint_upmode": "切换地图朝向 —— 北向上：地图固定、飞机随航向转动；机头朝上：地图随航向旋转、飞机始终朝上，罗盘同步转动。",
    "hint_view": "置顶不影响游戏操控；跟随模式每 10 秒以飞机为中心刷新地图，飞行中按 Ctrl+Alt+G 可调本窗口到前台。",

    # ---- 标签页与表单 ----
    "ph_place": "输入地点，如：哈尔滨中央大街 / Eiffel Tower",
    "btn_search": "搜索",
    "cap_search": "搜索定位",
    "cap_tools": "工具",
    "ph_combo": "也可直接填：45.75, 126.63",
    "cap_target": "目标位置与飞机设定",
    "lab_lat": "纬度",
    "lab_lon": "经度",
    "lab_alt": "海拔",
    "lab_hdg": "朝向（°）",
    "ph_lat": "如 45.75",
    "ph_lon": "如 126.63",
    "btn_teleport": "前往此位置",
    "btn_arm": "设为 Ctrl+Alt+T 快捷",
    "hint_arm": "设好后切回游戏窗口，按 Ctrl+Alt+T 即可定位（焦点不离开游戏，手柄不会失灵）",
    "fav_ask": "是否前往「{name}」上空？",
    "ask_yes": "是",
    "ask_no": "否",

    # ---- 诊断面板 ----
    "diag_title": "环境诊断",
    "lbl_dll": "SimConnect.dll：",
    "lbl_pkg_dir": "SimConnect 包目录：",
    "lbl_pkg_dll": "包目录内 DLL：",
    "lbl_msfs_proc": "MSFS 进程：",
    "lbl_app_dir": "程序目录：",
    "lbl_work_dir": "工作目录：",
    "lbl_python": "Python：",
    "lbl_log": "日志文件：",
    "lbl_diag_error": "诊断错误：",
    "v_found": "已找到",
    "v_not_found": "未找到",
    "v_ready": "已就绪",
    "v_unknown": "未知",
    "v_running": "运行中",
    "v_not_detected": "未检测到",
    "v_undetermined": "无法判断",
    # 连接失败时显示在红色提示里的“可操作建议”（单行，不换行）
    "diag_hint_no_dll": "未找到 SimConnect.dll，请把它放到本程序所在目录后重启程序。",
    "diag_hint_copy": "SimConnect.dll 尚未复制到库目录，若自动复制失败请手动复制到系统的 SimConnect 包目录。",
    "hint_fix_msfs": "未检测到游戏进程，请先启动微软模拟飞行并进入一次飞行。",

    # ---- 地图 ----
    "map_hint": "点击选点，滚轮缩放，拖动平移，双击放大",
    "btn_locate": "定位飞机",
    "btn_zoom_in": "＋",
    "btn_zoom_out": "－",
    "map_no_fix": "暂无飞机位置",

    # ---- 地图图层 ----
    "layer_lbl": "图层",
    "layer_street": "街道",
    "layer_sat": "卫星",
    "sel_moving": "移动中…",
    "sel_loading": "载入目标区域…",
    "layer_terrain": "地形",
    "tile_stat": "z{z}｜已载 {load}，失败 {fail}",

    # ---- 高度与速度预设 ----
    "lab_speed": "自定义空速（节）",
    "lab_preset_speed": "预设空速",
    "lab_alt_unit": "高度单位",
    "unit_m": "米",
    "unit_ft": "英尺",
    "lab_preset": "预设",
    "ac_airliner": "客机（450 节）",
    "ac_ga": "通航（120 节）",
    "ac_heli": "直升机（90 节）",
    "ac_glider": "滑翔机（60 节）",
    "ac_bush": "丛林机（110 节）",
    "ac_warplane": "军机（350 节）",
    "ac_custom": "自定义",
    "preset_ground": "地面",
    "preset_pattern": "起落航线",
    "preset_approach": "进近",
    "preset_standard": "标准",
    "preset_low_cruise": "低巡航",
    "preset_mid_cruise": "中巡航",
    "preset_high_cruise": "高巡航",

    # ---- 收藏点 ----
    "lab_fav": "收藏点",
    "btn_fav_add": "收藏此点",
    "ph_fav_name": "名称（可留空）",
    "ph_fav_choose": "选择收藏点…",
    "btn_fav_del": "删除",
    "fav_empty": "暂无收藏",
    "fav_need_coord": "请先选定坐标",

    # ---- 提示消息（Python 端） ----
    "connect_ok": "已连接到 MSFS2024（请确认已进入一次飞行）",
    "connect_fail": "连接失败：{err}",
    "invalid_params": "坐标或参数无效",
    "arm_ok": "已设为快捷目标 {lat}, {lon}：切回游戏后按 Ctrl+Alt+T 定位",
    "geocode_empty": "请输入地点名",
    "geocode_none": "未找到匹配地点，换个关键词试试",
    "geocode_fail": "解析失败：{err}",
    "parse_fail": "无法识别，请按 纬度,经度 格式，如 45.75, 126.63",
    "unpause_ok": "已切回游戏窗口并解除暂停，现在应该能正常操纵飞机了",
    "unpause_manual": "未能解除暂停：请手动点一下 MSFS 窗口，或关闭其“切出窗口自动暂停”设置",
    "unpause_fail": "解除暂停失败：{err}",
    "teleport_ok": "已定位到 {lat}, {lon}（海拔 {alt} 米），已切回游戏窗口",
    "teleport_fail": "瞬移失败：{err}",

    # ---- 页脚署名（左侧栏底部） ----
    "credit_design": "JerryXiao设计",
    "credit_made": "WorkBuddy 制作",
    "credit_ver": "版本号 v" + APP_VERSION,
    "lang_label": "中",
    "hint_lang": "点击切换中 / 英文界面",

    # ---- 提示消息（JS 端） ----
    "js_bridge_dead": "界面桥接未就绪，请重启程序",
    "js_call_error": "调用出错：",
    "js_not_ready": "界面尚未就绪",
    "js_not_ready_wait": "界面尚未就绪，请稍候 1-2 秒",
    "js_collecting": "正在收集环境信息…",
    "js_connecting": "正在连接 MSFS2024…",
    "js_search_fail": "搜索失败",
    "js_searching": "正在搜索全部数据源…",
    "js_candidates": "找到 {n} 个候选，点击选择",
    "js_chosen": "已选：{name}",
    "js_need_latlon": "请填写有效的纬度/经度",
    "js_prepare": "准备定位：先建立连接…",
    "js_connect_failed": "连接失败",
    "js_teleporting": "定位中…",
    "js_unpausing": "正在切回游戏并解除暂停…",
}

# ---------------------------------------------------------------- English
EN = {
    # ---- General / window ----
    "app_title": "FS Relocator",
    "html_lang": "en",
    "h1": "FS Relocator",

    # ---- Header status & buttons ----
    "reconnect_title": "Click to reconnect",
    "not_connected": "Disconnected",
    "connected": "Connected",
    "connecting": "Connecting…",
    "btn_unpause": "Resume Control",
    "unpause_title": "Click here if the stick/D-pad stops responding (or press Ctrl+Alt+E)",
    "btn_diag": "Diagnose",
    "opt_ontop": "Always on top",
    "opt_follow": "Follow aircraft",
    "opt_autozoom": "Zoom by altitude",
    "autozoom_off": "Manual zoom — altitude auto-zoom off",
    "btn_maponly": "Map only",
    "lbl_opacity": "Opacity",
    "btn_showpanel": "Show panel",
    "opt_northup": "North up",
    "opt_headup": "Heading up",
    "hint_upmode": "Map orientation — North up: the map stays fixed and the aircraft rotates with its heading. Heading up: the map rotates with the heading so the aircraft always points up, and the compass follows.",
    "hint_view": "Always-on-top does not steal input. Follow mode refreshes the map every 10 s with the aircraft centered. Press Ctrl+Alt+G during flight to bring this window forward.",

    # ---- Tabs & form ----
    "ph_place": "Enter a place, e.g. Eiffel Tower",
    "btn_search": "Search",
    "cap_search": "Search & Locate",
    "cap_tools": "Tools",
    "ph_combo": "Or type directly: 45.75, 126.63",
    "cap_target": "Target & Aircraft Setup",
    "lab_lat": "Latitude",
    "lab_lon": "Longitude",
    "lab_alt": "Altitude",
    "lab_hdg": "Heading (°)",
    "ph_lat": "e.g. 45.75",
    "ph_lon": "e.g. 126.63",
    "btn_teleport": "Go to Location",
    "btn_arm": "Arm Ctrl+Alt+T",
    "hint_arm": "Then switch back to the sim and press Ctrl+Alt+T. Focus never leaves the game, so your joystick keeps working.",
    "fav_ask": "Go to \"{name}\"?",
    "ask_yes": "Yes",
    "ask_no": "No",

    # ---- Diagnostics panel ----
    "diag_title": "Diagnostics",
    "lbl_dll": "SimConnect.dll: ",
    "lbl_pkg_dir": "SimConnect package dir: ",
    "lbl_pkg_dll": "DLL in package dir: ",
    "lbl_msfs_proc": "MSFS process: ",
    "lbl_app_dir": "App directory: ",
    "lbl_work_dir": "Working directory: ",
    "lbl_python": "Python: ",
    "lbl_log": "Log file: ",
    "lbl_diag_error": "Diagnostics error: ",
    "v_found": "found",
    "v_not_found": "not found",
    "v_ready": "ready",
    "v_unknown": "unknown",
    "v_running": "running",
    "v_not_detected": "not detected",
    "v_undetermined": "undetermined",
    # One-line actionable advice shown inside the red error message (no line breaks)
    "diag_hint_no_dll": "SimConnect.dll was not found. Put it in this tool's folder and restart the tool.",
    "diag_hint_copy": "SimConnect.dll is not in the library's package dir yet; if the auto-copy fails, copy it there manually.",
    "hint_fix_msfs": "No sim process detected. Start Microsoft Flight Simulator and enter a flight first.",

    # ---- Map ----
    "map_hint": "Click to pick, scroll to zoom, drag to pan, double-click to zoom in",
    "btn_locate": "Locate",
    "btn_zoom_in": "+",
    "btn_zoom_out": "-",
    "map_no_fix": "No aircraft position",

    # ---- Map layers ----
    "layer_lbl": "Layer",
    "layer_street": "Street",
    "layer_sat": "Satellite",
    "sel_moving": "Moving…",
    "sel_loading": "Loading target area…",
    "layer_terrain": "Terrain",
    "tile_stat": "z{z} | {load} loaded, {fail} failed",

    # ---- Altitude & speed presets ----
    "lab_speed": "Custom speed (kt)",
    "lab_preset_speed": "Preset airspeed",
    "lab_alt_unit": "Altitude unit",
    "unit_m": "Meters",
    "unit_ft": "Feet",
    "lab_preset": "Presets",
    "ac_airliner": "Airliner (450 kt)",
    "ac_ga": "GA (120 kt)",
    "ac_heli": "Helicopter (90 kt)",
    "ac_glider": "Glider (60 kt)",
    "ac_bush": "Bush (110 kt)",
    "ac_warplane": "Warplane (350 kt)",
    "ac_custom": "Custom",
    "preset_ground": "Ground",
    "preset_pattern": "Pattern",
    "preset_approach": "Approach",
    "preset_standard": "Standard",
    "preset_low_cruise": "Low cruise",
    "preset_mid_cruise": "Mid cruise",
    "preset_high_cruise": "High cruise",

    # ---- Favorites ----
    "lab_fav": "Favorites",
    "btn_fav_add": "Save point",
    "ph_fav_name": "Name (optional)",
    "ph_fav_choose": "Choose favorite…",
    "btn_fav_del": "Delete",
    "fav_empty": "No favorites yet",
    "fav_need_coord": "Pick a location first",

    # ---- Messages (Python side) ----
    "connect_ok": "Connected to MSFS 2024 (make sure you are already in a flight)",
    "connect_fail": "Connection failed: {err}",
    "invalid_params": "Invalid coordinates or parameters",
    "arm_ok": "Target armed: {lat}, {lon}. Switch back to the sim and press Ctrl+Alt+T.",
    "geocode_empty": "Please enter a place name",
    "geocode_none": "No match found. Try another keyword.",
    "geocode_fail": "Lookup failed: {err}",
    "parse_fail": "Unrecognized. Use \"latitude, longitude\", e.g. 45.75, 126.63",
    "unpause_ok": "Focus returned to the sim and pause cleared. You should be able to fly now.",
    "unpause_manual": "Could not clear pause: click the MSFS window manually, or turn off its pause-on-task-switch option.",
    "unpause_fail": "Failed to clear pause: {err}",
    "teleport_ok": "Relocated to {lat}, {lon} at {alt} m. Focus returned to the sim.",
    "teleport_fail": "Teleport failed: {err}",

    # ---- Footer credits (bottom of the side panel) ----
    "credit_design": "Designed by JerryXiao",
    "credit_made": "Made with WorkBuddy",
    "credit_ver": "Version v" + APP_VERSION,
    "lang_label": "En",
    "hint_lang": "Click to switch between Chinese / English",

    # ---- Messages (JS side) ----
    "js_bridge_dead": "UI bridge not ready, please restart the tool",
    "js_call_error": "Call failed: ",
    "js_not_ready": "UI not ready yet",
    "js_not_ready_wait": "UI not ready yet, please wait 1-2 seconds",
    "js_collecting": "Collecting diagnostics…",
    "js_connecting": "Connecting to MSFS 2024…",
    "js_search_fail": "Search failed",
    "js_searching": "Searching all sources…",
    "js_candidates": "{n} match(es) found, pick one",
    "js_chosen": "Selected: {name}",
    "js_need_latlon": "Please enter a valid latitude/longitude",
    "js_prepare": "Preparing: establishing connection…",
    "js_connect_failed": "Connection failed",
    "js_teleporting": "Relocating…",
    "js_unpausing": "Returning focus to the sim and clearing pause…",
}

STRINGS = {"zh": ZH, "en": EN}

# 前端 JS 需要用到的文案键
_JS_KEYS = [k for k in ZH if k.startswith("js_")] + [
    "v_found", "v_not_found", "v_ready", "v_unknown",
    "v_running", "v_not_detected", "v_undetermined",
    "lbl_dll", "lbl_pkg_dir", "lbl_pkg_dll", "lbl_msfs_proc",
    "lbl_app_dir", "lbl_work_dir", "lbl_python", "lbl_log", "lbl_diag_error",
    "diag_hint_no_dll", "diag_hint_copy", "hint_fix_msfs",
    "not_connected", "connected",
    "map_no_fix",
    "fav_empty", "fav_need_coord", "connecting", "fav_ask", "ask_yes", "ask_no",
    "autozoom_off",
    "preset_ground", "preset_pattern", "preset_approach", "preset_standard",
    "preset_low_cruise", "preset_mid_cruise", "preset_high_cruise",
    "layer_street", "layer_sat", "layer_terrain", "tile_stat",
    "sel_moving", "sel_loading",
    "opt_ontop", "opt_follow", "btn_maponly", "btn_showpanel", "hint_view",
    "opt_northup", "opt_headup",
    "app_title",
]


def _system_lang():
    """读 Windows 用户界面语言，中文返回 'zh'，其余一律 'en'。"""
    try:
        import ctypes

        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFFFF
        # 主语言 ID：0x04 = 中文（0x0804 简体 / 0x0C04 繁体 / 0x0404 台湾等）
        return "zh" if (lid & 0x3FF) == 0x04 else "en"
    except Exception:  # noqa: BLE001
        return None


def _msfs_lang():
    """读 MSFS2024 / 2020 的界面语言（UserCfg.opt 的 Language 字段）。

    返回 'zh' / 'en'；找不到文件或字段时返回 None。
    MSFS2024（微软商店/Xbox）：%LOCALAPPDATA%\\Packages\\Microsoft.Limitless_8wekyb3d8bbwe\\LocalCache\\UserCfg.opt
    MSFS2024（Steam）：%APPDATA%\\Microsoft Flight Simulator 2024\\UserCfg.opt
    MSFS2020 路径类似（包名 FlightSimulator_8wekyb3d8bbwe / Roaming\\Microsoft Flight Simulator）。
    """
    import re as _re

    try:
        local = os.environ.get("LOCALAPPDATA")
        roaming = os.environ.get("APPDATA")
        cands = []
        if local:
            for pkg in ("Microsoft.Limitless_8wekyb3d8bbwe",
                        "Microsoft.FlightSimulator_8wekyb3d8bbwe"):
                cands.append(os.path.join(local, "Packages", pkg,
                                          "LocalCache", "UserCfg.opt"))
            pkgs = os.path.join(local, "Packages")
            if os.path.isdir(pkgs):
                for name in os.listdir(pkgs):
                    low = name.lower()
                    if "limitless" in low or "flightsimulator" in low:
                        cands.append(os.path.join(pkgs, name,
                                                  "LocalCache", "UserCfg.opt"))
        if roaming:
            cands.append(os.path.join(roaming, "Microsoft Flight Simulator 2024",
                                      "UserCfg.opt"))
            cands.append(os.path.join(roaming, "Microsoft Flight Simulator",
                                      "UserCfg.opt"))
        seen = set()
        for p in cands:
            if p in seen:
                continue
            seen.add(p)
            if not os.path.isfile(p):
                continue
            try:
                txt = open(p, "r", encoding="utf-8", errors="ignore").read()
            except Exception:  # noqa: BLE001
                continue
            m = _re.search(r'^\s*Language\s+"([^"]+)"', txt, _re.M)
            if not m:
                m = _re.search(r'^\s*(?:UILanguage|Locale)\s+"([^"]+)"', txt, _re.M)
            if m:
                val = m.group(1).lower()
                return "zh" if "zh" in val else "en"
    except Exception:  # noqa: BLE001
        return None
    return None


def detect_lang():
    """语言判定顺序：环境变量 > EXE 文件名 > MSFS 界面语言 > 系统 UI 语言 > 默认中文。

    - 程序启动时优先采用 MSFS2024 当前的界面语言：zh-CN 等中文 → 中文界面，其他 → 英文界面。
    - 用户若在界面里手动切换过语言（彩蛋），该选择持久化在 ui.json，优先级最高（见 app.py 启动逻辑）。
    - 保留环境变量 MSFS_TELEPORT_LANG 与 `-EN.exe` 文件名后缀作为手动覆盖手段。
    """
    import sys

    env = os.environ.get("MSFS_TELEPORT_LANG", "").strip().lower()
    if env in ("zh", "en"):
        return env

    exe_name = os.path.basename(sys.executable or "").lower()
    if exe_name.endswith("-en.exe"):
        return "en"

    msfs = _msfs_lang()
    if msfs:
        return msfs

    sys_lang = _system_lang()
    if sys_lang:
        return sys_lang
    return "zh"


LANG = detect_lang()
T = STRINGS.get(LANG) or ZH


def set_lang(lang):
    """运行时切换界面语言（不重启进程）。

    lang 以 'en' 开头则切英文，否则切中文；返回生效后的语言码（'zh' / 'en'）。
    切换后全局 LANG / T 立即更新，随后重新 render 即可得到新语言界面。
    """
    global LANG, T
    code = "en" if str(lang).strip().lower().startswith("en") else "zh"
    LANG = code
    T = STRINGS.get(code) or ZH
    return LANG


def t(key, **kw):
    """取当前语言文案，可用 {占位符} 插值。缺 key 时回退中文再回退键名。"""
    s = T.get(key)
    if s is None:
        s = ZH.get(key)
    if s is None:
        s = key
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def js_strings():
    """给前端注入的文案表（JSON）。"""
    return json.dumps({k: t(k) for k in _JS_KEYS}, ensure_ascii=False)


def render(template):
    """把 HTML 模板里的 {{KEY}} 占位符替换为当前语言文案。

    未翻译的 key 会回退到中文，避免界面出现裸露占位符。
    """
    out = template
    all_keys = list(dict.fromkeys(list(ZH.keys()) + list(EN.keys())))
    for k in all_keys:
        token = "{{" + k + "}}"
        if token in out:
            val = T.get(k) or ZH.get(k) or ""
            out = out.replace(token, val)
    return out
