# -*- coding: utf-8 -*-
"""文件工具箱 · 统一设计系统（Design Tokens + 白底黑字浅色主题）
============================================================
集中管理颜色 / 字体 / 间距 / 圆角令牌，并把它们落地为 ttk 主题与
可复用的组件样式。所有界面元素从这里取色，杜绝散落的硬编码值。

风格方向：白底黑字（简洁高对比浅色）
  · 窗口 / 卡片 / 按钮面全部纯白 #FFFFFF，仅用发丝描边区分层次
  · 主文字纯黑 #000000；悬停/表头用极浅灰 #F5F5F5 点缀（非灰底大块）
  · 官方蓝强调色 #3B8ED0；语义色成功/警告/错误（浅底可读档）
  · 字号：导航 19 > 正文 16，层级清晰
"""
import os
import sys
import tkinter as tk
from tkinter import ttk

# ============================================================
# 1. 颜色令牌（Color Tokens）—— 白底黑字
# ============================================================
COLOR = {
    # 背景层（全部纯白，靠描边分区）
    "bg":            "#FFFFFF",   # 应用主背景
    "surface":       "#FFFFFF",   # 卡片 / 面板表面
    "surface_bright": "#FFFFFF",  # 亮表面（次级按钮底、表格行）
    "surface_inset": "#FFFFFF",   # 内嵌表面（日志框）
    "surface_alt":   "#F5F5F5",   # 交互点缀（悬停态、表头、选中底——极浅灰）
    "nav_bg":        "#FFFFFF",   # 左侧导航栏背景（与窗口同色，靠选中态分区）
    "nav_item_hover":"#F0F0F0",   # 导航项悬停

    # 描边 / 分隔（发丝线，非灰色块）
    "border":        "#E0E0E0",   # 常规描边
    "border_strong": "#C8C8C8",   # 强调描边 / 输入框描边

    # 文字
    "text":           "#000000",  # 主文字（纯黑）
    "text_secondary": "#444444",  # 次要文字 / 说明
    "text_muted":     "#888888",  # 占位 / 禁用
    "text_on_accent": "#FFFFFF",  # 强调色上的文字

    # 强调色（官方浅色蓝）
    "accent":         "#3B8ED0",
    "accent_hover":   "#36719F",
    "accent_active":  "#325882",
    "accent_soft":    "#D5E8F7",  # 强调色淡底（选中 / 标签 / 激活项）

    # 状态栏
    "status_bg":      "#FFFFFF",

    # 语义色（浅底可读的亮度档）
    "success":        "#1A7F37",
    "success_active": "#116329",
    "warning":        "#9A6700",
    "warning_active": "#7D4E00",
    "error":          "#CF222E",
    "error_active":   "#A40E26",
    "info":           "#0969DA",
}

# ============================================================
# 2. 字体令牌（Typography）
# ============================================================
FONT_FAMILY = "Microsoft YaHei"
FONT_MONO = "Consolas"


def font(size=12, weight="normal"):
    """统一字体工厂：尺寸 + 字重，家族固定为 Microsoft YaHei。"""
    return (FONT_FAMILY, size, weight)


FONTS = {
    "xs":          font(14),
    "sm":          font(15),
    "base":        font(16),
    "md":          font(17),
    "lg":          font(19),
    "xl":          font(22, "bold"),
    "title":       font(24, "bold"),
    "nav_title":   font(19, "bold"),
    "nav_group":   font(14, "bold"),  # 导航分组小标题
    "nav":         font(19),          # 导航项：比内容正文(base 16)大四号
    "nav_active":  font(19, "bold"),
    "page_title":  font(22, "bold"),  # 内容页标题
    "table":       font(14),          # 表格正文（正文 16 → 14）
    "table_head":  font(13, "bold"),  # 表格表头（原 15 → 13）
    "log":         (FONT_MONO, 13),
}

# ------------------------------------------------------------
# 全局缩放（DPI）
# ------------------------------------------------------------
_SCALE_CACHE = None


