# -*- coding: utf-8 -*-
"""v1.31 还原冒烟测试：纯 tkinter 构建 + 逐页切换。"""
import tkinter.messagebox as _mb
_mb.showwarning = lambda *a, **k: None
_mb.showerror = lambda *a, **k: None
_mb.showinfo = lambda *a, **k: None

import tkinter as tk
from toolbox.main import ToolboxApp

errors = []
root = tk.Tk()
root.geometry("1360x900")
try:
    app = ToolboxApp(root)
    root.update_idletasks()
    import toolbox.common as C
    print(f"BUILD_OK version={C.APP_VERSION} items={len(app.nav_items)}")
    for i in range(len(app.nav_items)):
        try:
            app.show_tool(i)
            root.update_idletasks()
        except Exception as e:
            errors.append((i, repr(e)))
    print(f"VISITED={len(app.nav_items)} errors={len(errors)}")
    for i, e in errors:
        print(f"  TAB#{i} -> {e}")
except Exception as e:
    import traceback; traceback.print_exc()
finally:
    try: root.destroy()
    except Exception: pass
