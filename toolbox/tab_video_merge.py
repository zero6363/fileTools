# -*- coding: utf-8 -*-
r"""⑨ 视频合并 —— 按文件名顺序无损拼接（不转码）+ 兼容性检测 / 智能分组

参考 LosslessCut 的「连接文件（Merge / concatenate files）」：
    · 拖入的视频按文件名自然排序（9 在 10 前面），从头到尾首尾相接；
    · 用 FFmpeg concat demuxer + `-c copy`，只复制原始码流，不重新编码；
    · 连接前逐文件读取编码参数，参数完全一致的才分到同一组；
      不一致的自动拆成多个兼容组，分组预览里可以逐组选择「合并 / 剔除」，
      每组各自输出一个文件（单文件组可选单独重新封装）。

为什么必须严格比参数：
    concat 只是把一段段码流接在一起，解码器仍然按第一条流的 SPS/PPS、
    时间基、分辨率来解。分辨率 / 编码 / 像素格式 / 帧率 / 声道数任何一项
    不同，轻则花屏、音画不同步，重则整段无法解码。所以这里不做「差不多
    就合」的猜测，只按可验证的参数分组。

兼容性判定（硬条件）：
    视频编码、宽高、像素格式、profile / level、帧率、SAR、扫描方式、旋转、
    HDR / 杜比视界、音轨列表（编码 / 采样率 / 声道数 / 声道布局 / profile）。
    容器不参与硬条件：mp4 / mov / mkv / ts / webm 编码参数一致就归同组，
    混合容器先逐段统一重整为 mkv 再拼接。
    色彩标记、codec tag、编码头长度、VFR 等只作为组内警告，不拆组。

输出：
    成品每组一个文件，命名 `<首片段名>_合并.<容器>`，写到「输出目录」；
    输出目录留空时写到该组首个片段所在目录。默认勾选「合并后移动源视频到」：
    合并成功后，只把该组实际参与合并的源视频移到目标——路径留空时每个源
    视频各自移到「所在目录\已合并」，指定目录则全部汇到该目录；未参与合并
    的文件、未选中的单文件组、以及刚生成的成品都不移动。取消勾选则不移动。
    默认容器：mp4/mov 组且编码兼容 → mp4；ts 组 → ts；webm 组 → webm；
    其余 → mkv。字幕轨只在组内结构完全一致时保留。
    可选「添加书签（章节）」默认关闭；开启后每个实际参与合并的片段开始处
    写一个章节，标题=对应源文件名，mp4 / mkv 成品支持。

直接拼接失败时（个别文件时间戳不干净）会自动把每个片段重新封装成
临时 mkv（仍是 `-c copy`，不转码），再拼接重试一次。
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import traceback
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, messagebox

from . import ui
from .common import (BgJob, add_table_hscroll, clean_path, clear_log,
                     get_unique_path, load_done_sound, make_logger,
                     natural_key, paste_path_to, play_done_sound,
                     save_done_sound, setup_tree_columns)
from .theme import COLOR, FONTS
from .tab_encode import (VIDEO_EXTS, ffmpeg_version, kill_process,
                         load_ffmpeg_hint, locate_ffmpeg, save_ffmpeg_hint)

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MERGED_DIR_NAME = "已合并"

# 表格列（名称, 标题, 默认宽）
COLS = (
    ("chk", "选", 32),
    ("no", "#", 42),
    ("name", "文件 / 分组", 230),
    ("dur", "总时长", 72),
    ("res", "分辨率", 92),
    ("fps", "帧率", 52),
    ("vcodec", "视频编码", 100),
    ("audio", "音频", 120),
    ("size", "大小", 64),
    ("status", "兼容性 / 状态", 212),
)
COLS_MIN = {"chk": 28, "no": 36, "name": 160, "dur": 64, "res": 80,
            "fps": 44, "vcodec": 80, "audio": 90, "size": 58, "status": 160}

# 输出容器能力（只用于决定「同参数组」优先输出哪个容器，不参与兼容性判定）
MP4_VIDEO_OK = {"h264", "hevc", "av1", "mpeg4", "mjpeg"}
MP4_AUDIO_OK = {"aac", "mp3", "mp2", "ac3", "eac3", "alac"}
MP4_SUB_OK = {"mov_text", "tx3g"}
TS_VIDEO_OK = {"h264", "hevc", "mpeg2video", "av1"}
TS_AUDIO_OK = {"aac", "mp3", "mp2", "ac3", "eac3"}
WEBM_VIDEO_OK = {"vp8", "vp9", "av1"}
WEBM_AUDIO_OK = {"opus", "vorbis"}
WEBM_SUB_OK = {"webvtt"}

CONTAINER_LABEL = {"mp4": "MP4/MOV", "mkv": "MKV", "ts": "TS", "webm": "WebM",
                   "mpg": "MPG", "avi": "AVI", "flv": "FLV", "asf": "WMV/ASF"}

VIDEO_CODEC_LABEL = {"h264": "H.264", "hevc": "H.265", "av1": "AV1",
                     "vp8": "VP8", "vp9": "VP9", "mpeg2video": "MPEG-2",
                     "mpeg4": "MPEG-4", "vc1": "VC-1", "wmv3": "WMV3",
                     "rv40": "RealVideo", "theora": "Theora"}


# ============================================================
# 小工具
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


def _ratio(v):
    """把 30/1、60/2 这类帧率统一成最简分数串；0/0 返回空。"""
    s = str(v or "").strip()
    try:
        f = Fraction(s)
        if f.numerator == 0 or f.denominator == 0:
            return ""
        return "%d/%d" % (f.numerator, f.denominator)
    except Exception:
        return ""


def _fps_value(v):
    s = _ratio(v)
    if not s:
        return 0.0
    n, _, d = s.partition("/")
    try:
        return float(n) / float(d)
    except Exception:
        return 0.0


def _fps_text(v):
    f = _fps_value(v)
    if not f:
        return "—"
    if abs(f - round(f)) < 0.01:
        return str(int(round(f)))
    return ("%.3f" % f).rstrip("0").rstrip(".")


def _norm_sar(v):
    s = str(v or "").strip().lower()
    if not s or s in ("n/a", "0:1", "0/1", "unknown"):
        return "1:1"
    return s


def _level_text(codec, level):
    level = _to_int(level)
    if not level:
        return ""
    if codec == "hevc":
        return ("%.1f" % (level / 30.0)).rstrip("0").rstrip(".")
    if codec in ("h264", "av1"):
        return ("%.1f" % (level / 10.0)).rstrip("0").rstrip(".")
    return str(level)


def _codec_label(codec):
    return VIDEO_CODEC_LABEL.get((codec or "").lower(), (codec or "?").upper())


def _fmt_dur(sec):
    sec = max(0.0, _to_float(sec))
    if sec <= 0:
        return "—"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    if h:
        return "%d:%02d:%04.1f" % (h, m, s)
    return "%02d:%04.1f" % (m, s)


def _fmt_size(n):
    n = max(0, _to_float(n))
    if n <= 0:
        return "—"
    if n >= 1024 ** 3:
        return "%.2f GB" % (n / 1024 ** 3)
    if n >= 1024 ** 2:
        return "%.1f MB" % (n / 1024 ** 2)
    return "%.0f KB" % (n / 1024)


def _rotation_of(stream):
    rot = 0
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            rot = _to_int(sd.get("rotation"))
            break
    tags = stream.get("tags") or {}
    if not rot and tags.get("rotate"):
        rot = _to_int(tags.get("rotate"))
    rot %= 360
    if rot > 180:
        rot -= 360
    return rot


def _dovi_of(stream):
    for sd in stream.get("side_data_list") or []:
        if sd.get("side_data_type") == "DOVI configuration record":
            return (_to_int(sd.get("dv_profile")), _to_int(sd.get("dv_level")),
                    _to_int(sd.get("dv_bl_signal_compatibility_id")))
    return None


def _container_family(path, fmt_name):
    """把 ffprobe 的 format_name + 扩展名归成「容器家族」。

    mp4 / mov / m4v / 3gp 同属 mov 家族，拼接实测兼容；
    mkv 与 webm 虽然都是 matroska，但 WebM 对编码有限制，必须分开。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".mp4", ".m4v", ".mov", ".3gp", ".3g2"):
        return "mp4"
    if ext == ".mkv":
        return "mkv"
    if ext == ".webm":
        return "webm"
    if ext in (".ts", ".m2ts", ".mts"):
        return "ts"
    if ext in (".mpg", ".mpeg", ".vob", ".m2v"):
        return "mpg"
    if ext == ".avi":
        return "avi"
    if ext in (".flv", ".f4v"):
        return "flv"
    if ext in (".wmv", ".asf"):
        return "asf"
    first = (fmt_name or "").split(",")[0].strip().lower()
    return first or ext.lstrip(".") or "unknown"


def _audio_sig(s):
    return ((s.get("codec_name") or "").lower(),
            str(s.get("sample_rate") or ""),
            _to_int(s.get("channels")),
            (s.get("channel_layout") or "").lower(),
            (s.get("profile") or "").lower())


