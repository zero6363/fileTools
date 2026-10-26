# -*- coding: utf-8 -*-
"""文件工具箱 · 统一设计系统（Design Tokens + CustomTkinter 官方浅色主题）
============================================================
集中管理颜色 / 字体 / 间距 / 圆角令牌，并把它们落地为 ttk 主题与
可复用的组件样式。所有界面元素从这里取色，杜绝散落的硬编码值。

风格方向：CustomTkinter 官方示例同款浅色（白天模式）
  · 窗口 #EBEBEB + 卡片 #DBDBDB + 输入框/按钮面 #F9F9FA + 日志内嵌 #F9F9FA
  · 官方蓝强调色 #3B8ED0（悬停 #36719F），文字 #1A1A1A
  · 圆角卡片、轻描边、扁平按钮、清晰层级
  · 语义色：成功 / 警告 / 错误（GitHub Light 亮度档，浅底可读）
"""
import tkinter as tk
from tkinter import ttk

# ============================================================
# 1. 颜色令牌（Color Tokens）—— 与 CTk 官方 blue.json 浅色值对齐
# ============================================================
COLOR = {
    # 背景层
    "bg":            "#EBEBEB",   # 应用主背景（CTk gray92）
    "surface":       "#DBDBDB",   # 卡片 / 面板表面（CTk gray86）
    "surface_bright": "#F9F9FA",  # 亮表面（次级按钮底、表格行，CTk 官方值）
    "surface_inset": "#F9F9FA",   # 内嵌表面（日志框，浅色下即近白）
    "surface_alt":   "#CFCFCF",   # 次级表面（悬停态、表头，CTk gray81）
    "nav_bg":        "#EBEBEB",   # 左侧导航栏背景（与窗口同色，靠选中态分区）
    "nav_item_hover":"#CFCFCF",   # 导航项悬停

    # 描边 / 分隔
    "border":        "#A6A6A6",   # 常规描边（CTk gray65）
    "border_strong": "#979DA2",   # 强调描边 / 输入框描边（CTk 官方值）

    # 文字
    "text":           "#1A1A1A",  # 主文字（CTk gray10）
    "text_secondary": "#57606A",  # 次要文字 / 说明
    "text_muted":     "#8C959F",  # 占位 / 禁用
    "text_on_accent": "#DCE4EE",  # 强调色上的文字（CTk 官方值）

    # 强调色（CTk 官方浅色蓝）
    "accent":         "#3B8ED0",
    "accent_hover":   "#36719F",
    "accent_active":  "#325882",
    "accent_soft":    "#CDE1F5",  # 强调色淡底（选中 / 标签 / 激活项）

    # 状态栏
    "status_bg":      "#E3E3E3",

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
    "xs":          font(11),
    "sm":          font(12),
    "base":        font(13),
    "md":          font(14),
    "lg":          font(16),
    "xl":          font(19, "bold"),
    "title":       font(21, "bold"),
    "nav_title":   font(16, "bold"),
    "nav":         font(14),          # 导航项：比内容正文(base 13)大一号
    "nav_active":  font(14, "bold"),
    "log":         (FONT_MONO, 10),
}

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
    须在创建任何 ttk 控件之前调用。浅色主题（CTk 官方示例同款，白天模式）。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    # 全局基线（clam 会向下继承）
    style.configure(".",
        background=COLOR["bg"],
        foreground=COLOR["text"],
        font=FONTS["base"])

    style.configure("TFrame", background=COLOR["bg"])
    style.configure("Nav.TFrame", background=COLOR["nav_bg"])

    style.configure("TLabel",
        background=COLOR["bg"], foreground=COLOR["text"], font=FONTS["base"])

    style.configure("TLabelframe",
        background=COLOR["bg"], foreground=COLOR["text_secondary"],
        bordercolor=COLOR["border"], relief="groove", borderwidth=1)
    style.configure("TLabelframe.Label",
        background=COLOR["bg"], foreground=COLOR["text_secondary"], font=FONTS["sm"])

    # 次要按钮（如「浏览…」）
    style.configure("TButton",
        background=COLOR["surface_bright"], foreground=COLOR["text"],
        bordercolor=COLOR["border_strong"], darkcolor=COLOR["surface_bright"],
        lightcolor=COLOR["surface_bright"], relief="solid", borderwidth=1,
        font=FONTS["base"], padding=(10, 4))
    style.map("TButton",
        background=[("active", COLOR["surface_alt"]), ("pressed", COLOR["border"])],
        foreground=[("disabled", COLOR["text_muted"])])

    # 输入框
    style.configure("TEntry",
        fieldbackground=COLOR["surface_bright"], foreground=COLOR["text"],
        bordercolor=COLOR["border_strong"], lightcolor=COLOR["surface_bright"],
        insertcolor=COLOR["text"], relief="solid", borderwidth=1, padding=(6, 3))
    style.map("TEntry",
        bordercolor=[("focus", COLOR["accent"])],
        fieldbackground=[("readonly", COLOR["surface_inset"])])

    style.configure("TCombobox",
        fieldbackground=COLOR["surface_bright"], foreground=COLOR["text"],
        arrowcolor=COLOR["text"], bordercolor=COLOR["border_strong"],
        relief="solid", borderwidth=1, padding=(6, 3))
    style.map("TCombobox",
        fieldbackground=[("readonly", COLOR["surface_alt"])],
        foreground=[("disabled", COLOR["text_muted"])])

    style.configure("TSpinbox",
        fieldbackground=COLOR["surface_bright"], foreground=COLOR["text"],
        arrowcolor=COLOR["text"], bordercolor=COLOR["border_strong"],
        background=COLOR["surface"], relief="solid", borderwidth=1, padding=(6, 3))

    # 勾选框
    style.configure("TCheckbutton",
        background=COLOR["bg"], foreground=COLOR["text"], font=FONTS["base"])
    style.map("TCheckbutton", background=[("active", COLOR["bg"])])

    # 笔记本（如有）
    style.configure("TNotebook", background=COLOR["bg"], bordercolor=COLOR["border"])
    style.configure("TNotebook.Tab",
        background=COLOR["surface"], foreground=COLOR["text_secondary"],
        padding=(12, 6), font=FONTS["base"])
    style.map("TNotebook.Tab",
        background=[("selected", COLOR["accent"])],
        foreground=[("selected", COLOR["text_on_accent"])])

    # 表格（硬盘哈希扫描等）
    style.configure("Treeview",
        background=COLOR["surface_bright"], foreground=COLOR["text"],
        fieldbackground=COLOR["surface_bright"], bordercolor=COLOR["border"],
        rowheight=28, font=FONTS["base"])
    style.configure("Treeview.Heading",
        background=COLOR["surface_alt"], foreground=COLOR["text_secondary"],
        font=FONTS["sm"], relief="flat", borderwidth=1)
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

    # 应用底色
    try:
        root.configure(background=COLOR["bg"])
    except Exception:
        pass
    return style


