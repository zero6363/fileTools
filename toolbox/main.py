# -*- coding: utf-8 -*-
"""文件工具箱（合并版）· 主窗口
=====================================
将原先分散的多个小工具合并为一个 GUI 程序，点击左侧纵向菜单即可切换使用对应工具。

编号规则：编号 = 左侧菜单从上到下的顺序（① 为第一项），由 TOOL_BUILDERS 的下标
自动生成，不手写；列表顺序即显示顺序，改顺序只需挪动列表项，编号会自动顺着排。

标签页与原工具对应关系（按菜单顺序）：
  ① 文件分组      <- 分组.bat / 分组 - jpg.bat / 分组 -mp4.bat / 分组 -txt.bat / 分组年月.bat
                    （四个"按数量"的 bat 已合并：后缀下拉可选（jpg/txt/mp4/*）也可自定义，
                      * = 按图片格式分组（jpg/jpeg/png/gif/bmp/tiff/webp 可改），不会分组所有文件；
                      每组数量可自定义，文件夹前缀默认随后缀自动变化；同时保留"按年月分组"模式）
  ② 文件名添加路径 <- 重命名.py（把目录层级添加到文件名前）
  ③ 移动到根目录  <- move.bat（省略了原 bat 的自我删除；默认同名跳过，可选同名重命名移动）
  ④ 删除空文件夹  <- 空文件夹删除.py
  ⑤ 实况图分组    <- 实况分组-可视化.py
  ⑥ 序号重命名    <- 新增：拖入文件/文件夹，遍历读取，数值化自然排序（9 在 10 前面），
                      支持【全部重命名为序号 001/002…】和【前加序号 001-原文件名】，
                      可勾选【仅处理文件夹】只重命名子文件夹
  ⑦ 文件名删除    <- 新增：按位置删除文件名中的字符（可选文件/文件夹、第几位开始、
                      删几个字、从前/从后），拖入列表读取，预览后执行
  ⑧ 视频常用压制  <- 新增：调用 FFmpeg 批量压制（NVENC 优先 / 无则 QSV / 再退软件），
                      AV1 / H.265 自由选，内嵌字幕硬烧（复刻 ShanaEncoder 样式），
                      成功后源文件归档到 success/
  ⑨ 视频横竖屏    <- 新增：识别指定文件夹下的视频是横屏还是竖屏，分别移动到
                      「横屏」「竖屏」子文件夹；不处理子文件夹，纯读容器头极速
  ⑩ 音轨提取      <- 新增：从 ts / flv / mp4 等视频里把音轨原样抽出来
                       （ffmpeg -c:a copy，不转码，几 GB 的视频也只要一两秒）；
                       按音轨编码自动选容器（AAC → m4a、MP3 → mp3、
                       FLAC → flac、AC-3 / DTS → mka）；输出文件夹里已有
                       同名音频则跳过，绝不覆盖；可递归子文件夹、保留目录
                       结构、多文件并行
  ⑪ WebP转JPG     <- webp2jpg.py
  ⑫ 图片压缩      <- image_compressor（D:/code/pigc）：mozjpeg 无损优化 + libjpeg-turbo 备选
  ⑬ 视频字幕清理  <- 新增：双向清理字幕与视频的配对残留。没有视频
                       伴生的字幕（srt/ass/ssa/sub，视频已删或未下载的
                       残留）统一移动到根目录的 novideo/；勾选反向后，
                       没有字幕伴生的视频也移动到 nosrt/。按文件主名
                       匹配（不要求同目录），默认认识 abc.zh.srt 这类
                       语言后缀；同名不覆盖（自动 _1/_2），可先「仅列出」
                       预览再执行
  ⑭ 图片生成MD    <- 记录md.py
  ⑮ TXT转图片     <- txt2img.py
  ⑯ TXT合并       <- txt合并.py
  ⑰ TXT重命名     <- txt重命名.py
  ⑱ 加密包分类    <- zipTestPsw.py
  ⑲ ReSoBackup备份 <- ReSoBackup-main（Go 项目，Python 重实现）：RS 纠删码分片 + AES-256-GCM
  ⑳ 硬盘哈希扫描  <- 新增：冷数据建档校验。数据库 filehash.db 直接建在
                      扫描根目录下（一块盘/一个根目录一个库）。四种模式按
                      「改不改库 × 算不算哈希」组合：快速建档 / 快速预检 /
                      完整建档 / 完整检查；只读模式（预检、检查）扫出的差异
                      清单可多选后「写入数据库」，底部可按 新增 / 更新 / 删除 /
                      已计算 / 未计算 筛选，列表分页（默认 500 条/页可改）；
                      扫描根可从下拉选已管理目录；管理弹窗里多选目录还能查重复
                      文件、两个目录镜像比对；扫描过的目录记在
                      toolbox_config.json 的 hashscan 分区里，可统一管理、跨库
                      检索；「合并数据库」把子目录的库并进来，「用上级库」按上级
                      库给子目录建库（两者都只动数据库、不读文件）
  ㉑ Robocopy极速复制 <- 新增：封装 Windows 自带 robocopy，在文件夹之间
                       多线程（/MT）快速复制；可选断点续传（/Z），中断后重跑
                       会跳过已完成文件、续传未完成的大文件；限制重试次数
                       避免被占用文件卡住；不启用镜像模式，绝不删除目标里
                       多出来的文件；运行日志默认收起

模块划分：
  common.py         通用基础设施 + 拖放支持
  config.py         统一配置（toolbox_config.json，按 window/compress/ffmpeg/hashscan/
                    speedcopy/audioextract 分区，老版本分散的四个 json 首次启动自动并入）
  tab_grouping.py   ①③④   tab_live.py ⑤    tab_rename.py ②⑥⑦
  tab_encode.py ⑧         tab_video.py ⑨         tab_audio.py ⑩
  tab_media.py ⑪⑱        tab_compress.py ⑫       tab_subtitle.py ⑬
  tab_docs.py ⑭⑮⑯⑰       tab_rsbackup.py ⑲
  tab_hashscan.py ⑳        tab_speedcopy.py ㉑
  （⑧ / ⑩ 依赖外部 FFmpeg；⑳ / ㉑ 只需标准库）

所有涉及文件移动/重命名的操作均防覆盖：同名跳过或自动改名，两阶段重命名，绝不丢失数据。
依赖（按需安装，缺失时对应标签页会在运行时提示）：
  pip install pillow chardet pycryptodome
  pip install pyzipper rarfile py7zr   # ⑱ 加密包分类增强（zip 用标准库即可识别）
"""
import os
import sys
import traceback
import tkinter as tk
from tkinter import messagebox

