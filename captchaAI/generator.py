"""合成拓元 captcha 訓練資料。

採樣 300 張真實 captcha 統計出來的參數：
  - 尺寸固定 120x100 RGB
  - 背景純色 RGB(2, 108, 223) — std=0 每張一模一樣
  - 白字 RGB(~253, 254, 255)
  - 4 字水平範圍 x≈10..100，垂直主體 y≈30..75
  - 旋轉 ±10° 上下、y 微抖動

字母 glyph 由 extract_glyphs.py 從官方公布字型範本切出，放在 glyphs/。
"""
import random
from pathlib import Path

from PIL import Image

GLYPHS_DIR = Path(__file__).parent / "glyphs"
OUT_DIR = Path(__file__).parent / "dataset_synth"

W, H = 120, 100
BG = (2, 108, 223)
LETTERS = "abcdefghijklmnopqrstuvwxyz"

# 弱字 oversample weights — 上一輪 evaluate 顯示 l/j/y/u 錯誤率最高（取樣不足）
# 1.0 = baseline，2.0 = 出現頻率為 2 倍。太極端會壓縮其他字學習機會，2x 是穩健設定。
LETTER_WEIGHTS = {ch: 1.0 for ch in LETTERS}
for ch in "ljyu":
    LETTER_WEIGHTS[ch] = 2.0
_WEIGHTS_LIST = [LETTER_WEIGHTS[c] for c in LETTERS]

# 每字 descender 在 atlas 像素中的高度（baseline 到 atlas bbox 底部的距離）。
# 從 extract_glyphs 切出的 PNG 高度反推：
#   主體字 (a,c,e,m,n,o,r,s) atlas_h ≈ 232~237 → 設為基準 235
#   descender 字 = atlas_h - 235
# 例：g h=321 → desc=86；p h=303 → desc=68；q h=317 → desc=82；y h=321 → desc=86；
#     j h=414 但 j 有 ascender 點，descender 比較長，估 ≈ 165
DESCENDER_ATLAS = {
    "g": 86, "p": 68, "q": 82, "y": 86, "j": 165,
}
ATLAS_BODY_REF_H = 235  # 主體字 atlas 高度參考

# Baseline (captcha 像素)：所有字「主體底部」對齊這條線
Y_BASELINE = 70

# base_scale 範圍：控制整體字尺寸（主體字 captcha 高 ≈ ATLAS_BODY_REF_H * scale）
BASE_SCALE_RANGE = (0.155, 0.180)

ROT_RANGE = (-7, 7)

# 字間 gap (px)：≥1 保證不重疊
GAP_RANGE = (1, 4)

Y_JITTER = 2
X_JITTER = 1
PER_LETTER_SCALE_JITTER = 0.05

_glyph_cache: dict[str, Image.Image] = {}


def _load_glyph(ch: str) -> Image.Image:
    if ch not in _glyph_cache:
        _glyph_cache[ch] = Image.open(GLYPHS_DIR / f"{ch}.png").convert("RGBA")
    return _glyph_cache[ch]


def render_captcha(text: str | None = None,
                   rng: random.Random | None = None) -> tuple[Image.Image, str]:
    """合成一張 captcha。回傳 (PIL Image, label string)。"""
    if rng is None:
        rng = random
    if text is None:
        text = "".join(rng.choices(LETTERS, weights=_WEIGHTS_LIST, k=4))

    img = Image.new("RGB", (W, H), BG)

    base_scale = rng.uniform(*BASE_SCALE_RANGE)
    gaps = [rng.randint(*GAP_RANGE) for _ in range(len(text) - 1)]

    # 動態縮放：若 base_scale 會讓 4 字爆出 frame，自動降回去
    raw_glyphs = [_load_glyph(ch) for ch in text]
    raw_w_sum = sum(g.width for g in raw_glyphs)
    max_letter_w = W - 4 - sum(gaps)   # 左右各留 2 px 安全 padding
    if raw_w_sum * base_scale > max_letter_w:
        base_scale = max_letter_w / raw_w_sum

    prepared = []
    total_w = 0
    for ch, g in zip(text, raw_glyphs):
        s = base_scale * rng.uniform(1 - PER_LETTER_SCALE_JITTER, 1 + PER_LETTER_SCALE_JITTER)
        scaled_w = max(1, int(g.width * s))
        scaled_h = max(1, int(g.height * s))
        scaled = g.resize((scaled_w, scaled_h), Image.LANCZOS)
        prepared.append((ch, s, scaled_w, scaled_h, scaled))
        total_w += scaled_w
    total_w += sum(gaps)
    start_x = (W - total_w) // 2 + rng.randint(-2, 2)

    x_cursor = start_x
    for i, (ch, s, scaled_w, scaled_h, scaled) in enumerate(prepared):
        angle = rng.uniform(*ROT_RANGE)
        rotated = scaled.rotate(angle, resample=Image.BICUBIC, expand=True)
        dw = rotated.width - scaled_w
        dh = rotated.height - scaled_h

        descender_scaled = int(DESCENDER_ATLAS.get(ch, 0) * s)
        py_scaled_bottom = Y_BASELINE + descender_scaled
        py_rotated_top = py_scaled_bottom - scaled_h - dh // 2
        py = py_rotated_top + rng.randint(-Y_JITTER, Y_JITTER)
        px = x_cursor - dw // 2 + rng.randint(-X_JITTER, X_JITTER)

        img.paste(rotated, (px, py), rotated)

        x_cursor += scaled_w
        if i < len(gaps):
            x_cursor += gaps[i]

    return img, text


def generate_dataset(n: int = 50_000, out_dir: Path = OUT_DIR, seed: int = 42):
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    for i in range(n):
        img, label = render_captcha(rng=rng)
        # 檔名 = label + _index（label 可能重複）
        img.save(out_dir / f"{label}_{i:06d}.png")
        if (i + 1) % 5000 == 0:
            print(f"  {i+1}/{n} 已生")
    print(f"[DONE] {n} 張合成 captcha → {out_dir}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=50_000)
    p.add_argument("--preview", action="store_true",
                   help="只生 12 張預覽，不寫 dataset")
    args = p.parse_args()

    if args.preview:
        preview_dir = Path(__file__).parent / "_synth_preview"
        preview_dir.mkdir(exist_ok=True)
        rng = random.Random(0)
        for i in range(12):
            img, label = render_captcha(rng=rng)
            img.save(preview_dir / f"{i:02d}_{label}.png")
        print(f"[PREVIEW] 12 張 → {preview_dir}")
    else:
        generate_dataset(args.n)
