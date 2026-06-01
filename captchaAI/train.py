"""自訓拓元 captcha OCR：4-head CNN，每個位置一個 26-class classifier。

訓練資料：
  - 合成（generator.py 產出，檔名 = label_index.png）
  - 真實 300 張（label_helper.py 產出 real_labels.json）

策略：
  Phase 1 (epoch 1..N1)  純合成 + augmentation
  Phase 2 (epoch N1..N2) 合成 + 真實 oversample，learning rate 降 10x

訓練完輸出 ONNX (tixcraft_ocr.onnx) 給 predict.py 用 onnxruntime 推論。
"""
import argparse
import json
import random
import sys
from pathlib import Path

# predict.py 載入時的 unicode print（這檔不會 import predict，但保險起見）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import numpy as np
from PIL import Image

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

HERE = Path(__file__).parent
SYNTH_DIR = HERE / "dataset_synth"
REAL_DIR = Path("D:/codehere/python/ticketBot/captchaAI/dataset")
REAL_LABELS_PATH = HERE / "real_labels.json"
OUT_ONNX = HERE / "tixcraft_ocr.onnx"
CKPT_PATH = HERE / "_train_ckpt.pt"

SEQ_LEN = 4               # 拓元 captcha 固定 4 字
NUM_CLASSES = 26          # a-z
BLANK_IDX = 26            # CTC blank
IMG_H, IMG_W = 100, 120

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# ---------- Model: CRNN + CTC ----------

class TixOCR(nn.Module):
    """CRNN: CNN backbone (壓 H 成 1 留 W 當 sequence) + BiLSTM + CTC head。

    輸出 (B, T, 27)，T = sequence length（feature 上的 width），用 CTC decode 出 4 字。
    這架構對「字位置漂移」有 invariance，避開 fixed-position 4-head 的瓶頸。
    """

    def __init__(self, num_classes: int = NUM_CLASSES, hidden: int = 128):
        super().__init__()
        # CNN backbone：3 次 H/W 同步 pool 後，再用 (H 維度) 額外 pool 把 H 壓到 1
        # input 100×120 → 50×60 → 25×30 → 12×15 →(H pool ×2)→ 6×15 →(H pool ×3 = adaptive)→ 1×15
        self.cnn = nn.Sequential(
            nn.Conv2d(3, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(inplace=True),
            nn.MaxPool2d(2),                                # 50×60
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(inplace=True),
            nn.MaxPool2d(2),                                # 25×30
            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(inplace=True),
            nn.MaxPool2d(2),                                # 12×15
            nn.Conv2d(128, 256, 3, padding=1), nn.BatchNorm2d(256), nn.ReLU(inplace=True),
            nn.MaxPool2d((2, 1)),                           # 6×15  (只壓 H)
            nn.Conv2d(256, 256, 3, padding=1), nn.BatchNorm2d(256), nn.ReLU(inplace=True),
            nn.AvgPool2d((6, 1)),                           # 1×15  (徹底壓 H, 保留 W 當 seq 長度)
        )
        # Sequence model: BiLSTM
        self.rnn = nn.LSTM(256, hidden, num_layers=2,
                            batch_first=True, bidirectional=True, dropout=0.2)
        # CTC head：輸出 26 + 1 blank
        self.fc = nn.Linear(hidden * 2, num_classes + 1)

    def forward(self, x):
        # x: (B, 3, 100, 120)
        f = self.cnn(x)                  # (B, 256, 1, T=15)
        f = f.squeeze(2).permute(0, 2, 1)  # (B, T, 256)
        out, _ = self.rnn(f)             # (B, T, 2H)
        logits = self.fc(out)            # (B, T, 27)
        return logits


def ctc_greedy_decode(logits: torch.Tensor, blank: int = BLANK_IDX) -> list[str]:
    """Greedy CTC decode：argmax 後 collapse 重複 + 移除 blank。"""
    pred = logits.argmax(dim=-1)         # (B, T)
    out: list[str] = []
    for seq in pred.cpu().tolist():
        chars = []
        prev = -1
        for c in seq:
            if c != blank and c != prev:
                chars.append(c)
            prev = c
        # 取前 SEQ_LEN 個（多餘的丟掉，少於 SEQ_LEN 也照樣回，evaluate 會記錯）
        out.append("".join(chr(ord("a") + i) for i in chars[:SEQ_LEN]))
    return out


# ---------- Dataset ----------

def _label_to_indices(label: str) -> list[int]:
    return [ord(c) - ord("a") for c in label]


def _img_to_tensor(img: Image.Image, augment: bool, rng: random.Random) -> torch.Tensor:
    if augment:
        # 隨機 gaussian blur（模擬 production captcha 的不同壓縮/縮放品質）
        if rng.random() < 0.35:
            from PIL import ImageFilter
            img = img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.3, 1.0)))

        arr = np.array(img, dtype=np.float32)
        # 加強的 brightness / contrast jitter
        brightness = rng.uniform(-15, 15)
        contrast = rng.uniform(0.88, 1.12)
        arr = np.clip((arr - 128) * contrast + 128 + brightness, 0, 255)
        # 加強 gaussian noise
        if rng.random() < 0.55:
            arr += np.random.randn(*arr.shape) * rng.uniform(1.0, 4.0)
        # 偶發 cutout：隨機小方塊變色（模擬遮蔽 / artifacts）
        if rng.random() < 0.15:
            cx = rng.randint(5, arr.shape[1] - 15)
            cy = rng.randint(5, arr.shape[0] - 15)
            cw = rng.randint(4, 10)
            ch = rng.randint(4, 10)
            fill = [rng.uniform(0, 255) for _ in range(3)]
            arr[cy:cy + ch, cx:cx + cw] = fill
        arr = np.clip(arr, 0, 255).astype(np.float32)
    else:
        arr = np.array(img, dtype=np.float32)
    arr = arr / 255.0
    arr = (arr - 0.5) / 0.5
    return torch.from_numpy(arr).permute(2, 0, 1)


