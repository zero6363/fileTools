# -*- coding: utf-8 -*-
"""⑧ 视频横竖屏识别（只读容器头，不递归子文件夹，纯本地无依赖）

设计要点：
- 每种容器只读文件头即可拿到宽高，毫秒级；对 TS 需先解 PAT/PMT 再找视频
  PES 里的 SPS 解析（支持 H.264 / HEVC / MPEG-2）。
- 无第三方依赖、无外部程序，打包后即可用。
- 大文件（MKV/TS）按上限分段读取，避免把几个 GB 读进内存。
"""
import os
import shutil
import struct
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from .common import (DEFAULT_IMAGE_FORMATS, clean_path, clear_log,
                     get_unique_path, make_check, make_entry_row, make_form,
                     make_log_panel, make_num_row, make_path_row,
                     make_start_button, make_tip, run_task)

# 默认支持的视频格式（不含 rm/rmvb：RealMedia 头不含分辨率，会报无法识别）
DEFAULT_VIDEO_EXTS = ("mp4 m4v mov mkv webm ts mts m2ts flv avi wmv asf "
                      "mpg mpeg vob 3gp f4v m2v mpv tp trp")

_MP4_EXTS = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".f4v", ".m4a", ".m4b"}
_MKV_EXTS = {".mkv", ".webm", ".mka"}
_AVI_EXTS = {".avi", ".divx"}
_ASF_EXTS = {".wmv", ".asf", ".wma"}
_FLV_EXTS = {".flv"}
_TS_EXTS = {".ts", ".mts", ".m2ts", ".tp", ".trp"}
_PS_EXTS = {".mpg", ".mpeg", ".vob", ".ps", ".m2v", ".mpv", ".dat"}

_TS_MAX_READ = 24 << 20      # TS 最多读 24MB 找 SPS
_MKV_PREFIX = 8 << 20        # MKV 只读前 8MB（Tracks 在很前面）
_PS_PREFIX = 8 << 20
_FLV_PREFIX = 4 << 20
_TS_PES_SCAN = 262144        # 每积累 256KB PES 就扫一次 SPS


# ============================================================
# 位读取 / SPS 解析
# ============================================================

