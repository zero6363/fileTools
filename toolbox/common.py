# -*- coding: utf-8 -*-
"""通用基础设施：日志 / 后台任务 / 常用输入行 / Windows 文件拖放"""
import os
import sys
import queue
import re
import shutil
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from . import config
from . import ui
from . import ui as ttk
from .theme import COLOR, FONTS, ttk_font, ui_scale

APP_VERSION = "1.74"
APP_TITLE = f"文件工具箱 · 多功能合集 v{APP_VERSION}"
DEFAULT_IMAGE_FORMATS = "jpg jpeg png gif bmp tiff webp"


def app_data_roots():
    """PyInstaller 打包后资源/数据可能的根目录（按优先级返回列表）：
      · onedir   -> exe 同级的 _internal 目录（PyInstaller 把 add-data / add-binary 放这里）
      · onefile  -> sys._MEIPASS（临时解压目录）
      · 两者都包含 exe 所在目录本身（兜底）
      · 源码运行 -> 项目根目录（bin/ 实际在此）
    各模块据此探测 mozjpeg / MediaInfo / ffmpeg 等资源，onedir / onefile / 源码三态通用。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        roots = [base]
        internal = os.path.join(base, "_internal")
        if os.path.isdir(internal):
            roots.append(internal)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass and meipass not in roots:
            roots.append(meipass)
        return roots
    # 源码运行：toolbox/common.py -> toolbox -> 项目根
    return [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]


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


class BgJob:
    """把「读文件 / 读库 / 遍历目录」这类慢活挪到后台线程，主线程始终能响应。

    用法：
        job = BgJob(parent, work, on_item=..., on_done=..., on_error=...)
        job.start()

    work(job) 在后台线程里跑；里面调 job.report(x) 把中间结果交给主线程，
    最后 return 的结果作为 on_done(result) 的参数。on_item / on_done /
    on_error 一律在**主线程**执行，所以回调里可以随便碰 Tk。

    三个设计要点（都是踩过的坑）：
      · 主线程每 tick 只处理 max_per_tick 条 —— 一次灌 5000 条插表格照样会卡；
      · 代际号（gen）：同一位置连续启动多个任务时，旧任务的结果自动丢弃，
        不会在用户已经换了目录之后把旧数据填回去；
      · cancel() 只是置个标记，work 里要自己看 job.cancelled 决定何时退出。
    """

    def __init__(self, widget, work, on_item=None, on_done=None, on_error=None,
                 interval=60, max_per_tick=200, log=None):
        self.widget = widget
        self.work = work
        self.on_item = on_item
        self.on_done = on_done
        self.on_error = on_error
        self.interval = max(10, int(interval))
        self.max_per_tick = max(1, int(max_per_tick))
        self.log = log or (lambda *_: None)
        self.q = queue.Queue()
        self.cancel_event = threading.Event()
        self.gen = 0
        self._thread = None
        self._done = False
        self._result = None
        self._error = None
        self._after_id = None

    # ---- 前台侧 -------------------------------------------------
    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def report(self, item):
        """后台线程专用：把一个中间结果交给主线程（线程安全）"""
        self.q.put(item)

    def start(self):
        self.gen = BgJob._next_gen()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._schedule()
        return self.gen

    def cancel(self):
        self.cancel_event.set()

    def alive(self):
        return not self.cancel_event.is_set() and not self._done

    _GEN = 0
    _GEN_LOCK = threading.Lock()

    @classmethod
    def _next_gen(cls):
        with cls._GEN_LOCK:
            cls._GEN += 1
            return cls._GEN

    # ---- 后台侧 -------------------------------------------------
    def _run(self):
        try:
            self._result = self.work(self)
        except Exception as e:                       # 异常一律带回主线程
            self._error = e
        finally:
            self._done = True

    # ---- 主线程轮询 ---------------------------------------------
    def _schedule(self):
        try:
            self._after_id = self.widget.after(self.interval, self._poll)
        except Exception:
            self._after_id = None

    def _poll(self):
        self._after_id = None
        try:
            if not self.widget.winfo_exists():
                return
        except Exception:
            return
        if self.cancel_event.is_set():
            return                       # 已作废：不再往界面里塞东西
        # 一段时间内最多处理 max_per_tick 条，剩下的下一 tick 继续，
        # 这样「5000 个文件」也不会把一次事件循环占满。
        n = 0
        try:
            while n < self.max_per_tick:
                item = self.q.get_nowait()
                n += 1
                if self.on_item is not None:
                    try:
                        self.on_item(item)
                    except Exception:
                        self.log("后台任务回调出错：\n" + traceback.format_exc())
        except queue.Empty:
            pass
        finished = self._done and self.q.empty()
        if finished:
            if self._error is not None:
                if self.on_error is not None:
                    try:
                        self.on_error(self._error)
                    except Exception:
                        self.log("后台任务出错回调异常：\n" + traceback.format_exc())
                else:
                    self.log("后台任务出错：\n"
                             + "".join(traceback.format_exception(
                                 type(self._error), self._error,
                                 self._error.__traceback__)))
            elif self.on_done is not None:
                try:
                    self.on_done(self._result)
                except Exception:
                    self.log("后台任务完成回调出错：\n" + traceback.format_exc())
            return
        self._schedule()


def fill_listbox(listbox, items, on_done=None, chunk=400):
    """分块填充 Listbox：一次插 400 行，几万项也不会让窗口白屏几十秒。

    分块期间列表要能被再次清空/重填，所以每个 Listbox 自带一个代际号，
    新的一轮 fill 会让上一轮剩下的分块自动作废。
    """
    gen = getattr(listbox, "_fill_gen", 0) + 1
    listbox._fill_gen = gen
    listbox.delete(0, tk.END)
    total = len(items)

    def push(start):
        if getattr(listbox, "_fill_gen", 0) != gen:
            return                       # 期间又刷新过一次，这批作废
        end = min(start + chunk, total)
        if end > start:
            try:
                listbox.insert(tk.END, *items[start:end])
            except tk.TclError:
                return
        if end < total:
            try:
                listbox.after(1, lambda: push(end))
            except tk.TclError:
                pass
        elif on_done is not None:
            on_done()

    push(0)


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


# ============================================================
# 剪贴板取 Windows 路径（各输入框右侧「粘贴」按钮共用）
# ============================================================
# 盘符路径（D:\a\b、D:/a/b）或 UNC 共享（\\nas\share\dir）。
# 前面加 (?<![A-Za-z0-9]) 是为了避开 http:// 这类协议里的「单个字母:」；
# 盘符后紧跟 (?![\\/]) 同理避开 http:// 的「h://」被当成 h 盘。
_WIN_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"[A-Za-z]:[\\/](?![\\/])[^<>:\"|?*\r\n\t]*"
    r"|\\\\[^\\/\s]+[\\/][^\\/\s]+[^<>:\"|?*\r\n\t]*"
    r")"
)


def _clipboard_text():
    """读剪贴板文本；没有文本（或非 Windows）返回空串"""
    try:
        root = tk._default_root
        if root is None:
            return ""
        text = root.clipboard_get()
        return text if isinstance(text, str) else ""
    except Exception:
        return ""


def _clipboard_hdrop():
    """剪贴板里只有文件对象（资源管理器复制）时，用 CF_HDROP 取第一条路径"""
    try:
        import ctypes
        from ctypes import wintypes
        CF_HDROP = 15
        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                           wintypes.LPWSTR, wintypes.UINT]
        shell32.DragQueryFileW.restype = wintypes.UINT
        user32.GetClipboardData.restype = wintypes.HANDLE
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(CF_HDROP)
            if not handle:
                return ""
            if not shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0):
                return ""
            buf = ctypes.create_unicode_buffer(32768)
            if not shell32.DragQueryFileW(handle, 0, buf, len(buf)):
                return ""
            return buf.value
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


def _normalize_win_path(p):
    """统一成反斜杠形式：去掉 file:/// 前缀与 %20、去掉末尾多余分隔符"""
    p = (p or "").strip().strip('"').strip("'").strip()
    if p.lower().startswith("file:"):
        p = p[5:]
        try:
            from urllib.parse import unquote
            p = unquote(p)
        except Exception:
            pass
        p = p.lstrip("/")
    p = p.replace("/", "\\")
    if re.fullmatch(r"[A-Za-z]:", p):
        p += "\\"
    while len(p) > 3 and p.endswith("\\"):
        p = p[:-1]
    return p