from .common import (APP_TITLE, _module_available, install_drop_support,
                     load_window_config, restore_window_geometry,
                     save_window_geometry)
from .theme import apply_theme, COLOR, FONTS, ui_scale
from . import ui, ui as ttk

# 左侧菜单宽度：默认值 / 可拖拽范围（记忆值越界时回退默认）
# 导航字号加大后需要更宽的默认值，避免最长的「⑲ ReSoBackup备份」被裁切。
NAV_DEFAULT_WIDTH = 250
NAV_MIN_WIDTH = 240
NAV_MAX_WIDTH = 380

# 编号字符：编号 = 列表下标 + 1，一律由 tool_title() 现算，不再手写。
# 这样以后增删 / 挪动页面，左侧菜单永远是从上到下 ①②③… 连续递增。
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚"


def tool_title(index, name=None):
    """第 index 个工具的标题：「① 文件分组」（导航 / 页头 / 状态栏共用同一份）。"""
    if name is None:
        name = TOOL_BUILDERS[index][0]
    num = _CIRCLED[index] if index < len(_CIRCLED) else "(%d)" % (index + 1)
    return "%s %s" % (num, name)


from .tab_grouping import build_empty_tab, build_flatten_tab, build_group_tab
from .tab_live import build_live_tab
from .tab_rename import build_rename_tab, build_seq_rename_tab, build_trim_tab
from .tab_video import build_video_tab
from .tab_docs import (build_md_tab, build_merge_tab, build_txt2img_tab,
                       build_txt_rename_tab)
