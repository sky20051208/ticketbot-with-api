"""寬宏驗證碼辨識 — ddddocr + 限制字元集。

寬宏的 /pic.aspx 是 4 碼、大小寫不敏感，而且**只會出現 25 種字**：現場抓 125 張人工標註，
從沒出現過 0 1 7 I J L O Q U V Z（故意排掉容易搞混的字）。ddddocr 原樣跑會把 9 認成 q、
T 認成 7、N 認成 IV，把輸出限制在這 25 個字裡，這類錯就全部消失。

實測（2026-09-17，現場 125 張人工標註；後 49 張是規則定好之後才抓的 held-out）：
  舊的自訓 ONNX（kham_captcha.onnx，已刪）   80%
  ddddocr 原樣                               90%
  ddddocr + 限制字元集（本檔）               96.8%，held-out 49/49
剩下的錯幾乎都是「斜體 B → 3」。每張約 20ms（要拿機率表自己解碼，比原樣慢 ~13ms）。

拓元用 captchaAI/predict.py（自訓 CRNN+CTC），兩者不共用。
"""
import io

import ddddocr
import numpy as np
from PIL import Image

CHARSET = "2345689ABCDEFGHKMNPRSTWXY"
_OCR = None
_MASK = None  # 對應 ddddocr charset 的布林遮罩（index 0 是 CTC blank，要留著）


def _get_ocr():
    global _OCR
    if _OCR is None:
        _OCR = ddddocr.DdddOcr(show_ad=False)
        print("[CAPTCHA] 寬宏 OCR 已載入 (ddddocr)")
    return _OCR


def _decode(charsets: list[str], probability) -> str:
    """CTC 解碼：每個時間步只在允許的字裡挑最大 → 去連續重複 → 去 blank。"""
    global _MASK
    if _MASK is None or len(_MASK) != len(charsets):
        _MASK = np.array([c == "" or (len(c) == 1 and c.upper() in CHARSET) for c in charsets])
    prob = np.asarray(probability, dtype=np.float32).reshape(-1, len(charsets))
    best = np.where(_MASK, prob, -1.0).argmax(axis=1)
    out, last = [], 0
    for i in best:
        if i != last and i != 0:
            out.append(charsets[i])
        last = i
    return "".join(out).upper()


def warmup():
    """開賣前呼叫：載入模型 + 跑一張空白圖，避免 T-0 才付初始化成本（載入約 1 秒）。"""
    try:
        buf = io.BytesIO()
        Image.new("RGB", (90, 25), "white").save(buf, format="PNG")
        recognize(buf.getvalue())
        print("[CAPTCHA] 寬宏 OCR 已暖機")
    except Exception as e:
        print(f"[CAPTCHA] warmup 失敗（不致命）: {e!r}")


def recognize(image_bytes: bytes) -> str:
    """回辨識結果（大寫）。正常是 4 碼，長度不對由呼叫端決定要不要換一張。失敗回 ""。"""
    try:
        res = _get_ocr().classification(image_bytes, probability=True)
        return _decode(res["charsets"], res["probability"])
    except Exception as e:
        print(f"[CAPTCHA] 辨識失敗: {e!r}")
        return ""
