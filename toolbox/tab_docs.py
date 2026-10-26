# -*- coding: utf-8 -*-
"""⑩ 图片生成MD / ⑪ TXT转图片 / ⑫ TXT合并 / ⑬ TXT重命名"""
import codecs
import os
import random
import re
import shutil
import string
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import messagebox, ttk

from .common import (_module_available, clean_path, clear_log, form_label,
                     form_note, make_check, make_entry_row, make_file_row,
                     make_form, make_log_panel, make_path_row, make_spin_row,
                     make_start_button, make_tip, natural_key, run_task)
from .theme import COLOR


# ⑩ 图片生成MD —— 原 记录md.py
# ============================================================

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
MD_IMAGE_LINE_RE = re.compile(r"^!\[(?P<alt>.*?)\]\((?P<url>.*?)\)\s*$")
MD_SECTION_RE = re.compile(r"^##\s+(?P<title>\S.*?)\s*$")
MD_FILE_NOTE_PREFIX = "> 文件："


def _display_folder(root_dir_name, rel_folder):
    """md 里显示、以及 ## 小节使用的文件夹名"""
    return root_dir_name if rel_folder == "." else rel_folder


def _rel_underline(root_dir_name, rel_folder):
    """复制后的图片文件名前缀（保证不同子目录的同名图片互不覆盖）"""
    if rel_folder == ".":
        return root_dir_name
    return f"{root_dir_name}_{rel_folder.replace('/', '_')}"


def _iter_images(root_input_dir, output_root, scan_subdir):
    """自然排序返回源目录中的图片：[(相对目录, 文件名, 绝对路径), ...]
    相对目录 "." = 源目录本身；输出目录（含以往复制进去的图）不参与扫描。"""
    root_abs = os.path.abspath(root_input_dir)
    out_abs = os.path.abspath(output_root)
    images = []
    for dirpath, dirnames, filenames in os.walk(root_abs):
        # 剪掉输出目录，避免把 appdata/Image 里的副本当源图
        dirnames[:] = [d for d in dirnames
                       if os.path.abspath(os.path.join(dirpath, d)) != out_abs]
        if not scan_subdir:
            dirnames[:] = []
            if os.path.abspath(dirpath) != root_abs:
                continue
        rel_folder = os.path.relpath(dirpath, root_abs).replace(os.sep, "/")
        for fn in sorted((f for f in filenames if f.lower().endswith(IMAGE_EXTS)),
                         key=natural_key):
            images.append((rel_folder, fn, os.path.join(dirpath, fn)))
    return images


def _copy_image(src_img, resource_dir, underline, img_filename):
    """复制图片到 appdata/Image（随机后缀，绝不覆盖旧文件），返回写进 md 的相对地址"""
    stem, ext = os.path.splitext(img_filename)
    while True:
        rand_str = "".join(random.choices(string.ascii_letters + string.digits, k=6))
        new_name = f"{underline}_{stem}_{rand_str}{ext}"
        dst_img = os.path.join(resource_dir, new_name)
        if not os.path.exists(dst_img):
            break
    shutil.copy2(src_img, dst_img)
    return f"appdata/Image/{new_name}"


def _md_block(img_filename, url, show_original_filename):
    lines = [f"![{img_filename}]({url})"]
    if show_original_filename:
        lines.append(f"{MD_FILE_NOTE_PREFIX}{img_filename}")
    lines.append("")
    return lines


def _ref_folder_underline(url, img_filename):
    """从已生成的图片地址反推「目录前缀」：
    地址规则 appdata/Image/{目录前缀}_{原文件名去扩展名}_{6位随机}{扩展名}。
    反推不出来（用户手工改过地址）返回 None，交给"按文件名"兜底判断。"""
    stem, ext = os.path.splitext(img_filename)
    prefix = "appdata/Image/"
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix):]
    marker = f"_{stem}_"
    pos = rest.rfind(marker)      # 随机段不含下划线，rfind 拿到的就是真正的分隔点
    if pos < 0:
        return None
    tail = rest[pos + len(marker):]
    if len(tail) != 6 + len(ext) or not tail.endswith(ext):
        return None
    return rest[:pos]


