# -*- coding: utf-8 -*-
"""⑲ 硬盘哈希扫描：冷数据建档 + 增量校验 + 检索
================================================================
按《冷数据哈希扫描工具（GUI版）开发文档》实现，并在其后追加了四项能力：

  一、四种模式（是否改库 × 是否算哈希 两两组合，界面按此顺序排）
      快速预检   —— 只读，不算哈希：只比对大小 + 修改时间，不读文件、不写库，只出差异清单
      快速建档   —— 改库，不算哈希：只记路径 / 大小 / 修改时间，几秒到几十秒建好全盘索引，
                    记录里 sha256 为空（界面标「未计算哈希」），之后跑完整建档会补上
      完整检查   —— 只读，算哈希：算出新哈希做核对，但一个字节都不写库
      完整建档   —— 改库，算哈希：读文件算 SHA256，并标记缺失
  一之二、差异写回
      「快速预检」「完整检查」扫完的差异清单可多选（Ctrl/Shift 或「全选」），
      右键或用表格上方的「写入数据库」只把选中的行写回库；底部可按 全部 / 新增 / 更新 / 删除 筛选。
  二、扫描目录管理
      程序目录的 toolbox_config.json（hashscan 分区）记录扫描过的根目录（时间 / 模式 / 记录数），
      界面可增删管理；搜索范围可选「当前目录」或「全部已管理目录」（跨库检索）。
  二之二、重复文件检查
      「管理...」里 Ctrl/Shift 多选若干已管理目录 → 「检查重复」：
      先按磁盘当前大小粗筛，只对同尺寸的候选比对 SHA256（库里的哈希在大小 + 修改时间
      都对得上时直接复用），结果按组展开，可导出 CSV。跨目录、单目录内都能查。
  三、合并数据库（独立按钮，扫描不再自动并入）
      「合并数据库」把当前目录下各级子文件夹的 filehash.db 合并进当前库：
      rel_path 加上子目录前缀，重复记录**保留较新的那条**（先比 last_hash_time，
      再比 mtime），合并成功后删除子库文件。只操作数据库，不读任何普通文件。
  三之二、用上级库生成当前目录的库（反方向）
      「用上级库」从当前目录**逐级往上**找最近的 filehash.db（上级不一定是一级，
      一直找到盘符根），把属于本目录范围的记录搬过来、rel_path 去掉前缀，
      生成 / 合并出当前目录自己的库。同样只搬数据库记录，不 stat、不读文件；
      往上到根都没有库时给出提示。
  三之三、右键校验哈希（只读）
      选中记录（Ctrl / Shift 多选，可跨库）重算 SHA256 与库里的值比对，
      **完全不写库**（连接是只读打开的）。结果分「一致 / 已变化 / 文件不存在 /
      无库内哈希 / 库里没有 / 读取失败」，单独开窗列出，可只读有问题的、导出 CSV。
  四、哈希状态标注
      表格多一列「哈希」：已计算 / 未计算 / 旧哈希（只读模式下库里的旧值）/ —，
      一眼看出哪些还没算哈希、哪些哈希已经不可信。

原始要点仍然成立：
  1. 一库一根：数据库固定名 `filehash.db`，直接放在扫描根目录下（不建 _index 目录）；
     filehash.db 及其 -wal / -shm / -journal、scan_log.txt、filehash.db.merged 等
     自己产生的文件永远不录入、也不写进日志。
  2. 增量三分支：无记录→建档；有且 active→size+mtime 一致就不读文件；deleted→文件还在则恢复。
  3. 缺失标记只作用于本次扫描范围内的 rel_path 子集；中途停止则不做缺失标记。
  4. 并发：同一个库同时只允许一个扫描进程（Windows 命名互斥体，不落锁文件）。
  5. 长路径：内部统一加 `\\\\?\\` 前缀访问（sqlite3 不认这个前缀，需要时再剥掉）。
  6. 权限异常的文件 / 目录：跳过、写日志、不中断。

rel_path 统一 `/` 分隔、相对扫描根；列声明 COLLATE NOCASE（对应 NTFS 不区分大小写），
UNIQUE(rel_path) 自带唯一索引，所以文档里的 idx_rel_path 不再重复创建。
"""
import csv
import hashlib
import os
import sqlite3
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from . import ui, ui as ttk
from .theme import FONTS

from . import config
from .common import (clean_path, clear_log, form_label, form_note, make_form,
                     make_logger, make_tip)
from .theme import COLOR

DB_NAME = "filehash.db"
LOG_NAME = "scan_log.txt"
CHUNK = 1024 * 1024          # 哈希读取块大小 1MB
SEARCH_LIMIT = 5000          # 搜索结果最多显示条数
PER_DB_LIMIT = 2000          # 跨库搜索时单个库最多取多少条
COMMIT_EVERY = 400           # 每处理多少个文件提交一次事务

# 四种模式 = 是否改库 × 是否算哈希
MODE_QUICK = "quick"        # 快速建档：改库，不算哈希
MODE_PRECHECK = "precheck"  # 快速预检：只读，不算哈希
MODE_FULL = "full"          # 完整建档：改库，算哈希
MODE_VERIFY = "verify"      # 完整检查：只读，算哈希
MODE_ORDER = (MODE_PRECHECK, MODE_QUICK, MODE_VERIFY, MODE_FULL)
MODE_NAMES = {MODE_QUICK: "快速建档", MODE_PRECHECK: "快速预检",
              MODE_FULL: "完整建档", MODE_VERIFY: "完整检查"}
# 「已管理目录」里记录的模式名（多一个「继承上级」，不是扫描模式）
ROOT_MODE_NAMES = dict(MODE_NAMES, seed="继承上级")
MODE_LABELS = {
    MODE_QUICK: "快速建档（改库 · 不算哈希）",
    MODE_PRECHECK: "快速预检（只读 · 不算哈希）",
    MODE_FULL: "完整建档（改库 · 算哈希）",
    MODE_VERIFY: "完整检查（只读 · 算哈希）",
}
# 哪些模式会写库 / 哪些会算哈希
WRITABLE_MODES = (MODE_QUICK, MODE_FULL)
HASHING_MODES = (MODE_FULL, MODE_VERIFY)

# 差异清单的分类（底部筛选按钮）
CAT_NEW, CAT_UPD, CAT_DEL = "新增", "更新", "删除"
CAT_HASHED, CAT_NOHASH = "已计算", "未计算"
CATS = ("全部", CAT_NEW, CAT_UPD, CAT_DEL, CAT_HASHED, CAT_NOHASH)

# 列表分页：默认每页 500 条，可改（0 = 不分页，一次全显示）
DEFAULT_PAGE_SIZE = 500
PAGE_SIZE_CHOICES = ("100", "200", "500", "1000", "2000", "5000", "全部")
DIFF_CAT = {"新增": CAT_NEW, "恢复": CAT_NEW,
            "变更": CAT_UPD, "补哈希": CAT_UPD,
            "缺失": CAT_DEL}

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path TEXT NOT NULL COLLATE NOCASE,
    sha256 TEXT,
    file_size INTEGER,
    mtime REAL,
    last_hash_time REAL,
    status TEXT DEFAULT 'active',
    note TEXT,
    UNIQUE(rel_path)
);
CREATE INDEX IF NOT EXISTS idx_sha256 ON file_records(sha256);
CREATE INDEX IF NOT EXISTS idx_status ON file_records(status);
"""

# 子库并入主库：已有记录优先，缺的字段用子库的补上
UPSERT_SQL = """
INSERT INTO file_records
    (rel_path, sha256, file_size, mtime, last_hash_time, status, note)
VALUES (?,?,?,?,?,?,?)
ON CONFLICT(rel_path) DO UPDATE SET
    sha256 = COALESCE(file_records.sha256, excluded.sha256),
    file_size = COALESCE(file_records.file_size, excluded.file_size),
    mtime = COALESCE(file_records.mtime, excluded.mtime),
    last_hash_time = COALESCE(file_records.last_hash_time, excluded.last_hash_time),
    note = COALESCE(file_records.note, excluded.note)
