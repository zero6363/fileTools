# -*- coding: utf-8 -*-
"""⑤ 文件名添加路径 / ⑥ 序号重命名（拖放 + 数值化自然排序 + 两阶段防覆盖）"""
import os
import tkinter as tk
from tkinter import filedialog, messagebox
from . import ui, ui as ttk

from .common import (clean_path, clear_log, make_check, make_log_panel,
                     make_num_box, make_path_row, make_spin_row,
                     make_start_button, make_tip, natural_key, run_task)
from .theme import COLOR


# ⑤ 文件夹重命名 —— 原 重命名.py
# ============================================================

def safe_filename(name):
    """清理 Windows 非法文件名符号"""
    bad_chars = r'\/:*?"<>|'
    for c in bad_chars:
        name = name.replace(c, "_")
    return name


def plan_rename(root_dir, recursive, use_root_name):
    """生成重命名计划，返回 ([(old_full, new_full)], [超长跳过的相对路径])"""
    preview = []
    long_items = []
    root_folder_name = os.path.basename(root_dir)
    if recursive:
        walk_func = os.walk(root_dir)
    else:
        walk_func = [(root_dir, [], os.listdir(root_dir))]

    for dirpath, _, filenames in walk_func:
        rel_dir = os.path.relpath(dirpath, root_dir)
        path_parts = rel_dir.split(os.sep)
        name_parts = []
        if use_root_name:
            name_parts.append(root_folder_name)
        if rel_dir != ".":
            name_parts.extend(path_parts)

        for fname in filenames:
            old_full = os.path.join(dirpath, fname)
            name_no_ext, ext = os.path.splitext(fname)
            new_base = safe_filename("-".join(name_parts + [name_no_ext]))
            new_full = os.path.join(dirpath, new_base + ext)

            if len(new_full) > 255:
                old_rel = os.path.relpath(old_full, root_dir).replace("/", "\\")
                long_items.append(".\\" + old_rel)
                continue
            if old_full == new_full:
                continue
            preview.append((old_full, new_full))
    return preview, long_items


def apply_rename(preview, log):
    success = fail = 0
    for old, new in preview:
        try:
            if os.path.exists(new):
                raise FileExistsError("目标文件已存在")
            os.rename(old, new)
            success += 1
        except Exception as e:
            fail += 1
            log(f"失败：{old} 原因:{e}")
    return success, fail


# ============================================================
# ⑥ 序号重命名（新增）
# ============================================================


def collect_files(paths, recursive):
    """把文件/文件夹路径列表展开为文件列表：文件夹递归遍历，去重后按自然排序"""
    files = []
    for p in paths:
        if os.path.isfile(p):
            files.append(os.path.abspath(p))
        elif os.path.isdir(p):
            if recursive:
                for dirpath, _dirs, names in os.walk(p):
                    for n in names:
                        fp = os.path.join(dirpath, n)
                        if os.path.isfile(fp):
                            files.append(os.path.abspath(fp))
            else:
                for n in os.listdir(p):
                    fp = os.path.join(p, n)
                    if os.path.isfile(fp):
                        files.append(os.path.abspath(fp))
    unique = []
    seen = set()
    for f in files:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    unique.sort(key=natural_key)
    return unique


def collect_dirs(paths):
    """仅文件夹模式：把拖入的文件夹展开为其子文件夹（仅当前层，文件忽略），
    去重后按自然排序。直接拖入的子文件夹自身也会被收集。"""
    dirs = []
    for p in paths:
        if not os.path.isdir(p):
            continue
        for name in os.listdir(p):
            fp = os.path.join(p, name)
            if os.path.isdir(fp):
                dirs.append(os.path.abspath(fp))
    unique = []
    seen = set()
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    unique.sort(key=natural_key)
    return unique


