# -*- coding: utf-8 -*-
"""⑯ 图片压缩（移植自 image_compressor）
mozjpeg 静态二进制随包分发（bin/mozjpeg/），Pillow(libjpeg-turbo) 为备选编码器"""
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import filedialog, messagebox
from . import ui, ui as ttk
from .theme import FONTS

from . import config
from .common import (APP_TITLE, _module_available, clean_path, clear_log,
                     make_log_panel, make_tip, run_task)
from .theme import COLOR


# ⑯ 图片压缩（移植自 image_compressor）
# mozjpeg 静态二进制随包分发（bin/mozjpeg/），Pillow(libjpeg-turbo) 为备选编码器
# ============================================================

COMPRESS_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}


def _tool_base_dir():
    """工具箱自身所在目录（exe 时为 exe 所在目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundled(rel):
    """打包后资源在 sys._MEIPASS，开发时在脚本同目录"""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


MOZ_CJPEG = _bundled(os.path.join("bin", "mozjpeg", "cjpeg.exe"))
MOZ_JPEGTRAN = _bundled(os.path.join("bin", "mozjpeg", "jpegtran.exe"))

# 配置统一存 toolbox_config.json 的 compress 分区（见 toolbox/config.py）
COMPRESS_LOG_PATH = os.path.join(_tool_base_dir(), "compressor.log")

clogger = logging.getLogger("imgpress")
clogger.propagate = False
clogger.setLevel(logging.INFO)
clogger.addHandler(logging.NullHandler())  # 默认不输出，由 setup_logging 控制

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def setup_logging(enable_log, log_path=COMPRESS_LOG_PATH):
    """按开关挂载/移除文件日志。默认不创建也不写入日志文件。"""
    for h in list(clogger.handlers):
        if isinstance(h, logging.FileHandler):
            clogger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    if enable_log:
        try:
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            clogger.addHandler(fh)
            clogger.info("日志已开启: %s", log_path)
        except Exception:
            pass


class SkipCompress(Exception):
    """主动跳过（如源文件只读），属于正常跳过而非错误"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def load_compress_config():
    """读取记住的设置；不存在则返回默认（默认统一为 JPG）"""
    default = {
        'save_mode': 0,
        'outdir': '',
        'out_fmt': 1,            # 默认统一为 JPG
        'encoder': 0,            # 0=mozjpeg(默认, 体积更小) / 1=libjpeg-turbo
        'enable_compress': True,
        'keep_exif': True,
        'quality': 100,
        'multithread': True,
        'force': False,
        'enable_log': False,
        'collect_failed': False,
        'faildir': 'dist',
        'remember': True,
    }
    try:
        data = config.get_section("compress")
        if data:
            default.update({k: v for k, v in data.items() if k in default})
            clogger.info("已读取上一次设置")
    except Exception as e:
        clogger.warning("读取配置失败, 使用默认: %s", e)
    return default


def save_compress_config(options):
    if not options.get('remember', False):
        return
    try:
        config.set_section("compress", options)
        clogger.info("已保存设置到 %s", config.config_path())
    except Exception as e:
        clogger.warning("保存配置失败: %s", e)


# IJG 标准亮度量化矩阵 (Annex K)，用于由 DQT 反推质量
_STD_LUMA = [
    16, 11, 12, 14, 12, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26,
    58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51,
    87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81,
    104, 113, 92, 49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98,
    112, 100, 103, 99,
]


