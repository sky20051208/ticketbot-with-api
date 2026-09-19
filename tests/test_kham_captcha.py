"""寬宏驗證碼辨識測試（ddddocr + 限制字元集）。

fixtures/kham/captcha_live_<答案>.png 是 2026-09-17 現場抓的圖，答案經人眼確認。
大部分是「ddddocr 原樣會錯、限制字元集後才對」的（9→Q、T→7、N→IV 這類），
換 ddddocr 版本或改解碼時，這組最先壞。沒裝 ddddocr 就 skip。
"""
import glob
import os
import re

import pytest

from conftest import FIXTURES

SAMPLES = sorted(glob.glob(os.path.join(FIXTURES, "kham", "captcha_*.png")))


@pytest.fixture(scope="module")
def captcha():
    try:
        from kham_api import captcha
    except Exception as e:
        pytest.skip(f"captcha 依賴未安裝: {e!r}")
    return captcha


def _answer(path):
    return re.search(r"_([0-9A-Z]{4})\.png$", path).group(1)


def test_fixtures_present():
    assert len(SAMPLES) >= 10


@pytest.mark.parametrize("path", SAMPLES, ids=_answer)
def test_recognize_live_sample(captcha, path):
    with open(path, "rb") as f:
        assert captcha.recognize(f.read()) == _answer(path)


def test_output_limited_to_charset(captcha):
    for path in SAMPLES:
        with open(path, "rb") as f:
            got = captcha.recognize(f.read())
        assert set(got) <= set(captcha.CHARSET), got


def test_bad_input_returns_empty(captcha):
    assert captcha.recognize(b"not an image") == ""
