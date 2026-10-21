# -*- coding: utf-8 -*-
"""④ 实况图分组（JPEG 内嵌视频识别 / LivePhotos 归档）"""
import os
import shutil
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from .common import (clean_path, clear_log, get_unique_path, make_check,
                     make_form, make_log_panel, make_path_row,
                     make_start_button, make_tip, run_task)


# ④ 实况图分组 —— 原 实况分组-可视化.py
# ============================================================

def extract_video_from_jpeg(jpeg_path):
    """尝试从 JPEG 中提取内嵌视频（安卓 Motion Photo 等）"""
    try:
        with open(jpeg_path, "rb") as f:
            data = f.read()
        video_markers = [b"ftypmp41", b"ftypmp42", b"ftypqt  ", b"ftypisom",
                         b"ftypavc1", b"ftyphevc", b"ftyphevx", b"ftypiso2", b"moov"]
        earliest_index = -1
        for marker in video_markers:
            index = data.find(marker)
            if index != -1:
                start_index = max(0, index - 4)
                if earliest_index == -1 or start_index < earliest_index:
                    earliest_index = start_index
        if earliest_index != -1:
            video_data = data[earliest_index:]
            if len(video_data) > 10240:
                return video_data, None
            return None, "视频数据过小(<10KB)，可能不是完整视频"
        return None, "未在文件内部找到视频流标识"
    except Exception as e:
        return None, f"读取文件异常: {e}"


def safe_move(src, dest_dir):
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")
    final_dest = get_unique_path(dest_dir / src.name)
    shutil.move(str(src), str(final_dest))
    return final_dest


def process_live_file(file_path, live_root, live_vids, static_dir,
                      move_live, move_static, extract_video, log, stats):
    """处理单个图片文件"""
    stats["total"] += 1
    stem = file_path.stem
    suffix = file_path.suffix.lower()

    is_live = False
    embedded_video_data = None
    extract_error = None

    if suffix in (".jpg", ".jpeg"):
        embedded_video_data, extract_error = extract_video_from_jpeg(file_path)
        if embedded_video_data:
            is_live = True

    if is_live:
        stats["live"] += 1
        log(f"[实况] {file_path.name}")
        if move_live:
            try:
                safe_move(file_path, live_root)
                log("  -> 已移动到 LivePhotos")
            except Exception as e:
                log(f"  -> 移动图片失败: {e}")
                stats["errors"] += 1
        else:
            log("  -> 图片保持原位 (未勾选移动实况图)")

        if extract_video:
            if embedded_video_data:
                dest_vid_path = get_unique_path(live_vids / f"{stem}.mp4")
                try:
                    with open(dest_vid_path, "wb") as f:
                        f.write(embedded_video_data)
                    log(f"  -> 视频已提取到 videos/{dest_vid_path.name}")
                except Exception as e:
                    log(f"  -> 提取视频失败: {e}")
                    stats["errors"] += 1
        else:
            log("  -> 不提取视频 (未勾选提取视频)")
    else:
        stats["static"] += 1
        log(f"[静态] {file_path.name}")
        if extract_video and suffix in (".jpg", ".jpeg") and extract_error:
            log(f"  -> 警告: 尝试提取视频失败，{extract_error}")
        if move_static:
            try:
                safe_move(file_path, static_dir)
                log("  -> 已移动到 StaticPhotos")
            except Exception as e:
                log(f"  -> 移动图片失败: {e}")
                stats["errors"] += 1
        else:
            log("  -> 图片保持原位 (未勾选移动静态图)")


def run_live_sort(target_dir, move_static, move_live, extract_video, log):
    stats = {"total": 0, "live": 0, "static": 0, "errors": 0, "skipped": 0}
    target_path = Path(target_dir).resolve()
    image_extensions = {".jpg", ".jpeg", ".heic", ".heif", ".png"}

    live_root = target_path / "LivePhotos"
    live_vids = live_root / "videos"
    static_dir = target_path / "StaticPhotos"

    if move_live:
        live_root.mkdir(parents=True, exist_ok=True)
    if extract_video:
        live_vids.mkdir(parents=True, exist_ok=True)
    if move_static:
        static_dir.mkdir(parents=True, exist_ok=True)

    all_files = [f for f in target_path.iterdir() if f.is_file()]
    image_files = [f for f in all_files if f.suffix.lower() in image_extensions]
    stats["skipped"] = len(all_files) - len(image_files)

    if not image_files:
        log("未找到支持的图片文件。任务结束。")
        return stats

    log(f"找到 {len(image_files)} 个图片文件，开始分析...")
    if stats["skipped"] > 0:
        log(f"(已忽略 {stats['skipped']} 个非图片文件)")
    log("-" * 40)

    for file_path in sorted(image_files, key=lambda x: x.name):
        if file_path.parent != target_path:
            continue
        try:
            process_live_file(file_path, live_root, live_vids, static_dir,
                              move_live, move_static, extract_video, log, stats)
        except Exception as e:
            log(f"处理 {file_path.name} 时发生严重错误: {e}")
            stats["errors"] += 1

    log("")
    log("=" * 40)
    log("             处理完成汇总")
    log("=" * 40)
    log(f"总处理图片数: {stats['total']}")
    log(f"识别为实况图: {stats['live']}")
    log(f"识别为静态图: {stats['static']}")
    if stats["skipped"] > 0:
        log(f"忽略非图片:   {stats['skipped']}")
    if stats["errors"] > 0:
        log(f"处理出错数:   {stats['errors']}")
    log("=" * 40)
    return stats


# ============================================================


def build_live_tab(parent):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "识别安卓实况图（JPEG 内嵌视频）：实况图移入 LivePhotos 并提取 mp4，"
                        "静态图移入 StaticPhotos。", colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 2))

    var_src = make_path_row(form, 0, "目标文件夹：", title="选择目标文件夹")
    var_static = make_check(form, "移动静态图片到 StaticPhotos（不勾选保持原位）",
                            default=True, row=1, column=1)
    var_live = make_check(form, "移动实况图片到 LivePhotos（不勾选保持原位）",
                          default=True, row=2, column=1)
    var_extract = make_check(form, "提取实况图视频到 LivePhotos/videos",
                             default=True, row=3, column=1)

    text_log, log = make_log_panel(parent, 2, colspan=1)

    def start():
        src = clean_path(var_src.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的目标文件夹！")
            return
        if not (var_static.get() or var_live.get() or var_extract.get()):
            messagebox.showwarning("提示", "您没有勾选任何操作选项，文件将不会有任何变化。")
            return
        clear_log(text_log)
        run_task(btn, log,
                 lambda: run_live_sort(src, var_static.get(), var_live.get(), var_extract.get(), log),
                 on_done=lambda err: None if err else messagebox.showinfo("完成", "处理结束，详情见日志。"))

    btn = make_start_button(form, 4, "开始处理", start)