from .tab_media import build_webp_tab, build_zip_tab
from .tab_compress import build_compress_tab
from .tab_encode import build_encode_tab
from .tab_audio import build_audioextract_tab
from .tab_rsbackup import build_rsbackup_tab
from .tab_hashscan import build_hash_tab
from .tab_speedcopy import build_speedcopy_tab
from .tab_subtitle import build_subtitle_tab

# 工具清单：列表顺序 = 左侧菜单显示顺序 = 编号顺序（下标 0 即 ①）。
# 名称里不要再写编号，标题由 tool_title() 拼；加页面只需插到对应分组的末尾，
# 并在下面 NAV_GROUPS 里把该分组的页数 +1 —— 后面的编号会自动顺延。
TOOL_BUILDERS = [
    ("文件分组", build_group_tab),
    ("文件名添加路径", build_rename_tab),
    ("移动到根目录", build_flatten_tab),
    ("删除空文件夹", build_empty_tab),
    ("实况图分组", build_live_tab),
    ("序号重命名", build_seq_rename_tab),
    ("文件名删除", build_trim_tab),
    ("视频常用压制", build_encode_tab),
    ("视频横竖屏", build_video_tab),
    ("音轨提取", build_audioextract_tab),
    ("WebP转JPG", build_webp_tab),
    ("图片压缩", build_compress_tab),
    ("视频字幕清理", build_subtitle_tab),
    ("图片生成MD", build_md_tab),
    ("TXT转图片", build_txt2img_tab),
    ("TXT合并", build_merge_tab),
    ("TXT重命名", build_txt_rename_tab),
    ("加密包分类", build_zip_tab),
    ("ReSoBackup备份", build_rsbackup_tab),
    ("硬盘哈希扫描", build_hash_tab),
    ("Robocopy极速复制", build_speedcopy_tab),
]

# 导航分组：按使用场景归类，顺序即左侧显示顺序，编号由顺序自动生成。
# 每组写「分组名, 本组页数」，边界按 TOOL_BUILDERS 顺序切分（合计必须等于总数）。
# 「② 文件名添加路径」放在「③ 移动到根目录」前面：两者常配套使用（先补路径再加根目录），
# 顺序按实际操作流程排。
NAV_GROUPS = [
    ("文件整理", 7),      # ①~⑦
    ("媒体处理", 6),      # ⑧~⑬
    ("文本与文档", 4),    # ⑭~⑰
    ("校验与备份", 4),    # ⑱~㉑
]

if sum(_count for _name, _count in NAV_GROUPS) != len(TOOL_BUILDERS):
    raise ValueError("NAV_GROUPS 页数合计 %d ≠ TOOL_BUILDERS 总数 %d"
                     % (sum(_count for _name, _count in NAV_GROUPS), len(TOOL_BUILDERS)))


# 主窗口
# ============================================================

class ToolboxApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        # 恢复上次窗口位置与大小；无记录或整块落在已拔掉的显示器上时用默认
        if not restore_window_geometry(root):
            self._place_default_window()
        self._fit_min_size()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 注入统一设计系统（现代浅色主题）：须在创建任何 ttk 控件前调用
        apply_theme(root)

        # 恢复上次左侧菜单宽度（越界/缺失时用默认）
        cfg = load_window_config()
        saved_nav_w = cfg.get("nav_width")
        nav_width = saved_nav_w if isinstance(saved_nav_w, int) \
            and NAV_MIN_WIDTH <= saved_nav_w <= NAV_MAX_WIDTH else NAV_DEFAULT_WIDTH

        # 状态栏先占住底部空间，再让主体填充剩余区域；
        # 否则主体先 pack 会一直铺到窗口底边，页面底部被状态栏盖住。
        self.status = ui.Label(
            root, text="就绪", anchor="w", padx=12, pady=4,
            bg=COLOR["status_bg"], fg=COLOR["text_secondary"], font=FONTS["sm"])
        self.status.pack(fill=tk.X, side=tk.BOTTOM)
        # 发丝分隔线放在状态栏上方（先 pack 状态栏，再 pack 分隔线）
        ui.Frame(root, height=1, fg_color=COLOR["border"], corner_radius=0).pack(
            fill=tk.X, side=tk.BOTTOM, padx=10)

        # 主体：左侧竖向导航 + 右侧工具内容区
        body = ui.Frame(root)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))

        # 左侧导航：细竖向滚动栏（CTkScrollableFrame），宽度可拖拽调整
        nav = ui.ScrollableFrame(body, width=nav_width)
        nav.pack(side=tk.LEFT, fill=tk.Y)
        self.nav = nav
        self._nav_applied_width = nav_width
        self._nav_resize_target = None
        self._nav_resize_job = None
        self._nav_resizing = False
        # CTk 6.x：可滚动容器实例自身即为内容容器，子控件直接放入它
        nav_inner = nav

        ui.Label(nav_inner, text="文件工具箱", font=FONTS["nav_title"],
                 text_color=COLOR["text"]).pack(anchor="w", padx=16, pady=(10, 0))
        ui.Label(nav_inner, text="%d 个实用工具" % len(TOOL_BUILDERS),
                 font=FONTS["xs"], text_color=COLOR["text_muted"]).pack(
                     anchor="w", padx=16, pady=(0, 4))

        # 分隔条：拖拽调整导航宽度（限制在 NAV_MIN~NAV_MAX），双击恢复默认宽度
        sash = tk.Frame(body, width=2, bg=COLOR["border"], cursor="sb_h_double_arrow")
        sash.pack(side=tk.LEFT, fill=tk.Y)
        sash.bind("<ButtonPress-1>", self._on_nav_resize_start)
        sash.bind("<B1-Motion>", self._on_nav_resize_move)
        sash.bind("<ButtonRelease-1>", self._on_nav_resize_end)
        sash.bind("<Double-Button-1>", self._on_nav_reset_width)

        content = ui.Frame(body)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(14, 0))

        # 内容页头：当前工具标题 + 所属分组 + 发丝分隔线
        header = ui.Frame(content)
        header.pack(fill=tk.X, padx=2, pady=(0, 2))
        self.page_title = ui.Label(header, text="", font=FONTS["page_title"],
                                   text_color=COLOR["text"])
        self.page_title.pack(side=tk.LEFT, anchor="w")
        self.page_group = ui.Label(header, text="", font=FONTS["sm"],
                                   text_color=COLOR["text_muted"])
        self.page_group.pack(side=tk.RIGHT, anchor="e", pady=(8, 0))
        ui.Frame(content, height=1, fg_color=COLOR["border"], corner_radius=0).pack(
            fill=tk.X, padx=2, pady=(0, 6))

        page_host = ui.Frame(content)
        page_host.pack(fill=tk.BOTH, expand=True)
        # 页面容器用 place 叠放，切页只做 lift()（见 show_tool 注释）：
        # pack_forget + pack 会把整页控件窗口全部 unmap 再 map，
        # CTk 每个控件都是独立 Win32 窗口，一页两三百个，切一次要
        # 150~570ms，还是一个一个冒出来的 —— 那就是「切页抽搐」。
        self.page_host = page_host
        self._page_pad = 4

        builders = TOOL_BUILDERS
        self.tool_frames = []
        self._tool_groups = [""] * len(builders)
        self._drop_handlers = {}  # {frame: handler} 各工具页注册的拖放处理器
        self._show_handlers = {}  # {frame: handler} 各工具页注册的「切换到该页时」回调
        self._current_index = 0
        self._hash_api = None    # ⑳ 页面首次打开后才创建（懒加载）
        self._encode_api = None  # ⑧ 同上
        # 启动时只造 N 个空容器，页面内容等第一次点开再建。
        # 全部 CTk 控件一次性造完要 2.4 秒（占启动耗时八成），
        # 而用户每次只看一页，不该为没打开的页面付这笔时间。
        self._builders = builders
        self._built = [False] * len(builders)
        for _index in range(len(builders)):
            self.tool_frames.append(ui.Frame(page_host))

        # 导航按场景分组显示，编号 = 列表下标 + 1，与从上到下的顺序一致。
        self.nav_items = [None] * len(builders)
        _next = 0
        for group_name, count in NAV_GROUPS:
            ui.Label(nav_inner, text=group_name, font=FONTS["nav_group"],
                     text_color=COLOR["text_muted"]).pack(
                         fill=tk.X, padx=18, pady=(6, 1))
            for index in range(_next, _next + count):
                title = tool_title(index)
                self._tool_groups[index] = group_name
                btn = ui.Button(
                    nav_inner, text=title, anchor="w", height=33,
                    command=lambda i=index: self.show_tool(i),
                    bg=COLOR["nav_bg"], fg=COLOR["text"],
                    activebackground=COLOR["nav_item_hover"],
                    font=FONTS["nav"], cursor="hand2",
                )
                btn._active = False
                btn.pack(fill=tk.X, padx=10, pady=0)
                self.nav_items[index] = (btn, self.tool_frames[index], title)
            _next += count

        # 导航内容不足一屏时隐藏滚动条，避免右侧多出一条灰条；
        # 窗口变矮、内容超出时自动恢复（CTkScrollableFrame 默认始终显示滚动条）。
        # 只在「显示 / 隐藏」状态真正变化时操作控件，且拖拽期间暂停检测，
        # 避免每次 <Configure> 都 grid()/grid_remove() 造成闪烁。
        _scroll_state = {"job": None, "visible": True}

        def _sync_nav_scrollbar(_event=None):
            if self._nav_resizing or _scroll_state["job"] is not None:
                return

            def _run():
                _scroll_state["job"] = None
                try:
                    canvas = nav._parent_canvas
                    bar = nav._scrollbar
                    bbox = canvas.bbox("all")
                    need = bool(bbox and bbox[3] - bbox[1] > canvas.winfo_height() + 4)
                    if need == _scroll_state["visible"]:
                        return
                    _scroll_state["visible"] = need
                    if need:
                        bar.grid()
                    else:
                        bar.grid_remove()
                except Exception:
                    pass

            _scroll_state["job"] = root.after(30, _run)

        self._sync_nav_scrollbar = _sync_nav_scrollbar
        nav.bind("<Configure>", _sync_nav_scrollbar, add="+")
        root.after(300, _sync_nav_scrollbar)

        # 注册 Windows 文件拖放（自实现，经队列在正常事件循环中处理）
        install_drop_support(root, self._on_files_dropped)

        self.show_tool(0)
        root.after(300, self._check_deps)

    def register_drop_handler(self, frame, handler):
        """由支持拖放的工具页注册处理器（按页面分发，互不干扰）"""
        self._drop_handlers[frame] = handler

    def register_show_handler(self, frame, handler):
        """由工具页注册「切换到该页」回调（用于打开页面时才检测依赖，不拖慢启动）"""
        self._show_handlers[frame] = handler

    def _on_files_dropped(self, files):
        """拖放回调：分发给当前可见工具页的处理器（经队列在正常事件循环中执行）"""
        try:
            paths = [f if isinstance(f, str) else f.decode("utf-8", "ignore") for f in files]
        except Exception:
            return
        frame = self.tool_frames[self._current_index]
        handler = self._drop_handlers.get(frame)
        if handler:
            handler(paths)
        else:
            self.status.config(text="当前工具不支持拖放（可拖入：⑥序号重命名 / ⑧视频常用压制 / "
                                    "⑨视频横竖屏 / ⑫图片压缩 / ⑳硬盘哈希扫描 / "
                                    "⑩音轨提取）")

    def _build_page(self, index):
        """首次打开某页时才构建它的内容（空容器在启动时已经建好）。

        CTk 控件是一个个 canvas 画出来的，20 页一次性造完要 2.4 秒左右，
        占了冷启动的八成；而用户每次只看一页，拆成按需构建后
        冷启动只造第 ① 页，其余各页打开时花 40~130 ms。
        """
        if self._built[index]:
            return
        # 先置位：即使构建抛异常也标记为已尝试，避免每次切页都重试一遍
        self._built[index] = True
        frame = self.tool_frames[index]
        title, builder = self._builders[index]
        # 最复杂的 ⑳ / ⑥ 首次构建约 0.4~0.5 秒，先把状态栏刷出来再建，
        # 免得点下去像是没反应（构建完 show_tool 会把状态栏改回当前工具）
        try:
            self.status.config(text=f"正在打开「{title}」…")
            self.root.update_idletasks()
        except Exception:
            pass
        try:
            if builder in (build_seq_rename_tab, build_trim_tab, build_video_tab,
                           build_compress_tab, build_rsbackup_tab, build_encode_tab,
                           build_hash_tab,
                           build_audioextract_tab):
                result = builder(frame, self)
                if builder is build_hash_tab:
                    # 暴露给自动化冒烟测试（不影响正常使用）
                    self._hash_api = result
                elif builder is build_encode_tab:
                    self._encode_api = result
            else:
                builder(frame)
        except Exception:
            detail = traceback.format_exc()
            print(detail, file=sys.stderr)
            try:
                self.status.config(text=f"打开「{title}」失败，详见弹窗")
                messagebox.showerror(
                    "页面构建失败",
                    f"打开「{title}」时出错：\n\n{detail[-1200:]}")
            except Exception:
                pass

    def _park_page(self, frame, current):
        """把页面摆进容器：当前页跟随容器尺寸，其它页钉住当前尺寸。

        为什么不干脆全用 relwidth=1：隐藏页也跟随容器的话，窗口一拉大，
        21 页（每页数百个 CTk 子窗口）全部重排，拖拽窗口会卡成幻灯片。
        钉住绝对尺寸后，窗口 resize 只重排当前页，代价和以前一样。
        """
        pad = self._page_pad
        # CTk 的 place() 禁止传 width/height，用 tk 的原始实现绕过去
        if not current:
            # 钉住「当前实际尺寸」而不是按容器算：数值一模一样，
            # Tk 判定几何没变就不会发 <Configure>，页面里的 Label
            # 也就不会集体重算折行（那才是切页卡顿的大头）。
            w = frame.winfo_width()
            h = frame.winfo_height()
            if w <= 1 or h <= 1:
                w = self.page_host.winfo_width() - pad * 2
                h = self.page_host.winfo_height() - pad * 2
            if w > 1 and h > 1:
                tk.Place.place(frame, x=pad, y=pad, width=w, height=h)
                return
        tk.Place.place(frame, x=pad, y=pad, relwidth=1, width=-pad * 2,
                       relheight=1, height=-pad * 2)

    def show_tool(self, index):
        """切换到指定工具：把对应页抬到最上层，并高亮菜单按钮"""
        self._build_page(index)
        old = self._current_index
        self._current_index = index
        frame = self.tool_frames[index]
        # 旧页不再 unmap，只钉住尺寸沉到下面：map/unmap 才是抖动的根源
        if old != index:
            of = self.tool_frames[old]
            try:
                if of.winfo_manager():
                    self._park_page(of, False)
            except Exception:
                pass
        self._park_page(frame, True)
        frame.lift()
        current_title = self.nav_items[index][2]
        self.page_title.configure(text=current_title)
        self.page_group.configure(text=self._tool_groups[index])
        # 只改动前后两项：以前每次切页把 21 个按钮全 config 一遍，
        # 等于让 21 个 CTkButton 各重画一次，纯属白花时间。
        if old != index:
            ob, _of, _ot = self.nav_items[old]
            ob._active = False
            ob.config(fg_color=COLOR["nav_bg"], text_color=COLOR["text"],
                      hover_color=COLOR["nav_item_hover"], font=FONTS["nav"])
        btn = self.nav_items[index][0]
        btn._active = True
        btn.config(fg_color=COLOR["accent_soft"], text_color=COLOR["accent_active"],
                   hover_color=COLOR["accent_soft"], font=FONTS["nav_active"])
        self.status.config(text=f"当前工具：{current_title}　·　{self._tool_groups[index]}")
        handler = self._show_handlers.get(frame)
        if handler:
            try:
                handler()
            except Exception:
                pass

    def _check_deps(self):
        missing, optional = check_dependencies()
        if missing:
            lines = ["以下依赖缺失，对应功能暂不可用："]
            for pkg, usage in missing:
                lines.append(f"  - {pkg}（{usage}）")
            lines.append("")
            lines.append("请运行：pip install " + " ".join(p for p, _ in missing))
            if optional:
                lines.append("")
                lines.append("可选增强（⑱ 加密包分类）：pip install " + " ".join(optional))
            self.status.config(text="部分依赖缺失，详见弹出提示")
            messagebox.showwarning("缺少依赖", "\n".join(lines))
        else:
            self.status.config(text="就绪 —— 所有依赖已安装")

    # 默认窗口尺寸（设计像素；实际大小按显示器工作区收放）
    DEFAULT_SIZE = (1280, 900)

    def _place_default_window(self):
        """首次启动 / 配置失效时：按当前显示器工作区取合适尺寸并居中。

        4K 上 1280x900 只占屏幕一角，小屏上又可能超出 —— 统一按工作区的
        比例收放，再居中到主屏（有parent时按触发窗口所在屏）。
        """
        scale = max(1.0, ui_scale())
        _x, _y, work_w, work_h = ui.monitor_work_area()
        want_w = min(self.DEFAULT_SIZE[0], max(720, int((work_w - 80) / scale)))
        want_h = min(self.DEFAULT_SIZE[1], max(480, int((work_h - 96) / scale)))
        # 小屏 + 高缩放同时出现时，上面的下限可能又把窗口顶出屏幕，再兜一次底
        want_w = min(want_w, max(480, int(work_w / scale)))
        want_h = min(want_h, max(360, int(work_h / scale)))
        ui.center_window(self.root, width=want_w * scale, height=want_h * scale)

    def _fit_min_size(self):
        """最小尺寸跟着屏幕走：小屏 / 高缩放下不把窗口卡死在屏幕外。"""
        scale = max(1.0, ui_scale())
        _x, _y, work_w, work_h = ui.monitor_work_area()
        self.root.minsize(min(1080, int(work_w / scale * 0.82)),
                          min(720, int(work_h / scale * 0.68)))

    def _save_window(self):
        """保存窗口几何 + 左侧菜单宽度（拖拽/双击调整后立即持久化）"""
        try:
            width = getattr(self, "_nav_applied_width", None)
            if not width:
                try:
                    width = int(float(self.nav.cget("width")))
                except Exception:
                    width = int(self.nav.winfo_width() / max(1.0, ui_scale()))
            save_window_geometry(self.root, extra={"nav_width": width})
        except Exception:
            pass

    def _on_nav_resize_start(self, e):
        if self._nav_resize_job is not None:
            try:
                self.root.after_cancel(self._nav_resize_job)
            except Exception:
                pass
            self._nav_resize_job = None
        self._nav_resizing = True
        self._nav_resize_x0 = e.x_root
        # winfo_width 是真实像素；导航宽度按设计像素配置（CTk 会再乘缩放）
        self._nav_resize_w0 = self.nav.winfo_width() / max(1.0, ui_scale())
        self._nav_resize_target = self._nav_resize_w0

    def _apply_nav_width(self):
        """把待应用的宽度写入控件（每帧最多一次，避免拖动时高频重排闪烁）"""
        self._nav_resize_job = None
        target = self._nav_resize_target
        if target is None or target == self._nav_applied_width:
            return
        target = int(round(target))
        self.nav.configure(width=target)
        self._nav_applied_width = target

    def _on_nav_resize_move(self, e):
        if getattr(self, "_nav_resize_x0", None) is None:
            return
        # 拖动位移是真实像素，换成设计像素后再夹到允许范围
        delta = (e.x_root - self._nav_resize_x0) / max(1.0, ui_scale())
        width = self._nav_resize_w0 + delta
        self._nav_resize_target = max(
            NAV_MIN_WIDTH, min(NAV_MAX_WIDTH, width))
        if self._nav_resize_job is None:
            self._nav_resize_job = self.root.after(20, self._apply_nav_width)

    def _on_nav_resize_end(self, _e):
        if getattr(self, "_nav_resize_x0", None) is None:
            return
        if self._nav_resize_job is not None:
            try:
                self.root.after_cancel(self._nav_resize_job)
            except Exception:
                pass
            self._nav_resize_job = None
        self._apply_nav_width()
        self._nav_resize_x0 = None
        self._nav_resizing = False
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        self._save_window()
        self._sync_nav_scrollbar()

    def _on_nav_reset_width(self, _e):
        if self._nav_resize_job is not None:
            try:
                self.root.after_cancel(self._nav_resize_job)
            except Exception:
                pass
            self._nav_resize_job = None
        self._nav_resizing = False
        self._nav_resize_x0 = None
        self._nav_resize_target = NAV_DEFAULT_WIDTH
        self._apply_nav_width()
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        self._save_window()
        self._sync_nav_scrollbar()

    def on_close(self):
        """关闭前记住窗口位置、大小与左侧菜单宽度"""
        self._save_window()
        self.root.destroy()


