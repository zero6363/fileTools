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
from tkinter import ttk

import customtkinter as ctk
from .theme import COLOR, FONTS

# CustomTkinter 默认外观：浅色模式（与现有设计系统一致）
ctk.set_appearance_mode("light")

# 字符宽度 → 像素 的近似换算（CTk 的 width/height 以像素计，旧 ttk 以字符计）
_CHAR_PX = 7


def _px(n, scale=_CHAR_PX, minimum=0):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return max(minimum, n * scale)


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
        height = kw.pop("height", None)   # 旧 ttk 的 height(行) 忽略，交给 CTk 自适应

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
        kw.setdefault("corner_radius", 8)
        # 仅当调用方显式给了宽度时换算；否则 CTk 按文字自适应，尺寸更统一
        w = _px(kw.pop("width", None))
        if w:
            kw["width"] = w
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        super().__init__(master, **kw)


class Label(_CTkCompat, ctk.CTkLabel):
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

        kw["text_color"] = fg
        if bg is not None:
            kw["fg_color"] = bg
        else:
            kw["fg_color"] = "transparent"
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        super().__init__(master, **kw)


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
            kw["fg_color"] = "transparent"
        kw["bg_color"] = COLOR["bg"]
        if bd not in (None, 0):
            kw["border_width"] = int(bd)
            kw.setdefault("border_color", COLOR["border"])
        kw.setdefault("corner_radius", 10)
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
        kw.pop("justify", None)
        kw.pop("exportselection", None)
        kw.pop("cursor", None)

        kw["text_color"] = fg
        if bg is not None:
            kw["fg_color"] = bg
        kw.setdefault("border_color", COLOR["border"])
        kw.setdefault("corner_radius", 6)
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        w = _px(kw.pop("width", None))
        if w:
            kw["width"] = w
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
        # ttk.Checkbutton 用 variable=；CTkCheckBox 同样用 variable=
        super().__init__(master, **kw)


class Combobox(_CTkCompat, ctk.CTkComboBox):
    def __init__(self, master=None, **kw):
        fg = kw.pop("foreground", kw.pop("fg", COLOR["text"]))
        bg = kw.pop("background", kw.pop("bg", None))
        kw.pop("relief", None)
        kw.pop("bd", kw.pop("borderwidth", None))
        kw.pop("state", None)  # 重新收集
        state = kw.get("state", "normal")
        kw.pop("postcommand", None)
        kw.pop("exportselection", None)
        kw.pop("justify", None)
        kw.pop("cursor", None)
        kw.pop("height", None)
        kw.pop("takefocus", None)

        kw["text_color"] = fg
        if bg is not None:
            kw["fg_color"] = bg
        kw["border_color"] = COLOR["border"]
        kw["button_color"] = COLOR["border_strong"]
        kw["button_hover_color"] = COLOR["text_muted"]
        kw["dropdown_fg_color"] = COLOR["surface"]
        kw["dropdown_hover_color"] = COLOR["accent_soft"]
        kw["dropdown_text_color"] = COLOR["text"]
        kw.setdefault("corner_radius", 6)
        if kw.get("font") is None:
            kw["font"] = FONTS["base"]
        # ttk.Combobox 用 textvariable=，CTkComboBox 用 variable=
        if "textvariable" in kw:
            kw["variable"] = kw.pop("textvariable")
        w = _px(kw.pop("width", None))
        if w:
            kw["width"] = w
        kw["state"] = state
        super().__init__(master, **kw)


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
        kw.pop("wrap", None)
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
        kw["scrollbar_button_color"] = COLOR["border_strong"]
        kw["scrollbar_button_hover_color"] = COLOR["text_muted"]
        kw.setdefault("corner_radius", 6)
        if kw.get("font") is None:
            kw["font"] = FONTS["log"]
        # 旧 height 是「行数」，换算成像素
        h = kw.pop("height", None)
        if h:
            kw["height"] = max(40, int(h) * 18)
        w = _px(kw.pop("width", None))
        if w:
            kw["width"] = w
        super().__init__(master, **kw)


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

        if bg is not None:
            kw["fg_color"] = bg
        else:
            kw["fg_color"] = "transparent"
        kw["bg_color"] = COLOR["nav_bg"]
        kw.setdefault("corner_radius", 0)
        kw.setdefault("border_width", 0)
        kw.setdefault("scrollbar_button_color", COLOR["border_strong"])
        kw.setdefault("scrollbar_button_hover_color", COLOR["text_muted"])
        super().__init__(master, **kw)


class Toplevel(_CTkCompat, ctk.CTkToplevel):
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
        super().__init__(master, **kw)

    def configure(self, **kw):
        # 旧式 bg/background → CTk 的 fg_color
        bg = kw.pop("background", kw.pop("bg", None))
        if bg is not None and "fg_color" not in kw:
            kw["fg_color"] = bg
        return super().configure(**kw)

    config = configure


# ============================================================
# 透传：其余 ttk 控件保持原样（CTk 无等价件）
# ============================================================
Treeview = ttk.Treeview
Scrollbar = ttk.Scrollbar
Style = ttk.Style
Progressbar = ttk.Progressbar
Separator = ttk.Separator
Notebook = ttk.Notebook
Labelframe = ttk.Labelframe
Menubutton = ttk.Menubutton
Panedwindow = ttk.Panedwindow
Sizegrip = ttk.Sizegrip
# 兼容旧代码中的 Tkinter 命名（与 CTk 风格小写并存）
LabelFrame = ttk.LabelFrame
Radiobutton = ttk.Radiobutton
Spinbox = ttk.Spinbox
Scale = ttk.Scale

# 供入口创建根窗口
CTk = ctk.CTk

__all__ = [
    "Button", "Label", "Frame", "Entry", "Checkbutton", "Combobox",
    "Textbox", "ScrollableFrame", "Toplevel", "CTk",
    "Treeview", "Scrollbar", "Style", "Progressbar", "Separator",
    "Notebook", "Labelframe", "Menubutton", "Panedwindow", "Sizegrip",
    "init_appearance", "ctk",
]
