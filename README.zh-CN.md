# FS Relocator（FS瞬移者）

把你的飞机瞬移到微软模拟飞行（Microsoft Flight Simulator 2024 / 2020）中的**任意地点或坐标**。

自由飞行默认只能从机场（按 ICAO 代码）起飞；FS Relocator 解除这个限制——进游戏开个飞行，然后按地名或经纬度把飞机移动到任何你想去的地方。

界面为双语（中文 / 英文），会自动跟随模拟器的语言。完全本地运行，无账号、无外部调用、无付费 API。

---

## 下载

前往仓库的 **Releases** 页面，下载最新的 `FSRelocator-vX.Y.zip`（推荐）或 `FSRelocator-vX.Y.exe`。解压后双击 `FSRelocator.exe` 即可，无需安装。

## 使用前准备

- 已安装 Microsoft Flight Simulator 2024（或 2020），并且**至少进过一次飞行**（飞机已加载）。
- Windows 10 / 11 自带 **Edge WebView2**（内嵌界面依赖它，系统已自带）。
- 发布的 exe 已经打包了 `SimConnect.dll`。若偶尔看到“SimConnect.dll not found”，把 MSFS SDK 里的该 DLL 复制到 exe 同目录即可。

## 使用方法

1. 打开工具——它会自动连接 MSFS；连接成功时状态点变绿。
2. **搜索** 标签页：输入中文或英文地名（例如 `Eiffel Tower`、`哈尔滨中央大街`），搜索后从候选里选一个。
3. **目标** 标签页：直接输入 `纬度, 经度`，或分别填写纬度 / 经度 / 高度（米）/ 航向（度）。
4. 点击 **前往此位置**——飞机即被瞬移到目标点。

### 主要功能

| 功能 | 说明 |
| --- | --- |
| 仅地图 | 收起侧边栏，只留地图窗口——方便叠加在模拟器全屏画面上。 |
| 置顶显示 | 窗口悬浮在模拟器之上。 |
| 跟随飞机 | 地图自动居中到当前飞机位置。 |
| 随高度缩放 | 缩放级别随飞行高度自动调整。 |
| 搜索选点 | 选中地点会自动取消“跟随”，让目标保持居中。 |
| 北向上 / 机头朝上 | 切换地图朝向；罗盘随之一起转。 |
| 启动自动定位 | 启动时地图居中到飞机，并匹配所选朝向。 |

快捷键：`Ctrl+Alt+T` 瞬移到已锁定目标 · `Ctrl+Alt+E` 恢复操控 · `Ctrl+Alt+G` 把本窗口提到最前。

## 注意事项

- 发布的 exe **未做代码签名**。首次启动时 Windows SmartScreen 可能提示“Windows 已保护你的电脑”——点击 **更多信息 → 仍要运行**。
- 地图瓦片：Esri（全球）与 高德 / 腾讯（中国），会缓存在本地。另打包了一份离线卫星底图（缩放 0–5），保证低缩放级别下世界不会一片空白。
- 地理编码：内置离线地名词典，外加在线地理编码（Photon、Nominatim、ArcGIS；国内用高德）。
- 在中国境内，程序对国内地图源使用 GCJ-02 坐标，保证飞机位置与影像对齐。

## 从源码构建

```bash
pip install -r requirements.txt
pip install pyinstaller
python build.py                 # -> dist/FSRelocator.exe
python build.py --distpath out  # 输出到指定目录
```

开发时可直接 `python app.py` 运行。

## 自动发布

本仓库使用 GitHub Actions（`.github/workflows/release.yml`）：

- 推送 `v*` 标签即会在云端 Windows 运行器上构建并发布 Release。
- 也可手动触发：**Actions → Build & Release → Run workflow**。

版本号取自 `strings.py` 中的 `APP_VERSION`；发布前请先自增它。

## 反馈与支持

使用中遇到问题，或有建议？欢迎反馈——这个工具会根据真实使用不断完善。

- 在仓库 **开一个 Issue**（推荐）。
- 或发邮件到 **jerryxiao@msn.com**。

每一条反馈我都会看，并尽快修复。

## 许可证

[MIT](LICENSE)