def _parse_md(lines):
    """解析已有 md，返回 (图片块列表, 小节表)
    图片块：{"start":起始行, "end":结束行(不含), "filename":原图名, "url":地址, "section":所在##标题}
    小节表：{标题: [起始行, 结束行(不含)]}（同名小节合并范围）"""
    sec_lines = []
    for i, line in enumerate(lines):
        m = MD_SECTION_RE.match(line)
        if m:
            sec_lines.append((i, m.group("title")))

    sections = {}
    for k, (i, title) in enumerate(sec_lines):
        end = sec_lines[k + 1][0] if k + 1 < len(sec_lines) else len(lines)
        if title in sections:
            sections[title][0] = min(sections[title][0], i)
            sections[title][1] = max(sections[title][1], end)
        else:
            sections[title] = [i, end]

    records = []
    cur_title = None
    cur_k = -1
    for i, line in enumerate(lines):
        while cur_k + 1 < len(sec_lines) and sec_lines[cur_k + 1][0] <= i:
            cur_k += 1
            cur_title = sec_lines[cur_k][1]
        m = MD_IMAGE_LINE_RE.match(line)
        if not m:
            continue
        end = i + 1
        if end < len(lines) and lines[end].lstrip().startswith(MD_FILE_NOTE_PREFIX):
            end += 1
        if end < len(lines) and not lines[end].strip():
            end += 1
        records.append({"start": i, "end": end,
                        "filename": m.group("alt").strip(),
                        "url": m.group("url").strip(),
                        "section": cur_title,
                        "key": natural_key(m.group("alt").strip())})
    return records, sections


def _sorted_section(records):
    """按文件名自然排序后的记录 + 键列表（供二分查找插入位置）"""
    ordered = sorted(records, key=lambda r: r["key"])
    return ordered, [r["key"] for r in ordered]


def _insert_before_next(ordered, keys, filename, section_end):
    """按文件名（自然排序）找"排在它后面的第一张已被引用的图"，插到那张图的文本上方；
    后面的图都没被引用（它本来就是最后一张）时，插到小节末尾。"""
    pos = bisect_right(keys, natural_key(filename))
    return ordered[pos]["start"] if pos < len(ordered) else section_end


