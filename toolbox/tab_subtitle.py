# -*- coding: utf-8 -*-
"""⑬ 视频字幕清理（找出没有视频伴生的 srt/ass 等字幕，移动到 novideo/）

设计要点：
- 「关联」按文件主名（去掉扩展名）匹配，Windows 文件名不区分大小写，
  所以比较一律 casefold 后进行，`ABC.srt` 能配到 `abc.ts`。
- 语言后缀字幕（abc.zh.srt / abc.chs.default.srt…）默认也算关联：
  循环剥掉最后一个 `.xxx` 后缀再去匹配视频主名，最多剥 3 层。
  剥只会让字幕「更像有关联」，绝不会把本该保留的误移走，方向安全。
- 扫描根目录下统一建 novideo/ 存放无视频伴生的字幕；递归扫描时把 novideo/
  本身排除掉，上一轮移进去的字幕不会被当成新一轮的扫描对象。
- 同名冲突不覆盖：自动追加 _1、_2（get_unique_path）。
- 目录遍历全部在后台线程（run_task），主线程只收日志。
"""
import os
import shutil
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

from . import config
from . import ui, ui as ttk
from .common import (clean_path, clear_log, get_unique_path, make_check,
                     make_entry_row, make_form, make_log_panel,
                     make_path_row, make_start_button, make_tip, run_task)

CONFIG_SECTION = "subtitleclean"

# 默认视频后缀（用户可改）。这里只按扩展名认视频、不解析文件头，
# 所以 rm/rmvb 这类读不出分辨率的容器也一并列上。
DEFAULT_VIDEO_EXTS = ("ts flv mp4 mkv mov m4v avi wmv webm mpg mpeg "
                      "m2ts mts tp trp f4v 3gp rmvb rm asf vob wtv")

# 默认字幕后缀：srt 是主力，ass/ssa/sub 顺手带上，不想清理哪个删掉即可
DEFAULT_SUB_EXTS = "srt ass ssa sub"


# ============================================================
# 配置
# ============================================================

def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _load_prefs():
    sec = config.get_section(CONFIG_SECTION)
    return {
        "source": str(sec.get("source") or ""),
        "video_exts": str(sec.get("video_exts") or DEFAULT_VIDEO_EXTS),
        "sub_exts": str(sec.get("sub_exts") or DEFAULT_SUB_EXTS),
        "recursive": _as_bool(sec.get("recursive", False), False),
        "lang_suffix": _as_bool(sec.get("lang_suffix", True), True),
        "move_nosrt": _as_bool(sec.get("move_nosrt", False), False),
        "preview_only": _as_bool(sec.get("preview_only", False), False),
    }


def _save_prefs(**values):
    try:
        config.update_section(CONFIG_SECTION, **values)
    except Exception:
        pass


def parse_exts(text):
    """「ts flv .mp4 ＊.MKV」→ {'.ts', '.flv', '.mp4', '.mkv'}（兼容全角分隔符）"""
    out = set()
    text = (text or "").replace("，", " ").replace("、", " ") \
                        .replace(";", " ").replace("；", " ") \
                        .replace(",", " ").replace("＊", "*")
    for raw in text.split():
        raw = raw.strip().lstrip("*").lstrip(".").lower()
        if raw:
            out.add("." + raw)
    return out


def is_related(sub_stem, video_stems):
    """字幕主名是否与某个视频关联（casefold 比较，忽略语言后缀交给调用方）"""
    return sub_stem.casefold() in video_stems


def strip_lang_suffixes(stem, max_layers=3):
    """`abc.zh.default` → 依次生成 `abc.zh`、`abc`（用于语言后缀匹配）"""
    cur = stem
    for _ in range(max_layers):
        if "." not in cur:
            break
        cur = cur.rsplit(".", 1)[0]
        yield cur


# ============================================================
# 核心扫描
# ============================================================

