# -*- coding: utf-8 -*-
"""⑩ 音轨提取

从视频里把音轨「原样搬出来」——`-c:a copy` 只拆封装、不重编码，
所以几个 GB 的视频取音频通常只要一两秒，音质与源文件完全一致。

每个文件的处理链路：
  1. 输出文件夹里已有同名音频 → 直接跳过（连 ffprobe 都不跑）；
  2. ffprobe 读音轨的编码 / 声道（顺带知道有没有音轨、一共几条）；
  3. 按编码挑一个「装得下」的容器：AAC/ALAC → .m4a、MP3 → .mp3、
     FLAC → .flac、Opus → .opus、Vorbis → .ogg；其余（AC-3 / E-AC-3 /
     DTS / TrueHD / WMA / PCM…）统一装进 .mka —— Matroska 音频什么都能装；
  4. ffmpeg 先写到同目录的 `~ae_xxxx.part`，成功后再改名成正式文件名。

为什么要临时文件 + 改名（而不是直接写正式文件名）：
  · 中途被停止 / 断电时留下的是 .part，不会被下一次运行误判成「已存在」
    而跳过（直接写正式名会留下半截音频，看着像提取成功，其实听不了）；
  · 两个同名视频（如 a.ts 与 a.mp4）同时落到同一个目标时，改名的原子性
    保证只有一个成功，另一个按「已存在」跳过，绝不互相覆盖。

页面只做三件事：选源文件夹、选输出文件夹、点开始。默认递归子文件夹并
保留目录结构；多音轨文件只取第一条，其余条数写进日志。
"""
import os
import re
import subprocess
import threading
import time
import tkinter as tk
import traceback
import uuid
from tkinter import filedialog, messagebox

from . import config
from . import ui
from .common import (ScrollPage, clean_path, clear_log, form_label, form_note,
                     make_check, make_entry_row, make_form, make_logger,
                     make_num_row, make_path_row, make_tip, natural_key)
from .tab_encode import locate_ffmpeg, save_ffmpeg_hint
from .theme import COLOR, FONTS

CONFIG_SECTION = "audioextract"

# Windows 进程创建标志：不弹出黑色控制台窗口
_CREATE_NO_WINDOW = 0x08000000

# 默认扫描的「视频」后缀（用户可改）。ts / flv / mp4 是主力，
# 其余常见容器一并列上，免得为了一个 mkv 还要回来改设置。
DEFAULT_EXTS = ("ts flv mp4 mkv mov m4v avi wmv webm mpg mpeg "
                "m2ts mts f4v 3gp rmvb rm asf vob")

# 音轨编码 → 容器扩展名。只放「能无转码装下该编码」的组合，
# 装不下的（AC-3 / DTS / TrueHD / WMA / PCM…）一律走 mka。
CODEC_EXT = {
    "aac": ".m4a",
    "alac": ".m4a",
    "mp3": ".mp3",
    "flac": ".flac",
    "opus": ".opus",
    "vorbis": ".ogg",
}
FALLBACK_EXT = ".mka"

# 扩展名 → ffmpeg 封装器名。写临时文件时扩展名是 .part，
# 无法靠扩展名猜格式，必须显式给 -f。
MUXER_BY_EXT = {
    ".m4a": "ipod",       # m4a 用 ipod（mp4 也能装，但 ipod 对 .m4a 更规矩）
    ".mp3": "mp3",
    ".flac": "flac",
    ".opus": "opus",
    ".ogg": "ogg",
    ".mka": "matroska",
}

# 「已存在」判定用的音频扩展名（含以前用别的模式提取出来的）
AUDIO_EXTS = (".m4a", ".mp3", ".flac", ".opus", ".ogg", ".mka", ".aac",
              ".wav", ".wma", ".ac3", ".eac3", ".dts", ".mp2", ".ape")

CONTAINER_CHOICES = (
    ("auto", "自动（按音轨编码选扩展名）"),
    ("mka", "统一 .mka（任何编码都能装）"),
)
_LABEL_TO_KEY = {label: key for key, label in CONTAINER_CHOICES}

