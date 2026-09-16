"""SimConnect 桥接：连接 MSFS2024 并瞬移飞机。

依赖 Python-SimConnect 库（pip install SimConnect），底层通过 ctypes 调用
SimConnect.dll（随 MSFS 安装）。set_pos() 是库在 SimConnect 连接对象上提供
的方法，一次写入经纬度/海拔/空速即可把飞机传送到目标点。

连接的健壮性处理：
- SimConnect.dll 探测（EXE 目录 / 脚本目录 / 系统 PATH），找不到直接给精准提示
- SimConnect_Open 可能长时间阻塞，因此放入后台线程并限时，超时给出明确提示
"""

import ctypes
import ctypes.util
import ctypes.wintypes as wintypes
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback  # 用于把 get_position 的异常完整堆栈写进日志，便于定位

import SimConnect


def _get_app_dir():
    """返回 EXE/脚本实际所在目录。PyInstaller 单文件模式下优先用 sys.argv[0]。"""
    # PyInstaller onefile 运行时 sys.argv[0] 是原始 EXE 路径；sys.executable 有时
    # 指向临时解压目录，会导致 ui.json/缓存无法在下次启动时保留，因此优先 argv[0]。
    if getattr(sys, "frozen", False):
        for exe in (sys.argv[0], getattr(sys, "executable", "")):
            if exe and os.path.isfile(exe):
                return os.path.dirname(os.path.abspath(exe))
    # 非打包运行时（python app.py）回退到脚本路径
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dirs():
    """PyInstaller 单文件模式下随 EXE 一起解压出来的目录（sys._MEIPASS）。

    build.py 用 `--add-data "<SimConnect.dll>;SimConnect"` 把 DLL 打进包里，
    运行时落在 <_MEIPASS>/SimConnect/SimConnect.dll。把 EXE 单独复制到别的
    文件夹也照样能用 —— 玩家不需要自己去找 DLL。
    """
    base = getattr(sys, "_MEIPASS", "") or ""
    if not base:
        return []
    return [os.path.join(base, "SimConnect"), base]


# 让 EXE/脚本所在目录、以及包内解压目录都参与 DLL 搜索
# （python 运行时进程目录是 python.exe，不包含这些目录）
_APP_DIR = _get_app_dir()
for _d in [_APP_DIR] + _bundle_dirs():
    if _d and os.path.isdir(_d):
        try:
            os.add_dll_directory(_d)
        except (AttributeError, OSError):
            pass


