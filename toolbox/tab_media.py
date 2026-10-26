# -*- coding: utf-8 -*-
"""⑭ WebP转JPG / ⑮ 加密包分类"""
import os
import shutil
import zipfile
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from . import ui, ui as ttk

from .common import (_is_relative_to, _module_available, clean_path,
                     clear_log, get_unique_path, make_check, make_entry_row,
                     make_form, make_log_panel, make_path_row, make_spin_row,
                     make_start_button, make_tip, run_task)


# ⑭ WebP转JPG —— 原 webp2jpg.py
# ============================================================

def webp_to_jpg(src_dir, out_dir, quality, delete_src, log):
    if not _module_available("PIL"):
        raise RuntimeError("缺少依赖 pillow，请先执行：pip install pillow")
    from PIL import Image

    if not os.path.isdir(src_dir):
        raise RuntimeError(f"源目录不存在：{src_dir}")
    out_dir = out_dir or src_dir
    os.makedirs(out_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(src_dir)
                   if f.lower().endswith(".webp") and os.path.isfile(os.path.join(src_dir, f)))
    if not files:
        log("未找到WebP文件。")
        return 0
    log(f"共 {len(files)} 个WebP文件，开始转换为JPG（质量 {quality}）...")
    ok = fail = renamed = 0
    for fname in files:
        try:
            with Image.open(os.path.join(src_dir, fname)) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")
                base_name = os.path.splitext(fname)[0]
                out_path = os.path.join(out_dir, base_name + ".jpg")
                # 防止覆盖：目标已存在则自动加 _1、_2 后缀
                if os.path.exists(out_path):
                    i = 1
                    while os.path.exists(os.path.join(out_dir, f"{base_name}_{i}.jpg")):
                        i += 1
                    out_path = os.path.join(out_dir, f"{base_name}_{i}.jpg")
                    renamed += 1
                    log(f"  已存在同名JPG，自动改名输出：{os.path.basename(out_path)}")
                im.save(out_path, "JPEG", quality=quality, subsampling=0)
            if delete_src:
                os.remove(os.path.join(src_dir, fname))
            ok += 1
            if ok % 100 == 0:
                log(f"  已转换 {ok}/{len(files)} ...")
        except Exception as e:
            fail += 1
            log(f"  转换失败 {fname}: {e}")
    log(f"转换完成：成功 {ok} 个，自动改名 {renamed} 个，失败 {fail} 个。")
    return ok


# ============================================================
# ⑮ 加密包分类 —— 原 zipTestPsw.py
# ============================================================

def _zip_encrypted(file_path):
    if _module_available("pyzipper"):
        try:
            import pyzipper
            with pyzipper.AESZipFile(str(file_path), "r") as zf:
                return any(info.flag_bits & 0x01 for info in zf.infolist())
        except Exception:
            pass
    import zipfile
    try:
        with zipfile.ZipFile(str(file_path), "r") as zf:
            return any(info.flag_bits & 0x01 for info in zf.infolist())
    except RuntimeError as e:
        if "encrypted" in str(e).lower():
            return True
        return None
    except Exception:
        return None


def _rar_encrypted(file_path):
    if not _module_available("rarfile"):
        return None
    import rarfile
    try:
        with rarfile.RarFile(str(file_path)) as rf:
            names = rf.namelist()
            if not names:
                return False
            rf.read(names[0], pwd=b"")
            return False
    except rarfile.PasswordRequired:
        return True
    except Exception:
        return None


def _7z_encrypted(file_path):
    if not _module_available("py7zr"):
        return None
    import py7zr
    try:
        with py7zr.SevenZipFile(str(file_path), "r") as sz:
            return sz.needs_password()
    except Exception:
        return None


def check_archive_encrypted(file_path):
    """True=加密 False=未加密 None=损坏/无法识别"""
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".zip":
            return _zip_encrypted(file_path)
        if suffix == ".rar":
            return _rar_encrypted(file_path)
        if suffix == ".7z":
            return _7z_encrypted(file_path)
        return None
    except Exception:
        return None


