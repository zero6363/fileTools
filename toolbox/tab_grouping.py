# -*- coding: utf-8 -*-
"""① 文件分组 / ③ 移动到根目录 / ④ 删除空文件夹"""
import os
import shutil
from datetime import datetime
import tkinter as tk
from tkinter import messagebox
from . import ui, ui as ttk

from .common import (clean_path, clear_log, form_label, form_note, make_check,
                     make_form, make_log_panel, make_num_box, make_path_row,
                     make_start_button, make_tip, run_task)


# ① 文件分组（按数量 / 按年月）  —— 原 分组*.bat
# ============================================================

ALL_FILES = "*"


def normalize_suffix(s):
    """把用户输入的后缀统一成 .xxx；* / 空 = 所有文件（任意后缀）。
    兼容 *.jpg 这类通配符写法（按 .jpg 处理）。"""
    s = (s or "").strip()
    if s in ("", "*", "*.*", ".*", "全部", "所有", "所有文件", "全部文件"):
        return ALL_FILES
    if s.startswith("*"):
        s = s[1:]
    if not s.startswith("."):
        s = "." + s
    return s


def prefix_from_suffix(s):
    """文件夹前缀随后缀自动变化；* / 空 使用 Group"""
    s = (s or "").strip()
    if s in ("", "*", "*.*", ".*", "全部", "所有", "所有文件", "全部文件"):
        return "Group"
    if s.startswith("*"):
        s = s[1:]
    return s.lstrip(".") or "Group"


def suffix_desc(suffix):
    """日志里描述当前筛选范围"""
    return "不限制后缀" if not suffix or suffix == ALL_FILES else f"后缀 {suffix}"


def empty_msg(suffix):
    """没有匹配文件时的提示：* 时说明是所有文件，别让人误以为是图片格式的问题"""
    if not suffix or suffix == ALL_FILES:
        return "未找到任何文件（目录内没有文件），无需处理。"
    return f"未找到匹配的文件（筛选条件：{suffix}），无需处理。"


def list_files_with_suffix(src_dir, suffix):
    """列出 src_dir 下（仅当前一层）匹配的文件名，按名称排序。
    suffix="*"（或空）表示所有文件，不限制后缀。"""
    match_all = not suffix or suffix == ALL_FILES
    result = []
    for name in os.listdir(src_dir):
        full = os.path.join(src_dir, name)
        if not os.path.isfile(full):
            continue
        if not match_all and not name.lower().endswith(suffix.lower()):
            continue
        result.append(name)
    return sorted(result)


