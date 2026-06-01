"""把拓元官方公布的 font_atlas.png 切成 26 個透明 PNG glyph（a.png ~ z.png）。

兩排佈局（user 確認）：
  row 1: a b c d e f g h i j k l m n o p q r s   (19)
  row 2: t u v w x y z                            (7)

策略（用 cv2 connected components，比 column projection robust）：
  1. 灰階 + threshold 找暗 pixel
  2. cv2.connectedComponentsWithStats 找所有連通區
  3. 過濾雜點（area 太小），但保留「點」（i / j 上方的點）暫不丟
  4. y_center 找最大 gap 把 CC 分成兩排
  5. 每排內：area 明顯小的視為「點」，attach 到 x_center 最接近的字母身體
  6. 排序、輸出透明白字 RGBA
"""
import cv2
import numpy as np
from PIL import Image
from pathlib import Path

ATLAS_PATH = Path(__file__).parent / "font_atlas.png"
OUT_DIR = Path(__file__).parent / "glyphs"

ROW1 = list("abcdefghijklmnopqrs")  # 19
ROW2 = list("tuvwxyz")               # 7

DARK_THRESHOLD = 100       # row 2 有殘影 / motion blur，threshold 必須夠嚴
ALPHA_CUTOFF = 200         # alpha 切 mask 用較寬鬆（保留 anti-aliasing 邊緣）
MIN_AREA = 50              # 過濾雜點 / 半透明殘影
OPEN_KERNEL = 3            # morphological opening 斷掉殘影細連


def main():
    assert ATLAS_PATH.exists(), f"找不到字型範本: {ATLAS_PATH}"
    gray = np.array(Image.open(ATLAS_PATH).convert("L"))
    H, W = gray.shape
    print(f"[ATLAS] {ATLAS_PATH.name} {W}x{H}")

    dark = (gray < DARK_THRESHOLD).astype(np.uint8)
    # opening 斷掉殘影細連（kernel 3 不會破壞字本體，但會切斷 ghosting 拖影）
    kernel = np.ones((OPEN_KERNEL, OPEN_KERNEL), np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, kernel)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(dark, connectivity=8)
    print(f"[CC] 原始 component 數 (含 background): {n}")

    comps = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < MIN_AREA:
            continue
        comps.append({
            "id": i, "x": x, "y": y, "w": w, "h": h, "area": area,
            "cx": x + w / 2, "cy": y + h / 2,
        })
    print(f"[CC] 過濾 area<{MIN_AREA} 後: {len(comps)} 個")

    # 分兩排：y_center 排序找最大 gap
    cys = sorted(c["cy"] for c in comps)
    gaps = [(cys[i + 1] - cys[i], i) for i in range(len(cys) - 1)]
    gaps.sort(reverse=True)
    gap_size, gap_idx = gaps[0]
    split_y = (cys[gap_idx] + cys[gap_idx + 1]) / 2
    print(f"[SPLIT] 兩排分界 y={split_y:.0f} (最大 gap={gap_size:.0f})")

    row1_comps = [c for c in comps if c["cy"] < split_y]
    row2_comps = [c for c in comps if c["cy"] >= split_y]
    print(f"[ROWS] row1={len(row1_comps)} comps, row2={len(row2_comps)} comps")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for row_letters, row_comps in [(ROW1, row1_comps), (ROW2, row2_comps)]:
        # 把點 attach 到字母身體：area 小於最大 area 30% 視為點
        max_area = max(c["area"] for c in row_comps)
        dot_thresh = max_area * 0.3
        bodies = [c for c in row_comps if c["area"] >= dot_thresh]
        dots = [c for c in row_comps if c["area"] < dot_thresh]
        for b in bodies:
            b["ids"] = [b["id"]]
        for d in dots:
            closest = min(bodies, key=lambda b: abs(b["cx"] - d["cx"]))
            closest["ids"].append(d["id"])
            # 擴大 bbox 把點包進來
            x0 = min(closest["x"], d["x"])
            y0 = min(closest["y"], d["y"])
            x1 = max(closest["x"] + closest["w"], d["x"] + d["w"])
            y1 = max(closest["y"] + closest["h"], d["y"] + d["h"])
            closest["x"], closest["y"] = x0, y0
            closest["w"], closest["h"] = x1 - x0, y1 - y0

        bodies.sort(key=lambda c: c["x"])
        label_str = "".join(row_letters)
        print(f"[ROW] {label_str}: 預期 {len(row_letters)} 字，body={len(bodies)}, dots={len(dots)}")

        if len(bodies) != len(row_letters):
            # 有 body 是「黏字」(殘影把多字連起來)。用 expected 總數反推每個 body 含幾字，
            # 內部 column projection 找 local min 切割。
            expected = len(row_letters)
            avg_w = sum(b["w"] for b in bodies) / expected
            counts = [max(1, round(b["w"] / avg_w)) for b in bodies]
            # 調整 counts 直到 sum == expected（誤差通常 ±1 內）
            while sum(counts) > expected:
                idx = max(range(len(counts)), key=lambda i: counts[i] - bodies[i]["w"] / avg_w)
                counts[idx] -= 1
            while sum(counts) < expected:
                idx = min(range(len(counts)), key=lambda i: counts[i] - bodies[i]["w"] / avg_w)
                counts[idx] += 1
            print(f"  [SPLIT] 黏字推估字數: {counts} (sum={sum(counts)})")

            new_bodies = []
            for b, n_letters in zip(bodies, counts):
                if n_letters == 1:
                    new_bodies.append(b)
                    continue
                bx, by, bw, bh = b["x"], b["y"], b["w"], b["h"]
                col_count = dark[by:by + bh, bx:bx + bw].sum(axis=0)
                splits = []
                for k in range(1, n_letters):
                    center = int(bw * k / n_letters)
                    window = max(int(bw / (n_letters * 3)), 8)
                    lo, hi = max(0, center - window), min(bw, center + window)
                    splits.append(lo + int(np.argmin(col_count[lo:hi])))
                boundaries = [0] + splits + [bw]
                for i in range(n_letters):
                    new_bodies.append({
                        "x": bx + boundaries[i], "y": by,
                        "w": boundaries[i + 1] - boundaries[i], "h": bh,
                        "ids": b["ids"],
                    })
            bodies = new_bodies
            print(f"  [SPLIT] 切完: {len(bodies)} 個 body")
            assert len(bodies) == expected, f"切後字數仍不符 {len(bodies)} != {expected}"

        for letter, b in zip(row_letters, bodies):
            x, y, w, h = b["x"], b["y"], b["w"], b["h"]
            sub_labels = labels[y:y + h, x:x + w]
            mask = np.isin(sub_labels, b["ids"])
            sub_gray = gray[y:y + h, x:x + w]

            alpha = np.zeros((h, w), dtype=np.uint8)
            alpha[mask] = (255 - sub_gray[mask]).clip(0, 255)
            alpha[sub_gray > ALPHA_CUTOFF] = 0

            rgba = np.zeros((h, w, 4), dtype=np.uint8)
            rgba[..., :3] = 255  # 白字
            rgba[..., 3] = alpha

            Image.fromarray(rgba, "RGBA").save(OUT_DIR / f"{letter}.png")
            print(f"  {letter}.png ({w}x{h})")

    print(f"\n[DONE] 26 個 glyph 已輸出到 {OUT_DIR}")


if __name__ == "__main__":
    main()
