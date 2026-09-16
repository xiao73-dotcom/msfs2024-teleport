"""打包成单文件 EXE（Windows）。

用法：
    python build.py                           # -> dist/FSRelocator.exe（中英双语单包）
    python build.py --distpath dist_new       # 输出到别的目录（绕开被占用的旧 EXE）

界面语言在运行时自动判定：优先采用 MSFS2024 当前界面语言（中文->中文界面，其他->英文），
可在界面里手动切换（彩蛋）并持久化；也可用环境变量 MSFS_TELEPORT_LANG=zh/en 手动覆盖。

说明：
- 依赖 webview(pywebview) 的内嵌浏览器（Windows 自带 Edge WebView2，一般已就绪）。
- SimConnect.dll 会随包一并分发（打在里面），运行时自动解压使用 ——
  用户把 EXE 单独复制到任何文件夹都能用，无需自己去找 DLL。
- 内置离线卫星底图（z0~z5）随包分发，低倍率地图不依赖网络。
"""

import ctypes.util
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_NAME = "FSRelocator"

# --distpath <dir>: 换一个输出目录，用来绕开"旧 EXE 还在运行、文件被锁无法覆盖"。
# （Windows 下 dist/MSFSTeleport.exe 被占用时删除会失败，PyInstaller 随之报错。）
_distpath = os.path.join(_HERE, "dist")
if "--distpath" in sys.argv:
    _i = sys.argv.index("--distpath")
    try:
        _distpath = sys.argv[_i + 1]
    except IndexError:
        print("[build] --distpath needs a directory argument")
        sys.exit(1)

print("[build] single bilingual build -> %s/%s.exe" % (_distpath, _NAME))

CMD = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--noconfirm",
    "--distpath", os.path.abspath(_distpath),
    "--workpath", os.path.join(_HERE, "build_tmp"),
    "--specpath", os.path.join(_HERE, "build_tmp"),
    "--name", _NAME,
    "--icon", os.path.join(_HERE, "app_icon.ico"),
    "--hidden-import", "webview",
    "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "SimConnect",
    "--hidden-import", "SimConnect.SimConnect",
    "--hidden-import", "strings",
]

# 如果能在打包机找到 SimConnect.dll，就把它作为数据文件塞进 SimConnect 包目录，
# 这样单文件 EXE 解压后库能直接在自己的包路径里找到它。
def _find_simconnect_dll():
    candidates = [
        os.path.join(_HERE, "dist", "SimConnect.dll"),
        os.path.join(_HERE, "SimConnect.dll"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    p = ctypes.util.find_library("SimConnect")
    if p and os.path.isfile(p):
        return os.path.abspath(p)
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{d}:\\"
        if not os.path.isdir(root):
            continue
        for sub in (
            os.path.join(root, "MSFS SDK"),
            os.path.join(root, "MSFS2024 SDK"),
            os.path.join(root, "Program Files", "MSFS SDK"),
        ):
            for lib in ("SimConnect.dll", os.path.join("lib", "SimConnect.dll")):
                p = os.path.join(sub, lib)
                if os.path.isfile(p):
                    return os.path.abspath(p)
    return None


_DLL = _find_simconnect_dll()
if _DLL:
    print("[build] Bundling SimConnect.dll:", _DLL)
    CMD.extend(["--add-data", f"{_DLL};SimConnect"])
else:
    print("[build] SimConnect.dll not found; runtime will need it next to the EXE.")

# 高德 Key 文件：本地存在才打包进 exe（已被 .gitignore 排除，切勿提交到仓库）。
# 云端 Release 构建时仓库不含此文件，会自动跳过 -> 仅用全球源。
_AMAP_KEY = os.path.join(_HERE, "amap_key.txt")
if os.path.isfile(_AMAP_KEY):
    print("[build] Bundling amap_key.txt (Amap key embedded for domestic geocoding)")
    CMD.extend(["--add-data", f"{_AMAP_KEY};."])
else:
    print("[build] amap_key.txt not found; Amap source disabled (global sources only).")

# 离线全球知名地名词典（公开数据，入库）：打包进 exe 供 geocode 离线解析。
_GAZ = os.path.join(_HERE, "world_famous.json")
if os.path.isfile(_GAZ):
    print("[build] Bundling world_famous.json (offline gazetteer)")
    CMD.extend(["--add-data", f"{_GAZ};."])
else:
    print("[build] world_famous.json not found; offline gazetteer disabled.")

# 内置离线卫星底图（z0~z5，由公开的 Esri World Imagery 瓦片合成）：随 EXE 分发。
# 启动那一屏（z5 总览）与低倍率平移因此完全不依赖网络，彻底消除黑块。
_BASEMAP = os.path.join(_HERE, "world_z5_sat.zip")
if os.path.isfile(_BASEMAP):
    print("[build] Bundling world_z5_sat.zip (offline z0-z5 satellite basemap, %.1fMB)"
          % (os.path.getsize(_BASEMAP) / 1048576))
    CMD.extend(["--add-data", f"{_BASEMAP};."])
else:
    print("[build] world_z5_sat.zip not found; low-zoom tiles will be fetched online.")

# 清理历史遗留的构建注入文件（语言已改为运行时按系统判定）
_legacy_lang = os.path.join(_HERE, "_build_lang.py")
if os.path.isfile(_legacy_lang):
    os.remove(_legacy_lang)
    print("[build] removed legacy _build_lang.py")

CMD.append(os.path.join(_HERE, "app.py"))

def _exe_locked(path):
    """输出 EXE 是否被占用：正在运行的程序会锁住自己的 exe，PyInstaller 删不掉它。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


if __name__ == "__main__":
    # 预检：旧 exe 正在运行 → PyInstaller 的 --noconfirm 清理会失败，
    # 报出来的却是一大段 safe-delete/traceback，很难看出真正原因。这里提前拦下。
    _out = os.path.join(os.path.abspath(_distpath), _NAME + ".exe")
    if _exe_locked(_out):
        print("")
        print("[build] !! 输出文件正被占用：%s" % _out)
        print("[build] !! 多半是本程序还在运行——请先关闭 FSRelocator.exe 再重新打包，")
        print("[build] !! 或先打到别的目录（成品是自包含的，可随时移入 dist_new）：")
        print("[build] !!     python build.py --distpath dist_v40")
        sys.exit(2)
    subprocess.run(CMD, check=True)
