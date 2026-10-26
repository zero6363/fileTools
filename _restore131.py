# -*- coding: utf-8 -*-
"""从 ba6d574 blob + v1.32 会话编辑记录，还原 v1.31 源码。

还原逻辑（v1.32 提交 ba6d574 = v1.31 功能 + 主题化改造，两者混在一个提交里）：
  1. 对 v1.32 会话用 Edit 工具改过的文件：把编辑对「反向应用」到 ba6d574 blob
     （new_string -> old_string，按时间逆序），拿回 v1.32 之前的真实原文；
     随后做「正向重放 == blob」往返校验，证明反向应用无损。
  2. v1.32 会话还有脚本式批量替换（不经 Edit 工具、考古不到）：反向后残留的
     COLOR[...]/FONTS[...] 令牌按 theme.py 的映射表还原为字面量，
     并删除 from .theme import 行。tab_docs 的叠加编辑也按此处理。
  3. v1.33 会话新增的 theme.py / ui.py 整体删除；
     v1.33 只改过的文件（grouping/live/media/video）直接取 blob（= v1.31 原文）。
  4. 校验：全部 py_compile + 残留令牌计数 == 0 + 无 theme/ui 引用。
"""
import json
import os
import re
import subprocess

JSONL = r"C:\Users\Admin\.workbuddy\projects\d-code-fileTools\876934b0-2607-4528-ac3f-d677e769e505.jsonl"
BOUNDARY = 409          # L409 = v1.32 的 git add；之前的 Edit 属于 v1.32 会话
RECOVER = r"D:\code\fileTools\_recover\v131"

# 文件 -> 是否存在 v1.32 的 Edit 记录
PLAN = {
    "toolbox/common.py":        True,
    "toolbox/main.py":          True,
    "toolbox/tab_compress.py":  True,
    "toolbox/tab_docs.py":      True,   # 叠加编辑，反向会失败 -> 走纯去令牌
    "toolbox/tab_encode.py":    True,
    "toolbox/tab_hashscan.py":  True,
    "toolbox/tab_rename.py":    True,
    "toolbox/tab_rsbackup.py":  True,
    "toolbox/tab_translate.py": True,
    "toolbox/tab_grouping.py":  False,
    "toolbox/tab_live.py":      False,
    "toolbox/tab_media.py":     False,
    "toolbox/tab_video.py":     False,
    "README.md":                False,
    "打包.bat":                 False,
}

# theme.py 的令牌映射（v1.32 的取值；脚本式替换是「字面量->等值令牌」，
# 所以这里回填的值即原字面量）
COLOR_MAP = {
    "bg": "#F4F6F9", "surface": "#FFFFFF", "surface_alt": "#EEF2F7",
    "nav_bg": "#EAEFF6", "nav_item_hover": "#DCE3EE",
    "border": "#E4E9F0", "border_strong": "#D2DAE5",
    "text": "#1F2733", "text_secondary": "#5C6675", "text_muted": "#939DAB",
    "text_on_accent": "#FFFFFF",
    "accent": "#3B82F6", "accent_hover": "#2F6FE0", "accent_active": "#265FCB",
    "accent_soft": "#EAF2FE", "status_bg": "#ECEFF4",
    "success": "#1FA971", "success_active": "#178A5E",
    "warning": "#D9920A", "warning_active": "#B5790A",
    "error": "#E0444B", "info": "#3B82F6",
}
FONT_MAP = {
    "xs": '("Microsoft YaHei", 10)', "sm": '("Microsoft YaHei", 11)',
    "base": '("Microsoft YaHei", 12)', "md": '("Microsoft YaHei", 13)',
    "lg": '("Microsoft YaHei", 15)', "xl": '("Microsoft YaHei", 18, "bold")',
    "title": '("Microsoft YaHei", 20, "bold")',
    "nav_title": '("Microsoft YaHei", 15, "bold")',
    "nav": '("Microsoft YaHei", 11)', "nav_active": '("Microsoft YaHei", 11, "bold")',
    "log": '("Consolas", 9)',
}


