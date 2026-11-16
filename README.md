**## PSP Media Manager v1.0.5**

**一款为 **PSP-3000（6.61 PRO-C）** 做的媒体管理与视频转换工具，Windows 图形界面，基于 tkinter + ffmpeg。转换、预览、播放、管理一条龙，直接把抖音视频批量转成掌机能播的格式,并且竖屏视频旋转90度来适配psp小屏幕。竖着看更爽。**

<img width="1122" height="732" alt="003" src="https://github.com/user-attachments/assets/3cdd8612-11c2-4fa7-84f4-485b7db43cf6" />
<img width="1122" height="732" alt="004" src="https://github.com/user-attachments/assets/459693b9-e5db-44ad-9f1c-f7dfd6da0d79" />
<img width="1122" height="732" alt="006" src="https://github.com/user-attachments/assets/ec273024-280b-4e5d-a907-e182a0bd1c4b" />
<img width="1122" height="732" alt="007" src="https://github.com/user-attachments/assets/912235d4-e221-4f37-9a45-a95ffa916fed" />

这次添加更多功能。
- 记忆棒页成了真文件管理器：面包屑地址栏、双击 / 回车展开收起、右键复制剪切粘贴重命名删除备份、删除进回收站、内部剪贴板不 冲掉系统剪贴板
- 删除走 Windows 回收站,误删能找回
- TXT 能看也能改：自动判编码（UTF-8 BOM → GB 系 → 单字节），只读态拦输入键，保存按原编码原换行写回并先备份
- 存档能看了：识别 `PARAM.SFO` + `ICON0.PNG`，给出存档标题、编号、所属游戏、说明、文件清单
- EBOOT.PBP 解析：标题、编号、版本、年龄分级、各分区大小；APP/GAME 这类集合目录直接列「几项、编号、名字、EBOOT 大小」
- 固定名字的目录自动带备注（APP / GAME / SAVEDATA / PHOTO / PSN / THEME / PLUGIN 等）
- 表头排序、大小按真实字节排、音频 ID3 标签 + 缓存、图片直接铺画面区
- 7 种可视化特效保留,频谱条、上下镜像、环形频谱、示波波形、频带粒子、频谱隧道、瀑布地形。
- 安装包和便携包都自带ffmpeg（放在 `_internal\bin`），装完即用，不需要另外装 ffmpeg 或配环境变量。
---
|              文件                  |    体积  |                       说明                               |

| `PSP媒体管理器_v1.0.5_安装程序.exe` | 121.6 MB | 安装版，内置 ffmpeg 三件套，可选桌面图标，装完可勾选立即启动 |
| `PSP媒体管理器_v1.0.5_便携版.zip`   | 178.7 MB | 解压即用，不写注册表，整个 `PSP媒体管理器\` 目录拿走就能跑 |

### 下载安装

下载下方 `PSP._v1.0.3_.exe`，双击安装。默认装到 `%LOCALAPPDATA%\Programs\PSP媒体管理器`，无需管理员权限。

**首次启动请等约 10 秒再点第一条视频**：程序在后台预热 ffplay，预热完成后点视频就是瞬时响应（0.1 秒级）。

---

### 兼容性矩阵（PSP-3000 实测）

| 分辨率 | 可用编码 |
|---|---|
| 320×240 | MPEG-4 SP |
| 480×240 | H.264 Baseline / Main |
| 480×272 | H.264 Baseline / Main |
| 640×480 | H.264 Main |
| 720×480 | H.264 Main |
| 640×360 | 不兼容（任何编码均无法播放） |
| 640×204 | 不兼容（任何编码均无法播放） |

码率上限：视频 `3400 kbps`，音频 `128 kbps`。

### 硬规则

1. 视频必须放在 `<盘>:\VIDEO` 目录（或其子目录）下
2. `VIDEO` 累计条目数约 **1000～1024** 上限，超过掌机只显示一部分

