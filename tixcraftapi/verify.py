"""Step 2: presale code 驗證頁（從 area 步驟被 redirect 過來才會用到）。"""
import json
import re

from curl_cffi import requests as cf_requests

import config
from tixcraftapi import BASE


def handle_verify(session: cf_requests.Session, verify_url: str,
                  headers: dict) -> bool:
    """處理 presale code 驗證頁。POST check-code endpoint 帶 _csrf + checkCode。"""
    res = session.get(verify_url, headers=headers)
    if res.status_code != 200:
        print(f"[VERIFY] GET 失敗 HTTP {res.status_code}")
        return False

    html = res.text

    # 抽出表單裡所有 hidden 欄位（不只 _csrf，可能還有 gameID 等）
    hidden: dict[str, str] = {}
    for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
        val_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m:
            hidden[name_m.group(1)] = val_m.group(1) if val_m else ""

    if "_csrf" not in hidden:
        print("[VERIFY] 找不到 _csrf")
        return False

    # 優先用 config 會員碼；沒填才 fallback 抓頁面【】裡的答案
    if config.PRESALE_CODE:
        answer = config.PRESALE_CODE
        print(f"[VERIFY] 使用 config 會員碼: {answer[:3]}***")
    else:
        ans_m = re.search(r'【([^】]+)】', html)
        if ans_m:
            answer = ans_m.group(1)
            print(f"[VERIFY] 頁面找到答案: {answer}")
        else:
            print("[VERIFY] 沒有 presale code，跳過")
            return False

    # 判斷 POST 目標: /activity/verify/ → /activity/check-code/
    #                  /ticket/verify/  → /ticket/check-code/
    check_url = verify_url.replace("/verify/", "/check-code/")

    payload = dict(hidden)
    payload["checkCode"] = answer

    # CSRF 同時放在 header (X-Csrf-Token) 跟 form body (_csrf)，兩處都要帶，
    # 否則 Yii 後端會接受 POST 但不 commit verified 狀態。
    ajax_headers = {
        **headers,
        "Referer": verify_url,
        "Origin": BASE,
        "X-Requested-With": "XMLHttpRequest",
        "X-Csrf-Token": hidden["_csrf"],
        "Accept": "*/*",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    # 嘗試一次 POST 直接帶 confirmed=true，省一個 RTT；
    # 若 server 不買單會回 confirm，再 fallback 走原本兩步
    payload["confirmed"] = "true"
    post_res = session.post(check_url, data=payload, headers=ajax_headers,
                            allow_redirects=False)
    try:
        data = post_res.json()
    except (ValueError, json.JSONDecodeError):
        print(f"[VERIFY] check-code 非 JSON: {post_res.text[:200]}")
        return False

    if data.get("message"):
        print(f"[VERIFY] 失敗: {data['message']}")
        return False

    if data.get("url"):
        print(f"[VERIFY] 一次過，跳轉: {data['url']}")
        return True

    if data.get("confirm"):
        print(f"[VERIFY] server 要兩步，fallback")
        payload.pop("confirmed", None)
        session.post(check_url, data=payload, headers=ajax_headers,
                     allow_redirects=False)
        payload["confirmed"] = "true"
        confirm_res = session.post(check_url, data=payload, headers=ajax_headers,
                                   allow_redirects=False)
        try:
            data2 = confirm_res.json()
        except (ValueError, json.JSONDecodeError):
            return False
        if data2.get("message"):
            print(f"[VERIFY] fallback 失敗: {data2['message']}")
            return False
        if data2.get("url"):
            print(f"[VERIFY] fallback 通過，跳轉: {data2['url']}")
        return True

    return False
