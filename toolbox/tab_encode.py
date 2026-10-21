# -*- coding: utf-8 -*-
"""⑦ 视频常用压制 —— FFmpeg 硬件编码（NVENC 优先 / 无则 QSV）+ 内嵌字幕硬烧 + 自动归档

压制参数参考 ShanaEncoder 的常用设置：
    AV1  (NVENC)   -preset p1      -cq 36
    H.265(NVENC)   -preset p4      -cq 29
    H.265(QSV)     -preset medium  -global_quality 24

字幕样式同样复刻 ShanaEncoder：微软雅黑 / 字号 15 / 轮廓 2 / 阴影 2 /
左右边距 10 / 底边距 20 / 强制 ASS 样式 / 居中底部；
滤镜顺序固定为「字幕在前、旋转在后」（-noautorotate + transpose）。

文件归档：压制成功把源文件移到 success/，失败移到 error/；
输出命名 原文件名.crf<质量值>.mp4。
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .common import (clean_path, clear_log, get_unique_path, make_logger,
                     make_num_box, natural_key)

# ============================================================
# 常量
# ============================================================

VIDEO_EXTS = (".mp4", ".flv", ".ts", ".mkv", ".mov", ".avi", ".wmv", ".m4v",
              ".webm", ".m2ts", ".mts", ".mpg", ".mpeg", ".3gp", ".rmvb",
              ".rm", ".vob", ".f4v", ".asf", ".ogv")

# 本程序自己的输出名：xxx.crf24.mp4 / xxx.crf24_1.mp4
OUTPUT_NAME_RE = re.compile(r"\.crf\d+(?:_\d+)?\.mp4$", re.I)

CODEC_AV1 = "av1"
CODEC_HEVC = "hevc"
CODEC_LABELS = {CODEC_AV1: "AV1", CODEC_HEVC: "H.265 (HEVC)"}

# 参考 ShanaEncoder：AV1(NVENC) cq36 / AV1(QSV) gq34，
# H.265(NVENC) cq29 / H.265(QSV) gq24
DEFAULT_QUALITY = {CODEC_AV1: 36, CODEC_HEVC: 29}   # 初始值，探测到编码器后按 default_quality() 覆盖
HD_CRF = 18                  # 高清模式：原分辨率，AV1 / H.265 都用 crf/cq 18
SIXFORTY_EDGE = 640          # 640p 模式：最长边限制 640
MODE_REMUX = "copy"          # 极速转封装：不解码重编码，-c copy 直接换容器成 mp4（flv/mkv/ts… → mp4）

# ShanaEncoder 字幕样式（强制 ASS 样式）
SUB_FORCE_STYLE = ("FontName=Microsoft YaHei,FontSize=15,Outline=2,Shadow=2,"
                   "MarginL=10,MarginR=10,MarginV=20,Alignment=2")

NVENC_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]
QSV_PRESETS = ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
X265_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow"]
SVT_PRESETS = [str(i) for i in range(0, 14)]
# libaom 用 -cpu-used 控制速度（0 最慢最好，8 最快）；rav1e 用 -speed（0 最慢最好，10 最快）
AOM_CPU_USED = [str(i) for i in range(0, 9)]
RAV1E_SPEEDS = [str(i) for i in range(0, 11, 2)]

# mp4 里可以直接 copy 的音频编码，其余转 AAC
MP4_AUDIO_OK = {"aac", "mp3", "mp2", "ac3", "eac3", "alac"}

# 位图字幕无法硬烧（PGS/VobSub 等），只能跳过
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt",
                   "text", "microdvd", "subviewer", "subviewer1", "jacosub",
                   "sami", "realtext", "stl", "vplayer", "pjs", "mpl2",
                   "eia_608", "eia_708", "dvb_teletext"}

_ENCODER_CHAIN = {
    CODEC_AV1: ("av1_nvenc", "av1_qsv", "libsvtav1", "libaom-av1", "librav1e"),
    CODEC_HEVC: ("hevc_nvenc", "hevc_qsv", "libx265"),
}

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _base_dir():
    """exe 所在目录（打包后）或项目根目录（源码运行）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# MediaInfo（可选）：优先用它的 FrameRate_Mode 精确判断 CFR / VFR
#   · 打包时 MediaInfo.dll + LIBCURL.DLL 一起放进 bin/mediainfo 并随 exe 解压到 _MEIPASS
#   · 找不到 DLL 时自动回退 ffprobe（比例 + 逐包 PTS）判断，功能不受影响
# ============================================================
_MI_LOCK = threading.Lock()
_MI_DLL = None
_MI_TRIED = False


def _mediainfo_dll_path():
    roots = [_base_dir()]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(meipass)
    for root in roots:
        for rel in (("bin", "mediainfo", "MediaInfo.dll"), ("MediaInfo.dll",)):
            p = os.path.join(root, *rel)
            if os.path.isfile(p):
                return p
    return ""


def _load_mediainfo():
    """懒加载 MediaInfo.dll，返回可用句柄；不可用返回 None"""
    global _MI_DLL, _MI_TRIED
    with _MI_LOCK:
        if _MI_TRIED:
            return _MI_DLL
        _MI_TRIED = True
        path = _mediainfo_dll_path()
        if not path:
            return None
        try:
            d = os.path.dirname(path)
            # 让 MediaInfo.dll 的依赖（LIBCURL.DLL 等）能从同目录加载
            try:
                os.add_dll_directory(d)
            except Exception:
                pass
            lib = ctypes.WinDLL(path)
            lib.MediaInfo_New.restype = ctypes.c_void_p
            lib.MediaInfo_Open.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            lib.MediaInfo_Open.restype = ctypes.c_size_t
            lib.MediaInfo_Get.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_wchar_p, ctypes.c_int, ctypes.c_int]
            lib.MediaInfo_Get.restype = ctypes.c_wchar_p
            lib.MediaInfo_Close.argtypes = [ctypes.c_void_p]
            lib.MediaInfo_Delete.argtypes = [ctypes.c_void_p]
            _MI_DLL = lib
        except Exception:
            _MI_DLL = None
        return _MI_DLL


def _mediainfo_dll_mode(path):
    """用 MediaInfo.dll 读 FrameRate_Mode：返回 'VFR' / 'CFR'；失败返回 None。"""
    lib = _load_mediainfo()
    if not lib:
        return None
    h = None
    try:
        h = lib.MediaInfo_New()
        if not h or not lib.MediaInfo_Open(h, path):
            return None
        v = lib.MediaInfo_Get(h, 1, 0, "FrameRate_Mode", 1, 0)   # Stream_Video=1, Info_Text=1
        if not v:
            return None
        u = v.strip().upper()
        if "VFR" in u or "VARIABLE" in u:
            return "VFR"
        if "CFR" in u or "CONSTANT" in u:
            return "CFR"
        return None
    except Exception:
        return None
    finally:
        if h:
            try:
                lib.MediaInfo_Close(h)
                lib.MediaInfo_Delete(h)
            except Exception:
                pass


def _mediainfo_cli_path():
    """找 MediaInfo 命令行版（可选；放进 bin/mediainfo 或 exe 同目录即启用）"""
    roots = [_base_dir()]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(meipass)
    for root in roots:
        for rel in (("bin", "mediainfo", "MediaInfo.exe"), ("MediaInfo.exe",)):
            p = os.path.join(root, *rel)
            if os.path.isfile(p):
                return p
    return ""


def mediainfo_cli_frame_rate_mode(path):
    """用 MediaInfo.exe --Inform 读 FrameRate_Mode；没有 CLI 或失败返回 None。"""
    exe = _mediainfo_cli_path()
    if not exe:
        return None
    try:
        r = _run([exe, "--Inform=Video;%FrameRate_Mode%", path], timeout=30)
        u = (r.stdout or b"").decode("utf-8", "ignore").strip().upper()
        if "VFR" in u or "VARIABLE" in u:
            return "VFR"
        if "CFR" in u or "CONSTANT" in u:
            return "CFR"
    except Exception:
        pass
    return None


def mediainfo_frame_rate_mode(path):
    """MediaInfo 的 FrameRate_Mode：优先 DLL，其次 CLI，都不可用返回 None（由 ffprobe 兜底）。"""
    mode = _mediainfo_dll_mode(path)
    if mode is not None:
        return mode
    return mediainfo_cli_frame_rate_mode(path)


FFMPEG_CONFIG_PATH = os.path.join(_base_dir(), "ffmpeg_config.json")


# ============================================================
# FFmpeg 定位与检测
# ============================================================

def _run(cmd, timeout=30, cwd=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout, cwd=cwd, creationflags=_NO_WINDOW)


def load_ffmpeg_hint():
    """读取上次手动指定的 ffmpeg 路径"""
    try:
        with open(FFMPEG_CONFIG_PATH, "r", encoding="utf-8") as f:
            return str(json.load(f).get("ffmpeg", "") or "")
    except Exception:
        return ""


def save_ffmpeg_hint(path):
    try:
        with open(FFMPEG_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"ffmpeg": path}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _exe_pair(dir_path):
    """在目录里成对找 ffmpeg / ffprobe"""
    for name in ("ffmpeg.exe", "ffmpeg"):
        ff = os.path.join(dir_path, name)
        if os.path.isfile(ff):
            stem = name.rsplit(".", 1)[0]
            fp = os.path.join(dir_path, stem.replace("ffmpeg", "ffprobe") +
                              (".exe" if name.endswith(".exe") else ""))
            return os.path.abspath(ff), (os.path.abspath(fp) if os.path.isfile(fp) else "")
    return None, None


def _search_roots():
    """ffmpeg 可能的存放位置：exe 目录 / 打包解压目录 / 程序目录"""
    roots = [_base_dir()]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(meipass)
    dirs = []
    for root in roots:
        dirs += [root, os.path.join(root, "bin", "ffmpeg"), os.path.join(root, "ffmpeg")]
    return dirs


def locate_ffmpeg(hint=""):
    """按「手动指定 → 程序目录 → bin/ffmpeg → PATH」找 ffmpeg，返回 (ffmpeg, ffprobe)"""
    dirs = []
    hint = ((hint or "").strip().strip('"')
            or load_ffmpeg_hint())          # 没传就用手动保存过的路径
    if hint:
        dirs.append(hint if os.path.isdir(hint) else os.path.dirname(hint))
    dirs += _search_roots()

    for d in dirs:
        if not d:
            continue
        ff, fp = _exe_pair(d)
        if ff:
            if not fp:      # ffprobe 不在同目录时退回 PATH
                fp = shutil.which("ffprobe") or ""
            return ff, fp

    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            ff, fp = _exe_pair(os.path.dirname(p))
            return ff, fp or (shutil.which("ffprobe") or "")
    return None, None


_DR_SUPPORT = None
_DV_SUPPORT = None


def supports_display_rotation(ffmpeg):
    """ffmpeg 是否支持 -display_rotation（6.x 起；老版本只能退回 -noautorotate）"""
    global _DR_SUPPORT
    if _DR_SUPPORT is not None:
        return _DR_SUPPORT
    try:
        r = _run([ffmpeg, "-hide_banner", "-v", "error", "-display_rotation", "0",
                  "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1", "-f", "null", "-"],
                 timeout=40)
        _DR_SUPPORT = r.returncode == 0
    except Exception:
        _DR_SUPPORT = False
    return _DR_SUPPORT


def supports_libx265_dovi(ffmpeg):
    """libx265 是否支持杜比视界 RPU 编码（需 x265 ≥3.5 且 -dolbyvision 选项存在）。

    注意：x265 写 RPU 还要求开启 HRD/VBV，参数在 video_encode_args 里一并给。"""
    global _DV_SUPPORT
    if _DV_SUPPORT is not None:
        return _DV_SUPPORT
    try:
        r = _run([ffmpeg, "-hide_banner", "-h", "encoder=libx265"], timeout=30)
        out = (r.stdout or b"").decode("utf-8", "ignore")
        _DV_SUPPORT = "-dolbyvision" in out
    except Exception:
        _DV_SUPPORT = False
    return _DV_SUPPORT


def dv_label(info):
    """杜比视界的简短标识，如 DV8.4（profile 8 + 兼容 id 4=HLG）；不是 DV 返回空串。"""
    dv = (info or {}).get("dovi") or {}
    if not dv:
        return ""
    prof = dv.get("profile") or 0
    compat = dv.get("bl_compat") or 0
    if prof and compat:
        return f"DV{prof}.{compat}"
    return f"DV{prof}" if prof else "DV"


