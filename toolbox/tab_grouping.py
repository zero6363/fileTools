# -*- coding: utf-8 -*-
"""① 文件分组 / ② 移动到根目录 / ③ 删除空文件夹"""
import os
import shutil
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk

from .common import (APP_TITLE, DEFAULT_IMAGE_FORMATS, clean_path, clear_log,
                     form_label, form_note, make_check, make_form, make_log_panel,
                     make_num_box, make_path_row, make_start_button, make_tip,
                     run_task)


# ① 文件分组（按数量 / 按年月）  —— 原 分组*.bat
# ============================================================

def normalize_suffix(s):
    """把用户输入的后缀统一成 .xxx；* / 空 = 按图片格式分组（不是分组所有文件）。
    兼容 *.jpg 这类通配符写法（按 .jpg 处理）。"""
    s = (s or "").strip()
    if s in ("", "*", "*.*", ".*", "全部", "所有", "全部图片", "所有图片"):
        return "*"
    if s.startswith("*"):
        s = s[1:]
    if not s.startswith("."):
        s = "." + s
    return s


def prefix_from_suffix(s):
    """文件夹前缀随后缀自动变化；* / 空 使用 Group"""
    s = (s or "").strip()
    if s in ("", "*", "*.*", ".*", "全部", "所有", "全部图片", "所有图片"):
        return "Group"
    if s.startswith("*"):
        s = s[1:]
    return s.lstrip(".") or "Group"


def parse_formats(formats_str):
    """把 'jpg png' 这类字符串解析成小写扩展名集合 {'.jpg', '.png'}"""
    exts = set()
    for fmt in (formats_str or "").split():
        exts.add(("." + fmt.lstrip(".")).lower())
    return exts


def list_files_with_suffix(src_dir, suffix, image_formats=None):
    """列出 src_dir 下（仅当前层）匹配的文件名，按名称排序。
    suffix="*"（或空）时按图片格式过滤；否则按指定后缀过滤。"""
    if not suffix or suffix == "*":
        exts = parse_formats(image_formats or DEFAULT_IMAGE_FORMATS)
    else:
        exts = None
    result = []
    for name in os.listdir(src_dir):
        full = os.path.join(src_dir, name)
        if not os.path.isfile(full):
            continue
        if exts is not None:
            if os.path.splitext(name)[1].lower() not in exts:
                continue
        elif not name.lower().endswith(suffix.lower()):
            continue
        result.append(name)
    return sorted(result)


def group_by_count(src_dir, suffix, per_group, prefix, log, image_formats=None):
    """按数量分组：每组 per_group 个文件，移入 前缀_1、前缀_2 ... 文件夹。
    suffix="*" 时按图片格式分组（不会分组其他文件）。
    目标已存在同名文件时跳过该文件，绝不覆盖，防止文件丢失。"""
    files = list_files_with_suffix(src_dir, suffix, image_formats)
    if not files:
        if not suffix or suffix == "*":
            log(f"未找到图片文件（格式：{image_formats or DEFAULT_IMAGE_FORMATS}），无需处理。")
        else:
            log(f"未找到匹配文件（后缀：{suffix}），无需处理。")
        return 0
    per_group = max(1, int(per_group))
    prefix = prefix or "Group"
    total_groups = (len(files) + per_group - 1) // per_group
    if not suffix or suffix == "*":
        suffix_desc = f"图片格式（{image_formats or DEFAULT_IMAGE_FORMATS}）"
    else:
        suffix_desc = suffix
    log(f"共找到 {len(files)} 个文件（{suffix_desc}），按每组 {per_group} 个分为 {total_groups} 组"
        f"（文件夹名前缀 {prefix}_）...")
    moved = skipped = 0
    for gi in range(1, total_groups + 1):
        group_dir = os.path.join(src_dir, f"{prefix}_{gi}")
        os.makedirs(group_dir, exist_ok=True)
        chunk = files[(gi - 1) * per_group: gi * per_group]
        for name in chunk:
            src = os.path.join(src_dir, name)
            dst = os.path.join(group_dir, name)
            if os.path.exists(dst):
                skipped += 1
                log(f"  跳过（分组文件夹内已存在同名文件，防止覆盖）：{name}")
                continue
            shutil.move(src, dst)
            moved += 1
        log(f"  {os.path.basename(group_dir)}：本组 {len(chunk)} 个文件")
    log(f"分组完成：共移动 {moved} 个文件，跳过同名 {skipped} 个，生成 {total_groups} 个文件夹。")
    return total_groups


