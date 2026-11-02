# -*- coding: utf-8 -*-
"""⑳ Robocopy 极速复制

封装 Windows 自带的 robocopy.exe，在两个文件夹之间快速复制：
  · /MT:n    多线程并发复制，机械硬盘 / SSD / 网络共享都能吃满带宽；
  · /Z       断点续传，单个大文件传到一半中断后，重新开始会接着传；
  · /R /W    限制失败重试次数和等待时间，避免被占用文件卡住一整晚；
  · /XJ      跳过目录联接点，防止目录环导致无限递归；
  · /NFL /NDL（默认）不在日志里逐条列文件，海量小文件也不会把界面刷爆。

重新点击「开始复制」本身也能续传：目标里已经完整的文件会被 Robocopy
判定为相同文件直接跳过，只有没传完 / 有变化的文件才会重新处理。
同名但内容 / 时间不同的文件以源文件为准覆盖（目标非空时界面会先确认）；
本工具不启用 /MIR，绝不会删除目标文件夹里多出来的文件。
"""
import ctypes
import locale
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox

from . import config
from . import ui
from .common import (ScrollPage, clean_path, clear_log, make_check, make_form,
                     make_logger, make_num_row, make_path_row, make_tip)
from .theme import COLOR, FONTS

CONFIG_SECTION = "speedcopy"

# Windows 进程创建标志：不弹出黑色控制台窗口
_CREATE_NO_WINDOW = 0x08000000
# Robocopy 输出里的分隔线只由这些字符组成
_SEP_CHARS = set("-=")


# ============================================================
# Robocopy 调用
# ============================================================

def find_robocopy():
    """返回 robocopy.exe 的完整路径；找不到返回空串。"""
    roots = []
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    # 32 位 Python 在 64 位 Windows 上访问 System32 会被重定向到 SysWOW64，
    # 两个位置都找一遍；Sysnative 只在 32 位进程里存在。
    roots.append(os.path.join(system_root, "Sysnative", "robocopy.exe"))
    roots.append(os.path.join(system_root, "System32", "robocopy.exe"))
    found = shutil.which("robocopy")
    if found:
        roots.append(found)
    for path in roots:
        if path and os.path.isfile(path):
            return path
    return ""


def build_robocopy_args(src, dst, threads=16, restart=True, retry=1, wait=2,
                        show_files=False, exe=None):
    """拼出 robocopy 参数列表（不经过 shell，路径里有空格也不用自己加引号）。"""
    exe = exe or find_robocopy()
    if not exe:
        raise RuntimeError("未找到 Windows 自带的 robocopy.exe，无法执行复制。")
    args = [
        exe, src, dst,
        "/E",                       # 含子文件夹（包括空目录）
        "/MT:%d" % max(1, int(threads)),
        "/R:%d" % max(0, int(retry)),
        "/W:%d" % max(0, int(wait)),
        "/NP",                      # 不输出实时百分比，避免刷屏
        "/NJH",                     # 不输出文件头（参数我们自己写进日志）
        "/XJ",                      # 跳过目录联接点，避免目录环
        "/DCOPY:DAT",               # 目录时间戳也一起复制
    ]
    if restart:
        args.append("/Z")           # 断点续传
    if not show_files:
        args.extend(["/NFL", "/NDL"])   # 不逐条列文件 / 目录
    return args


def _oem_encoding():
    """Robocopy 控制台输出使用系统 OEM 代码页（中文系统通常是 cp936）。"""
    try:
        cp = int(ctypes.windll.kernel32.GetOEMCP())
        if cp > 0:
            return "cp%d" % cp
    except Exception:
        pass
    try:
        return locale.getpreferredencoding(False) or "utf-8"
    except Exception:
        return "utf-8"


