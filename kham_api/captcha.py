"""寬宏驗證碼辨識 — 自訓小 ONNX（90x25 灰階 → 4 碼 x 36 類 CNN，~1.4MB）。

又準又快又小：
  - 小：kham_captcha.onnx ~1.4MB（取代 ddddocr ~10MB），吃既有的 onnxruntime，打包不變大。
  - 快：CPU 推論每張 ~1~3ms；`warmup()` 開賣前先載 session。
  - 準：對寬宏字型 val 逐字 ~98% / 全對 ~93%，搭配「送單失敗換一張重試」實際成功率 ~99.9%。

訓練腳本見 scratchpad/train_kham_captcha.py（用 ddddocr 自動標註 bootstrap + 人工裁決分歧）。
拓元用 captchaAI/predict.py（另一套自訓 CRNN+CTC），兩者不共用。
"""
import io
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

_MODEL_PATH = Path(__file__).parent / "kham_captcha.onnx"
_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_W, _H = 90, 25
_SESSION = None


def _get_session():
    global _SESSION
    if _SESSION is None:
        if not _MODEL_PATH.exists():
            raise FileNotFoundError(f"找不到 ONNX model: {_MODEL_PATH}")
        _SESSION = ort.InferenceSession(str(_MODEL_PATH), providers=["CPUExecutionProvider"])
        print(f"[CAPTCHA] 寬宏 OCR 已載入 ({_MODEL_PATH.name})")
    return _SESSION


def _preprocess(image_bytes: bytes) -> np.ndarray:
    im = Image.open(io.BytesIO(image_bytes)).convert("L").resize((_W, _H))
    arr = np.asarray(im, dtype=np.float32) / 255.0
    return arr.reshape(1, 1, _H, _W)


def warmup():
    """開賣前呼叫：載入 ONNX session + 跑一張 dummy，避免 T-0 才付初始化成本。"""
    try:
        sess = _get_session()
        sess.run(None, {sess.get_inputs()[0].name: np.zeros((1, 1, _H, _W), dtype=np.float32)})
        print("[CAPTCHA] 寬宏 OCR 已暖機")
    except Exception as e:
        print(f"[CAPTCHA] warmup 失敗（不致命）: {e!r}")


def recognize(image_bytes: bytes) -> str:
    """回 4 碼（大寫英數）。寬宏驗證碼大小寫不敏感。失敗回 ""。"""
    try:
        sess = _get_session()
        logits = sess.run(None, {sess.get_inputs()[0].name: _preprocess(image_bytes)})[0]
        idx = logits[0].argmax(axis=-1)  # [4]
        return "".join(_CHARSET[i] for i in idx)
    except Exception as e:
        print(f"[CAPTCHA] 辨識失敗: {e!r}")
        return ""