class _BitReader:
    __slots__ = ("data", "pos", "nbits")

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.nbits = len(data) * 8

    def u(self, bits):
        v = 0
        for _ in range(bits):
            if self.pos >= self.nbits:
                raise ValueError("bitstream eof")
            v = (v << 1) | ((self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self):
        zeros = 0
        while True:
            if self.pos >= self.nbits:
                raise ValueError("bitstream eof")
            bit = (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
            self.pos += 1
            if bit:
                break
            zeros += 1
            if zeros > 32:
                raise ValueError("bad exp-golomb")
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self):
        v = self.ue()
        return (v + 1) >> 1 if (v & 1) else -(v >> 1)


def _skip_scaling_list(br, size):
    last, nxt = 8, 8
    for _ in range(size):
        if nxt != 0:
            delta = br.se()
            nxt = (last + delta + 256) % 256
        if nxt != 0:
            last = nxt


def _parse_h264_sps(rbsp):
    """H.264 SPS(RBSP) -> (width, height)；失败返回 None"""
    br = _BitReader(rbsp)
    profile_idc = br.u(8)
    br.u(8)   # constraint flags + reserved
    br.u(8)   # level_idc
    br.ue()   # seq_parameter_set_id
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = br.ue()
        if chroma == 3:
            br.u(1)                     # separate_colour_plane_flag
        br.ue()                         # bit_depth_luma_minus8
        br.ue()                         # bit_depth_chroma_minus8
        br.u(1)                         # qpprime_y_zero_transform_bypass_flag
        if br.u(1):                     # seq_scaling_matrix_present_flag
            count = 12 if chroma == 3 else 8
            for i in range(count):
                if br.u(1):
                    _skip_scaling_list(br, 16 if i < 6 else 64)
    br.ue()                             # log2_max_frame_num_minus4
    poc_type = br.ue()
    if poc_type == 0:
        br.ue()
    elif poc_type == 1:
        br.u(1)
        br.se()
        br.se()
        for _ in range(br.ue()):
            br.se()
    br.ue()                             # max_num_ref_frames
    br.u(1)                             # gaps_in_frame_num_value_allowed_flag
    w_mbs = br.ue()
    h_map = br.ue()
    frame_mbs_only = br.u(1)
    w = (w_mbs + 1) * 16
    h = (h_map + 1) * 16 * (1 if frame_mbs_only else 2)
    return (w, h) if w and h else None


def _parse_hevc_sps(rbsp):
    """HEVC SPS(RBSP) -> (width, height)；失败返回 None"""
    br = _BitReader(rbsp)
    br.u(4)                             # sps_video_parameter_set_id
    max_sub = br.u(3)
    br.u(1)                             # sps_temporal_id_nesting_flag
    # profile_tier_level(1, max_sub)
    br.u(2); br.u(1); br.u(5)
    br.u(32)
    br.u(1); br.u(1); br.u(1); br.u(1)
    br.u(43); br.u(1)
    br.u(8)                             # general_level_idc
    if max_sub > 0:
        prof, lvl = [], []
        for _ in range(max_sub):
            prof.append(br.u(1))
            lvl.append(br.u(1))
        for _ in range(8 - max_sub):
            br.u(2)
        for i in range(max_sub):
            if prof[i]:
                br.u(2); br.u(1); br.u(5)
                br.u(32)
                br.u(1); br.u(1); br.u(1); br.u(1)
                br.u(43); br.u(1)
            if lvl[i]:
                br.u(8)
    br.ue()                             # sps_seq_parameter_set_id
    chroma = br.ue()                    # chroma_format_idc
    if chroma == 3:
        br.u(1)
    w = br.ue()                         # pic_width_in_luma_samples
    h = br.ue()                         # pic_height_in_luma_samples
    return (w, h) if w and h else None


def _nal_to_rbsp(data, start):
    """取 start 起的 NAL（到下一个起始码前），去掉仿真预防字节 00 00 03"""
    end = data.find(b"\x00\x00\x01", start)
    seg = data[start:] if end < 0 else data[start:end]
    out = bytearray()
    i, n = 0, len(seg)
    while i < n:
        if i + 2 < n and seg[i] == 0 and seg[i + 1] == 0 and seg[i + 2] == 3:
            out += b"\x00\x00"
            i += 3
        else:
            out.append(seg[i])
            i += 1
    return bytes(out)


def _scan_sps(data):
    """在 Annex-B 数据里找 SPS 解析宽高（H.264 / HEVC / MPEG-2）"""
    idx = 0
    while True:
        i = data.find(b"\x00\x00\x01\x67", idx)
        if i < 0:
            break
        try:
            # 起始码 4 字节，RBSP 从 NAL 头(0x67) 之后开始
            size = _parse_h264_sps(_nal_to_rbsp(data, i + 4))
            if size:
                return size
        except Exception:
            pass
        idx = i + 4
    idx = 0
    while True:
        i = data.find(b"\x00\x00\x01\x42\x01", idx)   # HEVC SPS NAL(33)
        if i < 0:
            break
        try:
            size = _parse_hevc_sps(_nal_to_rbsp(data, i + 5))
            if size:
                return size
        except Exception:
            pass
        idx = i + 5
    idx = 0
    while True:
        i = data.find(b"\x00\x00\x01\xb3", idx)       # MPEG-2 sequence header
        if i < 0 or i + 12 > len(data):
            break
        b = data[i + 4:i + 8]
        w = (b[0] << 4) | (b[1] >> 4)
        h = ((b[1] & 0x0F) << 8) | b[2]
        if 16 <= w <= 8192 and 16 <= h <= 8192:
            return w, h
        idx = i + 4
    return None


# ============================================================
# MP4 / MOV（ISO Base Media File Format）
# ============================================================

def _iter_mp4_boxes(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size = int.from_bytes(head[0:4], "big")
        btype = head[4:8]
        hdr = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = int.from_bytes(ext, "big")
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            return
        yield btype, pos + hdr, pos + size
        pos += size


def _mp4_children(f, start, end, names):
    """按 names 逐层下钻，返回最后一层所有 (data_start, data_end)"""
    level = [(start, end)]
    for name in names:
        nxt = []
        for s, e in level:
            for btype, ds, de in _iter_mp4_boxes(f, s, e):
                if btype == name:
                    nxt.append((ds, de))
        if not nxt:
            return []
        level = nxt
    return level


def _mp4_size(f, filesize):
    for ts, te in _mp4_children(f, 0, filesize, (b"moov", b"trak")):
        is_video = False
        for hs, he in _mp4_children(f, ts, te, (b"mdia", b"hdlr")):
            f.seek(hs)
            d = f.read(min(he - hs, 24))
            if len(d) >= 12 and d[8:12] == b"vide":
                is_video = True
                break
        if not is_video:
            continue
        for ks, ke in _mp4_children(f, ts, te, (b"tkhd",)):
            f.seek(ks)
            d = f.read(ke - ks)
            if len(d) < 8:
                continue
            # version(1)+flags(3) + 时间戳/ID/duration + 保留 + layer/volume + matrix
            off = (4 + 8 + 8 + 4 + 4 + 8 + 8 + 2 + 2 + 2 + 2 + 36) if d[0] == 1 \
                else (4 + 4 + 4 + 4 + 4 + 4 + 8 + 2 + 2 + 2 + 2 + 36)
            if len(d) < off + 8:
                continue
            w = int.from_bytes(d[off:off + 4], "big") >> 16
            h = int.from_bytes(d[off + 4:off + 8], "big") >> 16
            if w and h:
                return w, h
    return None


# ============================================================
# MKV / WebM（EBML）
# ============================================================

def _ebml_head(buf, pos, keep_marker):
    """返回 (value, next_pos, length)；失败 (None, pos, 0)"""
    if pos >= len(buf):
        return None, pos, 0
    first = buf[pos]
    length, mask = 1, 0x80
    while length <= 8 and not (first & mask):
        length += 1
        mask >>= 1
    if length > 8 or pos + length > len(buf):
        return None, pos, 0
    val = first if keep_marker else (first & (mask - 1))
    for i in range(1, length):
        val = (val << 8) | buf[pos + i]
    return val, pos + length, length


def _ebml_children(buf, start, end):
    """遍历 [start,end) 内的 EBML 元素，yield (id_int, data_start, data_end)"""
    p = start
    while p < end - 1:
        eid, np, elen = _ebml_head(buf, p, True)
        if eid is None:
            return
        size, np2, slen = _ebml_head(buf, np, False)
        if size is None:
            return
        unknown = size >= (1 << (7 * slen)) - 1
        yield eid, np2, (len(buf) if unknown else np2 + size)
        if unknown:
            return
        p = np2 + size


def _mkv_size(buf):
    _id, pos, _ = _ebml_head(buf, 0, True)
    if _id is None:
        return None
    size, pos, _ = _ebml_head(buf, pos, False)
    if size is None:
        return None
    pos += size
    seg_id, pos2, _ = _ebml_head(buf, pos, True)
    if seg_id != 0x1A45DFA3:                       # Segment
        return None
    seg_size, body, _ = _ebml_head(buf, pos2, False)
    if seg_size is None:
        return None
    for eid, ds, de in _ebml_children(buf, body, len(buf)):
        if eid == 0x1654AE6B:                      # Tracks
            for _t, ts, te in _ebml_children(buf, ds, de):
                if _t != 0xAE:                     # TrackEntry
                    continue
                track_type = None
                vw = vh = None
                for cid, cds, cde in _ebml_children(buf, ts, te):
                    if cid == 0x83:                # TrackType
                        track_type = int.from_bytes(buf[cds:cde], "big")
                    elif cid == 0xE0:              # Video
                        for vid, vds, vde in _ebml_children(buf, cds, cde):
                            if vid == 0xB0:
                                vw = int.from_bytes(buf[vds:vde], "big")
                            elif vid == 0xBA:
                                vh = int.from_bytes(buf[vds:vde], "big")
                if track_type == 1 and vw and vh:
                    return vw, vh
    return None


# ============================================================
# AVI（RIFF）
# ============================================================

def _avi_size(f, filesize):
    f.seek(0)
    head = f.read(12)
    if len(head) < 12 or head[0:4] != b"RIFF" or head[8:12] != b"AVI ":
        return None
    end = min(filesize, 8 + int.from_bytes(head[4:8], "little"))
    return _avi_walk(f, 12, end, 0)


def _avi_walk(f, pos, end, depth):
    if depth > 4:
        return None
    while pos + 8 <= end:
        f.seek(pos)
        h = f.read(8)
        if len(h) < 8:
            return None
        cid = h[0:4]
        csize = int.from_bytes(h[4:8], "little")
        if cid == b"avih":
            f.seek(pos + 8)
            d = f.read(40)
            if len(d) >= 40:
                w = int.from_bytes(d[32:36], "little")
                hh = int.from_bytes(d[36:40], "little")
                if w and hh:
                    return w, hh
        elif cid == b"LIST" and csize >= 4:
            f.seek(pos + 8)
            if f.read(4) == b"hdrl":
                r = _avi_walk(f, pos + 12, pos + 8 + csize, depth + 1)
                if r:
                    return r
        if csize < 0:
            return None
        pos += 8 + csize + (csize & 1)
    return None


# ============================================================
# ASF / WMV
# ============================================================

_ASF_HEADER = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_ASF_VIDEO = bytes.fromhex("c0ef19bc4d5bcf11a8fd00805f5c442b")


def _asf_size(f, filesize):
    f.seek(0)
    head = f.read(30)
    if len(head) < 30 or head[0:16] != _ASF_HEADER:
        return None
    hdr_size = int.from_bytes(head[16:24], "little")
    num = int.from_bytes(head[24:28], "little")
    end = min(filesize, hdr_size) if hdr_size > 30 else filesize
    pos = 30
    for _ in range(num + 4):
        if pos + 24 > end:
            break
        f.seek(pos)
        oh = f.read(24)
        if len(oh) < 24:
            break
        oid = oh[0:16]
        osize = int.from_bytes(oh[16:24], "little")
        if osize < 24:
            break
        if oid == bytes.fromhex("9107dcb7b7a9cf118ee600c00c205365"):  # Stream Properties
            f.seek(pos + 24)
            d = f.read(min(osize - 24, 4096))
            if len(d) >= 16 and d[0:16] == _ASF_VIDEO:
                # 24(对象头) + 16(StreamType) + 16(ErrorCorrectionType) + 8(TimeOffset)
                # + 4(TypeSpecificLen) + 4(ErrorCorrectionLen) + 2(Flags) + 4(Reserved)
                base = 16 + 16 + 8 + 4 + 4 + 2 + 4
                tsd = d[base:base + 40]
                if len(tsd) >= 12:
                    w = int.from_bytes(tsd[4:8], "little")
                    hh = int.from_bytes(tsd[8:12], "little")
                    if w and hh:
                        return w, hh
        pos += osize
    return None


# ============================================================
# FLV（onMetaData）
# ============================================================

def _flv_size(f, filesize):
    f.seek(0)
    head = f.read(13)
    if len(head) < 13 or head[0:3] != b"FLV":
        return None
    pos = 13
    end = min(filesize, _FLV_PREFIX)
    while pos + 11 <= end:
        f.seek(pos)
        th = f.read(11)
        if len(th) < 11:
            break
        ttype = th[0]
        tsize = int.from_bytes(th[1:4], "big")
        if tsize <= 0:
            break
        if ttype == 18:                              # script data
            f.seek(pos + 11)
            r = _flv_meta_size(f.read(tsize))
            if r:
                return r
        pos += 11 + tsize + 4
    return None


def _flv_meta_size(d):
    pos = 0
    if len(d) < 2:
        return None

    def read_str(p):
        # AMF 字符串：0x02 标记 + 长度(2) + 数据
        if p + 3 > len(d) or d[p] != 0x02:
            return None, p
        n = int.from_bytes(d[p + 1:p + 3], "big")
        p += 3
        if p + n > len(d):
            return None, p
        return d[p:p + n], p + n

    _s, pos = read_str(pos)                          # "onMetaData"
    if _s is None or pos >= len(d) or d[pos] != 0x08:  # ECMA array
        return None
    pos += 1
    if pos + 4 > len(d):
        return None
    count = int.from_bytes(d[pos:pos + 4], "big")
    pos += 4
    w = h = None
    for _ in range(min(count, 256)):
        # ECMA 数组属性名是带 0x02 标记的 AMF 字符串
        if pos + 3 > len(d) or d[pos] != 0x02:
            break
        nlen = int.from_bytes(d[pos + 1:pos + 3], "big")
        pos += 3
        name = d[pos:pos + nlen]
        pos += nlen
        if pos >= len(d):
            break
        vtype = d[pos]
        pos += 1
        if vtype == 0x00:                            # Number(double BE)
            if pos + 8 > len(d):
                break
            val = struct.unpack(">d", d[pos:pos + 8])[0]
            pos += 8
            if name == b"width":
                w = int(val)
            elif name == b"height":
                h = int(val)
        elif vtype == 0x01:                          # Boolean
            pos += 1
        elif vtype == 0x02:                          # String
            _v, pos = read_str(pos)
            if _v is None:
                break
        elif vtype == 0x04:                          # MovieClip
            pos += 8
        elif vtype in (0x03, 0x05, 0x06, 0x08, 0x0B, 0x0C, 0x0D):
            break                                   # 复杂类型：通常紧跟结束标记
        else:
            break
        if w and h:
            return w, h
    return None


# ============================================================
# MPEG-PS（mpg / vob）
# ============================================================

def _ps_size(f, filesize):
    f.seek(0)
    data = f.read(min(filesize, _PS_PREFIX))
    idx = 0
    while True:
        i = data.find(b"\x00\x00\x01\xb3", idx)
        if i < 0 or i + 12 > len(data):
            return None
        b = data[i + 4:i + 8]
        w = (b[0] << 4) | (b[1] >> 4)
        h = ((b[1] & 0x0F) << 8) | b[2]
        if 16 <= w <= 8192 and 16 <= h <= 8192:
            return w, h
        idx = i + 4


# ============================================================
# TS（PAT / PMT / 视频 PES / SPS）
# ============================================================

def _ts_packet_len(buf):
    for plen in (188, 204, 192):
        if len(buf) >= plen * 3 and buf[0] == 0x47 and buf[plen] == 0x47 \
                and buf[plen * 2] == 0x47:
            return plen
    return None


def _ts_section(acc, pid, body, pusi):
    """把一个 TS 包的 PSI 载荷并入累加器；section 完整则返回字节"""
    if pusi:
        if not body:
            return None
        pf = body[0]
        body = body[1:]
        if pf:
            if len(body) < pf:
                return None
            body = body[pf:]
        acc[pid] = bytearray(body)
    else:
        if pid not in acc:
            return None
        acc[pid] += body
    data = acc[pid]
    if len(data) >= 3:
        sec_len = ((data[1] & 0x0F) << 8) | data[2]
        total = sec_len + 3
        if len(data) >= total:
            return bytes(data[:total])
    return None


def _parse_pat(sec):
    if len(sec) < 13 or sec[0] != 0x00:
        return None
    body_len = ((sec[1] & 0x0F) << 8) | sec[2]
    end = min(len(sec), 3 + body_len - 4)
    p = 9   # 段头 3 字节 + tsid(2) + version(2) + section_number(1) + last(1)
    while p + 4 <= end:
        prog = (sec[p] << 8) | sec[p + 1]
        pid = ((sec[p + 2] & 0x1F) << 8) | sec[p + 3]
        if prog != 0:
            return pid
        p += 4
    return None


def _parse_pmt(sec):
    """返回 (elementary_pid, stream_type) 或 None"""
    if len(sec) < 13 or sec[0] != 0x02:
        return None
    body_len = ((sec[1] & 0x0F) << 8) | sec[2]
    end = min(len(sec), 3 + body_len - 4)
    p = 9   # 段头 3 字节 + program_number(2) + version(2) + section_number(1) + last(1)
    if p + 4 > end:
        return None
    p += 2                                            # PCR PID
    prog_info_len = ((sec[p] & 0x0F) << 8) | sec[p + 1]
    p += 2 + prog_info_len
    fallback = None
    while p + 5 <= end:
        stype = sec[p]
        epid = ((sec[p + 1] & 0x1F) << 8) | sec[p + 2]
        es_info_len = ((sec[p + 3] & 0x0F) << 8) | sec[p + 4]
        p += 5 + es_info_len
        if stype in (0x1B, 0x24):                     # H.264 / HEVC 优先
            return epid, stype
        if fallback is None and stype in (0x01, 0x02, 0x10, 0xEA):
            fallback = (epid, stype)
    return fallback


def _ts_size(f, filesize):
    f.seek(0)
    buf = f.read(1 << 19)
    pkt = _ts_packet_len(buf)
    if pkt is None:
        return None
    prefix = 4 if pkt == 192 else 0

    pmt_pid = None
    video_pid = None
    acc = {}
    pes = bytearray()
    started = False
    consumed = 0
    total = len(buf)
    read_more = True

    while True:
        while consumed + pkt <= total:
            pb = buf[consumed:consumed + pkt]
            if pb[prefix] != 0x47:
                consumed += 1
                continue
            b1, b2 = pb[prefix + 1], pb[prefix + 2]
            pid = ((b1 & 0x1F) << 8) | b2
            pusi = (b1 >> 6) & 0x01
            # adaptation_field_control 在包头第 4 字节的 bit5-4
            afc = (pb[prefix + 3] >> 4) & 0x03
            body = pb[prefix + 4:]
            if afc & 0x02:
                aflen = body[0] if body else 0
                body = body[1 + aflen:]
            if not (afc & 0x01):
                consumed += pkt
                continue
            if pid == 0x0000:
                if pmt_pid is None:
                    sec = _ts_section(acc, 0x0000, body, pusi)
                    if sec:
                        pmt_pid = _parse_pat(sec)
            elif pmt_pid is not None and pid == pmt_pid:
                sec = _ts_section(acc, pmt_pid, body, pusi)
                if sec:
                    r = _parse_pmt(sec)
                    if r:
                        video_pid = r[0]
            elif video_pid is not None and pid == video_pid:
                if pusi:
                    if len(body) < 9 or body[0:3] != b"\x00\x00\x01":
                        started = False
                        consumed += pkt
                        continue
                    hdr_len = body[8]
                    body = body[9 + hdr_len:]
                    started = True
                elif not started:
                    consumed += pkt
                    continue
                pes += body
            consumed += pkt
            if video_pid is not None and len(pes) >= _TS_PES_SCAN:
                r = _scan_sps(bytes(pes))
                if r:
                    return r
                pes.clear()
        # 本块处理完：读取更多
        if not read_more or total >= _TS_MAX_READ or total >= filesize:
            break
        buf = buf[consumed:]
        total -= consumed
        consumed = 0
        chunk = f.read(1 << 20)
        if not chunk:
            read_more = False
            continue
        buf += chunk
        total += len(chunk)

    if pes:
        return _scan_sps(bytes(pes))
    return None


# ============================================================
# 分发
# ============================================================

def _probe_by_content(f, filesize):
    f.seek(0)
    head = f.read(64)
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return _avi_size(f, filesize)
    if head[:3] == b"FLV":
        return _flv_size(f, filesize)
    if head[:4] == b"\x1a\x45\xdf\xa3":
        f.seek(0)
        return _mkv_size(f.read(min(filesize, _MKV_PREFIX)))
    if head[:16] == _ASF_HEADER:
        return _asf_size(f, filesize)
    if head[4:8] == b"ftyp" or head[4:8] == b"moov":
        return _mp4_size(f, filesize)
    if len(head) > 188 and head[0] == 0x47 and head[188] == 0x47:
        return _ts_size(f, filesize)
    return _ps_size(f, filesize)


def probe_video_size(path):
    """返回 (width, height)；无法识别返回 None"""
    try:
        filesize = os.path.getsize(path)
    except OSError:
        return None
    if filesize <= 0:
        return None
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            if ext in _MP4_EXTS:
                r = _mp4_size(f, filesize)
            elif ext in _MKV_EXTS:
                f.seek(0)
                r = _mkv_size(f.read(min(filesize, _MKV_PREFIX)))
            elif ext in _AVI_EXTS:
                r = _avi_size(f, filesize)
            elif ext in _ASF_EXTS:
                r = _asf_size(f, filesize)
            elif ext in _FLV_EXTS:
                r = _flv_size(f, filesize)
            elif ext in _TS_EXTS:
                r = _ts_size(f, filesize)
            elif ext in _PS_EXTS:
                r = _ps_size(f, filesize)
            else:
                r = _probe_by_content(f, filesize)
    except Exception:
        return None
    if r and isinstance(r[0], int) and isinstance(r[1], int) \
            and 16 <= r[0] <= 16384 and 16 <= r[1] <= 16384:
        return r
    return None


# ============================================================
# 分类执行
# ============================================================

def _classify(size):
    """(w,h) -> '横屏' / '竖屏' / None(无法判定)"""
    if not size:
        return None
    w, h = size
    if w > h:
        return "横屏"
    if h > w:
        return "竖屏"
    return None


def run_video_sort(directory, exts_text, threads, move_unknown, log):
    """识别 directory 直下（不含子目录）视频的横竖屏并分别移动。
    返回 (横屏数, 竖屏数, 未识别数) 或 None（目录/文件无效）。"""
    d = Path(directory)
    if not d.is_dir():
        log("目录不存在或不是文件夹：" + str(d))
        return None
    exts = set()
    for e in exts_text.replace(",", " ").split():
        exts.add(e if e.startswith(".") else "." + e)
    if not exts:
        log("视频格式为空，请至少填写一种。")
        return None

    files = []
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        try:
            if entry.is_file() and os.path.splitext(entry.name)[1].lower() in exts:
                files.append(entry.path)
        except OSError:
            continue
    if not files:
        log(f"未找到视频文件（格式：{' '.join(sorted(exts))}）")
        return None

    log(f"共找到 {len(files)} 个视频，开始识别（只处理当前文件夹，不进入子目录）...")
    items = []
    workers = max(1, min(int(threads or 8), 32))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(probe_video_size, p): p for p in files}
        for i, fut in enumerate(as_completed(futs), 1):
            p = futs[fut]
            try:
                size = fut.result()
            except Exception:
                size = None
            items.append((p, size))
            if size:
                log(f"  [{i}/{len(files)}] {os.path.basename(p)}  {size[0]}x{size[1]}")
            else:
                log(f"  [{i}/{len(files)}] {os.path.basename(p)}  （无法识别分辨率）")

    land_dir = d / "横屏"
    port_dir = d / "竖屏"
    unknown_dir = d / "未知"
    n_land = n_port = n_unknown = 0
    for p, size in items:
        tag = _classify(size)
        if tag is None:
            if not move_unknown:
                log(f"  保持原位：{os.path.basename(p)}")
                n_unknown += 1
                continue
            tag = "未知"
        target_dir = {"横屏": land_dir, "竖屏": port_dir, "未知": unknown_dir}[tag]
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"  无法创建文件夹 {target_dir.name}/：{e}")
            n_unknown += 1
            continue
        dst = get_unique_path(Path(target_dir) / os.path.basename(p))
        try:
            shutil.move(p, str(dst))
        except OSError as e:
            log(f"  移动失败：{os.path.basename(p)} -> {e}")
            n_unknown += 1
            continue
        if tag == "横屏":
            n_land += 1
        elif tag == "竖屏":
            n_port += 1
        else:
            n_unknown += 1
    log(f"完成：横屏 {n_land} 个，竖屏 {n_port} 个，未识别 {n_unknown} 个。")
    return n_land, n_port, n_unknown