def build_seq_plan(files, mode, width, start):
    """生成序号重命名计划 [(old, new)]；
    mode="all"    全部重命名为序号：001.ext、002.ext（文件保留扩展名，文件夹用纯序号 001）
    mode="prefix" 前加序号：001-原文件名
    序号按 files 的自然排序顺序递增；新旧名相同的条目不放入计划。"""
    plan = []
    seq = int(start)
    for old in files:
        directory = os.path.dirname(old)
        name = os.path.basename(old)
        if mode == "all":
            if os.path.isdir(old):
                new_name = f"{seq:0{width}d}"            # 文件夹：纯序号，不带扩展名
            else:
                new_name = f"{seq:0{width}d}{os.path.splitext(name)[1]}"
        else:
            new_name = f"{seq:0{width}d}-{name}"
        new = os.path.join(directory, new_name)
        if os.path.abspath(new) != os.path.abspath(old):
            plan.append((old, new))
        seq += 1
    return plan


def execute_seq_plan(plan, log):
    """两阶段重命名，彻底避免覆盖丢失：
    第一阶段先把所有文件改到唯一临时名；第二阶段再改为正式名。
    若正式名被计划外的同名文件占用，则还原该文件并跳过，保证不丢数据。"""
    temp_map = []
    for i, (old, final) in enumerate(plan):
        if not os.path.exists(old):
            log(f"跳过（原文件不存在）：{old}")
            continue
        tmp = os.path.join(os.path.dirname(old), f".__tb_tmp_{i}_{os.path.basename(old)}")
        while os.path.exists(tmp):
            tmp += "_"
        try:
            os.rename(old, tmp)
        except Exception as e:
            log(f"失败（第一阶段）：{old} -> {e}")
            continue
        temp_map.append((tmp, final, old))

    success = fail = 0
    for tmp, final, old in temp_map:
        if os.path.exists(final):
            try:
                os.rename(tmp, old)  # 还原，绝不覆盖
            except Exception:
                pass
            log(f"跳过（目标已存在同名文件，已还原）：{os.path.basename(final)}")
            fail += 1
            continue
        try:
            os.rename(tmp, final)
            success += 1
        except Exception as e:
            log(f"失败（第二阶段）：{old} -> {e}")
            fail += 1
    return success, fail


# ============================================================