def group_by_yearmonth(src_dir, formats, log):
    """按年月分组：按文件修改时间归入 年-月 文件夹（如 2025-12）。
    formats 为 * / 空 / 全部 时处理所有格式的文件。
    目标已存在同名文件时跳过该文件，绝不覆盖，防止文件丢失。"""
    fmt_str = (formats or "").strip()
    if fmt_str in ("", "*", "*.*", "全部", "所有"):
        exts = None  # * = 所有格式
    else:
        exts = parse_formats(fmt_str)
        if not exts:
            raise ValueError("图片格式不能为空，请至少填写一种（如：jpg png）；* 表示所有格式")
    if exts is None:
        files = [n for n in os.listdir(src_dir)
                 if os.path.isfile(os.path.join(src_dir, n))]
    else:
        files = [n for n in os.listdir(src_dir)
                 if os.path.isfile(os.path.join(src_dir, n))
                 and os.path.splitext(n)[1].lower() in exts]
    files.sort()
    if not files:
        log(f"未找到匹配的文件（格式：{fmt_str or '所有格式'}）。")
        return 0
    log(f"共找到 {len(files)} 个文件（格式：{fmt_str or '所有格式'}），按修改时间归入 年-月 文件夹...")
    moved = skipped = 0
    folders = {}
    for name in files:
        src = os.path.join(src_dir, name)
        mtime = os.path.getmtime(src)
        folder_name = datetime.fromtimestamp(mtime).strftime("%Y-%m")
        if folder_name not in folders:
            target = os.path.join(src_dir, folder_name)
            os.makedirs(target, exist_ok=True)
            folders[folder_name] = target
            log(f"  已创建文件夹：{folder_name}")
        dst = os.path.join(folders[folder_name], name)
        if os.path.exists(dst):
            skipped += 1
            log(f"  跳过（{folder_name} 内已存在同名文件，防止覆盖）：{name}")
            continue
        shutil.move(src, dst)
        moved += 1
        if moved % 500 == 0:
            log(f"  已移动 {moved}/{len(files)} ...")
    log(f"年月分组完成：共移动 {moved} 个文件，跳过同名 {skipped} 个，归入 {len(folders)} 个月份文件夹。")
    return len(folders)


# ============================================================
# ② 移动到根目录 —— 原 move.bat（省略了原 bat 的自我删除）
# ============================================================

def flatten_files(src_dir, recursive, delete_empty, rename_on_conflict, log):
    """把子目录里的文件全部移动到根目录，绝不覆盖已有文件：
    rename_on_conflict=False 时遇到同名文件跳过（文件留在原子目录，不丢失）；
    rename_on_conflict=True  时自动改名（name_1.ext）后移动。"""
    root = os.path.abspath(src_dir)
    to_move = []
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(root):
            if os.path.abspath(dirpath) == root:
                continue
            for fname in filenames:
                to_move.append(os.path.join(dirpath, fname))
    else:
        for name in os.listdir(root):
            sub = os.path.join(root, name)
            if os.path.isdir(sub):
                for fname in os.listdir(sub):
                    full = os.path.join(sub, fname)
                    if os.path.isfile(full):
                        to_move.append(full)
    log(f"共找到 {len(to_move)} 个待移动文件（{'递归所有层级子目录' if recursive else '仅一级子目录'}）...")
    moved = renamed = skipped = failed = 0
    for src in to_move:
        name = os.path.basename(src)
        final = os.path.join(root, name)
        if os.path.exists(final):
            if not rename_on_conflict:
                skipped += 1
                log(f"  已存在同名文件，跳过（防止覆盖）：{name}")
                continue
            stem, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(final):
                final = os.path.join(root, f"{stem}_{i}{ext}")
                i += 1
            renamed += 1
            log(f"  重名自动改名：{name} -> {os.path.basename(final)}")
        try:
            shutil.move(src, final)
            moved += 1
            if moved % 500 == 0:
                log(f"  已移动 {moved}/{len(to_move)} ...")
        except Exception as e:
            failed += 1
            log(f"  移动失败：{src} -> {e}")
    log(f"移动完成：成功 {moved} 个，重名改名 {renamed} 个，跳过同名 {skipped} 个，失败 {failed} 个。")
    if delete_empty:
        remove_empty_dirs_loop(root, log)
    return moved