# ============================================================
# UI
# ============================================================

def build_video_tab(parent, app=None):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "识别指定文件夹下的视频是横屏还是竖屏，分别移动到「横屏」「竖屏」子文件夹。"
                        "只处理当前文件夹，不进入子文件夹；只读视频头信息，速度极快、不改画质。",
             colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 2))

    var_src = make_path_row(form, 0, "目标文件夹：", title="选择视频文件夹")
    var_exts = make_entry_row(form, 1, "视频格式：", default=DEFAULT_VIDEO_EXTS,
                              entry_width=52, note="空格分隔，可自行增删")
    var_threads = make_num_row(form, 2, "识别线程数：", 8, 1, 32,
                               note="读文件头几乎不占 CPU，线程多更快")
    var_unknown = make_check(form, "无法识别的视频移到「未知」文件夹（不勾选保持原位）",
                             default=False, row=3, column=1)

    text_log, log = make_log_panel(parent, 2, colspan=1)

    def start():
        src = clean_path(var_src.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的目标文件夹！")
            return
        if not var_exts.get().strip():
            messagebox.showerror("错误", "请填写至少一种视频格式！")
            return
        clear_log(text_log)
        run_task(btn, log,
                 lambda: run_video_sort(src, var_exts.get(), var_threads.get(),
                                        var_unknown.get(), log),
                 on_done=lambda err: None if err else messagebox.showinfo(
                     "完成", "识别与移动结束，详情见日志。"))

    btn = make_start_button(form, 4, "开始识别", start)