def dpi_scale():
    """系统缩放系数：96 DPI = 1.0，4K@200% = 2.0。

    为什么要自己算一个统一的系数：
      · 进程按「系统 DPI 感知」启动（main.py 里 SetProcessDpiAwareness(1)），
        所以整机只有一个缩放比例，不需要逐屏换算；
      · CTk 的控件尺寸和字号都以「设计像素」计，启动时把 widget/window
        scaling 设成这个系数，控件与窗口会一起等比放大；
      · ttk / tk 原生控件（Treeview、Listbox、Menu…）没有这套机制，
        点数字号会被 Tk 按 DPI 再放大一次 —— 于是高分屏上出现
        「表格字比按钮大一倍」。这类控件统一用 ttk_font() 换成像素字号。

    可用环境变量 TOOLBOX_UI_SCALE=1.5 强制指定（高分屏显示异常时的兜底）。
    """
    global _SCALE_CACHE
    if _SCALE_CACHE is not None:
        return _SCALE_CACHE
    value = None
    env = (os.environ.get("TOOLBOX_UI_SCALE") or "").strip()
    if env:
        try:
            value = float(env)
        except ValueError:
            value = None
    if value is None:
        try:
            root = tk._default_root
            if root is not None:
                # 1 英寸有多少像素（已含系统缩放）
                value = float(root.winfo_fpixels("1i")) / 96.0
        except Exception:
            value = None
    if value is None and sys.platform.startswith("win"):
        try:
            import ctypes
            value = float(ctypes.windll.user32.GetDpiForSystem()) / 96.0
        except Exception:
            value = None
    if not value or value <= 0:
        value = 1.0
    _SCALE_CACHE = max(1.0, min(3.0, value))
    return _SCALE_CACHE


def reset_dpi_scale():
    """清掉缩放缓存（换了缩放设置 / 测试时用）。"""
    global _SCALE_CACHE
    _SCALE_CACHE = None


def ui_scale():
    """当前界面缩放系数（96 DPI = 1.0）。表格行高、列宽、弹窗留白都按它放大。"""
    return dpi_scale()


def ttk_font(key="base"):
    """把 FONTS 令牌换算成 ttk/tk 可用的**像素字号**（负数字号 = 像素）。

    换算时保留原来点数字号的视觉大小（1pt = 96/72 px），再乘系统缩放；
    这样 ttk 控件和 CTk 控件的字不会被 DPI 重复放大。
    """
    desc = FONTS.get(key)
    if not isinstance(desc, (tuple, list)) or len(desc) < 2:
        desc = FONTS["base"]
    family, size = desc[0], desc[1]
    try:
        px = int(round(float(size) * 96.0 / 72.0 * dpi_scale()))
    except (TypeError, ValueError):
        px = 16
    return (family, -max(1, px)) + tuple(desc[2:])


def option_font(key="base"):
    """option database 用的 Tcl 字体描述：字体名带空格要加花括号。"""
    desc = ttk_font(key)
    return " ".join([f"{{{desc[0]}}}"] + [str(x) for x in desc[1:]])

# ============================================================
# 3. 间距 / 圆角（Spacing / Radius，4px 基准）
# ============================================================
SPC = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 20,
       "2xl": 24, "3xl": 32, "4xl": 40}
RADIUS = {"sm": 4, "md": 6, "lg": 10, "pill": 999}