def _existing_dir_prefix(cand):
    """整串不存在时按空格逐段回退，返回磁盘上真实存在的最长前缀目录"""
    if os.path.isdir(cand):
        return cand
    parts = cand.split(" ")
    for k in range(len(parts) - 1, 0, -1):
        p = _normalize_win_path(" ".join(parts[:k]))
        if os.path.isdir(p):
            return p
    return ""


def parse_windows_path(text):
    """从任意文本（可带引号、多行、说明文字）里提取 Windows 绝对路径。

    优先取磁盘上真实存在的目录；只拿到文件路径时返回它所在目录；
    都不存在时返回语法正确的那条（方便用户接着改）。识别不到返回空串。
    """
    if not text:
        return ""
    if "file:" in text.lower():
        # file:///D:/a%20b 这种形式：先去掉协议头并还原 %20（普通路径里的 % 不动）
        try:
            from urllib.parse import unquote
            text = unquote(re.sub(r"file:/{2,3}", "", text, flags=re.I))
        except Exception:
            pass
    paths = []
    for m in _WIN_PATH_RE.finditer(text[:200000]):
        raw = m.group(0).strip().strip('"').strip("'")
        raw = raw.rstrip(" \t，。、；：！？,.;:!?'\"）)】]")
        cand = _normalize_win_path(raw)
        if len(cand) < 3:
            continue
        paths.append(_existing_dir_prefix(cand) or cand)
    for p in paths:
        if os.path.isdir(p):
            return p
    for p in paths:
        if os.path.isfile(p):
            d = os.path.dirname(p)
            if d:
                return d
    return paths[0] if paths else ""


def paste_path_to(var, entry=None, on_change=None, allow_file=False):
    """「粘贴」按钮：把剪贴板里的 Windows 路径填进输入框；识别不到就清空。

    支持资源管理器复制的文件夹（文本或 CF_HDROP）、带引号/空格的路径、
    file:///D:/xxx 形式。返回是否识别到路径（False 时输入框已清空）。
    """
    path = parse_windows_path(_clipboard_text())
    if not path:
        hdrop = _clipboard_hdrop()
        if hdrop:
            norm = _normalize_win_path(hdrop)
            if os.path.isdir(norm) or allow_file:
                path = norm
            else:
                path = os.path.dirname(norm) or norm
    var.set(path)
    try:
        if entry is not None and str(entry.cget("state")) != "disabled":
            entry.icursor(tk.END)
            entry.xview_moveto(1.0)
    except Exception:
        pass
    if on_change is not None:
        try:
            on_change(path)
        except Exception:
            pass
    return bool(path)