def _common_msfs_roots():
    """常见 MSFS / SDK 安装根目录候选。"""
    roots = []
    drives = [f"{d}:\\" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{d}:\\")]
    for d in drives:
        roots.extend([
            os.path.join(d, "MSFS SDK"),
            os.path.join(d, "MSFS2024 SDK"),
            os.path.join(d, "Program Files", "MSFS SDK"),
            os.path.join(d, "Program Files", "Microsoft Games", "Microsoft Flight Simulator 2024"),
            os.path.join(d, "Program Files (x86)", "Microsoft Games", "Microsoft Flight Simulator 2024"),
            os.path.join(d, "SteamLibrary", "steamapps", "common", "MicrosoftFlightSimulator"),
            os.path.join(d, "XboxGames", "Microsoft Flight Simulator 2024"),
        ])
    return roots


def find_simconnect_dll():
    """返回找到的 SimConnect.dll 路径；找不到返回 None。

    搜索顺序：程序同目录（用户可手动覆盖）→ 包内解压目录（EXE 自带，最可靠）
    → 当前工作目录 → 系统库搜索 → PATH → 常见 MSFS/SDK 安装路径。
    """
    logger.debug("find_simconnect_dll: app_dir=%s cwd=%s", _APP_DIR, os.getcwd())
    # 1. 程序/脚本同目录（用户手动放的优先），以及 EXE 包内自带的那份
    search_dirs = [_APP_DIR] + _bundle_dirs() + [os.getcwd()]
    for d in search_dirs:
        if not d:
            continue
        p = os.path.join(d, "SimConnect.dll")
        logger.debug("  checking %s -> exists=%s", p, os.path.isfile(p))
        if os.path.isfile(p):
            logger.info("SimConnect.dll found at %s", p)
            return p
    # 2. ctypes 系统库搜索
    try:
        p = ctypes.util.find_library("SimConnect")
        logger.debug("  ctypes found %s", p)
        if p and os.path.isfile(p):
            return p
    except Exception:  # noqa: BLE001
        pass
    # 3. PATH 各目录
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            p = os.path.join(d, "SimConnect.dll")
            if os.path.isfile(p):
                return p
    # 4. 常见 MSFS/SDK 安装路径兜底
    for root in _common_msfs_roots():
        for sub in ("", "lib", "SimConnect SDK", "SimConnect SDK\\lib"):
            p = os.path.join(root, sub, "SimConnect.dll") if sub else os.path.join(root, "SimConnect.dll")
            if os.path.isfile(p):
                return p
    logger.warning("SimConnect.dll not found")
    return None


def _classify_error(e):
    msg = str(e)
    low = msg.lower()
    if "126" in msg or "找不到指定的模块" in msg or "module could not be found" in low:
        # dll 已找到，但 ctypes 加载失败（PyInstaller 单文件模式常见：
        # Python-SimConnect 库默认从它自己的包目录加载 SimConnect.dll，
        # 打包时没有把用户 dll 塞进该目录）
        return (
            "SimConnect.dll 已找到，但 Python-SimConnect 库无法加载它。"
            "可能原因：游戏未运行 / DLL 版本不对 / 缺少依赖。"
            "请先确认 MSFS2024 已运行并已进入飞行，然后重启本程序再试。"
        )
    return "连不上 MSFS：请确认游戏已运行并已进入一次飞行"


def _is_msfs_running():
    """检查是否有 MSFS 相关进程在运行。"""
    try:
        patterns = (
            "flight", "microsoft flight", "flightsimulator", "flight simulator",
            "fs2024", "msf2024", "msfs2024",
        )
        hits = [name for _pid, name in _enum_processes()
                if any(p in name.lower() for p in patterns)]
        return bool(hits), list(dict.fromkeys(hits))[:10]
    except Exception:
        return None


class _PROCESSENTRY32(ctypes.Structure):
    """ctypes.wintypes 里并没有 PROCESSENTRY32，必须自己定义。"""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _enum_processes():
    """返回 [(pid, 进程名)]。"""
    out = []
    try:
        kernel = ctypes.windll.kernel32
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snapshot == -1:
            return out
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = kernel.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.szExeFile.decode("utf-8", "ignore")))
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
            ok = kernel.Process32Next(snapshot, ctypes.byref(entry))
        kernel.CloseHandle(snapshot)
    except Exception as e:  # noqa: BLE001
        logger.warning("_enum_processes failed: %s", e)
    return out


def _msfs_pids():
    """返回疑似 MSFS 进程的 PID 集合。"""
    pids = set()
    patterns = ("flight", "flightsimulator", "fs2024", "msf2024")
    for pid, name in _enum_processes():
        low = name.lower()
        if any(p in low for p in patterns):
            pids.add(pid)
    logger.debug("_msfs_pids -> %s", pids)
    return pids


