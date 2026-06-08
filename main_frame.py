"""嵌入式包裝 — 讓 照片批次加註工具 跑在 Launcher 的分頁裡。

實作 create_frame(parent) -> ttk.Frame,由 Launcher 動態載入。
用 importlib 從絕對路徑載入 app/core.py 與 app/main.py,並在載入 main.py
期間暫時把 sys.modules["app.core"] 指向我們的 core,讓 main.py 內的
`from app.core import ...` 解析成功,又不污染 Launcher 的 app.* 名稱空間。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import tkinter as tk
from tkinter import ttk

_TOOL_ROOT = Path(__file__).parent


def _load_tool_main():
    core_spec = importlib.util.spec_from_file_location(
        "_phototool.core", _TOOL_ROOT / "app" / "core.py")
    core_mod = importlib.util.module_from_spec(core_spec)
    sys.modules["_phototool.core"] = core_mod

    _prev = sys.modules.get("app.core")
    sys.modules["app.core"] = core_mod
    try:
        core_spec.loader.exec_module(core_mod)
        main_spec = importlib.util.spec_from_file_location(
            "_phototool.main", _TOOL_ROOT / "app" / "main.py")
        main_mod = importlib.util.module_from_spec(main_spec)
        sys.modules["_phototool.main"] = main_mod
        main_spec.loader.exec_module(main_mod)
    finally:
        if _prev is not None:
            sys.modules["app.core"] = _prev
        else:
            sys.modules.pop("app.core", None)
    return main_mod


_tool_main = _load_tool_main()


def create_frame(parent: tk.Widget) -> ttk.Frame:
    frame = ttk.Frame(parent)
    _tool_main.MainWindow(frame, embedded=True)
    return frame