def _ijg_table(q):
    """根据 IJG 算法生成质量 q 对应的亮度量化表"""
    s = 5000 // q if q < 50 else 200 - 2 * q
    return [max(1, min(255, (v * s + 50) // 100)) for v in _STD_LUMA]


def estimate_jpeg_quality(path):
    """解析 JPEG 的 DQT 量化表，估算质量(1-100)；非 JPEG 或解析失败返回 None"""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return None
    if head[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(head)
    tables = []
    while i + 4 < n:
        if head[i] != 0xFF:
            i += 1
            continue
        marker = head[i + 1]
        i += 2
        if marker == 0xD9 or marker == 0xDA:  # 图像数据开始
            break
        if marker == 0xDB:  # DQT
            seg_len = (head[i] << 8) | head[i + 1]
            j = i + 2
            end = i + seg_len
            while j < end:
                pq_tq = head[j]
                precision = (pq_tq >> 4) & 0x0F
                j += 1
                count = 128 if precision else 64
                step = 2 if precision else 1
                table = []
                for k in range(0, count, step):
                    if j + step <= len(head):
                        if precision:
                            table.append((head[j] << 8) | head[j + 1])
                        else:
                            table.append(head[j])
                    j += step
                if len(table) == 64:
                    tables.append(table)
            i = end
        else:
            if i + 1 < n:
                seg_len = (head[i] << 8) | head[i + 1]
                i += seg_len
            else:
                break
    if not tables:
        return None
    t = tables[0]  # 取亮度表
    best_q, best_d = 1, float("inf")
    for q in range(1, 101):
        ref = _ijg_table(q)
        d = sum((ref[k] - t[k]) ** 2 for k in range(64))
        if d < best_d:
            best_d, best_q = d, q
    return best_q


def human_size(n):
    if n is None or n < 0:
        return "-"
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            if unit == 'B':
                return f"{n} B"
            return f"{n:.1f} {unit}"
        n /= 1024.0


def collect_compress_images(path):
    """递归收集单个文件或文件夹下所有图片路径"""
    result = []
    if os.path.isfile(path):
        if os.path.splitext(path)[1].lower() in COMPRESS_IMAGE_EXTS:
            result.append(path)
    elif os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for f in files:
                if os.path.splitext(f)[1].lower() in COMPRESS_IMAGE_EXTS:
                    result.append(os.path.join(root, f))
    return result


def _retry_read(func, src, attempts=3, delay=0.3):
    """对可能被其它进程临时锁定的读操作做有限重试"""
    last = None
    for _ in range(attempts):
        try:
            return func()
        except (OSError, PermissionError) as e:
            last = e
            clogger.warning("读取被拒, 重试中: %s | %s", src, e)
            time.sleep(delay)
    raise last


def _run_cmd(cmd):
    """执行外部命令，失败抛异常并附上 stderr；隐藏子进程控制台窗口"""
    r = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        msg = r.stderr.decode('utf-8', 'replace').strip() or r.stdout.decode('utf-8', 'replace').strip()
        raise RuntimeError(f"{os.path.basename(cmd[0])} 失败({r.returncode}): {msg[:300]}")
    return r


def _real_format(src):
    """以真实内容为据判定格式（Pillow 读取文件头），避免 .jpg 实为其它格式"""
    from PIL import Image
    try:
        with _retry_read(lambda: Image.open(src), src) as im:
            return im.format
    except Exception as e:
        clogger.warning("无法识别图片格式: %s | %s", src, e)
        return None


def _is_readonly(path):
    """判断文件是否只读（无写入权限）：用于提前跳过避免覆盖写入失败"""
    try:
        if (os.stat(path).st_mode & 0o222) == 0:
            return True
        return not os.access(path, os.W_OK)
    except OSError:
        return False


def _is_animated(src):
    """判断是否为动图（多帧）。压缩会摊平成单帧从而丢失动画，故动图不压缩、直接跳过。"""
    from PIL import Image
    try:
        with _retry_read(lambda: Image.open(src), src) as im:
            if getattr(im, 'is_animated', False):
                return True
            try:
                return getattr(im, 'n_frames', 1) > 1
            except Exception:
                return False
    except Exception as e:
        clogger.warning("无法判断是否为动图: %s | %s", src, e)
        return False


def _flatten(im, options):
    """去透明：RGBA/带透明 PNG 填白底后转 RGB"""
    from PIL import Image
    if im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info):
        bg = Image.new('RGB', im.size, (255, 255, 255))
        bg.paste(im, mask=im.convert('RGBA').split()[-1])
        return bg
    if im.mode not in ('L', 'RGB'):
        return im.convert('RGB')
    return im


def _make_ppm(src, options):
    """非 JPEG 源转 RGB 后存为临时 PPM，供 mozjpeg cjpeg 读取"""
    from PIL import Image
    fd, ppm = tempfile.mkstemp(suffix='.ppm')
    os.close(fd)
    try:
        with _retry_read(lambda: Image.open(src), src) as im:
            im = _flatten(im, options)
            im.save(ppm, 'PPM')
        return ppm
    except Exception:
        if os.path.exists(ppm):
            os.remove(ppm)
        raise


def _pillow_encode(src, options, tmp, target_q):
    """libjpeg-turbo 模式：用 Pillow 自带的 libjpeg-turbo 编码 JPEG"""
    from PIL import Image
    with _retry_read(lambda: Image.open(src), src) as im:
        im = _flatten(im, options)
        save_kw = dict(quality=target_q, optimize=True, progressive=True)
        if options['keep_exif']:
            exif = im.info.get('exif')
            icc = im.info.get('icc_profile')
            if exif:
                save_kw['exif'] = exif
            if icc:
                save_kw['icc_profile'] = icc
        im.save(tmp, 'JPEG', **save_kw)


def _compress_other(src, options, target, orig_size, existing_q, src_is_jpeg):
    """非 JPEG 目标（PNG/WebP/TIFF）压缩，沿用 Pillow 无损逻辑"""
    from PIL import Image
    tmp = target + ".tmp"
    try:
        with _retry_read(lambda: Image.open(src), src) as im:
            icc = im.info.get('icc_profile') if options['keep_exif'] else None
            fmt_map = {'.png': 'PNG', '.webp': 'WEBP', '.tif': 'TIFF', '.tiff': 'TIFF'}
            fmt = fmt_map.get(os.path.splitext(target)[1].lower())
            if fmt is None:
                return src, None, "不支持的格式", None
            save_kw = {}
            if fmt == 'PNG':
                save_kw.update(optimize=True)
                if icc:
                    save_kw['icc_profile'] = icc
            elif fmt == 'WEBP':
                if im.mode == 'P':
                    im = im.convert('RGBA')
                save_kw.update(quality=options['quality'] if options['enable_compress'] else 100,
                               method=6)
            elif fmt == 'TIFF':
                save_kw.update(compression='tiff_lzw')
            im.save(tmp, fmt, **save_kw)
        new_size = os.path.getsize(tmp)
        # 压缩后体积 >= 压缩前 -> 不替换，保留原图（适用于所有保存方式）
        if new_size >= orig_size:
            os.remove(tmp)
            clogger.info("跳过(压缩后未变小): %s", src)
            return src, None, "跳过·压缩后体积未变小", None
        if os.path.abspath(tmp) != os.path.abspath(target) and os.path.exists(target):
            os.remove(target)
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    clogger.info("已压缩(非JPEG): %s -> %s (%d -> %d)", src, target, orig_size, new_size)
    return src, new_size, None, None


def _compress_inner(src, options):
    target, out_ext = _target_path_for(src, options)
    # 只读属性且需要原地覆盖该文件 -> 无法写入，直接跳过并提示
    if options['save_mode'] == 0 and os.path.abspath(target) == os.path.abspath(src) \
            and _is_readonly(src):
        raise SkipCompress("跳过·源文件为只读属性，无法覆盖写入")
    # 动图（多帧 GIF / 动画 WebP 等）：压缩会摊平为单帧、丢失动画，直接跳过
    if _is_animated(src):
        raise SkipCompress("跳过·动图（多帧动画）")
    clogger.info("开始处理: %s -> 目标格式 %s, 目标路径 %s", src, out_ext, target)
    orig_size = _retry_read(lambda: os.path.getsize(src), src)
    is_jpeg_target = out_ext.lower() in ('.jpg', '.jpeg')
    src_ext = os.path.splitext(src)[1].lower()
    real_fmt = _real_format(src)          # 以真实内容为据，避免 .jpg 实为其它格式导致 jpegtran 崩溃
    src_is_jpeg = (real_fmt == 'JPEG')
    existing_q = estimate_jpeg_quality(src) if src_is_jpeg else None
    target_q = options['quality'] if options['enable_compress'] else 100
    encoder = options.get('encoder', 0)  # 0=mozjpeg(默认) / 1=libjpeg-turbo
    moz_ok = os.path.isfile(MOZ_CJPEG) and os.path.isfile(MOZ_JPEGTRAN)

    # 目标不是 JPEG：走 Pillow 原格式无损压缩
    if not is_jpeg_target:
        return _compress_other(src, options, target, orig_size, existing_q, src_is_jpeg)

    # ---- 目标为 JPEG ----
    # 跳过：源量化质量已低于目标质量，且未勾选强行 -> 再压也无益（只会变大或画质更差）
    if src_is_jpeg and existing_q is not None and not options['force'] \
            and existing_q < target_q:
        raise SkipCompress("跳过·源量化质量低于目标质量")

    ppm_tmp = None
    tmp = target + ".tmp"
    try:
        if encoder == 0 and moz_ok:
            # mozjpeg：cjpeg 按目标质量重编码（自带 Huffman 优化）
            try:
                if src_is_jpeg:
                    _run_cmd([MOZ_CJPEG, '-quality', str(target_q), '-optimize',
                              '-progressive', '-outfile', tmp, src])
                else:
                    ppm_tmp = _make_ppm(src, options)
                    _run_cmd([MOZ_CJPEG, '-quality', str(target_q), '-optimize',
                              '-progressive', '-outfile', tmp, ppm_tmp])
            except Exception as e:
                # cjpeg 失败且为 JPEG -> 回退 jpegtran 无损优化（更稳）
                if src_is_jpeg:
                    clogger.warning("cjpeg 重编码失败, 回退 jpegtran 无损优化: %s | %s", src, e)
                    _run_cmd([MOZ_JPEGTRAN, '-optimize', '-copy', 'all', '-outfile', tmp, src])
                else:
                    raise
        else:
            # libjpeg-turbo (Pillow 自带)；或 mozjpeg 二进制缺失时自动回退
            if encoder == 0 and not moz_ok:
                clogger.warning("mozjpeg 二进制缺失，本次回退 libjpeg-turbo: %s", src)
            _pillow_encode(src, options, tmp, target_q)

        new_size = os.path.getsize(tmp)
        # 跳过：压缩后体积 >= 压缩前 -> 不替换，保留原图
        if new_size >= orig_size:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise SkipCompress("跳过·压缩后体积未变小" if new_size == orig_size
                               else "跳过·压缩后体积更大")

        need_delete_src = (os.path.abspath(tmp) != os.path.abspath(target))
        if need_delete_src and os.path.exists(target):
            os.remove(target)
        os.replace(tmp, target)
    except SkipCompress:
        raise
    except Exception:
        # 任何异常都要清理临时文件，避免残留 .tmp
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    finally:
        if ppm_tmp and os.path.exists(ppm_tmp):
            try:
                os.remove(ppm_tmp)
            except OSError:
                pass

    # 统一为 JPG 且覆盖原图时，旧扩展名文件（如 .png）应被删除
    if options['save_mode'] == 0 and os.path.abspath(target) != os.path.abspath(src) \
            and os.path.exists(src):
        try:
            os.remove(src)
            clogger.info("删除原文件: %s", src)
        except OSError as e:
            clogger.warning("删除原文件失败 %s: %s", src, e)

    clogger.info("已压缩: %s -> %s (%d -> %d 字节)", src, target, orig_size, new_size)
    return src, new_size, None, target_q


def compress_one(src, options):
    try:
        return _compress_inner(src, options)
    except SkipCompress as e:
        clogger.info("跳过: %s | %s", src, e.reason)
        return src, None, e.reason, None
    except Exception as e:
        clogger.exception("压缩失败: %s | %s", src, e)
        return src, None, f"{type(e).__name__}: {e}", None


def _collect_failed(src, options):
    """把压缩失败的原文件复制到 dist（或自定义）文件夹，便于排查"""
    if not options.get('collect_failed'):
        return
    try:
        d = options.get('faildir') or 'dist'
        if os.path.isabs(d):
            target_dir = d
        else:
            target_dir = os.path.join(os.path.dirname(os.path.abspath(src)), d)
        os.makedirs(target_dir, exist_ok=True)
        base = os.path.basename(src)
        dst = os.path.join(target_dir, base)
        if os.path.abspath(dst) == os.path.abspath(src):
            return
        if os.path.exists(dst):
            stem, ext = os.path.splitext(base)
            i = 1
            while os.path.exists(dst):
                dst = os.path.join(target_dir, f"{stem}_{i}{ext}")
                i += 1
        shutil.copy2(src, dst)
        clogger.info("失败文件已复制至: %s", dst)
    except Exception as e:
        clogger.warning("复制失败文件出错 %s: %s", src, e)


def _avoid_collision(target, src, root, ext):
    """当目标路径已存在且不是源文件自身时，自动加 _2/_3 重命名"""
    if os.path.abspath(target) == os.path.abspath(src) or not os.path.exists(target):
        return target
    parent = os.path.dirname(target)
    base = os.path.basename(target)
    stem, _ = os.path.splitext(base)
    i = 2
    while True:
        cand = os.path.join(parent, f"{stem}_{i}{ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def _target_path_for(src, options):
    mode = options['save_mode']
    root, ext = os.path.splitext(src)
    src_ext = ext.lower()
    # 统一为 JPG：除 JPEG 外全部改成 .jpg
    if options['out_fmt'] == 1 and src_ext not in ('.jpg', '.jpeg'):
        ext = '.jpg'
    if mode == 0:
        # 覆盖时，若统一 JPG 导致扩展名变化，目标文件名也要改
        target = (root + ext) if ext.lower() != src_ext else src
    elif mode == 1:
        target = root + "_压缩" + ext
    else:
        outdir = options['outdir']
        os.makedirs(outdir, exist_ok=True)
        target = os.path.join(outdir, os.path.basename(root) + ext)
    return _avoid_collision(target, src, root, ext), ext


def _size_of(text):
    try:
        if text.endswith('KB'):
            return float(text[:-2]) * 1024
        if text.endswith('MB'):
            return float(text[:-2]) * 1024 ** 2
        if text.endswith('GB'):
            return float(text[:-3]) * 1024 ** 3
        if text.endswith('B'):
            return float(text[:-1])
    except ValueError:
        pass
    return None


class CompressOptionsDialog(ui.Toplevel):
    """图片压缩操作选项对话框"""

    def __init__(self, master, options):
        super().__init__(master)
        self.options = options
        self.result = None
        self.title("操作选项")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        pad = dict(padx=10, pady=4)

        # ---- 保存路径 ----
        box1 = ui.LabelFrame(self, text="保存路径")
        box1.pack(fill="x", **pad)
        self.var_save_mode = tk.IntVar(value=options['save_mode'])
        for val, text in ((0, "覆盖原图"), (1, "原路径重命名"), (2, "保存到指定路径")):
            ttk.Radiobutton(box1, text=text, variable=self.var_save_mode,
                            value=val, command=self._pick_dir_state).pack(side="left", padx=8, pady=6)
        row = ui.Frame(box1)
        row.pack(fill="x", padx=8, pady=(0, 6))
        self.var_outdir = tk.StringVar(value=options.get('outdir', '') or '')
        self.ent_outdir = ttk.Entry(row, textvariable=self.var_outdir, width=32, state="disabled")
        self.ent_outdir.pack(side="left", fill="x", expand=True)
        self.btn_pick = ui.Button(row, text="浏览…", width=8, command=self._pick_dir, state="disabled")
        self.btn_pick.pack(side="left", padx=(6, 0))
        ui.Label(box1, text="说明：执行覆盖操作不可逆，请确认已备份。",
                  foreground=COLOR["text_muted"]).pack(anchor="w", padx=8, pady=(0, 6))

        # ---- 压缩设置 ----
        box2 = ui.LabelFrame(self, text="压缩设置")
        box2.pack(fill="x", **pad)
        row2 = ui.Frame(box2)
        row2.pack(fill="x", padx=8, pady=6)
        self.var_enable = tk.BooleanVar(value=options.get('enable_compress', True))
        ttk.Checkbutton(row2, text="设置图片压缩", variable=self.var_enable).pack(side="left")
        self.var_keep_exif = tk.BooleanVar(value=options.get('keep_exif', True))
        ttk.Checkbutton(row2, text="保留 Exif", variable=self.var_keep_exif).pack(side="left", padx=(20, 0))

        row_enc = ui.Frame(box2)
        row_enc.pack(fill="x", padx=8, pady=(0, 6))
        ui.Label(row_enc, text="编码器：").pack(side="left")
        self.var_encoder = tk.IntVar(value=options.get('encoder', 0))
        ttk.Radiobutton(row_enc, text="mozjpeg (默认·体积更小)", variable=self.var_encoder,
                        value=0).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(row_enc, text="libjpeg-turbo", variable=self.var_encoder,
                        value=1).pack(side="left", padx=(12, 0))

        # 失败收集
        box_fail = ui.LabelFrame(self, text="压缩失败收集")
        box_fail.pack(fill="x", **pad)
        self.var_collect = tk.BooleanVar(value=options.get('collect_failed', False))
        ttk.Checkbutton(box_fail, text="压缩失败的文件复制到",
                        variable=self.var_collect).pack(side="left", padx=8, pady=6)
        self.var_faildir = tk.StringVar(value=options.get('faildir', 'dist') or 'dist')
        ttk.Entry(box_fail, textvariable=self.var_faildir, width=26).pack(side="left", padx=4, pady=6)
        ui.Label(box_fail, text="（相对源文件目录或绝对路径）", foreground=COLOR["text_muted"]).pack(side="left", pady=6)

        box_fmt = ui.LabelFrame(self, text="结果格式")
        box_fmt.pack(fill="x", **pad)
        self.var_out_fmt = tk.IntVar(value=options.get('out_fmt', 1))
        ttk.Radiobutton(box_fmt, text="保持原格式", variable=self.var_out_fmt,
                        value=0).pack(side="left", padx=8, pady=6)
        ttk.Radiobutton(box_fmt, text="统一为 JPG", variable=self.var_out_fmt,
                        value=1).pack(side="left", padx=8, pady=6)
        ui.Label(box_fmt, text="统一为 JPG 时，PNG/WebP/TIFF/BMP 均按质量重压缩为 JPEG",
                  foreground=COLOR["text_muted"]).pack(anchor="w", padx=8, pady=(0, 6))

        box3 = ui.LabelFrame(self, text="图片质量")
        box3.pack(fill="x", **pad)
        qrow = ui.Frame(box3)
        qrow.pack(fill="x", padx=10, pady=(4, 0))
        ui.Label(qrow, text="低").pack(side="left")
        self.var_quality = tk.IntVar(value=options.get('quality', 100))
        self.qscale = ttk.Scale(qrow, from_=50, to=100, orient="horizontal",
                                variable=self.var_quality, command=self._qlabel)
        self.qscale.pack(side="left", fill="x", expand=True, padx=8)
        ui.Label(qrow, text="高").pack(side="left")
        self.qlabel = ui.Label(qrow, text="", width=5)
        self.qlabel.pack(side="left")
        self._qlabel()
        ui.Label(box3, text="说明：默认 mozjpeg 对 JPEG 做无损优化（Huffman 重排，画质不变、体积更小）；\n"
                             "质量滑条调低才触发有损重编码；PNG / TIFF 始终无损压缩。",
                  foreground=COLOR["text_muted"]).pack(anchor="w", padx=10, pady=(0, 6))

        # ---- 其他 ----
        box4 = ui.Frame(self)
        box4.pack(fill="x", **pad)
        self.var_thread = tk.BooleanVar(value=options.get('multithread', True))
        ttk.Checkbutton(box4, text="多线程", variable=self.var_thread).pack(side="left")
        self.var_remember = tk.BooleanVar(value=options.get('remember', True))
        ttk.Checkbutton(box4, text="记住本次设置", variable=self.var_remember).pack(side="left", padx=(20, 0))
        self.var_force = tk.BooleanVar(value=options.get('force', False))
        ttk.Checkbutton(box4, text="强行再次压缩", variable=self.var_force).pack(side="left", padx=(20, 0))
        self.var_log = tk.BooleanVar(value=options.get('enable_log', False))
        ttk.Checkbutton(box4, text="开启日志（compressor.log）", variable=self.var_log).pack(side="left", padx=(20, 0))

        btnrow = ui.Frame(self)
        btnrow.pack(fill="x", padx=10, pady=10)
        # 与系统 messagebox 统一：主操作（确定）在左，取消在右
        ui.Button(btnrow, text="取消", command=self.destroy, width=10).pack(side="right", padx=6)
        ui.Button(btnrow, text="确定", command=self._ok, width=10).pack(side="right")

        self._pick_dir_state()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        self._center()

    def _center(self):
        """相对 master（主窗口）居中，并防止超出屏幕"""
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        try:
            mx = self.master.winfo_rootx()
            my = self.master.winfo_rooty()
            mw = self.master.winfo_width()
            mh = self.master.winfo_height()
        except Exception:
            mx = my = 0
            mw, mh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = mx + max(0, (mw - w) // 2)
        y = my + max(0, (mh - h) // 2)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, min(sw - w, x))
        y = max(0, min(sh - h, y))
        self.geometry(f"+{x}+{y}")

    def _qlabel(self, *_):
        self.qlabel.config(text=f"{self.var_quality.get()}%")

    def _pick_dir_state(self):
        if self.var_save_mode.get() == 2:
            self.ent_outdir.config(state="normal")
            self.btn_pick.config(state="normal")
        else:
            self.ent_outdir.config(state="disabled")
            self.btn_pick.config(state="disabled")

    def _pick_dir(self):
        d = filedialog.askdirectory(parent=self, title="选择保存目录")
        if d:
            self.var_outdir.set(os.path.normpath(d))

    def _ok(self):
        if self.var_save_mode.get() == 2 and not self.var_outdir.get().strip():
            messagebox.showwarning("提示", "请先选择保存目录。", parent=self)
            return
        self.result = {
            'save_mode': self.var_save_mode.get(),
            'outdir': self.var_outdir.get().strip(),
            'out_fmt': self.var_out_fmt.get(),
            'encoder': self.var_encoder.get(),
            'enable_compress': self.var_enable.get(),
            'keep_exif': self.var_keep_exif.get(),
            'quality': int(self.var_quality.get()),
            'multithread': self.var_thread.get(),
            'force': self.var_force.get(),
            'collect_failed': self.var_collect.get(),
            'faildir': self.var_faildir.get().strip() or 'dist',
            'enable_log': self.var_log.get(),
            'remember': self.var_remember.get(),
        }
        self.destroy()


# ============================================================


# ⑯ 图片压缩标签页 UI
# ============================================================

def build_compress_tab(parent, app=None):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(1, weight=1)
    make_tip(parent, 0, "图片批量压缩：默认 mozjpeg 编码器（JPEG 走 Huffman 无损优化，画质不变体积更小），"
                        "备选 libjpeg-turbo；支持 JPEG/PNG/WebP/TIFF/BMP，可直接拖入文件或文件夹。",
             colspan=1, autowrap=True)

    options = load_compress_config()
    setup_logging(options.get('enable_log', False))
    state = {"compressing": False}

    # ---- 文件树 ----
    tree_frame = ui.Frame(parent)
    tree_frame.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=6, pady=4)
    tree_frame.grid_rowconfigure(0, weight=1)
    tree_frame.grid_columnconfigure(0, weight=1)

    cols = ("name", "path", "size", "qval", "newsize", "saved")
    tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
    headers = [("name", "图片名", 140, "w"),
               ("path", "路径", 340, "w"),
               ("size", "图片大小", 88, "e"),
               ("qval", "量化质量", 70, "e"),
               ("newsize", "压缩后大小", 100, "e"),
               ("saved", "节省", 90, "e")]
    for cid, text, width, anchor in headers:
        tree.heading(cid, text=text)
        tree.column(cid, width=width, anchor=anchor)
    vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    tree.tag_configure("done", foreground=COLOR["success"])
    tree.tag_configure("fail", foreground=COLOR["error"])

    # ---- 按钮行 ----
    frame_btn = ui.Frame(parent)
    frame_btn.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=4)
    btn_add_files = ui.Button(frame_btn, text="添加图片", command=lambda: add_files_dialog())
    btn_add_files.pack(side="left")
    btn_add_dir = ui.Button(frame_btn, text="添加文件夹", command=lambda: add_folder_dialog())
    btn_add_dir.pack(side="left", padx=6)
    btn_options = ui.Button(frame_btn, text="操作选项", command=lambda: show_options())
    btn_options.pack(side="left")
    btn_compress = ui.Button(frame_btn, text="一键压缩", command=lambda: start_compress(),
                             bg=COLOR["accent"], fg=COLOR["text_on_accent"],
                             disabledforeground=COLOR["text_on_accent"],
                             activebackground=COLOR["accent_active"], activeforeground=COLOR["text_on_accent"],
                             font=FONTS["base"],
                             padx=12, pady=2, cursor="hand2")
    btn_compress.pack(side="right")
    btn_clear = ui.Button(frame_btn, text="清空列表", command=lambda: clear_list())
    btn_clear.pack(side="right", padx=6)

    # ---- 进度行 ----
    frame_prog = ui.Frame(parent)
    frame_prog.grid(row=3, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 6))
    frame_prog.grid_columnconfigure(0, weight=1)
    pbar = ttk.Progressbar(frame_prog, mode="determinate")
    pbar.grid(row=0, column=0, sticky="ew")
    label_status = ui.Label(frame_prog, text="就绪", width=46, anchor="e")
    label_status.grid(row=0, column=1, sticky="e", padx=(10, 0))

    # ---- 功能函数 ----
    def set_status(text):
        label_status.config(text=text)
        if app is not None and getattr(app, "status", None) is not None:
            app.status.config(text=f"图片压缩：{text}")

    def existing_paths():
        return {tree.set(i, 'path') for i in tree.get_children()}

    def add_paths(paths):
        added = 0
        existing = existing_paths()
        for p in paths:
            p = os.path.normpath(p)
            if p in existing:
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            existing.add(p)
            q = estimate_jpeg_quality(p)
            qval = str(q) if q is not None else "-"
            tree.insert('', 'end', values=(os.path.basename(p), p, human_size(size), qval, "-", "-"))
            added += 1
        update_count()
        if added:
            set_status(f"已添加 {added} 张图片")
        return added

    def add_files_dialog():
        files = filedialog.askopenfilenames(
            title="选择图片",
            filetypes=[("图片文件", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"),
                       ("所有文件", "*.*")])
        if files:
            add_paths(list(files))

    def add_folder_dialog():
        d = filedialog.askdirectory(title="选择文件夹")
        if d:
            add_paths(collect_compress_images(d))

    def clear_list():
        if state["compressing"]:
            messagebox.showwarning("提示", "正在压缩, 请等待完成后再操作。")
            return
        n = len(tree.get_children())
        tree.delete(*tree.get_children())
        pbar['value'] = 0
        update_count()
        set_status(f"已清空 {n} 项")

    def update_count():
        n = len(tree.get_children())
        total = 0
        for i in tree.get_children():
            s = tree.set(i, 'size')
            v = _size_of(s)
            if v:
                total += v
        set_status(f"共 {n} 项，合计 {human_size(total)}")

    def show_options():
        # 每次打开都先同步本地(已保存)设置
        nonlocal options
        options = load_compress_config()
        setup_logging(options.get('enable_log', False))
        dlg = CompressOptionsDialog(parent, options)
        parent.wait_window(dlg)
        if dlg.result:
            options.update(dlg.result)
            setup_logging(options.get('enable_log', False))
            save_compress_config(options)
            set_status("操作选项已更新" + (" (已记住)" if options.get('remember') else ""))
        return dlg.result

    def toggle_buttons(enabled):
        st = "normal" if enabled else "disabled"
        for b in (btn_compress, btn_clear, btn_add_files, btn_add_dir, btn_options):
            b.config(state=st)

    def start_compress():
        if state["compressing"]:
            return
        items = tree.get_children()
        if not items:
            messagebox.showinfo("提示", "列表为空, 请先添加图片。")
            return
        # 每次压缩前先弹出操作选项(与本地设置同步)，取消则放弃本次压缩
        if show_options() is None:
            return
        if options['save_mode'] == 0:
            if not messagebox.askyesno(
                    "确认覆盖",
                    "当前保存方式为【覆盖原图】, 压缩后将直接替换原文件且不可恢复!\n\n确定继续?"):
                return
        state["compressing"] = True
        toggle_buttons(False)
        pbar['value'] = 0
        pbar['maximum'] = len(items)
        threading.Thread(target=compress_worker, args=(list(items),), daemon=True).start()

    def compress_worker(items):
        path2iid = {tree.set(i, 'path'): i for i in items}
        total = len(items)
        ok = skip = fail = ro_skip = q_skip = big_skip = anim_skip = 0
        saved_bytes = 0

        def task(p):
            return compress_one(p, options)

        paths = [tree.set(i, 'path') for i in items]
        workers = min(8, (os.cpu_count() or 2)) if options['multithread'] else 1
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(task, p): p for p in paths}
            for fut in as_completed(futures):
                p = futures[fut]
                done += 1
                try:
                    src, new_size, err, new_q = fut.result()
                except Exception as e:
                    src, new_size, err, new_q = p, None, str(e), None
                iid = path2iid.get(src)
                if iid:
                    vals = tree.item(iid, 'values')
                    qval = vals[3]
                    if err:
                        if err.startswith("跳过"):
                            if "只读" in err:
                                short, ro_skip = "跳过·只读", ro_skip + 1
                            elif "动图" in err:
                                short, anim_skip = "跳过·动图", anim_skip + 1
                            elif "质量" in err:
                                short, q_skip = "跳过·质量低", q_skip + 1
                            else:
                                short, big_skip = "跳过·体积大", big_skip + 1
                            tree.item(iid, values=(vals[0], vals[1], vals[2], qval, vals[2], short),
                                      tags=("done",))
                            skip += 1
                        else:
                            tree.item(iid, values=(vals[0], vals[1], vals[2], qval, "-", "失败"),
                                      tags=("fail",))
                            _collect_failed(p, options)
                            fail += 1
                    elif new_size is None:
                        tree.item(iid, values=(vals[0], vals[1], vals[2], qval, vals[2], "跳过"),
                                  tags=("done",))
                        skip += 1
                    else:
                        orig = _size_of(vals[2])
                        tree.item(
                            iid,
                            values=(vals[0], vals[1], vals[2], (str(new_q) if new_q is not None else qval),
                                    human_size(new_size),
                                    f"-{(1 - new_size / orig) * 100:.1f}%" if orig else "-"),
                            tags=("done",))
                        ok += 1
                        saved_bytes += max(0, orig - new_size)
                parent.after(0, _progress, done, total)

        msg = (f"完成: 成功 {ok}  跳过(只读 {ro_skip} / 动图 {anim_skip} / 质量过低 {q_skip} / 体积变大 {big_skip})  "
               f"失败 {fail}  |  共节省 {human_size(saved_bytes)}")
        if (ro_skip + q_skip + big_skip + anim_skip) > 0:
            detail = (f"跳过统计:\n"
                      f"  · 只读属性（未覆盖原图）: {ro_skip} 个\n"
                      f"  · 动图（多帧动画，压缩会丢失动画）: {anim_skip} 个\n"
                      f"  · 源质量低于目标质量: {q_skip} 个\n"
                      f"  · 压缩后体积未变小/变大: {big_skip} 个")
            if ro_skip:
                detail += "\n\n其中『只读属性』的文件未覆盖原图，请取消只读属性后重新加入列表压缩。"
            parent.after(0, lambda t=detail: messagebox.showinfo("压缩完成 · 跳过统计", t))
        parent.after(0, _done, msg)

    def _progress(done, total):
        pbar['value'] = done
        set_status(f"压缩中… {done}/{total}")

    def _done(msg):
        state["compressing"] = False
        toggle_buttons(True)
        set_status(msg)
        pbar['value'] = pbar['maximum']

    def on_drop(paths):
        """拖放回调：展开文件夹中的图片并加入列表"""
        if state["compressing"]:
            return
        all_paths = []
        for p in paths:
            all_paths.extend(collect_compress_images(p))
        add_paths(all_paths)

    update_count()
    if app is not None:
        app.register_drop_handler(parent, on_drop)


# ============================================================
