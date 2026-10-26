# -*- coding: utf-8 -*-
"""⑱ 文本翻译：TXT / LRC / SRT / ASS / VTT 等文本文件批量翻译成中文
================================================================
设计要点：
  1. 免费在线接口，无需注册即可用；多引擎自动切换（一个挂了自动换下一个）：
       · DeepL 免费网页版（www2.deepl.com/jsonrpc）—— 质量最好，偶有限流
       · Google 翻译免费接口（translate.googleapis.com）—— 需网络可达
       · MyMemory（api.mymemory.translated.net）—— 免密钥，匿名约 5000 字符/天，
         填写邮箱可提升到约 50000 字符/天（单条请求上限 500 字节）
       · LibreTranslate —— 可填自建/公共端点，可选 API Key
  2. 源语言可手动指定（中日/中英混合文本请在下拉里指定『日本語』『English』等）；
     『自动检测』按字符脚本统计挑源语言，且会避开目标语言（中文为主夹杂日文时
     会自动按日语处理，不会把整段原样返回）。
  3. 片段级翻译（默认开）：一行里只把「非目标语言」的片段送去翻译，中文片段原样保留，
     所以「一大段中文中间夹杂几个日文」的输出是全中文、且中文一字不动。
  4. 保留文件结构：SRT/VTT 的序号行与时间轴、ASS 的 Dialogue 字段前缀、
     LRC 的时间标签（含 [ti:]/[ar:] 等元数据行）一律不翻译。
  5. 输出同名文件：先把译文写入临时文件，再把原文件重命名为「原名.old」（覆盖旧备份），
     最后把临时文件替换回原名 —— 任何一步中断都不会把内容写丢。
"""
import json
import os
import random
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox
from . import ui, ui as ttk

from .common import (clean_path, clear_log, form_label, form_note, make_check,
                     make_form, make_log_panel, make_start_button, make_tip,
                     natural_key, run_task)
from .theme import COLOR


# ============================================================
# 引擎与语言
# ============================================================

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

ENGINE_AUTO = "auto"
ENGINE_DEEPL = "deepl"
ENGINE_GOOGLE = "google"
ENGINE_MYMEMORY = "mymemory"
ENGINE_LIBRE = "libre"

ENGINE_NAMES = {
    ENGINE_AUTO: "自动（DeepL → 谷歌 → MyMemory）",
    ENGINE_DEEPL: "DeepL 免费网页版",
    ENGINE_GOOGLE: "谷歌翻译（免费接口）",
    ENGINE_MYMEMORY: "MyMemory（免费）",
    ENGINE_LIBRE: "LibreTranslate（自定义端点）",
}
# 自动模式下的尝试顺序
AUTO_ORDER = [ENGINE_DEEPL, ENGINE_GOOGLE, ENGINE_MYMEMORY]

LANG_LIST = ["简体中文", "繁体中文", "English", "日本語", "한국어",
             "Русский", "Français", "Deutsch", "Español", "Português",
             "Italiano", "العربية"]
SRC_AUTO = "自动检测"
SRC_LIST = [SRC_AUTO] + LANG_LIST

_CODE_GOOGLE = {
    "简体中文": "zh-CN", "繁体中文": "zh-TW", "English": "en", "日本語": "ja",
    "한국어": "ko", "Русский": "ru", "Français": "fr", "Deutsch": "de",
    "Español": "es", "Português": "pt", "Italiano": "it", "العربية": "ar",
}
_CODE_DEEPL = {
    "简体中文": "ZH", "繁体中文": "ZH-HANT", "English": "EN", "日本語": "JA",
    "한국어": "KO", "Русский": "RU", "Français": "FR", "Deutsch": "DE",
    "Español": "ES", "Português": "PT", "Italiano": "IT", "العربية": "AR",
}
_CODE_LIBRE = {
    "简体中文": "zh", "繁体中文": "zh", "English": "en", "日本語": "ja",
    "한국어": "ko", "Русский": "ru", "Français": "fr", "Deutsch": "de",
    "Español": "es", "Português": "pt", "Italiano": "it", "العربية": "ar",
}

# 各语种对应的字符脚本（用于片段切分与源语言判断）
_TARGET_SCRIPT = {
    "简体中文": "cjk", "繁体中文": "cjk", "English": "latin", "日本語": "kana",
    "한국어": "hangul", "Русский": "cyrillic", "Français": "latin",
    "Deutsch": "latin", "Español": "latin", "Português": "latin",
    "Italiano": "latin", "العربية": "arabic",
}
_SCRIPT_LANG = {
    "kana": "日本語", "hangul": "한국어", "cyrillic": "Русский",
    "arabic": "العربية", "cjk": "简体中文", "latin": "English",
}