_SPLIT_RE = re.compile(r"[\s,;，；、]+")


# ============================================================
# 配置
# ============================================================

def _as_int(value, default, low, high):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _load_prefs():
    sec = config.get_section(CONFIG_SECTION)
    container = str(sec.get("container") or "auto")
    if container not in _LABEL_TO_KEY.values():
        container = "auto"
    return {
        "source": str(sec.get("source") or ""),
        "dest": str(sec.get("dest") or ""),
        "exts": str(sec.get("exts") or DEFAULT_EXTS),
        "container": container,
        "parallel": _as_int(sec.get("parallel", 2), 2, 1, 8),
        "recursive": _as_bool(sec.get("recursive", True), True),
        "keep_structure": _as_bool(sec.get("keep_structure", True), True),
    }


def _save_prefs(**values):
    try:
        config.update_section(CONFIG_SECTION, **values)
    except Exception:
        pass


# ============================================================
# 纯逻辑：后缀 / 探测 / 扫描
# ============================================================

def parse_exts(text):
    """「ts flv .mp4」→ {'.ts', '.flv', '.mp4'}"""
    out = set()
    for raw in _SPLIT_RE.split(text or ""):
        raw = raw.strip().lstrip("*").lstrip(".").lower()
        if raw:
            out.add("." + raw)
    return out


def probe_audio(ffprobe, path):
    """读所有音轨的 (编码, 声道数)。

    返回 [] = 文件里确实没有音轨（该跳过）；
    返回 None = 读不出来（损坏 / 权限 / 不是媒体文件）——仍然值得用 mka 试一把，
    所以两种情况必须分开，不能都当「无音轨」。
    """
    if not ffprobe:
        return None
    cmd = [ffprobe, "-v", "error", "-select_streams", "a",
           "-show_entries", "stream=codec_name,channels",
           "-of", "csv=p=0", path]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           creationflags=_CREATE_NO_WINDOW, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    text = (r.stdout or b"").decode("utf-8", "replace")
    tracks = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        codec = parts[0].strip().lower()
        ch = parts[1].strip() if len(parts) > 1 else ""
        tracks.append((codec, ch))
    return tracks


def choose_ext(codec, container="auto"):
    """按音轨编码挑容器扩展名。"""
    if container == "mka":
        return FALLBACK_EXT
    return CODEC_EXT.get((codec or "").strip().lower(), FALLBACK_EXT)


def scan_videos(root, exts, recursive=True):
    """遍历源文件夹，返回 [(绝对路径, 相对子目录)]，按自然顺序排序。

    只读目录项、不 stat 文件内容，十万个文件也是秒级。
    """
    found = []

    def walk(folder, rel):
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive:
                                walk(entry.path,
                                     os.path.join(rel, entry.name) if rel else entry.name)
                        elif entry.is_file(follow_symlinks=True):
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext in exts:
                                found.append((entry.path, rel))
                    except OSError:
                        continue
        except OSError:
            pass

    walk(root, "")
    found.sort(key=lambda item: natural_key(
        os.path.join(item[1], os.path.basename(item[0]))))
    return found


def _rel_label(rel, name):
    return os.path.join(rel, name) if rel else name


def _quiet_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ============================================================
# 已存在判定（目录内容缓存）
# ============================================================

class _NameCache:
    """同一目录只 listdir 一次：上千个文件在同一个文件夹时，
    免得每个文件都把目录读一遍（机械硬盘上差别很明显）。"""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def names(self, folder):
        with self._lock:
            known = self._data.get(folder)
        if known is not None:
            return known
        try:
            known = {n.lower() for n in os.listdir(folder)}
        except OSError:
            known = set()                     # 目录还不存在 → 什么都算没有
        with self._lock:
            self._data[folder] = known
        return known

    def has_any(self, folder, base):
        """同名 + 任意音频扩展名 → 认为已经提取过。"""
        names = self.names(folder)
        low = base.lower()
        for ext in AUDIO_EXTS:
            if low + ext in names:
                return True
        return False

    def add(self, folder, name):
        with self._lock:
            self._data.setdefault(folder, set()).add(name.lower())


