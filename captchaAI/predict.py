"""拓元 captcha OCR — 自訓 ONNX model 推論。

對外接口（沿用舊版，呼叫端不用改）：
  recognize_captcha(bytes) -> str         返回 4 字小寫英文（失敗回 ""）
  warmup_ocr() -> None                    提前載 + 跑一張暖機
  solve_captcha_nodriver(tab) -> str      瀏覽器模式：從 nodriver tab 抓圖辨識（async）
  get_captcha_base64_nodriver(tab) -> bytes | None
  CAPTCHA_IMAGE_ID                        captcha 圖片 DOM ID（bot.py 也用）

底層：onnxruntime + captchaAI/tixcraft_ocr.onnx（自 train.py 訓出）。
"""
import base64
import io
import time
from pathlib import Path
from typing import Optional

import numpy as np
import onnxruntime as ort
from PIL import Image

# 拓元驗證碼圖片 DOM ID（bot.py 也 import 這個）
CAPTCHA_IMAGE_ID = "TicketForm_verifyCode-image"

_MODEL_PATH = Path(__file__).parent / "tixcraft_ocr.onnx"
_IMG_H, _IMG_W = 100, 120
_SEQ_LEN = 4
_BLANK_IDX = 26          # CTC blank class (在 26 個 a-z 之外)
_BEAM_WIDTH = 6          # CTC beam search 寬度（5-10 對 4 字 sequence 足夠）
_SESSION: Optional[ort.InferenceSession] = None


def _get_session() -> ort.InferenceSession:
    """Lazy-load ONNX session（避免 import 時就讀檔，方便沒訓完 model 時也能 import）。"""
    global _SESSION
    if _SESSION is None:
        if not _MODEL_PATH.exists():
            raise FileNotFoundError(
                f"找不到 ONNX model: {_MODEL_PATH}\n"
                f"先跑 `python -m captchaAI.train` 訓練。"
            )
        # CPU EP 對 captcha 推論 <5ms，省 GPU memory
        _SESSION = ort.InferenceSession(
            str(_MODEL_PATH), providers=["CPUExecutionProvider"]
        )
        print(f"[OK] 拓元 OCR 已載入 ({_MODEL_PATH.name})")
    return _SESSION


def _log_softmax(x: np.ndarray) -> np.ndarray:
    """Numerically stable log-softmax over last axis."""
    m = x.max(axis=-1, keepdims=True)
    z = x - m
    return z - np.log(np.exp(z).sum(axis=-1, keepdims=True))


def _logsumexp2(a: float, b: float) -> float:
    """logsumexp for 2 scalars。處理 -inf。"""
    if a == float("-inf"):
        return b
    if b == float("-inf"):
        return a
    m = max(a, b)
    return m + np.log(np.exp(a - m) + np.exp(b - m))


def _ctc_beam_search(log_probs: np.ndarray, beam_width: int = _BEAM_WIDTH,
                     blank: int = _BLANK_IDX) -> list[int]:
    """Prefix CTC beam search (Hannun et al. 2014)。

    log_probs: (T, C) log-softmax'd model output。
    回 list of indices (collapsed + blank-removed sequence)。

    比 greedy 多解兩種錯誤：
      1. CTC collapse 漏字（如 jyla → jya）— beam 保留多 alignment 候選
      2. 高機率錯字字串（如 u → a）— beam 比較整 sequence joint prob，不只 per-frame argmax
    """
    T, _ = log_probs.shape
    NEG_INF = float("-inf")
    # state: prefix (tuple) → (P_ends_blank, P_ends_nonblank)  log-probs
    beam: dict[tuple, tuple[float, float]] = {(): (0.0, NEG_INF)}

    for t in range(T):
        new_beam: dict[tuple, tuple[float, float]] = {}
        row = log_probs[t]

        for prefix, (pb, pnb) in beam.items():
            p_total = _logsumexp2(pb, pnb)
            # blank: 延伸不改變 prefix
            lp_blank = row[blank]
            old_pb, old_pnb = new_beam.get(prefix, (NEG_INF, NEG_INF))
            new_beam[prefix] = (_logsumexp2(old_pb, p_total + lp_blank), old_pnb)

            # 每個 non-blank 字
            for c in range(blank):
                lp_c = row[c]
                if prefix and prefix[-1] == c:
                    # 同字 collapse：prev non-blank c + c → prefix 不變（pnb）
                    ob, onb = new_beam.get(prefix, (NEG_INF, NEG_INF))
                    new_beam[prefix] = (ob, _logsumexp2(onb, pnb + lp_c))
                    # prev blank + c → 真的加新字
                    new_pref = prefix + (c,)
                    ob, onb = new_beam.get(new_pref, (NEG_INF, NEG_INF))
                    new_beam[new_pref] = (ob, _logsumexp2(onb, pb + lp_c))
                else:
                    # 不同字 / 空 prefix：直接擴展
                    new_pref = prefix + (c,)
                    ob, onb = new_beam.get(new_pref, (NEG_INF, NEG_INF))
                    new_beam[new_pref] = (ob, _logsumexp2(onb, p_total + lp_c))

        # 修剪到 top beam_width
        scored = sorted(
            new_beam.items(),
            key=lambda kv: -_logsumexp2(kv[1][0], kv[1][1]),
        )
        beam = dict(scored[:beam_width])

    best_pref, _ = max(
        beam.items(),
        key=lambda kv: _logsumexp2(kv[1][0], kv[1][1]),
    )
    return list(best_pref)