# 各引擎单次请求上限（字符数 / 每批条数）
_ENGINE_LIMIT = {
    ENGINE_DEEPL: (3500, 40),
    ENGINE_GOOGLE: (1600, 30),
    ENGINE_MYMEMORY: (450, 1),     # 接口硬上限 500 字节
    ENGINE_LIBRE: (1600, 20),
}

DEFAULT_SUFFIXES = "txt lrc srt ass ssa vtt sub md"


# ============================================================
# HTTP 基础
# ============================================================

def _http(url, data=None, headers=None, timeout=20):
    """统一 HTTP 文本请求；失败抛异常由上层处理（data 为 str 时按 UTF-8 编码）"""
    hdr = {"User-Agent": UA, "Accept-Encoding": "identity"}
    if headers:
        hdr.update(headers)
    body = data.encode("utf-8") if isinstance(data, str) else data
    req = urllib.request.Request(url, data=body, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


# ============================================================
# 字符脚本：片段切分与源语言判断
# ============================================================

def _char_script(ch):
    """返回字符所属脚本：kana / cjk / hangul / cyrillic / arabic / latin / ''（标点数字空白）"""
    o = ord(ch)
    if 0x3040 <= o <= 0x30ff or 0x31f0 <= o <= 0x31ff:
        return "kana"
    if 0x3400 <= o <= 0x4dbf or 0x4e00 <= o <= 0x9fff or 0xf900 <= o <= 0xfaff:
        return "cjk"
    if 0xac00 <= o <= 0xd7af:
        return "hangul"
    if 0x0400 <= o <= 0x04ff:
        return "cyrillic"
    if 0x0600 <= o <= 0x06ff:
        return "arabic"
    if ch.isalpha():
        return "latin"
    return ""


def split_segments(text):
    """按脚本切成片段：[(is_word, 文本, 脚本集合)] —— 标点/数字/空白为 other 片段"""
    segs = []
    buf = ""
    kinds = set()
    prev_word = None
    for ch in text:
        s = _char_script(ch)
        w = bool(s)
        if prev_word is None:
            prev_word = w
        elif w != prev_word:
            segs.append((prev_word, buf, kinds))
            buf, kinds, prev_word = "", set(), w
        buf += ch
        if s:
            kinds.add(s)
    if buf:
        segs.append((bool(prev_word), buf, kinds))
    return segs


def _script_counts(text):
    counts = Counter()
    for ch in text:
        s = _char_script(ch)
        if s:
            counts[s] += 1
    return counts


def _guess_source(text, exclude_script=None):
    """按字符脚本统计推断源语言；exclude_script 用于排除目标语言（混合文本取次优语种）"""
    counts = _script_counts(text)
    if not counts:
        return SRC_AUTO
    # 假名/谚文/西里尔/阿拉伯文是强特征，出现即优先
    for s in ("kana", "hangul", "cyrillic", "arabic"):
        if counts.get(s) and s != exclude_script:
            return _SCRIPT_LANG[s]
    rest = [(cnt, s) for s, cnt in counts.items() if s != exclude_script]
    if not rest:
        return SRC_AUTO
    return _SCRIPT_LANG[max(rest)[1]]


def pick_source(texts, tgt):
    """自动模式：为一批文本挑源语言；全是目标语言时返回 None（表示无需翻译）"""
    counts = Counter()
    for t in texts:
        counts.update(_script_counts(t))
    if not counts:
        return None
    tscript = _TARGET_SCRIPT.get(tgt, "cjk")
    others = [(cnt, s) for s, cnt in counts.items() if s != tscript]
    if not others:
        return None          # 这批已经是目标语言，无需翻译
    if tscript == "cjk":
        # 目标中文：优先按强特征语种（日文假名 / 谚文 / 西里尔…）
        for s in ("kana", "hangul", "cyrillic", "arabic"):
            if counts.get(s):
                return _SCRIPT_LANG[s]
        return _SCRIPT_LANG[max(others)[1]]
    return _SCRIPT_LANG[max(others)[1]]


# ============================================================
# 各引擎实现：入参 texts(list[str])，返回等长 list[str]
# ============================================================

def _deepl_translate(texts, src, tgt, cfg):
    tgt_code = _CODE_DEEPL.get(tgt, "ZH")
    src_code = "AUTO" if src == SRC_AUTO else _CODE_DEEPL.get(src, "AUTO")
    payload = {
        "jsonrpc": "2.0",
        "method": "LMT_handle_texts",
        "params": {
            "splitting": "newlines",
            "lang": {"source_lang_user_selected": src_code, "target_lang": tgt_code},
            "texts": [{"text": t, "requestAlternatives": 0} for t in texts],
            "timestamp": int(time.time() * 1000),
        },
        "id": random.randint(1000000, 99999999),
    }
    raw = _http(
        "https://www2.deepl.com/jsonrpc",
        data=json.dumps(payload, ensure_ascii=False),
        headers={"Content-Type": "application/json; charset=UTF-8",
                 "Referer": "https://www.deepl.com/translator"},
        timeout=25,
    )
    data = json.loads(raw)
    if data.get("error"):
        raise RuntimeError(f"DeepL：{data['error'].get('message', data['error'])}")
    result = (data.get("result") or {}).get("texts") or []
    if len(result) != len(texts):
        raise RuntimeError("DeepL：返回条数不匹配")
    return [str(item.get("text", "")) for item in result]


def _google_translate(texts, src, tgt, cfg):
    tgt_code = _CODE_GOOGLE.get(tgt, "zh-CN")
    src_code = "auto" if src == SRC_AUTO else _CODE_GOOGLE.get(src, "auto")
    out = []
    for t in texts:
        body = urllib.parse.urlencode(
            [("client", "gtx"), ("sl", src_code), ("tl", tgt_code), ("dt", "t"), ("q", t)])
        raw = _http("https://translate.googleapis.com/translate_a/single",
                    data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=20)
        data = json.loads(raw)
        segs = data[0] if data and isinstance(data[0], list) else []
        out.append("".join(seg[0] for seg in segs if seg and isinstance(seg[0], str)))
    return out


def _mymemory_translate(texts, src, tgt, cfg):
    tgt_code = _CODE_GOOGLE.get(tgt, "zh-CN")
    real_src = src if src != SRC_AUTO else _guess_source(" ".join(texts))
    src_code = _CODE_GOOGLE.get(real_src, "en")
    out = []
    for t in texts:
        query = urllib.parse.urlencode({"q": t[:500], "langpair": f"{src_code}|{tgt_code}"})
        if cfg.get("email"):
            query += "&de=" + urllib.parse.quote(cfg["email"])
        raw = _http("https://api.mymemory.translated.net/get?" + query, timeout=20)
        data = json.loads(raw)
        status = str(data.get("responseStatus", ""))
        if status != "200":
            raise RuntimeError(f"MyMemory：{data.get('responseDetails') or status}")
        text = ((data.get("responseData") or {}).get("translatedText") or "").strip()
        if not text:
            raise RuntimeError("MyMemory：返回空译文")
        out.append(re.sub(r"\(Translated by.*?\)$", "", text).strip())
    return out


def _libre_translate(texts, src, tgt, cfg):
    endpoint = (cfg.get("endpoint") or "").strip() or "https://libretranslate.com/translate"
    tgt_code = _CODE_LIBRE.get(tgt, "zh")
    real_src = src if src != SRC_AUTO else _guess_source(" ".join(texts))
    src_code = _CODE_LIBRE.get(real_src, "en")
    out = []
    for t in texts:
        payload = {"q": t, "source": src_code, "target": tgt_code, "format": "text"}
        if cfg.get("apikey"):
            payload["api_key"] = cfg["apikey"]
        raw = _http(endpoint,
                    data=json.dumps(payload, ensure_ascii=False),
                    headers={"Content-Type": "application/json; charset=UTF-8"},
                    timeout=25)
        data = json.loads(raw)
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"LibreTranslate：{data['error']}")
        text = data.get("translatedText", "") if isinstance(data, dict) else ""
        if not text:
            raise RuntimeError("LibreTranslate：返回空译文")
        out.append(text)
    return out