def build_rename_tab(parent):
    # 本页控件较多，统一使用 pack 布局
    ui.Label(parent, text="把文件名改成带目录层级的名称（如 根目录-子目录-原名），先预览确认后再执行。",
              foreground=COLOR["text_secondary"]).pack(anchor="w", padx=8, pady=(0, 6))

    var_path = tk.StringVar()
    var_rec = tk.BooleanVar(value=True)
    var_root = tk.BooleanVar(value=True)

    frame_top = ui.Frame(parent, padding=6)
    frame_top.pack(fill=tk.X)
    ui.Label(frame_top, text="目标文件夹：").pack(side=tk.LEFT)
    ttk.Entry(frame_top, textvariable=var_path, width=60).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

    def browse():
        p = filedialog.askdirectory(title="选择文件夹")
        if p:
            var_path.set(p)

    ui.Button(frame_top, text="选择文件夹", command=browse).pack(side=tk.LEFT)

    frame_check = ui.Frame(parent, padding=4)
    frame_check.pack(anchor="w", padx=6)
    ttk.Checkbutton(frame_check, text="递归遍历子目录", variable=var_rec).pack(side=tk.LEFT, padx=10)
    ttk.Checkbutton(frame_check, text="带上输入文件夹名称", variable=var_root).pack(side=tk.LEFT, padx=10)

    frame_btn = ui.Frame(parent, padding=4)
    frame_btn.pack(anchor="w", padx=6, pady=4)

    table_frame = ui.Frame(parent)
    table_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
    table_frame.grid_columnconfigure(0, weight=1)
    table_frame.grid_columnconfigure(1, weight=1)
    table_frame.grid_rowconfigure(0, weight=1)

    left_frame = ui.Frame(table_frame)
    left_frame.grid(row=0, column=0, sticky="nsew", padx=2)
    left_frame.grid_rowconfigure(0, weight=1)
    left_frame.grid_columnconfigure(0, weight=1)

    tree_old = ttk.Treeview(left_frame, columns=("val",), show="headings")
    tree_old.heading("val", text="原文件(相对路径)")
    tree_old.column("val", width=460, minwidth=200, stretch=False)
    v_scroll_old = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=tree_old.yview)
    h_scroll_old = ttk.Scrollbar(left_frame, orient=tk.HORIZONTAL, command=tree_old.xview)
    tree_old.config(yscrollcommand=v_scroll_old.set, xscrollcommand=h_scroll_old.set)
    tree_old.grid(row=0, column=0, sticky="nsew")
    v_scroll_old.grid(row=0, column=1, sticky="ns")
    h_scroll_old.grid(row=1, column=0, sticky="ew")

    right_frame = ui.Frame(table_frame)
    right_frame.grid(row=0, column=1, sticky="nsew", padx=2)
    right_frame.grid_rowconfigure(0, weight=1)
    right_frame.grid_columnconfigure(0, weight=1)

    tree_new = ttk.Treeview(right_frame, columns=("val",), show="headings")
    tree_new.heading("val", text="重命名后(相对路径)")
    tree_new.column("val", width=460, minwidth=200, stretch=False)
    v_scroll_new = ttk.Scrollbar(right_frame, orient=tk.VERTICAL, command=tree_new.yview)
    h_scroll_new = ttk.Scrollbar(right_frame, orient=tk.HORIZONTAL, command=tree_new.xview)
    tree_new.config(yscrollcommand=v_scroll_new.set, xscrollcommand=h_scroll_new.set)
    tree_new.grid(row=0, column=0, sticky="nsew")
    v_scroll_new.grid(row=0, column=1, sticky="ns")
    h_scroll_new.grid(row=1, column=0, sticky="ew")

    def sync_y_old(*args):
        tree_new.yview_moveto(args[0])

    def sync_y_new(*args):
        tree_old.yview_moveto(args[0])

    tree_old.configure(yscrollcommand=lambda *a: (v_scroll_old.set(*a), sync_y_old(*a)))
    tree_new.configure(yscrollcommand=lambda *a: (v_scroll_new.set(*a), sync_y_new(*a)))

    frame_long = ui.LabelFrame(parent, text="因重命名后路径超过255字符被跳过的文件", padding=4)
    frame_long.pack(fill=tk.X, padx=6, pady=4)
    frame_long.grid_columnconfigure(0, weight=1)
    frame_long.grid_rowconfigure(0, weight=1)

    long_text = ui.Textbox(frame_long, height=5, state=tk.DISABLED)
    long_text.grid(row=0, column=0, columnspan=2, sticky="nsew")

    preview_list = []
    long_path_items = []

    def clear_preview():
        for item in tree_old.get_children():
            tree_old.delete(item)
        for item in tree_new.get_children():
            tree_new.delete(item)
        preview_list.clear()
        long_path_items.clear()
        long_text.config(state=tk.NORMAL)
        long_text.delete(1.0, tk.END)
        long_text.config(state=tk.DISABLED)

    def preview_rename():
        clear_preview()
        root_dir = clean_path(var_path.get())
        if not root_dir or not os.path.isdir(root_dir):
            messagebox.showerror("错误", "请选择有效的文件夹！")
            return
        preview, long_items = plan_rename(root_dir, var_rec.get(), var_root.get())
        long_path_items.extend(long_items)
        for old_full, new_full in preview:
            preview_list.append((old_full, new_full))
            old_rel = ".\\" + os.path.relpath(old_full, root_dir).replace("/", "\\")
            new_rel = ".\\" + os.path.relpath(new_full, root_dir).replace("/", "\\")
            tree_old.insert("", tk.END, values=(old_rel,))
            tree_new.insert("", tk.END, values=(new_rel,))
        long_text.config(state=tk.NORMAL)
        long_text.insert(tk.END, "\n".join(long_path_items) if long_path_items else "无超长路径文件")
        long_text.config(state=tk.DISABLED)
        msg = f"预览完成，待重命名文件：{len(preview_list)} 个"
        if long_path_items:
            msg += f"\n跳过 {len(long_path_items)} 个文件：重命名后路径超过255字符，Windows无法处理！"
        messagebox.showinfo("预览结果", msg)

    def do_rename():
        if not preview_list:
            messagebox.showwarning("提示", "请先点击【预览重命名结果】！")
            return
        ans = messagebox.askyesno("确认", "确认执行重命名？操作前务必备份文件！\n一旦重命名无法自动恢复！")
        if not ans:
            return
        errors = []
        success, fail = apply_rename(preview_list, errors.append)
        msg = f"成功:{success} 个，失败:{fail} 个"
        if errors:
            msg += "\n\n" + "\n".join(errors[:20])
        messagebox.showinfo("执行完成", msg)
        clear_preview()

    ui.Button(frame_btn, text="预览重命名结果", command=preview_rename).pack(side=tk.LEFT, padx=8)
    ui.Button(frame_btn, text="执行重命名", command=do_rename).pack(side=tk.LEFT, padx=8)
    ui.Button(frame_btn, text="清空预览", command=clear_preview).pack(side=tk.LEFT, padx=8)


