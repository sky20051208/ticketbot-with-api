"""半自動標註 300 張真實拓元 captcha。

流程：
  1. 啟動時用現有 ddddocr 預測所有未標記圖（一次性，避免每張等）
  2. tkinter GUI：放大 3x 顯示圖、Entry 預填 ddddocr 答案
  3. 按 Enter：對的直接過 / 改完按 Enter 提交
  4. 自動跳下一張、每 10 張存進度（_label_progress.json）
  5. 跑完寫 real_labels.json，格式 {filename: "abcd"}

控制：
  Enter   接受目前 Entry 內容（必須剛好 4 個 a-z）
  Backspace / 編輯  正常文字輸入
  Esc     存檔退出
  ←       回上一張（修錯時用）
"""
import sys
from pathlib import Path

# 讓 `python captchaAI/label_helper.py` 直接跑也能找到 captchaAI package
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# predict.py 載入時 print 含 Unicode 符號，Windows cp950 console 會炸；先換 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import json
import tkinter as tk

from PIL import Image, ImageTk

# label_helper 是 training-time 工具，用 ddddocr 預測幫忙 pre-fill（runtime predict.py 已換成自訓 ONNX）
import ddddocr
_DDDD = ddddocr.DdddOcr(show_ad=False, beta=True)


def _ddddocr_predict(image_bytes: bytes) -> str:
    try:
        return _DDDD.classification(image_bytes).lower()
    except Exception:
        return ""

DATASET = Path("D:/codehere/python/ticketBot/captchaAI/dataset")
OUT_PATH = Path(__file__).parent / "real_labels.json"
PROGRESS_PATH = Path(__file__).parent / "_label_progress.json"

DISPLAY_SCALE = 4  # 圖放大倍數（120x100 → 480x400）


class LabelApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Tixcraft Captcha Labeler")

        assert DATASET.exists(), f"dataset 不存在: {DATASET}"
        self.files = sorted(DATASET.glob("*.png"))
        assert self.files, f"{DATASET} 內沒 PNG"

        self.labels: dict[str, str] = {}
        if PROGRESS_PATH.exists():
            self.labels = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
            print(f"[RESUME] 讀回 {len(self.labels)} 筆既有 label")

        # 一次跑完所有未標記圖的 ddddocr 預測
        print(f"[PREDICT] 跑 ddddocr 預測 {len(self.files) - len(self.labels)} 張...")
        self.predictions: dict[str, str] = {}
        for f in self.files:
            if f.name in self.labels:
                continue
            self.predictions[f.name] = _ddddocr_predict(f.read_bytes())
        print(f"[PREDICT] 完成")

        # UI
        self.img_label = tk.Label(root, bg="#202020")
        self.img_label.pack(padx=20, pady=15)

        self.status = tk.Label(root, text="", font=("Consolas", 11), fg="#888")
        self.status.pack()

        entry_frame = tk.Frame(root)
        entry_frame.pack(pady=12)
        tk.Label(entry_frame, text="→", font=("Consolas", 22)).pack(side=tk.LEFT, padx=6)
        self.entry = tk.Entry(entry_frame, font=("Consolas", 28), width=8,
                              justify="center", relief="solid", borderwidth=2)
        self.entry.pack(side=tk.LEFT)
        self.entry.focus()

        tip = tk.Label(
            root,
            text="Enter=確認  ←=回上一張  Esc=存檔退出",
            font=("Consolas", 10), fg="#666",
        )
        tip.pack(pady=4)

        self.entry.bind("<Return>", self.on_enter)
        root.bind("<Escape>", lambda e: self.save_and_exit())
        root.bind("<Left>", self.on_prev)

        # 找下一張未標記的當起點
        self.idx = 0
        while self.idx < len(self.files) and self.files[self.idx].name in self.labels:
            self.idx += 1
        self.show_current()

    def show_current(self):
        if self.idx >= len(self.files):
            self.save_and_exit()
            return
        f = self.files[self.idx]
        img = Image.open(f)
        img = img.resize((img.width * DISPLAY_SCALE, img.height * DISPLAY_SCALE),
                         Image.NEAREST)
        self.photo = ImageTk.PhotoImage(img)
        self.img_label.config(image=self.photo)

        prefill = self.labels.get(f.name) or self.predictions.get(f.name, "")
        self.entry.delete(0, tk.END)
        self.entry.insert(0, prefill)
        self.entry.select_range(0, tk.END)
        self.entry.icursor(tk.END)

        done = sum(1 for x in self.files if x.name in self.labels)
        self.status.config(
            text=f"{self.idx + 1}/{len(self.files)}  |  已標 {done}  |  {f.name}"
        )

    def on_enter(self, _event):
        text = self.entry.get().strip().lower()
        if len(text) != 4 or not text.isalpha():
            self.status.config(text=f"[需要 4 個 a-z 字母，目前: '{text}']", fg="red")
            return
        f = self.files[self.idx]
        self.labels[f.name] = text
        self.idx += 1
        # 跳過已有 label 的
        while self.idx < len(self.files) and self.files[self.idx].name in self.labels:
            self.idx += 1
        if len(self.labels) % 10 == 0:
            self.save_progress()
        self.status.config(fg="#888")
        self.show_current()

    def on_prev(self, _event):
        if self.idx > 0:
            self.idx -= 1
            self.show_current()

    def save_progress(self):
        PROGRESS_PATH.write_text(
            json.dumps(self.labels, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def save_and_exit(self):
        self.save_progress()
        OUT_PATH.write_text(
            json.dumps(self.labels, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[DONE] {len(self.labels)}/{len(self.files)} 已標 → {OUT_PATH}")
        self.root.destroy()


def main():
    root = tk.Tk()
    LabelApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