def _audio_label(sigs):
    if not sigs:
        return "无音轨"
    codec = (sigs[0][0] or "?").upper()
    rate = sigs[0][1]
    try:
        rate = "%dk" % (int(rate) // 1000)
    except Exception:
        rate = rate or "?"
    ch = "%dch" % sigs[0][2] if sigs[0][2] else "?"
    return "%s %s %s%s" % (codec, rate, ch, (" ×%d" % len(sigs)) if len(sigs) > 1 else "")


def _fmt_audio_detail(sigs):
    """音频轨完整参数，供运行日志排查兼容性用。"""
    if not sigs:
        return "0条"
    parts = []
    for i, s in enumerate(sigs, 1):
        codec = (s[0] or "?").upper()
        rate = s[1] or "?"
        channels = s[2] or 0
        layout = s[3] or ""
        profile = s[4] or ""
        desc = "#%d %s %sHz %dch" % (i, codec, rate, channels)
        if layout:
            desc += " %s" % layout
        if profile:
            desc += " profile=%s" % profile
        parts.append(desc)
    return "%d条[%s]" % (len(parts), " | ".join(parts))


def _fmt_sub_detail(info):
    subs = info.get("sub_sigs") or []
    if not subs:
        return "0条"
    parts = []
    for i, s in enumerate(subs, 1):
        codec = s[0] or "?"
        lang = s[1] or "未标语言"
        parts.append("#%d %s %s" % (i, codec, lang))
    return "%d条[%s]" % (len(parts), " | ".join(parts))


def _fmt_color_detail(info):
    trc, primaries, space, color_range = info.get("color_key") or ("", "", "", "")
    dovi = info.get("dovi")
    if dovi:
        base = "DoVi(profile=%s level=%s bl=%s)" % (
            dovi[0] or "?", dovi[1] or "?", dovi[2] or "?")
    elif info.get("hdr"):
        base = "HDR"
    else:
        base = "SDR"
    return "%s(transfer=%s primaries=%s space=%s range=%s)" % (
        base, trc or "—", primaries or "—", space or "—", color_range or "—")


def _fmt_info_detail(it):
    """把一个文件的探测结果压成一行；字段名固定，方便直接发出来分析。"""
    info = it.get("info") or {}
    order = _to_int(it.get("order")) + 1
    path = it.get("path") or ""
    name = os.path.basename(path)
    if not info.get("ok"):
        return "[参数失败] #%d %s | 路径=%s | 原因=%s" % (
            order, name, path, info.get("error") or "未知")
    vcodec = info.get("vcodec") or ""
    profile = info.get("profile") or "—"
    level = _to_int(info.get("level"))
    level_txt = _level_text(vcodec, level)
    level_part = ("%s(%d)" % (level_txt, level)) if level_txt else "—"
    fam = info.get("family") or "?"
    fam_label = CONTAINER_LABEL.get(fam, fam.upper())
    fmt_name = info.get("format_name") or "—"
    r_rate = info.get("r_rate") or "—"
    avg_rate = info.get("avg_rate") or "—"
    fps_part = "%s(r=%s avg=%s %s)" % (
        _fps_text(info.get("r_rate")), r_rate, avg_rate,
        "VFR" if info.get("vfr") else "CFR")
    tag = info.get("codec_tag") or "—"
    extra = info.get("extradata")
    if extra is None:
        extra = "—"
    return (
        "[参数] #%d %s | 路径=%s | 容器=%s(%s) | 视频=%s(%s) profile=%s "
        "level=%s %dx%d %s fps=%s SAR=%s 扫描=%s 旋转=%d° | 色彩=%s | "
        "编码头=tag=%s extradata=%s | 音频=%s | 字幕=%s | 额外视频轨=%s | "
        "时长=%.3fs(%s) | 大小=%s(%d bytes)" % (
            order, name, path, fam_label, fmt_name,
            _codec_label(vcodec), vcodec or "?", profile, level_part,
            info.get("width") or 0, info.get("height") or 0,
            info.get("pix_fmt") or "—", fps_part,
            info.get("sar") or "—", info.get("field_order") or "—",
            _to_int(info.get("rotation")),
            _fmt_color_detail(info), tag, extra,
            _fmt_audio_detail(info.get("audio_sigs") or ()),
            _fmt_sub_detail(info),
            "是" if info.get("extra_video") else "否",
            _to_float(info.get("duration")), _fmt_dur(info.get("duration")),
            _fmt_size(info.get("size")), _to_int(info.get("size"))))


def log_group_details(groups, log):
    """把参数分组、每组签名、警告、排除原因和成员选择完整写入日志。"""
    groups = list(groups or [])
    ok_groups = [g for g in groups if not g.get("error")]
    error_groups = [g for g in groups if g.get("error")]
    n_items = sum(len(g.get("items") or []) for g in groups)
    log("========== 分组详情 ==========")
    log("文件 %d 个 | 参数组 %d 个 | 无法读取 %d 个" % (
        n_items, len(ok_groups), sum(len(g.get("items") or []) for g in error_groups)))
    if not groups:
        log("（没有文件）")
        return
    for g in groups:
        if g.get("error"):
            log("[无法读取] %d 个文件（不参与合并）" % len(g.get("items") or []))
            for it in g.get("items") or []:
                log("    ✗ %s | 原因=%s" % (
                    it.get("path") or "",
                    (it.get("info") or {}).get("error") or "未知"))
            continue
        inc = [it for it in (g.get("items") or []) if it.get("include")]
        if not g.get("merge"):
            status = "不参与合并"
        elif len(g.get("items") or []) == 1:
            status = "单文件单独输出"
        else:
            status = "无损合并"
        if len(inc) < len(g.get("items") or []):
            status += "（组内剔除 %d 个）" % (len(g.get("items") or []) - len(inc))
        log("[组%d] %d 个片段 | %s | 参与 %d 个 | 合计 %s / %s | 输出=%s" % (
            g.get("gid", 0), len(g.get("items") or []), status, len(inc),
            _fmt_dur(g.get("total_dur")), _fmt_size(g.get("total_size")),
            g.get("out_suffix") or "—"))
        log("  签名=%s" % (g.get("sig") or "—"))
        log("  混合容器=%s | 字幕轨保留=%s" % (
            "是" if g.get("mixed_container") else "否",
            "是" if g.get("preserve_subs") else "否"))
        if g.get("reason"):
            log("  拆分依据=%s" % g["reason"])
        else:
            log("  拆分依据=首个参数组")
        if g.get("warnings"):
            log("  组内警告=%s" % "；".join(g["warnings"]))
        if g.get("key"):
            log("  内部签名键=%r" % (g["key"],))
        log("  成员：")
        for seq, it in enumerate(g.get("items") or [], 1):
            log("    %s #%d %s" % (
                "☑参与" if it.get("include") else "☐剔除", seq,
                it.get("path") or ""))


def _nearest_existing_dir(path):
    """返回 path 自身或最近的已存在父目录；用于给尚未创建的输出目录测空间。"""
    p = os.path.abspath(path or ".")
    while p:
        if os.path.isdir(p):
            return p
        parent = os.path.dirname(p)
        if not parent or parent == p:
            return ""
        p = parent
    return ""


def _volume_key(path):
    """把同一磁盘的不同输出目录归并成一个 key，便于累计所需空间。"""
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if drive:
            return os.path.normcase(drive)
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return os.path.normcase(str(path))


def check_disk_space(groups, outdir):
    """估算本次实际参与合并的成品大小，并检查对应输出盘剩余空间。

    返回 (按磁盘汇总的报告列表, 最大单组临时副本大小)。
    need 比源文件总和多留 3% + 1MB，避免容器头 / 索引 / 对齐开销卡边界。
    """
    per_volume = {}
    max_group_need = 0
    for g in groups:
        inc = [it for it in (g.get("items") or []) if it.get("include")]
        if not inc:
            continue
        target = outdir or os.path.dirname(inc[0]["path"])
        raw = sum(max(0, _to_int((it.get("info") or {}).get("size"))) for it in inc)
        need = int(raw * 1.03) + 1024 * 1024
        max_group_need = max(max_group_need, need)
        key = _volume_key(target)
        if key not in per_volume:
            per_volume[key] = {"target": target, "need": 0, "free": None}
        per_volume[key]["need"] += need

    reports = []
    for rec in per_volume.values():
        probe_dir = _nearest_existing_dir(rec["target"])
        free = None
        if probe_dir:
            try:
                free = shutil.disk_usage(probe_dir).free
            except Exception:
                free = None
        rec["free"] = free
        rec["ok"] = True if free is None else free >= rec["need"] + 512 * 1024 * 1024
        reports.append(rec)
    return reports, max_group_need


def check_temp_space(max_group_need):
    """检查自动重整重试时临时目录是否有空间；不阻止直接拼接。"""
    if max_group_need <= 0:
        return None
    try:
        temp_dir = tempfile.gettempdir()
        free = shutil.disk_usage(temp_dir).free
    except Exception:
        return None
    reserve = 512 * 1024 * 1024
    return {"target": temp_dir, "need": max_group_need, "free": free,
            "short": max(0, max_group_need + reserve - free)}




# ============================================================
# 逐文件探测
# ============================================================

def probe_concat_source(ffprobe, path):
    """读取拼接所需的详细参数；失败返回 {'ok': False, 'error': ...}。"""
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
            creationflags=_NO_WINDOW)
        data = json.loads((r.stdout or b"").decode("utf-8", "ignore") or "{}")
    except Exception as e:
        return {"ok": False, "error": "读取失败：%s" % e}
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        return {"ok": False, "error": (err[-1] if err else "ffprobe 返回 %s" % r.returncode)}

    streams = data.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")]
    if not videos:
        return {"ok": False, "error": "没有视频流（可能是纯音频 / 封面图文件）"}
    v = videos[0]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    fmt = data.get("format") or {}

    codec = (v.get("codec_name") or "").lower()
    pix_fmt = (v.get("pix_fmt") or "").lower()
    profile = (v.get("profile") or "").lower()
    level = _to_int(v.get("level"))
    width = _to_int(v.get("width"))
    height = _to_int(v.get("height"))
    r_rate = _ratio(v.get("r_frame_rate")) or _ratio(v.get("avg_frame_rate"))
    avg_rate = _ratio(v.get("avg_frame_rate")) or r_rate
    vfr = bool(_fps_value(avg_rate) and _fps_value(r_rate)
               and abs(_fps_value(avg_rate) - _fps_value(r_rate)) /
               max(_fps_value(avg_rate), _fps_value(r_rate)) > 0.01)
    sar = _norm_sar(v.get("sample_aspect_ratio"))
    field_order = (v.get("field_order") or "unknown").lower()
    rotation = _rotation_of(v)
    color_trc = (v.get("color_transfer") or "").lower()
    hdr = color_trc in ("smpte2084", "arib-std-b67")
    dovi = _dovi_of(v)
    color_key = ((v.get("color_transfer") or "").lower(),
                 (v.get("color_primaries") or "").lower(),
                 (v.get("color_space") or "").lower(),
                 (v.get("color_range") or "").lower())
    codec_tag = (v.get("codec_tag_string") or "").lower()
    extradata = _to_int(v.get("extradata_size"))

    audio_sigs = tuple(_audio_sig(s) for s in audios)
    sub_sigs = tuple(((s.get("codec_name") or "").lower(),
                      ((s.get("tags") or {}).get("language") or "").lower())
                     for s in subs)
    sub_codes = tuple(x[0] for x in sub_sigs)

    family = _container_family(path, fmt.get("format_name") or "")
    duration = _to_float(fmt.get("duration"))
    if not duration:
        duration = _to_float(v.get("duration"))
    try:
        size = _to_int(fmt.get("size")) or os.path.getsize(path)
    except Exception:
        size = 0

    sig_key = (
        codec, width, height, pix_fmt, profile, level,
        r_rate, sar, field_order, rotation,
        "dv:%s:%s:%s" % dovi if dovi else ("hdr" if hdr else "sdr"),
        audio_sigs,
    )
    info = {
        "ok": True, "path": path, "name": os.path.basename(path),
        "family": family, "format_name": fmt.get("format_name") or "",
        "duration": duration, "size": size,
        "vcodec": codec, "profile": profile, "level": level,
        "width": width, "height": height, "pix_fmt": pix_fmt,
        "r_rate": r_rate, "avg_rate": avg_rate, "fps": _fps_value(r_rate),
        "vfr": vfr, "sar": sar, "field_order": field_order,
        "rotation": rotation, "hdr": hdr, "dovi": dovi,
        "color_key": color_key, "codec_tag": codec_tag,
        "extradata": extradata,
        "audio_sigs": audio_sigs, "has_audio": bool(audios),
        "sub_sigs": sub_sigs, "sub_codes": sub_codes,
        "extra_video": len(videos) > 1,
        "sig_key": sig_key,
    }
    return info


# ============================================================
# 兼容性比较 / 分组
# ============================================================