def _read_md_file(md_file_path):
    """读取 md：返回 (文本行, 是否带 BOM, 原换行符)，尽量保留原文件风格"""
    with open(md_file_path, "rb") as f:
        raw = f.read()
    has_bom = raw.startswith(codecs.BOM_UTF8)
    text = None
    for enc in (("utf-8-sig" if has_bom else "utf-8"), "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="ignore")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return text.splitlines(), has_bom, newline


def _write_md_file(md_file_path, lines, has_bom, newline):
    data = "\n".join(lines)
    with open(md_file_path, "w", encoding="utf-8-sig" if has_bom else "utf-8",
              newline=newline) as f:
        f.write(data)


def _create_image_md(md_file_path, images, root_dir_name, resource_dir,
                     show_folder_title, show_original_filename, log):
    md_lines = [""]
    cur_folder = None
    count = 0
    for rel_folder, img_filename, src_img in images:
        if show_folder_title and rel_folder != cur_folder:
            cur_folder = rel_folder
            md_lines.append(f"## {_display_folder(root_dir_name, rel_folder)}")
            md_lines.append("")
        url = _copy_image(src_img, resource_dir,
                          _rel_underline(root_dir_name, rel_folder), img_filename)
        md_lines.extend(_md_block(img_filename, url, show_original_filename))
        count += 1
        if log and count % 200 == 0:
            log(f"  已处理 {count} 张图片...")

    _write_md_file(md_file_path, md_lines, False, os.linesep)
    if log:
        log(f"新建 Markdown，共整理 {count} 张图片。")
    return {"mode": "create", "added": count, "existing": 0}


def _update_image_md(md_file_path, images, root_dir_name, resource_dir,
                     show_folder_title, show_original_filename, log):
    """更新模式：只把「还没被 md 引用」的图片复制过去，并按文件名排序插入到对应位置，
    已有内容（包括用户自己写的文字）一律不动。"""
    lines, has_bom, newline = _read_md_file(md_file_path)
    records, sections = _parse_md(lines)

    # 已引用判定：优先按（目录前缀 + 原文件名）精确匹配；地址被手工改过的按文件名兜底
    referenced = set()
    loose_names = set()
    for rec in records:
        folder = _ref_folder_underline(rec["url"], rec["filename"])
        if folder is None:
            loose_names.add(rec["filename"])
        else:
            referenced.add((folder, rec["filename"]))

    new_items = []
    for rel_folder, img_filename, src_img in images:
        underline = _rel_underline(root_dir_name, rel_folder)
        if (underline, img_filename) in referenced or img_filename in loose_names:
            continue
        new_items.append((rel_folder, img_filename, src_img))

    if not new_items:
        if log:
            log(f"更新模式：没有新增图片（已引用 {len(records)} 张），Markdown 未改动。")
        return {"mode": "update", "added": 0, "existing": len(records)}

    by_section = {}
    for rec in records:
        by_section.setdefault(rec["section"], []).append(rec)

    # 每个小节的记录按文件名排好序，插入位置用二分查找（上万张图也不慢）
    index_cache = {}

    def section_index(title):
        if title not in index_cache:
            index_cache[title] = _sorted_section(by_section.get(title, []))
        return index_cache[title]

    section_mode = bool(sections)   # 已有 md 是「## 分节」结构吗
    inserts = {}        # 行号 -> [块内容...]
    new_sections = {}   # 新出现的小节标题 -> [块内容...]

    def add_block(idx, block):
        inserts.setdefault(idx, []).extend(block)

    for rel_folder, img_filename, src_img in new_items:
        url = _copy_image(src_img, resource_dir,
                          _rel_underline(root_dir_name, rel_folder), img_filename)
        block = _md_block(img_filename, url, show_original_filename)
        folder_title = _display_folder(root_dir_name, rel_folder)
        if not section_mode:
            ordered, keys = section_index(None)
            add_block(_insert_before_next(ordered, keys, img_filename, len(lines)), block)
        elif folder_title in sections:
            ordered, keys = section_index(folder_title)
            add_block(_insert_before_next(ordered, keys, img_filename,
                                          sections[folder_title][1]), block)
        else:
            new_sections.setdefault(folder_title, []).extend(block)

    # 从后往前插，行号不会互相影响；同一位置的多个块按遍历顺序（=文件名排序）排列
    for idx in sorted(inserts, reverse=True):
        lines[idx:idx] = inserts[idx]

    if new_sections and show_folder_title:
        if lines and lines[-1].strip():
            lines.append("")
        for title in sorted(new_sections, key=natural_key):
            lines.append(f"## {title}")
            lines.append("")
            lines.extend(new_sections[title])
    elif new_sections:
        # md 里没有分节结构时，也不额外造小节标题，直接补在末尾
        if lines and lines[-1].strip():
            lines.append("")
        for title in sorted(new_sections, key=natural_key):
            lines.extend(new_sections[title])

    _write_md_file(md_file_path, lines, has_bom, newline)
    if log:
        log(f"更新模式：新增 {len(new_items)} 张图片，已按文件名顺序插入；"
            f"原有 {len(records)} 张保持不变。")
        for _rel, name, _src in new_items[:20]:
            log(f"  + {name}")
        if len(new_items) > 20:
            log(f"  ...（其余 {len(new_items) - 20} 张见 md）")
    return {"mode": "update", "added": len(new_items), "existing": len(records)}


def generate_image_md(root_input_dir, custom_dir_name, scan_subdir,
                      show_folder_title, show_original_filename, log=None):
    # 输出目录：源目录 / 自定义目录名
    output_root = os.path.join(root_input_dir, custom_dir_name)
    resource_dir = os.path.join(output_root, "appdata", "Image")
    os.makedirs(resource_dir, exist_ok=True)

    # md文件名：源目录名称，例如源目录 D:\照片 -> 照片.md
    root_dir_name = os.path.basename(os.path.abspath(root_input_dir))
    md_file_path = os.path.join(output_root, f"{root_dir_name}.md")

    images = _iter_images(root_input_dir, output_root, scan_subdir)

    exists = os.path.isfile(md_file_path)
    if exists:
        # 空文件当作新建，其余一律走更新模式（不动已有内容）
        try:
            empty = os.path.getsize(md_file_path) == 0
        except OSError:
            empty = False
        exists = not empty

    if exists:
        if log:
            log("检测到已有 Markdown，进入更新模式（只追加新增图片）...")
        stats = _update_image_md(md_file_path, images, root_dir_name, resource_dir,
                                 show_folder_title, show_original_filename, log)
    else:
        stats = _create_image_md(md_file_path, images, root_dir_name, resource_dir,
                                 show_folder_title, show_original_filename, log)
    stats["md_path"] = md_file_path
    stats["resource_dir"] = resource_dir
    return md_file_path, resource_dir, stats


# ============================================================
# ⑪ TXT转图片 —— 原 txt2img.py
# ============================================================

def read_text_auto(file_path):
    """自动猜测编码读取文本；有 chardet 时优先使用其检测结果"""
    with open(file_path, "rb") as f:
        raw = f.read()
    if not raw:
        return ""
    encodings = []
    if _module_available("chardet"):
        try:
            import chardet
            det = chardet.detect(raw)
            if det and det.get("encoding"):
                encodings.append(det["encoding"])
        except Exception:
            pass
    encodings += ["utf-8-sig", "utf-8", "gbk", "gb2312", "big5", "latin-1"]
    for enc in encodings:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def wrap_text_by_chinese(text, max_chars=50):
    """中文专用换行：一行超过 max_chars 个字符就换行，优先按标点拆分"""
    if not text:
        return []
    lines = []
    current_line = ""
    for char in text:
        current_line += char
        if len(current_line) >= max_chars:
            split_pos = -1
            for punc in ["，", "。", "！", "？", "；", "：", " ", "\t"]:
                pos = current_line.rfind(punc, 0, max_chars)
                if pos > max_chars // 2:
                    split_pos = pos
                    break
            if split_pos != -1:
                lines.append(current_line[:split_pos + 1])
                current_line = current_line[split_pos + 1:]
            else:
                lines.append(current_line[:max_chars])
                current_line = current_line[max_chars:]
    if current_line.strip():
        lines.append(current_line)
    return lines


def process_single_txt(filename, input_dir, output_dir, font_path, font_size, max_chars_per_line):
    from PIL import Image, ImageDraw, ImageFont
    txt_path = os.path.join(input_dir, filename)
    base_name = os.path.splitext(filename)[0]
    output_path = os.path.join(output_dir, base_name + ".png")
    # 防止覆盖：目标已存在则自动加 _1、_2 后缀
    if os.path.exists(output_path):
        i = 1
        while os.path.exists(os.path.join(output_dir, f"{base_name}_{i}.png")):
            i += 1
        output_path = os.path.join(output_dir, f"{base_name}_{i}.png")
    try:
        text = read_text_auto(txt_path)
        if not text.strip():
            return (filename, "skipped", "文件为空")
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception as e:
            return (filename, "failed", f"字体加载失败: {e}")

        all_lines = []
        for manual_line in text.split("\n"):
            if manual_line.strip() == "":
                all_lines.append("")
                continue
            all_lines.extend(wrap_text_by_chinese(manual_line, max_chars_per_line))
        if not all_lines:
            all_lines = [""]

        bbox = font.getbbox("中")
        line_height = bbox[3] - bbox[1] + 6
        img = Image.new("RGB", (950, len(all_lines) * line_height + 20), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        y = 10
        for line in all_lines:
            draw.text((10, y), line, font=font, fill=(0, 0, 0))
            y += line_height
        img.save(output_path)
        return (filename, "success", "")
    except Exception as e:
        return (filename, "failed", str(e))


def txt_to_images(input_dir, output_dir, font_path, font_size, max_chars_per_line, max_workers, log):
    if not _module_available("PIL"):
        raise RuntimeError("缺少依赖 pillow，请先执行：pip install pillow")
    if not os.path.isdir(input_dir):
        raise RuntimeError(f"TXT目录不存在：{input_dir}")
    if not os.path.isfile(font_path):
        raise RuntimeError(f"字体文件不存在：{font_path}\n可改用 C:/Windows/Fonts/msyh.ttc 等系统中文字体")
    os.makedirs(output_dir, exist_ok=True)
    txt_files = sorted(f for f in os.listdir(input_dir)
                       if f.lower().endswith(".txt") and not f.startswith(".")
                       and os.path.isfile(os.path.join(input_dir, f)))
    if not txt_files:
        log("未找到TXT文件。")
        return
    log(f"共 {len(txt_files)} 个TXT文件，{max_workers} 线程并发转换...")
    results = []
    done_count = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_single_txt, f, input_dir, output_dir,
                            font_path, font_size, max_chars_per_line): f
            for f in txt_files
        }
        for fut in as_completed(futures):
            results.append(fut.result())
            done_count += 1
            if done_count % 100 == 0:
                log(f"  进度 {done_count}/{len(txt_files)}")
    success = sum(1 for r in results if r[1] == "success")
    fail = sum(1 for r in results if r[1] == "failed")
    skipped = sum(1 for r in results if r[1] == "skipped")
    log(f"转换统计：成功 {success} 个，失败 {fail} 个，跳过空文件 {skipped} 个")
    failed_files = [r[0] for r in results if r[1] == "failed"]
    if failed_files:
        log("失败文件：" + "、".join(failed_files[:50]))


