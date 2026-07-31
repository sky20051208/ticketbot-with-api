"""驗證碼抓圖 + ddddocr 辨識（帶重試）。"""
import random
import threading
import time

from curl_cffi import requests as cf_requests

import config
from tixcraftapi import BASE
from captchaAI.predict import recognize_captcha


def fetch_captcha_image(session: cf_requests.Session, headers: dict) -> bytes | None:
    """單次抓驗證碼圖片，失敗回 None。可被 thread pool 平行呼叫。"""
    try:
        resp = session.get(
            f"{BASE}/ticket/captcha",
            params={"refresh": str(random.random())},
            headers=headers,
        )
        if resp.status_code != 200:
            return None

        body = resp.content
        # 樂觀路徑：直接當 PNG/JPEG bytes
        if len(body) >= 100 and not body.startswith(b"{"):
            return body

        # JSON 分支（罕見）：拿 url 再 GET 一次
        try:
            data = resp.json()
        except ValueError:
            return None
        img_url = data.get("url", "")
        if not img_url:
            return None
        img_resp = session.get(f"{BASE}{img_url}", headers=headers)
        if img_resp.status_code != 200 or len(img_resp.content) < 100:
            return None
        return img_resp.content
    except Exception as e:
        print(f"[CAPTCHA] 抓圖異常: {e}")
        return None


def solve_captcha(session: cf_requests.Session,
                  headers: dict, max_retry: int | None = None) -> str:
    # 延遲讀 config.CAPTCHA_MAX_RETRY — 不能放在 default arg，否則 import 時就鎖死，
    # GUI 透過 load_config_override 改值就會失效（CLAUDE.md 已警告同類陷阱）。
    if max_retry is None:
        max_retry = config.CAPTCHA_MAX_RETRY

    result = "0000"
    for attempt in range(1, max_retry + 1):
        img_bytes = fetch_captcha_image(session, headers)
        if not img_bytes:
            print(f"[CAPTCHA] #{attempt} 取圖失敗")
            continue
        result = recognize_captcha(img_bytes)
        print(f"[CAPTCHA] #{attempt} 辨識: {result} ({len(result)} chars)")
        if len(result) == 4:
            return result

    print("[CAPTCHA] 重試耗盡，使用最後結果")
    return result


class CaptchaPrefetch:
    """背景 thread 同時做 GET captcha 圖 + OCR，submit_ticket 時 .wait() 取結果。

    用途：在 area 步驟啟動，把 captcha GET 從 submit 的 critical path 砍掉。

    server 端只認最後一張 captcha image，所以從 prefetch 啟動到 submit POST 之間，
    這條 session **絕對不可再 GET /ticket/captcha**，否則記憶會被覆蓋。
    """

    def __init__(self, session: cf_requests.Session, headers: dict, pool=None):
        self.code: str | None = None     # 4 字 OCR 結果（失敗為 None）
        self.done = threading.Event()
        self.started = time.monotonic()
        # 有 WarmPool 就跑在它的常駐 worker 上（連線是熱的）；沒有才自己開 thread。
        # 自開 thread = 全新 curl handle = 全新連線，走 proxy 光握手就可能 1~3 秒，
        # prefetch 常常因此趕不上 submit（見 tixcraftapi/session.py 說明）。
        if pool is not None:
            pool.submit(self._run, session, headers)
        else:
            threading.Thread(target=self._run, args=(session, headers),
                             daemon=True).start()

    def _run(self, session: cf_requests.Session, headers: dict):
        """每一步都留耗時 log —— prefetch 沒生效時要能一眼看出是卡在 GET、OCR
        還是辨識結果被判不合格（之前只有沉默的 None，完全無從排錯）。"""
        t0 = time.monotonic()
        try:
            img = fetch_captcha_image(session, headers)
            get_ms = (time.monotonic() - t0) * 1000
            if not img:
                print(f"[PREFETCH] 抓圖失敗（空回應）| GET={get_ms:.0f}ms")
                return
            t1 = time.monotonic()
            code = recognize_captcha(img)
            ocr_ms = (time.monotonic() - t1) * 1000
            if len(code) == 4:
                self.code = code
                print(f"[PREFETCH] 就緒 {code} | GET={get_ms:.0f}ms OCR={ocr_ms:.0f}ms "
                      f"總={(time.monotonic() - self.started) * 1000:.0f}ms")
            else:
                print(f"[PREFETCH] 辨識結果 {code!r} 不是 4 字，作廢 | "
                      f"GET={get_ms:.0f}ms OCR={ocr_ms:.0f}ms")
        except Exception as e:
            print(f"[PREFETCH] captcha 異常 | {(time.monotonic() - t0) * 1000:.0f}ms "
                  f"{type(e).__name__}: {e}")
        finally:
            self.done.set()

    def wait(self, timeout: float = 3.0) -> str | None:
        t0 = time.monotonic()
        if self.done.wait(timeout):
            return self.code
        print(f"[PREFETCH] 等 {(time.monotonic() - t0) * 1000:.0f}ms 仍未完成"
              f"（已跑 {(time.monotonic() - self.started) * 1000:.0f}ms）→ 放棄，改現抓")
        return None