def sig_diff(a, b):
    """返回 a 与 b 的硬性不兼容项（人类可读），空列表表示可同一组。"""
    diffs = []

    def add(label, av, bv):
        if av != bv:
            diffs.append("%s %s ≠ %s" % (label, av if av != "" else "无",
                                         bv if bv != "" else "无"))

    add("编码", _codec_label(a["vcodec"]), _codec_label(b["vcodec"]))
    if (a["width"], a["height"]) != (b["width"], b["height"]):
        diffs.append("分辨率 %dx%d ≠ %dx%d" % (a["width"], a["height"],
                                               b["width"], b["height"]))
    add("像素格式", a["pix_fmt"], b["pix_fmt"])
    add("profile", a["profile"] or "未知", b["profile"] or "未知")
    la, lb = _level_text(a["vcodec"], a["level"]), _level_text(b["vcodec"], b["level"])
    if la != lb:
        diffs.append("level %s ≠ %s" % (la or "未知", lb or "未知"))
    if _fps_value(a["r_rate"]) != _fps_value(b["r_rate"]):
        diffs.append("帧率 %s ≠ %s" % (_fps_text(a["r_rate"]), _fps_text(b["r_rate"])))
    add("像素比", a["sar"], b["sar"])
    add("扫描方式", a["field_order"], b["field_order"])
    if a["rotation"] != b["rotation"]:
        diffs.append("旋转 %d° ≠ %d°" % (a["rotation"], b["rotation"]))
    if a["dovi"] != b["dovi"]:
        diffs.append("杜比视界 %s ≠ %s" % ("有" if a["dovi"] else "无",
                                            "有" if b["dovi"] else "无"))
    elif bool(a["hdr"]) != bool(b["hdr"]):
        diffs.append("色彩 %s ≠ %s" % ("HDR" if a["hdr"] else "SDR",
                                       "HDR" if b["hdr"] else "SDR"))
    if a["audio_sigs"] != b["audio_sigs"]:
        diffs.append("音轨 %s ≠ %s" % (_audio_label(a["audio_sigs"]),
                                       _audio_label(b["audio_sigs"])))
    return diffs


def sig_warnings(a, b):
    """不影响拼接、但值得提示的差异（组内警告）。"""
    warns = []
    ca, cb = a["color_key"], b["color_key"]
    if ca and cb and ca != cb:
        warns.append("色彩标记不同")
    if a["codec_tag"] and b["codec_tag"] and a["codec_tag"] != b["codec_tag"]:
        warns.append("封装 tag 不同")
    if a["extradata"] and b["extradata"] and a["extradata"] != b["extradata"]:
        warns.append("编码头长度不同")
    if a["vfr"] != b["vfr"]:
        warns.append("VFR/CFR 混合")
    return warns


def _group_signature(g):
    it = g["items"][0]
    info = it["info"]
    res = "%dx%d" % (info["width"], info["height"])
    vcodec = _codec_label(info["vcodec"])
    if info["profile"]:
        vcodec += " " + info["profile"].replace("main", "Main").replace("high", "High")
    family = "混合容器" if g.get("mixed_container") else CONTAINER_LABEL.get(
        info["family"], info["family"])
    return "%s · %s · %s · %s · %s" % (
        family, vcodec, res,
        _fps_text(info["r_rate"]) + "fps", _audio_label(info["audio_sigs"]))


def output_suffix(g):
    """按组内编码选一个能装下这些码流的容器；装不下就统一用 mkv。"""
    infos = [it["info"] for it in g["items"]]
    families = {i["family"] for i in infos}
    fam = infos[0]["family"]
    vcodecs = {i["vcodec"] for i in infos}
    acodecs = {a[0] for i in infos for a in i["audio_sigs"]}
    preserve_subs = bool(g.get("preserve_subs"))
    subcodes = {c for i in infos for c in i["sub_codes"]} if preserve_subs else set()

    # 混合容器（mp4 + mkv 等）无法直接 concat，统一先重整为 mkv
    if len(families) > 1:
        return ".mkv"
    if fam == "mp4" and vcodecs <= MP4_VIDEO_OK and acodecs <= MP4_AUDIO_OK \
            and subcodes <= MP4_SUB_OK:
        return ".mp4"
    if fam == "ts" and vcodecs <= TS_VIDEO_OK and acodecs <= TS_AUDIO_OK \
            and not preserve_subs:
        return ".ts"
    if fam == "webm" and vcodecs <= WEBM_VIDEO_OK and acodecs <= WEBM_AUDIO_OK \
            and subcodes <= WEBM_SUB_OK:
        return ".webm"
    return ".mkv"


# 章节（书签）只写入这些容器；ts / webm 没有可靠的章节支持，直接跳过。
BOOKMARK_SUFFIXES = {".mp4", ".mkv"}


def _ffmeta_title(name):
    """转义 ffmetadata 的标题值；文件名里的反斜杠 / 换行不能破坏格式。"""
    return str(name or "").replace("\\", "\\\\").replace("\r", " ").replace("\n", " ")


def write_chapter_metadata(path, items):
    """按实际参与合并的片段写 ffmetadata 章节，标题 = 源文件名。

    章节起点用各组片段的累计时长；第一段从 0 开始，最后一段到总时长。
    ffmpeg 用 `-map_chapters` 把这些章节写进成品，不额外生成文件。
    """
    total = sum(max(0.0, _to_float(it["info"].get("duration"))) for it in items)
    total_ms = max(1, int(round(total * 1000)))
    lines = [";FFMETADATA1"]
    cursor_ms = 0
    for idx, it in enumerate(items):
        if idx == len(items) - 1:
            end_ms = total_ms
        else:
            end_ms = cursor_ms + max(1, int(round(
                max(0.0, _to_float(it["info"].get("duration"))) * 1000)))
        end_ms = max(cursor_ms + 1, end_ms)
        title = it["info"].get("name") or os.path.basename(it.get("path") or "")
        lines.extend([
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            "START=%d" % cursor_ms,
            "END=%d" % end_ms,
            "title=%s" % _ffmeta_title(title),
            "",
        ])
        cursor_ms = end_ms
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return len(items)


def _chapter_input_args(path):
    """返回 ffmetadata 章节输入的参数；没有章节时为空。"""
    if not path:
        return []
    return ["-f", "ffmetadata", "-i", path, "-map_chapters", "1"]


def _count_chapters(ffprobe, path):
    """读取成品章节数量；失败返回 -1，不影响成品保留。"""
    if not ffprobe:
        return -1
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_chapters", "-of", "json", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
            creationflags=_NO_WINDOW)
        data = json.loads((r.stdout or b"").decode("utf-8", "ignore") or "{}")
        return len(data.get("chapters") or [])
    except Exception:
        return -1


def build_groups(ordered_items, single_default=False, prefs=None):
    """按硬参数精确分组；返回按首个片段顺序排列的 group 列表。

    同 key 的文件自然落到同一组，组内仍按文件名顺序；文件参数和谁都不
    一样时单独成组。这样即使 X、Y 两种参数交替出现（A-X, B-Y, C-X），
    A/C 仍能拼成一组、B 单独一组，不会因为中间夹了一个不兼容文件就
    把后面所有同参数文件都拆散。
    """
    prefs = prefs or {}
    buckets = {}
    errors = []
    for it in ordered_items:
        info = it["info"]
        if not info or not info.get("ok"):
            errors.append(it)
            continue
        key = info["sig_key"]
        g = buckets.get(key)
        if g is None:
            g = {"items": [], "key": key}
            buckets[key] = g
        g["items"].append(it)

    groups = sorted(buckets.values(), key=lambda g: min(it["order"] for it in g["items"]))
    for gid, g in enumerate(groups, 1):
        g["gid"] = gid
        g["error"] = False
        g["merge"] = prefs.get(g["key"], len(g["items"]) >= 2 or single_default)
        first = g["items"][0]["info"]

        # 组内警告 + 字幕轨策略
        warnings = []
        for it in g["items"][1:]:
            for w in sig_warnings(first, it["info"]):
                if w not in warnings:
                    warnings.append(w)
        sub_sigs = [it["info"]["sub_sigs"] for it in g["items"]]
        preserve_subs = bool(sub_sigs and sub_sigs[0]
                             and all(s == sub_sigs[0] for s in sub_sigs))
        if any(s != sub_sigs[0] for s in sub_sigs) and any(sub_sigs):
            if "字幕轨结构不一致，输出不带字幕" not in warnings:
                warnings.append("字幕轨结构不一致，输出不带字幕")
        if any(it["info"]["extra_video"] for it in g["items"]):
            warnings.append("含额外视频轨，只合并首条视频")
        families = {it["info"]["family"] for it in g["items"]}
        g["mixed_container"] = len(families) > 1
        if g["mixed_container"]:
            warnings.append("容器不同，先统一重整为 MKV 再拼接")
        g["preserve_subs"] = preserve_subs
        g["warnings"] = warnings

        if gid > 1:
            prev = groups[gid - 2]
            diffs = sig_diff(prev["items"][0]["info"], first)
            g["reason"] = "与组%d不兼容：%s" % (
                prev["gid"], "；".join(diffs[:3]) if diffs else "参数不同")
        else:
            g["reason"] = ""

        g["total_dur"] = sum(_to_float(it["info"]["duration"]) for it in g["items"])
        g["total_size"] = sum(_to_float(it["info"]["size"]) for it in g["items"])
        g["sig"] = _group_signature(g)
        g["out_suffix"] = output_suffix(g)
        for it in g["items"]:
            it["group"] = g
        if g["merge"]:
            if not any(it.get("include") for it in g["items"]):
                for it in g["items"]:
                    it["include"] = True
        else:
            for it in g["items"]:
                it["include"] = False

    # 读取失败的文件单独放在最后一个「无法读取」组，不参与合并
    if errors:
        g = {"gid": 0, "items": errors, "error": True, "merge": False,
             "warnings": [], "preserve_subs": False, "reason": "无法读取参数",
             "total_dur": 0, "total_size": 0, "out_suffix": ".mkv",
             "sig": "无法读取"}
        for it in errors:
            it["group"] = g
            it["include"] = False
        groups.append(g)
    return groups


# ============================================================
# 拼接执行
# ============================================================

def _ffconcat_line(path):
    """写 concat 列表的一行：绝对路径转 /，单引号按 ffmpeg 的转义规则处理。"""
    p = os.path.abspath(path).replace("\\", "/")
    p = p.replace("'", "'\\''")
    return "file '%s'" % p


def _write_concat_list(paths, list_path):
    lines = [_ffconcat_line(p) for p in paths]
    with open(list_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def _kill_tree(proc):
    kill_process(proc)


def _run_quiet(cmd, err_path, stop_event):
    """跑一个只关心成功/失败的命令；stderr 落文件，支持停止。返回 (rc, tail)。"""
    with open(err_path, "wb") as ef:
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=ef,
                                 stderr=subprocess.STDOUT,
                                 creationflags=_NO_WINDOW)
        except Exception as e:
            return -1, str(e)
        while p.poll() is None:
            if stop_event is not None and stop_event.is_set():
                _kill_tree(p)
                break
            time.sleep(0.2)
        try:
            rc = p.wait(timeout=10)
        except Exception:
            rc = -1
    tail = ""
    try:
        with open(err_path, "rb") as f:
            data = f.read()
        tail = "\n".join(data.decode("utf-8", "ignore").splitlines()[-8:])
    except Exception:
        pass
    return rc, tail