# ============================================================
# ⑫ TXT合并 —— 原 txt合并.py
# ============================================================

def merge_files(target_dir, target_suffix, scan_subdir):
    if not os.path.isdir(target_dir):
        return False, f"错误：路径「{target_dir}」不是有效文件夹！"

    split_line = "\n" + "=" * 50 + "\n"
    file_list = []
    if scan_subdir:
        for root, _, files in os.walk(target_dir):
            for fname in files:
                if fname.endswith(target_suffix):
                    file_list.append((os.path.join(root, fname), fname))
    else:
        for name in os.listdir(target_dir):
            full_path = os.path.join(target_dir, name)
            if os.path.isfile(full_path) and name.endswith(target_suffix):
                file_list.append((full_path, name))

    if not file_list:
        return False, f"提示：未找到任何 {target_suffix} 文件！"

    success_count = 0
    error_log = ""
    total_text = ""
    for index, (fpath, fname) in enumerate(file_list):
        try:
            content = read_text_auto(fpath)
            block = f"===== 文件：{fname} 路径：{fpath} =====\n{content}"
            if index != len(file_list) - 1:
                block += split_line
            total_text += block
            success_count += 1
        except Exception as e:
            error_log += f"读取失败 {fpath}：{str(e)}\n"
            continue

    output_name = f"全部合并结果{target_suffix}"
    output_path = os.path.join(target_dir, output_name)
    # 防止覆盖：已存在则自动加 _1、_2 后缀
    if os.path.exists(output_path):
        i = 1
        while os.path.exists(os.path.join(target_dir, f"全部合并结果{target_suffix}_{i}")):
            i += 1
        output_path = os.path.join(target_dir, f"全部合并结果{target_suffix}_{i}")
        output_name = os.path.basename(output_path)
    try:
        with open(output_path, "w", encoding="utf-8") as fw:
            fw.write(total_text)
    except Exception as e:
        error_log += f"写入合并文件失败：{str(e)}\n"

    mode_text = "【递归遍历所有子目录】" if scan_subdir else "【仅当前文件夹，不扫描子目录】"
    result = f"{mode_text}\n处理完成\n共读取文件：{success_count} 个\n合并文件保存至：{output_path}"
    if error_log:
        result += f"\n\n【错误记录】\n{error_log}"
    return True, result


