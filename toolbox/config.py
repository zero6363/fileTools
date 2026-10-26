# -*- coding: utf-8 -*-
"""统一配置：程序目录下只保留一个 toolbox_config.json
================================================================
以前分散在 4 个文件里，现在按分区（section）合并成一份：

    toolbox_config.json
    ├─ "window"   窗口位置 / 大小 / 最大化 / 左侧菜单宽度
    ├─ "compress" ⑯ 图片压缩的选项记忆
    ├─ "ffmpeg"   ⑦ 手动指定的 ffmpeg 路径
    └─ "hashscan" ⑲ 已管理的扫描目录清单

旧文件（toolbox_window.json / compressor_config.json / ffmpeg_config.json /
hashscan_roots.json）在**首次加载时自动并入**合并文件，内容读得出来才删除原文件；
读不出来的（比如半截损坏的）原样留着，避免误丢数据。

特点：
  · 读一次缓存到内存，各模块各自 get/update 自己那一段，互不覆盖；
  · 写文件是「临时文件 + os.replace」原子替换，断电/崩溃不会留下半个 json；
  · 加锁，多线程同时保存也不会互相踩。
"""
import json
import os
import sys
import threading

CONFIG_NAME = "toolbox_config.json"

# 分区 -> 旧版独立文件（只用于一次性迁移）
LEGACY_FILES = {
    "window": "toolbox_window.json",
    "compress": "compressor_config.json",
    "ffmpeg": "ffmpeg_config.json",
    "hashscan": "hashscan_roots.json",
}

_lock = threading.RLock()
_data = None            # 内存中的整份配置
_path_override = None   # 测试用：临时指定配置文件路径
_migrated = False


# ============================================================
# 路径
# ============================================================

def app_dir():
    """程序所在目录（打包后是 exe 目录，源码运行是工具箱根目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # toolbox/config.py -> toolbox -> 工具箱根目录
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path():
    return _path_override or os.path.join(app_dir(), CONFIG_NAME)


def set_config_path(path):
    """测试用：换一个配置文件（并清空缓存）"""
    global _path_override, _data, _migrated
    _path_override = path
    _data = None
    _migrated = False


# ============================================================
# 读 / 写
# ============================================================

def _load():
    global _data
    if _data is not None:
        return _data
    _data = {}
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            _data = raw
    except Exception:
        pass
    _auto_migrate()
    return _data


def _auto_migrate():
    """把旧的独立 json 并进来；只搬一次（合并文件里已有该分区就跳过）"""
    global _migrated
    if _migrated:
        return
    _migrated = True
    taken, legacy_used = [], []
    legacy_dir = os.path.dirname(config_path()) or app_dir()
    for section, fname in LEGACY_FILES.items():
        if isinstance(_data.get(section), dict) and _data[section]:
            continue                      # 合并文件里已有内容，不覆盖
        p = os.path.join(legacy_dir, fname)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue                      # 读不出来就留着，别删
        if not isinstance(raw, dict) or not raw:
            continue
        _data[section] = raw
        taken.append(section)
        legacy_used.append(p)
    if taken and save():
        for p in legacy_used:             # 确实写进合并文件了，才删旧文件
            try:
                os.remove(p)
            except OSError:
                pass


def save():
    """原子写：先写 .tmp 再 replace"""
    with _lock:
        p = config_path()
        parent = os.path.dirname(p)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                pass
        tmp = p + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_data or {}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            return True
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False


# ============================================================
# 分区读写
# ============================================================

def get_section(name):
    """取某个分区（返回副本；改了要自己 set/update 才会落盘）"""
    with _lock:
        sec = _load().get(name)
        return dict(sec) if isinstance(sec, dict) else {}


def set_section(name, values):
    """整段替换并立即保存"""
    with _lock:
        _load()[name] = dict(values or {})
        return save()


def update_section(name, **values):
    """合并若干键并立即保存（其它键、其它分区都不动）"""
    with _lock:
        data = _load()
        sec = data.get(name)
        if not isinstance(sec, dict):
            sec = {}
        sec.update(values)
        data[name] = sec
        return save()


def has_section(name):
    with _lock:
        return isinstance(_load().get(name), dict)
