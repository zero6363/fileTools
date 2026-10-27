# -*- coding: utf-8 -*-
"""统一 UI 适配层：把现有代码里的 ttk/tk 可视控件路由到 CustomTkinter，
同时保留 ttk.Treeview / Scrollbar / Style 等原样透传。

设计意图：
  · 现有 tab 只需把 `from tkinter import ttk` 换成 `from . import ui as ttk`，
    `ttk.Button / ttk.Label / ttk.Frame / ttk.Entry / ttk.Checkbutton / ttk.Combobox`
    就会自动变成现代风格的 CTk 控件；`ttk.Treeview / ttk.Scrollbar / ttk.Style`
    等仍走标准 ttk（CTk 未提供等价件）。
  · 旧的样式写法（bg/fg/activebackground/padx/pady/relief/borderwidth）会被
    翻译成 CTk 对应参数，调用方无需逐行改写。
  · 颜色 / 字体统一取自 theme 令牌，保证全应用字号、配色一致。
"""
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import customtkinter as ctk
from .theme import COLOR, FONTS

# CustomTkinter 默认外观：浅色模式（与现有设计系统一致）
ctk.set_appearance_mode("light")

# 字符宽度 → 像素 的近似换算（CTk 的 width/height 以像素计，旧 ttk 以字符计）
_CHAR_PX = 7
_FONT_CACHE = {}


def _px(n, scale=_CHAR_PX, minimum=0):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return max(minimum, n * scale)


def _text_px(text, font_desc=None):
    """测量文本像素宽度；失败时按字符数估算（中文按 14px/字）。"""
    text = "" if text is None else str(text)
    if not text:
        return 0
    font_desc = font_desc or FONTS["base"]
    try:
        key = tuple(font_desc) if isinstance(font_desc, (tuple, list)) else None
        if key is not None:
            f = _FONT_CACHE.get(key)
            if f is None:
                f = tkfont.Font(font=key)
                _FONT_CACHE[key] = f
            return f.measure(text)
    except Exception:
        pass
    return len(text) * 14


def init_appearance():
    """显式初始化外观（在创建任何控件前调用一次即可）。"""
    ctk.set_appearance_mode("light")


# ============================================================
# 控件封装（均为 CTk 子类，保留 .config/.cget/.pack/.grid/.bind 等全部方法）
# ============================================================
class _CTkCompat:
    """CTk 6 起移除了 .config()（强制用 .configure）。
    存量代码大量使用 .config(...)，这里恢复为 .configure 的别名，
    避免逐行改写各 tab；同时支持 .config("text") 读取属性。"""

    def config(self, cnf=None, **kw):
        if isinstance(cnf, str):
            return self.cget(cnf)
        if cnf:
            kw.update(cnf)
        return self.configure(**kw)


class Button(_CTkCompat, ctk.CTkButton):
    def __init__(self, master=None, **kw):
        # 旧样式 → CTk 参数
        bg = kw.pop("bg", None)
        fg = kw.pop("fg", None)
        ab = kw.pop("activebackground", None)
        dfg = kw.pop("disabledforeground", None)
        kw.pop("activeforeground", None)
        kw.pop("relief", None)
        bd = kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("padx", None)
        kw.pop("pady", None)
        kw.pop("cursor", None)            # CTk 不接收 cursor
        kw.pop("highlightthickness", None)
        kw.pop("highlightbackground", None)
        kw.pop("highlightcolor", None)
        kw.pop("justify", None)
        height = kw.pop("height", None)   # 旧 ttk 的 height(行) 交给 CTk 自适应
        try:
            height = int(height)
        except (TypeError, ValueError):
            height = 32
        kw.setdefault("height", max(28, height))

        if bg is None:
            # 次级按钮（未显式指定底色，如「浏览...」「添加文件」）：
            # 亮底描边，与设计系统 TButton 一致，避免满屏蓝色主按钮
            bg = COLOR["surface_bright"]
            fg = fg or COLOR["text"]
            dfg = dfg or COLOR["text_muted"]
            kw["border_width"] = 1
            kw["border_color"] = COLOR["border_strong"]
        else:
            # 主按钮/彩色按钮：底色上用白字（或调用方显式给的字色）
            fg = fg or COLOR["text_on_accent"]
            dfg = dfg or fg
        kw["fg_color"] = bg
        kw["text_color"] = fg
        if ab is not None:
            kw["hover_color"] = ab        # 显式给了悬停色才覆盖（否则 CTk 自动派生）
        kw["text_color_disabled"] = dfg
        kw.setdefault("corner_radius", 6)
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        # 按钮宽度按文字自动计算（CTk 默认固定 140px，会让「浏览」等小按钮过大）；
        # 调用方显式给的旧式字符宽度只作为下限。
        target = _text_px(kw.get("text", ""), kw["font"]) + 30
        w = _px(kw.pop("width", None))
        if w:
            target = max(target, w)
        kw["width"] = max(56, min(280, target))
        super().__init__(master, **kw)