# ============================================================
# ③ 删除空文件夹 —— 原 空文件夹删除.py
# ============================================================

def _remove_empty_one_round(root, log):
    """执行一轮删除，返回本轮删除数量"""
    deleted = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if os.path.abspath(dirpath) == os.path.abspath(root):
            continue  # 保留用户选择的根目录本身
        if not filenames and not dirnames:
            try:
                os.rmdir(dirpath)
                deleted += 1
                log(f"  已删除：{dirpath}")
            except OSError as e:
                log(f"  删除失败 {dirpath}: {e}")
    return deleted


def remove_empty_dirs_loop(root, log):
    """循环扫描直到没有空目录（处理嵌套空目录）"""
    log(f"开始循环扫描目录：{root}")
    rounds = 0
    total = 0
    while True:
        rounds += 1
        n = _remove_empty_one_round(root, log)
        total += n
        if n == 0:
            break
    log(f"清理完成：共扫描 {rounds} 轮，删除 {total} 个空文件夹。")
    return total


# ============================================================


def build_group_tab(parent):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "按数量：按后缀分组（.jpg / .txt / .mp4，* = 按图片格式），每组数量可自定义；"
                        "按年月：按修改时间归入 年-月 文件夹。同名文件自动跳过，不覆盖。",
             colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 2))

    form_label(form, 0, "分组方式：")
    var_mode = tk.StringVar(value="按数量分组")
    mode_box = ttk.Combobox(form, textvariable=var_mode, width=24, state="readonly",
                            values=["按数量分组", "按年月分组（按修改时间）"])
    mode_box.grid(row=0, column=1, sticky="w", padx=(0, 6), pady=5)

    var_src = make_path_row(form, 1, "源目录：", title="选择源目录")

    # ---------- 按数量分组 ----------
    suffix_label = form_label(form, 2, "文件后缀：")
    var_suffix = tk.StringVar(value=".jpg")
    suffix_box = ttk.Combobox(form, textvariable=var_suffix, width=14,
                              values=[".jpg", ".gif", ".txt", ".mp4", "*"])
    suffix_box.grid(row=2, column=1, sticky="w", padx=(0, 6), pady=5)
    suffix_note = form_note(form, 2, "可选常用后缀，也可自定义")

    fmt_label = form_label(form, 3, "图片格式：")
    var_img_formats = tk.StringVar(value=DEFAULT_IMAGE_FORMATS)
    fmt_entry = ttk.Entry(form, textvariable=var_img_formats, width=40)
    fmt_entry.grid(row=3, column=1, sticky="w", padx=(0, 6), pady=5)
    fmt_note = form_note(form, 3, "* 时只处理这些图片格式，空格分隔")

    per_label = form_label(form, 4, "每组数量：")
    box_per, var_per = make_num_box(form, 1500, 1, 1000000, width=8, step=100)
    box_per.grid(row=4, column=1, sticky="w", padx=(0, 6), pady=3)

    prefix_label = form_label(form, 5, "文件夹前缀：")
    var_prefix = tk.StringVar(value="jpg")
    prefix_entry = ttk.Entry(form, textvariable=var_prefix, width=20)
    prefix_entry.grid(row=5, column=1, sticky="w", padx=(0, 6), pady=5)
    prefix_note = form_note(form, 5, "随后缀自动变化，可手动修改")

    count_widgets = [suffix_label, suffix_box, suffix_note,
                     fmt_label, fmt_entry, fmt_note,
                     per_label, box_per, prefix_label, prefix_entry, prefix_note]

    # ---------- 按年月分组 ----------
    ym_label = form_label(form, 2, "图片格式：")
    var_formats = tk.StringVar(value=DEFAULT_IMAGE_FORMATS)
    ym_entry = ttk.Entry(form, textvariable=var_formats, width=40)
    ym_entry.grid(row=2, column=1, sticky="w", padx=(0, 6), pady=5)
    ym_note = form_note(form, 2, "* = 所有格式；按修改时间生成 年-月 文件夹")
    ym_widgets = [ym_label, ym_entry, ym_note]

    def refresh_suffix_ui(*_args):
        """后缀为 * / 空 时显示图片格式框并同步前缀；否则隐藏"""
        var_prefix.set(prefix_from_suffix(var_suffix.get()))
        if not var_mode.get().startswith("按数量"):
            return
        if var_suffix.get().strip() in ("", "*", "*.*"):
            fmt_label.grid()
            fmt_entry.grid()
            fmt_note.grid()
        else:
            fmt_label.grid_remove()
            fmt_entry.grid_remove()
            fmt_note.grid_remove()

    suffix_box.bind("<<ComboboxSelected>>", refresh_suffix_ui)
    var_suffix.trace_add("write", lambda *_a: refresh_suffix_ui())

    def refresh_mode(*_args):
        if var_mode.get().startswith("按数量"):
            for w in ym_widgets:
                w.grid_remove()
            for w in count_widgets:
                w.grid()
            refresh_suffix_ui()
        else:
            for w in count_widgets:
                w.grid_remove()
            for w in ym_widgets:
                w.grid()

    mode_box.bind("<<ComboboxSelected>>", refresh_mode)
    refresh_mode()

    text_log, log = make_log_panel(parent, 2, colspan=1)

    def start():
        src = clean_path(var_src.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的源目录！")
            return
        try:
            if var_mode.get().startswith("按数量"):
                suffix = normalize_suffix(var_suffix.get())
                per = max(1, int(var_per.get()))
                prefix = clean_path(var_prefix.get()) or "Group"
                img_formats = var_img_formats.get().strip() or DEFAULT_IMAGE_FORMATS
                work = lambda: group_by_count(src, suffix, per, prefix, log, img_formats)
            else:
                fmts = var_formats.get().strip() or DEFAULT_IMAGE_FORMATS
                work = lambda: group_by_yearmonth(src, fmts, log)
        except ValueError:
            messagebox.showerror("错误", "每组数量必须是数字！")
            return
        clear_log(text_log)
        run_task(btn, log, work,
                 on_done=lambda err: None if err else messagebox.showinfo("完成", "分组结束，详情见日志。"))

    btn = make_start_button(form, 6, "开始分组", start)


def build_flatten_tab(parent):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "把子目录里的文件全部移动到根目录。默认遇到同名文件跳过不移动；"
                        "勾选后自动改名移动，两种模式都不会覆盖已有文件。",
             colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 2))

    var_src = make_path_row(form, 0, "根目录：", title="选择根目录")
    var_rec = make_check(form, "递归所有层级的子目录（取消勾选只处理一级子目录）",
                         default=True, row=1, column=1)
    var_rename = make_check(form, "同名重命名移动（不勾选则跳过同名文件）",
                            default=False, row=2, column=1)
    var_del = make_check(form, "移动完成后删除空的子文件夹",
                         default=False, row=3, column=1)

    text_log, log = make_log_panel(parent, 2, colspan=1)

    def start():
        src = clean_path(var_src.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的根目录！")
            return
        clear_log(text_log)
        run_task(btn, log,
                 lambda: flatten_files(src, var_rec.get(), var_del.get(), var_rename.get(), log),
                 on_done=lambda err: None if err else messagebox.showinfo("完成", "移动结束，详情见日志。"))

    btn = make_start_button(form, 4, "开始移动", start)


def build_empty_tab(parent):
    parent.grid_rowconfigure(3, weight=1)
    make_tip(parent, 0, "循环删除目录下的所有空文件夹（包括嵌套的空目录），根目录本身会保留。")
    var_src = make_path_row(parent, 1, "目标目录：", title="选择目标目录")
    text_log, log = make_log_panel(parent, 3)

    def start():
        src = clean_path(var_src.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的目标目录！")
            return
        if not messagebox.askyesno("确认删除", f"将删除【{src}】下所有空文件夹（含嵌套空目录，根目录本身保留）。\n确认继续？"):
            return
        clear_log(text_log)
        run_task(btn, log, lambda: remove_empty_dirs_loop(src, log),
                 on_done=lambda err: None if err else messagebox.showinfo("完成", "清理结束，详情见日志。"))

    btn = make_start_button(parent, 2, "开始清理", start)