def trim_filename(name, start, count, from_end):
    """按位置删除文件名中的字符。
    from_end=False：从第 start 位（1 起）开始删 count 个字符；
    from_end=True ：从末尾数第 start 位开始删 count 个字符。
    删后为空或区间无效时返回原名。"""
    n = len(name)
    start = max(1, int(start))
    count = max(0, int(count))
    if count == 0 or start > n:
        return name
    if from_end:
        end_pos = n - start + 1          # 删除区间右端（不含）
        begin = max(0, end_pos - count)
        end_pos = min(n, end_pos)
    else:
        begin = min(n, start - 1)
        end_pos = min(n, begin + count)
    if begin >= end_pos:
        return name
    return name[:begin] + name[end_pos:]


def build_trim_plan(items, start, count, from_end):
    """生成文件名删除计划 [(old, new)] 及删后为空的跳过列表；
    文件只删除主文件名部分，扩展名（.jpg 等）保持不变；文件夹按整个名称处理。
    新旧名相同的条目不放入计划。"""
    plan = []
    empty_names = []
    for old in items:
        name = os.path.basename(old)
        if os.path.isdir(old):
            new_name = trim_filename(name, start, count, from_end)
            if not new_name:
                empty_names.append(old)
                continue
        else:
            stem, ext = os.path.splitext(name)
            new_stem = trim_filename(stem, start, count, from_end)
            if not new_stem:
                empty_names.append(old)
                continue
            new_name = new_stem + ext
        new = os.path.join(os.path.dirname(old), new_name)
        if os.path.abspath(new) != os.path.abspath(old):
            plan.append((old, new))
    return plan, empty_names


