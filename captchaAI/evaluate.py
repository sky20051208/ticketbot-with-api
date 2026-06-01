"""量自訓 ONNX model 在「所有 300 張真實 captcha」上的辨識率。

用 real_labels.json 當 ground truth，跑完印：
  - 整體 char accuracy (4 位置加總)
  - 整體 sequence accuracy (4 字全對)
  - per-position accuracy（看哪個位置最弱）
  - confusion matrix top 10（最常被搞混的字對）
  - 錯誤樣本前 30 個（filename / true / pred）

可跟 ddddocr baseline 對比（--ddddocr）。
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

HERE = Path(__file__).parent
REAL_DIR = Path("D:/codehere/python/ticketBot/captchaAI/dataset")
REAL_LABELS_PATH = HERE / "real_labels.json"


def _eval_set(predict_fn, pairs):
    """跑 predict_fn(bytes)->str over 所有 (path, label) pair，回 stats dict。"""
    n = len(pairs)
    char_correct = [0, 0, 0, 0]
    char_total = [0, 0, 0, 0]
    per_char_correct: Counter[str] = Counter()
    per_char_total: Counter[str] = Counter()
    seq_correct = 0
    confusion = Counter()
    errors = []
    for path, true_label in pairs:
        pred = predict_fn(path.read_bytes())
        if len(pred) != 4:
            errors.append((path.name, true_label, pred))
            for i in range(4):
                char_total[i] += 1
            for t in true_label:
                per_char_total[t] += 1
            continue
        all_ok = True
        for i, (t, p) in enumerate(zip(true_label, pred)):
            char_total[i] += 1
            per_char_total[t] += 1
            if t == p:
                char_correct[i] += 1
                per_char_correct[t] += 1
            else:
                all_ok = False
                confusion[(t, p)] += 1
        if all_ok:
            seq_correct += 1
        else:
            errors.append((path.name, true_label, pred))
    return {
        "n": n,
        "char_correct": char_correct,
        "char_total": char_total,
        "per_char_correct": per_char_correct,
        "per_char_total": per_char_total,
        "seq_correct": seq_correct,
        "confusion": confusion,
        "errors": errors,
    }


def _print_report(name: str, stats: dict):
    n = stats["n"]
    seq_acc = stats["seq_correct"] / n
    total_chars = sum(stats["char_total"])
    correct_chars = sum(stats["char_correct"])
    char_acc = correct_chars / total_chars
    print(f"\n=== {name} on {n} real captchas ===")
    print(f"  sequence acc: {stats['seq_correct']}/{n} = {seq_acc:.4f}")
    print(f"  char acc    : {correct_chars}/{total_chars} = {char_acc:.4f}")
    print(f"  per-position:")
    for i, (c, t) in enumerate(zip(stats["char_correct"], stats["char_total"])):
        print(f"    pos{i}: {c}/{t} = {c/t:.4f}")
    # per-character accuracy（按錯誤率排序，差的在前面）
    print(f"  per-character (按錯誤率排序，差的在前)：")
    chars = []
    for ch in "abcdefghijklmnopqrstuvwxyz":
        t = stats["per_char_total"].get(ch, 0)
        if t == 0:
            continue
        c = stats["per_char_correct"].get(ch, 0)
        chars.append((ch, c, t, c / t))
    chars.sort(key=lambda x: x[3])  # ascending acc
    for ch, c, t, acc in chars[:10]:
        print(f"    {ch}: {c}/{t} = {acc:.4f}")
    print(f"  top confusion (true → pred):")
    for (t, p), cnt in stats["confusion"].most_common(10):
        print(f"    {t} → {p}: {cnt}")
    print(f"  first 30 errors:")
    for fn, t, p in stats["errors"][:30]:
        print(f"    {fn}  true={t!r}  pred={p!r}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ddddocr", action="store_true",
                   help="同時跑 ddddocr baseline 對比")
    args = p.parse_args()

    assert REAL_LABELS_PATH.exists(), f"找不到 {REAL_LABELS_PATH}，先跑 label_helper.py"
    labels = json.loads(REAL_LABELS_PATH.read_text(encoding="utf-8"))
    pairs = [(REAL_DIR / fn, lab) for fn, lab in labels.items()
             if (REAL_DIR / fn).exists()]
    print(f"[DATA] {len(pairs)} 張帶 label 的真實 captcha")

    # --- 自訓 ONNX ---
    from captchaAI.predict import recognize_captcha
    stats = _eval_set(recognize_captcha, pairs)
    _print_report("Tixcraft ONNX (自訓)", stats)

    if args.ddddocr:
        try:
            import ddddocr
            ocr = ddddocr.DdddOcr(show_ad=False, beta=True)
            def ddd_predict(b: bytes) -> str:
                return ocr.classification(b).lower()
            stats2 = _eval_set(ddd_predict, pairs)
            _print_report("ddddocr baseline", stats2)
        except Exception as e:
            print(f"[WARN] ddddocr 測不了: {e}")


if __name__ == "__main__":
    main()