# ============================================================
# 4. ttk 主题落地
# ============================================================
def apply_theme(root):
    """把设计令牌写入 ttk 主题（基于 clam，跨平台可控），并设定应用底色。
    须在创建任何 ttk 控件之前调用。白底黑字浅色主题。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    # 全局基线（clam 会向下继承）
    style.configure(".",
        background=COLOR["bg"],
        foreground=COLOR["text"],
        font=ttk_font("base"))

    # 原生 tk 控件（Listbox / Text / Menu）的默认字体与配色：
    # 这些控件没有 CTk 等价件，用 option database 统一，避免出现系统默认字体。
    # 字号用「像素字号」并已乘上系统缩放：点数字号会被 Tk 按 DPI 二次放大，
    # 高分屏上会明显比 CTk 控件里的字大一截。
    _base_font = option_font("base")
    for pattern, value in (
        ("*Font", _base_font),
        ("*Listbox.font", _base_font),
        ("*Listbox.background", COLOR["surface_bright"]),
        ("*Listbox.foreground", COLOR["text"]),
        ("*Listbox.selectBackground", COLOR["accent_soft"]),
        ("*Listbox.selectForeground", COLOR["text"]),
        ("*Listbox.highlightThickness", 1),
        ("*Listbox.highlightBackground", COLOR["border"]),
        ("*Listbox.highlightColor", COLOR["accent"]),
        ("*Listbox.borderWidth", 0),
        ("*Listbox.relief", "flat"),
        ("*Listbox.activestyle", "none"),
        ("*Text.font", _base_font),
        ("*Text.background", COLOR["surface_bright"]),
        ("*Text.foreground", COLOR["text"]),
        ("*Text.insertBackground", COLOR["text"]),
        ("*Text.selectBackground", COLOR["accent_soft"]),
        ("*Text.selectForeground", COLOR["text"]),
        ("*Text.highlightThickness", 0),
        ("*Text.borderWidth", 0),
        ("*Text.relief", "flat"),
        ("*Text.padx", 8),
        ("*Text.pady", 6),
        ("*Menu.font", _base_font),
        ("*Menu.background", COLOR["surface_bright"]),
        ("*Menu.foreground", COLOR["text"]),
        ("*Menu.activeBackground", COLOR["accent_soft"]),
        ("*Menu.activeForeground", COLOR["text"]),
        ("*Menu.borderWidth", 0),
        ("*Menu.relief", "flat"),
    ):
        try:
            root.option_add(pattern, value)
        except Exception:
            pass

    style.configure("TFrame", background=COLOR["bg"])
    style.configure("Nav.TFrame", background=COLOR["nav_bg"])

    style.configure("TLabel",
        background=COLOR["bg"], foreground=COLOR["text"], font=ttk_font("base"))

    style.configure("TLabelframe",
        background=COLOR["bg"], foreground=COLOR["text_secondary"],
        bordercolor=COLOR["border"], relief="groove", borderwidth=1)
    style.configure("TLabelframe.Label",
        background=COLOR["bg"], foreground=COLOR["text_secondary"],
        font=ttk_font("sm"), padding=(6, 0))

    # 次要按钮（如「浏览…」）
    style.configure("TButton",
        background=COLOR["surface_bright"], foreground=COLOR["text"],
        bordercolor=COLOR["border_strong"], darkcolor=COLOR["surface_bright"],
        lightcolor=COLOR["surface_bright"], relief="solid", borderwidth=1,
        font=ttk_font("base"), padding=(10, 4))
    style.map("TButton",
        background=[("active", COLOR["surface_alt"]), ("pressed", COLOR["border"])],
        foreground=[("disabled", COLOR["text_muted"])])

    # 输入框
    style.configure("TEntry",
        fieldbackground=COLOR["surface_bright"], foreground=COLOR["text"],
        bordercolor=COLOR["border_strong"], lightcolor=COLOR["surface_bright"],
        insertcolor=COLOR["text"], relief="solid", borderwidth=1,
        font=ttk_font("base"), padding=(8, 4))
    style.map("TEntry",
        bordercolor=[("focus", COLOR["accent"])],
        fieldbackground=[("readonly", COLOR["surface_inset"])])

    style.configure("TCombobox",
        fieldbackground=COLOR["surface_bright"], foreground=COLOR["text"],
        arrowcolor=COLOR["text"], bordercolor=COLOR["border_strong"],
        relief="solid", borderwidth=1, font=ttk_font("base"), padding=(8, 4))
    style.map("TCombobox",
        fieldbackground=[("readonly", COLOR["surface_bright"])],
        foreground=[("disabled", COLOR["text_muted"])])

    style.configure("TSpinbox",
        fieldbackground=COLOR["surface_bright"], foreground=COLOR["text"],
        arrowcolor=COLOR["text"], bordercolor=COLOR["border_strong"],
        background=COLOR["surface"], relief="solid", borderwidth=1,
        font=ttk_font("base"), padding=(6, 3))
    style.map("TSpinbox",
        bordercolor=[("focus", COLOR["accent"])],
        foreground=[("disabled", COLOR["text_muted"])])

    # 勾选框 / 单选框
    style.configure("TCheckbutton",
        background=COLOR["bg"], foreground=COLOR["text"], font=ttk_font("base"),
        padding=(2, 2), focuscolor=COLOR["bg"])
    style.map("TCheckbutton", background=[("active", COLOR["bg"])])

    style.configure("TRadiobutton",
        background=COLOR["bg"], foreground=COLOR["text"], font=ttk_font("base"),
        padding=(2, 2), indicatorcolor=COLOR["surface_bright"],
        focuscolor=COLOR["bg"])
    style.map("TRadiobutton",
        background=[("active", COLOR["bg"])],
        indicatorcolor=[("selected", COLOR["accent"]),
                        ("!selected", COLOR["surface_bright"])],
        foreground=[("disabled", COLOR["text_muted"])])

    # 滑条（图片压缩选项）
    style.configure("TScale",
        background=COLOR["bg"], troughcolor=COLOR["surface_alt"],
        bordercolor=COLOR["border"], lightcolor=COLOR["accent"],
        darkcolor=COLOR["accent"], sliderlength=18, sliderrelief="flat",
        borderwidth=0)

    # 下拉菜单按钮 / 分隔线
    style.configure("TMenubutton",
        background=COLOR["surface_bright"], foreground=COLOR["text"],
        bordercolor=COLOR["border_strong"], relief="solid", borderwidth=1,
        font=ttk_font("base"), padding=(8, 4))
    style.map("TMenubutton",
        background=[("active", COLOR["surface_alt"])])
    style.configure("TSeparator", background=COLOR["border"])

    # 笔记本（如有）
    style.configure("TNotebook", background=COLOR["bg"], bordercolor=COLOR["border"])
    style.configure("TNotebook.Tab",
        background=COLOR["surface"], foreground=COLOR["text_secondary"],
        padding=(12, 6), font=ttk_font("base"))
    style.map("TNotebook.Tab",
        background=[("selected", COLOR["accent"])],
        foreground=[("selected", COLOR["text_on_accent"])])

    # 表格（硬盘哈希扫描等）
    # 行高 / 表头内边距跟着系统缩放走：高分屏下同号字实际像素更高，
    # 固定行高会把文字上下裁掉。
    _scale = ui_scale()
    _row_h = max(30, int(round(30 * _scale)))
    _head_pad = (max(6, int(round(6 * _scale))),
                 max(3, int(round(3 * _scale))))
    style.configure("Treeview",
        background=COLOR["surface_bright"], foreground=COLOR["text"],
        fieldbackground=COLOR["surface_bright"], bordercolor=COLOR["border"],
        rowheight=_row_h, font=ttk_font("table"))
    style.configure("Treeview.Heading",
        background=COLOR["surface_alt"], foreground=COLOR["text"],
        font=ttk_font("table_head"), relief="flat", borderwidth=1,
        padding=_head_pad)
    style.map("Treeview",
        background=[("selected", COLOR["accent"])],
        foreground=[("selected", COLOR["text_on_accent"])])
    style.map("Treeview.Heading", background=[("active", COLOR["border"])])

    # 滚动条
    style.configure("TScrollbar",
        background=COLOR["surface_bright"], troughcolor=COLOR["bg"],
        bordercolor=COLOR["bg"], arrowcolor=COLOR["text_muted"], relief="flat")
    style.map("TScrollbar",
        background=[("active", COLOR["surface_alt"])])

    # 进度条
    style.configure("TProgressbar",
        background=COLOR["accent"], troughcolor=COLOR["surface_alt"],
        bordercolor=COLOR["bg"], lightcolor=COLOR["accent"],
        darkcolor=COLOR["accent"])

    # 应用底色：CTk 根窗口必须设置 fg_color，透明子控件的颜色探测才能取到白色
    try:
        root.configure(fg_color=COLOR["bg"])
    except Exception:
        pass
    try:
        root.configure(background=COLOR["bg"])
    except Exception:
        pass
    return style


# ============================================================
# 5. 复用组件
# ============================================================
# 所有可复用控件（按钮 / 输入框 / 勾选框 / 卡片 / 进度条 / 滚动条 …）都在
# toolbox/ui.py 里以 CTk 子类实现，这里只保留设计令牌与 ttk 兜底样式
# （ttk 样式仅服务于 CustomTkinter 没有等价件的 Treeview 表格）。
