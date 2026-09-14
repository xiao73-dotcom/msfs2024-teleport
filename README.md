# MSFS2024 瞬移器 · MSFSTeleport

绕过《微软模拟飞行 2024》自由飞行"只能填机场四字代码"的限制：先进任意飞行，再用本工具把飞机瞬移到任意地点或经纬度。

界面支持中英双语，零外链、全本地运行。

---

## 下载

前往本仓库的 **Releases** 页面，下载最新 `MSFSTeleport-vX.Y.Z.exe`，双击即用，无需安装。

## 使用前准备

- 已安装 **Microsoft Flight Simulator 2024**，并**已进入一次飞行**（飞机已载入）
- Windows 10 / 11 自带的 **Edge WebView2**（内嵌界面依赖，一般系统已就绪）
- 发布版 EXE 已内嵌 `SimConnect.dll`；若运行提示找不到，可把 MSFS SDK 中的 `SimConnect.dll` 复制到 EXE 同目录

## 使用

1. 打开工具，自动连接 MSFS；状态点变绿即已连接
2. **地名**标签：输入中文或英文地点（如"哈尔滨中央大街""Eiffel Tower"），搜索后从候选点选一个
3. **经纬度**标签：直接填 `纬度,经度`，或分别填纬度 / 经度 / 海拔(米) / 朝向(°)
4. 点「瞬移至此」，飞机即被传送到目标坐标

### 常用功能

| 功能 | 说明 |
| --- | --- |
| 仅地图 | 收起侧栏，只留地图窗口，适合全屏铺在游戏上方 |
| 置顶显示 | 窗口始终浮在游戏画面之上 |
| 跟随飞机 | 地图自动居中到飞机当前位置 |
| 缩放随高度 | 缩放级别随飞行高度自动调整 |
| 搜索选点 | 选中候选地点后自动取消「跟随飞机」，保证目标居中 |

## 技术说明

- 界面：`pywebview`（Windows 上使用 Edge WebView2）
- 地景：多源瓦片（高德 / Esri / 腾讯），带本地磁盘缓存
- 地理编码：OpenStreetMap Nominatim（免费、支持中文、无需 API Key）
- 瞬移：通过 SimConnect `set_pos` 写入坐标。若游戏处于暂停态，先恢复运行再瞬移才生效
- 全部本地运行，无数据上传、无付费接口

## 从源码构建

```bash
pip install -r requirements.txt
pip install pyinstaller
python build.py                # -> dist/MSFSTeleport.exe
python build.py --distpath out # 输出到指定目录
```

开发调试直接运行：`python app.py`

## 自动发布

本仓库配置了 GitHub Actions（`.github/workflows/release.yml`）：

- 推送 `v*` 标签 → 云端 Windows 主机自动打包并发布 Release
- 也可在仓库网页 **Actions → Build & Release → Run workflow** 手动触发

版本号取自 `strings.py` 中的 `APP_VERSION`，发版时递增即可。

## 许可

[MIT](LICENSE)