def focus_msfs():
    """把 MSFS 窗口切到前台。

    MSFS 默认“切出窗口时进入主动暂停(Active Pause)”：飞机被冻结、只有摄像机能动，
    手柄左摇杆/十字键看起来就“失灵”了。瞬移前后把焦点还给游戏即可根治。
    返回被激活的窗口标题，未找到返回 None。
    """
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        pids = _msfs_pids()
        self_pid = os.getpid()
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        found = {"hwnd": None, "title": ""}

        # 注意：不能用 "msfs" 做标题关键词——本程序自己叫 MSFSTeleport.exe，
        # 其隐藏窗口标题 "GDI+ Window (MSFSTeleport.exe)" 会被误判成游戏窗口。
        title_keys = ("microsoft flight simulator", "flight simulator")
        # 明确排除本程序自身的窗口（含新标题与旧标题兼容）
        self_keys = ("msfsteleport", "gdi+ window", "msfs-2024 瞬移器", "msfs 任意地点起飞", "任意地点起飞")

        def _each(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            title_buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title_buf, 256)
            title = title_buf.value or ""
            low = title.lower()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            hit = (pid.value in pids) if pids else False
            if not hit and title and any(k in low for k in title_keys):
                hit = True
            # 排除本程序自身的窗口（含隐藏的 GDI+ 辅助窗口）
            if hit and (pid.value == self_pid or any(k in low for k in self_keys)):
                hit = False
            if hit:
                found["hwnd"] = hwnd
                found["title"] = title
                return False
            return True

        user32.EnumWindows(WNDENUMPROC(_each), 0)
        hwnd = found["hwnd"]
        if not hwnd:
            logger.warning("focus_msfs: MSFS window not found; pids=%s", pids)
            return None

        # 最小化时先还原
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return found["title"]
        # Windows 限制后台进程随意抢占前台：借助线程输入附加绕过
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        ok = user32.SetForegroundWindow(hwnd)
        # 多管齐下，确保输入焦点真的回到 3D 视图（手柄/摇杆依赖输入焦点）
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
        if fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        cur_fg = user32.GetForegroundWindow()
        logger.info(
            "focus_msfs: %s -> SetForegroundWindow=%s, now_foreground=%s",
            found["title"], bool(ok), bool(cur_fg == hwnd),
        )
        return found["title"]
    except Exception as e:  # noqa: BLE001
        logger.warning("focus_msfs failed: %s", e)
        return None


def diagnose():
    """返回环境诊断信息，帮助定位连接失败原因。本函数永不抛异常。"""
    try:
        dll = find_simconnect_dll()
        msfs = _is_msfs_running()
        pkg_dir = os.path.dirname(os.path.abspath(SimConnect.__file__)) if SimConnect else ""
        pkg_dll = os.path.join(pkg_dir, "SimConnect.dll") if pkg_dir else ""
        return {
            "app_dir": _APP_DIR or "",
            "dll_found": dll is not None,
            "dll_path": dll or "",
            "pkg_dir": pkg_dir,
            "pkg_dll_ok": os.path.isfile(pkg_dll) if pkg_dll else False,
            "pkg_dll": pkg_dll,
            "msfs_running": msfs[0] if isinstance(msfs, tuple) else (msfs if msfs is not None else False),
            "msfs_processes": msfs[1] if isinstance(msfs, tuple) else [],
            "python_exe": sys.executable or "",
            "working_dir": os.getcwd() or "",
            "log_path": LOG_PATH or "",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "app_dir": _APP_DIR or "",
            "dll_found": False,
            "dll_path": "",
            "pkg_dir": "",
            "pkg_dll_ok": False,
            "pkg_dll": "",
            "msfs_running": False,
            "msfs_processes": [],
            "python_exe": sys.executable or "",
            "working_dir": os.getcwd() or "",
            "log_path": LOG_PATH or "",
            "diag_error": str(e),
        }


# 日志配置：在 _APP_DIR 确定后再初始化
_LOG_PATH = None
_logger = None


# 日志单文件上限与保留份数：1 MB × (1 个当前 + 3 个备份) ≈ 4 MB 封顶。
# 旧版用 basicConfig(filename=...)，**没有任何轮转**，日志会一直追加、无限增长。
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUPS = 3