def collect_files(root, recursive, skip_dirs):
    """收集 (绝对路径, 相对路径)；skip_dirs 里的目录本身及其内容一律跳过"""
    items = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            # 剪枝：不进 novideo/ / nosrt/（避免把上轮结果再扫一遍）
            dirnames[:] = [d for d in sorted(dirnames)
                           if os.path.normpath(os.path.join(dirpath, d))
                           not in skip_dirs]
            for name in sorted(filenames):
                p = os.path.join(dirpath, name)
                rel = os.path.relpath(p, root)
                items.append((p, rel))
    else:
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            return items
        for entry in entries:
            try:
                if entry.is_file():
                    items.append((entry.path, entry.name))
            except OSError:
                continue
    return items


def _move_batch(items, dest, dest_name, title, preview_only, log):
    """移动一批文件到 dest（预览只列不动）。返回已移动数。"""
    if not items:
        return 0
    log(f"发现 {len(items)} 个{title}：")
    moved = 0
    for path, rel in items:
        if preview_only:
            log(f"  [仅列出] {rel}")
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"  无法创建 {dest_name}/ 文件夹：{e}")
            break
        dst = get_unique_path(dest / os.path.basename(path))
        try:
            shutil.move(path, str(dst))
        except OSError as e:
            log(f"  移动失败：{rel} -> {e}")
            continue
        moved += 1
        log(f"  已移动：{rel} -> {dest_name}/{dst.name}")
    return moved


def run_orphan_scan(root_dir, video_exts_text, sub_exts_text, recursive,
                    lang_suffix, move_nosrt, preview_only, log):
    """扫描并（可选）移动无视频伴生的字幕 / 没有字幕的视频。

    两种模式互斥，由 move_nosrt 切换：
      False —— 只清理无视频伴生的字幕（→ novideo/），视频不动；
      True  —— 只移动无字幕伴生的视频（→ nosrt/），字幕一律不动。

    返回统计 dict；参数无效返回 None：
      orphan_subs / moved_subs / kept_subs  无视频伴生的字幕（仅默认模式）
      nosrt_vids / moved_vids               没有字幕的视频（仅反向模式）
    """
    root = Path(root_dir)
    dest_sub = root / "novideo"
    dest_vid = root / "nosrt"
    skip_dirs = {os.path.normpath(str(dest_sub)),
                 os.path.normpath(str(dest_vid))}

    video_exts = parse_exts(video_exts_text)
    sub_exts = parse_exts(sub_exts_text)
    if not video_exts:
        log("视频格式为空，请至少填写一种。")
        return None
    if not sub_exts:
        log("字幕格式为空，请至少填写一种。")
        return None

    files = collect_files(str(root), recursive, skip_dirs)
    videos = []          # (path, rel, stem)
    subs = []            # (path, rel, stem)
    for path, rel in files:
        stem, ext = os.path.splitext(os.path.basename(path))
        if ext.lower() in video_exts:
            videos.append((path, rel, stem))
        elif ext.lower() in sub_exts:
            subs.append((path, rel, stem))
    log(f"扫描完成：视频 {len(videos)} 个，字幕 {len(subs)} 个。")

    video_stems = {s.casefold() for _p, _r, s in videos}

    # 字幕的「有效主名」：stem 本身 + 语言后缀剥壳产物（abc.zh.default → abc.zh → abc）。
    # 正反向都用它判定：字幕 abc.zh.srt 能护住视频 abc.mp4，反之亦然。
    sub_stems = set()
    for _p, _r, stem in subs:
        sub_stems.add(stem.casefold())
        if lang_suffix:
            for s in strip_lang_suffixes(stem):
                sub_stems.add(s.casefold())

    stats = {"orphan_subs": 0, "moved_subs": 0, "kept_subs": 0,
             "nosrt_vids": 0, "moved_vids": 0}

    # 两种模式互斥：
    #   默认      —— 没有视频伴生的字幕 → novideo/，视频不动
    #   反向勾选  —— 没有字幕伴生的视频 → nosrt/，字幕一律不动
    if move_nosrt:
        log("反向模式：只处理没有字幕的视频，字幕一律不动。")
        nosrt_vids = [(p, r) for p, r, s in videos
                      if s.casefold() not in sub_stems]
        stats["nosrt_vids"] = len(nosrt_vids)
        if nosrt_vids:
            stats["moved_vids"] = _move_batch(nosrt_vids, dest_vid, "nosrt",
                                              "无字幕视频", preview_only, log)
        else:
            log("没有发现无字幕伴生的视频。")
    else:
        orphan_subs = []
        for path, rel, stem in subs:
            if is_related(stem, video_stems):
                continue
            if lang_suffix and any(s.casefold() in video_stems
                                   for s in strip_lang_suffixes(stem)):
                log(f"  有关联（语言后缀）：{rel}")
                continue
            orphan_subs.append((path, rel))
        stats["orphan_subs"] = len(orphan_subs)
        stats["kept_subs"] = len(subs) - len(orphan_subs)
        if orphan_subs:
            stats["moved_subs"] = _move_batch(orphan_subs, dest_sub, "novideo",
                                              "无视频字幕", preview_only, log)
        else:
            log("没有发现无视频伴生的字幕。")

    if preview_only:
        log("预览结束：未移动任何文件。")
    elif move_nosrt:
        log(f"完成：无字幕视频移动 {stats['moved_vids']}/{stats['nosrt_vids']}。")
    else:
        log(f"完成：无视频字幕移动 {stats['moved_subs']}/{stats['orphan_subs']}。")
    return stats


