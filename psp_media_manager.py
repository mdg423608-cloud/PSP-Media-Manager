# -*- coding: utf-8 -*-
"""PSP 媒体管理器 —— 单文件 GUI 工具

功能
  1) 拖拽 / 添加媒体 → 视频转 PSP 兼容 MP4 + 同名 .THM 缩略图
     （5 档视频格式可选：480x272 / 720x480，H.264 Baseline/Main 或 MPEG-4 SP）
  2) 音频转 MP3（码率/采样率可选）；本身是 MP3 时可选择直接复制
  3) 记忆棒路径可选盘符也可选任意目录，卡内子目录：
     视频 默认 <根>/VIDEO/<原标题>.MP4（可在设置里切成 PSP 标准：AVC 进 <根>/MP_ROOT/100ANV01/MAQxxxxx.MP4、MPEG-4 进 <根>/MP_ROOT/100MNV01/M4Vxxxxx.MP4），
     音乐 <根>/MUSIC
  4) 临时目录暂存：先转到 <临时目录>\video / music，转完再搬进记忆棒（慢速卡不卡住）
  5) 读取抖音数据库（目录内须有 .appdata），按作者名字分类浏览、批量入队
  6) 三页同窗切换：PSP 记忆棒 / 添加媒体（队列）/ DY 数据库，选中文件右侧看详情

打包：build_exe.ps1 / 见 README
"""

import atexit
import ctypes
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import zlib
import queue as pyqueue

APP_NAME = "PSP 媒体管理器"
CONFIG_NAME = "psp_manager_config.json"

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".flv", ".ts", ".webm",
              ".wmv", ".3gp", ".m2ts", ".mpg", ".mpeg", ".vob", ".rmvb"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus",
              ".wma", ".aiff", ".aif", ".amr", ".ape", ".wv", ".mp2", ".ac3"}

W, H = 480, 272
THUMB_W, THUMB_H = 160, 120
FPS = 30
VBITRATE, VMAXRATE, VBUF = "768k", "1000k", "1500k"
ABITRATE, ASAMPLE = "128k", "44100"
LEVEL = "3.0"
NAME_MAX = 40

ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')

# ----------------------------------------------------------------- 视频档位
# PSP 支持 H.264(MPEG-4 AVC) Main/Baseline Level 3 与 MPEG-4 Simple Profile，
# 分辨率到 720x480。这里给出几个实测可播的档位。

# 实测定档（6.61 Pro-C，5 分辨率 × 3 编码全矩阵真机跑过）：
#   能播：480×272 / 480×240（H.264 Baseline 与 Main 都行）、720×480（只认 Main，Baseline 反而
#         判不兼容）、320×240（MPEG-4 唯一能真解码的尺寸）
#   判「不兼容的数据」：640×360、640×204——任何编码都进不去，是分辨率不在白盒列表里，与编码无关
#   MPEG-4 在 480×272 / 480×240 是「能显示但播放错误」，只有 320×240 可用
XP_BASE = "force-cfr=1:aud=1:cabac=0:8x8dct=0:weightp=0:bframes=0:ref=1"
XP_MAIN = "force-cfr=1:aud=1"

VPROFILES = {
    "psp_std": {"label": "480×272 · H.264 Baseline L3.0 · 1200k（实机通过，推荐）",
                "w": 480, "h": 272, "codec": "h264", "profile": "baseline",
                "level": "3.0", "refs": "1", "bitrate": "1200k",
                "maxrate": "1400k", "bufsize": "1600k", "xparams": XP_BASE},
    "psp_hq": {"label": "480×272 · H.264 Baseline L3.0 · 2400k（高码率，最清晰）",
               "w": 480, "h": 272, "codec": "h264", "profile": "baseline",
               "level": "3.0", "refs": "1", "bitrate": "2400k",
               "maxrate": "2600k", "bufsize": "3200k", "xparams": XP_BASE},
    "psp_full": {"label": "720×480 · H.264 Main L3.0 · 2400k（4:3 满屏，只认 Main）",
                 "w": 720, "h": 480, "codec": "h264", "profile": "main",
                 "level": "3.0", "refs": "1", "bitrate": "2400k",
                 "maxrate": "2600k", "bufsize": "3200k", "xparams": XP_MAIN},
    "psp_640": {"label": "640×480 · H.264 Main L3.0 · 2400k（4:3 满屏，省点码率）",
                "w": 640, "h": 480, "codec": "h264", "profile": "main",
                "level": "3.0", "refs": "1", "bitrate": "2400k",
                "maxrate": "2600k", "bufsize": "3200k", "xparams": XP_MAIN},
    "psp_mpeg4": {"label": "320×240 · MPEG-4 SP L1 · 900k（已到编码上限，再高无意义）",
                  "w": 320, "h": 240, "codec": "mpeg4", "profile": "simple",
                  "level": "1", "refs": None, "bitrate": "900k",
                  "maxrate": "1000k", "bufsize": "1200k"},
    "psp_mpeg4_sd": {"label": "320×240 · MPEG-4 SP L1 · 450k（最省空间）",
                     "w": 320, "h": 240, "codec": "mpeg4", "profile": "simple",
                     "level": "1", "refs": None, "bitrate": "450k",
                     "maxrate": "500k", "bufsize": "600k"},
}

VP_ORDER = [("psp_std", VPROFILES["psp_std"]), ("psp_hq", VPROFILES["psp_hq"]),
            ("psp_full", VPROFILES["psp_full"]), ("psp_640", VPROFILES["psp_640"]),
            ("psp_mpeg4", VPROFILES["psp_mpeg4"]),
            ("psp_mpeg4_sd", VPROFILES["psp_mpeg4_sd"])]

# ---- 设置页用：左侧选「编码格式（分辨率+Profile）」，右侧选码率
# 分工是固定的，界面不再暴露任意组合，避免拼出实测播不了的规格。
VBASES = {
    "480x272_h264b": {"label": "480×272 · H.264 Baseline",
                      "w": 480, "h": 272, "codec": "h264",
                      "profile": "baseline", "level": "3.0", "refs": "1",
                      "xparams": XP_BASE,
                      "rates": [600, 800, 1000, 1200, 1500, 1800, 2400],
                      "note": "16:9 主流尺寸，实机通过"},
    "480x272_h264m": {"label": "480×272 · H.264 Main",
                      "w": 480, "h": 272, "codec": "h264",
                      "profile": "main", "level": "3.0", "refs": "1",
                      "xparams": XP_MAIN,
                      "rates": [600, 800, 1000, 1200, 1500, 1800, 2400],
                      "note": "16:9 主流尺寸，实机通过（宽 ≤480 两种 Profile 都认）"},
    "480x240_h264b": {"label": "480×240 · H.264 Baseline",
                      "w": 480, "h": 240, "codec": "h264",
                      "profile": "baseline", "level": "3.0", "refs": "1",
                      "xparams": XP_BASE,
                      "rates": [600, 800, 1000, 1200, 1500, 1800, 2400],
                      "note": "2:1 矮屏，上下带黑边，实机通过"},
    "480x240_h264m": {"label": "480×240 · H.264 Main",
                      "w": 480, "h": 240, "codec": "h264",
                      "profile": "main", "level": "3.0", "refs": "1",
                      "xparams": XP_MAIN,
                      "rates": [600, 800, 1000, 1200, 1500, 1800, 2400],
                      "note": "2:1 矮屏，上下带黑边，实机通过"},
    "720x480_h264m": {"label": "720×480 · H.264 Main",
                      "w": 720, "h": 480, "codec": "h264",
                      "profile": "main", "level": "3.0", "refs": "1",
                      "xparams": XP_MAIN,
                      "rates": [800, 1000, 1200, 1500, 1800, 2100, 2400, 2700, 3000],
                      "note": "4:3 满屏，只认 Main"},
    "640x480_h264m": {"label": "640×480 · H.264 Main",
                      "w": 640, "h": 480, "codec": "h264",
                      "profile": "main", "level": "3.0", "refs": "1",
                      "xparams": XP_MAIN,
                      "rates": [800, 1000, 1200, 1500, 1800, 2100, 2400, 2700, 3000],
                      "note": "4:3 满屏，同码率更省像素"},
    "320x240_mpeg4": {"label": "320×240 · MPEG-4 SP",
                      "w": 320, "h": 240, "codec": "mpeg4",
                      "profile": "simple", "level": "1", "refs": None,
                      "rates": [250, 320, 400, 450, 500, 600, 700, 800],
                      "note": "MPEG-4 唯一能解的尺寸，800k 已是编码上限"},
}
VBASE_ORDER = ["480x272_h264b", "480x272_h264m", "480x240_h264b",
               "480x240_h264m", "720x480_h264m", "640x480_h264m",
               "320x240_mpeg4"]

# 老配置只存了 vprofile 档位名，这里映射回「基座 + 码率」
LEGACY_BASE = {
    "psp_std": ("480x272_h264b", 1200),
    "psp_hq": ("480x272_h264b", 2400),
    "psp_full": ("720x480_h264m", 2400),
    "psp_640": ("640x480_h264m", 2400),
    "psp_mpeg4": ("320x240_mpeg4", 800),
    "psp_mpeg4_sd": ("320x240_mpeg4", 450),
}


def make_profile(base_key, kbps):
    """把「编码基座 + 码率」合成一个可直接丢给 convert_video 的档位字典"""
    b = VBASES.get(base_key) or VBASES["480x272_h264b"]
    p = {k: v for k, v in b.items() if k != "rates"}
    try:
        kbps = int(kbps or 1200)
    except Exception:
        kbps = 1200
    if kbps not in b["rates"]:           # 落在档位表外就吸到最近的一档
        kbps = min(b["rates"], key=lambda x: abs(x - kbps))
    p["kbps"] = kbps
    p["base"] = base_key
    p["bitrate"] = "%dk" % kbps
    p["maxrate"] = "%dk" % int(round(kbps * 1.15))
    p["bufsize"] = "%dk" % int(round(kbps * 4.0 / 3.0))
    p["label"] = "%s · %dk" % (b["label"], kbps)
    return p


def resolve_profile(cfg):
    """从配置里解出当前档位；老配置（只有 vprofile）自动映射到新结构"""
    base = (cfg.get("vbase") or "").strip()
    if base in VBASES:
        return make_profile(base, cfg.get("vrate"))
    legacy = cfg.get("vprofile") or ""
    if legacy in LEGACY_BASE:
        return make_profile(*LEGACY_BASE[legacy])
    return make_profile("480x272_h264b", 1200)

# 音频输出格式：mp3 是 PSP 原生支持的，wma 要在 PSP 系统设置里开「WMA 播放」，wav 纯给电脑用
AUDIO_FMTS = {
    "mp3": {"label": "MP3（PSP 原生支持，最稳）", "ext": ".mp3"},
    "wma": {"label": "WMA（PSP 需在系统设置里开启「WMA 播放」）", "ext": ".wma"},
    "wav": {"label": "WAV（无损 PCM，体积大，PSP 播不了）", "ext": ".wav"},
}
AUDIO_ORDER = ["mp3", "wma", "wav"]


# ----------------------------------------------------------------- 基础工具

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def res_dir():
    return getattr(sys, "_MEIPASS", app_dir())


def find_tool(name):
    """找 ffmpeg / ffprobe：打包内置 → exe 同目录 bin → 本机常见路径"""
    base = name + (".exe" if os.name == "nt" else "")
    cands = [
        os.path.join(res_dir(), "bin", base),
        os.path.join(res_dir(), base),
        os.path.join(app_dir(), "bin", base),
        os.path.join(app_dir(), base),
    ]
    temp = os.environ.get("TEMP", "")
    if temp:
        cands.append(os.path.join(temp, "ffbox", "x",
                                  "ffmpeg-9.0.2-essentials_build", "bin", base))
    for p in cands:
        if os.path.isfile(p):
            return p
    return shutil.which(base)


def safe_name(s, limit=NAME_MAX):
    s = ILLEGAL.sub(" ", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        return ""
    if len(s) > limit:
        cut = s[:limit]
        for sep in ("，", "。", "！", "？", "、", "；", ",", ".", "!", "?", ";", " #"):
            i = cut.rfind(sep)
            if i >= limit // 2:
                cut = cut[:i]
                break
        s = cut.strip(" .，。！？、；,#")
    return s or "无标题"


def no_window():
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW
    return 0


_FFPLAY_WARMED = False
_THUMB_BUSY = False
_MEDIA_INFO_CACHE = {}


def warm_ffplay():
    """ffplay 是个 100 MB 的独立程序，实测头三次启动每次要两秒多（冷读+系统扫描），
    之后就降到二十几毫秒。程序刚起来时先在后台替它热几遍，
    用户点第一条视频时就不用再等那两三秒。交给 cmd 去跑，主进程不等它。"""
    global _FFPLAY_WARMED
    if _FFPLAY_WARMED or os.name != "nt":
        return
    ff = find_tool("ffplay")
    if not ff:
        return
    _FFPLAY_WARMED = True
    try:
        q = '"%s" -version >nul 2>nul' % ff
        line = "for /l %%i in (1,1,4) do %s" % q
        subprocess.Popen(["cmd", "/c", line], creationflags=no_window(),
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------- 音乐可视化（沿用 2026-11-07 那个可视化程序的配色与规格）
AURORA = ((0, 229, 255), (77, 124, 255), (184, 77, 255), (255, 61, 174))
_SPRITES = {}


def _pal(t):
    """极光配色取色：0 = 青，1 = 品红（原程序 PALETTES[0]）"""
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    stops = AURORA
    n = len(stops) - 1
    x = t * n
    i = min(int(x), n - 1)
    f = x - i
    a, c = stops[i], stops[i + 1]
    return (int(a[0] + (c[0] - a[0]) * f),
            int(a[1] + (c[1] - a[1]) * f),
            int(a[2] + (c[2] - a[2]) * f))


def _bar_sprite(w, h):
    """一根柱的竖直渐变（顶亮、底暗），按柱高裁一段贴上去"""
    from PIL import Image, ImageDraw
    key = ("bar", w, h)
    im = _SPRITES.get(key)
    if im is not None:
        return im
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        u = y / float(max(1, h - 1))          # 0 = 柱顶
        if u < 0.35:
            c = _pal(1.0 - 0.26 * (u / 0.35))
        elif u < 0.78:
            c = _pal(0.74 - 0.40 * ((u - 0.35) / 0.43))
        else:
            c = _pal(0.34 - 0.29 * ((u - 0.78) / 0.22))
        d.line([(0, y), (w - 1, y)], fill=c)
    if len(_SPRITES) > 24:
        _SPRITES.clear()
    _SPRITES[key] = im
    return im


def _refl_sprite(w, h):
    """柱下的倒影：越往下越透明"""
    from PIL import Image, ImageDraw
    key = ("refl", w, h)
    im = _SPRITES.get(key)
    if im is not None:
        return im
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for y in range(h):
        u = y / float(max(1, h - 1))
        c = _pal(0.5 if u < 0.5 else 0.35)
        a = 78 if u < 0.5 else int(78 - 156 * ((u - 0.5) / 0.5))
        d.line([(0, y), (w - 1, y)], fill=(c[0], c[1], c[2], max(0, a)))
    if len(_SPRITES) > 24:
        _SPRITES.clear()
    _SPRITES[key] = im
    return im


def _glow_sprite(w, h, col=(0, 150, 220), peak=26):
    """中心径向辉光（原程序那种中心亮到边缘透明的软光斑），带缓存"""
    from PIL import Image
    import numpy as np
    key = ("glow", w, h, col, peak)
    im = _SPRITES.get(key)
    if im is not None:
        return im
    yy, xx = np.mgrid[0:h, 0:w]
    rad = max(w, h) * 0.52
    r = np.sqrt((xx - w * 0.5) ** 2 + (yy - h * 0.52) ** 2) / rad
    a = (np.clip(1.0 - r, 0.0, 1.0) ** 1.9 * peak).astype(np.uint8)
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 0] = col[0]
    arr[..., 1] = col[1]
    arr[..., 2] = col[2]
    arr[..., 3] = a
    im = Image.fromarray(arr, "RGBA")
    if len(_SPRITES) > 24:
        _SPRITES.clear()
    _SPRITES[key] = im
    return im


def _mirror_sprite(w, h):
    """上下镜像用的对称竖条：中间最亮、两端收黑（原程序 mirror 的渐变停靠）"""
    from PIL import Image, ImageDraw
    key = ("mirror", w, h)
    im = _SPRITES.get(key)
    if im is not None:
        return im
    im = Image.new("RGB", (w, max(4, h)))
    d = ImageDraw.Draw(im)
    hh = max(4, h)
    for y in range(hh):
        u = y / float(hh - 1)
        if u < 0.28:
            c = _pal(0.06 + (0.45 - 0.06) * (u / 0.28))
        elif u < 0.5:
            c = _pal(0.45 + (0.98 - 0.45) * ((u - 0.28) / 0.22))
        elif u < 0.72:
            c = _pal(0.98 - (0.98 - 0.45) * ((u - 0.5) / 0.22))
        else:
            c = _pal(0.45 - (0.45 - 0.06) * ((u - 0.72) / 0.28))
        d.line([(0, y), (w - 1, y)], fill=c)
    if len(_SPRITES) > 24:
        _SPRITES.clear()
    _SPRITES[key] = im
    return im


def _vignette_sprite(w, h):
    """暗角：边缘压暗，把视线收到柱区"""
    from PIL import Image
    import numpy as np
    yy, xx = np.mgrid[0:h, 0:w]
    rad = max(w, h) * 0.78
    r = np.sqrt((xx - w * 0.5) ** 2 + (yy - h * 0.5) ** 2) / rad
    a = (np.clip((r - 0.62) / 0.38, 0.0, 1.0) ** 1.4 * 168).astype(np.uint8)
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 3] = a
    return Image.fromarray(arr, "RGBA")


def _spectrogram(raw, sr=8000, n_bands=56):
    """PCM → (帧 × 频带) 的 0..1 能量矩阵；对数频带 + 每带取峰值 dB，跟可视化程序一套口径"""
    import numpy as np
    if not raw:
        return None, 0.08
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n_fft = 512
    if x.size < n_fft:
        return None, 0.08
    hop = 256
    if (x.size - n_fft) // hop > 12000:       # 长音频放宽步长，别把内存吃满
        hop = int((x.size - n_fft) / 12000) + 1
    nfr = 1 + (x.size - n_fft) // hop
    win = np.hanning(n_fft).astype(np.float32)
    idx = np.arange(n_fft)[None, :] + (np.arange(nfr) * hop)[:, None]
    mag = np.abs(np.fft.rfft(x[idx] * win, axis=1)) / (n_fft * 0.5)
    db = 20.0 * np.log10(mag + 1e-7)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    edges = np.geomspace(45.0, min(3600.0, sr / 2.0 * 0.95), n_bands + 1)
    eidx = np.searchsorted(freqs, edges)
    out = np.empty((nfr, n_bands), dtype=np.float32)
    for i in range(n_bands):
        a = int(eidx[i])
        b = int(eidx[i + 1])
        if b <= a:
            b = a + 1
        if a >= db.shape[1]:
            out[:, i] = -100.0
        else:
            out[:, i] = db[:, a:min(b, db.shape[1])].max(axis=1)
    norm = np.clip((out + 70.0) / 50.0, 0.0, 1.0) ** 1.6
    return norm.astype(np.float32), hop / float(sr)


# ffmpeg / ffplay 是独立进程，主程序退出不会带走它们，所以统一登记，退出时兜底清干净
_ALL_PROCS = []
_PROCS_LOCK = threading.Lock()
_JOB = [None]


def _job_handle():
    """建一个 Windows Job 对象，设成“句柄关闭即杀子进程”，把 ffmpeg/ffplay 挂进去。

    这样即使程序被任务管理器强杀、或崩溃在 atexit 之前，子进程也活不下来。
    """
    if _JOB[0] is not None:
        return _JOB[0]
    if os.name != "nt":
        return None
    try:
        import ctypes.wintypes as wt

        class _BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wt.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]

        class _IO(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                        ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64),
                        ("ReadTransferCount", ctypes.c_uint64),
                        ("WriteTransferCount", ctypes.c_uint64),
                        ("OtherTransferCount", ctypes.c_uint64)]

        class _EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC),
                        ("IoInfo", _IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.windll.kernel32
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObject failed")
        info = _EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                    ctypes.sizeof(info))
        _JOB[0] = job
        return job
    except Exception:
        _JOB[0] = None
        return None


def reg_proc(p):
    try:
        with _PROCS_LOCK:
            _ALL_PROCS.append(p)
    except Exception:
        pass
    try:
        job = _job_handle()
        if job:
            ctypes.windll.kernel32.AssignProcessToJobObject(job, int(p._handle))
    except Exception:
        pass
    return p


def _terminate_proc(p, wait=0.8):
    """先 terminate 给 ffmpeg 收尾机会，超时还没退就 kill"""
    try:
        if p.poll() is not None:
            return
    except Exception:
        return
    try:
        p.terminate()
    except Exception:
        pass
    try:
        p.wait(timeout=wait)
        return
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass
    try:
        p.wait(timeout=0.5)
    except Exception:
        pass


def kill_all_procs():
    with _PROCS_LOCK:
        lst = [_p for _p in _ALL_PROCS if _p is not None]
        del _ALL_PROCS[:]
    for p in lst:
        _terminate_proc(p, wait=0.2)


atexit.register(kill_all_procs)


def run(cmd, timeout=None):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="ignore", creationflags=no_window(), timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def read_ini_text(path):
    raw = open(path, "rb").read()
    if raw[:2] == b"\xff\xfe":
        cands = ["utf-16"]
    elif raw[:3] == b"\xef\xbb\xbf":
        cands = ["utf-8-sig"]
    else:
        cands = ["gbk", "utf-8"]
    for enc in cands:
        try:
            txt = raw.decode(enc)
        except Exception:
            continue
        if "LocalizedResourceName" in txt or "ShellClassInfo" in txt:
            return txt
    return ""


def read_ini_name(folder):
    ini = os.path.join(folder, "desktop.ini")
    if os.path.isfile(ini):
        try:
            txt = read_ini_text(ini)
        except Exception:
            txt = ""
        if txt:
            m = re.search(r"LocalizedResourceName\s*=\s*(.*)", txt)
            if m and m.group(1).strip():
                return safe_name(m.group(1).strip().lstrip("_").strip(), 60)
    return ""


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


class _WinVol(object):
    """直接改 Windows 音频会话的音量/静音（ISimpleAudioVolume）

    ffplay 没有运行时调音量的接口，以前只能杀掉重启，于是拖一次音量就重播一次、
    和画面错位。这里按 pid 找到 ffplay 的音频会话，实时改音量，播放一点都不打断。
    索引是实测出来的：Manager2 的 GetSessionEnumerator=5，Control2 的 GetProcessId=14。
    """

    def __init__(self):
        self.ok = None
        self.pid = None
        self._mgr = None
        self._sav = None
        self._mod = None
        self._guid = {}

    # ---- 底层：一次取函数指针再调用
    def _load(self):
        if self.ok is not None:
            return self.ok
        try:
            import ctypes
            from ctypes import (POINTER, Structure, WINFUNCTYPE, byref, c_byte,
                                c_float, c_int, c_long, c_ulong, c_ushort,
                                c_void_p)
            self._ct = ctypes
            self._t = dict(POINTER=POINTER, Structure=Structure, WINFUNCTYPE=WINFUNCTYPE,
                           byref=byref, c_byte=c_byte, c_float=c_float, c_int=c_int,
                           c_long=c_long, c_ulong=c_ulong, c_ushort=c_ushort,
                           c_void_p=c_void_p)

            class GUID(Structure):
                _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                            ("Data3", c_ushort), ("Data4", c_byte * 8)]

            self._GUID = GUID
            self._ole = ctypes.windll.ole32

            def mk(s):
                g = GUID()
                if self._ole.CLSIDFromString(ctypes.c_wchar_p(s), byref(g)) != 0:
                    raise OSError(s)
                return g

            clsid_dev = mk("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
            iid_enum = mk("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            iid_mgr = mk("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
            self._iid_sav = mk("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
            self._guid.update(dev=clsid_dev, enum=iid_enum, mgr=iid_mgr)
            self._ctypes = ctypes
            self.ok = True
        except Exception:
            self.ok = False
        return self.ok

    def _fn(self, ptr, idx, *argtypes):
        t = self._ctypes.cast(ptr, self._t["POINTER"](
            self._t["POINTER"](self._t["c_void_p"])))[0]
        return self._t["WINFUNCTYPE"](self._t["c_long"], self._t["c_void_p"],
                                      *argtypes)(t[idx])

    def _init_mgr(self):
        if self._mgr:
            return True
        if not self._load():
            return False
        try:
            ct = self._ctypes
            try:
                self._ole.CoInitialize(None)
            except Exception:
                pass
            p = ct.c_void_p()
            hr = self._ole.CoCreateInstance(ct.byref(self._guid["dev"]), None, 23,
                                            ct.byref(self._guid["enum"]), ct.byref(p))
            if hr < 0:
                return False
            dev = ct.c_void_p()
            if self._fn(p, 4, ct.c_int, ct.c_int, ct.POINTER(ct.c_void_p))(
                    p, 0, 0, ct.byref(dev)) < 0:
                return False
            mgr = ct.c_void_p()
            if self._fn(dev, 3, ct.POINTER(self._GUID), ct.c_ulong, ct.c_void_p,
                        ct.POINTER(ct.c_void_p))(
                    dev, ct.byref(self._guid["mgr"]), 23, None, ct.byref(mgr)) < 0:
                return False
            self._mgr = mgr
            return True
        except Exception:
            self.ok = False
            return False

    def bind(self, pid):
        """按 pid 找到该进程的音量会话；成功返回 True"""
        if not pid:
            return False
        if self.pid == pid and self._sav:
            return True
        if not self._init_mgr():
            return False
        ct = self._ctypes
        try:
            enum = ct.c_void_p()
            if self._fn(self._mgr, 5, ct.POINTER(ct.c_void_p))(
                    self._mgr, ct.byref(enum)) < 0:
                return False
            n = ct.c_int(0)
            self._fn(enum, 3, ct.POINTER(ct.c_int))(enum, ct.byref(n))
            for i in range(max(0, n.value)):
                ctl = ct.c_void_p()
                if self._fn(enum, 4, ct.c_int, ct.POINTER(ct.c_void_p))(
                        enum, i, ct.byref(ctl)) < 0:
                    continue
                q = ct.c_ulong(0)
                if self._fn(ctl, 14, ct.POINTER(ct.c_ulong))(ctl, ct.byref(q)) < 0:
                    continue
                if q.value != pid:
                    continue
                sav = ct.c_void_p()
                if self._fn(ctl, 0, ct.POINTER(self._GUID), ct.POINTER(ct.c_void_p))(
                        ctl, ct.byref(self._iid_sav), ct.byref(sav)) == 0:
                    self._sav = sav
                    self.pid = pid
                    return True
        except Exception:
            pass
        return False

    def set_volume(self, frac):
        if not self._sav:
            return False
        frac = max(0.0, min(1.0, float(frac)))
        try:
            ct = self._ctypes
            return self._fn(self._sav, 3, ct.c_float, ct.POINTER(self._GUID))(
                self._sav, ct.c_float(frac), None) == 0
        except Exception:
            return False

    def set_mute(self, flag):
        if not self._sav:
            return False
        try:
            ct = self._ctypes
            return self._fn(self._sav, 5, ct.c_int, ct.POINTER(self._GUID))(
                self._sav, 1 if flag else 0, None) == 0
        except Exception:
            return False

    def get_volume(self):
        if not self._sav:
            return None
        try:
            ct = self._ctypes
            f = ct.c_float(-1)
            self._fn(self._sav, 4, ct.POINTER(ct.c_float))(self._sav, ct.byref(f))
            return f.value
        except Exception:
            return None

    def forget(self):
        self.pid = None
        self._sav = None


WINVOL = _WinVol()


def _atempo_chain(speed):
    """ffplay 的 atempo 只吃 0.5~2.0，超出范围就串几级，保证音画同速"""
    s = max(0.25, min(3.0, float(speed or 1.0)))
    parts = []
    while s > 2.0 + 1e-6:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5 - 1e-6:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append("atempo=%.4f" % s)
    return ",".join(parts)


def _fmt_time(sec):
    """把秒数写成 00:00（超过一小时写成 1:02:03）"""
    try:
        sec = max(0, int(round(float(sec or 0))))
    except Exception:
        sec = 0
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


# ----------------------------------------------------------------- 媒体探测

def probe(ffprobe, path):
    rc, out = run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                   "-of", "json", path])
    try:
        return json.loads(out[out.find("{"): out.rfind("}") + 1])
    except Exception:
        return {}


def analyse(info):
    """返回 (宽, 高, 音频流数, 旋转角度, 时长, 是否横屏显示)"""
    w = h = 0
    n_audio = 0
    rot = 0
    dur = 0.0
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and not w:
            w = int(s.get("width") or 0)
            h = int(s.get("height") or 0)
            for sd in s.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rot = int(float(sd["rotation"]))
            tags = s.get("tags") or {}
            if not rot and tags.get("rotate"):
                try:
                    rot = int(float(tags["rotate"]))
                except Exception:
                    pass
        elif s.get("codec_type") == "audio":
            n_audio += 1
    fmt = info.get("format") or {}
    try:
        dur = float(fmt.get("duration") or 0)
    except Exception:
        dur = 0.0
    rotated = abs(rot) % 180 == 90
    portrait = (h > w) != rotated
    return w, h, n_audio, rot, dur, portrait


# ----------------------------------------------------------------- 转换核心

def video_filter(portrait, w, h, fps=None):
    parts = []
    if portrait:
        parts.append("transpose=2")
    parts.append("scale=%d:%d:force_original_aspect_ratio=increase:flags=bicubic"
                 % (w, h))
    parts.append("crop=%d:%d" % (w, h))
    parts.append("setsar=1")
    if fps:
        parts.append("fps=%d" % fps)
    return ",".join(parts)


CURRENT = {"proc": None}


def kill_current():
    """停止要立刻见效：并行转码时可能同时有多个 ffmpeg 在跑，全杀掉"""
    procs = []
    cur = CURRENT.get("proc")
    if cur is not None:
        procs.append(cur)
    try:
        with _PROCS_LOCK:
            procs += [p for p in _ALL_PROCS if p.poll() is None]
    except Exception:
        pass
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


def convert_video(ffmpeg, src, out_mp4, dur, portrait, prof_key="psp_std", on_progress=None,
                  has_audio=True, prof=None):
    if prof is None:
        # 兼容两种调用：传档位 dict（新，基座+码率），或传 psp_std 这类档位名（老）
        prof = prof_key if isinstance(prof_key, dict) else (
            VPROFILES.get(prof_key) or VPROFILES["psp_std"])
    vf = video_filter(portrait, prof["w"], prof["h"], FPS)
    cmd = [ffmpeg, "-y", "-noautorotate", "-i", src]
    if not has_audio:
        # psp muxer 要求「一路视频 + 一路音频」，无声素材得补条静音，否则连头都写不出来
        cmd += ["-f", "lavfi"]
        if dur > 0:
            cmd += ["-t", "%.3f" % (dur + 0.2)]
        cmd += ["-i", "anullsrc=r=%s:cl=stereo" % ASAMPLE]
    cmd += ["-vf", vf]
    if prof["codec"] == "h264":
        cmd += ["-c:v", "libx264", "-profile:v", prof["profile"], "-level:v", prof["level"],
                "-refs", prof["refs"], "-bf", "0", "-pix_fmt", "yuv420p",
                "-preset", "veryfast",
                "-b:v", prof["bitrate"], "-maxrate", prof["maxrate"],
                "-bufsize", prof["bufsize"],
                "-g", str(FPS), "-keyint_min", str(FPS), "-sc_threshold", "0",
                # aud=1：每个访问单元前插 AUD 分隔符，PSP 硬解码器靠它切帧，
                # 少了这个同步头掌机容易整段判成不兼容。
                # Baseline 档把 8x8dct/weightp/bframes/ref 全钉死（veryfast 会开 weightp，
                # Baseline 本就不允许）；Main 档（720×480 必须）只关 B 帧，
                # 因为实测「真 Main」才过——特性关光了 x264 会把 SPS 降级成 Constrained
                # Baseline，720×480 反而不认。
                "-x264-params", prof.get("xparams") or XP_BASE]
    else:
        # mpeg4 编码器自身没有 profile 选项：不启用 B 帧/高级工具即为 Simple Profile
        cmd += ["-c:v", "mpeg4", "-vtag", "mp4v", "-bf", "0",
                "-pix_fmt", "yuv420p", "-b:v", prof["bitrate"],
                "-maxrate", prof["maxrate"], "-bufsize", prof["bufsize"],
                "-g", str(FPS)]
        # 分辨率与 level 必须对得上：480×272 标 L1 会被掌机判不兼容，得标 L3
        if prof.get("level"):
            cmd += ["-level:v", str(prof["level"])]
    cmd += ["-c:a", "aac", "-b:a", ABITRATE, "-ar", ASAMPLE, "-ac", "2"]
    if not has_audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    # PSP 的 MP4 解析器很挑：源视频带的 comment/title 之类的元数据会被 mov
    # muxer 写成 XMP uuid box 塞进 ftyp 与 moov，掌机见到未知 box 就报
    # 「不兼容的数据」。这里把元数据全部清掉、并让 muxer 走 bitexact 不写
    # encoder 标签，保证产出只有 ftyp / moov / mdat 这几样。
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-f", "psp", "-movflags", "+faststart",
            "-progress", "pipe:1", "-loglevel", "error", out_mp4]
    proc = reg_proc(subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8",
                                     errors="ignore",
                                     creationflags=no_window()))
    CURRENT["proc"] = proc
    last = 0.0
    t0 = time.time()
    while True:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        k, _, v = line.strip().partition("=")
        if k == "out_time_ms" and dur > 0:
            try:
                cur = int(v) / 1000000.0
            except Exception:
                continue
            if on_progress and cur - last > 0.4:
                last = cur
                on_progress(min(0.99, cur / dur), "%.0fs/%.0fs" % (cur, dur))
        elif k == "speed" and on_progress:
            pass
        if time.time() - t0 > 60 * 60:
            proc.kill()
            break
    err = proc.stderr.read() if proc.stderr else ""
    proc.wait()
    CURRENT["proc"] = None
    return proc.returncode, err


def make_thm(ffmpeg, src, out_thm, dur, portrait, from_mp4=None):
    """多时间点取帧，取体积最大的一张（避开黑场/纯色首帧），临时文件全部清理

    有转好的 480p 产物就直接从它取帧：4K 源取一帧要解 1 秒多，产物只要几十毫秒。
    """
    if from_mp4 and os.path.isfile(from_mp4):
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase:flags=bicubic"
              ",crop=%d:%d,setsar=1" % (THUMB_W, THUMB_H, THUMB_W, THUMB_H))
        pick_src = from_mp4
        portrait = False            # 产物已经是摆正的横屏
    else:
        vf = video_filter(portrait, THUMB_W, THUMB_H)
        pick_src = src
    points = [dur * 0.2 if dur else 1.0, dur * 0.5 if dur else 3.0,
              dur * 0.8 if dur else 5.0, dur * 0.05 if dur else 0.5]
    cands = []
    for k, t in enumerate(points):
        if t is None or t < 0:
            continue
        tmp = "%s.tmp%d.jpg" % (os.path.splitext(out_thm)[0], k)
        rc, _ = run([ffmpeg, "-y", "-noautorotate", "-ss", "%.2f" % t, "-i", pick_src,
                     "-frames:v", "1", "-vf", vf, "-q:v", "4", tmp])
        if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            cands.append((os.path.getsize(tmp), tmp))
        elif os.path.isfile(tmp):
            os.remove(tmp)
    best = None
    if cands:
        best = max(cands, key=lambda x: x[0])
        shutil.move(best[1], out_thm)
    for size, p in cands:
        if p != (best[1] if best else None) and os.path.isfile(p):
            try:
                os.remove(p)
            except Exception:
                pass
    return (True, best[0]) if best else (False, 0)


def thm_header_ok(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\xff\xd8\xff\xe0"
    except Exception:
        return False


# PSP 固件的实际能力（真机实测出来的，不是官方标称值）
# 官方规格写的视频 768k 早被实测推翻：1200k / 2400k 都能正常播放，
# 所以这里按实测上限放宽，别让自检把能播的高码率误报成不合规。
PSP_MAX_VBPS = 3400 * 1024        # 视频码率自检上限（实测 2400k 稳定，3000k 档留余量）
PSP_MAX_ABPS = 128 * 1024         # 音频码率上限 128kbps


def flush_to_disk(path):
    """把文件真正刷到闪存：写卡后立刻拔，PSP 才不会读到半截文件"""
    try:
        f = open(path, "rb+")
        try:
            os.fsync(f.fileno())
        finally:
            f.close()
    except Exception:
        pass


def psp_verify(ffprobe, path, prof_key="psp_std", prof=None):
    """转完自检：返回 (是否合规, 不合规原因)

    按实测定出来的真规则校验：H.264 只认 Baseline/Main、宽限 480/640/720、
    恒定 30fps、无 B 帧、AAC-LC 立体声 ≤128kbps、moov 在文件头。
    """
    if prof is None:
        prof = prof_key if isinstance(prof_key, dict) else (
            VPROFILES.get(prof_key) or VPROFILES["psp_std"])
    try:
        info = probe(ffprobe, path)
    except Exception:
        return False, "读不出编码信息"
    v = a = None
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and v is None:
            v = s
        elif s.get("codec_type") == "audio" and a is None:
            a = s
    bad = []
    if v is None:
        return False, "没有视频流"
    if (int(v.get("width") or 0), int(v.get("height") or 0)) != (prof["w"], prof["h"]):
        bad.append("尺寸 %sx%s 不是 %dx%d" % (v.get("width"), v.get("height"),
                                             prof["w"], prof["h"]))
    codec = (v.get("codec_name") or "").lower()
    if prof["codec"] == "h264" and codec != "h264":
        bad.append("视频编码是 %s，不是 H.264" % codec)
    if prof["codec"] == "mpeg4" and codec != "mpeg4":
        bad.append("视频编码是 %s，不是 MPEG-4" % codec)
    try:
        lv = int(v.get("level") or 0)
        if prof["level"] and lv and lv > 30:
            bad.append("Level %.1f 超过 3.0" % (lv / 10.0))
    except Exception:
        pass
    if prof["codec"] == "h264" and int(v.get("has_b_frames") or 0):
        bad.append("带 B 帧，掌机解码容易出问题")
    fps = (v.get("avg_frame_rate") or "").split("/")
    try:
        if len(fps) == 2 and float(fps[1] or 1) and abs(
                float(fps[0]) / float(fps[1]) - FPS) > 0.3:
            bad.append("帧率 %s 不是 %d" % (v.get("avg_frame_rate"), FPS))
    except Exception:
        pass
    vb = int(v.get("bit_rate") or 0)
    if vb > PSP_MAX_VBPS:
        bad.append("视频码率 %dk 超 PSP 实测上限 %dk" % (vb // 1024,
                                                    PSP_MAX_VBPS // 1024))
    if prof["codec"] == "h264":
        got_p = (v.get("profile") or "").lower()
        want_p = (prof.get("profile") or "baseline").lower()
        if want_p == "baseline" and got_p not in ("baseline", "constrained baseline"):
            bad.append("Profile 是 %s，该档位要求 Baseline" % (v.get("profile") or "?"))
        elif want_p == "main" and got_p != "main":
            bad.append("Profile 是 %s，720×480 实测只认 Main（Baseline 判不兼容）"
                       % (v.get("profile") or "?"))
    if prof["codec"] == "mpeg4" and prof.get("level"):
        try:
            want = int(str(prof["level"]))
            got_lv = int(v.get("level") or 0)
            if got_lv and got_lv != want:
                bad.append("MPEG-4 Level 是 %s，该分辨率要对上 L%d" % (got_lv, want))
        except Exception:
            pass
    if prof["codec"] == "h264" and (int(v.get("width") or 0) not in (480, 640, 720)):
        bad.append("宽 %s 不在掌机认的分辨率白名单（480 / 640 / 720）" % v.get("width"))
    if a is None:
        bad.append("没有音轨（PSP 必须有一路音频）")
    else:
        if (a.get("codec_name") or "").lower() != "aac":
            bad.append("音频不是 AAC")
        sr_a = int(a.get("sample_rate") or 0)
        if sr_a not in (44100, 48000):
            bad.append("音频采样率 %d 不是 44.1k/48k" % sr_a)
        if int(a.get("channels") or 0) > 2:
            bad.append("音频声道 %s 超过 2" % a.get("channels"))
        ab = int(a.get("bit_rate") or 0)
        if ab > PSP_MAX_ABPS:
            bad.append("音频码率 %dk 超 128k" % (ab // 1024))
    try:                                  # moov 必须在 mdat 前面，否则 PSP 读不了
        head = open(path, "rb").read(131072)
        moov, mdat = head.find(b"moov"), head.find(b"mdat")
        if moov < 0 or (mdat >= 0 and moov > mdat):
            bad.append("moov 不在文件头（没用 faststart）")
        # 注：-f psp 写出的 uuid PROF / uuid USMT 是 PSP 播放必需的，不能当垃圾清掉
        # 总码率：视频 + 音频各自封顶，合计超过才提醒
        try:
            tot = int((info.get("format") or {}).get("bit_rate") or 0)
            if tot > PSP_MAX_VBPS + PSP_MAX_ABPS:
                bad.append("总码率 %dk 超过 %dk（视频 %dk + 音频 %dk）" % (
                    tot // 1024, (PSP_MAX_VBPS + PSP_MAX_ABPS) // 1024,
                    PSP_MAX_VBPS // 1024, PSP_MAX_ABPS // 1024))
        except Exception:
            pass
    except Exception:
        pass
    return (len(bad) == 0), "；".join(bad)


def convert_audio(ffmpeg, src, out_path, bitrate, sr, fmt="mp3"):
    """音频统一出口：mp3 / wma / wav 三种。WAV 是无损 PCM，码率参数对它不起作用"""
    if fmt == "wav":
        codec = ["-c:a", "pcm_s16le"]
    elif fmt == "wma":
        codec = ["-c:a", "wmav2", "-b:a", "%dk" % int(bitrate)]
    else:
        codec = ["-c:a", "libmp3lame", "-b:a", "%dk" % int(bitrate)]
    rc, out = run([ffmpeg, "-y", "-i", src, "-vn"] + codec +
                  ["-ar", str(int(sr)), "-ac", "2", "-loglevel", "error", out_path])
    return rc, out


# ----------------------------------------------------------------- 抖音数据库

def load_js(path):
    txt = open(path, "rb").read().decode("utf-8-sig", errors="ignore")
    a, b = txt.find("`"), txt.rfind("`")
    if a < 0 or b <= a:
        raise ValueError("不是可识别的 .js 数据文件")
    return json.loads(txt[a + 1:b])


class DouyinDB:
    def __init__(self, root):
        self.root = root
        self.meta = os.path.join(root, ".appdata")
        if not os.path.isdir(self.meta):
            raise ValueError("所选目录下没有 .appdata 文件夹")
        self.authors_meta = load_js(os.path.join(self.meta, "db_authors.js"))
        self.videos = load_js(os.path.join(self.meta, "db_videos.js"))
        try:
            self.texts = load_js(os.path.join(self.meta, "db_texts.js"))
        except Exception:
            self.texts = {}
        self.buckets = self._scan()

    def _scan(self):
        buckets = []
        follow = os.path.join(self.root, "关注")
        if not os.path.isdir(follow):
            for cand in ("作品", "post", ""):
                p = os.path.join(self.root, cand) if cand else self.root
                if os.path.isdir(os.path.join(p, "关注")):
                    follow = os.path.join(p, "关注")
                    break
        if not os.path.isdir(follow):
            follow = self.root
        self.follow = follow
        for aid in sorted(os.listdir(follow)):
            d = os.path.join(follow, aid)
            if not os.path.isdir(d):
                continue
            items = []
            for cur, dirs, files in os.walk(d):
                for fn in files:
                    if os.path.splitext(fn)[1].lower() not in VIDEO_EXTS | AUDIO_EXTS:
                        continue
                    vid = os.path.splitext(fn)[0]
                    items.append({
                        "path": os.path.join(cur, fn),
                        "vid": vid,
                        "title": safe_name(self.texts.get(vid) or "", 60),
                        "kind": "video" if os.path.splitext(fn)[1].lower() in VIDEO_EXTS else "audio",
                        "size": os.path.getsize(os.path.join(cur, fn)),
                    })
            if not items:
                continue
            name = read_ini_name(d)
            if not name:
                meta = self.authors_meta.get(aid) or {}
                nicks = meta.get("nicknames") or []
                name = safe_name(nicks[0] if nicks else aid, 60)
            buckets.append({"aid": aid, "name": name, "items": items})
        buckets.sort(key=lambda b: (-len(b["items"]), b["name"]))
        return buckets

    def totals(self):
        return len(self.buckets), sum(len(b["items"]) for b in self.buckets)

    def avatar_map(self):
        """关注\\头像 目录的 uid→文件索引（一次扫完，别每个作者都去 stat）

        库里存的是 large_<uid>.jpg / small_<uid>.jpg，large 优先。
        """
        cached = getattr(self, "_avatars", None)
        if cached is not None:
            return cached
        idx = {}
        base = os.path.join(getattr(self, "follow", self.root), "头像")
        try:
            for fn in os.listdir(base):
                stem, ext = os.path.splitext(fn)
                if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    continue
                full = os.path.join(base, fn)
                if stem.lower().startswith("large_"):
                    idx[stem[6:]] = full            # 大图直接覆盖小图
                elif stem.lower().startswith("small_"):
                    idx.setdefault(stem[6:], full)  # 没有大图时才用
        except Exception:
            pass
        self._avatars = idx
        return idx

    def avatar_of(self, cands):
        """按候选 uid 一次查索引，返回头像路径（查不到返回空串）"""
        amap = self.avatar_map()
        if not amap:
            return ""
        for c in cands:
            c = str(c or "").strip()
            if not c:
                continue
            p = amap.get(c) or amap.get(c.lstrip("@")) or ""
            if p and os.path.isfile(p):
                return p
        return ""


# ----------------------------------------------------------------- 目标盘

def list_drives():
    out = []
    try:
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            path = "%s:\\" % letter
            if not os.path.exists(path):
                continue
            try:
                free = shutil.disk_usage(path).free
                total = shutil.disk_usage(path).total
            except Exception:
                continue
            try:
                dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(path))
            except Exception:
                dtype = 0
            kind = {2: "可移动", 3: "本地硬盘", 4: "网络", 5: "光驱", 6: "内存盘"}.get(dtype, "其他")
            label = ""
            try:
                buf = ctypes.create_unicode_buffer(261)
                ctypes.windll.kernel32.GetVolumeInformationW(
                    ctypes.c_wchar_p(path), buf, 260, None, None, None, None, 0)
                label = buf.value
            except Exception:
                pass
            out.append({"letter": letter, "path": path, "free": free,
                        "total": total, "type": kind, "label": label,
                        "removable": dtype == 2})
    except Exception:
        pass
    return out


# ----------------------------------------------------------------- PSP 游戏镜像（.iso / .cso 只读）

ISO_SECTOR = 2048


class PspImage(object):
    """只读打开 PSP 游戏镜像：普通 .iso 或压缩过的 .cso（CISO 格式）

    纯标准库实现：CISO 块用 raw deflate 解压，ISO9660 目录自己走。
    """

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        head = self.f.read(24)
        self.kind = "ISO"
        self.block_size = ISO_SECTOR
        self.align = 1
        self.nblocks = 0
        self._icache = {}
        if head[:4] == b"CISO":
            self.kind = "CSO"
            # 有的压制工具把 header_size 写成 0，实际索引仍紧跟 24 字节头
            self.header_size = struct.unpack_from("<I", head, 4)[0] or 0x18
            self.total = struct.unpack_from("<Q", head, 8)[0]
            self.block_size = struct.unpack_from("<I", head, 16)[0] or ISO_SECTOR
            self.align = head[0x15] or 1
            self.nblocks = (self.total + self.block_size - 1) // self.block_size
        else:
            self.total = os.path.getsize(path)
        self.iso_ok = self.read(16, ISO_SECTOR)[1:6] == b"CD001"

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass

    def _idx_vals(self, a, b):
        """按需读 CISO 索引：一次补 128 项，避免为 3GB 镜像整表读 6MB"""
        b = min(b, self.nblocks)
        if a >= b:
            return []
        need = [i for i in range(a, b) if i not in self._icache]
        if need:
            s = need[0]
            e = min(self.nblocks, need[-1] + 128)
            self.f.seek(self.header_size + s * 4)
            raw = self.f.read((e - s) * 4)
            for i in range(s, e):
                k = (i - s) * 4
                if k + 4 <= len(raw):
                    self._icache[i] = struct.unpack_from("<I", raw, k)[0]
        return [self._icache.get(i) for i in range(a, b)]

    def _offset(self, val):
        if val is None:
            return self.total
        return (val & 0x7FFFFFFF) * self.align

    def _inflate(self, raw):
        last = None
        for wbits in (-15, 15):          # CISO 用 raw deflate，少数工具带 zlib 头
            try:
                return zlib.decompressobj(wbits).decompress(raw)
            except Exception:
                last = sys.exc_info()[1]
        raise RuntimeError("CISO 块解压失败：%s" % last)

    def block(self, n):
        if self.kind == "ISO":
            self.f.seek(n * self.block_size)
            return self.f.read(self.block_size)
        vals = self._idx_vals(n, n + 2)
        v = vals[0] if vals else None
        nxt = vals[1] if len(vals) > 1 else None
        off, end = self._offset(v), self._offset(nxt)
        self.f.seek(off)
        raw = self.f.read(max(0, end - off))
        if v is not None and (v & 0x80000000):
            return raw[:self.block_size]
        return self._inflate(raw)

    def read(self, lba, size):
        """按扇区号读连续字节；CSO 时整段原始数据一次读出，再逐块解开"""
        if self.kind == "ISO":
            self.f.seek(lba * ISO_SECTOR)
            return self.f.read(size)
        start = lba * ISO_SECTOR
        if self.block_size != ISO_SECTOR:
            n0 = start // self.block_size
            n1 = (start + size + self.block_size - 1) // self.block_size
            data = b"".join(self.block(n) for n in range(n0, n1))
            s = start - n0 * self.block_size
            return data[s:s + size]
        n = (size + ISO_SECTOR - 1) // ISO_SECTOR
        vals = self._idx_vals(lba, lba + n + 1)
        offs = [self._offset(vals[i] if i < len(vals) else None) for i in range(n + 1)]
        base = offs[0]
        self.f.seek(base)
        raw = self.f.read(max(0, offs[-1] - base))
        out = []
        for i in range(n):
            chunk = raw[offs[i] - base:offs[i + 1] - base]
            v = vals[i] if i < len(vals) else None
            if v is not None and (v & 0x80000000):
                out.append(chunk[:self.block_size])
            else:
                out.append(self._inflate(chunk))
        return b"".join(out)[:size]

    def entries(self, lba, size):
        """目录项 → [(名字, lba, 字节数, 是否目录)]"""
        data = self.read(lba, size)
        out = []
        pos = 0
        while pos < len(data):
            ln = data[pos]
            if ln == 0:
                pos = (pos // ISO_SECTOR + 1) * ISO_SECTOR
                continue
            rec = data[pos:pos + ln]
            if len(rec) < 33:
                break
            ext_lba = struct.unpack_from("<I", rec, 2)[0]
            ext_size = struct.unpack_from("<I", rec, 10)[0]
            names = rec[33:33 + rec[32]].decode("ascii", "replace")
            pos += ln
            if names in ("\x00", "\x01"):
                continue
            out.append((names.split(";")[0], ext_lba, ext_size,
                        bool(rec[25] & 2)))
        return out

    def root(self):
        pvd = self.read(16, ISO_SECTOR)
        rec = pvd[156:156 + 255]
        return (struct.unpack_from("<I", rec, 2)[0],
                struct.unpack_from("<I", rec, 10)[0])

    def find(self, path):
        cur = self.root()
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            hit = None
            for name, lba, size, is_dir in self.entries(cur[0], cur[1]):
                if name.upper() == part.upper():
                    hit = (lba, size, is_dir)
                    break
            if not hit:
                return None
            cur = (hit[0], hit[1])
        return cur

    def parent_of(self, path):
        return "/".join([p for p in path.replace("\\", "/").split("/") if p][:-1])

    def read_file(self, path):
        hit = self.find(path)
        if not hit:
            return None
        return self.read(hit[0], hit[1])


def sfo_fields(data):
    """解析 PARAM.SFO（PSP 游戏元信息）"""
    out = {}
    if not data or data[:4] != b"\x00PSF":
        return out
    try:
        key_start, val_start, count = struct.unpack_from("<III", data, 8)
        for i in range(count):
            koff, fmt, ln, mx, doff = struct.unpack_from("<HHIII", data, 20 + i * 16)
            key = data[key_start + koff:].split(b"\x00")[0].decode("utf-8", "replace")
            raw = data[val_start + doff:val_start + doff + ln]
            if fmt == 0x0204:
                out[key] = raw.decode("utf-8", "replace").strip()
            elif fmt == 0x0404:
                out[key] = struct.unpack_from("<I", raw, 0)[0] if len(raw) >= 4 else 0
    except Exception:
        pass
    return out


def game_meta(path, want_images=False):
    """读一个 .iso/.cso 的元信息（可选带 ICON0/PIC0/PIC1 图像数据）"""
    meta = {"path": path, "file": os.path.basename(path),
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
            "kind": "CSO" if path.lower().endswith(".cso") else "ISO",
            "title": "", "disc_id": "", "version": "", "app_ver": "",
            "parental": "", "icon": None, "pic0": None, "pic1": None,
            "icon1": 0, "icon1_lba": None,
            "snd0": 0, "videos": [], "nfiles": "", "ok": False, "err": ""}
    try:
        img = PspImage(path)
        if not img.iso_ok:
            meta["err"] = "不是 ISO9660 镜像"
            img.close()
            return meta
        f = sfo_fields(img.read_file("PSP_GAME/PARAM.SFO"))
        meta["title"] = f.get("TITLE") or os.path.splitext(meta["file"])[0]
        meta["disc_id"] = f.get("DISC_ID", "")
        meta["version"] = f.get("DISC_VERSION", "")
        meta["app_ver"] = f.get("APP_VER", "")
        meta["parental"] = f.get("PARENTAL_LEVEL", "")
        if want_images:
            meta["icon"] = img.read_file("PSP_GAME/ICON0.PNG")
            meta["pic0"] = img.read_file("PSP_GAME/PIC0.PNG")
            meta["pic1"] = img.read_file("PSP_GAME/PIC1.PNG")
            hit1 = img.find("PSP_GAME/ICON1.PMF")   # 动态图标动画
            if hit1:
                meta["icon1"] = hit1[1]
                meta["icon1_lba"] = (hit1[0], hit1[1])
        snd0 = img.read_file("PSP_GAME/SND0.AT3")
        meta["snd0"] = len(snd0) if snd0 else 0
        # PSP 的视频可能躺在 UMD_VIDEO，也可能在 USRDIR/MOVIE（大多数游戏在这）
        if want_images:
            cands = ("UMD_VIDEO", "PSP_GAME/USRDIR/MOVIE", "PSP_GAME/USRDIR/UMD_VIDEO")
        else:
            cands = ("UMD_VIDEO",)
        for cand in cands:
            hit = img.find(cand)
            if not hit:
                continue
            try:
                for nm, lb, sz, is_dir in img.entries(hit[0], hit[1]):
                    if not is_dir and nm.lower().endswith(".pmf"):
                        meta["videos"].append([nm, sz, lb])
            except Exception:
                pass
        g = img.find("PSP_GAME")
        if g:
            meta["nfiles"] = len(img.entries(g[0], g[1]))
        meta["ok"] = True
        img.close()
    except Exception as e:
        meta["err"] = str(e)
    return meta


class PmfPlayer(object):
    """用 ffmpeg 把 PMF（或任意视频）解成固定尺寸 RGB，按源帧率逐帧喂给 Tk，播完自动循环

    关键点：ffmpeg 解码比屏幕刷新快得多，绝不能让它全速往外吐帧——那样队列里
    只剩最后两帧、瞬间播完再重开，画面就闪成黑块。这里在解码线程里按帧率节流。
    """

    FRAME_W, FRAME_H = 300, 170

    def __init__(self, ffmpeg, src, ffprobe=None, fps=None):
        self.ffmpeg = ffmpeg
        self.src = src
        if not fps:
            fps = self._probe_fps(ffprobe)
        self.fps = max(8.0, min(30.0, float(fps or 15.0)))
        self.interval = 1.0 / self.fps
        self.frames = pyqueue.Queue(maxsize=4)
        self.running = False
        self._proc = None
        self._thread = None

    def _probe_fps(self, ffprobe):
        """问 ffprobe 要帧率，拿不到就按 PSP 常见的 15fps"""
        if not ffprobe or not os.path.isfile(ffprobe):
            return 15.0
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate,avg_frame_rate",
                 "-of", "default=nw=1:nk=1", self.src],
                capture_output=True, timeout=8, creationflags=no_window())
            txt = (r.stdout or b"").decode("utf-8", "replace")
        except Exception:
            return 15.0
        vals = []
        for tok in txt.replace("\r", "").split("\n"):
            tok = tok.strip()
            try:
                if "/" in tok:
                    a, b = tok.split("/", 1)
                    num, den = float(a), float(b)
                    if den > 0 and num > 0:
                        vals.append(num / den)
                elif tok:
                    v = float(tok)
                    if v > 0:
                        vals.append(v)
            except Exception:
                pass
        for v in vals:
            if 8.0 <= v <= 40.0:
                return v
        return 15.0

    def _cmd(self):
        W, H = self.FRAME_W, self.FRAME_H
        vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black" % (W, H, W, H))
        return [self.ffmpeg, "-v", "error", "-i", self.src, "-vf", vf,
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-an", "pipe:1"]

    def _put(self, data):
        try:
            self.frames.put_nowait(data)
        except pyqueue.Full:            # 显示慢了就丢最旧的一帧
            try:
                self.frames.get_nowait()
            except pyqueue.Empty:
                pass
            try:
                self.frames.put_nowait(data)
            except pyqueue.Full:
                pass

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        size = self.FRAME_W * self.FRAME_H * 3
        while self.running:
            try:
                self._proc = reg_proc(subprocess.Popen(
                    self._cmd(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, creationflags=no_window()))
            except Exception:
                self.running = False
                break
            out = self._proc.stdout
            t_next = 0.0
            while self.running:
                try:
                    data = out.read(size)
                except Exception:
                    break
                if not data or len(data) < size:
                    break
                now = time.time()
                if t_next and now < t_next:      # 按帧率节奏放行，别让它冲出去
                    time.sleep(t_next - now)
                    now = time.time()
                t_next = now + self.interval
                self._put(data)
            try:
                out.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
            if self.running:
                time.sleep(0.08)                 # 循环重新开一遍，衔接短一点
        self.running = False

    def stop(self):
        self.running = False
        p = self._proc
        self._proc = None
        if p is not None:
            _terminate_proc(p)
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except pyqueue.Empty:
                break


class HintProxy(object):
    """详情下方那行字：自动把文件信息附在状态提示后面，切页/换文件都不丢"""

    def __init__(self, view):
        self._v = view

    def configure(self, **kw):
        lb = self._v["hint"]
        text = kw.pop("text", None)
        if text is None:
            lb.configure(**kw)
            return
        facts = self._v.get("facts") or ""
        if facts and facts != text:
            text = ("%s\n%s" % (text, facts)) if text else facts
        lb.configure(text=text, **kw)

    def cget(self, key):
        return self._v["hint"].cget(key)

    def __getattr__(self, name):
        return getattr(self._v["hint"], name)


class MediaPlayer(object):
    """mp4 等视频播放：ffmpeg 解成 640x360 RGB 进队列，按帧率节流；音频交给 ffplay

    与 PmfPlayer 的区别：不循环、支持从中途 seek 起播、支持变速（-r 重采样 + 音频 atempo）。
    """

    FRAME_W, FRAME_H = 640, 360

    def __init__(self, ffmpeg, ffprobe, src, start=0.0, speed=1.0, probe_from=None,
                 out_size=None, fs_box=None, fs_fit=None, known_meta=None):
        self.ffmpeg = ffmpeg
        self.src = src
        self.start_at = max(0.0, float(start or 0))
        self.speed = max(0.25, float(speed or 1.0))
        if known_meta is not None and known_meta[0] and known_meta[1] and known_meta[2]:
            # 调用方已经用 ffprobe 探过一遍了，宽高帧率直接拿来用，别再等第二遍
            self.fps = max(8.0, min(60.0, float(known_meta[0] or 25.0)))
            sw, sh = known_meta[1], known_meta[2]
        elif probe_from is not None:
            # 换速度时复用上一路探测结果，省掉阻塞的 ffprobe
            self.fps = probe_from.fps
            sw, sh = probe_from.src_w, probe_from.src_h
        else:
            fps, sw, sh = self._probe_meta(ffprobe)
            self.fps = max(8.0, min(60.0, float(fps or 25.0)))
        self.src_w, self.src_h = max(2, int(sw)), max(2, int(sh))
        self.fs_fit = None
        self.fs_box = None
        if out_size:
            # 全屏：让 ffmpeg 直接按屏幕尺寸出帧并裁/垫好
            # （源被压到 960 再由显示端放大，4K 视频就糊成马赛克）
            self.frame_w = max(2, int(out_size[0]))
            self.frame_h = max(2, int(out_size[1]))
            self.fs_fit = fs_fit
            self.fs_box = fs_box
        else:
            # 输出帧尺寸按源比例算（不加黑边），长边上限 BOX，竖屏就出竖的帧
            self.frame_w, self.frame_h = self._fit_box(self.src_w, self.src_h)
        self.interval = 1.0 / (self.fps * self.speed)
        self.frames = pyqueue.Queue(maxsize=4)
        self.running = False
        self.eof = False
        self._proc = None
        self._thread = None

    @property
    def interval(self):
        """每帧的显示节拍：解码进程固定按源帧率出帧，变速只改消费节奏"""
        return 1.0 / max(1.0, self.fps * self.speed)

    @interval.setter
    def interval(self, v):
        pass                                    # 兼容旧写法，真实节拍由 fps*speed 决定

    def set_speed(self, v):
        """换倍速：只改节拍，解码进程不重启 —— 画面连续、不黑屏"""
        v = max(0.25, min(3.0, float(v or 1.0)))
        if abs(v - self.speed) < 1e-6:
            return
        self.speed = v
        while True:                             # 旧节拍残留的几帧丢掉，立刻按新节奏走
            try:
                self.frames.get_nowait()
            except pyqueue.Empty:
                break

    # 解出来的画面长边上限，够给全屏用，又不会让内存和转换太吃力
    BOX = 960

    # 探测缓存：同一个文件反复点开时，别每次都起一遍 ffprobe
    _PROBE_CACHE = {}

    def _probe_meta(self, ffprobe):
        """一次 ffprobe 同时拿宽高和帧率（原来拆成两次，切视频时白等一倍）

        结果按「路径 + 修改时间」缓存，第二次点同一个文件直接命中，不再起进程。
        """
        try:
            key = (self.src, os.path.getmtime(self.src))
        except Exception:
            key = (self.src, 0)
        hit = MediaPlayer._PROBE_CACHE.get(key)
        if hit:
            return hit
        w = h = 0
        fps = 25.0
        if ffprobe and os.path.isfile(ffprobe):
            try:
                r = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
                     "-of", "default=nw=1", self.src],
                    capture_output=True, timeout=8, creationflags=no_window())
                txt = (r.stdout or b"").decode("utf-8", "replace")
            except Exception:
                txt = ""
            vals = {}
            for line in txt.replace("\r", "").split("\n"):
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k] = v.strip()
            try:
                w, h = int(vals.get("width", 0)), int(vals.get("height", 0))
            except Exception:
                w = h = 0
            for k in ("avg_frame_rate", "r_frame_rate"):
                tok = vals.get(k) or ""
                if "/" in tok:
                    try:
                        a, b = tok.split("/", 1)
                        num, den = float(a), float(b)
                        if den > 0 and 8 <= num / den <= 60:
                            fps = num / den
                            break
                    except Exception:
                        pass
                elif tok:
                    try:
                        v = float(tok)
                        if 8 <= v <= 60:
                            fps = v
                            break
                    except Exception:
                        pass
        if w <= 0 or h <= 0:
            # 探测失败：按 fallback 播，至少不挡住播放
            w, h = self.FRAME_W, self.FRAME_H
            fps = 25.0
        if len(MediaPlayer._PROBE_CACHE) > 64:
            MediaPlayer._PROBE_CACHE.clear()
        MediaPlayer._PROBE_CACHE[key] = (fps, w, h)
        return fps, w, h

    def _fit_box(self, sw, sh):
        """等比缩到长边不超过 BOX 的偶数尺寸；宽高比完全保留"""
        sw, sh = max(2, int(sw)), max(2, int(sh))
        r = min(1.0, float(self.BOX) / max(sw, sh))
        w, h = int(sw * r), int(sh * r)
        w += w % 2
        h += h % 2
        return max(2, w), max(2, h)

    def _cmd(self):
        W, H = self.frame_w, self.frame_h
        if self.fs_fit:
            # 全屏：缩放和裁切/垫黑边都交给 ffmpeg（swscale 比显示端放大清楚得多）
            bw, bh = self.fs_box or (W, H)
            if self.fs_fit == "cover":
                vf = ("scale=%d:%d:force_original_aspect_ratio=increase:flags=bicubic,"
                      "crop=%d:%d" % (bw, bh, bw, bh))
                if (bw, bh) != (W, H):
                    vf += ",pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black" % (W, H)
            else:
                # 适合窗口：decrease 出来的尺寸是随源比例变的（竖屏只有 810 宽），
                # 必须垫到整屏尺寸，否则读帧会跨帧拼接、画面全是横条纹
                vf = ("scale=%d:%d:force_original_aspect_ratio=decrease:flags=bicubic,"
                      "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black" % (bw, bh, W, H))
            vf += ",setsar=1"
        else:
            vf = ("scale=%d:%d:flags=bicubic,setsar=1" % (W, H))
        cmd = [self.ffmpeg, "-v", "error"]
        if self.start_at > 0.01:
            cmd += ["-ss", "%.3f" % self.start_at]
        cmd += ["-i", self.src, "-an", "-vf", vf,
                "-r", "%.4f" % self.fps,
                "-f", "rawvideo", "-pix_fmt",
                "bgr24" if self.fs_fit else "rgb24", "pipe:1"]
        return cmd

    def _put(self, data):
        try:
            self.frames.put_nowait(data)
        except pyqueue.Full:
            try:
                self.frames.get_nowait()
            except pyqueue.Empty:
                pass
            try:
                self.frames.put_nowait(data)
            except pyqueue.Full:
                pass

    def start(self):
        if self.running:
            return
        self.running = True
        self.eof = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        size = self.frame_w * self.frame_h * 3
        try:
            self._proc = reg_proc(subprocess.Popen(
                self._cmd(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=no_window()))
        except Exception:
            self.running = False
            self.eof = True
            return
        out = self._proc.stdout
        t_next = 0.0
        while self.running:
            try:
                data = out.read(size)
            except Exception:
                break
            if not data or len(data) < size:
                break
            now = time.time()
            # 节拍按上一帧计划时间累加（不叠加 sleep 的系统误差），睡多了下一帧自动追回来
            if not t_next:
                t_next = now
            elif now < t_next:
                d = t_next - now
                if d > 0.0008:
                    time.sleep(d)
                now = time.time()
            elif now > t_next + 0.30:
                t_next = now                       # 落后太多（暂停/负载）就别硬追了
            t_next += self.interval
            self._put(data)
        try:
            out.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
        except Exception:
            pass
        self._proc = None
        self.running = False
        self.eof = True

    def stop(self, quick=False):
        self.running = False
        p = self._proc
        self._proc = None
        if p is not None:
            if quick:                           # 拖进度条要立刻返回，不等收尾
                try:
                    p.kill()
                except Exception:
                    _terminate_proc(p)
            else:
                _terminate_proc(p)
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except pyqueue.Empty:
                break


# ----------------------------------------------------------------- 记忆棒 / 临时目录


def card_root(cfg):
    """记忆棒根目录：可填盘符（G:）也可填任意目录"""
    p = (cfg.get("card_path") or "").strip()
    if not p and cfg.get("drive"):
        p = str(cfg["drive"]).strip().rstrip(":") + ":\\"
    if not p:
        return ""
    p = os.path.expandvars(os.path.expanduser(p))
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    return p


def psp_video_sub(cfg):
    """PSP 标准布局下，AVC 与 MPEG-4 是两套完全不同的目录+前缀：
        H.264  → MP_ROOT\\100ANV01\\MAQxxxxx.MP4
        MPEG-4 → MP_ROOT\\100MNV01\\M4Vxxxxx.MP4
    放错目录或前缀对不上，掌机就只会报「不兼容的数据」。"""
    prof = resolve_profile(cfg)
    if prof["codec"] == "h264":
        return "100ANV01", "MAQ"
    return "100MNV01", "M4V"


def vid_dir(cfg, root):
    """视频一律落在 <根>\\VIDEO

    MP_ROOT\\100ANV01 / 100MNV01 那条路已经真机证伪：同一个文件放 VIDEO 能播，
    换个位置进去就报「不兼容的数据」，所以这里不再保留编号目录分支。
    """
    return os.path.join(root, "VIDEO")


def group_author_on(cfg):
    """是否按作者建子目录：新配置看 vid_layout，老配置的 group_author 仍然认"""
    return (cfg.get("vid_layout") == "author") or bool(cfg.get("group_author"))


def card_dirs(cfg, create=False):
    root = card_root(cfg)
    if not root:
        return "", "", ""
    vdir = vid_dir(cfg, root)
    mdir = os.path.join(root, "MUSIC")
    if create:
        os.makedirs(vdir, exist_ok=True)
        os.makedirs(mdir, exist_ok=True)
    return root, vdir, mdir


def temp_path(cfg):
    t = (cfg.get("temp_dir") or "").strip()
    if not t:
        t = os.path.join(os.environ.get("TEMP", "."), "PSP媒体管理器缓存")
    return os.path.normpath(os.path.expandvars(os.path.expanduser(t)))


def temp_dirs(cfg, create=False):
    t = temp_path(cfg)
    v = os.path.join(t, "video")
    m = os.path.join(t, "music")
    if create:
        os.makedirs(v, exist_ok=True)
        os.makedirs(m, exist_ok=True)
    return t, v, m


# ----------------------------------------------------------------- 媒体详情


def _fps_text(s):
    try:
        num, den = (s.get("r_frame_rate") or "0/1").split("/")
        v = float(num) / float(den or 1)
        if not v:
            return "-"
        return ("%.2f" % v).rstrip("0").rstrip(".") + " fps"
    except Exception:
        return "-"


def _kbps(v):
    try:
        return "%.0f kbps" % (int(v) / 1000.0)
    except Exception:
        return "-"


def media_facts(ffmpeg, ffprobe, path, kind, cache_dir):
    """给信息面板用：返回 {"lines": [(标题, 值)], "pic": 预览图路径或 None}"""
    info = probe(ffprobe, path)
    fmt = info.get("format") or {}
    lines = []
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    try:
        dur = float(fmt.get("duration") or 0)
    except Exception:
        dur = 0.0
    lines.append(("文件名", os.path.basename(path)))
    lines.append(("大小", human(size)))
    lines.append(("时长", "%.1f 秒" % dur if dur else "-"))
    pic = None

    streams = info.get("streams") or []
    if kind == "video":
        vs, aus = None, []
        for s in streams:
            if s.get("codec_type") == "video":
                if (s.get("disposition") or {}).get("attached_pic"):
                    continue
                if vs is None:
                    vs = s
            elif s.get("codec_type") == "audio":
                aus.append(s)
        if vs:
            w = int(vs.get("width") or 0)
            h = int(vs.get("height") or 0)
            rot = 0
            for sd in vs.get("side_data_list") or []:
                if "rotation" in sd:
                    try:
                        rot = int(float(sd["rotation"]))
                    except Exception:
                        rot = 0
            portrait = (abs(rot) % 180 == 90)
            lines.append(("分辨率", "%dx%d%s" % (w, h, "（竖屏）" if portrait and w > h
                                                 else "（已旋转标注）" if portrait else "")))
            codec = vs.get("codec_name") or "-"
            prof = vs.get("profile")
            lines.append(("视频编码", "%s%s" % (codec, " · " + prof if prof else "")))
            if vs.get("pix_fmt"):
                lines.append(("色彩格式", vs["pix_fmt"]))
            lines.append(("帧率", _fps_text(vs)))
            br = vs.get("bit_rate") or fmt.get("bit_rate")
            lines.append(("码率", _kbps(br)))
            if aus:
                a = aus[0]
                lines.append(("音频", "%s · %s · %s" % (
                    a.get("codec_name") or "-", _kbps(a.get("bit_rate") or fmt.get("bit_rate")),
                    ("%s Hz / %s 声道" % (a.get("sample_rate"), a.get("channels")))
                    if a.get("sample_rate") else "-")))
            else:
                lines.append(("音频", "无（已补静音）"))
        # 预览：优先用同名 THM，其次抽一帧
        thm = os.path.splitext(path)[0] + ".THM"
        if os.path.isfile(thm):
            pic = thm
        else:
            pic = _extract_frame(ffmpeg, path, cache_dir)
    else:
        aus = [s for s in streams if s.get("codec_type") == "audio"]
        a = aus[0] if aus else {}
        lines.append(("音频编码", "%s%s" % (a.get("codec_name") or "-",
                                        " · " + a["profile"] if a.get("profile") else "")))
        lines.append(("码率", _kbps(a.get("bit_rate") or fmt.get("bit_rate"))))
        lines.append(("采样率", "%s Hz" % a.get("sample_rate") if a.get("sample_rate") else "-"))
        lines.append(("声道", str(a.get("channels") or "-")))
        tags = dict(fmt.get("tags") or {})
        tags.update(a.get("tags") or {})
        for key, label in (("title", "标题"), ("artist", "艺术家"), ("album", "专辑"),
                           ("date", "年份"), ("genre", "流派"), ("comment", "备注"),
                           ("track", "音轨")):
            for tk_, tv_ in tags.items():
                if tk_.lower() == key and str(tv_).strip():
                    lines.append((label, str(tv_)))
                    break
        if not any(k in ("标题", "艺术家", "专辑") for k, _ in lines):
            lines.append(("标签", "无 ID3 标签"))
        pic = _extract_cover(ffmpeg, path, cache_dir)
    return {"lines": lines, "pic": pic}


def _cache_name(path, suffix):
    import zlib
    st = os.stat(path)
    key = "%s|%d|%d" % (path.lower(), int(st.st_mtime), st.st_size)
    return "%08x_%08x.%s" % (zlib.crc32(key.encode("utf-8", "ignore")) & 0xffffffff,
                             len(key), suffix)


def _extract_frame(ffmpeg, path, cache_dir):
    if not ffmpeg:
        return None
    out = os.path.join(cache_dir, _cache_name(path, "jpg"))
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    for t in ("", "3"):
        cmd = [ffmpeg, "-y", "-noautorotate"] + (["-ss", t] if t else []) + \
              ["-i", path, "-frames:v", "1", "-vf",
               "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "3", out]
        rc, _ = run(cmd)
        if rc == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    return None


def _extract_cover(ffmpeg, path, cache_dir):
    if not ffmpeg:
        return None
    out = os.path.join(cache_dir, _cache_name(path, "cover.jpg"))
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    rc, _ = run([ffmpeg, "-y", "-i", path, "-map", "0:v:0", "-frames:v", "1",
                 "-c:v", "mjpeg", out])
    if rc == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    if os.path.isfile(out):
        try:
            os.remove(out)
        except Exception:
            pass
    return None


# ----------------------------------------------------------------- 说明文件

NOTICE_NAME = "说明.txt"

DEFAULT_NOTICE = """【用法】
把视频 / 音频（或整个文件夹）拖进窗口就会入队，也可以点「媒体转换器」页的按钮添加。
上方四个页签切换功能：PSP 记忆棒 / 媒体转换器 / DY 数据库 / PSP 游戏。

【快捷键与手势】
滚轮在画面或控制条上可以调音量；双击画面进全屏，Esc 或再双击退出全屏；
右键画面切换「裁切 / 适合窗口」。

【转换口径】
视频 → {profile}，竖屏自动左转 90°，同名 .THM 缩略图。
音频 → {afmt}，本身格式相同可直接复制。
先转进临时目录（{temp}\\video 与 {temp}\\music），完成后再拷进记忆棒，慢速卡也不卡住。
记忆棒目录：{card}（视频落在 {vdir}，音乐进 MUSIC）。
存放方式可在「设置 → 视频存放位置」里改：默认直接放 VIDEO 用原标题名，选 PSP 标准则进
MP_ROOT\\100ANV01 并用 MAQxxxxx 命名（H.264），或 MP_ROOT\\100MNV01 用 M4Vxxxxx（MPEG-4）——
PSP 机内「视频」列表能直接看见。两套目录+前缀不能混用，混了掌机就报「不兼容的数据」。

【这个文件】
这段说明就是本目录下的「说明.txt」，直接用记事本改，保存后回到程序点右上「设置」里的刷新
（或重开程序）即可生效。支持占位符 {card} {vdir} {temp} {profile} {afmt} {mp3} {mp3_sr}。

----------------------------------------
"""


def notice_path():
    return os.path.join(app_dir(), NOTICE_NAME)


def read_notice():
    """读同目录下的说明文件；文件不存在就先写一份默认的，保证顶部永远有东西滚"""
    p = notice_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        txt = DEFAULT_NOTICE
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)
        except Exception:
            pass
    txt = (txt or "").strip()
    return txt or DEFAULT_NOTICE


# ----------------------------------------------------------------- 停止画面用的 logo

_LOGO_CACHE = {}


def logo_image(size):
    """PSP 媒体管理器 logo：深色径向渐变（外圈黑 → 中间发亮）+ 云母斜纹 + 播放符号"""
    import math
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    n = max(96, int(size))
    key = n
    if key in _LOGO_CACHE:
        return _LOGO_CACHE[key]

    y, xx = np.mgrid[0:n, 0:n].astype("float32")
    c = (n - 1) / 2.0
    r = np.sqrt((xx - c) ** 2 + (y - c) ** 2) / (n * 0.62)
    # 外圈纯黑、往中间慢慢过渡到深青蓝：t=1 在边缘
    t = np.clip(r, 0.0, 1.0) ** 0.85
    edge = np.array([2, 3, 6], dtype="float32")          # 最外圈几乎纯黑
    core = np.array([52, 116, 146], dtype="float32")     # 中心：深青蓝
    col = edge[None, None, :] + (core - edge)[None, None, :] * (1.0 - t)[..., None]
    # 云母：斜向柔和光带（两条）+ 极细噪点
    band = (np.sin((xx + y) / float(n) * math.pi * 2.2) * 0.5 + 0.5) ** 3
    col += (band * 0.16)[..., None] * np.array([70, 92, 108], dtype="float32")
    rng = np.random.default_rng(7)
    col += (rng.normal(0.0, 3.2, (n, n, 1)))            # 细微颗粒 → 云母的"砂感"
    col = np.clip(col, 0, 255).astype("uint8")
    im = Image.fromarray(col, "RGB").convert("RGBA")

    d = ImageDraw.Draw(im)
    s = n * 0.30
    cx = n / 2.0
    cy = n / 2.0 - n * 0.09
    # 外发光圈
    ring = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse([cx - s * 1.55, cy - s * 1.55, cx + s * 1.55, cy + s * 1.55],
               outline=(120, 210, 240, 110), width=max(2, int(n * 0.008)))
    ring = ring.filter(ImageFilter.GaussianBlur(max(1, int(n * 0.006))))
    im = Image.alpha_composite(im, ring)
    d = ImageDraw.Draw(im)
    # 中心播放三角（描边 + 半透明填充）
    tri = [(cx - s * 0.52, cy - s * 0.95), (cx - s * 0.52, cy + s * 0.95),
           (cx + s * 1.0, cy)]
    d.polygon(tri, fill=(196, 236, 250, 205))
    d.line(tri + [tri[0]], fill=(240, 250, 255, 235), width=max(2, int(n * 0.007)))
    # 产品名（雅黑粗体；找不到字体就跳过，不影响 logo 本身）
    try:
        from PIL import ImageFont
        f = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", max(10, int(n * 0.082)))
        txt = "PSP 媒体管理器"
        tw = d.textlength(txt, font=f)
        d.text(((n - tw) / 2.0, n * 0.70), txt, font=f, fill=(206, 228, 244, 235))
    except Exception:
        pass
    # 圆形羽化：边缘渐隐到透明，贴在黑画布上不会露方块边
    alpha = (np.clip((1.0 - r) / 0.26, 0.0, 1.0) * 255.0).astype("uint8")
    im.putalpha(Image.fromarray(alpha, "L"))
    _LOGO_CACHE[key] = im
    return im


def logo_photo(size, maker):
    """把 logo 转成 ImageTk（maker 传 ImageTk 模块，Pillow 只在 build_gui 里 import）"""
    try:
        return maker.PhotoImage(logo_image(size))
    except Exception:
        return None


# ------------------------------------------------------ 外部 logo 图（logo 文件夹）
_LOGO_FILE_CACHE = {}
_LOGO_BG_CACHE = {}

LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def logo_dir():
    """程序目录下的 logo 文件夹，默认图都放这里"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo")


def logo_files():
    """logo 文件夹里所有可选图片（完整路径，按名字排序）"""
    d = logo_dir()
    try:
        names = sorted(n for n in os.listdir(d) if n.lower().endswith(LOGO_EXTS))
    except Exception:
        names = []
    return [os.path.join(d, n) for n in names]


def resolve_logo(p):
    """配置里的 logo 路径 → 可用的绝对路径；相对路径按程序目录算"""
    if not p:
        return ""
    p = str(p).strip().strip('"')
    try:
        if os.path.isabs(p):
            return p if os.path.isfile(p) else ""
        q = os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
        return q if os.path.isfile(q) else ""
    except Exception:
        return ""


def pick_logo_path(cfg):
    """当前该用的 logo：配置里的，配不上就用 logo 文件夹第一张"""
    p = resolve_logo((cfg or {}).get("logo_path"))
    if p:
        return p
    files = logo_files()
    return files[0] if files else ""


def logo_file_image(path, side):
    """logo 图缩到 side 见方（等比居中、边缘羽化到透明，贴在黑底上不露方框）"""
    from PIL import Image, ImageDraw, ImageFilter
    try:
        side = max(24, int(side))
    except Exception:
        return None
    key = (path, side)
    hit = _LOGO_FILE_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((side, side), Image.LANCZOS)
        rgba = im.convert("RGBA")
        m = Image.new("L", im.size, 0)
        r = int(min(im.size) * 0.20)
        ImageDraw.Draw(m).rounded_rectangle(
            [1, 1, im.width - 2, im.height - 2], radius=r, fill=255)
        m = m.filter(ImageFilter.GaussianBlur(max(3, int(min(im.size) * 0.10))))
        rgba.putalpha(m)
        box = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        box.paste(rgba, ((side - im.width) // 2, (side - im.height) // 2), rgba)
        if len(_LOGO_FILE_CACHE) > 16:
            _LOGO_FILE_CACHE.clear()
        _LOGO_FILE_CACHE[key] = box
        return box
    except Exception:
        return None


def logo_file_photo(path, side, maker):
    try:
        im = logo_file_image(path, side)
        return maker.PhotoImage(im) if im is not None else None
    except Exception:
        return None


def logo_bg_image(W, H, path, dim=0.40):
    """音频可视化的底图：logo 等比铺满、柔化压暗，波形在前面跑"""
    from PIL import Image, ImageEnhance, ImageFilter
    try:
        W = max(2, int(W))
        H = max(2, int(H))
        key = (W, H, path, round(float(dim), 2))
    except Exception:
        return None
    hit = _LOGO_BG_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        src = Image.open(path).convert("RGB")
        k = max(W / float(src.width), H / float(src.height))
        im = src.resize((max(1, int(src.width * k)), max(1, int(src.height * k))),
                        Image.BILINEAR)
        ox = max(0, (im.width - W) // 2)
        oy = max(0, (im.height - H) // 2)
        im = im.crop((ox, oy, ox + W, oy + H))
        im = im.filter(ImageFilter.GaussianBlur(max(1, int(min(W, H) * 0.006))))
        im = ImageEnhance.Brightness(im).enhance(float(dim))
        im = ImageEnhance.Color(im).enhance(1.15)
    except Exception:
        return None
    if len(_LOGO_BG_CACHE) > 4:
        _LOGO_BG_CACHE.clear()
    _LOGO_BG_CACHE[key] = im
    return im


# ----------------------------------------------------------------- 配置

def load_config():
    p = os.path.join(app_dir(), CONFIG_NAME)
    cfg = {"drive": "", "card_path": "", "temp_dir": "",
           "vbase": "480x272_h264b", "vrate": 1200, "vprofile": "psp_std",
           "mp3_bitrate": 320, "mp3_sr": 44100, "copy_mp3": True, "naming": 0,
           "db_root": "", "last_dir": "",
           "auto_play": True, "loop_mode": 0, "fit_mode": "cover", "aspect": 0,
           "vid_layout": "video", "audio_fmt": "mp3", "group_author": False,
           "logo_path": "logo/折线文件夹.png"}
    if os.path.isfile(p):
        try:
            cfg.update(json.loads(open(p, encoding="utf-8").read()))
        except Exception:
            pass
    # 老配置里存过 vid_layout="psp"（MP_ROOT 编号目录，实测掌机不认），一律归到 VIDEO
    if cfg.get("vid_layout") not in ("video", "author"):
        cfg["vid_layout"] = "author" if cfg.get("group_author") else "video"
    # 老配置只勾了 group_author、没有 vid_layout：以勾选为准，别让两个控件的状态打架
    if cfg.get("group_author"):
        cfg["vid_layout"] = "author"
    return cfg


def save_config(cfg):
    try:
        with open(os.path.join(app_dir(), CONFIG_NAME), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------- GUI

_CV2_STATE = None      # None=还没试过，True/False=opencv 是否可用于全屏显示


class FsVideoWindow:
    """全屏视频显示窗口（OpenCV）。

    tkinter 的 PhotoImage 在 2560×1440 上每帧要 56~60ms（约 17fps），换谁画 Canvas/Label
    都一样，瓶颈是 Tk 的位图贴图；同样尺寸用 OpenCV 窗口 + cv2.resize 只要 11ms 左右，
    30fps 有充足余量。交互（Esc / 双击退出 / 右键切画面模式 / 滚轮调音量 / 点进度条）
    也在这个窗口里接，事件丢回主线程处理。
    """

    TITLE = "PSP 媒体管理器 - 全屏播放"

    def __init__(self, size, q, fit="cover", aspect=0.0, hint=None, fps=30.0):
        self.w, self.h = max(320, int(size[0])), max(180, int(size[1]))
        self.q = q
        self.fit = fit
        self.aspect = float(aspect or 0.0)
        self.hint = hint
        self.fps = max(8.0, float(fps or 30.0))
        self.frac = 0.0
        self.bgr = False                # 全屏解码器直接给 bgr24，省掉每帧一次通道反转
        self.title_text = ""
        self._latest = None
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None
        self._buf = None
        self._click = (0.0, 0, 0)
        self._hint_img = None
        self._dead = False
        self.shown = 0

    # ---------- 主线程侧 ----------
    def open(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def push(self, rgb):
        """送一帧 numpy RGB（H×W×3 uint8），只保留最新一帧"""
        with self._lock:
            self._latest = rgb

    def close(self):
        self._stop = True
        t = self._thread
        if t is not None:
            try:
                t.join(timeout=1.2)
            except Exception:
                pass
        self._thread = None

    @property
    def alive(self):
        return (not self._dead) and self._thread is not None and self._thread.is_alive()

    # ---------- 显示线程 ----------
    def _loop(self):
        try:
            import cv2
            import numpy as np
        except Exception:
            self._dead = True
            self.q.put(("error", None))
            return
        cv2.namedWindow(self.TITLE, cv2.WINDOW_NORMAL)
        try:
            cv2.setWindowProperty(self.TITLE, cv2.WND_PROP_FULLSCREEN,
                                  cv2.WINDOW_FULLSCREEN)
        except Exception:
            try:
                cv2.resizeWindow(self.TITLE, self.w, self.h)
            except Exception:
                pass
        try:
            cv2.setWindowProperty(self.TITLE, cv2.WND_PROP_TOPMOST, 1)
        except Exception:
            pass
        try:
            cv2.setMouseCallback(self.TITLE, self._mouse, cv2)
        except Exception:
            pass
        self._buf = np.zeros((self.h, self.w, 3), np.uint8)
        try:
            cv2.imshow(self.TITLE, self._buf)
            cv2.waitKey(1)
        except Exception:
            self._dead = True
            return
        self._hint_img = self._make_hint(cv2, np)
        last = time.time()
        while not self._stop:
            arr = None
            with self._lock:
                if self._latest is not None:
                    arr = self._latest
                    self._latest = None
            if arr is not None:
                try:
                    self._paint(arr, cv2)
                    cv2.imshow(self.TITLE, self._buf)
                    self.shown += 1
                except Exception:
                    pass
            k = cv2.waitKey(1)
            if k == 27:
                self.q.put(("exit", None))
                self._stop = True
            elif k in (ord(" "), ord("\r"), 13):
                self.q.put(("toggle", None))
            gap = 1.0 / self.fps - (time.time() - last)
            if arr is not None and gap > 0.002:
                time.sleep(min(0.06, gap))
            last = time.time()
        try:
            cv2.destroyWindow(self.TITLE)
        except Exception:
            pass
        self._dead = True

    def _make_hint(self, cv2, np):
        """把提示文字用 PIL 渲一次（cv2 自带字体不支持中文），之后每帧直接叠加"""
        text = "  ESC 退出全屏    双击退出    右键切换画面    滚轮调音量  "
        try:
            from PIL import Image as PImage, ImageDraw, ImageFont
            fnt = None
            for name in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf"):
                p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
                if os.path.isfile(p):
                    try:
                        fnt = ImageFont.truetype(p, 26)
                        break
                    except Exception:
                        fnt = None
            if fnt is None:
                return None
            im = PImage.new("RGBA", (1, 1))
            dr = ImageDraw.Draw(im)
            box = dr.textbbox((0, 0), text, font=fnt)
            w = max(2, box[2] - box[0] + 16)
            h = max(2, box[3] - box[1] + 14)
            tile = PImage.new("RGBA", (w, h), (0, 0, 0, 0))
            d2 = ImageDraw.Draw(tile)
            d2.rectangle([0, 0, w - 1, h - 1], fill=(8, 12, 18, 150))
            d2.text((8, 6 - box[1]), text, font=fnt, fill=(244, 250, 255, 235))
            arr = np.array(tile)
            return (arr[:, :, 2].copy(), arr[:, :, :3][:, :, ::-1].copy())
        except Exception:
            return None

    def _mouse(self, event, x, y, flags, param):
        cv2 = param
        try:
            if event == cv2.EVENT_LBUTTONDOWN:
                if y >= self.h - 34:                # 点在底部进度条上 → 跳转
                    x0, x1 = 16, self.w - 16
                    f = min(1.0, max(0.0, (x - x0) / float(max(1, x1 - x0))))
                    self.q.put(("seek", f))
                    return
                now = time.time()
                lf, lx, ly = self._click
                if now - lf < 0.35 and abs(x - lx) < 30 and abs(y - ly) < 30:
                    self._click = (0.0, 0, 0)
                    self.q.put(("exit", None))
                else:
                    self._click = (now, x, y)
            elif event == cv2.EVENT_RBUTTONDOWN:
                self.q.put(("fit", None))
            elif getattr(cv2, "EVENT_MOUSEWHEEL", -1) == event:
                self.q.put(("vol", 1 if flags > 0 else -1))
        except Exception:
            pass

    def _paint(self, rgb, cv2):
        import numpy as np
        buf = self._buf
        sh, sw = rgb.shape[:2]
        if (sw, sh) == (self.w, self.h):
            # 解码器已经按整屏尺寸裁/垫好了：直接拷像素，别再缩放一次
            buf[:] = rgb if self.bgr else rgb[:, :, ::-1]
            self._chrome(cv2)
            return
        buf[:] = 0
        bw, bh = self.w, self.h
        asp = float(self.aspect or 0.0)
        if asp > 0.01:
            if (bw / float(bh)) > asp:
                bw = max(2, int(bh * asp))
            else:
                bh = max(2, int(bw / asp))
        if self.fit == "cover":
            r = max(bw / float(sw), bh / float(sh))
            cw = max(1, min(sw, int(round(bw / r))))
            ch = max(1, min(sh, int(round(bh / r))))
            x0 = max(0, (sw - cw) // 2)
            y0 = max(0, (sh - ch) // 2)
            part = np.ascontiguousarray(rgb[y0:y0 + ch, x0:x0 + cw])
            dst = cv2.resize(part, (bw, bh), interpolation=cv2.INTER_LINEAR)
        else:
            r = min(bw / float(sw), bh / float(sh))
            tw = max(2, int(round(sw * r)))
            th = max(2, int(round(sh * r)))
            src = rgb if (tw, th) == (sw, sh) else cv2.resize(
                np.ascontiguousarray(rgb), (tw, th), interpolation=cv2.INTER_LINEAR)
            dst = src
        ox = (self.w - dst.shape[1]) // 2
        oy = (self.h - dst.shape[0]) // 2
        buf[oy:oy + dst.shape[0], ox:ox + dst.shape[1]] = (dst if self.bgr
                                                            else dst[:, :, ::-1])
        self._chrome(cv2)

    def _chrome(self, cv2):
        """画在图上的那层东西：底部进度条 + 右上角操作提示"""
        buf = self._buf
        # 底部进度条：已播橙色、未播浅灰，圆点收尾
        x0, x1, y = 16, self.w - 16, self.h - 11
        try:
            cv2.line(buf, (x0, y), (x1, y), (191, 178, 168), 3, cv2.LINE_AA)
            px = int(x0 + (x1 - x0) * min(1.0, max(0.0, float(self.frac or 0.0))))
            if px > x0:
                cv2.line(buf, (x0, y), (px, y), (46, 154, 255), 3, cv2.LINE_AA)
            cv2.circle(buf, (px, y), 7, (255, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(buf, (px, y), 5, (77, 180, 255), -1, cv2.LINE_AA)
        except Exception:
            pass
        if self._hint_img is not None:
            mask, col = self._hint_img
            hh, ww = mask.shape[:2]
            oy2, ox2 = 8, self.w - ww - 8
            if ox2 > 0 and oy2 + hh <= self.h:
                sub = buf[oy2:oy2 + hh, ox2:ox2 + ww]
                sub[mask > 0] = col[mask > 0]


def build_gui():
    import io
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk, filedialog, messagebox
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk, ImageChops

    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        root_cls = TkinterDnD.Tk
        has_dnd = True
    except Exception:
        root_cls = tk.Tk
        has_dnd = False

    cfg = load_config()
    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")

    # ---- 字体
    def cjk_font(size, bold=True):
        names = ["msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc"] if bold \
            else ["msyh.ttc", "simhei.ttf", "simsun.ttc"]
        for n in names:
            p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", n)
            if os.path.isfile(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    continue
        return ImageFont.load_default()

    # ---- 渐变工具
    def vgrad(w, h, stops):
        img = Image.new("RGB", (1, h))
        px = img.load()
        for y in range(h):
            t = y / max(1, h - 1)
            c = stops[0][1]
            for i in range(1, len(stops)):
                p0, c0 = stops[i - 1]
                p1, c1 = stops[i]
                if p0 <= t <= p1:
                    k = (t - p0) / max(1e-6, p1 - p0)
                    c = tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))
                    break
                c = c1
            px[0, y] = c
        return img.resize((w, h), Image.BILINEAR)

    def aero_button(text, w, h, fs=16, state="normal", sym=None, flag=True):
        if state == "normal":
            stops = [(0.0, (255, 249, 219)), (0.42, (247, 199, 92)),
                     (0.46, (233, 160, 22)), (0.53, (243, 179, 60)),
                     (1.0, (253, 226, 160))]
            border = (183, 126, 20)
        elif state == "hover":
            stops = [(0.0, (255, 253, 240)), (0.42, (253, 219, 140)),
                     (0.46, (246, 186, 56)), (0.53, (252, 205, 110)),
                     (1.0, (255, 240, 195))]
            border = (206, 150, 40)
        elif state == "disabled":
            stops = [(0.0, (244, 246, 249)), (0.5, (225, 229, 236)),
                     (1.0, (241, 244, 248))]
            border = (180, 188, 200)
        else:
            stops = [(0.0, (232, 196, 140)), (0.4, (222, 160, 60)),
                     (0.5, (205, 138, 30)), (1.0, (238, 205, 160))]
            border = (160, 108, 16)
        img = vgrad(w, h, stops).convert("RGB")
        gloss = Image.new("L", (w, h), 0)
        gd = ImageDraw.Draw(gloss)
        gd.rectangle([0, 0, w, int(h * 0.5)], fill=110)
        gloss = gloss.filter(__import__("PIL.ImageFilter", fromlist=["x"]).GaussianBlur(2))
        white = Image.new("RGB", (w, h), (255, 255, 255))
        img = Image.composite(white, img, gloss.point(lambda v: int(v * 0.55)))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], 3, fill=255)
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        d = ImageDraw.Draw(out)
        d.rounded_rectangle([0, 0, w - 1, h - 1], 3, outline=border, width=1)
        if sym:                              # 几何符号，不靠字体，避免缺字变方块
            cx, cy = w / 2.0, h / 2.0
            s = min(w, h) * 0.32
            ink = (150, 156, 166) if state == "disabled" else (10, 10, 10)
            if sym in ("loop", "loop1", "order", "shuffle") and not flag:
                ink = (112, 118, 128)       # 没点亮：符号发灰，一眼看出没开
            if sym == "play":
                d.polygon([(cx - s * 0.55, cy - s), (cx - s * 0.55, cy + s),
                           (cx + s * 0.95, cy)], fill=ink)
            elif sym == "pause":
                bw = max(3, int(s * 0.55))
                gap = max(2, int(s * 0.4))
                d.rectangle([cx - gap - bw, cy - s, cx - gap, cy + s], fill=ink)
                d.rectangle([cx + gap, cy - s, cx + gap + bw, cy + s], fill=ink)
            elif sym == "stop":
                half = s * 0.72
                d.rectangle([cx - half, cy - half, cx + half, cy + half], fill=ink)
            elif sym == "vol":              # 喇叭 + 两道声波
                d.polygon([(cx - s * 0.95, cy - s * 0.36),
                           (cx - s * 0.55, cy - s * 0.36),
                           (cx - s * 0.02, cy - s * 0.95),
                           (cx - s * 0.02, cy + s * 0.95),
                           (cx - s * 0.55, cy + s * 0.36),
                           (cx - s * 0.95, cy + s * 0.36)], fill=ink)
                d.arc([cx + s * 0.1, cy - s * 0.72, cx + s * 1.05, cy + s * 0.72],
                      start=-60, end=60, fill=ink, width=max(2, int(s * 0.3)))
                d.arc([cx + s * 0.1, cy - s * 1.05, cx + s * 1.4, cy + s * 1.05],
                      start=-60, end=60, fill=ink, width=max(2, int(s * 0.3)))
            elif sym == "mute":             # 喇叭 + 一道叉，表示静音
                d.polygon([(cx - s * 0.95, cy - s * 0.36),
                           (cx - s * 0.55, cy - s * 0.36),
                           (cx - s * 0.02, cy - s * 0.95),
                           (cx - s * 0.02, cy + s * 0.95),
                           (cx - s * 0.55, cy + s * 0.36),
                           (cx - s * 0.95, cy + s * 0.36)], fill=ink)
                lw = max(2, int(s * 0.3))
                d.line([(cx + s * 0.28, cy - s * 0.6), (cx + s * 1.06, cy + s * 0.6)],
                       fill=ink, width=lw)
                d.line([(cx + s * 1.06, cy - s * 0.6), (cx + s * 0.28, cy + s * 0.6)],
                       fill=ink, width=lw)
            elif sym == "camera":           # 相机：机身轮廓 + 镜头圈 + 顶部取景凸起
                lw = max(2, int(s * 0.24))
                d.rounded_rectangle([cx - s * 1.05, cy - s * 0.62,
                                     cx + s * 1.05, cy + s * 0.82], 3,
                                    outline=ink, width=lw)
                d.ellipse([cx - s * 0.42, cy - s * 0.2,
                           cx + s * 0.42, cy + s * 0.62], outline=ink, width=lw)
                d.rectangle([cx - s * 0.34, cy - s * 0.98, cx + s * 0.22,
                             cy - s * 0.56], outline=ink, width=lw)
            elif sym == "full":             # 四角张开
                ln = max(3, int(s * 0.5))
                bw = max(2, int(s * 0.22))
                hx, hy = cx + s * 0.92, cy + s * 0.72
                for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                    px, py = cx + sx * s * 0.92, cy + sy * s * 0.72
                    d.line([(px, py), (px - sx * ln, py)], fill=ink, width=bw)
                    d.line([(px, py), (px, py - sy * ln)], fill=ink, width=bw)
            elif sym == "shrink":           # 四角收拢
                ln = max(3, int(s * 0.5))
                bw = max(2, int(s * 0.22))
                for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                    px, py = cx + sx * s * 0.42, cy + sy * s * 0.32
                    d.line([(px, py), (px - sx * ln, py)], fill=ink, width=bw)
                    d.line([(px, py), (px, py - sy * ln)], fill=ink, width=bw)
            elif sym == "fast":             # 双三角（变速）
                t = s * 0.8
                d.polygon([(cx - t * 1.75, cy - t), (cx - t * 1.75, cy + t),
                           (cx - t * 0.25, cy)], fill=ink)
                d.polygon([(cx - t * 0.15, cy - t), (cx - t * 0.15, cy + t),
                           (cx + t * 1.35, cy)], fill=ink)
            elif sym == "order":            # 顺序：一列条目 + 向右箭头（播完就停）
                lw = max(2, int(s * 0.26))
                for i in (-0.72, 0.0, 0.72):
                    d.line([(cx - s * 1.15, cy + i * s * 0.6),
                            (cx - s * 0.15, cy + i * s * 0.6)], fill=ink, width=lw)
                d.polygon([(cx + s * 1.2, cy), (cx + s * 0.4, cy - s * 0.72),
                           (cx + s * 0.4, cy + s * 0.72)], fill=ink)
            elif sym in ("loop", "loop1"):  # 循环：圆环 + 右上箭头；loop1 中间多一个点
                lw = max(2, int(s * 0.3))
                r = s * 1.0
                d.arc([cx - r, cy - r * 0.82, cx + r, cy + r * 0.82],
                      start=300, end=240, fill=ink, width=lw)
                d.polygon([(cx + r * 1.0, cy - r * 0.98),
                           (cx + r * 1.0, cy - r * 0.02),
                           (cx + r * 0.28, cy - r * 0.5)], fill=ink)
                if sym == "loop1":
                    d.rectangle([cx - s * 0.16, cy - s * 0.26,
                                 cx + s * 0.16, cy + s * 0.26], fill=ink)
            elif sym == "shuffle":          # 随机：两条交叉箭头
                lw = max(2, int(s * 0.26))
                d.line([(cx - s * 1.2, cy - s * 0.6), (cx - s * 0.15, cy - s * 0.6)],
                       fill=ink, width=lw)
                d.line([(cx - s * 0.15, cy - s * 0.6), (cx + s * 0.62, cy + s * 0.6)],
                       fill=ink, width=lw)
                d.line([(cx - s * 1.2, cy + s * 0.6), (cx - s * 0.15, cy + s * 0.6)],
                       fill=ink, width=lw)
                d.line([(cx - s * 0.15, cy + s * 0.6), (cx + s * 0.62, cy - s * 0.6)],
                       fill=ink, width=lw)
                d.polygon([(cx + s * 1.22, cy - s * 0.62),
                           (cx + s * 0.5, cy - s * 1.02),
                           (cx + s * 0.55, cy - s * 0.24)], fill=ink)
                d.polygon([(cx + s * 1.22, cy + s * 0.62),
                           (cx + s * 0.5, cy + s * 1.02),
                           (cx + s * 0.55, cy + s * 0.24)], fill=ink)
            elif sym == "fit":              # 适合窗口：外框 + 内缩的实心块（整幅装进去）
                lw = max(2, int(s * 0.2))
                d.rectangle([cx - s * 1.02, cy - s * 0.74,
                             cx + s * 1.02, cy + s * 0.74], outline=ink, width=lw)
                d.rectangle([cx - s * 0.56, cy - s * 0.3,
                             cx + s * 0.56, cy + s * 0.3], fill=ink)
            elif sym == "crop":             # 裁切：画框超出边界，四角裁切线
                lw = max(2, int(s * 0.2))
                d.rectangle([cx - s * 0.66, cy - s * 0.46,
                             cx + s * 1.12, cy + s * 0.84], outline=ink, width=lw)
                for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                    px, py = cx + sx * s * 0.66, cy + sy * s * 0.46
                    d.line([(px - sx * s * 0.46, py), (px, py)], fill=ink, width=lw)
                    d.line([(px, py), (px, py - sy * s * 0.46)], fill=ink, width=lw)
            elif sym == "prev":             # 上一条：左三角 + 最左竖线
                d.rectangle([cx - s * 1.24, cy - s * 0.92,
                             cx - s * 1.0, cy + s * 0.92], fill=ink)
                d.polygon([(cx + s * 1.0, cy - s * 0.9),
                           (cx + s * 1.0, cy + s * 0.9),
                           (cx - s * 0.58, cy)], fill=ink)
            elif sym == "next":             # 下一条：右三角 + 最右竖线
                d.rectangle([cx + s * 1.0, cy - s * 0.92,
                             cx + s * 1.24, cy + s * 0.92], fill=ink)
                d.polygon([(cx - s * 1.0, cy - s * 0.9),
                           (cx - s * 1.0, cy + s * 0.9),
                           (cx + s * 0.58, cy)], fill=ink)
            elif sym == "ratio":            # 比例：外框 + 里面一个宽扁画框
                lw = max(2, int(s * 0.2))
                d.rectangle([cx - s * 1.05, cy - s * 0.8,
                             cx + s * 1.05, cy + s * 0.8], outline=ink, width=lw)
                d.rectangle([cx - s * 0.74, cy - s * 0.4,
                             cx + s * 0.74, cy + s * 0.4], outline=ink, width=lw)
            return ImageTk.PhotoImage(out)
        font = cjk_font(fs)
        tw = d.textlength(text, font=font)
        d.text(((w - tw) / 2, (h - fs * 1.35) / 2), text, font=font, fill=(10, 10, 10))
        return ImageTk.PhotoImage(out)

    def flat_button(text, w, h, fs=12, fg=(15, 15, 15), top=(255, 255, 255), bot=(214, 214, 220)):
        img = vgrad(w, h, [(0.0, top), (1.0, bot)]).convert("RGB")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], 3, fill=255)
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        d = ImageDraw.Draw(out)
        d.rounded_rectangle([0, 0, w - 1, h - 1], 3, outline=(140, 140, 150), width=1)
        font = cjk_font(fs)
        tw = d.textlength(text, font=font)
        d.text(((w - tw) / 2, (h - fs * 1.35) / 2), text, font=font, fill=fg)
        return ImageTk.PhotoImage(out)

    def btn_width(text, fs=11, pad=26, mini=58):
        """按文字宽度算按钮宽度，保证所有按钮留白一致"""
        f = cjk_font(fs)
        d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        return max(mini, int(d.textlength(text, font=f)) + pad * 2)

    BG_TOP = (223, 230, 238)
    BG_BOT = (247, 200, 216)
    BAR_H = 46
    STATUS_H = 38

    class ImgButton(tk.Label):
        def __init__(self, master, text, cmd, w, h, fs=16, kind="aero", bg="#dfe6ee",
                     sym=None, flag=True):
            # 属性名全带 ib_ 前缀：tk.Label 自己会占 _w/_text，撞名就被覆盖
            self._ib_text, self._ib_w, self._ib_h, self._ib_fs = text, w, h, fs
            self._ib_kind, self._ib_bg = kind, bg
            self._ib_sym = sym
            self._ib_flag = flag
            self._enabled = True
            self._make_imgs()
            tk.Label.__init__(self, master, image=self.imgs["normal"], bd=0,
                              bg=bg, cursor="hand2")
            self.cmd = cmd
            self.bind("<Enter>", lambda e: self._enabled and self._show("hover"))
            self.bind("<Leave>", lambda e: self._show(
                (self._base or "normal") if self._enabled else "disabled"))
            self.bind("<ButtonPress-1>", lambda e: self._enabled and
                      self.config(image=self.imgs["press"]))
            self.bind("<ButtonRelease-1>", self._click)
            self._base = "normal"

        def _make_imgs(self):
            text = self._ib_text
            w, h, fs = self._ib_w, self._ib_h, self._ib_fs
            sym = self._ib_sym
            if self._ib_kind == "aero":
                fl = self._ib_flag
                self.imgs = {"normal": aero_button(text, w, h, fs, "normal", sym, fl),
                             "hover": aero_button(text, w, h, fs, "hover", sym, fl),
                             "press": aero_button(text, w, h, fs, "press", sym, fl),
                             "disabled": aero_button(text, w, h, fs, "disabled", sym, fl)}
            else:
                self.imgs = {"normal": flat_button(text, w, h, fs),
                             "hover": flat_button(text, w, h, fs, (245, 248, 255),
                                                  (188, 200, 220)),
                             "press": flat_button(text, w, h, fs, (198, 205, 218),
                                                  (168, 178, 196)),
                             "disabled": flat_button(text, w, h, fs, (154, 160, 172),
                                                    (246, 248, 250), (226, 230, 236))}

        def set_sym(self, sym):
            """换按钮上的几何符号（play / pause / stop）"""
            if sym == self._ib_sym:
                return
            self._ib_sym = sym
            self._make_imgs()
            self._show(self._base or "normal")

        def set_flag(self, flag):
            """随机/循环这类开关：符号从灰变黑，表示已点亮"""
            if flag == self._ib_flag:
                return
            self._ib_flag = bool(flag)
            self._make_imgs()
            self._show(self._base or "normal")

        def set_text(self, text, w=None):
            """换按钮上的文字；给 w 就同时改宽度（倍速这类文字长度会变的用）"""
            if w is not None:
                w = int(w)
            if text == self._ib_text and (w is None or w == self._ib_w):
                return
            self._ib_text = text
            if w is not None:
                self._ib_w = w
            self._make_imgs()
            self._show(self._base or "normal")

        def set_enabled(self, on):
            """无视频时按钮置灰，点了不响应"""
            on = bool(on)
            if on == self._enabled:
                if on:
                    self._show(self._base or "normal")
                return
            self._enabled = on
            self._show((self._base or "normal") if on else "disabled")

        def _show(self, st):
            self.config(image=self.imgs[st])

        def set_active(self, on):
            self._base = "press" if on else "normal"
            self._show(self._base)

        def _click(self, e):
            if not self._enabled:
                return
            self._show("hover")
            if self.cmd:
                self.cmd()

    class App(root_cls):
        def __init__(self):
            super().__init__()
            self.title(APP_NAME)
            # 默认尺寸按桌面可用区域（不含任务栏）收敛，保证底部状态栏按钮不被切掉
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            wa = ctypes.create_string_buffer(16)
            try:
                ctypes.windll.user32.SystemParametersInfoW(0x30, 0, wa, 0)   # SPI_GETWORKAREA
                wl, wt, wr, wb = ctypes.cast(wa, ctypes.POINTER(ctypes.c_long * 4)).contents
                sw, sh = wr - wl, wb - wt
            except Exception:
                pass
            dw = min(1120, max(980, sw - 40))
            dh = min(700, max(600, sh - 80))
            dx = max(10, (sw - dw) // 2)
            dy = max(10, (sh - dh) // 3)
            self.geometry("%dx%d+%d+%d" % (dw, dh, dx, dy))
            self.minsize(980, 620)
            self.configure(bg="#dfe6ee")
            self.w, self.h = dw, dh
            try:
                ico = os.path.join(res_dir(), "psp.ico")
                if os.path.isfile(ico):
                    self.iconbitmap(ico)
            except Exception:
                pass

            self.cfg = cfg
            self.items = []
            self.done = 0
            self.fail = 0
            self.running = False
            self.stop_flag = False
            self.msgs = pyqueue.Queue()
            self.db = None
            self.db_rows = []
            self.card_rows = []
            self.info_cache = {}
            self.t0 = time.time()
            self.page = "card"
            self.pages = {}

            self.canvas = tk.Canvas(self, highlightthickness=0, bg="#dfe6ee")
            self.canvas.pack(fill="both", expand=True)
            self.bg_item = None
            self.bar_item = None
            self.status_item = None
            self.notice_win = None
            self.body_win = None
            self.table_win = None
            self.status_text = None
            self.canvas.bind("<Configure>", self._on_resize)

            self._build_toolbar()
            self._build_body()
            self._build_status()
            self._bind_dnd()
            self.protocol("WM_DELETE_WINDOW", self._close_app)
            self.after(120, self._poll_msgs)
            self.show_page("card")

            if not ffmpeg or not ffprobe:
                messagebox.showwarning(
                    "缺少 ffmpeg",
                    "没有找到 ffmpeg/ffprobe。\n\n请把 ffmpeg.exe 与 ffprobe.exe 放到本程序同目录的 bin 文件夹里。")

        # ---------------- 布局
        def _close_app(self):
            """关窗口时把所有后台播放/转码进程一并收走，别留 ffmpeg、ffplay 在后台"""
            for fn in ("game_stop_play", "db_exit_fs", "db_stop", "_db_audio_stop"):
                try:
                    f = getattr(self, fn, None)
                    if callable(f):
                        f()
                except Exception:
                    pass
            try:
                kill_all_procs()
            except Exception:
                pass
            try:
                self.destroy()
            except Exception:
                pass

        def _on_resize(self, e):
            w, h = e.width, e.height
            self.w, self.h = w, h
            bg = vgrad(w, h, [(0.0, BG_TOP), (0.45, (236, 231, 238)), (1.0, BG_BOT)])
            self.bg_img = ImageTk.PhotoImage(bg)
            if self.bg_item is None:
                self.bg_item = self.canvas.create_image(0, 0, anchor="nw", image=self.bg_img)
            else:
                self.canvas.itemconfig(self.bg_item, image=self.bg_img)
                self.canvas.coords(self.bg_item, 0, 0)
            bar = vgrad(w, BAR_H, [(0.0, (245, 250, 253)), (1.0, (214, 233, 244))]
                        ).convert("RGB")
            self.bar_img = ImageTk.PhotoImage(bar)
            if self.bar_item is None:
                self.bar_item = self.canvas.create_image(0, 0, anchor="nw", image=self.bar_img)
            else:
                self.canvas.itemconfig(self.bar_item, image=self.bar_img)
                self.canvas.coords(self.bar_item, 0, 0)
            st = vgrad(w, STATUS_H, [(0.0, (159, 147, 233)), (1.0, (113, 96, 205))]
                       ).convert("RGB")
            self.status_img = ImageTk.PhotoImage(st)
            sy = h - STATUS_H
            if self.status_item is None:
                self.status_item = self.canvas.create_image(0, sy, anchor="nw",
                                                           image=self.status_img)
                self.canvas.create_line(0, sy, w, sy, fill="#5d4eaa")
            else:
                self.canvas.itemconfig(self.status_item, image=self.status_img)
                self.canvas.coords(self.status_item, 0, sy)
            self.canvas.tag_lower(self.bg_item)
            for it in (self.bar_item, self.notice_win, self.status_item,
                       self.status_text, self.pbar_bg, self.pbar):
                if it:
                    self.canvas.tag_raise(it)
            self._layout()

        def _layout(self):
            w_set = self.btn_set.winfo_reqwidth()
            self.canvas.coords(self.btn_set_id, self.w - 8 - w_set, 6)
            # 说明框（独立子画布，自带裁剪，向上滚动）
            nx = self.notice_x
            nw_ = max(120, self.w - nx - w_set - 18)
            lh = max(12, int(getattr(self, "marquee_lh", 16)))
            nh_ = min(BAR_H - 6, lh * 2 + 2)
            self.canvas.coords(self.notice_win, nx, 4)
            self.canvas.itemconfig(self.notice_win, width=nw_, height=nh_)
            self.notice_canvas.configure(width=nw_, height=nh_)
            for it in (self.marquee_id, self.marquee_id2):
                try:
                    self.notice_canvas.itemconfig(it, width=nw_ - 14)
                except Exception:
                    pass
            self.marquee_h = nh_
            # 内容区（底部留 2px，避免页内最后一行按钮被主体窗口裁掉边）
            pad = 8
            cw = self.w - pad * 2
            ch = self.h - BAR_H - STATUS_H - 6
            self.canvas.coords(self.body_win, pad, BAR_H + 4)
            self.canvas.itemconfig(self.body_win, width=cw, height=ch)
            # 状态文字与进度条
            self._place_status_buttons()
            self.canvas.coords(self.status_text, 10, self.h - STATUS_H + STATUS_H / 2)
            self.status_maxw = max(100, self.btn_left - 12 - 340)
            self.pbar_x1 = max(340, self.btn_left - 12)
            self.pbar_x0 = 330
            self._refresh_status()

        def _place_status_buttons(self):
            gap, right = 6, 8
            w_clear = self.btn_clear.winfo_reqwidth()
            w_stop = self.btn_stop.winfo_reqwidth()
            w_start = self.btn_start.winfo_reqwidth()
            x_clear = self.w - right - w_clear
            x_stop = x_clear - gap - w_stop
            x_start = x_stop - gap - w_start
            y = self.h - STATUS_H + 3
            self.canvas.coords(self.btn_start._id, x_start, y)
            self.canvas.coords(self.btn_stop._id, x_stop, y)
            self.canvas.coords(self.btn_clear._id, x_clear, y)
            self.btn_left = x_start

        def _btn(self, master, text, cmd, h=30, fs=11, bg="#eef2f7", w=None, kind="aero",
                 sym=None, flag=True):
            """统一按钮工厂：全程序同一套 Aero 橙渐变 + 同圆角"""
            return ImgButton(master, text, cmd, w or btn_width(text, fs), h, fs,
                             kind=kind, bg=bg, sym=sym, flag=flag)

        def _build_toolbar(self):
            tab_w = 156
            self.notice_x = 8 + tab_w * 4 + 3 * 2 + 14
            self.btn_card = ImgButton(self.canvas, "PSP 记忆棒",
                                      lambda: self.show_page("card"), tab_w, BAR_H - 12)
            self.btn_queue = ImgButton(self.canvas, "媒体转换器",
                                       lambda: self.show_page("queue"), tab_w, BAR_H - 12)
            self.btn_db = ImgButton(self.canvas, "DY 数据库",
                                    lambda: self.show_page("db"), tab_w, BAR_H - 12)
            self.btn_game = ImgButton(self.canvas, "PSP 游戏",
                                      lambda: self.show_page("game"), tab_w, BAR_H - 12)
            for i, b in enumerate((self.btn_card, self.btn_queue, self.btn_db, self.btn_game)):
                self.canvas.create_window(8 + i * (tab_w + 2), 6, anchor="nw", window=b)
            # 说明（小字号，向上滚动）
            self.notice_canvas = tk.Canvas(self.canvas, width=200, height=BAR_H - 8,
                                           bg="#dcf2fe", highlightthickness=1,
                                           highlightbackground="#96c4da", bd=0)
            self.notice_win = self.canvas.create_window(self.notice_x, 4, anchor="nw",
                                                        window=self.notice_canvas)
            self.marquee_h = BAR_H - 8
            self.marquee_text = self._notice_text()
            self.marquee_id = self.notice_canvas.create_text(
                8, 2, anchor="nw", width=200, text=self.marquee_text,
                font=("Microsoft YaHei UI", 9), fill="#12506f", justify="left")
            # 第二段紧接在第一段后面，用来做"无缝循环"：一段滚出顶部就接到另一段下面
            self.marquee_id2 = self.notice_canvas.create_text(
                8, 2, anchor="nw", width=200, text=self.marquee_text,
                font=("Microsoft YaHei UI", 9), fill="#12506f", justify="left")
            # 量一个字的行高，滚动判边界用
            probe = self.notice_canvas.create_text(0, 0, text="测",
                                                   font=("Microsoft YaHei UI", 9))
            lh = self.notice_canvas.bbox(probe)
            self.notice_canvas.delete(probe)
            self.marquee_lh = (lh[3] - lh[1]) if lh else 16
            self._marq_relayout()
            self._tick_marquee()
            # 设置
            self.btn_set = self._btn(self.canvas, "设置", self.open_settings, h=BAR_H - 12, fs=12,
                                     bg="#dfe6ee")
            self.btn_set_id = self.canvas.create_window(940, 7, anchor="nw", window=self.btn_set)

        def _notice_text(self):
            """顶部说明：读同目录下的「说明.txt」，占位符换成实时值"""
            card = card_root(self.cfg) or "未设置（点右上【设置】）"
            temp = temp_path(self.cfg)
            prof = resolve_profile(self.cfg)["label"]
            txt = read_notice()
            root = card_root(self.cfg)
            vdir = vid_dir(self.cfg, root) if root else "未设置"
            af = self.cfg.get("audio_fmt") or "mp3"
            if af == "wav":
                afmt = "WAV %sHz 无损" % int(self.cfg.get("mp3_sr") or 44100)
            elif af == "wma":
                afmt = "WMA %sk / %sHz" % (int(self.cfg.get("mp3_bitrate") or 320),
                                          int(self.cfg.get("mp3_sr") or 44100))
            else:
                afmt = "MP3 %sk / %sHz" % (int(self.cfg.get("mp3_bitrate") or 320),
                                          int(self.cfg.get("mp3_sr") or 44100))
            for k, v in (("{card}", card), ("{vdir}", vdir), ("{temp}", temp),
                         ("{profile}", prof), ("{afmt}", afmt),
                         ("{mp3_sr}", str(int(self.cfg.get("mp3_sr") or 44100))),
                         ("{mp3}", str(int(self.cfg.get("mp3_bitrate") or 320)))):
                txt = txt.replace(k, v)
            return txt

        def _marq_relayout(self):
            """说明文字换了以后重排两段：第一段从顶上开始，第二段接在它下面"""
            try:
                cv = self.notice_canvas
                for it in (self.marquee_id, self.marquee_id2):
                    cv.itemconfig(it, text=self.marquee_text,
                                  width=max(120, cv.winfo_width() - 14))
                cv.coords(self.marquee_id, 8, 2)
                b1 = cv.bbox(self.marquee_id)
                cv.coords(self.marquee_id2, 8, (b1[3] if b1 else 24) + 16)
                self._marq_t0 = time.time()
            except Exception:
                pass

        def _tick_marquee(self):
            """说明缓慢向上滚：每 35ms 上移 1px，一段滚出顶部就接到另一段下面，永不跳行"""
            try:
                cv = self.notice_canvas
                b1 = cv.bbox(self.marquee_id)
                b2 = cv.bbox(self.marquee_id2)
                gap = 16
                if b1 and b2:
                    self._marq_px = getattr(self, "_marq_px", 0) + 1
                    if self._marq_px >= 2:          # 两帧走 1px，约 14px/s，很慢
                        self._marq_px = 0
                        if b1[3] <= 0:
                            cv.coords(self.marquee_id, 8, b2[3] + gap)
                        elif b2[3] <= 0:
                            cv.coords(self.marquee_id2, 8, b1[3] + gap)
                        cv.move(self.marquee_id, 0, -1)
                        cv.move(self.marquee_id2, 0, -1)
            except Exception:
                pass
            self.after(35, self._tick_marquee)

        # ---------------- 主体（三页切换）
        def _build_body(self):
            self.body = tk.Frame(self.canvas, bg="#eef2f7", bd=0)
            self.body_win = self.canvas.create_window(8, BAR_H + 8, anchor="nw", window=self.body)
            # 顶部一条：左边是各页自己的地址栏（切页时替换），右边是全局唯一的播放控件
            self.band = tk.Frame(self.body, bg="#eef2f7")
            self.band.pack(fill="x", pady=(0, 6))
            self.addr_host = tk.Frame(self.band, bg="#eef2f7")
            self.addr_host.pack(side="left", fill="x", expand=True)
            self.page_bars = {}
            self._db_ctl_state = (False, None)
            self._pb_vol_on = False
            try:
                self.loop_mode = max(0, min(2, int(self.cfg.get("loop_mode") or 0)))
            except Exception:
                self.loop_mode = 0          # 0 单条循环 / 1 顺序停止 / 2 列表循环
            self.shuffle = False
            self._shuf_done = set()
            self.media_list = []            # 当前页左侧列表（循环/随机都基于它）
            self.media_idx = 0
            self.wave_mode = False
            self.wave_env = []
            self.wave_fx = 0                # 可视化特效：0 频谱条 / 1 镜像 / 2 环形 / 3 波形 / 4 粒子
            self.db_wave_id = None
            self.views = {}                 # 每页一套画面 canvas（同一套播放核心共用）
            self.panes = {}                 # 三页各自的左列表 + 右详情分栏
            self.detail_w = 470             # 三页共用的详情宽度，拖任一页都会带上
            self._db_prog_sets = []
            self._build_playbar(self.band)
            self._build_card_page()
            self._build_queue_page()
            self._build_db_page()
            self._build_game_page()
            self._build_settings()          # 内嵌设置页（不弹窗）
            self._settings_open = False
            # 通用表格样式
            style = ttk.Style()
            try:
                style.theme_use("clam")
            except Exception:
                pass
            style.configure("Mgr.Treeview", background="#f7f9fc", fieldbackground="#f7f9fc",
                            foreground="#202020", rowheight=24, borderwidth=0,
                            font=("Microsoft YaHei UI", 10))
            style.configure("Mgr.Treeview.Heading", background="#e2e9f3",
                            foreground="#1b2437", font=("Microsoft YaHei UI", 10, "bold"),
                            relief="flat")
            style.map("Mgr.Treeview", background=[("selected", "#a9c7e8")],
                      foreground=[("selected", "#101010")])
            style.layout("Mgr.Treeview", [("Mgr.Treeview.treearea", {"sticky": "nswe"})])

        def _make_tree(self, parent, cols, heights=None, tree=False):
            fr = tk.Frame(parent, bg="#f3f5f9", bd=1, relief="solid")
            if tree:
                tv = ttk.Treeview(fr, columns=[c[0] for c in cols], show="tree headings",
                                  style="Mgr.Treeview", selectmode="browse")
                tv.heading("#0", text="名称")
                tv.column("#0", width=360, anchor="w")
            else:
                tv = ttk.Treeview(fr, columns=[c[0] for c in cols], show="headings",
                                  style="Mgr.Treeview", selectmode="browse")
            for c, t, w in cols:
                tv.heading(c, text=t)
                # minwidth 卡住原宽：分栏被拖窄时列不会挤成一团，而是出现横向滚动
                tv.column(c, width=w, minwidth=w, anchor="w")
            hsb = ttk.Scrollbar(fr, orient="horizontal", command=tv.xview)
            hsb.pack(side="bottom", fill="x")
            sb = ttk.Scrollbar(fr, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
            tv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            return fr, tv

        # ---- 全局播放控件（地址栏右侧，四页共用这一套）
        def _build_playbar(self, parent):
            bar = tk.Frame(parent, bg="#f7f9fc", bd=1, relief="solid")
            bar.pack(side="right", padx=(8, 0))
            self.pb_bar = bar
            BW, BH = 26, 22
            self.db_time = tk.Label(bar, text="00:00 / 00:00", bg="#f7f9fc",
                                    fg="#5a6472", font=("Microsoft YaHei UI", 9),
                                    width=11, anchor="w")
            self.db_time.pack(side="left", padx=(8, 4))
            # 循环方式（单条循环 / 顺序播放 / 全部循环）与随机播放
            self.db_loop_btn = self._btn(bar, "", self.pb_loop, h=BH, fs=11,
                                        bg="#f7f9fc", w=28,
                                        sym=self.LOOP_SYMS[getattr(self, "loop_mode", 0)])
            self.db_loop_btn.pack(side="left")
            self.db_shuf_btn = self._btn(bar, "", self.pb_shuffle, h=BH, fs=11,
                                        bg="#f7f9fc", w=28, sym="shuffle",
                                        flag=False)
            self.db_shuf_btn.pack(side="left", padx=(4, 0))
            # 上一条 / 播放 / 下一条：一串放在一起，中间就是播放键
            self.db_prev_btn = self._btn(bar, "", lambda: self.pb_step(-1), h=BH, fs=11,
                                         bg="#f7f9fc", w=24, sym="prev")
            self.db_prev_btn.pack(side="left", padx=(6, 0))
            self.db_play_btn = self._btn(bar, "", self.pb_play, h=BH, fs=11,
                                        bg="#f7f9fc", w=BW, sym="play")
            self.db_play_btn.pack(side="left", padx=(3, 0))
            self.db_next_btn = self._btn(bar, "", lambda: self.pb_step(1), h=BH, fs=11,
                                         bg="#f7f9fc", w=24, sym="next")
            self.db_next_btn.pack(side="left", padx=(3, 0))
            self.db_stop_btn = self._btn(bar, "", self.pb_stop, h=BH, fs=11,
                                        bg="#f7f9fc", w=BW, sym="stop")
            self.db_stop_btn.pack(side="left", padx=(4, 0))
            # 倍速：按钮上直接写倍速文字（字号大一级，宽度只留一点点边）
            self.db_speed_btn = self._btn(bar, self._sp_label(getattr(self, "db_speed", 1.0)),
                                          self.pb_speed, h=BH, fs=12, bg="#f7f9fc",
                                          w=self._sp_width(getattr(self, "db_speed", 1.0)))
            self.db_speed_btn.pack(side="left", padx=(4, 0))
            self.db_shot_btn = self._btn(bar, "", self.pb_shot, h=BH, fs=11,
                                        bg="#f7f9fc", w=BW, sym="camera")
            self.db_shot_btn.pack(side="left", padx=(4, 0))
            # 画面适配：适合窗口（可留黑边） / 裁切（填满不掉边）
            self.db_fit_btn = self._btn(bar, "", self.db_toggle_fit, h=BH, fs=11,
                                        bg="#f7f9fc", w=BW, sym="fit")
            self.db_fit_btn.pack(side="left", padx=(4, 0))
            # 画面比例：点开下拉菜单选常见比例（原全屏按钮挪到这里）
            self.db_fs_btn = self._btn(bar, "", self.pb_aspect, h=BH, fs=11,
                                       bg="#f7f9fc", w=BW, sym="ratio")
            self.db_fs_btn.pack(side="left", padx=(4, 0))
            self.db_vol_btn = self._btn(bar, "", self.pb_mute, h=BH, fs=11,
                                       bg="#f7f9fc", w=BW, sym="vol")
            self.db_vol_btn.pack(side="right", padx=(0, 6))
            self.db_vol_cv = tk.Canvas(bar, width=104, height=BH, bg="#f7f9fc",
                                       highlightthickness=0, bd=0, cursor="hand2")
            self.db_vol_cv.pack(side="right", padx=(8, 6))
            self.db_vol_cv.bind("<Button-1>", self._db_vol_press)
            self.db_vol_cv.bind("<B1-Motion>", self._db_vol_press)
            self.db_vol_cv.bind("<ButtonRelease-1>", self._db_vol_release)
            self.db_vol_cv.bind("<Configure>", lambda e: self._db_vol_draw())
            self._bind_wheel_vol(bar)

        def _is_media_page(self):
            """前三个页共用一套播放核心"""
            return getattr(self, "page", "") in ("db", "card", "queue")

        def pb_play(self):
            """播放键：按当前页分发——前三页走同一套，游戏页走 PMF 播放"""
            page = getattr(self, "page", "")
            if self._is_media_page():
                self.db_play_toggle()
            elif page == "game":
                self.game_toggle_play()

        def pb_step(self, d):
            """上/下一条：在当前页的列表里前后移动，到头绕回另一端"""
            page = getattr(self, "page", "")
            if page not in ("db", "card", "queue"):
                self._pb_tip("这一页只有播放和停止可用")
                return
            items = getattr(self, "media_list", None) or []
            if not items:
                self._pb_tip("列表里还没有条目")
                return
            cur = int(getattr(self, "media_idx", 0) or 0)
            if getattr(self, "shuffle", False) and len(items) > 1:
                # 随机开着：上/下一条都是随机挑一条没播过的，手动切歌不受「播完停止」限制
                done = set(getattr(self, "_shuf_done", set()))
                done.add(cur)
                rest = [k for k in range(len(items)) if k not in done]
                if not rest:                 # 整轮都播过了：重新开一轮
                    rest = [k for k in range(len(items)) if k != cur]
                    done = {cur}
                # 有别的作者就优先跳去别的作者，别在同一作者里绕
                cur_auth = items[cur].get("author") if cur < len(items) else None
                other = [k for k in rest
                         if items[k].get("author") not in (None, cur_auth)]
                pool = other or rest
                i = random.choice(pool) if pool else cur
                self._shuf_done = done | {i}
            else:
                i = (cur + d) % len(items)
            self._db_open(items[i], i, autoplay=True)
            self._pb_sync_row(i)      # 提示行留给 _db_open 的文件信息，不再抢

        def _pb_sync_row(self, idx):
            """把左侧列表的选中行挪到正在播的那一条（不回头再触发一次播放）"""
            items = getattr(self, "media_list", None) or []
            if idx >= len(items):
                return
            page = getattr(self, "page", "")
            self._pb_quiet = True
            try:
                if page == "queue":
                    iid = str(items[idx].get("queue_idx", idx))
                    if self.tree.exists(iid):
                        self.tree.selection_set(iid)
                        self.tree.see(iid)
                elif page == "db":
                    if (getattr(self, "db_view", "") == "authors"
                            and getattr(self, "db_authors", None)):
                        # 作者列表在播：左侧高亮跟着正在播的那位作者走
                        aid = items[idx].get("author_aid")
                        if aid:
                            for a_i, a in enumerate(self.db_authors):
                                iid = str(a_i)
                                if a.get("aid") == aid and self.db_a_tree.exists(iid):
                                    self.db_a_tree.selection_set(iid)
                                    self.db_a_tree.see(iid)
                                    break
                    else:
                        iid = str(idx)
                        if self.db_v_tree.exists(iid):
                            self.db_v_tree.selection_set(iid)
                            self.db_v_tree.see(iid)
                elif page == "card":
                    path = items[idx].get("path")
                    for iid, it in self.card_map.items():
                        if it.get("path") == path:
                            self.card_tree.selection_set(iid)
                            self.card_tree.see(iid)
                            break
            except Exception:
                pass
            finally:
                self._pb_quiet = False

        def pb_stop(self):
            page = getattr(self, "page", "")
            if self._is_media_page():
                self.db_stop()
            elif page == "game":
                self.game_stop_play()

        def pb_speed(self):
            if self._is_media_page():
                self.db_speed_menu()

        def pb_shot(self):
            if self._is_media_page():
                self.db_snapshot()

        def pb_full(self):
            if self._is_media_page():
                self.db_fullscreen()

        # 常见画面比例（宽/高）：0 = 跟随片源
        ASPECTS = ((0.0, "原始比例（跟随片源）"), (16 / 9.0, "16:9 宽屏"),
                   (4 / 3.0, "4:3 标清"), (1.0, "1:1 方形"), (9 / 16.0, "9:16 竖屏"),
                   (21 / 9.0, "21:9 超宽"), (2.35, "2.35:1 影院"),
                   (4 / 5.0, "4:5 竖版"))

        def pb_aspect(self):
            """比例按钮：弹出下拉菜单选画面比例"""
            if not self._is_media_page():
                return
            m = tk.Menu(self, tearoff=0, font=("Microsoft YaHei UI", 9))
            cur = float(getattr(self, "db_aspect", 0.0) or 0.0)
            for val, label in self.ASPECTS:
                mark = "✓ " if abs(val - cur) < 1e-6 else "    "
                m.add_command(label=mark + label,
                              command=lambda v=val, t=label: self.db_set_aspect(v, t))
            m.add_separator()
            m.add_command(label="    适合窗口 / 裁切（切换）",
                          command=self.db_toggle_fit)
            m.add_command(label="    全屏（双击画面也可以）", command=self.db_fullscreen)
            try:
                btn = self.db_fs_btn
                m.tk_popup(btn.winfo_rootx(),
                           btn.winfo_rooty() + btn.winfo_height() + 2)
            finally:
                try:
                    m.grab_release()
                except Exception:
                    pass
            self._asp_menu = m

        def db_set_aspect(self, val, label):
            self.db_aspect = float(val or 0.0)
            self.cfg["aspect"] = self.db_aspect
            save_config(self.cfg)
            self._db_redraw()
            self._pb_tip("画面比例：%s" % label)

        def pb_mute(self):
            page = getattr(self, "page", "")
            if self._is_media_page() or page == "game":
                self.db_mute_toggle()

        # 循环三态：0 = 单条循环（默认），1 = 顺序播完停止，2 = 列表循环
        LOOP_SYMS = ("loop1", "order", "loop")
        LOOP_TIPS = ("单条循环", "顺序播放（播完停止）", "列表循环")

        def pb_loop(self):
            self.loop_mode = (getattr(self, "loop_mode", 0) + 1) % 3
            self.cfg["loop_mode"] = self.loop_mode     # 记住，下次启动还是这种方式
            save_config(self.cfg)
            try:
                self.db_loop_btn.set_sym(self.LOOP_SYMS[self.loop_mode])
            except Exception:
                pass
            self._pb_tip("循环：%s" % self.LOOP_TIPS[self.loop_mode])

        def pb_shuffle(self):
            self.shuffle = not getattr(self, "shuffle", False)
            if self.shuffle:
                self._shuf_done = set()     # 新一轮随机：清空"已播过"的记录
            try:
                self.db_shuf_btn.set_flag(self.shuffle)
            except Exception:
                pass
            self._pb_tip("随机播放：%s" % ("开（整库随机，手动上/下一条也随机）"
                                        if self.shuffle else "关（按列表顺序）"))

        def _pb_tip(self, text):
            """把模式提示写到当前页的提示行"""
            page = getattr(self, "page", "")
            lb = {"db": getattr(self, "db_hint", None),
                  "card": getattr(self, "card_hint2", None),
                  "queue": getattr(self, "queue_hint2", None)}.get(page)
            if lb is not None:
                try:
                    lb.configure(text=text)
                except Exception:
                    pass

        def _pb_apply(self):
            """按当前页重新决定这套控件的可用性；PSP 游戏页只开播放/停止"""
            page = getattr(self, "page", "")
            on, play_only = getattr(self, "_db_ctl_state", (False, None))
            all_btn = (self.db_play_btn, self.db_prev_btn, self.db_next_btn,
                       self.db_stop_btn, self.db_speed_btn,
                       self.db_shot_btn, self.db_fit_btn, self.db_fs_btn,
                       self.db_vol_btn)
            mode_btn = (self.db_loop_btn, self.db_shuf_btn)
            for b in all_btn + mode_btn:
                try:
                    b.set_enabled(False)
                except Exception:
                    pass
            self._pb_vol_on = False
            if page in ("db", "card", "queue"):
                # 这三个页的详情是一套：有列表就能播、能切循环与随机
                for b in mode_btn:
                    try:
                        b.set_enabled(True)
                    except Exception:
                        pass
            if page in ("db", "card", "queue") and on:
                for b in all_btn:
                    try:
                        b.set_enabled(True)
                    except Exception:
                        pass
                if play_only is False:          # 纯音频条目不参与播放
                    self.db_play_btn.set_enabled(False)
                    self.db_stop_btn.set_enabled(False)
                elif play_only == "play":       # 作者列表待播：只开播放键
                    for b in all_btn:
                        if b is not self.db_play_btn:
                            try:
                                b.set_enabled(False)
                            except Exception:
                                pass
                self._pb_vol_on = play_only != "play"
            elif page == "game":
                has = bool(getattr(self, "game_video_src", None))
                audio = bool(getattr(self, "game_audio_on", False))
                try:
                    self.db_play_btn.set_enabled(has)
                    self.db_stop_btn.set_enabled(has)
                    self.db_play_btn.set_sym(
                        "pause" if getattr(self, "game_state", "idle") == "playing"
                        else "play")
                    # 游戏页的音量要能调：选中镜像即可拖，播放中实时生效
                    self.db_vol_btn.set_enabled(has or audio)
                except Exception:
                    pass
                self._pb_vol_on = bool(has or audio)
                try:
                    # 游戏页没有时间轴，别显示 00:00 / 00:00 让人以为没加载
                    if has:
                        self.db_time.configure(
                            text="游戏音乐" if audio else "镜像无视频")
                    else:
                        self.db_time.configure(text="00:00 / 00:00")
                except Exception:
                    pass
            else:
                try:
                    self.db_time.configure(text="%s / %s" % (
                        _fmt_time(getattr(self, "db_pos", 0)),
                        _fmt_time(getattr(self, "db_dur", 0))))
                except Exception:
                    pass
            try:
                self._db_vol_draw()
            except Exception:
                pass

        def _show_page_bar(self, name):
            for k, f in getattr(self, "page_bars", {}).items():
                try:
                    f.pack_forget()
                except Exception:
                    pass
            bar = getattr(self, "page_bars", {}).get(name)
            if bar is not None:
                bar.pack(fill="x", expand=True)

        def _make_pane(self, parent, page):
            """三页共用的可拖分栏：左列表、右详情，拖完记下详情宽度给其它页跟随"""
            pane = tk.PanedWindow(parent, orient="horizontal", sashwidth=7,
                                  sashrelief="flat", bg="#c6d2e2", bd=0,
                                  handlepad=0)
            left = tk.Frame(pane, bg="#eef2f7")
            right = tk.Frame(pane, bg="#eef2f7")
            pane.add(left, minsize=300, stretch="always")
            pane.add(right, minsize=430, stretch="always")
            self.panes[page] = pane
            pane.bind("<ButtonRelease-1>", lambda e: self._pane_drag_done(page))
            return pane, left, right

        def _pane_drag_done(self, page):
            pane = getattr(self, "panes", {}).get(page)
            if pane is None:
                return
            try:
                w = pane.winfo_width()
                x = pane.sash_coord(0)[0]
                self.detail_w = max(300, w - x)
            except Exception:
                return
            self._pane_apply(page)

        def _pane_apply(self, skip=None):
            """把记住的详情宽度套到其它页，拖哪一页都一样宽"""
            for k, pane in getattr(self, "panes", {}).items():
                if k == skip:
                    continue
                try:
                    w = pane.winfo_width()
                    if w < 40:
                        continue
                    x = max(320, w - self.detail_w)
                    if x > w - 300:
                        x = max(320, w - 300)
                    pane.sash_place(0, x, 0)
                except Exception:
                    pass

        def _make_media_detail(self, parent, page):
            """统一的播放详情：画面（进度条画在画面底部）+ 下方两行信息"""
            fr = tk.Frame(parent, bg="#f7f9fc", bd=1, relief="solid")
            cv = tk.Canvas(fr, bg="#101014", highlightthickness=0, bd=0,
                           cursor="hand2")
            cv.pack(fill="both", expand=True, padx=8, pady=(8, 0))
            cv.bind("<Configure>", self._db_on_canvas_conf)
            cv.bind("<Double-1>", lambda e: self.db_fullscreen())
            cv.bind("<Button-1>", self._db_prog_press)
            cv.bind("<B1-Motion>", self._db_prog_move)
            cv.bind("<ButtonRelease-1>", self._db_prog_release)
            cv.bind("<Button-3>", lambda e: self.db_toggle_fit())
            cv.bind("<MouseWheel>", self._db_wheel_vol, add="+")
            img_id = cv.create_image(0, 0, anchor="center")
            wave_id = cv.create_image(0, 0, anchor="center", state="hidden")
            tip_id = cv.create_text(0, 0, anchor="center", text="选中后显示画面",
                                    fill="#8a93a5", font=("Microsoft YaHei UI", 10))
            tk_track = cv.create_line(0, 0, 1, 1, fill="#a8b2bf", width=2)
            tk_fill = cv.create_line(0, 0, 1, 1, fill="#ff9a2e", width=2)
            tk_dot = cv.create_oval(0, 0, 1, 1, fill="#ffb44d",
                                    outline="#ffffff", width=1)
            self._db_prog_sets.append((cv, tk_track, tk_fill, tk_dot))
            hint = tk.Label(fr, text="", bg="#f7f9fc", fg="#5a6472", anchor="w",
                            justify="left", font=("Microsoft YaHei UI", 9))
            hint.pack(fill="x", padx=10, pady=(2, 6))
            author = tk.Label(fr, text="", bg="#f7f9fc", fg="#31465f", anchor="w",
                              justify="left", font=("Microsoft YaHei UI", 9, "bold"))
            cap = tk.Label(fr, text="", bg="#f7f9fc", fg="#6b7686", anchor="w",
                           justify="left", font=("Microsoft YaHei UI", 9))
            v = {"cv": cv, "img": img_id, "wave": wave_id, "tip": tip_id,
                 "hint": hint, "author": author, "cap": cap, "fr": fr}
            self.views[page] = v
            return fr, v

        def _pv(self):
            """当前页的画面视图（同一套播放核心共用）"""
            views = getattr(self, "views", {})
            page = getattr(self, "page", "")
            v = views.get(page) or views.get("db")
            if v and getattr(self, "db_hint", None) is not v["hint"]:
                # 老的 self.db_hint.configure 调用自动落到当前页，并带上文件信息
                self.db_hint = HintProxy(v)
            return v

        def _pv_hint(self, text):
            v = self._pv()
            if not v:
                return
            v["hint"].configure(text=(text or ""))
            self.db_hint = HintProxy(v)

        def _db_set_video_author(self, name, page=None):
            """播放详情信息上方那行「作者：xxx」；没有作者（记忆棒/添加媒体）就收起"""
            views = getattr(self, "views", {})
            pg = page or getattr(self, "page", "")
            v = views.get(pg) or views.get("db")
            if not v or "author" not in v:
                return
            lb = v["author"]
            nm = (name or "").strip()
            if not nm:
                try:
                    lb.pack_forget()
                except Exception:
                    pass
                return
            lb.configure(text="作者：%s" % nm)
            try:
                if not lb.winfo_ismapped():
                    lb.pack(fill="x", padx=10, pady=(2, 0), before=v["hint"])
            except Exception:
                pass

        def _db_set_video_caption(self, text, show=True, page=None):
            """详情里的「文案：xxx」：作者列表页显示；视频列表页只留作者名"""
            views = getattr(self, "views", {})
            pg = page or getattr(self, "page", "")
            v = views.get(pg) or views.get("db")
            if not v or "cap" not in v:
                return
            lb = v["cap"]
            t = (text or "").strip().replace("\n", " ").replace("\r", " ")
            if len(t) > 60:
                t = t[:60] + "…"
            if not show or not t:
                try:
                    lb.pack_forget()
                except Exception:
                    pass
                return
            lb.configure(text="文案：%s" % t)
            try:
                if not lb.winfo_ismapped():
                    lb.pack(fill="x", padx=10, pady=(0, 0), before=v["hint"])
            except Exception:
                pass

        # ---- 记忆棒页
        def _build_card_page(self):
            p = tk.Frame(self.body, bg="#eef2f7")
            self.pages["card"] = p
            top = tk.Frame(self.addr_host, bg="#eef2f7")
            self.page_bars["card"] = top
            self.card_label = tk.Label(top, text="记忆棒：未设置", bg="#eef2f7", anchor="w",
                                       font=("Microsoft YaHei UI", 10))
            self.card_label.pack(side="left")
            self._btn(top, "刷新", self.refresh_card).pack(side="right")
            self._btn(top, "打开目录", self.open_card_dir).pack(side="right", padx=(0, 6))
            body = tk.Frame(p, bg="#eef2f7")
            body.pack(fill="both", expand=True)
            pane, left, right = self._make_pane(body, "card")
            pane.pack(fill="both", expand=True)
            cols = (("kind", "类型", 80), ("size", "大小", 90), ("date", "修改时间", 150))
            self.card_tree_fr, self.card_tree = self._make_tree(left, cols, tree=True)
            self.card_tree_fr.pack(fill="both", expand=True)
            self.card_info, v = self._make_media_detail(right, "card")
            self.card_info.pack(fill="both", expand=True)
            self.card_hint2 = v["hint"]
            self.card_hint = tk.Label(self.card_tree_fr,
                                      text="（提示：请在设置里添加记忆棒目录）",
                                      bg="#f7f9fc", fg="#788294",
                                      font=("Microsoft YaHei UI", 16), justify="center")
            self.card_tree.bind("<<TreeviewSelect>>", self._on_card_select)
            self.card_tree.bind("<<TreeviewOpen>>", self._on_card_open)
            self.card_tree.bind("<Double-1>", self._on_card_dclick)
            self.card_map = {}
            self._card_seq = 0

        # ---- 队列页
        def _build_queue_page(self):
            p = tk.Frame(self.body, bg="#eef2f7")
            self.pages["queue"] = p
            top = tk.Frame(self.addr_host, bg="#eef2f7")
            self.page_bars["queue"] = top
            tk.Label(top, text="转换队列（可拖入文件 / 文件夹）", bg="#eef2f7",
                     font=("Microsoft YaHei UI", 10)).pack(side="left")
            self._btn(top, "添加文件", self.add_files).pack(side="right")
            self._btn(top, "移除选中", self.remove_selected).pack(side="right", padx=(0, 6))
            self._btn(top, "打开源文件", self.play_selected).pack(side="right", padx=(0, 6))
            body = tk.Frame(p, bg="#eef2f7")
            body.pack(fill="both", expand=True)
            pane, left, right = self._make_pane(body, "queue")
            pane.pack(fill="both", expand=True)
            cols = (("idx", "序号", 52), ("kind", "类型", 62), ("author", "作者", 130),
                    ("file", "文件", 300), ("info", "状态", 230))
            self.queue_tree_fr, self.tree = self._make_tree(left, cols)
            self.queue_tree_fr.pack(fill="both", expand=True)
            self.tree.configure(selectmode="extended")
            self.queue_info, v = self._make_media_detail(right, "queue")
            self.queue_info.pack(fill="both", expand=True)
            self.queue_hint2 = v["hint"]
            self.hint = tk.Label(self.queue_tree_fr,
                                 text="把视频 / 音频文件拖进这个窗口\n或点右上【添加文件】",
                                 bg="#f7f9fc", fg="#788294",
                                 font=("Microsoft YaHei UI", 14), justify="center")
            self.tree.bind("<Delete>", lambda e: self.remove_selected())
            self.tree.bind("<<TreeviewSelect>>", self._on_queue_select)
            self.tree.bind("<Double-1>", lambda e: self._db_ensure_play())

        # ---- 数据库页（作者列表 → 双击进视频列表；右侧可拖宽窄的播放区）
        def _build_db_page(self):
            p = tk.Frame(self.body, bg="#eef2f7")
            self.pages["db"] = p
            self.db_view = "authors"
            self.db_play_panel = False      # 右侧是否显示播放界面（作者列表下播放时才切过去）
            self.db_ai_mode = False         # 是否处于"从所选作者起播全部作者"的模式
            self.db_cur_author = None
            self.db_cur_video = None
            self.db_videos = []
            self.db_authors = []
            self.db_state = "idle"
            self.db_player = None
            self.db_audio_proc = None
            self.db_frame = None
            self.db_free = None
            self.db_dur = 0.0
            self.db_speed = 1.0
            self.db_vol = 70
            self.db_vol_cache = 70
            self.db_muted = False
            self._db_sym_muted = False
            self.db_drag = False
            self.db_speed_pop = None
            self.db_seek_base = 0.0
            self.db_t0 = 0.0
            self.db_pos = 0.0
            self.db_pump_id = None
            self.db_tick_id = None
            self.db_fs = None
            self.fs_win = None            # 全屏视频的 OpenCV 窗口（Tk 贴 2K 太慢）
            self._fs_q = pyqueue.Queue()  # 全屏窗口里产生的事件，主线程轮询处理
            self._fs_poll_id = None

            top = tk.Frame(self.addr_host, bg="#eef2f7")
            self.page_bars["db"] = top
            self.db_path_var = tk.StringVar(value=self.cfg.get("db_root") or "")
            self.db_back_btn = self._btn(top, "← 作者列表", self.db_back_authors)
            self.db_browse_btn = self._btn(top, "浏览…", self.db_browse)
            ent = tk.Entry(top, textvariable=self.db_path_var,
                           font=("Microsoft YaHei UI", 9))
            # 用 grid 钉在一行：Entry 吃满剩余，右侧两个按钮固定在最右
            top.grid_columnconfigure(0, weight=1)
            ent.grid(row=0, column=0, sticky="ew")
            self.db_browse_btn.grid(row=0, column=1, padx=(6, 0), sticky="e")
            self.db_back_btn.grid(row=0, column=2, padx=(6, 0), sticky="e")

            # 左右两栏用 PanedWindow，中间的 sash 可拖动调宽度（三页共用同一套）
            pane, left, right = self._make_pane(p, "db")
            pane.pack(fill="both", expand=True)
            self.db_pane = self.panes["db"]

            a_cols = (("name", "作者", 150), ("uid", "UID", 130),
                      ("sig", "签名", 260), ("cnt", "视频数量", 80))
            self.db_a_tree_fr, self.db_a_tree = self._make_tree(left, a_cols)
            self.db_a_tree_fr.pack(fill="both", expand=True)
            v_cols = (("title", "视频文案", 260), ("vid", "视频ID", 130),
                      ("res", "分辨率", 80), ("size", "大小", 70), ("dur", "时长", 60))
            self.db_v_tree_fr, self.db_v_tree = self._make_tree(left, v_cols)
            self.db_a_tree.bind("<<TreeviewSelect>>", self._on_db_author_select)
            self.db_a_tree.bind("<Double-1>", self._on_db_author_dbl)
            self.db_v_tree.bind("<<TreeviewSelect>>", self._on_db_video_select)
            self.db_v_tree.bind("<Double-1>", lambda e: self._db_ensure_play())

            # ---- 右侧上：作者详情（头像 + 更多信息）
            self.db_author_fr = tk.Frame(right, bg="#f7f9fc", bd=1, relief="solid")
            head_box = tk.Frame(self.db_author_fr, width=300, height=300, bg="#eef2f7")
            head_box.pack(pady=(10, 6))
            head_box.pack_propagate(False)
            self.db_head = tk.Label(head_box, bg="#eef2f7", text="无头像",
                                    fg="#8a93a5", font=("Microsoft YaHei UI", 10))
            self.db_head.pack(fill="both", expand=True)
            self.db_txt = tk.Text(self.db_author_fr, wrap="char", bd=0, bg="#f7f9fc",
                                  fg="#202020", font=("Microsoft YaHei UI", 9),
                                  cursor="arrow", height=8, width=10)
            self.db_txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            self.db_txt.configure(state="disabled")

            # ---- 右侧下：画面（与记忆棒/添加媒体同一套详情组件）
            self.db_fit = "contain" if self.cfg.get("fit_mode") == "contain" else "cover"
            self.db_aspect = float(self.cfg.get("aspect") or 0.0)
            self.db_video_fr, v = self._make_media_detail(right, "db")
            self.db_canvas = v["cv"]
            self.db_img_id = v["img"]
            self.db_tip_id = v["tip"]
            self.db_hint = v["hint"]
            self.db_hint.configure(text="双击列表里的视频即可播放")

            self.db_author_fr.pack(fill="both", expand=True)

            bottom = tk.Frame(p, bg="#eef2f7")
            bottom.pack(fill="x", pady=(6, 4))
            self.db_status = tk.Label(bottom, text="点【浏览…】选到含 .appdata 的目录即自动载入",
                                      bg="#eef2f7", anchor="w",
                                      font=("Microsoft YaHei UI", 9))
            self.db_status.pack(side="left")
            self._btn(bottom, "添加该作者全部", lambda: self.db_add(True)).pack(side="right")
            self._btn(bottom, "添加选中", lambda: self.db_add(False)).pack(side="right",
                                                                       padx=(0, 6))
            self._db_apply_view()
            self.after(80, self._db_init_ctl)

        # ---- PSP 游戏页（读 .iso / .cso 里的图标、背景图、元信息、音乐、视频）
        def _make_game_info(self, parent):
            """游戏页详情：背景图+图标合成图 / 视频画面，下面挂播放控件"""
            fr = tk.Frame(parent, bg="#f7f9fc", bd=1, relief="solid")
            tk.Label(fr, text="详情", bg="#e2e9f3", anchor="w",
                     font=("Microsoft YaHei UI", 10, "bold")).pack(fill="x")
            # 图和控制条放进同一个 300 宽的盒子：按钮左边缘正好贴着画面左边
            box = tk.Frame(fr, bg="#f7f9fc")
            box.pack(pady=(8, 0))
            img = tk.Label(box, bg="#101014", bd=0)
            img.pack()
            img.bind("<MouseWheel>", self._db_wheel_vol, add="+")
            # 播放/停止已经搬到顶部全局控件条，这里只留一行状态提示
            self.game_play_hint = tk.Label(box, text="", bg="#f7f9fc", fg="#5a6472",
                                           anchor="w", font=("Microsoft YaHei UI", 9))
            self.game_play_hint.pack(fill="x", pady=(6, 4))
            tk.Frame(fr, bg="#c6d2e2", height=1).pack(fill="x", padx=8, pady=(6, 0))
            txt = tk.Text(fr, wrap="char", bd=0, bg="#f7f9fc", fg="#202020",
                          font=("Microsoft YaHei UI", 9), cursor="arrow", height=8)
            txt.pack(fill="both", expand=True, padx=8, pady=(8, 8))
            txt.configure(state="disabled")
            return fr, img, txt

        def _build_game_page(self):
            p = tk.Frame(self.body, bg="#eef2f7")
            self.pages["game"] = p
            top = tk.Frame(self.addr_host, bg="#eef2f7")
            self.page_bars["game"] = top
            tk.Label(top, text="镜像目录", bg="#eef2f7",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            self.game_path_var = tk.StringVar(value=self._game_default_dir())
            ent = tk.Entry(top, textvariable=self.game_path_var,
                           font=("Microsoft YaHei UI", 9))
            ent.pack(side="left", fill="x", expand=True, padx=(6, 0))
            self._btn(top, "浏览…", self.game_browse).pack(side="left", padx=(6, 0))
            self._btn(top, "扫描", self.game_scan).pack(side="left", padx=(6, 0))
            body = tk.Frame(p, bg="#eef2f7")
            body.pack(fill="both", expand=True)
            cols = (("title", "游戏名", 290), ("file", "文件", 282),
                    ("size", "大小", 84), ("kind", "镜像", 56))
            self.game_tree_fr, self.game_tree = self._make_tree(body, cols)
            self.game_tree_fr.pack(side="left", fill="both", expand=True)
            self.game_info, self.game_img, self.game_txt = self._make_game_info(body)
            self.game_info.configure(width=340)
            self.game_info.pack_propagate(False)
            self.game_info.pack(side="left", fill="y", padx=(8, 0))
            self.game_tree.bind("<<TreeviewSelect>>", self._on_game_select)
            self.game_tree.bind("<Double-1>", lambda e: self.game_files())
            bottom = tk.Frame(p, bg="#eef2f7")
            bottom.pack(fill="x", pady=(6, 4))
            self.game_status = tk.Label(bottom, text="选好目录后点【扫描】",
                                        bg="#eef2f7", anchor="w",
                                        font=("Microsoft YaHei UI", 9))
            self.game_status.pack(side="left")
            self._btn(bottom, "内部文件", self.game_files).pack(side="right")
            self._btn(bottom, "导出视频", self.game_export_video).pack(side="right", padx=(0, 6))
            self._btn(bottom, "导出音乐", self.game_export_music).pack(side="right", padx=(0, 6))
            self._btn(bottom, "导出背景", lambda: self._game_export_png("pic1")).pack(
                side="right", padx=(0, 6))
            self._btn(bottom, "导出图标", lambda: self._game_export_png("icon")).pack(
                side="right", padx=(0, 6))
            self.game_map = {}
            self.game_cur = None
            self._game_cache = {}
            self._game_playing = False
            self.game_meta_cur = None
            self.game_state = "idle"
            self.game_video_src = None
            self.game_audio_src = None
            self.game_audio_on = False
            self.game_audio_proc = None
            self.game_player = None
            self.game_play_after = None
            self.game_pump_id = None

        def _build_settings(self):
            """内嵌设置页：点右上「设置」在主体区展开，不再弹独立窗口"""
            fr = tk.Frame(self.body, bg="#f4f7fb", bd=1, relief="solid")
            self.settings_page = fr
            LAB = ("Microsoft YaHei UI", 10)
            HINT = ("Microsoft YaHei UI", 8)
            BG, FG = "#f4f7fb", "#27313f"

            head = tk.Frame(fr, bg=BG)
            head.pack(fill="x", padx=18, pady=(14, 2))
            tk.Label(head, text="设置", bg=BG, fg="#204060",
                     font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
            tk.Label(head, text="保存后写进配置文件，重开也生效",
                     bg=BG, fg="#63707f", font=HINT).pack(side="left", padx=(10, 0))

            body = tk.Frame(fr, bg=BG)
            body.pack(fill="x", padx=18, pady=(0, 4))
            body.columnconfigure(0, weight=1, uniform="col")
            body.columnconfigure(1, weight=1, uniform="col")

            L = tk.Frame(body, bg=BG)          # 左列：目录 + 播放行为
            L.grid(row=0, column=0, sticky="new", padx=(0, 16))
            L.columnconfigure(1, weight=1)
            R = tk.Frame(body, bg=BG)          # 右列：格式 + logo + 说明
            R.grid(row=0, column=1, sticky="new")
            R.columnconfigure(1, weight=1)

            def row(fr, r, text, hint=""):
                tk.Label(fr, text=text, bg=BG, fg=FG, font=LAB, anchor="w").grid(
                    row=r, column=0, sticky="w", pady=(10, 2))
                if hint:
                    tk.Label(fr, text=hint, bg=BG, fg="#63707f", font=HINT,
                             justify="left", anchor="w", wraplength=430).grid(
                        row=r + 1, column=0, columnspan=3, sticky="w")

            # ==== 左列
            # 1) 记忆棒目录
            row(L, 0, "记忆棒目录", "可填盘符（G:）或任意目录（G:\\PSP）")
            self.set_card_var = tk.StringVar(
                value=self.cfg.get("card_path") or
                ((str(self.cfg.get("drive")) + ":\\") if self.cfg.get("drive") else ""))
            tk.Entry(L, textvariable=self.set_card_var,
                     font=("Microsoft YaHei UI", 9), relief="sunken",
                     bd=1).grid(row=0, column=1, sticky="ew", padx=(10, 6), pady=(10, 2))
            self._btn(L, "浏览…", self._set_pick_card, h=24,
                      fs=9).grid(row=0, column=2, sticky="e", pady=(10, 2))

            # 2) 临时目录
            row(L, 3, "临时目录", "先转到这里（自动建 video / music），转完再搬进记忆棒，慢速卡也不卡住")
            self.set_temp_var = tk.StringVar(value=temp_path(self.cfg))
            tk.Entry(L, textvariable=self.set_temp_var,
                     font=("Microsoft YaHei UI", 9), relief="sunken",
                     bd=1).grid(row=3, column=1, sticky="ew", padx=(10, 6), pady=(10, 2))
            self._btn(L, "浏览…", self._set_pick_temp, h=24,
                      fs=9).grid(row=3, column=2, sticky="e", pady=(10, 2))

            # 3) 自动播放
            row(L, 6, "自动播放", "勾上：选中条目就播放；不勾：双击条目，或选中后点右上角播放按钮")
            self.set_auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_play", True)))
            tk.Checkbutton(L, text="选中即播放（不勾则要双击或点播放按钮）",
                           variable=self.set_auto_var, bg=BG, fg=FG,
                           activebackground=BG, anchor="w",
                           font=LAB).grid(row=6, column=1, columnspan=2, sticky="w",
                                          padx=(10, 0), pady=(10, 2))

            # 4) 默认播放方式
            row(L, 9, "默认播放方式", "程序启动时用哪种；右上角那个循环按钮可以随时临时切换")
            loop_labels = ["列表播放（顺序播完停止）", "单个循环（单条重复）",
                           "列表循环（播完从头再来）"]
            self.set_loop_var = tk.StringVar(
                value={1: loop_labels[0], 0: loop_labels[1],
                       2: loop_labels[2]}.get(int(self.cfg.get("loop_mode") or 0),
                                              loop_labels[1]))
            ttk.Combobox(L, textvariable=self.set_loop_var, values=loop_labels,
                         state="readonly", width=26,
                         font=("Microsoft YaHei UI", 9)).grid(
                row=9, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(10, 2))

            # 5) 顶部说明文件
            row(L, 12, "顶部滚动说明", "内容就是本目录下的「说明.txt」，随时记事本改，改完点「重读说明」")
            nrow = tk.Frame(L, bg=BG)
            nrow.grid(row=12, column=1, columnspan=2, sticky="w",
                      padx=(10, 0), pady=(10, 2))
            tk.Label(nrow, text=os.path.basename(notice_path()), bg=BG, fg="#3c5a72",
                     font=HINT).pack(side="left")
            self._btn(nrow, "重读说明", self._set_reload_notice, h=22,
                      fs=9).pack(side="left", padx=(8, 0))
            self._btn(nrow, "编辑说明", self._set_edit_notice, h=22,
                      fs=9).pack(side="left", padx=(6, 0))

            # ==== 右列
            # 6) 视频格式：左选编码格式，右选码率
            row(R, 0, "视频格式",
                "左边选编码格式、右边选码率；竖屏自动左转 90°，同名 .THM 缩略图自动生成")
            vrow = tk.Frame(R, bg=BG)
            vrow.grid(row=0, column=1, columnspan=2, sticky="w",
                      padx=(10, 0), pady=(10, 2))
            cur_p = resolve_profile(self.cfg)
            vb_labels = [VBASES[k]["label"] for k in VBASE_ORDER]
            self.set_vb = ttk.Combobox(vrow, values=vb_labels, width=25,
                                       state="readonly",
                                       font=("Microsoft YaHei UI", 9))
            try:
                self.set_vb.current(VBASE_ORDER.index(cur_p["base"]))
            except (ValueError, KeyError):
                self.set_vb.current(0)
            self.set_vb.pack(side="left")
            self.set_vr = ttk.Combobox(vrow, width=6, state="readonly",
                                       font=("Microsoft YaHei UI", 9))
            self.set_vr.pack(side="left", padx=(8, 0))
            tk.Label(vrow, text="kbps", bg=BG, fg=FG,
                     font=LAB).pack(side="left", padx=(4, 0))
            # 基座提示单独占一行，塞在同一行会把 kbps 挤变形
            self.set_vnote = tk.Label(R, text="", bg=BG, fg="#63707f",
                                      font=HINT, anchor="w", justify="left")
            self.set_vnote.grid(row=2, column=0, columnspan=3, sticky="w",
                                pady=(2, 4))

            def _fill_rates(keep=None):
                b = VBASES[VBASE_ORDER[self.set_vb.current()]]
                vals = [str(r) for r in b["rates"]]
                self.set_vr["values"] = vals
                if keep is None:
                    keep = cur_p.get("kbps") or 1200
                want = str(keep)
                if want not in vals:     # 换基座后原码率不在列表里，吸到最近的一档
                    want = str(min(b["rates"], key=lambda x: abs(x - keep)))
                self.set_vr.set(want)
                self.set_vnote.config(text=b.get("note") or "")

            def _on_vb(*_):
                try:
                    prev = int(self.set_vr.get())
                except (TypeError, ValueError):
                    prev = cur_p.get("kbps") or 1200
                _fill_rates(prev)

            self.set_vb.bind("<<ComboboxSelected>>", _on_vb)
            _fill_rates()

            # 7) 视频存放位置（只在 VIDEO 下，MP_ROOT 那条路已实测作废）
            row(R, 3, "视频存放位置",
                "视频一律放 <盘>\\VIDEO（掌机实测唯一能播的位置），文件名用原标题、扩展名大写 .MP4。"
                "掌机在 VIDEO 里累计约 1000 个视频就到头了，超出的按顺序不显示（子目录里的也算），"
                "所以一卡别塞太多；平铺时全部放在 VIDEO 根、不建子目录")
            self.set_vl_labels = ["VIDEO 目录（推荐，平铺不建子目录）",
                                  "VIDEO 目录 + 按作者分目录"]
            self.set_vl = ttk.Combobox(R, values=self.set_vl_labels, width=40,
                                       state="readonly", font=("Microsoft YaHei UI", 9))
            self.set_vl.current(1 if group_author_on(self.cfg) else 0)
            self.set_vl.grid(row=3, column=1, columnspan=2, sticky="w",
                             padx=(10, 0), pady=(10, 2))

            # 8) 音频格式
            row(R, 6, "音频格式",
                "MP3 是 PSP 原生支持的；WMA 要先在 PSP 系统设置里开启「WMA 播放」；WAV 无损但 PSP 放不了")
            af_labels = [AUDIO_FMTS[k]["label"] for k in AUDIO_ORDER]
            self.set_af = ttk.Combobox(R, values=af_labels, width=40, state="readonly",
                                       font=("Microsoft YaHei UI", 9))
            cur_af = self.cfg.get("audio_fmt") or "mp3"
            self.set_af.current(AUDIO_ORDER.index(cur_af) if cur_af in AUDIO_ORDER else 0)
            self.set_af.grid(row=6, column=1, columnspan=2, sticky="w",
                             padx=(10, 0), pady=(10, 2))

            # 9) 音频码率 / 采样率
            row(R, 9, "音频码率 / 采样率", "WAV 是无损 PCM，码率对它不起作用，只认采样率")
            mrow = tk.Frame(R, bg=BG)
            mrow.grid(row=9, column=1, columnspan=2, sticky="w",
                      padx=(10, 0), pady=(10, 2))
            self.set_br = ttk.Combobox(mrow, values=[320, 256, 192, 160, 128, 96],
                                       width=6, state="readonly",
                                       font=("Microsoft YaHei UI", 9))
            self.set_br.set(int(self.cfg.get("mp3_bitrate") or 320))
            self.set_br.pack(side="left")
            tk.Label(mrow, text="kbps", bg=BG, fg=FG,
                     font=LAB).pack(side="left", padx=(4, 14))
            self.set_sr = ttk.Combobox(mrow, values=[44100, 48000, 32000, 22050, 16000],
                                       width=7, state="readonly",
                                       font=("Microsoft YaHei UI", 9))
            self.set_sr.set(int(self.cfg.get("mp3_sr") or 44100))
            self.set_sr.pack(side="left")
            tk.Label(mrow, text="Hz", bg=BG, fg=FG,
                     font=LAB).pack(side="left", padx=(4, 0))

            self.set_cp_var = tk.BooleanVar(value=bool(self.cfg.get("copy_mp3", True)))
            tk.Checkbutton(R, text="本身是 MP3 时直接复制进记忆棒（不重编码，仅 MP3 格式有效）",
                           variable=self.set_cp_var, bg=BG, fg=FG, activebackground=BG,
                           anchor="w", font=LAB).grid(row=11, column=0, columnspan=3,
                                                      sticky="w", pady=(2, 0))

            # 10) 播放窗口 / 可视化用的 logo
            row(R, 12, "播放窗口 Logo",
                "停止播放时画面显示它，音乐可视化也拿它当背景；默认图在程序目录的 logo 文件夹")
            lrow = tk.Frame(R, bg=BG)
            lrow.grid(row=12, column=1, columnspan=2, sticky="w",
                      padx=(10, 0), pady=(10, 2))
            self._logo_pick = self._logo_cur()
            names = [os.path.basename(p) for p in logo_files()]
            cur_name = os.path.basename(self._logo_pick) if self._logo_pick else ""
            self.set_logo_var = tk.StringVar(value=cur_name or "（内置图形）")
            self.set_logo_box = ttk.Combobox(lrow, textvariable=self.set_logo_var,
                                             values=names, state="readonly", width=18,
                                             font=("Microsoft YaHei UI", 9))
            self.set_logo_box.grid(row=0, column=0, sticky="w")
            self.set_logo_box.bind("<<ComboboxSelected>>", self._set_logo_sel)
            self._btn(lrow, "浏览图片…", self._set_pick_logo, h=22,
                      fs=9).grid(row=0, column=1, padx=(8, 0))
            self._btn(lrow, "打开文件夹", self._set_open_logo_dir, h=22,
                      fs=9).grid(row=0, column=2, padx=(6, 0))

            btns = tk.Frame(fr, bg=BG)
            btns.pack(fill="x", padx=18, pady=(4, 14))
            self._btn(btns, "保存", self.save_settings, h=32, fs=11,
                      w=btn_width("保存", 11, pad=44, mini=112)).pack(side="left")
            self._btn(btns, "关闭", self.hide_settings, h=32, fs=11,
                      w=btn_width("关闭", 11, pad=44, mini=112)).pack(
                side="left", padx=(10, 0))
            tk.Label(btns, text="改完目录/格式后，已经入队的老文件仍按当时的格式转换",
                     bg=BG, fg="#63707f", font=HINT).pack(side="right")

        # ---- 设置页交互
        def _set_pick_card(self):
            p = filedialog.askdirectory(title="选择记忆棒根目录（PSP 盘根或任意目录）")
            if p:
                self.set_card_var.set(p)

        def _set_pick_temp(self):
            p = filedialog.askdirectory(title="选择临时目录")
            if p:
                self.set_temp_var.set(p)

        def _set_reload_notice(self):
            self.marquee_text = self._notice_text()
            self._marq_relayout()

        # ---- logo 选择
        def _set_logo_sel(self, _=None):
            name = self.set_logo_var.get()
            if name and name != "（内置图形）":
                self._logo_pick = os.path.join(logo_dir(), name)

        def _set_pick_logo(self):
            p = filedialog.askopenfilename(
                title="选择 logo 图片",
                filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"),
                           ("所有文件", "*.*")])
            if p:
                self._logo_pick = p
                self.set_logo_var.set(os.path.basename(p))

        def _set_open_logo_dir(self):
            d = logo_dir()
            try:
                if not os.path.isdir(d):
                    os.makedirs(d)
                if os.name == "nt":
                    os.startfile(d)
                else:
                    subprocess.Popen(["xdg-open", d])
            except Exception:
                pass

        def _set_edit_notice(self):
            p = notice_path()
            try:
                if not os.path.isfile(p):        # 没有就先落一份默认内容
                    self._notice_text()
                if os.name == "nt":
                    os.startfile(p)
                else:
                    subprocess.Popen(["xdg-open", p])
            except Exception:
                pass

        def save_settings(self):
            """保存到配置文件：目录/格式/自动播放/默认播放方式"""
            p = (self.set_card_var.get() or "").strip()
            if len(p) == 2 and p[1] == ":":
                p = p + "\\"
            lm = {"列表播放（顺序播完停止）": 1, "单个循环（单条重复）": 0,
                  "列表循环（播完从头再来）": 2}.get(self.set_loop_var.get(), 0)
            lp = getattr(self, "_logo_pick", "") or ""
            if lp:                              # logo 文件夹里的存相对路径，外部图存绝对路径
                try:
                    base = os.path.dirname(os.path.abspath(__file__))
                    rel = os.path.relpath(lp, base).replace("\\", "/")
                    lp = (os.path.abspath(lp).replace("\\", "/")
                          if rel.startswith("..") else rel)
                except Exception:
                    lp = lp.replace("\\", "/")
            self.cfg.update({
                "card_path": p,
                "drive": p[0].upper() if len(p) >= 2 and p[1] == ":" else "",
                "temp_dir": (self.set_temp_var.get() or "").strip(),
                "vbase": VBASE_ORDER[max(0, self.set_vb.current())],
                "vrate": int(self.set_vr.get() or 1200),
                "vid_layout": "author" if self.set_vl.current() == 1 else "video",
                "audio_fmt": AUDIO_ORDER[max(0, self.set_af.current())],
                "mp3_bitrate": int(self.set_br.get()),
                "mp3_sr": int(self.set_sr.get()),
                "copy_mp3": bool(self.set_cp_var.get()),
                "group_author": self.set_vl.current() == 1,
                "auto_play": bool(self.set_auto_var.get()),
                "loop_mode": lm,
                "logo_path": lp,
            })
            save_config(self.cfg)
            _LOGO_FILE_CACHE.clear()            # 换图了：缓存清掉，画面立刻换
            _LOGO_BG_CACHE.clear()
            self._wave_buf = None
            self._wave_buf_fs = None
            try:
                if getattr(self, "wave_mode", False) and self.db_state == "playing":
                    self._db_wave_draw()
                else:
                    self._db_repaint_logos()     # 三页画面一起换，切过去不会还是旧图
            except Exception:
                pass
            self.loop_mode = lm
            try:
                self.db_loop_btn.set_sym(self.LOOP_SYMS[lm])
            except Exception:
                pass
            self.marquee_text = self._notice_text()
            self._marq_relayout()
            self._refresh_status()
            try:
                self.refresh_card()
            except Exception:
                pass
            self.hide_settings()

        def _game_default_dir(self):
            saved = (self.cfg.get("iso_root") or "").strip()
            if saved and os.path.isdir(saved):
                return saved
            r = card_root(self.cfg)
            if not r:
                return ""
            cand = os.path.join(r, "ISO")
            return cand if os.path.isdir(cand) else r

        def game_browse(self):
            cur = self.game_path_var.get().strip() or self._game_default_dir()
            d = filedialog.askdirectory(initialdir=cur or os.environ.get("USERPROFILE", "C:\\"))
            if d:
                self.game_path_var.set(os.path.normpath(d))
                self.game_scan()

        def game_scan(self):
            root = self.game_path_var.get().strip()
            if not root or not os.path.isdir(root):
                messagebox.showwarning(APP_NAME, "请选择一个存在的镜像目录（放 .iso / .cso 的文件夹）")
                return
            try:
                names = sorted(os.listdir(root))
            except Exception as e:
                messagebox.showerror(APP_NAME, "读不了这个目录：%s" % e)
                return
            files = [os.path.join(root, f) for f in names
                     if os.path.splitext(f)[1].lower() in (".iso", ".cso")]
            for iid in self.game_tree.get_children():
                self.game_tree.delete(iid)
            self.game_map.clear()
            self._game_cache.clear()
            self.game_cur = None
            self._clear_info(self.game_txt, self.game_img)
            if not files:
                self.game_status.configure(text="这个目录里没有 .iso / .cso")
                return
            self.cfg["iso_root"] = root
            try:
                save_config(self.cfg)
            except Exception:
                pass
            self.game_status.configure(text="正在扫描 %d 个镜像…" % len(files))
            self._game_sid = getattr(self, "_game_sid", 0) + 1
            sid = self._game_sid

            def worker():
                for i, f in enumerate(files, 1):
                    try:
                        m = game_meta(f)
                    except Exception:
                        m = None
                    self.msgs.put(("game_row", sid, m, i, len(files)))
                self.msgs.put(("game_done", sid, len(files)))

            threading.Thread(target=worker, daemon=True).start()

        def _game_row(self, sid, m, n, total):
            if not m or sid != getattr(self, "_game_sid", 0):
                return
            iid = "g%d_%d" % (sid, n)
            self.game_map[iid] = m
            title = m["title"] or m["file"]
            if not m.get("ok"):
                title = "%s（读取失败）" % title
            self.game_tree.insert("", "end", iid=iid, values=(
                title, m["file"], "%.1f MB" % (m["size"] / 1048576.0), m["kind"]))
            if n % 8 == 0 or n == total:
                self.game_status.configure(text="已读取 %d / %d" % (n, total))

        def _game_full(self, path):
            hit = self._game_cache.get(path)
            if hit:
                return hit
            m = game_meta(path, want_images=True)
            if len(self._game_cache) > 20:
                self._game_cache.clear()
            self._game_cache[path] = m
            return m

        def _game_pic_file(self, m, key):
            data = m.get(key)
            if not data:
                return None
            base = re.sub(r"[^0-9A-Za-z]+", "_", m["file"])[:40]
            p = os.path.join(self._thumb_cache(), "gm_%s_%s.png" % (key, base))
            try:
                if not os.path.exists(p) or os.path.getsize(p) != len(data):
                    with open(p, "wb") as fh:
                        fh.write(data)
                return p
            except Exception:
                return None

        def _on_game_select(self, _=None):
            sel = self.game_tree.selection()
            if not sel:
                return
            m0 = self.game_map.get(sel[0])
            if not m0:
                return
            self.game_cur = m0
            full = None
            if m0.get("ok"):
                self.game_status.configure(text="正在读取 %s …" % m0["file"])
                self.update_idletasks()
                try:
                    full = self._game_full(m0["path"])
                    self.game_cur = full
                except Exception as e:
                    self.game_cur = dict(m0)
                    self.game_cur["err"] = str(e)
            if self.game_cur.get("ok"):
                self.game_status.configure(
                    text="%s · %s" % (self.game_cur["title"], self.game_cur["kind"]))
            self._game_show(self.game_cur)
            if full and full.get("title") and m0["title"] != full["title"]:
                try:
                    self.game_tree.set(sel[0], "title", full["title"])
                except Exception:
                    pass

        def _game_show(self, m):
            self._game_stop_video()
            if not m.get("ok"):
                self._show_lines(self.game_txt, self.game_img,
                                 [("读取失败", m.get("err") or "未知错误"),
                                  ("文件", m.get("file", ""))], blank_img=True)
                self.game_play_hint.configure(text="镜像读不了，没法播放")
                return
            parental = m.get("parental")
            lines = [
                ("游戏名", m["title"]),
                ("文件", m["file"]),
                ("大小", "%.1f MB" % (m["size"] / 1048576.0)),
                ("镜像类型", m["kind"]),
                ("光盘编号", m["disc_id"] or "-"),
                ("游戏版本", m["version"] or "-"),
                ("应用版本", m["app_ver"] or "-"),
                ("年龄分级", str(parental) if parental not in ("", None) else "-"),
                ("图标 ICON0", "144×80 PNG" if m["icon"] else "无"),
                ("背景 PIC1", "480×272 PNG" if m["pic1"] else "无"),
                ("主题曲 SND0", "%.0f KB" % (m["snd0"] / 1024.0) if m["snd0"] else "无"),
                ("动态图标", "ICON1.PMF %.0f KB" % (m["icon1"] / 1024.0)
                 if m.get("icon1") else "无"),
                ("镜像内视频", ("%d 个 PMF：%s" % (len(m["videos"]), m["videos"][0][0])
                                if m["videos"] else "无")),
                ("PSP_GAME 条目", str(m["nfiles"] or "-")),
            ]
            self.game_meta_cur = m
            self._show_lines(self.game_txt, self.game_img, lines,
                             pil=self._game_base_image(m))
            self._game_prepare_video(m)
            self._game_sync_btn()

        def _game_base_image(self, m):
            """背景图 PIC1/PIC0 打底，ICON0 图标叠在左上，尺寸固定不跳动"""
            W, H = PmfPlayer.FRAME_W, PmfPlayer.FRAME_H
            base = Image.new("RGB", (W, H), (16, 16, 20))
            data = m.get("pic1") or m.get("pic0")
            if data:
                try:
                    im = Image.open(io.BytesIO(data)).convert("RGB")
                    if im.size != (W, H):
                        im = im.resize((W, H), Image.LANCZOS)
                    base.paste(im, (0, 0))
                except Exception:
                    pass
            icon = m.get("icon")
            if icon:
                try:
                    ic = Image.open(io.BytesIO(icon)).convert("RGBA")
                    ih = 64
                    iw = max(1, int(round(ic.width * ih / float(ic.height))))
                    ic = ic.resize((iw, ih), Image.LANCZOS)
                    base.paste(ic, (14, 14), ic)
                except Exception:
                    pass
            return base

        def _set_game_img(self, im):
            try:
                ph = ImageTk.PhotoImage(im)
                self.game_img.configure(image=ph, text="")
                self.game_img.image = ph
            except Exception:
                pass

        def _game_video_source(self, m):
            """优先 ICON1.PMF（菜单里那圈动态图标，短且循环自然），
            没有再用镜像里的游戏视频（USRDIR/MOVIE、UMD_VIDEO）"""
            if m.get("icon1_lba"):
                lb, sz = m["icon1_lba"]
                return "ICON1.PMF", lb, sz
            if m.get("videos"):
                nm, sz, lb = m["videos"][0]
                return nm, lb, sz
            return None

        def _game_prepare_video(self, m):
            self.game_video_src = None
            self.game_audio_src = None
            src = self._game_video_source(m)
            if not src:
                self.game_play_hint.configure(text="镜像无视频")
                self._game_sync_btn()
                return
            path = os.path.join(self._thumb_cache(),
                                "pv_%s.pmf" % re.sub(r"[^0-9A-Za-z]+", "_", m["file"])[:40])
            try:
                img = PspImage(m["path"])
                data = img.read(src[1], src[2])
                img.close()
                if not data:
                    raise RuntimeError("没读到数据")
                with open(path, "wb") as fh:
                    fh.write(data)
            except Exception as e:
                self.game_play_hint.configure(text="读视频失败")
                self._game_sync_btn()
                return
            self.game_video_src = path
            self.game_audio_src = self._game_audio_make(m, path)
            self.game_state = "pending"
            self._game_sync_btn()
            self.game_play_hint.configure(text="1 秒后自动播放")
            self.game_play_after = self.after(1000, self._game_start_video)

        def _game_audio_make(self, m, pmf_path):
            """准备跟着画面一起循环的背景音乐。
            PSP 主界面选中游戏时就是「ICON1 动画 + SND0.AT3 主题曲」一起放，
            所以优先取 SND0；没有 SND0 才退而取 PMF 自带音轨；都没有就静音。
            统一转成 WAV（winsound 只吃 WAV），固定文件名单个覆盖，不占缓存。"""
            if not ffmpeg:
                return None
            wav = os.path.join(self._thumb_cache(), "au_cur.wav")
            for old in (wav, os.path.join(self._thumb_cache(), "au_cur.at3")):
                try:
                    if os.path.exists(old):
                        os.remove(old)
                except Exception:
                    pass
            src = None
            if m.get("snd0"):
                at3 = os.path.join(self._thumb_cache(), "au_cur.at3")
                try:
                    img = PspImage(m["path"])
                    data = img.read_file("PSP_GAME/SND0.AT3")
                    img.close()
                    if data:
                        with open(at3, "wb") as fh:
                            fh.write(data)
                        src = at3
                except Exception:
                    src = None
            if not src and pmf_path:
                src = pmf_path
            if not src:
                return None
            try:
                run([ffmpeg, "-y", "-v", "error", "-i", src, "-vn",
                     "-ac", "2", "-ar", "44100", wav])
            except Exception:
                pass
            if os.path.isfile(wav) and os.path.getsize(wav) > 4096:
                return wav
            return None

        def _game_audio_play(self):
            """游戏音乐也交给 ffplay：这样能用音频会话实时改音量/静音，
            既不用重启进程，也不会让音乐从头重放"""
            self._game_audio_stop()
            wav = getattr(self, "game_audio_src", None)
            self.game_audio_on = False
            if not wav:
                return
            ffplay = find_tool("ffplay")
            if not ffplay:
                return
            use_sess = getattr(self, "_db_use_session", True)
            cmd = [ffplay, "-nodisp", "-autoexit", "-loop", "0",
                   "-loglevel", "quiet",
                   "-volume", "100" if use_sess else str(int(self.db_vol)), wav]
            env = dict(os.environ)
            env["SDL_VIDEODRIVER"] = "dummy"
            env["SDL_AUDIODRIVER"] = "directsound"
            try:
                self.game_audio_proc = reg_proc(subprocess.Popen(
                    cmd, env=env, creationflags=no_window(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                self.game_audio_on = True
            except Exception:
                self.game_audio_proc = None
                return
            if use_sess:
                try:
                    self._game_audio_apply_id = self.after(220, self._game_audio_apply)
                except Exception:
                    self._game_audio_apply_id = None

        def _game_audio_apply(self):
            self._game_audio_apply_id = None
            p = getattr(self, "game_audio_proc", None)
            if p is None or p.poll() is not None:
                return
            if WINVOL.bind(p.pid):
                WINVOL.set_volume(self.db_vol / 100.0)
                WINVOL.set_mute(self.db_muted or self.db_vol <= 0)
                return
            if self.game_audio_on and not getattr(self, "_game_sess_failed", False):
                self._game_sess_failed = True
                self._game_audio_play()      # 拿不到会话：退回命令行音量

        def _game_audio_stop(self):
            self.game_audio_on = False
            try:
                a = getattr(self, "_game_audio_apply_id", None)
                if a:
                    self.after_cancel(a)
                    self._game_audio_apply_id = None
            except Exception:
                pass
            p = getattr(self, "game_audio_proc", None)
            self.game_audio_proc = None
            try:
                if p is not None and WINVOL.pid == p.pid:
                    WINVOL.forget()
            except Exception:
                pass
            if p is None:
                return
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=0.5)
                    except Exception:
                        p.kill()
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

        def _game_start_video(self):
            self.game_play_after = None
            if self.game_state != "pending" or not self.game_video_src:
                return
            if not ffmpeg:
                self.game_state = "idle"
                self.game_play_hint.configure(text="无 ffmpeg 无法播放")
                return
            self.game_player = PmfPlayer(ffmpeg, self.game_video_src, ffprobe)
            self.game_player.start()
            self._game_audio_play()
            self.game_state = "playing"
            self._game_sync_btn()
            self._game_hint_audio()
            self._game_pump()

        def _game_pump(self):
            self.game_pump_id = None
            pl = self.game_player
            if self.game_state != "playing" or not pl:
                return
            data = None
            try:
                data = pl.frames.get_nowait()
            except pyqueue.Empty:
                pass
            if data:
                try:
                    self._set_game_img(Image.frombytes(
                        "RGB", (PmfPlayer.FRAME_W, PmfPlayer.FRAME_H), data))
                except Exception:
                    pass
            # 按半帧间隔去取，帧一到就上屏，也不至于堆在队列里
            iv = int(1000.0 / max(8.0, pl.fps))
            self.game_pump_id = self.after(max(15, iv // 2), self._game_pump)

        def _game_stop_video(self):
            self._game_audio_stop()
            if getattr(self, "game_play_after", None):
                try:
                    self.after_cancel(self.game_play_after)
                except Exception:
                    pass
            self.game_play_after = None
            if getattr(self, "game_pump_id", None):
                try:
                    self.after_cancel(self.game_pump_id)
                except Exception:
                    pass
            self.game_pump_id = None
            pl = getattr(self, "game_player", None)
            if pl:
                pl.stop()
            self.game_player = None
            self.game_state = "idle"
            self._game_sync_btn()

        def _game_sync_btn(self):
            """播放键跟着状态换符号：播放中 = 两条竖线，其它 = 三角；
            镜像里没有视频时两个键都置灰，点了不响应"""
            st = getattr(self, "game_state", "idle")
            has = bool(getattr(self, "game_video_src", None))
            try:
                self.db_play_btn.set_sym("pause" if st == "playing" else "play")
            except Exception:
                pass
            if getattr(self, "page", "") == "game":
                self._pb_apply()

        def game_stop_play(self):
            """停止：停掉播放器并把画面还原成图标 + 背景图"""
            self._game_stop_video()
            m = getattr(self, "game_meta_cur", None)
            if m and m.get("ok"):
                self._set_game_img(self._game_base_image(m))
            self._game_sync_btn()
            self.game_play_hint.configure(text="已停止" if self.game_video_src
                                          else "镜像无视频")

        def game_toggle_play(self):
            st = getattr(self, "game_state", "idle")
            if st == "pending":
                if getattr(self, "game_play_after", None):
                    try:
                        self.after_cancel(self.game_play_after)
                    except Exception:
                        pass
                    self.game_play_after = None
                self._game_start_video()
                return
            if st == "playing":
                self._game_stop_video()
                self.game_state = "paused"
                self._game_sync_btn()
                self.game_play_hint.configure(text="已暂停")
                return
            if st in ("paused", "idle") and self.game_video_src and ffmpeg:
                if getattr(self, "game_player", None):
                    self.game_player.stop()
                self.game_player = PmfPlayer(ffmpeg, self.game_video_src, ffprobe)
                self.game_player.start()
                self._game_audio_play()
                self.game_state = "playing"
                self._game_sync_btn()
                self._game_hint_audio()
                self._game_pump()
                return
            if not self.game_video_src:
                self.game_play_hint.configure(text="镜像无视频")

        def _game_export_png(self, key):
            m = self.game_cur
            if not m or not m.get("ok"):
                messagebox.showinfo(APP_NAME, "先在列表里选中一个游戏镜像")
                return
            mm = m if m.get(key) else self._game_full(m["path"])
            data = mm.get(key)
            if not data:
                messagebox.showinfo(APP_NAME, "这个镜像里没有 %s"
                                    % ("ICON0.PNG" if key == "icon" else "PIC1.PNG"))
                return
            stem = os.path.splitext(m["file"])[0]
            p = filedialog.asksaveasfilename(
                defaultextension=".png",
                initialfile="%s_%s.png" % (stem, "ICON0" if key == "icon" else "PIC1"),
                filetypes=[("PNG 图片", "*.png")])
            if not p:
                return
            with open(p, "wb") as fh:
                fh.write(data)
            self.game_status.configure(text="已导出 %s" % os.path.basename(p))

        def game_export_music(self):
            m = self.game_cur
            if not m or not m.get("ok"):
                messagebox.showinfo(APP_NAME, "先在列表里选中一个游戏镜像")
                return
            if not m.get("snd0"):
                messagebox.showinfo(APP_NAME, "这个镜像里没有 SND0.AT3 主题曲")
                return
            stem = os.path.splitext(m["file"])[0]
            p = filedialog.asksaveasfilename(
                defaultextension=".at3", initialfile="%s_SND0.at3" % stem,
                filetypes=[("AT3 音频", "*.at3")])
            if not p:
                return
            try:
                img = PspImage(m["path"])
                data = img.read_file("PSP_GAME/SND0.AT3")
                img.close()
                with open(p, "wb") as fh:
                    fh.write(data)
                self.game_status.configure(text="已导出 %s" % os.path.basename(p))
            except Exception as e:
                messagebox.showerror(APP_NAME, "导出失败：%s" % e)

        def game_export_video(self):
            m = self.game_cur
            if not m or not m.get("ok"):
                messagebox.showinfo(APP_NAME, "先在列表里选中一个游戏镜像")
                return
            vids = list(m.get("videos") or [])
            if not vids and m.get("icon1_lba"):      # 没有 UMD_VIDEO 时退回动态图标
                vids = [["ICON1.PMF", m["icon1_lba"][1], m["icon1_lba"][0]]]
            if not vids:
                messagebox.showinfo(APP_NAME, "这个镜像里没有 UMD_VIDEO 视频，也没有 ICON1.PMF")
                return
            if len(vids) > 1:
                messagebox.showinfo(
                    APP_NAME,
                    "里面有 %d 个 PMF 视频，请点【内部文件】选中具体那一个再导出"
                    % len(vids))
                return
            name, size, lba = vids[0]
            stem = os.path.splitext(name)[0]
            p = filedialog.asksaveasfilename(
                defaultextension=".mp4", initialfile="%s.mp4" % stem,
                filetypes=[("MP4 视频", "*.mp4")])
            if not p:
                return
            tmp = os.path.join(self._thumb_cache(),
                               "umd_%s.pmf" % re.sub(r"[^0-9A-Za-z]+", "_", stem)[:40])
            try:
                img = PspImage(m["path"])
                data = img.read(lba, size)
                img.close()
                with open(tmp, "wb") as fh:
                    fh.write(data)
                self.game_status.configure(text="正在转 mp4…")
                self.update_idletasks()
                run([ffmpeg, "-y", "-v", "error", "-i", tmp,
                     "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", p])
                if os.path.exists(p):
                    self.game_status.configure(text="已导出 %s" % os.path.basename(p))
                else:
                    messagebox.showwarning(APP_NAME, "转码没有输出文件，可能 ffmpeg 读不了这个 PMF")
            except Exception as e:
                messagebox.showerror(APP_NAME, "导出视频失败：%s" % e)

        def game_files(self):
            m = self.game_cur
            if not m or not m.get("ok"):
                messagebox.showinfo(APP_NAME, "先在列表里选中一个游戏镜像")
                return
            try:
                img = PspImage(m["path"])
            except Exception as e:
                messagebox.showerror(APP_NAME, "打不开镜像：%s" % e)
                return
            win = tk.Toplevel(self)
            win.title("镜像内部文件 · %s" % m["file"])
            win.geometry("760x540")
            win.configure(bg="#eef2f7")
            win.transient(self)
            win.lift()
            win.focus_force()
            bar = tk.Frame(win, bg="#eef2f7")
            bar.pack(fill="x", padx=8, pady=6)
            tk.Label(bar, text="只读浏览：双击文件夹展开，右键可导出文件",
                     bg="#eef2f7", font=("Microsoft YaHei UI", 9)).pack(side="left")
            fr, tv = self._make_tree(win, (("size", "大小", 120),), tree=True)
            fr.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            node = {}
            seq = [0]

            def add(parent, path, lba, size, is_dir):
                seq[0] += 1
                iid = "n%d" % seq[0]
                node[iid] = (path, lba, size, is_dir)
                tv.insert(parent, "end", iid=iid, text=path.split("/")[-1] or path,
                          values=("" if is_dir else "%.0f KB" % (size / 1024.0),))
                if is_dir:
                    tv.insert(iid, "end", text="…")
                return iid

            def fill(iid):
                path, lba, size, is_dir = node[iid]
                for c in tv.get_children(iid):
                    tv.delete(c)
                ents = img.entries(lba, size)
                for nm, lb, sz, isd in ents:
                    add(iid, path + "/" + nm, lb, sz, isd)

            def on_open(_=None):
                for iid in list(node):
                    if tv.exists(iid) and node[iid][3] and tv.get_children(iid):
                        kids = tv.get_children(iid)
                        if len(kids) == 1 and tv.item(kids[0], "text") == "…":
                            try:
                                fill(iid)
                            except Exception:
                                pass

            tv.bind("<<TreeviewOpen>>", on_open)
            root_iid = add("", m["file"], img.root()[0], img.root()[1], True)

            def close_all():
                img.close()
                win.destroy()

            def export_sel(as_mp4=False):
                sel = tv.selection()
                if not sel:
                    messagebox.showinfo(APP_NAME, "先选中一个文件")
                    return
                path, lba, size, is_dir = node[sel[0]]
                if is_dir:
                    messagebox.showinfo(APP_NAME, "文件夹不能导出，请选文件")
                    return
                base = path.split("/")[-1]
                if as_mp4:
                    p = filedialog.asksaveasfilename(
                        defaultextension=".mp4",
                        initialfile="%s.mp4" % os.path.splitext(base)[0],
                        filetypes=[("MP4 视频", "*.mp4")])
                else:
                    p = filedialog.asksaveasfilename(initialfile=base)
                if not p:
                    return
                try:
                    data = img.read(lba, size)
                    tmp = p
                    if as_mp4:
                        tmp = os.path.join(self._thumb_cache(), "umd_exp.pmf")
                    with open(tmp, "wb") as fh:
                        fh.write(data)
                    if as_mp4:
                        run([ffmpeg, "-y", "-v", "error", "-i", tmp,
                             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", p])
                    self.game_status.configure(text="已导出 %s" % os.path.basename(p))
                except Exception as e:
                    messagebox.showerror(APP_NAME, "导出失败：%s" % e)

            menu = tk.Menu(win, tearoff=0)
            menu.add_command(label="导出选中文件", command=lambda: export_sel(False))
            menu.add_command(label="导出并转成 mp4", command=lambda: export_sel(True))

            def popup(e):
                iid = tv.identify_row(e.y)
                if iid:
                    tv.selection_set(iid)
                menu.tk_popup(e.x_root, e.y_root)

            tv.bind("<Button-3>", popup)
            self._btn(bar, "关闭", close_all).pack(side="right")
            self._btn(bar, "导出选中", lambda: export_sel(False)).pack(side="right", padx=(0, 6))
            win.protocol("WM_DELETE_WINDOW", close_all)
            try:
                fill(root_iid)          # 根目录先铺一层，省得用户还要点一下
                tv.item(root_iid, open=True)
                for kid in tv.get_children(root_iid):
                    nm = node.get(kid, ("", 0, 0, True))[0].split("/")[-1].upper()
                    if nm == "PSP_GAME":
                        fill(kid)       # 顺手展开 PSP_GAME：PARAM.SFO / ICON0 就在这
                        tv.item(kid, open=True)
                        break
            except Exception:
                pass

        def show_page(self, name):
            if getattr(self, "_settings_open", False):      # 从设置页点页签回来
                self._settings_open = False
                try:
                    self.settings_page.pack_forget()
                except Exception:
                    pass
                try:
                    self.btn_set.set_active(False)
                except Exception:
                    pass
                self._pack_band(name)
            if name != "game" and getattr(self, "game_state", "idle") != "idle":
                self._game_stop_video()      # 切走就把播放器停掉，别在后台解码
            if getattr(self, "page", "") != name:
                if getattr(self, "db_state", "idle") != "idle":
                    self.db_stop()
                self.db_exit_fs()
                # 切页就停播、控件回灰；画面留在各自那页，切回来还看得见
                self.db_cur_video = None
                self.media_list = []
                self.media_idx = 0
                self._db_enable_ctl(False)
                self._db_set_time(0)          # 时间栏回 00:00，不留上一页的残留
            for k, f in self.pages.items():
                if k == name:
                    f.pack(fill="both", expand=True)
                else:
                    f.pack_forget()
            self.page = name
            for b, key in ((self.btn_card, "card"), (self.btn_queue, "queue"),
                           (self.btn_db, "db"), (self.btn_game, "game")):
                b.set_active(key == name)
            self._show_page_bar(name)          # 地址栏跟着页换，右边的播放条不动
            self._pv()                         # 把 db_hint 指向当前页的提示行
            self._pb_apply()
            # 这一页的画面按当前状态重画一遍：切页别留着别的页那只旧 logo / 旧帧
            try:
                self.db_frame = None
                if getattr(self, "page", "") != "game":
                    self._db_logo_paint(self._pv(), with_prog=True)
            except Exception:
                pass
            self.after(20, lambda: self._pane_apply(name))   # 三页详情宽度联动
            if name == "card":
                self.refresh_card()
            elif name == "game" and not self.game_tree.get_children():
                path = self.game_path_var.get().strip()
                if path and os.path.isdir(path):
                    self.game_scan()      # 第一次进来就自动扫一遍，省一次点击
            elif name == "db" and not self.db_authors:
                path = (self.db_path_var.get() or "").strip()
                if path and os.path.isdir(path):
                    self.db_load()        # 第一次进来直接载入上次的库
            if name == "db":
                self._db_resync_ctl()     # 切回这页：按选中的作者/视频把控件放出来

        # ---------------- 底部状态
        def _build_status(self):
            self.status_text = self.canvas.create_text(
                10, 0, anchor="w", text="", font=("Microsoft YaHei UI", 12),
                fill="#0c0c0c")
            self.status_font = tkfont.Font(family="Microsoft YaHei UI", size=12)
            self.status_maxw = 300
            self.pbar_bg = self.canvas.create_rectangle(0, 0, 0, 0, fill="#ded9fb",
                                                       outline="#6a5ac0")
            self.pbar = self.canvas.create_rectangle(0, 0, 0, 0, fill="#ffb64d", outline="")
            self.btn_start = self._btn(self.canvas, "开始转换", self.start_convert,
                                       h=STATUS_H - 6, fs=11, bg="#7866d2")
            self.btn_stop = self._btn(self.canvas, "停止", self.stop_convert, h=STATUS_H - 6,
                                      fs=11, bg="#7866d2")
            self.btn_clear = self._btn(self.canvas, "清空", self.clear_all, h=STATUS_H - 6,
                                       fs=11, bg="#7866d2")
            self.btn_start._id = self.canvas.create_window(0, 0, anchor="nw", window=self.btn_start)
            self.btn_stop._id = self.canvas.create_window(0, 0, anchor="nw", window=self.btn_stop)
            self.btn_clear._id = self.canvas.create_window(0, 0, anchor="nw", window=self.btn_clear)
            self.pbar_x0, self.pbar_x1 = 340, 600
            self.btn_left = 600
            self._refresh_status()

        def _refresh_status(self):
            card = card_root(self.cfg)
            if card:
                try:
                    free = human(shutil.disk_usage(card).free)
                except Exception:
                    free = "?"
                shown = card.rstrip("\\") or card
                if len(shown) > 24:
                    shown = "…" + shown[-20:]
                dtext = "记忆棒 %s 剩 %s" % (shown, free)
            else:
                dtext = "未设记忆棒（点右上【设置】）"
            pending = sum(1 for it in self.items if it["state"] == "queued")
            text = ("队列 %d 待 %d 完成 %d 失败 %d  ｜  %s"
                    % (len(self.items), pending, self.done, self.fail, dtext))
            font = getattr(self, "status_font", None)
            maxw = getattr(self, "status_maxw", 0)
            if font and maxw > 40 and font.measure(text) > maxw:
                while text and font.measure(text + "…") > maxw:
                    text = text[:-1]
                text = text + "…"
            self.canvas.itemconfig(self.status_text, text=text)
            bb = self.canvas.bbox(self.status_text)
            self.pbar_x0 = max((bb[2] if bb else 300) + 14, 330)
            self._update_progress_visual()

        def _update_progress_visual(self):
            total = len(self.items) or 1
            ratio = min(1.0, (self.done + self.fail) / total)
            h = self.h
            x0, x1 = self.pbar_x0, max(self.pbar_x1, self.pbar_x0 + 40)
            y0, y1 = h - STATUS_H + 11, h - STATUS_H + 27
            fillx = x0 + (x1 - x0) * ratio
            self.canvas.coords(self.pbar_bg, x0, y0, x1, y1)
            self.canvas.coords(self.pbar, x0, y0, max(x0, fillx), y1)

        # ---------------- 信息面板
        def _clear_info(self, txt, img):
            img.configure(image="")
            img.image = None
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.configure(state="disabled")

        def _show_lines(self, txt, img, lines, pic=None, blank_img=False, pil=None):
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            for k, v in lines:
                txt.insert("end", "%s：%s\n" % (k, v))
            txt.configure(state="disabled")
            if pil is not None:                 # 直接给 PIL 图（游戏页的合成图 / 视频帧）
                try:
                    photo = ImageTk.PhotoImage(pil)
                    img.configure(image=photo, text="")
                    img.image = photo
                    return
                except Exception:
                    pass
            if pic and os.path.isfile(pic):
                try:
                    im = Image.open(pic)
                    im.thumbnail((280, 260), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(im)
                    img.configure(image=photo, text="")
                    img.image = photo
                    return
                except Exception:
                    pass
            if blank_img:
                img.configure(image="", text="")
                img.image = None
                return
            img.configure(image="", text="（无预览）", fg="#8a93a5")

        def _thumb_cache(self):
            if not hasattr(self, "_thumb_dir"):
                d = os.path.join(os.environ.get("TEMP", "."), "psp_thumb_cache")
                os.makedirs(d, exist_ok=True)
                self._thumb_dir = d
            return self._thumb_dir

        def _on_queue_select(self, _=None):
            if getattr(self, "_pb_quiet", False):
                return                    # 上一首/下一首自己选中行时不重复开播
            sel = self.tree.selection()
            if not sel:
                return
            i = int(sel[0])
            # 队列整体就是左侧列表，循环/随机都在这条列表上走
            lst = []
            for n, it in enumerate(self.items):
                if not os.path.isfile(it["src"]):
                    continue
                lst.append({"path": it["src"], "kind": it["kind"],
                            "title": it.get("title") or "",
                            "author": it.get("author") or "",
                            "queue_idx": n})
            if not lst:
                return
            pos = 0
            for k, it in enumerate(lst):
                if it["queue_idx"] == i:
                    pos = k
                    break
            self.media_list = lst
            if not self._sel_is_current(lst[pos]["path"]):
                self._db_open(lst[pos], pos, autoplay=self._auto_play())

        def _auto_play(self):
            """设置里的「自动播放」：勾上 = 选中就播；不勾 = 双击或点播放按钮才播"""
            return bool(self.cfg.get("auto_play", True))

        def _sel_is_current(self, path):
            """选中的这一条就是正在播 / 刚装好画面的同一条时，别再重开一遍"""
            cur = getattr(self, "db_cur_video", None) or {}
            return bool(path) and cur.get("path") == path and \
                getattr(self, "db_state", "idle") in ("playing", "paused", "idle")

        def _db_ensure_play(self):
            """双击列表条目：不管自动播放开关，都让它真的播起来"""
            if getattr(self, "db_state", "idle") != "playing":
                try:
                    self.db_play_toggle()
                except Exception:
                    pass

        def _card_siblings(self, path, kind):
            """同一目录里同类文件当播放列表，选中即播、循环/随机都基于它"""
            d = os.path.dirname(path)
            exts = VIDEO_EXTS if kind == "video" else AUDIO_EXTS
            out = []
            try:
                names = sorted(os.listdir(d))
            except Exception:
                return []
            for fn in names:
                if os.path.splitext(fn)[1].lower() not in exts:
                    continue
                p = os.path.join(d, fn)
                if os.path.isfile(p):
                    out.append({"path": p, "kind": kind,
                                "title": os.path.splitext(fn)[0]})
            return out

        def _on_card_select(self, _=None):
            if getattr(self, "_pb_quiet", False):
                return
            sel = self.card_tree.selection()
            if not sel:
                return
            item = self.card_map.get(sel[0])
            if not item:
                return
            path = item["path"]
            label = self._kind_label(path, item["is_dir"])
            if item["is_dir"]:
                self.db_stop()
                self.db_cur_video = None
                self.db_time.configure(text="00:00 / 00:00")
                self._db_show_folder_panel(path)
                return
            if label in ("视频", "音乐"):
                kind = "video" if label == "视频" else "audio"
                lst = self._card_siblings(path, kind)
                if not lst:
                    lst = [{"path": path, "kind": kind,
                            "title": os.path.splitext(os.path.basename(path))[0]}]
                self.media_list = lst
                pos = 0
                for k, it in enumerate(lst):
                    if it["path"].lower() == path.lower():
                        pos = k
                        break
                if not self._sel_is_current(lst[pos]["path"]):
                    self._db_open(lst[pos], pos, autoplay=self._auto_play())
                return
            # 其它（.thm / 说明书等）：只列信息，不动播放
            self.db_stop()
            self.db_cur_video = None
            self._db_show_plain_panel(path, label)

        def _db_show_folder_panel(self, path):
            """文件夹：画面上显示提示，下面给本层统计"""
            self.db_frame = None
            self.wave_mode = False
            self._pv_clear_wave()
            self._db_tip("📁 文件夹")
            self._db_redraw()
            nd = nf = 0
            total = 0
            try:
                for e in os.scandir(path):
                    try:
                        if e.is_dir():
                            nd += 1
                        else:
                            nf += 1
                            total += e.stat().st_size
                    except Exception:
                        pass
            except Exception:
                pass
            self._pv_hint("%s\n本层 %d 个文件夹 / %d 个文件 · 合计 %s"
                          % (path, nd, nf, human(total))[:300])
            self._db_enable_ctl(False)

        def _db_show_plain_panel(self, path, label):
            self.db_frame = None
            self.wave_mode = False
            self._pv_clear_wave()
            self._db_tip(label)
            self._db_redraw()
            try:
                size = human(os.path.getsize(path))
            except Exception:
                size = "-"
            self._pv_hint("%s · %s\n%s" % (os.path.basename(path), size, label))
            self._db_enable_ctl(False)

        def _on_card_open(self, _=None):
            iid = self.card_tree.focus()
            if iid in self.card_map:
                self._card_fill(iid)

        def _on_card_dclick(self, event):
            iid = self.card_tree.identify_row(event.y)
            item = self.card_map.get(iid)
            if not item:
                return
            if item["is_dir"]:
                opened = bool(self.card_tree.item(iid, "open"))
                self.card_tree.item(iid, open=not opened)
                if not opened:
                    self._card_fill(iid)
                return
            label = self._kind_label(item["path"], False)
            if label == "视频":
                self._on_card_select()
                self.after(80, self._db_ensure_play)   # 加了「自动播放」开关，双击一律开播
            elif label == "音乐":
                self._on_card_select()
                self.after(80, self._db_ensure_play)
            else:
                try:
                    os.startfile(item["path"])
                except Exception:
                    pass

        # ---------------- 记忆棒浏览（目录树，按需展开）
        def _kind_label(self, path, is_dir=False):
            if is_dir:
                return "文件夹"
            ext = os.path.splitext(path)[1].lower()
            if ext == ".thm":
                return "缩略图"
            if ext in VIDEO_EXTS:
                return "视频"
            if ext in AUDIO_EXTS:
                return "音乐"
            return "其他"

        def _card_rel(self, path):
            root = card_root(self.cfg)
            try:
                return os.path.relpath(path, root) if root else path
            except Exception:
                return path

        def _card_insert(self, parent, path, name, is_dir):
            self._card_seq += 1
            iid = "n%d" % self._card_seq
            try:
                st = os.stat(path)
                size = "" if is_dir else human(st.st_size)
                date = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            except Exception:
                size, date = "", ""
            self.card_tree.insert(parent, "end", iid=iid, text=name,
                                  values=(self._kind_label(path, is_dir), size, date),
                                  open=False)
            self.card_map[iid] = {"path": path, "is_dir": is_dir, "name": name,
                                  "loaded": False}
            if is_dir:
                self.card_tree.insert(iid, "end", iid=iid + "_d", text="加载中…")
            return iid

        def _card_fill(self, iid):
            item = self.card_map.get(iid)
            if not item or item["loaded"]:
                return
            item["loaded"] = True
            d = item["path"]
            for child in self.card_tree.get_children(iid):
                self.card_tree.delete(child)
                self.card_map.pop(child, None)
            entries = []
            try:
                for e in os.scandir(d):
                    if e.name.startswith("$") or e.name.lower() == "system volume information":
                        continue
                    entries.append(e)
            except Exception:
                return
            entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()))
            for e in entries:
                try:
                    is_dir = e.is_dir()
                except Exception:
                    is_dir = False
                self._card_insert(iid, e.path, e.name, is_dir)

        def refresh_card(self):
            root = card_root(self.cfg)
            self.card_tree.delete(*self.card_tree.get_children())
            self.card_map = {}
            self.card_hint.place_forget()
            if not root:
                self.card_label.configure(text="记忆棒：未设置")
                self.card_hint.configure(text="（提示：请在设置里添加记忆棒目录）")
                self.card_hint.place(relx=0.5, rely=0.45, anchor="center")
                return
            self.card_label.configure(text="记忆棒：%s" % root)
            if not os.path.isdir(root):
                self.card_hint.configure(text="记忆棒目录不存在：%s" % root)
                self.card_hint.place(relx=0.5, rely=0.45, anchor="center")
                return
            self.card_tree.insert("", "end", iid="root", text=root,
                                  values=("记忆棒根目录", "", ""), open=True)
            self.card_map["root"] = {"path": root, "is_dir": True, "name": root,
                                     "loaded": True}
            # 卡里动辄几万个文件，直接 scandir 会把界面冻住，改到后台去读
            self.card_hint.configure(text="正在读取记忆棒…")
            self.card_hint.place(relx=0.5, rely=0.45, anchor="center")

            def work(report):
                report("正在读取 %s …" % root)
                entries = []
                try:
                    for e in os.scandir(root):
                        if (e.name.startswith("$")
                                or e.name.lower() == "system volume information"):
                            continue
                        try:
                            is_dir = e.is_dir()
                        except Exception:
                            is_dir = False
                        entries.append((e.path, e.name, is_dir))
                except Exception:
                    pass
                entries.sort(key=lambda x: (not x[2], x[1].lower()))
                return entries

            def done(res):
                try:
                    self.card_hint.place_forget()
                    for path, name, is_dir in res:
                        self._card_insert("root", path, name, is_dir)
                    if not self.card_tree.get_children("root"):
                        self.card_hint.configure(text="记忆棒里是空的")
                        self.card_hint.place(relx=0.5, rely=0.45, anchor="center")
                except Exception:
                    pass

            self._bg_task("读取记忆棒", work, done, "正在读取目录…",
                          modal=False, quiet=True)

        def open_card_dir(self):
            root = card_root(self.cfg)
            if root and os.path.isdir(root):
                try:
                    os.startfile(root)
                except Exception:
                    pass

        def _bg_task(self, title, work, done, busy_text="处理中…",
                     modal=True, quiet=False):
            """把耗时 I/O 丢进后台线程，主界面照常响应

            work(report) 在后台线程跑，report(文字, 已完成, 总数) 回报进度；
            done(结果) 回到主线程执行。
            quiet=False 弹一个自绘进度窗盯着（modal=True 时期间主窗口不可点，
            防止连点触发两次）；quiet=True 不弹窗，读取这种一闪而过的活儿用它。
            """
            q = pyqueue.Queue()
            win = lbl = cv = bar = None

            def set_pct(p):
                if cv is None:
                    return
                try:
                    cv.coords(bar, 0, 0, (w - 34) * max(0.0, min(1.0, p)), 12)
                except Exception:
                    pass

            if not quiet:
                win = tk.Toplevel(self)
                win.title(title)
                win.transient(self)
                win.resizable(False, False)
                win.configure(bg="#f4f7fb")
                try:
                    win.attributes("-toolwindow", True)
                except Exception:
                    pass
                w, h = 420, 110
                # 直接按屏幕居中，不用 update_idletasks 去量主窗口（那一下会整窗重绘）
                try:
                    sw = self.winfo_screenwidth()
                    sh = self.winfo_screenheight()
                    win.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                                  max(0, (sh - h) // 3)))
                except Exception:
                    win.geometry("%dx%d" % (w, h))

                tk.Label(win, text=title, bg="#f4f7fb", fg="#204060",
                         font=("Microsoft YaHei UI", 11, "bold")).pack(
                    anchor="w", padx=16, pady=(12, 2))
                lbl = tk.Label(win, text=busy_text, bg="#f4f7fb", fg="#63707f",
                               font=("Microsoft YaHei UI", 9), anchor="w")
                lbl.pack(anchor="w", padx=16, fill="x")
                cv = tk.Canvas(win, width=w - 34, height=12, bg="#e2e9f1",
                               highlightthickness=1, highlightbackground="#c9d3e0")
                cv.pack(padx=16, pady=(9, 14), fill="x")
                bar = cv.create_rectangle(0, 0, 0, 12, fill="#ff8a26", width=0)
                win.protocol("WM_DELETE_WINDOW", lambda: None)   # 干活期间别让人误关
                if modal:
                    win.grab_set()

            def report(text="", cur=0, total=0):
                q.put(("p", text, cur, total))

            def runner():
                try:
                    q.put(("ok", work(report)))
                except Exception as e:
                    q.put(("err", "%s" % e))

            threading.Thread(target=runner, daemon=True).start()
            got = []

            def tick():
                while True:
                    try:
                        m = q.get_nowait()
                    except pyqueue.Empty:
                        break
                    if m[0] == "p":
                        if win is not None:
                            if m[1]:
                                lbl.configure(text=m[1])
                            if m[3]:
                                set_pct(float(m[2]) / float(m[3]))
                    else:
                        got.append(m)
                        break
                if got:
                    if win is not None:
                        try:
                            if modal:
                                win.grab_release()
                        except Exception:
                            pass
                        try:
                            win.destroy()
                        except Exception:
                            pass
                    if got[0][0] == "ok":
                        try:
                            done(got[0][1])
                        except Exception as e:
                            messagebox.showerror(APP_NAME, "处理完但回写出错：%s" % e)
                    else:
                        messagebox.showerror(APP_NAME, "出错了：%s" % got[0][1])
                    return
                try:
                    if win is not None:
                        win.after(90, tick)
                    else:
                        self.after(60, tick)
                except Exception:
                    pass

            tick()

        # ---------------- 队列
        def add_files(self):
            files = filedialog.askopenfilenames(
                title="选择要转换的媒体文件",
                filetypes=[("视频 / 音频",
                            "*.mp4 *.m4v *.mov *.mkv *.avi *.flv *.ts *.webm *.wmv *.3gp *.mpg *.mpeg *.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.wma *.amr"),
                           ("全部文件", "*.*")])
            if files:
                n = self.add_paths(list(files))
                self.canvas.itemconfig(self.status_text,
                                       text="已加入 %d 个文件" % n)
                self.after(1500, self._refresh_status)

        def add_paths(self, paths, base=None):
            added = 0
            for p in paths:
                if os.path.isdir(p):
                    for cur, dirs, fs in os.walk(p):
                        for fn in sorted(fs):
                            ext = os.path.splitext(fn)[1].lower()
                            if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
                                added += self._add_one(os.path.join(cur, fn), base)
                elif os.path.isfile(p):
                    added += self._add_one(p, base)
            self._rebuild_table()
            self._refresh_status()
            return added

        def _add_one(self, path, base=None):
            ext = os.path.splitext(path)[1].lower()
            if ext in VIDEO_EXTS:
                kind = "video"
            elif ext in AUDIO_EXTS:
                kind = "audio"
            else:
                return 0
            if any(it["src"].lower() == path.lower() for it in self.items):
                return 0
            title = safe_name(os.path.splitext(os.path.basename(path))[0])
            self.items.append({"src": path, "kind": kind, "title": title,
                               "author": (base or {}).get("author", ""),
                               "state": "queued", "info": "待转换", "pct": 0})
            return 1

        def _rebuild_table(self):
            self.tree.delete(*self.tree.get_children())
            for i, it in enumerate(self.items, 1):
                self.tree.insert("", "end", iid=str(i - 1), values=(
                    i, "视频" if it["kind"] == "video" else "音频",
                    it["author"] or "-",
                    os.path.basename(it["src"]), it["info"]))
            if self.items:
                self.hint.place_forget()
            else:
                self.hint.place(relx=0.5, rely=0.45, anchor="center")
            self._refresh_status()

        def _set_row(self, idx, info=None, pct=None):
            if idx < len(self.items):
                if info is not None:
                    self.items[idx]["info"] = info
                if pct is not None:
                    self.items[idx]["pct"] = pct
                try:
                    self.tree.set(str(idx), "info", self.items[idx]["info"])
                except Exception:
                    pass

        def remove_selected(self):
            sel = sorted([int(i) for i in self.tree.selection()], reverse=True)
            for i in sel:
                if 0 <= i < len(self.items) and self.items[i]["state"] == "queued":
                    self.items.pop(i)
            self._rebuild_table()

        def clear_all(self):
            if self.running:
                self.stop_flag = True
                kill_current()
                return
            self.items = []
            self.done = self.fail = 0
            self._rebuild_table()

        def play_selected(self):
            sel = self.tree.selection()
            if not sel:
                return
            p = self.items[int(sel[0])]["src"]
            try:
                os.startfile(p)
            except Exception:
                pass

        # ---------------- 拖放
        def _bind_dnd(self):
            if not has_dnd:
                return

            def parse(data):
                out, buf, brace = [], "", False
                for ch in data:
                    if ch == "{":
                        brace = True
                    elif ch == "}":
                        brace = False
                        if buf:
                            out.append(buf)
                            buf = ""
                    elif ch == " " and not brace:
                        if buf:
                            out.append(buf)
                            buf = ""
                    else:
                        buf += ch
                if buf:
                    out.append(buf)
                return out

            def on_drop(e):
                n = self.add_paths(parse(e.data))
                self.canvas.itemconfig(self.status_text, text="已加入 %d 个文件" % n)
                self.after(1200, self._refresh_status)
                if n and self.page != "queue":
                    self.show_page("queue")

            for w in (self, self.canvas, self.body, self.tree, self.queue_tree_fr,
                      self.card_tree, self.card_tree_fr, self.db_a_tree,
                      self.db_v_tree):
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", on_drop)
                except Exception:
                    pass

        # ---------------- 设置（内嵌在主体区，不弹窗）
        def open_settings(self):
            """右上「设置」：在主体区展开设置页；再点一次收起"""
            if getattr(self, "_settings_open", False):
                self.hide_settings()
                return
            self._settings_open = True
            for f in self.pages.values():
                try:
                    f.pack_forget()
                except Exception:
                    pass
            try:
                self.band.pack_forget()
            except Exception:
                pass
            self.settings_page.pack(fill="both", expand=True)
            try:
                self.btn_set.set_active(True)
            except Exception:
                pass

        def hide_settings(self, reopen=True):
            self._settings_open = False
            try:
                self.settings_page.pack_forget()
            except Exception:
                pass
            try:
                self.btn_set.set_active(False)
            except Exception:
                pass
            if reopen:
                page = getattr(self, "page", "card") or "card"
                self._pack_band(page)
                self.show_page(page)

        def _pack_band(self, before_name="card"):
            """把地址栏 + 播放条那一行放回页面顶部"""
            before = self.pages.get(before_name)
            try:
                if before is not None:
                    self.band.pack(fill="x", pady=(0, 6), before=before)
                else:
                    self.band.pack(fill="x", pady=(0, 6))
            except Exception:
                try:                      # before 还没被管理时，退回普通 pack
                    self.band.pack(fill="x", pady=(0, 6))
                except Exception:
                    pass

        # ---------------- 抖音数据库：作者列表 → 视频列表
        def db_browse(self):
            p = filedialog.askdirectory(title="选择抖音数据库目录（含 .appdata）")
            if p:
                self.db_path_var.set(p)
                self.db_load()        # 选完直接载入，不再需要「载入」按钮

        def db_load(self):
            root = (self.db_path_var.get() or "").strip()
            if not root:
                return
            if getattr(self, "_db_loading", False):
                return
            self._db_loading = True
            # 先拿上次扫好的索引顶上：窗口立刻有内容，不用等磁盘转完
            obj = self._db_read_cache(root)
            if obj:
                self._db_apply_load(self._db_shim(root, obj), obj["authors"], root)
                try:
                    self.db_status.configure(
                        text="已载入 %d 位作者（缓存），后台核对中…"
                        % len(obj["authors"]))
                except Exception:
                    pass
            else:
                try:
                    self.db_status.configure(text="正在扫描数据库…")
                except Exception:
                    pass
            # 磁盘慢也不冻界面：全量扫在后台，扫完覆盖并写回缓存
            if not hasattr(self, "_db_load_q"):
                self._db_load_q = pyqueue.Queue()
            threading.Thread(target=self._db_load_worker, args=(root,),
                             daemon=True).start()
            self._db_load_poll()

        def _db_load_poll(self):
            """主线程收后台扫描的结果（Tk 控件只能在主线程碰）"""
            try:
                res = self._db_load_q.get_nowait()
            except pyqueue.Empty:
                self.after(150, self._db_load_poll)
                return
            if res[0] == "err":
                self._db_load_fail(res[1])
                return
            self._db_apply_load(res[1], res[2], res[3])

        def _db_cache_file(self, root):
            return os.path.join(os.path.normpath(root), ".appdata",
                                "psp_authors_cache.json")

        def _db_read_cache(self, root):
            """读上次扫好的作者索引（含条目清单），损坏或对不上就当没有"""
            try:
                with open(self._db_cache_file(root), "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if not obj.get("authors"):
                    return None
                return obj
            except Exception:
                return None

        def _db_write_cache(self, root, db, authors):
            p = self._db_cache_file(root)
            obj = {"root": os.path.normpath(root), "ts": time.time(),
                   "follow": getattr(db, "follow", ""),
                   "authors": authors}
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, p)

        def _db_shim(self, root, obj):
            """按缓存内容拼一个够用的库对象（totals/头像索引都能用）"""
            db = DouyinDB.__new__(DouyinDB)
            db.root = root
            db.meta = os.path.join(root, ".appdata")
            db.follow = obj.get("follow") or root
            db.authors_meta = {}
            db.videos = {}
            db.texts = {}
            db.buckets = [{"aid": a["aid"], "name": a["name"],
                           "items": a["items"]} for a in obj["authors"]]
            return db

        def _db_make_authors(self, db):
            """把扫描结果整理成作者行（头像、封面、签名都在这里定）"""
            brief = ("signature", "shortId", "uniqueIds", "nicknames",
                     "followerCount", "videoCount", "secUid")
            authors = []
            for b in db.buckets:
                full = db.authors_meta.get(b["aid"]) or {}
                meta = dict((k, full[k]) for k in brief if k in full)
                uid = (meta.get("shortId") or (meta.get("uniqueIds") or [""])[0]
                       or b["aid"])
                cover = ""
                for it in b["items"]:
                    if it["kind"] != "video":
                        continue
                    c = os.path.join(os.path.dirname(os.path.dirname(it["path"])),
                                     "封面", it["vid"] + ".jpg")
                    if os.path.isfile(c):
                        cover = c
                        break
                authors.append({
                    "aid": b["aid"], "name": b["name"], "uid": str(uid),
                    "sig": (meta.get("signature") or "").replace("\n", " "),
                    "cnt": len(b["items"]), "cover": cover, "meta": meta,
                    "head": db.avatar_of([
                        uid,
                        meta.get("shortId"),
                        b["aid"],
                    ] + list((meta.get("uniqueIds") or [])[:4])),
                    "items": b["items"],
                })
            return authors

        def _db_load_worker(self, root):
            try:
                db = DouyinDB(root)
                authors = self._db_make_authors(db)
            except Exception as e:
                self._db_load_q.put(("err", str(e)))
                return
            try:
                self._db_write_cache(root, db, authors)
            except Exception:
                pass
            self._db_load_q.put(("ok", db, authors, root))

        def _db_load_fail(self, msg):
            self._db_loading = False
            try:
                self.db_status.configure(text="载入失败：%s" % msg)
            except Exception:
                pass

        def _db_apply_load(self, db, authors, root):
            self.db = db
            self.db_authors = authors
            self._db_loading = False
            self.cfg["db_root"] = root
            try:
                save_config(self.cfg)
            except Exception:
                pass
            self.db_back_authors()
            try:
                a, n = db.totals()
                self.db_status.configure(text="共 %d 位作者 / %d 个文件 · 双击作者看视频"
                                         % (a, n))
            except Exception:
                pass

        def _db_apply_view(self):
            """作者视图 / 视频视图切换；作者视图下播放时右侧换成播放界面"""
            in_videos = self.db_view == "videos"
            show_play = in_videos or bool(getattr(self, "db_play_panel", False))
            if in_videos:
                self.db_a_tree_fr.pack_forget()
                self.db_v_tree_fr.pack(fill="both", expand=True)
            else:
                self.db_v_tree_fr.pack_forget()
                self.db_a_tree_fr.pack(fill="both", expand=True)
            if show_play:
                self.db_author_fr.pack_forget()
                self.db_video_fr.pack(fill="both", expand=True)
            else:
                self.db_video_fr.pack_forget()
                self.db_author_fr.pack(fill="both", expand=True)
                if getattr(self, "db_authors", None):
                    # 作者列表待播：只留播放 + 循环/随机，其余按钮灰掉
                    self._db_enable_ctl(True, play_only="play")
            try:
                self.db_back_btn.set_enabled(in_videos)
            except Exception:
                pass

        def db_back_authors(self):
            keep = self.db_cur_author
            self.db_stop()
            self.db_view = "authors"
            self.db_play_panel = False
            self.db_ai_mode = False
            self.db_cur_video = None
            self.db_pos = 0.0
            self._db_apply_view()
            self.db_a_tree.delete(*self.db_a_tree.get_children())
            for i, a in enumerate(self.db_authors):
                self.db_a_tree.insert("", "end", iid=str(i), values=(
                    a["name"], a["uid"], a["sig"][:34] + ("…" if len(a["sig"]) > 34
                                                          else ""), a["cnt"]))
            if self.db_authors:
                idx = 0
                if keep is not None:
                    try:
                        idx = self.db_authors.index(keep)
                    except ValueError:
                        idx = 0
                self.db_cur_author = self.db_authors[idx]
                self.db_a_tree.selection_set(str(idx))
                self.db_a_tree.see(str(idx))
                self._show_db_author(self.db_authors[idx])

        def db_open_author(self, aid=None):
            a = self.db_cur_author or (self.db_authors[0] if self.db_authors else None)
            if not a:
                return
            self.db_view = "videos"
            self.db_ai_mode = False
            self.db_cur_author = a
            self._db_apply_view()
            self.db_videos = a["items"]
            self.db_v_tree.delete(*self.db_v_tree.get_children())
            for i, it in enumerate(self.db_videos):
                self.db_v_tree.insert("", "end", iid=str(i), values=(
                    it["title"] or os.path.splitext(os.path.basename(it["path"]))[0],
                    it["vid"], "", human(it["size"]), ""))
            self.db_cur_video = None
            self._db_show_video_panel(None)
            self.db_status.configure(text="作者：%s · %d 个文件（双击一行播放）"
                                     % (a["name"], len(self.db_videos)))

        def _on_db_author_select(self, _=None):
            sel = self.db_a_tree.selection()
            i = int(sel[0]) if sel else -1
            if 0 <= i < len(self.db_authors):
                self.db_cur_author = self.db_authors[i]
                if (self.db_view == "authors"
                        and not getattr(self, "db_play_panel", False)):
                    self._show_db_author(self.db_cur_author)

        def _on_db_author_dbl(self, _=None):
            sel = self.db_a_tree.selection()
            if not sel:
                return
            self.db_cur_author = self.db_authors[int(sel[0])]
            self.db_open_author()

        def _db_all_list(self, start_aid=None, whole=False):
            """从所选作者这一行起，把之后的作者视频按顺序拼成一条播放列表；
            whole=True 表示不管当前选中谁，整库所有作者都收进来（随机播放用）"""
            out = []
            want = None if whole else (start_aid or (self.db_cur_author or {}).get("aid"))
            started = not bool(want)
            for a in self.db_authors:
                if not started:
                    if a.get("aid") == want:
                        started = True
                    else:
                        continue
                for it in a.get("items") or []:
                    if not os.path.isfile(it["path"]):
                        continue
                    out.append(dict(it, kind=it.get("kind") or "video",
                                    author=a.get("name") or "",
                                    author_aid=a.get("aid") or ""))
            if not out:                       # 后面没东西了：整库兜底
                for a in self.db_authors:
                    for it in a.get("items") or []:
                        if os.path.isfile(it["path"]):
                            out.append(dict(it, kind=it.get("kind") or "video",
                                            author=a.get("name") or "",
                                            author_aid=a.get("aid") or ""))
            return out

        def db_play_authors(self):
            """作者列表按播放：从所选作者的第一条起，顺序播完后面所有作者的视频"""
            if not getattr(self, "db_authors", None):
                self.db_hint.configure(text="先点【浏览…】载入抖音数据库")
                return
            sel = self.db_a_tree.selection()
            a = None
            if sel:
                try:
                    a = self.db_authors[int(sel[0])]
                except Exception:
                    a = None
            a = a or self.db_cur_author or self.db_authors[0]
            self.db_cur_author = a
            self._shuf_done = set()
            if getattr(self, "shuffle", False):
                # 随机：整库所有作者的视频都在候选里，随便挑一条起步
                lst = self._db_all_list(whole=True)
                pos = random.randrange(len(lst)) if lst else 0
                scope = "整库随机"
            else:
                # 顺序：从所选作者的第一条起，往后一位作者接一位作者
                lst = self._db_all_list(a.get("aid"))
                pos = 0
                scope = "从「%s」起" % (a.get("name"),)
            if not lst:
                self.db_hint.configure(text="没有可播放的文件")
                return
            n_auth = len(set(x.get("author") for x in lst))
            self.media_list = lst
            self.media_idx = pos
            self.db_ai_mode = True
            self.db_play_panel = True         # 右侧：头像信息 → 播放界面
            self._db_apply_view()
            self.db_status.configure(
                text="%s · %s：%d 位作者 / %d 个文件排队"
                % (scope, "随机播放" if self.shuffle else "顺序播放", n_auth, len(lst)))
            self._db_open(lst[pos], pos, autoplay=True)

        def _db_rest_author_view(self):
            """作者列表视图下：右侧从播放界面切回作者头像与信息，并清掉当前文件"""
            if (getattr(self, "page", "") != "db"
                    or getattr(self, "db_view", "") != "authors"):
                return False
            self.db_play_panel = False
            self.db_cur_video = None
            self.db_pos = 0.0
            self._db_apply_view()
            a = self.db_cur_author or (self.db_authors[0]
                                       if getattr(self, "db_authors", None) else None)
            if a:
                self.db_cur_author = a
                self._show_db_author(a)
            return True

        def _show_db_author(self, a):
            """作者头像：先用 关注\\头像\\large_<uid>.jpg，没有再退回第一支视频封面"""
            pic = None
            src = ""
            for cand in (a.get("head"), a.get("cover")):
                if cand and os.path.isfile(cand):
                    src = cand
                    break
            if src:
                try:
                    im = Image.open(src).convert("RGB")
                    side = min(im.size)
                    im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                                  (im.width + side) // 2, (im.height + side) // 2))
                    im = im.resize((300, 300), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(im)
                    self.db_head.configure(image=photo, text="")
                    self.db_head.image = photo
                    pic = True
                except Exception:
                    pic = None
            if not pic:
                self.db_head.configure(image="", text="无头像",
                                       fg="#8a93a5", font=("Microsoft YaHei UI", 12))
                self.db_head.image = None
            meta = a.get("meta") or {}
            fans = meta.get("followerCount")
            lines = [
                ("作者", a["name"]),
                ("抖音号", a["uid"]),
                ("视频数量", "%d 个（库内 %d 个文件）" % (meta.get("videoCount")
                                                          or a["cnt"], a["cnt"])),
                ("粉丝数", ("%s" % fans) if fans else "-"),
            ]
            ids = meta.get("uniqueIds") or []
            if ids:
                lines.append(("曾用昵称", "、".join(meta.get("nicknames") or []) or "-"))
            lines.append(("签名", a["sig"] or "-"))
            lines.append(("作者ID", a["aid"]))
            if meta.get("secUid"):
                lines.append(("secUid", meta["secUid"]))
            self.db_txt.configure(state="normal")
            self.db_txt.delete("1.0", "end")
            for k, v in lines:
                self.db_txt.insert("end", "%s：%s\n" % (k, v))
            self.db_txt.insert("end", "\n提示：双击左侧作者，进入该作者的视频列表。\n")
            self.db_txt.configure(state="disabled")

        def _on_db_video_select(self, _=None):
            if getattr(self, "_pb_quiet", False):
                return
            sel = self.db_v_tree.selection()
            if not sel:
                return
            i = int(sel[0])
            item = self.db_videos[i]
            # 作者的全部视频就是左侧列表：选中即播，循环/随机都在这条列表上
            lst = []
            for v in self.db_videos:
                if os.path.isfile(v["path"]):
                    lst.append(dict(v, kind=v.get("kind") or "video"))
            if not lst:
                self.db_cur_video = item
                self._db_show_video_panel(item)
                return
            pos = 0
            for k, v in enumerate(lst):
                if v.get("path") == item["path"]:
                    pos = k
                    break
            self.media_list = lst
            if not self._sel_is_current(lst[pos].get("path")):
                self._db_open(lst[pos], pos, autoplay=self._auto_play())
            # 顺便补上分辨率/时长两列
            self.after(60, lambda: self._db_fill_video_facts(i, item))

        def _db_fill_video_facts(self, i, item):
            try:
                info = probe(ffprobe, item["path"])
                vs = None
                for s in info.get("streams") or []:
                    if s.get("codec_type") == "video":
                        vs = s
                        break
                res = "%sx%s" % (vs.get("width"), vs.get("height")) if vs else "-"
                dur = _fmt_time(float((info.get("format") or {}).get("duration") or 0))
                self.db_v_tree.set(str(i), "res", res)
                self.db_v_tree.set(str(i), "dur", dur)
            except Exception:
                pass

        def _db_init_ctl(self):
            """等 Canvas 布局出来后再画一遍轨道，否则尺寸还是 1"""
            try:
                self._db_vol_draw()
            except Exception:
                pass
            try:
                self._db_draw_prog(self.db_pos / self.db_dur if self.db_dur else 0.0)
            except Exception:
                pass

        def _db_resync_ctl(self):
            """切回数据库页时把顶部控件按当前选中状态恢复，别让播放键一直灰着"""
            if getattr(self, "page", "") != "db":
                return
            if (getattr(self, "db_state", "idle") != "idle"
                    or getattr(self, "db_cur_video", None)):
                self._db_enable_ctl(True)
                return
            if getattr(self, "db_view", "authors") == "authors":
                ok = bool(getattr(self, "db_cur_author", None))
            else:
                try:
                    ok = bool(self.db_v_tree.selection())
                except Exception:
                    ok = False
            # 只亮播放键：点它就是从这个作者 / 这条视频起播
            self._db_enable_ctl(True, "play") if ok else self._db_enable_ctl(False)

        def _db_enable_ctl(self, on, play_only=None):
            """记下当前页的控件可用性，实际启用交给顶部全局条按当前页判断"""
            self._db_ctl_state = (bool(on), play_only)
            if getattr(self, "page", "") in ("db", "card", "queue"):
                self._pb_apply()

        # ---------------- 三页共用的"打开一个媒体"
        def _media_info(self, path, kind=None):
            """带缓存的入口：同一条文件第二次点就秒开，不再重跑 ffprobe"""
            try:
                key = (path, os.path.getmtime(path))
            except Exception:
                key = (path, 0.0)
            hit = _MEDIA_INFO_CACHE.get(key)
            if hit is not None:
                r = dict(hit)
                r["path"] = path
                return r
            d = self._media_info_raw(path, kind)
            if len(_MEDIA_INFO_CACHE) > 3000:
                _MEDIA_INFO_CACHE.clear()
            _MEDIA_INFO_CACHE[key] = dict(d)
            return d

        def _media_info_raw(self, path, kind=None):
            """探测一条媒体的展示信息：大小/时长/编码/码率/帧率/分辨率/标签"""
            d = {"path": path, "size": 0, "dur": 0.0, "vcodec": "", "acodec": "",
                 "kbps": 0, "fps": 0.0, "w": 0, "h": 0, "title": "",
                 "artist": "", "album": "", "genre": ""}
            try:
                d["size"] = os.path.getsize(path)
            except Exception:
                pass
            if not ffprobe:
                return d
            try:
                info = probe(ffprobe, path)
            except Exception:
                return d
            fmt = info.get("format") or {}
            try:
                d["dur"] = float(fmt.get("duration") or 0)
            except Exception:
                pass
            try:
                d["kbps"] = int(float(fmt.get("bit_rate") or 0) / 1000)
            except Exception:
                pass
            tags = fmt.get("tags") or {}
            for k in ("title", "artist", "album", "genre"):
                v = tags.get(k) or tags.get(k.upper()) or tags.get(k.capitalize())
                if isinstance(v, str):
                    d[k] = v.strip()
            for s in info.get("streams") or []:
                ct = s.get("codec_type")
                if ct == "video" and not d["vcodec"]:
                    d["vcodec"] = s.get("codec_name") or ""
                    d["w"] = int(s.get("width") or 0)
                    d["h"] = int(s.get("height") or 0)
                    fr = s.get("avg_frame_rate") or s.get("r_frame_rate") or ""
                    try:
                        if "/" in fr:
                            a, b = fr.split("/")
                            d["fps"] = float(a) / float(b) if float(b) else 0.0
                        elif fr:
                            d["fps"] = float(fr)
                    except Exception:
                        pass
                    st = s.get("tags") or {}
                    if not d["title"]:
                        d["title"] = (st.get("title") or "").strip()
                elif ct == "audio" and not d["acodec"]:
                    d["acodec"] = s.get("codec_name") or ""
            return d

        def _media_facts(self, d, extra_lines=None):
            """把探测结果压成两行短文案：一行名字大小，一行技术参数"""
            name = os.path.basename(d["path"])
            size = human(d["size"])
            if d["vcodec"]:
                l2 = "%s · %dx%d · %s · %s · %s · %s" % (
                    d["vcodec"].upper(), d["w"], d["h"],
                    ("%.0ffps" % d["fps"]) if d["fps"] else "-",
                    ("%dkbps" % d["kbps"]) if d["kbps"] else "-",
                    _fmt_time(d["dur"]) if d["dur"] else "-",
                    d["acodec"].upper() if d["acodec"] else "无音轨")
            else:
                parts = []
                if d["title"]:
                    parts.append("曲名 " + d["title"])
                if d["artist"]:
                    parts.append("艺术家 " + d["artist"])
                if d["album"]:
                    parts.append("专辑 " + d["album"])
                if d["genre"]:
                    parts.append("流派 " + d["genre"])
                if d["acodec"]:
                    parts.append(d["acodec"].upper())
                if d["kbps"]:
                    parts.append("%dkbps" % d["kbps"])
                if d["dur"]:
                    parts.append(_fmt_time(d["dur"]))
                l2 = " · ".join(parts) or "音频文件"
            lines = ["%s · %s" % (name, size), l2]
            if extra_lines:
                lines.extend(extra_lines)
            return "\n".join(lines)

        def _db_open(self, item, idx=None, autoplay=True):
            """三页共用：视频出画面+声音，音乐出 ffplay + 波形可视化"""
            if not item:
                return
            if idx is not None:
                self.media_idx = idx
            try:
                self._pb_sync_row(int(getattr(self, "media_idx", 0) or 0))
            except Exception:
                pass
            self.db_cur_video = item
            self.db_state = "idle"
            # 换内容时旧画面/声音都要丢掉，不能在这儿等它优雅收尾
            self._db_stop_player_only(quick=True)
            self._db_stop_wave()
            self.db_drag = False
            kind = item.get("kind") or "video"
            facts = item.get("facts") or self._media_info(item["path"])
            item.setdefault("facts", facts)
            item["size"] = item.get("size") or facts["size"]
            item["dur"] = item.get("dur") or facts["dur"]
            self.db_dur = float(facts["dur"] or 0)
            self.db_speed = getattr(self, "db_speed", 1.0)
            title = item.get("title") or ""
            v = self._pv()
            on_db = getattr(self, "page", "") == "db"
            self._db_set_video_author(
                (item.get("author")
                 or (getattr(self, "db_cur_author", None) or {}).get("name"))
                if on_db else None)
            # 作者列表页：作者名 + 视频文案；进了作者的视频列表页就只留作者名
            self._db_set_video_caption(
                title, show=(on_db and self.db_view == "authors"))
            # 播到别的作者那条时，把「当前作者」也跟着挪过去：
            # 停下后右侧头像、状态栏显示的就是刚播的这一位
            aid = item.get("author_aid")
            if aid and getattr(self, "db_authors", None):
                cur = getattr(self, "db_cur_author", None) or {}
                if cur.get("aid") != aid:
                    for a in self.db_authors:
                        if a.get("aid") == aid:
                            self.db_cur_author = a
                            break
            facts_txt = self._media_facts(facts)
            if v:
                v["facts"] = facts_txt
            if kind == "audio":
                self.wave_mode = True
                self.db_frame = None
                self.db_poster = None
                self._db_tip("♪ 音乐 · 频谱")
                self._db_enable_ctl(True)
                self.db_play_btn.set_sym("play")
                self._db_set_time(0)
                self._pv_hint(facts_txt)
                self._db_wave_prepare(item["path"], self.db_dur)
                self._db_redraw()
                if autoplay:
                    self._db_audio_only_play(0.0)
                return
            self.wave_mode = False
            self._pv_clear_wave()
            self._db_show_video_panel(item)
            if autoplay:
                self.db_play_from(0.0)

        # ---------------- 音乐：ffplay 出声 + 波形可视化
        def _db_audio_only_play(self, pos=0.0):
            item = getattr(self, "db_cur_video", None)
            if not item:
                return
            if not find_tool("ffplay"):
                self._pv_hint("缺少 ffplay，无法播放音频")
                return
            if (self.db_view == "authors"
                    and not getattr(self, "db_play_panel", False)):
                self.db_play_panel = True        # 作者视图下开播：右侧换成播放界面
                self._db_apply_view()
            self._db_stop_player_only()
            self.wave_mode = True
            self.db_state = "playing"
            self.db_seek_base = float(pos)
            self.db_t0 = time.time()
            self.db_play_btn.set_sym("pause")
            self.db_play_btn.set_enabled(True)
            self.db_stop_btn.set_enabled(True)
            self._db_set_time(pos)
            self.db_hint.configure(text="音乐播放中 · %s" % self._sp_label(self.db_speed))
            self._db_audio_start(pos)
            self._db_wave_start()
            if not self.db_tick_id:
                self._db_tick()

        def _pv_clear_wave(self):
            """从音乐切回视频时把波形藏起来，别盖在画面上"""
            v = self._pv()
            if not v:
                return
            try:
                v["cv"].itemconfig(v["wave"], state="hidden")
                v["cv"].delete("fxbtn")
            except Exception:
                pass
            self._wave_buf = None

        def _db_wave_prepare(self, path, dur):
            """后台算出整首歌的频谱矩阵（56 个固定频段 × 每 32ms 一帧），供各特效取用"""
            need = int(max(1.0, min(dur or 0.0, 900.0)) / 0.08)
            cache = getattr(self, "_wave_cache", None)
            if cache and cache[0] == path and len(cache[1]) >= need - 4:
                self.wave_env = cache[1]
                self.wave_spec = cache[2] if len(cache) > 2 else None
                self.wave_dt = cache[3] if len(cache) > 3 else 0.08
                self.wave_pcm = cache[4] if len(cache) > 4 else None
                return
            self.wave_env = [0.0] * max(8, need)
            self.wave_spec = None
            self.wave_pcm = None
            self.wave_dt = 0.08
            if not ffmpeg:
                return
            self.wave_env_path = path

            def work():
                try:
                    p = subprocess.Popen(
                        [ffmpeg, "-v", "error", "-i", path,
                         "-t", "900", "-ac", "1", "-ar", "8000", "-f", "s16le",
                         "-acodec", "pcm_s16le", "pipe:1"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        creationflags=no_window())
                    raw = p.communicate()[0] or b""
                except Exception:
                    return
                step = 640          # 8kHz × 0.08s
                env = []
                for i in range(0, len(raw) - step, step):
                    chunk = raw[i:i + step]
                    try:
                        vals = struct.unpack("<%dh" % (len(chunk) // 2), chunk)
                    except Exception:
                        continue
                    peak = 0
                    for v in vals[::3]:
                        a = v if v >= 0 else -v
                        if a > peak:
                            peak = a
                    env.append(peak / 32768.0)
                if env:
                    mx = max(env) or 1.0
                    env = [min(1.0, (v / mx) ** 0.55) for v in env]

                spec, dt = None, 0.08
                try:
                    spec, dt = _spectrogram(raw)
                except Exception:
                    spec = None
                if spec is None and not env:
                    env = [0.12 + 0.42 * abs(((i * 37) % 53) - 26) / 26.0
                           for i in range(max(8, need))]
                self._wave_cache = (path, env, spec, dt, raw)
                self.wave_env = env
                self.wave_spec = spec
                self.wave_dt = dt
                self.wave_pcm = raw

            threading.Thread(target=work, daemon=True).start()

        def _db_wave_start(self):
            if not getattr(self, "db_wave_id", None):
                self._db_wave_tick()

        def _db_stop_wave(self):
            try:
                if getattr(self, "db_wave_id", None):
                    self.after_cancel(self.db_wave_id)
            except Exception:
                pass
            self.db_wave_id = None

        def _db_wave_tick(self):
            """可视化刷新：按目标帧周期补偿绘制耗时，避免 after 固定值越拖越慢"""
            self.db_wave_id = None
            if not getattr(self, "wave_mode", False) or self.db_state != "playing":
                return
            t0 = time.perf_counter()
            try:
                self._db_wave_draw()
            except Exception:
                pass
            self._wave_ms = (time.perf_counter() - t0) * 1000.0
            # 目标 18ms 一帧（≈55fps）；本帧绘制慢就顺延，别把消息循环堵死
            wait = 18.0 - self._wave_ms
            self.db_wave_id = self.after(max(1, int(wait)), self._db_wave_tick)

        FX_NAMES = ("频谱条", "上下镜像", "环形频谱", "示波波形", "频带粒子",
                    "频谱隧道", "瀑布地形")
        WAVE_BANDS = 56

        def _wave_build(self, v, cv, W, H, n):
            """底图 + 泛光 + 暗角 + 柱体精灵一次烤好；换尺寸或换特效时才重建"""
            base = int(H * 0.80)
            maxh = int(H * 0.62)
            left = int(W * 0.055)
            right = int(W * 0.945)
            slot = (right - left) / float(n)
            bw = max(2, int(slot * 0.78))
            bg = Image.new("RGB", (W, H), (7, 8, 13))
            dd = ImageDraw.Draw(bg)
            dd.line([(0, 0), (int(W * 0.35), H)], fill=(3, 4, 9))
            dd.line([(int(W * 0.35), H), (W, 0)], fill=(9, 6, 14))
            for y in range(H):
                c = int(8 + 5 * (y / max(1.0, H)))
                dd.line([(0, y), (W, y)], fill=(c, c, c + 5))
            try:                             # logo 当背景：铺满压暗，再把上面这层渐变轻轻叠上去
                lp = pick_logo_path(getattr(self, "cfg", None))
                lbg = logo_bg_image(W, H, lp, 0.55) if lp else None
                if lbg is not None:
                    bg = Image.blend(lbg, bg, 0.18)
                    dd = ImageDraw.Draw(bg)
            except Exception:
                pass
            clean = bg.copy()
            vig = None
            try:
                glow = _glow_sprite(W, H, (0, 150, 220), 26)
                clean.paste(glow, (0, 0), glow)
                vig = _vignette_sprite(W, H)
                clean.paste(vig, (0, 0), vig)
            except Exception:
                pass
            photo = ImageTk.PhotoImage(bg)
            cv.itemconfig(v["wave"], image=photo, state="normal")
            cv.coords(v["wave"], W // 2, H // 2)
            cv.tag_raise(v["wave"])
            lay_w = max(8, W // 3)
            lay_h = max(8, H // 3)
            lay = Image.new("RGBA", (lay_w, lay_h), (0, 0, 0, 0))
            mh = int(H * 0.33)
            b = {
                "key": (id(cv), W, H), "n": n,
                "bg": bg, "clean": clean, "photo": photo,
                "draw": ImageDraw.Draw(bg), "vig": vig,
                "crops": {}, "refc": {},
                "base": base, "maxh": maxh, "left": left, "right": right,
                "slot": slot, "bw": bw,
                "bar": _bar_sprite(bw, maxh),
                "refl": _refl_sprite(bw, max(2, int(maxh * 0.45))),
                "lay": lay, "lay_w": lay_w, "lay_h": lay_h,
                "lbase": base / 3.0, "lmaxh": maxh / 3.0,
                "lleft": left / 3.0, "lslot": slot / 3.0,
                "lbw": max(1, bw // 3),
                "lbar": _bar_sprite(max(1, bw // 3), max(8, maxh // 3)),
                "mb": _mirror_sprite(bw, mh),
                "smooth": [0.0] * n, "peaks": [0.0] * n, "hold": [0] * n,
                "parts": [], "trail": [],
            }
            self._wave_make_fx_btn(cv, W, H)
            return b

        def _wave_make_fx_btn(self, cv, W, H):
            """右下角特效切换按钮：胶囊底 + 当前特效名，点一下换下一种"""
            try:
                cv.delete("fxbtn")
            except Exception:
                pass
            x1 = W - 10
            y1 = H - 26          # 抬到进度条上方，按钮底别压在进度带上
            x0 = x1 - 92
            y0 = y1 - 24
            col = "#0d1826"
            cv.create_oval(x0, y0, x0 + 24, y1, fill=col, outline="#31465c",
                           tags="fxbtn")
            cv.create_oval(x1 - 24, y0, x1, y1, fill=col, outline="#31465c",
                           tags="fxbtn")
            cv.create_rectangle(x0 + 12, y0, x1 - 12, y1, fill=col, outline="",
                                tags="fxbtn")
            cv.create_line(x0 + 12, y0, x1 - 12, y0, fill="#31465c", tags="fxbtn")
            cv.create_line(x0 + 12, y1, x1 - 12, y1, fill="#31465c", tags="fxbtn")
            cv.create_text((x0 + x1) / 2.0, (y0 + y1) / 2.0,
                           text="%s ›" % self.FX_NAMES[getattr(self, "wave_fx", 0)],
                           fill="#9fd8ee", font=("Microsoft YaHei UI", 9),
                           tags=("fxbtn", "fxtx"))
            cv.tag_bind("fxbtn", "<Button-1>", self._db_fx_next)
            cv.tag_raise("fxbtn")
        def _db_fx_next(self, _=None):
            self.wave_fx = (getattr(self, "wave_fx", 0) + 1) % len(self.FX_NAMES)
            self._wave_buf = None
            self._wave_buf_fs = None
            v = self._pv()
            try:
                if v:
                    self._wave_make_fx_btn(v["cv"], v["cv"].winfo_width(),
                                           v["cv"].winfo_height())
            except Exception:
                pass
            self._db_wave_draw()
            return "break"          # 别让这次点击继续冒到画布上的进度条

        def _wave_points(self, pos, npts=200, amp=0.26, cy=None, b=None):
            """从整段 PCM 里取当前播放位置的一小窗，折成折线（用发光层的坐标）"""
            pcm = getattr(self, "wave_pcm", None)
            if b is None:
                b = getattr(self, "_wave_buf", None)       # 兼容旧调用
            if not pcm or not b:
                return []
            lw = b["lay_w"]
            lh = b["lay_h"]
            x0 = int(lw * 0.03)
            x1 = int(lw * 0.97)
            cyv = (lh // 2) if cy is None else cy
            sr = 8000
            wl = 256
            c = int(max(0.0, pos) * sr)
            c = min(max(0, c - wl // 2), max(0, len(pcm) // 2 - wl))
            seg = pcm[c * 2:c * 2 + wl * 2]
            if len(seg) < wl * 2:
                return []
            vals = struct.unpack("<%dh" % (len(seg) // 2), seg)
            step = max(1, len(vals) // npts)
            amp_px = amp * lh
            pts = []
            for i in range(npts):
                sub = vals[i * step:(i + 1) * step]
                v = (sum(sub) / float(len(sub))) if sub else 0
                pts.append((x0 + (x1 - x0) * i / float(npts - 1),
                            cyv - (v / 32768.0) * amp_px))
            return pts

        def _db_wave_draw(self):
            """音乐可视化：照 2026-11-07 那个可视化程序的规格（极光配色 / 发光 / 暗角），
            七种效果用右下角按钮切换。全屏时直接按屏幕尺寸画，不靠放大图片（否则又黑又慢）"""
            fs = getattr(self, "db_fs", None)
            is_fs = fs is not None
            v = None
            if is_fs:                       # 全屏：目标就是全屏画布上的那张图
                cv = fs
                v = {"wave": self.db_fs_img_id}
            else:
                v = self._pv()
                if not v:
                    return
                cv = v["cv"]
            try:
                W = max(80, cv.winfo_width())
                H = max(80, cv.winfo_height())
            except Exception:
                return
            try:
                if v.get("tip") and cv.itemcget(v["tip"], "state") != "hidden":
                    cv.itemconfig(v["tip"], state="hidden")
                if v.get("img") and cv.itemcget(v["img"], "state") != "hidden":
                    cv.itemconfig(v["img"], image="", state="hidden")
            except Exception:
                pass
            n = self.WAVE_BANDS
            buf_name = "_wave_buf_fs" if is_fs else "_wave_buf"
            b = getattr(self, buf_name, None)
            if not b or b["key"] != (id(cv), W, H):
                b = self._wave_build(v or {}, cv, W, H, n)
                setattr(self, buf_name, b)
            bg = b["bg"]
            d = b["draw"]
            lay = b["lay"]
            fx = getattr(self, "wave_fx", 0)
            bg.paste(b["clean"])
            if fx:
                lay.paste((0, 0, 0, 0), (0, 0, b["lay_w"], b["lay_h"]))
            ld = ImageDraw.Draw(lay)

            spec = getattr(self, "wave_spec", None)
            dt = getattr(self, "wave_dt", 0.08) or 0.08
            pos = self._db_now()
            if spec is not None and len(spec):
                # 帧间线性插值：刷新比频谱帧率慢，取整会一格一格跳，看着就是卡
                fpos = pos / dt
                i0 = int(fpos)
                wgt = fpos - i0
                i0 = min(max(i0, 0), len(spec) - 1)
                i1 = min(i0 + 1, len(spec) - 1)
                r0 = spec[i0]
                r1 = spec[i1]
                amps = [r0[i] * (1.0 - wgt) + r1[i] * wgt for i in range(n)]
            else:
                env = getattr(self, "wave_env", None) or []
                total = len(env) if env else 1
                amps = [env[min(total - 1, max(0, int((i + 1) * total / n) - 1))]
                        if env else 0.0 for i in range(n)]

            sm = b["smooth"]
            pk = b["peaks"]
            hold = b["hold"]
            for i in range(n):
                a = float(amps[i]) if i < len(amps) else 0.0
                sm[i] += (a - sm[i]) * (0.30 if a > sm[i] else 0.10)
                if sm[i] >= pk[i]:
                    pk[i] = sm[i]
                    hold[i] = 9                    # 峰值帽先停 9 帧再落
                else:
                    hold[i] = max(hold[i] - 1, 0)
                    if hold[i] <= 0:
                        pk[i] = max(pk[i] - 0.022, sm[i])

            try:
                if fx == 1:
                    self._fx_mirror(bg, d, b, sm, lay, ld, W, H, pos)
                elif fx == 2:
                    self._fx_radial(bg, d, b, sm, pk, lay, ld, W, H, pos)
                elif fx == 3:
                    self._fx_wave(bg, d, b, sm, lay, ld, W, H, pos)
                elif fx == 4:
                    self._fx_particles(bg, d, b, sm, lay, ld, W, H)
                elif fx == 5:
                    self._fx_tunnel(bg, d, b, sm, lay, ld, W, H, pos)
                elif fx == 6:
                    self._fx_terrain(bg, d, b, sm, lay, ld, W, H, pos)
                else:
                    self._fx_bars(bg, d, b, sm, pk, lay, ld, W, H, pos)
            except Exception:
                pass
            try:
                # 频谱条不需要额外发光层（柱体本身就是亮渐变），只有其它特效才合成，
                # 省掉整帧 resize + alpha 合成那 4ms，刷新才拉得回 30fps
                if fx:
                    big = lay.resize((W, H), Image.BILINEAR)
                    bg.paste(big, (0, 0), big)
                cv.tag_raise("fxbtn")
            except Exception:
                pass
            b["photo"].paste(bg)
            self._db_draw_prog((pos / self.db_dur) if self.db_dur else 0.0)
            try:
                cv.tag_raise("fxbtn")
                if is_fs and getattr(self, "db_fs_esc_id", None):
                    cv.tag_raise(self.db_fs_esc_id)   # 全屏提示别被波形盖住
            except Exception:
                pass

        def _fx_bars(self, bg, d, b, sm, pk, lay, ld, W, H, pos):
            """经典频谱条：竖直渐变柱 + 倒影 + 白色峰值帽 + 基线"""
            base = b["base"]
            maxh = b["maxh"]
            left = b["left"]
            slot = b["slot"]
            bw = b["bw"]
            crops = b["crops"]
            refc = b["refc"]
            x = left
            lx = b["lleft"]
            for i in range(len(sm)):
                bh = int(max(2, sm[i] * maxh))
                hq = bh - (bh & 3) or 3
                piece = crops.get(hq)
                if piece is None:               # 柱体贴图缓存，别每帧 produce 56 个新图
                    piece = crops[hq] = b["bar"].crop((0, maxh - hq, bw, maxh))
                bg.paste(piece, (int(x), base - hq))
                if bh > hq:                     # 量化掉的那几像素补一段同色亮头
                    d.rectangle([int(x), base - bh, int(x) + bw - 1, base - hq - 1],
                                fill=piece.getpixel((0, 0)))
                rh = int(bh * 0.45)
                if rh > 1:                      # 柱下倒影
                    rp = refc.get(rh)
                    if rp is None:
                        rp = refc[rh] = b["refl"].crop((0, 0, bw, rh))
                    bg.paste(rp, (int(x), base + 2), rp)
                if sm[i] > 0.02 and int(pk[i] * maxh) - bh < 46:
                    # 只画贴近柱顶的帽，跳转/静音时不飘一片白点
                    py = base - int(pk[i] * maxh) - int(H * 0.008)
                    d.rectangle([int(x), py, int(x) + bw - 1, py + 2],
                                fill=(255, 255, 255))
                x += slot
            d.rectangle([left, base + 1, int(W * 0.945), base + 1], fill=(38, 86, 116))

        def _fx_mirror(self, bg, d, b, sm, lay, ld, W, H, pos):
            """上下镜像：柱从中心线同时向上下长，中间压一条波形带"""
            cy = int(H * 0.5)
            maxh = int(H * 0.33)
            left = int(W * 0.05)
            right = int(W * 0.95)
            n = len(sm)
            slot = (right - left) / float(n)
            bw = max(2, int(slot * 0.82))
            spr = b["mb"]
            crops = b["crops"]
            for i in range(n):
                bh = int(max(2, sm[i] * maxh))
                hq = bh - (bh & 3) or 3
                key = ("m", hq)
                piece = crops.get(key)
                if piece is None:
                    top = spr.crop((0, maxh - hq, bw, maxh))
                    piece = crops[key] = (top, top.transpose(1))
                top, bot = piece
                x = int(left + i * slot + (slot - bw) * 0.5)
                bg.paste(top, (x, cy - hq))
                bg.paste(bot, (x, cy))
            # 中心波形 + 亮线
            pts = self._wave_points(pos, 180, 0.20, b=b)
            if pts:
                ld.line(pts, fill=(110, 230, 255, 110), width=2, joint="curve")
                ld.line(pts, fill=(240, 252, 255, 210), width=1, joint="curve")
            ld.line([(0, b["lay_h"] // 2), (b["lay_w"], b["lay_h"] // 2)],
                    fill=(150, 235, 255, 70), width=1)
            d.rectangle([left, cy - 1, right, cy + 1], fill=(52, 196, 232))

        def _fx_radial(self, bg, d, b, sm, pk, lay, ld, W, H, pos):
            """环形频谱：频带做成从内圈向外的扇柱，中心压暗成池"""
            n = len(sm)
            cx = W * 0.5
            cy = H * 0.53
            scale = min(W, H)
            r0 = scale * 0.132
            rmax = scale * 0.345
            rot = pos * 0.22
            half = math.pi / n * 0.66
            for i in range(n):
                v = float(sm[i])
                ang = -math.pi / 2 + 2 * math.pi * i / n + rot
                rr = r0 + max(scale * 0.006, min(0.86, v) * rmax)
                col = _pal(0.15 + 0.70 * min(1.0, v))
                ca, sa = math.cos(ang - half), math.sin(ang - half)
                cb, sb = math.cos(ang + half), math.sin(ang + half)
                d.polygon([(cx + ca * r0, cy + sa * r0),
                           (cx + ca * rr, cy + sa * rr),
                           (cx + cb * rr, cy + sb * rr),
                           (cx + cb * r0, cy + sb * r0)], fill=col)
            for i in range(n):                  # 峰值点，只画贴着柱顶的
                v = float(sm[i])
                q = float(pk[i])
                if q < 0.05 or (q - v) > 0.10:
                    continue
                ang = -math.pi / 2 + 2 * math.pi * i / n + rot
                rr = r0 + (q ** 0.75) * rmax + scale * 0.010
                px = cx + math.cos(ang) * rr
                py = cy + math.sin(ang) * rr
                d.ellipse([px - 2, py - 2, px + 2, py + 2], fill=(255, 255, 255))
            d.ellipse([cx - r0 * 0.99, cy - r0 * 0.99, cx + r0 * 0.99, cy + r0 * 0.99],
                      fill=(3, 4, 11))
            lvl = min(1.0, (sum(sm) / max(1, len(sm))) * 1.25)
            lcx = cx / 3.0
            lcy = cy / 3.0
            hr = r0 * 0.55 / 3.0
            ld.ellipse([lcx - hr, lcy - hr, lcx + hr, lcy + hr],
                       fill=(70, 190, 235, int(36 + 40 * lvl)))
            d.ellipse([cx - r0 * 0.042, cy - r0 * 0.042, cx + r0 * 0.042, cy + r0 * 0.042],
                      fill=(255, 255, 255))
            d.ellipse([cx - r0, cy - r0, cx + r0, cy + r0], outline=(72, 200, 230),
                      width=1)

        def _fx_wave(self, bg, d, b, sm, lay, ld, W, H, pos):
            """示波波形：网格 + 中心上下摆的亮线，带一点残影"""
            for k in range(1, 8):
                d.line([(0, H * k / 8.0), (W, H * k / 8.0)], fill=(18, 20, 28))
            for k in range(1, 12):
                d.line([(W * k / 12.0, 0), (W * k / 12.0, H)], fill=(18, 20, 28))
            cy = int(H * 0.5)
            d.line([(0, cy), (W, cy)], fill=(46, 60, 74))
            pts = self._wave_points(pos, 160, 0.17, b=b)
            for old in b["trail"][-2:]:
                if old:
                    ld.line(old, fill=(90, 200, 240, 46), width=1, joint="curve")
            if pts:
                ld.line(pts, fill=(110, 230, 255, 70), width=3, joint="curve")
                ld.line(pts, fill=(240, 252, 255, 225), width=1, joint="curve")
            b["trail"] = (b["trail"][-1:] + [pts])[-2:]

        def _hist_rows(self, pos, k_max):
            """取最近 k_max 帧频谱（新→旧截断后反过来），tunnel / terrain 用"""
            spec = getattr(self, "wave_spec", None)
            if spec is None or not len(spec):
                return []
            dt = getattr(self, "wave_dt", 0.08) or 0.08
            i = min(max(0, int(pos / dt)), len(spec) - 1)
            rows = []
            for k in range(k_max):
                j = i - k
                if j < 0:
                    break
                rows.append(spec[j])
            return rows[::-1]                   # 最早的在最前，画的时候由远及近

        def _ribbon(self, ld, xs, ys, thick, fill):
            """把折线做成等宽填充带（比一根根描线便宜，边缘也更像原程序）"""
            half = thick * 0.5
            pts = [(x, y - half) for x, y in zip(xs, ys)]
            pts += [(x, y + half) for x, y in zip(xs[::-1], ys[::-1])]
            ld.polygon(pts, fill=fill)

        def _fx_tunnel(self, bg, d, b, sm, lay, ld, W, H, pos):
            """频谱隧道：最近 18 帧频谱各占一个圆环，远处小而密、近处大而亮"""
            rows = self._hist_rows(pos, 18)
            if not rows:
                return
            lw, lh = b["lay_w"], b["lay_h"]
            n = 32
            step = max(1, len(sm) // n)
            cx, cy = lw * 0.5, lh * 0.5
            base = min(lw, lh)
            rmax = base * 0.42
            k_max = len(rows)
            for k in range(k_max):
                row0 = rows[k]
                vals = [float(row0[min(len(row0) - 1, i * step)]) for i in range(n)]
                vals = [(vals[(i - 1) % n] + 2.0 * vals[i] + vals[(i + 1) % n]) / 4.0
                        for i in range(n)]
                frac = k / max(1, k_max - 1)
                r = rmax * 0.10 * (10.0 ** frac)
                alpha = int(60 + 180 * frac ** 1.4)
                twist = pos * 0.9 + (1.0 - frac) * 0.9
                amp = r * (0.11 + 0.05 * frac)
                thick = max(1.0, lh * 0.0014 + r * 0.011)
                col = _pal(0.08 + 0.88 * frac)
                ro = []
                ri = []
                for i in range(n):
                    ang = -math.pi / 2 + 2 * math.pi * i / n + twist
                    rr = max(r + vals[i] * amp, thick * 0.5 + 1.0)
                    ca, sa = math.cos(ang), math.sin(ang)
                    ro.append((cx + ca * (rr + thick * 0.5), cy + sa * (rr + thick * 0.5)))
                    ri.append((cx + ca * max(rr - thick * 0.5, 1.0),
                               cy + sa * max(rr - thick * 0.5, 1.0)))
                fill = (col[0], col[1], col[2], alpha)
                for i in range(n):
                    j = (i + 1) % n
                    ld.polygon([ro[i], ro[j], ri[j], ri[i]], fill=fill)
            core = base * (0.05 + 0.05 * min(1.2, sum(sm) / max(1, len(sm))))
            lcx, lcy = lw * 0.5, lh * 0.5
            gs = int(core * 5.2)
            if gs > 4:
                g = _glow_sprite(gs, gs, (255, 255, 255), 200)
                lay.paste(g, (int(lcx - gs * 0.5), int(lcy - gs * 0.5)), g)
            hr = max(2, int(core * 0.75))
            ld.ellipse([lcx - hr, lcy - hr, lcx + hr, lcy + hr],
                       fill=(255, 255, 255, 240))

        def _fx_terrain(self, bg, d, b, sm, lay, ld, W, H, pos):
            """瀑布地形：22 层频谱叠成层叠山脊，越靠前越亮，后层被前层挡住"""
            rows = self._hist_rows(pos, 22)
            if len(rows) < 2:
                return
            lw, lh = b["lay_w"], b["lay_h"]
            n = min(32, len(sm))
            step = max(1, len(sm) // n)
            left, right = lw * 0.02, lw * 0.98
            top, bottom = lh * 0.22, lh * 0.96
            k_max = len(rows)
            step_y = (bottom - top) / max(1, k_max - 1)
            xs = [left + (right - left) * i / float(max(1, n - 1)) for i in range(n)]
            for idx in range(k_max):
                row = rows[idx]
                vals = [float(row[min(len(row) - 1, i * step)]) for i in range(n)]
                vals = [(vals[(i - 1) % n] + 2.0 * vals[i] + vals[(i + 1) % n]) / 4.0
                        for i in range(n)]
                frac = idx / max(1, k_max - 1)
                y0 = top + (bottom - top) * (frac ** 1.6)
                amp = step_y * (0.45 + 3.4 * frac)
                ys = [y0 - v * amp for v in vals]
                body = [(xs[0], y0 + step_y * 3.2)] + list(zip(xs, ys))
                body.append((xs[-1], y0 + step_y * 3.2))
                ld.polygon(body, fill=(int(7 + 9 * frac), int(9 + 11 * frac),
                                       int(17 + 23 * frac), 250))
                c1 = _pal(0.30 + 0.70 * frac)
                self._ribbon(ld, xs, [y + step_y * 0.30 for y in ys],
                             max(2.0, lh * 0.030 * (0.6 + 0.6 * frac)),
                             (c1[0], c1[1], c1[2], int(12 + 22 * frac)))
                c2 = _pal(0.32 + 0.68 * frac)
                self._ribbon(ld, xs, [y + step_y * 0.15 for y in ys],
                             max(1.5, lh * 0.014 * (0.6 + 0.6 * frac)),
                             (c2[0], c2[1], c2[2], int(20 + 30 * frac)))
                c3 = _pal(0.25 + 0.75 * frac)
                self._ribbon(ld, xs, ys,
                             max(1.0, lh * 0.0026 * (0.7 + 0.8 * frac)),
                             (c3[0], c3[1], c3[2], int(60 + 185 * frac)))

        def _fx_particles(self, bg, d, b, sm, lay, ld, W, H):
            """频带粒子：底部频带指示灯 + 能量越大喷得越密的火星"""
            n = len(sm)
            left = int(W * 0.04)
            right = int(W * 0.96)
            base = int(H * 0.90)
            unit = (right - left) / float(n)
            for i in range(n):
                v = float(sm[i])
                if v < 0.03:
                    continue
                bw2 = max(2, int(unit * 0.52))
                x = int(left + i * unit + (unit - bw2) * 0.5)
                bh = int(max(2, v * H * 0.14))
                d.rectangle([x, base - bh, x + bw2 - 1, base],
                            fill=_pal(0.24 + 0.62 * (i / float(max(1, n - 1)))))
            parts = b["parts"]
            # 每条频带各自限流，并按随机起点轮询：否则池子被前几根柱塞满，
            # 粒子会越堆越挤在左边（之前"播放一会全跑到左上角"就是这么来的）
            per_cap = 6
            band_cnt = {}
            for part in parts:
                bi = part[7] if len(part) > 7 else -1
                band_cnt[bi] = band_cnt.get(bi, 0) + 1
            start = random.randrange(n)
            for k in range(n):
                i = (start + k) % n
                v = float(sm[i])
                if v <= 0.02:
                    if random.random() < 0.35:
                        cnt = 1
                    else:
                        continue
                else:
                    cnt = 1 + int(v * 3.5)
                room = per_cap - band_cnt.get(i, 0)
                if room <= 0:
                    continue
                cnt = min(cnt, room)
                for _ in range(cnt):
                    if len(parts) >= 6 * n:
                        break
                    parts.append([left + i * unit + random.uniform(0.1, 0.9) * unit,
                                  base - H * 0.012,
                                  random.uniform(-1, 1) * W * 0.0009,
                                  -H * (0.0034 + 0.0070 * v) * random.uniform(0.55, 1.5),
                                  random.uniform(0.75, 1.0),
                                  (1.1 + 3.4 * v) * max(0.9, H / 900.0),
                                  max(0.0, min(1.0, i / float(max(1, n - 1)))),
                                  i])
                band_cnt[i] = band_cnt.get(i, 0) + cnt
            alive = []
            for part in parts:
                x, y, vx, vy, life = part[0], part[1], part[2], part[3], part[4]
                vy *= 0.995
                vx = vx * 0.99 + random.uniform(-1, 1) * W * 0.00018
                x += vx
                y += vy
                life -= 1.0 / 140.0
                if life <= 0 or y < -H * 0.05:
                    continue
                part[0], part[1], part[2], part[3], part[4] = x, y, vx, vy, life
                alive.append(part)
            b["parts"] = alive
            # 粒子：亮核画在清晰层（1px 白点放大 3 倍会被插值洗淡），拖尾留在发光层
            for part in alive:
                x, y, vx, vy, life, size, ct = part[0], part[1], part[2], part[3], part[4], part[5], part[6]
                lx = int(x / 3.0)
                ly = int(y / 3.0)
                ss = max(1, int(size * 0.55))
                tail = max(ss, int(abs(vy) / 5.0))
                col = _pal(min(1.0, ct + 0.12))
                ld.rectangle([lx, ly, lx + ss, ly + tail],
                             fill=(col[0], col[1], col[2],
                                   int(120 * min(1.0, life * 1.6))))
                px = int(x)
                py = int(y)
                d.rectangle([px, py, px + ss, py + ss],
                            fill=(255, 255, 255))

        def _db_show_video_panel(self, item):
            """右侧：封面/首帧 + 控制条信息"""
            self.db_frame = None
            self.db_poster = None
            self._db_tip("（无画面）" if item else "选中后显示画面")
            self._db_redraw()
            if not item:
                self._db_set_video_author(None)
                self._db_set_video_caption("", show=False)
                self._db_enable_ctl(False)
                self.db_hint.configure(text="选中列表里的文件即可播放")
                self.db_time.configure(text="00:00 / 00:00")
                self.db_play_btn.set_sym("play")
                self._db_draw_prog(0.0)
                return
            self._db_enable_ctl(True)
            self.db_play_btn.set_sym("play")
            if (item.get("kind") or "video") != "video":
                self.db_hint.configure(text="这个是音频文件")
                self._db_show_audio_pic(item)
                return
            self.db_dur = 0.0
            try:
                # 时长优先用打开时已经算好的 facts，省掉又一次 ffprobe
                d = item.get("facts") or {}
                self.db_dur = float(d.get("dur") or 0)
                if not self.db_dur:
                    info = probe(ffprobe, item["path"])
                    self.db_dur = float((info.get("format") or {}).get("duration") or 0)
                self._db_set_time(0.0)
            except Exception:
                pass
            self.db_hint.configure(text="%s · 右键切 填满/适应"
                                   % os.path.basename(item["path"]))
            pic = item.get("cover")
            vid = item.get("vid")
            if not pic and vid:
                c = os.path.join(os.path.dirname(os.path.dirname(item["path"])),
                                 "封面", vid + ".jpg")
                pic = c if os.path.isfile(c) else None
            if not pic:
                pic = self._thumb_hit(item["path"])
                if pic is None:
                    # 抽首帧要起 ffmpeg，慢的时候一两秒；绝不能挡在开播前面
                    self._db_tip("（正在取首帧…）")
                    self._db_async_thumb(item)
                    return
            if pic and os.path.isfile(pic):
                try:
                    im = Image.open(pic).convert("RGB")
                    self.db_frame = im
                    self.db_poster = im          # 停止播放时回到这张截图
                    self._db_redraw()
                    return
                except Exception:
                    pass
            self._db_tip("（正在取首帧…）")

        def _thumb_hit(self, path):
            """只在缓存里找现成的缩略图；没有就返回 None，现场不抽帧"""
            try:
                out = os.path.join(self._thumb_cache(), _cache_name(path, "jpg"))
                if os.path.isfile(out) and os.path.getsize(out) > 0:
                    return out
            except Exception:
                pass
            return None

        def _db_async_thumb(self, item):
            """后台抽首帧，抽好了再补画成海报；这期间播放照常，界面不等它。
            同一时刻只跑一个抽帧：抢 GIL 会让下一次开播也跟着变慢。"""
            global _THUMB_BUSY
            if _THUMB_BUSY:
                return
            _THUMB_BUSY = True
            path = item["path"]
            cache = self._thumb_cache()

            def work():
                global _THUMB_BUSY
                try:
                    pic = _extract_frame(ffmpeg, path, cache)
                except Exception:
                    pic = None
                finally:
                    _THUMB_BUSY = False
                try:
                    self.after(0, lambda: self._db_thumb_done(path, pic))
                except Exception:
                    pass

            threading.Thread(target=work, daemon=True).start()

        def _db_thumb_done(self, path, pic):
            """抽回来时：还停在同一条、且没在播，才把这张图当海报画上去"""
            try:
                if (getattr(self, "db_cur_video", None) or {}).get("path") != path:
                    return
                if self.db_state == "playing" or getattr(self, "wave_mode", False):
                    return
                if not pic or not os.path.isfile(pic):
                    return
                im = Image.open(pic).convert("RGB")
                self.db_frame = im
                self.db_poster = im
                self._db_redraw()
            except Exception:
                pass

        def _db_show_audio_pic(self, item):
            pic = _extract_cover(ffmpeg, item["path"], self._thumb_cache())
            if pic and os.path.isfile(pic):
                try:
                    im = Image.open(pic).convert("RGB")
                    im.thumbnail((240, 240), Image.LANCZOS)
                    self.db_frame = im
                    self._db_redraw()
                    return
                except Exception:
                    pass
            self._db_tip("（无封面）")

        def _db_tip(self, text):
            v = self._pv()
            if not v:
                return
            try:
                v["cv"].itemconfig(v["tip"], text=text, state="normal")
                v["cv"].itemconfig(v["img"], image="")
            except Exception:
                pass

        # ---------------- 进度条（画在画面底部，细轨道 + 小圆点）
        def _db_prog_geom(self, cv):
            try:
                W = max(2, cv.winfo_width())
                H = max(2, cv.winfo_height())
            except Exception:
                W, H = 560, 315
            return 16, W - 16, H - 11

        def _db_draw_prog(self, frac):
            frac = min(1.0, max(0.0, float(frac or 0.0)))
            for cv, tid, fid, did in list(getattr(self, "_db_prog_sets", [])):
                if cv is None:
                    continue
                try:
                    if not cv.winfo_exists():
                        continue
                    x0, x1, y = self._db_prog_geom(cv)
                    cv.coords(tid, x0, y, x1, y)
                    px = x0 + (x1 - x0) * frac
                    cv.coords(fid, x0, y, max(x0, px), y)
                    cv.coords(did, px - 4, y - 4, px + 4, y + 4)
                    cv.tag_raise(tid)       # 轨道也要抬到波形图上面，否则被盖住看不见
                    cv.tag_raise(fid)
                    cv.tag_raise(did)
                except Exception:
                    pass

        def _db_on_canvas_conf(self, e=None):
            v = self._pv()
            cv = getattr(e, "widget", None) if e is not None else None
            if v and cv is not None and v["cv"] is not cv:
                # 别的页的画布变了：找到它自己的视图再居中提示
                for vv in getattr(self, "views", {}).values():
                    if vv["cv"] is cv:
                        v = vv
                        break
            if v:
                try:
                    v["cv"].coords(v["tip"], v["cv"].winfo_width() // 2,
                                  v["cv"].winfo_height() // 2)
                except Exception:
                    pass
            self._db_redraw()

        def _db_prog_frac(self, x):
            v = self._pv()
            x0, x1, y = self._db_prog_geom(v["cv"] if v else None)
            return min(1.0, max(0.0, (x - x0) / float(max(1, x1 - x0))))

        def _db_prog_hit(self, e):
            """只有点到底部那条进度带（含小圆点周边）才算拖进度，点画面别跳"""
            cv = getattr(e, "widget", None)
            v = self._pv()
            if cv is None and v:
                cv = v["cv"]
            try:
                W = max(2, cv.winfo_width())
                H = max(2, cv.winfo_height())
            except Exception:
                return False
            if abs(e.y - (H - 11)) > 9:
                return False
            # 右下角那个特效按钮自己占一块（按钮底在 H-26），这块不算进度
            if getattr(self, "wave_mode", False):
                bw_x1, bw_y1 = W - 10, H - 26
                bw_x0, bw_y0 = bw_x1 - 92, bw_y1 - 24
                if (bw_x0 - 6 <= e.x <= bw_x1 + 6
                        and bw_y0 - 6 <= e.y <= bw_y1 + 6):
                    return False
            return True

        def _db_prog_press(self, e):
            if not getattr(self, "db_cur_video", None) or not self.db_dur:
                return
            if not self._db_prog_hit(e):
                return
            self.db_drag = True
            self._db_preview_seek(self._db_prog_frac(e.x))

        def _db_prog_move(self, e):
            if not getattr(self, "db_drag", False):
                return
            self._db_preview_seek(self._db_prog_frac(e.x))

        def _db_prog_release(self, e):
            if not getattr(self, "db_drag", False):
                return
            self.db_drag = False
            if not self.db_dur:
                return
            self._db_seek_frac(self._db_prog_frac(e.x))

        def _db_seek_frac(self, frac):
            """按比例跳转并续播（窗口内进度条与全屏 OpenCV 窗口共用）"""
            if not self.db_dur:
                return
            pos = min(1.0, max(0.0, float(frac))) * self.db_dur
            was = self.db_state
            item = getattr(self, "db_cur_video", None)
            # 只收掉播放器，别走 db_stop——它会切回作者面板、还要重画一遍 logo，
            # 点一下进度条就成了「停播 + 闪一下」；quick=True 不等着 ffmpeg 收尾
            self._db_stop_player_only(quick=True)
            self._db_stop_wave()
            self.db_state = "idle"
            self.db_seek_base = pos
            self.db_t0 = 0.0
            self.db_pos = pos
            self._db_set_time(pos)
            if was == "playing" and item:
                kind = item.get("kind")

                def go():
                    try:
                        if kind == "audio":
                            self._db_audio_only_play(pos)
                        else:
                            self.db_play_from(pos)
                    except Exception:
                        pass

                # 先让进度点/时间刷出去，再起解码器，点击不会卡住界面
                try:
                    self.after(1, go)
                except Exception:
                    go()

        def _db_preview_seek(self, frac):
            pos = frac * (self.db_dur or 0.0)
            self._db_draw_prog(frac)
            self.db_time.configure(text="%s / %s" % (_fmt_time(pos),
                                                     _fmt_time(self.db_dur)))

        def _db_frame_size(self):
            """画面显示区尺寸（跟随右侧分栏宽度，保证横屏）"""
            v = self._pv()
            try:
                cv = v["cv"]
                ww = max(320, cv.winfo_width())
                hh = max(180, cv.winfo_height())
            except Exception:
                ww, hh = 560, 315
            return ww, hh

        def _fit_img(self, im, w, h, fast=False):
            """contain：整幅等比缩小（可能留边）；cover：填满并裁掉边缘（无黑边）

            选了固定画面比例时，先按比例算出"盒子"，再把画面装进这个盒子；
            盒子比画面区小，多余的地方自然就是黑边。
            fast=True 用双线性插值：全屏每帧都在算，LANCZOS 太慢会掉帧。
            """
            w, h = max(2, int(w)), max(2, int(h))
            asp = float(getattr(self, "db_aspect", 0.0) or 0.0)
            bw, bh = w, h
            if asp > 0.01:
                if (w / float(h)) > asp:            # 画面区比目标宽 → 以高为准
                    bw = max(2, int(h * asp))
                else:                               # 画面区比目标高 → 以宽为准
                    bh = max(2, int(w / asp))
            if getattr(self, "db_fit", "cover") == "cover":
                try:
                    return ImageOps.fit(
                        im, (bw, bh),
                        Image.BILINEAR if fast else Image.LANCZOS,
                        centering=(0.5, 0.5))
                except Exception:
                    pass
            r = min(bw / float(im.width), bh / float(im.height))
            tw, th = max(2, int(im.width * r)), max(2, int(im.height * r))
            return im if (tw, th) == im.size else im.resize((tw, th), Image.BILINEAR)

        def db_toggle_fit(self):
            """切换：适合窗口（完整显示，可留黑边） / 裁切（填满，边缘裁掉）"""
            self.db_fit = "contain" if getattr(self, "db_fit", "cover") == "cover" \
                else "cover"
            self.cfg["fit_mode"] = self.db_fit
            save_config(self.cfg)
            try:
                self.db_fit_btn.set_sym("fit" if self.db_fit == "contain" else "crop")
            except Exception:
                pass
            self._db_replay_fs()          # 全屏时画面模式变了，按新规格重新出帧
            self._db_redraw()
            self._pb_tip("画面：%s" % ("适合窗口（完整显示，可能留黑边）"
                                     if self.db_fit == "contain"
                                     else "裁切（填满画面，边缘会裁掉一点）"))

        def _db_redraw(self):
            v = self._pv()
            if not v:
                return
            w = getattr(self, "fs_win", None)
            if w is not None:               # OpenCV 全屏窗在显示，页面画布看不见就别画了
                self._fs_push(win=w)
                return
            cv = v["cv"]
            try:
                cv.coords(v["tip"], cv.winfo_width() // 2,
                          cv.winfo_height() // 2)
            except Exception:
                pass
            frac = (self.db_pos / self.db_dur) if self.db_dur else 0.0
            self._db_draw_prog(frac)
            if getattr(self, "wave_mode", False) and not self.db_frame:
                self._db_wave_draw()        # 音乐：画面位置画波形，不画图
                return
            if self.db_frame is None:
                self._db_show_logo()        # 空闲/停止：画面区直接显示 logo
                return
            ww, hh = self._db_frame_size()
            im = self.db_frame
            fs = self.db_fs
            fs_live = False
            if fs is not None:
                try:
                    fs_live = (fs.winfo_exists() and fs.winfo_width() > 2
                               and fs.winfo_height() > 2)
                except Exception:
                    fs_live = False
            # 全屏开着时页面那块已经看不见：跳过它，每帧省一份缩放 + 位图传输
            if not fs_live:
                try:
                    im2 = self._fit_img(im, ww, hh)
                    photo = ImageTk.PhotoImage(im2)
                    cv.itemconfig(v["img"], image=photo, state="normal")
                    cv.coords(v["img"], ww // 2, hh // 2)
                    cv.image = photo
                    cv.itemconfig(v["wave"], state="hidden")
                    cv.itemconfig(v["tip"], state="hidden")
                    cv.tag_lower(v["img"])
                    cv.tag_lower(v["tip"])
                except Exception:
                    return
            # 全屏那张也同步
            if fs_live:
                try:
                    sw, sh = fs.winfo_width(), fs.winfo_height()
                    # fast=True：全屏每帧都要放大到整屏，LANCZOS 会掉到二十几帧
                    big = self._fit_img(im, sw, sh, fast=True)
                    ph = ImageTk.PhotoImage(big)
                    if getattr(self, "_db_fs_sz", None) != (sw, sh):
                        self._db_fs_sz = (sw, sh)
                        fs.coords(self.db_fs_img_id, sw // 2, sh // 2)
                        fs.itemconfig(self.db_fs_img_id, state="normal")
                    fs.itemconfig(self.db_fs_img_id, image=ph)
                    fs.image = ph
                    self._db_draw_prog(frac)
                except Exception:
                    pass

        def _db_pump(self):
            self.db_pump_id = None
            if self.db_state != "playing" or not self.db_player:
                return
            data = None
            try:
                # 追帧：渲染跟不上时把积压的旧帧全丢掉，只画最新的一帧，
                # 否则队列里的旧画面会让全屏看起来"卡 + 慢半拍"
                while True:
                    data = self.db_player.frames.get_nowait()
            except pyqueue.Empty:
                pass
            if data:
                if getattr(self, "fs_win", None) is not None:
                    # 全屏：原始字节直送 cv2 窗口，PIL 和页面画布都不用碰
                    self._fs_push(raw=data)
                else:
                    try:
                        # frombuffer 不拷贝像素，省掉每帧几毫秒
                        try:
                            self.db_frame = Image.frombuffer(
                                "RGB", (self.db_player.frame_w, self.db_player.frame_h),
                                data, "raw", "RGB", 0, 1)
                        except Exception:
                            self.db_frame = Image.frombytes(
                                "RGB", (self.db_player.frame_w, self.db_player.frame_h), data)
                        self._db_redraw()
                    except Exception:
                        pass
            elif self.db_player and self.db_player.eof:
                self._db_stop_player_only()
                self.db_state = "idle"
                self._db_on_end()
                return
            iv = int(1000.0 / max(8.0, self.db_player.fps * self.db_player.speed))
            fs_live = getattr(self, "fs_win", None) is not None
            if not fs_live and self.db_fs is not None:
                try:
                    fs_live = (self.db_fs.winfo_exists()
                               and self.db_fs.winfo_width() > 2)
                except Exception:
                    fs_live = False
            # 全屏：显示窗口自己控帧率，这里只管尽快把新帧递过去；窗口内照旧追帧
            self.db_pump_id = self.after(max(8, iv // 2) if fs_live else max(15, iv // 2),
                                         self._db_pump)

        def _db_set_time(self, pos):
            self.db_pos = max(0.0, min(pos, self.db_dur or pos))
            if self.db_dur > 0:
                self._db_draw_prog(self.db_pos / self.db_dur)
            else:
                self._db_draw_prog(0.0)
            self.db_time.configure(text="%s / %s" % (_fmt_time(self.db_pos),
                                                     _fmt_time(self.db_dur)))

        def _db_tick(self):
            self.db_tick_id = None
            if self.db_state != "playing" or not self.db_t0:
                return
            # 手指还按在进度条上时不要用真实播放位置把圆点拽回去
            if not getattr(self, "db_drag", False):
                pos = self.db_seek_base + (time.time() - self.db_t0) * self.db_speed
                if self.db_dur and pos >= self.db_dur:
                    self._db_set_time(self.db_dur)
                    self._db_stop_player_only()
                    self.db_state = "idle"
                    self._db_on_end()
                    return
                self._db_set_time(pos)
            self.db_tick_id = self.after(300, self._db_tick)

        def _db_now(self):
            """真正的播放位置（db_pos 只在 tick 里刷新，最多旧 300ms）"""
            if self.db_state == "playing" and self.db_t0:
                pos = self.db_seek_base + (time.time() - self.db_t0) * self.db_speed
                if self.db_dur:
                    pos = min(pos, self.db_dur)
                return max(0.0, pos)
            return self.db_pos

        # ---------- 一条播完之后：按循环三态 + 随机决定下一个
        def _db_on_end(self):
            items = getattr(self, "media_list", None) or []
            idx = getattr(self, "media_idx", 0)
            n = len(items)
            if n <= 1:
                if self.loop_mode == 0 and n == 1:
                    self.db_hint.configure(text="单条循环：重播")
                    self._db_open(items[0], 0)
                    return
                self.db_hint.configure(text="播放结束")
                self._db_after_end()
                return
            if self.shuffle:
                self._shuf_done.add(idx)
                rest = [i for i in range(n) if i not in self._shuf_done]
                if not rest:
                    if self.loop_mode == 2:      # 列表循环 + 随机：一直随机下去
                        self._shuf_done = set()
                        rest = [i for i in range(n) if i != idx]
                    else:
                        self.db_hint.configure(text="随机播放已播完全部 %d 条，停止" % n)
                        self._db_after_end()
                        return
                # 优先换到别的作者的视频
                ca = items[idx].get("author") if idx < n else None
                other = [k for k in rest
                         if items[k].get("author") not in (None, ca)]
                nxt = random.choice(other or rest)
                self.db_hint.configure(text="随机播放下一条")
                self._db_open(items[nxt], nxt)
                return
            if self.loop_mode == 0:              # 单条循环
                self._db_open(items[idx], idx)
                return
            nxt = idx + 1
            if nxt >= n:
                if self.loop_mode == 2:          # 列表循环：回到第一条
                    nxt = 0
                else:                            # 顺序播放：播完停止
                    self.db_hint.configure(text="顺序播放完毕")
                    self._db_after_end()
                    return
            self._db_open(items[nxt], nxt)

        def _db_after_end(self):
            """真的不播了：回到待播状态，画面停在最后一帧/封面"""
            try:
                self.db_play_btn.set_sym("play")
                self._db_set_time(self.db_dur)
            except Exception:
                pass
            self._db_stop_wave()
            self.db_state = "idle"
            try:
                self.db_stop_btn.set_enabled(False)
                if self._db_rest_author_view():
                    self.db_hint.configure(text="播放结束 · 按播放从当前作者继续")
            except Exception:
                pass

        def _db_audio_start(self, pos, keep_old=False):
            prev = None
            if keep_old:
                # 切倍速：旧那路先留着出声，等新的接上再收，听感上不断音
                prev = getattr(self, "db_audio_proc", None)
                self.db_audio_proc = None
                try:
                    a = getattr(self, "_db_audio_apply_id", None)
                    if a:
                        self.after_cancel(a)
                        self._db_audio_apply_id = None
                except Exception:
                    pass
            else:
                self._db_audio_stop(quick=True)
            ffplay = find_tool("ffplay")
            if not ffplay or not getattr(self, "db_cur_video", None):
                return
            # 音量走 Windows 会话（100 是会话内的满值），会话可用时不再靠命令行音量
            use_sess = getattr(self, "_db_use_session", True)
            cmd = [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
                   "-volume", "100" if use_sess else str(int(self.db_vol))]
            if abs(self.db_speed - 1.0) > 0.01:
                cmd += ["-af", _atempo_chain(self.db_speed)]
            if pos > 0.02:
                cmd += ["-ss", "%.3f" % pos]
            cmd += [self.db_cur_video["path"]]
            env = dict(os.environ)
            env["SDL_VIDEODRIVER"] = "dummy"
            env["SDL_AUDIODRIVER"] = "directsound"
            # ffplay 是个 100 MB 的独立程序，实测冷启动要 2 秒多。
            # 这一段绝不能压在主线程上，否则画面得等声音就绪才出来。
            self._db_audio_token = getattr(self, "_db_audio_token", 0) + 1
            token = self._db_audio_token

            def work():
                proc = None
                try:
                    proc = reg_proc(subprocess.Popen(
                        cmd, env=env, creationflags=no_window(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                except Exception:
                    proc = None

                def back():
                    self._db_audio_spawned(token, proc, use_sess, prev)

                try:
                    self.after(0, back)
                except Exception:
                    if proc is not None:
                        try:
                            _terminate_proc(proc)
                        except Exception:
                            pass

            threading.Thread(target=work, daemon=True).start()

        def _db_audio_spawned(self, token, proc, use_sess, prev):
            """ffplay 起来了；这期间要是已经切到别的条目，就直接把它收掉"""
            if token != getattr(self, "_db_audio_token", 0):
                if proc is not None:
                    try:
                        _terminate_proc(proc)
                    except Exception:
                        pass
                return
            self.db_audio_proc = proc
            if proc is not None and use_sess:
                try:
                    self._db_audio_apply_id = self.after(220, self._db_audio_apply)
                except Exception:
                    self._db_audio_apply_id = None
            if prev is not None:
                # 新的一路已经起来了，旧那路再收：重叠几十毫秒，听着不断音
                try:
                    self.after(60, lambda: _terminate_proc(prev))
                except Exception:
                    _terminate_proc(prev)

        def _db_audio_apply(self):
            """把音量挂到 ffplay 的音频会话上；挂不上就退回命令行音量"""
            self._db_audio_apply_id = None
            p = getattr(self, "db_audio_proc", None)
            if p is None or p.poll() is not None:
                return
            if WINVOL.bind(p.pid):
                WINVOL.set_volume(self.db_vol / 100.0)
                WINVOL.set_mute(self.db_muted or self.db_vol <= 0)
                return
            # 找不到会话（系统限制）：标记降级，用命令行音量重起一次
            if self.db_state == "playing" and not getattr(self, "_db_sess_failed", False):
                self._db_sess_failed = True
                self._db_use_session = False
                self._db_audio_start(self._db_now())

        def _db_audio_stop(self, quick=False):
            # 让还在后台启动的那一路 ffplay 启完发现令牌过期，自己收掉
            self._db_audio_token = getattr(self, "_db_audio_token", 0) + 1
            p = getattr(self, "db_audio_proc", None)
            self.db_audio_proc = None
            try:
                a = getattr(self, "_db_audio_apply_id", None)
                if a:
                    self.after_cancel(a)
                    self._db_audio_apply_id = None
            except Exception:
                pass
            try:
                if p is not None and WINVOL.pid == p.pid:
                    WINVOL.forget()      # 只清掉自己的会话，别误伤另一页的音频
            except Exception:
                pass
            if p is None:
                return
            try:
                if p.poll() is None:
                    if quick:               # 拖进度条这种：不等着收尾，直接收进程
                        p.kill()
                    else:
                        p.terminate()
                        try:
                            p.wait(timeout=0.6)
                        except Exception:
                            p.kill()
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

        def db_play_toggle(self):
            if self.db_state == "playing":
                self.db_pause()
                return
            if self.db_state == "paused":
                self.db_resume()
                return
            # 作者列表界面：永远从「当前选中的作者」起播，不受上一轮残留的文件影响
            if (getattr(self, "page", "") == "db" and self.db_view == "authors"
                    and getattr(self, "db_authors", None)):
                self.db_play_authors()
                return
            item = getattr(self, "db_cur_video", None)
            if not item:
                self.db_hint.configure(text="先选一条视频或音乐")
                return
            if item.get("kind") == "audio":
                self._db_audio_only_play(self.db_pos if self.db_pos > 0.5 else 0.0)
                return
            self.db_play_from(self.db_pos if self.db_pos > 0.5 else 0.0)

        def db_play_from(self, pos=0.0, keep_audio=False):
            item = getattr(self, "db_cur_video", None)
            if not item or not ffmpeg:
                self.db_hint.configure(text="缺少 ffmpeg，无法播放")
                return
            if item.get("kind") == "audio":
                self._db_audio_only_play(pos)
                return
            if (self.db_view == "authors"
                    and not getattr(self, "db_play_panel", False)):
                self.db_play_panel = True        # 作者视图下开播：右侧换成播放界面
                self._db_apply_view()
            if not keep_audio:
                try:                                # 先给个反馈，别让界面看着像没反应过来
                    self.db_hint.configure(text="正在打开…")
                    self.db_hint.update_idletasks()
                except Exception:
                    pass
            old = getattr(self, "db_player", None)
            # 切视频时旧画面本来就要丢掉，别在这儿等它优雅收尾
            self._db_stop_player_only(quick=True, keep_audio=keep_audio)
            self.wave_mode = False
            # 同一个文件接着换位置：复用上一次的帧率/尺寸探测，省掉两次阻塞的 ffprobe
            reuse = old if (old is not None and getattr(old, "src", None) == item["path"]
                            ) else None
            fs_size, fs_box, fs_fit = self._fs_out()
            kf = (item.get("facts") or {})
            known = (kf.get("fps"), kf.get("w"), kf.get("h"))
            self.db_player = MediaPlayer(ffmpeg, ffprobe, item["path"],
                                        start=pos, speed=self.db_speed,
                                        probe_from=reuse,
                                        known_meta=known,
                                        out_size=fs_size, fs_box=fs_box,
                                        fs_fit=fs_fit)
            self.db_player.start()
            self.db_state = "playing"
            self.db_play_btn.set_sym("pause")
            self.db_play_btn.set_enabled(True)
            self.db_stop_btn.set_enabled(True)
            self.db_seek_base = pos
            self.db_t0 = time.time()
            self._db_set_time(pos)
            if abs(self.db_speed - 1.0) > 0.01:
                self.db_hint.configure(
                    text="%s 倍速播放中（含音频）" % self._sp_label(self.db_speed))
            else:
                self.db_hint.configure(text="正在播放（含音频）")
            if not keep_audio:
                self._db_audio_start(pos)
            self._db_pump()
            if not self.db_tick_id:
                self._db_tick()

        def db_pause(self):
            if self.db_state != "playing":
                return
            self.db_state = "paused"
            pos = self.db_seek_base + (time.time() - self.db_t0) * self.db_speed
            self.db_pos = pos
            self._db_stop_player_only(quick=True)
            self._db_stop_wave()
            self.db_play_btn.set_sym("play")
            self.db_hint.configure(text="已暂停")
            self._db_set_time(pos)

        def db_resume(self):
            if self.db_state != "paused":
                return
            if (getattr(self, "db_cur_video", None) or {}).get("kind") == "audio":
                self._db_audio_only_play(self.db_pos)
            else:
                self.db_play_from(self.db_pos)

        def _db_stop_player_only(self, quick=False, keep_audio=False):
            pend = getattr(self, "_speed_pend", None)
            if pend:                            # 倍速切换还没落地就把新解码器收掉
                self._speed_pend = None
                try:
                    pend[0].stop(quick=quick)
                except Exception:
                    pass
            if getattr(self, "db_pump_id", None):
                try:
                    self.after_cancel(self.db_pump_id)
                except Exception:
                    pass
                self.db_pump_id = None
            if getattr(self, "db_player", None):
                self.db_player.stop(quick=quick)
            self.db_player = None
            if not keep_audio:                  # 全屏切换只换视频解码器，声音别断
                self._db_audio_stop(quick=quick)

        def db_stop(self):
            if getattr(self, "fs_win", None) is not None:
                self.db_exit_fs()
            self._db_stop_player_only()
            self._db_stop_wave()
            self.db_state = "idle"
            self.db_seek_base = 0.0
            self.db_t0 = 0.0
            self._db_set_time(0.0)
            try:
                self.db_play_btn.set_sym("play")
                if (getattr(self, "page", "") == "db"
                        and getattr(self, "db_view", "") == "authors"):
                    # 作者列表界面：停下来就回到头像与信息，别留着上一首当"当前文件"
                    self._db_rest_author_view()
                    self.db_hint.configure(text="已停止 · 按播放从所选作者起播")
                    self.db_play_btn.set_enabled(True)
                    return
                item = getattr(self, "db_cur_video", None)
                on = bool(item)
                self.db_play_btn.set_enabled(on)
                self.db_stop_btn.set_enabled(on)
                if on:
                    kind = item.get("kind")
                    if kind == "audio":
                        self.db_hint.configure(text="已停止（音乐）")
                        self._db_redraw()
                    else:
                        self.db_hint.configure(text="已停止 · 双击列表可继续播放")
                        self._db_show_logo()
            except Exception:
                pass

        def _logo_cur(self):
            """当前 logo 图的绝对路径（配置没配好就退回 logo 文件夹第一张）"""
            return pick_logo_path(getattr(self, "cfg", None))

        def _db_logo_paint(self, v, with_prog=False):
            """把当前 logo 画到指定页的画面 canvas 上（不依赖当前页）"""
            if not v:
                return False
            cv = v["cv"]
            try:
                ww, hh = cv.winfo_width(), cv.winfo_height()
            except Exception:
                return False
            if ww < 2 or hh < 2:            # 这页还没显示出来：等它的 Configure 再画
                return False
            try:
                side = int(max(140, min(ww, hh) * 0.82))
                photo = None
                path = self._logo_cur()
                if path:
                    photo = logo_file_photo(path, side, ImageTk)
                if photo is None:
                    photo = logo_photo(side, ImageTk)
                if photo is None:
                    return False
                cv.itemconfig(v["img"], image=photo, state="normal")
                cv.coords(v["img"], ww // 2, hh // 2)
                cv.image = photo
                v["logo_path"] = path
                v["logo_side"] = side
                cv.itemconfig(v["wave"], state="hidden")
                cv.itemconfig(v["tip"], text="")
                cv.tag_lower(v["img"])
                cv.tag_lower(v["tip"])
                if with_prog:
                    self._db_draw_prog(0.0)
                return True
            except Exception:
                return False

        def _db_repaint_logos(self):
            """三页的画面都按当前 logo 重画一遍：设置里换图后立刻全同步"""
            page = getattr(self, "page", "")
            ok_cur = self._db_logo_paint(self._pv(), with_prog=(page != "game"))
            for pg, v in (getattr(self, "views", {}) or {}).items():
                if pg == page:
                    continue
                self._db_logo_paint(v, with_prog=False)
            try:
                self._db_logo_fs()
            except Exception:
                pass
            return ok_cur

        def _db_show_logo(self):
            """停止 / 空闲时的画面：显示 logo 图（配不上就用内置画的图形）"""
            try:
                self.db_frame = None         # 画面区当前没有视频帧
                self._db_logo_paint(self._pv(), with_prog=True)
                self._db_logo_fs()
            except Exception:
                pass

        def _db_logo_fs(self):
            """全屏那张也换成 logo（停止时若还开着全屏）"""
            fs = getattr(self, "db_fs", None)
            if fs is None:
                return
            try:
                if not fs.winfo_exists():
                    return
                sw, sh = fs.winfo_width(), fs.winfo_height()
                if sw < 2 or sh < 2:
                    return
                side = int(max(160, min(sw, sh) * 0.72))
                ph = None
                path = self._logo_cur()
                if path:
                    ph = logo_file_photo(path, side, ImageTk)
                if ph is None:
                    ph = logo_photo(side, ImageTk)
                if ph:
                    fs.itemconfig(self.db_fs_img_id, image=ph, state="normal")
                    fs.coords(self.db_fs_img_id, sw // 2, sh // 2)
                    fs.image = ph
            except Exception:
                pass

        # ---------- 倍速：点按钮弹菜单选，不再循环切换
        SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)

        def _sp_label(self, s):
            """1.0x / 1.25x / 2.0x —— 整数档也留一位小数，去掉多余的零"""
            txt = ("%.2f" % float(s)).rstrip("0")
            if txt.endswith("."):
                txt += "0"
            return txt + "x"

        def _sp_width(self, s):
            """倍速按钮宽度：贴着这几个字，只留 3px 内边（原来 26px 太占地方）"""
            return max(28, btn_width(self._sp_label(s), 12) - 24)

        def db_speed_menu(self):
            pop = getattr(self, "db_speed_pop", None)
            if pop is not None:
                try:
                    pop.unpost()
                    pop.destroy()
                except Exception:
                    pass
                self.db_speed_pop = None
                return
            m = tk.Menu(self, tearoff=0, font=("Microsoft YaHei UI", 9))
            for s in self.SPEEDS:
                cur = "✔ " if abs(s - self.db_speed) < 0.01 else "   "
                m.add_command(label="%s%s" % (cur, self._sp_label(s)),
                              command=lambda v=s: self.db_set_speed(v))
            self.db_speed_pop = m
            try:
                b = self.db_speed_btn
                m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height() + 2)
            except Exception:
                pass

        def db_set_speed(self, v):
            self.db_speed = float(v)
            self.db_speed_btn.set_text(self._sp_label(self.db_speed),
                                       w=self._sp_width(self.db_speed))
            pop = getattr(self, "db_speed_pop", None)
            self.db_speed_pop = None
            if pop is not None:
                try:
                    pop.unpost()
                    pop.destroy()
                except Exception:
                    pass
            if self.db_state == "playing" and getattr(self, "db_cur_video", None):
                pos = self._db_now()        # 用真实位置，别用最多旧 300ms 的 db_pos
                p = getattr(self, "db_player", None)
                if p is not None and hasattr(p, "set_speed"):
                    # 画面：解码进程不动，只改消费节拍 —— 切换瞬间不断流、不黑屏
                    p.set_speed(self.db_speed)
                    self.db_seek_base = pos
                    self.db_t0 = time.time()
                    self._db_set_time(pos)
                    # 音频：atempo 是启动参数，只能重开一路，但画面不受影响
                    self._db_audio_start(pos, keep_old=True)
                    self.db_hint.configure(
                        text="%s 倍速播放中（含音频）" % ("%.1f" % self.db_speed))
                else:
                    item = getattr(self, "db_cur_video", None) or {}
                    if item.get("kind") == "audio":
                        # 音乐：波形不用重建，只把音频换一路，旧那路留到新的接上
                        self.db_seek_base = pos
                        self.db_t0 = time.time()
                        self._db_set_time(pos)
                        self._db_audio_start(pos, keep_old=True)
                        self.db_hint.configure(
                            text="音乐播放中 · %s" % self._sp_label(self.db_speed))
                    else:
                        self.db_play_from(pos)
                        self.db_hint.configure(text="倍速 %s（音频随之重开）"
                                               % self._sp_label(self.db_speed))
            else:
                self.db_hint.configure(text="倍速 %s（播放时生效）"
                                       % self._sp_label(self.db_speed))

        # ---------- 音量：细轨道 + 小圆点，喇叭点击静音
        def _db_vol_geom(self):
            cv = self.db_vol_cv
            try:
                w = max(2, cv.winfo_width())
                h = max(2, cv.winfo_height())
            except Exception:
                w, h = 104, 22
            return 6, w - 6, h // 2

        def _db_vol_draw(self):
            """只更新坐标，不整块重画——拖动时每秒几十次，重建图元会把主线程拖卡"""
            cv = self.db_vol_cv
            x0, x1, y = self._db_vol_geom()
            frac = min(1.0, max(0.0, self.db_vol / 100.0))
            px = x0 + (x1 - x0) * frac
            on = getattr(self, "_pb_vol_on", False)
            trk, fil, dot = (("#59616f", "#ff9a2e", "#ffb44d") if on
                             else ("#c9d0da", "#d8dee6", "#dfe4ea"))
            ids = getattr(self, "_db_vol_ids", None)
            try:
                if not ids or not cv.find_withtag("vol"):
                    cv.delete("all")
                    t = cv.create_line(x0, y, x1, y, fill=trk, width=2,
                                       tags="vol")
                    f = cv.create_line(x0, y, x0, y, fill=fil, width=2,
                                       tags="vol")
                    d = cv.create_oval(x0 - 4, y - 4, x0 + 4, y + 4,
                                       fill=dot, outline="#ffffff",
                                       width=1, tags="vol")
                    ids = (t, f, d)
                    self._db_vol_ids = ids
                t, f, d = ids
                cv.itemconfig(t, fill=trk)
                cv.itemconfig(f, fill=fil)
                cv.itemconfig(d, fill=dot)
                cv.coords(t, x0, y, x1, y)
                cv.coords(f, x0, y, max(x0, px), y)
                cv.coords(d, px - 4, y - 4, px + 4, y + 4)
            except Exception:
                pass

        def _db_vol_press(self, e):
            page = getattr(self, "page", "")
            if not getattr(self, "_pb_vol_on", False) and page not in (
                    "db", "card", "queue"):
                return              # 游戏页没视频、没音乐时音量条才有理由不动
            x0, x1, y = self._db_vol_geom()
            frac = min(1.0, max(0.0, (e.x - x0) / float(max(1, x1 - x0))))
            self.db_vol = int(round(frac * 100))
            muted_now = self.db_vol <= 0
            if not muted_now:
                self.db_vol_cache = self.db_vol
            # 图标只在"静音/有声"翻转时才重绘，逐像素重绘会卡住播放
            if muted_now != getattr(self, "_db_sym_muted", None):
                self._db_sym_muted = muted_now
                self.db_muted = muted_now
                self.db_vol_btn.set_sym("mute" if muted_now else "vol")
            self._db_vol_draw()
            self._db_vol_live()

        def _pb_audio_proc(self):
            """当前页正在出声的音频进程：前三页共用 db_audio_proc，游戏页用主题曲的 ffplay"""
            page = getattr(self, "page", "")
            if page == "game":
                if not getattr(self, "game_audio_on", False):
                    return None
                return getattr(self, "game_audio_proc", None)
            if getattr(self, "db_state", "idle") != "playing":
                return None
            return getattr(self, "db_audio_proc", None)

        def _db_vol_live(self):
            """拖动中就把新音量推给当前页音频的会话——不重启、不重播、不错位"""
            if not getattr(self, "_db_use_session", True):
                return
            p = self._pb_audio_proc()
            if p is None or p.poll() is not None:
                return
            if WINVOL.pid != p.pid:
                WINVOL.bind(p.pid)
            if WINVOL.set_volume(self.db_vol / 100.0):
                WINVOL.set_mute(self.db_muted or self.db_vol <= 0)

        def _db_wheel_vol(self, e):
            """滚轮调音量：视频画面区、整条控制条上滚都算，一格 5%"""
            try:
                d = int(getattr(e, "delta", 0))
            except Exception:
                d = 0
            if not d:
                return "break"
            v = int(round(self.db_vol + (5 if d > 0 else -5)))
            v = max(0, min(100, v))
            if v != self.db_vol:
                self.db_vol = v
                muted_now = v <= 0
                if not muted_now:
                    self.db_vol_cache = v
                if muted_now != getattr(self, "_db_sym_muted", None):
                    self._db_sym_muted = muted_now
                    self.db_muted = muted_now
                    self.db_vol_btn.set_sym("mute" if muted_now else "vol")
                self._db_vol_draw()
                self._db_vol_live()      # 走会话音量，实时改，不重启音频
            return "break"

        def _bind_wheel_vol(self, w):
            """把滚轮调音量绑到控件和它所有的子控件（按钮内部还有小 canvas/label）"""
            try:
                w.bind("<MouseWheel>", self._db_wheel_vol, add="+")
            except Exception:
                pass
            try:
                kids = w.winfo_children()
            except Exception:
                return
            for c in kids:
                self._bind_wheel_vol(c)

        def _db_vol_release(self, _=None):            # 会话音量已经实时生效，这里只在降级模式下才重起音频
            if getattr(self, "_db_use_session", True):
                self._db_vol_live()
                return
            page = getattr(self, "page", "")
            if page in ("db", "card", "queue") and self.db_state == "playing":
                self._db_audio_start(self._db_now())
            elif page == "game" and getattr(self, "game_audio_on", False):
                self._game_audio_play()

        def db_mute_toggle(self):
            page = getattr(self, "page", "")
            if self.db_muted or self.db_vol <= 0:
                self.db_muted = False
                self.db_vol = self.db_vol_cache or 70
                self._db_sym_muted = False
                self.db_vol_btn.set_sym("vol")
                if page == "game":
                    self._game_hint_audio()
                else:
                    self.db_hint.configure(text="音量 %d%%" % self.db_vol)
            else:
                self.db_vol_cache = self.db_vol
                self.db_muted = True
                self.db_vol = 0
                self._db_sym_muted = True
                self.db_vol_btn.set_sym("mute")
                if page == "game":
                    self.game_play_hint.configure(text="循环播放中（已静音）")
                else:
                    self.db_hint.configure(text="已静音（再点喇叭恢复）")
            self._db_vol_draw()
            # 会话可用时直接静音/恢复，不重起音频（重起会重播并错位）
            if getattr(self, "_db_use_session", True):
                self._db_vol_live()
            elif page == "game" and getattr(self, "game_audio_on", False):
                self._game_audio_play()
            elif self.db_state == "playing":
                self._db_audio_start(self._db_now())

        def _game_hint_audio(self):
            """游戏页提示行：把音乐状态带上"""
            if getattr(self, "db_muted", False) or self.db_vol <= 0:
                self.game_play_hint.configure(text="循环播放中（已静音）")
            else:
                self.game_play_hint.configure(
                    text="循环播放中（含音乐）" if getattr(self, "game_audio_on", False)
                    else "循环播放中（无音乐）")

        def db_snapshot(self):
            item = getattr(self, "db_cur_video", None)
            if not item or not self.db_frame:
                self.db_hint.configure(text="还没有画面可截图")
                return
            name = safe_name(os.path.splitext(os.path.basename(item["path"]))[0], 40)
            p = filedialog.asksaveasfilename(
                title="保存截图", defaultextension=".png",
                initialfile="%s_截图.png" % name,
                filetypes=[("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg")])
            if not p:
                return
            try:
                self.db_frame.save(p)
                self.db_hint.configure(text="截图已保存：%s" % os.path.basename(p))
            except Exception as e:
                self.db_hint.configure(text="截图失败：%s" % e)

        # ---------- 全屏：视频走 OpenCV 窗口，音乐可视化仍用 Tk 画布
        def _fs_out(self):
            """全屏时给解码器的输出规格：整屏尺寸 + 画面模式（cine 框按比例缩过）"""
            w = getattr(self, "fs_win", None)
            if w is None:
                return None, None, None
            W, H = int(w.w), int(w.h)
            asp = float(getattr(self, "db_aspect", 0.0) or 0.0)
            bw, bh = W, H
            if asp > 0.01:
                if (W / float(H)) > asp:
                    bw = max(2, int(H * asp))
                else:
                    bh = max(2, int(W / asp))
            return (W, H), (bw, bh), getattr(self, "db_fit", "cover")

        def _db_replay_fs(self):
            """进出全屏 / 换画面模式时按新规格重起解码器（音频不断，画面接当前位置）"""
            item = getattr(self, "db_cur_video", None)
            if not item or self.db_state != "playing":
                return
            pos = self._db_now()
            self.db_play_from(pos, keep_audio=True)

        def _fs_cv2_ok(self):
            global _CV2_STATE
            if _CV2_STATE is None:
                try:
                    import cv2            # noqa: F401
                    _CV2_STATE = True
                except Exception:
                    _CV2_STATE = False
            return bool(_CV2_STATE)

        def _fs_open_cv2(self):
            try:
                sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
                p = getattr(self, "db_player", None)
                win = FsVideoWindow((sw, sh), self._fs_q,
                                    fit=getattr(self, "db_fit", "cover"),
                                    aspect=float(getattr(self, "db_aspect", 0.0) or 0.0),
                                    fps=(p.fps if p else 30.0))
                win.open()
                self.fs_win = win
            except Exception:
                self.fs_win = None
                return False
            self._fs_poll()
            if getattr(self, "fs_win", None) is None:      # 窗口没起来 → 退回 Tk 全屏
                return False
            self._db_redraw()             # 先把手上这一帧顶上去，换规格期间不黑屏
            self._db_replay_fs()          # 解码器改成整屏规格：4K 源直接按屏幕尺寸出帧
            self._db_redraw()
            if getattr(self, "_fs_poll_id", None) is None:
                self._fs_poll_id = self.after(40, self._fs_poll_loop)
            self._pb_tip("全屏：滚轮调音量，右键切换画面模式，双击或 Esc 退出")
            return True

        def _fs_push(self, raw=None, win=None):
            win = win if win is not None else getattr(self, "fs_win", None)
            if win is None:
                return
            p = getattr(self, "db_player", None)
            if raw is None:
                im = self.db_frame
                if im is None:
                    return
                try:
                    import numpy as np
                    arr = np.asarray(im)
                    if arr.ndim != 3 or arr.shape[2] != 3:
                        arr = np.array(im.convert("RGB"))
                except Exception:
                    return
            else:
                # 全屏走这条：原始字节直接 reshape，一帧都不经过 PIL
                import numpy as np
                try:
                    arr = np.frombuffer(raw, dtype=np.uint8).reshape(
                        p.frame_h, p.frame_w, 3)
                except Exception:
                    return
            win.bgr = bool(getattr(p, "fs_fit", None))
            win.fit = getattr(self, "db_fit", "cover")
            win.aspect = float(getattr(self, "db_aspect", 0.0) or 0.0)
            pos = self._db_now() if self.db_state == "playing" else self.db_pos
            win.frac = (pos / self.db_dur) if self.db_dur else 0.0
            if p:
                win.fps = max(8.0, p.fps * max(0.25, p.speed))
            win.push(arr)

        def _fs_poll(self):
            w = getattr(self, "fs_win", None)
            while True:
                try:
                    name, val = self._fs_q.get_nowait()
                except pyqueue.Empty:
                    break
                except Exception:
                    break
                try:
                    if name == "exit":
                        self.db_exit_fs()
                    elif name == "fit":
                        self.db_toggle_fit()
                    elif name == "seek":
                        self._db_seek_frac(val)
                    elif name == "vol":
                        ev = type("E", (), {"delta": 120 * int(val or 1)})()
                        self._db_wheel_vol(ev)
                    elif name == "toggle":
                        self.db_play_toggle()
                    elif name == "error":
                        self.fs_win = None
                        self._pb_tip("全屏窗口打不开（opencv 不可用），已用普通全屏")
                        self.db_fullscreen()
                except Exception:
                    pass
            if w is not None:
                try:
                    if not w.alive and not w._stop:
                        self.fs_win = None
                        self._db_redraw()
                except Exception:
                    pass

        def _fs_poll_loop(self):
            self._fs_poll_id = None
            if getattr(self, "fs_win", None) is None:
                return
            self._fs_poll()
            if getattr(self, "fs_win", None) is not None:
                self._fs_poll_id = self.after(40, self._fs_poll_loop)

        def db_fullscreen(self):
            item = getattr(self, "db_cur_video", None)
            if not item:
                return
            if getattr(self, "fs_win", None) is not None:
                self.db_exit_fs()
                return
            if self.db_fs is not None and self.db_fs.winfo_exists():
                self.db_exit_fs()
                return
            # 视频全屏用 OpenCV 窗口（Tk 在 2K 屏上贴图只有 17fps）
            if (not getattr(self, "wave_mode", False)) and self._fs_cv2_ok():
                if self._fs_open_cv2():
                    return
            fs = tk.Toplevel(self)
            fs.configure(bg="black")
            try:
                fs.attributes("-fullscreen", True)
            except Exception:                 # 个别主题不接受这个属性，退回无边框全屏
                fs.overrideredirect(True)
                fs.geometry("%dx%d+0+0" % (fs.winfo_screenwidth(),
                                          fs.winfo_screenheight()))
            fs.attributes("-topmost", True)
            cv = tk.Canvas(fs, bg="black", highlightthickness=0, bd=0)
            cv.pack(fill="both", expand=True)
            cv.bind("<MouseWheel>", self._db_wheel_vol, add="+")
            self.db_fs_img_id = cv.create_image(0, 0, anchor="center")
            fs_track = cv.create_line(0, 0, 1, 1, fill="#a8b2bf", width=3)
            fs_fill = cv.create_line(0, 0, 1, 1, fill="#ff9a2e", width=3)
            fs_dot = cv.create_oval(0, 0, 1, 1, fill="#ffb44d",
                                    outline="#ffffff", width=1)
            self._db_prog_sets.append((cv, fs_track, fs_fill, fs_dot))
            fs.bind("<Escape>", lambda e: self.db_exit_fs())
            fs.bind("<Double-1>", lambda e: self.db_exit_fs())
            fs.bind("<F11>", lambda e: self.db_exit_fs())
            fs.bind("<Button-3>", lambda e: self.db_toggle_fit())
            # 右上角给个显眼的退出提示，点它也能退（键盘 Esc 同样有效）
            try:
                sw = fs.winfo_screenwidth()
                tip = "  ESC 退出全屏  "
                fnt = ("Microsoft YaHei UI", 13, "bold")
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                               (-1, -1), (1, 1), (-1, 1), (1, -1)):
                    cv.create_text(sw - 24 + dx, 24 + dy, anchor="ne", text=tip,
                                   fill="#080c12", font=fnt)   # 一圈深色描边
                self.db_fs_esc_id = cv.create_text(
                    sw - 24, 24, anchor="ne", text=tip,
                    fill="#f4faff", font=fnt)
                cv.tag_bind(self.db_fs_esc_id, "<Button-1>",
                            lambda e: self.db_exit_fs())
            except Exception:
                pass
            self.db_fs = cv
            self.db_fs_win = fs
            self._db_fs_sz = None
            try:
                fs.lift()
                fs.focus_force()      # 焦点给全屏窗，Esc 才一定收得到
                cv.focus_set()
            except Exception:
                pass
            self._db_redraw()

        def db_exit_fs(self):
            w = getattr(self, "fs_win", None)
            if w is not None:
                self.fs_win = None
                pid = getattr(self, "_fs_poll_id", None)
                if pid:
                    try:
                        self.after_cancel(pid)
                    except Exception:
                        pass
                self._fs_poll_id = None
                try:
                    w.close()
                except Exception:
                    pass
                try:
                    while True:
                        self._fs_q.get_nowait()
                except Exception:
                    pass
                self._db_replay_fs()      # 回到窗口规格，别继续按整屏尺寸解码
                self._db_redraw()
                return
            win = getattr(self, "db_fs_win", None)
            cv = getattr(self, "db_fs", None)
            self.db_fs = None
            self.db_fs_win = None
            self.db_fs_img_id = None
            self._db_fs_sz = None
            self._wave_buf_fs = None         # 全屏那张大图缓存放掉，别占内存
            if cv is not None:
                try:
                    self._db_prog_sets = [s for s in self._db_prog_sets
                                          if s[0] is not cv]
                except Exception:
                    self._db_prog_sets = []
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass

        def db_add(self, whole_author):
            if self.db_view == "authors":
                a = getattr(self, "db_cur_author", None)
                if not a:
                    self.db_status.configure(text="先选一位作者")
                    return
                picks = a["items"]
                who = a["name"]
            else:
                sel = self.db_v_tree.selection()
                if not sel:
                    self.db_status.configure(text="先在视频列表里选一行")
                    return
                a = getattr(self, "db_cur_author", None)
                who = a["name"] if a else "-"
                picks = (a["items"] if whole_author else
                         [self.db_videos[int(s)] for s in sel])
            n = 0
            for it in picks:
                if self._add_one(it["path"], {"author": who}):
                    n += 1
            self._rebuild_table()
            self.db_status.configure(text="已加入 %d 个文件（作者：%s）→ 去【添加媒体】页可看队列"
                                     % (n, who))

        # ---------------- 转换
        def stop_convert(self):
            if self.running:
                self.stop_flag = True
                kill_current()
                self.canvas.itemconfig(self.status_text, text="正在停止当前任务…")
            else:
                self._refresh_status()

        def start_convert(self):
            if self.running:
                self.canvas.itemconfig(self.status_text, text="正在转换中，要中断请点【停止】")
                return
            if not ffmpeg or not ffprobe:
                messagebox.showerror("缺少 ffmpeg", "找不到 ffmpeg/ffprobe，无法转换。")
                return
            root, vdir, mdir = card_dirs(self.cfg)
            if not root:
                messagebox.showinfo("先设记忆棒", "请点右上【设置】添加记忆棒目录（可填盘符或目录）。")
                return
            try:
                card_dirs(self.cfg, create=True)
                tdir, tvid, tmus = temp_dirs(self.cfg, create=True)
            except Exception as e:
                messagebox.showerror("目录创建失败", "%s" % e)
                return
            todo = [i for i, it in enumerate(self.items) if it["state"] == "queued"]
            if not todo:
                messagebox.showinfo("队列为空", "没有待转换的文件。")
                return
            # 掌机对子目录数量极敏感（文件再多没事，目录一多就列不全），
            # 所以开始前先看一眼，超了就把话说在状态栏上
            try:
                self._author_dirs = {}
                if group_author_on(self.cfg):
                    authors = []
                    for i in todo:
                        a2 = (self.items[i].get("author") or "").strip()
                        if a2 and a2 not in authors:
                            authors.append(a2)
                    self._author_dirs = self._plan_author_dirs(vdir, authors)
            except Exception:
                pass
            self.running = True
            self.stop_flag = False
            self.done = self.fail = 0
            self.t0 = time.time()
            # 多路转码 + 单独的拷贝线程：一个转完就往卡里搬，别的文件接着转，互不等待
            self._conv_left = self._conv_workers()
            self._job_lock = threading.Lock()
            self._idx_lock = threading.Lock()
            self._job_pos = 0
            self._copy_q = pyqueue.Queue()
            self._copy_stop_sent = False
            self._copy_end_seen = False
            self._finish_sent = False
            try:
                _sub, _pfx = psp_video_sub(self.cfg)
                self._conv_idx = self._next_index(vdir, tvid, _pfx)
            except Exception:
                self._conv_idx = 1
            for _k in range(self._conv_left):
                threading.Thread(target=self._worker_conv, args=(todo,),
                                 daemon=True).start()
            threading.Thread(target=self._worker_copy, daemon=True).start()
            self._update_progress_visual()

        def _conv_workers(self):
            """并行路数：x264 很轻但 4K 源解码吃 CPU，8 线程给 2 路最稳"""
            try:
                n = (os.cpu_count() or 4) // 3
            except Exception:
                n = 2
            return max(2, min(3, n))

        def _plan_author_dirs(self, vdir, authors):
            """一个作者一间目录，不做任何合并。

            早先按「掌机只显示 14 个目录」的说法把作者合成「A+B+C」，
            真机验证过没用，还会让名字变长；后来查明掌机卡的是视频条目总量，
            跟目录数量无关，所以这里只负责建目录、给作者名去重。
            """
            mapping = {}
            dirs = []
            try:
                dirs = sorted(d for d in os.listdir(vdir)
                              if os.path.isdir(os.path.join(vdir, d)))
            except Exception:
                dirs = []
            # 卡上已有的目录先认领回作者（老合并目录留下的「A+B」也按 + 号拆着认）
            for d in dirs:
                for a in d.split("+"):
                    if a.strip():
                        mapping[a.strip()] = d

            used = set(mapping.values())
            for a in authors:
                a = (a or "").strip()
                if not a or a in mapping:
                    continue
                name = safe_name(a, 24).strip() or "作者"
                base, k = name, 2
                while name in used and not os.path.isdir(os.path.join(vdir, name)):
                    name = "%s_%d" % (base, k)
                    k += 1
                if name not in used:
                    try:
                        os.makedirs(os.path.join(vdir, name), exist_ok=True)
                    except Exception:
                        pass
                    used.add(name)
                    dirs.append(name)
                mapping[a] = name
            return mapping

        def _video_job_names(self, it, vdir, tvid):
            """按存放布局定临时名与落地名：锁内取号 + 查重，多路并行也不会撞名"""
            with self._idx_lock:
                n = self._conv_idx
                self._conv_idx += 1
                # 掌机一个文件夹里累计约 1000 个视频就到头了，超出的按顺序不再显示，
                # 所以默认全部平铺在 VIDEO 根；只有选了「按作者分目录」才归档，
                # 归档时一个作者一间目录，不合并。
                dst_dir = vdir
                adir = ""
                if group_author_on(self.cfg):
                    adir = getattr(self, "_author_dirs", {}).get(
                        (it.get("author") or "").strip(), "")
                    if adir:
                        dst_dir = os.path.join(vdir, adir)
                        try:
                            os.makedirs(dst_dir, exist_ok=True)
                        except Exception:
                            dst_dir = vdir
                stem = safe_name(it["title"] or os.path.splitext(
                    os.path.basename(it["src"]))[0], 40).strip() or "video"
                out_name = stem + ".MP4"
                k = 2
                while os.path.isfile(os.path.join(dst_dir, out_name)):
                    out_name = "%s (%d).MP4" % (stem, k)
                    k += 1
                tmp_mp4 = os.path.join(tvid, "_%05d_%s" % (n, out_name))
            return tmp_mp4, os.path.splitext(tmp_mp4)[0] + ".THM", out_name, dst_dir

        def _next_index(self, vdir, tvid, prefix="M4V"):
            """PSP 断号就不往后列，所以取号要从 1 起「填洞」，不能简单地 max+1"""
            used = set()
            for d in (vdir, tvid):
                try:
                    for fn in os.listdir(d):
                        m = re.match(prefix + r"(\d{5})", fn, re.I)
                        if m:
                            used.add(int(m.group(1)))
                except Exception:
                    pass
            n = 1
            while n in used and n <= 99999:
                n += 1
            return n

        def _worker_conv(self, todo):
            """转码线程：只管解码/编码，产物丢给拷贝线程，不等卡写完"""
            root, vdir, mdir = card_dirs(self.cfg, create=True)
            tdir, tvid, tmus = temp_dirs(self.cfg, create=True)
            if not vdir or not mdir:
                self.msgs.put(("conv_end",))
                return
            bitrate = int(self.cfg["mp3_bitrate"])
            sr = int(self.cfg["mp3_sr"])
            copy_mp3 = bool(self.cfg["copy_mp3"])
            prof = resolve_profile(self.cfg)
            while True:
                if self.stop_flag:
                    break
                # 从共享清单里领一个活，几个线程各转各的
                with self._job_lock:
                    if self._job_pos >= len(todo):
                        break
                    i = todo[self._job_pos]
                    self._job_pos += 1
                it = self.items[i]
                src = it["src"]
                made = []
                if not os.path.isfile(src):
                    self.msgs.put(("row", i, "源文件不见了", 0))
                    self.msgs.put(("fail", i))
                    continue
                try:
                    if it["kind"] == "video":
                        info = probe(ffprobe, src)
                        w, h, na, rot, dur, portrait = analyse(info)
                        if not w:
                            raise RuntimeError("读不到视频信息（文件可能损坏）")
                        tmp_mp4, tmp_thm, out_name, dst_dir = self._video_job_names(it, vdir, tvid)
                        made += [tmp_mp4, tmp_thm]
                        self.msgs.put(("row", i, "转换中…（%dx%d%s）" %
                                       (w, h, "，竖屏左转" if portrait else ""), 0))

                        def cb(p, txt, _i=i):
                            self.msgs.put(("row", _i, "转换中 %d%%  %s" % (int(p * 100), txt), p))

                        rc, err = convert_video(ffmpeg, src, tmp_mp4, dur, portrait, prof, cb,
                                                has_audio=(na > 0))
                        if rc != 0 or not os.path.isfile(tmp_mp4) or os.path.getsize(tmp_mp4) < 10000:
                            raise RuntimeError("编码失败：%s" % (
                                err.strip().splitlines()[-1] if err.strip() else "未知"))
                        self.msgs.put(("row", i, "生成缩略图…", 0.97))
                        make_thm(ffmpeg, src, tmp_thm, dur, portrait, from_mp4=tmp_mp4)
                        ok_psp, why = psp_verify(ffprobe, tmp_mp4, prof)
                        if ok_psp:
                            self.msgs.put(("row", i, "已转好，排队写入记忆棒…", 0.985))
                        else:                # 不合规格也照写，但把原因摆在界面上
                            self.msgs.put(("row", i, "⚠ 掌机可能报数据错误（%s）" % why, 0.985))
                        pairs = [(tmp_mp4, os.path.join(dst_dir, out_name))]
                        if os.path.isfile(tmp_thm):
                            pairs.append((tmp_thm, os.path.join(
                                dst_dir, os.path.splitext(out_name)[0] + ".THM")))
                        self._copy_q.put(("video", i, pairs, src))
                    else:
                        stem = safe_name(it["title"] or os.path.splitext(os.path.basename(src))[0], 60)
                        with self._idx_lock:
                            n = self._conv_idx
                            self._conv_idx += 1
                        a_key = self.cfg.get("audio_fmt") or "mp3"
                        if a_key not in AUDIO_FMTS:
                            a_key = "mp3"
                        a_ext = AUDIO_FMTS[a_key]["ext"]
                        tmp_aud = os.path.join(tmus, "_%05d_%s%s" % (n, stem, a_ext))
                        made.append(tmp_aud)
                        ext = os.path.splitext(src)[1].lower()
                        if a_key == "mp3" and ext == ".mp3" and copy_mp3:
                            shutil.copy2(src, tmp_aud)
                            self.msgs.put(("row", i, "复制中…", 0.9))
                        else:
                            if a_key == "wav":
                                tip = "转 WAV %dHz 无损…" % sr
                            elif a_key == "wma":
                                tip = "转 WMA %dk/%dHz…" % (bitrate, sr)
                            else:
                                tip = "转 MP3 %dk/%dHz…" % (bitrate, sr)
                            self.msgs.put(("row", i, tip, 0.3))
                            rc, err = convert_audio(ffmpeg, src, tmp_aud, bitrate, sr, a_key)
                            if rc != 0 or not os.path.isfile(tmp_aud):
                                raise RuntimeError("音频转换失败：%s" % (
                                    err.strip().splitlines()[-1] if err.strip() else "未知"))
                        self.msgs.put(("row", i, "已转好，排队写入记忆棒…", 0.97))
                        self._copy_q.put(("audio", i,
                                          [(tmp_aud, os.path.join(mdir, stem + a_ext))],
                                          src))
                except Exception as e:
                    for p in made:          # 半成品不留在临时目录
                        try:
                            if p and os.path.isfile(p):
                                os.remove(p)
                        except Exception:
                            pass
                    if self.stop_flag:
                        self.msgs.put(("row", i, "已停止", 0))
                        break
                    self.msgs.put(("row", i, "失败：%s" % str(e)[:120], 0))
                    self.msgs.put(("fail", i))
            self.msgs.put(("conv_end",))

        def _worker_copy(self):
            """拷贝线程：慢慢往卡里搬也不挡住后面的转码，真正搬完才算完成"""
            while True:
                try:
                    job = self._copy_q.get(timeout=0.25)
                except pyqueue.Empty:
                    continue
                if job is None:              # 全部转完，收尾退出
                    break
                kind, i, pairs, src = job
                first = pairs[0][1] if pairs else ""
                if self.stop_flag:           # 停止：把还没搬的临时文件清掉
                    for a, _b in pairs:
                        try:
                            if os.path.isfile(a):
                                os.remove(a)
                        except Exception:
                            pass
                    self.msgs.put(("row", i, "已停止", 0))
                    continue
                try:
                    self.msgs.put(("row", i, "写入记忆棒…", 0.995))
                    for a, b in pairs:
                        if os.path.isfile(a):
                            shutil.move(a, b)
                            flush_to_disk(b)   # 立刻落盘：直接拔卡也不会只剩半截文件
                    size = os.path.getsize(first) if os.path.isfile(first) else 0
                    stem = os.path.splitext(os.path.basename(first))[0]
                    if kind == "video":
                        self.msgs.put(("row", i, "完成  %s + THM" % human(size), 1.0))
                        self.msgs.put(("video_done", i, first, stem, src))
                    else:
                        self.msgs.put(("row", i, "完成  %s" % human(size), 1.0))
                        self.msgs.put(("audio_done", i, first, stem, src))
                except Exception as e:
                    self.msgs.put(("row", i, "写入失败：%s" % str(e)[:120], 0))
                    self.msgs.put(("fail", i))
                    for a, _b in pairs:      # 搬了一半的临时文件也别留着
                        try:
                            if os.path.isfile(a):
                                os.remove(a)
                        except Exception:
                            pass
            self.msgs.put(("copy_end",))

        def _poll_msgs(self):
            try:
                while True:
                    m = self.msgs.get_nowait()
                    tag = m[0]
                    if tag == "row":
                        self._set_row(m[1], m[2], m[3])
                        self._update_progress_visual()
                    elif tag == "game_row":
                        self._game_row(m[1], m[2], m[3], m[4])
                    elif tag == "game_done":
                        if m[1] == getattr(self, "_game_sid", 0):
                            self.game_status.configure(
                                text="共 %d 个游戏（点一行看图标 / 背景 / 元信息）" % m[2])
                    elif tag in ("video_done", "audio_done"):
                        self.items[m[1]]["state"] = "done"
                        self.done += 1
                        self._update_progress_visual()
                        self._refresh_status()
                    elif tag == "fail":
                        self.items[m[1]]["state"] = "fail"
                        self.fail += 1
                        self._update_progress_visual()
                        self._refresh_status()
                    elif tag == "conv_end":
                        self._conv_left = getattr(self, "_conv_left", 0) - 1
                        if self._conv_left <= 0 and not getattr(self, "_copy_stop_sent", True):
                            self._copy_stop_sent = True
                            self._copy_q.put(None)   # 转码全完：让拷贝线程把剩下的搬完就收尾
                    elif tag == "copy_end":
                        self._copy_end_seen = True
                    elif tag == "warn":
                        self.canvas.itemconfig(self.status_text, text=m[1])
                    elif tag == "stop":
                        self.running = False
                        el = time.time() - self.t0
                        msg = "本轮结束 —— 完成 %d，失败 %d，用时 %s" % (
                            self.done, self.fail, time.strftime("%M分%S秒", time.gmtime(el)))
                        if self.stop_flag:
                            msg = "已停止 —— 完成 %d，失败 %d" % (self.done, self.fail)
                        self.canvas.itemconfig(self.status_text, text=msg)
                        self._update_progress_visual()
                        if self.page == "card":
                            self.refresh_card()
            except pyqueue.Empty:
                pass
            # 转码与拷贝两条线都收工了才算这一轮结束
            if (getattr(self, "running", False)
                    and getattr(self, "_copy_end_seen", False)
                    and not getattr(self, "_finish_sent", False)):
                self._finish_sent = True
                self.msgs.put(("stop", ""))
            self.after(120, self._poll_msgs)

    # ------------------------------------------------------------- 设置弹窗
    class SettingsDialog:
        def __new__(cls, master, cfg, drives):
            dlg = tk.Toplevel(master)
            dlg.title("设置")
            dlg.transient(master)
            dlg.grab_set()
            dlg.resizable(True, False)
            dlg.configure(bg="#e6ecf3")
            dlg.result = None

            frm = tk.Frame(dlg, bg="#e6ecf3")
            frm.pack(padx=16, pady=12, fill="both", expand=True)
            frm.columnconfigure(1, weight=1)
            LAB = ("Microsoft YaHei UI", 10)
            HINT = ("Microsoft YaHei UI", 8)

            def mk_hint(text, row, colspan=2):
                tk.Label(frm, text=text, bg="#e6ecf3", fg="#5b6472", font=HINT).grid(
                    row=row, column=1, columnspan=colspan, sticky="w", padx=(10, 0), pady=(0, 6))

            # 1) 记忆棒目录
            tk.Label(frm, text="记忆棒目录", bg="#e6ecf3", font=LAB).grid(
                row=0, column=0, sticky="w", pady=(2, 2))
            card_var = tk.StringVar(value=cfg.get("card_path") or
                                    (str(cfg.get("drive")) + ":\\" if cfg.get("drive") else ""))
            card_entry = tk.Entry(frm, textvariable=card_var, font=("Microsoft YaHei UI", 9),
                                  relief="sunken", bd=1)
            card_entry.grid(row=0, column=1, sticky="ew", padx=(10, 8), pady=(2, 2))

            def pick_card():
                p = filedialog.askdirectory(title="选择记忆棒根目录（PSP 盘根或任意目录）")
                if p:
                    card_var.set(p)

            ImgButton(frm, "浏览…", pick_card, btn_width("浏览…", 10), 28, 10,
                      kind="aero", bg="#e6ecf3").grid(row=0, column=2, sticky="e", pady=(2, 2))
            mk_hint("可填盘符（G:）或任意目录（G:\\PSP）；盘内子目录固定，不用改", 1)

            drv_var = tk.StringVar(value="")
            drv_box = ttk.Combobox(frm, values=["%s:\\   （%s，剩 %s）" % (d["letter"], d["type"],
                                                                       human(d["free"]))
                                                for d in drives] or ["（没有可用的盘符）"],
                                   width=34, state="readonly", font=("Microsoft YaHei UI", 9))
            drv_box.grid(row=2, column=1, sticky="w", padx=(10, 8))
            drv_box.set("")
            tk.Label(frm, text="或选盘符", bg="#e6ecf3", font=LAB).grid(row=2, column=0, sticky="w")

            def use_drive(_=None):
                sel = drv_box.current()
                if sel >= 0 and drives:
                    card_var.set(drives[sel]["path"])

            drv_box.bind("<<ComboboxSelected>>", use_drive)

            ttk.Separator(frm, orient="horizontal").grid(
                row=3, column=0, columnspan=3, sticky="ew", pady=8)

            # 2) 临时目录
            tk.Label(frm, text="临时目录", bg="#e6ecf3", font=LAB).grid(row=4, column=0, sticky="w")
            temp_var = tk.StringVar(value=temp_path(cfg))
            temp_entry = tk.Entry(frm, textvariable=temp_var, font=("Microsoft YaHei UI", 9),
                                  relief="sunken", bd=1)
            temp_entry.grid(row=4, column=1, sticky="ew", padx=(10, 8))

            def pick_temp():
                p = filedialog.askdirectory(title="选择临时目录")
                if p:
                    temp_var.set(p)

            ImgButton(frm, "浏览…", pick_temp, btn_width("浏览…", 10), 28, 10,
                      kind="aero", bg="#e6ecf3").grid(row=4, column=2, sticky="e")
            mk_hint("先转到这里（自动建 video / music），转完再搬进记忆棒，慢速卡也不卡住", 5)

            ttk.Separator(frm, orient="horizontal").grid(
                row=6, column=0, columnspan=3, sticky="ew", pady=8)

            # 3) 视频格式
            tk.Label(frm, text="视频格式", bg="#e6ecf3", font=LAB).grid(row=7, column=0, sticky="w")
            labels = [v["label"] for _, v in VP_ORDER]
            vp = ttk.Combobox(frm, values=labels, width=44, state="readonly",
                              font=("Microsoft YaHei UI", 9))
            cur_key = cfg.get("vprofile") or "psp_std"
            try:
                vp.current([k for k, _ in VP_ORDER].index(cur_key))
            except ValueError:
                vp.current(0)
            vp.grid(row=7, column=1, columnspan=2, sticky="w", padx=(10, 8))

            # 4) MP3
            mrow = tk.Frame(frm, bg="#e6ecf3")
            mrow.grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))
            tk.Label(mrow, text="MP3 码率", bg="#e6ecf3", font=LAB).pack(side="left")
            br = ttk.Combobox(mrow, values=[320, 256, 192, 160, 128, 96], width=8,
                              state="readonly", font=("Microsoft YaHei UI", 9))
            br.set(int(cfg.get("mp3_bitrate") or 320))
            br.pack(side="left", padx=(6, 18))
            tk.Label(mrow, text="采样率", bg="#e6ecf3", font=LAB).pack(side="left")
            sr = ttk.Combobox(mrow, values=[44100, 48000, 32000, 22050, 16000], width=8,
                              state="readonly", font=("Microsoft YaHei UI", 9))
            sr.set(int(cfg.get("mp3_sr") or 44100))
            sr.pack(side="left", padx=(6, 0))
            tk.Label(mrow, text="Hz", bg="#e6ecf3", font=LAB).pack(side="left", padx=(4, 0))

            cp = tk.BooleanVar(value=bool(cfg.get("copy_mp3", True)))
            tk.Checkbutton(frm, text="文件本身是 MP3 时直接复制进记忆棒（不重新编码）", variable=cp,
                           bg="#e6ecf3", font=LAB, activebackground="#e6ecf3").grid(
                row=9, column=0, columnspan=3, sticky="w", pady=(10, 0))

            def ok():
                p = (card_var.get() or "").strip()
                if len(p) == 2 and p[1] == ":":
                    p = p + "\\"
                d = {"card_path": p,
                     "drive": p[0].upper() if len(p) >= 2 and p[1] == ":" else "",
                     "temp_dir": (temp_var.get() or "").strip(),
                     "vprofile": [k for k, _ in VP_ORDER][vp.current()],
                     "mp3_bitrate": int(br.get()), "mp3_sr": int(sr.get()),
                     "copy_mp3": bool(cp.get()), "naming": 0}
                dlg.result = d
                dlg.destroy()

            btns = tk.Frame(dlg, bg="#e6ecf3")
            btns.pack(pady=(4, 12))
            ImgButton(btns, "保存", ok, btn_width("保存", 11, pad=34, mini=104), 32, 11,
                      kind="aero", bg="#e6ecf3").pack(side="left", padx=8)
            ImgButton(btns, "取消", dlg.destroy, btn_width("取消", 11, pad=34, mini=104), 32,
                      11, kind="aero", bg="#e6ecf3").pack(side="left", padx=8)
            return dlg

    app = App()
    # 窗口一出来就在后台把 ffplay 热好，别让用户第一条视频替它付那两三秒
    try:
        app.after(120, warm_ffplay)
    except Exception:
        pass
    if os.environ.get("PSP_NO_MAINLOOP"):
        return app
    app.mainloop()


# ----------------------------------------------------------------- 自检（无界面）

def lagtest():
    """打包版自检：把「点视频」每一步的真实耗时写进 %TEMP%\\psp_lagtest.log
    加 --lagtest 启动就会跑一遍并弹记事本，专门用来查"源码版不卡、打包版卡"这种。"""
    log_path = os.path.join(os.environ.get("TEMP", "."), "psp_lagtest.log")
    try:
        os.remove(log_path)
    except Exception:
        pass

    def w(s):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(str(s) + "\n")
        except Exception:
            pass

    w("==== lagtest %s ====" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("frozen  = %s" % bool(getattr(sys, "frozen", False)))
    w("exe     = %s" % sys.executable)
    w("MEIPASS = %s" % getattr(sys, "_MEIPASS", "-"))
    w("app_dir = %s" % app_dir())
    w("ffplay  = %s" % find_tool("ffplay"))
    w("ffmpeg  = %s" % find_tool("ffmpeg"))
    w("ffprobe = %s" % find_tool("ffprobe"))

    fm = find_tool("ffmpeg")
    if fm:
        w("---- 先验证子进程能不能起（ffmpeg -version）----")
        t = time.perf_counter()
        try:
            p = subprocess.Popen([fm, "-version"], creationflags=no_window(),
                                 stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            w("ffmpeg Popen %.1f ms" % ((time.perf_counter() - t) * 1000))
            try:
                p.wait(timeout=20)
                w("ffmpeg 正常结束 rc=%s" % p.returncode)
            except Exception:
                p.kill()
                w("ffmpeg 20 秒没结束，已杀")
        except Exception as e:
            w("ffmpeg 失败: %r" % e)

    ff = find_tool("ffplay")
    if ff:
        base = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=no_window())
        env_all = dict(os.environ)
        env_sdl = dict(os.environ)
        env_sdl["SDL_VIDEODRIVER"] = "dummy"
        env_sdl["SDL_AUDIODRIVER"] = "directsound"

        def try_popen(label, args, kwargs, timeout=12):
            box = {}

            def run():
                t = time.perf_counter()
                try:
                    p = subprocess.Popen(args, **kwargs)
                    box["ms"] = (time.perf_counter() - t) * 1000
                    try:
                        p.kill(); p.wait(timeout=5)
                    except Exception:
                        pass
                except Exception as e:
                    box["err"] = repr(e)

            th = threading.Thread(target=run, daemon=True)
            th.start()
            th.join(timeout)
            if th.is_alive():
                w("  %-40s 卡住 >%ds" % (label, timeout))
            elif "err" in box:
                w("  %-40s 报错 %s" % (label, box["err"]))
            else:
                w("  %-40s %7.1f ms" % (label, box.get("ms", -1)))

        w("---- ffplay 四种启动方式对比 ----")
        try_popen("不传 env + NO_WINDOW", [ff, "-version"], base)
        try_popen("传全部 env + NO_WINDOW", [ff, "-version"], dict(base, env=env_all))
        try_popen("传 SDL env + NO_WINDOW", [ff, "-version"], dict(base, env=env_sdl))
        try_popen("不传 env + 不加 flags", [ff, "-version"],
                  dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL))
        pyi = [k for k in os.environ if k.upper().startswith("_PYI")]
        w("  os.environ 里的 PyInstaller 变量: %s" % (pyi or "无"))

    sample_dir = r"E:\xunxun\抖音作者解析下载\作品保存\数据库\data\关注\97878602588"
    vids = []
    if os.path.isdir(sample_dir):
        for cur, dirs, files in os.walk(sample_dir):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    p = os.path.join(cur, fn)
                    try:
                        if os.path.getsize(p) > 100000:
                            vids.append(p)
                    except Exception:
                        pass
            if len(vids) >= 3:
                break
    w("样片 %d 条" % len(vids))
    if not vids:
        w("没找到样片，跳过界面部分")
        return

    os.environ["PSP_NO_MAINLOOP"] = "1"
    try:
        app = build_gui()
        app.update()
        app.show_page("db")
        app.update()
    except Exception:
        w("建界面失败:\n" + traceback.format_exc())
        return

    for v in vids[:3]:
        item = {"path": v, "kind": "video", "author": "", "title": ""}
        t = time.perf_counter()
        try:
            app._db_open(item, 0, autoplay=True)
        except Exception:
            w("打开失败:\n" + traceback.format_exc())
            continue
        open_ms = (time.perf_counter() - t) * 1000
        got = -1.0
        for _ in range(200):
            app.update()
            time.sleep(0.01)
            if app.db_frame is not None and app.db_state == "playing":
                got = (time.perf_counter() - t) * 1000
                break
        w("点 %-24s 打开 %.1f ms  画面 %.1f ms" % (os.path.basename(v)[:24], open_ms, got))
        try:
            app.db_stop()
        except Exception:
            pass
        app.update()

    item = {"path": vids[0], "kind": "video", "author": "", "title": ""}
    for i in range(2):
        t = time.perf_counter()
        app._db_open(item, 0, autoplay=True)
        w("循环重播 第%d次  %.1f ms" % (i + 1, (time.perf_counter() - t) * 1000))
        try:
            app.db_stop()
        except Exception:
            pass
        app.update()
    try:
        app.destroy()
    except Exception:
        pass


def lagtest_and_show():
    lagtest()
    try:
        os.startfile(os.path.join(os.environ.get("TEMP", "."), "psp_lagtest.log"))
    except Exception:
        pass
    # 万一有 Popen 卡在后台线程里，正常退出会被它拖住，这里直接收工
    os._exit(0)


def selftest():
    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    print("ffmpeg :", ffmpeg)
    print("ffprobe:", ffprobe)
    tmp = os.path.join(os.environ.get("TEMP", "."), "psp_selftest")
    os.makedirs(tmp, exist_ok=True)
    sample_dir = r"E:\xunxun\抖音作者解析下载\作品保存\数据库\data\关注\97878602588"
    vids = []
    for cur, dirs, files in os.walk(sample_dir):
        for fn in files:
            if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                vids.append(os.path.join(cur, fn))
    if not vids:
        print("没有找到测试视频")
        return
    src = vids[0]
    print("\n测试视频:", src)
    info = probe(ffprobe, src)
    w, h, na, rot, dur, portrait = analyse(info)
    print("  尺寸 %dx%d  音频流 %d  旋转 %d  时长 %.1fs  竖屏=%s" % (w, h, na, rot, dur, portrait))
    out = os.path.join(tmp, "M4V00001.MP4")
    t0 = time.time()
    rc, err = convert_video(ffmpeg, src, out, dur, portrait)
    print("  视频转换 rc=%s 用时 %.1fs 大小 %.0f KB" % (rc, time.time() - t0,
                                                os.path.getsize(out) / 1024 if os.path.isfile(out) else 0))
    if rc != 0:
        print("  错误:", err[-300:])
    ok, size = make_thm(ffmpeg, src, os.path.join(tmp, "M4V00001.THM"), dur, portrait)
    print("  缩略图 ok=%s size=%d 头=%s" % (ok, size,
                                       "FFE0" if thm_header_ok(os.path.join(tmp, "M4V00001.THM")) else "FE"))
    chk = probe(ffprobe, out)
    for s in chk.get("streams", []):
        print("  成品流:", {k: s.get(k) for k in ("codec_name", "profile", "level", "width",
                                                  "height", "pix_fmt", "r_frame_rate",
                                                  "channels", "sample_rate")})
    with open(out, "rb") as f:
        head = f.read(40000)
    print("  moov@%d  mdat@%d  ftyp@%d" % (head.find(b"moov"), head.find(b"mdat"), head.find(b"ftyp")))

    wav = os.path.join(tmp, "tone.wav")
    run([ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-ar", "48000", wav])
    mp3 = os.path.join(tmp, "tone.mp3")
    rc, err = convert_audio(ffmpeg, wav, mp3, 320, 44100)
    print("\n音频转换 rc=%s 大小 %.0f KB" % (rc, os.path.getsize(mp3) / 1024 if os.path.isfile(mp3) else 0))
    a = probe(ffprobe, mp3)
    for s in a.get("streams", []):
        print("  成品音频:", {k: s.get(k) for k in ("codec_name", "bit_rate", "sample_rate", "channels")})
    print("\n输出目录:", tmp)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--lagtest" in sys.argv:
        lagtest_and_show()
    else:
        try:
            build_gui()
        except Exception:
            errtxt = traceback.format_exc()
            try:
                log = os.path.join(os.environ.get("TEMP", "."), "psp_ui_error.log")
                with open(log, "w", encoding="utf-8") as f:
                    f.write(errtxt)
            except Exception:
                pass
            try:
                import tkinter.messagebox as mb
                mb.showerror(APP_NAME, errtxt[-800:])
            except Exception:
                print(errtxt)
