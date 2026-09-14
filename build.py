"""打包成单文件 EXE（Windows）。

用法：
    python build.py                           # -> dist/MSFSTeleport.exe（中英双语单包）
    python build.py --distpath dist_new       # 输出到别的目录（绕开被占用的旧 EXE）

界面语言在运行时自动判定：中文系统 -> 中文界面，其他 -> 英文界面。
可用环境变量 MSFS_TELEPORT_LANG=zh/en 手动覆盖。

说明：
- 依赖 webview(pywebview) 的内嵌浏览器（Windows 自带 Edge WebView2，一般已就绪）。
- 运行时仍需本机装有 MSFS2024（提供 SimConnect.dll）。若系统找不到 SimConnect.dll，
  请把 MSFS SDK 里的 SimConnect.dll 复制到 EXE 同目录。
"""

import ctypes.util
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_NAME = "MSFSTeleport"

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

# 清理历史遗留的构建注入文件（语言已改为运行时按系统判定）
_legacy_lang = os.path.join(_HERE, "_build_lang.py")
if os.path.isfile(_legacy_lang):
    os.remove(_legacy_lang)
    print("[build] removed legacy _build_lang.py")

CMD.append(os.path.join(_HERE, "app.py"))

if __name__ == "__main__":
    subprocess.run(CMD, check=True)