_ENGINE_FN = {
    ENGINE_DEEPL: _deepl_translate,
    ENGINE_GOOGLE: _google_translate,
    ENGINE_MYMEMORY: _mymemory_translate,
    ENGINE_LIBRE: _libre_translate,
}


def _split_long(text, limit):
    """超长单条文本按标点优先切分，避免接口截断"""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for ch in text:
        cur += ch
        if ch in "。！？!?；;…" and len(cur) >= limit * 0.5:
            parts.append(cur)
            cur = ""
        elif len(cur) >= limit:
            parts.append(cur)
            cur = ""
    if cur:
        parts.append(cur)
    return parts or [text]


def _call_engine(engine, texts, src, tgt, cfg):
    """调一次引擎；条数不匹配时自动逐条重试一次，保证返回等长列表"""
    fn = _ENGINE_FN[engine]
    res = fn(texts, src, tgt, cfg)
    if res and len(res) == len(texts):
        return res
    retry = []
    for t in texts:
        r = fn([t], src, tgt, cfg)
        retry.append(r[0] if r else t)
    if len(retry) != len(texts):
        raise RuntimeError("译文条数不匹配")
    return retry


def translate_texts(engine, texts, src, tgt, cfg):
    """按引擎上限分小批请求；超长文本先切分再合并，返回与 texts 等长的译文列表"""
    if not texts:
        return []
    limit, max_lines = _ENGINE_LIMIT.get(engine, (1500, 20))

    pieces = []   # 每条原文在切分后对应的归属索引
    flat = []
    for i, t in enumerate(texts):
        for seg in _split_long(t, limit):
            pieces.append(i)
            flat.append(seg)

    merged = []
    idx = 0
    while idx < len(flat):
        batch, blen = [], 0
        while idx < len(flat) and len(batch) < max_lines and blen + len(flat[idx]) <= limit:
            batch.append(flat[idx])
            blen += len(flat[idx])
            idx += 1
        if not batch:
            batch = [flat[idx]]
            idx += 1
        merged.extend(_call_engine(engine, batch, src, tgt, cfg))

    out = [""] * len(texts)
    for i, owner in enumerate(pieces):
        if i < len(merged):
            out[owner] += merged[i]
    return out