class CaptchaDataset(Dataset):
    """合成 + 真實混合。真實 oversample 讓兩邊量級接近。"""

    def __init__(self, synth_files: list[Path], real_pairs: list[tuple[Path, str]],
                 real_oversample: int = 1, augment: bool = True):
        self.synth_files = synth_files
        self.real_pairs = real_pairs
        self.real_oversample = real_oversample
        self.augment = augment
        self._rng = random.Random()

    def __len__(self):
        return len(self.synth_files) + len(self.real_pairs) * self.real_oversample

    def __getitem__(self, idx):
        if idx < len(self.synth_files):
            f = self.synth_files[idx]
            label = f.name.split("_")[0]
        else:
            real_idx = (idx - len(self.synth_files)) % len(self.real_pairs)
            f, label = self.real_pairs[real_idx]
        img = Image.open(f).convert("RGB")
        if img.size != (IMG_W, IMG_H):
            img = img.resize((IMG_W, IMG_H), Image.LANCZOS)
        tensor = _img_to_tensor(img, augment=self.augment, rng=self._rng)
        target = torch.tensor(_label_to_indices(label), dtype=torch.long)
        return tensor, target


# ---------- Train / eval ----------

def evaluate(model, loader) -> tuple[float, float]:
    """量 char_acc 跟 seq_acc。CTC decode 後跟 ground truth 比。"""
    model.eval()
    total_chars = 0
    correct_chars = 0
    total_seqs = 0
    correct_seqs = 0
    with torch.no_grad():
        for x, y in loader:
            x = x.to(DEVICE)
            logits = model(x)                          # (B, T, 27)
            preds = ctc_greedy_decode(logits)          # list[str]
            for pred, target_tensor in zip(preds, y):
                target = "".join(chr(ord("a") + int(i)) for i in target_tensor.tolist())
                # 若長度不對，pad 用空字 → 全部視為錯
                if len(pred) == SEQ_LEN:
                    correct_chars += sum(p == t for p, t in zip(pred, target))
                total_chars += SEQ_LEN
                if pred == target:
                    correct_seqs += 1
                total_seqs += 1
    return correct_chars / total_chars, correct_seqs / total_seqs