class Label(_CTkCompat, ctk.CTkLabel):
    def configure(self, cnf=None, **kw):
        """兼容 Tk 的 foreground/background 写法：CTkLabel 用 text_color/fg_color。"""
        if isinstance(cnf, str):
            return self.cget(cnf)
        if cnf:
            kw.update(cnf)
        fg = kw.pop("foreground", kw.pop("fg", None))
        if fg is not None:
            kw["text_color"] = fg
        bg = kw.pop("background", kw.pop("bg", None))
        if bg is not None:
            kw["fg_color"] = bg
        return super().configure(**kw)

    def __init__(self, master=None, **kw):
        fg = kw.pop("foreground", kw.pop("fg", COLOR["text"]))
        bg = kw.pop("background", kw.pop("bg", None))
        kw.pop("relief", None)
        kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("highlightthickness", None)
        kw.pop("highlightbackground", None)
        kw.pop("highlightcolor", None)
        kw.pop("cursor", None)
        kw.pop("disabledforeground", None)

        # 自动换行：长文本 / 动态状态文字默认按容器宽度折行，避免撑破窗口。
        # 调用方显式给 wraplength 时把它当作上限（窗口更窄时继续收窄，
        # 窗口更宽时不会超过调用方期望的宽度）；宽度很小的定宽标签
        # （如百分比）保持原样，避免把一两个字拆成多行。
        explicit_wrap = kw.get("wraplength")
        auto_wrap = kw.pop("auto_wrap", None)
        try:
            wrap_cap = int(float(explicit_wrap)) if explicit_wrap not in (
                None, "") else 0
        except (TypeError, ValueError):
            wrap_cap = 0

        kw["text_color"] = fg
        if bg is not None:
            kw["fg_color"] = bg
        else:
            kw["fg_color"] = "transparent"
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        # 说明文字 / 提示默认左对齐，避免多行文字被居中
        kw.setdefault("anchor", "w")
        kw.setdefault("justify", "left")
        w = _px(kw.pop("width", None))
        if w:
            kw["width"] = w
        super().__init__(master, **kw)

        if auto_wrap is None:
            if explicit_wrap in (None, "", 0, "0"):
                auto_wrap = (w is None or w >= 80)
            else:
                auto_wrap = wrap_cap > 0
        self._wrap_cap = wrap_cap if wrap_cap > 0 else None
        self._wrap_fixed = w if auto_wrap and w else None
        if auto_wrap:
            self._install_auto_wrap()

    def _install_auto_wrap(self):
        """按父容器可用宽度自动设置 wraplength；容器变宽 / 变窄时跟着调整。"""
        state = {"job": None}

        def apply():
            state["job"] = None
            try:
                if not self.winfo_exists():
                    return
                if self._wrap_fixed:
                    avail = self._wrap_fixed
                else:
                    master = self.master
                    mw = master.winfo_width()
                    if mw <= 1:
                        return
                    avail = mw - max(0, self.winfo_x()) - 12
                if self._wrap_cap:
                    avail = min(avail, self._wrap_cap)
                avail = max(80, int(avail))
                cur = int(float(self.cget("wraplength") or 0))
                if abs(cur - avail) > 12:
                    self.configure(wraplength=avail)
            except Exception:
                pass

        def schedule(_event=None):
            try:
                if state["job"] is not None:
                    self.after_cancel(state["job"])
                state["job"] = self.after(60, apply)
            except Exception:
                pass

        try:
            self.master.bind("<Configure>", schedule, add="+")
        except Exception:
            pass
        try:
            self.after_idle(apply)
            self.after(250, apply)
        except Exception:
            pass