def group_by_count(src_dir, suffix, per_group, prefix, log):
    """按数量分组：每组 per_group 个文件，移入 前缀_1、前缀_2 ... 文件夹。
    suffix="*"（或空）表示所有文件，任意后缀都会分组。
    目标已存在同名文件时跳过该文件，绝不覆盖，防止文件丢失。"""
    files = list_files_with_suffix(src_dir, suffix)
    if not files:
        log(empty_msg(suffix))
        return 0
    per_group = max(1, int(per_group))
    prefix = prefix or "Group"
    total_groups = (len(files) + per_group - 1) // per_group
    log(f"共找到 {len(files)} 个文件（{suffix_desc(suffix)}），按每组 {per_group} 个分为 {total_groups} 组"
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


def group_by_yearmonth(src_dir, suffix, log):
    """按年月分组：按文件修改时间归入 年-月 文件夹（如 2025-12）。
    suffix="*"（或空）表示所有文件，不限制后缀；也可以只处理某个后缀（如 .amr）。
    目标已存在同名文件时跳过该文件，绝不覆盖，防止文件丢失。"""
    files = list_files_with_suffix(src_dir, suffix)
    if not files:
        log(empty_msg(suffix))
        return 0
    log(f"共找到 {len(files)} 个文件（{suffix_desc(suffix)}），按修改时间归入 年-月 文件夹...")
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
# ③ 移动到根目录 —— 原 move.bat（省略了原 bat 的自我删除）
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
# ④ 删除空文件夹 —— 原 空文件夹删除.py
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
    make_tip(parent, 0, "按数量：按后缀分组（.jpg / .txt / .mp4，* = 所有文件、任意后缀），每组数量可自定义；"
                        "按年月：按修改时间归入 年-月 文件夹。同名文件自动跳过，不覆盖。",
             colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))

    MODE_COUNT = "按数量分组"
    MODE_YM = "按年月分组（按修改时间）"
    MODE_VALUES = (MODE_COUNT, MODE_YM)

    form_label(form, 0, "分组方式：")
    var_mode = tk.StringVar(value=MODE_COUNT)
    mode_box = ttk.Combobox(form, textvariable=var_mode, width=24, state="readonly",
                            values=list(MODE_VALUES))
    mode_box.grid(row=0, column=1, sticky="w", padx=(0, 6), pady=5)
    # 两种方式各带一句说明（只显示当前方式那句，免得被「每组数量/前缀」搞混）
    note_count = form_note(form, 0, "每组 N 个文件一个文件夹：前缀_1、前缀_2 …")
    note_ym = form_note(form, 0, "按修改时间归入 2026-11 这类年-月文件夹")
    note_ym.grid_remove()

    var_src = make_path_row(form, 1, "源目录：", title="选择源目录")

    # 后缀：两种模式各一份（按数量默认 .jpg；按年月默认 * = 所有文件，适合归档）
    form_label(form, 2, "文件后缀：")
    var_suffix = tk.StringVar(value=".jpg")
    suffix_box = ttk.Combobox(form, textvariable=var_suffix, width=14,
                              values=[".jpg", ".png", ".gif", ".txt", ".mp4", "*"])
    suffix_box.grid(row=2, column=1, sticky="w", padx=(0, 6), pady=5)
    var_suffix_ym = tk.StringVar(value="*")
    suffix_box_ym = ttk.Combobox(form, textvariable=var_suffix_ym, width=14,
                                 values=["*", ".jpg", ".png", ".mp4", ".txt"])
    suffix_box_ym.grid(row=2, column=1, sticky="w", padx=(0, 6), pady=5)
    suffix_note = form_note(form, 2, "任意后缀可直接输入；* = 所有文件（不限后缀）")

    per_label = form_label(form, 3, "每组数量：")
    box_per, var_per = make_num_box(form, 1500, 1, 1000000, width=8, step=100)
    box_per.grid(row=3, column=1, sticky="w", padx=(0, 6), pady=3)
    per_note = form_note(form, 3, "1 ~ 1000000")

    prefix_label = form_label(form, 4, "文件夹前缀：")
    var_prefix = tk.StringVar(value="jpg")
    prefix_entry = ttk.Entry(form, textvariable=var_prefix, width=20)
    prefix_entry.grid(row=4, column=1, sticky="w", padx=(0, 6), pady=5)
    prefix_note = form_note(form, 4, "随后缀自动变化")

    count_widgets = [suffix_box, per_label, box_per, per_note,
                     prefix_label, prefix_entry, prefix_note]
    ym_widgets = [suffix_box_ym]

    def sync_prefix(*_a):
        """按数量分组时，前缀跟着后缀走"""
        var_prefix.set(prefix_from_suffix(var_suffix.get()))

    def refresh_mode(*_a):
        """切换分组方式：按数量显示后缀/每组数量/前缀，按年月只留后缀"""
        mode = var_mode.get()
        if mode not in MODE_VALUES:
            return  # CTkComboBox 改写变量时会先过一次空值，忽略
        count_mode = mode == MODE_COUNT
        for w in count_widgets:
            if count_mode:
                w.grid()
            else:
                w.grid_remove()
        for w in ym_widgets:
            if count_mode:
                w.grid_remove()
            else:
                w.grid()
        if count_mode:
            sync_prefix()
        note_count.grid() if count_mode else note_count.grid_remove()
        note_ym.grid_remove() if count_mode else note_ym.grid()

    # CTkComboBox 不产生 <<ComboboxSelected>>（bind 被转发到内部 Entry，永远不触发），
    # 下拉选择只改内部 Entry 的 textvariable，所以用 command + 变量跟踪两条路。
    mode_box.configure(command=lambda _v=None: refresh_mode())
    suffix_box.configure(command=lambda _v=None: sync_prefix())
    var_mode.trace_add("write", lambda *_a: refresh_mode())
    var_suffix.trace_add("write", lambda *_a: sync_prefix())
    refresh_mode()

    text_log, log = make_log_panel(parent, 2, colspan=1)

    def start():
        src = clean_path(var_src.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的源目录！")
            return
        count_mode = var_mode.get() == MODE_COUNT
        suffix = normalize_suffix((var_suffix if count_mode else var_suffix_ym).get())
        try:
            if count_mode:
                per = max(1, int(var_per.get()))
                prefix = clean_path(var_prefix.get()) or "Group"
                work = lambda: group_by_count(src, suffix, per, prefix, log)
            else:
                work = lambda: group_by_yearmonth(src, suffix, log)
        except ValueError:
            messagebox.showerror("错误", "每组数量必须是数字！")
            return
        clear_log(text_log)
        run_task(btn, log, work,
                 on_done=lambda err: None if err else messagebox.showinfo("完成", "分组结束，详情见日志。"))

    btn = make_start_button(form, 5, "开始分组", start)


def build_flatten_tab(parent):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "把子目录里的文件全部移动到根目录。默认遇到同名文件跳过不移动；"
                        "勾选后自动改名移动，两种模式都不会覆盖已有文件。"
                        "移动完成后默认删除变空的子文件夹（含嵌套空目录，根目录本身保留）。",
             colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))

    var_src = make_path_row(form, 0, "根目录：", title="选择根目录")
    var_rec = make_check(form, "递归所有层级的子目录（取消勾选只处理一级子目录）",
                         default=True, row=1, column=1, columnspan=3)
    var_rename = make_check(form, "同名重命名移动（不勾选则跳过同名文件）",
                            default=False, row=2, column=1, columnspan=3)
    var_del = make_check(form, "移动完成后删除空的子文件夹",
                         default=True, row=3, column=1, columnspan=3)

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
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "循环删除目录下的所有空文件夹（包括嵌套的空目录），根目录本身会保留。",
             colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))
    var_src = make_path_row(form, 0, "目标目录：", title="选择目标目录")
    text_log, log = make_log_panel(parent, 2, colspan=1)

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

    btn = make_start_button(form, 1, "开始清理", start)
