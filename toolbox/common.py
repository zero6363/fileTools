# -*- coding: utf-8 -*-
"""通用基础设施：日志 / 后台任务 / 常用输入行 / Windows 文件拖放"""
import json
import os
import queue
import re
import shutil
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

APP_TITLE = "文件工具箱 · 多功能合集"
DEFAULT_IMAGE_FORMATS = "jpg jpeg png gif bmp tiff webp"


# ============================================================
# Windows 文件拖放支持（自实现，替代第三方 windnd）
# 修复两个崩溃点：
#   1. windnd 用 GetWindowLongPtrA 不设 restype，64 位窗口过程指针被截断成
#      负数，之后 CallWindowProcW 访问冲突直接闪退；
#   2. 在窗口过程回调里直接操作 Tk 会触发 GIL 致命错误
#      （PyEval_RestoreThread: thread state is NULL）。
# 本实现：SetWindowSubclass 安全挂接；回调里只做纯 Python 入队；
#         Tk 操作全部放回正常事件循环里轮询执行。
# ============================================================

_DROP_QUEUE = None
_DROP_KEEPALIVE = []  # 防止回调对象被垃圾回收


def install_drop_support(tk_window, handler):
    """给窗口注册文件拖放。handler(paths) 只会在正常事件循环中被调用。
    返回 True/False；非 Windows 或任何失败都安全返回 False。"""
    global _DROP_QUEUE
    try:
        import ctypes
        from ctypes import wintypes

        if _DROP_QUEUE is None:
            _DROP_QUEUE = queue.Queue()

        user32 = ctypes.windll.user32
        comctl32 = ctypes.windll.comctl32
        shell32 = ctypes.windll.shell32

        WM_DROPFILES = 0x0233

        DragQueryFileW = shell32.DragQueryFileW
        DragQueryFileW.restype = wintypes.UINT
        DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                   wintypes.LPWSTR, wintypes.UINT]
        DragAcceptFiles = shell32.DragAcceptFiles
        DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        DragFinish = shell32.DragFinish
        DragFinish.argtypes = [wintypes.HANDLE]

        SetWindowSubclass = comctl32.SetWindowSubclass
        SetWindowSubclass.argtypes = [wintypes.HWND, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.c_size_t]
        SetWindowSubclass.restype = wintypes.BOOL
        DefSubclassProc = comctl32.DefSubclassProc
        DefSubclassProc.restype = ctypes.c_ssize_t
        DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT,
                                    ctypes.c_size_t, ctypes.c_ssize_t]

        SUBCLASSPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t,
            ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_size_t)

        def subclass_proc(h, msg, wp, lp, uid, refdata):
            if msg == WM_DROPFILES:
                try:
                    count = DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                    paths = []
                    for i in range(count):
                        need = DragQueryFileW(wp, i, None, 0)
                        if not need:
                            continue
                        buf = ctypes.create_unicode_buffer(need + 1)
                        DragQueryFileW(wp, i, buf, need + 1)
                        paths.append(buf.value)
                    DragFinish(wp)
                    # 只做纯 Python 入队：绝不在窗口过程里操作 Tk
                    _DROP_QUEUE.put(paths)
                except Exception:
                    pass
                return 0
            return DefSubclassProc(h, msg, wp, lp)

        hwnd = tk_window.winfo_id()
        proc_obj = SUBCLASSPROC(subclass_proc)
        _DROP_KEEPALIVE.append(proc_obj)
        if not SetWindowSubclass(hwnd, proc_obj, 1, 0):
            return False
        DragAcceptFiles(hwnd, True)

        def poll():
            try:
                while True:
                    paths = _DROP_QUEUE.get_nowait()
                    try:
                        handler(paths)
                    except Exception:
                        pass
            except queue.Empty:
                pass
            tk_window.after(50, poll)

        tk_window.after(50, poll)
        return True
    except Exception:
        return False




# ============================================================
# 通用基础设施：日志 / 后台任务 / 常用输入行
# ============================================================

def make_logger(text_widget):
    """生成线程安全的日志函数（自动切回主线程刷新文本框）"""

    def log(msg):
        def _append():
            try:
                text_widget.configure(state="normal")
                text_widget.insert(tk.END, str(msg) + "\n")
                text_widget.see(tk.END)
                text_widget.configure(state="disabled")
            except Exception:
                pass

        try:
            text_widget.after(0, _append)
        except Exception:
            pass

    return log