class Frame(_CTkCompat, ctk.CTkFrame):
    def __init__(self, master=None, **kw):
        bg = kw.pop("background", kw.pop("bg", None))
        kw.pop("relief", None)
        bd = kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("highlightthickness", None)
        kw.pop("highlightbackground", None)
        kw.pop("highlightcolor", None)
        kw.pop("takefocus", None)
        kw.pop("padding", None)  # CTkFrame 无 padding 参数，忽略以保持兼容

        if bg is not None:
            kw["fg_color"] = bg
        else:
            kw.setdefault("fg_color", "transparent")
        if bd not in (None, 0):
            kw["border_width"] = int(bd)
            kw.setdefault("border_color", COLOR["border"])
        kw.setdefault("corner_radius", 8)
        super().__init__(master, **kw)


class Entry(_CTkCompat, ctk.CTkEntry):
    def __init__(self, master=None, **kw):
        fg = kw.pop("foreground", kw.pop("fg", COLOR["text"]))
        bg = kw.pop("background", kw.pop("bg", None))
        kw.pop("relief", None)
        kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("highlightthickness", None)
        kw.pop("highlightbackground", None)
        kw.pop("highlightcolor", None)
        kw.pop("insertbackground", None)
        kw.pop("exportselection", None)
        kw.pop("cursor", None)

        kw["text_color"] = fg
        # 输入框统一白底（CTk 默认是浅灰 #F9F9FA，和纯白卡片不协调）
        kw["fg_color"] = bg if bg is not None else COLOR["surface_bright"]
        kw.setdefault("border_color", COLOR["border_strong"])
        kw.setdefault("corner_radius", 6)
        kw.setdefault("height", 32)
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        # width 按字符宽度换算；需要精确像素时用 width_px（数字输入框等）
        wpx = kw.pop("width_px", None)
        w = _px(kw.pop("width", None))
        if wpx is not None:
            kw["width"] = int(wpx)
        elif w:
            kw["width"] = w
        super().__init__(master, **kw)


class StepButton(_CTkCompat, ctk.CTkButton):
    """数量步进按钮（−/＋）：固定小方块，浅底描边。

    普通 ui.Button 会按文字自动定宽（最小 56px），放进数字输入行会把整行
    撑高、也抢走视觉重量；这里用固定 26px 宽，与官方 CTkButton 的描边/圆角
    风格保持一致。
    """

    def __init__(self, master=None, **kw):
        kw.pop("padx", None)
        kw.pop("pady", None)
        kw.pop("cursor", None)
        kw.setdefault("width", 26)
        kw.setdefault("height", 28)
        kw.setdefault("corner_radius", 6)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", COLOR["border_strong"])
        kw.setdefault("fg_color", COLOR["surface_alt"])
        kw.setdefault("hover_color", COLOR["border"])
        kw.setdefault("text_color", COLOR["text"])
        kw.setdefault("text_color_disabled", COLOR["text_muted"])
        kw.setdefault("font", FONTS["xs"])
        super().__init__(master, **kw)


class Checkbutton(_CTkCompat, ctk.CTkCheckBox):
    def __init__(self, master=None, **kw):
        fg = kw.pop("foreground", kw.pop("fg", COLOR["text"]))
        bg = kw.pop("background", kw.pop("bg", None))
        kw.pop("relief", None)
        kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("highlightthickness", None)
        kw.pop("highlightbackground", None)
        kw.pop("highlightcolor", None)
        kw.pop("padx", None)
        kw.pop("pady", None)
        kw.pop("anchor", None)
        kw.pop("cursor", None)

        kw["text_color"] = fg
        # CTkCheckBox 的 fg_color 是勾选框填充色，不允许 transparent；
        # 未显式指定底色时统一用强调色（与全站设计系统一致）
        kw["fg_color"] = bg if bg is not None else COLOR["accent"]
        kw["border_color"] = COLOR["border_strong"]
        kw["checkmark_color"] = COLOR["text_on_accent"]
        kw["hover_color"] = COLOR["accent_soft"]
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        # 按文字自动计算宽度：勾选框 18px + 间距，留 26px 即可；
        # 旧值 48px 会在多复选框并排时累计出几十像素的空白，把整行顶宽。
        target = _text_px(kw.get("text", ""), kw["font"]) + 26
        w = _px(kw.pop("width", None))
        if w:
            target = max(target, w)
        kw["width"] = max(60, min(560, target))
        kw.setdefault("height", 28)
        kw.setdefault("checkbox_width", 20)
        kw.setdefault("checkbox_height", 20)
        # ttk.Checkbutton 用 variable=；CTkCheckBox 同样用 variable=
        super().__init__(master, **kw)


