"""Step 4: 送出訂單表單（驗證碼錯會在內部重試）。

並行 GET 表單 + 抓驗證碼省一個 RTT；驗證碼錯時換一張，倒數第二輪起重抓表單避免 _csrf 過期。
"""
import time
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cf_requests

import config
from tixcraftapi import BASE, alerts
from tixcraftapi.captcha import fetch_captcha_image, solve_captcha
from tixcraftapi.parsing import parse_ticket_form, find_ticket_codes
from captchaAI.predict import recognize_captcha


def submit_ticket(session: cf_requests.Session,
                  ticket_url: str, headers: dict) -> str | None:
    captcha_headers = {**headers, "Referer": ticket_url}

    cached_payload: dict | None = None
    cached_code: str | None = None
    prefetched_code: str | None = None
    prefetched_img: bytes | None = None

    # 從 area 步驟掛在 session 上的 captcha prefetch（背景抓圖 + OCR 一條龍）。
    # 用過就清，避免下一個 ticket_url 誤用舊的結果。
    area_prefetch = getattr(session, "_captcha_prefetch", None)
    if area_prefetch is not None:
        session._captcha_prefetch = None

    def _timed_form_get():
        t0 = time.perf_counter()
        r = session.get(ticket_url, headers=headers)
        return r, (time.perf_counter() - t0) * 1000

    def _timed_captcha():
        t0 = time.perf_counter()
        img = fetch_captcha_image(session, captcha_headers)
        return img, (time.perf_counter() - t0) * 1000

    for round_n in range(1, config.TICKET_CAPTCHA_RETRY + 1):
        if cached_payload is None:
            t_par = time.perf_counter()
            with ThreadPoolExecutor(max_workers=2) as ex:
                form_future = ex.submit(_timed_form_get)
                # 第一輪 + 有 area prefetch：等預抓的 OCR 結果（不再 GET captcha）。
                # 其他情況：fallback 並行抓圖。
                use_prefetch = round_n == 1 and area_prefetch is not None
                if use_prefetch:
                    captcha_future = ex.submit(area_prefetch.wait, 3.0)
                else:
                    captcha_future = ex.submit(_timed_captcha)
                try:
                    res, form_ms = form_future.result()
                except Exception as e:
                    print(f"[TICKET] GET 異常: {e}")
                    return None
                captcha_result = captcha_future.result()
            par_ms = (time.perf_counter() - t_par) * 1000

            if use_prefetch:
                prefetched_code = captcha_result  # str | None
                print(f"[PERF] form GET={form_ms:.0f}ms / prefetch OCR={prefetched_code} / 並行總={par_ms:.0f}ms")
            else:
                prefetched_img, captcha_ms = captcha_result
                print(f"[PERF] form GET={form_ms:.0f}ms / captcha GET={captcha_ms:.0f}ms / 並行總={par_ms:.0f}ms")

            if res.status_code != 200:
                print(f"[TICKET] GET 失敗 HTTP {res.status_code}")
                if res.status_code == 403:
                    alerts.play_403("TICKET GET")
                    raise alerts.Blocked403("TICKET GET")
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
            print(f"[TICKET] 票區代碼: {cached_code} (parse {parse_ms:.0f}ms)")

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

        # 組裝 payload（以 cached 為基底，避免汙染）
        post_payload = dict(cached_payload)
        post_payload[f"TicketForm[ticketPrice][{cached_code}]"] = config.TICKET_AMOUNT
        post_payload[f"TicketForm[priceSize][{cached_code}]"] = config.TICKET_AMOUNT
        post_payload["TicketForm[verifyCode]"] = verify_code
        post_payload["TicketForm[agree]"] = "1"

        post_res = session.post(ticket_url, data=post_payload, headers=headers,
                                allow_redirects=False)
        status = post_res.status_code
        loc = post_res.headers.get("Location", "")
        print(f"[TICKET] 第{round_n}輪 POST: {status} -> {loc}")

        if status == 403:
            alerts.play_403("TICKET POST")
            raise alerts.Blocked403("TICKET POST")

        # 302/301 = 成功（不管導去哪，交給主程式判斷）
        if status in (301, 302) and loc:
            return loc if loc.startswith("http") else BASE + loc

        # 200 = 驗證碼錯或其他表單錯誤
        if status == 200:
            print(f"[TICKET] 驗證碼錯誤，換一張重試 ({round_n}/{config.TICKET_CAPTCHA_RETRY})")
            # 倒數第二輪起 fallback：可能 _csrf 真的過期，下一輪重抓表單
            if round_n >= config.TICKET_CAPTCHA_RETRY - 1:
                cached_payload = None
                cached_code = None
            continue

        print(f"[TICKET] 未預期狀態碼: {status}")
        return None

    print("[TICKET] 驗證碼重試耗盡")
    return None