# ============================================================
# ffmpeg 执行
# ============================================================

def _kill(proc):
    """结束 ffmpeg：先 terminate，超时再 kill。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def run_ffmpeg(rt, ffmpeg, src, temp_out, ext):
    """把一个文件的音轨复制到 temp_out，返回 (返回码, 输出文本)。"""
    muxer = MUXER_BY_EXT.get(ext, "matroska")
    args = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", src,
        "-map", "0:a:0?",          # 只取第一条音轨；末尾 ? 让无音轨时报错而不是崩
        "-c:a", "copy",            # 关键：不转码，只搬字节
        "-map_metadata", "0",      # 标题 / 艺术家等标签一起带过来
        "-f", muxer,
        temp_out,
    ]
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW)
    except OSError as e:
        return -1, str(e)
    with rt["plock"]:
        rt["procs"].add(proc)
    try:
        out, _ = proc.communicate()
    finally:
        with rt["plock"]:
            rt["procs"].discard(proc)
    return proc.returncode, (out or b"").decode("utf-8", "replace").strip()


def _sweep_parts(folder, seen, lock):
    """清掉上次被强行结束时留下的半成品。

    只认自己的 `~ae_` 前缀，并且只删「一小时以前」的：正在跑的提取
    文件不过几分钟，一小时还没收走的必然是弃儿。用户自己的文件不会
    以这个前缀命名，所以碰不到别人的东西。
    """
    with lock:
        if folder in seen:
            return
        seen.add(folder)
    try:
        now = time.time()
        for name in os.listdir(folder):
            if name.startswith("~ae_") and name.endswith(".part"):
                p = os.path.join(folder, name)
                try:
                    if now - os.path.getmtime(p) > 3600:
                        os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


def extract_job(files, opts, rt, log):
    """并行提取。进度写在 rt["stats"] 里，由主线程轮询显示。"""
    stop = rt["stop"]
    ffmpeg = opts["ffmpeg"]
    ffprobe = opts.get("ffprobe") or ""
    container = opts.get("container") or "auto"
    out_root = opts["dest"]
    keep = bool(opts.get("keep_structure"))
    cache = _NameCache()
    swept = set()
    sweep_lock = threading.Lock()

    def bump(key, n=1):
        with rt["lock"]:
            rt["stats"][key] = rt["stats"].get(key, 0) + n

    def handle(src, rel):
        name = os.path.basename(src)
        base = os.path.splitext(name)[0] or "audio"
        folder = os.path.join(out_root, rel) if (keep and rel) else out_root

        # ① 已有同名音频 → 跳过。放在 ffprobe 之前，已提取过的文件几乎零成本。
        if cache.has_any(folder, base):
            bump("skip")
            log("跳过·已存在　%s" % _rel_label(rel, name))
            return

        # ② 读音轨信息
        tracks = probe_audio(ffprobe, src) if ffprobe else None
        if tracks is not None and not tracks:
            bump("noaudio")
            log("跳过·无音轨　%s" % _rel_label(rel, name))
            return
        codec = tracks[0][0] if tracks else ""

        ext = choose_ext(codec, container)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            bump("fail")
            log("✘ 建不了输出目录 %s：%s" % (folder, e))
            return
        _sweep_parts(folder, swept, sweep_lock)

        dst = os.path.join(folder, base + ext)
        temp = os.path.join(folder, "~ae_%s.part" % uuid.uuid4().hex[:10])
        rc, out = run_ffmpeg(rt, ffmpeg, src, temp, ext)

        if stop.is_set():
            _quiet_remove(temp)
            return
        if rc == 0 and not (os.path.isfile(temp) and os.path.getsize(temp) > 0):
            # 返回码 0 但没产出内容：ffmpeg 认为这条流写不出东西，按无音轨处理
            _quiet_remove(temp)
            bump("noaudio")
            log("跳过·无音轨（提取结果为空）　%s" % _rel_label(rel, name))
            return
        if rc == 0:
            try:
                os.rename(temp, dst)       # 目标已存在会报 FileExistsError
            except FileExistsError:
                _quiet_remove(temp)
                bump("skip")
                log("跳过·已存在（本次刚生成）　%s" % _rel_label(rel, name))
                return
            except OSError as e:
                _quiet_remove(temp)
                bump("fail")
                log("✘ 改名失败　%s：%s" % (_rel_label(rel, name), e))
                return
            cache.add(folder, os.path.basename(dst))
            bump("ok")
            try:
                size = os.path.getsize(dst) / 1048576.0
                size_txt = "%.1f MB" % size if size >= 0.1 else "%.0f KB" % (size * 1024)
            except OSError:
                size_txt = "?"
            extra = ""
            if tracks is not None and len(tracks) > 1:
                extra = "（另有 %d 条音轨未取）" % (len(tracks) - 1)
            log("✔ %s → %s　%s%s" % (_rel_label(rel, name),
                                     os.path.basename(dst), size_txt, extra))
            return

        # ③ 失败
        _quiet_remove(temp)
        text = out or ""
        if "does not contain any stream" in text or "Output file is empty" in text:
            bump("noaudio")
            log("跳过·无音轨　%s" % _rel_label(rel, name))
            return
        bump("fail")
        detail = text.strip().splitlines()[-1] if text.strip() else "返回码 %d" % rc
        log("✘ 失败　%s\n    %s" % (_rel_label(rel, name), detail[:300]))

    it = iter(files)
    it_lock = threading.Lock()

    def next_item():
        with it_lock:
            try:
                return next(it)
            except StopIteration:
                return None

    def worker():
        while not stop.is_set():
            item = next_item()
            if item is None:
                return
            try:
                handle(item[0], item[1])
            except Exception:
                bump("fail")
                log("✘ 处理出错　%s\n%s"
                    % (os.path.basename(item[0]), traceback.format_exc()[-400:]))
            bump("done")

    count = max(1, min(8, int(opts.get("parallel") or 2)))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ============================================================
# 界面
# ============================================================

def _make_runtime():
    return {
        "running": False,
        "started": 0.0,
        "timer": None,
        "stop": threading.Event(),
        "lock": threading.Lock(),
        "plock": threading.Lock(),
        "procs": set(),
        "stats": {"total": 0, "done": 0, "ok": 0, "skip": 0,
                  "noaudio": 0, "fail": 0},
    }


def _snapshot(rt):
    with rt["lock"]:
        return dict(rt["stats"])


def build_audioextract_tab(parent, app=None):
    prefs = _load_prefs()

    page = ScrollPage(parent)
    page.pack(fill=tk.BOTH, expand=True)
    body = page.body
    body.grid_columnconfigure(0, weight=1)
    body.grid_rowconfigure(3, weight=1)

    make_tip(
        body, 0,
        "把视频里的音轨原样搬出来：只拆封装、不重编码（ffmpeg -c:a copy），"
        "几个 GB 的视频也只要一两秒，音质和源文件完全一致。"
        "输出文件夹里已经有同名音频就直接跳过，绝不覆盖；"
        "AAC 存成 .m4a、MP3 存成 .mp3、FLAC 存成 .flac，"
        "AC-3 / DTS 这类统一存成 .mka。多音轨视频只取第一条，其余条数记在日志里。",
        colspan=1, autowrap=True)

    form = make_form(body)
    form.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))

    var_src = make_path_row(form, 0, "源文件夹：", default=prefs["source"],
                            title="选择要提取音轨的文件夹")
    var_dst = make_path_row(form, 1, "输出文件夹：", default=prefs["dest"],
                            title="音频保存到哪个文件夹",
                            note="不存在会自动创建")
    var_exts = make_entry_row(form, 2, "视频后缀：", default=prefs["exts"],
                              entry_width=48,
                              note="空格分隔，默认已含 ts / flv / mp4 等")
    form_label(form, 3, "输出容器：")
    var_container = tk.StringVar(
        value=dict(CONTAINER_CHOICES).get(prefs["container"],
                                          CONTAINER_CHOICES[0][1]))
    ui.Combobox(form, variable=var_container, width=42, state="readonly",
                values=[label for _key, label in CONTAINER_CHOICES]).grid(
                    row=3, column=1, sticky="w", padx=(0, 6), pady=3)
    form_note(form, 3, "自动 = 按音轨编码选扩展名；mka 什么编码都能装")
    var_parallel = make_num_row(
        form, 4, "并行数：", prefs["parallel"], 1, 8,
        note="机械硬盘 1~2，SSD 可 4")
    var_recursive = make_check(form, "递归子文件夹", default=prefs["recursive"],
                               row=5, column=1, columnspan=3)
    var_keep = make_check(form, "保留子目录结构（递归时输出到对应子文件夹）",
                          default=prefs["keep_structure"],
                          row=6, column=1, columnspan=3)

    form_label(form, 7, "FFmpeg：")
    lbl_ff = form_note(form, 7, "检测中…（⑧ 视频压制 与本页共用同一个 ffmpeg）")

    def refresh_ff():
        """只在切到本页 / 点「指定…」后跑，不参与启动。"""
        try:
            ff, fp = locate_ffmpeg()
        except Exception:
            ff, fp = None, None
        if not ff:
            lbl_ff.configure(
                text="未找到 ffmpeg。把 ffmpeg.exe 放进程序目录或 bin\\ffmpeg，"
                     "或点左边的「指定…」。",
                text_color=COLOR["error"])
        elif not fp:
            lbl_ff.configure(
                text="找到 %s，但同目录没有 ffprobe —— 无法判断音轨编码，"
                     "会统一输出 .mka。" % os.path.basename(ff),
                text_color=COLOR["warning"])
        else:
            lbl_ff.configure(text="已就绪：%s" % os.path.dirname(ff),
                             text_color=COLOR["text_secondary"])

    def pick_ffmpeg():
        p = filedialog.askopenfilename(
            title="选择 ffmpeg.exe",
            filetypes=[("ffmpeg", "ffmpeg.exe"), ("可执行文件", "*.exe"),
                       ("所有文件", "*.*")])
        if p:
            save_ffmpeg_hint(p)
            refresh_ff()

    ui.Button(form, text="指定…", command=pick_ffmpeg).grid(
        row=7, column=1, sticky="w", padx=(0, 6), pady=3)

    # ---------------- 操作按钮 / 进度 ----------------
    btn_row = ui.Frame(body)
    btn_row.grid(row=2, column=0, sticky="ew", padx=8, pady=(10, 2))

    rt = _make_runtime()

    btn_start = ui.Button(
        btn_row, text="开始提取", height=36,
        bg=COLOR["accent"], fg=COLOR["text_on_accent"],
        activebackground=COLOR["accent_active"],
        disabledforeground=COLOR["text_on_accent"])
    btn_start.pack(side=tk.LEFT, padx=(0, 6))
    btn_stop = ui.Button(btn_row, text="停止", height=36, state=tk.DISABLED)
    btn_stop.pack(side=tk.LEFT, padx=(0, 16))

    bar = ui.ProgressBar(btn_row, mode="determinate", length=240)
    bar.pack(side=tk.LEFT, padx=(0, 10))
    lbl_status = ui.RowLabel(btn_row, text="未开始", font=FONTS["base"],
                             text_color=COLOR["text_secondary"])
    lbl_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # ---------------- 运行日志（默认收起） ----------------
    log_wrap = ui.Frame(body)
    log_wrap.grid(row=3, column=0, sticky="nsew", padx=8, pady=(4, 6))
    log_wrap.grid_columnconfigure(0, weight=1)
    log_wrap.grid_rowconfigure(1, weight=1)

    log_head = ui.Frame(log_wrap)
    log_head.grid(row=0, column=0, sticky="ew")
    var_log_open = tk.BooleanVar(value=False)
    btn_log_toggle = ui.Button(log_head, text="▸ 运行日志", width=11)
    btn_log_toggle.pack(side=tk.LEFT)
    ui.Button(log_head, text="清空", width=6,
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
        page.refresh()

    btn_log_toggle.config(command=toggle_log)
    log_body.grid_remove()

    # ---------------- 计时 / 进度轮询 ----------------
    def cancel_timer():
        if rt["timer"] is not None:
            try:
                page.after_cancel(rt["timer"])
            except Exception:
                pass
            rt["timer"] = None

    def tick():
        rt["timer"] = None
        if not rt["running"]:
            return
        st = _snapshot(rt)
        total, done = st["total"], st["done"]
        elapsed = max(0, int(time.monotonic() - rt["started"]))
        try:
            if total:
                bar.configure(maximum=total, value=done)
                lbl_status.configure(text=(
                    "已处理 %d/%d　成功 %d　跳过 %d　失败 %d　%02d:%02d"
                    % (done, total, st["ok"], st["skip"] + st["noaudio"],
                       st["fail"], elapsed // 60, elapsed % 60)))
            else:
                lbl_status.configure(text="正在扫描视频文件…　%02d:%02d"
                                          % (elapsed // 60, elapsed % 60))
            rt["timer"] = page.after(300, tick)
        except Exception:
            rt["timer"] = None

    # ---------------- 停止 ----------------
    def stop():
        if not rt["running"]:
            return
        rt["stop"].set()
        with rt["plock"]:
            running = list(rt["procs"])
        for proc in running:
            _kill(proc)
        try:
            btn_stop.config(state=tk.DISABLED)
            lbl_status.configure(text="正在停止…")
        except Exception:
            pass
        log("已请求停止：正在跑的那几个文件会被中断并清掉半成品，"
            "已经提取完的文件都保留。")

    btn_stop.config(command=stop)

    # ---------------- 开始 ----------------
    def start():
        if rt["running"]:
            return
        src = clean_path(var_src.get())
        dst = clean_path(var_dst.get())
        exts = parse_exts(var_exts.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的源文件夹！")
            return
        if not dst:
            messagebox.showerror("错误", "请填写输出文件夹！")
            return
        if os.path.exists(dst) and not os.path.isdir(dst):
            messagebox.showerror("错误", "输出路径已存在，但它不是文件夹！")
            return
        if not exts:
            messagebox.showerror("错误", "请至少填写一种视频后缀（如 ts flv mp4）！")
            return
        ffmpeg, ffprobe = locate_ffmpeg()
        if not ffmpeg:
            messagebox.showerror(
                "错误",
                "没找到 ffmpeg。\n\n"
                "把 ffmpeg.exe 和 ffprobe.exe 放到程序目录（或 bin\\ffmpeg 子目录），"
                "也可以点本页的「指定…」选一次，路径会记住。")
            return
        try:
            parallel = max(1, min(8, int(var_parallel.get())))
        except (TypeError, ValueError):
            messagebox.showerror("错误", "并行数必须是数字！")
            return

        container = _LABEL_TO_KEY.get(var_container.get().strip(), "auto")
        recursive = bool(var_recursive.get())
        keep = bool(var_keep.get())
        _save_prefs(source=src, dest=dst, exts=var_exts.get().strip(),
                    container=container, parallel=parallel,
                    recursive=recursive, keep_structure=keep)

        # 先把上一轮的轮询停掉再重置运行时（重置会把 timer 记录清空，
        # 清空后旧定时器还在队列里，会让 tick 出现两条并行的链）
        cancel_timer()
        rt.update(_make_runtime())
        rt["running"] = True
        rt["started"] = time.monotonic()
        summary = {"files": 0}

        btn_start.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
        bar.configure(maximum=1, value=0)
        lbl_status.configure(text="正在扫描视频文件…")
        clear_log(text_log)
        log("源文件夹：%s" % src)
        log("输出文件夹：%s" % dst)
        log("视频后缀：%s" % " ".join(sorted(e.lstrip(".") for e in exts)))
        log("输出容器：%s" % dict(CONTAINER_CHOICES).get(container, container))
        log("递归子文件夹：%s　保留目录结构：%s　并行：%d"
            % ("是" if recursive else "否", "是" if keep else "否", parallel))
        log("已存在的同名音频会自动跳过，不会覆盖任何文件。")
        log("— 开始扫描 —")
        tick()

        opts = {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "dest": dst,
                "container": container, "keep_structure": keep,
                "parallel": parallel}

        def work():
            files = scan_videos(src, exts, recursive)
            summary["files"] = len(files)
            with rt["lock"]:
                rt["stats"]["total"] = len(files)
            log("扫描完成：共 %d 个视频文件。" % len(files))
            if files:
                extract_job(files, opts, rt, log)

        def worker():
            error = None
            try:
                work()
            except Exception:
                error = traceback.format_exc()
            try:
                page.after(0, lambda: finish(error))
            except Exception:
                pass

        def finish(error):
            rt["running"] = False
            cancel_timer()
            try:
                bar.stop()
                btn_stop.config(state=tk.DISABLED)
                btn_start.config(state=tk.NORMAL)
            except Exception:
                pass
            if error:
                log("运行出错：\n" + error)
                lbl_status.configure(text="执行失败，详情见日志")
                messagebox.showerror("执行失败",
                                     "提取过程出错，详情见「运行日志」。")
                return

            st = _snapshot(rt)
            elapsed = max(0, int(time.monotonic() - rt["started"]))
            if rt["stop"].is_set():
                lbl_status.configure(text="已停止（成功 %d 个）" % st["ok"])
                log("已停止：成功 %d，跳过 %d，失败 %d。"
                    % (st["ok"], st["skip"] + st["noaudio"], st["fail"]))
                messagebox.showinfo(
                    "已停止",
                    "已停止。\n\n已提取好的文件都保留在输出文件夹里，"
                    "再次点「开始提取」会跳过它们继续。")
                return
            if not summary["files"]:
                lbl_status.configure(text="没有找到视频文件")
                messagebox.showinfo(
                    "没有找到视频",
                    "在源文件夹里没找到符合后缀的视频文件。\n\n"
                    "可检查：视频后缀是否填对、源文件夹是否选对、"
                    "要不要勾选「递归子文件夹」。")
                return

            lbl_status.configure(
                text="完成　成功 %d　跳过 %d　失败 %d　%02d:%02d"
                     % (st["ok"], st["skip"] + st["noaudio"], st["fail"],
                        elapsed // 60, elapsed % 60))
            log("完成：成功 %d 个，跳过 %d 个（已存在 %d / 无音轨 %d），失败 %d 个，"
                "用时 %d 分 %d 秒。"
                % (st["ok"], st["skip"] + st["noaudio"], st["skip"],
                   st["noaudio"], st["fail"], elapsed // 60, elapsed % 60))
            lines = ["共 %d 个视频文件：" % summary["files"],
                     "　成功提取：%d 个" % st["ok"],
                     "　已存在跳过：%d 个" % st["skip"],
                     "　无音轨跳过：%d 个" % st["noaudio"],
                     "　失败：%d 个" % st["fail"]]
            if st["fail"]:
                lines.append("")
                lines.append("失败原因见「运行日志」。")
            messagebox.showinfo("完成", "\n".join(lines))

        threading.Thread(target=worker, daemon=True).start()

    btn_start.config(command=start)

    # 关闭程序 / 页面销毁时别把 ffmpeg 留在后台
    def on_destroy(event):
        if event.widget is not page:
            return
        rt["running"] = False
        cancel_timer()
        rt["stop"].set()
        with rt["plock"]:
            running = list(rt["procs"])
        for proc in running:
            _kill(proc)

    page.bind("<Destroy>", on_destroy, add="+")

    # 拖入文件夹即可当源文件夹（拖的是文件则取它所在目录）
    if app is not None:
        def on_drop(paths):
            for p in paths:
                folder = p if os.path.isdir(p) else os.path.dirname(p)
                if folder and os.path.isdir(folder):
                    var_src.set(folder)
                    log("已接收拖入的文件夹：%s" % folder)
                    return

        try:
            app.register_drop_handler(parent, on_drop)
        except Exception:
            pass
        try:
            app.register_show_handler(parent, refresh_ff)
        except Exception:
            pass

    refresh_ff()
    return page
