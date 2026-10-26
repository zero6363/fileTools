# -*- coding: utf-8 -*-
"""通用基础设施：日志 / 后台任务 / 常用输入行 / Windows 文件拖放"""
import queue
import re
import shutil
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from . import ui
from . import ui as ttk
from .theme import COLOR, FONTS

APP_VERSION = "1.35"
APP_TITLE = f"文件工具箱 · 多功能合集 v{APP_VERSION}"
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


def make_form(parent):
    """表单容器：所有输入行放进同一个容器，标签列宽统一，
    输入框/浏览按钮左边缘对齐，说明文字统一放在第 4 列。
    （不要把提示文字直接跨列放在外层，否则会把列宽撑开，导致各行错位。）"""
    frame = ttk.Frame(parent)
    try:
        from tkinter import font as tkfont
        ref = tkfont.nametofont("TkDefaultFont").measure("输出文件夹名：") + 18
    except Exception:
        ref = 108
    frame.grid_columnconfigure(0, minsize=ref)
    frame.grid_columnconfigure(3, weight=1)
    return frame


def form_label(parent, row, text):
    """表单标签：右对齐，使同一容器内的输入框起点一致"""
    lbl = ttk.Label(parent, text=text)
    lbl.grid(row=row, column=0, sticky="e", padx=(6, 8), pady=5)
    return lbl


def form_note(parent, row, text, column=3, columnspan=1):
    """表单说明文字：固定放第 4 列，不参与列宽计算；
    按主窗口剩余宽度自动换行，避免长说明被裁掉（CTk 下自身 Configure 会
    收敛到过窄宽度，这里改为跟随顶层窗口宽度计算可用空间）。"""
    lbl = ttk.Label(parent, text=text, foreground=COLOR["text_secondary"], justify="left")
    lbl.grid(row=row, column=column, columnspan=columnspan,
             sticky="w", padx=(8, 4), pady=5)

    def _rewrap(_event=None):
        try:
            top = lbl.winfo_toplevel()
            if not top.winfo_exists() or top.winfo_width() <= 1:
                return
            avail = top.winfo_width() - (lbl.winfo_rootx() - top.winfo_rootx()) - 24
            w = max(200, min(640, avail))
            cur = int(float(lbl.cget("wraplength") or 0))
            if abs(cur - w) > 12:
                lbl.configure(wraplength=w)
        except Exception:
            pass

    try:
        top = lbl.winfo_toplevel()
        top.bind("<Configure>", _rewrap, add="+")
        top.after(200, _rewrap)
    except Exception:
        pass
    return lbl


def make_path_row(parent, row, label, default="", title="选择文件夹", entry_width=52, note=None):
    """目录输入行：标签 + 可编辑文本框 + 浏览按钮"""
    form_label(parent, row, label)
    var = tk.StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=entry_width).grid(
        row=row, column=1, sticky="w", padx=(0, 6), pady=5)

    def _browse():
        p = filedialog.askdirectory(title=title)
        if p:
            var.set(p)

    ttk.Button(parent, text="浏览...", command=_browse).grid(
        row=row, column=2, sticky="w", padx=(0, 4), pady=5)
    if note:
        form_note(parent, row, note)
    return var


def make_file_row(parent, row, label, default="", title="选择文件", entry_width=52, note=None):
    """文件输入行（用于字体等文件选择）"""
    form_label(parent, row, label)
    var = tk.StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=entry_width).grid(
        row=row, column=1, sticky="w", padx=(0, 6), pady=5)

    def _browse():
        p = filedialog.askopenfilename(
            title=title,
            filetypes=[("字体文件", "*.ttf *.ttc *.otf"), ("所有文件", "*.*")],
        )
        if p:
            var.set(p)

    ttk.Button(parent, text="浏览...", command=_browse).grid(
        row=row, column=2, sticky="w", padx=(0, 4), pady=5)
    if note:
        form_note(parent, row, note)
    return var


