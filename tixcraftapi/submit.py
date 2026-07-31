"""Step 4: 送出訂單表單（驗證碼錯會在內部重試）。

並行 GET 表單 + 抓驗證碼省一個 RTT；驗證碼錯時換一張，倒數第二輪起重抓表單避免 _csrf 過期。
captcha prefetch 由 runner 從 Context 取出後以參數傳入（不再從 session 摸暗渡屬性）；
ticket_amount / max_rounds 也由 runner 注入，本檔不讀 config。
"""
import time
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cf_requests

from tixcraftapi import BASE
from tixcraftapi.captcha import CaptchaPrefetch, fetch_captcha_image, solve_captcha
from tixcraftapi.errors import raise_if_blocked
from tixcraftapi.parsing import parse_ticket_form, find_ticket_codes
from captchaAI.predict import recognize_captcha


# 沒帶 WarmPool 時（測試 / 單獨呼叫）的後備：至少是常駐的，同一 process 內反覆呼叫
# 還能重用同一條 worker 的連線，比每次 `with ThreadPoolExecutor()` 現開好。
_LOCAL_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="submit-fallback")


def submit_ticket(session: cf_requests.Session, ticket_url: str, headers: dict,
                  ticket_amount, max_rounds: int,
                  prefetch: CaptchaPrefetch | None = None,
                  pool=None) -> str | None:
    captcha_headers = {**headers, "Referer": ticket_url}

    cached_payload: dict | None = None
    cached_code: str | None = None
    cached_codes: list[str] = []
    prefetched_code: str | None = None
    prefetched_img: bytes | None = None

    def _timed_form_get():
        t0 = time.perf_counter()
        r = session.get(ticket_url, headers=headers)
        return r, (time.perf_counter() - t0) * 1000

    def _timed_captcha():
        t0 = time.perf_counter()
        img = fetch_captcha_image(session, captcha_headers)
        return img, (time.perf_counter() - t0) * 1000

    for round_n in range(1, max_rounds + 1):
        if cached_payload is None:
            t_par = time.perf_counter()
            # 第一輪 + 有 area prefetch：等預抓的 OCR 結果（不再 GET captcha）。
            # 其他情況：fallback 並行抓圖。
            use_prefetch = round_n == 1 and prefetch is not None

            # **form GET 一定留在呼叫端這條 thread（主執行緒）**：curl_cffi 連線池是
            # thread-local，主執行緒那條在倒數期間一直被 ping 保持熱，丟去別的 thread
            # 等於自願重跑一次 TLS 握手（走 proxy 實測貴 0.5~3 秒）。
            # 真正需要並行的只有 captcha GET，丟給 WarmPool 的常駐 worker（連線同樣是熱的）。
            captcha_future = None
            if not use_prefetch:
                captcha_future = (pool.submit(_timed_captcha) if pool is not None
                                  else _LOCAL_POOL.submit(_timed_captcha))
            try:
                res, form_ms = _timed_form_get()
            except Exception as e:
                print(f"[TICKET] GET 異常: {e}")
                return None

            if use_prefetch:
                # prefetch 從 AREA 就開始跑，form GET 這段時間它一直在進行 → 這裡通常是 0 等待
                prefetched_code = prefetch.wait(3.0)
                par_ms = (time.perf_counter() - t_par) * 1000
                print(f"[PERF] form GET={form_ms:.0f}ms / prefetch OCR={prefetched_code} / 並行總={par_ms:.0f}ms")
            else:
                prefetched_img, captcha_ms = captcha_future.result()
                par_ms = (time.perf_counter() - t_par) * 1000
                print(f"[PERF] form GET={form_ms:.0f}ms / captcha GET={captcha_ms:.0f}ms / 並行總={par_ms:.0f}ms")

            raise_if_blocked(res, "TICKET GET")
            if res.status_code != 200:
                print(f"[TICKET] GET 失敗 HTTP {res.status_code}")
                return None

            t_parse = time.perf_counter()
            payload = parse_ticket_form(res.text)
            if "_csrf" not in str(payload):
                print("[TICKET] 找不到 _csrf，Cookie 可能過期")
                return None

            codes = find_ticket_codes(payload)
            if not codes:
                print("[TICKET] 找不到票區代碼（尚未開賣？）")
                return None
            parse_ms = (time.perf_counter() - t_parse) * 1000

            cached_payload = payload
            cached_code = codes[0]
            cached_codes = codes
            extra = f"（同頁另有 {codes[1:]}，會明確歸零不送出）" if len(codes) > 1 else ""
            print(f"[TICKET] 票區代碼: {cached_code} (parse {parse_ms:.0f}ms){extra}")

        # 取得驗證碼：優先用 area prefetch 的 OCR 結果、再來是並行 fallback 的圖、最後重抓
        verify_code = ""
        if prefetched_code:
            verify_code = prefetched_code
            prefetched_code = None
        elif prefetched_img is not None:
            verify_code = recognize_captcha(prefetched_img)
            print(f"[CAPTCHA] (parallel) 辨識: {verify_code}")
            prefetched_img = None
        if len(verify_code) != 4:
            verify_code = solve_captcha(session, captcha_headers)

        # 組裝 payload（以 cached 為基底，避免汙染）。
        # 同頁可能不只一個票區代碼（不同價格區間），只買 cached_code 這個；其餘代碼
        # 明確歸零覆蓋，不能依賴原始表單 hidden input 殘留的預設值——實測過殘留值不一定
        # 是 0，會導致明明只設定買 1 張卻多買到其他代碼那份，多花錢。
        post_payload = dict(cached_payload)
        for code in cached_codes:
            amount = ticket_amount if code == cached_code else "0"
            post_payload[f"TicketForm[ticketPrice][{code}]"] = amount
            post_payload[f"TicketForm[priceSize][{code}]"] = amount
        post_payload["TicketForm[verifyCode]"] = verify_code
        post_payload["TicketForm[agree]"] = "1"

        post_res = session.post(ticket_url, data=post_payload, headers=headers,
                                allow_redirects=False)
        status = post_res.status_code
        loc = post_res.headers.get("Location", "")
        print(f"[TICKET] 第{round_n}輪 POST: {status} -> {loc}")

        raise_if_blocked(post_res, "TICKET POST")

        # 302/301 = 成功（不管導去哪，交給 FSM classify 判斷）
        if status in (301, 302) and loc:
            return loc if loc.startswith("http") else BASE + loc

        # 200 = 驗證碼錯或其他表單錯誤
        if status == 200:
            print(f"[TICKET] 驗證碼錯誤，換一張重試 ({round_n}/{max_rounds})")
            # 倒數第二輪起 fallback：可能 _csrf 真的過期，下一輪重抓表單
            if round_n >= max_rounds - 1:
                cached_payload = None
                cached_code = None
            continue

        print(f"[TICKET] 未預期狀態碼: {status}")
        return None

    print("[TICKET] 驗證碼重試耗盡")
    return None
