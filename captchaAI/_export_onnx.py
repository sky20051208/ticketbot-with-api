"""從 _train_ckpt.pt 直接 export ONNX（model arch fix 過後不用重訓）。"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import torch

from captchaAI.train import TixOCR, CKPT_PATH, OUT_ONNX, IMG_H, IMG_W

device = "cuda" if torch.cuda.is_available() else "cpu"
model = TixOCR().to(device)
ckpt = torch.load(CKPT_PATH, map_location=device, weights_only=True)
model.load_state_dict(ckpt["model"])
model.eval()
print(f"載入 ckpt epoch={ckpt['epoch']} seq_acc={ckpt['seq_acc']:.4f}")

dummy = torch.zeros(1, 3, IMG_H, IMG_W, device=device)
torch.onnx.export(
    model, dummy, str(OUT_ONNX),
    input_names=["image"], output_names=["logits"],
    dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}},
    opset_version=17,
)
print(f"[EXPORT] ONNX → {OUT_ONNX}")