def check_dependencies():
    missing = []
    if not _module_available("PIL"):
        missing.append(("pillow", "⑮ TXT转图片 / ⑪ WebP转JPG"))
    if not _module_available("chardet"):
        missing.append(("chardet", "⑮ TXT转图片（缺失时使用内置编码猜测，功能仍可用）"))
    optional = []
    for mod in ("pyzipper", "rarfile", "py7zr"):
        if not _module_available(mod):
            optional.append(mod)
    return missing, optional


def main():
    # 高 DPI 适配，避免高分屏下界面发虚。按「系统 DPI 感知」运行：
    # 换到不同缩放的显示器时由 Windows 整体缩放窗口，Tk 8.6 不用重排。
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    # 关掉 CTk 自己的逐屏 DPI 追踪：它会在窗口换屏时再按新屏 DPI 重排一遍，
    # 与 Windows 的整体缩放叠加，窗口宽高会突然跳变。
    try:
        ui.ctk.ScalingTracker.deactivate_automatic_dpi_awareness = True
    except Exception:
        pass
    root = ui.CTk()
    # 统一缩放：CTk 的控件尺寸 / 字号都是「设计像素」，启动时一次性乘上系统
    # 缩放（4K@200% → 2.0），控件与窗口一起等比放大；
    # ttk 控件（Treeview 等）由 theme.ttk_font() 换算成同样的像素字号。
    try:
        _scale = ui_scale()
        ui.ctk.set_widget_scaling(_scale)
        ui.ctk.set_window_scaling(_scale)
    except Exception:
        pass
    app = ToolboxApp(root)
    try:
        root.mainloop()
    except Exception:
        # windowed exe 无控制台：致命错误写入日志文件，便于排查（不再静默闪退）
        try:
            if getattr(sys, "frozen", False):
                base = os.path.dirname(sys.executable)
            else:
                base = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(base, "文件工具箱_错误日志.txt"), "a", encoding="utf-8") as f:
                f.write("=" * 50 + "\n")
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        raise
    finally:
        # 兜底：非正常退出路径也尽量记住窗口几何与菜单宽度（root 已销毁则跳过）
        try:
            if root.winfo_exists():
                extra = {}
                try:
                    # 存设计像素：CTk 的 width 配置值，不随系统缩放变化
                    w = int(app.nav.winfo_width() / max(1.0, ui_scale()))
                    if w > 1:
                        extra["nav_width"] = w
                except Exception:
                    pass
                save_window_geometry(root, extra=extra)
        except Exception:
            pass


if __name__ == "__main__":
    main()