def translate_all(texts, engines, src, tgt, cfg, log, stop_event=None, progress_cb=None):
    """批量翻译（含按批选源语言与引擎切换），返回与 texts 等长的译文列表"""
    out = list(texts)
    dead = set()
    pos = 0
    limit, max_lines = _ENGINE_LIMIT.get(engines[0], (1500, 20))
    while pos < len(texts):
        if stop_event and stop_event.is_set():
            raise RuntimeError("已取消")
        idx_list, batch, blen = [], [], 0
        while pos < len(texts) and len(batch) < max_lines and blen < limit:
            idx_list.append(pos)
            batch.append(texts[pos])
            blen += len(texts[pos])
            pos += 1

        # 自动检测：按这批的字符脚本挑源语言（会避开目标语言，混合文本才不会被原样返回）
        eff_src = src
        if src == SRC_AUTO:
            picked = pick_source(batch, tgt)
            if picked is None:          # 整批已是目标语言，直接保留原文
                if progress_cb:
                    progress_cb(pos, len(texts))
                continue
            eff_src = picked

        ok, last_err = False, ""
        for eng in engines:
            if eng in dead:
                continue
            # 每个引擎按自己的上限再切小批
            elimit, emax = _ENGINE_LIMIT.get(eng, (1500, 20))
            res_all, i2, ok_all = [], 0, True
            while i2 < len(batch):
                sub, slen = [], 0
                while i2 < len(batch) and len(sub) < emax \
                        and slen + len(batch[i2]) <= elimit:
                    sub.append(batch[i2])
                    slen += len(batch[i2])
                    i2 += 1
                if not sub:
                    sub = [batch[i2]]
                    i2 += 1
                try:
                    res_all.extend(translate_texts(eng, sub, eff_src, tgt, cfg))
                except Exception as e:
                    last_err = str(e)
                    ok_all = False
                    break
            if ok_all and len(res_all) == len(batch):
                for k, val in enumerate(res_all):
                    out[idx_list[k]] = val
                ok = True
                break
            dead.add(eng)
            log(f"    引擎 {ENGINE_NAMES.get(eng, eng)} 不可用（{last_err}），切换下一个")
        if not ok:
            raise RuntimeError(f"翻译失败：{last_err}")
        if progress_cb:
            progress_cb(pos, len(texts))
    return out


# ============================================================
# 文件格式解析：只挑出需要翻译的行
# ============================================================

_META_LRC_RE = re.compile(r"^\[(ti|ar|al|by|offset|length|re|ve|kana|au)\s*:", re.I)
_TAG_RE = re.compile(r"\{[^}]*\}")
_HTML_RE = re.compile(r"<[^>]+>")


def detect_format(suffix):
    s = suffix.lower().lstrip(".")
    if s in ("srt", "vtt", "sub"):
        return "srt"
    if s in ("ass", "ssa"):
        return "ass"
    if s == "lrc":
        return "lrc"
    return "txt"


def needs_translation(text, target):
    """整行模式：判断一行是否需要翻译（纯数字/符号/已是目标语言的行跳过）"""
    body = _HTML_RE.sub("", _TAG_RE.sub("", text)).strip()
    if not body or not any(c.isalpha() for c in body):
        return False
    tscript = _TARGET_SCRIPT.get(target, "cjk")
    counts = _script_counts(body)
    if not counts:
        return False
    return any(s != tscript for s in counts)