def build_seq_rename_tab(parent, app=None):
    """⑥ 序号重命名：拖放/按钮添加文件或文件夹，遍历读取，自然排序，预览后执行"""
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(7, weight=1)
    make_tip(parent, 0, "把文件或文件夹拖到下方列表（也可点按钮添加）。按数值化自然排序（9 在 10 前面，"
                        "不会出现 10 排到 9 前面），支持【全部重命名为序号 001、002…】和"
                        "【原文件名前加序号 001-原文件名】；勾选【仅处理文件夹】则忽略文件，"
                        "只把拖入文件夹内的子文件夹按序号重命名。",
             colspan=4, autowrap=True)

    file_list = []      # 收集到的文件完整路径（自然排序）
    preview_plan = []   # [(old, new)]

    # ---- 第一行：添加按钮 ----
    frame_add = ui.Frame(parent)
    frame_add.grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=4)

    def on_add_files():
        paths = filedialog.askopenfilenames(title="选择文件（可多选）")
        if paths:
            add_paths(list(paths))

    def on_add_folder():
        p = filedialog.askdirectory(title="选择文件夹（会遍历读取其中文件）")
        if p:
            add_paths([p])

    btn_add_files = ui.Button(frame_add, text="添加文件", command=on_add_files)
    btn_add_files.pack(side=tk.LEFT, padx=4)
    ui.Button(frame_add, text="添加文件夹", command=on_add_folder).pack(side=tk.LEFT, padx=4)
    ui.Button(frame_add, text="清空列表", command=lambda: (file_list.clear(), clear_preview(), refresh_list())
               ).pack(side=tk.LEFT, padx=4)

    # ---- 文件列表框 ----
    list_frame = ui.Frame(parent)
    list_frame.grid(row=2, column=0, columnspan=3, sticky="nsew", padx=6, pady=4)
    list_frame.grid_rowconfigure(0, weight=1)
    list_frame.grid_columnconfigure(0, weight=1)
    listbox = ui.Listbox(list_frame, selectmode=tk.EXTENDED, height=6)
    lb_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=listbox.yview)
    listbox.config(yscrollcommand=lb_scroll.set)
    listbox.grid(row=0, column=0, sticky="nsew")
    lb_scroll.grid(row=0, column=1, sticky="ns")

    info_row = ui.Frame(parent)
    info_row.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 0))
    label_count = ui.Label(info_row, text="共 0 个文件")
    label_count.pack(side=tk.LEFT)
    var_rec = tk.BooleanVar(value=True)
    chk_rec = ttk.Checkbutton(info_row, text="添加文件夹时遍历子目录", variable=var_rec)
    chk_rec.pack(side=tk.LEFT, padx=(24, 0))

    # ---- 模式与序号参数 ----
    mode_row = ui.Frame(parent)
    mode_row.grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=2)
    var_mode = tk.StringVar(value="all")
    ttk.Radiobutton(mode_row, text="全部重命名为序号", variable=var_mode,
                    value="all").pack(side=tk.LEFT, padx=(0, 18))
    ttk.Radiobutton(mode_row, text="添加序号到前面", variable=var_mode,
                    value="prefix").pack(side=tk.LEFT, padx=(0, 18))
    var_dirs_only = tk.BooleanVar(value=False)
    ttk.Checkbutton(mode_row, text="仅处理文件夹（忽略文件，只重命名子文件夹）",
                    variable=var_dirs_only, command=lambda: sync_dirs_mode()
                    ).pack(side=tk.LEFT, padx=(12, 0))

    # 位数/起始序号放入独立子框架，避免父容器列宽被其他行的宽控件撑开导致错位
    frame_params = ui.Frame(parent)
    frame_params.grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=5)
    ui.Label(frame_params, text="序号位数：").grid(row=0, column=0, sticky="w", padx=8)
    box_w, var_width = make_num_box(frame_params, 3, 1, 10)
    box_w.grid(row=0, column=1, sticky="w")
    ui.Label(frame_params, text="起始序号：").grid(row=0, column=2, sticky="w", padx=(24, 0))
    box_s, var_start = make_num_box(frame_params, 1, 1, 99999999, width=10)
    box_s.grid(row=0, column=3, sticky="w")
    ui.Label(frame_params, text="（位数 1-10；起始序号默认 1，可改成任意正整数）",
              foreground=COLOR["text_secondary"]).grid(row=0, column=4, sticky="w", padx=8)

    # ---- 预览 / 执行 ----
    frame_run = ui.Frame(parent)
    frame_run.grid(row=6, column=0, columnspan=3, sticky="w", padx=6, pady=4)

    # ---- 预览表格 ----
    table_frame = ui.Frame(parent)
    table_frame.grid(row=7, column=0, columnspan=3, sticky="nsew", padx=6, pady=4)
    table_frame.grid_rowconfigure(0, weight=1)
    table_frame.grid_columnconfigure(0, weight=1)

    tree = ttk.Treeview(table_frame, columns=("old", "new"), show="headings", height=6)
    tree.heading("old", text="原文件（完整路径）")
    tree.heading("new", text="重命名后（新文件名）")
    tree.column("old", width=640, minwidth=200, stretch=False)
    tree.column("new", width=260, minwidth=120, stretch=False)
    v_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
    h_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=tree.xview)
    tree.config(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    v_scroll.grid(row=0, column=1, sticky="ns")
    h_scroll.grid(row=1, column=0, sticky="ew")

    label_preview = ui.Label(parent, text="", foreground=COLOR["text_secondary"])
    label_preview.grid(row=8, column=0, columnspan=3, sticky="w", padx=8)

    text_log, log = make_log_panel(parent, 9, colspan=4, title="执行日志", height=5)

    # ---- 功能函数 ----
    def unit_name():
        return "文件夹" if var_dirs_only.get() else "文件"

    def sync_dirs_mode():
        """仅文件夹模式：禁用添加文件按钮与遍历子目录选项（只收子文件夹）"""
        dirs_only = var_dirs_only.get()
        btn_add_files.config(state="disabled" if dirs_only else "normal")
        chk_rec.config(state="disabled" if dirs_only else "normal")
        # 模式切换后旧列表失效，清空避免混淆
        file_list.clear()
        clear_preview()
        refresh_list()

    def refresh_list():
        listbox.delete(0, tk.END)
        for f in file_list:
            listbox.insert(tk.END, f)
        label_count.config(text=f"共 {len(file_list)} 个{unit_name()}")

    def clear_preview():
        for item in tree.get_children():
            tree.delete(item)
        preview_plan.clear()
        label_preview.config(text="")

    def add_paths(paths):
        if var_dirs_only.get():
            new_items = collect_dirs(paths)
        else:
            new_items = collect_files(paths, var_rec.get())
        existing = set(file_list)
        for f in new_items:
            if f not in existing:
                existing.add(f)
                file_list.append(f)
        file_list.sort(key=natural_key)
        refresh_list()
        clear_preview()
        if not new_items and paths:
            messagebox.showinfo("提示",
                                f"未找到可处理的{unit_name()}。\n"
                                f"仅文件夹模式下，请拖入/添加文件夹（收集其中的子文件夹）。")

    def do_preview():
        clear_preview()
        if not file_list:
            messagebox.showwarning("提示", f"请先添加{unit_name()}或文件夹！")
            return
        try:
            width = int(var_width.get().strip())
            start = int(var_start.get().strip())
        except ValueError:
            messagebox.showerror("错误", "序号位数与起始序号必须是正整数！")
            return
        if not 1 <= width <= 10:
            messagebox.showerror("错误", "序号位数需在 1-10 之间！")
            return
        if start < 1:
            messagebox.showerror("错误", "起始序号需大于等于 1！")
            return
        plan = build_seq_plan(file_list, var_mode.get(), width, start)
        preview_plan.extend(plan)
        for old, new in plan:
            tree.insert("", tk.END, values=(old, os.path.basename(new)))
        label_preview.config(text=f"预览完成：共 {len(file_list)} 个{unit_name()}，其中 {len(plan)} 个将重命名"
                                  f"（新旧名相同的自动跳过）")

    def do_execute():
        if not preview_plan:
            messagebox.showwarning("提示", "请先点击【预览重命名结果】！")
            return
        ans = messagebox.askyesno(
            "确认", f"确认重命名 {len(preview_plan)} 个{unit_name()}？\n操作前务必备份！一旦重命名无法自动恢复！")
        if not ans:
            return
        clear_log(text_log)
        success, fail = execute_seq_plan(preview_plan, log)
        if fail == 0:
            # 全部改名成功：列表里剩下的都是改完的路径，留着没意义，直接清空
            file_list.clear()
            clear_preview()
            refresh_list()
            log(f"全部成功（{success} 个），列表已自动清空")
            messagebox.showinfo("执行完成", f"成功 {success} 个，无失败项，列表已自动清空")
        else:
            # 有失败/跳过：保留列表（失败项保持原名），方便修正后重试
            renamed = dict(preview_plan)
            refreshed = []
            for f in file_list:
                nf = renamed.get(f, f)
                refreshed.append(nf if os.path.exists(nf) else f)
            file_list[:] = refreshed
            file_list.sort(key=natural_key)
            refresh_list()
            clear_preview()
            messagebox.showinfo("执行完成",
                                f"成功 {success} 个，失败/跳过 {fail} 个\n"
                                f"（失败项保留在列表里，可修正后重试）")

    ui.Button(frame_run, text="预览重命名结果", command=do_preview).pack(side=tk.LEFT, padx=8)
    ui.Button(frame_run, text="执行重命名", command=do_execute).pack(side=tk.LEFT, padx=8)

    # 注册拖放：拖入文件/文件夹直接加入列表（修复⑥序号重命名无法拖入的问题）
    if app is not None:
        app.register_drop_handler(parent, add_paths)



def build_trim_tab(parent, app=None):
    """⑨ 文件名删除：按位置删除文件名中的字符（可选文件/文件夹，从前/从后）"""
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(7, weight=1)
    make_tip(parent, 0, "把文件或文件夹拖到下方列表（也可点按钮添加），按位置删除文件名中的字符："
                        "可设置从第几位开始、删除几个字、从前还是从后。"
                        "例如从第 1 位开始删 2 个字：'01_照片.jpg' → '照片.jpg'。",
             colspan=4, autowrap=True)

    file_list = []      # 收集到的文件/文件夹完整路径（自然排序）
    preview_plan = []   # [(old, new)]

    # ---- 第一行：添加按钮 ----
    frame_add = ui.Frame(parent)
    frame_add.grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=4)

    def on_add_files():
        paths = filedialog.askopenfilenames(title="选择文件（可多选）")
        if paths:
            add_paths(list(paths))

    def on_add_folder():
        p = filedialog.askdirectory(title="选择文件夹（仅文件夹模式下取其子文件夹）")
        if p:
            add_paths([p])

    btn_add_files = ui.Button(frame_add, text="添加文件", command=on_add_files)
    btn_add_files.pack(side=tk.LEFT, padx=4)
    ui.Button(frame_add, text="添加文件夹", command=on_add_folder).pack(side=tk.LEFT, padx=4)
    ui.Button(frame_add, text="清空列表", command=lambda: (file_list.clear(), clear_preview(), refresh_list())
               ).pack(side=tk.LEFT, padx=4)

    # ---- 列表框 ----
    list_frame = ui.Frame(parent)
    list_frame.grid(row=2, column=0, columnspan=3, sticky="nsew", padx=6, pady=4)
    list_frame.grid_rowconfigure(0, weight=1)
    list_frame.grid_columnconfigure(0, weight=1)
    listbox = ui.Listbox(list_frame, selectmode=tk.EXTENDED, height=6)
    lb_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=listbox.yview)
    listbox.config(yscrollcommand=lb_scroll.set)
    listbox.grid(row=0, column=0, sticky="nsew")
    lb_scroll.grid(row=0, column=1, sticky="ns")

    info_row = ui.Frame(parent)
    info_row.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 0))
    label_count = ui.Label(info_row, text="共 0 个文件")
    label_count.pack(side=tk.LEFT)
    var_rec = tk.BooleanVar(value=True)
    chk_rec = ttk.Checkbutton(info_row, text="添加文件夹时遍历子目录", variable=var_rec)
    chk_rec.pack(side=tk.LEFT, padx=(24, 0))

    # ---- 处理对象与删除参数 ----
    var_dirs_only = tk.BooleanVar(value=False)
    ttk.Checkbutton(parent, text="仅处理文件夹（忽略文件，只重命名子文件夹）",
                    variable=var_dirs_only, command=lambda: sync_dirs_mode()
                    ).grid(row=4, column=0, sticky="w", padx=8)

    # 参数行放独立子框架，避免列宽互相干扰
    frame_params = ui.Frame(parent)
    frame_params.grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=5)
    ui.Label(frame_params, text="从第几位开始：").grid(row=0, column=0, sticky="w", padx=8)
    box_start, var_start = make_num_box(frame_params, 1, 1, 999)
    box_start.grid(row=0, column=1, sticky="w")
    ui.Label(frame_params, text="删除几个字：").grid(row=0, column=2, sticky="w", padx=(16, 0))
    box_count, var_count = make_num_box(frame_params, 2, 0, 999)
    box_count.grid(row=0, column=3, sticky="w")
    var_from_end = tk.BooleanVar(value=False)
    ttk.Radiobutton(frame_params, text="从前", variable=var_from_end, value=False
                    ).grid(row=0, column=4, sticky="w", padx=(20, 0))
    ttk.Radiobutton(frame_params, text="从后", variable=var_from_end, value=True
                    ).grid(row=0, column=5, sticky="w", padx=(12, 0))
    ui.Label(frame_params, text="位数从 1 开始；文件只删主名、不动后缀；删后为空自动跳过",
              foreground=COLOR["text_secondary"]).grid(
                  row=1, column=0, columnspan=7, sticky="w", padx=8, pady=(4, 0))

    # ---- 预览 / 执行 ----
    frame_run = ui.Frame(parent)
    frame_run.grid(row=6, column=0, columnspan=3, sticky="w", padx=6, pady=4)

    table_frame = ui.Frame(parent)
    table_frame.grid(row=7, column=0, columnspan=3, sticky="nsew", padx=6, pady=4)
    table_frame.grid_rowconfigure(0, weight=1)
    table_frame.grid_columnconfigure(0, weight=1)
    tree = ttk.Treeview(table_frame, columns=("old", "new"), show="headings", height=6)
    tree.heading("old", text="原名（完整路径）")
    tree.heading("new", text="删除后（新文件名）")
    tree.column("old", width=640, minwidth=200, stretch=False)
    tree.column("new", width=260, minwidth=120, stretch=False)
    v_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
    h_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=tree.xview)
    tree.config(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    v_scroll.grid(row=0, column=1, sticky="ns")
    h_scroll.grid(row=1, column=0, sticky="ew")

    label_preview = ui.Label(parent, text="", foreground=COLOR["text_secondary"])
    label_preview.grid(row=8, column=0, columnspan=3, sticky="w", padx=8)

    text_log, log = make_log_panel(parent, 9, colspan=4, title="执行日志", height=5)

    # ---- 功能函数 ----
    def unit_name():
        return "文件夹" if var_dirs_only.get() else "文件"

    def sync_dirs_mode():
        dirs_only = var_dirs_only.get()
        btn_add_files.config(state="disabled" if dirs_only else "normal")
        chk_rec.config(state="disabled" if dirs_only else "normal")
        file_list.clear()
        clear_preview()
        refresh_list()

    def refresh_list():
        listbox.delete(0, tk.END)
        for f in file_list:
            listbox.insert(tk.END, f)
        label_count.config(text=f"共 {len(file_list)} 个{unit_name()}")

    def clear_preview():
        for item in tree.get_children():
            tree.delete(item)
        preview_plan.clear()
        label_preview.config(text="")

    def add_paths(paths):
        if var_dirs_only.get():
            new_items = collect_dirs(paths)
        else:
            new_items = collect_files(paths, var_rec.get())
        existing = set(file_list)
        for f in new_items:
            if f not in existing:
                existing.add(f)
                file_list.append(f)
        file_list.sort(key=natural_key)
        refresh_list()
        clear_preview()
        if not new_items and paths:
            messagebox.showinfo("提示",
                                f"未找到可处理的{unit_name()}。\n"
                                f"仅文件夹模式下，请拖入/添加文件夹（收集其中的子文件夹）。")

    def do_preview():
        clear_preview()
        if not file_list:
            messagebox.showwarning("提示", f"请先添加{unit_name()}或文件夹！")
            return
        try:
            start = int(var_start.get().strip())
            count = int(var_count.get().strip())
        except ValueError:
            messagebox.showerror("错误", "起始位数与删除字数必须是正整数！")
            return
        if start < 1:
            messagebox.showerror("错误", "起始位数需大于等于 1！")
            return
        if count < 0:
            messagebox.showerror("错误", "删除字数不能为负！")
            return
        plan, empty_names = build_trim_plan(file_list, start, count, var_from_end.get())
        preview_plan.extend(plan)
        for old, new in plan:
            tree.insert("", tk.END, values=(old, os.path.basename(new)))
        msg = f"预览完成：共 {len(file_list)} 个{unit_name()}，其中 {len(plan)} 个将重命名"
        if empty_names:
            msg += f"，{len(empty_names)} 个删后文件名为空已跳过"
        label_preview.config(text=msg)

    def do_execute():
        if not preview_plan:
            messagebox.showwarning("提示", "请先点击【预览重命名结果】！")
            return
        ans = messagebox.askyesno(
            "确认", f"确认重命名 {len(preview_plan)} 个{unit_name()}？\n操作前务必备份！一旦重命名无法自动恢复！")
        if not ans:
            return
        clear_log(text_log)
        success, fail = execute_seq_plan(preview_plan, log)
        renamed = dict(preview_plan)
        refreshed = []
        for f in file_list:
            nf = renamed.get(f, f)
            refreshed.append(nf if os.path.exists(nf) else f)
        file_list[:] = refreshed
        file_list.sort(key=natural_key)
        refresh_list()
        clear_preview()
        messagebox.showinfo("执行完成", f"成功 {success} 个，失败/跳过 {fail} 个")

    ui.Button(frame_run, text="预览重命名结果", command=do_preview).pack(side=tk.LEFT, padx=8)
    ui.Button(frame_run, text="执行重命名", command=do_execute).pack(side=tk.LEFT, padx=8)

    # 注册拖放：拖入文件/文件夹直接加入列表
    if app is not None:
        app.register_drop_handler(parent, add_paths)