class Radiobutton(_CTkCompat, ctk.CTkRadioButton):
    """ttk.Radiobutton 的 CTk 替代：统一字体、圆点颜色，并按文字自动定宽。"""

    def __init__(self, master=None, **kw):
        fg = kw.pop("foreground", kw.pop("fg", COLOR["text"]))
        bg = kw.pop("background", kw.pop("bg", None))
        kw.pop("relief", None)
        kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("highlightthickness", None)
        kw.pop("highlightbackground", None)
        kw.pop("highlightcolor", None)
        kw.pop("padx", None)
        kw.pop("pady", None)
        kw.pop("anchor", None)
        kw.pop("cursor", None)
        kw.pop("takefocus", None)
        kw.pop("width", None)

        kw["text_color"] = fg
        kw["fg_color"] = COLOR["accent"]
        kw["hover_color"] = COLOR["accent_soft"]
        kw["border_color"] = COLOR["border_strong"]
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        target = _text_px(kw.get("text", ""), kw["font"]) + 26
        kw["width"] = max(60, min(620, target))
        kw.setdefault("height", 28)
        kw.setdefault("radiobutton_width", 20)
        kw.setdefault("radiobutton_height", 20)
        super().__init__(master, **kw)


class Combobox(_CTkCompat, ctk.CTkComboBox):
    def __init__(self, master=None, **kw):
        fg = kw.pop("foreground", kw.pop("fg", COLOR["text"]))
        bg = kw.pop("background", kw.pop("bg", None))
        kw.pop("relief", None)
        kw.pop("bd", kw.pop("borderwidth", None))
        state = kw.pop("state", "normal")
        kw.pop("postcommand", None)
        kw.pop("exportselection", None)
        kw.pop("justify", None)
        kw.pop("cursor", None)
        kw.pop("takefocus", None)

        kw["text_color"] = fg
        kw["fg_color"] = bg if bg is not None else COLOR["surface_bright"]
        kw["border_color"] = COLOR["border_strong"]
        kw["button_color"] = COLOR["surface_alt"]
        kw["button_hover_color"] = COLOR["border"]
        kw["dropdown_fg_color"] = COLOR["surface"]
        kw["dropdown_hover_color"] = COLOR["accent_soft"]
        kw["dropdown_text_color"] = COLOR["text"]
        kw.setdefault("corner_radius", 6)
        kw.setdefault("height", 32)
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        kw.setdefault("dropdown_font", FONTS["base"])   # 下拉列表选项字号
        # ttk.Combobox 用 textvariable=，CTkComboBox 用 variable=
        if "textvariable" in kw:
            kw["variable"] = kw.pop("textvariable")
        w = _px(kw.pop("width", None))
        if w:
            kw["width"] = w
        kw["state"] = state
        super().__init__(master, **kw)
        self._shrink_dropdown_arrow()

    def _shrink_dropdown_arrow(self, scale=0.55):
        """CTkComboBox 默认箭头按高度 1/3 绘制，视觉上偏大；
        这里包装绘制函数，把箭头缩到约 55%，中心位置保持不变。"""
        try:
            engine = self.draw_engine
            original = engine.draw_dropdown_arrow
            if getattr(original, "_toolbox_scaled", False):
                return

            def draw_dropdown_arrow(x_position, y_position, size):
                return original(x_position, y_position,
                                max(4.0, size * scale))

            draw_dropdown_arrow._toolbox_scaled = True
            engine.draw_dropdown_arrow = draw_dropdown_arrow
            self._draw()
        except Exception:
            pass