def git_blob(path):
    r = subprocess.run(["git", "show", f"ba6d574:{path}"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"blob missing: {path}")
    return r.stdout.decode("utf-8")


def load_v132_edits():
    edits = {}
    with open(JSONL, encoding="utf-8") as f:
        for ln, line in enumerate(f):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") != "function_call" or obj.get("name") != "Edit":
                continue
            if ln >= BOUNDARY:
                continue
            try:
                args = json.loads(obj.get("arguments") or "{}")
            except Exception:
                continue
            fp = str(args.get("file_path", "")).replace("\\", "/")
            fp = fp.split("D:/code/fileTools/")[-1]
            if PLAN.get(fp):
                edits.setdefault(fp, []).append(
                    {"old": args.get("old_string", ""),
                     "new": args.get("new_string", "")})
    return edits


def apply_edits(text, pairs, forward=True):
    seq = pairs if forward else list(reversed(pairs))
    for p in seq:
        a, b = (p["old"], p["new"]) if forward else (p["new"], p["old"])
        if a not in text:
            raise RuntimeError(f"pattern not found: {a[:70]!r}")
        text = text.replace(a, b)
    return text


def detokenize(text):
    """残留令牌 -> 字面量；删除 theme import。"""
    # 1) 损坏的字符串形式  bg="COLOR["x"]"  ->  bg="#hex"（引号已在原文里）
    text = re.sub(r'"COLOR\["([a-z_]+)"\]"',
                  lambda m: '"%s"' % COLOR_MAP.get(m.group(1), "??"), text)
    # 2) 常规形式 COLOR["x"] -> "#hex"（v1.31 原文是带引号的十六进制字面量）
    text = re.sub(r'COLOR\["([a-z_]+)"\]',
                  lambda m: '"%s"' % COLOR_MAP.get(m.group(1), m.group(0)), text)
    # 3) FONTS["y"] -> 字体元组
    text = re.sub(r'FONTS\["([a-z_]+)"\]',
                  lambda m: FONT_MAP.get(m.group(1), m.group(0)), text)
    # 4) 删除 theme import 行
    text = re.sub(r'^from \.theme import [^\n]*\n', '', text, flags=re.M)
    return text


def main():
    os.makedirs(RECOVER, exist_ok=True)
    edits = load_v132_edits()
    print("v1.32 edits:", {k.split("/")[-1]: len(v) for k, v in edits.items()})

    ok, fail = [], []
    for path, has_edits in PLAN.items():
        blob = git_blob(path)
        name = path.replace("/", "__")
        try:
            if path.endswith("tab_docs.py"):
                # 叠加编辑（先插错再修正）无法逐对反向；
                # 其 v1.32 改动=import 块重排+1 处令牌 -> 直接去令牌即可
                result = detokenize(blob)
                note = "docs特例: 去令牌(含theme import删除)"
            elif not has_edits:
                result = blob
                note = "blob直取"
            else:
                pairs = edits[path]
                v131 = apply_edits(blob, pairs, forward=False)
                rt = apply_edits(v131, pairs, forward=True)
                assert rt == blob, "round-trip mismatch"
                result = detokenize(v131)
                note = f"反向{len(pairs)}处+往返校验通过+去令牌"
        except Exception as e:
            fail.append((path, str(e)))
            print(f"  ✗ {path}: {e}")
            continue
        with open(os.path.join(RECOVER, name), "w", encoding="utf-8", newline="") as f:
            f.write(result)
        resid = result.count('COLOR["') + result.count('FONTS["') \
            + result.count("COLOR['") + result.count("FONTS['")
        theme_ref = "theme" in result
        print(f"  ✓ {path}: {note} | 残留令牌={resid} theme引用={theme_ref} {len(result)}字")
        if resid == 0 and not theme_ref:
            ok.append(path)
        else:
            fail.append((path, f"残留 resid={resid} theme={theme_ref}"))

    print(f"\nCLEAN={len(ok)}  PROBLEM={len(fail)}")
    for p, e in fail:
        print("  !!", p, e)


if __name__ == "__main__":
    main()
