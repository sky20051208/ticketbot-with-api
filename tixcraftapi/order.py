"""Step 5: 跟隨 POST 後的 redirect，必要時輪詢 /ticket/check 等到 checkout。

成功的唯一定義：最終 URL 落在 /checkout。
非 checkout / 非排隊頁的落點一律回傳 URL 交給 runner classify —
落到 /login 會被 FSM 判成 LOGIN_FAIL terminal，不再拿死 cookie 空轉。
poll_interval / poll_max 由 runner 注入，本檔不讀 config。
"""
import re
import time

from curl_cffi import requests as cf_requests

from tixcraftapi import BASE
from tixcraftapi.errors import raise_if_blocked


def follow_order(session: cf_requests.Session, redirect_url: str, headers: dict,
                 poll_interval: float, poll_max: int) -> str | None:
    """POST 成功後被 302 到某個 URL，follow 它並判斷結果。
    - 直接到 checkout → 回 checkout URL（terminal success）
    - 到 /ticket/order → 進輪詢等 checkout
    - 其他落點 → 回落點 URL 交給 FSM classify
    """
    print(f"[ORDER] 跟隨 redirect: {redirect_url}")

    resp = session.get(redirect_url,
                       headers={**headers, "Referer": redirect_url},
                       allow_redirects=True)
    raise_if_blocked(resp, "ORDER")
    final_url = resp.url

    print(f"[ORDER] 最終 URL: {final_url} (HTTP {resp.status_code})")

    if "checkout" in final_url:
        print("[ORDER] 已到結帳頁!")
        return final_url

    if "/ticket/order" in final_url or "/order" in final_url:
        print("[ORDER] 進入 order 頁，開始輪詢等 checkout...")
        return _poll_order_loop(session, final_url, headers, poll_interval, poll_max)

    # 其他落點（login / activity / game / 未知頁）→ 交回 FSM 分類
    clean = re.sub(r'<[^>]+>', ' ', resp.text)
    print(f"[ORDER] 非預期落點（交回 FSM 分類），頁面文字前 300 字: {clean[:300]}")
    return final_url


def _poll_order_loop(session: cf_requests.Session, order_url: str, headers: dict,
                     poll_interval: float, poll_max: int) -> str | None:
    check_url = f"{BASE}/ticket/check"

    # 拓元 /ticket/check 只回 {waiting, message, time}，沒給排隊位置（已確認）。
    # time 是 server 建議的下次 polling 間隔（秒），過載時可能動態加大。
    queue_start = time.monotonic()
    next_interval = poll_interval

    for i in range(1, poll_max + 1):
        try:
            check_resp = session.get(check_url, headers={
                **headers,
                "Referer": order_url,
                "X-Requested-With": "XMLHttpRequest",
            })

            ts = time.strftime('%H:%M:%S')
            elapsed = int(time.monotonic() - queue_start)
            elapsed_str = f"{elapsed // 60}m{elapsed % 60:02d}s"

            try:
                data = check_resp.json()
                waiting = data.get("waiting", None)
                msg = data.get("message", "")
                server_interval = data.get("time")
                if isinstance(server_interval, (int, float)) and server_interval > 0:
                    next_interval = float(server_interval)
                print(f"  [{ts}] [QUEUE] #{i} | 已排 {elapsed_str} | waiting={waiting} | next={next_interval:.0f}s | {msg[:60]}")

                if waiting is False or waiting == 0:
                    loc_m = re.search(r"location\.(?:replace|href)\s*[=(]\s*['\"]([^'\"]+)", msg)
                    if loc_m:
                        target = loc_m.group(1)
                        full = target if target.startswith("http") else BASE + target
                        resp = session.get(full, headers=headers, allow_redirects=True)
                        if "checkout" in resp.url:
                            print(f"[QUEUE] #{i} 已到結帳頁: {resp.url}")
                            return resp.url
                        print(f"[QUEUE] #{i} JSON 指示跳 {full}，落點非 checkout（交回 FSM 分類）: {resp.url}")
                        return resp.url

                    resp = session.get(order_url, headers=headers, allow_redirects=True)
                    if "checkout" in resp.url:
                        print(f"[QUEUE] #{i} 已到結帳頁: {resp.url}")
                        return resp.url

                    print(f"[QUEUE] #{i} 排隊結束但沒跳到 checkout（交回 FSM 分類）: {resp.url}")
                    return resp.url

            except (ValueError, KeyError):
                body = check_resp.text[:200]
                print(f"  [{ts}] 輪詢 #{i} | HTTP {check_resp.status_code} | 非 JSON: {body}")

        except Exception as e:
            print(f"[QUEUE] #{i} 異常: {e}")

        # 尊重 server 給的 next interval（過載時會變大），fallback 注入的 poll_interval
        time.sleep(next_interval)

    print("[QUEUE] 輪詢超時")
    return None