def run_task(btn, log, work, on_done=None):
    """在后台线程执行 work()；异常自动记入日志；完成后切回主线程恢复按钮。"""

    def worker():
        err = None
        try:
            work()
        except Exception:
            err = traceback.format_exc()

        def finish():
            try:
                btn.config(state=tk.NORMAL)
            except Exception:
                pass
            if err:
                log("运行出错：\n" + err)
            if on_done:
                try:
                    on_done(err)
                except Exception:
                    log("完成回调出错：\n" + traceback.format_exc())

        try:
            btn.winfo_toplevel().after(0, finish)
        except Exception:
            pass

    btn.config(state=tk.DISABLED)
    threading.Thread(target=worker, daemon=True).start()


def clear_log(text_widget):
    text_widget.configure(state="normal")
    text_widget.delete("1.0", tk.END)
    text_widget.configure(state="disabled")


_NAT_RE = re.compile(r"(\d+)")


def natural_key(s):
    """数值化自然排序键：让 9 排在 10 前面（Windows 字符串排序会把 10 排到 9 前面）"""
    parts = _NAT_RE.split(str(s).lower())
    key = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # 数字段按整数比较
            key.append((1, int(part), ""))
        else:
            key.append((0, 0, part))
    return key


def clean_path(p):
    """去掉路径两端空白，以及粘贴时可能带入的引号"""
    if not p:
        return ""
    return p.strip().strip('"').strip("'").strip()


def make_path_row(parent, row, label, default="", title="选择文件夹", entry_width=52, note=None):
    """目录输入行：标签 + 可编辑文本框 + 浏览按钮"""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=5)
    var = tk.StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=entry_width).grid(row=row, column=1, sticky="w", padx=6)

    def _browse():
        p = filedialog.askdirectory(title=title)
        if p:
            var.set(p)

    ttk.Button(parent, text="浏览...", command=_browse).grid(row=row, column=2, sticky="w")
    if note:
        ttk.Label(parent, text=note, foreground="#666666").grid(row=row, column=3, sticky="w", padx=4)
    return var


def make_file_row(parent, row, label, default="", title="选择文件", entry_width=52, note=None):
    """文件输入行（用于字体等文件选择）"""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=5)
    var = tk.StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=entry_width).grid(row=row, column=1, sticky="w", padx=6)

    def _browse():
        p = filedialog.askopenfilename(
            title=title,
            filetypes=[("字体文件", "*.ttf *.ttc *.otf"), ("所有文件", "*.*")],
        )
        if p:
            var.set(p)

    ttk.Button(parent, text="浏览...", command=_browse).grid(row=row, column=2, sticky="w")
    if note:
        ttk.Label(parent, text=note, foreground="#666666").grid(row=row, column=3, sticky="w", padx=4)
    return var


def make_entry_row(parent, row, label, default="", entry_width=30, note=None):
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=5)
    var = tk.StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=entry_width).grid(row=row, column=1, sticky="w", padx=6)
    if note:
        ttk.Label(parent, text=note, foreground="#666666").grid(row=row, column=2, sticky="w", padx=4)
    return var


def make_num_box(parent, default, min_, max_, width=6, step=1):
    """[－] Entry [＋] 数字小组件（方便鼠标点加减），返回 (frame, StringVar)。
    点击按钮时按 min_/max_ 钳制、按 step 步进；输入非法文本时点按钮回退到 min_。"""
    frame = ttk.Frame(parent)
    var = tk.StringVar(value=str(default))
    step = max(1, int(step))

    def do_step(delta):
        try:
            v = int(var.get().strip())
        except ValueError:
            v = min_
        v = max(min_, min(max_, v + delta))
        var.set(str(v))

    ttk.Button(frame, text="－", width=2, command=lambda: do_step(-step)).pack(side=tk.LEFT)
    ttk.Entry(frame, textvariable=var, width=width).pack(side=tk.LEFT, padx=2)
    ttk.Button(frame, text="＋", width=2, command=lambda: do_step(step)).pack(side=tk.LEFT)
    return frame, var


def make_num_row(parent, row, label, default, min_, max_, note=None, width=6, step=1):
    """带标签/说明的整行数字输入（[－] Entry [＋]，方便鼠标操作），返回 StringVar"""
    frame = ttk.Frame(parent)
    frame.grid(row=row, column=0, columnspan=4, sticky="w", padx=2, pady=3)
    ttk.Label(frame, text=label).pack(side=tk.LEFT, padx=6)
    box, var = make_num_box(frame, default, min_, max_, width, step)
    box.pack(side=tk.LEFT)
    if note:
        ttk.Label(frame, text=note, foreground="#666666").pack(side=tk.LEFT, padx=6)
    return var


def make_spin_row(parent, row, label, default, from_, to, note=None, width=10, step=1):
    # 数字输入行统一走 make_num_row（带加减按钮，鼠标操作更顺手）
    return make_num_row(parent, row, label, default, from_, to, note, width, step)