def make_paste_button(parent, var, entry=None, on_change=None, **pack_kw):
    """创建统一的「粘贴」按钮（位于路径输入框右侧）"""
    btn = ttk.Button(parent, text="粘贴",
                     command=lambda: paste_path_to(var, entry, on_change))
    pack_kw.setdefault("side", tk.LEFT)
    btn.pack(**pack_kw)
    return btn


def make_form(parent):
    """表单容器：所有输入行放进同一个容器，标签列宽统一，
    输入框/浏览按钮左边缘对齐，说明文字统一放在第 4 列。
    （不要把提示文字直接跨列放在外层，否则会把列宽撑开，导致各行错位。）"""
    frame = ttk.Frame(parent)
    try:
        from tkinter import font as tkfont
        # ttk_font 已经是「像素字号」，量出来就是屏幕上的真实宽度
        ref = tkfont.Font(font=ttk_font("base")).measure("输出文件夹名：") + 22
    except Exception:
        ref = 118
    frame.grid_columnconfigure(0, minsize=ref)
    frame.grid_columnconfigure(3, weight=1)
    return frame


def form_label(parent, row, text):
    """表单标签：右对齐，使同一容器内的输入框起点一致"""
    lbl = ttk.Label(parent, text=text)
    lbl.grid(row=row, column=0, sticky="e", padx=(6, 8), pady=3)
    return lbl


def form_note(parent, row, text, column=3, columnspan=1):
    """表单说明文字：固定放第 4 列，不参与列宽计算；
    按主窗口剩余宽度自动换行，避免长说明被裁掉（CTk 下自身 Configure 会
    收敛到过窄宽度，这里改为跟随顶层窗口宽度计算可用空间）。"""
    lbl = ttk.Label(parent, text=text, foreground=COLOR["text_secondary"],
                    wraplength=340, justify="left", anchor="w")
    lbl.grid(row=row, column=column, columnspan=columnspan,
             sticky="w", padx=(8, 4), pady=3)

    def _rewrap(_event=None):
        try:
            top = lbl.winfo_toplevel()
            if not top.winfo_exists() or top.winfo_width() <= 1:
                return
            # wraplength 按设计像素生效，winfo 是真实像素，先除回缩放
            scale = max(1.0, ui_scale())
            avail = (top.winfo_width()
                     - (lbl.winfo_rootx() - top.winfo_rootx())) / scale - 24
            w = max(80, min(640, avail))
            cur = int(float(lbl.cget("wraplength") or 0))
            if abs(cur - w) > 12:
                lbl.configure(wraplength=w)
        except Exception:
            pass

    try:
        top = lbl.winfo_toplevel()
        top.bind("<Configure>", _rewrap, add="+")
        top.after_idle(_rewrap)
        top.after(200, _rewrap)
    except Exception:
        pass
    return lbl


def make_path_row(parent, row, label, default="", title="选择文件夹", entry_width=40, note=None):
    """目录输入行：标签 + 可编辑文本框 + 粘贴 / 浏览按钮。
    默认宽度按 1080 宽的最小窗口取值，保证按钮不会被挤出右边界。"""
    form_label(parent, row, label)
    var = tk.StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=entry_width).grid(
        row=row, column=1, sticky="w", padx=(0, 6), pady=3)

    def _browse():
        p = filedialog.askdirectory(title=title)
        if p:
            var.set(p)

    btns = ttk.Frame(parent)
    btns.grid(row=row, column=2, sticky="w", padx=(0, 4), pady=3)
    make_paste_button(btns, var, padx=(0, 4))
    ttk.Button(btns, text="浏览…", command=_browse).pack(side=tk.LEFT)
    if note:
        form_note(parent, row, note)
    return var


def make_file_row(parent, row, label, default="", title="选择文件", entry_width=40, note=None):
    """文件输入行（用于字体等文件选择）"""
    form_label(parent, row, label)
    var = tk.StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=entry_width).grid(
        row=row, column=1, sticky="w", padx=(0, 6), pady=3)

    def _browse():
        p = filedialog.askopenfilename(
            title=title,
            filetypes=[("字体文件", "*.ttf *.ttc *.otf"), ("所有文件", "*.*")],
        )
        if p:
            var.set(p)

    ttk.Button(parent, text="浏览…", command=_browse).grid(
        row=row, column=2, sticky="w", padx=(0, 4), pady=3)
    if note:
        form_note(parent, row, note)
    return var


def make_entry_row(parent, row, label, default="", entry_width=30, note=None):
    form_label(parent, row, label)
    var = tk.StringVar(value=default)
    ttk.Entry(parent, textvariable=var, width=entry_width).grid(
        row=row, column=1, sticky="w", padx=(0, 6), pady=3)
    if note:
        form_note(parent, row, note)
    return var