def _preprocess(image_bytes: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if img.size != (_IMG_W, _IMG_H):
        img = img.resize((_IMG_W, _IMG_H), Image.LANCZOS)
    arr = np.array(img, dtype=np.float32) / 255.0
    arr = (arr - 0.5) / 0.5
    return arr.transpose(2, 0, 1)[None]  # (1, 3, H, W)


def recognize_captcha(image_bytes: bytes) -> str:
    """辨識 captcha 圖 bytes，回 4 字小寫英文（失敗回 ""）。"""
    try:
        session = _get_session()
    except FileNotFoundError as e:
        print(f"[ERR] {e}")
        return ""
    try:
        x = _preprocess(image_bytes)
        logits, = session.run(["logits"], {"image": x})  # (1, T, 27)
        log_probs = _log_softmax(logits[0])              # (T, 27)
        indices = _ctc_beam_search(log_probs, _BEAM_WIDTH, _BLANK_IDX)
        return "".join(chr(ord("a") + i) for i in indices[:_SEQ_LEN])
    except Exception as e:
        print(f"[ERR] OCR 推論失敗: {e}")
        return ""


def warmup_ocr() -> None:
    """提前載 session + 跑一張假圖暖機，攤掉首次推論的 lazy init。"""
    try:
        _get_session()
    except FileNotFoundError as e:
        print(f"[WARN] OCR 暖機跳過: {e}")
        return
    try:
        t0 = time.perf_counter()
        dummy = Image.new("RGB", (_IMG_W, _IMG_H), (2, 108, 223))
        buf = io.BytesIO()
        dummy.save(buf, format="PNG")
        recognize_captcha(buf.getvalue())
        print(f"[OK] OCR 引擎已暖機 ({(time.perf_counter() - t0) * 1000:.0f}ms)")
    except Exception as e:
        print(f"[WARN] OCR 暖機失敗（不致命）: {e}")


# ---------- nodriver / 瀏覽器模式抓圖介面 (bot.py 用) ----------

async def get_captcha_base64_nodriver(tab) -> Optional[bytes]:
    """從 nodriver tab 把 captcha img 畫到 canvas、回 PNG bytes。"""
    js_script = f"""
    (async function() {{
        var img = document.getElementById('{CAPTCHA_IMAGE_ID}');
        if (!img) return null;
        if (!img.complete || img.naturalWidth === 0) {{
            await new Promise(r => img.onload = r);
        }}
        var canvas = document.createElement('canvas');
        var context = canvas.getContext('2d');
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        context.drawImage(img, 0, 0);
        var dataURL = canvas.toDataURL('image/png');
        return dataURL.split(',')[1];
    }})()
    """
    try:
        base64_str = await tab.evaluate(js_script, await_promise=True)
        if base64_str:
            return base64.b64decode(base64_str)
    except Exception:
        pass
    return None


async def solve_captcha_nodriver(tab) -> str:
    image_data = await get_captcha_base64_nodriver(tab)
    if not image_data:
        return ""
    captcha_text = recognize_captcha(image_data)
    return captcha_text.strip().replace(" ", "") if captcha_text else ""
