# -*- coding: utf-8 -*-
"""统一消息弹窗（替代 tkinter.messagebox）
================================================
为什么不用原生 messagebox：
  Windows 上 Tk 的 tk_messageBox 走系统 MessageBoxW，**即使传了 parent，
  窗口也固定出现在屏幕正中**（实测：主窗 600x400@(200,150)，弹窗仍在屏幕
  中心），既不跟主窗口走、也不会夹在屏幕内，和应用的视觉语言也不一致。

这里用 CTk 自己画一个：
  · 居中于触发它的窗口（多级弹窗逐级居中），并保证整窗在屏幕内；
  · 白底 / 发丝描边 / 圆角，与主界面同一套设计令牌；
  · 返回值和 tkinter.messagebox 完全一致（"ok" / True / False / None /
    "yes" / "no"），存量 157 处调用一行都不用改；
  · 主线程之外（工作线程里弹提示）或还没有 Tk 根窗口时，自动退回原生实现。
"""
import threading
import time
import tkinter as tk
from tkinter import messagebox as _native

from . import ui
from .theme import COLOR, FONTS

# 窗口内部留白与正文档位（宽度按最长一行量出来，夹在这个范围内）
_PAD_X = 26
_PAD_Y = 22
_MIN_CONTENT = 280
_MAX_CONTENT = 480

# kind -> (默认标题, 圆点颜色)
_KIND = {
    "info": ("提示", COLOR["accent"]),
    "warning": ("警告", COLOR["warning"]),
    "error": ("错误", COLOR["error"]),
    "question": ("确认", COLOR["accent"]),
}

# 按钮组 -> [(文案, 返回值, 是否主按钮)]，顺序按 Windows 习惯：
# 主操作在最左，「取消」永远在最右。
_BUTTONS = {
    "ok": [("确定", "ok", True)],
    "okcancel": [("确定", "ok", True), ("取消", "cancel", False)],
    "yesno": [("是", "yes", True), ("否", "no", False)],
    "yesnocancel": [("是", "yes", True), ("否", "no", False),
                    ("取消", "cancel", False)],
    "retrycancel": [("重试", "retry", True), ("取消", "cancel", False)],
}

# 关闭窗口（标题栏 ×）时返回什么，与原生行为对齐
_ON_CLOSE = {"ok": "ok", "okcancel": False, "yesno": False,
             "yesnocancel": None, "retrycancel": False}
_ON_ESC = {"ok": "ok", "okcancel": False, "yesno": "no",
           "yesnocancel": None, "retrycancel": False}


def _content_width(text):
    """正文宽度：量最长一行文字，夹在 280~480 之间。

    短提示（“已复制”）窄一点不空荡；长说明宽一点少折断；
    短文案基本能排成一行，不会出现句号被挤到第二行的尴尬。
    """
    widest = 0
    for line in str(text).split("\n"):
        try:
            widest = max(widest, ui._text_px(line, FONTS["base"]))
        except Exception:
            pass
    if not widest:
        return 380
    return int(max(_MIN_CONTENT, min(_MAX_CONTENT, widest + 10)))