def _terminate(proc):
    """结束 robocopy 进程，先温和 terminate，超时再 kill。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def run_robocopy(args, log, proc_holder=None, stop_event=None):
    """执行 robocopy，并把输出逐行写进日志。

    返回 (returncode, stopped)。returncode 沿用 Robocopy 的位掩码：
    0~7 都算正常（含「没有文件需要复制」「复制了文件」「检测到多余文件」等），
    8 及以上表示有文件复制失败或发生严重错误。
    """
    encoding = _oem_encoding()
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding=encoding,
        errors="replace",
        bufsize=1,
        creationflags=_CREATE_NO_WINDOW,
    )
    if proc_holder is not None:
        proc_holder["proc"] = proc

    buffer = []
    last_flush = time.monotonic()
    stopped = False

    def flush():
        nonlocal last_flush
        if buffer:
            log("\n".join(buffer))
            buffer.clear()
        last_flush = time.monotonic()

    try:
        # 点「停止」时可能进程还没建好，这里再兜一次
        if stop_event is not None and stop_event.is_set():
            _terminate(proc)
            return proc.wait(), True
        for raw in proc.stdout:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            line = raw.rstrip("\r\n")
            stripped = line.strip()
            if not stripped or set(stripped) <= _SEP_CHARS:
                continue
            buffer.append(line)
            # 批量刷新：逐行 after(0) 在几万个小文件时会把主线程压垮
            if len(buffer) >= 50 or time.monotonic() - last_flush >= 0.2:
                flush()
        # 主线程可能在两次读输出之间结束了进程（管道直接 EOF），
        # 这时循环体里没机会看到 stop_event，必须在收尾时再判一次。
        if not stopped and stop_event is not None and stop_event.is_set():
            stopped = True
        if stopped:
            _terminate(proc)
        rc = proc.wait()
        flush()
        return rc, stopped
    finally:
        flush()
        if proc_holder is not None and proc_holder.get("proc") is proc:
            proc_holder["proc"] = None
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass


# ============================================================
# 配置 / 路径校验
# ============================================================

def _as_int(value, default, low, high):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _load_prefs():
    sec = config.get_section(CONFIG_SECTION)
    return {
        "source": str(sec.get("source") or ""),
        "dest": str(sec.get("dest") or ""),
        "threads": _as_int(sec.get("threads", 16), 16, 1, 128),
        "restart": _as_bool(sec.get("restart", True), True),
        "retry": _as_int(sec.get("retry", 1), 1, 0, 10),
        "wait": _as_int(sec.get("wait", 2), 2, 0, 60),
        "show_files": _as_bool(sec.get("show_files", False), False),
    }


def _save_prefs(**values):
    try:
        config.update_section(CONFIG_SECTION, **values)
    except Exception:
        pass


def _norm_path(path):
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def _is_within(child, parent):
    """child 是否等于 parent 或位于 parent 里面。"""
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        # 不同盘符 / UNC 与盘符混用时 commonpath 会抛 ValueError
        return False


def _dir_has_any(path):
    """目录里有没有东西（只读第一条，百万文件也不会卡住）。"""
    try:
        with os.scandir(path) as it:
            return next(it, None) is not None
    except OSError:
        return False


# ============================================================
# 界面
# ============================================================

def build_speedcopy_tab(parent):
    prefs = _load_prefs()

    page = ScrollPage(parent)
    page.pack(fill=tk.BOTH, expand=True)
    body = page.body
    body.grid_columnconfigure(0, weight=1)
    body.grid_rowconfigure(3, weight=1)

    make_tip(
        body, 0,
        "调用 Windows 自带的 Robocopy 在两个文件夹之间复制。多线程（/MT）加速；"
        "开启「断点续传」（/Z）后，大文件传到一半中断，重新开始会接着传。"
        "目标里已经完整的相同文件会自动跳过；同名但内容 / 时间不同的文件会以源文件"
        "为准覆盖；目标已存在且非空时会先确认。本工具不启用镜像模式，"
        "不会删除目标文件夹里多出来的任何文件。",
        colspan=1, autowrap=True)

    form = make_form(body)
    form.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))

    var_src = make_path_row(form, 0, "源文件夹：", default=prefs["source"],
                            title="选择要复制的源文件夹")
    var_dst = make_path_row(form, 1, "目标文件夹：", default=prefs["dest"],
                            title="选择复制到哪个文件夹",
                            note="不存在会自动创建")
    var_threads = make_num_row(
        form, 2, "多线程数：", prefs["threads"], 1, 128,
        note="/MT：机械硬盘建议 8~16，SSD 可 32~64，网络共享建议 8~16")
    var_restart = make_check(
        form, "断点续传（/Z）：大文件中断后重新开始会接着传（速度略降）",
        default=prefs["restart"], row=3, column=1, columnspan=3)
    var_retry = make_num_row(
        form, 4, "失败重试次数：", prefs["retry"], 0, 10,
        note="/R：0 = 不重试，避免被占用的文件卡住整晚")
    var_wait = make_num_row(
        form, 5, "重试等待秒数：", prefs["wait"], 0, 60,
        note="/W：两次重试之间等多久")
    var_show_files = make_check(
        form, "日志里逐个列出复制的文件（关掉更清爽，海量小文件也更快）",
        default=prefs["show_files"], row=6, column=1, columnspan=3)

    # ---------------- 操作按钮 / 进度 ----------------
    btn_row = ui.Frame(body)
    btn_row.grid(row=2, column=0, sticky="ew", padx=8, pady=(10, 2))

    state = {
        "running": False,
        "started": 0.0,
        "timer": None,
        "stop": threading.Event(),
        "holder": {"proc": None},
    }
    outcome = {}

    btn_start = ui.Button(
        btn_row, text="开始复制", height=36,
        bg=COLOR["accent"], fg=COLOR["text_on_accent"],
        activebackground=COLOR["accent_active"],
        disabledforeground=COLOR["text_on_accent"])
    btn_start.pack(side=tk.LEFT, padx=(0, 6))
    btn_stop = ui.Button(
        btn_row, text="停止", height=36, state=tk.DISABLED)
    btn_stop.pack(side=tk.LEFT, padx=(0, 16))

    bar = ui.ProgressBar(btn_row, mode="indeterminate", length=260)
    bar.pack(side=tk.LEFT, padx=(0, 10))
    lbl_status = ui.RowLabel(
        btn_row, text="未开始", font=FONTS["base"],
        text_color=COLOR["text_secondary"])
    lbl_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # ---------------- 运行日志（默认收起） ----------------
    log_wrap = ui.Frame(body)
    log_wrap.grid(row=3, column=0, sticky="nsew", padx=8, pady=(4, 6))
    log_wrap.grid_columnconfigure(0, weight=1)
    log_wrap.grid_rowconfigure(1, weight=1)

    log_head = ui.Frame(log_wrap)
    log_head.grid(row=0, column=0, sticky="ew")
    var_log_open = tk.BooleanVar(value=False)
    btn_log_toggle = ui.Button(log_head, text="▸ 运行日志", width=11)
    btn_log_toggle.pack(side=tk.LEFT)
    ui.Button(log_head, text="清空", width=6,
              command=lambda: clear_log(text_log)).pack(side=tk.LEFT, padx=6)

    log_body = ui.Frame(log_wrap)
    log_body.grid(row=1, column=0, sticky="nsew", pady=(2, 0))
    log_body.grid_rowconfigure(0, weight=1)
    log_body.grid_columnconfigure(0, weight=1)
    text_log = ui.Textbox(log_body, height=10, state="disabled", wrap=tk.WORD,
                          font=FONTS["log"])
    text_log.grid(row=0, column=0, sticky="nsew")
    log = make_logger(text_log)

    def toggle_log():
        if var_log_open.get():
            log_body.grid_remove()
            btn_log_toggle.config(text="▸ 运行日志")
            var_log_open.set(False)
        else:
            log_body.grid()
            btn_log_toggle.config(text="▾ 运行日志")
            var_log_open.set(True)
        page.refresh()

    btn_log_toggle.config(command=toggle_log)
    log_body.grid_remove()

    # ---------------- 计时器 ----------------
    def cancel_timer():
        if state["timer"] is not None:
            try:
                page.after_cancel(state["timer"])
            except Exception:
                pass
            state["timer"] = None

    def tick():
        state["timer"] = None
        if not state["running"]:
            return
        try:
            elapsed = max(0, int(time.monotonic() - state["started"]))
            lbl_status.configure(
                text="正在复制 · 已运行 %02d:%02d" % (elapsed // 60, elapsed % 60))
            state["timer"] = page.after(1000, tick)
        except Exception:
            state["timer"] = None

    # ---------------- 停止 ----------------
    def stop():
        if not state["running"]:
            return
        state["stop"].set()
        proc = state["holder"].get("proc")
        if proc is not None and proc.poll() is None:
            _terminate(proc)
        try:
            btn_stop.config(state=tk.DISABLED)
            lbl_status.configure(text="正在停止…")
        except Exception:
            pass
        log("已请求停止。已复制完的文件会保留，再次点击「开始复制」即可继续。")

    btn_stop.config(command=stop)

    # ---------------- 开始 ----------------
    def start():
        if state["running"]:
            return
        src = clean_path(var_src.get())
        dst = clean_path(var_dst.get())
        if not src or not os.path.isdir(src):
            messagebox.showerror("错误", "请填写有效的源文件夹！")
            return
        if not dst:
            messagebox.showerror("错误", "请填写目标文件夹！")
            return
        if os.path.exists(dst) and not os.path.isdir(dst):
            messagebox.showerror("错误", "目标路径已存在，但它不是文件夹！")
            return
        if _is_within(_norm_path(dst), _norm_path(src)):
            messagebox.showerror(
                "错误",
                "目标文件夹不能是源文件夹本身，也不能放在源文件夹里面。\n\n"
                "否则 Robocopy 会把刚复制出来的文件再次当作源文件，越复制越多。")
            return

        exe = find_robocopy()
        if not exe:
            messagebox.showerror(
                "错误",
                "未找到 Windows 自带的 robocopy.exe。\n\n"
                "Robocopy 是 Windows 系统组件，正常情况下位于 "
                r"C:\Windows\System32\robocopy.exe。")
            return

        try:
            threads = max(1, min(128, int(var_threads.get())))
            retry = max(0, min(10, int(var_retry.get())))
            wait = max(0, min(60, int(var_wait.get())))
        except (ValueError, TypeError):
            messagebox.showerror("错误", "线程数 / 重试次数 / 等待秒数必须是数字！")
            return

        if os.path.isdir(dst) and _dir_has_any(dst):
            if not messagebox.askyesno(
                    "确认复制",
                    "目标文件夹已存在且非空。\n\n"
                    "本次会合并复制：源中与目标同名但内容 / 时间不同的文件会以源文件为准"
                    "覆盖；目标里多出来的文件不会被删除。\n\n"
                    "继续吗？"):
                return

        restart = bool(var_restart.get())
        show_files = bool(var_show_files.get())
        _save_prefs(source=src, dest=dst, threads=threads, restart=restart,
                    retry=retry, wait=wait, show_files=show_files)

        state["stop"] = threading.Event()
        state["holder"] = {"proc": None}
        state["running"] = True
        outcome.clear()
        cancel_timer()
        state["started"] = time.monotonic()
        btn_stop.config(state=tk.NORMAL)
        btn_start.config(state=tk.DISABLED)
        lbl_status.configure(text="正在启动 Robocopy…")
        bar.start()
        tick()

        clear_log(text_log)
        log("源文件夹：%s" % src)
        log("目标文件夹：%s" % dst)
        log("多线程：%d　断点续传：%s　重试：%d 次 × 等待 %d 秒"
            % (threads, "开" if restart else "关", retry, wait))
        if os.path.isdir(dst):
            log("提示：目标文件夹已存在，本次会合并复制；"
                "同名但内容 / 时间不同的文件会以源文件为准覆盖，"
                "目标里多出来的文件不会被删除。")

        args = build_robocopy_args(
            src, dst, threads=threads, restart=restart, retry=retry,
            wait=wait, show_files=show_files, exe=exe)
        log("命令：%s" % subprocess.list2cmdline(args))
        log("— 开始复制，点击「停止」可随时中断 —")

        def work():
            try:
                rc, stopped = run_robocopy(
                    args, log, proc_holder=state["holder"],
                    stop_event=state["stop"])
                outcome["rc"] = rc
                outcome["stopped"] = stopped
            except Exception as e:
                outcome["error"] = e
                log("Robocopy 启动失败：%s" % e)
                raise

        def done(err):
            state["running"] = False
            cancel_timer()
            try:
                bar.stop()
                bar.configure(value=0)
                btn_stop.config(state=tk.DISABLED)
            except Exception:
                pass

            if err:
                lbl_status.configure(text="执行失败，详情见日志")
                messagebox.showerror("执行失败", "Robocopy 未能正常执行，详情见「运行日志」。")
                return
            if outcome.get("stopped"):
                lbl_status.configure(text="已停止（再次开始可继续）")
                log("已停止。再次点击「开始复制」会跳过已完成的文件继续。")
                return

            rc = int(outcome.get("rc") or 0)
            if rc >= 8:
                lbl_status.configure(text="复制失败（返回码 %d）" % rc)
                messagebox.showerror(
                    "复制失败",
                    "Robocopy 返回码 %d，说明有文件复制失败或发生严重错误。\n\n"
                    "展开「运行日志」查看具体原因；常见原因是文件被占用、"
                    "权限不足或磁盘空间不够。\n\n"
                    "可直接再次点击「开始复制」，已完成的文件会自动跳过。" % rc)
            elif rc == 0:
                lbl_status.configure(text="完成（没有需要复制的文件）")
                messagebox.showinfo(
                    "完成",
                    "没有需要复制的文件：目标里已经是最新的，或源文件夹为空。")
            else:
                lbl_status.configure(text="复制完成（返回码 %d）" % rc)
                messagebox.showinfo(
                    "完成",
                    "复制完成，详情见「运行日志」。\n\n"
                    "返回码 %d：表示复制成功（可能包含「检测到目标中有多余文件」，"
                    "本次没有删除它们）。" % rc)

        # 这里不用 run_task：它会在结束时恢复「开始」按钮，
        # 但本页还要同时管理「停止」按钮、计时器和进度条。
        def worker():
            error = None
            try:
                work()
            except Exception:
                import traceback
                error = traceback.format_exc()
            try:
                btn_start.winfo_toplevel().after(0, lambda: _finish(error))
            except Exception:
                pass

        def _finish(error):
            try:
                btn_start.config(state=tk.NORMAL)
            except Exception:
                pass
            if error:
                log("运行出错：\n" + error)
            done(error)

        threading.Thread(target=worker, daemon=True).start()

    btn_start.config(command=start)

    # 关闭程序 / 页面销毁时，别把 robocopy 留在后台继续复制
    def on_destroy(event):
        if event.widget is not page:
            return
        state["running"] = False
        cancel_timer()
        state["stop"].set()
        _terminate(state["holder"].get("proc"))

    page.bind("<Destroy>", on_destroy, add="+")
    return page
