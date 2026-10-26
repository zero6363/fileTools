# -*- coding: utf-8 -*-
"""文件工具箱 · 统一设计系统（Design Tokens + CustomTkinter 官方深色主题）
============================================================
集中管理颜色 / 字体 / 间距 / 圆角令牌，并把它们落地为 ttk 主题与
可复用的组件样式。所有界面元素从这里取色，杜绝散落的硬编码值。

风格方向：CustomTkinter 官方示例同款深色
  · 窗口 #242424 + 卡片 #2B2B2B + 输入框 #343638 + 日志内嵌 #1D1E1E
  · 官方蓝强调色 #1F6AA5（悬停 #144870），文字 #DCE4EE
  · 圆角卡片、轻描边、扁平按钮、清晰层级
  · 语义色：成功 / 警告 / 错误（GitHub Dark 亮度档，深底可读）
"""
import tkinter as tk
from tkinter import ttk

# ============================================================
# 1. 颜色令牌（Color Tokens）—— 与 CTk 官方 blue.json 深色值对齐
# ============================================================
COLOR = {
    # 背景层
    "bg":            "#242424",   # 应用主背景（CTk gray14）
    "surface":       "#2B2B2B",   # 卡片 / 面板表面（CTk gray17）
    "surface_inset": "#1D1E1E",   # 内嵌表面（日志框，比主背景更深一档）
    "surface_alt":   "#333333",   # 次级表面（悬停态、表头，CTk gray20）
    "nav_bg":        "#242424",   # 左侧导航栏背景（与窗口同色，靠选中态分区）
    "nav_item_hover":"#333333",   # 导航项悬停

    # 描边 / 分隔
    "border":        "#474747",   # 常规描边（CTk gray28）
    "border_strong": "#565B5E",   # 强调描边 / 输入框描边（CTk 官方值）

    # 文字
    "text":           "#DCE4EE",  # 主文字（CTk 官方值）
    "text_secondary": "#9EA6AE",  # 次要文字 / 说明
    "text_muted":     "#6B7075",  # 占位 / 禁用
    "text_on_accent": "#DCE4EE",  # 强调色上的文字（CTk 官方值）

    # 强调色（CTk 官方深色蓝）
    "accent":         "#1F6AA5",
    "accent_hover":   "#144870",
    "accent_active":  "#203A4F",
    "accent_soft":    "#1F4A6E",  # 强调色淡底（选中 / 标签 / 激活项）

    # 状态栏
    "status_bg":      "#1D1E1E",

    # 语义色（深底可读的亮度档）
    "success":        "#3FB950",
    "success_active": "#2E9E44",
    "warning":        "#D29922",
    "warning_active": "#B08800",
    "error":          "#F85149",
    "error_active":   "#C93C37",
    "info":           "#58A6FF",
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
    "xs":          font(10),
    "sm":          font(11),
    "base":        font(12),
    "md":          font(13),
    "lg":          font(15),
    "xl":          font(18, "bold"),
    "title":       font(20, "bold"),
    "nav_title":   font(15, "bold"),
    "nav":         font(11),
    "nav_active":  font(11, "bold"),
    "log":         (FONT_MONO, 9),
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
    须在创建任何 ttk 控件之前调用。深色主题（CTk 官方示例同款）。"""
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
        background=COLOR["surface"], foreground=COLOR["text"],
        bordercolor=COLOR["border_strong"], darkcolor=COLOR["surface"],
        lightcolor=COLOR["surface"], relief="solid", borderwidth=1,
        font=FONTS["base"], padding=(10, 4))
    style.map("TButton",
        background=[("active", COLOR["surface_alt"]), ("pressed", COLOR["border"])],
        foreground=[("disabled", COLOR["text_muted"])])

    # 输入框
    style.configure("TEntry",
        fieldbackground=COLOR["surface_alt"], foreground=COLOR["text"],
        bordercolor=COLOR["border_strong"], lightcolor=COLOR["surface_alt"],
        insertcolor=COLOR["text"], relief="solid", borderwidth=1, padding=(6, 3))
    style.map("TEntry",
        bordercolor=[("focus", COLOR["accent"])],
        fieldbackground=[("readonly", COLOR["surface_inset"])])

    style.configure("TCombobox",
        fieldbackground=COLOR["surface_alt"], foreground=COLOR["text"],
        arrowcolor=COLOR["text"], bordercolor=COLOR["border_strong"],
        relief="solid", borderwidth=1, padding=(6, 3))
    style.map("TCombobox",
        fieldbackground=[("readonly", COLOR["surface_alt"])],
        foreground=[("disabled", COLOR["text_muted"])])

    style.configure("TSpinbox",
        fieldbackground=COLOR["surface_alt"], foreground=COLOR["text"],
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
        background=COLOR["surface"], foreground=COLOR["text"],
        fieldbackground=COLOR["surface"], bordercolor=COLOR["border"],
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
        background=COLOR["surface"], troughcolor=COLOR["bg"],
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
