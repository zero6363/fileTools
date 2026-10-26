# -*- coding: utf-8 -*-
"""从 WorkBuddy 会话 jsonl 提取 Read/Write/Edit 事件 + 文件备份快照。"""
import json
import os
import sys

TARGETS = {"common.py", "main.py", "tab_compress.py", "tab_docs.py",
           "tab_encode.py", "tab_rename.py", "tab_rsbackup.py",
           "tab_hashscan.py", "tab_translate.py", "README.md",
           "tab_grouping.py", "tab_live.py", "tab_media.py", "tab_video.py",
           "theme.py", "ui.py", "打包.bat", ".gitignore"}


def base_name(fp):
    return str(fp).replace("\\", "/").rstrip("/").split("/")[-1]


def main():
    src, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    calls = {}          # callId -> (name, args)
    seq = []            # 按顺序的事件
    backups = []        # 文件备份快照
    with open(src, encoding="utf-8") as f:
        for ln, line in enumerate(f):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            if t == "function_call":
                name = obj.get("name", "")
                try:
                    args = json.loads(obj.get("arguments") or "{}")
                except Exception:
                    args = {}
                calls[obj.get("callId")] = (name, args, ln)
            elif t == "function_call_result":
                cid = obj.get("callId")
                if cid in calls:
                    name, args, cln = calls[cid]
                    out = obj.get("output") or {}
                    text = out.get("text", "") if isinstance(out, dict) else str(out)
                    seq.append(dict(kind=name, args=args, text=text,
                                    call_line=cln, result_line=ln))
            elif t == "file-history-snapshot":
                snap = (obj.get("snapshot") or {}).get("trackedFileBackups") or {}
                if snap:
                    backups.append((ln, snap))

    # 统计目标文件事件
    stat = {}
    for e in seq:
        fp = str(e["args"].get("file_path", ""))
        b = base_name(fp)
        if b in TARGETS:
            stat.setdefault(b, []).append(e)
    print(f"{os.path.basename(src)}: tool events={len(seq)} backup snaps={len(backups)}")
    for b in sorted(stat):
        evs = stat[b]
        kinds = {}
        for e in evs:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        print(f"  {b}: {kinds}")

    # 导出 Read 结果（剥行号）与 Write/Edit 参数
    for b, evs in stat.items():
        reads = [e for e in evs if e["kind"] == "Read"]
        for i, e in enumerate(reads):
            txt = strip_catn(e["text"])
            with open(os.path.join(outdir, f"{b}.r{i:03d}.L{e['result_line']}.txt"),
                      "w", encoding="utf-8") as f:
                f.write(f"@@ARGS={json.dumps(e['args'], ensure_ascii=False)}\n")
                f.write(txt)
        for i, e in enumerate([x for x in evs if x["kind"] == "Write"]):
            with open(os.path.join(outdir, f"{b}.w{i:03d}.L{e['call_line']}.txt"),
                      "w", encoding="utf-8") as f:
                f.write(e["args"].get("content") or "")
        edits = [e for e in evs if e["kind"] == "Edit"]
        if edits:
            with open(os.path.join(outdir, f"{b}.edits.json"), "w", encoding="utf-8") as f:
                json.dump([{"old": e["args"].get("old_string"),
                            "new": e["args"].get("new_string"),
                            "call_line": e["call_line"],
                            "replace_all": e["args"].get("replace_all", False)}
                           for e in edits], f, ensure_ascii=False, indent=1)

    # 导出备份快照索引
    if backups:
        with open(os.path.join(outdir, "_backups.json"), "w", encoding="utf-8") as f:
            json.dump([{"line": ln, "backups": snap} for ln, snap in backups],
                      f, ensure_ascii=False, indent=1)


def strip_catn(text):
    out = []
    for raw in text.split("\n"):
        if "→" in raw:
            head, rest = raw.split("→", 1)
            if head.strip().isdigit():
                out.append(rest)
                continue
        out.append(raw)
    return "\n".join(out)


if __name__ == "__main__":
    main()