# ============================================================
# UI
# ============================================================

def build_subtitle_tab(parent, app=None):
    prefs = _load_prefs()

    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "清理字幕与视频的配对残留，两种模式互斥：默认找出没有视频伴生的字幕"
                        "（视频已删/未下载）统一移到 novideo/；勾选反向后改为找出没有字幕伴生的"
                        "视频移到 nosrt/，字幕一律不动。按文件主名匹配（不要求同目录），"
                        "abc.zh.srt 这类语言后缀字幕默认也能配到 abc.mp4。",
             colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))

    var_src = make_path_row(form, 0, "目标文件夹：", default=prefs["source"],
                            title="选择要清理的文件夹")
    var_video = make_entry_row(form, 1, "视频格式：", default=prefs["video_exts"],
                               entry_width=46, note="空格分隔，可增删")
    var_subs = make_entry_row(form, 2, "字幕格式：", default=prefs["sub_exts"],
                              entry_width=46, note="空格分隔，可增删")
    var_rec = make_check(form, "包含子文件夹（移出的文件统一放到根目录的 novideo/ 或 nosrt/）",
                         default=prefs["recursive"], row=3, column=1,
                         columnspan=3)
    var_lang = make_check(form, "识别语言后缀（abc.zh.srt / abc.chs.default.srt 配 abc.mp4）",
                          default=prefs["lang_suffix"], row=4, column=1,
                          columnspan=3)
    var_nosrt = make_check(form, "反向模式：没有字幕的视频移到 nosrt/（不再处理字幕）",
                           default=prefs["move_nosrt"], row=5, column=1,
                           columnspan=3)
    var_preview = make_check(form, "仅列出，不移动（先预览确认再执行）",
                             default=prefs["preview_only"], row=6, column=1,
                             columnspan=3)

    text_log, log = make_log_panel(parent, 2, colspan=1)

    def start():
        src = clean_path(var_src.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的目标文件夹！")
            return
        if not var_video.get().strip() or not var_subs.get().strip():
            messagebox.showerror("错误", "视频格式与字幕格式至少各填一种！")
            return
        _save_prefs(source=src, video_exts=var_video.get().strip(),
                    sub_exts=var_subs.get().strip(), recursive=var_rec.get(),
                    lang_suffix=var_lang.get(), move_nosrt=var_nosrt.get(),
                    preview_only=var_preview.get())
        clear_log(text_log)
        run_task(btn, log,
                 lambda: run_orphan_scan(src, var_video.get(), var_subs.get(),
                                         var_rec.get(), var_lang.get(),
                                         var_nosrt.get(), var_preview.get(),
                                         log),
                 on_done=lambda err: None if err else messagebox.showinfo(
                     "完成", "扫描结束，详情见日志。"))

    btn = make_start_button(form, 6, "开始扫描", start)