# ============================================================
# ⑬ TXT重命名 —— 原 txt重命名.py
# ============================================================

def clean_filename(s):
    """清理文件名中的非法字符"""
    if not s:
        return "未命名"
    s = s.replace("\x00", "")
    invalid_chars = '\\/:*?"<>|'
    for c in invalid_chars:
        s = s.replace(c, "")
    s = s.strip()
    return s[:200] if s else "未命名"


def rename_txt_by_first_line(src_dir, log):
    """用每个 TXT 第一行的文字重命名文件"""
    files = sorted(f for f in os.listdir(src_dir)
                   if os.path.isfile(os.path.join(src_dir, f)) and f.lower().endswith(".txt"))
    if not files:
        log("未找到TXT文件。")
        return 0
    log(f"共 {len(files)} 个TXT文件，开始按首行重命名...")
    ok = fail = skip = 0
    for fname in files:
        file_old = os.path.join(src_dir, fname)
        try:
            text = read_text_auto(file_old)
            lines = text.splitlines()
            if not lines or not lines[0].strip():
                log(f"【跳过】{fname} 首行为空")
                skip += 1
                continue
            clean_name = clean_filename(lines[0])
            target_filename = f"{clean_name}.txt"
            if fname == target_filename:
                skip += 1
                continue
            file_new = os.path.join(src_dir, target_filename).replace("\x00", "")
            idx = 1
            while os.path.exists(file_new):
                file_new = os.path.join(src_dir, f"{clean_name}_{idx}.txt")
                idx += 1
            os.rename(file_old, file_new)
            ok += 1
            if ok % 200 == 0:
                log(f"  已重命名 {ok} 个...")
        except Exception as e:
            fail += 1
            log(f"【失败】{fname} -> {e}")
    log(f"重命名完成：成功 {ok} 个，跳过 {skip} 个，失败 {fail} 个。")
    return ok