"""

# 自己产生的文件，永不录入（数据库本体 + WAL/SHM/回滚日志 + 扫描日志）
SELF_FILES = {
    DB_NAME,
    DB_NAME + "-wal",
    DB_NAME + "-shm",
    DB_NAME + "-journal",
    LOG_NAME.lower(),
}


# ============================================================
# 路径 / 格式化工具
# ============================================================

def long_path(path):
    r"""给绝对路径加 \\?\ 前缀，突破 Windows 260 字符限制；非 Windows 原样返回"""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def strip_long(path):
    r"""去掉 \\?\ 前缀（sqlite3 / subprocess 这类不吃前缀的接口要用）"""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def is_skipped_name(name):
    """自己产生的文件不参与扫描、也不记日志：
    filehash.db 及其 -wal / -shm / -journal（连变体一起按前缀排除）、
    scan_log.txt，以及 .merged（已并入）、.broken-*（坏库备份）这些派生文件。"""
    low = name.lower()
    return (low in SELF_FILES
            or low.startswith(DB_NAME + "-")
            or low.startswith(DB_NAME + "."))


def fmt_size(n):
    if n is None:
        return "—"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.0f} {u}" if u == "B" else f"{v:.2f} {u}"
        v /= 1024
    return f"{n} B"


def short_sha(sha, keep=16):
    """表格里只显示哈希前 keep 位（完整值仍在数据里，复制/导出不受影响）"""
    if not sha:
        return ""
    s = str(sha)
    return s if len(s) <= keep else s[:keep] + "…"


def fmt_delta(d):
    """带符号的体积差：+8 B / -2.35 MB"""
    sign = "+" if d >= 0 else "-"
    return sign + fmt_size(abs(d))


def fmt_time(ts):
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (ValueError, OverflowError, OSError):
        return "—"


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def short_path(p, keep=34):
    """界面上显示路径：太长只保留「最后两级」（尾部才是区分度最高的部分）"""
    if not p:
        return ""
    p = str(p)
    if len(p) <= keep:
        return p
    parts = [x for x in p.replace("/", "\\").split("\\") if x]
    if len(parts) >= 2:
        s = "…\\" + "\\".join(parts[-2:])
        if len(s) <= keep:
            return s
    return "…" + p[-(keep - 1):]


def reveal_in_explorer(path):
    """在资源管理器中定位文件"""
    try:
        if os.path.isfile(path):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"',
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif os.path.isdir(path):
            os.startfile(path)
    except Exception:
        try:
            os.startfile(os.path.dirname(path))
        except Exception:
            pass


# ============================================================
# 扫描目录配置（存在工具箱统一配置 toolbox_config.json 的 hashscan 分区）
# ============================================================

def roots_config_path():
    """配置文件路径（统一为 toolbox_config.json，界面上提示用）"""
    return config.config_path()


def load_roots():
    """读取已管理的扫描目录列表；损坏/缺失返回 []"""
    try:
        data = config.get_section("hashscan")
    except Exception:
        return []
    items = data.get("roots") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        path = it.get("path")
        if not isinstance(path, str) or not path:
            continue
        out.append({
            "path": path,
            "db": it.get("db") or os.path.join(path, DB_NAME),
            "last_scan": it.get("last_scan") or 0,
            "mode": it.get("mode") or "",
            "files": it.get("files") or 0,
            "active": it.get("active") or 0,
            "deleted": it.get("deleted") or 0,
            "nohash": it.get("nohash") or 0,
        })
    return out


def save_roots(roots):
    try:
        return config.set_section("hashscan", {"roots": roots})
    except Exception:
        return False


def upsert_root(roots, path, **info):
    """把某个根目录记进配置（最近用过的排最前），返回新列表"""
    key = os.path.normcase(os.path.abspath(path))
    item = next((r for r in roots
                 if os.path.normcase(os.path.abspath(r["path"])) == key), None)
    if item is None:
        item = {"path": os.path.abspath(path), "db": os.path.join(path, DB_NAME),
                "last_scan": 0, "mode": "", "files": 0, "active": 0,
                "deleted": 0, "nohash": 0}
        roots.insert(0, item)
    else:
        roots.remove(item)
        roots.insert(0, item)
    item.update(info)
    item["path"] = os.path.abspath(path)
    item.setdefault("db", os.path.join(item["path"], DB_NAME))
    return roots


def drop_roots_by_db(roots, db_paths):
    """并入后把「子库已经不存在」的条目从配置里去掉"""
    keys = {os.path.normcase(os.path.abspath(p)) for p in db_paths}
    return [r for r in roots if os.path.normcase(os.path.abspath(r["db"])) not in keys]


# ============================================================
# 单进程锁（Windows 命名互斥体，不产生任何锁文件）
# ============================================================

class DbLock:
    """同一个 filehash.db 同一时间只允许一个扫描进程；
    用命名互斥体实现，进程退出/崩溃自动释放，不会留下锁文件。"""

    def __init__(self, db_path):
        key = os.path.abspath(db_path).lower().replace("\\", "/")
        self.name = "Local\\filehash_" + hashlib.md5(key.encode("utf-8")).hexdigest()
        self.handle = None

    def acquire(self):
        if os.name != "nt":
            return True, ""
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                         wintypes.LPCWSTR]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.CreateMutexW(None, False, self.name)
            if not handle:
                return False, "创建互斥体失败（错误码 %d）" % ctypes.get_last_error()
            if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
                k32.CloseHandle(handle)
                return False, "该数据库正被另一个扫描进程占用，请等它结束后再试"
            self.handle = handle
            return True, ""
        except Exception as e:
            return True, "" if os.name != "nt" else f"（锁检测不可用：{e}）"

    def release(self):
        if self.handle and os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CloseHandle.argtypes = [wintypes.HANDLE]
                k32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


# ============================================================
# SQLite 访问层
# ============================================================

class HashDB:
    def __init__(self, path):
        self.path = path
        self.conn = None
        self.readonly = False

    def open(self, readonly=False):
        if readonly:
            uri = "file:" + self.path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, timeout=15,
                                        isolation_level=None)
            self.readonly = True
        else:
            self.conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            self.conn.executescript(SCHEMA)
        return self.conn

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    # ---- 查询 ----
    def get(self, rel_path):
        cur = self.conn.execute(
            "SELECT id, sha256, file_size, mtime, status, last_hash_time "
            "FROM file_records WHERE rel_path = ?", (rel_path,))
        return cur.fetchone()

    def stats(self):
        """返回 (总数, 有效, 缺失, 有效记录总字节, 有效但未算哈希)"""
        try:
            row = self.conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='deleted' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='active' THEN file_size ELSE 0 END), "
                "SUM(CASE WHEN status='active' AND (sha256 IS NULL OR sha256='') "
                "    THEN 1 ELSE 0 END) "
                "FROM file_records").fetchone()
            return (row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0, row[4] or 0)
        except sqlite3.Error:
            return (0, 0, 0, 0, 0)

    def _search_sql(self, cond, order="rel_path"):
        return ("SELECT rel_path, sha256, file_size, mtime, last_hash_time, status "
                "FROM file_records WHERE " + cond + " ORDER BY " + order + " LIMIT ?")

    def search_path(self, kw, limit=SEARCH_LIMIT, include_deleted=False):
        cond = ("" if include_deleted else "status='active' AND ") + \
               "rel_path LIKE ? ESCAPE '\\'"
        return self.conn.execute(self._search_sql(cond),
                                 ("%" + _like_escape(kw) + "%", limit)).fetchall()

    def count_path(self, kw, include_deleted=False):
        cond = ("" if include_deleted else "status='active' AND ") + \
               "rel_path LIKE ? ESCAPE '\\'"
        return self.conn.execute("SELECT COUNT(*) FROM file_records WHERE " + cond,
                                 ("%" + _like_escape(kw) + "%",)).fetchone()[0]

    def search_hash(self, kw, limit=SEARCH_LIMIT):
        return self.conn.execute(self._search_sql("sha256 LIKE ? ESCAPE '\\'"),
                                 (_like_escape(kw.strip().lower()) + "%", limit)).fetchall()

    def count_hash(self, kw):
        return self.conn.execute(
            "SELECT COUNT(*) FROM file_records WHERE sha256 LIKE ? ESCAPE '\\'",
            (_like_escape(kw.strip().lower()) + "%",)).fetchone()[0]

    def list_all(self, limit=SEARCH_LIMIT, include_deleted=False):
        cond = "1=1" if include_deleted else "status='active'"
        return self.conn.execute(self._search_sql(cond), (limit,)).fetchall()

    def count_all(self, include_deleted=False):
        cond = "1=1" if include_deleted else "status='active'"
        return self.conn.execute(
            "SELECT COUNT(*) FROM file_records WHERE " + cond).fetchone()[0]

    def list_deleted(self, prefix="", limit=SEARCH_LIMIT):
        """列出标记缺失的记录（只查指定范围）"""
        if prefix:
            return self.conn.execute(
                self._search_sql("status='deleted' AND "
                                 "(rel_path = ? OR rel_path LIKE ? ESCAPE '\\')"),
                (prefix, _like_escape(prefix) + "/%", limit)).fetchall()
        return self.conn.execute(self._search_sql("status='deleted'"),
                                 (limit,)).fetchall()

    def count_nohash(self, prefix=""):
        """范围内「有效但还没算哈希」的记录数"""
        cond = "status='active' AND (sha256 IS NULL OR sha256='')"
        if prefix:
            cond += " AND (rel_path = ? OR rel_path LIKE ? ESCAPE '\\')"
            return self.conn.execute(
                "SELECT COUNT(*) FROM file_records WHERE " + cond,
                (prefix, _like_escape(prefix) + "/%")).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM file_records WHERE " + cond).fetchone()[0]


def check_db_file(db_path):
    """扫描前快速检查库文件：返回 (ok, message)。不存在视为正常（将新建）。"""
    if not os.path.exists(db_path):
        return True, ""
    try:
        conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
        try:
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
        finally:
            conn.close()
        if not row or str(row[0]).lower() != "ok":
            return False, str(row[0]) if row else "未知错误"
        return True, ""
    except sqlite3.DatabaseError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def db_counts(db_path):
    """只读打开库里取统计；失败返回 None"""
    if not os.path.isfile(db_path):
        return None
    db = HashDB(db_path)
    try:
        try:
            db.open(readonly=True)
        except Exception:
            db.open()
        return db.stats()
    except Exception:
        return None
    finally:
        db.close()


# ============================================================
# 差异写回（只读模式扫完，勾选后落库）
# ============================================================

def _upsert_record(conn, stored, sha, size, mtime, last):
    """按 rel_path 写一条有效记录（有则更新，无则插入）。
    老版本 SQLite 不支持 UPSERT 时退化成 UPDATE + INSERT。"""
    try:
        conn.execute(
            "INSERT INTO file_records"
            " (rel_path, sha256, file_size, mtime, last_hash_time, status)"
            " VALUES(?,?,?,?,?,'active')"
            " ON CONFLICT(rel_path) DO UPDATE SET"
            " sha256=excluded.sha256, file_size=excluded.file_size,"
            " mtime=excluded.mtime, last_hash_time=excluded.last_hash_time,"
            " status='active'", (stored, sha, size, mtime, last))
    except sqlite3.OperationalError:
        cur = conn.execute(
            "UPDATE file_records SET sha256=?, file_size=?, mtime=?,"
            " last_hash_time=?, status='active' WHERE rel_path=?",
            (sha, size, mtime, last, stored))
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO file_records"
                " (rel_path, sha256, file_size, mtime, last_hash_time, status)"
                " VALUES(?,?,?,?,?,'active')",
                (stored, sha, size, mtime, last))


def write_pending(conn, p):
    """把一条差异记录写回数据库。
    新增 / 变更 / 恢复 → 整理成 active 记录（没有新哈希就把 sha256 置空）；
    补哈希 → 只更新哈希；缺失 → 只把 status 标成 deleted。"""
    stored = p["stored"]
    if p["cat"] == CAT_DEL:
        conn.execute("UPDATE file_records SET status='deleted' WHERE rel_path=?",
                     (stored,))
        return
    sha = p.get("digest") or None
    last = time.time() if sha else None
    if p["kind"] == "补哈希":
        conn.execute("UPDATE file_records SET sha256=?, last_hash_time=?"
                     " WHERE rel_path=?", (sha, last, stored))
        return
    _upsert_record(conn, stored, sha, p["size"], p["mtime"], last)


# ============================================================
# 遍历 / 哈希
# ============================================================

def iter_files(root, log=None, stop_event=None, on_tick=None):
    """递归产出 (rel_path, size, mtime)。跳过软链接/联接点、目录、自产文件
    （含各级子目录里的 filehash.db）。权限异常逐项记录后跳过，不中断。"""
    root_scan = long_path(root)
    stack = [root_scan]
    found = 0
    while stack:
        if stop_event is not None and stop_event.is_set():
            return
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError as e:
            if log:
                log(f"[跳过目录] {strip_long(cur)}：{e}")
            continue
        for entry in entries:
            try:
                if entry.is_symlink():          # 软链接 / 目录联接：跳过，防环路
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                rel = os.path.relpath(entry.path, root_scan).replace(os.sep, "/")
                if is_skipped_name(entry.name):
                    continue
                st = entry.stat(follow_symlinks=False)
                found += 1
                if on_tick and found % 200 == 0:
                    on_tick(found)
                yield rel, st.st_size, st.st_mtime
            except OSError as e:
                if log:
                    log(f"[跳过文件] {strip_long(entry.path)}：{e}")


def sha256_file(path, stop_event=None, on_chunk=None):
    """流式计算 SHA256；on_chunk(本次字节数) 用于当前文件进度；
    中途收到停止信号返回 None。"""
    h = hashlib.sha256()
    with open(long_path(path), "rb", buffering=0) as f:
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            if on_chunk:
                on_chunk(len(chunk))
    return h.hexdigest()


# ============================================================
# 扫描核心
# ============================================================

def resolve_db_root(root, use_ancestor):
    """决定用哪个库：默认用根目录自己的 filehash.db；
    use_ancestor 时若本级没有、上层有，则沿用上层库并返回前缀。
    返回 (db_root, prefix)。"""
    root = os.path.abspath(root)
    if os.path.isfile(os.path.join(root, DB_NAME)):
        return root, ""
    if not use_ancestor:
        return root, ""
    cur = root
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
        if os.path.isfile(os.path.join(cur, DB_NAME)):
            prefix = os.path.relpath(root, cur).replace(os.sep, "/")
            if prefix in ("", "."):
                return root, ""
            return cur, prefix
    return root, ""


class Scanner:
    """一次扫描任务。所有 UI 更新交给主线程轮询 ui 字典完成，避免跨线程操作 Tk。"""

    def __init__(self, root, db_path, prefix, mode, log, logfile=None,
                 stop_event=None):
        self.root = os.path.abspath(root)
        self.db_path = db_path
        self.prefix = prefix
        self.mode = mode                 # full / quick / precheck
        self.log = log
        self.logfile = logfile
        self.stop = stop_event or threading.Event()
        self.ui = {"phase": "准备", "done": 0, "total": 0,
                   "cur_name": "", "cur_done": 0, "cur_total": 0,
                   "done_flag": False, "aborted": False, "error": None,
                   "rows": [],        # 只读模式的差异清单（表格用 7 元组）
                   "pending": []}      # 与 rows 平行的结构化记录（写回库用）
        self.stats = dict(new=0, changed=0, same=0, restored=0, missing=0,
                          errors=0, hashed=0, filled=0, bytes=0, nohash=0)

    @property
    def writable(self):
        """会写库的模式：快速建档 / 完整建档"""
        return self.mode in WRITABLE_MODES

    @property
    def hashing(self):
        """会读文件算哈希的模式：完整建档 / 完整检查"""
        return self.mode in HASHING_MODES

    # ---- 日志 ----
    def _log(self, msg):
        if self.log:
            self.log(msg)
        if self.logfile:
            try:
                with open(self.logfile, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
            except Exception:
                pass

    def _stored_rel(self, rel):
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def run(self):
        """返回 stats 字典（结束后 ui["done_flag"]=True）"""
        started = time.time()
        self._log("=" * 60)
        self._log(f"扫描开始：{self.root}")
        self._log(f"模式：{MODE_NAMES.get(self.mode, self.mode)}"
                  f"（{'写库' if self.writable else '只读，不写库'} · "
                  f"{'算哈希' if self.hashing else '不算哈希'}）；"
                  f"数据库：{self.db_path}"
                  + (f"；相对路径前缀：{self.prefix}/" if self.prefix else ""))
        db = None
        try:
            # ---------- 阶段 1：枚举 ----------
            self.ui["phase"] = "枚举文件"
            files = []
            t0 = time.time()

            def tick(n):
                self.ui["total"] = n
                self.ui["phase"] = f"枚举文件（已发现 {n}）"

            for item in iter_files(self.root, self.log, self.stop, tick):
                files.append(item)
            files.sort(key=lambda x: x[0].lower())
            self.ui["total"] = len(files)
            if self.stop.is_set():
                self.ui["aborted"] = True
                self._log(f"已手动停止（已枚举 {len(files)} 个文件，未做缺失标记）")
                return self.stats
            self._log(f"枚举完成：{len(files)} 个文件，用时 {time.time() - t0:.1f}s")

            # ---------- 阶段 2：打开库 ----------
            db = HashDB(self.db_path)
            if not self.writable and os.path.isfile(self.db_path):
                try:
                    db.open(readonly=True)          # 预检 / 检查只读：连 WAL 头都不碰
                except Exception as e:
                    self._log(f"只读打开失败，改为读写打开：{e}")
                    db.open()
            else:
                db.open()

            # 本次遍历到的 rel_path 集合（连接级 TEMP 表，不写主库），
            # 收尾用它算「库里有效但本次没找到」= 缺失
            db.conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen("
                            "rel_path TEXT COLLATE NOCASE PRIMARY KEY)")

            # ---------- 阶段 3：比对 / 哈希 ----------
            seen_buf = []
            pending = 0
            if self.writable:
                db.conn.execute("BEGIN")

            for i, (rel, size, mtime) in enumerate(files, 1):
                if self.stop.is_set():
                    self.ui["aborted"] = True
                    break
                stored = self._stored_rel(rel)
                abs_path = os.path.join(self.root, rel.replace("/", os.sep))
                self.ui["cur_name"] = rel
                self.ui["cur_done"] = 0
                self.ui["cur_total"] = size or 0
                self.ui["done"] = i - 1
                try:
                    pending += self._process_one(db, stored, abs_path, rel, size, mtime)
                except OSError as e:
                    self.stats["errors"] += 1
                    self._log(f"[跳过文件] {rel}：{e}")
                except sqlite3.Error as e:
                    self.stats["errors"] += 1
                    self._log(f"[数据库错误] {rel}：{e}")
                # 无论成功/跳过都登记到 seen，避免仅因读失败被误判为「缺失」
                seen_buf.append((stored,))
                if len(seen_buf) >= 2000:
                    db.conn.executemany(
                        "INSERT OR IGNORE INTO seen(rel_path) VALUES(?)", seen_buf)
                    seen_buf.clear()
                if self.writable and pending >= COMMIT_EVERY:
                    db.conn.execute("COMMIT")
                    db.conn.execute("BEGIN")
                    pending = 0

            if seen_buf:
                db.conn.executemany(
                    "INSERT OR IGNORE INTO seen(rel_path) VALUES(?)", seen_buf)
                seen_buf.clear()
            if self.writable:
                db.conn.execute("COMMIT")

            # ---------- 阶段 4：缺失标记 ----------
            if self.ui["aborted"]:
                self._log("扫描被中断：本次不做缺失标记"
                          "（避免把还没遍历到的文件误标为缺失）")
            elif self.writable:
                self.ui["phase"] = "标记缺失记录"
                self.stats["missing"] = self._mark_missing(db)
            else:
                self.ui["phase"] = "查找缺失记录"
                self.stats["missing"] = self._list_missing(db)

            # 统计「范围内还没算哈希」的条数
            if not self.ui["aborted"]:
                try:
                    self.stats["nohash"] = db.count_nohash(self.prefix)
                except sqlite3.Error:
                    pass

            self.ui["done"] = len(files)
            dur = time.time() - started
            s = self.stats
            self._log(
                f"扫描结束，用时 {dur:.1f}s —— 新增 {s['new']}，变更 {s['changed']}，"
                f"恢复 {s['restored']}，未变 {s['same']}，缺失 {s['missing']}，"
                f"补哈希 {s['filled']}，"
                f"跳过(权限/读失败) {s['errors']}")
            if s["nohash"]:
                self._log(f"当前范围内有 {s['nohash']} 条记录尚未计算哈希"
                          f"（跑「完整建档」可补上）")
            if not self.writable:
                self._log(f"只读模式：{len(self.ui['rows'])} 条差异没有写库，"
                          f"可在表格里勾选后点「写入数据库」")
            return s
        except Exception as e:
            import traceback
            self.ui["error"] = traceback.format_exc()
            self._log(f"[致命错误] {e}")
            return self.stats
        finally:
            if db is not None:
                db.close()
            self.ui["done_flag"] = True

    # ---- 单文件处理：A 新增 / B 比对 / C 恢复 ----
    def _process_one(self, db, stored, abs_path, rel, size, mtime):
        """返回本次写的行数（0/1），供批量提交计数。
        只读模式（快速预检 / 完整检查）一律不写库，只往差异清单里加一条。"""
        row = db.get(stored)
        hashing, writable = self.hashing, self.writable

        # ---- 分支 A：库里没有 → 新文件 ----
        if row is None:
            digest = None
            if hashing:
                digest = self._hash(abs_path, size)
                if digest is None:
                    return 0
            self.stats["new"] += 1
            if writable:
                db.conn.execute(
                    "INSERT INTO file_records"
                    "(rel_path, sha256, file_size, mtime, last_hash_time, status)"
                    " VALUES(?,?,?,?,?,'active')",
                    (stored, digest, size, mtime, time.time() if digest else None))
                return 1
            self._add_diff("新增", rel, stored, size, mtime, digest=digest)
            return 0

        rid, old_sha, old_size, old_mtime, status, old_last = row
        same = (old_size == size and old_mtime is not None
                and abs(old_mtime - mtime) < 1e-6)

        # ---- 分支 B-1：有效且 size+mtime 一致 ----
        if status == "active" and same:
            if hashing and not old_sha:
                # 之前只是快速建档（没算哈希）→ 这次补上
                digest = self._hash(abs_path, size)
                if digest is None:
                    return 0
                self.stats["filled"] += 1
                if writable:
                    db.conn.execute(
                        "UPDATE file_records SET sha256=?, last_hash_time=? WHERE id=?",
                        (digest, time.time(), rid))
                    return 1
                self._add_diff("补哈希", rel, stored, size, mtime, digest=digest,
                               old_size=old_size, old_last=old_last)
                return 0
            self.stats["same"] += 1
            return 0

        # ---- 分支 B-2：有效但变了 ----
        if status == "active":
            digest = None
            if hashing:
                digest = self._hash(abs_path, size)
                if digest is None:
                    return 0
            self.stats["changed"] += 1
            if writable:
                if digest:
                    db.conn.execute(
                        "UPDATE file_records SET sha256=?, file_size=?, mtime=?,"
                        " last_hash_time=? WHERE id=?",
                        (digest, size, mtime, time.time(), rid))
                else:
                    # 不算哈希的模式：旧哈希已经不可信，清掉等完整建档补
                    db.conn.execute(
                        "UPDATE file_records SET sha256=NULL, file_size=?, mtime=?,"
                        " last_hash_time=NULL WHERE id=?", (size, mtime, rid))
                return 1
            self._add_diff("变更", rel, stored, size, mtime, digest=digest,
                           old_sha=old_sha, old_size=old_size, old_last=old_last)
            return 0

        # ---- 分支 C：库里标 deleted，磁盘上又出现 → 恢复有效 ----
        digest = None
        if hashing:
            if same and old_sha:
                digest = old_sha          # 大小时间都没变、哈希还在 → 不必重读
            else:
                digest = self._hash(abs_path, size)
                if digest is None:
                    return 0
        elif same and old_sha:
            digest = old_sha              # 没算哈希，但大小/时间一致，旧哈希仍可信
        self.stats["restored"] += 1
        if writable:
            if hashing and digest:
                if same:
                    db.conn.execute(
                        "UPDATE file_records SET status='active' WHERE id=?", (rid,))
                else:
                    db.conn.execute(
                        "UPDATE file_records SET sha256=?, file_size=?, mtime=?,"
                        " last_hash_time=?, status='active' WHERE id=?",
                        (digest, size, mtime, time.time(), rid))
            elif not hashing and same and old_sha:
                db.conn.execute(
                    "UPDATE file_records SET status='active' WHERE id=?", (rid,))
            else:
                db.conn.execute(
                    "UPDATE file_records SET status='active', sha256=NULL,"
                    " file_size=?, mtime=?, last_hash_time=NULL WHERE id=?",
                    (size, mtime, rid))
            return 1
        self._add_diff("恢复", rel, stored, size, mtime, digest=digest,
                       old_sha=old_sha, old_size=old_size, old_last=old_last)
        return 0

    # ---- 内部工具 ----
    def _hash(self, abs_path, size):
        self.stats["hashed"] += 1
        self.ui["phase"] = "计算哈希"

        def on_chunk(n):
            self.stats["bytes"] += n
            self.ui["cur_done"] += n

        try:
            digest = sha256_file(abs_path, self.stop, on_chunk)
        except OSError as e:
            self.stats["errors"] += 1
            self._log(f"[跳过文件] {os.path.basename(abs_path)}：{e}")
            return None
        if digest is None:
            self._log("已停止（当前文件哈希中断，未写入该记录）")
        return digest

    def _add_diff(self, kind, rel, stored, size, mtime, digest=None,
                  old_sha=None, old_size=None, old_last=None, hash_state=None):
        """只读模式：差异清单加一条（表格行 + 写回库用的结构化记录）。
        digest 是本次算出的新哈希（不算哈希的模式为 None）；
        old_sha 是库里的旧哈希，变更行也显示出来，但写回时绝不会用它。"""
        detail = kind
        if kind == "变更" and old_size is not None and size is not None:
            try:
                detail = "变更 " + fmt_delta(size - old_size)
            except (TypeError, ValueError):
                pass
        if hash_state is None:
            hash_state = ("已计算" if digest
                          else ("旧哈希" if old_sha else "未计算"))
        self.ui["rows"].append(
            (rel, digest or old_sha or "", size, mtime, old_last, detail, self.root))
        self.ui["pending"].append({
            "kind": kind,
            "cat": DIFF_CAT.get(kind, CAT_UPD),
            "stored": stored,
            "rel": rel,
            "digest": digest,
            "old_sha": old_sha,
            "size": size,
            "mtime": mtime,
            "hash_state": hash_state,
            "root": self.root,
            "db": self.db_path,
        })

    def _scope_sql(self):
        """缺失标记的范围：本次扫描根在库里对应的 rel_path 子集"""
        if self.prefix:
            return ("(rel_path = ? OR rel_path LIKE ? ESCAPE '\\')",
                    (self.prefix, _like_escape(self.prefix) + "/%"))
        return "1=1", ()

    def _mark_missing(self, db):
        scope_sql, params = self._scope_sql()
        rows = db.conn.execute(
            f"SELECT rel_path FROM file_records WHERE status='active' AND {scope_sql} "
            f"AND rel_path NOT IN (SELECT rel_path FROM seen)", params).fetchall()
        if not rows:
            return 0
        db.conn.execute("BEGIN")
        db.conn.execute(
            f"UPDATE file_records SET status='deleted' WHERE status='active' "
            f"AND {scope_sql} AND rel_path NOT IN (SELECT rel_path FROM seen)", params)
        db.conn.execute("COMMIT")
        for (rp,) in rows[:200]:
            self._log(f"[缺失] {rp}")
        if len(rows) > 200:
            self._log(f"[缺失] …… 另有 {len(rows) - 200} 条，详见数据库")
        return len(rows)

    def _list_missing(self, db):
        """只读模式：列出缺失清单，一个字节也不往主库写"""
        scope_sql, params = self._scope_sql()
        rows = db.conn.execute(
            f"SELECT rel_path, sha256, file_size, mtime FROM file_records "
            f"WHERE status='active' AND {scope_sql} "
            f"AND rel_path NOT IN (SELECT rel_path FROM seen)", params).fetchall()
        tag = MODE_NAMES.get(self.mode, "只读")
        for rp, _sha, _size, _mt in rows[:200]:
            self._log(f"[{tag}·缺失] {rp}")
        if len(rows) > 200:
            self._log(f"[{tag}·缺失] …… 另有 {len(rows) - 200} 条")
        for rp, sha, _size, _mt in rows:
            rel = rp
            if self.prefix and rp.startswith(self.prefix + "/"):
                rel = rp[len(self.prefix) + 1:]
            self._add_diff("缺失", rel, rp, None, None,
                           old_sha=sha, hash_state="—")
        return len(rows)


# ============================================================
# 补全哈希（只给「有效但没算哈希」的记录补 sha256）
# ============================================================

def count_no_hash(db_path):
    """返回 (条数, 总字节) —— 有效但还没算哈希的记录"""
    if not os.path.isfile(db_path):
        return 0, 0
    db = HashDB(db_path)
    try:
        try:
            db.open(readonly=True)
        except Exception:
            db.open()
        row = db.conn.execute(
            "SELECT COUNT(*), SUM(file_size) FROM file_records "
            "WHERE status='active' AND (sha256 IS NULL OR sha256='')").fetchone()
        return int(row[0] or 0), int(row[1] or 0)
    except sqlite3.Error:
        return 0, 0
    finally:
        db.close()


class HashFiller:
    """给库里「有效但没算哈希」的记录补算 SHA256。

    约定（按需求）：
      · **只写 sha256 与 last_hash_time**，file_size / mtime / status 一概不动；
      · 文件已经不在了 → 跳过 + 记日志，**不**顺手标缺失；
      · 每算完一条立即单独提交（连接是 autocommit），中断也不会丢已完成的。
    """

    def __init__(self, root, db_path, log=None, stop_event=None):
        self.root = os.path.abspath(root)
        self.db_path = db_path
        self.log = log or (lambda *_: None)
        self.stop = stop_event or threading.Event()
        self.ui = {"phase": "补全哈希", "done": 0, "total": 0,
                   "cur_name": "", "cur_done": 0, "cur_total": 0,
                   "done_flag": False, "aborted": False, "error": None}
        self.stats = dict(filled=0, skipped=0, errors=0, bytes=0)

    def _tick(self, n):
        self.stats["bytes"] += n
        self.ui["cur_done"] += n

    def run(self):
        self.log("=" * 60)
        self.log(f"补全哈希：{self.db_path}")
        db = HashDB(self.db_path)
        try:
            db.open()
            rows = db.conn.execute(
                "SELECT id, rel_path, file_size FROM file_records "
                "WHERE status='active' AND (sha256 IS NULL OR sha256='') "
                "ORDER BY rel_path").fetchall()
            self.ui["total"] = len(rows)
            if not rows:
                self.log("没有需要补哈希的记录")
                return self.stats
            self.log(f"待补 {len(rows)} 条（只写 sha256，其余字段不动）")
            for i, (rid, rel, rec_size) in enumerate(rows, 1):
                if self.stop.is_set():
                    self.ui["aborted"] = True
                    self.log("已手动停止（已算完的都已入库）")
                    break
                abs_path = os.path.join(self.root, *rel.split("/"))
                self.ui["cur_name"] = rel
                self.ui["cur_done"] = 0
                self.ui["done"] = i - 1
                try:
                    st = os.stat(long_path(abs_path))
                except OSError:
                    self.stats["skipped"] += 1
                    self.log(f"[跳过·文件不在了] {rel}")
                    self.ui["done"] = i
                    continue
                self.ui["cur_total"] = st.st_size
                try:
                    digest = sha256_file(abs_path, self.stop, self._tick)
                except OSError as e:
                    self.stats["errors"] += 1
                    self.log(f"[读失败] {rel}：{e}")
                    self.ui["done"] = i
                    continue
                if digest is None:
                    self.ui["aborted"] = True
                    self.log("已手动停止（当前文件读到一半，未写入）")
                    break
                try:
                    db.conn.execute(
                        "UPDATE file_records SET sha256=?, last_hash_time=? "
                        "WHERE id=?", (digest, time.time(), rid))
                except sqlite3.Error as e:
                    self.stats["errors"] += 1
                    self.log(f"[写库失败] {rel}：{e}")
                    self.ui["done"] = i
                    continue
                self.stats["filled"] += 1
                if rec_size is not None and int(rec_size) != int(st.st_size):
                    self.log(f"[已补] {rel} —— 注意：文件大小与建档时不同，"
                             f"按要求只写了哈希，size/mtime 未改")
                self.ui["done"] = i
            s = self.stats
            self.log(f"补全哈希结束 —— 已补 {s['filled']} 条，"
                     f"跳过(文件不在) {s['skipped']} 条，失败 {s['errors']} 条，"
                     f"共读取 {fmt_size(s['bytes'])}")
            if s["skipped"]:
                self.log(f"提示：这 {s['skipped']} 条的文件已经不在，按要求没有改任何状态；"
                         f"跑一次「完整建档」会把它们标记为缺失，之后就不会再出现在待补列表里")
            return s
        except Exception as e:
            import traceback
            self.ui["error"] = traceback.format_exc()
            self.log(f"[致命错误] {e}")
            return self.stats
        finally:
            db.close()
            self.ui["done_flag"] = True


# ============================================================
# 用上级库生成当前目录的库（只搬数据库记录，完全不查文件）
# ============================================================

def find_parent_db(path):
    """从 path 的**上一级**开始逐级往上找 filehash.db，直到盘符根。
    上级不一定是一级，会一直往上看。返回 (上级根目录, 库文件路径)，找不到返回 None。"""
    cur = os.path.abspath(path)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:              # 已经是根（D:\ 这种没有上一级）
            return None
        cur = parent
        dbp = os.path.join(cur, DB_NAME)
        if os.path.isfile(dbp):
            return cur, dbp


def scope_prefix(target, parent_root):
    """target 相对 parent_root 的路径前缀（/ 分隔、不带首尾斜杠）；同级返回 """""
    rel = os.path.relpath(os.path.abspath(target),
                          os.path.abspath(parent_root)).replace(os.sep, "/")
    return "" if rel in (".", "") else rel


def count_scope(db_path, prefix):
    """只读统计某前缀下的记录：(条数, 总字节)。打不开返回 (-1, 0)。"""
    if not os.path.isfile(db_path):
        return -1, 0
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            if prefix:
                row = conn.execute(
                    "SELECT COUNT(*), SUM(file_size) FROM file_records"
                    " WHERE rel_path = ? OR rel_path LIKE ? ESCAPE '\\'",
                    (prefix, _like_escape(prefix) + "/%")).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*), SUM(file_size) FROM file_records").fetchone()
            return int(row[0] or 0), int(row[1] or 0)
        finally:
            conn.close()
    except sqlite3.Error:
        return -1, 0


class SeedTask:
    """用上级库的数据生成本目录的 filehash.db —— **只搬数据库记录，不查任何文件**。

    规则（按需求）：
      · 上级库可以隔好几级（从上一级一直往上找，直到盘符根）；
      · 只取「属于本目录范围」的记录，rel_path 去掉前缀后写入目标库；
      · sha256 / file_size / mtime / last_hash_time / status 原样搬过来，
        **不 stat、不读文件、不判断文件是否还在**；
      · 目标库已存在时按「取较新」合并（last_hash_time → mtime），与「合并数据库」一致。
    """

    def __init__(self, parent_root, parent_db, target_root, target_db,
                 prefix, log=None, stop_event=None):
        self.parent_root = os.path.abspath(parent_root)
        self.parent_db = os.path.abspath(parent_db)
        self.target_root = os.path.abspath(target_root)
        self.target_db = os.path.abspath(target_db)
        self.prefix = prefix or ""
        self.log = log or (lambda *_: None)
        self.stop = stop_event or threading.Event()
        self.ui = {"phase": "读取上级库", "done": 0, "total": 0,
                   "cur_name": "", "cur_done": 0, "cur_total": 0,
                   "done_flag": False, "aborted": False, "error": None}
        self.stats = dict(rows=0, inserted=0, updated=0,
                          missing=0, bytes=0, target_exists=False)

    def run(self):
        self.log("=" * 60)
        self.log(f"用上级库生成：{self.parent_db}"
                 + (f"（范围 {self.prefix}/）" if self.prefix else "")
                 + f" → {self.target_db}")
        try:
            # ---------- 1. 只读上级库，取本目录范围内的记录 ----------
            self.ui["phase"] = "读取上级库"
            pdb = HashDB(self.parent_db)
            try:
                try:
                    pdb.open(readonly=True)
                except Exception:
                    pdb.open()
                if self.prefix:
                    rows = pdb.conn.execute(
                        "SELECT rel_path, sha256, file_size, mtime, last_hash_time,"
                        " status, note FROM file_records"
                        " WHERE rel_path = ? OR rel_path LIKE ? ESCAPE '\\'"
                        " ORDER BY rel_path",
                        (self.prefix, _like_escape(self.prefix) + "/%")).fetchall()
                else:
                    rows = pdb.conn.execute(
                        "SELECT rel_path, sha256, file_size, mtime, last_hash_time,"
                        " status, note FROM file_records ORDER BY rel_path"
                    ).fetchall()
            finally:
                pdb.close()

            self.ui["total"] = len(rows)
            if not rows:
                self.log(f"上级库里没有「{self.prefix or '.'}」下的记录")
                return self.stats
            self.log(f"上级库命中 {len(rows)} 条"
                     f"（{fmt_size(sum((r[2] or 0) for r in rows))}），"
                     f"开始写入目标库")

            # ---------- 2. 拼目标库记录（去掉前缀，其余字段原样） ----------
            cut = len(self.prefix) + 1 if self.prefix else 0
            data = []
            for rel, sha, size, mt, lh, status, note in rows:
                if self.stop.is_set():
                    self.ui["aborted"] = True
                    self.log("已手动停止（目标库尚未写入，保持原样）")
                    return self.stats
                new_rel = rel[cut:] if self.prefix else rel
                data.append((new_rel, sha, size, mt, lh, status or "active", note))
                self.stats["bytes"] += int(size or 0)
                if (status or "active") == "deleted":
                    self.stats["missing"] += 1

            # ---------- 3. 写目标库（已有则取较新合并） ----------
            self.ui["phase"] = "写入目标库"
            tdb = HashDB(self.target_db)
            try:
                had = os.path.isfile(self.target_db)
                tdb.open()               # 不存在则新建表
                before = tdb.conn.execute(
                    "SELECT COUNT(*) FROM file_records").fetchone()[0] or 0
                self.stats["target_exists"] = had and before > 0
                try:
                    tdb.conn.executemany(MERGE_UPSERT_SQL, data)
                except sqlite3.OperationalError:
                    # 老版本 SQLite 没有 UPSERT：退化成逐条比对
                    MergeTask._merge_slow(tdb, data)
                after = tdb.conn.execute(
                    "SELECT COUNT(*) FROM file_records").fetchone()[0] or 0
            finally:
                tdb.close()

            self.stats["rows"] = len(data)
            self.stats["inserted"] = max(0, int(after) - int(before))
            self.stats["updated"] = len(data) - self.stats["inserted"]
            s = self.stats
            self.log(f"完成 —— 搬入 {s['rows']} 条（新增 {s['inserted']}，更新 "
                     f"{s['updated']}），其中库内标缺失 {s['missing']} 条；"
                     f"全程未读取任何文件")
            return s
        except Exception as e:
            import traceback
            self.ui["error"] = traceback.format_exc()
            self.log(f"[致命错误] {e}")
            return self.stats
        finally:
            self.ui["done_flag"] = True


# ============================================================
# 校验哈希（只读：重算一遍和库里比对，绝不写库）
# ============================================================

VERIFY_SAME = "一致"
VERIFY_CHANGED = "已变化"
VERIFY_GONE = "文件不存在"
VERIFY_NOHASH = "无库内哈希"
VERIFY_NOREC = "库里没有"
VERIFY_ERROR = "读取失败"


class VerifyTask:
    """只读校验：重算文件当前 SHA256，和库里的值比对，**一个字节都不写库**。

    items = [(db_path, rel_path, abs_path, root)]，可跨库（跨库搜索结果）；
    差异行（更新 / 新增）也能校验：库里有旧记录就和旧哈希比，没有就标「库里没有」。
    结果逐条记在 self.results：
      [(结论, 相对路径, 库内哈希, 当前哈希, 当前大小, 根目录, 库路径, 磁盘修改时间, 库内修改时间)]
    """

    def __init__(self, items, log=None, stop_event=None):
        self.items = list(items)
        self.log = log or (lambda *_: None)
        self.stop = stop_event or threading.Event()
        self.ui = {"phase": "校验哈希", "done": 0, "total": len(self.items),
                   "cur_name": "", "cur_done": 0, "cur_total": 0,
                   "done_flag": False, "aborted": False, "error": None}
        self.results = []
        self.stats = dict(done=0, same=0, changed=0, gone=0,
                          nohash=0, missing=0, errors=0, bytes=0)

    def _tick(self, n):
        self.stats["bytes"] += n
        self.ui["cur_done"] += n

    def run(self):
        by_db = {}
        for item in self.items:
            by_db.setdefault(item[0], []).append((item[1], item[2], item[3]))
        i = 0
        try:
            for dbp, rows in by_db.items():
                if self.stop.is_set():
                    self.ui["aborted"] = True
                    break
                db = HashDB(dbp)
                try:
                    try:
                        db.open(readonly=True)     # 只读连接，物理上就写不了库
                    except Exception:
                        db.open()
                    for rel, abs_path, root in rows:
                        if self.stop.is_set():
                            self.ui["aborted"] = True
                            break
                        i += 1
                        self.ui["cur_name"] = rel
                        self.ui["cur_done"] = 0
                        self.ui["done"] = i - 1
                        try:
                            rec = db.conn.execute(
                                "SELECT sha256, mtime FROM file_records"
                                " WHERE rel_path=?", (rel,)).fetchone()
                        except sqlite3.Error as e:
                            self.stats["errors"] += 1
                            self.results.append(
                                (VERIFY_ERROR, rel, "", "", None, root, dbp,
                                 None, None))
                            self.log(f"[读库失败] {rel}：{e}")
                            self.ui["done"] = i
                            continue
                        if rec is None:
                            self.stats["missing"] += 1
                            self.results.append(
                                (VERIFY_NOREC, rel, "", "", None, root, dbp,
                                 None, None))
                            self.log(f"[库里没有] {rel}")
                            self.ui["done"] = i
                            continue
                        expect = rec[0] or ""
                        db_mtime = rec[1]
                        try:
                            st = os.stat(long_path(abs_path))
                        except OSError:
                            self.stats["gone"] += 1
                            self.results.append(
                                (VERIFY_GONE, rel, expect, "", None, root, dbp,
                                 None, db_mtime))
                            self.log(f"[文件不存在] {rel}")
                            self.ui["done"] = i
                            continue
                        self.ui["cur_total"] = st.st_size
                        try:
                            digest = sha256_file(abs_path, self.stop, self._tick)
                        except OSError as e:
                            self.stats["errors"] += 1
                            self.results.append(
                                (VERIFY_ERROR, rel, expect, "", st.st_size,
                                 root, dbp, st.st_mtime, db_mtime))
                            self.log(f"[读失败] {rel}：{e}")
                            self.ui["done"] = i
                            continue
                        if digest is None:
                            self.ui["aborted"] = True
                            break
                        self.stats["done"] += 1
                        if not expect:
                            self.stats["nohash"] += 1
                            self.results.append(
                                (VERIFY_NOHASH, rel, "", digest, st.st_size,
                                 root, dbp, st.st_mtime, db_mtime))
                        elif expect.lower() == digest.lower():
                            self.stats["same"] += 1
                            self.results.append(
                                (VERIFY_SAME, rel, expect, digest, st.st_size,
                                 root, dbp, st.st_mtime, db_mtime))
                        else:
                            self.stats["changed"] += 1
                            self.results.append(
                                (VERIFY_CHANGED, rel, expect, digest,
                                 st.st_size, root, dbp, st.st_mtime, db_mtime))
                            self.log(f"[已变化] {rel}")
                        self.ui["done"] = i
                finally:
                    db.close()
            s = self.stats
            self.log(f"校验完成（未写库）—— 共 {s['done']} 条：一致 {s['same']}，"
                     f"已变化 {s['changed']}，文件不存在 {s['gone']}，"
                     f"无库内哈希 {s['nohash']}，库里没有 {s['missing']}，"
                     f"读取失败 {s['errors']}")
            return s
        except Exception as e:
            import traceback
            self.ui["error"] = traceback.format_exc()
            self.log(f"[致命错误] {e}")
            return self.stats
        finally:
            self.ui["done_flag"] = True


# ============================================================
# 重算哈希（右键选中记录，重读文件并直接写回）
# ============================================================

class RehashTask:
    """对选中的记录重新读文件算 SHA256，算完立即写回数据库。

    与「补全哈希」保持一致：**只更新 sha256 与 last_hash_time**，
    file_size / mtime / status 一概不动；文件不在就跳过。
    items = [(db_path, rel_path, abs_path)]，可能来自多个库（跨库搜索的结果）。
    """

    def __init__(self, items, log=None, stop_event=None):
        self.items = list(items)
        self.log = log or (lambda *_: None)
        self.stop = stop_event or threading.Event()
        self.ui = {"phase": "重算哈希", "done": 0, "total": len(self.items),
                   "cur_name": "", "cur_done": 0, "cur_total": 0,
                   "done_flag": False, "aborted": False, "error": None}
        self.stats = dict(done=0, changed=0, skipped=0, errors=0, bytes=0)

    def _tick(self, n):
        self.stats["bytes"] += n
        self.ui["cur_done"] += n

    def run(self):
        by_db = {}
        for item in self.items:
            dbp, rel, abs_path = item[0], item[1], item[2]
            by_db.setdefault(dbp, []).append((rel, abs_path))
        i = 0
        try:
            for dbp, rows in by_db.items():
                if self.stop.is_set():
                    self.ui["aborted"] = True
                    break
                lock = DbLock(dbp)
                ok, msg = lock.acquire()
                if not ok:
                    self.stats["errors"] += len(rows)
                    self.log(f"[跳过] 数据库被占用：{dbp}（{msg}）")
                    i += len(rows)
                    self.ui["done"] = i
                    continue
                db = HashDB(dbp)
                try:
                    db.open()
                    for rel, abs_path in rows:
                        if self.stop.is_set():
                            self.ui["aborted"] = True
                            break
                        i += 1
                        self.ui["cur_name"] = rel
                        self.ui["cur_done"] = 0
                        self.ui["done"] = i - 1
                        try:
                            st = os.stat(long_path(abs_path))
                        except OSError:
                            self.stats["skipped"] += 1
                            self.log(f"[跳过·文件不在了] {rel}")
                            self.ui["done"] = i
                            continue
                        self.ui["cur_total"] = st.st_size
                        try:
                            digest = sha256_file(abs_path, self.stop, self._tick)
                        except OSError as e:
                            self.stats["errors"] += 1
                            self.log(f"[读失败] {rel}：{e}")
                            self.ui["done"] = i
                            continue
                        if digest is None:
                            self.ui["aborted"] = True
                            break
                        try:
                            old = db.conn.execute(
                                "SELECT sha256 FROM file_records WHERE rel_path=?",
                                (rel,)).fetchone()
                            if old is None:
                                self.stats["errors"] += 1
                                self.log(f"[跳过·库里没有这条] {rel}")
                                self.ui["done"] = i
                                continue
                            db.conn.execute(
                                "UPDATE file_records SET sha256=?, last_hash_time=?"
                                " WHERE rel_path=?", (digest, time.time(), rel))
                        except sqlite3.Error as e:
                            self.stats["errors"] += 1
                            self.log(f"[写库失败] {rel}：{e}")
                            self.ui["done"] = i
                            continue
                        self.stats["done"] += 1
                        if old and old[0] and old[0] != digest:
                            self.stats["changed"] += 1
                            self.log(f"[已更新·哈希变化] {rel}")
                        self.ui["done"] = i
                finally:
                    db.close()
                    lock.release()
            s = self.stats
            self.log(f"重算完成 —— 成功 {s['done']} 条（其中 {s['changed']} 条哈希有变化），"
                     f"跳过(文件不在) {s['skipped']} 条，失败 {s['errors']} 条")
            return s
        except Exception as e:
            import traceback
            self.ui["error"] = traceback.format_exc()
            self.log(f"[致命错误] {e}")
            return self.stats
        finally:
            self.ui["done_flag"] = True


# ============================================================
# 检查不重复（两个镜像目录按相对路径比对）
# ============================================================

MIRROR_SAME = "内容一致"
MIRROR_DIFF = "内容不同"
MIRROR_UPD_L = "左侧有更新"
MIRROR_UPD_R = "右侧有更新"
MIRROR_LEFT = "只在左边"
MIRROR_RIGHT = "只在右边"
MIRROR_UNKNOWN = "无法比较"


def _mirror_hash(root, rel, sha, rec_size, rec_mtime, stop=None, tick=None):
    """取一条记录在磁盘上的当前哈希：库里的可信（大小 + 时间都对得上）就直接用，
    否则现读现算；文件不在就退回库里的哈希（可能为 None）。
    返回 (digest, size_now, exists, mtime_now)。"""
    abs_path = os.path.join(root, *rel.split("/"))
    try:
        st = os.stat(long_path(abs_path))
    except OSError:
        return sha or None, None, False, None
    digest = sha
    if (not digest or rec_mtime is None or int(rec_size or -1) != int(st.st_size)
            or abs(st.st_mtime - rec_mtime) > 1e-6):
        try:
            digest = sha256_file(abs_path, stop, tick)
        except OSError:
            return sha or None, st.st_size, True, st.st_mtime
    return digest, st.st_size, True, st.st_mtime


def compare_dirs(left, right, log=None, stop=None, on_progress=None,
                 include_same=False):
    """按「相对路径」把两个目录的库对齐比对（镜像盘校验）。

    left / right = (root, db_path)。
    返回 (rows, stats)：
      rows  = [(类别, rel, 左哈希, 右哈希, 左大小, 右大小, 左修改时间, 右修改时间, 右rel)]，
              rel = 左路径（只在右边时 = 右路径），右rel = 右侧实际路径（普通模式 = rel）；
              类别 ∈ 内容不同 / 左右侧有更新 / 只在左边 / 只在右边 / 无法比较；
              include_same=True 时「内容一致」的行也进 rows（MIRROR_SAME）
      stats = dict(common, same, diff, update_left, update_right,
                   only_left, only_right, unknown, bytes)
    哈希在「库里的大小 + 修改时间都对得上」时直接复用，否则现读现算。
    """
    def load(db_path):
        if not db_path or not os.path.isfile(db_path):
            raise FileNotFoundError(db_path or "")
        db = HashDB(db_path)
        try:
            try:
                db.open(readonly=True)
            except Exception:
                db.open()
            return db.conn.execute(
                "SELECT rel_path, sha256, file_size, mtime FROM file_records"
                " WHERE status='active'").fetchall()
        finally:
            db.close()

    lroot, ldb = left
    rroot, rdb = right
    lrows = load(ldb)
    rrows = load(rdb)
    lmap = {r[0].lower(): r for r in lrows}
    rmap = {r[0].lower(): r for r in rrows}

    stats = dict(common=0, same=0, diff=0, update_left=0, update_right=0,
                 only_left=0, only_right=0, unknown=0, bytes=0)
    rows = []
    common = [k for k in lmap if k in rmap]
    stats["common"] = len(common)
    total = len(common) or 1

    def tick(n):
        stats["bytes"] += n

    for i, key in enumerate(common, 1):
        if stop is not None and stop.is_set():
            break
        lrel, lsha, lsize, lmt = lmap[key]
        rrel, rsha, rsize, rmt = rmap[key]
        if on_progress:
            on_progress(i, total, lrel)
        ldig, lsize_now, _lex, lmt_now = _mirror_hash(
            lroot, lrel, lsha, lsize, lmt, stop, tick)
        rdig, rsize_now, _rex, rmt_now = _mirror_hash(
            rroot, rrel, rsha, rsize, rmt, stop, tick)
        if ldig is None or rdig is None:
            stats["unknown"] += 1
            rows.append((MIRROR_UNKNOWN, lrel, ldig, rdig,
                         lsize_now if lsize_now is not None else lsize,
                         rsize_now if rsize_now is not None else rsize,
                         lmt_now, rmt_now, rrel))
        elif ldig == rdig:
            stats["same"] += 1
            if include_same:
                rows.append((MIRROR_SAME, lrel, ldig, rdig,
                             lsize_now if lsize_now is not None else lsize,
                             rsize_now if rsize_now is not None else rsize,
                             lmt_now, rmt_now, rrel))
        else:
            stats["diff"] += 1
            # 内容不同时按修改时间拆分方向：左侧较新 = 要用左更新右；反之亦然
            if lmt_now is not None and rmt_now is not None and lmt_now > rmt_now:
                stats["update_left"] += 1
                kind = MIRROR_UPD_L
            elif lmt_now is not None and rmt_now is not None \
                    and rmt_now > lmt_now:
                stats["update_right"] += 1
                kind = MIRROR_UPD_R
            else:
                kind = MIRROR_DIFF
            rows.append((kind, lrel, ldig, rdig,
                         lsize_now if lsize_now is not None else lsize,
                         rsize_now if rsize_now is not None else rsize,
                         lmt_now, rmt_now, rrel))

    for key, (rel, sha, size, mt) in lmap.items():
        if key in rmap:
            continue
        stats["only_left"] += 1
        rows.append((MIRROR_LEFT, rel, sha, "", size, None, mt, None, ""))
    for key, (rel, sha, size, mt) in rmap.items():
        if key in lmap:
            continue
        stats["only_right"] += 1
        rows.append((MIRROR_RIGHT, rel, "", sha, None, size, None, mt, rel))

    order = {MIRROR_DIFF: 0, MIRROR_UPD_L: 1, MIRROR_UPD_R: 2,
             MIRROR_LEFT: 3, MIRROR_RIGHT: 4, MIRROR_UNKNOWN: 5,
             MIRROR_SAME: 6}
    rows.sort(key=lambda r: (order.get(r[0], 9), str(r[1]).lower()))
    if log:
        s = stats
        log(f"镜像比对完成 —— 共同 {s['common']} 个文件，一致 {s['same']}，"
            f"内容不同 {s['diff']}（左侧有更新 {s['update_left']}，"
            f"右侧有更新 {s['update_right']}），只在左边 {s['only_left']}，"
            f"只在右边 {s['only_right']}，无法比较 {s['unknown']}")
    return rows, stats


def compare_by_hash(left, right, log=None, stop=None, on_progress=None):
    """按「哈希」对齐两个目录：找出改名 / 移动 / 合并的文件（合并查重场景）。

    只比较库里已算过哈希（sha256 非空）的记录；同一哈希两边都按相对路径排序后
    顺序配对，多出来的算单边。
    返回 (rows, stats)：
      rows  = [(类别, 左rel, 左哈希, 右哈希, 左大小, 右大小, 左mtime, 右mtime, 右rel)]
              类别 ∈ 内容一致 / 只在左边 / 只在右边（配对成功 = 内容一致，
              左右 rel 不同即「改名 / 移动」）
      stats = dict(same, renamed, only_left, only_right, nohash_l, nohash_r)
    """
    def load(db_path):
        if not db_path or not os.path.isfile(db_path):
            raise FileNotFoundError(db_path or "")
        db = HashDB(db_path)
        try:
            try:
                db.open(readonly=True)
            except Exception:
                db.open()
            return db.conn.execute(
                "SELECT rel_path, sha256, file_size, mtime FROM file_records"
                " WHERE status='active'").fetchall()
        finally:
            db.close()

    lrows = load(left[1])
    rrows = load(right[1])
    stats = dict(same=0, renamed=0, only_left=0, only_right=0,
                 nohash_l=0, nohash_r=0)
    lmap, rmap = {}, {}
    for rel, sha, size, mt in lrows:
        if not sha:
            stats["nohash_l"] += 1
            continue
        lmap.setdefault(sha.lower(), []).append((rel, size, mt))
    for rel, sha, size, mt in rrows:
        if not sha:
            stats["nohash_r"] += 1
            continue
        rmap.setdefault(sha.lower(), []).append((rel, size, mt))

    rows = []
    keys = sorted(set(lmap) | set(rmap))
    total = len(keys) or 1
    for i, k in enumerate(keys, 1):
        if stop is not None and stop.is_set():
            break
        if on_progress:
            on_progress(i, total, (lmap.get(k) or rmap.get(k))[0][0])
        litems = sorted(lmap.get(k, []))
        ritems = sorted(rmap.get(k, []))
        if litems and ritems:
            for (lrel, lsize, lmt), (rrel, rsize, rmt) in zip(litems, ritems):
                stats["same"] += 1
                if lrel.lower() != rrel.lower():
                    stats["renamed"] += 1
                rows.append((MIRROR_SAME, lrel, k, k, lsize, rsize,
                             lmt, rmt, rrel))
            for (lrel, lsize, lmt) in litems[len(ritems):]:
                stats["only_left"] += 1
                rows.append((MIRROR_LEFT, lrel, k, "", lsize, None,
                             lmt, None, ""))
            for (rrel, rsize, rmt) in ritems[len(litems):]:
                stats["only_right"] += 1
                rows.append((MIRROR_RIGHT, rrel, "", k, None, rsize,
                             None, rmt, rrel))
        elif litems:
            for (lrel, lsize, lmt) in litems:
                stats["only_left"] += 1
                rows.append((MIRROR_LEFT, lrel, k, "", lsize, None,
                             lmt, None, ""))
        else:
            for (rrel, rsize, rmt) in ritems:
                stats["only_right"] += 1
                rows.append((MIRROR_RIGHT, rrel, "", k, None, rsize,
                             None, rmt, rrel))

    order = {MIRROR_SAME: 0, MIRROR_LEFT: 1, MIRROR_RIGHT: 2}
    rows.sort(key=lambda r: (order.get(r[0], 9), str(r[1]).lower()))
    if log:
        s = stats
        log(f"哈希比对完成 —— 配对成功 {s['same']} 个"
            f"（其中改名/移动 {s['renamed']} 个），"
            f"只在左边 {s['only_left']}，只在右边 {s['only_right']}；"
            f"未算哈希 左 {s['nohash_l']} / 右 {s['nohash_r']}（不参与比对）")
    return rows, stats


# ============================================================
# 镜像同步（把一边的内容 / 修改时间同步到另一边）
# ============================================================

SYNC_BOTH = "both"
SYNC_LEFT = "left"
SYNC_RIGHT = "right"


def trash_files(paths, log=None):
    """把文件移入回收站（Windows SHFileOperationW + FOF_ALLOWUNDO）；非 Windows 直接删。
    返回移入回收站的文件个数（-1 表示调用失败）。"""
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        return 0
    if os.name != "nt":
        n = 0
        for p in paths:
            try:
                os.remove(p)
                n += 1
            except OSError as e:
                if log:
                    log(f"[回收站失败] {p}：{e}")
        return n
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND),
                    ("wFunc", ctypes.c_uint),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x4

    pfrom = "\0".join(paths) + "\0"
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = pfrom
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if r != 0 and log:
        log(f"[回收站] SHFileOperationW 返回 {r}")
    return len(paths) if r == 0 else -1


def build_sync_plan(rows, mode, lroot, rroot):
    """根据比对结果生成同步计划。
    rows = [(kind, rel, lsha, rsha, lsize, rsize, lmt, rmt, rrel)]，
    rel = 左路径，rrel = 右侧实际路径（哈希模式改名后可能和 rel 不同；可为空）
    mode ∈ SYNC_BOTH（双向，较新覆盖较旧）/ SYNC_LEFT（以左为准）/ SYNC_RIGHT（以右为准）
    返回 plan = {"copy": [(src, dst, overwrite)], "delete": [path],
                 "touch": [(path, mtime)], "skip": [rel]}"""
    plan = {"copy": [], "delete": [], "touch": [], "skip": []}
    for kind, rel, lsha, rsha, lsize, rsize, lmt, rmt, rrel in rows:
        lpath = os.path.join(lroot, *rel.split("/"))
        # 右侧目标：哈希模式下用右侧实际路径（保持改名后的名字），没有就用同名
        rpath = os.path.join(rroot, *(rrel or rel).split("/"))
        if kind == MIRROR_UNKNOWN:
            plan["skip"].append(rel)
            continue
        if mode == SYNC_LEFT:
            if kind in (MIRROR_LEFT, MIRROR_DIFF, MIRROR_UPD_L, MIRROR_UPD_R):
                plan["copy"].append((lpath, rpath, os.path.exists(rpath)))
            elif kind == MIRROR_RIGHT:
                if os.path.exists(rpath):
                    plan["delete"].append(rpath)
                else:
                    plan["skip"].append(rel)
            elif kind == MIRROR_SAME:
                if lmt is not None and rmt is not None \
                        and abs(lmt - rmt) > 1e-6:
                    plan["touch"].append((rpath, lmt))
        elif mode == SYNC_RIGHT:
            if kind in (MIRROR_RIGHT, MIRROR_DIFF, MIRROR_UPD_L, MIRROR_UPD_R):
                plan["copy"].append((rpath, lpath, os.path.exists(lpath)))
            elif kind == MIRROR_LEFT:
                if os.path.exists(lpath):
                    plan["delete"].append(lpath)
                else:
                    plan["skip"].append(rel)
            elif kind == MIRROR_SAME:
                if lmt is not None and rmt is not None \
                        and abs(lmt - rmt) > 1e-6:
                    plan["touch"].append((lpath, rmt))
        else:  # SYNC_BOTH
            if kind == MIRROR_LEFT:
                plan["copy"].append((lpath, rpath, False))
            elif kind == MIRROR_RIGHT:
                plan["copy"].append((rpath, lpath, False))
            elif kind in (MIRROR_DIFF, MIRROR_UPD_L, MIRROR_UPD_R):
                if lmt is None or rmt is None:
                    plan["skip"].append(rel)
                elif lmt > rmt:
                    plan["copy"].append((lpath, rpath, True))
                elif rmt > lmt:
                    plan["copy"].append((rpath, lpath, True))
                else:
                    plan["skip"].append(rel)
            elif kind == MIRROR_SAME:
                # 哈希一致但日期不一致：只把较新的修改时间应用到较旧一侧
                if lmt is not None and rmt is not None \
                        and abs(lmt - rmt) > 1e-6:
                    if lmt > rmt:
                        plan["touch"].append((rpath, lmt))
                    else:
                        plan["touch"].append((lpath, rmt))
    return plan


def execute_sync_plan(plan, use_trash, log=None, on_progress=None):
    """执行同步计划。返回 (stats, failed)。
    stats = dict(copy, overwrite, delete, touch, recycled)"""
    import shutil
    stats = {"copy": 0, "overwrite": 0, "delete": 0, "touch": 0,
             "recycled": 0}
    failed = []

    def ensure_dir(p):
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)

    # 1) 删除：单边多出来的（回收站 / 直接删）
    to_trash = []
    for p in plan["delete"]:
        if not os.path.exists(p):
            continue
        if use_trash:
            to_trash.append(p)
        else:
            try:
                os.remove(p)
                stats["delete"] += 1
            except OSError as e:
                failed.append((p, str(e)))
    # 2) 覆盖前，把被覆盖的目标移入回收站（仅回收站模式）
    if use_trash:
        for _src, dst, overwrite in plan["copy"]:
            if overwrite and os.path.exists(dst):
                to_trash.append(dst)
    if to_trash:
        n = trash_files(to_trash, log)
        if n < 0:
            failed.append(("回收站", "SHFileOperationW 失败"))
        else:
            stats["recycled"] += n
    # 3) 复制（新增 / 覆盖）
    for src, dst, overwrite in plan["copy"]:
        try:
            if not os.path.exists(src):
                failed.append((src, "源文件不存在"))
                continue
            ensure_dir(dst)
            shutil.copy2(src, dst)
            stats["overwrite" if overwrite else "copy"] += 1
        except OSError as e:
            failed.append((src, str(e)))
    # 4) 只改修改时间
    for path, mt in plan["touch"]:
        try:
            if os.path.exists(path):
                os.utime(path, (mt, mt))
                stats["touch"] += 1
            else:
                failed.append((path, "文件不存在"))
        except OSError as e:
            failed.append((path, str(e)))
    return stats, failed




# 合并时「谁更新」的判定：先比 last_hash_time，再比 mtime，都相同就保留主库原值
_NEWER_SQL = (
    "(COALESCE(excluded.last_hash_time, 0) > COALESCE(file_records.last_hash_time, 0)"
    " OR (COALESCE(excluded.last_hash_time, 0) = COALESCE(file_records.last_hash_time, 0)"
    " AND COALESCE(excluded.mtime, 0) > COALESCE(file_records.mtime, 0)))"
)

MERGE_UPSERT_SQL = (
    "INSERT INTO file_records"
    " (rel_path, sha256, file_size, mtime, last_hash_time, status, note)"
    " VALUES (?,?,?,?,?,?,?)"
    " ON CONFLICT(rel_path) DO UPDATE SET"
    " sha256 = CASE WHEN " + _NEWER_SQL +
    " THEN excluded.sha256 ELSE file_records.sha256 END,"
    " file_size = CASE WHEN " + _NEWER_SQL +
    " THEN excluded.file_size ELSE file_records.file_size END,"
    " mtime = CASE WHEN " + _NEWER_SQL +
    " THEN excluded.mtime ELSE file_records.mtime END,"
    " last_hash_time = CASE WHEN " + _NEWER_SQL +
    " THEN excluded.last_hash_time ELSE file_records.last_hash_time END,"
    " status = CASE WHEN " + _NEWER_SQL +
    " THEN excluded.status ELSE file_records.status END,"
    " note = CASE WHEN " + _NEWER_SQL +
    " THEN excluded.note ELSE file_records.note END"
)


def db_row_count(db_path):
    """只读拿一个库的记录条数；打不开返回 -1"""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM file_records").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return -1


def find_sub_dbs(root, skip=None):
    """列出 root 之下各级子目录里的 filehash.db（只看文件名，不读普通文件）。
    返回 [(rel_dir, abs_path)]，rel_dir 用 / 分隔、按层级由浅到深排序。"""
    root = os.path.abspath(root)
    skip_key = os.path.normcase(os.path.abspath(skip)) if skip else ""
    out = []
    stack = [long_path(root)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                if entry.name.lower() != DB_NAME:
                    continue
            except OSError:
                continue
            abs_path = strip_long(entry.path)
            if skip_key and os.path.normcase(os.path.abspath(abs_path)) == skip_key:
                continue
            rel_dir = os.path.relpath(os.path.dirname(abs_path), root)
            rel_dir = rel_dir.replace(os.sep, "/")
            out.append(("" if rel_dir == "." else rel_dir, abs_path))
    out.sort(key=lambda x: (x[0].count("/"), x[0].lower()))
    return out


class MergeTask:
    """把 root 下各级子目录的 filehash.db 合并进 root 自己的库。

    规则（按需求）：
      · rel_path 加上「子库相对 root 的目录」前缀；
      · 重复 rel_path 时**保留较新的那条**（先比 last_hash_time，再比 mtime）；
      · 每个子库合并完立即提交，成功后**删除子库文件**（-wal / -shm / -journal 一起删）；
      · 只操作数据库文件，不读取、不修改任何普通文件（连大小/时间都不看）。
    """

    def __init__(self, root, db_path, log=None, stop_event=None):
        self.root = os.path.abspath(root)
        self.db_path = os.path.abspath(db_path)
        self.log = log or (lambda *_: None)
        self.stop = stop_event or threading.Event()
        self.ui = {"phase": "扫描子库", "done": 0, "total": 0,
                   "cur_name": "", "cur_done": 0, "cur_total": 0,
                   "done_flag": False, "aborted": False, "error": None}
        self.stats = dict(dbs=0, rows=0, inserted=0, updated=0,
                          removed=0, errors=0, failed=[], removed_paths=[])

    def run(self):
        self.log("=" * 60)
        self.log(f"合并数据库：{self.root} → {self.db_path}")
        try:
            subs = find_sub_dbs(self.root, skip=self.db_path)
            self.ui["total"] = len(subs)
            if not subs:
                self.log("没有找到子目录里的 filehash.db")
                return self.stats
            self.log(f"找到 {len(subs)} 个子库，开始合并")
            db = HashDB(self.db_path)
            try:
                db.open()          # 主库不存在时自动建表
                for i, (rel_dir, sub_path) in enumerate(subs, 1):
                    if self.stop.is_set():
                        self.ui["aborted"] = True
                        self.log("已手动停止（已合并的子库都已提交并删除）")
                        break
                    self.ui["phase"] = f"合并子库 {i}/{len(subs)}"
                    self.ui["cur_name"] = rel_dir or os.path.basename(sub_path)
                    self.ui["done"] = i - 1
                    try:
                        n, ins, upd = self._merge_one(db, rel_dir, sub_path)
                    except Exception as e:
                        self.stats["errors"] += 1
                        self.stats["failed"].append(sub_path)
                        self.log(f"[合并失败] {rel_dir or '.'}：{e}")
                        self.ui["done"] = i
                        continue
                    self.stats["dbs"] += 1
                    self.stats["rows"] += n
                    self.stats["inserted"] += ins
                    self.stats["updated"] += upd
                    self.log(f"[已合并] {rel_dir or '.'}：{n} 条"
                             f"（新增 {ins}，更新 {upd}）")
                    if self._remove_db(sub_path):
                        self.stats["removed"] += 1
                        self.stats["removed_paths"].append(sub_path)
                    self.ui["done"] = i
            finally:
                db.close()
            s = self.stats
            self.log(f"合并完成 —— {s['dbs']} 个子库，共 {s['rows']} 条"
                     f"（新增 {s['inserted']}，更新 {s['updated']}），"
                     f"删除子库 {s['removed']} 个"
                     + (f"，失败 {s['errors']} 个" if s["errors"] else ""))
            return s
        except Exception as e:
            import traceback
            self.ui["error"] = traceback.format_exc()
            self.log(f"[致命错误] {e}")
            return self.stats
        finally:
            self.ui["done_flag"] = True

    def _merge_one(self, db, rel_dir, sub_path):
        """合并一个子库，返回 (行数, 新增, 更新)"""
        sub = sqlite3.connect(sub_path, timeout=10)
        try:
            rows = sub.execute(
                "SELECT rel_path, sha256, file_size, mtime, last_hash_time,"
                " status, note FROM file_records").fetchall()
        finally:
            try:
                sub.close()
            except Exception:
                pass
        prefix = (rel_dir + "/") if rel_dir else ""
        data = [(prefix + r[0], r[1], r[2], r[3], r[4], r[5] or "active", r[6])
                for r in rows]
        if not data:
            return 0, 0, 0
        before = db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0]
        try:
            db.conn.executemany(MERGE_UPSERT_SQL, data)
        except sqlite3.OperationalError:
            # 老版本 SQLite 没有 UPSERT：退化成逐条比对
            self._merge_slow(db, data)
        after = db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0]
        inserted = max(0, int(after) - int(before))
        return len(data), inserted, len(data) - inserted

    @staticmethod
    def _merge_slow(db, data):
        for rel, sha, size, mt, lh, st, note in data:
            old = db.conn.execute(
                "SELECT last_hash_time, mtime FROM file_records WHERE rel_path=?",
                (rel,)).fetchone()
            if old is None:
                db.conn.execute(
                    "INSERT INTO file_records(rel_path, sha256, file_size, mtime,"
                    " last_hash_time, status, note) VALUES(?,?,?,?,?,?,?)",
                    (rel, sha, size, mt, lh, st, note))
                continue
            newer = ((lh or 0) > (old[0] or 0)
                     or ((lh or 0) == (old[0] or 0) and (mt or 0) > (old[1] or 0)))
            if newer:
                db.conn.execute(
                    "UPDATE file_records SET sha256=?, file_size=?, mtime=?,"
                    " last_hash_time=?, status=?, note=? WHERE rel_path=?",
                    (sha, size, mt, lh, st, note, rel))

    def _remove_db(self, sub_path):
        ok = True
        for p in (sub_path, sub_path + "-wal", sub_path + "-shm",
                  sub_path + "-journal"):
            if not os.path.exists(p):
                continue
            try:
                os.remove(p)
            except OSError as e:
                ok = False
                self.log(f"[删除失败] {p}：{e}")
        return ok


# ============================================================
# 重复文件检查
# ============================================================

def find_duplicates(dirs, log=None, stop=None, on_progress=None):
    """在若干目录里查重：先按**磁盘上的当前大小**粗筛，再比对 SHA256。
    dirs = [(root, db_path)]。
    库里的哈希在「大小 + 修改时间」都对得上时直接复用，否则只对同尺寸候选现读现算，
    不会整盘重读。返回 [(size, sha, [(root, rel, abs_path, mtime)])]，体积从大到小。"""
    entries = []
    for root, db_path in dirs:
        if not os.path.isfile(db_path):
            if log:
                log(f"[查重] 跳过（还没有库）：{root}")
            continue
        db = HashDB(db_path)
        rows = []
        try:
            try:
                db.open(readonly=True)
            except Exception:
                db.open()
            rows = db.conn.execute(
                "SELECT rel_path, sha256, file_size, mtime FROM file_records"
                " WHERE status='active' AND file_size IS NOT NULL").fetchall()
        except sqlite3.Error as e:
            if log:
                log(f"[查重] 读取失败 {root}：{e}")
        finally:
            db.close()
        for rel, sha, size, mt in rows:
            abs_path = os.path.join(root, *rel.split("/"))
            try:
                st = os.stat(long_path(abs_path))
            except OSError:
                continue                       # 文件已经不在了
            entries.append((int(st.st_size), root, rel, abs_path, sha, mt,
                            st.st_mtime, int(size)))

    by_size = {}
    for e in entries:
        by_size.setdefault(e[0], []).append(e)
    pairs = [(sz, v) for sz, v in by_size.items() if len(v) > 1]
    pairs.sort(key=lambda x: -x[0])

    groups = []
    for idx, (size, items) in enumerate(pairs, 1):
        if stop is not None and stop.is_set():
            break
        if on_progress:
            on_progress(idx, len(pairs), size, len(items))
        by_sha = {}
        for size_now, root, rel, abs_path, sha, mt, mtime_now, rec_size in items:
            digest = sha
            # 库里的哈希只在「大小 + 修改时间都对得上」时才算可信
            if (not digest or mt is None or rec_size != size_now
                    or abs(mtime_now - mt) > 1e-6):
                try:
                    digest = sha256_file(abs_path, stop)
                except OSError as e:
                    if log:
                        log(f"[查重] 读不了 {abs_path}：{e}")
                    continue
                if digest is None:
                    break
            by_sha.setdefault(digest, []).append(
                (root, rel, abs_path, mtime_now))
        for digest, files in by_sha.items():
            if len(files) < 2:
                continue
            files.sort(key=lambda x: (x[0].lower(), x[1].lower()))
            groups.append((size, digest, files))
    groups.sort(key=lambda g: (-g[0], g[1]))
    return groups


# ============================================================
# GUI
# ============================================================

DIFF_TAGS = {"新增": "new", "恢复": "new", "缺失": "missing", "变更": "changed"}

# 库里存 active/deleted，界面上显示中文
STATUS_TEXT = {"active": "有效", "deleted": "缺失"}

# 表格列：single = 只搜当前目录；multi = 跨库搜索时多一列「所属目录」
# 第一列是复选框（点一下即勾选，与 Treeview 的 selection 同步）；
# 路径最宽（唯一可伸缩列），哈希状态在中间，完整 SHA256 放最后且只显示前 16 位。
COLS = [("chk", "选", 34, 30, False),
        ("root", "所属目录", 180, 130, False),
        ("rel", "相对路径", 380, 140, True),
        ("size", "文件大小", 84, 74, False),
        ("mtime", "修改时间", 132, 118, False),
        ("last", "上次哈希校验", 132, 118, False),
        ("hash", "哈希", 76, 66, False),
        ("sha", "SHA256（前 16 位）", 150, 120, False),
        ("status", "状态", 86, 70, False)]
COLS_SINGLE = ("chk", "rel", "size", "mtime", "last", "hash", "sha", "status")
COLS_MULTI = ("chk", "root", "rel", "size", "mtime", "last", "hash", "sha", "status")
FIXED_W_SINGLE = 34 + 84 + 132 + 132 + 76 + 150 + 86
FIXED_W_MULTI = FIXED_W_SINGLE + 180


def build_hash_tab(parent, app=None):
    """⑲ 硬盘哈希扫描：选根目录 → 四模式扫描 → 差异多选写回 → 搜路径/哈希"""
    parent.grid_columnconfigure(0, weight=1)
    parent.grid_rowconfigure(4, weight=1)

    make_tip(parent, 0,
             "冷数据建档与校验：扫描根可以直接从下拉框选已管理的目录（也可输入/浏览），"
             "数据库 filehash.db 建在该目录下。模式按「改不改库 / 算不算哈希」组合："
             "【快速预检】只读·不算哈希，【快速建档】改库·不算哈希（秒级建索引），"
             "【完整检查】只读·算哈希，【完整建档】改库·算哈希并标记缺失。"
             "两种只读模式扫完的差异清单可多选后「写入数据库」，底部按 新增 / 更新 / 删除 / 已计算 / 未计算 分类看；"
             "「管理...」里按住 Ctrl/Shift 多选目录，还能做重复文件检查与镜像比对；"
             "「合并数据库」把子目录的库并进来，「用上级库」反过来按上级库生成本目录的库（都不读文件）。",
             colspan=1, autowrap=True)

    state = {
        "running": False,
        "stop": threading.Event(),
        "rows": [],          # 表格全量内容 [(rel, sha, size, mtime, last, status, root)]
        "pending": [],        # 与 rows 平行：差异行的结构化记录，库内记录为 None
        "shown": [],          # 当前筛选后真正显示的行 [(row, pending)]
        "page_rows": [],      # 当前页实际渲染的行（shown 的切片）
        "filter": "全部",     # 底部筛选：全部 / 新增 / 更新 / 删除 / 已计算 / 未计算
        "page": 0,            # 当前页码（0 基）
        "page_size": DEFAULT_PAGE_SIZE,   # 每页条数，0 = 不分页
        "scanner": None,
        "task": "",           # 当前后台任务："scan" 扫描 / "hash" 补全哈希
        "lock": None,
        "anim": False,
        "multi": False,      # 当前表格是否为跨库结果（多显示「所属目录」列）
    }

    # ---------------- 表单 ----------------
    form = make_form(parent)
    form.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 2))

    # 扫描根（下拉可选已管理目录，也可以直接输入/浏览）
    form_label(form, 0, "扫描根目录：")
    var_root = tk.StringVar()
    cmb_root = ttk.Combobox(form, textvariable=var_root, width=52, values=[])
    cmb_root.grid(row=0, column=1, sticky="w", padx=(0, 6), pady=5)

    def choose_root():
        p = filedialog.askdirectory(title="选择扫描根目录（盘符或任意文件夹）")
        if p:
            var_root.set(os.path.abspath(p))
            refresh_db_info()

    btn_box = ui.Frame(form)
    btn_box.grid(row=0, column=2, sticky="w", padx=(0, 4), pady=5)
    ui.Button(btn_box, text="浏览...", command=choose_root).pack(side=tk.LEFT)
    ui.Button(btn_box, text="管理...", width=7,
               command=lambda: open_manager()).pack(side=tk.LEFT, padx=(4, 0))
    form_note(form, 0, "下拉可选已管理的目录；库文件固定为「根目录\\filehash.db」")

    # 数据库
    form_label(form, 1, "当前数据库：")
    var_db = tk.StringVar(value="（先选择扫描根目录）")
    ttk.Entry(form, textvariable=var_db, width=52, state="readonly").grid(
        row=1, column=1, sticky="w", padx=(0, 6), pady=5)
    db_btns = ui.Frame(form)
    db_btns.grid(row=1, column=2, sticky="w", padx=(0, 4), pady=5)
    ui.Button(db_btns, text="查看", width=6,
               command=lambda: view_db()).pack(side=tk.LEFT)
    ui.Button(db_btns, text="补全哈希", width=9,
               command=lambda: fill_hashes()).pack(side=tk.LEFT, padx=(4, 0))
    ui.Button(db_btns, text="合并数据库", width=10,
               command=lambda: merge_dbs()).pack(side=tk.LEFT, padx=(4, 0))
    ui.Button(db_btns, text="用上级库", width=9,
               command=lambda: seed_from_parent()).pack(side=tk.LEFT, padx=(4, 0))
    lbl_scope = ui.Label(form, text="", foreground=COLOR["text_secondary"])
    lbl_scope.grid(row=1, column=3, sticky="w", padx=(8, 4), pady=5)

    # 模式
    form_label(form, 2, "扫描模式：")
    frame_mode = ui.Frame(form)
    frame_mode.grid(row=2, column=1, sticky="w", pady=3)
    var_mode = tk.StringVar(value=MODE_PRECHECK)     # 默认快速预检（只读、不算哈希）
    for _i, _key in enumerate(MODE_ORDER):
        ttk.Radiobutton(frame_mode, text=MODE_LABELS[_key], value=_key,
                        variable=var_mode).grid(
            row=_i // 2, column=_i % 2, sticky="w", padx=(0, 18), pady=1)
    form_note(form, 2, "只读模式扫完可多选写回数据库")

    # ---------------- 执行 / 进度 ----------------
    run_row = ui.Frame(parent)
    run_row.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 2))
    run_row.grid_columnconfigure(4, weight=1)

    btn_start = ui.Button(run_row, text="开始扫描", bg=COLOR["accent"], fg=COLOR["text_on_accent"],
                          disabledforeground=COLOR["text_on_accent"],
                          activebackground=COLOR["accent_active"], activeforeground=COLOR["text_on_accent"],
                          font=FONTS["base"], padx=18, pady=4,
                          cursor="hand2")
    btn_start.grid(row=0, column=0, padx=(0, 6))
    btn_stop = ui.Button(run_row, text="停止", state=tk.DISABLED, padx=14, pady=4,
                         command=lambda: on_stop())
    btn_stop.grid(row=0, column=1, padx=(0, 10))

    bar_all = ttk.Progressbar(run_row, mode="determinate", maximum=100, length=220)
    bar_all.grid(row=0, column=2, sticky="w")
    bar_cur = ttk.Progressbar(run_row, mode="determinate", maximum=100, length=110)
    bar_cur.grid(row=0, column=3, sticky="w", padx=(8, 10))
    lbl_prog = ui.Label(run_row, text="未开始", foreground=COLOR["text_secondary"])
    lbl_prog.grid(row=0, column=4, sticky="w")

    # ---------------- 搜索 ----------------
    search_row = ui.Frame(parent)
    search_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 2))
    ui.Label(search_row, text="搜索：").pack(side=tk.LEFT)
    var_kw = tk.StringVar()
    ent_kw = ttk.Entry(search_row, textvariable=var_kw, width=34)
    ent_kw.pack(side=tk.LEFT, padx=(0, 6))
    var_kind = tk.StringVar(value="按路径模糊")
    ttk.Combobox(search_row, textvariable=var_kind, width=12, state="readonly",
                 values=["按路径模糊", "按 SHA256"]).pack(side=tk.LEFT, padx=(0, 6))
    var_scope = tk.StringVar(value="当前目录")
    ttk.Combobox(search_row, textvariable=var_scope, width=16, state="readonly",
                 values=["当前目录", "全部已管理目录"]).pack(side=tk.LEFT, padx=(0, 6))
    ui.Button(search_row, text="搜索", width=6, command=lambda: do_search()).pack(
        side=tk.LEFT, padx=(0, 6))
    var_inc_deleted = tk.BooleanVar(value=False)
    ttk.Checkbutton(search_row, text="含缺失", variable=var_inc_deleted).pack(
        side=tk.LEFT, padx=(0, 6))
    ui.Button(search_row, text="导出 CSV", command=lambda: export_csv()).pack(
        side=tk.LEFT, padx=(0, 6))
    lbl_count = ui.Label(search_row, text="", foreground=COLOR["text_secondary"])
    lbl_count.pack(side=tk.LEFT, padx=6)

    # ---------------- 结果表格 ----------------
    tree_frame = ui.Frame(parent)
    tree_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=(0, 4))
    tree_frame.grid_rowconfigure(1, weight=1)
    tree_frame.grid_columnconfigure(0, weight=1)

    # 列表上方：写回数据库 / 全选
    bar_list = ui.Frame(tree_frame)
    bar_list.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
    btn_apply = ui.Button(bar_list, text="写入数据库", bg=COLOR["success"], fg=COLOR["text_on_accent"],
                          disabledforeground=COLOR["text_on_accent"],
                          activebackground=COLOR["success_active"], activeforeground=COLOR["text_on_accent"],
                          font=FONTS["sm"], padx=12, pady=2,
                          cursor="hand2")
    btn_apply.pack(side=tk.LEFT)
    btn_sel_all = ui.Button(bar_list, text="全选本页", width=9, state=tk.DISABLED)
    btn_sel_all.pack(side=tk.LEFT, padx=(8, 0))
    btn_sel_none = ui.Button(bar_list, text="全不选", width=7, state=tk.DISABLED)
    btn_sel_none.pack(side=tk.LEFT, padx=(4, 0))
    lbl_sel = ui.Label(bar_list, text="", foreground=COLOR["text_secondary"])
    lbl_sel.pack(side=tk.LEFT, padx=(10, 0))
    lbl_hint = ui.Label(bar_list, text="", foreground=COLOR["success"])
    lbl_hint.pack(side=tk.LEFT, padx=(12, 0))

    tree = ttk.Treeview(tree_frame, columns=[c[0] for c in COLS], show="headings",
                        selectmode="extended", displaycolumns=COLS_SINGLE)
    for key, title, width, minwidth, stretch in COLS:
        tree.heading(key, text=title,
                     anchor="center" if key == "chk" else "w")
        tree.column(key, width=width, minwidth=minwidth,
                    anchor="center" if key == "chk" else "w", stretch=stretch)

    def _fit_columns(_e=None):
        """除「相对路径」外都是固定宽，剩余空间全给路径列。
        Tk 的 stretch 只会加宽不会收窄，不自己算的话最后一列会掉到横向滚动区外面。"""
        try:
            w = tree.winfo_width()
        except Exception:
            return
        if w <= 1:
            return
        fixed = FIXED_W_MULTI if state["multi"] else FIXED_W_SINGLE
        rel_w = max(160, w - fixed - 14)
        try:
            if int(tree.column("rel", "width")) != rel_w:
                tree.column("rel", width=rel_w)
        except Exception:
            pass

    tree.bind("<Configure>", _fit_columns)
    vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.grid(row=1, column=0, sticky="nsew")
    vsb.grid(row=1, column=1, sticky="ns")
    hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=tree.xview)
    tree.configure(xscrollcommand=hsb.set)
    hsb.grid(row=2, column=0, sticky="ew")

    # 列表下方：左侧分类筛选（全部 / 新增 / 更新 / 删除 / 已计算 / 未计算），右侧分页
    filter_bar = ui.Frame(tree_frame)
    filter_bar.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
    filter_btns = {}
    for _key in CATS:
        _b = ui.Button(filter_bar, text=_key, font=FONTS["sm"],
                       relief=tk.FLAT, bd=0, padx=12, pady=2, cursor="hand2")
        _b.pack(side=tk.LEFT, padx=(0, 4))
        filter_btns[_key] = _b

    # 分页单独一行靠右：每页条数 + 首页/上页/页码/下页/末页
    page_box = ui.Frame(tree_frame)
    page_box.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 0))
    page_inner = ui.Frame(page_box)
    page_inner.pack(side=tk.RIGHT)
    ui.Label(page_inner, text="每页", foreground=COLOR["text_secondary"]).pack(side=tk.LEFT)
    var_page_size = tk.StringVar(value=str(DEFAULT_PAGE_SIZE))
    cmb_page_size = ttk.Combobox(page_inner, textvariable=var_page_size, width=6,
                                 values=PAGE_SIZE_CHOICES)
    cmb_page_size.pack(side=tk.LEFT, padx=(3, 10))
    btn_pg_first = ui.Button(page_inner, text="⏮", width=3)
    btn_pg_first.pack(side=tk.LEFT)
    btn_pg_prev = ui.Button(page_inner, text="<", width=3)
    btn_pg_prev.pack(side=tk.LEFT, padx=(2, 0))
    var_page = tk.StringVar(value="1")
    ent_page = ttk.Entry(page_inner, textvariable=var_page, width=6, justify="center")
    ent_page.pack(side=tk.LEFT, padx=(6, 4))
    lbl_pages = ui.Label(page_inner, text="/ 1 页 · 共 0 条", foreground=COLOR["text_secondary"])
    lbl_pages.pack(side=tk.LEFT)
    btn_pg_next = ui.Button(page_inner, text=">", width=3)
    btn_pg_next.pack(side=tk.LEFT, padx=(6, 0))
    btn_pg_last = ui.Button(page_inner, text="⏭", width=3)
    btn_pg_last.pack(side=tk.LEFT, padx=(2, 0))

    tree.tag_configure("missing", foreground=COLOR["error"])
    tree.tag_configure("new", foreground=COLOR["success"])
    tree.tag_configure("changed", foreground=COLOR["warning"])

    # ---------------- 日志（默认收起，把空间留给表格） ----------------
    log_wrap = ui.Frame(parent)
    log_wrap.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 6))
    log_wrap.grid_columnconfigure(0, weight=1)
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
    text_log = ui.Textbox(log_body, height=9, state="disabled",
                                         wrap=tk.WORD, font=("Consolas", 9))
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

    btn_log_toggle.config(command=toggle_log)
    log_body.grid_remove()

    # ============================================================
    # 目录 / 数据库信息
    # ============================================================

    def current_db_path():
        root = clean_path(var_root.get())
        if not root or not os.path.isdir(root):
            return ""
        db_root, _prefix = resolve_db_root(root, False)
        return os.path.join(db_root, DB_NAME)

    def refresh_managed():
        """刷新「扫描根目录」下拉里的已管理目录列表"""
        roots = load_roots()
        cmb_root.config(values=[r["path"] for r in roots])
        return roots

    def refresh_db_info():
        root = clean_path(var_root.get())
        if not root or not os.path.isdir(root):
            var_db.set("（先选择扫描根目录）")
            lbl_scope.config(text="")
            return
        db_root, prefix = resolve_db_root(root, False)
        db_path = os.path.join(db_root, DB_NAME)
        var_db.set(db_path)
        if os.path.isfile(db_path):
            st = db_counts(db_path)
            if st is None:
                lbl_scope.config(text="库读取失败（可能被占用或文件损坏）")
            else:
                total, active, deleted, nbytes, nohash = st
                lbl_scope.config(
                    text=f"库内 {total} 条 · 有效 {active} · 缺失 {deleted} · "
                         f"未计哈希 {nohash} · 共 {fmt_size(nbytes)}")
        else:
            lbl_scope.config(text="库不存在，扫描时自动新建")
        if prefix:
            log(f"提示：本级没有库，将沿用上层 {db_root} 的库，"
                f"相对路径前会加「{prefix}/」")

    # ============================================================
    # 表格渲染 / 检索
    # ============================================================

    def row_cat(row, pend):
        """一行的分类：差异行用 pending 里的 cat；库内记录只有 deleted 算「删除」"""
        if pend:
            return pend["cat"]
        return CAT_DEL if row[5] == "deleted" else ""

    def page_count(total=None):
        """总页数（每页 0 条 = 不分页，永远 1 页）"""
        if total is None:
            total = len(state["shown"])
        size = state["page_size"]
        if not size or size <= 0:
            return 1
        return max(1, (total + size - 1) // size)

    def goto_page(n):
        """跳到第 n 页（0 基，越界自动夹住）"""
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = state["page"]
        state["page"] = min(max(0, n), page_count() - 1)
        render()

    def render():
        """按当前筛选 + 分页重建表格（state["rows"] 保持全量不动）"""
        flt = state["filter"]
        all_rows, all_pend = state["rows"], state["pending"]
        if len(all_pend) != len(all_rows):
            all_pend = state["pending"] = [None] * len(all_rows)

        def match(r, p):
            if flt == "全部":
                return True
            if flt == CAT_HASHED:          # 按哈希状态筛（差异行也算）
                return bool(r[1])
            if flt == CAT_NOHASH:
                return not r[1]
            return row_cat(r, p) == flt

        shown = [(r, p) for r, p in zip(all_rows, all_pend) if match(r, p)]
        state["shown"] = shown

        # ---- 分页切片 ----
        total = len(shown)
        pages = page_count(total)
        page = min(max(0, state["page"]), pages - 1)
        state["page"] = page
        size = state["page_size"]
        start = 0 if not size or size <= 0 else page * size
        end = total if not size or size <= 0 else min(total, start + size)
        page_items = shown[start:end]
        state["page_rows"] = page_items

        tree.delete(*tree.get_children())
        # iid 用「全量筛选结果里的下标」，选中行反查 shown[i] 时不受分页影响
        for k, (row, pend) in enumerate(page_items):
            rel, sha, size_b, mtime, last, status, root = row
            status_raw = str(status or "")
            is_diff = status_raw not in ("active", "deleted")
            text = status_raw if is_diff else STATUS_TEXT.get(status_raw, status_raw)
            if pend:
                hashed = pend.get("hash_state") or "—"
            elif is_diff:
                hashed = "—"
            else:
                hashed = "已计算" if sha else "未计算"
            tag = DIFF_TAGS.get(status_raw.split(" ")[0]) if is_diff else None
            tree.insert("", tk.END, iid=f"r{start + k}",
                        values=("☐", short_path(root), rel,
                                fmt_size(size_b) if size_b is not None else "—",
                                fmt_time(mtime) if mtime else "—",
                                fmt_time(last) if last else "—",
                                hashed, short_sha(sha), text),
                        tags=(tag,) if tag else ())

        # 分类计数 + 高亮当前筛选
        counts = {k: 0 for k in CATS}
        counts["全部"] = len(all_rows)
        for r, p in zip(all_rows, all_pend):
            c = row_cat(r, p)
            if c in counts:
                counts[c] += 1
            counts[CAT_HASHED if r[1] else CAT_NOHASH] += 1
        for k, b in filter_btns.items():
            on = (k == flt)
            try:
                b.config(text=(f"{k} {counts[k]}" if all_rows else k),
                         bg=COLOR["accent"] if on else COLOR["surface_alt"],
                         fg=COLOR["text_on_accent"] if on else COLOR["text"],
                         activebackground=COLOR["accent_active"] if on else COLOR["border"],
                         activeforeground=COLOR["text_on_accent"] if on else COLOR["text"])
            except Exception:
                pass

        # 分页状态
        try:
            var_page.set(str(page + 1) if total else "")
            lbl_pages.config(text=f"/ {pages} 页 · 共 {total} 条")
            has_prev, has_next = page > 0, page < pages - 1
            btn_pg_first.config(state=tk.NORMAL if has_prev else tk.DISABLED)
            btn_pg_prev.config(state=tk.NORMAL if has_prev else tk.DISABLED)
            btn_pg_next.config(state=tk.NORMAL if has_next else tk.DISABLED)
            btn_pg_last.config(state=tk.NORMAL if has_next else tk.DISABLED)
        except Exception:
            pass

        lbl_hint.config(text="勾选差异行（可 Ctrl / Shift 多选）后点「写入数据库」"
                        if any(p for _r, p in shown) else "")
        for b in (btn_sel_all, btn_sel_none):
            b.config(state=tk.NORMAL if page_items else tk.DISABLED)
        update_sel()

    def set_filter(key):
        state["filter"] = key
        state["page"] = 0          # 换分类回到第一页
        render()

    def jump_page(_e=None):
        """页码框回车：跳到指定页"""
        raw = str(var_page.get()).strip()
        if not raw:
            render()
            return "break"
        try:
            goto_page(int(raw) - 1)
        except ValueError:
            render()
        return "break"

    def change_page_size(_e=None):
        """每页条数：数字或「全部」（不分页）"""
        raw = str(var_page_size.get()).strip()
        if raw in ("", "0", "全部", "不限"):
            size = 0
        else:
            try:
                size = int(raw)
            except ValueError:
                var_page_size.set("全部" if not state["page_size"]
                                  else str(state["page_size"]))
                return "break"
            size = max(1, size)
        state["page_size"] = size
        state["page"] = 0
        var_page_size.set("全部" if size <= 0 else str(size))
        render()
        return "break"

    def refresh_checks():
        """按当前 selection 刷新每行开头的复选框（只改内容变了的单元格）"""
        try:
            sel = set(tree.selection())
            for iid in tree.get_children():
                mark = "☑" if iid in sel else "☐"
                if tree.set(iid, "chk") != mark:
                    tree.set(iid, "chk", mark)
        except Exception:
            pass

    def on_tree_click(e):
        """点最左边的复选框列：切换该行勾选，不触发 Treeview 默认的选中行为"""
        try:
            if tree.identify("region", e.x, e.y) != "cell":
                return None
            if tree.identify_column(e.x) != "#1":
                return None
            iid = tree.identify_row(e.y)
            if not iid:
                return None
            if iid in tree.selection():
                tree.selection_remove(iid)
            else:
                tree.selection_add(iid)
            refresh_checks()
            update_sel()
            return "break"
        except Exception:
            return None

    def update_sel(_e=None):
        refresh_checks()
        n = len(tree.selection()) if tree.get_children() else 0
        lbl_sel.config(text=f"已选 {n} 条" if n else "")

    def select_all(flag=True):
        ids = tree.get_children()
        if not ids:
            return
        if flag:
            tree.selection_set(ids)
            try:
                tree.focus_set()
            except Exception:
                pass
        else:
            tree.selection_remove(*ids)
        update_sel()

    def fill_tree(rows, multi=False, pending=None):
        rows = [r if len(r) == 7 else tuple(r) + ("",) for r in rows]
        state["rows"] = rows
        state["pending"] = (list(pending) if pending is not None
                            and len(pending) == len(rows) else [None] * len(rows))
        state["page"] = 0          # 新数据从第一页开始
        if multi != state["multi"]:
            state["multi"] = multi
            tree.configure(displaycolumns=COLS_MULTI if multi else COLS_SINGLE)
            _fit_columns()
        render()

    def db_conn_readonly():
        dbp = current_db_path()
        if not dbp or not os.path.isfile(dbp):
            return None, "数据库还不存在，请先做一次扫描"
        db = HashDB(dbp)
        try:
            db.open(readonly=True)
        except Exception:
            try:
                db.open()          # WAL 库可能读不了只读，退回读写
            except Exception as e:
                return None, f"打开数据库失败：{e}"
        return db, ""

    def managed_dbs():
        """所有已管理目录里真实存在的库 [(root, db_path)]，同一个库只取一次"""
        out, seen = [], set()
        cur = clean_path(var_root.get())
        if cur and os.path.isdir(cur):
            dbp = current_db_path()
            if dbp and os.path.isfile(dbp):
                key = os.path.normcase(os.path.abspath(dbp))
                seen.add(key)
                out.append((os.path.abspath(cur), dbp))
        for r in load_roots():
            dbp = r.get("db") or os.path.join(r["path"], DB_NAME)
            key = os.path.normcase(os.path.abspath(dbp))
            if os.path.isfile(dbp) and key not in seen:
                seen.add(key)
                out.append((r["path"], dbp))
        return out

    def query_one(db_path, kw, kind, inc, limit):
        db = HashDB(db_path)
        try:
            try:
                db.open(readonly=True)
            except Exception:
                db.open()
            if not kw:
                rows = db.list_all(limit, include_deleted=inc)
                total = db.count_all(include_deleted=inc)
            elif kind.startswith("按路径"):
                rows = db.search_path(kw, limit=limit, include_deleted=inc)
                total = db.count_path(kw, include_deleted=inc)
            else:
                rows = db.search_hash(kw, limit=limit)
                total = db.count_hash(kw)
            return [tuple(r) for r in rows], total
        except Exception as e:
            log(f"查询 {db_path} 失败：{e}")
            return [], 0
        finally:
            db.close()

    def do_search():
        kw = var_kw.get().strip()
        kind = var_kind.get()
        inc = var_inc_deleted.get()
        multi = var_scope.get().startswith("全部")
        state["filter"] = "全部"

        if not multi:
            db, err = db_conn_readonly()
            if db is None:
                messagebox.showwarning("提示", err)
                return
            db.close()
            rows, total = query_one(current_db_path(), kw, kind, inc, SEARCH_LIMIT)
            fill_tree(rows, multi=False)
            label = "全部记录" if not kw else f"搜索「{kw}」"
        else:
            targets = managed_dbs()
            if not targets:
                messagebox.showwarning(
                    "提示", "还没有可搜的库。先扫描几个目录，它们会自动记进「已管理目录」。")
                return
            rows, total = [], 0
            for root_path, dbp in targets:
                part, t = query_one(dbp, kw, kind, inc, PER_DB_LIMIT)
                total += t
                rows.extend([tuple(r) + (root_path,) for r in part])
                if len(rows) >= SEARCH_LIMIT:
                    break
            rows.sort(key=lambda r: (r[6].lower(), r[0].lower()))
            rows = rows[:SEARCH_LIMIT]
            fill_tree(rows, multi=True)
            label = ("全部记录" if not kw else f"搜索「{kw}」") + \
                    f"（{len(targets)} 个库）"

        if total > len(rows):
            lbl_count.config(text=f"命中 {total} 条，显示前 {len(rows)} 条")
            log(f"{label}命中 {total} 条，表格只显示前 {len(rows)} 条")
        else:
            lbl_count.config(text=f"命中 {total} 条")
            log(f"{label}命中 {total} 条")

    def fill_hashes():
        """只给「有效但没算哈希」的记录补 SHA256（只写哈希值）"""
        if state["running"]:
            messagebox.showinfo("提示", "当前有任务在跑，等它结束再补哈希")
            return
        root = clean_path(var_root.get())
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "请先选择一个存在的扫描根目录")
            return
        db_root, _prefix = resolve_db_root(root, False)
        dbp = os.path.join(db_root, DB_NAME)
        if not os.path.isfile(dbp):
            messagebox.showinfo("提示", "当前目录还没有数据库，先扫描一次再来补哈希")
            return
        n, nbytes = count_no_hash(dbp)
        if not n:
            messagebox.showinfo("提示", "这个库里所有有效记录都已经有哈希了")
            return
        if not messagebox.askyesno(
                "补全哈希",
                f"有 {n} 条记录还没有哈希（合计 {fmt_size(nbytes)}）。\n\n"
                f"· 只写 sha256（和校验时间），大小 / 修改时间 / 状态一概不动\n"
                f"· 文件已经不在了的自动跳过，不会误标缺失\n"
                f"· 每算完一条立刻入库，中途停止也不丢\n\n"
                f"现在开始补算？"):
            return
        lock = DbLock(dbp)
        ok, msg = lock.acquire()
        if not ok:
            messagebox.showwarning("数据库被占用", msg + f"\n\n{dbp}")
            return
        state["lock"] = lock
        state["task"] = "hash"
        state["stop"] = threading.Event()
        fr = HashFiller(db_root, dbp, log, state["stop"])
        state["scanner"] = fr
        state["running"] = True
        btn_start.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
        bar_all["value"] = 0
        bar_cur["value"] = 0
        lbl_prog.config(text=f"准备补全哈希（{n} 条）…")

        def worker():
            try:
                fr.run()
            finally:
                fr.ui["done_flag"] = True

        threading.Thread(target=worker, daemon=True).start()
        parent.after(200, poll_progress)

    def seed_from_parent():
        """用最靠近的上级库里「属于本目录」的记录，生成本目录的 filehash.db"""
        if state["running"]:
            messagebox.showinfo("提示", "当前有任务在跑，等它结束再操作")
            return
        root = clean_path(var_root.get())
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "请先选择一个存在的扫描根目录")
            return
        root = os.path.abspath(root)
        own_db = os.path.join(root, DB_NAME)

        found = find_parent_db(root)
        if not found:
            messagebox.showinfo(
                "没有找到上级数据库",
                f"从「{root}」逐级往上一直到盘符根，都没有找到 {DB_NAME}。\n\n"
                f"先扫描一次上级目录（快速建档即可），再回来用这个功能。")
            return
        parent_root, parent_db = found
        prefix = scope_prefix(root, parent_root)
        n, nbytes = count_scope(parent_db, prefix)
        if n < 0:
            messagebox.showwarning(
                "打不开上级库", f"{parent_db}\n\n可能正在被占用或文件损坏。")
            return
        if n == 0:
            messagebox.showinfo(
                "上级库里没有这个目录的记录",
                f"上级库：{parent_db}\n范围：{prefix}/\n\n"
                f"里面没有属于「{root}」的记录。\n"
                f"可能是上级扫描的时候这个目录还不存在，先补扫一次上级目录。")
            return

        exists = os.path.isfile(own_db)
        msg = (f"上级库：{parent_db}\n"
               f"范围：{prefix}/　共 {n} 条（{fmt_size(nbytes)}）\n"
               f"目标库：{own_db}\n\n")
        msg += ("当前目录已有数据库，会把上级的记录合并进来"
                "（同路径保留较新的那条）。\n\n" if exists
                else "当前目录还没有数据库，将新建一个。\n\n")
        msg += ("· 直接沿用上级库的哈希 / 大小 / 修改时间\n"
                "· **完全不读取、不检查任何文件**，只搬数据库记录\n"
                "· 生成后可以直接跑「快速预检」，只比大小和时间，几乎不用读盘")
        if not messagebox.askyesno("用上级库生成", msg):
            return

        lock = DbLock(own_db)
        ok, lmsg = lock.acquire()
        if not ok:
            messagebox.showwarning("数据库被占用", lmsg + f"\n\n{own_db}")
            return
        state["lock"] = lock
        state["task"] = "seed"
        state["stop"] = threading.Event()
        task = SeedTask(parent_root, parent_db, root, own_db, prefix,
                        log, state["stop"])
        state["scanner"] = task
        state["running"] = True
        btn_start.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
        bar_all["value"] = 0
        bar_cur["value"] = 0
        lbl_prog.config(text=f"准备从上级库搬入 {n} 条…")

        def worker():
            try:
                task.run()
            finally:
                task.ui["done_flag"] = True

        threading.Thread(target=worker, daemon=True).start()
        parent.after(200, poll_progress)

    def finish_seed():
        """用上级库生成 / 合并的收尾：刷新库概况、列表和「已管理目录」"""
        task = state["scanner"]
        state["running"] = False
        state["task"] = ""
        if state["anim"]:
            bar_all.stop()
            bar_all.config(mode="determinate")
            bar_all["value"] = 0
            state["anim"] = False
        btn_start.config(state=tk.NORMAL)
        btn_stop.config(state=tk.DISABLED)
        if state["lock"] is not None:
            state["lock"].release()
            state["lock"] = None
        bar_cur["value"] = 0
        s = task.stats if task else {}
        errored = bool(task and task.ui.get("error"))
        if errored:
            lbl_prog.config(text="用上级库生成出错，详见日志")
        else:
            bar_all["value"] = 100
            tail = f"，其中库内标缺失 {s['missing']} 条" if s.get("missing") else ""
            lbl_prog.config(
                text=f"已生成 —— 从上级库搬入 {s.get('rows', 0)} 条"
                     f"（新增 {s.get('inserted', 0)}，更新 {s.get('updated', 0)}）"
                     f"{tail}，未读取任何文件")
        # 记进「已管理目录」，模式标「继承上级」
        if not errored and s.get("rows"):
            try:
                d = db_counts(os.path.join(clean_path(var_root.get()), DB_NAME)) \
                    or (0, 0, 0, 0, 0)
                roots = upsert_root(
                    load_roots(), clean_path(var_root.get()),
                    db=os.path.join(clean_path(var_root.get()), DB_NAME),
                    last_scan=time.time(), mode="seed",
                    files=d[0], active=d[1], deleted=d[2], nohash=d[4])
                save_roots(roots)
            except Exception:
                pass
        refresh_managed()
        refresh_db_info()
        if not errored:
            do_search()
        state["scanner"] = None

    def merge_dbs():
        """把当前目录下各级子文件夹的 filehash.db 合并进当前库（只动数据库）"""
        if state["running"]:
            messagebox.showinfo("提示", "当前有任务在跑，等它结束再合并")
            return
        root = clean_path(var_root.get())
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "请先选择一个存在的扫描根目录")
            return
        db_root, _prefix = resolve_db_root(root, False)
        dbp = os.path.join(db_root, DB_NAME)
        subs = find_sub_dbs(db_root, skip=dbp)
        if not subs:
            messagebox.showinfo(
                "提示", f"「{db_root}」下面没有子文件夹的 {DB_NAME}，不需要合并")
            return
        lines = []
        for rel_dir, sp in subs[:10]:
            n = db_row_count(sp)
            lines.append(f"  · {rel_dir}  （{'读不出' if n < 0 else str(n) + ' 条'}）")
        if len(subs) > 10:
            lines.append(f"  · …… 另有 {len(subs) - 10} 个")
        if not messagebox.askyesno(
                "合并数据库",
                f"把「{db_root}」下 {len(subs)} 个子库合并进来？\n\n"
                + "\n".join(lines)
                + f"\n\n目标库：{dbp}\n"
                  f"· 记录路径加子目录前缀；重复的保留较新的那条\n"
                  f"· 合并成功后删除子库文件（连同 -wal / -shm）\n"
                  f"· 只操作数据库，不读取、不修改任何普通文件"):
            return
        lock = DbLock(dbp)
        ok, msg = lock.acquire()
        if not ok:
            messagebox.showwarning("数据库被占用", msg + f"\n\n{dbp}")
            return
        state["lock"] = lock
        state["task"] = "merge"
        state["stop"] = threading.Event()
        mt = MergeTask(db_root, dbp, log, state["stop"])
        state["scanner"] = mt
        state["running"] = True
        btn_start.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
        bar_all["value"] = 0
        bar_cur["value"] = 0
        lbl_prog.config(text=f"准备合并 {len(subs)} 个子库…")

        def worker():
            try:
                mt.run()
            finally:
                mt.ui["done_flag"] = True

        threading.Thread(target=worker, daemon=True).start()
        parent.after(200, poll_progress)

    def view_db():
        """把当前数据库里的记录直接渲染到列表（相当于空条件查全部）"""
        dbp = current_db_path()
        if not dbp or not os.path.isfile(dbp):
            messagebox.showinfo(
                "提示", "当前目录还没有数据库，\n先扫描一次（快速建档即可）再来查看。")
            return
        var_kw.set("")
        var_scope.set("当前目录")
        do_search()
        log(f"查看数据库：{dbp}")

    def show_deleted(prefix, root):
        db, err = db_conn_readonly()
        if db is None:
            return
        state["filter"] = "全部"
        try:
            rows = [tuple(r) + (root,) for r in db.list_deleted(prefix)]
        except Exception as e:
            log(f"读取缺失清单失败：{e}")
            db.close()
            return
        db.close()
        fill_tree(rows)
        lbl_count.config(text=f"缺失 {len(rows)} 条")
        log(f"本次标记缺失 {len(rows)} 条（库内状态 deleted，"
            f"勾选「含缺失」可重新搜出来）")

    def absolute_of(rel, root=None):
        """表格里的 rel 是相对扫描根的路径 → 还原成绝对路径"""
        base = root or clean_path(var_root.get())
        if not base:
            return ""
        return os.path.join(os.path.abspath(base), *rel.split("/"))

    def selected_rows():
        """选中行的原始数据 (rel, sha, size, mtime, last, status, root)。
        取的是数据本身而不是表格显示值——列顺序调整、哈希截断都不影响复制/定位。"""
        out, shown = [], state["shown"]
        for iid in tree.selection():
            try:
                i = int(str(iid)[1:])
            except ValueError:
                continue
            if 0 <= i < len(shown):
                out.append(shown[i][0])
        return out

    def on_open_folder():
        rows = selected_rows()
        if rows:
            reveal_in_explorer(absolute_of(rows[0][0], rows[0][6]))

    def on_copy_hash():
        rows = [r for r in selected_rows() if r[1]]
        if not rows:
            messagebox.showinfo("提示", "选中的行没有哈希值（快速建档的记录还没算哈希）")
            return
        parent.clipboard_clear()
        parent.clipboard_append("\n".join(r[1] for r in rows))
        log(f"已复制 {len(rows)} 个完整哈希到剪贴板")

    def on_copy_path():
        rows = selected_rows()
        if not rows:
            return
        parent.clipboard_clear()
        parent.clipboard_append("\n".join(absolute_of(r[0], r[6]) for r in rows))
        log(f"已复制 {len(rows)} 个绝对路径到剪贴板")

    def on_copy_rel():
        rows = selected_rows()
        if not rows:
            return
        parent.clipboard_clear()
        parent.clipboard_append("\n".join(r[0] for r in rows))

    def rows_to_items(rows):
        """选中行 → [(db_path, rel, abs_path, base)]；定不到库的计入 skipped。
        差异行（更新 / 新增 / 缺失）也照常交给校验：库里有旧记录就和旧哈希比，
        没有就标「库里没有」；文件不在了标「文件不存在」。"""
        items, skipped = [], 0
        for rel, _sha, _size, _mtime, _last, status, root in rows:
            if root:
                dbp, base = "", os.path.abspath(root)
                for r, d in managed_dbs():
                    if os.path.normcase(os.path.abspath(r)) == \
                            os.path.normcase(os.path.abspath(root)):
                        dbp = d
                        break
                if not dbp:
                    dbp = os.path.join(base, DB_NAME)
            else:
                dbp, base = current_db_path(), clean_path(var_root.get())
                if not dbp or not base:
                    skipped += 1
                    continue
                base = os.path.abspath(base)
            items.append((dbp, rel, os.path.join(base, *rel.split("/")), base))
        return items, skipped

    def verify_selected():
        """只读校验选中文件的哈希：重算一遍和库里比，**不改数据库**"""
        if state["running"]:
            messagebox.showinfo("提示", "当前有任务在跑，等它结束再校验")
            return
        rows = selected_rows()
        if not rows:
            messagebox.showinfo("提示", "先在列表里选中要校验的文件（可 Ctrl / Shift 多选）")
            return
        items, skipped = rows_to_items(rows)
        if not items:
            messagebox.showinfo(
                "提示", "选中的行定不到对应的数据库，没法校验。")
            return
        if not messagebox.askyesno(
                "校验哈希",
                f"重新读取选中的 {len(items)} 个文件，算 SHA256 和库里的值比对？\n\n"
                f"· **只读校验，不会改动数据库**\n"
                f"· 差异行也能校验：库里「更新」过的行会和旧哈希比，\n"
                f"  一致说明内容没变（只是日期动了），可随后统一修改时间\n"
                f"· 结果单独列出来（一致 / 已变化 / 文件不存在 / 无库内哈希 / 库里没有）\n"
                f"· 文件大或数量多会比较慢，可以随时停止"
                + (f"\n· 另有 {skipped} 条定不到库，不参与" if skipped else "")):
            return
        state["task"] = "verify"
        state["stop"] = threading.Event()
        task = VerifyTask(items, log, state["stop"])
        state["scanner"] = task
        state["running"] = True
        btn_start.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
        bar_all["value"] = 0
        bar_cur["value"] = 0
        lbl_prog.config(text=f"准备校验 {len(items)} 条…")

        def worker():
            try:
                task.run()
            finally:
                task.ui["done_flag"] = True

        threading.Thread(target=worker, daemon=True).start()
        parent.after(200, poll_progress)

    def rehash_selected():
        """把选中的记录重新读文件算哈希，算完直接写回数据库"""
        if state["running"]:
            messagebox.showinfo("提示", "当前有任务在跑，等它结束再重算")
            return
        rows = selected_rows()
        if not rows:
            messagebox.showinfo("提示", "先在列表里选中要重算的文件")
            return
        items, diff_rows = rows_to_items(rows)
        if not items:
            messagebox.showinfo(
                "提示", "选中的行定不到对应的数据库，没法重算。")
            return
        if not messagebox.askyesno(
                "重新计算哈希",
                f"重新读取选中的 {len(items)} 个文件，计算 SHA256 并直接写回数据库？\n\n"
                f"· 只更新 sha256 和校验时间，大小 / 修改时间 / 状态不动\n"
                f"· 文件已经不在了的会自动跳过\n"
                + (f"· 另有 {diff_rows} 条定不到库，不参与\n" if diff_rows else "")):
            return
        state["task"] = "rehash"
        state["stop"] = threading.Event()
        task = RehashTask(items, log, state["stop"])
        state["scanner"] = task
        state["running"] = True
        btn_start.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
        bar_all["value"] = 0
        bar_cur["value"] = 0
        lbl_prog.config(text=f"准备重算 {len(items)} 条…")

        def worker():
            try:
                task.run()
            finally:
                task.ui["done_flag"] = True

        threading.Thread(target=worker, daemon=True).start()
        parent.after(200, poll_progress)

    def apply_selected():
        """把表格里选中的差异行写回数据库（只读模式扫描的产物）"""
        shown = state["shown"]
        picked = []
        for iid in tree.selection():
            try:
                i = int(str(iid)[1:])
            except ValueError:
                continue
            if 0 <= i < len(shown) and shown[i][1]:
                picked.append(shown[i][1])
        if not picked:
            avail = [p for _r, p in state["page_rows"] if p]
            if not avail:
                messagebox.showinfo(
                    "提示",
                    "当前列表没有可写回的差异行。\n\n"
                    "先用【快速预检】或【完整检查】扫描根目录，扫出来的差异清单\n"
                    "勾选（可 Ctrl / Shift 多选，或点「全选」）后再点这个按钮，\n"
                    "程序只把选中的行写进数据库，其余记录一律不动。")
                return
            if not messagebox.askyesno(
                    "没有选中行",
                    f"当前没有选中任何行。\n\n"
                    f"要把这一页的 {len(avail)} 条差异全部写入数据库吗？\n"
                    f"（只想写一部分的话，先勾选再点按钮）"):
                return
            picked = avail
        counts = {CAT_NEW: 0, CAT_UPD: 0, CAT_DEL: 0}
        for p in picked:
            counts[p["cat"]] = counts.get(p["cat"], 0) + 1
        db_path = picked[0]["db"]
        if not messagebox.askyesno(
                "写入数据库",
                f"把选中的 {len(picked)} 条写回数据库？\n\n"
                f"新增 {counts[CAT_NEW]} 条，更新 {counts[CAT_UPD]} 条，"
                f"标记删除 {counts[CAT_DEL]} 条\n\n{db_path}"):
            return
        lock = DbLock(db_path)
        ok, msg = lock.acquire()
        if not ok:
            messagebox.showwarning("数据库被占用", msg + f"\n\n{db_path}")
            return
        done, failed = 0, 0
        try:
            try:
                db = HashDB(db_path)
                db.open()
            except Exception as e:
                messagebox.showerror("写入失败", f"打不开数据库：{e}")
                return
            try:
                db.conn.execute("BEGIN")
                for p in picked:
                    try:
                        write_pending(db.conn, p)
                        done += 1
                    except sqlite3.Error as e:
                        failed += 1
                        log(f"[写入失败] {p['rel']}：{e}")
                db.conn.execute("COMMIT")
            except sqlite3.Error as e:
                try:
                    db.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                messagebox.showerror("写入失败", f"已回滚，数据库没有改动：\n{e}")
                return
            finally:
                db.close()
        finally:
            lock.release()

        log(f"写入数据库：成功 {done} 条"
            + (f"，失败 {failed} 条" if failed else "") + f" → {db_path}")
        # 已写回的行从差异清单里移除，剩下的继续处理
        written = {id(p) for p in picked}
        keep_rows, keep_pend = [], []
        for r, p in zip(state["rows"], state["pending"]):
            if p is not None and id(p) in written:
                continue
            keep_rows.append(r)
            keep_pend.append(p)
        state["rows"], state["pending"] = keep_rows, keep_pend
        left = sum(1 for p in keep_pend if p)
        if left == 0 or not any(row_cat(r, p) == state["filter"]
                                for r, p in zip(keep_rows, keep_pend)):
            state["filter"] = "全部"          # 当前分类已经处理完，回到「全部」
        render()
        lbl_count.config(text=f"剩余差异 {left} 条" if left else "差异已全部处理")
        refresh_db_info()
        messagebox.showinfo(
            "完成",
            f"已写入 {done} 条" + (f"，失败 {failed} 条（详见运行日志）" if failed else "")
            + ("。\n写过的行已从清单里移除。" if left else "。\n差异清单已清空。"))

    menu = tk.Menu(parent, tearoff=0)
    menu.add_command(label="校验哈希（只读）", command=verify_selected)   # 0
    menu.add_command(label="重新计算哈希", command=rehash_selected)      # 1
    menu.add_separator()                                                # 2
    menu.add_command(label="写入数据库（选中的差异行）", command=apply_selected)
    menu.add_command(label="打开所在文件夹", command=on_open_folder)
    menu.add_separator()
    menu.add_command(label="复制哈希", command=on_copy_hash)
    menu.add_command(label="复制完整绝对路径", command=on_copy_path)
    menu.add_command(label="复制相对路径", command=on_copy_rel)

    def on_right_click(e):
        iid = tree.identify_row(e.y)
        if iid and iid not in tree.selection():
            tree.selection_set(iid)
        if not tree.selection():
            return
        try:
            n = len(tree.selection())
            menu.entryconfigure(0, label=f"校验哈希（{n} 条，只读）")
            menu.entryconfigure(1, label=f"重新计算哈希（{n} 条）")
        except Exception:
            pass
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    tree.bind("<Button-3>", on_right_click)
    tree.bind("<Double-1>", lambda _e: on_open_folder())

    # ---------------- 导出 CSV ----------------
    def export_csv():
        rows = [r for r, _p in state["shown"]]
        if not rows:
            messagebox.showinfo("提示", "表格是空的，先扫描或搜索再导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出当前表格为 CSV", defaultextension=".csv",
            initialfile="hash_" + time.strftime("%Y%m%d_%H%M%S") + ".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["相对路径", "SHA256", "哈希状态", "文件大小(字节)",
                            "修改时间", "上次哈希校验", "状态", "分类",
                            "扫描根", "绝对路径"])
                for (rel, sha, size, mtime, last, status, root), pend in state["shown"]:
                    is_diff = str(status) not in ("active", "deleted")
                    w.writerow([
                        rel, sha or "",
                        (pend.get("hash_state") if pend
                         else ("—" if is_diff else ("已计算" if sha else "未计算"))),
                        "" if size is None else size,
                        fmt_time(mtime) if mtime else "",
                        fmt_time(last) if last else "",
                        str(status) if is_diff else STATUS_TEXT.get(str(status), status),
                        row_cat((rel, sha, size, mtime, last, status, root), pend),
                        root or clean_path(var_root.get()),
                        absolute_of(rel, root)])
            log(f"已导出 {len(rows)} 行（筛选：{state['filter']}，含全部页）→ {path}")
            messagebox.showinfo(
                "完成",
                f"已导出 {len(rows)} 行到：\n{path}\n\n"
                f"（导出的是当前筛选下的全部记录，不受分页限制）")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    # ============================================================
    # 扫描
    # ============================================================

    def on_stop():
        if state["running"]:
            state["stop"].set()
            log("正在停止…（当前文件读完即退出，本次不做缺失标记）")

    def poll_progress():
        sc = state["scanner"]
        if not state["running"] or sc is None:
            return
        ui = sc.ui
        total = ui.get("total") or 0
        done = ui.get("done") or 0
        phase = ui.get("phase", "")
        # 枚举阶段还不知道总量：总进度条走「忙碌动画」，别让用户以为卡在 0%
        if phase.startswith("枚举") or (done == 0 and not total):
            if not state["anim"]:
                bar_all.config(mode="indeterminate")
                bar_all.start(14)
                state["anim"] = True
        elif state["anim"]:
            bar_all.stop()
            bar_all.config(mode="determinate")
            bar_all["value"] = 0
            state["anim"] = False
        if not state["anim"]:
            bar_all["value"] = (done / total * 100) if total else 0
        cur_total = ui.get("cur_total") or 0
        cur_done = ui.get("cur_done") or 0
        bar_cur["value"] = (cur_done / cur_total * 100) if cur_total else 0
        name = ui.get("cur_name") or ""
        if len(name) > 55:
            name = "…" + name[-53:]
        lbl_prog.config(
            text=f"{phase} {done}/{total}　{name}"
                 + (f"　{fmt_size(cur_done)}/{fmt_size(cur_total)}" if cur_total else ""))
        if ui.get("done_flag"):
            task = state.get("task")
            if task == "hash":
                finish_fill()
            elif task == "merge":
                finish_merge()
            elif task == "seed":
                finish_seed()
            elif task == "verify":
                finish_verify()
            elif task == "rehash":
                finish_rehash()
            else:
                finish_scan()
            return
        parent.after(200, poll_progress)

    def finish_scan():
        sc = state["scanner"]
        state["running"] = False
        if state["anim"]:
            bar_all.stop()
            bar_all.config(mode="determinate")
            bar_all["value"] = 0
            state["anim"] = False
        btn_start.config(state=tk.NORMAL)
        btn_stop.config(state=tk.DISABLED)
        if state["lock"] is not None:
            state["lock"].release()
            state["lock"] = None
        bar_cur["value"] = 0
        stats = sc.stats if sc else {}
        mode = sc.mode if sc else MODE_FULL
        errored = bool(sc and sc.ui.get("error"))
        aborted = bool(sc and sc.ui.get("aborted"))
        if errored:
            lbl_prog.config(text="扫描出错，详见日志")
        elif aborted:
            bar_all["value"] = 0
            lbl_prog.config(text=f"已停止 —— 新增 {stats.get('new', 0)}，"
                                 f"变更 {stats.get('changed', 0)}，"
                                 f"未变 {stats.get('same', 0)}")
        else:
            bar_all["value"] = 100
            extra = f"，补哈希 {stats.get('filled', 0)}" if stats.get("filled") else ""
            updated = (stats.get("changed", 0) + stats.get("restored", 0)
                       + stats.get("filled", 0))
            text = (f"{MODE_NAMES.get(mode, '')}完成 —— "
                    f"新增 {stats.get('new', 0)}，更新 {updated}，"
                    f"未变 {stats.get('same', 0)}，删除 {stats.get('missing', 0)}")
            if sc is not None and not sc.writable:
                text += "（只读，未写库）"
            elif mode == MODE_QUICK:
                text += "（未计算哈希）"
            elif stats.get("nohash"):
                text += f"，未计哈希 {stats['nohash']}"
            lbl_prog.config(text=text + extra)

        # 记录到「已管理目录」配置；被并入的子库条目去掉
        if sc is not None and not errored:
            try:
                roots = load_roots()
                dstat = db_counts(sc.db_path) or (0, 0, 0, 0, 0)
                upsert_root(roots, sc.root, db=sc.db_path,
                            last_scan=time.time(), mode=mode,
                            files=dstat[0], active=dstat[1],
                            deleted=dstat[2], nohash=dstat[4])
                save_roots(roots)
                refresh_managed()
            except Exception:
                pass

        # 只读模式：表格直接显示差异清单（可多选写回）；建档有缺失时优先摆出缺失清单
        refresh_db_info()
        state["filter"] = "全部"
        if mode in (MODE_PRECHECK, MODE_VERIFY) and sc is not None:
            rows = sc.ui.get("rows", [])
            pends = sc.ui.get("pending", [])
            if len(pends) != len(rows):
                pends = [None] * len(rows)
            pairs = list(zip(rows, pends))
            order = {CAT_NEW: 0, CAT_UPD: 1, CAT_DEL: 2}
            pairs.sort(key=lambda x: (order.get(x[1]["cat"], 9) if x[1] else 9,
                                      str(x[0][0]).lower()))
            fill_tree([p[0] for p in pairs], pending=[p[1] for p in pairs])
            lbl_count.config(
                text=f"{MODE_NAMES.get(mode, '')}差异 {len(pairs)} 条（未写库）")
        elif sc is not None and stats.get("missing"):
            show_deleted(sc.prefix, sc.root)
        elif not aborted:
            do_search()
        state["scanner"] = None

    def finish_fill():
        """补全哈希收尾（不走扫描的 finish_scan，那个会写目录配置）"""
        fr = state["scanner"]
        state["running"] = False
        state["task"] = ""
        if state["anim"]:
            bar_all.stop()
            bar_all.config(mode="determinate")
            bar_all["value"] = 0
            state["anim"] = False
        btn_start.config(state=tk.NORMAL)
        btn_stop.config(state=tk.DISABLED)
        if state["lock"] is not None:
            state["lock"].release()
            state["lock"] = None
        bar_cur["value"] = 0
        s = fr.stats if fr else {}
        errored = bool(fr and fr.ui.get("error"))
        aborted = bool(fr and fr.ui.get("aborted"))
        if errored:
            lbl_prog.config(text="补全哈希出错，详见日志")
        elif aborted:
            bar_all["value"] = 0
            lbl_prog.config(text=f"已停止 —— 已补 {s.get('filled', 0)} 条"
                                 f"（都已入库），跳过 {s.get('skipped', 0)}")
        else:
            bar_all["value"] = 100
            lbl_prog.config(text=f"补全哈希完成 —— 已补 {s.get('filled', 0)}，"
                                 f"跳过(文件不在) {s.get('skipped', 0)}，"
                                 f"失败 {s.get('errors', 0)}")
        refresh_db_info()
        if not errored:
            do_search()          # 刷新列表里的哈希列
        state["scanner"] = None

    def finish_verify():
        """校验哈希收尾：只读，不改库、不刷新列表（列表数据没变）"""
        task = state["scanner"]
        state["running"] = False
        state["task"] = ""
        if state["anim"]:
            bar_all.stop()
            bar_all.config(mode="determinate")
            bar_all["value"] = 0
            state["anim"] = False
        btn_start.config(state=tk.NORMAL)
        btn_stop.config(state=tk.DISABLED)
        bar_cur["value"] = 0
        s = task.stats if task else {}
        errored = bool(task and task.ui.get("error"))
        aborted = bool(task and task.ui.get("aborted"))
        if errored:
            lbl_prog.config(text="校验哈希出错，详见日志")
        else:
            bar_all["value"] = 0 if aborted else 100
            extra = ""
            if s.get("gone"):
                extra += f"，文件不存在 {s['gone']}"
            if s.get("nohash"):
                extra += f"，无库内哈希 {s['nohash']}"
            if s.get("missing"):
                extra += f"，库里没有 {s['missing']}"
            if s.get("errors"):
                extra += f"，读取失败 {s['errors']}"
            lbl_prog.config(
                text=f"{'已停止' if aborted else '校验完成'}（未改库）—— "
                     f"共 {len(task.results) if task else 0} 条："
                     f"一致 {s.get('same', 0)}，"
                     f"已变化 {s.get('changed', 0)}{extra}")
        if not errored and task and task.results:
            parent.after(150, lambda: open_verify_result(task.results))
        state["scanner"] = None

    def finish_rehash():
        """重算哈希收尾（锁在任务内部按库获取/释放）"""
        tk_task = state["scanner"]
        state["running"] = False
        state["task"] = ""
        if state["anim"]:
            bar_all.stop()
            bar_all.config(mode="determinate")
            bar_all["value"] = 0
            state["anim"] = False
        btn_start.config(state=tk.NORMAL)
        btn_stop.config(state=tk.DISABLED)
        bar_cur["value"] = 0
        s = tk_task.stats if tk_task else {}
        errored = bool(tk_task and tk_task.ui.get("error"))
        aborted = bool(tk_task and tk_task.ui.get("aborted"))
        if errored:
            lbl_prog.config(text="重算哈希出错，详见日志")
        else:
            bar_all["value"] = 0 if aborted else 100
            lbl_prog.config(
                text=f"{'已停止' if aborted else '重算完成'} —— "
                     f"成功 {s.get('done', 0)} 条"
                     f"（其中 {s.get('changed', 0)} 条哈希有变化），"
                     f"跳过(文件不在) {s.get('skipped', 0)}，"
                     f"失败 {s.get('errors', 0)}")
        refresh_db_info()
        if not errored:
            do_search()
        state["scanner"] = None

    def finish_merge():
        """合并数据库收尾"""
        mt = state["scanner"]
        state["running"] = False
        state["task"] = ""
        if state["anim"]:
            bar_all.stop()
            bar_all.config(mode="determinate")
            bar_all["value"] = 0
            state["anim"] = False
        btn_start.config(state=tk.NORMAL)
        btn_stop.config(state=tk.DISABLED)
        if state["lock"] is not None:
            state["lock"].release()
            state["lock"] = None
        bar_cur["value"] = 0
        s = mt.stats if mt else {}
        errored = bool(mt and mt.ui.get("error"))
        aborted = bool(mt and mt.ui.get("aborted"))
        if errored:
            lbl_prog.config(text="合并出错，详见日志")
        else:
            bar_all["value"] = 0 if aborted else 100
            lbl_prog.config(
                text=f"{'已停止' if aborted else '合并完成'} —— "
                     f"{s.get('dbs', 0)} 个子库，共 {s.get('rows', 0)} 条"
                     f"（新增 {s.get('inserted', 0)}，更新 {s.get('updated', 0)}），"
                     f"已删子库 {s.get('removed', 0)} 个"
                     + (f"，失败 {s.get('errors', 0)} 个" if s.get("errors") else ""))
        # 子库文件已删 → 顺手把「已管理目录」里对应的条目去掉
        if s.get("removed_paths"):
            try:
                roots = drop_roots_by_db(load_roots(), s["removed_paths"])
                save_roots(roots)
            except Exception:
                pass
        refresh_managed()
        refresh_db_info()
        if not errored:
            do_search()
        state["scanner"] = None

    def start():
        root = clean_path(var_root.get())
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "请先选择一个存在的扫描根目录（盘符或文件夹）")
            return
        root = os.path.abspath(root)
        mode = var_mode.get()
        db_root, prefix = resolve_db_root(root, False)
        db_path = os.path.join(db_root, DB_NAME)

        ok, msg = check_db_file(db_path)
        if not ok:
            if not messagebox.askyesno(
                    "数据库可能损坏",
                    f"{db_path}\n\nSQLite 检查结果：{msg}\n\n"
                    f"是否把它改名备份为 filehash.db.broken-时间戳 并新建空库继续？\n"
                    f"（只改名不删除）"):
                return
            try:
                backup = db_path + ".broken-" + time.strftime("%Y%m%d-%H%M%S")
                os.rename(db_path, backup)
                log(f"已备份损坏库 → {backup}")
            except Exception as e:
                messagebox.showerror("无法备份", f"改名失败：{e}")
                return

        lock = DbLock(db_path)
        ok2, msg2 = lock.acquire()
        if not ok2:
            messagebox.showwarning("数据库被占用", msg2 + f"\n\n{db_path}")
            return
        state["lock"] = lock

        state["stop"] = threading.Event()
        # 不写 scan_log.txt（日志只在界面里看）；子库固定自动并入
        sc = Scanner(root, db_path, prefix, mode, log, "", state["stop"])
        state["scanner"] = sc
        state["task"] = "scan"
        state["running"] = True
        btn_start.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
        bar_all["value"] = 0
        bar_cur["value"] = 0
        lbl_prog.config(text="准备中…")

        def worker():
            try:
                sc.run()
            finally:
                sc.ui["done_flag"] = True

        threading.Thread(target=worker, daemon=True).start()
        parent.after(200, poll_progress)

    # ============================================================
    # 重复文件检查（管理弹窗里多选目录后打开）
    # ============================================================

    def open_dup_check(dirs, on_close=None):
        """dirs = [(root, db_path)]：在选中的若干目录里查重复文件"""
        dirs = [(r, d) for r, d in dirs if os.path.isfile(d)]
        if not dirs:
            messagebox.showinfo(
                "提示", "选中的目录还没有数据库，\n先扫描一次（快速建档即可）再来查重。")
            return

        win = ui.Toplevel(parent)
        win.title("重复文件检查")
        win.geometry("1060x640")
        try:
            win.transient(parent.winfo_toplevel())
            win.grab_set()
        except Exception:
            pass
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(2, weight=1)

        lbl_head = ui.Label(win, foreground=COLOR["text_secondary"])
        lbl_head.grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        bar = ttk.Progressbar(win, mode="indeterminate", length=260)
        bar.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 4))
        bar.start(14)

        frame = ui.Frame(win)
        frame.grid(row=2, column=0, sticky="nsew", padx=12)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        tv = ttk.Treeview(frame, columns=("size", "mtime", "root"),
                          show="tree headings", selectmode="extended")
        tv.heading("#0", text="重复组 / 相对路径")
        tv.column("#0", width=420, minwidth=200, stretch=True, anchor="w")
        for key, title, width, stretch in (("size", "文件大小", 100, False),
                                           ("mtime", "修改时间", 140, False),
                                           ("root", "所在目录", 250, True)):
            tv.heading(key, text=title)
            tv.column(key, width=width, minwidth=70, anchor="w", stretch=stretch)
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        tv.tag_configure("group", background=COLOR["surface_alt"])
        tv.tag_configure("child", foreground=COLOR["text"])

        bottom = ui.Frame(win)
        bottom.grid(row=3, column=0, sticky="ew", padx=12, pady=8)
        lbl_sum = ui.Label(bottom, text="正在读取数据库…", foreground=COLOR["text_secondary"])
        lbl_sum.pack(side=tk.LEFT)

        info = {"done": False, "groups": [], "err": "",
                "msg": "正在读取各目录的数据库…"}
        abs_map = {}

        def close():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            if on_close:
                try:
                    on_close()
                except Exception:
                    pass

        def fill(groups):
            tv.delete(*tv.get_children())
            abs_map.clear()
            wasted = 0
            for gi, (size, sha, files) in enumerate(groups):
                save = size * (len(files) - 1)
                wasted += save
                pid = f"g{gi}"
                tv.insert("", tk.END, iid=pid, text=(
                    f"第 {gi + 1} 组 · {fmt_size(size)} · {len(files)} 份 · "
                    f"可省 {fmt_size(save)} · {sha[:16]}…"),
                    values=("", "", ""), open=True, tags=("group",))
                for fi, (root, rel, abs_path, mt) in enumerate(files):
                    iid = f"g{gi}f{fi}"
                    abs_map[iid] = (abs_path, root, rel)
                    tv.insert(pid, tk.END, iid=iid, text=rel,
                              values=(fmt_size(size), fmt_time(mt),
                                      short_path(root, 40)),
                              tags=("child",))
            if groups:
                for gi in range(len(groups)):
                    tv.item(f"g{gi}", open=True)
                lbl_sum.config(
                    text=f"共 {len(groups)} 组 · 涉及 "
                         f"{sum(len(g[2]) for g in groups)} 个文件 · "
                         f"可省 {fmt_size(wasted)}")
            else:
                lbl_sum.config(text="没有发现重复文件")

        def poll():
            lbl_head.config(text=info["msg"])
            if info["done"]:
                bar.stop()
                bar.grid_remove()
                if info["err"]:
                    lbl_sum.config(text=info["err"])
                else:
                    lbl_head.config(
                        text=f"检查范围：{len(dirs)} 个目录 · 已完成"
                             f"（双击或右键文件可打开所在文件夹）")
                    fill(info["groups"])
                return
            win.after(200, poll)

        def worker():
            def prog(idx, total, size, n):
                info["msg"] = (f"正在比对：第 {idx}/{total} 组同尺寸候选"
                               f"（{fmt_size(size)}，{n} 个文件）")

            try:
                info["groups"] = find_duplicates(dirs, log=log, on_progress=prog)
                log(f"查重完成：{len(dirs)} 个目录，{len(info['groups'])} 组重复")
            except Exception as e:
                info["err"] = f"查重失败：{e}"
                log(f"[查重] 失败：{e}")
            finally:
                info["done"] = True

        def export_dup():
            groups = info["groups"]
            if not groups:
                messagebox.showinfo("提示", "没有可导出的结果", parent=win)
                return
            path = filedialog.asksaveasfilename(
                parent=win, title="导出重复文件清单", defaultextension=".csv",
                initialfile="duplicates_" + time.strftime("%Y%m%d_%H%M%S") + ".csv",
                filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["组号", "相对路径", "大小(字节)", "修改时间",
                                "所在目录", "绝对路径", "SHA256"])
                    for gi, (size, sha, files) in enumerate(groups, 1):
                        for root, rel, abs_path, mt in files:
                            w.writerow([gi, rel, size, fmt_time(mt), root,
                                        abs_path, sha])
                log(f"已导出重复清单（{len(groups)} 组）→ {path}")
                messagebox.showinfo("完成", f"已导出 {len(groups)} 组到：\n{path}",
                                    parent=win)
            except Exception as e:
                messagebox.showerror("导出失败", str(e), parent=win)

        def cur_paths():
            return [abs_map[i][0] for i in tv.selection() if i in abs_map]

        def open_sel():
            ps = cur_paths()
            if ps:
                reveal_in_explorer(ps[0])

        def copy_sel():
            ps = cur_paths()
            if ps:
                win.clipboard_clear()
                win.clipboard_append("\n".join(ps))
                log(f"已复制 {len(ps)} 个路径")

        menu = tk.Menu(win, tearoff=0)
        menu.add_command(label="打开所在文件夹", command=open_sel)
        menu.add_command(label="复制完整路径", command=copy_sel)

        def popup(e):
            iid = tv.identify_row(e.y)
            if iid and iid not in tv.selection():
                tv.selection_set(iid)
            if not tv.selection():
                return
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        tv.bind("<Button-3>", popup)
        tv.bind("<Double-1>", lambda _e: open_sel())

        def toggle_all(open_all):
            for gi in range(len(info["groups"])):
                try:
                    tv.item(f"g{gi}", open=open_all)
                except Exception:
                    pass

        ui.Button(bottom, text="关闭", command=close).pack(side=tk.RIGHT)
        ui.Button(bottom, text="导出 CSV", command=export_dup).pack(
            side=tk.RIGHT, padx=(0, 6))
        ui.Button(bottom, text="全部折叠",
                   command=lambda: toggle_all(False)).pack(
            side=tk.RIGHT, padx=(0, 6))
        ui.Button(bottom, text="全部展开",
                   command=lambda: toggle_all(True)).pack(
            side=tk.RIGHT, padx=(0, 6))
        win.protocol("WM_DELETE_WINDOW", close)

        threading.Thread(target=worker, daemon=True).start()
        win.after(200, poll)

    # ============================================================
    # 检查不重复（两个目录镜像比对）
    # ============================================================

    def open_mirror_check(pair, on_close=None):
        """pair = [(root, db_path), (root, db_path)]：按相对路径比对两个目录"""
        if len(pair) != 2:
            messagebox.showinfo("提示", "请先选中两个目录")
            return
        for r, d in pair:
            if not os.path.isfile(d):
                messagebox.showinfo(
                    "提示", f"「{r}」还没有数据库，\n先扫描一次（快速建档即可）再来比对。")
                return

        win = ui.Toplevel(parent)
        win.title("检查不重复（镜像比对）")
        win.geometry("1120x640")
        try:
            win.transient(parent.winfo_toplevel())
            win.grab_set()
        except Exception:
            pass
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(2, weight=1)

        lbl_head = ui.Label(win, foreground=COLOR["text_secondary"])
        lbl_head.grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))

        topbar = ui.Frame(win)
        topbar.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        bar = ttk.Progressbar(topbar, mode="indeterminate", length=220)
        bar.pack(side=tk.LEFT)
        var_cmp = tk.StringVar(value="path")

        def on_mode_change():
            if cur["done"]:
                run_compare()

        ttk.Radiobutton(topbar, text="按路径比对（镜像）", value="path",
                        variable=var_cmp,
                        command=on_mode_change).pack(side=tk.LEFT, padx=(16, 4))
        ttk.Radiobutton(topbar, text="按哈希比对（找改名 / 移动）", value="hash",
                        variable=var_cmp,
                        command=on_mode_change).pack(side=tk.LEFT, padx=4)

        # FreeFileSync 风格的左右双栏视图：左栏（左边目录）+ 中间状态列 + 右栏（右边目录），
        # 三栏同一行对齐滚动；底部分类筛选（全部 / 内容不同 / 只在一边 / 内容一致 …）
        frame = ui.Frame(win)
        frame.grid(row=2, column=0, sticky="nsew", padx=12)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(2, weight=1)

        # 符号语义：→ ← = 单边复制（另一侧没有）；⇥ ⇤ = 更新覆盖（两边都有，新的覆盖旧的）
        SYM = {MIRROR_SAME: "＝", MIRROR_DIFF: "≠",
               MIRROR_UPD_L: "⇥", MIRROR_UPD_R: "⇤",
               MIRROR_LEFT: "→", MIRROR_RIGHT: "←", MIRROR_UNKNOWN: "？"}
        KIND_TAG = {MIRROR_SAME: "same", MIRROR_DIFF: "diff",
                    MIRROR_UPD_L: "upd", MIRROR_UPD_R: "upd",
                    MIRROR_LEFT: "one", MIRROR_RIGHT: "one",
                    MIRROR_UNKNOWN: "bad"}
        MAXM = 3000                      # 渲染上限，超出提示用筛选缩小范围

        def make_side():
            tv = ttk.Treeview(frame, columns=("size", "mtime"),
                              show="tree headings", selectmode="extended")
            tv.heading("#0", text="相对路径")
            tv.column("#0", width=280, minwidth=140, stretch=True, anchor="w")
            tv.heading("size", text="大小")
            tv.column("size", width=84, minwidth=60, stretch=False, anchor="e")
            tv.heading("mtime", text="修改时间")
            tv.column("mtime", width=136, minwidth=80, stretch=False,
                      anchor="w")
            return tv

        ltv = make_side()
        mtv = ttk.Treeview(frame, show="tree", selectmode="extended")
        mtv.column("#0", width=48, minwidth=48, stretch=False, anchor="center")
        mtv.heading("#0", text="状态")
        rtv = make_side()
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        ltv.grid(row=0, column=0, sticky="nsew")
        mtv.grid(row=0, column=1, sticky="ns")
        rtv.grid(row=0, column=2, sticky="nsew")
        vsb.grid(row=0, column=3, sticky="ns")

        trees = (ltv, mtv, rtv)

        def _vmove(*args):
            for t in trees:
                try:
                    t.yview(*args)
                except Exception:
                    pass

        def _ysync(first, last):
            vsb.set(first, last)

        for t in trees:
            t.configure(yscrollcommand=_ysync)
        vsb.config(command=_vmove)

        def _wheel(e):
            step = -1 if getattr(e, "delta", 0) > 0 else 1
            _vmove("scroll", step * 2, "units")
            return "break"

        for t in trees:
            t.bind("<MouseWheel>", _wheel)
            t.bind("<Button-4>", _wheel)
            t.bind("<Button-5>", _wheel)

        for t in trees:
            t.tag_configure("same", foreground=COLOR["text_muted"])
            t.tag_configure("diff", foreground=COLOR["warning"])
            t.tag_configure("upd", foreground=COLOR["warning"])
            t.tag_configure("one", foreground=COLOR["error"])
            t.tag_configure("bad", foreground=COLOR["error"])
            t.tag_configure("miss", foreground=COLOR["text_secondary"])

        syncing = {"on": False}

        def on_sel(e):
            """左右栏选中联动（中间符号列只含文件行，不存在的 iid 跳过）"""
            if syncing["on"]:
                return
            syncing["on"] = True
            try:
                sel = tuple(e.widget.selection())
                for t in trees:
                    if t is not e.widget:
                        sub = tuple(i for i in sel if t.exists(i))
                        if tuple(t.selection()) != sub:
                            t.selection_set(sub)
            except Exception:
                pass
            finally:
                syncing["on"] = False

        for t in trees:
            t.bind("<<TreeviewSelect>>", on_sel, add="+")

        # 底部分类筛选（带实时条数，默认「全部」；点「内容一致」可单独看相同文件）
        flt_bar = ui.Frame(win)
        flt_bar.grid(row=3, column=0, sticky="w", padx=12, pady=(4, 0))
        MFILTERS = ("全部", MIRROR_UPD_L, MIRROR_UPD_R, MIRROR_DIFF,
                    MIRROR_LEFT, MIRROR_RIGHT, MIRROR_UNKNOWN, MIRROR_SAME)
        filter_btns = {}
        state_flt = {"k": "全部"}

        def set_filter(k):
            state_flt["k"] = k
            fill()

        for k in MFILTERS:
            b = ui.Button(flt_bar, text=k, font=FONTS["sm"],
                          relief=tk.FLAT, bd=0, padx=12, pady=2, cursor="hand2",
                          command=lambda k=k: set_filter(k))
            b.pack(side=tk.LEFT, padx=(0, 4))
            filter_btns[k] = b

        bottom = ui.Frame(win)
        bottom.grid(row=4, column=0, sticky="ew", padx=12, pady=8)
        lbl_sum = ui.Label(bottom, text="准备比对…", foreground=COLOR["text_secondary"])
        lbl_sum.pack(side=tk.LEFT)

        cur = {"left": pair[0], "right": pair[1], "rows": [], "stats": {},
               "done": True, "err": "", "msg": "正在读取两个库…",
               "mode": "path"}

        def visible_rows():
            k = state_flt["k"]
            if k == "全部":
                return cur["rows"]
            return [r for r in cur["rows"] if r[0] == k]

        def head_text():
            return (f"左：{cur['left'][0]}\n右：{cur['right'][0]}\n"
                    f"{cur['msg']}")

        def close():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            if on_close:
                try:
                    on_close()
                except Exception:
                    pass

        def fill():
            rows, st = visible_rows(), cur["stats"]
            for t in trees:
                t.delete(*t.get_children())
            shown = rows[:MAXM]
            # 目录节点：按侧各自推导（该侧没有文件的目录在对面显示为空占位）
            l_dirs, r_dirs = set(), set()
            for kind, rel, *_rest in shown:
                parts = rel.split("/")
                for j in range(1, len(parts)):
                    d = "/".join(parts[:j])
                    if kind != MIRROR_RIGHT:
                        l_dirs.add(d)
                    if kind != MIRROR_LEFT:
                        r_dirs.add(d)
            all_dirs = sorted(l_dirs | r_dirs)
            dir_parent = {d: ("d" + d.rsplit("/", 1)[0] if "/" in d else "")
                          for d in all_dirs}
            for d in all_dirs:
                name = d.rsplit("/", 1)[-1]
                pl = dir_parent[d]
                ltv.insert(pl, tk.END, iid="d" + d,
                           text=name if d in l_dirs else "",
                           values=("", ""),
                           tags=("dir",) if d in l_dirs else ("dir", "miss"),
                           open=True)
                rtv.insert(pl, tk.END, iid="d" + d,
                           text=name if d in r_dirs else "",
                           values=("", ""),
                           tags=("dir",) if d in r_dirs else ("dir", "miss"),
                           open=True)
            iid_rel = {}
            for i, (kind, rel, lsha, rsha, lsize, rsize, lmt, rmt,
                    rrel) in enumerate(shown):
                tag = KIND_TAG.get(kind)
                tags = (tag,) if tag else ()
                parent = "d" + rel.rsplit("/", 1)[0] if "/" in rel else ""
                name = rel.rsplit("/", 1)[-1] if "/" in rel else rel
                rname = rrel.rsplit("/", 1)[-1] if rrel else ""
                has_l = kind != MIRROR_RIGHT
                has_r = kind != MIRROR_LEFT
                # 哈希模式下同一个 rel 可能出现多行（同名不同内容），iid 加序号保唯一
                iid = "f%d~%s" % (i, rel)
                iid_rel[iid] = rel
                ltv.insert(parent, tk.END, iid=iid,
                           text=name if has_l else "",
                           values=(fmt_size(lsize)
                                   if has_l and lsize is not None else "",
                                   fmt_time(lmt) if has_l and lmt else ""),
                           tags=tags if has_l else tags + ("miss",))
                rtv.insert(parent, tk.END, iid=iid,
                           text=rname if has_r else "",
                           values=(fmt_size(rsize)
                                   if has_r and rsize is not None else "",
                                   fmt_time(rmt) if has_r and rmt else ""),
                           tags=tags if has_r else tags + ("miss",))
            cur["iid_rel"] = iid_rel
            counts = {"全部": len(cur["rows"]),
                      MIRROR_UPD_L: st.get("update_left", 0),
                      MIRROR_UPD_R: st.get("update_right", 0),
                      MIRROR_DIFF: st.get("diff", 0),
                      MIRROR_LEFT: st.get("only_left", 0),
                      MIRROR_RIGHT: st.get("only_right", 0),
                      MIRROR_UNKNOWN: st.get("unknown", 0),
                      MIRROR_SAME: st.get("same", 0)}
            for k, b in filter_btns.items():
                on = (k == state_flt["k"])
                try:
                    b.config(text=f"{k} {counts[k]}",
                             bg=COLOR["accent"] if on else COLOR["surface_alt"],
                             fg=COLOR["text_on_accent"] if on else COLOR["text"],
                             activebackground=COLOR["accent_active"] if on else COLOR["border"],
                             activeforeground=COLOR["text_on_accent"] if on else COLOR["text"])
                except Exception:
                    pass
            ltv.heading("#0", text=f"左：{short_path(cur['left'][0], 32)}")
            rtv.heading("#0", text=f"右：{short_path(cur['right'][0], 32)}")
            if cur.get("mode") == "hash":
                txt = (f"按哈希配对 —— 一致 {st.get('same', 0)}"
                       f"（其中改名/移动 {st.get('renamed', 0)}） · "
                       f"只在左边 {st.get('only_left', 0)} · "
                       f"只在右边 {st.get('only_right', 0)} · "
                       f"未算哈希 左 {st.get('nohash_l', 0)} / "
                       f"右 {st.get('nohash_r', 0)}（不参与）")
            else:
                n_diff = st.get("diff", 0)
                n_amb = max(0, n_diff - st.get("update_left", 0)
                            - st.get("update_right", 0))
                txt = (f"共同 {st.get('common', 0)} 个文件 · 一致 {st.get('same', 0)} · "
                       f"左侧有更新 {st.get('update_left', 0)} · "
                       f"右侧有更新 {st.get('update_right', 0)} · "
                       f"方向不明 {n_amb} · "
                       f"只在左边 {st.get('only_left', 0)} · "
                       f"只在右边 {st.get('only_right', 0)} · "
                       f"无法比较 {st.get('unknown', 0)}")
            if state_flt["k"] != "全部":
                txt += f" · 当前显示「{state_flt['k']}」{len(rows)} 条"
            if len(rows) > MAXM:
                txt += f"（只渲染前 {MAXM} 条，请用筛选缩小范围）"
            lbl_sum.config(text=txt)
            fill_middle()

        def visible_file_iids():
            """按左栏当前的展开状态，顺序收集可见的文件行 iid（中间栏据此平铺符号）"""
            out = []

            def walk(parent):
                for iid in ltv.get_children(parent):
                    if iid.startswith("d"):
                        if ltv.item(iid, "open"):
                            walk(iid)
                    else:
                        out.append(iid)

            walk("")
            return out

        def fill_middle():
            """中间状态列：纯符号平铺（不带文件夹折叠标识），行序与左右栏可见行一致"""
            mtv.delete(*mtv.get_children())
            shown = visible_rows()[:MAXM]
            sym, tag = {}, {}
            for i, r in enumerate(shown):
                iid = "f%d~%s" % (i, r[1])
                sym[iid] = SYM.get(r[0], r[0])
                tag[iid] = KIND_TAG.get(r[0])
            for iid in visible_file_iids():
                mtv.insert("", tk.END, iid=iid, text=sym.get(iid, ""),
                           tags=(tag[iid],) if tag.get(iid) else ())
            sel = [i for i in ltv.selection() if i.startswith("f")
                   and mtv.exists(i)]
            if sel:
                mtv.selection_set(sel)

        def set_all_open(open_flag):
            """三栏同步：全部展开 / 全部折叠（只对目录节点）"""
            for t in (ltv, rtv):
                for iid in t.get_children():
                    _rec_open(t, iid, open_flag)
            fill_middle()

        def _rec_open(t, iid, open_flag):
            if iid.startswith("d"):
                t.item(iid, open=open_flag)
            for c in t.get_children(iid):
                _rec_open(t, c, open_flag)

        def on_open_close(e):
            """折叠 / 展开联动：另一侧跟着，中间符号列重建"""
            if syncing["on"]:
                return
            syncing["on"] = True
            try:
                iid = e.widget.focus()
                if iid and iid.startswith("d"):
                    state_open = bool(e.widget.item(iid, "open"))
                    for t in (ltv, rtv):
                        if t is not e.widget and t.exists(iid):
                            t.item(iid, open=state_open)
                    fill_middle()
            except Exception:
                pass
            finally:
                syncing["on"] = False

        for t in trees:
            t.bind("<<TreeviewOpen>>", on_open_close, add="+")
            t.bind("<<TreeviewClose>>", on_open_close, add="+")

        def poll():
            lbl_head.config(text=head_text())
            if cur["done"]:
                bar.stop()
                bar.pack_forget()
                if cur["err"]:
                    lbl_sum.config(text=cur["err"])
                else:
                    fill()
                return
            win.after(200, poll)

        def worker():
            def prog(i, total, rel):
                cur["msg"] = f"正在比对 {i}/{total}：{rel}"

            try:
                if var_cmp.get() == "hash":
                    cur["mode"] = "hash"
                    rows, st = compare_by_hash(cur["left"], cur["right"],
                                               log, on_progress=prog)
                else:
                    cur["mode"] = "path"
                    rows, st = compare_dirs(cur["left"], cur["right"], log,
                                            on_progress=prog, include_same=True)
                cur["rows"], cur["stats"] = rows, st
                cur["msg"] = "比对完成"
            except Exception as e:
                cur["err"] = f"比对失败：{e}"
                log(f"[镜像比对] 失败：{e}")
            finally:
                cur["done"] = True

        def run_compare():
            cur["done"] = False
            cur["err"] = ""
            cur["rows"] = []
            cur["stats"] = {}
            cur["msg"] = "正在读取两个库…"
            for t in trees:
                t.delete(*t.get_children())
            lbl_sum.config(text="正在比对…")
            lbl_head.config(text=head_text())
            bar.pack()
            bar.start(14)
            threading.Thread(target=worker, daemon=True).start()
            win.after(200, poll)

        def swap():
            if not cur["done"]:
                return
            cur["left"], cur["right"] = cur["right"], cur["left"]
            run_compare()

        def export_csv():
            rows, st = visible_rows(), cur["stats"]
            if rows is None:
                return
            path = filedialog.asksaveasfilename(
                parent=win, title="导出比对结果", defaultextension=".csv",
                initialfile="mirror_" + time.strftime("%Y%m%d_%H%M%S") + ".csv",
                filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["左目录", cur["left"][0], "右目录", cur["right"][0]])
                    w.writerow(["状态", "左相对路径", "右相对路径", "左哈希", "右哈希",
                                "左大小", "右大小", "左完整路径", "右完整路径"])
                    for kind, rel, lsha, rsha, lsize, rsize, lmt, rmt, rrel in rows:
                        w.writerow([kind, rel, rrel or rel, lsha or "", rsha or "",
                                    "" if lsize is None else lsize,
                                    "" if rsize is None else rsize,
                                    os.path.join(cur["left"][0], *rel.split("/")),
                                    os.path.join(cur["right"][0],
                                                 *(rrel or rel).split("/"))])
                    w.writerow([])
                    if cur.get("mode") == "hash":
                        w.writerow(["配对成功", st.get("same", 0),
                                    "改名/移动", st.get("renamed", 0),
                                    "只在左边", st.get("only_left", 0),
                                    "只在右边", st.get("only_right", 0),
                                    "未算哈希(左)", st.get("nohash_l", 0),
                                    "未算哈希(右)", st.get("nohash_r", 0)])
                    else:
                        w.writerow(["共同文件", st.get("common", 0),
                                    "一致", st.get("same", 0),
                                    "内容不同", st.get("diff", 0),
                                    "只在左边", st.get("only_left", 0),
                                    "只在右边", st.get("only_right", 0),
                                    "无法比较", st.get("unknown", 0)])
                log(f"已导出镜像比对结果（{len(rows)} 条）→ {path}")
                messagebox.showinfo("完成", f"已导出 {len(rows)} 条到：\n{path}",
                                    parent=win)
            except Exception as e:
                messagebox.showerror("导出失败", str(e), parent=win)

        def cur_file_iid():
            for iid in (ltv.selection() or rtv.selection()):
                if iid.startswith("f"):
                    return iid
            return None

        def cur_rel():
            iid = cur_file_iid()
            return cur.get("iid_rel", {}).get(iid) if iid else None

        def open_side(which):
            rel = cur_rel()
            if rel:
                reveal_in_explorer(
                    os.path.join(cur[which][0], *rel.split("/")))

        def open_row():
            """双击：文件 → 打开有文件的一侧；目录 → 切换展开/折叠"""
            iid = cur_file_iid()
            if iid:
                rel = cur.get("iid_rel", {}).get(iid)
                row = next((r for r in visible_rows() if r[1] == rel), None)
                open_side("right" if row and row[0] == MIRROR_RIGHT else "left")
                return
            # 目录节点双击：切换展开（三栏联动）
            sel = ltv.selection()
            if sel and sel[0].startswith("d"):
                new = not bool(ltv.item(sel[0], "open"))
                for t in trees:
                    if t.exists(sel[0]):
                        t.item(sel[0], open=new)

        MODE_NAME = {SYNC_BOTH: "双向（较新覆盖较旧）",
                     SYNC_LEFT: "以左边为准",
                     SYNC_RIGHT: "以右边为准"}

        def target_rows():
            """同步范围：选中文件/目录 → 只处理选中（目录 = 其下所有可见文件）；
            没选中 → 当前筛选后的全部"""
            sel = list(ltv.selection() or rtv.selection())
            rows = visible_rows()
            if not sel:
                return rows
            file_iids = set()

            def collect(iid):
                if iid.startswith("f"):
                    file_iids.add(iid)
                    return
                for c in ltv.get_children(iid):
                    collect(c)

            for iid in sel:
                collect(iid)
            shown = rows[:MAXM]
            return [r for i, r in enumerate(shown)
                    if ("f%d~%s" % (i, r[1])) in file_iids]

        def confirm_sync(mode, n_new, n_ovr, n_del, n_touch, n_skip, rows, plan):
            dlg = ui.Toplevel(win)
            dlg.title("确认同步")
            dlg.geometry("620x520")
            try:
                dlg.transient(win)
                dlg.grab_set()
            except Exception:
                pass
            dlg.grid_columnconfigure(0, weight=1)
            dlg.grid_rowconfigure(1, weight=1)
            ui.Label(dlg, text=f"同步方向：{MODE_NAME[mode]}",
                      font=FONTS["base"]).grid(
                row=0, column=0, sticky="w", padx=12, pady=(10, 2))
            lines = [f"涉及 {len(rows)} 个文件：",
                     f"  新增复制 {n_new} 个", f"  覆盖 {n_ovr} 个",
                     f"  删除 {n_del} 个", f"  仅改修改日期 {n_touch} 个"]
            if n_skip:
                lines.append(f"  跳过（无法比较 / 已一致）{n_skip} 个")
            head = []
            for src, dst, ovr in plan["copy"][:8]:
                head.append("  复制" + ("覆盖 " if ovr else " ") + short_path(dst, 52))
            for p in plan["delete"][:8]:
                head.append("  删除 " + short_path(p, 52))
            for p, _mt in plan["touch"][:8]:
                head.append("  改日期 " + short_path(p, 52))
            if len(plan["copy"]) + len(plan["delete"]) + len(plan["touch"]) > len(head):
                head.append("  …")
            body = "\n".join(lines + (["——明细——"] + head if head else []))
            txt = tk.Text(dlg, height=12, wrap="none")
            txt.insert("1.0", body)
            txt.configure(state="disabled")
            txt.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 4))
            var_trash = tk.BooleanVar(value=True)
            ttk.Checkbutton(dlg, text="删除 / 覆盖时把被删除或覆盖的文件移入回收站（可还原）",
                            variable=var_trash).grid(
                row=2, column=0, sticky="w", padx=12, pady=(0, 6))
            result = {"ok": False}

            def ok():
                result["ok"] = True
                dlg.destroy()

            btns = ui.Frame(dlg)
            btns.grid(row=3, column=0, sticky="e", padx=12, pady=(0, 10))
            ui.Button(btns, text="取消", command=dlg.destroy).pack(side=tk.RIGHT, padx=3)
            ui.Button(btns, text="开始同步", command=ok).pack(side=tk.RIGHT, padx=3)
            dlg.wait_window()
            return var_trash.get() if result["ok"] else None

        def do_sync(mode):
            if not cur["done"]:
                return
            rows = target_rows()
            if not rows:
                messagebox.showinfo("提示", "没有可同步的条目（当前筛选/选中范围内）",
                                    parent=win)
                return
            plan = build_sync_plan(rows, mode, cur["left"][0], cur["right"][0])
            n_copy = len(plan["copy"])
            n_ovr = sum(1 for _s, _d, o in plan["copy"] if o)
            n_new = n_copy - n_ovr
            n_del = len(plan["delete"])
            n_touch = len(plan["touch"])
            n_skip = len(plan["skip"])
            if n_copy == 0 and n_del == 0 and n_touch == 0:
                messagebox.showinfo("同步", "这些条目已经一致，无需处理。", parent=win)
                return
            use_trash = confirm_sync(mode, n_new, n_ovr, n_del, n_touch,
                                     n_skip, rows, plan)
            if use_trash is None:
                return
            stats, failed = execute_sync_plan(plan, use_trash, log)
            msg = (f"新增复制 {stats['copy']}，覆盖 {stats['overwrite']}，"
                   f"删除 {stats['delete']}，改日期 {stats['touch']}，"
                   f"进回收站 {stats['recycled']}")
            if failed:
                msg += f"，失败 {len(failed)} 条（详见运行日志）"
            log(f"[同步] {MODE_NAME[mode]} —— " + msg)
            messagebox.showinfo("同步完成", msg, parent=win)
            run_compare()

        menu = tk.Menu(win, tearoff=0)
        menu.add_command(label="打开左边所在文件夹",
                         command=lambda: open_side("left"))
        menu.add_command(label="打开右边所在文件夹",
                         command=lambda: open_side("right"))
        menu.add_command(label="复制相对路径", command=lambda: (
            win.clipboard_clear(),
            win.clipboard_append(cur_rel() or "")))
        menu.add_separator()
        menu.add_command(label="同步：双向（较新覆盖较旧）",
                         command=lambda: do_sync(SYNC_BOTH))
        menu.add_command(label="同步：以左边为准", command=lambda: do_sync(SYNC_LEFT))
        menu.add_command(label="同步：以右边为准", command=lambda: do_sync(SYNC_RIGHT))

        def popup(e):
            iid = e.widget.identify_row(e.y)
            if iid and iid not in e.widget.selection():
                e.widget.selection_set(iid)
            if not ltv.selection():
                return
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        for t in trees:
            t.bind("<Button-3>", popup)
            t.bind("<Double-1>", lambda _e: open_row())

        ui.Button(bottom, text="关闭", command=close).pack(side=tk.RIGHT)
        ui.Button(bottom, text="导出 CSV", command=export_csv).pack(
            side=tk.RIGHT, padx=(0, 6))
        ui.Button(bottom, text="交换左右", command=swap).pack(
            side=tk.RIGHT, padx=(0, 6))
        ui.Button(bottom, text="全部折叠",
                   command=lambda: set_all_open(False)).pack(
            side=tk.RIGHT, padx=(0, 6))
        ui.Button(bottom, text="全部展开",
                   command=lambda: set_all_open(True)).pack(
            side=tk.RIGHT, padx=(0, 6))
        win.protocol("WM_DELETE_WINDOW", close)

        run_compare()

    # ============================================================
    # 校验哈希的结果窗口（只读，不改库）
    # ============================================================

    def open_verify_result(results):
        """results = [(结论, rel, 库内哈希, 当前哈希, 当前大小, root, db_path,
                       磁盘修改时间, 库内修改时间)]"""
        if not results:
            messagebox.showinfo("校验哈希", "没有可显示的结果")
            return
        win = ui.Toplevel(parent)
        win.title("校验哈希结果（勾选可写回 / 可统一修改时间）")
        win.geometry("1180x640")
        try:
            win.transient(parent.winfo_toplevel())
            win.grab_set()
        except Exception:
            pass
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(2, weight=1)

        lbl_head = ui.Label(win, text="", foreground=COLOR["text_secondary"])
        lbl_head.grid(row=0, column=0, sticky="w", padx=12, pady=(8, 4))

        bar = ui.Frame(win)
        bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        var_only = tk.BooleanVar(value=bool(
            sum(1 for r in results
                if r[0] in (VERIFY_CHANGED, VERIFY_GONE, VERIFY_ERROR)
                or (r[0] == VERIFY_SAME and r[7] is not None and r[8] is not None
                    and abs(r[7] - r[8]) > 1e-6))))
        ttk.Checkbutton(bar, text="只看有问题的（已变化 / 文件不存在 / 读取失败 / 日期不同）",
                        variable=var_only).pack(side=tk.LEFT)
        lbl_dirty = ui.Label(bar, text="", foreground=COLOR["text_secondary"])
        lbl_dirty.pack(side=tk.LEFT, padx=(12, 0))

        frame = ui.Frame(win)
        frame.grid(row=2, column=0, sticky="nsew", padx=12)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        cols = (("chk", "选", 34, False), ("kind", "结果", 100, False),
                ("rel", "相对路径", 320, True), ("expect", "库内哈希", 140, False),
                ("actual", "当前哈希", 140, False),
                ("size", "当前大小", 84, False),
                ("mtime", "修改时间", 132, False),
                ("root", "所在目录", 160, True))
        tv = ttk.Treeview(frame, columns=[c[0] for c in cols],
                          show="headings", selectmode="extended")
        for key, title, width, stretch in cols:
            tv.heading(key, text=title,
                       anchor="center" if key == "chk" else "w")
            tv.column(key, width=width, minwidth=30 if key == "chk" else 70,
                      anchor="center" if key == "chk" else "w", stretch=stretch)
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tv.xview)
        tv.configure(xscrollcommand=hsb.set)
        hsb.grid(row=1, column=0, sticky="ew")
        tv.tag_configure("changed", foreground=COLOR["error"])
        tv.tag_configure("gone", foreground=COLOR["warning"])
        tv.tag_configure("bad", foreground=COLOR["error"])
        tv.tag_configure("nohash", foreground=COLOR["text_secondary"])
        tv.tag_configure("ok", foreground=COLOR["success"])
        tv.tag_configure("timediff", foreground=COLOR["warning"])

        bottom = ui.Frame(win)
        bottom.grid(row=3, column=0, sticky="ew", padx=12, pady=8)
        lbl_sum = ui.Label(bottom, text="", foreground=COLOR["text_secondary"])
        lbl_sum.pack(side=tk.LEFT)

        limit = 3000
        shown_now = []

        def is_writable(r):
            """能写回库的：已变化 / 无库内哈希，且有当前哈希（连大小/日期一起更新）"""
            return r[0] in (VERIFY_CHANGED, VERIFY_NOHASH) and bool(r[3])

        def time_diff(r):
            """内容一致但修改时间和库对不上（可统一时间）"""
            return (r[0] == VERIFY_SAME and r[7] is not None
                    and r[8] is not None and abs(r[7] - r[8]) > 1e-6)

        def refresh_checks():
            try:
                sel = set(tv.selection())
                for iid in tv.get_children():
                    tv.set(iid, "chk", "☑" if iid in sel else "☐")
            except Exception:
                pass

        def on_click(e):
            try:
                if tv.identify("region", e.x, e.y) != "cell":
                    return None
                if tv.identify_column(e.x) != "#1":
                    return None
                iid = tv.identify_row(e.y)
                if not iid:
                    return None
                if iid in tv.selection():
                    tv.selection_remove(iid)
                else:
                    tv.selection_add(iid)
                refresh_checks()
                return "break"
            except Exception:
                return None

        def update_head():
            counts = {}
            for r in results:
                counts[r[0]] = counts.get(r[0], 0) + 1
            order = (VERIFY_CHANGED, VERIFY_GONE, VERIFY_NOHASH,
                     VERIFY_NOREC, VERIFY_ERROR, VERIFY_SAME)
            head = " · ".join(f"{k} {counts[k]}" for k in order if counts.get(k))
            n_time = sum(1 for r in results if time_diff(r))
            lbl_head.config(text=f"共 {len(results)} 条：{head}"
                             + (f"（其中 {n_time} 条内容一致但日期不同，可统一修改时间）"
                                if n_time else ""))
            n = sum(1 for r in results if is_writable(r))
            lbl_dirty.config(text=f"可写回 {n} 条（勾选后点右下「写入数据库」）"
                             if n else "没有可写回的记录")

        def fill():
            tv.delete(*tv.get_children())
            shown = [r for r in results
                     if (not var_only.get()) or r[0] != VERIFY_SAME
                     or time_diff(r)]
            shown_now[:] = shown
            tag_map = {VERIFY_CHANGED: "changed", VERIFY_GONE: "gone",
                       VERIFY_NOHASH: "nohash", VERIFY_SAME: "ok",
                       VERIFY_NOREC: "bad", VERIFY_ERROR: "bad"}
            for i, r in enumerate(shown[:limit]):
                kind, rel, expect, actual, size = r[0], r[1], r[2], r[3], r[4]
                root, _dbp, dmt = r[5], r[6], r[7]
                tags = [tag_map.get(kind, "")] if tag_map.get(kind) else []
                if time_diff(r):
                    tags.append("timediff")
                tv.insert("", tk.END, iid=f"r{i}", values=(
                    "☐", kind, rel, short_sha(expect), short_sha(actual),
                    fmt_size(size) if size is not None else "—",
                    fmt_time(dmt) if dmt else "—",
                    short_path(root, 40)), tags=tuple(tags))
            update_head()
            if len(shown) > limit:
                lbl_sum.config(
                    text=f"当前显示前 {limit} 条（共 {len(shown)} 条，可导出 CSV 看全部）")
            elif len(shown) != len(results):
                lbl_sum.config(text=f"当前显示 {len(shown)} / {len(results)} 条")
            else:
                lbl_sum.config(text=f"共 {len(results)} 条")

        def close():
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        def export_csv():
            shown = shown_now
            if not shown:
                messagebox.showinfo("提示", "没有可导出的行", parent=win)
                return
            path = filedialog.asksaveasfilename(
                parent=win, title="导出校验结果", defaultextension=".csv",
                initialfile="verify_" + time.strftime("%Y%m%d_%H%M%S") + ".csv",
                filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["结果", "相对路径", "库内哈希", "当前哈希",
                                "当前大小", "修改时间(磁盘)", "修改时间(库)",
                                "所在目录", "完整路径", "数据库"])
                    for r in shown:
                        kind, rel, expect, actual, size = r[0], r[1], r[2], r[3], r[4]
                        root, dbp, dmt, rmt = r[5], r[6], r[7], r[8]
                        w.writerow([kind, rel, expect or "", actual or "",
                                    "" if size is None else size,
                                    fmt_time(dmt) if dmt else "",
                                    fmt_time(rmt) if rmt else "",
                                    root or "",
                                    os.path.join(root, *rel.split("/"))
                                    if root else "", dbp or ""])
                log(f"已导出校验结果（{len(shown)} 行）→ {path}")
                messagebox.showinfo("完成", f"已导出 {len(shown)} 行到：\n{path}",
                                    parent=win)
            except Exception as e:
                messagebox.showerror("导出失败", str(e), parent=win)

        def check_writable():
            ids = [f"r{i}" for i, r in enumerate(shown_now) if is_writable(r)]
            if not ids:
                messagebox.showinfo(
                    "提示",
                    "当前显示的行里没有可写回的（需要「已变化」或「无库内哈希」）",
                    parent=win)
                return
            tv.selection_set(ids)
            refresh_checks()

        def check_timediff():
            ids = [f"r{i}" for i, r in enumerate(shown_now) if time_diff(r)]
            if not ids:
                messagebox.showinfo(
                    "提示",
                    "当前显示的行里没有「内容一致但日期不同」的行", parent=win)
                return
            tv.selection_set(ids)
            refresh_checks()

        def picked_rows():
            picked = []
            for iid in tv.selection():
                try:
                    idx = int(str(iid)[1:])
                except ValueError:
                    continue
                if 0 <= idx < len(shown_now):
                    picked.append(shown_now[idx])
            return picked

        def apply_selected():
            """把勾选行里「已变化 / 无库内哈希」的写回数据库（哈希+大小+日期全更新）"""
            picked = [r for r in picked_rows() if is_writable(r)]
            if not picked:
                messagebox.showinfo(
                    "提示",
                    "没有勾选可写回的行。\n\n"
                    "只有「已变化」和「无库内哈希」的行能写回\n"
                    "（把当前哈希、大小、修改时间一起存进库，预检不会再报差异）；\n"
                    "「一致」的不用写，「文件不存在」的去跑一次完整建档标缺失。",
                    parent=win)
                return
            if not messagebox.askyesno(
                    "写入数据库",
                    f"把勾选的 {len(picked)} 行写回数据库？\n\n"
                    f"· 更新 sha256、大小、修改时间和校验时间（都按磁盘当前值）\n"
                    f"· 写回后这些行会变成「一致」，预检也不会再报差异", parent=win):
                return
            by_db = {}
            for r in picked:
                by_db.setdefault(r[6], []).append(r)
            done = failed = 0
            for dbp, rows in by_db.items():
                lk = DbLock(dbp)
                ok, msg = lk.acquire()
                if not ok:
                    failed += len(rows)
                    log(f"[跳过] 数据库被占用：{dbp}（{msg}）")
                    continue
                try:
                    db = HashDB(dbp)
                    try:
                        db.open()
                        db.conn.execute("BEGIN")
                        for r in rows:
                            db.conn.execute(
                                "UPDATE file_records SET sha256=?, file_size=?,"
                                " mtime=?, last_hash_time=? WHERE rel_path=?",
                                (r[3], r[4], r[7], time.time(), r[1]))
                        db.conn.execute("COMMIT")
                        done += len(rows)
                    except sqlite3.Error as e:
                        try:
                            db.conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                        failed += len(rows)
                        log(f"[写入失败] {dbp}：{e}")
                    finally:
                        db.close()
                finally:
                    lk.release()
            log(f"校验结果写回：成功 {done} 条"
                + (f"，失败 {failed} 条" if failed else ""))
            keys = {(r[6], r[1]) for r in picked}
            for i, r in enumerate(results):
                if (r[6], r[1]) in keys:
                    results[i] = (VERIFY_SAME, r[1], r[3], r[3], r[4], r[5],
                                  r[6], r[7], r[7])
            fill()
            try:
                refresh_db_info()
                do_search()
            except Exception:
                pass
            messagebox.showinfo(
                "完成",
                f"已写回 {done} 条" + (f"，失败 {failed} 条（详见运行日志）" if failed else "")
                + "。\n这些行已变成「一致」。", parent=win)

        def apply_time():
            """勾选的「内容一致但日期不同」行：统一修改时间（新时间=更新库 / 旧时间=改回文件）"""
            picked = [r for r in picked_rows() if time_diff(r)]
            if not picked:
                messagebox.showinfo(
                    "提示",
                    "没有勾选「内容一致但日期不同」的行。\n\n"
                    "这类行内容没变（哈希一致），只是文件的修改日期和库对不上；\n"
                    "统一之后预检就不会再报「更新」。",
                    parent=win)
                return
            use_new = var_time.get() == "new"
            if not messagebox.askyesno(
                    "统一修改时间",
                    f"把勾选的 {len(picked)} 行统一修改时间？\n\n"
                    + (f"· 用【新时间】：数据库的修改时间更新为文件当前日期（不改文件）\n"
                       if use_new else
                       f"· 用【旧时间】：把文件的修改日期改回数据库里记录的日期（不改库）\n")
                    + f"· 统一后这些行不会再报「更新」差异", parent=win):
                return
            done = failed = 0
            if use_new:
                by_db = {}
                for r in picked:
                    by_db.setdefault(r[6], []).append(r)
                for dbp, rows in by_db.items():
                    lk = DbLock(dbp)
                    ok, msg = lk.acquire()
                    if not ok:
                        failed += len(rows)
                        log(f"[跳过] 数据库被占用：{dbp}（{msg}）")
                        continue
                    try:
                        db = HashDB(dbp)
                        try:
                            db.open()
                            db.conn.execute("BEGIN")
                            for r in rows:
                                db.conn.execute(
                                    "UPDATE file_records SET mtime=?"
                                    " WHERE rel_path=?", (r[7], r[1]))
                            db.conn.execute("COMMIT")
                            done += len(rows)
                        except sqlite3.Error as e:
                            try:
                                db.conn.execute("ROLLBACK")
                            except sqlite3.Error:
                                pass
                            failed += len(rows)
                            log(f"[写入失败] {dbp}：{e}")
                        finally:
                            db.close()
                    finally:
                        lk.release()
            else:
                for r in picked:
                    try:
                        p = os.path.join(r[5], *r[1].split("/")) if r[5] else None
                        if p and os.path.exists(p):
                            os.utime(p, (r[8], r[8]))
                            done += 1
                        else:
                            failed += 1
                    except OSError as e:
                        failed += 1
                        log(f"[改日期失败] {r[1]}：{e}")
            log(f"统一修改时间：成功 {done} 条（{'新时间' if use_new else '旧时间'}）"
                + (f"，失败 {failed} 条" if failed else ""))
            for i, r in enumerate(results):
                if time_diff(r) and (r[6], r[1]) in {(x[6], x[1]) for x in picked}:
                    if use_new:
                        results[i] = (r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                                      r[7], r[7])
                    else:
                        results[i] = (r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                                      r[8], r[8])
            fill()
            try:
                refresh_db_info()
                do_search()
            except Exception:
                pass
            messagebox.showinfo(
                "完成", f"已统一 {done} 条修改时间"
                + (f"，失败 {failed} 条（详见运行日志）" if failed else "")
                + "。", parent=win)

        def cur_row():
            ids = tv.selection()
            if not ids:
                return None
            idx = int(str(ids[0])[1:])
            return shown_now[idx] if 0 <= idx < len(shown_now) else None

        def open_folder():
            r = cur_row()
            if r and r[5]:
                reveal_in_explorer(os.path.join(r[5], *r[1].split("/")))

        def copy_path():
            r = cur_row()
            if r and r[5]:
                win.clipboard_clear()
                win.clipboard_append(os.path.join(r[5], *r[1].split("/")))

        m = tk.Menu(win, tearoff=0)
        m.add_command(label="写入数据库（勾选的行）", command=apply_selected)
        m.add_command(label="统一修改时间（勾选的行）", command=apply_time)
        m.add_separator()
        m.add_command(label="打开所在文件夹", command=open_folder)
        m.add_command(label="复制完整路径", command=copy_path)

        def popup(e):
            iid = tv.identify_row(e.y)
            if iid and iid not in tv.selection():
                tv.selection_set(iid)
            if not tv.selection():
                return
            try:
                m.tk_popup(e.x_root, e.y_root)
            finally:
                m.grab_release()

        tv.bind("<Button-1>", on_click)
        tv.bind("<Button-3>", popup)
        tv.bind("<Double-1>", lambda _e: open_folder())
        tv.bind("<<TreeviewSelect>>", lambda _e: refresh_checks())
        var_only.trace_add("write", lambda *_a: fill())

        ui.Button(bottom, text="关闭", command=close).pack(side=tk.RIGHT)
        ui.Button(bottom, text="导出 CSV", command=export_csv).pack(
            side=tk.RIGHT, padx=(0, 6))
        btn_apply = ui.Button(bottom, text="写入数据库", bg=COLOR["success"], fg=COLOR["text_on_accent"],
                              activebackground=COLOR["success_active"], activeforeground=COLOR["text_on_accent"],
                              font=FONTS["sm"], padx=12, pady=2,
                              cursor="hand2", command=apply_selected)
        btn_apply.pack(side=tk.RIGHT, padx=(0, 6))
        ui.Button(bottom, text="勾选可写回", command=check_writable).pack(
            side=tk.RIGHT, padx=(0, 6))
        btn_time = ui.Button(bottom, text="统一修改时间", bg=COLOR["warning"], fg=COLOR["text_on_accent"],
                             activebackground=COLOR["warning_active"], activeforeground=COLOR["text_on_accent"],
                             font=FONTS["sm"], padx=12, pady=2,
                             cursor="hand2", command=apply_time)
        btn_time.pack(side=tk.RIGHT, padx=(0, 6))
        ui.Button(bottom, text="勾选可对时", command=check_timediff).pack(
            side=tk.RIGHT, padx=(0, 6))
        var_time = tk.StringVar(value="new")
        ttk.Radiobutton(bottom, text="用新时间(更新库)", value="new",
                        variable=var_time).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Radiobutton(bottom, text="用旧时间(改回文件)", value="old",
                        variable=var_time).pack(side=tk.RIGHT, padx=(0, 6))
        win.protocol("WM_DELETE_WINDOW", close)
        fill()


    # ============================================================
    # 管理扫描目录
    # ============================================================

    def open_manager():
        win = ui.Toplevel(parent)
        win.title("管理扫描目录")
        win.geometry("960x460")
        try:
            win.transient(parent.winfo_toplevel())
            win.grab_set()
        except Exception:
            pass
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        ui.Label(win, text=f"配置文件：{roots_config_path()}"
                            f"　　点最左列的勾选框 / Ctrl / Shift 多选："
                            f"「检查重复」找重复文件，"
                            f"「检查不重复」比对两个目录的镜像差异",
                  foreground=COLOR["text_secondary"]).grid(
            row=0, column=0, sticky="w", padx=10, pady=(8, 4))

        cols = [("chk", "选", 34, False), ("path", "扫描目录", 280, True),
                ("db", "数据库", 280, True), ("count", "记录", 110, False),
                ("time", "最后扫描", 145, False), ("mode", "模式", 90, False)]
        frame = ui.Frame(win)
        frame.grid(row=1, column=0, sticky="nsew", padx=10)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        tv = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings",
                          selectmode="extended")   # 多选：查重时可选多个目录
        for key, title, width, stretch in cols:
            tv.heading(key, text=title,
                       anchor="center" if key == "chk" else "w")
            tv.column(key, width=width, minwidth=30 if key == "chk" else 80,
                      anchor="center" if key == "chk" else "w", stretch=stretch)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        hsb2 = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tv.xview)
        tv.configure(xscrollcommand=hsb2.set)
        hsb2.grid(row=1, column=0, sticky="ew")
        tv.tag_configure("gone", foreground=COLOR["error"])

        bar = ui.Frame(win)
        bar.grid(row=2, column=0, sticky="ew", padx=10, pady=8)

        def roots_now():
            return load_roots()

        def refresh_checks():
            """按当前 selection 刷新每行开头复选框（☑/☐）"""
            try:
                sel = set(tv.selection())
                for iid in tv.get_children():
                    mark = "☑" if iid in sel else "☐"
                    if tv.set(iid, "chk") != mark:
                        tv.set(iid, "chk", mark)
            except Exception:
                pass

        def on_click(e):
            """点最左边的复选框列：切换该行勾选，不触发默认选中"""
            try:
                if tv.identify("region", e.x, e.y) != "cell":
                    return None
                if tv.identify_column(e.x) != "#1":
                    return None
                iid = tv.identify_row(e.y)
                if not iid:
                    return None
                if iid in tv.selection():
                    tv.selection_remove(iid)
                else:
                    tv.selection_add(iid)
                refresh_checks()
                return "break"
            except Exception:
                return None

        def refresh():
            tv.delete(*tv.get_children())
            for r in roots_now():
                st = db_counts(r["db"])
                gone = st is None
                count = "库不存在" if gone else f"有效 {st[1]} / 共 {st[0]}"
                tv.insert("", tk.END, iid=r["path"], values=(
                    "☐", r["path"], r["db"], count,
                    fmt_time(r.get("last_scan")) if
                    r.get("last_scan") else "—",
                    ROOT_MODE_NAMES.get(r.get("mode", ""), r.get("mode", "") or "—")),
                    tags=("gone",) if gone else ())

        def add_folder():
            p = filedialog.askdirectory(title="选择要纳入管理的扫描目录",
                                        parent=win)
            if not p:
                return
            roots = load_roots()
            upsert_root(roots, os.path.abspath(p))
            save_roots(roots)
            refresh()
            refresh_managed()
            log(f"已加入管理：{p}")

        def remove_selected():
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("提示", "先选中要移除的目录", parent=win)
                return
            if not messagebox.askyesno(
                    "确认", "从列表里移除选中的 {} 个目录？\n\n"
                            "只删除配置文件里的记录，不会动硬盘上的文件和 "
                            "filehash.db。".format(len(sel)), parent=win):
                return
            keep = [r for r in load_roots() if r["path"] not in set(sel)]
            save_roots(keep)
            refresh()
            refresh_managed()
            log(f"已从管理列表移除 {len(sel)} 个目录")

        def scan_this():
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("提示", "先选中一个目录", parent=win)
                return
            var_root.set(sel[0])
            refresh_db_info()
            win.destroy()
            log(f"扫描根已设为：{sel[0]}（点「开始扫描」执行）")

        def open_folder():
            sel = tv.selection()
            if sel:
                reveal_in_explorer(sel[0])

        ui.Button(bar, text="添加目录", command=add_folder).pack(side=tk.LEFT, padx=3)
        ui.Button(bar, text="移除选中", command=remove_selected).pack(side=tk.LEFT, padx=3)
        ui.Button(bar, text="刷新", command=refresh).pack(side=tk.LEFT, padx=3)
        ui.Button(bar, text="设为扫描目录", command=scan_this).pack(side=tk.LEFT, padx=3)

        def select_all(flag=True):
            ids = tv.get_children()
            if not ids:
                return
            if flag:
                tv.selection_set(ids)
            else:
                tv.selection_remove(*ids)
            refresh_checks()

        ui.Button(bar, text="全选", command=lambda: select_all(True)).pack(
            side=tk.LEFT, padx=(10, 3))
        ui.Button(bar, text="全不选", command=lambda: select_all(False)).pack(
            side=tk.LEFT, padx=3)
        def check_dup():
            sel = list(tv.selection())
            if not sel:
                messagebox.showinfo(
                    "提示", "先选中要查重的目录（Ctrl / Shift 可多选）", parent=win)
                return
            dbmap = {r["path"]: (r.get("db") or os.path.join(r["path"], DB_NAME))
                     for r in load_roots()}
            dirs = [(p, dbmap.get(p) or os.path.join(p, DB_NAME)) for p in sel]
            win.grab_release()
            open_dup_check(dirs, on_close=lambda: _regrab(win))

        def _regrab(w):
            try:
                if w.winfo_exists():
                    w.grab_set()
            except Exception:
                pass

        def check_mirror():
            sel = list(tv.selection())
            if len(sel) != 2:
                messagebox.showinfo(
                    "提示", "「检查不重复」要选中两个目录（Ctrl / Shift 多选）",
                    parent=win)
                return
            dbmap = {r["path"]: (r.get("db") or os.path.join(r["path"], DB_NAME))
                     for r in load_roots()}
            pair = [(p, dbmap.get(p) or os.path.join(p, DB_NAME)) for p in sel]
            win.grab_release()
            open_mirror_check(pair, on_close=lambda: _regrab(win))

        ui.Button(bar, text="检查重复", command=check_dup).pack(side=tk.LEFT, padx=(16, 3))
        ui.Button(bar, text="检查不重复", command=check_mirror).pack(side=tk.LEFT, padx=3)
        ui.Button(bar, text="打开文件夹", command=open_folder).pack(side=tk.LEFT, padx=3)
        ui.Button(bar, text="关闭", command=win.destroy).pack(side=tk.RIGHT, padx=3)

        tv.bind("<Double-1>", lambda _e: scan_this())
        tv.bind("<<TreeviewSelect>>", lambda _e: refresh_checks())
        tv.bind("<Button-1>", on_click)      # 点第一列 = 勾选 / 取消
        refresh()

    btn_start.config(command=start)
    btn_apply.config(command=apply_selected)
    btn_sel_all.config(command=lambda: select_all(True))
    btn_sel_none.config(command=lambda: select_all(False))
    tree.bind("<<TreeviewSelect>>", update_sel)
    tree.bind("<Button-1>", on_tree_click)      # 点第一列 = 勾选 / 取消
    for _k, _b in filter_btns.items():
        _b.config(command=lambda k=_k: set_filter(k))
    # 分页：首页 / 上页 / 下页 / 末页 / 页码跳转 / 每页条数
    btn_pg_first.config(command=lambda: goto_page(0))
    btn_pg_prev.config(command=lambda: goto_page(state["page"] - 1))
    btn_pg_next.config(command=lambda: goto_page(state["page"] + 1))
    btn_pg_last.config(command=lambda: goto_page(page_count() - 1))
    ent_page.bind("<Return>", jump_page)
    ent_page.bind("<FocusOut>", jump_page)
    cmb_page_size.bind("<<ComboboxSelected>>", change_page_size)
    cmb_page_size.bind("<Return>", change_page_size)
    cmb_page_size.bind("<FocusOut>", change_page_size)
    cmb_root.bind("<<ComboboxSelected>>", lambda _e: refresh_db_info())
    cmb_root.bind("<Return>", lambda _e: refresh_db_info())
    cmb_root.bind("<FocusOut>", lambda _e: refresh_db_info())
    ent_kw.bind("<Return>", lambda _e: do_search())

    # 拖入文件夹 = 设为扫描根
    def on_drop(paths):
        for p in paths:
            p = clean_path(p)
            if os.path.isdir(p):
                var_root.set(os.path.abspath(p))
                refresh_db_info()
                log(f"扫描根已设为：{p}")
                return
            if os.path.isfile(p):
                var_root.set(os.path.dirname(os.path.abspath(p)))
                refresh_db_info()
                return

    if app is not None:
        try:
            app.register_drop_handler(parent, on_drop)
        except Exception:
            pass

    refresh_managed()
    refresh_db_info()
    render()          # 先画一次空表，让筛选按钮有初始外观

    # 供外部（自动化冒烟测试 / 其它模块）调用
    return {
        "var_root": var_root,
        "var_mode": var_mode,
        "var_kw": var_kw,
        "var_kind": var_kind,
        "var_scope": var_scope,
        "var_inc_deleted": var_inc_deleted,
        "start": start,
        "stop": on_stop,
        "search": do_search,
        "view": view_db,
        "fill": fill_hashes,
        "merge": merge_dbs,
        "seed": seed_from_parent,
        "verify": verify_selected,
        "rehash": rehash_selected,
        "refresh": refresh_db_info,
        "rows": lambda: state["rows"],
        "pending": lambda: state["pending"],
        "shown": lambda: state["shown"],
        "page_rows": lambda: state["page_rows"],
        "page": lambda: state["page"] + 1,
        "pages": page_count,
        "page_size": lambda: state["page_size"],
        "goto_page": goto_page,
        "jump_page": jump_page,
        "set_page_size": change_page_size,
        "var_page": var_page,
        "var_page_size": var_page_size,
        "apply": apply_selected,
        "select_all": select_all,
        "filter": set_filter,
        "is_running": lambda: state["running"],
        "log": log,
        "tree": tree,
        "manager": open_manager,
        "managed": refresh_managed,
        "dup": open_dup_check,
        "mirror": open_mirror_check,
        "root_values": lambda: list(cmb_root.cget("values")),
    }