def collect_translatable(lines, fmt):
    """返回 [(行索引, 待翻译文本)]，其余行（序号/时间轴/标签/空行）原样保留"""
    items = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if fmt == "srt":
            if "-->" in s:
                continue
            # 序号行（SRT 为纯数字，VTT 可能是文本型编号）：下一行是时间轴则跳过
            if i + 1 < len(lines) and "-->" in lines[i + 1]:
                continue
            if s.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
                continue
            items.append((i, line))
        elif fmt == "ass":
            if s.startswith("[") or s.startswith(";") or s.startswith("!"):
                continue
            if not s.lower().startswith("dialogue:"):
                continue
            parts = s.split(",", 9)
            if len(parts) < 10 or not parts[9].strip():
                continue
            items.append((i, parts[9]))
        elif fmt == "lrc":
            if _META_LRC_RE.match(s):
                continue
            pos = line.rfind("]")
            if pos < 0:
                items.append((i, line))
            else:
                text = line[pos + 1:].strip()
                if text:
                    items.append((i, text))
        else:
            items.append((i, line))
    return items


def build_segment_plan(items, tgt, translate_cjk):
    """片段模式：把每行切成片段，只挑出「非目标语言」的片段。
    返回 (plans, todo)：
      plans = [(行索引, [[原文, 需翻译, 译文]])]
      todo  = [(plan序号, 片段序号, 原文)] —— 顺序与待翻译片段一致"""
    tscript = _TARGET_SCRIPT.get(tgt, "cjk")
    plans, todo = [], []
    for idx, raw in items:
        segs = []
        for is_word, s, kinds in split_segments(raw):
            if not is_word or not kinds:
                segs.append([s, False, ""])     # 标点/数字/空白：原样保留
                continue
            if kinds == {tscript}:
                # 已是目标语言的片段（如中文）：默认保留；勾选后才送去翻译
                segs.append([s, bool(translate_cjk), ""])
            else:
                segs.append([s, True, ""])
        if not any(s[1] for s in segs):
            continue        # 整行都无需翻译
        pi = len(plans)
        plans.append((idx, segs))
        for sj, s in enumerate(segs):
            if s[1]:
                todo.append((pi, sj, s[0]))
    return plans, todo


def _write_back(lines, fmt, results):
    """把译文写回行列表（ASS/LRC 只替换文本部分，前缀保留）。
    results = [(行索引, 新文本)]"""
    for idx, new in results:
        if not new:
            continue
        if fmt == "ass":
            parts = lines[idx].split(",", 9)
            parts[9] = new
            lines[idx] = ",".join(parts)
        elif fmt == "lrc":
            pos = lines[idx].rfind("]")
            lines[idx] = new if pos < 0 else lines[idx][:pos + 1] + new
        else:
            lines[idx] = new


def read_text_auto(file_path):
    """自动猜测编码读取文本；有 chardet 时优先使用其检测结果"""
    with open(file_path, "rb") as f:
        raw = f.read()
    if not raw:
        return "", "utf-8"
    candidates = []
    try:
        import chardet
        det = chardet.detect(raw)
        if det and det.get("encoding"):
            candidates.append(det["encoding"])
    except Exception:
        pass
    candidates += ["utf-8-sig", "utf-8", "gbk", "gb2312", "big5", "latin-1"]
    for enc in candidates:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore"), "utf-8"


# ============================================================
# 单文件处理
# ============================================================

def translate_file(path, engines, src, tgt, cfg, log, stop_event=None, chunk_cb=None):
    """翻译单个文件。返回 (状态, 说明)：状态 in {ok, skip, fail}"""
    fmt = detect_format(Path(path).suffix)
    text, src_enc = read_text_auto(path)
    if not text.strip():
        return "skip", "空文件"
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    items = collect_translatable(lines, fmt)
    if not items:
        return "skip", "没有可翻译内容"

    if cfg.get("segment", True):
        plans, todo = build_segment_plan(items, tgt, cfg.get("cjk_seg", False))
    else:
        if cfg.get("skip_plain", True):
            items = [(i, t) for i, t in items if needs_translation(t, tgt)]
        plans = [(i, [[t, True, ""]]) for i, t in items]
        todo = [(pi, 0, t) for pi, (_i, t) in enumerate(items)]
    if not todo:
        return "skip", "无需翻译的内容"

    try:
        results = translate_all([t for _p, _s, t in todo], engines, src, tgt, cfg,
                                log, stop_event, chunk_cb)
    except RuntimeError as e:
        if "已取消" in str(e):
            return "skip", "已取消"
        return "fail", str(e)

    for (pi, sj, _src), val in zip(todo, results):
        plans[pi][1][sj][2] = val

    out_lines = [(idx, "".join(s[2] if s[1] and s[2] else s[0] for s in segs))
                 for idx, segs in plans]
    _write_back(lines, fmt, out_lines)

    out_enc = cfg.get("encoding", "utf-8")
    if out_enc == "keep":
        out_enc = src_enc or "utf-8"
    try:
        data = newline.join(lines).encode(out_enc, errors="replace")
    except LookupError:
        data = newline.join(lines).encode("utf-8", errors="replace")

    tmp_path = Path(str(path) + ".trtmp")
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        if cfg.get("backup", True):
            old_path = Path(str(path) + ".old")
            try:
                os.replace(path, old_path)   # 覆盖已有 .old（那只是上一次的备份）
            except Exception:
                shutil.copy2(path, old_path)
                os.remove(path)
        os.replace(tmp_path, path)
    except Exception as e:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return "fail", f"写入失败：{e}"
    return "ok", f"{len(todo)} 段"


