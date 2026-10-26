# -*- coding: utf-8 -*-
"""文件工具箱（合并版）· 主窗口
=====================================
将原先分散的多个小工具合并为一个 GUI 程序，点击左侧纵向菜单即可切换使用对应工具。

标签页与原工具对应关系：
  ① 文件分组      <- 分组.bat / 分组 - jpg.bat / 分组 -mp4.bat / 分组 -txt.bat / 分组年月.bat
                    （四个"按数量"的 bat 已合并：后缀下拉可选（jpg/txt/mp4/*）也可自定义，
                      * = 按图片格式分组（jpg/jpeg/png/gif/bmp/tiff/webp 可改），不会分组所有文件；
                      每组数量可自定义，文件夹前缀默认随后缀自动变化；同时保留"按年月分组"模式）
  ② 移动到根目录  <- move.bat（省略了原 bat 的自我删除；默认同名跳过，可选同名重命名移动）
  ③ 删除空文件夹  <- 空文件夹删除.py
  ④ 实况图分组    <- 实况分组-可视化.py
  ⑤ 文件名添加路径 <- 重命名.py（把目录层级添加到文件名前）
  ⑥ 序号重命名    <- 新增：拖入文件/文件夹，遍历读取，数值化自然排序（9 在 10 前面），
                      支持【全部重命名为序号 001/002…】和【前加序号 001-原文件名】，
                      可勾选【仅处理文件夹】只重命名子文件夹
  ⑦ 视频常用压制  <- 新增：调用 FFmpeg 批量压制（NVENC 优先 / 无则 QSV / 再退软件），
                      AV1 / H.265 自由选，内嵌字幕硬烧（复刻 ShanaEncoder 样式），
                      成功后源文件归档到 success/
  ⑧ 视频横竖屏    <- 新增：识别指定文件夹下的视频是横屏还是竖屏，分别移动到
                      「横屏」「竖屏」子文件夹；不处理子文件夹，纯读容器头极速
  ⑨ 文件名删除    <- 新增：按位置删除文件名中的字符（可选文件/文件夹、第几位开始、
                      删几个字、从前/从后），拖入列表读取，预览后执行
  ⑩ 图片生成MD    <- 记录md.py
  ⑪ TXT转图片     <- txt2img.py
  ⑫ TXT合并       <- txt合并.py
  ⑬ TXT重命名     <- txt重命名.py
  ⑭ WebP转JPG     <- webp2jpg.py
  ⑮ 加密包分类    <- zipTestPsw.py
  ⑯ 图片压缩      <- image_compressor（D:/code/pigc）：mozjpeg 无损优化 + libjpeg-turbo 备选
  ⑰ ReSoBackup备份 <- ReSoBackup-main（Go 项目，Python 重实现）：RS 纠删码分片 + AES-256-GCM
  ⑱ 文本翻译      <- 新增：拖入 txt/lrc/srt/ass/vtt 等文本文件，调用免费翻译接口
                      （DeepL 免费网页版 / 谷歌免费接口 / MyMemory / LibreTranslate，
                        一个失败自动切换下一个）翻译成中文；保留字幕序号与时间轴、
                      LRC 时间标签，译文写回同名文件，原文件重命名为「原名.old」
  ⑲ 硬盘哈希扫描  <- 新增：冷数据建档校验。数据库 filehash.db 直接建在
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

模块划分：
  common.py         通用基础设施 + 拖放支持
  config.py         统一配置（toolbox_config.json，按 window/compress/ffmpeg/hashscan 分区，
                    老版本分散的四个 json 首次启动自动并入）
  tab_grouping.py   ①②③   tab_live.py ④   tab_rename.py ⑤⑥⑨
  tab_encode.py ⑦         tab_video.py ⑧
  tab_docs.py ⑩⑪⑫⑬       tab_media.py ⑭⑮
  tab_compress.py ⑯       tab_rsbackup.py ⑰   tab_translate.py ⑱
  tab_hashscan.py ⑲
  （⑦ tab_encode.py 依赖外部 FFmpeg；⑱ 只需标准库 + 联网；⑲ 只需标准库）

所有涉及文件移动/重命名的操作均防覆盖：同名跳过或自动改名，两阶段重命名，绝不丢失数据。
依赖（按需安装，缺失时对应标签页会在运行时提示）：
  pip install pillow chardet pycryptodome
  pip install pyzipper rarfile py7zr   # ⑮ 加密包分类增强（zip 用标准库即可识别）
"""
import os
import sys
import traceback
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk as real_ttk