def train(args):
    print(f"[DEVICE] {DEVICE}")

    # ---- 載入合成 ----
    synth_files = sorted(SYNTH_DIR.glob("*.png"))
    assert synth_files, f"沒合成資料，先跑 generator.py 寫到 {SYNTH_DIR}"
    print(f"[DATA] 合成 {len(synth_files)} 張")

    # ---- 載入真實 + label ----
    assert REAL_LABELS_PATH.exists(), f"找不到 {REAL_LABELS_PATH}，先跑 label_helper.py"
    real_labels = json.loads(REAL_LABELS_PATH.read_text(encoding="utf-8"))
    real_pairs_all = [(REAL_DIR / fn, lab) for fn, lab in real_labels.items()
                      if (REAL_DIR / fn).exists()]
    print(f"[DATA] 真實 {len(real_pairs_all)} 張帶 label")

    # ---- Split 真實：val 50 張，剩下訓練 ----
    rng = random.Random(42)
    rng.shuffle(real_pairs_all)
    val_n = min(args.val_size, len(real_pairs_all) // 2)
    val_real = real_pairs_all[:val_n]
    train_real = real_pairs_all[val_n:]
    print(f"[DATA] train_real={len(train_real)}  val_real={len(val_real)}")

    # Phase 1: 純合成
    phase1_ds = CaptchaDataset(synth_files, [], real_oversample=0, augment=True)
    phase1_loader = DataLoader(phase1_ds, batch_size=args.batch, shuffle=True,
                                num_workers=args.workers, persistent_workers=args.workers > 0)
    # Phase 2: 合成 + 真實 oversample
    real_oversample = max(1, len(synth_files) // max(len(train_real), 1) // 5)
    phase2_ds = CaptchaDataset(synth_files, train_real,
                                real_oversample=real_oversample, augment=True)
    phase2_loader = DataLoader(phase2_ds, batch_size=args.batch, shuffle=True,
                                num_workers=args.workers, persistent_workers=args.workers > 0)
    print(f"[DATA] phase2 真實 oversample x{real_oversample}")

    # Val: 純真實
    val_ds = CaptchaDataset([], val_real, real_oversample=1, augment=False)
    val_loader = DataLoader(val_ds, batch_size=args.batch, shuffle=False,
                             num_workers=0)

    # ---- Model ----
    model = TixOCR().to(DEVICE)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    # CTC loss：input log_probs (T, B, C)，target (B, S)，input_lengths (B,)、target_lengths (B,)
    ctc_loss = nn.CTCLoss(blank=BLANK_IDX, zero_infinity=True)

    best_val_seq = 0.0
    total_epochs = args.epoch_phase1 + args.epoch_phase2

    for epoch in range(1, total_epochs + 1):
        in_phase2 = epoch > args.epoch_phase1
        loader = phase2_loader if in_phase2 else phase1_loader
        if in_phase2 and epoch == args.epoch_phase1 + 1:
            for g in optimizer.param_groups:
                g["lr"] = args.lr * 0.1
            print(f"[PHASE2] 切到合成+真實，lr 降 10x → {args.lr * 0.1}")

        model.train()
        running = 0.0
        n_batches = 0
        for x, y in loader:
            x, y = x.to(DEVICE), y.to(DEVICE)
            logits = model(x)                                       # (B, T, 27)
            log_probs = F.log_softmax(logits, dim=-1).permute(1, 0, 2)  # (T, B, 27)
            B, T = logits.size(0), logits.size(1)
            input_lengths = torch.full((B,), T, dtype=torch.long, device=DEVICE)
            target_lengths = torch.full((B,), SEQ_LEN, dtype=torch.long, device=DEVICE)
            loss = ctc_loss(log_probs, y, input_lengths, target_lengths)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            running += loss.item()
            n_batches += 1
        train_loss = running / max(n_batches, 1)

        char_acc, seq_acc = evaluate(model, val_loader)
        phase = "P2" if in_phase2 else "P1"
        print(f"[{phase}] epoch {epoch:>2}/{total_epochs}  loss={train_loss:.4f}  "
              f"val char_acc={char_acc:.4f}  seq_acc={seq_acc:.4f}")

        if seq_acc > best_val_seq:
            best_val_seq = seq_acc
            torch.save({"model": model.state_dict(), "epoch": epoch,
                        "seq_acc": seq_acc}, CKPT_PATH)
            print(f"  ✓ best seq_acc，存 ckpt")

    # ---- 用最好的 ckpt export ONNX ----
    ckpt = torch.load(CKPT_PATH, map_location=DEVICE, weights_only=True)
    model.load_state_dict(ckpt["model"])
    model.eval()
    print(f"[EXPORT] 載回 best ckpt (seq_acc={ckpt['seq_acc']:.4f})")
    dummy = torch.zeros(1, 3, IMG_H, IMG_W, device=DEVICE)
    torch.onnx.export(
        model, dummy, str(OUT_ONNX),
        input_names=["image"], output_names=["logits"],
        dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=17,
    )
    print(f"[EXPORT] ONNX → {OUT_ONNX}")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, default=64)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--epoch_phase1", type=int, default=8,
                   help="純合成 epoch 數")
    p.add_argument("--epoch_phase2", type=int, default=6,
                   help="合成+真實 epoch 數")
    p.add_argument("--val_size", type=int, default=50)
    p.add_argument("--workers", type=int, default=2)
    return p.parse_args()


if __name__ == "__main__":
    train(parse_args())