def make_entry_row(parent, row, label, default="", entry_width=30, note=None):
    form_label(parent, row, label)
    var = tk.StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=entry_width).grid(
        row=row, column=1, sticky="w", padx=(0, 6), pady=5)
    if note:
        form_note(parent, row, note)
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
    form_label(parent, row, label)
    box, var = make_num_box(parent, default, min_, max_, width, step)
    box.grid(row=row, column=1, sticky="w", padx=(0, 6), pady=3)
    if note:
        form_note(parent, row, note)
    return var


def make_spin_row(parent, row, label, default, from_, to, note=None, width=10, step=1):
    # 数字输入行统一走 make_num_row（带加减按钮，鼠标操作更顺手）
    return make_num_row(parent, row, label, default, from_, to, note, width, step)


def make_check(parent, text, default=False, row=None, column=None, var=None):
    if var is None:
        var = tk.BooleanVar(value=default)
    cb = ttk.Checkbutton(parent, text=text, variable=var)
    if row is not None:
        cb.grid(row=row, column=column, sticky="w", padx=(0, 6), pady=3)
    else:
        cb.pack(anchor="w", padx=6, pady=2)
    return var


def make_start_button(parent, row, text, command, colspan=3):
    """主操作按钮：统一放在输入列，左边缘与输入框对齐。
    使用设计系统令牌：扁平蓝 + 悬停加深 + 禁用仍可读（白字）。"""
    btn = ui.Button(
        parent, text=text, command=command,
        bg=COLOR["accent"], fg=COLOR["text_on_accent"],
        activebackground=COLOR["accent_active"],
        disabledforeground=COLOR["text_on_accent"],
        padx=18, pady=6, cursor="hand2",
    )
    btn.grid(row=row, column=1, sticky="w", padx=(0, 6), pady=(12, 6))
    return btn


def make_log_panel(parent, row, colspan=3, title="运行日志", height=12):
    frame = ttk.LabelFrame(parent, text=title, padding=4)
    frame.grid(row=row, column=0, columnspan=colspan, sticky="nsew", padx=8, pady=6)
    frame.grid_rowconfigure(0, weight=1)
    frame.grid_columnconfigure(0, weight=1)
    text = ui.Textbox(frame, height=height, state="disabled",
                      font=FONTS["log"],
                      background=COLOR["surface_inset"], foreground=COLOR["text"])
    text.grid(row=0, column=0, sticky="nsew")
    return text, make_logger(text)


def make_tip(parent, row, text, colspan=3, autowrap=False):
    """提示文字：按容器宽度自动换行，避免长提示在窄窗口被裁掉；
    配合 colspan=1 使用可避免跨列提示把表单列宽撑开。"""
    lbl = ttk.Label(parent, text=text, foreground=COLOR["text_secondary"],
                    wraplength=820, justify="left")
    lbl.grid(row=row, column=0, columnspan=colspan,
             sticky="we" if autowrap else "w", padx=8, pady=(0, 6))

    def _rewrap(event):
        try:
            if event.width > 80:
                w = max(240, event.width - 32)
                if abs(int(lbl.cget("wraplength")) - w) > 12:
                    lbl.configure(wraplength=w)
        except Exception:
            pass

    parent.bind("<Configure>", _rewrap, add="+")
    return lbl


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


def load_window_config():
    """窗口相关配置（toolbox_config.json 的 window 分区；缺失时返回 {}）"""
    try:
        return config.get_section("window")
    except Exception:
        return {}


def save_window_geometry(root, extra=None):
    """保存主窗口几何（位置+大小）与最大化状态，供下次启动恢复。
    extra 可附加额外字段（如左侧菜单宽度 nav_width）。
    只更新 window 分区，不碰配置文件里的其它分区。"""
    try:
        data = {"geometry": root.geometry()}
        try:
            data["zoomed"] = (root.wm_state() == "zoomed")
        except Exception:
            data["zoomed"] = False
        if extra:
            data.update(extra)
        config.update_section("window", **data)
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


def restore_window_geometry(root, default="1280x900", min_w=720, min_h=420):
    """恢复上次窗口几何；配置缺失/越界/超出屏幕时返回 False（调用方用默认）"""
    try:
        data = config.get_section("window")
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