def ffmpeg_version(ffmpeg):
    try:
        r = _run([ffmpeg, "-hide_banner", "-version"], timeout=20)
        out = (r.stdout or b"").decode("utf-8", "ignore").splitlines()
        return out[0].strip() if out else ""
    except Exception:
        return ""


def ffmpeg_ready():
    """启动时快速检测：返回 (是否可用, 提示文本)"""
    ff, fp = locate_ffmpeg(load_ffmpeg_hint())
    if not ff:
        return False, "未找到 ffmpeg"
    if not fp:
        return False, "找到 ffmpeg，但同目录缺少 ffprobe"
    return True, ffmpeg_version(ff)


def list_encoder_text(ffmpeg):
    try:
        r = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=25)
        return (r.stdout or b"").decode("utf-8", "ignore")
    except Exception:
        return ""


def encoder_works(ffmpeg, enc):
    """真跑 0.2 秒黑帧：列出编码器 ≠ 真的能用（没显卡 / 驱动旧都会失败）"""
    try:
        r = _run([ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi",
                  "-i", "color=black:s=256x256:d=0.2", "-c:v", enc,
                  "-f", "null", "-"], timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def detect_encoders(ffmpeg, log=None):
    """探测可用编码器：每个编码格式按 NVENC → QSV → 软件 的顺序找第一个真能跑的。
    返回 {'av1': 编码器名|None, 'hevc': 编码器名|None, 'detail': {...}}"""
    text = list_encoder_text(ffmpeg)
    result = {"detail": {}}
    for codec, chain in _ENCODER_CHAIN.items():
        picked = None
        for enc in chain:
            if enc not in text:
                result["detail"][enc] = "未编译"
                continue
            if log:
                log(f"  检测编码器 {enc} ...")
            ok = encoder_works(ffmpeg, enc)
            result["detail"][enc] = "可用" if ok else "不可用"
            if ok:
                picked = enc
                break
        result[codec] = picked
    return result


def is_hardware(enc):
    """NVENC / QSV 算硬件编码，lib* 是 CPU 软件编码"""
    return bool(enc) and (enc.endswith("_nvenc") or enc.endswith("_qsv"))


def encoder_label(enc):
    if not enc:
        return "无"
    if enc.endswith("_nvenc"):
        return f"NVENC（{enc}）"
    if enc.endswith("_qsv"):
        return f"QSV（{enc}）"
    return f"软件（{enc}）"


def preset_values(enc):
    if not enc:
        return ["无"]
    if enc.endswith("_nvenc"):
        return NVENC_PRESETS
    if enc.endswith("_qsv"):
        return QSV_PRESETS
    if enc == "libsvtav1":
        return SVT_PRESETS
    if enc == "libaom-av1":
        return AOM_CPU_USED
    if enc == "librav1e":
        return RAV1E_SPEEDS
    return X265_PRESETS


def default_preset(enc, codec):
    """参考 ShanaEncoder 的默认 preset"""
    if not enc:
        return "无"
    if enc.endswith("_nvenc"):
        return "p1" if codec == CODEC_AV1 else "p4"
    if enc.endswith("_qsv"):
        return "medium"
    if enc == "libsvtav1":
        return "8"          # SVT-AV1：数字越大越快
    if enc == "libaom-av1":
        return "6"
    if enc == "librav1e":
        return "8"
    return "medium"


def best_preset(enc):
    """最高画质那一档（也最慢）：
        QSV veryslow ／ libx265 veryslow
        SVT-AV1 0 ／ libaom cpu-used 0 ／ rav1e speed 0
        NVENC p7（注意：NVENC preset 语义相反，p1 最快最差、p7 最慢最好）"""
    if not enc:
        return "无"
    if enc.endswith("_nvenc"):
        return "p7"
    if enc.endswith("_qsv"):
        return "veryslow"
    if enc == "libsvtav1":
        return "0"
    if enc == "libaom-av1":
        return "0"
    if enc == "librav1e":
        return "0"
    return "veryslow"


def default_quality(enc, codec):
    """参考 ShanaEncoder 的默认质量（QSV 与 NVENC 的取值不同）：
        Intel QSV：AV1 global_quality 34 / H.265 global_quality 24
        NVIDIA NVENC：AV1 cq 36 / H.265 cq 29
        软件编码沿用 NVENC 的数值（crf 体系接近）"""
    if codec == CODEC_AV1:
        return 34 if (enc or "").endswith("_qsv") else 36
    return 24 if (enc or "").endswith("_qsv") else 29


# ============================================================
# 媒体信息探测
# ============================================================

def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _parse_ratio(v):
    """avg_frame_rate 形如 24000/1001"""
    try:
        num, _, den = str(v).partition("/")
        den_f = float(den) if den else 1.0
        return round(float(num) / den_f, 3) if den_f else 0.0
    except Exception:
        return 0.0


def _detect_vfr_by_pts(ffprobe, path, max_seconds=120):
    """用逐包显示时间戳（PTS）的间隔方差判断 VFR。

    只读 packet 元数据、不解码，能抓到 `r_frame_rate` 与 `avg_frame_rate` 很接近的
    小幅可变帧率（例如手机视频 28.571~30fps、平均 29.97，两者相对差只有 0.1%，
    单看 r/avg 会误判成 CFR）。最多读取开头 max_seconds 秒的包，避免大文件太慢。
    返回 True/False；数据不足或失败返回 None（调用方沿用 r/avg 的判断）。"""
    try:
        r = _run([ffprobe, "-v", "error", "-select_streams", "v:0",
                  "-read_intervals", f"%+{int(max_seconds)}",
                  "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                 timeout=60)
    except Exception:
        return None
    vals = []
    for line in (r.stdout or b"").decode("utf-8", "ignore").splitlines():
        s = line.strip().strip(",")
        if not s or s.upper() in ("N/A", "NAN"):
            continue
        try:
            vals.append(float(s))
        except ValueError:
            pass
    if len(vals) < 8:
        return None
    vals.sort()
    deltas = [b - a for a, b in zip(vals, vals[1:]) if b - a > 0]
    if len(deltas) < 6:
        return None
    med = sorted(deltas)[len(deltas) // 2]
    if med <= 0:
        return None
    # 偏离中位帧间隔超过 2% 的帧占比 >0.5% 即视为 VFR。
    # 实测手机 VFR（28.571~30fps、平均 29.97）只有约 1% 的帧是慢帧，所以占比阈值不能太高；
    # 而 CFR 的 NTSC 取整偏差 <0.1%，达不到 2% 的单帧偏差，不会误判。
    dev = sum(1 for d in deltas if abs(d - med) > med * 0.02) / len(deltas)
    return dev > 0.005


def _chroma_of(pix_fmt):
    """从 ffmpeg 像素格式名解析色度：4:2:0 / 4:2:2 / 4:4:4 / 灰阶。"""
    pf = (pix_fmt or "").lower()
    if "444" in pf:
        return 444
    if "422" in pf:
        return 422
    if "400" in pf or "gray" in pf:
        return 400
    return 420


def _parse_master_display(sd):
    """把 ffprobe 的 Mastering display metadata 转成 x265 -master_display 字符串。
       ffprobe 给的 xy 是 "n/50000"、亮度是 "n/10000"，正好对应 x265 的整数刻度，取分子即可。"""
    try:
        def _frac(v):
            v = str(v).strip()
            if "/" in v:
                return int(v.split("/")[0])
            return int(float(v))
        rx, ry = _frac(sd.get("red_x")), _frac(sd.get("red_y"))
        gx, gy = _frac(sd.get("green_x")), _frac(sd.get("green_y"))
        bx, by = _frac(sd.get("blue_x")), _frac(sd.get("blue_y"))
        wx, wy = _frac(sd.get("white_point_x")), _frac(sd.get("white_point_y"))
        lmax, lmin = _frac(sd.get("max_luminance")), _frac(sd.get("min_luminance"))
        if None not in (rx, ry, gx, gy, bx, by, wx, wy, lmax, lmin):
            return (f"G({gx},{gy})B({bx},{by})R({rx},{ry})"
                    f"WP({wx},{wy})L({lmax},{lmin})")
    except Exception:
        pass
    return None


def _parse_max_cll(sd):
    """把 ffprobe 的 Content light level metadata 转成 x265 -max_cll 字符串（"max,avg"）。"""
    try:
        mc, ma = int(sd.get("max_content")), int(sd.get("max_average"))
        return f"{mc},{ma}"
    except Exception:
        return None


def probe_media(ffprobe, path):
    """用 ffprobe 读视频信息；失败返回 None。"""
    if not ffprobe:
        return None
    try:
        r = _run([ffprobe, "-v", "error", "-print_format", "json",
                  "-show_streams", "-show_format", path], timeout=60)
        data = json.loads((r.stdout or b"").decode("utf-8", "ignore") or "{}")
    except Exception:
        return None

    streams = data.get("streams") or []
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not vstream:
        return None
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    subs = []
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        subs.append({
            "index": _to_int(s.get("index")),
            "codec": (s.get("codec_name") or "").lower(),
            "lang": (tags.get("language") or "").lower(),
            "title": tags.get("title") or "",
            "default": bool(disp.get("default")),
            "forced": bool(disp.get("forced")),
        })

    width = _to_int(vstream.get("width"))
    height = _to_int(vstream.get("height"))
    pix_fmt = (vstream.get("pix_fmt") or "").lower()
    chroma = _chroma_of(pix_fmt)
    bits = _to_int(vstream.get("bits_per_raw_sample"))
    if not bits:
        m = re.search(r"p(\d{2})(?:le|be)?$|yuv4\d\dp(\d{1,2})", pix_fmt)
        if m:
            bits = int(next(g for g in m.groups() if g))
        elif "10" in pix_fmt:
            bits = 10
        elif "12" in pix_fmt:
            bits = 12
        else:
            bits = 8

    # 旋转：ffprobe 的 side_data rotation（手机竖拍常见 -90）
    rotation = 0
    for sd in vstream.get("side_data_list") or []:
        if "rotation" in sd:
            rotation = _to_int(sd.get("rotation"))
            break
    tags = vstream.get("tags") or {}
    if not rotation and tags.get("rotate"):
        rotation = _to_int(tags.get("rotate"))
    rotation = rotation % 360
    if rotation > 180:
        rotation -= 360

    # HDR10 静态元数据（母版显示色域 + 内容亮度），libx265 可原样写回；
    # 取不到就留空（软/硬件编码都不强求）。
    master_display = None
    max_cll = None
    # 杜比视界（Dolby Vision）：ffprobe 把 dvcC/dvvC 配置盒报成 DOVI configuration record。
    # profile 8.4 = HLG 兼容（手机 HDR 视频常见），8.1 = HDR10 兼容。
    dovi = None
    for sd in vstream.get("side_data_list") or []:
        st = sd.get("side_data_type", "")
        if st == "Mastering display metadata" and master_display is None:
            master_display = _parse_master_display(sd)
        elif st == "Content light level metadata" and max_cll is None:
            max_cll = _parse_max_cll(sd)
        elif st == "DOVI configuration record" and dovi is None:
            dovi = {
                "profile": _to_int(sd.get("dv_profile")),
                "level": _to_int(sd.get("dv_level")),
                "bl_compat": _to_int(sd.get("dv_bl_signal_compatibility_id")),
            }

    color_trc = (vstream.get("color_transfer") or "").lower()
    hdr = color_trc in ("smpte2084", "arib-std-b67")

    fmt = data.get("format") or {}
    duration = _to_float(fmt.get("duration")) or _to_float(vstream.get("duration"))

    # CFR / VFR 判断：ffprobe 的 r_frame_rate 是「最大/基准帧率」，avg_frame_rate 是平均帧率。
    # 两者相差 >1% 视为可变帧率（手机录制的 VFR 常见 r 远大于 avg，例如 r=120、avg≈27）。
    # 恒帧率（含 29.97 这种 NTSC 取整）差异通常在 0.1% 以内，不会被误判。
    avg_fps = _parse_ratio(vstream.get("avg_frame_rate"))
    r_fps = _parse_ratio(vstream.get("r_frame_rate"))
    vfr = False
    if r_fps > 0 and avg_fps > 0:
        vfr = abs(r_fps - avg_fps) / max(r_fps, avg_fps) > 0.01
    # 优先用 MediaInfo 的 FrameRate_Mode（与 MediaInfo 里显示的一致）；
    # 没带 DLL 时回退 ffprobe：先看 r/avg，再用逐包 PTS 方差复核小幅 VFR。
    mi_mode = mediainfo_frame_rate_mode(path)
    if mi_mode is not None:
        vfr = mi_mode == "VFR"
    elif not vfr:
        pts_vfr = _detect_vfr_by_pts(ffprobe, path)
        if pts_vfr is not None:
            vfr = pts_vfr

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "vcodec": (vstream.get("codec_name") or "?").lower(),
        "pix_fmt": pix_fmt,
        "bit_depth": bits,
        "chroma": chroma,
        "hdr": hdr,
        "master_display": master_display,
        "max_cll": max_cll,
        "dovi": dovi,
        "rotation": rotation,
        "fps": avg_fps,
        "r_fps": r_fps,
        "vfr": vfr,
        "subs": subs,
        "has_audio": bool(astreams),
        "audio_codec": (astreams[0].get("codec_name") or "").lower() if astreams else "",
        # 逐条音轨的编码（顺序与 -map 0:a? 映射出的输出轨一致），用于逐轨决定 copy / 转 AAC
        "audio_codecs": [(s.get("codec_name") or "").lower() for s in astreams],
        "audio_all_ok": all((s.get("codec_name") or "").lower() in MP4_AUDIO_OK
                            for s in astreams) if astreams else True,
        "color_primaries": vstream.get("color_primaries") or "",
        "color_trc": vstream.get("color_transfer") or "",
        "color_space": vstream.get("color_space") or "",
    }


def display_size(info):
    """按旋转后的显示方向返回 (宽, 高)"""
    w, h = info["width"], info["height"]
    if info["rotation"] in (90, -90, 270, -270):
        return h, w
    return w, h


# ============================================================
# 参数计算
# ============================================================

def _even(n):
    n = int(round(n))
    return max(2, n - (n % 2))


def calc_target_size(width, height, long_edge):
    """最长边限制到 long_edge（不放大）；返回 (宽, 高) 或 None"""
    if width <= 0 or height <= 0:
        return None
    if max(width, height) <= long_edge:
        return None
    edge = long_edge if long_edge % 2 == 0 else long_edge - 1
    if width >= height:                     # 横屏：限宽
        return edge, _even(height * edge / width)
    return _even(width * edge / height), edge   # 竖屏：限高


def rotation_filter(rotation):
    """旋转滤镜（实测与 ffmpeg autorotate 完全一致）"""
    if rotation in (90, -270):
        return "transpose=2"
    if rotation in (-90, 270):
        return "transpose=1"
    if abs(rotation) == 180:
        return "hflip,vflip"
    return ""


def pick_subtitle(info):
    """挑一条要硬烧的字幕：优先中文 → 默认轨 → 第一条"""
    subs = info.get("subs") or []
    text_subs = [s for s in subs if s["codec"] in TEXT_SUB_CODECS]
    if not text_subs:
        return None
    zh_keys = ("zh", "chi", "zho", "cn", "chs", "cht", "sc", "tc")

    def is_zh(s):
        return (s["lang"] in zh_keys or s["lang"].startswith("zh")
                or any(k in s["title"] for k in ("中文", "简体", "繁体", "中字", "chs", "cht")))

    for s in text_subs:
        if is_zh(s):
            return s
    for s in text_subs:
        if s["default"]:
            return s
    return text_subs[0]


def extract_subtitle(ffmpeg, src, sub_index, work_dir):
    """把内嵌字幕导成 .ass（工作目录内的相对文件名，避免滤镜里转义盘符）"""
    out_name = "sub.ass"
    out_path = os.path.join(work_dir, out_name)
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass
    try:
        _run([ffmpeg, "-y", "-v", "error", "-i", src,
              "-map", f"0:{sub_index}", "-c:s", "ass", out_name],
             timeout=300, cwd=work_dir)
    except Exception:
        return None
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return out_name
    return None


def quality_tag(enc, quality):
    """日志里的质量参数写法（三种编码器体系不同，别一律写 crf）：
       QSV 用 global_quality、NVENC 用 cq、软件 x265/x264 用 crf。"""
    e = enc or ""
    if e.endswith("_qsv"):
        return f"gq{quality}"
    if e.endswith("_nvenc"):
        return f"cq{quality}"
    return f"crf{quality}"


def select_pix_fmt(info, mode, enc, keep_dv=False):
    """按模式与编码器决定输出像素格式：
        640p（最长边 640，输出短边 360 → 俗称 360p）→ 一律 8bit 4:2:0，不保留高位深；
        高清 / 自定义 → 保留源的色度（420/422/444）与位深（8/10bit），传出与传入一致。
      硬件编码器（NVENC / QSV）只稳定支持 4:2:0，强制 420；软件编码器完整保留源色度。
      杜比视界（保留 DV 时）：Profile 8 规定 BL 必须是 10bit 4:2:0，此时即使 640p 也输出 10bit。"""
    if keep_dv and info.get("dovi"):
        return "yuv420p10le"
    if mode == "640":
        return "nv12" if enc.endswith("_qsv") else "yuv420p"
    is_10 = info["bit_depth"] > 8 or info["hdr"]
    if enc.endswith("_nvenc") or enc.endswith("_qsv"):
        # 硬件只做 4:2:0
        if enc.endswith("_qsv"):
            return "p010le" if is_10 else "nv12"
        return "p010le" if is_10 else "yuv420p"
    chroma = info.get("chroma", 420)
    base = "yuv{}p".format(chroma)
    if is_10:
        base += "10le"
    return base


def video_encode_args(enc, codec, quality, preset, pix_fmt, dovi=None, keep_dv=False,
                      dims=None):
    """按编码器生成视频参数（cq / global_quality / crf 三种质量体系）。
       pix_fmt 由 select_pix_fmt 决定（保留源色度与位深；640p 强制 8bit 4:2:0）。
       dovi / keep_dv：源是杜比视界且要保留元数据时，libx265 用 -dolbyvision 1 写回 RPU。
       x265 的杜比视界必须开 HRD/VBV，否则报 "Dolby Vision requires VBV settings" 直接打不开编码器。"""
    args = ["-c:v", enc]
    if enc.endswith("_nvenc"):
        # ShanaEncoder 同款：VBR + cq，b:v 0 表示纯质量模式
        args += ["-preset", str(preset), "-rc", "vbr",
                 "-cq", str(quality), "-b:v", "0"]
        if codec == CODEC_HEVC:
            args += ["-profile:v", "main10" if "10" in pix_fmt else "main"]
        args += ["-pix_fmt", pix_fmt]
    elif enc.endswith("_qsv"):
        args += ["-preset", str(preset), "-global_quality", str(quality)]
        if codec == CODEC_HEVC:
            args += ["-profile:v", "main10" if "10" in pix_fmt else "main"]
        # QSV 只吃 nv12 / p010le，喂 yuv420p 会多打印一条自动转换警告
        args += ["-pix_fmt", pix_fmt]
    elif enc == "libsvtav1":
        args += ["-preset", str(preset), "-crf", str(quality),
                 "-pix_fmt", pix_fmt]
    elif enc == "libaom-av1":
        # libaom 不看 -preset，速度由 -cpu-used 控制（0 最慢最好）
        cpu = str(preset) if str(preset).isdigit() else "6"
        args += ["-cpu-used", cpu, "-crf", str(quality), "-b:v", "0",
                 "-pix_fmt", pix_fmt]
    elif enc == "librav1e":
        # rav1e 的速度参数是 -speed（0 最慢最好）
        spd = str(preset) if str(preset).isdigit() else "8"
        args += ["-speed", spd, "-qp", str(quality),
                 "-pix_fmt", pix_fmt]
    else:   # libx265
        args += ["-preset", str(preset), "-crf", str(quality),
                 # profile 由 libx265 按 pix_fmt 自动选（main / main10 / main422-10 / main444-10 …）
                 "-pix_fmt", pix_fmt]
        if dovi and keep_dv:
            # 杜比视界：x265 直接读解码器给的 DOVI 帧元数据，把 RPU 写回码流（NAL 62）。
            # 必须显式开 HRD + VBV，否则 x265 报 "Dolby Vision requires VBV settings" 拒绝启动。
            # VBV 上限取得比较宽松（按像素率估算），正常 CRF 编码不会撞到，不影响画质。
            vbv = _dv_vbv_kbps(*(dims or (1920, 1080, 30)))
            args += ["-dolbyvision", "1",
                     "-x265-params", f"hrd=1:vbv-maxrate={vbv}:vbv-bufsize={vbv * 2}"]
    return args


def _dv_vbv_kbps(w, h, fps):
    """按输出像素率估一个宽松的 VBV 上限（kbps），只用于满足 x265 的 DV/HRD 要求。"""
    try:
        rate = float(w) * float(h) * float(fps or 30) * 0.30 / 1000.0
    except Exception:
        rate = 20000.0
    return int(max(3000, min(200000, round(rate))))


def sub_style(width, height):
    """按最终画面尺寸算字幕样式：竖屏 / 小画幅自动缩字号 + 智能换行，保证一行放得下、不超出屏幕。

    字号以 640 宽时 15px 为基准按比例缩放（12–26 封顶），左右边距取宽度 2%、
    下边距取高度 3%，描边阴影随字号走。"""
    w = max(1, int(width))
    h = max(1, int(height))
    size = max(12, min(26, round(w * 15 / 640)))
    mlr = max(6, min(40, round(w * 0.02)))
    mv = max(10, min(60, round(h * 0.03)))
    edge = max(1, min(3, round(size / 7)))
    return (f"FontName=Microsoft YaHei,FontSize={size},Outline={edge},Shadow={edge},"
            f"MarginL={mlr},MarginR={mlr},MarginV={mv},Alignment=2,WrapStyle=0")


def build_filters(info, target, sub_name):
    """滤镜顺序：旋转 → 缩放 → 字幕。

    字幕必须放最后，渲染在「旋转、缩放之后」的最终画面上。放前面的话：
      · 手机竖拍视频是像素横存 + rotation 标记，字幕会跟着画面一起转 90°，
        文字长度方向变成竖直方向，直接超出屏幕；
      · 640p 缩放会把提前烧好的字一起缩成看不清的小字。
    放最后则按最终画幅排版，字号还能跟着画幅自适应。"""
    filters = []
    rot = rotation_filter(info["rotation"])
    if rot:
        filters.append(rot)
    if target:
        # 缩放算法显式用 bicubic：ffmpeg scale 默认也是 bicubic（SWS_BICUBIC），
        # 这里写明确与 ShanaEncoder 默认缩放滤镜对齐，避免不同 ffmpeg 版本/滤镜链默认差异。
        filters.append(f"scale={target[0]}:{target[1]}:flags=bicubic")
    if sub_name:
        dw, dh = display_size(info)
        fw, fh = target if target else (dw, dh)
        filters.append(f"subtitles={sub_name}:force_style='{sub_style(fw, fh)}'")
    return filters


# ============================================================
# 压制任务
# ============================================================

def resolve_plan(settings, info):
    """按「模式 + 编码格式」算出（编码器、质量、preset、目标尺寸）。
    编码格式在三种模式下都可以自由选 AV1 / H.265；质量与 preset 由模式决定：
      640p  → 该编码器的默认质量（QSV 34/24，NVENC 36/29）
      高清  → 固定 crf/cq 18，保持原分辨率
      自定义 → 用界面里填的质量与 preset"""
    mode = settings["mode"]
    codec = settings["codec"]
    enc = settings["encoders"].get(codec)
    if mode == MODE_REMUX:
        # 极速转封装：不挑编码器、不缩放、不动质量，只 -c copy 换容器
        return {"codec": None, "enc": None, "quality": None,
                "preset": None, "target": None, "remux": True}
    # 杜比视界：RPU 只能由 libx265 写回（QSV/NVENC/AV1 一律丢弃），
    # 所以在保留 DV 的前提下把编码器换成软件 x265（编 H.265 时）。
    keep_dv = bool(settings.get("keep_dv", False)) and bool(info.get("dovi"))
    if keep_dv and codec == CODEC_HEVC and supports_libx265_dovi(settings.get("ffmpeg")):
        enc = "libx265"
    else:
        keep_dv = False
    if mode == "640":
        quality = default_quality(enc, codec)
        preset = default_preset(enc, codec)
        dw, dh = display_size(info)
        target = calc_target_size(dw, dh, SIXFORTY_EDGE)
    elif mode == "hd":
        quality, preset, target = HD_CRF, best_preset(enc), None
    else:
        quality, preset, target = settings["quality"], settings["preset"], None
    return {"codec": codec, "enc": enc, "quality": quality,
            "preset": preset, "target": target, "keep_dv": keep_dv}


def build_command(settings, info, plan, src, dst, work_dir, sub_name,
                  progress_to="pipe:1"):
    ff = settings["ffmpeg"]
    show_console = bool(settings.get("show_console"))
    # 显示 ffmpeg 窗口时用 warning + 自带统计行；否则静默，只看我们解析的进度
    level = "warning" if show_console else "error"
    args = [ff, "-hide_banner", "-y", "-loglevel", level,
            "-progress", progress_to]
    if show_console:
        args.append("-stats")
    else:
        args.append("-nostats")
    if plan.get("remux"):
        # 极速转封装：不解码、不重编码、不加任何滤镜，画质音质原样搬进 mp4
        # （等价于 ffmpeg -i "%%a" -c copy -y "%%~na.mp4"，多了 faststart 便于在线播放）
        args += ["-i", src, "-c", "copy"]
        if info.get("dovi"):
            # RPU（NAL 62）本来就在码流里，-c copy 会原样带走；但 ffmpeg 默认不肯写
            # dvcC/dvvC 配置盒（提示 Requires -strict unofficial），没有它播放器就不认 DV。
            args += ["-strict", "unofficial", "-tag:v", "hvc1"]
        args += ["-movflags", "+faststart", "-f", "mp4", dst]
        return args
    filters = build_filters(info, plan["target"], sub_name)
    if info["rotation"]:
        # 旋转由我们自己用 transpose 处理（滤镜顺序：字幕 → 旋转）。
        # -display_rotation 0 让 ffmpeg 既不自动旋转、也不把旋转矩阵带到输出
        # （否则输出会被播放器"二次旋转"）；老版本没有该参数时退回 -noautorotate。
        if supports_display_rotation(ff):
            args += ["-display_rotation", "0"]
        else:
            args += ["-noautorotate"]
    args += ["-i", src, "-map", "0:v:0"]
    if info["has_audio"]:
        args += ["-map", "0:a?"]
    if filters:
        args += ["-vf", ",".join(filters)]
    dw, dh = display_size(info)
    fw, fh = plan["target"] if plan.get("target") else (dw, dh)
    pix_fmt = select_pix_fmt(info, settings["mode"], plan["enc"],
                             keep_dv=bool(plan.get("keep_dv")))
    args += video_encode_args(plan["enc"], plan["codec"], plan["quality"],
                              plan["preset"], pix_fmt,
                              dovi=info.get("dovi"), keep_dv=bool(plan.get("keep_dv")),
                              dims=(fw, fh, info.get("fps") or 30))
    # 只在真正输出 10bit 时才写 HDR 色彩标签（640p 强制 8bit 不写，否则 SDR 内容会被误标 HDR）
    if info["hdr"] and "10" in pix_fmt:
        args += ["-color_primaries", info["color_primaries"] or "bt2020",
                 "-color_trc", info["color_trc"] or "smpte2084",
                 "-colorspace", info["color_space"] or "bt2020nc"]
    # HDR10 静态元数据：仅 libx265 可靠写回（NVENC/QSV/AV1 不支持该参数），
    # 且只在输出 10bit 时附带；取不到就跳过（不影响压制成功）
    if plan["enc"] == "libx265":
        if info.get("master_display") and "10" in pix_fmt:
            args += ["-master_display", info["master_display"]]
        if info.get("max_cll") and "10" in pix_fmt:
            args += ["-max_cll", info["max_cll"]]
    if info["has_audio"]:
        # 逐条音轨单独判断：能直接进 mp4 的原样 copy，装不下的只把那一条转 AAC 192k，
        # 其余轨道不受影响（不再「一条不兼容就把所有轨道都转」）
        codecs = info.get("audio_codecs") or []
        if not codecs:
            # 老数据没有逐轨信息，退回整体判断
            args += ["-c:a", "copy"] if info.get("audio_all_ok", True) else \
                    ["-c:a", "aac", "-b:a", "192k"]
        else:
            for i, ac in enumerate(codecs):
                if ac in MP4_AUDIO_OK:
                    args += [f"-c:a:{i}", "copy"]
                else:
                    args += [f"-c:a:{i}", "aac", f"-b:a:{i}", "192k"]
    if info.get("vfr"):
        # 源是可变帧率（VFR）：显式透传时间戳，保证压制后仍是 VFR，
        # 不被 muxer 按平均帧率重采样成恒定帧率（CFR）。CFR 源不加，保持原时间戳。
        args += ["-fps_mode", "passthrough"]
        # 光有 passthrough 不够：编码器默认按「帧时长」量化时间基（libx265 也是），
        # 小幅抖动（如 28.571~30fps）会被抹平成 CFR。给一个更细的编码时间基
        # （90kHz，MPEG 标准时基）才能真正保留可变间隔。QSV/NVENC/libx265 都适用。
        args += ["-enc_time_base", "1/90000"]
    if plan.get("keep_dv"):
        # x265 已把 RPU 写进码流，但 mp4 muxer 默认不写 dvcC/dvvC 配置盒
        # （提示 Not writing 'dvcC'/'dvvC' box. Requires -strict unofficial），
        # 没有它播放器只当普通 HDR 播，不会点亮杜比视界。
        args += ["-strict", "unofficial", "-tag:v", "hvc1"]
    elif plan.get("codec") == CODEC_HEVC:
        # 非 DV 的 HEVC 也标 hvc1：QuickTime / Safari / iOS 只认 hvc1，hev1 无法播放
        args += ["-tag:v", "hvc1"]
    args += ["-movflags", "+faststart", "-f", "mp4", dst]
    return args


def output_name(src, quality, remux=False):
    stem = os.path.splitext(os.path.basename(src))[0]
    # 源本身就是我们压出来的成品（xxx.crf24.mp4 / xxx.crf24_1.mp4）时去掉后缀，避免 crf24.crf24 叠名字
    stem = re.sub(r"(?:\.crf\d+(?:_\d+)?)+$", "", stem) or stem
    if remux:
        # 极速转封装：原文件名.mp4（对应 %%~na.mp4），不带 crf 后缀
        return f"{stem}.mp4"
    return f"{stem}.crf{quality}.mp4"


ARCHIVE_DIRS = ("success", "error", "oversize")
OVERSIZE_DIR = "oversize"


def fmt_size(n):
    """字节数转可读体积：1536 → 1.5KB"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "0B"
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= scale:
            v = n / scale
            return f"{v:.1f}{unit}" if v < 100 else f"{v:.0f}{unit}"
    return f"{n:.0f}B"


def size_change(src_size, dst_size):
    """返回 (涨跌百分比, 是否变大)。任一为 0 时返回 (None, False)。"""
    if not src_size or not dst_size or src_size <= 0 or dst_size <= 0:
        return None, False
    pct = (dst_size - src_size) / float(src_size) * 100.0
    return pct, pct > 0


def in_archive_dir(path):
    """路径是否已经在 success/ 或 error/ 归档目录里（任意层级）"""
    try:
        parts = Path(os.path.abspath(path)).parts
    except Exception:
        return False
    return any(p.lower() in ARCHIVE_DIRS for p in parts)


def archive_tag(path):
    """归档位置的可读写法：success/ 、error/success/ （最多两层）"""
    parent = os.path.dirname(path)
    parts = []
    for _ in range(2):
        name = os.path.basename(parent)
        if name.lower() in ARCHIVE_DIRS:
            parts.insert(0, name)
            parent = os.path.dirname(parent)
        else:
            break
    return ("/".join(parts) + "/") if parts else ""


def _move_into(path, folder):
    """把文件移进 folder（同名自动加序号，绝不覆盖）；成功返回新路径，失败返回 ""。"""
    try:
        os.makedirs(folder, exist_ok=True)
        for _ in range(20):
            target = get_unique_path(Path(folder) / os.path.basename(path))
            try:
                shutil.move(path, str(target))
                return str(target)
            except FileExistsError:
                continue
            except OSError:
                # Windows 下目标已存在会抛 OSError（不是 FileExistsError），换个名字重试
                time.sleep(0.05)
                continue
        return ""
    except Exception:
        return ""


def archive_oversize(src, dst, out_dir):
    """成品比源还大：源文件与成品一起移到 <输出目录>/oversize/。

    返回 (源文件去向, 成品去向)，失败为空串。源/成品已在 oversize/ 里则原地不动。"""
    folder = os.path.join(out_dir, OVERSIZE_DIR)

    def inside(p):
        try:
            return os.path.basename(os.path.dirname(os.path.abspath(p))).lower() == OVERSIZE_DIR
        except Exception:
            return False

    dst_moved = ""
    if dst and os.path.isfile(dst):
        dst_moved = dst if inside(dst) else _move_into(dst, folder)
    src_moved = ""
    if src and os.path.isfile(src):
        src_moved = src if inside(src) else _move_into(src, folder)
    return src_moved, dst_moved


def archive_source(src, out_dir, ok):
    """把源文件归档到 success/ 或 error/（同名自动加序号，绝不覆盖）

    规则：
      · 普通目录里的文件 → 移到 <输出目录>/success 或 /error；
      · 已经在 error/ 里的文件再次压制成功 → 移到 error/success（重新救回来的分开放）；
      · 已经在 error/ 里又失败 → 原地不动，不再套 error/error；
      · 已经在 success/ 里再次成功 → 原地不动，不再套 success/success。
    """
    leaf = "success" if ok else "error"
    parent = os.path.dirname(os.path.abspath(src))
    if os.path.basename(parent).lower() == leaf:
        return ""                 # 已经在这个结果的目录里，别再套一层
    # 归档目录里「救回来」的文件（error/ 里这次压成功了）：就地归到该目录下的 success/，
    # 即 error/success/，而不是跳到输出目录的顶层 success/（add_paths 里就是这样提示用户的）
    base = parent if (ok and in_archive_dir(parent)
                      and os.path.basename(parent).lower() != OVERSIZE_DIR) else out_dir
    folder = os.path.join(base, leaf)
    return _move_into(src, folder)


def kill_process(proc):
    """结束进程；带控制台窗口时是 cmd.exe 包着 ffmpeg，要连子进程一起结束"""
    if not proc:
        return
    try:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW)
        if proc.poll() is None:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _quote_arg(a):
    """给命令行参数加引号（ffmpeg 参数里可能有空格 / 中文路径）"""
    a = str(a)
    if a and not a.startswith('"'):
        a = '"' + a + '"'
    return a


def console_cmdline(cmd, title="ffmpeg"):
    """拼出「在独立控制台里先打印 ffmpeg 命令、再执行它」的命令行。

    直接把整条命令交给 `cmd.exe /s /c`，不落地 .bat 文件：
    命令行是按 Unicode 传给 cmd 的，中文路径不会像 UTF-8 批处理那样
    被 cmd 按系统代码页（GBK）解码成乱码而整条命令失败。
    """
    line = " ".join(_quote_arg(a) for a in cmd)
    t = re.sub(r'[&|<>^"]', " ", str(title))[:120]
    # /v:on：用 !ERRORLEVEL! 取真实返回码（ffmpeg 打不开输入时返回 -2，
    # 用 if errorlevel 1 判断会漏掉负数，窗口就不会停留）。
    # set "RC=..." 必须带引号，否则值末尾会吃进一个空格，导致 "0 " 不等于 "0"，
    # 成功的任务也会误报「出错」。
    return ('cmd.exe /v:on /s /c "title ' + t +
            ' & echo === ffmpeg 命令 === & echo ' + line +
            ' & echo =================== & ' + line +
            ' & set "RC=!ERRORLEVEL!" & if not "!RC!"=="0" (echo.'
            ' & echo [ffmpeg 出错，返回码 !RC!，10 秒后关闭本窗口]'
            ' & ping -n 11 127.0.0.1 >nul & exit /b !RC!)"')


def _read_progress_file(path, offset, state):
    """增量读取 -progress 文件，返回 (新 offset, 是否结束)"""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return offset, False
    if not data:
        return offset, False
    offset += len(data)
    done = False
    for raw in data.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k in ("out_time_us", "out_time_ms"):
            state["us"] = _to_float(v)
        elif k == "speed":
            state["speed"] = v.strip()
        elif k == "progress" and v.strip() == "end":
            done = True
    return offset, done


def read_report_errors(path, limit=5):
    """从 FFREPORT 生成的日志里挑出错误行（窗口模式下 stderr 归控制台，只能走后门）"""
    lines = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                s = raw.strip()
                if not s:
                    continue
                low = s.lower()
                if s.startswith('"') or "ffmpeg.exe" in low:
                    continue          # 跳过 ffmpeg 自己回显的命令行
                if ("error" in low or "invalid" in low or "failed" in low
                        or "not supported" in low):
                    lines.append(s[:200])
    except OSError:
        return []
    # 去掉重复的（ffmpeg 常把同一句打两遍）
    uniq = []
    for s in lines:
        if s not in uniq:
            uniq.append(s)
    return uniq[-limit:]


def run_encode(settings, info, plan, src, dst, work_dir, on_progress, stop_event,
               running, on_cmd=None):
    """跑一次 ffmpeg，返回 (成功?, 说明, 字幕说明)。on_progress(百分比, 速度) 会被高频调用。

    on_cmd(命令行字符串) 在启动前回调一次，用来把实际执行的命令写进日志。"""
    sub_name = None
    sub_note = ""
    # 极速转封装不解码，没法烧字幕（烧字幕必须重编码），直接跳过
    if settings["burn_sub"] and not plan.get("remux"):
        sub = pick_subtitle(info)
        if sub is None:
            sub_note = "无可用内嵌字幕（位图字幕不支持硬烧）"
            if info["subs"]:
                sub_note = "只有位图字幕（PGS/VobSub），无法硬烧"
        else:
            sub_name = extract_subtitle(settings["ffmpeg"], src, sub["index"], work_dir)
            if sub_name is None:
                sub_note = "字幕提取失败，已跳过硬烧"
            else:
                sub_note = f"硬烧第{sub['index']}条字幕"
    show_console = bool(settings.get("show_console"))
    prog_path = os.path.join(work_dir, "progress.txt") if show_console else "pipe:1"
    report = os.path.join(work_dir, "ffmpeg-report.txt") if show_console else ""
    tail = []
    try:
        cmd = build_command(settings, info, plan, src, dst,
                            work_dir, sub_name, progress_to=prog_path)
        if on_cmd:
            on_cmd(cmd)
        if show_console:
            # 弹一个独立控制台窗口：先打印命令行，再跑 ffmpeg，进度从 progress.txt 轮询
            line = console_cmdline(cmd, "ffmpeg - " + os.path.basename(dst))
            env = dict(os.environ)
            # 窗口模式下 stderr 归控制台，拿不到文字；用 FFREPORT 另存一份日志，
            # 出错时把错误行写进运行日志，窗口关了也还能查
            env["FFREPORT"] = "file=ffmpeg-report.txt:level=24"
            # 注意：这里绝对不能传 stdin/stdout/stderr。只要传了任意一个，Python 就会
            # 用 STARTF_USESTDHANDLES 把父进程（exe 没有控制台 → NULL）的句柄塞给子进程，
            # ffmpeg 写状态信息失败会直接返回 1。不传，子进程才用新建控制台的句柄。
            proc = subprocess.Popen(line, cwd=work_dir, env=env,
                                    creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            proc = subprocess.Popen(cmd, cwd=work_dir, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, creationflags=_NO_WINDOW)
    except Exception as e:
        return False, f"启动 ffmpeg 失败：{e}", sub_note

    running.add(proc)
    try:
        duration = info["duration"] or 0

        def emit(us, speed):
            if duration > 0:
                on_progress(max(0, min(100, int(us / 1e6 / duration * 100))), speed)
            else:
                on_progress(None, speed)

        if show_console:
            # 轮询进度文件（窗口模式下 stdout 归控制台，拿不到管道）
            state = {"us": 0.0, "speed": ""}
            offset = 0
            last_key = (0.0, "")
            while True:
                offset, done = _read_progress_file(prog_path, offset, state)
                key = (state["us"], state["speed"])
                if key != last_key:
                    last_key = key
                    emit(state["us"], state["speed"])
                if done:
                    on_progress(100, "")
                if stop_event.is_set():
                    kill_process(proc)
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.3)
            if proc.poll() is None:
                proc.wait()
        else:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if line.startswith(("out_time_us=", "out_time_ms=")):
                    us = _to_float(line.split("=", 1)[1])
                    if duration > 0:
                        on_progress(max(0, min(100, int(us / 1e6 / duration * 100))), "")
                elif line.startswith("speed="):
                    on_progress(None, line.split("=", 1)[1].strip())
                elif line == "progress=end":
                    on_progress(100, "")
                elif not re.match(r"^(frame|fps|bitrate|total_size|out_time|dup_frames|"
                                  r"drop_frames|stream_\d|progress)=", line):
                    tail.append(line)
                    if len(tail) > 40:
                        tail.pop(0)
                if stop_event.is_set():
                    kill_process(proc)
                    break
            proc.wait()
    finally:
        running.discard(proc)

    if stop_event.is_set():
        return False, "已取消", sub_note
    if proc.returncode != 0:
        rc = proc.returncode
        if rc > 2 ** 31:          # Windows 返回码按无符号给出的，转回可读的负数
            rc -= 2 ** 32
        if tail:
            detail = " | ".join(tail[-6:])
        elif report and os.path.isfile(report):
            detail = " | ".join(read_report_errors(report)) or "详见弹出的 ffmpeg 窗口"
        elif show_console:
            detail = "详见弹出的 ffmpeg 窗口"
        else:
            detail = "（无输出）"
        return False, "ffmpeg 返回 %s：%s" % (rc, detail), sub_note
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        return False, "输出文件为空", sub_note
    return True, "", sub_note


# ============================================================
# 界面
# ============================================================

COLS = (
    ("name", "文件", 290),
    ("dur", "时长", 76),
    ("res", "分辨率", 100),
    ("fps", "帧率", 84),
    ("codec", "编码", 150),
    ("sub", "字幕", 90),
    ("status", "状态", 300),
)


def fmt_duration(sec):
    if not sec or sec <= 0:
        return "—"
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fps_label(info):
    """列表里的帧率文本：`30 CFR` / `29.97 CFR` / `27.11 VFR`"""
    fps = info.get("fps") or 0
    if fps <= 0:
        return "—"
    if abs(fps - round(fps)) < 0.01:
        num = str(int(round(fps)))
    else:
        num = f"{fps:.2f}".rstrip("0").rstrip(".")
    return f"{num} {'VFR' if info.get('vfr') else 'CFR'}"


def estimate_eta(duration, pct, speed):
    """根据总时长、当前百分比、ffmpeg 速度（如 "3.12x"）估算剩余真实时间（秒）。

    ffmpeg 的 speed 就是"处理时长 / 墙钟时间"，所以：
        剩余视频时长 = 总时长 × (100 - 百分比) / 100
        剩余墙钟时间 = 剩余视频时长 / 速度
    取不到速度时退回按百分比线性外推（仅粗略估计）。无足够信息时返回 None。"""
    if not duration or duration <= 0 or pct < 0:
        return None
    sp = 0.0
    if speed:
        m = re.search(r"([\d.]+)", speed)
        if m:
            sp = float(m.group(1))
    if sp > 0:
        # 有速度就能算：剩余视频时长（含 0% 时 = 全片）÷ 速度
        rem_video = duration * (100 - pct) / 100.0
        return max(0.0, rem_video / sp)
    if pct <= 0:
        # 既没速度、又还没进度，无法估计
        return None
    # 无速度：用已用时间反推总耗时（百分比占比），误差较大
    return max(0.0, duration * (100 - pct) / pct)


def collect_videos(paths):
    """把拖入/选择的路径展开成视频文件列表（文件夹递归），按自然排序并去重。

    会跳过 success/ error/ 归档目录（含之前跑出来的成品），避免把已经压好的文件
    再压一遍、或者把归档目录挤成 success/success/error/error。"""
    files = []
    for p in paths:
        p = clean_path(p)
        if not p:
            continue
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                # 不进入归档目录
                dirs[:] = [d for d in dirs if d.lower() not in ARCHIVE_DIRS]
                dirs.sort(key=natural_key)
                for name in sorted(names, key=natural_key):
                    if not name.lower().endswith(VIDEO_EXTS):
                        continue
                    if OUTPUT_NAME_RE.search(name):
                        continue      # 以前压出来的 xxx.crf24.mp4 不再当源
                    files.append(os.path.abspath(os.path.join(root, name)))
        elif os.path.isfile(p) and p.lower().endswith(VIDEO_EXTS):
            files.append(os.path.abspath(p))
    seen = set()
    out = []
    for f in files:
        key = f.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def build_encode_tab(parent, app=None):
    parent.grid_columnconfigure(3, weight=1)
    parent.grid_rowconfigure(3, weight=1)

    state = {
        "ffmpeg": "",
        "ffprobe": "",
        "encoders": {},
        "items": {},          # iid -> {...}
        "running": False,
        "stop": threading.Event(),
        "procs": set(),
        "seq": 0,
        "checked": False,
        "warned_missing": False,
        "codec_touched": False,   # 用户是否手动改过编码格式（改过就不再自动切）
        "progress": {},       # iid -> 当前百分比（用于总进度条）
        "total": 0,
        "ok": 0,
        "fail": 0,
        "cancel": 0,
        "skip": 0,
        "grown": 0,           # 成品比源还大的个数（已归入 oversize/）
    }
    probe_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe")

    def ui(fn):
        try:
            parent.after(0, fn)
        except Exception:
            pass

    # ---------------- 顶部：FFmpeg 路径（紧凑单行，去掉大段说明） ----------------
    ff_row = ttk.Frame(parent)
    ff_row.grid(row=0, column=0, columnspan=4, sticky="ew", padx=8, pady=(6, 2))
    ff_row.grid_columnconfigure(1, weight=1)
    ttk.Label(ff_row, text="FFmpeg：").grid(row=0, column=0, sticky="w", padx=(2, 4))
    var_ff = tk.StringVar()
    ent_ff = ttk.Entry(ff_row, textvariable=var_ff)
    ent_ff.grid(row=0, column=1, sticky="ew", padx=4)

    lbl_ff = ttk.Label(ff_row, text="检测中…", foreground="#666666")
    lbl_ff.grid(row=0, column=4, sticky="w", padx=6)

    def _pick_ff():
        p = filedialog.askopenfilename(
            title="选择 ffmpeg.exe", filetypes=[("ffmpeg", "ffmpeg.exe"), ("所有文件", "*.*")])
        if not p:
            return
        var_ff.set(p)
        save_ffmpeg_hint(p)
        apply_ffmpeg()

    ttk.Button(ff_row, text="浏览…", width=7, command=_pick_ff).grid(row=0, column=2, padx=2)
    btn_redetect = ttk.Button(ff_row, text="重新检测", width=9)
    btn_redetect.grid(row=0, column=3, padx=2)

    # ---------------- 压制设置（模式 + 编码参数 + 选项，紧凑几行） ----------------
    settings_box = ttk.LabelFrame(parent, text=" 压制设置 ", padding=(6, 4))
    settings_box.grid(row=1, column=0, columnspan=4, sticky="ew", padx=8, pady=(2, 4))

    # 第 1 行：模式
    mode_line = ttk.Frame(settings_box)
    mode_line.grid(row=0, column=0, columnspan=6, sticky="w")
    var_mode = tk.StringVar(value="640")
    for i, (val, text) in enumerate((("640", "640p（最长边 640）"),
                                     ("hd", "高清（原分辨率）"),
                                     ("custom", "自定义"),
                                     (MODE_REMUX, "极速转封装（-c copy）"))):
        ttk.Radiobutton(mode_line, text=text, value=val, variable=var_mode,
                        command=lambda: refresh_mode_ui()).pack(side=tk.LEFT, padx=(4, 14))

    # 第 2 行：编码格式 / 质量 / preset / 并发
    param_line = ttk.Frame(settings_box)
    param_line.grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))
    ttk.Label(param_line, text="编码格式：").pack(side=tk.LEFT, padx=(4, 2))
    var_codec = tk.StringVar(value=CODEC_LABELS[CODEC_AV1])
    cbo_codec = ttk.Combobox(param_line, textvariable=var_codec, width=12, state="readonly",
                             values=[CODEC_LABELS[CODEC_AV1], CODEC_LABELS[CODEC_HEVC]])
    cbo_codec.pack(side=tk.LEFT, padx=(0, 12))

    ttk.Label(param_line, text="质量：").pack(side=tk.LEFT, padx=(0, 2))
    var_quality = tk.StringVar(value=str(DEFAULT_QUALITY[CODEC_AV1]))
    sp_quality = ttk.Spinbox(param_line, from_=1, to=51, width=5, textvariable=var_quality)
    sp_quality.pack(side=tk.LEFT, padx=(0, 12))

    ttk.Label(param_line, text="preset：").pack(side=tk.LEFT, padx=(0, 2))
    var_preset = tk.StringVar(value="p1")
    cbo_preset = ttk.Combobox(param_line, textvariable=var_preset, width=10, state="readonly",
                              values=NVENC_PRESETS)
    cbo_preset.pack(side=tk.LEFT, padx=(0, 12))

    ttk.Label(param_line, text="并发：").pack(side=tk.LEFT, padx=(0, 2))
    box_conc, var_conc = make_num_box(param_line, 4, 1, 16, width=3)
    box_conc.pack(side=tk.LEFT)

    # 第 3 行：输出目录 / 硬烧字幕 / 显示 ffmpeg 窗口
    opt_line = ttk.Frame(settings_box)
    opt_line.grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))
    ttk.Label(opt_line, text="输出目录：").pack(side=tk.LEFT, padx=(4, 2))
    var_outdir = tk.StringVar()
    ttk.Entry(opt_line, textvariable=var_outdir, width=24).pack(side=tk.LEFT)
    ttk.Button(opt_line, text="浏览…", width=7,
               command=lambda: var_outdir.set(filedialog.askdirectory(title="选择输出目录") or
                                              var_outdir.get())).pack(side=tk.LEFT, padx=3)
    ttk.Label(opt_line, text="留空=源目录", foreground="#666666").pack(side=tk.LEFT, padx=(0, 10))

    var_burn = tk.BooleanVar(value=True)
    ttk.Checkbutton(opt_line, text="硬烧字幕", variable=var_burn).pack(side=tk.LEFT, padx=(0, 10))
    var_keepdv = tk.BooleanVar(value=False)
    ttk.Checkbutton(opt_line, text="保留杜比视界（很慢）", variable=var_keepdv).pack(side=tk.LEFT, padx=(0, 10))
    var_console = tk.BooleanVar(value=False)
    ttk.Checkbutton(opt_line, text="显示 ffmpeg 窗口", variable=var_console).pack(side=tk.LEFT)

    # 第 3 行：归档动作开关（默认全开；关掉就地把文件留在原位）
    move_line = ttk.Frame(settings_box)
    move_line.grid(row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))
    ttk.Label(move_line, text="完成后移动：", foreground="#666666").pack(side=tk.LEFT, padx=(4, 2))
    var_move_ok = tk.BooleanVar(value=True)
    ttk.Checkbutton(move_line, text="成功 → success/", variable=var_move_ok).pack(side=tk.LEFT, padx=(0, 10))
    var_move_fail = tk.BooleanVar(value=True)
    ttk.Checkbutton(move_line, text="失败 → error/", variable=var_move_fail).pack(side=tk.LEFT, padx=(0, 10))
    var_move_grow = tk.BooleanVar(value=True)
    ttk.Checkbutton(move_line, text="变大 → oversize/", variable=var_move_grow).pack(side=tk.LEFT)

    # 第 4 行：编码器状态（一行小字）
    lbl_enc = ttk.Label(settings_box, text="编码器：检测中…", foreground="#1a6b1a")
    lbl_enc.grid(row=4, column=0, columnspan=6, sticky="w", padx=4, pady=(4, 0))

    # ---------------- 文件列表 ----------------
    list_bar = ttk.Frame(parent)
    list_bar.grid(row=2, column=0, columnspan=4, sticky="ew", padx=8, pady=(4, 2))
    lbl_count = ttk.Label(list_bar, text="共 0 个文件")
    lbl_count.pack(side=tk.LEFT, padx=4)

    def _add_files():
        paths = filedialog.askopenfilenames(
            title="选择视频",
            filetypes=[("视频文件", "*.mp4 *.flv *.ts *.mkv *.mov *.avi *.wmv *.m4v "
                                   "*.webm *.m2ts *.mts *.mpg *.mpeg *.3gp *.rmvb *.vob *.f4v"),
                       ("所有文件", "*.*")])
        if paths:
            add_paths(list(paths))

    def _add_folder():
        d = filedialog.askdirectory(title="选择视频文件夹")
        if d:
            add_paths([d])

    ttk.Button(list_bar, text="添加文件", command=_add_files).pack(side=tk.LEFT, padx=3)
    ttk.Button(list_bar, text="添加文件夹", command=_add_folder).pack(side=tk.LEFT, padx=3)
    ttk.Button(list_bar, text="移除选中", command=lambda: remove_selected()).pack(side=tk.LEFT, padx=3)
    ttk.Button(list_bar, text="清空列表", command=lambda: clear_list()).pack(side=tk.LEFT, padx=3)
    ttk.Label(list_bar, text="可拖放视频/文件夹",
              foreground="#666666").pack(side=tk.LEFT, padx=8)

    tree_frame = ttk.Frame(parent)
    tree_frame.grid(row=3, column=0, columnspan=4, sticky="nsew", padx=8, pady=2)
    tree_frame.grid_rowconfigure(0, weight=1)
    tree_frame.grid_columnconfigure(0, weight=1)
    tree = ttk.Treeview(tree_frame, columns=[c[0] for c in COLS], show="headings",
                        selectmode="extended", height=10)
    for key, title, width in COLS:
        tree.heading(key, text=title)
        tree.column(key, width=width, minwidth=60, stretch=(key in ("name", "status")))
    vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")

    # ---------------- 按钮 + 总进度 ----------------
    btn_row = ttk.Frame(parent)
    btn_row.grid(row=4, column=0, columnspan=4, sticky="ew", padx=8, pady=4)
    btn_start = tk.Button(btn_row, text="开始压制", bg="#2f6bff", fg="white",
                          activebackground="#2455cc", activeforeground="white",
                          font=("Microsoft YaHei", 10, "bold"), padx=18, pady=4,
                          cursor="hand2", command=lambda: start_batch())
    btn_start.pack(side=tk.LEFT, padx=4)
    btn_stop = tk.Button(btn_row, text="停止", state=tk.DISABLED, padx=16, pady=4,
                         command=lambda: (state["stop"].set(),
                                          log("正在停止：已启动的 ffmpeg 会被结束…")))
    btn_stop.pack(side=tk.LEFT, padx=4)

    bar_prog = ttk.Progressbar(btn_row, orient=tk.HORIZONTAL, mode="determinate",
                               length=300, maximum=1, value=0)
    bar_prog.pack(side=tk.LEFT, padx=(18, 8))
    lbl_prog = ttk.Label(btn_row, text="未开始", foreground="#444444")
    lbl_prog.pack(side=tk.LEFT)

    # ---------------- 运行日志（可折叠，默认收起，把空间让给文件列表） ----------------
    log_wrap = ttk.Frame(parent)
    log_wrap.grid(row=5, column=0, columnspan=4, sticky="ew", padx=8, pady=(0, 6))
    log_wrap.grid_columnconfigure(0, weight=1)

    log_head = ttk.Frame(log_wrap)
    log_head.grid(row=0, column=0, sticky="ew")
    var_log_open = tk.BooleanVar(value=False)
    btn_log_toggle = ttk.Button(log_head, text="▸ 运行日志", width=11)
    btn_log_toggle.pack(side=tk.LEFT)
    ttk.Button(log_head, text="清空", width=6,
               command=lambda: clear_log(text_log)).pack(side=tk.LEFT, padx=6)

    log_body = ttk.Frame(log_wrap)
    log_body.grid(row=1, column=0, sticky="nsew", pady=(2, 0))
    log_body.grid_rowconfigure(0, weight=1)
    log_body.grid_columnconfigure(0, weight=1)
    text_log = scrolledtext.ScrolledText(log_body, height=10, state="disabled", wrap=tk.WORD,
                                         font=("Consolas", 9))
    text_log.grid(row=0, column=0, sticky="nsew")
    log = make_logger(text_log)

    def toggle_log():
        if var_log_open.get():
            log_body.grid_remove()
            btn_log_toggle.config(text="▸ 运行日志")
            var_log_open.set(False)
        else:
            log_body.grid()
            btn_log_toggle.config(text="▾ 运行日志")
            var_log_open.set(True)

    btn_log_toggle.config(command=toggle_log)
    log_body.grid_remove()   # 默认收起

    # ============================================================
    # 列表操作
    # ============================================================

    def item_cells(it):
        return [it["cells"].get(k, "") for k, _t, _w in COLS]

    def set_cell(iid, key, value):
        it = state["items"].get(iid)
        if not it:
            return
        it["cells"][key] = value
        try:
            tree.item(iid, values=item_cells(it))
        except Exception:
            pass

    def refresh_count():
        lbl_count.config(text=f"共 {len(state['items'])} 个文件")

    def add_paths(paths):
        files = collect_videos(paths)
        if not files:
            log("没有找到支持的视频文件（" + " ".join(VIDEO_EXTS) + "）")
            return
        for f in files:
            if any(it["path"].lower() == f.lower() for it in state["items"].values()):
                continue
            state["seq"] += 1
            iid = f"v{state['seq']}"
            state["items"][iid] = {
                "path": f,
                "info": None,
                "plan": None,
                "cells": {"name": os.path.basename(f), "dur": "…", "res": "…",
                          "fps": "…", "codec": "…", "sub": "…", "status": "读取信息…"},
            }
            tree.insert("", tk.END, iid=iid, values=item_cells(state["items"][iid]))
            queue_probe(iid)
        refresh_count()
        log(f"已添加 {len(files)} 个视频，正在后台读取信息…")
        in_arch = [f for f in files if in_archive_dir(os.path.dirname(f))]
        if in_arch:
            log(f"注意：其中 {len(in_arch)} 个文件在 success/ 或 error/ 归档目录里，"
                f"会就地处理：输出写到同一目录，成功后再归到该目录下的 success/，"
                f"失败留在 error/（不会再套 error/error）。")

    def remove_selected():
        for iid in tree.selection():
            it = state["items"].pop(iid, None)
            if it:
                tree.delete(iid)
        refresh_count()

    def clear_list():
        if state["running"]:
            messagebox.showwarning("提示", "正在压制，请先停止。")
            return
        for iid in list(state["items"]):
            tree.delete(iid)
        state["items"].clear()
        refresh_count()

    # ============================================================
    # 信息探测（多线程，不阻塞 GUI）
    # ============================================================

    def fill_info(iid, info):
        it = state["items"].get(iid)
        if not it:
            return
        it["info"] = info
        if not info:
            it["cells"].update({"dur": "—", "res": "—", "fps": "—", "codec": "读取失败",
                                "sub": "—", "status": "无法读取信息"})
            tree.item(iid, values=item_cells(it))
            return
        dw, dh = display_size(info)
        cx = {420: "420", 422: "422", 444: "444", 400: "灰"}.get(
            info.get("chroma", 420), str(info.get("chroma", 420)))
        bits = f"{info['bit_depth']}bit"
        codec_txt = f"{info['vcodec']} {bits} {cx}" + (" HDR" if info["hdr"] else "")
        if dv_label(info):
            codec_txt += " " + dv_label(info)
        subs = info["subs"]
        if not subs:
            sub_txt = "无"
        else:
            langs = ",".join(s["lang"] or "?" for s in subs[:2])
            sub_txt = f"{len(subs)}条 {langs}"
        it["cells"].update({
            "dur": fmt_duration(info["duration"]),
            "res": f"{dw}x{dh}" + (" ↻" if info["rotation"] else ""),
            "fps": fps_label(info),
            "codec": codec_txt,
            "sub": sub_txt,
            "status": "待压制",
        })
        tree.item(iid, values=item_cells(it))

    def queue_probe(iid):
        path = state["items"][iid]["path"]

        def work():
            ff, fp = state["ffmpeg"], state["ffprobe"]
            if not ff:
                ff, fp = locate_ffmpeg(load_ffmpeg_hint())
            info = probe_media(fp, path) if fp else None
            ui(lambda: fill_info(iid, info))

        probe_pool.submit(work)

    # ============================================================
    # FFmpeg / 编码器检测
    # ============================================================

    def apply_ffmpeg():
        """定位 ffmpeg 并在后台探测编码器"""
        ff, fp = locate_ffmpeg(clean_path(var_ff.get()))
        state["ffmpeg"], state["ffprobe"] = ff, fp
        if not ff:
            lbl_ff.config(text="✗ 未找到 ffmpeg", foreground="#b00020")
            lbl_enc.config(text="编码器：未检测（先配置 ffmpeg）", foreground="#b00020")
            log("未找到 ffmpeg：请把 ffmpeg.exe 放到程序目录 / bin\\ffmpeg，"
                "或点『浏览…』手动指定（选 ffmpeg.exe 即可，同目录需有 ffprobe.exe）。")
            if not state["warned_missing"]:
                state["warned_missing"] = True
                messagebox.showwarning(
                    "未找到 FFmpeg",
                    "「视频常用压制」需要 FFmpeg（ffmpeg.exe + ffprobe.exe）。\n\n"
                    "补救方法（任选其一）：\n"
                    "  1. 把 ffmpeg.exe、ffprobe.exe 放到本程序同目录；\n"
                    "  2. 放到 程序目录\\bin\\ffmpeg\\ 下；\n"
                    "  3. 把 ffmpeg 加入系统 PATH；\n"
                    "  4. 点本页『浏览…』手动指定 ffmpeg.exe。\n\n"
                    "下载：https://www.gyan.dev/ffmpeg/builds/（release-full，"
                    "解压后 bin 目录里就是这两个 exe）")
            return
        var_ff.set(ff)
        ver = ffmpeg_version(ff)
        lbl_ff.config(text=("✓ " + ver.replace("ffmpeg version ", "v").split(" Copyright")[0][:46]
                            if ver else "✓ 已找到 ffmpeg"), foreground="#1a6b1a")
        lbl_enc.config(text="编码器：检测中…", foreground="#666666")
        log(f"FFmpeg：{ff}")
        if ver:
            log("  " + ver[:120])
        if not fp:
            log("  警告：同目录未找到 ffprobe.exe，无法读取视频信息与字幕。")

        def work():
            try:
                enc = detect_encoders(ff, log=lambda m: ui(lambda m=m: log(m)))
            except Exception:
                tb = traceback.format_exc()
                ui(lambda: log("编码器检测失败：\n" + tb))
                ui(lambda: lbl_enc.config(text="编码器：检测失败", foreground="#b00020"))
                return
            ui(lambda: apply_encoders(enc))

        threading.Thread(target=work, daemon=True).start()

    def apply_encoders(enc):
        state["encoders"] = enc
        parts = []
        any_sw = False
        for codec in (CODEC_AV1, CODEC_HEVC):
            e = enc.get(codec)
            parts.append(f"{CODEC_LABELS[codec]} → {encoder_label(e)}")
            if e and not is_hardware(e):
                any_sw = True
        lbl_enc.config(text="编码器：" + "；".join(parts),
                       foreground="#b35c00" if any_sw else "#1a6b1a")
        for codec in (CODEC_AV1, CODEC_HEVC):
            e = enc.get(codec)
            if not e:
                log(f"  警告：{CODEC_LABELS[codec]} 没有可用编码器（NVENC/QSV/软件都不可用）")
            elif not is_hardware(e):
                log(f"  提示：{CODEC_LABELS[codec]} 只能用 CPU 软件编码（{e}），速度慢很多；"
                    f"想跑硬件编码请换另一种编码格式")
        # 本机 AV1 没有硬件编码（多数 Intel 核显都不支持 AV1 编码）、而 H.265 有硬件时，
        # 默认把编码格式切到 H.265，避免 640p 默认模式掉进慢速 CPU 编码
        if (not is_hardware(enc.get(CODEC_AV1)) and is_hardware(enc.get(CODEC_HEVC))
                and not state["codec_touched"] and current_codec() == CODEC_AV1):
            var_codec.set(CODEC_LABELS[CODEC_HEVC])
            log("  已自动把编码格式切到 H.265（本机 AV1 没有硬件编码器）；"
                "仍想用 AV1 软件编码可在下拉框里改回来")
        refresh_mode_ui()

    def ensure_encoders():
        """开始压制前确保编码器已探测（在后台线程里调用，阻塞无妨）"""
        if state["encoders"]:
            return state["encoders"]
        ff = state["ffmpeg"] or locate_ffmpeg(load_ffmpeg_hint())[0]
        if not ff:
            raise RuntimeError("未找到 ffmpeg，无法压制")
        enc = detect_encoders(ff, log=lambda m: ui(lambda m=m: log(m)))
        ui(lambda: apply_encoders(enc))
        return enc

    # ============================================================
    # 模式联动
    # ============================================================

    def current_codec():
        for k, v in CODEC_LABELS.items():
            if v == var_codec.get():
                return k
        return CODEC_AV1

    def refresh_presets(codec, keep=False):
        enc = state["encoders"].get(codec)
        values = preset_values(enc)
        cbo_preset.config(values=values)
        cur = var_preset.get()
        if keep and cur in values:
            return
        var_preset.set(default_preset(enc, codec))

    def refresh_mode_ui():
        """模式或编码格式变化后：编码格式始终可选；质量与 preset 按「模式 + 编码格式」自动填。
        只有自定义模式才允许手动改质量 / preset。"""
        codec = current_codec()
        refresh_presets(codec, keep=True)
        mode = var_mode.get()
        enc = state["encoders"].get(codec)
        if mode == "custom":
            sp_quality.config(state="normal")
            cbo_preset.config(state="readonly")
            return
        if mode == MODE_REMUX:
            # 极速转封装不编码：质量 / preset / 编码器都无意义，灰掉且不再改写
            sp_quality.config(state="disabled")
            cbo_preset.config(state="disabled")
            return
        quality = HD_CRF if mode == "hd" else default_quality(enc, codec)
        var_quality.set(str(quality))
        # 高清模式 = 画质优先，速度预设固定用「最好那一档」（最慢）
        var_preset.set(best_preset(enc) if mode == "hd" else default_preset(enc, codec))
        sp_quality.config(state="disabled")
        cbo_preset.config(state="disabled")

    def on_codec_change(_e=None):
        state["codec_touched"] = True
        refresh_mode_ui()

    cbo_codec.bind("<<ComboboxSelected>>", on_codec_change)
    btn_redetect.config(command=apply_ffmpeg)

    # ============================================================
    # 开始 / 停止
    # ============================================================

    def collect_settings():
        mode = var_mode.get()
        codec = current_codec()
        try:
            quality = int(str(var_quality.get()).strip())
        except ValueError:
            quality = default_quality(state["encoders"].get(codec), codec)
        quality = max(0, min(63, quality))
        try:
            conc = max(1, min(16, int(str(var_conc.get()).strip())))
        except ValueError:
            conc = 4
        return {
            "mode": mode,
            "codec": codec,
            "quality": quality,
            "preset": var_preset.get().strip(),
            "burn_sub": bool(var_burn.get()),
            "keep_dv": bool(var_keepdv.get()),
            "move_ok": bool(var_move_ok.get()),
            "move_fail": bool(var_move_fail.get()),
            "move_grow": bool(var_move_grow.get()),
            "show_console": bool(var_console.get()),
            "concurrency": conc,
            "out_dir": clean_path(var_outdir.get()),
        }

    def reset_progress(total):
        state["progress"].clear()
        state["total"] = total
        state["ok"] = state["fail"] = state["cancel"] = state["skip"] = 0
        state["grown"] = 0
        refresh_progress()

    def refresh_progress():
        total = state["total"]
        done = state["ok"] + state["fail"] + state["cancel"] + state["skip"]
        running = sum(state["progress"].values()) / 100.0
        bar_prog.config(maximum=max(1, total), value=min(total, done + running))
        if not total:
            lbl_prog.config(text="未开始")
            return
        txt = f"已完成 {done}/{total}"
        if state["ok"]:
            txt += f" · 成功 {state['ok']}"
        if state["fail"]:
            txt += f" · 失败 {state['fail']}"
        if state["cancel"]:
            txt += f" · 取消 {state['cancel']}"
        if state["skip"]:
            txt += f" · 跳过 {state['skip']}"
        if state["grown"]:
            txt += f" · 变大 {state['grown']}"
        lbl_prog.config(text=txt)

    def start_batch():
        if state["running"]:
            return
        if not state["items"]:
            messagebox.showwarning("提示", "请先添加要压制的视频。")
            return
        settings = collect_settings()
        clear_log(text_log)
        state["stop"].clear()
        # 开始前过滤一遍：列表是之前拍的快照，文件可能已被上一轮压制的归档动作移走
        jobs = []
        for iid, it in list(state["items"].items()):
            if os.path.isfile(it["path"]):
                jobs.append(iid)
            else:
                tree.delete(iid)
                state["items"].pop(iid, None)
                log(f"已从列表移除（文件已不存在，可能上次压制已归档）：{it['path']}")
        refresh_count()
        if not jobs:
            messagebox.showwarning("提示", "列表里的文件都已经不在了（可能上次压制已归档），请重新添加。")
            return
        state["running"] = True
        btn_stop.config(state=tk.NORMAL)
        btn_start.config(state=tk.DISABLED)
        reset_progress(len(jobs))
        for iid in jobs:
            set_cell(iid, "status", "排队中")
        mode_txt = {"640": "640p（最长边 640；已 ≤640 的跳过）",
                    "hd": "高清（原分辨率，画质优先）",
                    "custom": "自定义",
                    MODE_REMUX: "极速转封装（不重编码，-c copy）"}[settings["mode"]]
        if settings["mode"] == "hd":
            q_txt = f"质量 {HD_CRF}（高清固定）"
        elif settings["mode"] == "640":
            q_txt = "质量 按编码器默认"
        else:
            q_txt = f"质量 {settings['quality']}"
        if settings["mode"] == MODE_REMUX:
            log(f"模式：{mode_txt}　并发：{settings['concurrency']}　"
                f"共 {len(jobs)} 个文件（只换容器，不烧字幕）")
        else:
            log(f"模式：{mode_txt}　编码：{CODEC_LABELS[settings['codec']]}　"
                f"{q_txt}　preset {settings['preset']}　"
                f"并发：{settings['concurrency']}　硬烧字幕：{'开' if settings['burn_sub'] else '关'}　"
                f"共 {len(jobs)} 个文件")
        # 关掉了哪类移动就提示一句，免得用户以为文件丢了
        mv_off = []
        if not settings.get("move_ok", True):
            mv_off.append("成功")
        if not settings.get("move_fail", True):
            mv_off.append("失败")
        if not settings.get("move_grow", True):
            mv_off.append("变大")
        if mv_off:
            log(f"移动：{'、'.join(mv_off)}的文件不移动（源文件留在原位）")
        # 列表里有杜比视界视频、但没勾「保留杜比视界」时提示一句，避免压完才发现 RPU 没保住
        if settings["mode"] != MODE_REMUX and not settings.get("keep_dv"):
            dv_n = 0
            for iid in jobs:
                it = state["items"].get(iid) or {}
                if (it.get("info") or {}).get("dovi"):
                    dv_n += 1
            if dv_n:
                log(f"提示：{dv_n} 个杜比视界视频将丢失 RPU（软件 x265 保留会明显变慢，"
                    f"需要时勾选「保留杜比视界（很慢）」）")

        def work():
            try:
                enc = ensure_encoders()
            except Exception as e:
                ui(lambda: log(f"错误：{e}"))
                ui(lambda: finish_batch(0, 0, 0, 0))
                return
            settings["ffmpeg"] = state["ffmpeg"]
            settings["encoders"] = enc
            ok_n = fail_n = cancel_n = skip_n = grown_n = 0

            def one(iid):
                nonlocal ok_n, fail_n, cancel_n, skip_n, grown_n
                if state["stop"].is_set():
                    ui(lambda: set_cell(iid, "status", "已取消"))
                    cancel_n += 1
                    state["cancel"] += 1
                    ui(refresh_progress)
                    return
                it = state["items"].get(iid)
                if not it:
                    return
                src = it["path"]
                if not os.path.isfile(src):
                    # 列表是开始前拍的快照，文件可能已被上一轮压制的归档动作移走
                    fail_n += 1
                    state["fail"] += 1
                    ui(lambda: set_cell(iid, "status", "失败：文件已不存在"))
                    ui(lambda: log(f"  ✗ 文件已不存在（可能已被上一次压制归档或移动）：{src}"))
                    ui(refresh_progress)
                    return
                info = it["info"] or probe_media(state["ffprobe"], src)
                if not info:
                    # 读不到视频信息：有 ffprobe 说明文件本身有问题，按失败归档
                    out_dir0 = settings["out_dir"] or os.path.dirname(src)
                    move_fail = bool(settings.get("move_fail", True))
                    archived = (archive_source(src, out_dir0, False)
                                if (state["ffprobe"] and move_fail) else "")
                    fail_n += 1
                    state["fail"] += 1
                    ui(lambda: set_cell(iid, "status", "失败：读不到视频信息"))
                    ui(lambda: log(f"  ✗ 读不到视频信息：{os.path.basename(src)}"
                                   + (f"（已移到 {archive_tag(archived)}）" if archived
                                      else "（按要求不移动源文件）" if not move_fail else "")))
                    ui(refresh_progress)
                    return
                plan = resolve_plan(settings, info)
                if not plan.get("remux") and not plan["enc"]:
                    ui(lambda: set_cell(iid, "status", f"失败：没有可用的 {CODEC_LABELS[plan['codec']]} 编码器"))
                    fail_n += 1
                    state["fail"] += 1
                    ui(refresh_progress)
                    return
                # 640p 模式：最长边本来就不超过 640 的直接跳过（既不放大，也不浪费时间再压一遍）
                if settings["mode"] == "640":
                    dw0, dh0 = display_size(info)
                    if max(dw0, dh0) <= SIXFORTY_EDGE:
                        skip_n += 1
                        state["skip"] += 1
                        state["progress"].pop(iid, None)
                        ui(lambda: set_cell(iid, "status", f"跳过：已 ≤640p（{dw0}x{dh0}）"))
                        ui(lambda: log(f"  ⊘ 跳过：{os.path.basename(src)}"
                                       f"（{dw0}x{dh0}，最长边已 ≤640，无需压制，源文件保持原位）"))
                        ui(refresh_progress)
                        return
                out_dir = settings["out_dir"] or os.path.dirname(src)
                try:
                    os.makedirs(out_dir, exist_ok=True)
                except Exception:
                    ui(lambda: set_cell(iid, "status", "失败：输出目录不可写"))
                    fail_n += 1
                    state["fail"] += 1
                    ui(refresh_progress)
                    return
                dst = os.path.join(out_dir,
                                   output_name(src, plan["quality"], plan.get("remux", False)))
                if os.path.exists(dst):
                    dst = str(get_unique_path(Path(dst)))
                dw, dh = display_size(info)
                size_txt = (f"{plan['target'][0]}x{plan['target'][1]}" if plan["target"]
                            else f"{dw}x{dh}")
                if plan.get("remux"):
                    dv_tag = f"，杜比视界 {dv_label(info)} 随码流保留" if info.get("dovi") else ""
                    ui(lambda: log(f"▶ {os.path.basename(src)}  →  {os.path.basename(dst)}"
                                   f"（极速转封装：-c copy，不重编码，画质音质不变{dv_tag}）"))
                else:
                    cx = {420: "4:2:0", 422: "4:2:2", 444: "4:4:4", 400: "灰阶"}.get(
                        info.get("chroma", 420), str(info.get("chroma", 420)))
                    # 写日志前必须自己算一次 pix_fmt（build_command 内部也调用同一个
                    # select_pix_fmt，两处参数一致、结果相同）。之前漏了这行会抛
                    # NameError，把 worker 线程直接打死 → 列表永远停在「排队中」。
                    pix_fmt = select_pix_fmt(info, settings["mode"], plan["enc"],
                                             keep_dv=bool(plan.get("keep_dv")))
                    depth = "10bit" if "10" in pix_fmt else "8bit"
                    hdr_tag = " HDR" if info["hdr"] else ""
                    vfr_tag = " VFR" if info.get("vfr") else ""
                    dv_tag = ""
                    if info.get("dovi"):
                        dv_tag = (" " + dv_label(info) +
                                  (" 保留RPU" if plan.get("keep_dv") else " RPU未保留"))
                    ui(lambda: log(f"▶ {os.path.basename(src)}  →  {os.path.basename(dst)}"
                                   f"（{CODEC_LABELS[plan['codec']]} / {plan['enc']} / "
                                   f"{quality_tag(plan['enc'], plan['quality'])} / "
                                   f"preset {plan['preset']} / {size_txt} / "
                                   f"{depth} {cx}{hdr_tag}{vfr_tag}{dv_tag}）"))
                ui(lambda: set_cell(iid, "status", "压制中 0%"))
                ui(lambda: state["progress"].__setitem__(iid, 0))
                work_dir = tempfile.mkdtemp(prefix="vfx_")
                last = {"t": 0.0, "pct": -1, "speed": ""}

                def on_progress(pct, speed):
                    import time as _t
                    now = _t.time()
                    # ffmpeg 把速度单独成行上报（pct=None）：记住最新速度，
                    # 用上一次记录的百分比继续刷新文本，让速度实时跳动。
                    # 只保存真正带数字的（过滤 "N/A" / "?" 这类无效值）。
                    speed_changed = False
                    if speed:
                        m = re.search(r"([\d.]+)\s*x?", speed, re.I)
                        if m and speed != last["speed"]:
                            last["speed"] = speed
                            speed_changed = True
                    if pct is None:
                        pct = last["pct"]
                    if pct < 0:
                        return
                    # 节流：同一百分比、速度也没变、且离上次刷新不到 0.4s 就跳过；
                    # 速度变了（即便百分比没变）也允许刷新——速度行本身约 1 秒一次，不会刷太频。
                    if (pct == last["pct"] and not speed_changed
                            and now - last["t"] < 0.4 and pct < 100):
                        return
                    last["t"] = now
                    last["pct"] = pct
                    txt = f"压制中 {pct}%"
                    if last["speed"]:
                        txt += f" · {last['speed']}"
                    if pct < 100:
                        rem = estimate_eta(info["duration"], pct, last["speed"])
                        if rem is not None:
                            txt += " · 剩 " + fmt_duration(rem)

                    def apply():
                        set_cell(iid, "status", txt)
                        state["progress"][iid] = pct
                        refresh_progress()

                    ui(apply)

                def on_cmd(cmd):
                    # 弹出 ffmpeg 窗口时，把实际执行的命令行也写进日志（可复制去手动跑）
                    if settings.get("show_console"):
                        line = subprocess.list2cmdline([str(c) for c in cmd])
                        ui(lambda: log("  命令：" + line))

                ok, msg, sub_note = run_encode(settings, info, plan, src, dst, work_dir,
                                               on_progress, state["stop"], state["procs"],
                                               on_cmd=on_cmd)
                if sub_note:
                    ui(lambda: log(f"  字幕：{sub_note}"))
                try:
                    shutil.rmtree(work_dir, ignore_errors=True)
                except Exception:
                    pass
                if ok:
                    # 体积变化：成品 ÷ 源，涨跌百分比写进状态列
                    try:
                        src_size = os.path.getsize(src)
                    except OSError:
                        src_size = 0
                    try:
                        dst_size = os.path.getsize(dst)
                    except OSError:
                        dst_size = 0
                    pct, grown = size_change(src_size, dst_size)
                    if pct is None:
                        delta_txt = ""
                        size_note = ""
                    else:
                        sizes = f"{fmt_size(src_size)}→{fmt_size(dst_size)}"
                        if abs(pct) < 0.5:
                            delta_txt = f" ±0%（{sizes}）"
                            size_note = f"（体积 ±0%，{sizes}）"
                        else:
                            delta_txt = f" {'↑' if grown else '↓'}{abs(pct):.0f}%（{sizes}）"
                            size_note = f"（体积 {'+' if grown else '-'}{abs(pct):.0f}%，{sizes}）"
                    if grown:
                        grown_n += 1
                        state["grown"] += 1
                        if settings.get("move_grow", True):
                            # 越压越大：源文件和成品都挪进 oversize/，方便一起删掉或重调参数
                            src_new, dst_new = archive_oversize(src, dst, out_dir)
                            moved_all = bool(src_new and dst_new)
                            move_note = (f"；体积变大，源文件与成品已移到 {OVERSIZE_DIR}/"
                                         if moved_all else
                                         f"；体积变大，但移到 {OVERSIZE_DIR}/ 未完全成功")
                            status_txt = f"完成 ✔{delta_txt} → {OVERSIZE_DIR}/"
                        else:
                            move_note = "；体积变大，按要求不动文件（源与成品留在原位）"
                            status_txt = f"完成 ✔{delta_txt}"
                        log_txt = f"  ✔ 完成{size_note}：{os.path.basename(dst)}{move_note}"
                    else:
                        if settings.get("move_ok", True):
                            archived = archive_source(src, out_dir, True)
                            if archived:
                                move_note = f"；源文件已移到 {archive_tag(archived)}"
                            elif os.path.basename(os.path.dirname(src)).lower() == "success":
                                move_note = "；源文件已在 success/ 里，保持原位"
                            else:
                                move_note = "；源文件移动失败"
                        else:
                            move_note = "；按要求不移动源文件（留在原位）"
                        status_txt = f"完成 ✔{delta_txt}"
                        log_txt = f"  ✔ 完成{size_note}：{os.path.basename(dst)}{move_note}"
                    ok_n += 1
                    state["ok"] += 1
                    state["progress"].pop(iid, None)
                    ui(lambda iid=iid, t=status_txt: set_cell(iid, "status", t))
                    ui(lambda t=log_txt: log(t))
                    ui(refresh_progress)
                elif state["stop"].is_set():
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
                    cancel_n += 1
                    state["cancel"] += 1
                    state["progress"].pop(iid, None)
                    ui(lambda: set_cell(iid, "status", "已取消"))
                    ui(refresh_progress)
                else:
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
                    if settings.get("move_fail", True):
                        archived = archive_source(src, out_dir, False)
                        if archived:
                            fail_note = f"（源文件已移到 {archive_tag(archived)}）"
                        elif os.path.basename(os.path.dirname(src)).lower() == "error":
                            fail_note = "（源文件已在 error/ 里，保持原位）"
                        else:
                            fail_note = ""
                    else:
                        fail_note = "（按要求不移动源文件，留在原位）"
                    fail_n += 1
                    state["fail"] += 1
                    state["progress"].pop(iid, None)
                    ui(lambda: set_cell(iid, "status", "失败 ✗"))
                    ui(lambda: log(f"  ✗ 失败：{os.path.basename(src)}：{msg}{fail_note}"))
                    ui(refresh_progress)

            # 逐个收集结果：任一文件内部异常只把它自己标记失败，绝不让整个批次线程
            # 静默崩掉（否则所有行会永远停在「排队中」，按钮状态也会卡死）。
            with ThreadPoolExecutor(max_workers=settings["concurrency"]) as pool:
                futures = {pool.submit(one, iid): iid for iid in jobs}
                for fut in as_completed(futures):
                    iid = futures[fut]
                    try:
                        fut.result()
                    except Exception:
                        tb = traceback.format_exc()
                        it = state["items"].get(iid) or {}
                        bad = os.path.basename(str(it.get("path", "")))
                        fail_n += 1
                        state["fail"] += 1
                        state["progress"].pop(iid, None)
                        ui(lambda iid=iid: set_cell(iid, "status", "失败：内部异常"))
                        ui(lambda bad=bad, tb=tb: log(f"  ✗ 内部异常：{bad}\n{tb}"))
                        ui(refresh_progress)
            ui(lambda: finish_batch(ok_n, fail_n, cancel_n, skip_n, grown_n,
                                    bool(settings.get("move_grow", True))))

        def finish_batch(ok_n, fail_n, cancel_n, skip_n=0, grown_n=0, moved_grow=True):
            state["running"] = False
            btn_stop.config(state=tk.DISABLED)
            btn_start.config(state=tk.NORMAL)
            state["progress"].clear()
            refresh_progress()
            # 完成后保留列表条目：状态列里要能看到「体积涨跌%」。
            # 已归档（源文件不在原位）的条目会在下次点「开始」时自动清掉。
            tail = f"，跳过 {skip_n}" if skip_n else ""
            if state["stop"].is_set():
                log(f"已停止：成功 {ok_n}，失败 {fail_n}，取消 {cancel_n}{tail}")
            else:
                log(f"全部结束：成功 {ok_n}，失败 {fail_n}，取消 {cancel_n}{tail}")
            summary = f"总结：已完成 {ok_n}，失败 {fail_n}，变大 {grown_n} 个"
            if grown_n and moved_grow:
                summary += f"（体积变大的源文件与成品都归到了 {OVERSIZE_DIR}/）"
            log(summary)
            lbl_prog.config(text=f"已完成 {ok_n}，失败 {fail_n}，变大 {grown_n} 个")
            state["stop"].clear()

        threading.Thread(target=work, daemon=True).start()
        return True

    # 拖放（先把上次记住的 ffmpeg 路径显示出来）
    var_ff.set(load_ffmpeg_hint())
    if app is not None:
        try:
            app.register_drop_handler(parent, add_paths)
        except Exception:
            pass

    # 依赖检测：只在打开本页时做（不拖慢程序启动）
    def on_show():
        if not state["checked"]:
            state["checked"] = True
            apply_ffmpeg()
        else:
            refresh_mode_ui()

    if app is not None:
        try:
            app.register_show_handler(parent, on_show)
        except Exception:
            parent.after(150, on_show)
    else:
        parent.after(150, on_show)

    refresh_mode_ui()
    return state