class Textbox(_CTkCompat, ctk.CTkTextbox):
    """替代 tkinter.scrolledtext.ScrolledText：自带滚动条。"""
    def __init__(self, master=None, **kw):
        fg = kw.pop("foreground", kw.pop("fg", COLOR["text"]))
        bg = kw.pop("background", kw.pop("bg", COLOR["surface_inset"]))
        kw.pop("insertbackground", None)
        kw.pop("insertofftime", None)
        kw.pop("insertontime", None)
        kw.pop("relief", None)
        kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("highlightthickness", None)
        kw.pop("highlightbackground", None)
        kw.pop("highlightcolor", None)
        kw.pop("spacing1", None)
        kw.pop("spacing2", None)
        kw.pop("spacing3", None)
        kw.pop("exportselection", None)
        kw.pop("undo", None)
        kw.pop("maxundo", None)
        kw.pop("autoseparators", None)
        kw.pop("cursor", None)
        kw.pop("tabs", None)
        kw.pop("padx", None)
        kw.pop("pady", None)
        kw.pop("selectbackground", None)
        kw.pop("selectforeground", None)
        kw.pop("inactiveselectbackground", None)
        kw.pop("takefocus", None)

        kw["text_color"] = fg
        kw["fg_color"] = bg
        kw["border_color"] = COLOR["border"]
        kw.setdefault("border_width", 1)
        kw["scrollbar_button_color"] = COLOR["border_strong"]
        kw["scrollbar_button_hover_color"] = COLOR["text_muted"]
        kw.setdefault("corner_radius", 6)
        if kw.get("font") is None:
            kw["font"] = FONTS["log"]
        # 旧 height 是「行数」，换算成像素
        h = kw.pop("height", None)
        if h:
            kw["height"] = max(40, int(h) * 20)
        w = _px(kw.pop("width", None))
        if w:
            kw["width"] = w
        super().__init__(master, **kw)


class ProgressBar(_CTkCompat, ctk.CTkProgressBar):
    """ttk.Progressbar 的 CTk 替代：保留 maximum / value / mode 与下标读写，
    存量代码 `bar['value'] = n` / `bar.config(maximum=...)` 不用改写。"""

    def __init__(self, master=None, **kw):
        # ttk.Progressbar 的默认 maximum 是 100，这里必须保持一致：
        # 少写 maximum 的调用方会用 0~100 的整数赋 value，按 1 算会被全部截成满格。
        maximum = kw.pop("maximum", 100)
        value = kw.pop("value", 0)
        mode = kw.pop("mode", "determinate")
        kw.pop("orient", None)
        length = kw.pop("length", 200)
        kw.setdefault("height", 10)
        kw.setdefault("corner_radius", 999)
        kw.setdefault("border_width", 0)
        kw.setdefault("progress_color", COLOR["accent"])
        # 槽底用描边灰：surface_alt(#F5F5F5) 在白底卡片上几乎看不见，
        # 0% 时会只剩一个孤立的小圆点，看不出「这是一条进度条」
        kw.setdefault("fg_color", COLOR["border"])
        try:
            kw.setdefault("width", int(length))
        except (TypeError, ValueError):
            pass
        self._maximum = 1.0
        super().__init__(master, **kw)
        self._maximum = max(1e-9, float(maximum or 1))
        if mode == "indeterminate":
            super().configure(mode="indeterminate")
        else:
            super().set(min(1.0, max(0.0, float(value or 0) / self._maximum)))

    # ttk 的 start(interval) 带参数，CTk 的 start() 不带：吞掉多余参数保持兼容
    def start(self, *args, **kwargs):
        return super().start()

    def stop(self):
        return super().stop()

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            return self.cget(cnf)
        if cnf:
            kw.update(cnf)
        if "maximum" in kw:
            self._maximum = max(1e-9, float(kw.pop("maximum") or 1))
        if "value" in kw:
            v = float(kw.pop("value") or 0)
            super().set(min(1.0, max(0.0, v / self._maximum)))
        if "mode" in kw:
            m = kw.pop("mode")
            super().configure(mode="determinate" if m == "determinate" else "indeterminate")
        return super().configure(**kw)

    config = configure

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    def __getitem__(self, key):
        if key == "value":
            return self.get() * self._maximum
        if key == "maximum":
            return self._maximum
        return self.cget(key)