# ============================================================


def build_md_tab(parent):
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(2, weight=1)
    make_tip(parent, 0, "把目录中的图片整理成 Markdown：图片复制到 输出文件夹/appdata/Image 并生成 源目录名.md；"
                        "已存在同名 md 时只追加新增图片，不改动已有内容。",
             colspan=1, autowrap=True)

    form = make_form(parent)
    form.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 2))

    var_src = make_path_row(form, 0, "源目录：", title="选择源目录")
    # 输出文件夹名：默认“记录”，下拉可选“a3”，也支持直接输入自定义名称
    form_label(form, 1, "输出文件夹名：")
    var_out = tk.StringVar(value="记录")
    ttk.Combobox(form, textvariable=var_out, width=24,
                 values=["记录", "a3"]).grid(row=1, column=1, sticky="w", padx=(0, 6), pady=5)
    form_note(form, 1, "在源目录下创建该文件夹，可自定义输入")

    # 三个复选框放在输入列，与上方输入框左对齐
    check_row = ttk.Frame(form)
    check_row.grid(row=2, column=1, sticky="w", pady=2)
    var_sub = make_check(check_row, "递归遍历子目录", default=True, row=0, column=0)
    var_folder = make_check(check_row, "md添加文件夹名称", default=True, row=0, column=1)
    var_fname = make_check(check_row, "md添加原图名称", default=True, row=0, column=2)

    text_log, log = make_log_panel(parent, 2, colspan=1)
    result = {}

    def start():
        src = clean_path(var_src.get())
        out_name = clean_path(var_out.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请先填写有效的源目录！")
            return
        if not out_name:
            messagebox.showerror("错误", "输出文件夹名称不能为空！")
            return
        clear_log(text_log)
        result.clear()

        def work():
            md_path, res_path, stats = generate_image_md(
                src, out_name, var_sub.get(), var_folder.get(), var_fname.get(), log)
            result.update(stats)
            log(f"{'更新' if stats['mode'] == 'update' else '生成'}成功：{md_path}")

        def done(err):
            if err:
                return
            st = result
            if st.get("mode") == "update":
                if st.get("added"):
                    msg = (f"更新完成：新增 {st['added']} 张图片，已按文件名顺序插入。\n"
                           f"原有 {st['existing']} 张保持不变。")
                else:
                    msg = f"没有新增图片（已引用 {st['existing']} 张），md 未改动。"
            else:
                msg = f"Markdown 生成成功，共整理 {st.get('added', 0)} 张图片。"
            messagebox.showinfo("完成", msg)

        run_task(btn, log, work, on_done=done)

    btn = make_start_button(form, 3, "开始生成", start)


def build_txt2img_tab(parent):
    parent.grid_rowconfigure(8, weight=1)
    make_tip(parent, 0, "把 TXT 文件批量渲染为 PNG 图片（中文自动换行）。需要 pillow 依赖。")
    var_in = make_path_row(parent, 1, "TXT目录：", title="选择TXT目录")
    var_out = make_path_row(parent, 2, "输出目录：", title="选择输出目录")
    var_font = make_file_row(parent, 3, "字体文件：", default="C:/Windows/Fonts/simhei.ttf", title="选择字体文件")
    var_size = make_spin_row(parent, 4, "字体大小：", 16, 6, 200)
    var_chars = make_spin_row(parent, 5, "每行字数：", 50, 5, 500)
    var_workers = make_spin_row(parent, 6, "并发线程：", 8, 1, 64)
    text_log, log = make_log_panel(parent, 8)

    def start():
        in_dir = clean_path(var_in.get())
        out_dir = clean_path(var_out.get())
        font = clean_path(var_font.get())
        if not in_dir or not os.path.isdir(in_dir):
            messagebox.showerror("错误", "请填写有效的TXT目录！")
            return
        if not out_dir:
            messagebox.showerror("错误", "请填写输出目录！")
            return
        if not font:
            messagebox.showerror("错误", "请填写字体文件路径！")
            return
        try:
            size = int(var_size.get())
            chars = int(var_chars.get())
            workers = int(var_workers.get())
        except ValueError:
            messagebox.showerror("错误", "字体大小/每行字数/线程数必须是数字！")
            return
        clear_log(text_log)
        run_task(btn, log,
                 lambda: txt_to_images(in_dir, out_dir, font, size, chars, workers, log),
                 on_done=lambda err: None if err else messagebox.showinfo("完成", "转换结束，详情见日志。"))

    btn = make_start_button(parent, 7, "开始转换", start)


def build_merge_tab(parent):
    parent.grid_rowconfigure(4, weight=1)
    make_tip(parent, 0, "把指定后缀的文件合并为一个文件，每个文件前会带上原文件名与路径。")
    var_dir = make_path_row(parent, 1, "目标文件夹：", title="选择目标文件夹")
    ttk.Label(parent, text="目标后缀：").grid(row=2, column=0, sticky="w", padx=6, pady=5)
    var_suffix = tk.StringVar(value=".txt")
    ttk.Combobox(parent, textvariable=var_suffix, width=12,
                 values=[".txt", ".srt", ".md", ".log"]).grid(row=2, column=1, sticky="w", padx=6)
    ttk.Label(parent, text="可直接输入自定义后缀", foreground=COLOR["text_secondary"]).grid(row=2, column=2, sticky="w", padx=4)
    var_sub = make_check(parent, "递归遍历子目录", default=False, row=2, column=3)
    text_log, log = make_log_panel(parent, 4)

    def start():
        dir_path = clean_path(var_dir.get())
        suffix = var_suffix.get().strip()
        if not dir_path or not os.path.isdir(dir_path):
            messagebox.showerror("错误", "请先填写有效的目标文件夹！")
            return
        if not suffix:
            messagebox.showwarning("提醒", "请选择需要合并的文件后缀！")
            return
        clear_log(text_log)

        def work():
            ok, msg = merge_files(dir_path, suffix, var_sub.get())
            for line in msg.splitlines():
                log(line)

        run_task(btn, log, work,
                 on_done=lambda err: (messagebox.showerror("失败", "合并失败，详情见日志。") if err
                                      else messagebox.showinfo("完成", "合并结束，详情见日志。")))

    btn = make_start_button(parent, 3, "开始合并", start)


def build_txt_rename_tab(parent):
    parent.grid_rowconfigure(3, weight=1)
    make_tip(parent, 0, "把 TXT 文件用它第一行的文字重命名（自动清理非法字符、自动去重）。")
    var_dir = make_path_row(parent, 1, "目标文件夹：", title="选择目标文件夹")
    text_log, log = make_log_panel(parent, 3)

    def start():
        dir_path = clean_path(var_dir.get())
        if not dir_path or not os.path.isdir(dir_path):
            messagebox.showerror("错误", "请先填写有效的目标文件夹！")
            return
        clear_log(text_log)
        run_task(btn, log, lambda: rename_txt_by_first_line(dir_path, log),
                 on_done=lambda err: None if err else messagebox.showinfo("完成", "重命名结束，详情见日志。"))

    btn = make_start_button(parent, 2, "开始重命名", start)