def _ensure_log():
    global _LOG_PATH, _logger
    if _logger is not None:
        return _logger
    log_path = os.path.join(_APP_DIR, "msfs-teleport.log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        # 自动轮转：写满 maxBytes 即滚为 msfs-teleport.log.1/.2/.3，最旧的丢弃。
        # 若启动时旧日志已超限，首次写入会立刻触发一次轮转 —— 等于自动“瘦身”。
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        logging.basicConfig(level=logging.DEBUG, handlers=[handler], force=True)
    except Exception:  # noqa: BLE001
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s", force=True)
        log_path = ""
    _LOG_PATH = log_path
    _logger = logging.getLogger("msfs_teleport")
    _logger.info("sim_bridge loaded; app_dir=%s", _APP_DIR)
    return _logger


logger = _ensure_log()
LOG_PATH = _LOG_PATH


def classify_aircraft(title, model="", category=""):
    """根据 SimConnect 的 TITLE / ATC_MODEL / CATEGORY 推断机型大类，
    用于自动设定空速档位（与前端 acType 选项的 value 一一对应）。

    返回：airliner / ga / heli / glider / bush / warplane 之一。
    """
    s = " ".join(str(x or "") for x in (title, model, category)).lower()
    cl = (category or "").lower()
    # 优先用 MSFS 自带的 CATEGORY 字段，最可靠
    if "glider" in cl:
        return "glider"
    if "helicopter" in cl:
        return "heli"
    # TITLE / MODEL 关键词兜底（CATEGORY 有时为空或不够细）
    if "glider" in s:
        return "glider"
    if any(k in s for k in ("helicopter", "chopper", "rotorcraft", "héli")):
        return "heli"
    mil = (
        "fighter", "warplane", "warbird", "military", "f-15", "f-16", "f-22", "f-35",
        "f/a-18", "fa-18", "a-10", "f-14", "f-4", "mig", "su-", "su27", "su-27",
        "su35", "su-35", "j-", "spitfire", "p-51", "p-47", "bf-109", "bf109",
        "me-262", "me262", "zero", "harrier", "typhoon", "rafale", "gripen", "eurofighter",
    )
    if any(k in s for k in mil):
        return "warplane"
    jet = (
        "airbus", "boeing", "a320", "a321", "a330", "a350", "a380", "b737", "b747",
        "b777", "b787", "b767", "b757", "crj", "embraer", "erj", "e175", "e190",
        "e195", "atr", "dash 8", "q400", "saab", "cj4", "citation", "learjet",
        "phenom", "hondajet", "airliner",
    )
    if any(k in s for k in jet):
        return "airliner"
    bush = (
        "bush", "super cub", "cub", "maule", "kodiak", "caravan", "cessna 208",
        "beaver", "savage", "advitec",
    )
    if any(k in s for k in bush):
        return "bush"
    return "ga"


class SimBridge:
    # SimConnect 不是线程安全的：所有 DLL 调用都必须串行化（_sim_lock），
    # 否则并发请求会让 SimConnect 返回 0xC00000B0 并使连接彻底失效。
    def __init__(self):
        self.sm = None
        self._events = None
        self._requests = None
        self._sim_lock = threading.RLock()

    def _make_requests(self):
        """创建 AircraftRequests：加长轮询等待（30×10ms=300ms），减少读到 None 的概率。"""
        return SimConnect.AircraftRequests(self.sm, _time=10, _attemps=30)

    def _make_events(self):
        return SimConnect.AircraftEvents(self.sm)

    def connect(self, timeout=10.0):
        """建立与 MSFS2024 的连接（限时）。成功返回 True，失败抛 RuntimeError。"""
        logger.info("connect() called")
        if self.sm is not None:
            if self._is_connection_alive():
                logger.info("already connected and alive")
                return True
            logger.warning("existing connection appears dead; closing and reconnecting")
            self.close()

        dll = find_simconnect_dll()
        if dll is None:
            logger.error("SimConnect.dll not found; app_dir=%s bundle=%s",
                         _APP_DIR, _bundle_dirs())
            raise RuntimeError(
                "未找到 SimConnect.dll（程序自带的副本也没解压出来）。"
                "请重新下载完整版 EXE；确有必要时再把 MSFS SDK 里的 SimConnect.dll 放到本程序同目录"
            )

        # Python-SimConnect 库默认从它自己的包目录加载 SimConnect.dll：
        # _library_path = <SimConnect 包>/SimConnect.dll
        # 单文件打包时该目录位于临时解压区（<_MEIPASS>/SimConnect），build.py 已把
        # DLL 塞进去；这里再做两件事：能复制进包目录就复制（最贴合库的设计），
        # 否则直接以绝对路径传给库（库支持 library_path 参数，windll.LoadLibrary 吃绝对路径）。
        dll_abs = os.path.abspath(dll)
        pkg_dir = os.path.dirname(os.path.abspath(SimConnect.__file__))
        expected_dll = os.path.join(pkg_dir, "SimConnect.dll")
        if os.path.isfile(expected_dll):
            logger.info("SimConnect.dll already in package dir: %s", expected_dll)
        else:
            try:
                import shutil
                os.makedirs(pkg_dir, exist_ok=True)
                shutil.copy2(dll_abs, expected_dll)
                logger.info("copied SimConnect.dll to package dir: %s", expected_dll)
            except Exception as e:  # noqa: BLE001
                logger.warning("could not copy dll to package dir: %s", e)
                expected_dll = dll_abs

        result = {}
        logger.info("trying SimConnect.SimConnect(library_path=%s) with timeout=%s", expected_dll, timeout)

        def _open():
            try:
                result["sm"] = SimConnect.SimConnect(library_path=expected_dll)
            except Exception as e:  # noqa: BLE001
                logger.exception("SimConnect.SimConnect() failed")
                result["err"] = e

        t = threading.Thread(target=_open, daemon=True)
        t.start()
        t.join(timeout)

        if t.is_alive():
            logger.error("connect timed out after %s seconds", timeout)
            raise RuntimeError(
                f"连接超时（{timeout:.0f} 秒）：请确认游戏已运行并已进入一次飞行，然后重新点击右上角重连"
            )
        if "err" in result:
            logger.error("connect error: %s", result["err"])
            raise RuntimeError(_classify_error(result["err"])) from result["err"]

        self.sm = result["sm"]
        # 预创建共享的 Requests/Events 对象，避免每次调用都新建导致
        # SimConnect 对象槽耗尽（MSFS2024 实测会报 TOO_MANY_OBJECTS）。
        try:
            self._events = self._make_events()
            logger.info("AircraftEvents created")
        except Exception as e:  # noqa: BLE001
            logger.warning("could not create AircraftEvents: %s", e)
            self._events = None
        try:
            self._requests = self._make_requests()
            logger.info("AircraftRequests created")
        except Exception as e:  # noqa: BLE001
            logger.warning("could not create AircraftRequests: %s", e)
            self._requests = None
        logger.info("connected successfully")
        return True

    def is_connected(self):
        return self.sm is not None

    def unpause(self):
        """强制解除暂停：把焦点还给游戏 + 发送 PAUSE_OFF / PAUSE_SET(0)。

        MSFS 的“切出窗口自动暂停”会进入 Active Pause：飞机冻结、只留摄像机可动，
        表现为手柄摇杆/十字键“失灵”。本方法用于手动或自动恢复。
        """
        if self.sm is None:
            return False
        try:
            logger.info("unpause(): focusing MSFS window")
            focus_msfs()
            time.sleep(0.4)
            with self._sim_lock:
                if self._events is None:
                    self._events = self._make_events()
                misc = self._events.Miscellaneous_Events
                # 连发两次：焦点切换后游戏可能立刻重新进入暂停，第二发用于收尾
                for i in range(2):
                    try:
                        misc.PAUSE_OFF()
                        time.sleep(0.15)
                        misc.PAUSE_SET(0)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("unpause event #%s failed: %s", i, e)
                    time.sleep(0.3)
            logger.info("unpause(): done")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("unpause failed: %s", e)
            return False

    def _ensure_unpaused(self):
        """瞬移前确保 MSFS 处于运行态：先把焦点还给游戏，再解除暂停。

        MSFS 默认“切出窗口自动暂停”会让飞机冻结，表现为手柄左摇杆/十字键失灵。
        这里先把焦点还给游戏，再连发两次 PAUSE_OFF / PAUSE_SET(0)——第一次可能
        被“焦点刚切换”重新进入暂停覆盖，第二次用于收尾，确保恢复运行态。
        """
        if self.sm is None:
            return
        try:
            logger.info("focusing MSFS window before set_pos")
            focus_msfs()
            time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            logger.warning("focus_msfs failed: %s", e)
        try:
            logger.info("sending PAUSE_OFF event before set_pos")
            with self._sim_lock:
                if self._events is None:
                    self._events = self._make_events()
                # 正确的事件组是 Miscellaneous_Events（含 PAUSE_TOGGLE/PAUSE_ON/PAUSE_OFF）
                misc = self._events.Miscellaneous_Events
                for _ in range(2):
                    misc.PAUSE_OFF()
                    time.sleep(0.1)
                    misc.PAUSE_SET(0)
                    time.sleep(0.15)
            # 给模拟器一点时间来响应事件
            time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            logger.warning("PAUSE_OFF event failed: %s", e)

    def get_position(self, timeout=1.5):
        """读取飞机当前位置/航向/高度，用于地图上显示。

        暂停时 SimConnect 请求同样可能挂起，因此放进线程并限时；
        超时返回 None（调用方直接跳过本次刷新，不影响界面）。
        """
        if self.sm is None:
            return None
        result = {}

        def _query():
            try:
                with self._sim_lock:
                    if self._requests is None:
                        self._requests = self._make_requests()
                    req = self._requests
                # 注意：只读取浮点型变量（经纬度/航向/海拔）。
                # 之前尝试用 TITLE / ATC_MODEL 读取机型字符串，但本机装的 Python-SimConnect
                # 对字符串型数据定义存在 bug（"a bytes-like object is required"），会连带让
                # 整个位置查询失败。因此机型自动识别改为不在此处读取，避免影响核心的位置获取；
                # 预设空速仍由用户在侧栏下拉框手动选择。
                lat = req.get("PLANE_LATITUDE")
                lon = req.get("PLANE_LONGITUDE")
                if lat is None or lon is None:
                    result["err"] = "lat/lon not available"
                    return
                hdg = req.get("PLANE_HEADING_DEGREES_TRUE")  # 弧度
                alt = req.get("PLANE_ALTITUDE")  # 英尺 MSL（海拔，用于瞬移）
                agl = req.get("PLANE_ALT_ABOVE_GROUND")  # 英尺 AGL（离地高度，用于缩放随高度）
                result["data"] = {
                    "lat": float(lat),
                    "lon": float(lon),
                    "heading": float(hdg) * 57.295779513 if hdg is not None else None,
                    "alt_ft": float(alt) if alt is not None else None,
                    "agl_ft": float(agl) if agl is not None else None,
                }
            except Exception as e:  # noqa: BLE001
                logger.debug("get_position failed: %s\n%s", e, traceback.format_exc())
                result["err"] = e

        t = threading.Thread(target=_query, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            logger.debug("get_position timed out (sim paused?)")
            return None
        data = result.get("data")
        if not data:
            return None
        if data["lat"] == 0.0 and data["lon"] == 0.0:
            return None
        return data

    def teleport(self, lat, lon, alt=1500.0, heading=90.0, airspeed=130.0, set_pos_timeout=8.0):
        """瞬移到指定坐标（直接写入位置，不进入 Slew）。

        lat/lon : 十进制度（北纬为正，东经为正）
        alt     : 海拔（米，MSL）
        heading : 机头朝向（真航向，度）
        airspeed: 空速（节），默认 130 节巡航速度，避免瞬移后失速下坠

        说明：刻意不使用 Slew 模式——Slew 会接管 MSFS 的操控映射，退出后手柄
        左摇杆/十字键容易失效（仅剩摄像机可动），正是“手柄无法操控”的根因。
        这里只确保模拟器处于运行态并把焦点还给游戏，写入后立即再次归还焦点，
        使手柄在瞬移后马上恢复控制（这也是最早没有 Slew 功能时好用的做法）。
        """
        if self.sm is None:
            self.connect()
        logger.info("teleport -> lat=%.6f lon=%.6f alt=%.1f heading=%.1f airspeed=%.1f",
                    lat, lon, alt, heading, airspeed)

        # 确保运行态 + 焦点回到游戏（清除 Active Pause），否则 set_pos 可能挂起
        self._ensure_unpaused()

        ok = self._try_set_pos(lat, lon, alt, heading, airspeed, set_pos_timeout)
        if not ok:
            # 第一次失败：很可能是连接已死（0xC00000B0）。尝试重连一次再写。
            logger.warning("set_pos failed, trying reconnect and retry once")
            self.reconnect()
            self._ensure_unpaused()
            ok = self._try_set_pos(lat, lon, alt, heading, airspeed, set_pos_timeout)
        if not ok:
            logger.error("set_pos failed after reconnect")
            raise RuntimeError("SimConnect 拒绝了位置写入（set_pos 失败），请重试")

        # 瞬移完成后把焦点还给 MSFS，让手柄立即恢复操控
        time.sleep(0.15)
        focus_msfs()
        logger.info("teleport done (direct set_pos)")
        return {"ok": True}

    def _try_set_pos(self, lat, lon, alt, heading, airspeed, timeout):
        """执行一次位置写入，带线程限时。成功返回 True。

        set_pos 在暂停/焦点异常时可能无限阻塞，因此放进线程并限时。
        """
        result = {}

        def _set_pos():
            try:
                with self._sim_lock:
                    # 库的 SIMCONNECT_DATA_INITPOSITION 结构体：
                    #   Altitude/Latitude/Longitude/Pitch/Bank/Heading = c_double（英尺/度）
                    #   OnGround/Airspeed = DWORD 整数 → 传 float 会 TypeError
                    ok = self.sm.set_pos(
                        _Altitude=float(alt) * 3.28084,  # 米 -> 英尺
                        _Latitude=float(lat),
                        _Longitude=float(lon),
                        _Airspeed=int(float(airspeed)),
                        _Heading=float(heading),
                    )
                result["ok"] = ok
            except Exception as e:  # noqa: BLE001
                logger.exception("set_pos exception")
                result["err"] = e

        t = threading.Thread(target=_set_pos, daemon=True)
        t.start()
        t.join(timeout)

        if t.is_alive():
            logger.error("set_pos timed out after %s seconds (sim likely paused)", timeout)
            return False
        if "err" in result:
            logger.error("set_pos error: %s", result["err"])
            return False
        return bool(result.get("ok"))

    def close(self):
        with self._sim_lock:
            sm, self.sm = self.sm, None
            self._events = None
            self._requests = None
        if sm is not None:
            # sm.exit() 会 join 库内部的消息泵线程；若该线程卡住会永远等下去，
            # 因此用带超时的后台线程收尾，超时就放弃（对象随后被 GC 回收）。
            def _do_exit():
                try:
                    sm.exit()
                except Exception:  # noqa: BLE001
                    pass

            t = threading.Thread(target=_do_exit, daemon=True)
            t.start()
            t.join(2.0)

    def _is_connection_alive(self, timeout=2.0):
        """轻量健康检查：尝试读一个已知变量。失败返回 False（调用方负责重连）。

        必须限时——MSFS 处于暂停/卡顿时，SimConnect 请求可能无限阻塞，
        若在这里死等会让 connect()/teleport() 整个卡住。
        """
        if self.sm is None:
            return False
        result = {}

        def _probe():
            try:
                with self._sim_lock:
                    if self._requests is None:
                        self._requests = self._make_requests()
                    v = self._requests.get("PLANE_ALTITUDE")
                result["v"] = v
            except Exception as e:  # noqa: BLE001
                result["err"] = e

        t = threading.Thread(target=_probe, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            logger.debug("connection health check timed out")
            return False
        if "err" in result:
            logger.debug("connection health check failed: %s", result["err"])
            return False
        return result.get("v") is not None

    def reconnect(self):
        """关闭当前连接并重新建立。"""
        logger.info("reconnecting to MSFS...")
        self.close()
        return self.connect()