from .common import (APP_TITLE, _module_available, install_drop_support,
                     load_window_config, restore_window_geometry,
                     save_window_geometry)
from .theme import apply_theme, COLOR, FONTS
from . import ui, ui as ttk

# 左侧菜单宽度：默认值 / 可拖拽范围（记忆值越界时回退默认）
# 导航字号加大后需要更宽的默认值，避免最长的「⑰ ReSoBackup备份」被裁切。
NAV_DEFAULT_WIDTH = 250
NAV_MIN_WIDTH = 240
NAV_MAX_WIDTH = 380

# 导航分组：按使用场景归类，顺序即左侧显示顺序（工具编号保持不变）
NAV_GROUPS = [
    ("文件整理", [0, 1, 2, 3, 4, 5, 8]),
    ("媒体处理", [6, 7, 13, 15]),
    ("文本与文档", [9, 10, 11, 12, 17]),
    ("校验与备份", [14, 16, 18]),
]
from .tab_grouping import build_empty_tab, build_flatten_tab, build_group_tab
from .tab_live import build_live_tab
from .tab_rename import build_rename_tab, build_seq_rename_tab, build_trim_tab
from .tab_video import build_video_tab
from .tab_docs import (build_md_tab, build_merge_tab, build_txt2img_tab,
                       build_txt_rename_tab)
from .tab_media import build_webp_tab, build_zip_tab
from .tab_compress import build_compress_tab
from .tab_encode import build_encode_tab
from .tab_rsbackup import build_rsbackup_tab
from .tab_translate import build_translate_tab
from .tab_hashscan import build_hash_tab


# 主窗口
# ============================================================

class ToolboxApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        # 恢复上次窗口位置与大小；无记录或越界时用默认
        if not restore_window_geometry(root):
            root.geometry("1280x900")
        root.minsize(1080, 720)
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
        tk.Frame(root, height=1, bg=COLOR["border"]).pack(
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
        ui.Label(nav_inner, text="19 个实用工具", font=FONTS["xs"],
                 text_color=COLOR["text_muted"]).pack(anchor="w", padx=16, pady=(0, 4))

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
        tk.Frame(content, height=1, bg=COLOR["border"]).pack(
            fill=tk.X, padx=2, pady=(0, 6))

        page_host = real_ttk.Frame(content)
        page_host.pack(fill=tk.BOTH, expand=True)

        self.tool_frames = []
        self.nav_items = []  # 按工具编号索引：[None] * 19 → (button, frame, title)
        self._tool_groups = [""] * 19
        self._drop_handlers = {}  # {frame: handler} 各工具页注册的拖放处理器
        self._show_handlers = {}  # {frame: handler} 各工具页注册的「切换到该页时」回调
        self._current_index = 0
        builders = [
            ("① 文件分组", build_group_tab),
            ("② 移动到根目录", build_flatten_tab),
            ("③ 删除空文件夹", build_empty_tab),
            ("④ 实况图分组", build_live_tab),
            ("⑤ 文件名添加路径", build_rename_tab),
            ("⑥ 序号重命名", build_seq_rename_tab),
            ("⑦ 视频常用压制", build_encode_tab),
            ("⑧ 视频横竖屏", build_video_tab),
            ("⑨ 文件名删除", build_trim_tab),
            ("⑩ 图片生成MD", build_md_tab),
            ("⑪ TXT转图片", build_txt2img_tab),
            ("⑫ TXT合并", build_merge_tab),
            ("⑬ TXT重命名", build_txt_rename_tab),
            ("⑭ WebP转JPG", build_webp_tab),
            ("⑮ 加密包分类", build_zip_tab),
            ("⑯ 图片压缩", build_compress_tab),
            ("⑰ ReSoBackup备份", build_rsbackup_tab),
            ("⑱ 文本翻译", build_translate_tab),
            ("⑲ 硬盘哈希扫描", build_hash_tab),
        ]
        for index, (title, builder) in enumerate(builders):
            frame = real_ttk.Frame(page_host, padding=(4, 4, 4, 4))
            if builder in (build_seq_rename_tab, build_trim_tab, build_video_tab,
                            build_compress_tab, build_rsbackup_tab, build_encode_tab,
                            build_translate_tab, build_hash_tab):
                result = builder(frame, self)
                if builder is build_hash_tab:
                    # 暴露给自动化冒烟测试（不影响正常使用）
                    self._hash_api = result
            else:
                builder(frame)
            self.tool_frames.append(frame)

        # 导航按场景分组显示；nav_items 仍按工具编号索引，
        # show_tool 的编号映射不受左侧显示顺序影响。
        self.nav_items = [None] * len(builders)
        for group_name, indices in NAV_GROUPS:
            ui.Label(nav_inner, text=group_name, font=FONTS["nav_group"],
                     text_color=COLOR["text_muted"]).pack(
                         fill=tk.X, padx=18, pady=(6, 1))
            for index in indices:
                title = builders[index][0]
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
            self.status.config(text="当前工具不支持拖放（可拖入：⑥序号重命名 / ⑦视频常用压制 / "
                                    "⑧视频横竖屏 / ⑯图片压缩 / ⑱文本翻译 / ⑲硬盘哈希扫描）")

    def show_tool(self, index):
        """切换到指定工具：隐藏其他页，显示对应页，并高亮菜单按钮"""
        self._current_index = index
        frame = self.tool_frames[index]
        for i, f in enumerate(self.tool_frames):
            if i != index:
                f.pack_forget()
        frame.pack(fill=tk.BOTH, expand=True)
        current_title = self.nav_items[index][2]
        self.page_title.configure(text=current_title)
        self.page_group.configure(text=self._tool_groups[index])
        for i, (btn, _f, _title) in enumerate(self.nav_items):
            if i == index:
                btn._active = True
                btn.config(fg_color=COLOR["accent_soft"], text_color=COLOR["accent_active"],
                           hover_color=COLOR["accent_soft"], font=FONTS["nav_active"])
            else:
                btn._active = False
                btn.config(fg_color=COLOR["nav_bg"], text_color=COLOR["text"],
                           hover_color=COLOR["nav_item_hover"], font=FONTS["nav"])
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
                lines.append("可选增强（⑮ 加密包分类）：pip install " + " ".join(optional))
            self.status.config(text="部分依赖缺失，详见弹出提示")
            messagebox.showwarning("缺少依赖", "\n".join(lines))
        else:
            self.status.config(text="就绪 —— 所有依赖已安装")

    def _save_window(self):
        """保存窗口几何 + 左侧菜单宽度（拖拽/双击调整后立即持久化）"""
        try:
            width = getattr(self, "_nav_applied_width", None) or self.nav.winfo_width()
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
        self._nav_resize_w0 = self.nav.winfo_width()
        self._nav_resize_target = self._nav_resize_w0

    def _apply_nav_width(self):
        """把待应用的宽度写入控件（每帧最多一次，避免拖动时高频重排闪烁）"""
        self._nav_resize_job = None
        target = self._nav_resize_target
        if target is None or target == self._nav_applied_width:
            return
        self.nav.configure(width=target)
        self._nav_applied_width = target

    def _on_nav_resize_move(self, e):
        if getattr(self, "_nav_resize_x0", None) is None:
            return
        width = self._nav_resize_w0 + (e.x_root - self._nav_resize_x0)
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
        missing.append(("pillow", "⑪ TXT转图片 / ⑭ WebP转JPG"))
    if not _module_available("chardet"):
        missing.append(("chardet", "⑪ TXT转图片（缺失时使用内置编码猜测，功能仍可用）"))
    optional = []
    for mod in ("pyzipper", "rarfile", "py7zr"):
        if not _module_available(mod):
            optional.append(mod)
    return missing, optional


def main():
    # 高 DPI 适配，避免高分屏下界面发虚
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = ui.CTk()
    # 统一缩放：CTk 默认会按 Windows 显示缩放再放大自己的控件字体，
    # 而 ttk/tk 控件不缩放，混用后「选项字体比文本大」。归一到 1.0，
    # 让 CTk 与 ttk 渲染尺寸一致（应用已开启 DPI 感知）。
    try:
        ui.ctk.set_widget_scaling(1.0)
        ui.ctk.set_window_scaling(1.0)
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
                    w = app.nav.winfo_width()
                    if w > 1:
                        extra["nav_width"] = w
                except Exception:
                    pass
                save_window_geometry(root, extra=extra)
        except Exception:
            pass


if __name__ == "__main__":
    main()
