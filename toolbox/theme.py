# -*- coding: utf-8 -*-
"""文件工具箱 · 统一设计系统（Design Tokens + 白底黑字浅色主题）
============================================================
集中管理颜色 / 字体 / 间距 / 圆角令牌，并把它们落地为 ttk 主题与
可复用的组件样式。所有界面元素从这里取色，杜绝散落的硬编码值。

风格方向：白底黑字（简洁高对比浅色）
  · 窗口 / 卡片 / 按钮面全部纯白 #FFFFFF，仅用发丝描边区分层次
  · 主文字纯黑 #000000；悬停/表头用极浅灰 #F5F5F5 点缀（非灰底大块）
  · 官方蓝强调色 #3B8ED0；语义色成功/警告/错误（浅底可读档）
  · 字号：导航 15 > 正文 14，层级清晰
"""
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
    "xs":          font(12),
    "sm":          font(13),
    "base":        font(14),
    "md":          font(15),
    "lg":          font(17),
    "xl":          font(20, "bold"),
    "title":       font(22, "bold"),
    "nav_title":   font(17, "bold"),
    "nav_group":   font(12, "bold"),  # 导航分组小标题
    "nav":         font(15),          # 导航项：比内容正文(base 14)大一号
    "nav_active":  font(15, "bold"),
    "page_title":  font(20, "bold"),  # 内容页标题
    "table_head":  font(13, "bold"),  # 表格表头
    "log":         (FONT_MONO, 11),
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
        font=FONTS["base"])

    # 原生 tk 控件（Listbox / Text / Menu）的默认字体与配色：
    # 这些控件没有 CTk 等价件，用 option database 统一，避免出现系统默认字体。
    # Tcl 字体描述里带空格的字体名必须用花括号包起来，
    # 否则 "Microsoft YaHei 14" 会被拆成 family=Microsoft, size=YaHei。
    _base_font = f"{{{FONT_FAMILY}}} {FONTS['base'][1]}"
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
        background=COLOR["bg"], foreground=COLOR["text"], font=FONTS["base"])

    style.configure("TLabelframe",
        background=COLOR["bg"], foreground=COLOR["text_secondary"],
        bordercolor=COLOR["border"], relief="groove", borderwidth=1)
    style.configure("TLabelframe.Label",
        background=COLOR["bg"], foreground=COLOR["text_secondary"],
        font=FONTS["sm"], padding=(6, 0))

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
        insertcolor=COLOR["text"], relief="solid", borderwidth=1,
        font=FONTS["base"], padding=(8, 4))
    style.map("TEntry",
        bordercolor=[("focus", COLOR["accent"])],
        fieldbackground=[("readonly", COLOR["surface_inset"])])

    style.configure("TCombobox",
        fieldbackground=COLOR["surface_bright"], foreground=COLOR["text"],
        arrowcolor=COLOR["text"], bordercolor=COLOR["border_strong"],
        relief="solid", borderwidth=1, font=FONTS["base"], padding=(8, 4))
    style.map("TCombobox",
        fieldbackground=[("readonly", COLOR["surface_bright"])],
        foreground=[("disabled", COLOR["text_muted"])])

    style.configure("TSpinbox",
        fieldbackground=COLOR["surface_bright"], foreground=COLOR["text"],
        arrowcolor=COLOR["text"], bordercolor=COLOR["border_strong"],
        background=COLOR["surface"], relief="solid", borderwidth=1,
        font=FONTS["base"], padding=(6, 3))
    style.map("TSpinbox",
        bordercolor=[("focus", COLOR["accent"])],
        foreground=[("disabled", COLOR["text_muted"])])

    # 勾选框 / 单选框
    style.configure("TCheckbutton",
        background=COLOR["bg"], foreground=COLOR["text"], font=FONTS["base"],
        padding=(2, 2), focuscolor=COLOR["bg"])
    style.map("TCheckbutton", background=[("active", COLOR["bg"])])

    style.configure("TRadiobutton",
        background=COLOR["bg"], foreground=COLOR["text"], font=FONTS["base"],
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
        font=FONTS["base"], padding=(8, 4))
    style.map("TMenubutton",
        background=[("active", COLOR["surface_alt"])])
    style.configure("TSeparator", background=COLOR["border"])

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
        rowheight=30, font=FONTS["base"])
    style.configure("Treeview.Heading",
        background=COLOR["surface_alt"], foreground=COLOR["text"],
        font=FONTS["table_head"], relief="flat", borderwidth=1,
        padding=(6, 4))
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