def _run_concat(cmd, total_dur, on_progress, stop_event, procs, log):
    """跑拼接命令并解析 -progress pipe:1；返回 (rc, 错误尾部)。"""
    tail = []
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             bufsize=1, creationflags=_NO_WINDOW)
    except Exception as e:
        return -1, "启动 ffmpeg 失败：%s" % e
    procs.add(p)
    speed = ""
    try:
        for raw in p.stdout:
            if stop_event.is_set():
                _kill_tree(p)
                break
            line = (raw or "").strip()
            if not line:
                continue
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                us = _to_int(line.partition("=")[2])
                if total_dur > 0 and us >= 0:
                    on_progress(min(0.999, (us / 1e6) / total_dur), speed)
            elif line.startswith("speed="):
                speed = line.partition("=")[2].strip()
            elif line.startswith("progress="):
                if line.endswith("end") and total_dur > 0:
                    on_progress(1.0, speed)
            elif line.startswith(("frame=", "fps=", "stream_0_0_q=", "bitrate=",
                                  "total_size=", "out_time=", "dup_frames=",
                                  "drop_frames=")):
                pass
            else:
                tail.append(line)
                if len(tail) > 40:
                    del tail[:20]
        rc = p.wait()
    except Exception:
        _kill_tree(p)
        rc = -1
        tail.append(traceback.format_exc())
    finally:
        procs.discard(p)
    return rc, "\n".join(tail[-12:])


def _map_args(preserve_subs):
    args = ["-map", "0:v:0", "-map", "0:a?"]
    if preserve_subs:
        args += ["-map", "0:s?"]
    return args


def _normalize_one(ffmpeg, src, dst, preserve_subs, stop_event):
    """把单个片段重新封装成只含目标轨的 mkv（copy，不转码）。"""
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", src,
           "-map", "0:v:0", "-map", "0:a?"]
    if preserve_subs:
        cmd += ["-map", "0:s?"]
    cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero",
            "-f", "matroska", dst]
    err_path = dst + ".err.txt"
    rc, tail = _run_quiet(cmd, err_path, stop_event)
    return rc, tail


def _probe_duration(ffprobe, path):
    """读成品时长；读不到返回 0。"""
    if not ffprobe:
        return 0.0
    try:
        cp = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15, creationflags=_NO_WINDOW,
        )
        return _to_float((cp.stdout or "").strip())
    except Exception:
        return 0.0


def _short_output(ffprobe, path, expected):
    """成品明显短于源总时长时返回描述，否则空字符串。

    concat 遇到坏时间戳时 ffmpeg 可能返回 0 但只写出前一两段；这里用
    0.75 倍 + 2 秒双阈值，避开 AAC priming / VFR 导致的正常小幅偏差。
    """
    if not ffprobe or expected <= 2:
        return ""
    actual = _probe_duration(ffprobe, path)
    if actual > 0 and actual < expected * 0.75 and expected - actual > 2:
        return "成品时长异常：%.1fs，源总时长约 %.1fs" % (actual, expected)
    return ""


def default_merged_folder(path):
    """默认的「已合并」目录：视频所在目录\\已合并；已经在该目录里就不再套一层。"""
    d = os.path.dirname(os.path.abspath(path))
    if os.path.basename(d) == MERGED_DIR_NAME:
        return d
    return os.path.join(d, MERGED_DIR_NAME)


def move_group_sources(items, move_on, move_dir, log):
    """合并成功后，只移动这一组实际参与合并的源视频；返回 (成功数, 失败数)。

    未参与合并的文件、以及合并出来的成品都不在这里处理。
    """
    if not move_on:
        return 0, 0
    moved = failed = 0
    for it in items:
        src = it["path"]
        if not os.path.isfile(src):
            log("  ⚠ 源视频不存在，跳过移动：%s" % src)
            continue
        dest_dir = move_dir or default_merged_folder(src)
        try:
            src_dir = os.path.abspath(os.path.dirname(src))
            dest_abs = os.path.abspath(dest_dir)
            if src_dir == dest_abs:
                log("  源视频已在目标目录，跳过：%s" % os.path.basename(src))
                continue
            os.makedirs(dest_dir, exist_ok=True)
            dst = str(get_unique_path(Path(dest_dir) / os.path.basename(src)))
            if os.path.abspath(dst) == os.path.abspath(src):
                continue
            shutil.move(src, dst)
            it["moved_to"] = dst
            moved += 1
            log("  移动源视频：%s → %s" % (os.path.basename(src), dst))
        except Exception as e:
            failed += 1
            log("  ⚠ 移动源视频失败：%s（%s）" % (os.path.basename(src), e))
    return moved, failed


def merge_group(ffmpeg, group, out_dir, stop_event, procs, on_progress, log,
                on_phase=None, ffprobe=None, add_bookmarks=False):
    """合并一个组；返回 (ok, msg, 输出路径)。"""

    def phase(msg):
        try:
            if on_phase:
                on_phase(msg)
        except Exception:
            pass

    if not ffprobe and ffmpeg:
        cand = os.path.join(os.path.dirname(os.path.abspath(ffmpeg)), "ffprobe.exe")
        if os.path.isfile(cand):
            ffprobe = cand
    items = [it for it in group["items"] if it.get("include")]
    if not items:
        return False, "组内没有选中的文件", ""
    paths = [it["path"] for it in items]
    # 按「实际参与的文件」重新算容器与字幕策略：用户可能剔除了组里
    # 唯一一个不同容器 / 不同字幕结构的片段。
    sub_sigs = [it["info"]["sub_sigs"] for it in items]
    preserve_subs = bool(sub_sigs and sub_sigs[0]
                         and all(s == sub_sigs[0] for s in sub_sigs))
    mixed_container = len({it["info"]["family"] for it in items}) > 1
    suffix = output_suffix({"items": [{"info": it["info"]} for it in items],
                            "preserve_subs": preserve_subs})

    # 输出文件名：首片段名 + _合并；同名自动 _1/_2，不覆盖
    stem = os.path.splitext(os.path.basename(paths[0]))[0][:120]
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        return False, "无法创建输出目录：%s" % e, ""
    out_path = str(get_unique_path(Path(out_dir) / ("%s_合并%s" % (stem, suffix))))

    total_dur = sum(_to_float(it["info"].get("duration")) for it in items)
    work = tempfile.mkdtemp(prefix="vmerge_")

    # 书签（章节）：标题 = 该片段源文件名；容器不支持时跳过，不影响合并。
    bookmark_meta = None
    if add_bookmarks:
        if suffix in BOOKMARK_SUFFIXES:
            try:
                bookmark_meta = os.path.join(work, "chapters.txt")
                n_ch = write_chapter_metadata(bookmark_meta, items)
                log("  书签：%d 个章节（标题=源文件名）" % n_ch)
            except Exception as e:
                bookmark_meta = None
                log("  ⚠ 书签写入失败，已跳过：%s" % e)
        else:
            log("  书签：输出容器 %s 不支持章节，已跳过" % suffix)

    def verify_bookmarks(path):
        if not bookmark_meta:
            return
        n = _count_chapters(ffprobe, path)
        if n < 0:
            log("  书签校验：无法读取成品章节")
        elif n == 0:
            log("  ⚠ 书签校验：成品里没有读到章节")
        else:
            log("  书签校验：%d 个章节" % n)

    def drop_bad_output():
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

    def normalize_all():
        """逐片段重整为 mkv（copy），返回 (路径列表, 错误)。"""
        norm_paths = []
        for idx, src in enumerate(paths, 1):
            if stop_event.is_set():
                return [], "已停止"
            phase("正在重整 %d/%d：%s" % (idx, len(paths), os.path.basename(src)))
            log("  重整 %d/%d：%s" % (idx, len(paths), os.path.basename(src)))
            dst = os.path.join(work, "n%02d.mkv" % idx)
            rc2, tail2 = _normalize_one(ffmpeg, src, dst, preserve_subs, stop_event)
            if rc2 != 0 or not os.path.isfile(dst):
                e2 = tail2.strip().splitlines()[-1] if tail2.strip() else "返回码 %s" % rc2
                return [], "整理片段失败（%s）：%s" % (os.path.basename(src), e2)
            norm_paths.append(dst)
        return norm_paths, ""

    try:
        if len(items) == 1:
            # 单文件：直接重新封装（不经过 concat 列表）
            cmd = [ffmpeg, "-hide_banner", "-v", "warning", "-y", "-i", paths[0]]
            cmd += _chapter_input_args(bookmark_meta)
            cmd += _map_args(preserve_subs)
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero",
                    "-progress", "pipe:1", "-stats_period", "0.5", out_path]
            log("  ffmpeg：%s" % " ".join('"%s"' % c if " " in c else c for c in cmd))
            rc, tail = _run_concat(cmd, total_dur, on_progress, stop_event, procs, log)
            if rc == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                verify_bookmarks(out_path)
                return True, "", out_path
            first_err = tail.strip().splitlines()[-1] if tail.strip() else "返回码 %s" % rc
            drop_bad_output()
            if stop_event.is_set():
                return False, "已停止", ""
            return False, "重新封装失败：%s" % first_err, ""

        first_err = ""
        if not mixed_container:
            # 同容器先直接拼接（最快，只读一遍写一遍）
            list_path = os.path.join(work, "concat.txt")
            _write_concat_list(paths, list_path)
            cmd = [ffmpeg, "-hide_banner", "-v", "warning", "-y",
                   "-f", "concat", "-safe", "0", "-i", list_path]
            cmd += _chapter_input_args(bookmark_meta)
            cmd += _map_args(preserve_subs)
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero",
                    "-progress", "pipe:1", "-stats_period", "0.5", out_path]
            log("  ffmpeg：%s" % " ".join('"%s"' % c if " " in c else c for c in cmd))
            rc, tail = _run_concat(cmd, total_dur, on_progress, stop_event, procs, log)
            if rc == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                bad = _short_output(ffprobe, out_path, total_dur)
                if not bad:
                    verify_bookmarks(out_path)
                    return True, "", out_path
                first_err = bad
                log("  直接拼接%s" % bad)
                drop_bad_output()
                if stop_event.is_set():
                    return False, "已停止", ""
            else:
                first_err = tail.strip().splitlines()[-1] if tail.strip() else "返回码 %s" % rc
                drop_bad_output()
                if stop_event.is_set():
                    return False, "已停止", ""
            log("  直接拼接失败（%s），正在自动整理各段时间戳后重试…" % first_err)
        else:
            log("  组内容器不一致，先统一重整为 MKV 再拼接（仍是 copy，不转码）…")

        # 统一重整后拼接：修正容器 / 时间戳 / 轨道结构差异
        norm_paths, err = normalize_all()
        if err:
            return False, err, ""
        list_path = os.path.join(work, "concat_norm.txt")
        _write_concat_list(norm_paths, list_path)
        cmd2 = [ffmpeg, "-hide_banner", "-v", "warning", "-y",
                "-f", "concat", "-safe", "0", "-i", list_path]
        cmd2 += _chapter_input_args(bookmark_meta)
        cmd2 += _map_args(preserve_subs)
        cmd2 += ["-c", "copy", "-avoid_negative_ts", "make_zero",
                 "-progress", "pipe:1", "-stats_period", "0.5", out_path]
        log("  ffmpeg（重整后）：%s" % " ".join(
            '"%s"' % c if " " in c else c for c in cmd2))
        rc, tail = _run_concat(cmd2, total_dur, on_progress, stop_event, procs, log)
        if rc == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            bad = _short_output(ffprobe, out_path, total_dur)
            if not bad:
                verify_bookmarks(out_path)
                if first_err:
                    log("  已通过「整理时间戳后重试」完成")
                return True, "", out_path
            last_err = bad
            drop_bad_output()
            return False, last_err, ""
        drop_bad_output()
        last_err = tail.strip().splitlines()[-1] if tail.strip() else "返回码 %s" % rc
        if stop_event.is_set():
            return False, "已停止", ""
        return False, "重整后仍失败：%s" % last_err, ""
    finally:
        try:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass


# ============================================================
# 页面
# ============================================================

def build_video_merge_tab(parent, app=None):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(4, weight=1)

    state = {
        "ffmpeg": "", "ffprobe": "",
        "items": {},          # iid -> item
        "order": [],          # 按文件名排序后的 item 列表
        "groups": [],
        "item_group": {},     # iid -> group
        "tree_group": {},     # tree parent iid -> group
        "group_pref": {},     # sig_key -> 组是否参与（新增同签名文件时保留选择）
        "running": False,
        "stop": threading.Event(),
        "procs": set(),
        "seq": 0,
        "checked": False,
        "warned_missing": False,
        "mode": "flat",
        "last_outputs": [],
        # 列表代际：清空后旧扫描 / 旧探测结果一律丢弃，避免列表被重新填回来
        "list_epoch": 0,
        "scan_jobs": [],
    }
    probe_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vmerge-probe")

    def run_ui(fn):
        try:
            parent.after(0, fn)
        except Exception:
            pass

    CTL_H = 30
    F_LABEL = FONTS["sm"]

    def field_label(master, text):
        return ui.Label(master, text=text, font=F_LABEL, auto_wrap=False,
                        foreground=COLOR["text"])

    def hint_label(master, text, color=None):
        return ui.Label(master, text=text, font=F_LABEL, auto_wrap=False,
                        foreground=color or COLOR["text_muted"])

    def check(master, text, var, pad=(0, 0)):
        cb = ui.Checkbutton(master, text=text, variable=var, font=F_LABEL,
                            height=CTL_H, checkbox_width=18, checkbox_height=18)
        cb.pack(side=tk.LEFT, padx=pad)
        return cb

    # ---------------- 顶部 FFmpeg ----------------
    ff_row = ui.Frame(parent)
    ff_row.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
    ff_row.grid_columnconfigure(1, weight=1)
    field_label(ff_row, "FFmpeg：").grid(row=0, column=0, sticky="w", padx=(2, 6))
    var_ff = tk.StringVar()
    ent_ff = ui.Entry(ff_row, textvariable=var_ff, height=CTL_H, font=F_LABEL)
    ent_ff.grid(row=0, column=1, sticky="ew", padx=(0, 6))
    lbl_ff = ui.Label(ff_row, text="检测中…", font=F_LABEL, auto_wrap=False,
                      foreground=COLOR["text_muted"])
    lbl_ff.grid(row=0, column=4, sticky="w", padx=(8, 8))

    def _pick_ff():
        p = filedialog.askopenfilename(
            title="选择 ffmpeg.exe",
            filetypes=[("ffmpeg", "ffmpeg.exe"), ("所有文件", "*.*")])
        if not p:
            return
        var_ff.set(p)
        save_ffmpeg_hint(p)
        apply_ffmpeg()

    ui.Button(ff_row, text="浏览…", width=7, height=CTL_H, font=F_LABEL,
              command=_pick_ff).grid(row=0, column=2, padx=2)
    btn_redetect = ui.Button(ff_row, text="重新检测", width=9, height=CTL_H,
                             font=F_LABEL, command=lambda: apply_ffmpeg())
    btn_redetect.grid(row=0, column=3, padx=2)

    # ---------------- 设置卡 ----------------
    card = ui.Frame(parent, fg_color=COLOR["surface"], border_width=1,
                    border_color=COLOR["border"], corner_radius=8)
    card.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
    card.grid_columnconfigure(1, weight=1)
    PAD = 12

    ui.Label(card, text="合并设置", font=F_LABEL, auto_wrap=False,
             foreground=COLOR["text_secondary"]).grid(
        row=0, column=0, sticky="w", padx=(PAD, 0), pady=(8, 6))
    hint_label(card, "按文件名自然排序 · 不转码").grid(
        row=0, column=3, sticky="e", padx=(0, PAD), pady=(8, 6))

    out_line = ui.Frame(card)
    out_line.grid(row=1, column=0, columnspan=4, sticky="ew", padx=PAD, pady=(0, 6))
    field_label(out_line, "输出目录").pack(side=tk.LEFT, padx=(0, 6))
    var_outdir = tk.StringVar()
    ui.Entry(out_line, textvariable=var_outdir, width=30,
             height=CTL_H, font=F_LABEL).pack(side=tk.LEFT)
    ui.Button(out_line, text="粘贴", width=5, height=CTL_H, font=F_LABEL,
              command=lambda: paste_path_to(var_outdir)).pack(side=tk.LEFT, padx=(6, 0))
    ui.Button(out_line, text="浏览…", width=7, height=CTL_H, font=F_LABEL,
              command=lambda: var_outdir.set(
                  filedialog.askdirectory(title="选择输出目录") or var_outdir.get())).pack(
        side=tk.LEFT, padx=6)
    hint_label(out_line, "留空 = 每组首个片段所在目录").pack(
        side=tk.LEFT, padx=(0, 18))

    move_line = ui.Frame(card)
    move_line.grid(row=2, column=0, columnspan=4, sticky="ew", padx=PAD, pady=(0, 6))
    var_move = tk.BooleanVar(value=True)
    check(move_line, "合并后移动源视频到", var_move, pad=(0, 8))
    var_move_dir = tk.StringVar()
    ui.Entry(move_line, textvariable=var_move_dir, width=30,
             height=CTL_H, font=F_LABEL).pack(side=tk.LEFT)
    ui.Button(move_line, text="粘贴", width=5, height=CTL_H, font=F_LABEL,
              command=lambda: paste_path_to(var_move_dir)).pack(side=tk.LEFT, padx=(6, 0))
    ui.Button(move_line, text="浏览…", width=7, height=CTL_H, font=F_LABEL,
              command=lambda: var_move_dir.set(
                  filedialog.askdirectory(title="选择源视频移动目录") or var_move_dir.get())).pack(
        side=tk.LEFT, padx=6)
    hint_label(move_line, "留空 = 每个源视频所在目录\\%s" % MERGED_DIR_NAME).pack(
        side=tk.LEFT, padx=(0, 18))

    run_line = ui.Frame(card)
    run_line.grid(row=3, column=0, columnspan=4, sticky="w", padx=PAD, pady=(0, 8))
    hint_label(run_line, "只复制视频 + 全部音轨；字幕轨仅当组内结构一致时保留。").pack(
        side=tk.LEFT, padx=(0, 18))
    var_single = tk.BooleanVar(value=False)
    check(run_line, "不兼容的单文件也单独输出", var_single, pad=(0, 18))
    var_bookmarks = tk.BooleanVar(value=False)
    check(run_line, "添加书签（章节）", var_bookmarks, pad=(0, 18))
    hint_label(run_line, "标题=对应源文件名").pack(side=tk.LEFT, padx=(0, 18))

    # ---------------- 工具条 ----------------
    list_bar = ui.Frame(parent)
    list_bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 3))
    lbl_count = ui.Label(list_bar, text="共 0 个视频", font=F_LABEL, auto_wrap=False,
                         foreground=COLOR["text_secondary"])
    lbl_count.pack(side=tk.LEFT, padx=(2, 8))

    def _add_files():
        paths = filedialog.askopenfilenames(
            title="选择视频",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.flv *.ts *.m2ts *.mts "
                                   "*.avi *.wmv *.webm *.m4v *.mpg *.mpeg *.vob "
                                   "*.3gp *.rmvb *.f4v"),
                       ("所有文件", "*.*")])
        if paths:
            add_paths(list(paths))

    def _add_folder():
        d = filedialog.askdirectory(title="选择视频文件夹")
        if d:
            add_paths([d])

    ui.Button(list_bar, text="添加文件", height=CTL_H, font=F_LABEL,
              command=_add_files).pack(side=tk.LEFT, padx=(0, 6))
    ui.Button(list_bar, text="添加文件夹", height=CTL_H, font=F_LABEL,
              command=_add_folder).pack(side=tk.LEFT, padx=(0, 6))
    ui.Button(list_bar, text="移除选中", height=CTL_H, font=F_LABEL,
              command=lambda: remove_selected()).pack(side=tk.LEFT, padx=(0, 6))
    ui.Button(list_bar, text="清空列表", height=CTL_H, font=F_LABEL,
              command=lambda: clear_list()).pack(side=tk.LEFT, padx=(0, 6))
    ui.Button(list_bar, text="重新检测", height=CTL_H, font=F_LABEL,
              command=lambda: reprobe_all()).pack(side=tk.LEFT, padx=(0, 6))
    hint_label(list_bar, "勾选 = 合并，取消 = 剔除").pack(side=tk.LEFT, padx=(10, 0))
    hint_label(list_bar, "可拖放视频 / 文件夹").pack(side=tk.LEFT, padx=(14, 0))

    # ---------------- 表格 ----------------
    tree_frame = ui.Frame(parent)
    tree_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=2)
    tree_frame.grid_rowconfigure(0, weight=1)
    tree_frame.grid_columnconfigure(0, weight=1)
    tree = ui.Treeview(tree_frame, columns=[c[0] for c in COLS], show="headings",
                       selectmode="extended", height=12)
    for key, title, width in COLS:
        tree.heading(key, text=title)
        tree.column(key, width=width, minwidth=COLS_MIN.get(key, 50),
                    stretch=(key in ("name", "status")))
    ui.attach_tree_hover(tree)
    vsb = ui.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    add_table_hscroll(tree, tree_frame, row=1, column=0)
    setup_tree_columns(tree, "videomerge.main.v1")
    tree.tag_configure("group", background=COLOR["surface_alt"])
    tree.tag_configure("muted", foreground=COLOR["text_muted"])
    tree.tag_configure("warn", foreground=COLOR["warning"])
    tree.tag_configure("error", foreground=COLOR["error"])
    tree.tag_configure("ok", foreground=COLOR["success"])

    # ---------------- 选中项说明 ----------------
    detail_bar = ui.Frame(parent)
    detail_bar.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 2))
    lbl_detail = ui.Label(detail_bar, text="选中组行查看拆分原因 / 兼容性警告",
                          font=F_LABEL, auto_wrap=False, anchor="w",
                          foreground=COLOR["text_muted"])
    lbl_detail.pack(side=tk.LEFT, fill="x", expand=True)

    def on_tree_select(event=None):
        sel = tree.selection()
        if not sel:
            return
        iid = sel[0]
        g = state["tree_group"].get(iid)
        it = None
        if g is None:
            it = state["items"].get(iid)
            g = it.get("group") if it else None
        if g is None:
            lbl_detail.config(text="逐文件读取参数中…",
                              foreground=COLOR["text_muted"])
            return
        head = "无法读取" if g.get("error") else "组%d" % g["gid"]
        if it is not None:
            head = os.path.basename(it["path"])
        color = COLOR["text_secondary"]
        if g.get("error"):
            color = COLOR["error"]
        elif not g["merge"] or g["warnings"]:
            color = COLOR["warning"]
        lbl_detail.config(text="%s · %s" % (head, group_status(g)),
                          foreground=color)

    tree.bind("<<TreeviewSelect>>", on_tree_select, add="+")

    # ---------------- 按钮 / 进度 ----------------
    btn_row = ui.Frame(parent)
    btn_row.grid(row=6, column=0, sticky="ew", padx=8, pady=4)
    btn_start = ui.Button(btn_row, text="开始合并", bg=COLOR["accent"],
                          fg=COLOR["text_on_accent"],
                          disabledforeground=COLOR["text_on_accent"],
                          activebackground=COLOR["accent_active"],
                          activeforeground=COLOR["text_on_accent"],
                          font=FONTS["base"], padx=18, pady=4,
                          cursor="hand2", command=lambda: start_batch())
    btn_start.pack(side=tk.LEFT, padx=4)
    btn_stop = ui.Button(btn_row, text="停止", state=tk.DISABLED, padx=16, pady=4,
                         command=lambda: (state["stop"].set(),
                                          log("正在停止：当前 ffmpeg 会被结束…")))
    btn_stop.pack(side=tk.LEFT, padx=4)

    bar_prog = ui.ProgressBar(btn_row, mode="determinate", maximum=1, value=0,
                              width=300)
    bar_prog.pack(side=tk.LEFT, padx=(18, 8))
    lbl_prog = ui.Label(btn_row, text="未开始", font=F_LABEL, auto_wrap=False,
                        foreground=COLOR["text_secondary"])
    lbl_prog.pack(side=tk.LEFT)

    lbl_sum = ui.Label(btn_row, text="", font=F_LABEL, auto_wrap=False,
                       foreground=COLOR["text_muted"])
    lbl_sum.pack(side=tk.RIGHT, padx=(8, 2))

    # ---------------- 日志 ----------------
    log_wrap = ui.Frame(parent)
    log_wrap.grid(row=7, column=0, sticky="ew", padx=8, pady=(0, 6))
    log_wrap.grid_columnconfigure(0, weight=1)
    log_head = ui.Frame(log_wrap)
    log_head.grid(row=0, column=0, sticky="ew")
    var_log_open = tk.BooleanVar(value=False)
    btn_log_toggle = ui.Button(log_head, text="▸ 运行日志", width=11,
                               height=CTL_H, font=F_LABEL)
    btn_log_toggle.pack(side=tk.LEFT)

    def copy_log_text():
        try:
            txt = text_log.get("1.0", "end-1c")
            if not txt.strip():
                log("日志为空，没有可复制的内容")
                return
            text_log.clipboard_clear()
            text_log.clipboard_append(txt)
            log("日志已复制到剪贴板（%d 字符）" % len(txt))
        except Exception as e:
            log("复制日志失败：%s" % e)

    ui.Button(log_head, text="复制", width=6, height=CTL_H, font=F_LABEL,
              command=copy_log_text).pack(side=tk.LEFT, padx=(6, 0))
    ui.Button(log_head, text="清空", width=6, height=CTL_H, font=F_LABEL,
              command=lambda: clear_log(text_log)).pack(side=tk.LEFT, padx=6)

    log_body = ui.Frame(log_wrap)
    log_body.grid(row=1, column=0, sticky="nsew", pady=(2, 0))
    log_body.grid_rowconfigure(0, weight=1)
    log_body.grid_columnconfigure(0, weight=1)
    text_log = ui.Textbox(log_body, height=10, state="disabled", wrap=tk.WORD,
                          font=FONTS["log"])
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
    log_body.grid_remove()

    # ============================================================
    # 列表与探测
    # ============================================================

    def sorted_items():
        return sorted(state["items"].values(),
                      key=lambda it: (natural_key(os.path.basename(it["path"])),
                                      it["path"].lower()))

    def resort():
        order = sorted_items()
        for i, it in enumerate(order):
            it["order"] = i
        state["order"] = order

    def included(g):
        return [it for it in g["items"] if it.get("include")]

    def maybe_regroup():
        if state["running"]:
            return
        if any(it["probe_state"] == "pending" for it in state["items"].values()):
            return
        regroup()

    def fill_item_flat(it):
        info = it.get("info")
        if info and info.get("ok"):
            res = "%dx%d" % (info["width"], info["height"])
            vals = ["", str(it["order"] + 1), os.path.basename(it["path"]),
                    _fmt_dur(info["duration"]), res, _fps_text(info["r_rate"]),
                    _codec_label(info["vcodec"]), _audio_label(info["audio_sigs"]),
                    _fmt_size(info["size"]), "读取中…"]
        else:
            vals = ["", str(it["order"] + 1), os.path.basename(it["path"]),
                    "…", "…", "…", "…", "…", _fmt_size(os.path.getsize(it["path"])
                                                        if os.path.exists(it["path"]) else 0),
                    "读取信息…" if it["probe_state"] == "pending" else "无法读取"]
        return vals

    def rebuild_tree():
        old_rows = tree.get_children()
        if old_rows:
            tree.delete(*old_rows)
        state["tree_group"].clear()
        if state["mode"] == "flat" or not state["groups"]:
            state["mode"] = "flat"
            for it in state["order"]:
                tree.insert("", tk.END, iid=it["iid"], values=fill_item_flat(it))
            lbl_detail.config(
                text="逐文件读取参数中…" if state["items"] else
                "拖入视频后自动检测兼容性",
                foreground=COLOR["text_muted"])
            return
        state["mode"] = "grouped"
        for g in state["groups"]:
            key = "grp_%d" % g["gid"] if not g["error"] else "grp_error"
            state["tree_group"][key] = g
            parent_iid = key
            tree.insert("", tk.END, iid=parent_iid, values=group_values(g),
                        tags=group_tags(g), open=True)
            for seq, it in enumerate(g["items"], 1):
                it["seq"] = seq
                tree.insert(parent_iid, tk.END, iid=it["iid"], values=child_values(it),
                            tags=child_tags(it))
        kids = tree.get_children()
        if kids:
            tree.selection_set(kids[0])
            tree.focus(kids[0])

    def group_chk(g):
        if g["error"]:
            return ""
        inc = included(g)
        if not g["merge"] or not inc:
            return "☐"
        return "☑" if len(inc) == len(g["items"]) else "◐"

    def group_status(g):
        if g["error"]:
            return "无法读取参数，不参与合并"
        n = len(g["items"])
        if not g["merge"]:
            base = "已剔除，不参与合并"
            if g["reason"]:
                base += "；" + g["reason"]
            if g["warnings"]:
                base += "　⚠ " + "；".join(g["warnings"][:2])
            return base
        if n == 1:
            base = "将单独输出（仅重新封装，不转码）"
            if g["reason"]:
                base += "；" + g["reason"]
        else:
            base = "可无损合并"
            if g["reason"]:
                base += "；" + g["reason"]
        if g["warnings"]:
            base += "　⚠ " + "；".join(g["warnings"][:2])
        return base

    def group_name(g):
        base = "%d 个片段" % len(g["items"])
        if g["warnings"]:
            base += " ⚠"
        return base

    def group_values(g):
        if g["error"]:
            return [group_chk(g), "×", "%d 个文件" % len(g["items"]),
                    "—", "—", "—", "—", "—", "—", group_status(g)]
        first = g["items"][0]["info"]
        res = "%dx%d" % (first["width"], first["height"])
        vcodec = _codec_label(first["vcodec"])
        if first["profile"]:
            vcodec += " " + first["profile"].capitalize()
        return [group_chk(g), "组%d" % g["gid"], group_name(g),
                _fmt_dur(g["total_dur"]), res, _fps_text(first["r_rate"]),
                vcodec, _audio_label(first["audio_sigs"]),
                _fmt_size(g["total_size"]), group_status(g)]

    def group_tags(g):
        if g["error"]:
            return ("group", "error")
        if not g["merge"]:
            return ("group", "muted")
        if g["warnings"]:
            return ("group", "warn")
        return ("group",)

    def child_status(it):
        g = it.get("group")
        if it["probe_state"] != "ok":
            return "无法读取：%s" % (it["info"].get("error", "") if it.get("info") else "")
        if not it.get("include"):
            return "已剔除（不参与）"
        if g and g["error"]:
            return "无法读取"
        if g and not g["merge"]:
            return "所在组已剔除"
        warns = []
        if g and len(g["items"]) > 1:
            for w in sig_warnings(g["items"][0]["info"], it["info"]):
                if w not in warns:
                    warns.append(w)
        if it["info"]["extra_video"]:
            warns.append("含额外视频轨")
        if warns:
            return "⚠ " + "；".join(warns[:2])
        return "✓ 与组内参数一致"

    def child_values(it):
        info = it.get("info") or {}
        if it["probe_state"] != "ok" or not info.get("ok"):
            return ["", str(it.get("seq", "")), os.path.basename(it["path"]),
                    "—", "—", "—", "—", "—", "—", child_status(it)]
        g = it.get("group")
        one = bool(g and len(g["items"]) == 1 and not g["error"])
        checked = bool(g["merge"]) if one else bool(it.get("include"))
        return ["☑" if checked else "☐", str(it.get("seq", "")),
                os.path.basename(it["path"]), _fmt_dur(info["duration"]),
                "%dx%d" % (info["width"], info["height"]),
                _fps_text(info["r_rate"]), _codec_label(info["vcodec"]),
                _audio_label(info["audio_sigs"]), _fmt_size(info["size"]),
                child_status(it)]

    def child_tags(it):
        g = it.get("group")
        if it["probe_state"] != "ok":
            return ("error",)
        if not it.get("include") or (g and not g["merge"]):
            return ("muted",)
        if g:
            for w in sig_warnings(g["items"][0]["info"], it["info"]):
                return ("warn",)
        return ()

    def update_tree_values():
        if state["mode"] != "grouped":
            return
        for g in state["groups"]:
            key = "grp_%d" % g["gid"] if not g["error"] else "grp_error"
            try:
                tree.item(key, values=group_values(g), tags=group_tags(g))
            except Exception:
                pass
            for it in g["items"]:
                try:
                    tree.item(it["iid"], values=child_values(it), tags=child_tags(it))
                except Exception:
                    pass
        if tree.selection():
            on_tree_select()

    def update_summary():
        pending = sum(1 for it in state["items"].values()
                      if it["probe_state"] == "pending")
        failed = sum(1 for it in state["items"].values()
                     if it["probe_state"] == "fail")
        groups = [g for g in state["groups"] if not g["error"]]
        run_groups = [g for g in groups if g["merge"] and included(g)]
        n_out = len(run_groups)
        n_files = sum(len(included(g)) for g in run_groups)
        txt = ""
        if groups:
            txt = "%d 组 · 输出 %d 个文件（%d 段）" % (len(groups), n_out, n_files)
        if pending:
            txt = (txt + " · " if txt else "") + "读取中 %d" % pending
        elif failed:
            txt = (txt + " · " if txt else "") + "读取失败 %d" % failed
        elif not state["items"]:
            txt = "拖入视频后自动检测兼容性"
        lbl_count.config(text="共 %d 个视频" % len(state["items"]))
        lbl_sum.config(text=txt)
        return run_groups

    def regroup():
        resort()
        state["groups"] = build_groups(state["order"], var_single.get(),
                                       state["group_pref"])
        state["item_group"] = {}
        for g in state["groups"]:
            for it in g["items"]:
                state["item_group"][it["iid"]] = g
        state["mode"] = "grouped"
        rebuild_tree()
        update_summary()
        if state["items"]:
            log_group_details(state["groups"], log)

    def on_probe_done(iid):
        it = state["items"].get(iid)
        if not it:
            return
        if state["mode"] == "flat":
            try:
                tree.item(iid, values=fill_item_flat(it))
            except Exception:
                pass
        if not any(x["probe_state"] == "pending" for x in state["items"].values()):
            regroup()

    def queue_probe(iid):
        it = state["items"].get(iid)
        if not it:
            return
        it["probe_state"] = "pending"
        it["probe_seq"] = _to_int(it.get("probe_seq")) + 1
        token = it["probe_seq"]
        epoch = state["list_epoch"]
        path = it["path"]

        def work():
            ff, fp = state["ffmpeg"], state["ffprobe"]
            if not ff or not fp:
                ff, fp = locate_ffmpeg(load_ffmpeg_hint())
            info = probe_concat_source(fp, path) if fp else {
                "ok": False, "error": "未找到 ffprobe.exe"}
            def apply():
                if epoch != state["list_epoch"]:
                    return
                cur = state["items"].get(iid)
                if not cur or cur.get("probe_seq") != token:
                    return
                cur["info"] = info
                cur["probe_state"] = "ok" if info.get("ok") else "fail"
                log(_fmt_info_detail(cur))
                on_probe_done(iid)
            run_ui(apply)

        probe_pool.submit(work)

    # ============================================================
    # 列表操作
    # ============================================================

    def add_paths(paths):
        if state["running"]:
            messagebox.showwarning("提示", "正在合并，请先停止。")
            return
        paths = [p for p in (paths or []) if p]
        if not paths:
            return
        epoch = state["list_epoch"]

        def apply(files):
            if epoch != state["list_epoch"]:
                log("列表已清空，忽略本次扫描结果")
                return
            if not files:
                log("没有找到支持的视频文件（" + " ".join(VIDEO_EXTS) + "）")
                return
            known = {it["path"].lower() for it in state["items"].values()}
            added = []
            for f in files:
                if f.lower() in known:
                    continue
                known.add(f.lower())
                state["seq"] += 1
                iid = "vm%d" % state["seq"]
                state["items"][iid] = {
                    "iid": iid, "path": f, "probe_state": "pending",
                    "info": None, "include": True, "order": 0, "seq": 0,
                    "group": None, "probe_seq": 0,
                }
                added.append(iid)
            if not added:
                log("这些视频已经在列表里了")
                return
            resort()
            state["mode"] = "flat"
            rebuild_tree()
            update_summary()
            log("已添加 %d 个视频，正在后台读取参数…" % len(added))
            for iid in added:
                queue_probe(iid)

        log("正在扫描文件夹…")
        job = None

        def scan(j):
            if j.cancelled:
                return []
            return collect_merge_paths(paths)

        def finish(files):
            try:
                state["scan_jobs"].remove(job)
            except ValueError:
                pass
            apply(files)

        def scan_error(exc):
            try:
                state["scan_jobs"].remove(job)
            except ValueError:
                pass
            if epoch == state["list_epoch"]:
                log("扫描失败：%s" % exc)

        job = BgJob(parent, work=scan, on_done=finish, on_error=scan_error,
                    interval=40)
        state["scan_jobs"].append(job)
        job.start()

    def remove_selected():
        if state["running"]:
            messagebox.showwarning("提示", "正在合并，请先停止。")
            return
        sels = list(tree.selection())
        if not sels:
            return
        removed = 0
        for iid in sels:
            g = state["tree_group"].get(iid)
            if g:
                for it in list(g["items"]):
                    state["items"].pop(it["iid"], None)
                    removed += 1
                continue
            it = state["items"].get(iid)
            if it:
                state["items"].pop(iid, None)
                removed += 1
        if not removed:
            return
        log("已移除 %d 个文件" % removed)
        if any(it["probe_state"] == "pending" for it in state["items"].values()):
            state["mode"] = "flat"
            resort()
            rebuild_tree()
            update_summary()
        else:
            regroup()

    def clear_list():
        if state["running"]:
            messagebox.showwarning("提示", "正在合并，请先停止。")
            return
        jobs = list(state["scan_jobs"])
        state["scan_jobs"].clear()
        for job in jobs:
            try:
                job.cancel()
            except Exception:
                pass
        # 代际 +1：清空前已经排队的扫描结果、参数探测结果全部作废
        state["list_epoch"] += 1
        state["items"].clear()
        state["order"] = []
        state["groups"] = []
        state["item_group"].clear()
        state["tree_group"].clear()
        state["group_pref"].clear()
        state["mode"] = "flat"
        state["last_outputs"] = []
        rebuild_tree()
        update_summary()
        lbl_detail.config(text="拖入视频后自动检测兼容性", foreground=COLOR["text_muted"])
        log("列表已清空" + ("，已取消 %d 个未完成扫描" % len(jobs) if jobs else ""))

    def reprobe_all():
        if state["running"]:
            messagebox.showwarning("提示", "正在合并，请先停止。")
            return
        if not state["items"]:
            return
        for it in state["items"].values():
            it["info"] = None
            it["probe_state"] = "pending"
        state["mode"] = "flat"
        rebuild_tree()
        log("正在重新读取 %d 个视频的参数…" % len(state["items"]))
        for it in state["order"]:
            queue_probe(it["iid"])

    # ============================================================
    # 选择（点击「选」列）
    # ============================================================

    def toggle_group(g):
        if g["error"]:
            return
        g["merge"] = not g["merge"]
        if g["merge"]:
            for it in g["items"]:
                it["include"] = True
        else:
            for it in g["items"]:
                it["include"] = False
        state["group_pref"][g["key"]] = g["merge"]
        update_tree_values()
        update_summary()

    def toggle_item(iid):
        if state["mode"] != "grouped":
            return
        it = state["items"].get(iid)
        if not it:
            return
        g = it.get("group")
        if not g or g["error"]:
            return
        if len(g["items"]) == 1:
            want = not bool(g["merge"])
            it["include"] = want
            g["merge"] = want
        else:
            it["include"] = not it.get("include", True)
            if it["include"]:
                g["merge"] = True
            if not included(g):
                g["merge"] = False
        state["group_pref"][g["key"]] = g["merge"]
        update_tree_values()
        update_summary()

    def on_tree_click(event):
        if tree.identify_region(event.x, event.y) != "cell":
            return
        if tree.identify_column(event.x) != "#1":
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        g = state["tree_group"].get(iid)
        if g is not None:
            toggle_group(g)
        else:
            toggle_item(iid)
        return "break"

    tree.bind("<Button-1>", on_tree_click, add="+")

    def set_all_merge(flag):
        for g in state["groups"]:
            if g["error"]:
                continue
            if flag and len(g["items"]) < 2:
                continue
            g["merge"] = flag
            if flag:
                for it in g["items"]:
                    it["include"] = True
            state["group_pref"][g["key"]] = flag
        update_tree_values()
        update_summary()

    def on_single_toggle():
        for g in state["groups"]:
            if g["error"] or len(g["items"]) != 1:
                continue
            g["merge"] = var_single.get()
            if g["merge"]:
                g["items"][0]["include"] = True
            state["group_pref"][g["key"]] = g["merge"]
        update_tree_values()
        update_summary()

    var_single.trace_add("write", lambda *_: on_single_toggle())

    # ============================================================
    # FFmpeg 检测
    # ============================================================

    def apply_ffmpeg():
        ff, fp = locate_ffmpeg(clean_path(var_ff.get()))
        state["ffmpeg"], state["ffprobe"] = ff, fp
        if not ff:
            lbl_ff.config(text="✗ 未找到 ffmpeg", foreground=COLOR["error"])
            log("未找到 ffmpeg：请把 ffmpeg.exe 放到程序目录 / bin\\ffmpeg，"
                "或点『浏览…』手动指定（同目录需有 ffprobe.exe）。")
            if not state["warned_missing"]:
                state["warned_missing"] = True
                messagebox.showwarning(
                    "未找到 FFmpeg",
                    "「视频合并」需要 FFmpeg（ffmpeg.exe + ffprobe.exe）。\n\n"
                    "补救方法（任选其一）：\n"
                    "  1. 把 ffmpeg.exe、ffprobe.exe 放到本程序同目录；\n"
                    "  2. 放到 程序目录\\bin\\ffmpeg\\ 下；\n"
                    "  3. 把 ffmpeg 加入系统 PATH；\n"
                    "  4. 点本页『浏览…』手动指定 ffmpeg.exe。\n\n"
                    "下载：https://www.gyan.dev/ffmpeg/builds/（release-full）")
            return
        var_ff.set(ff)
        ver = ffmpeg_version(ff)
        ver_txt = ""
        if ver:
            ver_txt = ver.replace("ffmpeg version ", "v").split(" Copyright")[0]
            ver_txt = ver_txt.split("-full")[0]
        lbl_ff.config(text=("✓ " + (ver_txt or "已找到 ffmpeg")),
                      foreground=COLOR["success"])
        log("FFmpeg：%s" % ff)
        if not fp:
            log("  警告：同目录未找到 ffprobe.exe，无法读取视频参数。")

    # ============================================================
    # 开始合并
    # ============================================================

    def start_batch():
        if state["running"]:
            return
        if any(it["probe_state"] == "pending" for it in state["items"].values()):
            messagebox.showwarning("提示", "还有文件在读取参数，请稍等。")
            return
        if not state["groups"]:
            messagebox.showwarning("提示", "请先添加视频。")
            return
        run_groups = [g for g in state["groups"]
                      if not g["error"] and g["merge"] and included(g)]
        if not run_groups:
            messagebox.showwarning("提示", "没有选中任何要合并的分组。")
            return
        ff = state["ffmpeg"] or locate_ffmpeg(load_ffmpeg_hint())[0]
        if not ff:
            messagebox.showwarning("提示", "未找到 ffmpeg，无法合并。")
            return
        outdir = clean_path(var_outdir.get())
        move_on = bool(var_move.get())
        move_dir = clean_path(var_move_dir.get())
        if outdir:
            if os.path.isfile(outdir):
                messagebox.showwarning("提示", "输出目录指向的是一个文件，请改成文件夹。")
                return
            try:
                os.makedirs(outdir, exist_ok=True)
            except Exception as e:
                messagebox.showwarning("提示", "无法创建输出目录：\n%s" % e)
                return
        if move_on and move_dir:
            if os.path.isfile(move_dir):
                messagebox.showwarning("提示", "源视频移动目录指向的是一个文件，请改成文件夹。")
                return
            try:
                os.makedirs(move_dir, exist_ok=True)
            except Exception as e:
                messagebox.showwarning("提示", "无法创建源视频移动目录：\n%s" % e)
                return

        log("========== 本次执行计划 ==========")
        for g in state["groups"]:
            if g.get("error"):
                log("[跳过] 无法读取组 | %d 个文件" % len(g.get("items") or []))
                continue
            inc = included(g)
            if g.get("merge") and inc:
                outdir_g = outdir or os.path.dirname(inc[0]["path"])
                log("[执行] 组%d | 参与 %d/%d | %s | 输出目录=%s" % (
                    g["gid"], len(inc), len(g["items"]), g.get("sig") or "—",
                    outdir_g))
                for it in inc:
                    log("    + #%d %s" % (_to_int(it.get("order")) + 1, it["path"]))
            else:
                reason = "组未勾选" if not g.get("merge") else "组内没有参与片段"
                log("[跳过] 组%d | %s | %d 个文件" % (
                    g["gid"], reason, len(g.get("items") or [])))
        log("=================================")

        space_reports, max_group_need = check_disk_space(run_groups, outdir)
        space_bad = []
        for r in space_reports:
            if r.get("free") is None:
                log("[空间] 输出目标=%s | 无法读取剩余空间，跳过预检" % r["target"])
                continue
            log("[空间] 输出目标=%s | 预计成品≈%s（含3%%余量） | 可用=%s" % (
                r["target"], _fmt_size(r["need"]), _fmt_size(r["free"])))
            if not r.get("ok"):
                space_bad.append(r)
        temp_warn = check_temp_space(max_group_need)
        if temp_warn and temp_warn.get("short", 0) > 0:
            log("[空间警告] 自动重整重试临时目录=%s | 预计临时副本≈%s | 可用=%s；"
                "直接拼接顺利时不会用到它" % (
                    temp_warn["target"], _fmt_size(temp_warn["need"]),
                    _fmt_size(temp_warn["free"])))
        if space_bad:
            lines = []
            for r in space_bad:
                gap = max(0, r["need"] + 512 * 1024 * 1024 - r["free"])
                lines.append("目标：%s\n预计需要：约 %s（含 3%% 余量）\n当前可用：%s\n缺口：约 %s" % (
                    r["target"], _fmt_size(r["need"]), _fmt_size(r["free"]),
                    _fmt_size(gap)))
            msg = ("输出磁盘空间可能不足，合并很可能在中途写满失败。\n\n"
                   + "\n\n".join(lines)
                   + "\n\n请把「输出目录」改到空间足够的磁盘，或先清理空间。")
            if temp_warn and temp_warn.get("short", 0) > 0:
                msg += ("\n\n另外，自动重整重试的临时目录 %s 也只剩 %s，"
                        "重试需要约 %s。" % (
                            temp_warn["target"], _fmt_size(temp_warn["free"]),
                            _fmt_size(temp_warn["need"])))
            log("⚠ 输出空间不足：\n" + "\n\n".join(lines))
            if not messagebox.askyesno("磁盘空间不足", msg + "\n\n是否仍要继续？",
                                       icon="warning", default="no"):
                log("已取消合并：输出空间不足")
                return
            log("已确认继续：输出空间不足")

        state["running"] = True
        state["stop"].clear()
        state["last_outputs"] = []
        btn_start.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
        bar_prog.set(0)
        total_groups = len(run_groups)
        total_dur = sum(g["total_dur"] for g in run_groups) or 1.0
        log("书签：%s" % ("开启（标题=源文件名，仅 mp4/mkv 支持）"
                            if var_bookmarks.get() else "关闭"))
        if move_on:
            log("合并成功后移动参与合并的源视频到：%s" % (
                move_dir or "每个源视频所在目录下的 %s" % MERGED_DIR_NAME))
        log("开始合并：%d 组 / %d 个片段" % (
            total_groups, sum(len(included(g)) for g in run_groups)))

        def work():
            done_dur = 0.0
            ok_n = fail_n = 0
            moved_n = move_fail_n = 0
            stopped = False
            outputs = []
            try:
                for idx, g in enumerate(run_groups, 1):
                    if state["stop"].is_set():
                        stopped = True
                        break
                    inc = included(g)
                    g_dur = max(1e-6, sum(_to_float(it["info"].get("duration")) for it in inc))
                    outdir_g = outdir or os.path.dirname(inc[0]["path"])
                    title = "组 %d：%d 个片段" % (g["gid"], len(inc))
                    run_ui(lambda t=title, i=idx: (
                        lbl_prog.config(text="正在合并 %d/%d · %s" % (i, total_groups, t)),
                        log("%s（%s）" % (t, _fmt_dur(g_dur)))))

                    def on_prog(pct, speed, base=done_dur, cur=g_dur, t=title, i=idx):
                        overall = (base + cur * max(0.0, min(1.0, pct))) / total_dur
                        run_ui(lambda: (
                            bar_prog.set(max(0.0, min(1.0, overall))),
                            lbl_prog.config(text="正在合并 %d/%d · %s %d%%%s" % (
                                i, total_groups, t, int(pct * 100),
                                ("  " + speed) if speed else ""))))

                    def on_phase(msg, i=idx, t=title):
                        run_ui(lambda: lbl_prog.config(
                            text="正在合并 %d/%d · %s · %s" % (
                                i, total_groups, t, msg)))

                    ok, msg, out_path = merge_group(
                        ff, g, outdir_g, state["stop"], state["procs"],
                        on_prog, log, on_phase, state["ffprobe"],
                        add_bookmarks=var_bookmarks.get())
                    done_dur += g_dur
                    if ok:
                        ok_n += 1
                        outputs.append(out_path)
                        files = len(inc)
                        log("✅ 组 %d 完成：%d 个片段 → %s（%s）" % (
                            g["gid"], files, out_path, _fmt_size(os.path.getsize(out_path))))
                        if move_on:
                            moved, moved_fail = move_group_sources(inc, move_on, move_dir, log)
                            moved_n += moved
                            move_fail_n += moved_fail
                        run_ui(lambda o=out_path: lbl_prog.config(
                            text="已完成：%s" % os.path.basename(o)))
                    else:
                        fail_n += 1
                        log("❌ 组 %d 失败：%s" % (g["gid"], msg))
                        run_ui(lambda m=msg: lbl_prog.config(text="失败：%s" % m))
                    run_ui(lambda: bar_prog.set(min(1.0, done_dur / total_dur)))
                if state["stop"].is_set():
                    stopped = True
            except Exception:
                log("合并线程出错：\n" + traceback.format_exc())
                fail_n += 1
            finally:
                state["last_outputs"] = outputs

                def finish():
                    state["running"] = False
                    state["stop"].clear()
                    btn_start.config(state=tk.NORMAL)
                    btn_stop.config(state=tk.DISABLED)
                    if moved_n:
                        # 源文件已经搬走，从待合并列表里移除，避免下次再点开始
                        # 时去读已经不存在的路径。
                        for iid, it in list(state["items"].items()):
                            if it.get("moved_to"):
                                state["items"].pop(iid, None)
                        try:
                            regroup()
                        except Exception:
                            pass
                    bar_prog.set(1.0 if not stopped and fail_n == 0 else
                                 min(1.0, done_dur / total_dur))
                    if stopped:
                        lbl_prog.config(text="已停止：完成 %d，失败 %d" % (ok_n, fail_n))
                    else:
                        lbl_prog.config(text="完成 %d 组，失败 %d 组" % (ok_n, fail_n))
                    log("总结：完成 %d 组，失败 %d 组%s" % (
                        ok_n, fail_n, "，已停止" if stopped else ""))
                    if move_on:
                        log("源视频移动：成功 %d 个，失败 %d 个" % (
                            moved_n, move_fail_n))
                    if outputs:
                        log("输出文件：")
                        for p in outputs:
                            log("  " + p)
                    if not stopped and fail_n == 0 and var_sound.get():
                        play_done_sound()
                        log("提示音已播放")

                run_ui(finish)

        threading.Thread(target=work, daemon=True).start()

    # 完成提示音
    var_sound = tk.BooleanVar(value=load_done_sound("videomerge"))
    cb_sound = ui.Checkbutton(run_line, text="完成后提示音", variable=var_sound,
                              font=F_LABEL, height=CTL_H,
                              checkbox_width=18, checkbox_height=18,
                              command=lambda: save_done_sound("videomerge", var_sound.get()))
    cb_sound.pack(side=tk.LEFT)

    # 拖放 / 显示回调
    var_ff.set(load_ffmpeg_hint())
    if app is not None:
        try:
            app.register_drop_handler(parent, add_paths)
        except Exception:
            pass

    def on_show():
        if not state["checked"]:
            state["checked"] = True
            apply_ffmpeg()
        update_summary()

    if app is not None:
        try:
            app.register_show_handler(parent, on_show)
        except Exception:
            parent.after(150, on_show)
    else:
        parent.after(150, on_show)

    rebuild_tree()
    update_summary()

    # 供自动化冒烟测试 / 其它模块使用
    state["add_paths"] = add_paths
    state["clear_list"] = clear_list
    state["start"] = start_batch
    state["regroup"] = regroup
    state["toggle_group"] = toggle_group
    state["toggle_item"] = toggle_item
    state["tree"] = tree
    state["log_text"] = text_log
    state["log"] = log
    state["copy_log"] = copy_log_text
    state["toggle_log"] = toggle_log
    state["var_single"] = var_single
    state["var_bookmarks"] = var_bookmarks
    state["var_sound"] = var_sound
    state["var_move"] = var_move
    state["var_move_dir"] = var_move_dir
    state["var_outdir"] = var_outdir
    return state


def collect_merge_paths(paths):
    """展开拖入的文件/文件夹，按文件名自然排序（只收视频）。"""
    files = []
    for p in paths:
        p = clean_path(p)
        if not p:
            continue
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                # 合并后源视频默认归档到「已合并」；重复拖入父目录时
                # 不把归档文件再次收进来
                dirs[:] = [d for d in sorted(dirs, key=natural_key)
                           if d != MERGED_DIR_NAME]
                for name in sorted(names, key=natural_key):
                    if name.lower().endswith(VIDEO_EXTS):
                        files.append(os.path.abspath(os.path.join(root, name)))
        elif os.path.isfile(p) and p.lower().endswith(VIDEO_EXTS):
            files.append(os.path.abspath(p))
    seen = set()
    out = []
    for f in files:
        k = f.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    out.sort(key=lambda p: (natural_key(os.path.basename(p)), p.lower()))
    return out