def make_num_box(parent, default, min_, max_, width=6, step=1, height=30):
    """[−] 输入 [＋] 紧凑数字组件，返回 (frame, StringVar)。

    按钮固定 26×height 的小方块（旧版走通用按钮，被自动定宽成 56px，
    在一行参数里显得又大又挤）；输入框居中对齐、宽度按字符数换算。
    frame.set_enabled(False) 可整体禁用（质量等只读场景）。
    点击按钮按 min_/max_ 钳制、按 step 步进；输入非法文本时点按钮回退到 min_，
    失焦或回车时把输入框内容收敛到合法范围。
    """
    frame = ui.Frame(parent)
    var = tk.StringVar(value=str(default))
    step = max(1, int(step))

    def do_step(delta):
        try:
            v = int(str(var.get()).strip())
        except ValueError:
            v = min_
        v = max(min_, min(max_, v + delta))
        var.set(str(v))

    def commit(_e=None):
        txt = str(var.get()).strip()
        if txt in ("", "-", "+"):
            return
        try:
            v = int(txt)
        except ValueError:
            var.set(str(min_))
            return
        v = max(min_, min(max_, v))
        if str(v) != txt:
            var.set(str(v))

    try:
        chars = max(5, int(width))
    except (TypeError, ValueError):
        chars = 6
    btn_dec = ui.StepButton(frame, text="−", height=height,
                            command=lambda: do_step(-step))
    btn_dec.pack(side=tk.LEFT)
    ent = ui.Entry(frame, textvariable=var, width_px=chars * 9, height=height,
                   justify="center")
    ent.pack(side=tk.LEFT, padx=3)
    btn_inc = ui.StepButton(frame, text="＋", height=height,
                            command=lambda: do_step(step))
    btn_inc.pack(side=tk.LEFT)
    ent.bind("<FocusOut>", commit)
    ent.bind("<Return>", commit)

    def set_enabled(flag):
        st = tk.NORMAL if flag else tk.DISABLED
        for w in (btn_dec, ent, btn_inc):
            try:
                w.configure(state=st)
            except Exception:
                pass

    frame.set_enabled = set_enabled
    return frame, var


def make_num_row(parent, row, label, default, min_, max_, note=None, width=6, step=1):
    """带标签/说明的整行数字输入（[－] Entry [＋]，方便鼠标操作），返回 StringVar"""
    form_label(parent, row, label)
    box, var = make_num_box(parent, default, min_, max_, width, step)
    box.grid(row=row, column=1, sticky="w", padx=(0, 6), pady=2)
    if note:
        form_note(parent, row, note)
    return var


def make_spin_row(parent, row, label, default, from_, to, note=None, width=10, step=1):
    # 数字输入行统一走 make_num_row（带加减按钮，鼠标操作更顺手）
    return make_num_row(parent, row, label, default, from_, to, note, width, step)


def make_check(parent, text, default=False, row=None, column=None, var=None,
               columnspan=1):
    """复选框；columnspan 用于宽选项横跨输入框/按钮列，避免把第 1 列撑宽。"""
    if var is None:
        var = tk.BooleanVar(value=default)
    cb = ttk.Checkbutton(parent, text=text, variable=var)
    if row is not None:
        cb.grid(row=row, column=column, columnspan=columnspan,
                sticky="w", padx=(0, 6), pady=3)
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
    """日志面板：区块标题 + 带描边的文本框（比 LabelFrame 标题更清爽）。"""
    wrap = ttk.Frame(parent)
    wrap.grid(row=row, column=0, columnspan=colspan, sticky="nsew", padx=8, pady=6)
    wrap.grid_rowconfigure(1, weight=1)
    wrap.grid_columnconfigure(0, weight=1)
    ttk.Label(wrap, text=title, font=FONTS["md"],
              foreground=COLOR["text"]).grid(row=0, column=0, sticky="w", pady=(0, 4))
    text = ui.Textbox(wrap, height=height, state="disabled",
                      font=FONTS["log"],
                      background=COLOR["surface_inset"], foreground=COLOR["text"])
    text.grid(row=1, column=0, sticky="nsew")
    return text, make_logger(text)


def make_tip(parent, row, text, colspan=3, autowrap=False, font=None):
    """提示文字：按容器宽度自动换行，避免长提示在窄窗口被裁掉；
    配合 colspan=1 使用可避免跨列提示把表单列宽撑开。

    `auto_wrap=False` 很关键：ui.Label 自带的自动折行会按父容器宽度
    自行改 wraplength，和这里的 <Configure> 逻辑抢同一个属性，
    两个公式算出的值不同，文字就会在 1 行 / 2 行之间来回跳（上下抖）。
    """
    lbl = ttk.Label(parent, text=text, foreground=COLOR["text_secondary"],
                    font=font or FONTS["base"], auto_wrap=False,
                    wraplength=820, justify="left", anchor="w")
    lbl.grid(row=row, column=0, columnspan=colspan,
             sticky="we" if autowrap else "w", padx=8, pady=(0, 6))

    seen = {"w": -1}

    def _apply(width):
        """按给定宽度定折行。

        event.width 是**真实像素**，而 wraplength 走设计像素（CTk 会再乘
        缩放），高 DPI 下要先除回去，否则永远差一截、文字还是被裁。
        宽度没实质变化（<24px）就不动。
        """
        if width <= 80:
            return
        design = int(round(width / max(1.0, ui_scale())))
        if design == seen["w"]:
            return
        seen["w"] = design
        w = max(240, design - 16)
        if abs(int(float(lbl.cget("wraplength") or 0)) - w) > 24:
            lbl.configure(wraplength=w)

    def _rewrap(event):
        """只按**宽度**重算折行（高度变化也发 <Configure>，那种一律忽略）。"""
        try:
            _apply(event.width)
        except Exception:
            pass

    parent.bind("<Configure>", _rewrap, add="+")
    # 容器宽度可能在 bind 之前就定好了（那样 <Configure> 再也不会来），
    # 排一次延迟测量，否则折行宽度永远停在初始的 820
    lbl.after(200, lambda: _apply(parent.winfo_width()))
    return lbl