def make_check(parent, text, default=False, row=None, column=None, var=None):
    if var is None:
        var = tk.BooleanVar(value=default)
    cb = ttk.Checkbutton(parent, text=text, variable=var)
    if row is not None:
        cb.grid(row=row, column=column, sticky="w", padx=6, pady=3)
    else:
        cb.pack(anchor="w", padx=6, pady=2)
    return var


def make_start_button(parent, row, text, command, colspan=3):
    btn = tk.Button(
        parent, text=text, command=command,
        bg="#2f6bff", fg="white", activebackground="#2455cc", activeforeground="white",
        font=("Microsoft YaHei", 10, "bold"), height=1, padx=16, pady=4, cursor="hand2",
    )
    btn.grid(row=row, column=0, columnspan=colspan, pady=12)
    return btn


def make_log_panel(parent, row, colspan=3, title="运行日志", height=12):
    frame = ttk.LabelFrame(parent, text=title, padding=4)
    frame.grid(row=row, column=0, columnspan=colspan, sticky="nsew", padx=8, pady=6)
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(0, weight=1)
    text = scrolledtext.ScrolledText(frame, height=height, state="disabled", wrap=tk.WORD,
                                     font=("Consolas", 9))
    text.grid(row=0, column=0, sticky="nsew")
    return text, make_logger(text)


def make_tip(parent, row, text, colspan=3):
    ttk.Label(parent, text=text, foreground="#666666", wraplength=1120).grid(
        row=row, column=0, columnspan=colspan, sticky="w", padx=8, pady=(0, 6))


def _module_available(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _is_relative_to(p, base):
    """Path.is_relative_to 的兼容实现"""
    try:
        return p.is_relative_to(base)
    except AttributeError:
        try:
            p.relative_to(base)
            return True
        except ValueError:
            return False


def get_unique_path(target_path):
    """目标路径已存在时生成 _1、_2 后缀的唯一路径（防止覆盖）"""
    if not target_path.exists():
        return target_path
    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent
    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def load_window_config(path):
    """读取窗口配置文件（不存在/损坏时返回 {}）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_window_geometry(path, root, extra=None):
    """保存主窗口几何（位置+大小）与最大化状态，供下次启动恢复。
    extra 可附加额外字段（如左侧菜单宽度 nav_width）。"""
    try:
        data = {"geometry": root.geometry()}
        try:
            if root.wm_state() == "zoomed":
                data["zoomed"] = True
        except Exception:
            pass
        if extra:
            data.update(extra)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_MB_ORIGINALS = {}


def _patch_messagebox_parent():
    """让 messagebox 系列弹窗默认居中于主窗口（而非屏幕中心）。
    已显式传入 parent 的调用不受影响。幂等，可重复调用。"""
    try:
        import tkinter.messagebox as _mb
    except Exception:
        return
    if getattr(_mb, "_toolbox_patched", False):
        return
    _mb._toolbox_patched = True

    def _default_root():
        try:
            r = tk._default_root
            if r is not None and r.winfo_exists():
                return r
        except Exception:
            pass
        return None

    def _wrap(name):
        _MB_ORIGINALS[name] = getattr(_mb, name)

        def wrapper(*args, **kwargs):
            if "parent" not in kwargs:
                p = _default_root()
                if p is not None:
                    kwargs["parent"] = p
            return _MB_ORIGINALS[name](*args, **kwargs)
        setattr(_mb, name, wrapper)

    for _n in ("showinfo", "showwarning", "showerror", "askquestion",
               "askokcancel", "askyesno", "askyesnocancel", "askretrycancel"):
        try:
            _wrap(_n)
        except Exception:
            pass


def restore_window_geometry(path, root, default="1280x900", min_w=720, min_h=420):
    """恢复上次窗口几何；配置缺失/越界/超出屏幕时返回 False（调用方用默认）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    geo = data.get("geometry", "")
    m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(geo))
    if not m:
        return False
    w, h, x, y = (int(g) for g in m.groups())
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    if not (min_w <= w <= sw * 2 and min_h <= h <= sh * 2):
        return False
    # 位置基本在虚拟屏幕内（允许部分溢出，但标题栏要够得着）
    if x < -w + 120 or y < 0 or x > sw - 120 or y > sh - 80:
        return False
    root.geometry(f"{w}x{h}+{x}+{y}")
    if data.get("zoomed"):
        def _do_zoom():
            try:
                if root.winfo_exists():
                    root.state("zoomed")
            except Exception:
                pass
        root.after(120, _do_zoom)
    return True


# 模块导入即生效：让所有 messagebox 弹窗跟随主窗口居中
_patch_messagebox_parent()


# ============================================================


def _walk_widgets(w):
    yield w
    for c in w.winfo_children():
        yield from _walk_widgets(c)