# ============================================================
# 5. 复用组件
# ============================================================
def make_primary_button(parent, text, command, **kw):
    """主操作按钮（tk.Button）：扁平蓝 + 悬停加深 + 禁用仍可读（白字）。"""
    btn = tk.Button(
        parent, text=text, command=command,
        bg=COLOR["accent"], fg=COLOR["text_on_accent"],
        activebackground=COLOR["accent_active"], activeforeground=COLOR["text_on_accent"],
        disabledforeground=COLOR["text_on_accent"],
        relief=tk.FLAT, borderwidth=0,
        font=FONTS["base"], padx=kw.pop("padx", 18), pady=kw.pop("pady", 6),
        cursor="hand2", **kw,
    )
    _bind_accent_hover(btn)
    return btn


def _bind_accent_hover(btn):
    def _enter(e):
        if str(btn.cget("state")) != tk.DISABLED:
            btn.configure(bg=COLOR["accent_hover"])
    def _leave(e):
        btn.configure(bg=COLOR["accent"])
    btn.bind("<Enter>", _enter)
    btn.bind("<Leave>", _leave)


def make_section_title(parent, text):
    """区块标题标签：中等字号主文字，用于分隔面板内的功能段落。"""
    return ttk.Label(parent, text=text, font=FONTS["md"], foreground=COLOR["text"])