class Scrollbar(_CTkCompat, ctk.CTkScrollbar):
    """ttk.Scrollbar 的 CTk 替代（Treeview / Listbox / Textbox 通用）。

    CTk 没有 Treeview，但滚动条本身有官方控件；ttk 的 orient / command /
    set(first, last) 在这里一一对应，存量调用方不用改写。
    """

    def __init__(self, master=None, **kw):
        orient = kw.pop("orient", kw.pop("orientation", "vertical"))
        kw.pop("style", None)
        kw.pop("cursor", None)
        kw.pop("takefocus", None)
        kw.pop("class_", None)
        kw.pop("width", None)          # ttk 的字符宽度对滚动条没意义
        if str(orient).lower().startswith("h"):
            kw["orientation"] = "horizontal"
            kw.setdefault("height", 12)
        else:
            kw["orientation"] = "vertical"
            kw.setdefault("width", 12)
        kw.setdefault("corner_radius", 7)
        kw.setdefault("border_spacing", 2)
        kw.setdefault("fg_color", COLOR["surface_alt"])
        kw.setdefault("button_color", COLOR["border_strong"])
        kw.setdefault("button_hover_color", COLOR["text_muted"])
        super().__init__(master, **kw)

    def get(self):
        """ttk 兼容：返回 (first, last) 可见区间。"""
        try:
            return (self._start_value, self._end_value)
        except Exception:
            return (0.0, 1.0)


class Slider(_CTkCompat, ctk.CTkSlider):
    """ttk.Scale 的 CTk 替代：from_ / to / orient / variable / command 一一对应。

    CTkSlider 内部对 IntVar 会做 round() 处理，所以质量滑条这类 IntVar
    不需要改代码。
    """

    def __init__(self, master=None, **kw):
        frm = kw.pop("from_", kw.pop("from", 0))
        to = kw.pop("to", 100)
        orient = kw.pop("orient", kw.pop("orientation", "horizontal"))
        for dead in ("length", "style", "showvalue", "takefocus", "cursor",
                     "resolution", "digits", "sliderlength", "sliderrelief"):
            kw.pop(dead, None)
        kw["from_"] = frm
        kw["to"] = to
        kw["orientation"] = ("horizontal" if str(orient).lower().startswith("h")
                             else "vertical")
        kw.setdefault("height", 16)
        kw.setdefault("fg_color", COLOR["surface_alt"])
        kw.setdefault("progress_color", COLOR["accent"])
        kw.setdefault("button_color", COLOR["accent"])
        kw.setdefault("button_hover_color", COLOR["accent_hover"])
        super().__init__(master, **kw)


def make_card(parent, title="", padding=(10, 8)):
    """白底描边卡片：标题 + 内容区（CTk 没有 LabelFrame，用卡片代替）。

    返回 (卡片, 内容区)：卡片负责 pack/grid，内容区当父容器放子控件，
    这样内部用 pack 还是 grid 都不冲突（ttk.LabelFrame 的标题是画在边框上的，
    CTk 里改成一行标题更清爽）。
    """
    px, py = padding
    outer = Frame(parent, fg_color=COLOR["surface"], border_width=1,
                  border_color=COLOR["border"], corner_radius=8)
    if title:
        Label(outer, text=title, font=FONTS["sm"], auto_wrap=False,
              foreground=COLOR["text_secondary"]).pack(
            anchor="w", fill="x", padx=px, pady=(py, 0))
    body = Frame(outer, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=px,
              pady=(4 if title else py, py))
    return outer, body