# ============================================================
# 界面
# ============================================================

def make_combo_row(parent, row, label, values, default, width=18, note=None):
    form_label(parent, row, label)
    var = tk.StringVar(value=default)
    cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=width)
    cb.grid(row=row, column=1, sticky="w", padx=(0, 6), pady=5)
    if note:
        form_note(parent, row, note)
    return var


def build_translate_tab(parent, app=None):
    """⑱ 文本翻译：拖入 txt/lrc/srt 等文件，调用免费接口翻译成中文，
    译文写回同名文件，原文件重命名为「原名.old」"""
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    parent.grid_rowconfigure(7, weight=1)
    make_tip(parent, 0,
             "把 .txt / .lrc / .srt / .ass / .vtt 等文本文件拖到下方列表（也可点按钮添加），"
             "调用免费在线接口翻译成中文，译文写回同名文件，原文件重命名为「原名.old」。\n"
             "混合文本（一大段中文夹几个日文/英文）：【片段模式】只把非中文片段送去翻译，中文一字不动；"
             "源语言可手动指定为『日本語』『English』等，混合文件建议手动指定，比自动检测更稳。",
             colspan=1, autowrap=True)

    file_list = []
    stop_event = threading.Event()

    # ---- 添加按钮 ----
    frame_add = ui.Frame(parent)
    frame_add.grid(row=1, column=0, sticky="w", padx=6, pady=4)

    # ---- 文件列表 ----
    list_frame = ui.Frame(parent)
    list_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=4)
    list_frame.grid_rowconfigure(0, weight=1)
    list_frame.grid_columnconfigure(0, weight=1)
    listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED)
    lb_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=listbox.yview)
    listbox.config(yscrollcommand=lb_scroll.set)
    listbox.grid(row=0, column=0, sticky="nsew")
    lb_scroll.grid(row=0, column=1, sticky="ns")

    label_count = ui.Label(parent, text="共 0 个文件")
    label_count.grid(row=3, column=0, sticky="w", padx=8)

    # ---- 参数区 ----
    form = make_form(parent)
    form.grid(row=4, column=0, sticky="ew", padx=6, pady=(0, 2))

    engine_values = [ENGINE_NAMES[k] for k in
                     (ENGINE_AUTO, ENGINE_DEEPL, ENGINE_GOOGLE,
                      ENGINE_MYMEMORY, ENGINE_LIBRE)]
    var_engine = make_combo_row(form, 0, "翻译引擎：", engine_values,
                                ENGINE_NAMES[ENGINE_AUTO], width=30,
                                note="自动：一个失败自动切换下一个（均免费、无需注册）")
    var_src = make_combo_row(form, 1, "源语言：", SRC_LIST, SRC_AUTO, width=18,
                             note="混合文本建议手动指定，如『日本語』『English』")
    var_tgt = make_combo_row(form, 2, "目标语言：", LANG_LIST, "简体中文", width=18,
                             note="混合文本的目标是「全中文」")
    var_enc = make_combo_row(form, 3, "输出编码：",
                             ["UTF-8", "UTF-8 BOM", "GBK", "跟随原文件"],
                             "UTF-8", width=18,
                             note="播放器不认中文时可改 UTF-8 BOM")
    form_label(form, 4, "文件后缀：")
    var_suffix = tk.StringVar(value=DEFAULT_SUFFIXES)
    ttk.Entry(form, textvariable=var_suffix, width=30).grid(
        row=4, column=1, sticky="w", padx=(0, 6), pady=5)
    form_note(form, 4, "空格分隔，拖入文件夹时只收这些后缀")

    form_label(form, 5, "选项：")
    frame_opt = ui.Frame(form)
    frame_opt.grid(row=5, column=1, sticky="w", pady=3)
    var_backup = make_check(frame_opt, "原文件重命名为「原名.old」（覆盖旧备份）", default=True)
    var_skip = make_check(frame_opt, "跳过无需翻译的行（仅整行模式）", default=True)
    var_rec = make_check(frame_opt, "添加文件夹时遍历子目录", default=True)
    for w in frame_opt.winfo_children():
        w.pack_configure(side=tk.LEFT)

    form_label(form, 6, "混合文本：")
    frame_mix = ui.Frame(form)
    frame_mix.grid(row=6, column=1, sticky="w", pady=3)
    var_seg = make_check(frame_mix, "片段模式：只翻译非目标语言片段（中文原样保留）",
                         default=True)
    var_cjk = make_check(frame_mix, "连纯汉字片段也翻译（处理无假名的日文词）", default=False)
    for w in frame_mix.winfo_children():
        w.pack_configure(side=tk.LEFT)
    ui.Label(frame_mix, text="关闭片段模式 = 整行交给引擎（不再保留中文原句）",
              foreground=COLOR["text_secondary"]).pack(side=tk.LEFT, padx=(10, 0))

    form_label(form, 7, "可选端点：")
    var_endpoint = tk.StringVar(value="https://libretranslate.com/translate")
    ttk.Entry(form, textvariable=var_endpoint, width=34).grid(
        row=7, column=1, sticky="w", padx=(0, 6), pady=5)
    form_note(form, 7, "仅 LibreTranslate 引擎需要")

    form_label(form, 8, "邮箱 / Key：")
    var_email = tk.StringVar(value="")
    ttk.Entry(form, textvariable=var_email, width=34).grid(
        row=8, column=1, sticky="w", padx=(0, 6), pady=5)
    form_note(form, 8, "可选：MyMemory 填邮箱可提额度；LibreTranslate 填 API Key")

    # ---- 执行 / 停止 ----
    frame_run = ui.Frame(parent)
    frame_run.grid(row=5, column=0, sticky="w", padx=6, pady=4)

    # ---- 进度 ----
    frame_prog = ui.Frame(parent)
    frame_prog.grid(row=6, column=0, sticky="ew", padx=8, pady=2)
    frame_prog.grid_columnconfigure(0, weight=1)
    pbar = ttk.Progressbar(frame_prog, mode="determinate", maximum=100)
    pbar.grid(row=0, column=0, sticky="ew")
    label_prog = ui.Label(frame_prog, text="")
    label_prog.grid(row=1, column=0, sticky="w")

    text_log, log = make_log_panel(parent, 7, colspan=1, title="运行日志", height=9)

    # ---- 功能函数 ----
    def refresh_list():
        listbox.delete(0, tk.END)
        for f in file_list:
            listbox.insert(tk.END, f)
        label_count.config(text=f"共 {len(file_list)} 个文件")

    def _suffix_set():
        raw = var_suffix.get().strip() or DEFAULT_SUFFIXES
        return {"." + s.strip().lstrip(".").lower() for s in raw.split() if s.strip()}

    def add_paths(paths):
        exts = _suffix_set()
        found = []
        for p in paths:
            p = clean_path(p)
            if os.path.isfile(p):
                if Path(p).suffix.lower() in exts and not p.lower().endswith(".old"):
                    found.append(os.path.abspath(p))
            elif os.path.isdir(p):
                if var_rec.get():
                    for dirpath, _dirs, names in os.walk(p):
                        for n in names:
                            fp = os.path.join(dirpath, n)
                            if Path(fp).suffix.lower() in exts and not fp.lower().endswith(".old"):
                                found.append(os.path.abspath(fp))
                else:
                    for n in os.listdir(p):
                        fp = os.path.join(p, n)
                        if os.path.isfile(fp) and Path(fp).suffix.lower() in exts \
                                and not fp.lower().endswith(".old"):
                            found.append(os.path.abspath(fp))
        existing = set(file_list)
        added = 0
        for f in found:
            if f not in existing:
                existing.add(f)
                file_list.append(f)
                added += 1
        file_list.sort(key=natural_key)
        refresh_list()
        if not added and paths:
            messagebox.showinfo("提示",
                                "未找到符合后缀的文本文件。\n"
                                "可在上方修改后缀，或勾选『添加文件夹时遍历子目录』。")

    def on_add_files():
        exts = _suffix_set()
        types = [("文本文件", " ".join("*" + e for e in sorted(exts))), ("所有文件", "*.*")]
        paths = filedialog.askopenfilenames(title="选择要翻译的文本文件（可多选）",
                                            filetypes=types)
        if paths:
            add_paths(list(paths))

    def on_add_folder():
        p = filedialog.askdirectory(title="选择文件夹（会读取其中的文本文件）")
        if p:
            add_paths([p])

    def on_remove_selected():
        for i in reversed(list(listbox.curselection())):
            if 0 <= i < len(file_list):
                file_list.pop(i)
        refresh_list()

    ui.Button(frame_add, text="添加文件", command=on_add_files).pack(side=tk.LEFT, padx=4)
    ui.Button(frame_add, text="添加文件夹", command=on_add_folder).pack(side=tk.LEFT, padx=4)
    ui.Button(frame_add, text="移除选中", command=on_remove_selected).pack(side=tk.LEFT, padx=4)
    ui.Button(frame_add, text="清空列表",
               command=lambda: (file_list.clear(), refresh_list())).pack(side=tk.LEFT, padx=4)

    def selected_engine_key():
        name = var_engine.get()
        for k, v in ENGINE_NAMES.items():
            if v == name:
                return k
        return ENGINE_AUTO

    def start():
        if not file_list:
            messagebox.showwarning("提示", "请先添加或拖入要翻译的文件！")
            return
        key = selected_engine_key()
        engines = AUTO_ORDER if key == ENGINE_AUTO else [key]
        cfg = {
            "email": var_email.get().strip(),
            "apikey": var_email.get().strip(),
            "endpoint": var_endpoint.get().strip(),
            "backup": var_backup.get(),
            "skip_plain": var_skip.get(),
            "segment": var_seg.get(),
            "cjk_seg": var_cjk.get(),
        }
        enc_map = {"UTF-8": "utf-8", "UTF-8 BOM": "utf-8-sig",
                   "GBK": "gbk", "跟随原文件": "keep"}
        cfg["encoding"] = enc_map.get(var_enc.get(), "utf-8")
        src, tgt = var_src.get(), var_tgt.get()
        files = list(file_list)
        clear_log(text_log)
        stop_event.clear()
        pbar["value"] = 0
        log(f"开始翻译：{len(files)} 个文件，引擎 {var_engine.get()}，"
            f"{src} → {tgt}，输出编码 {var_enc.get()}，"
            f"{'片段模式' if cfg['segment'] else '整行模式'}"
            f"{'（含纯汉字片段）' if cfg['segment'] and cfg['cjk_seg'] else ''}")

        state = {"i": 0, "name": "", "infile": 0, "intotal": 0}
        holder = {}

        def refresh():
            i, n = state["i"], len(files) or 1
            base = (i - 1) / n * 100 if i else 0
            if state["intotal"]:
                pbar["value"] = min(100, base + state["infile"] / state["intotal"] * (100 / n))
            else:
                pbar["value"] = base
            extra = f"（{state['infile']}/{state['intotal']} 段）" if state["intotal"] else ""
            label_prog.config(text=f"({i}/{len(files)}) {state['name']} {extra}")

        def work():
            ok = skip = fail = 0

            def tick(done, all_):
                state["infile"], state["intotal"] = done, all_

            for idx, path in enumerate(files, 1):
                if stop_event.is_set():
                    log("已手动停止。")
                    break
                state.update({"i": idx, "name": os.path.basename(path),
                              "infile": 0, "intotal": 0})
                parent.after(0, refresh)
                try:
                    status, detail = translate_file(path, engines, src, tgt, cfg,
                                                    log, stop_event, tick)
                except Exception as e:
                    status, detail = "fail", str(e)
                if status == "ok":
                    ok += 1
                    log(f"[{idx}/{len(files)}] 完成 {os.path.basename(path)}（{detail}）")
                elif status == "skip":
                    skip += 1
                    log(f"[{idx}/{len(files)}] 跳过 {os.path.basename(path)}（{detail}）")
                else:
                    fail += 1
                    log(f"[{idx}/{len(files)}] 失败 {os.path.basename(path)}（{detail}）")
                parent.after(0, refresh)
            state.update({"i": len(files), "intotal": 0})
            parent.after(0, refresh)
            return ok, skip, fail

        def done(err):
            btn_stop.config(state="disabled")
            pbar["value"] = 100
            ok, skip, fail = holder.get("res", (0, 0, 0))
            label_prog.config(text=f"完成：成功 {ok}，跳过 {skip}，失败 {fail}")
            log(f"全部结束 —— 成功 {ok}，跳过 {skip}，失败 {fail}")
            if not err:
                messagebox.showinfo("完成",
                                    f"翻译结束：成功 {ok}，跳过 {skip}，失败 {fail}\n"
                                    f"原文件已重命名为「原名.old」，详情见日志。")

        def wrapper():
            holder["res"] = work()

        run_task(btn, log, wrapper, on_done=done)

    def on_stop():
        stop_event.set()
        log("正在停止…（当前文件处理完即退出）")

    btn_stop = ui.Button(frame_run, text="停止", width=8, state="disabled", command=on_stop)
    btn_stop.grid(row=0, column=2, sticky="w", padx=(12, 0))

    def on_start():
        btn_stop.config(state="normal")
        start()

    btn = make_start_button(frame_run, 0, "开始翻译", on_start)

    if app is not None:
        app.register_drop_handler(parent, add_paths)
