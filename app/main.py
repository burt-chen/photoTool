"""照片批次加註文字 — Tkinter 介面。

排版:四個分頁 (照片 / 文字內容 / 樣式與位置 / 預覽與輸出)。
繁重的批次輸出在背景執行緒跑,用 root.after 把結果送回 UI 執行緒。

MainWindow 同時支援獨立視窗 (root = tk.Tk) 與嵌入 Launcher 分頁
(root = ttk.Frame, embedded=True)。
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageTk

from app.core import (
    DATE_ERAS,
    DATE_PATTERN_PRESETS,
    IMAGE_EXTS,
    META_FIELDS,
    POSITIONS,
    PRESET_NAMES,
    PRESETS,
    TextStyle,
    annotate_image,
    available_fonts,
    build_mapping,
    format_date,
    list_sheets,
    load_for_annotation,
    lookup_text,
    output_filename,
    read_photo_info,
    read_table,
    render_thumbnail,
    save_image,
    text_from_meta,
)

APP_TITLE = "照片批次加註工具"
UI_FONT_SIZE = 11

SEP_OPTIONS = [("換行", "\n"), ("空格", " "), ("逗號", ", "), ("斜線", " / "), ("自訂", None)]

STYLES_PATH = Path.home() / ".phototool_styles.json"
THUMB_BOX = 132  # 縮圖最長邊像素


def _configure_global_fonts(size: int = UI_FONT_SIZE) -> None:
    import tkinter.font as tkfont
    for name in (
        "TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
        "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
        "TkIconFont", "TkTooltipFont",
    ):
        try:
            tkfont.nametofont(name).configure(size=size)
        except tk.TclError:
            pass
    style = ttk.Style()
    for st in (
        "TButton", "TLabel", "TEntry", "TCombobox", "TCheckbutton",
        "TRadiobutton", "TMenubutton", "TNotebook", "TNotebook.Tab",
        "TLabelframe", "TLabelframe.Label", "Treeview", "Treeview.Heading",
        "TProgressbar", "TSpinbox",
    ):
        try:
            style.configure(st, font=("TkDefaultFont", size))
        except tk.TclError:
            pass


class ColorButton(ttk.Frame):
    """一個顯示目前顏色的小方塊,點擊開啟取色器。"""

    def __init__(self, parent, initial: str, on_change=None):
        super().__init__(parent)
        self._color = initial or "#FFFFFF"
        self._on_change = on_change
        self.swatch = tk.Label(
            self, width=4, relief="solid", borderwidth=1, bg=self._color
        )
        self.swatch.pack(side="left")
        self.swatch.bind("<Button-1>", lambda _e: self._pick())
        self.btn = ttk.Button(self, text="選色…", width=6, command=self._pick)
        self.btn.pack(side="left", padx=(4, 0))

    def _pick(self):
        rgb, hexv = colorchooser.askcolor(color=self._color, title="選擇顏色")
        if hexv:
            self.set_color(hexv)
            if self._on_change:
                self._on_change(hexv)

    def color(self) -> str:
        return self._color

    def set_color(self, hexv: str):
        self._color = hexv
        try:
            self.swatch.configure(bg=hexv)
        except tk.TclError:
            pass

    def set_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.btn.configure(state=state)


class ThumbnailGrid(ttk.Frame):
    """可捲動的縮圖牆(像檔案總管),點縮圖回呼 on_select(index)。

    縮圖由外部以 set_thumb(i, PIL.Image) 逐張填入(背景產生),
    本元件只負責版面、捲動、選取高亮與欄數重排。
    """

    CELL_W = THUMB_BOX + 18
    CELL_H = THUMB_BOX + 38

    def __init__(self, parent, on_select):
        super().__init__(parent)
        self._on_select = on_select
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#1e1e1e",
                                width=2 * self.CELL_W + 24)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.inner = tk.Frame(self.canvas, background="#1e1e1e")
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.inner.bind("<Configure>",
                        lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda _e: self._relayout())
        self.canvas.bind("<Enter>", lambda _e: self._wheel(True))
        self.canvas.bind("<Leave>", lambda _e: self._wheel(False))
        self._cells: list[dict] = []
        self._imgs: dict[int, ImageTk.PhotoImage] = {}
        self._cols = 0
        self._selected = -1
        self._blank: ImageTk.PhotoImage | None = None

    def _wheel(self, on: bool):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        else:
            self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        first, last = self.canvas.yview()
        if last - first >= 1.0:
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def set_photos(self, paths):
        for c in self._cells:
            c["frame"].destroy()
        self._cells.clear()
        self._imgs.clear()
        self._selected = -1
        if self._blank is None:
            ph = Image.new("RGB", (THUMB_BOX, int(THUMB_BOX * 0.72)), "#3a3a3a")
            self._blank = ImageTk.PhotoImage(ph)
        for i, p in enumerate(paths):
            f = tk.Frame(self.inner, background="#1e1e1e",
                         highlightthickness=2, highlightbackground="#1e1e1e")
            img_lbl = tk.Label(f, image=self._blank, background="#000")
            img_lbl.pack(padx=2, pady=2)
            nm = tk.Label(f, text=Path(p).name, background="#1e1e1e", foreground="#ddd",
                          width=18, anchor="center")
            nm.pack()
            for w in (f, img_lbl, nm):
                w.bind("<Button-1>", lambda _e, idx=i: self._on_select(idx))
            self._cells.append({"frame": f, "img": img_lbl})
        self._cols = 0
        self._relayout(force=True)

    def set_thumb(self, i: int, pil_img):
        if not (0 <= i < len(self._cells)):
            return
        photo = ImageTk.PhotoImage(pil_img)
        self._imgs[i] = photo  # 保留參照,否則被 GC 後變空白
        try:
            self._cells[i]["img"].configure(image=photo)
        except tk.TclError:
            pass

    def select(self, i: int):
        if 0 <= self._selected < len(self._cells):
            try:
                self._cells[self._selected]["frame"].configure(highlightbackground="#1e1e1e")
            except tk.TclError:
                pass
        self._selected = i
        if 0 <= i < len(self._cells):
            try:
                self._cells[i]["frame"].configure(highlightbackground="#4da3ff")
            except tk.TclError:
                pass
            self._ensure_visible(i)

    def _ensure_visible(self, i: int):
        self.update_idletasks()
        try:
            cell = self._cells[i]["frame"]
            y, h = cell.winfo_y(), cell.winfo_height()
            total = self.inner.winfo_height()
            view_h = self.canvas.winfo_height()
            if total <= 0 or view_h <= 0:
                return
            top, bot = self.canvas.yview()
            if y / total < top:
                self.canvas.yview_moveto(y / total)
            elif (y + h) / total > bot:
                self.canvas.yview_moveto(max(0.0, (y + h) / total - view_h / total))
        except (tk.TclError, ZeroDivisionError):
            pass

    def _relayout(self, force: bool = False):
        w = self.canvas.winfo_width()
        cols = max(1, w // self.CELL_W)
        if cols == self._cols and not force:
            return
        self._cols = cols
        for idx, c in enumerate(self._cells):
            r, cc = divmod(idx, cols)
            c["frame"].grid(row=r, column=cc, padx=4, pady=4)


class MainWindow:
    def __init__(self, root, embedded: bool = False):
        self.root = root
        self.embedded = embedded
        _configure_global_fonts()

        if not embedded:
            root.title(APP_TITLE)
            try:
                root.state("zoomed")
            except tk.TclError:
                root.geometry("1200x820")

        # ---- state ----
        self.photos: list[str] = []
        self._fonts = available_fonts()
        self._mapping: dict[str, str] | None = None
        self._preview_imgtk = None
        self._running = False
        self._preview_index = -1
        self._thumb_gen = 0
        self._thumb_sig = None

        # 避免滑鼠滾輪停在 Combobox 上誤改值
        try:
            root.bind_class("TCombobox", "<MouseWheel>", lambda _e: "break")
        except tk.TclError:
            pass

        self._build_ui()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self.notebook.add(self._build_photos_tab(self.notebook), text="1. 照片")
        self.notebook.add(self._build_text_tab(self.notebook), text="2. 文字內容")
        self.notebook.add(self._build_style_tab(self.notebook), text="3. 樣式與位置")
        self.notebook.add(self._build_output_tab(self.notebook), text="4. 預覽 / 輸出")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ---- Tab 1: 照片 ----
    def _build_photos_tab(self, parent):
        page = ttk.Frame(parent)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        bar = ttk.Frame(page)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(bar, text="加入照片…", command=self._add_files).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="加入資料夾…", command=self._add_folder).pack(side="left", padx=(0, 4))
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="含子資料夾", variable=self.recursive_var).pack(side="left", padx=(0, 12))
        ttk.Button(bar, text="移除選取", command=self._remove_selected).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="清空", command=self._clear_photos).pack(side="left")
        self.count_var = tk.StringVar(value="尚未加入照片")
        ttk.Label(bar, textvariable=self.count_var, foreground="#1976d2").pack(side="right")

        list_box = ttk.LabelFrame(page, text="照片清單")
        list_box.grid(row=1, column=0, sticky="nsew")
        list_box.rowconfigure(0, weight=1)
        list_box.columnconfigure(0, weight=1)
        self.photo_list = tk.Listbox(list_box, selectmode="extended", activestyle="none")
        self.photo_list.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        sb = ttk.Scrollbar(list_box, orient="vertical", command=self.photo_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.photo_list.configure(yscrollcommand=sb.set)

        tip = ("流程:加入照片 → 設定文字內容 → 調樣式與位置 → 預覽確認後輸出。\n"
               "支援 jpg / png / bmp / tif / webp。")
        ttk.Label(page, text=tip, foreground="#444").grid(row=2, column=0, sticky="w", pady=(6, 0))
        return page

    # ---- Tab 2: 文字內容 ----
    def _build_text_tab(self, parent):
        page = ttk.Frame(parent)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        mode_box = ttk.LabelFrame(page, text="文字來源")
        mode_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.mode_var = tk.StringVar(value="uniform")
        modes = [
            ("每張文字相同(統一輸入)", "uniform"),
            ("每張不同 — 讀對照表(csv / xlsx)", "table"),
            ("每張不同 — 讀照片資訊(檔名 / 日期 / 座標)", "meta"),
        ]
        for i, (lbl, val) in enumerate(modes):
            ttk.Radiobutton(
                mode_box, text=lbl, value=val, variable=self.mode_var,
                command=self._on_mode_changed,
            ).grid(row=i, column=0, sticky="w", padx=8, pady=2)

        # 各模式設定區(疊在同一格,依模式切換顯示)
        self.mode_frames_holder = ttk.Frame(page)
        self.mode_frames_holder.grid(row=1, column=0, sticky="nsew")
        self.mode_frames_holder.columnconfigure(0, weight=1)
        self.mode_frames_holder.rowconfigure(0, weight=1)

        self._frame_uniform = self._build_uniform_frame(self.mode_frames_holder)
        self._frame_table = self._build_table_frame(self.mode_frames_holder)
        self._frame_meta = self._build_meta_frame(self.mode_frames_holder)
        for fr in (self._frame_uniform, self._frame_table, self._frame_meta):
            fr.grid(row=0, column=0, sticky="nsew")
        self._on_mode_changed()
        return page

    def _build_uniform_frame(self, parent):
        fr = ttk.LabelFrame(parent, text="統一文字(可多行)")
        fr.columnconfigure(0, weight=1)
        fr.rowconfigure(1, weight=1)
        ttk.Label(fr, text="每張照片都會加上以下文字:").grid(row=0, column=0, sticky="w", padx=6, pady=(6, 2))
        self.uniform_text = tk.Text(fr, height=5, wrap="word")
        self.uniform_text.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        return fr

    def _build_table_frame(self, parent):
        fr = ttk.LabelFrame(parent, text="對照表(需有「照片名稱」與「文字」兩欄)")
        fr.columnconfigure(1, weight=1)
        ttk.Label(fr, text="表格檔:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.table_path_var = tk.StringVar()
        ttk.Entry(fr, textvariable=self.table_path_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(fr, text="選檔…", command=self._pick_table).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(fr, text="工作表:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.sheet_var = tk.StringVar()
        self.sheet_cb = ttk.Combobox(fr, textvariable=self.sheet_var, state="disabled", width=24)
        self.sheet_cb.grid(row=1, column=1, sticky="w", padx=4, pady=4)
        self.sheet_cb.bind("<<ComboboxSelected>>", lambda _e: self._reload_table())

        ttk.Label(fr, text="照片名稱欄:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.name_col_var = tk.StringVar()
        self.name_col_cb = ttk.Combobox(fr, textvariable=self.name_col_var, state="readonly", width=24)
        self.name_col_cb.grid(row=2, column=1, sticky="w", padx=4, pady=4)
        self.name_col_cb.bind("<<ComboboxSelected>>", lambda _e: self._rebuild_mapping())

        ttk.Label(fr, text="文字內容欄:").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        self.text_col_var = tk.StringVar()
        self.text_col_cb = ttk.Combobox(fr, textvariable=self.text_col_var, state="readonly", width=24)
        self.text_col_cb.grid(row=3, column=1, sticky="w", padx=4, pady=4)
        self.text_col_cb.bind("<<ComboboxSelected>>", lambda _e: self._rebuild_mapping())

        self.table_status = tk.StringVar(value="尚未載入對照表")
        ttk.Label(fr, textvariable=self.table_status, foreground="#1976d2").grid(
            row=4, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 6))
        ttk.Label(
            fr, foreground="#666",
            text="比對方式:對照表的照片名稱與檔案相符即套用(含 / 不含副檔名皆可)。",
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 6))
        return fr

    def _build_meta_frame(self, parent):
        fr = ttk.LabelFrame(parent, text="從照片資訊選取要加註的欄位")
        fr.columnconfigure(0, weight=1)
        ttk.Label(fr, text="勾選欄位(依下列順序組合;無資料的欄位會自動略過):").grid(
            row=0, column=0, sticky="w", padx=6, pady=(6, 2))
        checks = ttk.Frame(fr)
        checks.grid(row=1, column=0, sticky="w", padx=10)
        self.meta_vars: dict[str, tk.BooleanVar] = {}
        for i, (lbl, key) in enumerate(META_FIELDS):
            v = tk.BooleanVar(value=(key == "datetime"))
            self.meta_vars[key] = v
            ttk.Checkbutton(checks, text=lbl, variable=v).grid(
                row=i // 3, column=i % 3, sticky="w", padx=6, pady=2)

        sep_row = ttk.Frame(fr)
        sep_row.grid(row=2, column=0, sticky="w", padx=6, pady=(8, 6))
        ttk.Label(sep_row, text="欄位間隔:").pack(side="left")
        self.sep_var = tk.StringVar(value="換行")
        self.sep_cb = ttk.Combobox(
            sep_row, textvariable=self.sep_var, state="readonly", width=8,
            values=[lbl for lbl, _ in SEP_OPTIONS])
        self.sep_cb.pack(side="left", padx=(4, 4))
        self.sep_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_sep_changed())
        self.sep_custom_var = tk.StringVar(value="")
        self.sep_custom_entry = ttk.Entry(sep_row, textvariable=self.sep_custom_var, width=10, state="disabled")
        self.sep_custom_entry.pack(side="left")

        # 日期格式(套用到「拍攝日期 / 拍攝日期時間」)
        date_box = ttk.LabelFrame(fr, text="日期格式(套用到拍攝日期 / 拍攝日期時間)")
        date_box.grid(row=3, column=0, sticky="ew", padx=6, pady=(2, 6))
        ttk.Label(date_box, text="年制:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.date_era_var = tk.StringVar(value="西元")
        era_row = ttk.Frame(date_box)
        era_row.grid(row=0, column=1, sticky="w", pady=4)
        for e in DATE_ERAS:
            ttk.Radiobutton(era_row, text=e, value=e, variable=self.date_era_var,
                            command=self._update_date_example).pack(side="left", padx=(0, 8))
        ttk.Label(date_box, text="格式:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.date_pattern_var = tk.StringVar(value="YYYY-MM-DD")
        self.date_pattern_cb = ttk.Combobox(date_box, textvariable=self.date_pattern_var,
                                            values=DATE_PATTERN_PRESETS, width=22)
        self.date_pattern_cb.grid(row=1, column=1, sticky="w", pady=4)
        self.date_pattern_cb.bind("<<ComboboxSelected>>", lambda _e: self._update_date_example())
        self.date_pattern_var.trace_add("write", lambda *_: self._update_date_example())
        self.date_example_var = tk.StringVar(value="")
        ttk.Label(date_box, textvariable=self.date_example_var, foreground="#1976d2").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))
        ttk.Label(date_box, foreground="#666",
                  text="可用代碼:YYYY 年(民國自動換算) MM 月 DD 日 HH:mm:ss 時分秒").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))

        ttk.Label(fr, foreground="#666",
                  text="提示:GPS / 拍攝日期需照片本身含 EXIF 資訊才讀得到。").grid(
            row=4, column=0, sticky="w", padx=6, pady=(0, 6))
        self._update_date_example()
        return fr

    # ---- Tab 3: 樣式與位置 ----
    def _build_style_tab(self, parent):
        page = ttk.Frame(parent)
        page.columnconfigure(0, weight=1)

        # 預設樣式 + 字型
        s1 = ttk.LabelFrame(page, text="樣式")
        s1.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        s1.columnconfigure(1, weight=1)

        ttk.Label(s1, text="預設樣式:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.preset_var = tk.StringVar(value="白字黑框")
        self.preset_cb = ttk.Combobox(s1, textvariable=self.preset_var, state="readonly",
                                       values=PRESET_NAMES, width=14)
        self.preset_cb.grid(row=0, column=1, sticky="w", padx=4, pady=4)
        self.preset_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_preset_changed())

        ttk.Label(s1, text="字型:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        font_row = ttk.Frame(s1)
        font_row.grid(row=1, column=1, sticky="w", padx=4, pady=4)
        self.font_var = tk.StringVar(value=(self._fonts[0][0] if self._fonts else ""))
        self.font_cb = ttk.Combobox(font_row, textvariable=self.font_var, state="readonly",
                                    width=20, values=[f[0] for f in self._fonts])
        self.font_cb.pack(side="left")
        ttk.Button(font_row, text="自訂字型檔…", command=self._pick_custom_font).pack(side="left", padx=(4, 0))
        self._custom_font: tuple[str, int] | None = None  # (path, index)

        # 顏色 / 描邊
        ttk.Label(s1, text="文字顏色:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.fill_btn = ColorButton(s1, "#FFFFFF")
        self.fill_btn.grid(row=2, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(s1, text="描邊顏色:").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        stroke_row = ttk.Frame(s1)
        stroke_row.grid(row=3, column=1, sticky="w", padx=4, pady=4)
        self.stroke_on_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(stroke_row, text="描邊", variable=self.stroke_on_var,
                        command=self._on_stroke_toggle).pack(side="left", padx=(0, 6))
        self.stroke_btn = ColorButton(stroke_row, "#000000")
        self.stroke_btn.pack(side="left")
        ttk.Label(stroke_row, text="粗細%").pack(side="left", padx=(10, 2))
        self.stroke_ratio_var = tk.DoubleVar(value=12.0)
        ttk.Spinbox(stroke_row, from_=1, to=40, increment=1, width=5,
                    textvariable=self.stroke_ratio_var).pack(side="left")

        # 大小
        s2 = ttk.LabelFrame(page, text="文字大小")
        s2.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.size_mode_var = tk.StringVar(value="auto")
        ttk.Radiobutton(s2, text="自動(依圖寬比例)", value="auto", variable=self.size_mode_var,
                        command=self._on_size_mode_changed).grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.size_percent_var = tk.DoubleVar(value=4.0)
        self.size_percent_spin = ttk.Spinbox(s2, from_=1, to=20, increment=0.5, width=6,
                                              textvariable=self.size_percent_var)
        self.size_percent_spin.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(s2, text="% 圖寬").grid(row=0, column=2, sticky="w")

        ttk.Radiobutton(s2, text="固定像素", value="fixed", variable=self.size_mode_var,
                        command=self._on_size_mode_changed).grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.size_px_var = tk.IntVar(value=48)
        self.size_px_spin = ttk.Spinbox(s2, from_=8, to=400, increment=2, width=6,
                                        textvariable=self.size_px_var)
        self.size_px_spin.grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(s2, text="px").grid(row=1, column=2, sticky="w")

        # 位置
        s3 = ttk.LabelFrame(page, text="位置")
        s3.grid(row=2, column=0, sticky="ew")
        self.position_var = tk.StringVar(value="左下")
        grid_pos = ttk.Frame(s3)
        grid_pos.grid(row=0, column=0, padx=6, pady=6)
        layout = {"左上": (0, 0), "右上": (0, 1), "左下": (1, 0), "右下": (1, 1)}
        for name, (r, c) in layout.items():
            ttk.Radiobutton(grid_pos, text=name, value=name,
                            variable=self.position_var).grid(row=r, column=c, sticky="w", padx=10, pady=2)
        ttk.Label(s3, text="邊距%").grid(row=0, column=1, sticky="w", padx=(20, 2))
        self.margin_var = tk.DoubleVar(value=3.0)
        ttk.Spinbox(s3, from_=0, to=25, increment=0.5, width=6,
                    textvariable=self.margin_var).grid(row=0, column=2, sticky="w")

        # 我的樣式(把以上所有設定存成具名樣式,下次一鍵套用)
        s4 = ttk.LabelFrame(page, text="我的樣式")
        s4.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(s4, text="已存樣式:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.saved_style_var = tk.StringVar()
        self.saved_style_cb = ttk.Combobox(s4, textvariable=self.saved_style_var,
                                           state="readonly", width=22, values=[])
        self.saved_style_cb.grid(row=0, column=1, sticky="w", padx=4, pady=6)
        ttk.Button(s4, text="套用", command=self._apply_saved_style).grid(row=0, column=2, padx=2, pady=6)
        ttk.Button(s4, text="另存新樣式…", command=self._save_new_style).grid(row=0, column=3, padx=2, pady=6)
        ttk.Button(s4, text="覆蓋更新", command=self._overwrite_style).grid(row=0, column=4, padx=2, pady=6)
        ttk.Button(s4, text="刪除", command=self._delete_style).grid(row=0, column=5, padx=2, pady=6)

        self._on_preset_changed()
        self._on_size_mode_changed()
        self._refresh_saved_styles()
        return page

    # ---- Tab 4: 預覽 / 輸出 ----
    def _build_output_tab(self, parent):
        page = ttk.Frame(parent)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)

        # 上半:縮圖牆(左) | 大圖預覽(右),可拖曳分隔
        split = ttk.Panedwindow(page, orient="horizontal")
        split.grid(row=0, column=0, sticky="nsew")

        left = ttk.LabelFrame(split, text="縮圖(點選檢視大圖)")
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        thumb_bar = ttk.Frame(left)
        thumb_bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(thumb_bar, text="重新整理縮圖", command=self._refresh_thumbs).pack(side="left", padx=4, pady=2)
        self.thumb_status = tk.StringVar(value="")
        ttk.Label(thumb_bar, textvariable=self.thumb_status, foreground="#1976d2").pack(side="left", padx=(4, 0))
        self.thumb_grid = ThumbnailGrid(left, on_select=self._on_thumb_click)
        self.thumb_grid.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        split.add(left, weight=1)

        right = ttk.LabelFrame(split, text="預覽")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        nav = ttk.Frame(right)
        nav.grid(row=0, column=0, sticky="ew", pady=2)
        ttk.Button(nav, text="⏮ 第一張", command=lambda: self._nav("first")).pack(side="left", padx=2)
        ttk.Button(nav, text="◀ 上一張", command=lambda: self._nav("prev")).pack(side="left", padx=2)
        self.nav_pos_var = tk.StringVar(value="- / -")
        ttk.Label(nav, textvariable=self.nav_pos_var, width=9, anchor="center").pack(side="left", padx=4)
        ttk.Button(nav, text="下一張 ▶", command=lambda: self._nav("next")).pack(side="left", padx=2)
        ttk.Button(nav, text="最後一張 ⏭", command=lambda: self._nav("last")).pack(side="left", padx=2)
        ttk.Button(nav, text="更新預覽", command=self._update_preview).pack(side="left", padx=(12, 2))
        self.preview_label = tk.Label(right, background="#2b2b2b", anchor="center",
                                      text="(加入照片後,點縮圖或按「更新預覽」)", foreground="#ccc")
        self.preview_label.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.preview_info = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.preview_info, foreground="#1976d2").grid(
            row=2, column=0, sticky="w", padx=6, pady=(0, 4))
        split.add(right, weight=2)

        # 下半:輸出設定
        out_box = ttk.LabelFrame(page, text="輸出設定")
        out_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        out_box.columnconfigure(1, weight=1)

        ttk.Label(out_box, text="輸出資料夾:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.out_dir_var = tk.StringVar()
        ttk.Entry(out_box, textvariable=self.out_dir_var).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(out_box, text="瀏覽…", command=self._pick_out_dir).grid(row=0, column=2, padx=4, pady=4)

        name_row = ttk.Frame(out_box)
        name_row.grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=4)
        self.naming_var = tk.StringVar(value="same")
        ttk.Radiobutton(name_row, text="同原始檔名", value="same", variable=self.naming_var,
                        command=self._on_naming_changed).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(name_row, text="自訂前綴 + 流水號", value="custom", variable=self.naming_var,
                        command=self._on_naming_changed).pack(side="left")
        ttk.Label(name_row, text="前綴").pack(side="left", padx=(12, 2))
        self.prefix_var = tk.StringVar(value="photo_")
        self.prefix_entry = ttk.Entry(name_row, textvariable=self.prefix_var, width=12, state="disabled")
        self.prefix_entry.pack(side="left")
        ttk.Label(name_row, text="起始").pack(side="left", padx=(10, 2))
        self.start_idx_var = tk.IntVar(value=1)
        self.start_spin = ttk.Spinbox(name_row, from_=0, to=999999, width=6,
                                      textvariable=self.start_idx_var, state="disabled")
        self.start_spin.pack(side="left")
        ttk.Label(name_row, text="位數").pack(side="left", padx=(10, 2))
        self.digits_var = tk.IntVar(value=3)
        self.digits_spin = ttk.Spinbox(name_row, from_=1, to=8, width=4,
                                       textvariable=self.digits_var, state="disabled")
        self.digits_spin.pack(side="left")

        run_row = ttk.Frame(out_box)
        run_row.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=(4, 6))
        run_row.columnconfigure(2, weight=1)
        self.run_btn = ttk.Button(run_row, text="開始批次輸出", command=self._run_batch)
        self.run_btn.grid(row=0, column=0, padx=(0, 12))
        ttk.Label(run_row, text="進度:").grid(row=0, column=1)
        self.progress = ttk.Progressbar(run_row, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=2, sticky="ew", padx=(4, 8))
        self.status_var = tk.StringVar(value="待命")
        ttk.Label(run_row, textvariable=self.status_var, width=24).grid(row=0, column=3)

        log_box = ttk.LabelFrame(page, text="日誌")
        log_box.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        log_box.columnconfigure(0, weight=1)
        self.log_text = ScrolledText(log_box, height=5, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        return page

    # ----------------------------------------------------------- handlers --
    def _on_tab_changed(self, _e=None):
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return
        if idx == 3:
            self._enter_preview_tab()

    def _on_mode_changed(self):
        mode = self.mode_var.get()
        target = {"uniform": self._frame_uniform, "table": self._frame_table,
                  "meta": self._frame_meta}[mode]
        target.tkraise()

    def _on_sep_changed(self):
        is_custom = self.sep_var.get() == "自訂"
        self.sep_custom_entry.configure(state="normal" if is_custom else "disabled")

    def _date_opts(self) -> dict:
        return {"era": self.date_era_var.get(), "pattern": self.date_pattern_var.get()}

    def _update_date_example(self):
        sample = datetime(2026, 6, 9, 14, 30, 5)
        try:
            txt = format_date(sample, self.date_pattern_var.get(), self.date_era_var.get())
        except Exception:
            txt = ""
        self.date_example_var.set(f"範例:{txt}")

    def _on_preset_changed(self):
        name = self.preset_var.get()
        spec = PRESETS.get(name)
        is_custom = name == "自訂"
        if spec is not None:
            fill, stroke = spec
            self.fill_btn.set_color(fill)
            if stroke is None:
                self.stroke_on_var.set(False)
            else:
                self.stroke_on_var.set(True)
                self.stroke_btn.set_color(stroke)
        # 自訂時開放手動調色;預設樣式時鎖住(改色請選「自訂」)
        self.fill_btn.set_enabled(is_custom)
        self.stroke_btn.set_enabled(is_custom and self.stroke_on_var.get())
        self._on_stroke_toggle()

    def _on_stroke_toggle(self):
        on = self.stroke_on_var.get()
        is_custom = self.preset_var.get() == "自訂"
        self.stroke_btn.set_enabled(on and is_custom)

    def _on_size_mode_changed(self):
        auto = self.size_mode_var.get() == "auto"
        self.size_percent_spin.configure(state="normal" if auto else "disabled")
        self.size_px_spin.configure(state="disabled" if auto else "normal")

    def _on_naming_changed(self):
        custom = self.naming_var.get() == "custom"
        st = "normal" if custom else "disabled"
        self.prefix_entry.configure(state=st)
        self.start_spin.configure(state=st)
        self.digits_spin.configure(state=st)

    def _pick_custom_font(self):
        path = filedialog.askopenfilename(
            title="選擇字型檔",
            filetypes=[("字型檔", "*.ttf *.ttc *.otf"), ("所有檔案", "*.*")])
        if not path:
            return
        self._custom_font = (path, 0)
        name = f"自訂:{Path(path).name}"
        vals = list(self.font_cb["values"])
        # 移除舊的自訂項再加入
        vals = [v for v in vals if not v.startswith("自訂:")]
        vals.append(name)
        self.font_cb.configure(values=vals)
        self.font_var.set(name)

    # ---- 照片清單 ----
    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="選擇照片",
            filetypes=[("影像檔", " ".join(f"*{e}" for e in IMAGE_EXTS)), ("所有檔案", "*.*")])
        self._append_photos(paths)

    def _add_folder(self):
        """可連續選取多個資料夾:選一個加一個,按取消結束。"""
        picked = 0
        last_dir = ""
        while True:
            title = ("選擇照片資料夾(可連續選多個,取消結束)"
                     if picked == 0 else f"已加 {picked} 個資料夾,繼續選下一個(取消結束)")
            d = filedialog.askdirectory(title=title, initialdir=last_dir or None)
            if not d:
                break
            last_dir = str(Path(d).parent)
            base = Path(d)
            it = base.rglob("*") if self.recursive_var.get() else base.glob("*")
            found = sorted(str(p) for p in it
                           if p.suffix.lower() in IMAGE_EXTS and p.is_file())
            self._append_photos(found)
            picked += 1
        if picked:
            self._log(f"共處理 {picked} 個資料夾")

    def _append_photos(self, paths):
        existing = set(self.photos)
        added = 0
        for p in paths:
            if p not in existing:
                self.photos.append(p)
                self.photo_list.insert("end", Path(p).name)
                existing.add(p)
                added += 1
        self._update_count()
        if added:
            self._log(f"加入 {added} 張照片")

    def _remove_selected(self):
        sel = list(self.photo_list.curselection())
        for i in reversed(sel):
            self.photo_list.delete(i)
            del self.photos[i]
        self._update_count()

    def _clear_photos(self):
        self.photos.clear()
        self.photo_list.delete(0, "end")
        self._update_count()

    def _update_count(self):
        n = len(self.photos)
        self.count_var.set("尚未加入照片" if n == 0 else f"共 {n} 張照片")

    # ---- 對照表 ----
    def _pick_table(self):
        path = filedialog.askopenfilename(
            title="選擇對照表",
            filetypes=[("表格檔", "*.xlsx *.xlsm *.csv"), ("所有檔案", "*.*")])
        if not path:
            return
        self.table_path_var.set(path)
        ext = Path(path).suffix.lower()
        if ext in (".xlsx", ".xlsm"):
            try:
                sheets = list_sheets(path)
            except Exception as e:
                messagebox.showerror("讀取失敗", str(e))
                return
            self.sheet_cb.configure(values=sheets, state="readonly")
            if sheets:
                self.sheet_var.set(sheets[0])
        else:
            self.sheet_cb.configure(values=[], state="disabled")
            self.sheet_var.set("")
        self._reload_table()

    def _reload_table(self):
        path = self.table_path_var.get()
        if not path:
            return
        try:
            headers, rows = read_table(path, self.sheet_var.get() or None)
        except Exception as e:
            messagebox.showerror("讀取對照表失敗", f"{e}\n\n{traceback.format_exc()}")
            return
        self._table_headers = headers
        self._table_rows = rows
        self.name_col_cb.configure(values=headers)
        self.text_col_cb.configure(values=headers)
        # 智慧預設欄位
        self._auto_pick_col(self.name_col_var, headers, ("照片名稱", "檔名", "檔案", "name", "filename", "photo"))
        self._auto_pick_col(self.text_col_var, headers, ("文字", "日期", "註記", "內容", "text", "date", "label"))
        self._rebuild_mapping()
        self.table_status.set(f"已載入 {len(rows)} 列、{len(headers)} 欄")

    @staticmethod
    def _auto_pick_col(var, headers, keywords):
        if var.get() in headers:
            return
        for h in headers:
            hl = str(h).lower()
            if any(k.lower() in hl for k in keywords):
                var.set(h)
                return
        if headers and not var.get():
            var.set(headers[0])

    def _rebuild_mapping(self):
        headers = getattr(self, "_table_headers", None)
        rows = getattr(self, "_table_rows", None)
        nc, tc = self.name_col_var.get(), self.text_col_var.get()
        if not headers or nc not in headers or tc not in headers:
            self._mapping = None
            return
        try:
            self._mapping = build_mapping(headers, rows, nc, tc)
            self.table_status.set(f"對照表就緒:{len(rows)} 列,索引 {len(self._mapping)} 筆")
        except Exception as e:
            self._mapping = None
            messagebox.showerror("建立對照失敗", str(e))

    # ---- 輸出資料夾 ----
    def _pick_out_dir(self):
        d = filedialog.askdirectory(title="選擇輸出資料夾")
        if d:
            self.out_dir_var.set(d)

    # ------------------------------------------------------------- 樣式收集 --
    def _resolve_font(self) -> tuple[str, int]:
        name = self.font_var.get()
        if name.startswith("自訂:") and self._custom_font:
            return self._custom_font
        for fn, path, idx in self._fonts:
            if fn == name:
                return path, idx
        if self._fonts:
            return self._fonts[0][1], self._fonts[0][2]
        raise RuntimeError("系統找不到可用字型,請用「自訂字型檔」選一個 .ttf/.ttc")

    def _build_style(self) -> TextStyle:
        path, idx = self._resolve_font()
        stroke = self.stroke_btn.color() if self.stroke_on_var.get() else None
        return TextStyle(
            font_path=path,
            font_index=idx,
            preset=self.preset_var.get(),
            fill=self.fill_btn.color(),
            stroke=stroke,
            stroke_ratio=max(0.01, self.stroke_ratio_var.get() / 100.0),
            size_mode=self.size_mode_var.get(),
            size_percent=self.size_percent_var.get(),
            size_px=self.size_px_var.get(),
            position=self.position_var.get(),
            margin_percent=self.margin_var.get(),
        )

    # ------------------------------------------------------------- 我的樣式 --
    def _collect_style_dict(self) -> dict:
        """把目前樣式分頁的所有設定收成可序列化的 dict。"""
        path, idx = self._resolve_font()
        return {
            "font_name": self.font_var.get(),
            "font_path": path,
            "font_index": idx,
            "preset": self.preset_var.get(),
            "fill": self.fill_btn.color(),
            "stroke_on": bool(self.stroke_on_var.get()),
            "stroke": self.stroke_btn.color(),
            "stroke_ratio": float(self.stroke_ratio_var.get()),
            "size_mode": self.size_mode_var.get(),
            "size_percent": float(self.size_percent_var.get()),
            "size_px": int(self.size_px_var.get()),
            "position": self.position_var.get(),
            "margin_percent": float(self.margin_var.get()),
        }

    def _apply_style_dict(self, d: dict):
        """把存下來的樣式 dict 套回 UI。"""
        name = d.get("font_name", "")
        known = [f[0] for f in self._fonts]
        if name in known:
            self.font_var.set(name)
        elif d.get("font_path"):
            # 還原自訂字型
            self._custom_font = (d["font_path"], int(d.get("font_index", 0)))
            disp = name if name.startswith("自訂:") else f"自訂:{Path(d['font_path']).name}"
            vals = [v for v in list(self.font_cb["values"]) if not v.startswith("自訂:")]
            vals.append(disp)
            self.font_cb.configure(values=vals)
            self.font_var.set(disp)
        self.preset_var.set(d.get("preset", "自訂"))
        self.fill_btn.set_color(d.get("fill", "#FFFFFF"))
        self.stroke_on_var.set(bool(d.get("stroke_on", True)))
        self.stroke_btn.set_color(d.get("stroke", "#000000"))
        self.stroke_ratio_var.set(float(d.get("stroke_ratio", 12.0)))
        self.size_mode_var.set(d.get("size_mode", "auto"))
        self.size_percent_var.set(float(d.get("size_percent", 4.0)))
        self.size_px_var.set(int(d.get("size_px", 48)))
        self.position_var.set(d.get("position", "左下"))
        self.margin_var.set(float(d.get("margin_percent", 3.0)))
        # 還原啟用狀態(顏色鈕鎖定 / 大小欄位)
        self._on_preset_changed()
        self._on_size_mode_changed()

    @staticmethod
    def _load_styles_file() -> dict:
        if STYLES_PATH.exists():
            try:
                data = json.loads(STYLES_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save_styles_file(self, data: dict):
        try:
            STYLES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception as e:
            messagebox.showerror("存檔失敗", f"無法寫入樣式檔:\n{e}")

    def _refresh_saved_styles(self, select: str | None = None):
        data = self._load_styles_file()
        names = sorted(data.keys())
        self.saved_style_cb.configure(values=names)
        if select and select in names:
            self.saved_style_var.set(select)
        elif self.saved_style_var.get() not in names:
            self.saved_style_var.set(names[0] if names else "")

    def _apply_saved_style(self):
        name = self.saved_style_var.get()
        if not name:
            messagebox.showinfo("提示", "目前沒有已存樣式")
            return
        data = self._load_styles_file()
        if name in data:
            self._apply_style_dict(data[name])
            self._log(f"已套用樣式「{name}」")

    def _save_new_style(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("另存樣式", "樣式名稱:", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        data = self._load_styles_file()
        if name in data and not messagebox.askyesno("覆蓋", f"樣式「{name}」已存在,要覆蓋?"):
            return
        data[name] = self._collect_style_dict()
        self._save_styles_file(data)
        self._refresh_saved_styles(select=name)
        self._log(f"已儲存樣式「{name}」")

    def _overwrite_style(self):
        name = self.saved_style_var.get()
        if not name:
            messagebox.showinfo("提示", "請先選一個要覆蓋的樣式,或用「另存新樣式」")
            return
        if not messagebox.askyesno("覆蓋更新", f"用目前設定覆蓋樣式「{name}」?"):
            return
        data = self._load_styles_file()
        data[name] = self._collect_style_dict()
        self._save_styles_file(data)
        self._log(f"已更新樣式「{name}」")

    def _delete_style(self):
        name = self.saved_style_var.get()
        if not name:
            return
        if not messagebox.askyesno("刪除", f"刪除樣式「{name}」?"):
            return
        data = self._load_styles_file()
        data.pop(name, None)
        self._save_styles_file(data)
        self._refresh_saved_styles()
        self._log(f"已刪除樣式「{name}」")

    def _text_params(self) -> dict:
        """在 UI 執行緒蒐集決定文字的所有設定,打包成純資料。

        背景批次執行緒只能拿這份快照(不可直接讀 Tk widget/變數)。
        """
        return {
            "mode": self.mode_var.get(),
            "uniform": self.uniform_text.get("1.0", "end-1c"),
            "mapping": self._mapping,
            "meta_fields": [key for _lbl, key in META_FIELDS if self.meta_vars[key].get()],
            "sep": self._meta_sep(),
            "date_opts": self._date_opts(),
        }

    @staticmethod
    def _text_for(photo_path: str, params: dict) -> str:
        """純函式:依快照算出某張照片要加的文字(可在背景執行緒呼叫)。"""
        mode = params["mode"]
        if mode == "uniform":
            return params["uniform"]
        if mode == "table":
            m = params["mapping"]
            return (lookup_text(m, photo_path) or "") if m else ""
        info = read_photo_info(photo_path)
        return text_from_meta(info, params["meta_fields"], params["sep"],
                              params.get("date_opts"))

    def _compute_text(self, photo_path: str) -> str:
        return self._text_for(photo_path, self._text_params())

    def _meta_sep(self) -> str:
        lbl = self.sep_var.get()
        for name, val in SEP_OPTIONS:
            if name == lbl:
                if val is None:
                    return self.sep_custom_var.get()
                return val
        return "\n"

    # --------------------------------------------------------------- 預覽 --
    def _enter_preview_tab(self):
        """切到預覽分頁:照片清單有變動才重建縮圖,否則沿用。"""
        sig = tuple(self.photos)
        if sig != self._thumb_sig:
            self._refresh_thumbs()
        elif self.photos and self._preview_index < 0:
            self._show_index(0)

    def _nav(self, where: str):
        n = len(self.photos)
        if n == 0:
            messagebox.showinfo("提示", "請先到「照片」分頁加入照片")
            return
        cur = self._preview_index if self._preview_index >= 0 else 0
        target = {"first": 0, "last": n - 1,
                  "prev": max(0, cur - 1), "next": min(n - 1, cur + 1)}[where]
        self._show_index(target)

    def _on_thumb_click(self, i: int):
        self._show_index(i)

    def _show_index(self, i: int):
        n = len(self.photos)
        if not (0 <= i < n):
            return
        self._preview_index = i
        self.nav_pos_var.set(f"{i + 1} / {n}")
        self.thumb_grid.select(i)
        self._render_preview(self.photos[i])

    def _render_preview(self, path: str):
        try:
            style = self._build_style()
            img = load_for_annotation(path)
            text = self._compute_text(path)
            annotate_image(img, text, style)
        except Exception as e:
            messagebox.showerror("預覽失敗", f"{e}\n\n{traceback.format_exc()}")
            return
        self.preview_label.update_idletasks()
        avail_w = max(self.preview_label.winfo_width() - 8, 400)
        avail_h = max(self.preview_label.winfo_height() - 8, 320)
        disp = img.copy()
        disp.thumbnail((avail_w, avail_h), Image.LANCZOS)
        self._preview_imgtk = ImageTk.PhotoImage(disp)
        self.preview_label.configure(image=self._preview_imgtk, text="")
        note = "(此張無文字)" if not text else ""
        self.preview_info.set(f"{Path(path).name} — 原圖 {img.width}×{img.height} {note}".strip())

    def _update_preview(self):
        """重新渲染目前這張(設定改了之後按此即時更新大圖)。"""
        if not self.photos:
            messagebox.showinfo("提示", "請先到「照片」分頁加入照片")
            return
        i = self._preview_index if self._preview_index >= 0 else 0
        self._show_index(i)

    def _refresh_thumbs(self):
        """(重新)產生整牆縮圖。背景逐張渲染,用世代計數作廢過期的工作。"""
        photos = list(self.photos)
        self._thumb_sig = tuple(photos)
        self.thumb_grid.set_photos(photos)
        self._preview_index = -1
        self.nav_pos_var.set("- / -" if not photos else f"1 / {len(photos)}")
        if not photos:
            self.thumb_status.set("尚無照片")
            self.preview_label.configure(image="", text="(加入照片後,點縮圖或按「更新預覽」)")
            return
        try:
            style = self._build_style()
        except Exception:
            style = None
        params = self._text_params()
        self._thumb_gen += 1
        gen = self._thumb_gen
        total = len(photos)
        self.thumb_status.set(f"產生縮圖 0/{total}…")

        def worker():
            for i, src in enumerate(photos):
                if gen != self._thumb_gen:
                    return
                try:
                    text = self._text_for(src, params) if style is not None else ""
                    img = render_thumbnail(src, THUMB_BOX, text, style)
                except Exception:
                    img = None
                if img is not None:
                    self._post(lambda i=i, img=img: self.thumb_grid.set_thumb(i, img))
                done = i + 1
                if done % 4 == 0 or done == total:
                    self._post(lambda d=done: self.thumb_status.set(
                        f"產生縮圖 {d}/{total}…" if d < total else f"縮圖完成,共 {d} 張"))
            self._post(lambda: self._after_thumbs(gen))

        threading.Thread(target=worker, daemon=True).start()

    def _after_thumbs(self, gen: int):
        if gen != self._thumb_gen:
            return
        if self.photos and self._preview_index < 0:
            self._show_index(0)

    def _post(self, fn):
        """從背景執行緒安排到 UI 執行緒;若視窗已銷毀則靜默略過。"""
        try:
            self.root.after(0, fn)
        except (tk.TclError, RuntimeError):
            pass

    # --------------------------------------------------------------- 批次 --
    def _run_batch(self):
        if self._running:
            messagebox.showinfo("處理中", "目前已有作業在執行")
            return
        if not self.photos:
            messagebox.showwarning("缺少照片", "請先加入照片")
            return
        out_dir = self.out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("缺少輸出資料夾", "請選擇輸出資料夾")
            return
        out_path = Path(out_dir)
        try:
            out_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("無法建立輸出資料夾", str(e))
            return

        # 防止輸出覆蓋原始檔(同名同夾)
        same_dir_risk = any(Path(p).resolve().parent == out_path.resolve()
                            and self.naming_var.get() == "same" for p in self.photos)
        if same_dir_risk:
            if not messagebox.askyesno(
                    "確認", "輸出資料夾與部分原始照片相同,且使用「同原始檔名」,\n"
                            "將直接覆蓋原圖。確定繼續?"):
                return

        try:
            style = self._build_style()
        except Exception as e:
            messagebox.showerror("樣式錯誤", str(e))
            return

        if self.mode_var.get() == "table" and not self._mapping:
            if not messagebox.askyesno("對照表未就緒", "尚未載入對照表,沒有文字的照片只會原樣輸出。繼續?"):
                return

        naming = self.naming_var.get()
        prefix = self.prefix_var.get()
        start = self.start_idx_var.get()
        digits = self.digits_var.get()
        text_params = self._text_params()  # 在 UI 執行緒先快照

        self._running = True
        self.run_btn.configure(state="disabled")
        self.progress.configure(value=0, maximum=len(self.photos))
        self.status_var.set("開始處理…")
        self._log(f"開始批次輸出 {len(self.photos)} 張 → {out_dir}")

        photos = list(self.photos)

        def worker():
            ok = 0
            no_text = 0
            errors: list[str] = []
            for i, src in enumerate(photos):
                try:
                    text = self._text_for(src, text_params)
                    img = load_for_annotation(src)
                    if text:
                        annotate_image(img, text, style)
                    else:
                        no_text += 1
                    name = output_filename(src, naming, prefix, start + i, digits)
                    dest = str(out_path / name)
                    save_image(img, dest)
                    ok += 1
                except Exception as e:
                    errors.append(f"{Path(src).name}: {e}")
                self.root.after(0, lambda i=i, src=src: self._on_batch_progress(i + 1, src))
            self.root.after(0, lambda: self._on_batch_done(ok, no_text, errors))

        threading.Thread(target=worker, daemon=True).start()

    def _on_batch_progress(self, done: int, src: str):
        self.progress.configure(value=done)
        self.status_var.set(f"處理中 {done}/{len(self.photos)}")

    def _on_batch_done(self, ok: int, no_text: int, errors: list[str]):
        self._running = False
        self.run_btn.configure(state="normal")
        self.status_var.set(f"完成:成功 {ok} 張")
        self._log(f"完成:成功輸出 {ok} 張" + (f",其中 {no_text} 張無文字僅複製" if no_text else ""))
        for e in errors[:20]:
            self._log(f"  失敗 — {e}")
        if errors:
            self._log(f"共 {len(errors)} 張失敗")
            messagebox.showwarning("部分失敗", f"成功 {ok} 張,失敗 {len(errors)} 張(詳見日誌)")
        else:
            messagebox.showinfo("完成", f"已成功輸出 {ok} 張照片")

    # --------------------------------------------------------------- log --
    def _log(self, msg: str):
        t = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{t}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    MainWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