class ScrollableFrame(_CTkCompat, ctk.CTkScrollableFrame):
    def __init__(self, master=None, **kw):
        bg = kw.pop("background", kw.pop("bg", None))
        kw.pop("relief", None)
        kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("highlightthickness", None)
        kw.pop("highlightbackground", None)
        kw.pop("highlightcolor", None)
        kw.pop("takefocus", None)
        kw.pop("confine", None)
        kw.pop("cursor", None)
        kw.pop("xscrollcommand", None)
        kw.pop("yscrollcommand", None)
        kw.pop("xscrollincrement", None)
        kw.pop("yscrollincrement", None)
        kw.pop("scrollregion", None)
        kw.pop("offset", None)
        kw.pop("closeenough", None)

        # 导航栏必须有明确的白色底：透明容器会让内部透明 Label 取不到正确底色，
        # 从而出现灰色文字块（CTk 的透明色探测会回退到根窗口颜色）。
        kw["fg_color"] = bg if bg is not None else COLOR["surface"]
        kw["bg_color"] = COLOR["nav_bg"]
        kw.setdefault("corner_radius", 0)
        kw.setdefault("border_width", 0)
        kw.setdefault("scrollbar_fg_color", COLOR["surface"])
        kw.setdefault("scrollbar_button_color", COLOR["border"])
        kw.setdefault("scrollbar_button_hover_color", COLOR["border_strong"])
        super().__init__(master, **kw)