def sort_encrypted_archives(scan_dir, suffix_str, folder_name, recursive, log):
    scan_path = Path(scan_dir)
    if not scan_path.is_dir():
        raise RuntimeError(f"扫描目录不存在：{scan_dir}")
    suffixes = set()
    for s in suffix_str.split():
        suffixes.add(("." + s.lstrip(".")).lower())
    if not suffixes:
        raise RuntimeError("请至少填写一种压缩包后缀")

    has_rar = _module_available("rarfile")
    has_7z = _module_available("py7zr")
    if ".rar" in suffixes and not has_rar:
        log("提示：未安装 rarfile，.rar 文件将跳过（pip install rarfile）")
    if ".7z" in suffixes and not has_7z:
        log("提示：未安装 py7zr，.7z 文件将跳过（pip install py7zr）")

    encrypt_dir = scan_path / folder_name
    file_list = []
    if recursive:
        for f in scan_path.rglob("*"):
            if f.is_file() and f.suffix.lower() in suffixes and not _is_relative_to(f, encrypt_dir):
                file_list.append(f)
    else:
        for f in scan_path.iterdir():
            if f.is_file() and f.suffix.lower() in suffixes:
                file_list.append(f)

    log(f"共找到 {len(file_list)} 个压缩包，开始检测...")
    moved = 0
    for fp in sorted(file_list, key=lambda x: x.name):
        suffix = fp.suffix.lower()
        if suffix == ".rar" and not has_rar:
            continue
        if suffix == ".7z" and not has_7z:
            continue
        res = check_archive_encrypted(fp)
        if res is None:
            log(f"【损坏/无法识别】跳过: {fp.name}")
            continue
        if res is True:
            encrypt_dir.mkdir(exist_ok=True)
            target = get_unique_path(encrypt_dir / fp.name)
            shutil.move(str(fp), str(target))
            extra = f" -> {target.name}" if target.name != fp.name else ""
            log(f"【加密】已移动: {fp.name}{extra}")
            moved += 1
        else:
            log(f"【未加密】跳过: {fp.name}")
    log(f"处理完成！共移动 {moved} 个加密压缩包至 [{encrypt_dir}]")
    return moved


# ============================================================


def build_webp_tab(parent):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "把 WebP 图片转换为 JPG。需要 pillow 依赖。", colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))

    var_src = make_path_row(form, 0, "源目录：", title="选择源目录")
    var_out = make_path_row(form, 1, "输出目录：", title="选择输出目录", note="留空则输出到源目录")
    var_q = make_spin_row(form, 2, "JPG质量：", 100, 1, 100)
    var_del = make_check(form, "转换成功后删除原 webp", default=True, row=3, column=1,
                         columnspan=3)

    text_log, log = make_log_panel(parent, 2, colspan=1)

    def start():
        src = clean_path(var_src.get())
        out = clean_path(var_out.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的源目录！")
            return
        try:
            quality = int(var_q.get())
        except ValueError:
            messagebox.showerror("错误", "JPG质量必须是数字！")
            return
        clear_log(text_log)
        run_task(btn, log, lambda: webp_to_jpg(src, out, quality, var_del.get(), log),
                 on_done=lambda err: None if err else messagebox.showinfo("完成", "转换结束，详情见日志。"))

    btn = make_start_button(form, 4, "开始转换", start)


def build_zip_tab(parent):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "扫描压缩包，把加密的移动到指定文件夹。zip 用标准库即可识别；"
                        "rar/7z 需要 rarfile / py7zr。", colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))

    var_dir = make_path_row(form, 0, "扫描目录：", title="选择扫描目录")
    var_suffix = make_entry_row(form, 1, "压缩包后缀：", default=".zip .rar .7z",
                                entry_width=24, note="空格分隔")
    var_folder = make_entry_row(form, 2, "目标文件夹名：", default="加密",
                                entry_width=16, note="在扫描目录下创建")
    var_rec = make_check(form, "递归扫描子文件夹", default=True, row=3, column=1,
                         columnspan=3)

    text_log, log = make_log_panel(parent, 2, colspan=1)

    def start():
        scan_dir = clean_path(var_dir.get())
        suffix_str = var_suffix.get().strip()
        folder_name = clean_path(var_folder.get()) or "加密"
        if not scan_dir or not os.path.isdir(scan_dir):
            messagebox.showerror("错误", "请填写有效的扫描目录！")
            return
        if not suffix_str:
            messagebox.showerror("错误", "请填写至少一种压缩包后缀！")
            return
        clear_log(text_log)
        run_task(btn, log,
                 lambda: sort_encrypted_archives(scan_dir, suffix_str, folder_name, var_rec.get(), log),
                 on_done=lambda err: None if err else messagebox.showinfo("完成", "检测结束，详情见日志。"))

    btn = make_start_button(form, 4, "开始检测", start)


# ============================================================