# ============================================================
# 表格列宽记忆 / 横向滚动条
# ============================================================

TABLE_CONFIG_SECTION = "tables"

# 完成提示音：默认关闭，用户勾选后记住（两个 Tab 各存一个开关）
NOTIFY_SECTION = "notify"


def load_done_sound(key):
    """读取某个 Tab 的完成提示音开关，默认 False（不勾选）。"""
    try:
        return bool(config.get_section(NOTIFY_SECTION).get(key, False))
    except Exception:
        return False


def save_done_sound(key, value):
    """记住某个 Tab 的完成提示音开关。"""
    try:
        config.update_section(NOTIFY_SECTION, **{key: bool(value)})
    except Exception:
        pass


def play_done_sound():
    """播放 Windows 系统提示音（异步，不阻塞界面）；其它平台静默忽略。"""
    try:
        import winsound
    except Exception:
        return
    try:
        winsound.PlaySound("SystemAsterisk",
                           winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass


def _tree_column_ids(tree):
    """返回表格所有列（含 Treeview 的 #0 树列），用于列宽记忆。"""
    try:
        cols = list(tree["columns"])
    except Exception:
        cols = []
    try:
        if "tree" in str(tree.cget("show")):
            cols.insert(0, "#0")
    except Exception:
        pass
    return cols


def _tree_widths(tree, cols):
    out = {}
    for col in cols:
        try:
            w = int(tree.column(col, "width"))
        except Exception:
            continue
        if w >= 1:
            out[col] = w
    return out


def setup_tree_columns(tree, key):
    """恢复并记忆表格列宽。

    · 创建表格、设置好默认列宽后调用一次；
    · 用户拖动列宽后（鼠标释放 / 表格销毁）自动写入 toolbox_config.json 的
      tables 分区，下次启动自动恢复；
    · 所有表格共用同一套逻辑，互不影响。

    列宽不设严格最低宽度：用户想把「修改时间」压到几十像素也得能压下去。
    做法是**只在真正要锁宽度时才把 minwidth 放开**——
      1) 有记忆宽度（所有列 stretch=False，不会再随窗口缩放）→ 直接放开；
      2) 用户按下分隔线开始拖动 → 松手前放开。
    没拖过的首次运行仍保留各表自带的 minwidth，窗口变窄时列不会被压窄。
    放开也不是放开到 1px，而是保底 ~20 设计像素：全部放开到 1px 的老做法
    会把列拖成一条缝——分隔线抓不回来，列就「消失」了；保底几个像素宽
    既能压得很窄，又永远能重新抓住分隔线拖回（或双击自适应）。
    """
    cols = _tree_column_ids(tree)
    if not cols:
        return
    section = config.get_section(TABLE_CONFIG_SECTION)
    saved = section.get(key)
    has_saved = isinstance(saved, dict) and bool(saved)
    try:
        saved_scale = float(section.get(key + ".scale") or 0)
    except (TypeError, ValueError):
        saved_scale = 0.0
    if not (0.5 <= saved_scale <= 3.0):
        saved_scale = 0.0
    # 高分屏下表格字体按 DPI 放大，列宽必须同步放大，
    # 否则日期、大小等文字会被列边界裁掉。
    scale = ui_scale()
    # 列宽保底：按 DPI 换算的 ~20 设计像素，任何缩放下分隔线都抓得住
    min_w = max(18, int(round(20 * scale)))

    orig_stretch = {}
    orig_minwidth = {}
    for col in cols:
        try:
            orig_stretch[col] = bool(int(tree.column(col, "stretch")))
        except Exception:
            orig_stretch[col] = True
        try:
            orig_minwidth[col] = int(tree.column(col, "minwidth"))
        except Exception:
            orig_minwidth[col] = 20

    def unlock_minwidth():
        """放开最低列宽限制到保底值（见上方 docstring）。"""
        for col in cols:
            try:
                tree.column(col, minwidth=min_w)
            except Exception:
                pass

    def relock_minwidth():
        for col, mw in orig_minwidth.items():
            try:
                tree.column(col, minwidth=mw)
            except Exception:
                pass

    if has_saved:
        # 保存时也记录当时的缩放系数：换到不同 DPI 的屏幕后按比例换算，
        # 避免同一份配置在 100% / 200% 之间来回切换时越放越大或越缩越小。
        factor = (scale / saved_scale) if saved_scale else scale
        unlock_minwidth()
        # 先固定所有列再写宽度：否则写某一列时，其余可拉伸的列会被
        # Tk 按窗口宽度重新分配（最低宽度已放开到 1px，会被压成一条缝）。
        for col in cols:
            try:
                tree.column(col, stretch=False)
            except Exception:
                pass
        for col, width in saved.items():
            if col not in cols:
                continue
            try:
                width = int(width)
            except (TypeError, ValueError):
                continue
            if width < 1:
                continue
            try:
                tree.column(col, width=int(round(width * factor)))
            except Exception:
                pass
    else:
        for col in cols:
            try:
                w = int(tree.column(col, "width"))
                tree.column(col, width=max(20, int(round(w * scale))))
            except Exception:
                pass

    state = {"last": _tree_widths(tree, cols), "dragging": False,
             "press": None}

    def save_now():
        widths = _tree_widths(tree, cols)
        if not widths or widths == state["last"]:
            return
        state["last"] = widths
        try:
            section = config.get_section(TABLE_CONFIG_SECTION)
            section[key] = widths
            section[key + ".scale"] = round(scale, 3)
            config.set_section(TABLE_CONFIG_SECTION, section)
        except Exception:
            pass

    def lock_columns():
        for col in cols:
            try:
                tree.column(col, stretch=False)
            except Exception:
                pass

    def on_press(event=None):
        # 用户按下表头分隔线准备拖动：先取消 stretch，否则 Tk 会在拖动
        # 过程中按窗口宽度重新分配，把用户拖出来的宽度改掉。
        if event is not None:
            try:
                region = tree.identify_region(event.x, event.y)
            except Exception:
                region = ""
            if region != "separator":
                return
        lock_columns()
        unlock_minwidth()        # 拖动时不设严格最低宽度，仅保底 min_w
        state["dragging"] = True
        state["press"] = _tree_widths(tree, cols)

    def on_release(event=None):
        if not state["dragging"]:
            return
        state["dragging"] = False
        widths = _tree_widths(tree, cols)
        press_widths = state["press"]
        state["press"] = None
        if widths == press_widths:
            # 只是点了一下分隔线、没有真正拖动：恢复原来的拉伸行为
            if not has_saved:
                for col, stretch in orig_stretch.items():
                    try:
                        tree.column(col, stretch=stretch)
                    except Exception:
                        pass
                relock_minwidth()
            return
        save_now()

    def fit_column(col_id):
        """按表头 + 当前行内容的实际文字宽度算一个合适列宽（双击分隔线用）。"""
        from tkinter import font as tkfont
        try:
            # 表格列宽按真实像素存，字体也要用像素字号量
            f = tkfont.Font(font=ttk_font("table"))
        except Exception:
            return None
        try:
            head = tree.heading(col_id, "text")
        except Exception:
            head = ""
        try:
            widest = f.measure(str(head)) + 26
        except Exception:
            widest = 80
        try:
            items = tree.get_children()[:400]
        except Exception:
            items = []
        for iid in items:
            try:
                text = tree.item(iid, "text") if col_id == "#0" \
                    else tree.set(iid, col_id)
            except Exception:
                continue
            try:
                widest = max(widest, f.measure(str(text)) + 26)
            except Exception:
                pass
        return int(min(1200, max(min_w, widest)))

    def on_double_click(event=None):
        """双击分隔线 = 按内容自适应该列宽度（列被压窄后不用手动拖回来）。"""
        if event is None:
            return
        try:
            if tree.identify_region(event.x, event.y) != "separator":
                return
            col_id = tree.identify_column(event.x)
        except Exception:
            return
        # identify_column 给的是 "#2" 这种序号，换回真实列 id
        real = col_id
        if col_id.startswith("#") and col_id != "#0":
            try:
                real = cols[int(col_id[1:]) - 1]
            except Exception:
                return
        width = fit_column(real)
        if not width:
            return
        try:
            tree.column(real, width=width)
            save_now()
        except Exception:
            pass

    try:
        tree.bind("<ButtonPress-1>", on_press, add="+")
        tree.bind("<ButtonRelease-1>", on_release, add="+")
        tree.bind("<Double-Button-1>", on_double_click, add="+")
    except Exception:
        pass


def add_table_hscroll(tree, parent, row, column=0, columnspan=1):
    """给表格补一条横向滚动条：内容超出表格宽度时才显示，不超出时收起，
    避免每一页底部都白占一条 17px 的空滚动条。"""
    hsb = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=tree.xview)
    hsb.grid(row=row, column=column, columnspan=columnspan, sticky="ew")

    def _sync(_e=None):
        try:
            first, last = tree.xview()
            need = not (first <= 0.001 and last >= 0.999)
            if need and not hsb.winfo_ismapped():
                hsb.grid()
            elif not need and hsb.winfo_ismapped():
                hsb.grid_remove()
        except Exception:
            pass

    def _on_xscroll(first, last):
        hsb.set(first, last)
        # 在滚动回调里改布局容易抖动，放到空闲时再判断
        try:
            tree.after_idle(_sync)
        except Exception:
            pass

    tree.configure(xscrollcommand=_on_xscroll)
    tree.bind("<Configure>", lambda e: tree.after_idle(_sync), add="+")
    tree.after(150, _sync)
    tree.after(600, _sync)
    return hsb