def virtual_screen_bounds():
    """整个虚拟桌面的范围 (x, y, w, h)：多显示器时可能带负坐标，
    只用 winfo_screenwidth 会把副屏上的窗口拽回主屏。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        x = int(u.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
        y = int(u.GetSystemMetrics(77))   # SM_YVIRTUALSCREEN
        w = int(u.GetSystemMetrics(78))   # SM_CXVIRTUALSCREEN
        h = int(u.GetSystemMetrics(79))   # SM_CYVIRTUALSCREEN
        if w > 0 and h > 0:
            return x, y, w, h
    except Exception:
        pass
    try:
        r = tk._default_root
        if r is not None:
            return 0, 0, int(r.winfo_screenwidth()), int(r.winfo_screenheight())
    except Exception:
        pass
    return 0, 0, 1920, 1080


def center_window(win, parent=None, width=None, height=None):
    """把 win 居中到 parent 所在窗口，并保证整窗落在屏幕内。

    · 尺寸优先用调用方给的 width/height，否则取「布局请求尺寸」与「当前尺寸」
      的较大值（CTk 控件高度延迟生效，只用 winfo_height 会算出偏小的框，
      导致窗口下半截跑到屏幕外）；
    · 没有任何 parent（或 parent 还没显示）时退回屏幕居中。
    返回实际使用的 (x, y, w, h)。
    """
    try:
        win.update_idletasks()
    except Exception:
        pass
    try:
        w = int(width or 0)
    except (TypeError, ValueError):
        w = 0
    try:
        h = int(height or 0)
    except (TypeError, ValueError):
        h = 0
    # 还没显示的窗口不能用 winfo_width/height（CTkToplevel 默认 200x200，
    # 会把「按内容自适应」的弹窗撑成固定 200 高，底部留一大片空白）
    try:
        req_w, req_h = int(win.winfo_reqwidth()), int(win.winfo_reqheight())
    except Exception:
        req_w, req_h = 320, 160
    try:
        mapped = bool(win.winfo_ismapped())
    except Exception:
        mapped = False
    cur_w, cur_h = (req_w, req_h)
    if mapped:
        try:
            cur_w = max(req_w, int(win.winfo_width()))
            cur_h = max(req_h, int(win.winfo_height()))
        except Exception:
            pass
    w = max(w, cur_w)
    h = max(h, cur_h)

    ref = parent if parent is not None else getattr(win, "master", None)
    try:
        ref = ref.winfo_toplevel() if ref is not None else None
    except Exception:
        ref = None

    placed = False
    if ref is not None and ref is not win:
        try:
            if ref.winfo_viewable() and ref.winfo_width() > 1:
                rx, ry = ref.winfo_rootx(), ref.winfo_rooty()
                x = rx + (ref.winfo_width() - w) // 2
                y = ry + (ref.winfo_height() - h) // 2
                placed = True
        except Exception:
            placed = False

    vx, vy, vw, vh = virtual_screen_bounds()
    if not placed:
        x = vx + (vw - w) // 2
        y = vy + (vh - h) // 2
    # 窗口比屏幕还大时贴左上角，否则夹到屏幕内（避免标题栏跑到屏幕外）
    x = vx if w >= vw else max(vx, min(vx + vw - w, x))
    y = vy if h >= vh else max(vy, min(vy + vh - h, y))
    try:
        win.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        pass
    return x, y, w, h


class Toplevel(_CTkCompat, ctk.CTkToplevel):
    """弹窗基类：第一次显示时自动居中到所属窗口。

    以往各处 `geometry("1200x700")` 不带坐标，位置由 Windows 决定，
    弹窗常常出现在屏幕左上角、盖住左侧导航，看起来像「跑偏了」。
    统一在这里做一次性居中，调用方不必逐处改。
    """

    def __init__(self, master=None, **kw):
        kw.pop("relief", None)
        kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("highlightthickness", None)
        kw.pop("highlightbackground", None)
        kw.pop("highlightcolor", None)
        kw.pop("cursor", None)
        kw.pop("takefocus", None)
        kw.pop("class_", None)
        kw.pop("container", None)
        kw.pop("menu", None)
        kw.pop("screen", None)
        kw.pop("use", None)
        kw.pop("visual", None)
        kw.pop("colormap", None)
        bg = kw.pop("background", kw.pop("bg", None))
        if bg is not None and "fg_color" not in kw:
            kw["fg_color"] = bg
        kw.setdefault("fg_color", COLOR["bg"])
        self._autocenter_done = False
        super().__init__(master, **kw)
        try:
            self.bind("<Map>", self._on_first_map, add="+")
        except Exception:
            pass

    def _on_first_map(self, _event=None):
        if self._autocenter_done or getattr(self, "_no_autocenter", False):
            return
        self._autocenter_done = True
        try:
            self.after_idle(lambda: center_window(self))
        except Exception:
            pass

    def configure(self, **kw):
        # 旧式 bg/background → CTk 的 fg_color
        bg = kw.pop("background", kw.pop("bg", None))
        if bg is not None and "fg_color" not in kw:
            kw["fg_color"] = bg
        return super().configure(**kw)

    config = configure


class Listbox(tk.Listbox):
    """带统一主题的 Listbox（CTk 没有等价件，这里补齐字体 / 选中色 / 描边）。"""

    def __init__(self, master=None, **kw):
        kw.setdefault("font", FONTS["base"])
        kw.setdefault("background", COLOR["surface_bright"])
        kw.setdefault("foreground", COLOR["text"])
        kw.setdefault("selectbackground", COLOR["accent_soft"])
        kw.setdefault("selectforeground", COLOR["text"])
        kw.setdefault("activestyle", "none")
        kw.setdefault("exportselection", False)
        kw.setdefault("borderwidth", 0)
        kw.setdefault("relief", "flat")
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightbackground", COLOR["border"])
        kw.setdefault("highlightcolor", COLOR["accent"])
        kw.setdefault("takefocus", True)
        super().__init__(master, **kw)


# ============================================================
# 透传：CustomTkinter 确实没有等价件的控件（尽量少）
# ============================================================
# 表格 / 树：CTk 没有 Treeview，只能用 ttk（外观由 theme.apply_theme 统一）
Treeview = ttk.Treeview
Style = ttk.Style
Separator = ttk.Separator
Notebook = ttk.Notebook
# 兼容旧写法：这层名字都指向 CTk 实现，不要再指回 ttk
Progressbar = ProgressBar          # 旧代码 bar_all = ttk.Progressbar(...)
Scale = Slider                     # 旧代码 ttk.Scale(from_=.., to=..)

# 供入口创建根窗口
CTk = ctk.CTk

__all__ = [
    "Button", "StepButton", "Label", "Frame", "Entry", "Checkbutton", "Radiobutton",
    "Combobox", "Textbox", "Listbox", "ScrollableFrame", "Toplevel", "CTk",
    "ProgressBar", "Progressbar", "Scrollbar", "Slider", "Scale", "make_card",
    "center_window", "virtual_screen_bounds",
    "Treeview", "Style", "Separator", "Notebook", "init_appearance", "ctk",
]
