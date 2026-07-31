"""寬宏驗證碼模型 smoke test（自訓 ONNX + onnxruntime）。

需 onnxruntime / Pillow / numpy；沒裝就 skip。樣本標註經人眼確認（2H5G）。
"""
import os
import pytest

from conftest import FIXTURES

SAMPLE = os.path.join(FIXTURES, "kham", "captcha_sample_2H5G.png")


def _load_captcha_module():
    try:
        from kham_api import captcha
        return captcha
    except Exception as e:
        pytest.skip(f"captcha 依賴未安裝: {e!r}")


def test_recognize_known_sample():
    captcha = _load_captcha_module()
    if not os.path.exists(SAMPLE):
        pytest.skip("缺 captcha 樣本 fixture")
    got = captcha.recognize(open(SAMPLE, "rb").read())
    assert got == "2H5G", f"辨識為 {got}"


def test_recognize_shape():
    """任意輸入回 4 碼大寫英數（模型輸出格式固定）。"""
    captcha = _load_captcha_module()
    if not os.path.exists(SAMPLE):
        pytest.skip("缺 captcha 樣本 fixture")
    got = captcha.recognize(open(SAMPLE, "rb").read())
    assert len(got) == 4 and got.isalnum() and got.isupper()