def _page_contains(ancestor, widget):
    """widget 是否在 ancestor 里（walk master 链；CTk 复合控件也适用）"""
    cur = widget
    while cur is not None:
        if cur is ancestor:
            return True
        try:
            cur = cur.master
        except Exception:
            return False
    return False


def _self_scrolls(widget):
    """自己吃滚轮、不该被页面抢走的控件：表格 / 文本框 / 列表。"""
    try:
        if isinstance(widget, (ttk.Treeview, tk.Text, tk.Listbox)):
            return True
    except Exception:
        pass
    return False


class ScrollPage(tk.Frame):
    """整页竖向滚动容器：内容放 `.body`，表格给固定行数，窗口再矮也看得见。

    为什么需要它：⑲ 这类页面顶部有一堆表单 + 进度 + 搜索栏，窗口一矮，
    带 weight 的表格行就被压成一条缝（表格越小越看不见，越看不见越不敢用）。

    高度策略：
      · 内容自然高度 ≥ 可视高度 → 内容按自然高度摆放，整页上下滚；
      · 内容更矮 → 把内容拉到可视高度，带 weight 的行（表格）因此撑满，底部不留白。
      滚动条常驻右侧（内容矮时滑块满格），只做视觉提示，不参与宽度计算。

    用法：
        page = ScrollPage(parent)
        page.pack(fill="both", expand=True)      # 或 page.grid(...)
        body = page.body                          # 之后所有控件都放进 body
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(4, weight=1)
        ...
        page.refresh()          # 内容高度可能变了（展开日志 / 提示折行）时叫一次

    滚轮：指针在本页内、且不在表格 / 文本框上时滚页面（那些控件自己滚）。

    三个抗抖动设计（v1.67，之前实测会「上下抽搐 + 顶部说明文字抖」）：
      · body 用**原生 tk.Frame**。CTk 的 Frame 会把「当前实际高度」反算进
        winfo_reqheight()，于是「把它拉高 20px → 它报的请求高度也高 20px →
        再拉高一次」，need 在 906 / 926 之间反复横跳，scrollregion 每 500ms
        变一次，页面就小幅上下抽。原生 Frame 的请求高度只由子控件决定。
      · 滚动条**常驻**，不做显示 / 隐藏切换。移除会让 canvas 变宽十几像素，
        正文跟着重排、提示文字多折一行，`<Configure>` 又反过来触发布局 ——
        宽度一变就是一台自我驱动的振荡器。常驻后正文宽度是常量。
      · 内容比可视区高时**不写死高度**，用 canvas 的 height=0（窗口自然
        请求高度），免得把 body 压矮又让子控件把矮高度当请求高度报回来。
    """

    def __init__(self, master=None, bg=None, **kw):
        bg = bg or COLOR["bg"]
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, **kw)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                takefocus=0,
                                yscrollincrement=max(20, int(round(48 * ui_scale()))))
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar = ttk.Scrollbar(self, orient=tk.VERTICAL,
                                 command=self.canvas.yview)
        self.bar.grid(row=0, column=1, sticky="ns")      # 常驻：宽度恒定
        self.canvas.configure(yscrollcommand=self.bar.set)
        self._scrollable = False        # 内容是否超出可视区（决定滚轮是否响应）
        self._key = None
        self._need = -1
        self._pending = None
        self._job = None
        self._watch = None

        # 原生 tk.Frame：请求高度只反映子控件，不会被「刚被拉高」带偏
        self.body = tk.Frame(self.canvas, bg=bg, highlightthickness=0, bd=0)
        self._win = self.canvas.create_window(0, 0, window=self.body,
                                              anchor="nw")
        self.canvas.bind("<Configure>", lambda _e: self.schedule(), add="+")
        # 注意：**不要**给 body 绑 <Configure>。body 的尺寸是这里设的，
        # 它回过来的 Configure 只是自己动作的回声；拿它当输入就会形成
        # 「设高度 → 收到 Configure → 再设高度」的自激循环。
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.after_idle(self.schedule)
        self._tick()

    # ---------------- 滚轮 ----------------
    def _on_wheel(self, event):
        """页面自己滚，并吃掉事件，别让外层再滚一次（那是「抽搐」的来源之一）。"""
        try:
            if not self._scrollable or not self.winfo_exists() \
                    or not self.winfo_ismapped():
                return
            widget = event.widget
            if _self_scrolls(widget) or not _page_contains(self, widget):
                return
            # 一格滚轮（delta=120）滚 yscrollincrement 个像素；
            # 触控板那种小 delta 也至少动 1 格，不然手感发黏
            step = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(step, "units")
            return "break"
        except Exception:
            pass

    # ---------------- 布局 ----------------
    def refresh(self):
        """内容高度变化后叫一次（展开日志、提示文字折行等）。"""
        self.schedule()

    def schedule(self):
        if self._job is not None:
            return
        try:
            self._job = self.after(16, self._layout)
        except Exception:
            self._job = None

    def _layout(self):
        self._job = None
        try:
            if not self.canvas.winfo_exists():
                return
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            if cw <= 1 or ch <= 1:            # 还没 map，等 <Configure>
                return
            need = self.body.winfo_reqheight()
            if need <= 1:                     # 还没测量出来，别急着定高度
                return
            # 滞回：出滚动条要超出 2px，撤的时候要真的回到可视高度内，
            # 中间留 2px 死区，免得在临界高度上来回翻。
            # （滚动条槽位已固定宽度，正文不会因它出现而重新折行，
            #   这里的死区只是给外部内容变化留余量。）
            # 内容能不能滚：只影响滚轮是否响应 + 滚动条要不要灰掉，
            # **不再改变布局宽度**（滚动条常驻），所以这里可以放心随高度变。
            scrollable = need > ch + 2
            if scrollable != self._scrollable:
                self._scrollable = scrollable
                if not scrollable:
                    self.canvas.yview_moveto(0)
            # 内容矮 → 拉到可视高度，让带 weight 的行（表格）撑满；
            # 内容高 → **height=0 交给 canvas 用窗口自身的请求高度**，
            # 绝不写死一个像素值。写死成 max(need, ch) 会把 body 压得比
            # 内容矮，被压的 CTk 表格容器又会把这个矮高度当成自己的请求
            # 高度报回来，need 就在 906 / 926 之间来回跳 —— 页面上下抽搐的
            # 真凶。（height=0 在 Tk 里表示「用窗口的请求尺寸」）
            tall = need > ch
            want = need if tall else ch
            key = (cw, want, tall)
            if key != self._key:
                self._key = key
                if tall:
                    self.canvas.itemconfigure(self._win, width=cw, height=0)
                else:
                    self.canvas.itemconfigure(self._win, width=cw, height=want)
                self.canvas.configure(scrollregion=(0, 0, cw, want))
            self._need = need
        except Exception:
            pass

    def _tick(self):
        """低频兜底：内容高度确实变了（展开日志、提示多折一行）时补一次布局。

        只在**连续两次读到同一个新值**时才认，噪声（1~2px 抖动、
        中间态）一律丢掉 —— 这个轮询是安全网，不是主驱动。
        """
        try:
            if not self.winfo_exists():
                self._watch = None
                return
            need = self.body.winfo_reqheight()
            if abs(need - self._need) >= 8:
                if self._pending == need:
                    self._pending = None
                    self.schedule()
                else:
                    self._pending = need
            else:
                self._pending = None
            self._watch = self.after(1000, self._tick)
        except Exception:
            self._watch = None

    def destroy(self):
        for job in (self._job, self._watch):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._job = self._watch = None
        super().destroy()


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
    只更新 window 分区，不碰配置文件里的其它分区。

    同时记下当时的缩放：`root.geometry()` 读回的宽高是「设计像素」（CTk
    会按系统缩放反算），位置是真实像素；换到 4K / 不同缩放比例的机器后
    靠这个值判断要不要重新夹取位置。
    """
    try:
        data = {"geometry": root.geometry(),
                "scale": round(ui_scale(), 3)}
        try:
            data["zoomed"] = (root.wm_state() == "zoomed")
        except Exception:
            data["zoomed"] = False
        if extra:
            data.update(extra)
        config.update_section("window", **data)
    except Exception:
        pass


def _install_themed_messagebox():
    """接管 tkinter.messagebox：换成主题化、跟随触发窗口居中的弹窗。

    原生 MessageBox 在 Windows 上固定出现在屏幕正中（传 parent 也没用），
    多级弹窗会越弹越偏，样式也和主界面不一致。这里统一交给 dialogs
    模块（幂等，可重复调用）。
    """
    try:
        from . import dialogs
        dialogs.install()
    except Exception:
        pass


def _rect_overlap(a, b):
    """两个 (x, y, w, h) 矩形的重叠面积（不相交为 0）"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0
    return (x2 - x1) * (y2 - y1)


