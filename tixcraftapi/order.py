"""Step 5: 跟隨 POST 後的 redirect，必要時輪詢 /ticket/check 等到 checkout。

成功的唯一定義：最終 URL 落在 /checkout。
"""
import re
import time

from curl_cffi import requests as cf_requests

import config
from tixcraftapi import BASE, alerts


def follow_order(session: cf_requests.Session, redirect_url: str,
                 headers: dict) -> str | None:
    """POST 成功後被 302 到某個 URL，follow 它並判斷結果。
    - 直接到 checkout → 成功
    - 到 /ticket/order → 進輪詢等 checkout
    - 其他 → 失敗
    """
    print(f"[ORDER] 跟隨 redirect: {redirect_url}")

    resp = session.get(redirect_url,
                       headers={**headers, "Referer": redirect_url},
                       allow_redirects=True)
    final_url = resp.url
    html = resp.text
    status = resp.status_code

    print(f"[ORDER] 最終 URL: {final_url} (HTTP {status})")
    if status == 403:
        alerts.play_403("ORDER")
        raise alerts.Blocked403("ORDER")

    if "checkout" in final_url:
        print("[ORDER] 已到結帳頁!")
        return final_url

    if "login" in final_url:
        print("[ORDER] Cookie 失效")
        return None

    if "activity" in final_url or "game" in final_url:
        print("[ORDER] 被踢回活動頁（售罄或異常）")
        return None

    if "/ticket/order" in final_url or "/order" in final_url:
        print("[ORDER] 進入 order 頁，開始輪詢等 checkout...")
        return _poll_order_loop(session, final_url, headers)

    print(f"[ORDER] 未知頁面，前 500 字:")
    clean = re.sub(r'<[^>]+>', ' ', html)
    print(clean[:500])
    return None


def _poll_order_loop(session: cf_requests.Session, order_url: str,
                     headers: dict) -> str | None:
    check_url = f"{BASE}/ticket/check"

    # 拓元 /ticket/check 只回 {waiting, message, time}，沒給排隊位置（已確認）。
    # time 是 server 建議的下次 polling 間隔（秒），過載時可能動態加大。
    queue_start = time.monotonic()
    next_interval = config.ORDER_POLL_INTERVAL

    for i in range(1, config.ORDER_POLL_MAX + 1):
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
                        print(f"[QUEUE] #{i} JSON 指示跳 {full}，但落點非 checkout: {resp.url}")
                        return None

                    resp = session.get(order_url, headers=headers, allow_redirects=True)
                    if "checkout" in resp.url:
                        print(f"[QUEUE] #{i} 已到結帳頁: {resp.url}")
                        return resp.url

                    print(f"[QUEUE] #{i} 排隊結束但沒跳到 checkout: {resp.url}")
                    return None

            except (ValueError, KeyError):
                body = check_resp.text[:200]
                print(f"  [{ts}] 輪詢 #{i} | HTTP {check_resp.status_code} | 非 JSON: {body}")

        except Exception as e:
            print(f"[QUEUE] #{i} 異常: {e}")

        # 尊重 server 給的 next interval（過載時會變大），fallback config.ORDER_POLL_INTERVAL
        time.sleep(next_interval)

    print("[QUEUE] 輪詢超時")
    return None