class MessageDialog(ui.Toplevel):
    """主题化消息框：构造后调用 show() 会阻塞到用户关闭，返回结果。"""

    def __init__(self, owner, title, message, kind="info", buttons="ok",
                 default=None):
        super().__init__(owner)
        # 关掉 CTk 的「重设标题栏颜色」流程：它会先 withdraw、5ms 后再恢复，
        # 和这里「先隐藏、算好位置再显示」的节奏打架，弹窗会被它重新藏回去。
        self._deactivate_windows_window_header_manipulation = True
        self.withdraw()                     # 布局完成、算好位置再显示，避免闪一下
        self._owner = owner
        self._result = None
        self.title(title)
        self.resizable(False, False)
        self.configure(fg_color=COLOR["surface"])

        text = "" if message is None else str(message)
        lines = text.count("\n") + 1
        self._content_w = _content_width(text)

        body = ui.Frame(self, fg_color=COLOR["surface"], width=self._content_w,
                        height=1)
        body.pack(fill="both", expand=True, padx=_PAD_X, pady=_PAD_Y)
        body.grid_columnconfigure(1, weight=1)

        # 标题行：语义色圆点 + 标题
        dot = ui.Frame(body, width=9, height=9, corner_radius=5,
                       fg_color=_KIND.get(kind, _KIND["info"])[1])
        dot.grid(row=0, column=0, padx=(0, 9), pady=(9, 0), sticky="n")
        ui.Label(body, text=title, font=FONTS["md"], auto_wrap=False,
                 text_color=COLOR["text"]).grid(row=0, column=1, sticky="w")

        if len(text) > 700 or lines > 13:
            rows = min(15, max(6, lines + len(text) // 60))
            box = ui.Textbox(body, width=self._content_w // 7, height=rows,
                             wrap="word", font=FONTS["base"],
                             fg_color=COLOR["surface_inset"])
            box.insert("1.0", text)
            box.configure(state="disabled")
            box.grid(row=1, column=0, columnspan=2, sticky="ew",
                     padx=(18, 0), pady=(10, 0))
        else:
            # 固定折行宽度（不跟着父容器缩），窗口宽度在 _center 里按它定死，
            # 这样长文案不会因为「先窄后宽」来回折行、把弹窗撑高。
            ui.Label(body, text=text, font=FONTS["base"], auto_wrap=False,
                     wraplength=self._content_w, justify="left",
                     text_color=COLOR["text_secondary"]).grid(
                row=1, column=0, columnspan=2, sticky="w",
                padx=(18, 0), pady=(10, 0))

        footer = ui.Frame(body, fg_color="transparent")
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(20, 0))
        self._buttons = {}
        default = (default or "").lower()
        focus_btn = None
        for label, value, primary in reversed(_BUTTONS.get(buttons, _BUTTONS["ok"])):
            kw = {}
            if primary:
                kw["bg"] = COLOR["accent"]
                kw["activebackground"] = COLOR["accent_hover"]
            btn = ui.Button(footer, text=label, width=8,
                            command=lambda v=value: self._close(v), **kw)
            btn.pack(side=tk.RIGHT, padx=(8, 0))
            self._buttons[value] = btn
            if focus_btn is None and default and default == value:
                focus_btn = btn
        if focus_btn is None:
            focus_btn = self._buttons.get(
                _BUTTONS.get(buttons, _BUTTONS["ok"])[0][1])
        self._focus_btn = focus_btn

        keep = str(buttons or "ok").lower()
        self.protocol("WM_DELETE_WINDOW", lambda: self._close(_ON_CLOSE.get(keep, "ok")))
        self.bind("<Escape>", lambda _e: self._close(_ON_ESC.get(keep, "ok")))
        self.bind("<Return>", self._on_return)

        self._center_after_idle()

    def _center_after_idle(self):
        try:
            self.after_idle(self._center)
        except Exception:
            pass

    def _center(self):
        try:
            if not self.winfo_exists():
                return
            self.update_idletasks()
            # 宽度按正文档位定死（两边各留 _PAD_X），高度按内容自适应
            ui.center_window(self, parent=self._owner,
                             width=self._content_w + 2 * _PAD_X)
        except Exception:
            pass

    def _on_return(self, _event=None):
        """回车：焦点在某个按钮上就按它，否则按默认按钮。"""
        try:
            w = self.focus_get()
        except Exception:
            w = None
        if w is not None and w in tuple(self._buttons.values()):
            try:
                w.invoke()
                return "break"
            except Exception:
                pass
        self._invoke_focus()
        return "break"

    def _invoke_focus(self):
        """回车 = 默认按钮（没有默认按钮时 = 主按钮）。"""
        try:
            self._focus_btn.invoke()
        except Exception:
            pass

    def _close(self, value):
        self._result = value
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def show(self):
        prev_grab = None
        try:
            prev_grab = self._owner.grab_current() if self._owner is not None else None
        except Exception:
            prev_grab = None
        try:
            if self._owner is not None:
                self.transient(self._owner)
        except Exception:
            pass
        # CTk 的尺寸是「延迟生效」的：文字 / 按钮要等一两轮事件循环才量准，
        # 先跑完再定位，弹窗才不会偏几个像素、也不会下半截出屏。
        # 这里只等 ~60ms（用户无感），换来「一出现就在正确位置」而不是先弹再跳。
        for _ in range(3):
            try:
                self.update_idletasks()
                self.update()
            except Exception:
                break
            time.sleep(0.02)
        self._center()                      # 先摆好位置，再显示
        self.deiconify()
        for _ in range(4):
            try:
                self.grab_set()
                break
            except Exception:
                try:
                    self.update()
                except Exception:
                    break
        try:
            self.lift()
            self._focus_btn.focus_set()
        except Exception:
            pass
        # CTk 控件尺寸要晚一两拍才稳定，再校正两次，弹窗才是真正居中的
        for _delay in (90, 220):
            try:
                self.after(_delay, self._center)
            except Exception:
                pass
        try:
            self.wait_window(self)
        except Exception:
            pass
        # 弹窗盖在被 grab 的对话框上时，关掉后要把原来的 grab 还回去
        if prev_grab is not None:
            try:
                prev_grab.grab_set()
            except Exception:
                pass
        return self._result


def show(kind, title, message, parent=None, buttons="ok", default=None):
    """弹出一条主题化消息框（阻塞）。非主线程 / 无根窗口时退回原生实现。"""
    if parent is None:
        try:
            parent = tk._default_root
        except Exception:
            parent = None
    if (parent is None or threading.current_thread() is not threading.main_thread()):
        return _native_fallback(kind, title, message, parent, buttons)
    try:
        dlg = MessageDialog(parent, title or _KIND.get(kind, _KIND["info"])[0],
                            message, kind, buttons, default)
        return dlg.show()
    except Exception:
        return _native_fallback(kind, title, message, parent, buttons)


# ============================================================
# 与 tkinter.messagebox 的兼容层
# ============================================================
_ORIGINALS = {}


def _native_fallback(kind, title, message, parent, buttons):
    """原生 messagebox：只用于工作线程 / 没有可用的 Tk 窗口等极端情况。"""
    if str(buttons) == "ok":
        name = {"error": "showerror", "warning": "showwarning"}.get(
            kind, "showinfo")
    else:
        name = {"okcancel": "askokcancel", "yesno": "askyesno",
                "yesnocancel": "askyesnocancel",
                "retrycancel": "askretrycancel"}.get(str(buttons), "askyesno")
    fn = _ORIGINALS.get(name) or getattr(_native, name)
    kw = {"parent": parent} if parent is not None else {}
    try:
        return fn(title, message, **kw)
    except Exception:
        return {"ok": "ok", "yesno": True}.get(str(buttons), True)


def _convert(name, raw):
    """把对话框结果翻译成各个 messagebox 函数的原生返回值。"""
    if name.startswith("show"):
        return "ok"
    if name == "askquestion":
        return "yes" if raw in ("yes", "ok", "retry") else "no"
    if name == "askyesnocancel":
        return {"yes": True, "no": False}.get(raw)
    return raw in ("yes", "ok", "retry")


def _wrap(name, kind, buttons):
    def fn(title=None, message="", **options):
        return _convert(name, show(kind, title, message,
                                   options.get("parent"), buttons,
                                   options.get("default")))
    fn.__name__ = name
    return fn


def install():
    """把 tkinter.messagebox 的 8 个函数换成主题化实现（幂等）。"""
    if getattr(_native, "_toolbox_themed", False):
        return
    names = ("showinfo", "showwarning", "showerror", "askquestion",
             "askokcancel", "askyesno", "askyesnocancel", "askretrycancel")
    for n in names:
        try:
            _ORIGINALS[n] = getattr(_native, n)
        except Exception:
            pass
    _native.showinfo = _wrap("showinfo", "info", "ok")
    _native.showwarning = _wrap("showwarning", "warning", "ok")
    _native.showerror = _wrap("showerror", "error", "ok")
    _native.askquestion = _wrap("askquestion", "question", "yesno")
    _native.askokcancel = _wrap("askokcancel", "question", "okcancel")
    _native.askyesno = _wrap("askyesno", "question", "yesno")
    _native.askyesnocancel = _wrap("askyesnocancel", "question", "yesnocancel")
    _native.askretrycancel = _wrap("askretrycancel", "warning", "retrycancel")
    _native._toolbox_themed = True