def restore_window_geometry(root, default="1280x900", min_w=720, min_h=420):
    """恢复上次窗口几何；配置缺失 / 整块落在已拔掉的显示器上时返回 False。

    多显示器下的两个坑：
      · 配置里的宽高是「设计像素」（CTk 按缩放反算），位置是真实像素 ——
        校验可见性前必须先按当前缩放换算成真实矩形；
      · 副屏坐标可能是负数、也可能在右边（x 接近 1920+），只拿主屏
        `winfo_screenwidth()` 判断会把副屏位置当成「越界」直接丢弃。
    这里改为逐块显示器判重叠：至少 120×48 露在某个工作区里才恢复，
    否则交给调用方用默认尺寸；恢复时再夹进该屏工作区，避免标题栏在屏幕外。
    """
    try:
        data = config.get_section("window")
    except Exception:
        return False
    geo = data.get("geometry", "")
    m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(geo))
    if not m:
        return False
    w, h, x, y = (int(g) for g in m.groups())
    if not (min_w <= w <= 20000 and min_h <= h <= 20000):
        return False
    scale = max(1.0, ui_scale())
    px_w, px_h = int(round(w * scale)), int(round(h * scale))
    # 至少要有一块能看见、够得着标题栏的区域
    visible = max((_rect_overlap((x, y, px_w, px_h), area)
                   for area in ui.monitor_work_areas()), default=0)
    if visible < 120 * 48:
        return False
    bx, by, bw, bh = ui.monitor_work_area(rect=(x, y, px_w, px_h))
    px_w = min(px_w, bw)
    px_h = min(px_h, bh)
    x = max(bx, min(bx + bw - px_w, x))
    y = max(by, min(by + bh - px_h, y))
    if not ui.set_geometry_px(root, px_w, px_h, x, y):
        return False
    if data.get("zoomed"):
        def _do_zoom():
            try:
                if root.winfo_exists():
                    root.state("zoomed")
            except Exception:
                pass
        root.after(120, _do_zoom)
    return True


# 模块导入即生效：所有 messagebox 换成主题化、跟随窗口居中的弹窗
_install_themed_messagebox()


# ============================================================


def _walk_widgets(w):
    yield w
    for c in w.winfo_children():
        yield from _walk_widgets(c)
